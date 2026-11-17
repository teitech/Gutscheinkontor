"""Gutschein-Arten: Betrag, Prozent, Freitext und Mindestbestellwert."""
import json
import zipfile
import io
from datetime import date

import pytest

from app import dates, reminders
from tests.conftest import csrf_value, do_setup
from tests.test_reminders import at, mails, make, sql  # noqa: F401 (mails ist eine Fixture)
from tests.test_transfer import doc, entry, import_zip, make_zip
from tests.test_vouchers import create, create_id, db_rows, post


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))


@pytest.fixture
def me(client, clock):
    do_setup(client, clock)
    return client


# --- Anlegen und Prüfen ---------------------------------------------------------------------


def test_prozent_gutschein_anlegen_und_anzeigen(me):
    vid = create_id(me, title="Drogerie", kind="percent", percent="10 %", min_order="50",
                    expires_on="2026-12-31")
    row = db_rows("SELECT * FROM vouchers WHERE id = ?", (vid,))[0]
    assert (row["kind"], row["percent"], row["value_cents"], row["remaining_cents"]) == ("percent", 10, None, None)
    assert row["min_order_cents"] == 5000
    detail = me.get(f"/vouchers/{vid}").text
    assert "10 % Rabatt" in detail and "50,00 €" in detail and "Mindestbestellwert" in detail
    listing = me.get("/").text
    assert "10 %" in listing and "ab 50,00 €" in listing


def test_freitext_gutschein(me):
    vid = create_id(me, title="Pizza", kind="other", benefit_text="  Gratis   Getränk ")
    row = db_rows("SELECT * FROM vouchers WHERE id = ?", (vid,))[0]
    assert row["kind"] == "other" and row["benefit_text"] == "Gratis Getränk"
    assert "Gratis Getränk" in me.get(f"/vouchers/{vid}").text


def test_betrag_bleibt_standard_und_ignoriert_fremde_felder(me):
    vid = create_id(me, value="25", percent="99", benefit_text="egal")
    row = db_rows("SELECT * FROM vouchers WHERE id = ?", (vid,))[0]
    assert row["kind"] == "amount" and row["value_cents"] == 2500 and row["percent"] is None
    assert row["benefit_text"] == "" and row["min_order_cents"] is None


def test_prozent_ignoriert_betragsfelder(me):
    vid = create_id(me, kind="percent", percent="20", value="25")
    row = db_rows("SELECT * FROM vouchers WHERE id = ?", (vid,))[0]
    assert row["value_cents"] is None and row["remaining_cents"] is None


@pytest.mark.parametrize("fields,message", [
    ({"kind": "percent", "percent": ""}, "von 1 bis 100"),
    ({"kind": "percent", "percent": "0"}, "von 1 bis 100"),
    ({"kind": "percent", "percent": "101"}, "von 1 bis 100"),
    ({"kind": "percent", "percent": "abc"}, "von 1 bis 100"),
    ({"kind": "percent", "percent": "10,5"}, "von 1 bis 100"),
    ({"kind": "other", "benefit_text": "  "}, "beschreibe den Vorteil"),
    ({"kind": "other", "benefit_text": "x" * 101}, "Höchstens 100"),
    ({"kind": "unsinn"}, "Art aus der Liste"),
    ({"value": "25", "min_order": "viel"}, "Betrag wie"),
])
def test_ungueltige_eingaben_werden_abgelehnt(me, fields, message):
    r = create(me, **fields)
    assert r.status_code == 400 and message in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0


def test_mindestbestellwert_null_bedeutet_keiner(me):
    vid = create_id(me, value="10", min_order="0")
    assert db_rows("SELECT min_order_cents FROM vouchers WHERE id = ?", (vid,))[0][0] is None
    assert "Mindestbestellwert" not in me.get(f"/vouchers/{vid}").text


def test_art_beim_bearbeiten_wechseln(me):
    vid = create_id(me, title="Shop", value="25", min_order="30")
    page = me.get(f"/vouchers/{vid}/edit").text
    assert 'value="amount"' in page and "30,00" in page
    r = post(me, f"/vouchers/{vid}/edit", title="Shop", kind="percent", percent="15")
    assert r.status_code == 303
    row = db_rows("SELECT * FROM vouchers WHERE id = ?", (vid,))[0]
    assert (row["kind"], row["percent"], row["value_cents"], row["min_order_cents"]) == ("percent", 15, None, None)
    assert 'value="15"' in me.get(f"/vouchers/{vid}/edit").text


def test_prozent_gutschein_laesst_sich_komplett_einloesen(me):
    vid = create_id(me, kind="percent", percent="10")
    post(me, f"/vouchers/{vid}/state", action="redeem_all")
    assert db_rows("SELECT state FROM vouchers WHERE id = ?", (vid,))[0][0] == "redeemed"
    assert "Eingelöst" in me.get("/?status=eingeloest").text


