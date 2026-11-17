import asyncio
import dataclasses
import sqlite3
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import pytest

from app import config, dates, mail, reminders
from app.config import get_settings
from tests.conftest import csrf_value, do_setup
from tests.test_vouchers import create_id, db_rows, post

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=BERLIN)
EXPIRES = "2026-12-12"  # am 17.11. noch 25 Tage


def at(days: int, hour: int = 9, minute: int = 0) -> datetime:
    return (NOW + timedelta(days=days)).replace(hour=hour, minute=minute)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))


@pytest.fixture
def mails(monkeypatch):
    """Erinnerungen und Mailversand eingeschaltet, Mails landen in einer Liste."""
    sent = []
    base = get_settings()
    monkeypatch.setattr(reminders, "get_settings", lambda: dataclasses.replace(base, reminders_enabled=True))
    monkeypatch.setattr(mail, "is_enabled", lambda: True)

    def fake_send(to, subject, body):
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(mail, "send_mail", fake_send)
    return sent


@pytest.fixture
def me(client, clock):
    do_setup(client, clock)
    return client


def make(client, title="Müller Möbel", expires_on=EXPIRES, set_on="2026-09-01", **fields):
    """Gutschein anlegen. Das Datum, an dem das Ablaufdatum gesetzt wurde, liegt weit zurück."""
    vid = create_id(client, title=title, expires_on=expires_on or "", **fields)
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE vouchers SET expiry_set_on = ? WHERE id = ?", (set_on, vid))
    conn.commit()
    conn.close()
    return vid


def sql(statement, args=()):
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute(statement, args)
    conn.commit()
    conn.close()


