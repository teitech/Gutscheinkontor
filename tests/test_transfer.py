import dataclasses
import io
import json
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

import pytest

from app import attachments, dates, routes_transfer, transfer
from app.config import get_settings
from app.db import connect
from tests.conftest import csrf_value, do_setup
from tests.test_vouchers import PDF, PNG, create_id, db_rows, post, upload

TODAY = date(2026, 11, 17)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: TODAY)


@pytest.fixture
def me(client, clock):
    do_setup(client, clock)
    return client


def export_zip(client, include_codes=True):
    fields = {"csrf_token": csrf_value(client)}
    if include_codes:
        fields["include_codes"] = "on"
    r = client.post("/data/export.zip", data=fields)
    assert r.status_code == 200, r.text
    return r


def import_zip(client, content, name="export.zip", dry_run=False, skip_duplicates=True):
    data = {"csrf_token": csrf_value(client)}
    if dry_run:
        data["dry_run"] = "on"
    if skip_duplicates:
        data["skip_duplicates"] = "on"
    return client.post("/data/import", data=data, files={"file": (name, content, "application/zip")})


def make_zip(document=None, members=None, raw_json=None):
    """Baut ein Export-ZIP von Hand, auch mit absichtlich kaputtem Inhalt."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if raw_json is not None:
            archive.writestr("export.json", raw_json)
        elif document is not None:
            archive.writestr("export.json", json.dumps(document))
        for name, content in (members or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def doc(*vouchers_, **overrides):
    base = {"format": transfer.FORMAT_NAME, "format_version": 1, "vouchers": list(vouchers_)}
    base.update(overrides)
    return base


def entry(**fields):
    base = {"title": "Eintrag", "code": "", "value_cents": None, "remaining_cents": None,
            "expires_on": None, "state": "open", "owner_email": None, "notes": "", "tags": [],
            "attachments": []}
    base.update(fields)
    return base


def read_zip(response):
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return archive, json.loads(archive.read("export.json"))


def stored_files():
    return sorted(p.name for p in Path(get_settings().attachments_dir).iterdir())


def wipe_vouchers():
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("PRAGMA foreign_keys = ON")  # sonst bleiben Tags und Anhänge übrig
    conn.execute("DELETE FROM vouchers")
    conn.commit()
    conn.close()
    for p in Path(get_settings().attachments_dir).iterdir():
        p.unlink()


def snapshot():
    """Alle fachlichen Felder aller Gutscheine zum Vergleichen (ohne IDs und Dateinamen)."""
    from app import vouchers
    conn = sqlite3.connect(get_settings().db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = []
        for v in conn.execute("SELECT * FROM vouchers ORDER BY title"):
            code, _ = vouchers.decrypt_code(v)
            tags = [r[0] for r in conn.execute(
                "SELECT tag FROM voucher_tags WHERE voucher_id = ? ORDER BY tag", (v["id"],))]
            files = [(r["original_name"], r["content_type"], r["size_bytes"]) for r in conn.execute(
                "SELECT * FROM voucher_attachments WHERE voucher_id = ? ORDER BY original_name", (v["id"],))]
            result.append((v["title"], code, v["value_cents"], v["remaining_cents"], v["expires_on"],
                           v["state"], v["notes"], tags, files, v["created_at"]))
        return result
    finally:
        conn.close()


def seed(client):
    a = create_id(client, title="Müller Möbel", code="ABC-123", value="50", expires_on="2026-12-24",
                  notes="Zeile 1\nZeile 2", tags="Möbel, Wohnen", owner="1")
    post(client, f"/vouchers/{a}/redeem", amount="20")
    upload(client, a, PNG, name="scan.png")
    upload(client, a, PDF, name="beleg.pdf", ctype="application/pdf")
    b = create_id(client, title="Bäckerei", code="B-1", value="10")
    post(client, f"/vouchers/{b}/state", action="redeem_all")
    c = create_id(client, title="Kino", expires_on="2027-03-01")
    post(client, f"/vouchers/{c}/state", action="archive")
    return a, b, c


# --- Export ----------------------------------------------------------------------------


def test_zip_export_inhalt(me):
    seed(me)
    r = export_zip(me)
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="gutscheinkontor-20261117.zip"'
    assert r.headers["cache-control"] == "no-store"
    archive, document = read_zip(r)

    assert document["format"] == "gutscheinkontor-export" and document["format_version"] == 5
    assert document["includes_codes"] is True and len(document["vouchers"]) == 3
    first = document["vouchers"][0]
    assert first["title"] == "Müller Möbel" and first["code"] == "ABC-123"
    assert first["value_cents"] == 5000 and first["remaining_cents"] == 3000
    assert first["owner_email"] == "anna@example.de" and first["tags"] == ["Möbel", "Wohnen"]
    assert first["notes"] == "Zeile 1\nZeile 2"
    assert [f["original_name"] for f in first["attachments"]] == ["scan.png", "beleg.pdf"]
    assert archive.read(first["attachments"][0]["file"]) == PNG
    assert archive.read(first["attachments"][1]["file"]) == PDF
    assert sorted(archive.namelist()) == sorted(
        ["export.json"] + [f["file"] for f in first["attachments"]])


def test_zip_export_ohne_codes(me):
    seed(me)
    archive, document = read_zip(export_zip(me, include_codes=False))
    assert document["includes_codes"] is False
    assert all(v["code"] is None for v in document["vouchers"])
    assert "ABC-123" not in archive.read("export.json").decode()


def test_export_enthaelt_keine_konten_oder_geheimnisse(me):
    seed(me)
    text = export_zip(me).content
    archive = zipfile.ZipFile(io.BytesIO(text))
    blob = archive.read("export.json").decode().lower()
    for forbidden in ("argon2", "password", "totp", "secret", "session"):
        assert forbidden not in blob


def test_export_ueberspringt_fehlende_dateien(me):
    vid, _, _ = seed(me)
    for p in Path(get_settings().attachments_dir).iterdir():
        p.unlink()
    _, document = read_zip(export_zip(me))
    assert document["vouchers"][0]["attachments"] == []


def test_leerer_export_ist_gueltig(me):
    _, document = read_zip(export_zip(me))
    assert document["vouchers"] == []


def test_csv_export(me):
    create_id(me, title="=SUMME(1+1)", code="+49-X", value="1234,5", expires_on="2026-12-24",
              notes="Mit; Semikolon\nund Zeilenumbruch", tags="a, b", owner="1")
    create_id(me, title="Müller, Möbel \"groß\"")
    r = me.post("/data/export.csv", data={"csrf_token": csrf_value(me), "include_codes": "on"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="gutscheinkontor-20261117.csv"'
    assert r.content.startswith(b"\xef\xbb\xbf")  # BOM für Excel
    text = r.content.decode("utf-8-sig")
    import csv
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    assert rows[0] == transfer.CSV_HEADER
    first = rows[1]
    assert first[0] == "'=SUMME(1+1)" and first[1] == "'+49-X"        # Formel-Schutz
    assert first[2] == "Betrag in Euro"
    assert first[3] == "1234,50" and first[4] == "1234,50"             # deutsches Zahlenformat
    assert first[5] == "" and first[6] == "" and first[7] == ""         # Prozent, Vorteil, Mindestwert
    assert first[8] == "" and first[9] == ""                            # Nur online, Shop-Adresse
    assert first[10] == "24.12.2026" and first[11] == "Aktiv" and first[12] == "Anna"
    assert first[13] == "a, b" and first[14] == "Mit; Semikolon\nund Zeilenumbruch"
    assert rows[2][0] == 'Müller, Möbel "groß"'


def test_csv_ohne_codes(me):
    create_id(me, title="X", code="GEHEIM")
    r = me.post("/data/export.csv", data={"csrf_token": csrf_value(me)})
    assert "GEHEIM" not in r.content.decode("utf-8-sig")


# --- Import: Rundreise --------------------------------------------------------------------


def test_rundreise_ist_verlustfrei(me):
    seed(me)
    before = snapshot()
    archive_bytes = export_zip(me).content
    wipe_vouchers()
    assert snapshot() == []

    r = import_zip(me, archive_bytes)
    assert r.status_code == 200 and "Import abgeschlossen" in r.text
    assert snapshot() == before  # inklusive Zustand, Restwert, Zeitstempel, Tags und Anhängen
    # Die Anhänge-Dateien sind wirklich da und lesbar.
    assert me.get("/attachments/1").content in (PNG, PDF)
    assert len(stored_files()) == 2


def test_import_zeigt_bericht(me):
    seed(me)
    archive_bytes = export_zip(me).content
    wipe_vouchers()
    text = import_zip(me, archive_bytes).text
    assert "3 Gutscheine hinzugefügt, 2 Anhänge" in text


def test_import_in_leere_instanz_ordnet_unbekannte_person_niemandem_zu(me):
    archive = make_zip(doc(entry(title="Fremd", owner_email="jemand@andere-instanz.de")))
    r = import_zip(me, archive)
    assert "gibt es hier nicht" in r.text
    assert db_rows("SELECT owner_user_id FROM vouchers")[0]["owner_user_id"] is None


def test_person_wird_ueber_email_zugeordnet_ohne_gross_klein(me):
    archive = make_zip(doc(entry(title="Annas", owner_email="ANNA@Example.de")))
    import_zip(me, archive)
    assert db_rows("SELECT owner_user_id FROM vouchers")[0]["owner_user_id"] == 1


def test_importierte_gutscheine_loesen_keine_alte_erinnerung_aus(me):
    archive = make_zip(doc(entry(title="Alt", expires_on="2026-12-01")))
    import_zip(me, archive)
    assert db_rows("SELECT expiry_set_on FROM vouchers")[0]["expiry_set_on"] == "2026-11-17"


# --- Import: Duplikate und Probelauf -------------------------------------------------------


def test_duplikate_werden_uebersprungen(me):
    seed(me)
    archive_bytes = export_zip(me).content
    r = import_zip(me, archive_bytes)
    assert "0 Gutscheine hinzugefügt" in r.text and "3 Duplikate übersprungen" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 3
    assert len(stored_files()) == 2  # keine zusätzlichen Dateien


def test_duplikate_koennen_erlaubt_werden(me):
    seed(me)
    archive_bytes = export_zip(me).content
    import_zip(me, archive_bytes, skip_duplicates=False)
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 6


def test_duplikate_innerhalb_der_datei(me):
    archive = make_zip(doc(entry(title="Doppelt"), entry(title="DOPPELT")))
    r = import_zip(me, archive)
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 1
    assert "1 Duplikate übersprungen" in r.text


def test_probelauf_speichert_nichts(me):
    seed(me)
    archive_bytes = export_zip(me).content
    wipe_vouchers()
    r = import_zip(me, archive_bytes, dry_run=True)
    assert "Probelauf" in r.text and "3 Gutscheine würden hinzugefügt, 2 Anhänge" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_attachments")[0]["n"] == 0
    assert stored_files() == []


# --- Import: fehlerhafte Einträge ------------------------------------------------------------


def test_ungueltige_eintraege_werden_gemeldet_der_rest_importiert(me):
    archive = make_zip(doc(
        entry(title="Gut"),
        entry(title=""),
        entry(title="Falscher Betrag", value_cents=-5),
        entry(title="Rest zu groß", value_cents=100, remaining_cents=200),
        entry(title="Kaputtes Datum", expires_on="31.02.2026"),
        entry(title="Falscher Zustand", state="geloescht"),
        entry(title="Tags falsch", tags="nicht-liste"),
        "kein objekt",
    ))
    r = import_zip(me, archive)
    assert r.status_code == 200
    assert [v["title"] for v in db_rows("SELECT title FROM vouchers")] == ["Gut"]
    assert "7 Einträge unlesbar" in r.text
    assert "Eintrag 2" in r.text and "Eintrag 8" in r.text and "Falscher Zustand" in r.text


def test_eintrag_mit_falschem_typ(me):
    archive = make_zip(doc(entry(title=123), entry(title="Ok", notes=["liste"])))
    r = import_zip(me, archive)
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0
    assert "2 Einträge unlesbar" in r.text


def test_zustand_wird_konsistent_gemacht(me):
    archive = make_zip(doc(
        entry(title="Eingeloest", value_cents=500, remaining_cents=500, state="redeemed"),
        entry(title="Leer aber offen", value_cents=500, remaining_cents=0, state="open"),
    ))
    import_zip(me, archive)
    rows = {r["title"]: r for r in db_rows("SELECT * FROM vouchers")}
    assert rows["Eingeloest"]["remaining_cents"] == 0 and rows["Eingeloest"]["state"] == "redeemed"
    assert rows["Leer aber offen"]["state"] == "redeemed"


def test_ungueltige_zeitstempel_werden_ersetzt(me):
    archive = make_zip(doc(entry(title="Zeit", created_at="gestern", updated_at=5)))
    import_zip(me, archive)
    row = db_rows("SELECT created_at, updated_at FROM vouchers")[0]
    assert row["created_at"].endswith("Z") and row["updated_at"].endswith("Z")


# --- Import: abgelehnte Dateien ---------------------------------------------------------------


@pytest.mark.parametrize("content,message", [
    (b"das ist kein zip", "keine gültige ZIP-Datei"),
    (make_zip(members={"anderes.txt": "x"}), "export.json fehlt"),
    (make_zip(raw_json="{kaputt"), "nicht lesbar"),
    (make_zip(raw_json="[]"), "kein Export des Gutscheinkontors"),
    (make_zip(doc(format="anderes-programm")), "kein Export des Gutscheinkontors"),
    (make_zip(doc(format_version=6)), "neueren Version"),
    (make_zip(doc(format_version="1")), "Versionsangabe"),
    (make_zip(doc(format_version=True)), "Versionsangabe"),
    (make_zip({"format": transfer.FORMAT_NAME, "format_version": 1, "vouchers": "nein"}), "Liste der Gutscheine"),
    (make_zip(raw_json=b"\xff\xfe\x00"), "nicht lesbar"),
    (make_zip(raw_json="[" * 100000), "nicht lesbar"),
])
def test_unbrauchbare_dateien_werden_abgelehnt(me, content, message):
    r = import_zip(me, content)
    assert r.status_code == 400 and message in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0


def test_zu_viele_mitglieder(me, monkeypatch):
    monkeypatch.setattr(transfer, "MAX_MEMBERS", 3)
    archive = make_zip(doc(), members={f"attachments/{i:04d}_01.png": PNG for i in range(5)})
    assert "zu viele Dateien" in import_zip(me, archive).text


def test_zu_grosse_json_datei(me, monkeypatch):
    monkeypatch.setattr(transfer, "MAX_JSON_BYTES", 100)
    archive = make_zip(doc(*[entry(title=f"G{i}") for i in range(20)]))
    r = import_zip(me, archive)
    assert r.status_code == 400 and "zu groß" in r.text


def test_zu_viel_entpackt(me, monkeypatch):
    monkeypatch.setattr(transfer, "MAX_TOTAL_BYTES", 10)
    assert "entpackt zu groß" in import_zip(me, make_zip(doc(entry()))).text


def test_zu_viele_gutscheine(me, monkeypatch):
    monkeypatch.setattr(transfer, "MAX_VOUCHERS", 2)
    archive = make_zip(doc(entry(title="a"), entry(title="b"), entry(title="c")))
    assert "zu viele Gutscheine" in import_zip(me, archive).text


def test_upload_groesse_wird_vor_dem_lesen_geprueft(me, monkeypatch):
    monkeypatch.setattr(routes_transfer, "settings",
                        dataclasses.replace(get_settings(), max_import_mb=1))
    r = import_zip(me, b"x" * (2 * 1024 * 1024))
    assert r.status_code == 413 and "zu groß" in r.text


def test_ohne_datei(me):
    r = me.post("/data/import", data={"csrf_token": csrf_value(me)},
                files={"other": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 400 and "wähle eine Datei" in r.text


# --- Import: Angriffe über Anhänge ----------------------------------------------------------


def with_attachment(member, content, name="x.png", **entry_fields):
    item = {"file": member, "original_name": name, "content_type": "image/png", "size_bytes": 1}
    return make_zip(doc(entry(title="Mit Anhang", attachments=[item], **entry_fields)),
                    members={member: content} if content is not None else {})


def test_anhang_mit_pfad_aus_der_json_wird_nie_gelesen(me):
    for evil in ("../../etc/passwd", "/etc/passwd", "attachments/../export.json",
                 "attachments/0001_01.png/../../x", "export.json", "attachments/evil.png"):
        # Wo möglich, liegt unter dem bösen Namen sogar eine echte Datei im ZIP.
        extra = {} if ".." in evil or evil == "export.json" else {evil.lstrip("/"): PNG}
        archive = make_zip(
            doc(entry(title="Böse", attachments=[{"file": evil, "original_name": "x.png"}])),
            members=extra,
        )
        r = import_zip(me, archive)
        assert "ungültigen Namen" in r.text, evil
        assert db_rows("SELECT COUNT(*) AS n FROM voucher_attachments")[0]["n"] == 0
        wipe_vouchers()
    assert stored_files() == []


def test_anhang_inhalt_statt_behauptetem_typ(me):
    archive = with_attachment("attachments/0001_01.png", b"#!/bin/sh\nrm -rf /\n", name="harmlos.png")
    r = import_zip(me, archive)
    assert "Erlaubt sind nur" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 1  # Gutschein bleibt, Anhang nicht
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_attachments")[0]["n"] == 0
    assert stored_files() == []


def test_pdf_mit_javascript_im_import(me):
    archive = with_attachment("attachments/0001_01.pdf", PDF + b"/JavaScript (x)", name="a.pdf")
    assert "aktive Inhalte" in import_zip(me, archive).text
    assert stored_files() == []


def test_anhang_zu_gross(me, monkeypatch):
    monkeypatch.setattr(transfer, "get_settings",
                        lambda: dataclasses.replace(get_settings(), max_upload_mb=1))
    archive = with_attachment("attachments/0001_01.png", PNG + b"\x00" * (1024 * 1024))
    assert "größer als 1 MB" in import_zip(me, archive).text
    assert stored_files() == []


def test_anhang_fehlt_im_zip(me):
    archive = with_attachment("attachments/0001_01.png", None)
    r = import_zip(me, archive)
    assert "fehlt oder hat einen ungültigen Namen" in r.text


def test_zu_viele_anhaenge_pro_gutschein(me):
    members, items = {}, []
    for n in range(1, 13):
        member = f"attachments/0001_{n:02d}.png"
        members[member] = PNG
        items.append({"file": member, "original_name": f"{n}.png"})
    archive = make_zip(doc(entry(title="Viele", attachments=items)), members=members)
    r = import_zip(me, archive)
    assert "nur die ersten 10" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_attachments")[0]["n"] == 10


def test_anhang_name_wird_bereinigt(me):
    archive = with_attachment("attachments/0001_01.png", PNG, name="../../etc/passwd.png")
    import_zip(me, archive)
    row = db_rows("SELECT * FROM voucher_attachments")[0]
    assert row["original_name"] == "passwd.png" and len(row["stored_name"]) == 32


def test_fehler_mitten_im_import_raeumt_dateien_auf(me, monkeypatch):
    members, items = {}, []
    for n in (1, 2):
        member = f"attachments/0001_{n:02d}.png"
        members[member] = PNG
        items.append({"file": member, "original_name": f"{n}.png"})
    archive = make_zip(doc(entry(title="A", attachments=items), entry(title="B")), members=members)
    calls = {"n": 0}
    original = transfer.vouchers.create_voucher

    def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Datenbank kaputt")
        return original(*args, **kwargs)

    monkeypatch.setattr(transfer.vouchers, "create_voucher", explode)
    conn = connect()
    with pytest.raises(RuntimeError):
        transfer.import_zip(conn, io.BytesIO(archive), skip_duplicates=True, dry_run=False, created_by=1)
    conn.close()
    assert stored_files() == []
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0


# --- Zugriff und Schutz ------------------------------------------------------------------------


def test_csrf_pflicht(me):
    assert me.post("/data/export.zip").status_code == 403
    assert me.post("/data/export.csv").status_code == 403
    r = me.post("/data/import", files={"file": ("e.zip", make_zip(doc()), "application/zip")})
    assert r.status_code == 403


def test_ohne_login_kein_zugriff(me):
    me.cookies.clear()
    for method, url in [("get", "/data"), ("post", "/data/export.zip"),
                        ("post", "/data/export.csv"), ("post", "/data/import")]:
        r = getattr(me, method)(url)
        assert r.status_code == 303 and r.headers["location"] == "/login", url


def test_import_ist_begrenzt(me):
    archive = make_zip(doc())
    for _ in range(routes_transfer.IMPORT_LIMIT):
        assert import_zip(me, archive).status_code == 200
    assert import_zip(me, archive).status_code == 429


def test_datenseite(me):
    seed(me)
    page = me.get("/data")
    assert page.status_code == 200 and "3 Gutscheine, 2 Anhänge" in page.text
    assert "im Klartext" in page.text
    assert "/data" in me.get("/").text
