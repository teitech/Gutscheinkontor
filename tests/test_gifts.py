"""Gutscheine weitergeben: per E-Mail-Link, Übernahme, Zurückholen, Vermerk, PDF, Export."""
import io
import re
import sqlite3
import time
from datetime import date

import pytest

from app import dates, gifts, mail, reminders
from app.config import get_settings
from app.db import connect
from tests.conftest import csrf_value, do_setup
from tests.test_reminders import mails as reminder_mails  # noqa: F401
from tests.test_transfer import doc, entry, import_zip, make_zip
from tests.test_vouchers import create_id, db_rows, post, upload
from tests.test_kinds import exported

CODE = "GEHEIM-4711"
FAR = "2099-12-31"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))


@pytest.fixture
def sent(monkeypatch):
    """Mailversand an, Mails landen in einer Liste."""
    outbox = []
    monkeypatch.setattr(mail, "is_enabled", lambda: True)
    monkeypatch.setattr(mail, "send_mail", lambda to, subject, body: outbox.append((to, subject, body)) or True)
    return outbox


@pytest.fixture
def me(client, clock):
    do_setup(client, clock)
    return client


def make(client, **fields):
    data = {"title": "Hagebau", "code": CODE, "value": "20", "expires_on": FAR, **fields}
    return create_id(client, **data)


def give(client, vid, recipients="anna@example.de, ben@example.de", message="", **extra):
    return post(client, f"/vouchers/{vid}/gift", recipients=recipients, message=message, **extra)


def tokens(outbox):
    """Persönliche Links aus den verschickten Mails: {Adresse: Token}."""
    result = {}
    for to, _subject, body in outbox:
        match = re.search(r"/gift/([A-Za-z0-9_-]+)", body)
        if match:
            result[to] = match.group(1)
    return result


def anonymous(client):
    """Neuer Client ohne Anmeldung, wie ein Empfänger im eigenen Browser."""
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, follow_redirects=False)


def claim(guest, token):
    return guest.post(f"/gift/{token}/claim", data={"csrf_token": csrf_value(guest)})


def gift_rows():
    return db_rows("SELECT * FROM voucher_gifts ORDER BY id")


# --- Eingaben ---------------------------------------------------------------------------


def test_adressen_zerlegen_und_doppelte_entfernen():
    emails, error = gifts.parse_recipients("a@example.de, B@example.de;\nb@EXAMPLE.de  c@example.de")
    assert error is None and emails == ["a@example.de", "B@example.de", "c@example.de"]


@pytest.mark.parametrize("text", ["", "   ", "kein-at", "a@b", "a@@b.de", "a b@c.de d", "x" * 300 + "@example.de"])
def test_ungueltige_adressen(text):
    emails, error = gifts.parse_recipients(text)
    assert emails == [] and error


def test_hoechstens_zehn_adressen():
    ok, _ = gifts.parse_recipients(", ".join(f"p{i}@example.de" for i in range(10)))
    assert len(ok) == 10
    emails, error = gifts.parse_recipients(", ".join(f"p{i}@example.de" for i in range(11)))
    assert emails == [] and "Höchstens" in error


