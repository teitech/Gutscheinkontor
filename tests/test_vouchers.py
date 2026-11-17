import dataclasses
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app import attachments, dates, formatting, routes_vouchers, vouchers
from app.config import get_settings
from tests.conftest import csrf_value, do_setup

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
TODAY = date(2026, 11, 17)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: TODAY)


@pytest.fixture
def me(client, clock):
    """Eingeloggter Client (Ersteinrichtung abgeschlossen)."""
    do_setup(client, clock)
    return client


def create(client, **fields):
    data = {"title": "Testgutschein", **fields, "csrf_token": csrf_value(client)}
    return client.post("/vouchers", data=data)


def create_id(client, **fields) -> int:
    r = create(client, **fields)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def post(client, url, **fields):
    return client.post(url, data={"csrf_token": csrf_value(client), **fields})


def upload(client, vid, content, name="datei.png", ctype="image/png"):
    return client.post(
        f"/vouchers/{vid}/attachments",
        data={"csrf_token": csrf_value(client)},
        files={"file": (name, content, ctype)},
    )


def db_rows(sql, args=()):
    conn = sqlite3.connect(get_settings().db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def order(text, *titles):
    positions = [text.index(t) for t in titles]
    return positions == sorted(positions)


# --- Reine Funktionen ---------------------------------------------------------------


@pytest.mark.parametrize("text,cents", [
    ("25", 2500), ("25,5", 2550), ("1.234,50", 123450), ("12.50 €", 1250),
    ("0", 0), ("", None), ("  ", None),
])
def test_parse_money_ok(text, cents):
    assert vouchers.parse_money(text) == cents


@pytest.mark.parametrize("text", ["abc", "1,234", "-5", "1,2,3", "25 Euro", "99999999999"])
def test_parse_money_fehler(text):
    with pytest.raises(ValueError):
        vouchers.parse_money(text)


def test_parse_date():
    assert vouchers.parse_date("2026-12-31") == "2026-12-31"
    assert vouchers.parse_date("31.12.2026") == "2026-12-31"
    assert vouchers.parse_date("") is None
    for bad in ("2026-13-01", "31.02.2026", "morgen", "1999-01-01", "2101-01-01"):
        with pytest.raises(ValueError):
            vouchers.parse_date(bad)


def test_parse_tags():
    assert vouchers.parse_tags("Essen, essen ;  Kleidung ,, ") == ["Essen", "Kleidung"]
    with pytest.raises(ValueError):
        vouchers.parse_tags("x" * 31)
    with pytest.raises(ValueError):
        vouchers.parse_tags(",".join(f"t{i}" for i in range(11)))


def test_formatierung():
    assert formatting.format_euro(123456) == "1.234,56 €"
    assert formatting.format_euro(5) == "0,05 €"
    assert formatting.format_date("2026-03-07") == "07.03.2026"
    assert formatting.expiry_text("2026-11-17", TODAY) == "Läuft heute ab"
    assert formatting.expiry_text("2026-11-18", TODAY) == "Läuft morgen ab"
    assert formatting.expiry_text("2026-11-27", TODAY) == "Noch 10 Tage"
    assert formatting.expiry_text("2026-11-16", TODAY) == "Seit 1 Tag abgelaufen"
    assert formatting.expiry_text("2026-11-10", TODAY) == "Seit 7 Tagen abgelaufen"
    assert formatting.expiry_text(None, TODAY) == "Kein Ablaufdatum"
    assert formatting.urgency("2026-11-20", TODAY, False) == "soon"
    assert formatting.urgency("2026-11-20", TODAY, True) == "none"


def test_dateiname_wird_bereinigt():
    assert attachments.clean_filename("../../etc/passwd.png", "image/png") == "passwd.png"
    assert attachments.clean_filename("C:\\Users\\x\\scan.pdf", "application/pdf") == "scan.pdf"
    assert attachments.clean_filename("", "image/png") == "anhang.png"
    assert attachments.path_for("../geheim") is None
    assert attachments.path_for("0" * 32) is not None


# --- Anlegen, Anzeigen, Prüfen ----------------------------------------------------------


def test_leere_liste(me):
    assert "Noch keine Gutscheine" in me.get("/").text


def test_anlegen_und_anzeigen(me):
    vid = create_id(me, title="Müller Möbel", code="ABC-123-XYZ", value="25", expires_on="2026-12-24",
                    notes="Nur im Laden", tags="Möbel, Wohnen", owner="1")
    assert vid == 1
    listing = me.get("/").text
    assert "Müller Möbel" in listing and "25,00 €" in listing and "24.12.2026" in listing
    detail = me.get(f"/vouchers/{vid}").text
    assert "ABC-123-XYZ" in detail and "Nur im Laden" in detail and "Wohnen" in detail and "Anna" in detail


def test_code_nicht_im_klartext_in_der_db(me):
    create_id(me, code="GEHEIMER-CODE-999")
    row = db_rows("SELECT code_enc FROM vouchers")[0]
    assert row["code_enc"] and "GEHEIMER-CODE-999" not in row["code_enc"]


def test_validierungsfehler(me):
    r = create(me, title="")
    assert r.status_code == 400 and "Bitte gib einen Namen" in r.text
    assert create(me, value="abc").status_code == 400
    assert create(me, expires_on="31.02.2026").status_code == 400
    assert create(me, remaining="5").status_code == 400  # Restwert ohne Wert
    assert create(me, value="10", remaining="20").status_code == 400
    assert create(me, owner="99").status_code == 400
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0


def test_formular_behaelt_eingaben_bei_fehlern(me):
    r = create(me, title="Eingabe bleibt", value="abc")
    assert r.status_code == 400 and 'value="Eingabe bleibt"' in r.text


def test_nicht_lesbarer_code_stuerzt_nicht_ab(me):
    vid = create_id(me, code="ABC")
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE vouchers SET code_enc = 'kaputt'")
    conn.commit()
    conn.close()
    r = me.get(f"/vouchers/{vid}")
    assert r.status_code == 200 and "nicht lesbar" in r.text


# --- Liste: Status, Suche, Filter, Sortierung -----------------------------------------------


def test_status_filter_und_sortierung(me):
    create_id(me, title="Spaeter", expires_on="2027-01-01")
    create_id(me, title="Bald", expires_on="2026-11-20")
    create_id(me, title="OhneDatum")
    create_id(me, title="Vorbei", expires_on="2026-11-01")

    default = me.get("/").text
    assert "Vorbei" not in default
    assert order(default, "Bald", "Spaeter", "OhneDatum")  # früheste zuerst, ohne Datum zuletzt

    expired = me.get("/?status=abgelaufen").text
    assert "Vorbei" in expired and "Bald" not in expired and "Seit 16 Tagen abgelaufen" in expired
    assert "Vorbei" in me.get("/?status=alle").text
    assert order(me.get("/?sort=titel").text, "Bald", "OhneDatum", "Spaeter")
    # Unbekannte Werte fallen auf die Standardwerte zurück.
    assert me.get("/?status=quatsch&sort=quatsch").status_code == 200


def test_suche_ignoriert_gross_klein_und_umlaute_nicht(me):
    create_id(me, title="Müller Möbel", notes="Sofa")
    create_id(me, title="Bäckerei", tags="Frühstück")
    assert "Müller Möbel" in me.get("/?q=MÜLLER").text
    assert "Müller Möbel" in me.get("/?q=möbel sofa").text
    assert "Bäckerei" in me.get("/?q=frühstück").text
    assert "Müller Möbel" not in me.get("/?q=bäcker").text
    assert "Keine Gutscheine für diese Auswahl" in me.get("/?q=gibtesnicht").text


def test_filter_nach_schlagwort_und_person(me):
    create_id(me, title="MitTag", tags="Essen", owner="1")
    create_id(me, title="OhneTag", owner="")
    assert "OhneTag" not in me.get("/?tag=essen").text
    assert "MitTag" in me.get("/?tag=essen").text
    assert "MitTag" not in me.get("/?owner=none").text
    assert "OhneTag" in me.get("/?owner=none").text
    assert "OhneTag" not in me.get("/?owner=1").text


def test_zusammenfassung(me):
    create_id(me, title="A", value="50")
    create_id(me, title="B", value="20,50")
    create_id(me, title="Alt", value="100", expires_on="2026-01-01")  # abgelaufen zählt nicht
    assert "2 offene Gutscheine" in me.get("/").text
    assert "70,50 €" in me.get("/").text


# --- Einlösen und Zustand ----------------------------------------------------------------------


def test_teilbetrag_einloesen(me):
    vid = create_id(me, title="Teil", value="50")
    assert post(me, f"/vouchers/{vid}/redeem", amount="20").status_code == 303
    detail = me.get(f"/vouchers/{vid}").text
    assert "30,00 €" in detail and "Teilweise eingelöst" in detail

    r = post(me, f"/vouchers/{vid}/redeem", amount="40")
    assert r.status_code == 400 and "nur noch 30,00 €" in r.text
    assert post(me, f"/vouchers/{vid}/redeem", amount="0").status_code == 400
    assert post(me, f"/vouchers/{vid}/redeem", amount="abc").status_code == 400
    assert post(me, f"/vouchers/{vid}/redeem", amount="").status_code == 400

    assert post(me, f"/vouchers/{vid}/redeem", amount="30").status_code == 303
    assert "Eingelöst" in me.get(f"/vouchers/{vid}").text
    assert post(me, f"/vouchers/{vid}/redeem", amount="1").status_code == 400  # nicht mehr offen


def test_einloesen_ohne_wert_nicht_moeglich(me):
    vid = create_id(me, title="OhneWert")
    assert post(me, f"/vouchers/{vid}/redeem", amount="5").status_code == 400


def test_zustandsaktionen(me):
    vid = create_id(me, title="Aktionen", value="40")
    post(me, f"/vouchers/{vid}/state", action="redeem_all")
    row = db_rows("SELECT * FROM vouchers")[0]
    assert row["state"] == "redeemed" and row["remaining_cents"] == 0
    assert "Aktionen" not in me.get("/").text and "Aktionen" in me.get("/?status=eingeloest").text

    post(me, f"/vouchers/{vid}/state", action="reopen")
    row = db_rows("SELECT * FROM vouchers")[0]
    assert row["state"] == "open" and row["remaining_cents"] == 4000

    post(me, f"/vouchers/{vid}/state", action="archive")
    assert db_rows("SELECT state FROM vouchers")[0]["state"] == "archived"
    assert "Aktionen" in me.get("/?status=archiviert").text
    post(me, f"/vouchers/{vid}/state", action="unarchive")
    assert db_rows("SELECT state FROM vouchers")[0]["state"] == "open"

    assert post(me, f"/vouchers/{vid}/state", action="boom").status_code == 400
    assert post(me, "/vouchers/999/state", action="archive").status_code == 404


def test_bearbeiten_und_zustand_folgt_restwert(me):
    vid = create_id(me, title="Alt", value="40", tags="a")
    edit = f"/vouchers/{vid}/edit"
    page = me.get(edit).text
    assert 'value="Alt"' in page and 'name="remaining"' in page

    assert post(me, edit, title="Neu", value="40", remaining="0", tags="b, c").status_code == 303
    row = db_rows("SELECT * FROM vouchers")[0]
    assert row["title"] == "Neu" and row["state"] == "redeemed"
    assert [r["tag"] for r in db_rows("SELECT tag FROM voucher_tags ORDER BY tag")] == ["b", "c"]

    post(me, edit, title="Neu", value="40", remaining="15")
    assert db_rows("SELECT state FROM vouchers")[0]["state"] == "open"
    assert post(me, edit, title="", value="40").status_code == 400
    assert post(me, "/vouchers/999/edit", title="x").status_code == 404


def test_bearbeiten_zeigt_code_im_formular(me):
    vid = create_id(me, code="MEIN-CODE")
    assert 'value="MEIN-CODE"' in me.get(f"/vouchers/{vid}/edit").text


# --- Löschen -------------------------------------------------------------------------------------


def test_loeschen_entfernt_gutschein_und_dateien(me):
    vid = create_id(me, title="Weg damit", tags="x")
    assert upload(me, vid, PNG).status_code == 303
    stored = db_rows("SELECT stored_name FROM voucher_attachments")[0]["stored_name"]
    assert (Path(get_settings().attachments_dir) / stored).exists()

    assert "endgültig gelöscht" in me.get(f"/vouchers/{vid}/delete").text
    assert post(me, f"/vouchers/{vid}/delete").status_code == 303
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_tags")[0]["n"] == 0
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_attachments")[0]["n"] == 0
    assert not (Path(get_settings().attachments_dir) / stored).exists()
    assert post(me, f"/vouchers/{vid}/delete").status_code == 404


# --- Anhänge --------------------------------------------------------------------------------------


def test_bild_hochladen_und_anzeigen(me):
    vid = create_id(me)
    assert upload(me, vid, PNG, name="scan.png").status_code == 303
    detail = me.get(f"/vouchers/{vid}").text
    assert "scan.png" in detail and "/attachments/1" in detail

    r = me.get("/attachments/1")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_pdf_wird_nur_als_download_ausgeliefert(me):
    vid = create_id(me)
    assert upload(me, vid, PDF, name="gutschein.pdf", ctype="application/pdf").status_code == 303
    r = me.get("/attachments/1")
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("attachment")


def test_falscher_dateityp_wird_abgelehnt(me):
    vid = create_id(me)
    # Der Browser behauptet "image/png", der Inhalt ist aber ein Skript.
    r = upload(me, vid, b"#!/bin/sh\necho hallo\n", name="evil.png", ctype="image/png")
    assert r.status_code == 400 and "Erlaubt sind nur" in r.text
    assert upload(me, vid, b"<html><script>alert(1)</script>", name="x.html", ctype="text/html").status_code == 400
    assert list(Path(get_settings().attachments_dir).iterdir()) == []
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_attachments")[0]["n"] == 0


def test_pdf_mit_javascript_wird_abgelehnt(me):
    vid = create_id(me)
    bad = PDF + b"<< /S /JavaScript /JS (app.alert(1)) >>"
    r = upload(me, vid, bad, name="x.pdf", ctype="application/pdf")
    assert r.status_code == 400 and "aktive Inhalte" in r.text
    assert list(Path(get_settings().attachments_dir).iterdir()) == []


def test_leere_datei_wird_abgelehnt(me):
    vid = create_id(me)
    assert upload(me, vid, b"", name="leer.png").status_code in (400,)


def test_groessenlimit(me, monkeypatch):
    monkeypatch.setattr(routes_vouchers, "settings", dataclasses.replace(get_settings(), max_upload_mb=1))
    vid = create_id(me)
    knapp_drueber = PNG + b"\x00" * (1024 * 1024)  # knapp über 1 MB: erst beim Speichern abgelehnt
    r = upload(me, vid, knapp_drueber)
    assert r.status_code == 400 and "größer als 1 MB" in r.text
    viel_zu_gross = PNG + b"\x00" * (2 * 1024 * 1024)  # schon am Header erkannt
    assert upload(me, vid, viel_zu_gross).status_code == 413
    assert list(Path(get_settings().attachments_dir).iterdir()) == []
    assert upload(me, vid, PNG).status_code == 303


def test_anzahl_der_anhaenge_ist_begrenzt(me):
    vid = create_id(me)
    for _ in range(attachments.MAX_ATTACHMENTS_PER_VOUCHER):
        assert upload(me, vid, PNG).status_code == 303
    r = upload(me, vid, PNG)
    assert r.status_code == 400 and "Höchstens" in r.text


def test_dateiname_mit_pfad_wird_bereinigt(me):
    vid = create_id(me)
    upload(me, vid, PNG, name="../../etc/passwd.png")
    row = db_rows("SELECT * FROM voucher_attachments")[0]
    assert row["original_name"] == "passwd.png"
    assert len(row["stored_name"]) == 32 and "/" not in row["stored_name"]


def test_anhang_entfernen(me):
    vid = create_id(me)
    upload(me, vid, PNG)
    stored = db_rows("SELECT stored_name FROM voucher_attachments")[0]["stored_name"]
    assert post(me, "/attachments/1/delete").status_code == 303
    assert not (Path(get_settings().attachments_dir) / stored).exists()
    assert me.get("/attachments/1").status_code == 404
    assert post(me, "/attachments/1/delete").status_code == 404


def test_upload_ohne_datei(me):
    vid = create_id(me)
    r = me.post(f"/vouchers/{vid}/attachments", data={"csrf_token": csrf_value(me)},
                files={"other": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 400 and "wähle eine Datei" in r.text


def test_upload_zu_unbekanntem_gutschein(me):
    assert upload(me, 999, PNG).status_code == 404
    assert list(Path(get_settings().attachments_dir).iterdir()) == []


# --- Sicherheit -------------------------------------------------------------------------------------


def test_csrf_pflicht_auf_allen_schreibaktionen(me):
    vid = create_id(me, value="10")
    for url, data in [
        ("/vouchers", {"title": "x"}),
        (f"/vouchers/{vid}/edit", {"title": "x"}),
        (f"/vouchers/{vid}/redeem", {"amount": "1"}),
        (f"/vouchers/{vid}/state", {"action": "archive"}),
        (f"/vouchers/{vid}/delete", {}),
        ("/attachments/1/delete", {}),
    ]:
        assert me.post(url, data=data).status_code == 403, url
    r = me.post(f"/vouchers/{vid}/attachments", files={"file": ("a.png", PNG, "image/png")})
    assert r.status_code == 403
    assert list(Path(get_settings().attachments_dir).iterdir()) == []
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 1


def test_ohne_login_kein_zugriff(me):
    vid = create_id(me)
    upload(me, vid, PNG)
    me.cookies.clear()
    for method, url in [
        ("get", "/"), ("get", "/vouchers/new"), ("get", f"/vouchers/{vid}"),
        ("get", f"/vouchers/{vid}/edit"), ("get", f"/vouchers/{vid}/delete"),
        ("get", "/attachments/1"), ("post", "/vouchers"), ("post", f"/vouchers/{vid}/attachments"),
        ("post", f"/vouchers/{vid}/delete"),
    ]:
        r = getattr(me, method)(url)
        assert r.status_code == 303 and r.headers["location"] == "/login", url


def test_unbekannte_seite_auf_deutsch(me):
    r = me.get("/vouchers/999")
    assert r.status_code == 404 and "existiert nicht" in r.text
    assert "Not Found" not in r.text