def test_summe_zaehlt_nur_betraege(me):
    create_id(me, title="A", value="20")
    create_id(me, title="B", kind="percent", percent="10")
    assert "2 offene Gutscheine" in me.get("/").text and "20,00 €" in me.get("/").text


# --- Erinnerungen -----------------------------------------------------------------------------


def test_erinnerung_nennt_vorteil_und_mindestwert(me, mails):  # noqa: F811
    make(me, title="Drogerie", kind="percent", percent="10", min_order="50")
    make(me, title="Pizza", kind="other", benefit_text="Gratis Getränk")
    assert reminders.run_once(at(0)) == 1
    body = mails[0][2]
    assert "Drogerie" in body and ", 10 %, ab 50,00 € Warenkorb" in body
    assert "Pizza" in body and ", Gratis Getränk" in body
    assert "Restwert" not in body


# --- Export und Import --------------------------------------------------------------------------


def exported(client):
    r = client.post("/data/export.zip", data={"csrf_token": csrf_value(client), "include_codes": "on"})
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        return r.content, json.loads(z.read("export.json"))


def test_rundreise_mit_neuen_feldern(me):
    create_id(me, title="Drogerie", kind="percent", percent="10", min_order="50")
    create_id(me, title="Pizza", kind="other", benefit_text="Gratis Getränk")
    content, document = exported(me)
    assert document["format_version"] == 2
    by_title = {v["title"]: v for v in document["vouchers"]}
    assert by_title["Drogerie"]["kind"] == "percent" and by_title["Drogerie"]["percent"] == 10
    assert by_title["Drogerie"]["min_order_cents"] == 5000
    assert by_title["Pizza"]["benefit_text"] == "Gratis Getränk"

    sql("DELETE FROM vouchers")
    r = import_zip(me, content)
    assert r.status_code == 200 and "Import abgeschlossen" in r.text
    rows = {v["title"]: v for v in db_rows("SELECT * FROM vouchers")}
    assert (rows["Drogerie"]["kind"], rows["Drogerie"]["percent"], rows["Drogerie"]["min_order_cents"]) == ("percent", 10, 5000)
    assert (rows["Pizza"]["kind"], rows["Pizza"]["benefit_text"]) == ("other", "Gratis Getränk")


def test_duplikate_unterscheiden_die_art(me):
    create_id(me, title="Shop", kind="percent", percent="10")
    content, _ = exported(me)
    create_id(me, title="Shop", kind="percent", percent="20")
    r = import_zip(me, content)  # gleicher Eintrag wie der erste: Duplikat
    assert "Duplikate" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 2


def test_export_aus_version_0_1_laesst_sich_importieren(me):
    old = doc(entry(title="Alt", value_cents=5000, remaining_cents=3000), format_version=1)
    r = import_zip(me, make_zip(old))
    assert r.status_code == 200 and "Import abgeschlossen" in r.text
    row = db_rows("SELECT * FROM vouchers")[0]
    assert row["kind"] == "amount" and row["value_cents"] == 5000 and row["remaining_cents"] == 3000


@pytest.mark.parametrize("fields", [
    {"kind": "percent", "percent": 0}, {"kind": "percent", "percent": "10"},
    {"kind": "percent", "percent": True}, {"kind": "zufall"}, {"kind": "other"},
    {"min_order_cents": -5}, {"min_order_cents": "50"},
])
def test_import_prueft_neue_felder(me, fields):
    r = import_zip(me, make_zip(doc(entry(title="X", **fields), format_version=2)))
    assert r.status_code == 200 and "unlesbar und übersprungen" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0


def test_csv_enthaelt_neue_spalten(me):
    create_id(me, title="Drogerie", kind="percent", percent="10", min_order="50")
    r = me.post("/data/export.csv", data={"csrf_token": csrf_value(me)})
    import csv
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    header, first = rows[0], rows[1]
    assert first[header.index("Art")] == "Prozent-Rabatt"
    assert first[header.index("Prozent")] == "10" and first[header.index("Mindestbestellwert")] == "50,00"


# --- Migration -------------------------------------------------------------------------------------


def test_bestehende_gutscheine_bleiben_betraege(me):
    # Zeile so anlegen, wie sie vor der Migration aussah (ohne die neuen Spalten)
    sql("INSERT INTO vouchers (title, value_cents, remaining_cents, created_at, updated_at) "
        "VALUES ('Alt', 4000, 4000, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    row = db_rows("SELECT kind, percent, benefit_text, min_order_cents FROM vouchers")[0]
    assert tuple(row) == ("amount", None, "", None)