def test_linkdauer_vierzehn_tage_oder_bis_zum_ablauf():
    now = 1_800_000_000
    assert gifts.link_expiry(None, now) == now + 14 * 24 * 3600
    assert gifts.link_expiry("2099-12-31", now) == now + 14 * 24 * 3600
    # Läuft der Gutschein früher ab, endet der Link am Ende dieses Tages (Europe/Berlin).
    end = gifts.link_expiry("2026-12-25", 1_798_000_000)  # jetzt: 23.12.2026
    assert end < 1_798_000_000 + 14 * 24 * 3600
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert datetime.fromtimestamp(end, ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M:%S") == "2026-12-25 23:59:59"


def test_nachricht_ohne_steuerzeichen_und_begrenzt():
    assert gifts.clean_text("Hallo\x00\x07 Welt\r\nZeile 2", 100) == "Hallo Welt\nZeile 2"
    assert len(gifts.clean_text("x" * 900, 500)) == 500


# --- Weitergeben ---------------------------------------------------------------------------


def test_weitergeben_verschickt_mails_ohne_code(me, sent):
    vid = make(me, notes="privat", shop_url="https://www.hagebau.de")
    r = give(me, vid, message="Viel Spaß!")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/vouchers/{vid}?gift=sent")
    assert [m[0] for m in sent] == ["anna@example.de", "ben@example.de"]
    for to, subject, body in sent:
        assert CODE not in body and CODE not in subject
        assert "Hagebau" in body and "Viel Spaß!" in body and "Anna gibt dir einen Gutschein weiter" in body
        assert "an 2 Personen" in body and "privat" not in body
    links = tokens(sent)
    assert len(set(links.values())) == 2  # jeder hat einen eigenen Link
    row = gift_rows()[0]
    assert (row["kind"], row["status"], row["recipient_count"]) == ("link", "open", 2)
    # In der Datenbank steht nur der Hash der Links.
    stored = [r["token_hash"] for r in db_rows("SELECT token_hash FROM gift_recipients")]
    assert all(t not in stored for t in links.values()) and len(stored) == 2


def test_hinweise_in_detail_und_liste(me, sent):
    vid = make(me)
    give(me, vid)
    detail = me.get(f"/vouchers/{vid}?gift=sent").text
    assert "Weitergegeben an 2 Personen" in detail and "Zurückholen" in detail
    assert "wurde per E-Mail weitergegeben" in detail
    assert "Angeboten an" in me.get("/").text
    assert "Hagebau" in me.get("/").text  # bleibt in der Liste der aktuellen Gutscheine


def test_nur_mit_mailserver(me, monkeypatch):
    monkeypatch.setattr(mail, "is_enabled", lambda: False)
    vid = make(me)
    r = give(me, vid)
    assert r.status_code == 400 and "nicht eingerichtet" in r.text
    assert gift_rows() == []
    assert "E-Mail-Versand eingerichtet sein" in me.get(f"/vouchers/{vid}").text


def test_ohne_code_eingeloest_oder_archiviert_nicht_moeglich(me, sent):
    no_code = make(me, code="")
    assert give(me, no_code).status_code == 400
    done = make(me)
    post(me, f"/vouchers/{done}/state", action="redeem_all")
    assert give(me, done).status_code == 400
    archived = make(me)
    post(me, f"/vouchers/{archived}/state", action="archive")
    assert give(me, archived).status_code == 400
    assert sent == [] and gift_rows() == []


def test_ungueltige_eingaben_zeigen_fehler_und_behalten_text(me, sent):
    vid = make(me)
    r = give(me, vid, recipients="anna@example.de, kaputt", message="Mein Text")
    assert r.status_code == 400 and "keine gültige E-Mail-Adresse" in r.text
    assert "anna@example.de, kaputt" in r.text and "Mein Text" in r.text
    assert give(me, vid, message="x" * 501).status_code == 400
    assert sent == []


def test_zweite_weitergabe_nur_nach_zurueckholen(me, sent):
    vid = make(me)
    give(me, vid)
    r = give(me, vid, recipients="carl@example.de")
    assert r.status_code == 400 and "schon weitergegeben" in r.text
    post(me, f"/vouchers/{vid}/gift/recall")
    assert give(me, vid, recipients="carl@example.de").status_code == 303
    assert [r["status"] for r in gift_rows()] == ["revoked", "open"]


def test_csrf_ist_pflicht(me, sent):
    vid = make(me)
    r = me.post(f"/vouchers/{vid}/gift", data={"recipients": "a@example.de", "csrf_token": "falsch"})
    assert r.status_code == 403 and gift_rows() == []
    assert me.post(f"/vouchers/{vid}/gift/recall", data={"csrf_token": "x"}).status_code == 403


def test_nur_angemeldete_koennen_weitergeben(client, clock, sent):
    do_setup(client, clock)
    vid = make(client)
    guest = anonymous(client)
    r = guest.post(f"/vouchers/{vid}/gift", data={"recipients": "a@example.de", "csrf_token": csrf_value(guest)})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert guest.get(f"/vouchers/{vid}/pdf").status_code == 303
    assert sent == [] and gift_rows() == []


def test_begrenzung_pro_stunde(me, sent):
    for i in range(10):
        vid = make(me, title=f"G{i}")
        assert give(me, vid, recipients=f"p{i}@example.de").status_code == 303
    vid = make(me, title="Elf")
    assert give(me, vid).status_code == 429


def test_mailfehler_nimmt_weitergabe_zurueck(me, monkeypatch):
    monkeypatch.setattr(mail, "is_enabled", lambda: True)
    monkeypatch.setattr(mail, "send_mail", lambda *a: False)
    vid = make(me)
    r = give(me, vid)
    assert r.status_code == 502 and "nicht verschickt" in r.text
    assert gift_rows() == [] and db_rows("SELECT * FROM gift_recipients") == []


def test_teilweise_verschickt(me, monkeypatch):
    calls = []

    def flaky(to, subject, body):
        calls.append(to)
        return len(calls) == 1  # nur die erste Mail geht raus

    monkeypatch.setattr(mail, "is_enabled", lambda: True)
    monkeypatch.setattr(mail, "send_mail", flaky)
    vid = make(me)
    r = give(me, vid, recipients="a@example.de b@example.de c@example.de")
    assert r.status_code == 303 and "gift=partial" in r.headers["location"]
    assert calls == ["a@example.de", "b@example.de"]  # nach dem ersten Fehler wird nicht weiter versucht
    assert gift_rows()[0]["recipient_count"] == 1
    assert [r["email"] for r in db_rows("SELECT email FROM gift_recipients")] == ["a@example.de"]


# --- Übernehmen ------------------------------------------------------------------------------


def test_link_zeigt_angebot_ohne_code_und_uebernimmt_nicht_per_get(me, sent):
    vid = make(me, shop_url="https://www.hagebau.de", online_only="on", min_order="50")
    give(me, vid, message="Für dich")
    token = tokens(sent)["anna@example.de"]
    guest = anonymous(me)
    page = guest.get(f"/gift/{token}")
    assert page.status_code == 200 and "Hagebau" in page.text and "Für dich" in page.text
    assert "Anna" in page.text and "Mindestbestellwert: 50,00 €" in page.text and "Nur online" in page.text
    assert CODE not in page.text and "Gutschein übernehmen" in page.text
    assert "noindex" in page.headers["x-robots-tag"] and page.headers["cache-control"] == "no-store"
    # Ein bloßes Öffnen (etwa durch einen Mail-Scanner) übernimmt nichts.
    guest.get(f"/gift/{token}")
    assert gift_rows()[0]["status"] == "open"


def test_wer_zuerst_uebernimmt_hat_den_gutschein(me, sent):
    vid = make(me)
    give(me, vid)
    links = tokens(sent)
    ana, ben = anonymous(me), anonymous(me)
    sent.clear()

    r = claim(ana, links["anna@example.de"])
    assert r.status_code == 303 and r.headers["location"] == f"/gift/{links['anna@example.de']}"
    page = ana.get(r.headers["location"])
    assert page.status_code == 200 and CODE in page.text and "Als PDF speichern" in page.text

    # Ben kommt zu spät.
    assert "nicht mehr verfügbar" in ben.get(f"/gift/{links['ben@example.de']}").text
    late = claim(ben, links["ben@example.de"])
    assert late.status_code == 404 and CODE not in late.text

    row = gift_rows()[0]
    assert (row["status"], row["given_to"]) == ("claimed", "anna@example.de")
    # Von den Empfängern bleibt nur die Person, die übernommen hat.
    assert [r["email"] for r in db_rows("SELECT email FROM gift_recipients")] == ["anna@example.de"]
    # Der Absender bekommt Bescheid, ohne Code.
    assert len(sent) == 1 and sent[0][0] == "anna@example.de"  # Absender Anna (Konto) bekommt Bescheid
    assert "übernommen" in sent[0][2] and CODE not in sent[0][2] and "anna@example.de" in sent[0][2]


def set_expiry(vid, expires_on):
    """Ablaufdatum direkt setzen (der Link läuft nach der echten Uhr, die Tests nach der festen)."""
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE vouchers SET expires_on = ?, expiry_set_on = '2026-09-01' WHERE id = ?", (expires_on, vid))
    conn.commit()
    conn.close()


def test_uebernahme_aendert_status_und_stoppt_erinnerungen(me, reminder_mails):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    set_expiry(vid, "2026-11-20")
    claim(anonymous(me), tokens(reminder_mails)["anna@example.de"])

    detail = me.get(f"/vouchers/{vid}").text
    assert "Übernommen von anna@example.de" in detail and "Weitergegeben" in detail
    assert "Hagebau" not in me.get("/").text  # nicht mehr bei den aktuellen
    assert "Hagebau" in me.get("/?status=weitergegeben").text
    assert "Hagebau" in me.get("/?status=alle").text

    from datetime import datetime
    from zoneinfo import ZoneInfo
    reminder_mails.clear()
    assert reminders.run_once(datetime(2026, 11, 17, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))) == 0
    assert reminder_mails == []