def add_user(email, name, reminders_enabled=1) -> int:
    conn = sqlite3.connect(get_settings().db_path)
    cur = conn.execute(
        "INSERT INTO users (email, display_name, password_hash, reminders_enabled) VALUES (?,?,?,?)",
        (email, name, "x", reminders_enabled),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


# --- Reine Funktionen ---------------------------------------------------------------


@pytest.mark.parametrize("days_left,stage", [
    (45, None), (31, None), (30, 30), (25, 30), (8, 30), (7, 7), (2, 7), (1, 1), (0, 1),
])
def test_aktuelle_stufe(days_left, stage):
    assert reminders.current_stage(days_left, (30, 7, 1)) == stage


def test_tage_aus_der_umgebung_lesen():
    assert config._parse_days("30, 7;1,7") == (30, 7, 1)
    assert config._parse_days("") == ()
    assert config._parse_days("dreissig") == ()
    bad = dataclasses.replace(get_settings(), reminder_days=())
    with pytest.raises(RuntimeError):
        bad.validate()
    with pytest.raises(RuntimeError):
        dataclasses.replace(get_settings(), reminder_hour=24).validate()


# --- Auslösen -----------------------------------------------------------------------


def test_erste_stufe_wird_einmal_geschickt(me, mails):
    make(me, code="GEHEIMCODE-42", value="30", notes="Notiz")
    assert reminders.run_once(at(0)) == 1
    to, subject, body = mails[0]
    assert to == "anna@example.de"
    assert subject == "Gutscheinkontor: Ein Gutschein läuft bald ab"
    assert "Müller Möbel" in body and "läuft in 25 Tagen ab" in body and "12.12.2026" in body
    assert "Restwert 30,00 €" in body
    assert f"{get_settings().base_url}/vouchers/1" in body
    assert f"{get_settings().base_url}/settings" in body
    assert "GEHEIMCODE-42" not in body  # der Code steht nie in der Mail
    assert reminders.run_once(at(0)) == 0 and reminders.run_once(at(0, hour=15)) == 0
    assert len(mails) == 1


def test_alle_drei_stufen_nacheinander(me, mails):
    make(me)
    assert reminders.run_once(at(0)) == 1    # 25 Tage: Stufe 30
    assert reminders.run_once(at(10)) == 0   # 15 Tage: gleiche Stufe, schon geschickt
    assert reminders.run_once(at(19)) == 1   # 6 Tage: Stufe 7
    assert reminders.run_once(at(24)) == 1   # 1 Tag: Stufe 1
    assert "läuft morgen ab" in mails[-1][2]
    assert reminders.run_once(at(25)) == 0   # heute: noch Stufe 1, schon geschickt
    assert reminders.run_once(at(26)) == 0   # abgelaufen
    assert len(mails) == 3


def test_nach_einer_pause_nur_die_aktuelle_stufe(me, mails):
    make(me)
    assert reminders.run_once(at(23)) == 1   # erst bei 2 Tagen wieder an: Stufe 7, kein Schwall
    assert len(mails) == 1 and "läuft in 2 Tagen ab" in mails[0][2]


def test_nichts_zu_frueh_zu_spaet_oder_erledigt(me, mails):
    make(me, "Zu früh", "2027-01-01")
    make(me, "Abgelaufen", "2026-11-16")
    make(me, "Ohne Datum", "")
    done = make(me, "Eingelöst", EXPIRES, value="10")
    post(me, f"/vouchers/{done}/state", action="redeem_all")
    archived = make(me, "Archiviert", EXPIRES)
    post(me, f"/vouchers/{archived}/state", action="archive")
    assert reminders.run_once(at(0)) == 0 and mails == []


def test_neu_angelegt_ueberspringt_vergangene_stufen(me, mails):
    make(me, expires_on="2026-12-07", set_on="2026-11-17")  # heute mit 20 Tagen Restlaufzeit angelegt
    assert reminders.run_once(at(0)) == 0   # keine 30-Tage-Mail für etwas, das gerade erst angelegt wurde
    assert reminders.run_once(at(14)) == 1  # 6 Tage vor Ablauf: Stufe 7 kommt
    assert reminders.run_once(at(19)) == 1  # 1 Tag vor Ablauf: Stufe 1 kommt


def test_geaendertes_datum_beginnt_neu(me, mails):
    vid = make(me, set_on="2026-09-01")
    assert reminders.run_once(at(0)) == 1                    # Stufe 30 für den 12.12.
    assert post(me, f"/vouchers/{vid}/edit", title="Müller Möbel", expires_on="2027-01-20").status_code == 303
    assert db_rows("SELECT expiry_set_on FROM vouchers")[0]["expiry_set_on"] == "2026-11-17"
    assert reminders.run_once(at(0)) == 0                    # 64 Tage: zu früh
    assert reminders.run_once(at(40)) == 1                   # 24 Tage: Stufe 30 fürs neue Datum
    assert reminders.run_once(at(40)) == 0
    # Ohne Änderung des Datums bleibt das Setzdatum unverändert.
    post(me, f"/vouchers/{vid}/edit", title="Umbenannt", expires_on="2027-01-20")
    assert db_rows("SELECT expiry_set_on FROM vouchers")[0]["expiry_set_on"] == "2026-11-17"


# --- Empfänger ----------------------------------------------------------------------


def test_zugeordnet_nur_an_die_person_sonst_an_alle(me, mails):
    ben = add_user("ben@example.de", "Ben")
    make(me, "Annas Gutschein", owner="1")
    make(me, "Gemeinsamer Gutschein", expires_on="2026-12-13")
    assert reminders.run_once(at(0)) == 2
    by_user = {to: body for to, _, body in mails}
    assert set(by_user) == {"anna@example.de", "ben@example.de"}
    assert "Annas Gutschein" in by_user["anna@example.de"] and "Gemeinsamer Gutschein" in by_user["anna@example.de"]
    assert "Gemeinsamer Gutschein" in by_user["ben@example.de"] and "Annas Gutschein" not in by_user["ben@example.de"]
    assert ben


def test_wer_abgeschaltet_hat_bekommt_nichts(me, mails):
    add_user("ben@example.de", "Ben", reminders_enabled=0)
    make(me, "Gemeinsam")
    assert reminders.run_once(at(0)) == 1
    assert [to for to, _, _ in mails] == ["anna@example.de"]
    # Gehört der Gutschein Ben, der abgeschaltet hat, meldet sich niemand.
    mails.clear()
    make(me, "Bens Gutschein", owner="2", expires_on="2026-12-13")
    assert reminders.run_once(at(0, hour=10)) == 0


def test_sammelmail_pro_person(me, mails):
    make(me, "Später", "2026-12-15")
    make(me, "Zuerst", "2026-12-10")
    assert reminders.run_once(at(0)) == 1
    to, subject, body = mails[0]
    assert subject == "Gutscheinkontor: 2 Gutscheine laufen bald ab"
    assert body.index("Zuerst") < body.index("Später")  # der früheste zuerst


def test_betreff_enthaelt_keine_nutzereingaben(me, mails):
    vid = make(me)
    sql("UPDATE vouchers SET title = ? WHERE id = ?", ("Böse\nBcc: fremd@example.de", vid))
    assert reminders.run_once(at(0)) == 1
    message = EmailMessage()
    message["Subject"] = mails[0][1]  # wirft bei Zeilenumbruch einen Fehler
    assert "\n" not in mails[0][1] and "Bcc" not in mails[0][1]


# --- Ausfälle und Schalter ------------------------------------------------------------


def test_fehlgeschlagener_versand_wird_wiederholt(me, mails, monkeypatch):
    make(me)
    monkeypatch.setattr(mail, "send_mail", lambda *a: False)
    assert reminders.run_once(at(0)) == 0
    assert db_rows("SELECT COUNT(*) AS n FROM reminders_sent")[0]["n"] == 0  # nichts vermerkt
    monkeypatch.setattr(mail, "send_mail", lambda to, s, b: mails.append((to, s, b)) or True)
    assert reminders.run_once(at(0, hour=10)) == 1
    assert db_rows("SELECT COUNT(*) AS n FROM reminders_sent")[0]["n"] == 1


def test_nichts_ohne_mailserver_oder_wenn_ausgeschaltet(me, mails, monkeypatch):
    make(me)
    monkeypatch.setattr(mail, "is_enabled", lambda: False)
    assert reminders.run_once(at(0)) == 0
    monkeypatch.setattr(mail, "is_enabled", lambda: True)
    base = get_settings()
    monkeypatch.setattr(reminders, "get_settings", lambda: dataclasses.replace(base, reminders_enabled=False))
    assert reminders.run_once(at(0)) == 0
    assert mails == []


def test_erst_ab_der_eingestellten_uhrzeit(me, mails):
    make(me)
    assert reminders.run_once(at(0, hour=7, minute=59)) == 0
    assert reminders.run_once(at(0, hour=8, minute=0)) == 1


def test_eigene_stufen_aus_der_konfiguration(me, mails, monkeypatch):
    base = get_settings()
    monkeypatch.setattr(reminders, "get_settings",
                        lambda: dataclasses.replace(base, reminders_enabled=True, reminder_days=(14,)))
    make(me)
    assert reminders.run_once(at(0)) == 0   # 25 Tage: noch keine Stufe
    assert reminders.run_once(at(11)) == 1  # 14 Tage
    assert reminders.run_once(at(20)) == 0


def test_loeschen_raeumt_vermerke_auf(me, mails):
    vid = make(me)
    reminders.run_once(at(0))
    assert db_rows("SELECT COUNT(*) AS n FROM reminders_sent")[0]["n"] == 1
    post(me, f"/vouchers/{vid}/delete")
    assert db_rows("SELECT COUNT(*) AS n FROM reminders_sent")[0]["n"] == 0


# --- Hintergrundlauf -----------------------------------------------------------------------


def test_planer_laeuft_regelmaessig_und_stoppt(monkeypatch):
    calls = []
    monkeypatch.setattr(mail, "is_enabled", lambda: True)
    monkeypatch.setattr(reminders, "run_once", lambda: calls.append(1) or 0)

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(reminders.scheduler(stop, tick=0.01))
        await asyncio.sleep(0.15)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(main())
    assert len(calls) >= 2


def test_planer_ueberlebt_fehler_ohne_details_im_log(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(mail, "is_enabled", lambda: True)

    def boom():
        calls.append(1)
        raise RuntimeError("anna@example.de geheim")

    monkeypatch.setattr(reminders, "run_once", boom)

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(reminders.scheduler(stop, tick=0.01))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(main())
    assert len(calls) >= 2
    assert "RuntimeError" in caplog.text and "anna@example.de" not in caplog.text


def test_planer_startet_nicht_ohne_mailserver(monkeypatch):
    monkeypatch.setattr(mail, "is_enabled", lambda: False)

    async def main():
        # Kehrt sofort zurück, ohne dass jemand `stop` setzt.
        await asyncio.wait_for(reminders.scheduler(asyncio.Event(), tick=0.01), timeout=2)

    asyncio.run(main())


# --- Einstellungen -------------------------------------------------------------------------


def test_einstellungen_erinnerungen_ein_und_ausschalten(me):
    page = me.get("/settings")
    assert page.status_code == 200 and "anna@example.de" in page.text and "checked" in page.text
    assert post(me, "/settings").status_code == 303                     # Haken fehlt = aus
    assert db_rows("SELECT reminders_enabled FROM users")[0]["reminders_enabled"] == 0
    assert "checked" not in me.get("/settings").text
    assert post(me, "/settings", reminders="on").status_code == 303
    assert db_rows("SELECT reminders_enabled FROM users")[0]["reminders_enabled"] == 1
    assert "Gespeichert" in me.get("/settings?saved=1").text


def test_einstellungen_brauchen_login_und_csrf(me):
    assert me.post("/settings", data={"reminders": "on"}).status_code == 403
    assert me.post("/settings/test-mail").status_code == 403
    me.cookies.clear()
    assert me.get("/settings").headers["location"] == "/login"


def test_hinweis_wenn_kein_mailserver(me, monkeypatch):
    monkeypatch.setattr(mail, "is_enabled", lambda: False)
    page = me.get("/settings")
    assert "nicht eingerichtet" in page.text and "Testmail" not in page.text
    assert post(me, "/settings/test-mail").status_code == 503


def test_testmail(me, mails):
    r = post(me, "/settings/test-mail")
    assert r.status_code == 200 and "Testmail wurde verschickt" in r.text
    assert mails[0][0] == "anna@example.de" and mails[0][1] == "Gutscheinkontor: Testmail"
    assert post(me, "/settings/test-mail").status_code == 200
    assert post(me, "/settings/test-mail").status_code == 200
    assert post(me, "/settings/test-mail").status_code == 429  # höchstens 3 pro Stunde
    assert len(mails) == 3


def test_testmail_fehler_wird_angezeigt(me, mails, monkeypatch):
    monkeypatch.setattr(mail, "send_mail", lambda *a: False)
    r = post(me, "/settings/test-mail")
    assert r.status_code == 502 and "nicht angenommen" in r.text