def test_ohne_erinnerung_vorher_mit_erinnerung(me, reminder_mails):
    """Gegenprobe: Ohne Weitergabe wäre der Gutschein erinnert worden."""
    vid = make(me, expires_on="2026-11-20")
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE vouchers SET expiry_set_on = '2026-09-01' WHERE id = ?", (vid,))
    conn.commit()
    conn.close()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert reminders.run_once(datetime(2026, 11, 17, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))) == 1


def test_gleichzeitige_uebernahme_nur_einer_gewinnt(me, sent):
    vid = make(me)
    give(me, vid)
    links = tokens(sent)
    db1, db2 = connect(), connect()
    try:
        first = gifts.claim(db1, links["anna@example.de"], "2026-11-17")
        second = gifts.claim(db2, links["ben@example.de"], "2026-11-17")
    finally:
        db1.close()
        db2.close()
    assert first is not None and second is None
    assert gift_rows()[0]["given_to"] == "anna@example.de"


def test_gewinner_link_bleibt_nutzbar_ohne_erneutes_uebernehmen(me, sent):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    token = tokens(sent)["anna@example.de"]
    ana = anonymous(me)
    claim(ana, token)
    again = claim(ana, token)  # Doppelklick oder Neuladen
    assert again.status_code == 303
    assert CODE in ana.get(f"/gift/{token}").text
    assert len(gift_rows()) == 1


def test_uebernahme_braucht_csrf(me, sent):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    token = tokens(sent)["anna@example.de"]
    r = anonymous(me).post(f"/gift/{token}/claim", data={"csrf_token": "falsch"})
    assert r.status_code == 403 and gift_rows()[0]["status"] == "open"


def test_unbekannter_link(me):
    guest = anonymous(me)
    assert guest.get("/gift/" + "x" * 43).status_code == 404
    assert guest.get("/gift/" + "x" * 500).status_code == 404
    assert guest.get("/gift/" + "x" * 43 + "/pdf").status_code == 404


def test_zu_viele_falsche_links_werden_gebremst(me):
    guest = anonymous(me)
    codes = [guest.get(f"/gift/falsch{i}").status_code for i in range(32)]
    assert codes[:30] == [404] * 30 and codes[30] == 429


def test_abgelaufener_link(me, sent):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    token = tokens(sent)["anna@example.de"]
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE voucher_gifts SET expires_at = ?", (int(time.time()) - 5,))
    conn.commit()
    conn.close()
    guest = anonymous(me)
    assert guest.get(f"/gift/{token}").status_code == 404
    assert claim(guest, token).status_code == 404
    # Der Gutschein bleibt offen, mit Hinweis, und lässt sich erneut weitergeben.
    detail = me.get(f"/vouchers/{vid}").text
    assert "Links sind abgelaufen" in detail and "Zuletzt weitergegeben an 1 Personen" in detail
    assert "Angeboten an" not in me.get("/").text
    assert give(me, vid, recipients="ben@example.de").status_code == 303
    assert [r["status"] for r in gift_rows()] == ["expired", "open"]


def test_aufraeumen_loescht_adressen(me, sent):
    vid = make(me)
    give(me, vid)
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE voucher_gifts SET expires_at = ?", (int(time.time()) - 5,))
    conn.commit()
    conn.close()
    db = connect()
    try:
        gifts.sweep(db)
    finally:
        db.close()
    assert db_rows("SELECT * FROM gift_recipients") == []
    assert gift_rows()[0]["status"] == "expired"


def test_zugang_nach_uebernahme_endet(me, sent):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    token = tokens(sent)["anna@example.de"]
    ana = anonymous(me)
    claim(ana, token)
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE voucher_gifts SET claimed_at = ?", (int(time.time()) - gifts.ACCESS_TTL - 5,))
    conn.commit()
    conn.close()
    assert ana.get(f"/gift/{token}").status_code == 404
    db = connect()
    try:
        gifts.sweep(db)
    finally:
        db.close()
    assert db_rows("SELECT * FROM gift_recipients") == []
    assert gift_rows()[0]["given_to"] == "anna@example.de"  # der Vermerk beim Absender bleibt


# --- Zurückholen und Nebenwirkungen -------------------------------------------------------------


def test_zurueckholen_macht_links_ungueltig(me, sent):
    vid = make(me)
    give(me, vid)
    links = tokens(sent)
    r = post(me, f"/vouchers/{vid}/gift/recall")
    assert r.status_code == 303 and "gift=recalled" in r.headers["location"]
    guest = anonymous(me)
    assert guest.get(f"/gift/{links['anna@example.de']}").status_code == 404
    assert claim(guest, links["ben@example.de"]).status_code == 404
    assert gift_rows()[0]["status"] == "revoked" and db_rows("SELECT * FROM gift_recipients") == []
    detail = me.get(f"/vouchers/{vid}").text
    assert "wieder zurückgeholt" in detail and "Per E-Mail weitergeben" in detail


@pytest.mark.parametrize("action", ["redeem_all", "archive"])
def test_erledigen_oder_archivieren_holt_weitergabe_zurueck(me, sent, action):
    vid = make(me)
    give(me, vid)
    token = tokens(sent)["anna@example.de"]
    post(me, f"/vouchers/{vid}/state", action=action)
    assert anonymous(me).get(f"/gift/{token}").status_code == 404
    assert gift_rows()[0]["status"] == "revoked"


def test_vollstaendig_einloesen_holt_weitergabe_zurueck(me, sent):
    vid = make(me)
    give(me, vid)
    token = tokens(sent)["anna@example.de"]
    post(me, f"/vouchers/{vid}/redeem", amount="5")  # teilweise: Weitergabe bleibt
    assert gift_rows()[0]["status"] == "open"
    post(me, f"/vouchers/{vid}/redeem", amount="15")
    assert gift_rows()[0]["status"] == "revoked"
    assert anonymous(me).get(f"/gift/{token}").status_code == 404


def test_loeschen_entfernt_weitergabe(me, sent):
    vid = make(me)
    give(me, vid)
    token = tokens(sent)["anna@example.de"]
    post(me, f"/vouchers/{vid}/delete")
    assert gift_rows() == [] and db_rows("SELECT * FROM gift_recipients") == []
    assert anonymous(me).get(f"/gift/{token}").status_code == 404


# --- Persönlich verschenkt ---------------------------------------------------------------------


def test_verschenkt_vermerken_und_zuruecknehmen(me):
    vid = make(me)
    r = post(me, f"/vouchers/{vid}/gift/given", given_to="Oma Erna")
    assert r.status_code == 303 and "gift=given" in r.headers["location"]
    detail = me.get(f"/vouchers/{vid}").text
    assert "Verschenkt" in detail and "Oma Erna" in detail and "Doch behalten" in detail
    assert "Hagebau" not in me.get("/").text
    r = post(me, f"/vouchers/{vid}/gift/undo")
    assert r.status_code == 303 and gift_rows() == []
    assert "Hagebau" in me.get("/").text


def test_verschenkt_ohne_namen_und_mit_langem_text(me):
    vid = make(me)
    post(me, f"/vouchers/{vid}/gift/given", given_to="x" * 300 + "\n<script>")
    assert len(gift_rows()[0]["given_to"]) == 100


def test_verschenkt_nicht_bei_laufender_weitergabe(me, sent):
    vid = make(me)
    give(me, vid)
    r = post(me, f"/vouchers/{vid}/gift/given", given_to="Oma")
    assert r.status_code == 400 and "Hole sie zuerst zurück" in r.text
    assert len(gift_rows()) == 1


def test_vermerk_nach_uebernahme_per_link_nicht_zuruecknehmbar(me, sent):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    claim(anonymous(me), tokens(sent)["anna@example.de"])
    assert post(me, f"/vouchers/{vid}/gift/undo").status_code == 400
    assert gift_rows()[0]["status"] == "claimed"
    assert "Doch behalten" not in me.get(f"/vouchers/{vid}").text


# --- PDF und Anhänge ------------------------------------------------------------------------------


def test_pdf_fuer_angemeldete(me):
    vid = make(me, title="Café Müller – 10 € 😀", benefit_text="Gratis", kind="amount")
    r = me.get(f"/vouchers/{vid}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "private, no-store"
    assert me.get("/vouchers/99999/pdf").status_code == 404


def test_pdf_fuer_alle_arten_und_ohne_code(me):
    for fields in ({"kind": "percent", "percent": "10", "value": ""},
                   {"kind": "other", "benefit_text": "Gratis-Versand", "value": ""},
                   {"code": "", "min_order": "50", "online_only": "on", "shop_url": "www.hagebau.de"}):
        vid = make(me, **fields)
        assert me.get(f"/vouchers/{vid}/pdf").content.startswith(b"%PDF")


def test_pdf_mit_logo_und_langem_code(me):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, "PNG")
    vid = make(me, code="A" * 400)
    r = me.post(f"/vouchers/{vid}/logo", data={"csrf_token": csrf_value(me)},
                files={"file": ("l.png", buf.getvalue(), "image/png")})
    assert r.status_code == 303
    assert me.get(f"/vouchers/{vid}/pdf").content.startswith(b"%PDF")


def test_empfaenger_pdf_und_anhang_erst_nach_uebernahme(me, sent):
    vid = make(me)
    upload(me, vid, b"%PDF-1.4 original", name="shop.pdf", ctype="application/pdf")
    attachment_id = db_rows("SELECT id FROM voucher_attachments")[0]["id"]
    give(me, vid)
    links = tokens(sent)
    ana, ben = anonymous(me), anonymous(me)
    token = links["anna@example.de"]

    # Vorher: weder PDF noch Anhang.
    assert ana.get(f"/gift/{token}/pdf").status_code == 404
    assert ana.get(f"/gift/{token}/files/{attachment_id}").status_code == 404
    assert "shop.pdf" not in ana.get(f"/gift/{token}").text

    claim(ana, token)
    page = ana.get(f"/gift/{token}").text
    assert "shop.pdf" in page and f"/gift/{token}/files/{attachment_id}" in page
    pdf = ana.get(f"/gift/{token}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    file = ana.get(f"/gift/{token}/files/{attachment_id}")
    assert file.status_code == 200 and file.content == b"%PDF-1.4 original"
    assert file.headers["content-security-policy"] == "default-src 'none'; sandbox"

    # Wer nicht übernommen hat, bekommt nichts.
    other = links["ben@example.de"]
    assert ben.get(f"/gift/{other}/pdf").status_code == 404
    assert ben.get(f"/gift/{other}/files/{attachment_id}").status_code == 404
    assert ana.get(f"/gift/{token}/files/999999").status_code == 404


def test_anhang_eines_anderen_gutscheins_nicht_abrufbar(me, sent):
    other = make(me, title="Anderer")
    upload(me, other, b"%PDF-1.4 fremd", name="fremd.pdf", ctype="application/pdf")
    foreign_id = db_rows("SELECT id FROM voucher_attachments")[0]["id"]
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    token = tokens(sent)["anna@example.de"]
    ana = anonymous(me)
    claim(ana, token)
    assert ana.get(f"/gift/{token}/files/{foreign_id}").status_code == 404


def test_code_seite_mit_unlesbarem_code(me, sent, monkeypatch):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    token = tokens(sent)["anna@example.de"]
    ana = anonymous(me)
    claim(ana, token)
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE vouchers SET code_enc = 'kaputt' WHERE id = ?", (vid,))
    conn.commit()
    conn.close()
    page = ana.get(f"/gift/{token}")
    assert page.status_code == 200 and "nicht lesbar" in page.text


def test_angebot_bei_inzwischen_abgelaufenem_gutschein(me, sent, monkeypatch):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    set_expiry(vid, "2026-11-18")
    token = tokens(sent)["anna@example.de"]
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 19))
    guest = anonymous(me)
    assert guest.get(f"/gift/{token}").status_code == 404
    assert claim(guest, token).status_code == 404
    assert gift_rows()[0]["status"] == "open"


# --- Export und Import ------------------------------------------------------------------------------


def test_export_enthaelt_vermerk_und_import_stellt_ihn_wieder_her(me, sent):
    vid = make(me)
    give(me, vid, recipients="anna@example.de")
    claim(anonymous(me), tokens(sent)["anna@example.de"])
    _raw, document = exported(me)
    assert document["vouchers"][0]["gift"]["given_to"] == "anna@example.de"

    # CSV zeigt den Status.
    from app import transfer
    db = connect()
    try:
        csv_bytes = transfer.build_csv(db, include_codes=False).decode("utf-8-sig")
    finally:
        db.close()
    assert "Weitergegeben" in csv_bytes

    r = import_zip(me, make_zip(doc(entry(title="Importiert", gift={"given_to": "Oma", "claimed_at": "2026-11-01T10:00:00Z"}))))
    assert r.status_code == 200
    rows = db_rows("SELECT g.* FROM voucher_gifts g JOIN vouchers v ON v.id = g.voucher_id WHERE v.title = 'Importiert'")
    assert len(rows) == 1 and (rows[0]["kind"], rows[0]["status"], rows[0]["given_to"]) == ("manual", "claimed", "Oma")
    assert "Hagebau" not in me.get("/").text and "Importiert" not in me.get("/").text


def test_import_ignoriert_kaputten_vermerk(me):
    for bad in ("x", 5, ["a"], {"given_to": 5, "claimed_at": "gestern"}):
        r = import_zip(me, make_zip(doc(entry(title=f"G{bad}"[:10], gift=bad))))
        assert r.status_code == 200
    assert all(len(r["given_to"]) <= 100 for r in gift_rows())
