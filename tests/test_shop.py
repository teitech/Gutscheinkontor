"""Webshop-Adresse und 'nur online einlösbar'."""
import csv
import io
import json
import zipfile
from datetime import date

import pytest

from app import dates, reminders, vouchers
from tests.conftest import csrf_value, do_setup
from tests.test_kinds import exported
from tests.test_reminders import at, mails, make, sql  # noqa: F401
from tests.test_transfer import doc, entry, import_zip, make_zip
from tests.test_vouchers import create, create_id, db_rows, post


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))


@pytest.fixture
def me(client, clock):
    do_setup(client, clock)
    return client


@pytest.mark.parametrize("eingabe,erwartet", [
    ("", ""),
    ("  ", ""),
    ("https://www.hagebau.de", "https://www.hagebau.de"),
    ("http://shop.example.de/gutschein?x=1&y=2", "http://shop.example.de/gutschein?x=1&y=2"),
    ("www.hagebau.de", "https://www.hagebau.de"),
    ("shop.example.de:8443/pfad", "https://shop.example.de:8443/pfad"),
    ("HTTPS://Shop.de", "HTTPS://Shop.de"),
])
def test_gueltige_adressen(eingabe, erwartet):
    assert vouchers.parse_shop_url(eingabe) == erwartet


@pytest.mark.parametrize("eingabe", [
    "javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<b>x</b>", "mailto:a@b.de",
    "ftp://shop.de", "file:///etc/passwd", "//shop.de", "https://", "https://localhost",
    "https://user:pass@shop.de", "https://shop.de/mit leerzeichen", "https://shop.de/\x00",
    "https://shop.de:abc", "https://shop.de/a'b", "https://shop.de/a\\b", "https://" + "a" * 500 + ".de", "nur text",
])
def test_ungueltige_adressen(eingabe):
    with pytest.raises(ValueError):
        vouchers.parse_shop_url(eingabe)


def test_anlegen_mit_adresse_und_online_haken(me):
    vid = create_id(me, title="Hagebau", value="20", shop_url="www.hagebau.de", online_only="on")
    row = db_rows("SELECT shop_url, online_only FROM vouchers WHERE id = ?", (vid,))[0]
    assert (row["shop_url"], row["online_only"]) == ("https://www.hagebau.de", 1)
    detail = me.get(f"/vouchers/{vid}").text
    assert 'href="https://www.hagebau.de"' in detail and "Nur online" in detail
    assert 'rel="noopener noreferrer nofollow"' in detail and 'target="_blank"' in detail
    assert "Nur online" in me.get("/").text


def test_ohne_angaben_ist_alles_wie_vorher(me):
    vid = create_id(me, value="20")
    row = db_rows("SELECT shop_url, online_only FROM vouchers WHERE id = ?", (vid,))[0]
    assert (row["shop_url"], row["online_only"]) == ("", 0)
    detail = me.get(f"/vouchers/{vid}").text
    assert "Nur online" not in detail and "Einlösen" not in detail


def test_online_haken_ohne_adresse(me):
    vid = create_id(me, value="20", online_only="on")
    detail = me.get(f"/vouchers/{vid}").text
    assert "Nur online" in detail and "<a href=\"https://" not in detail.split("Einlösen")[1].split("</dd>")[0]


def test_unsichere_adresse_wird_abgelehnt_und_nichts_gespeichert(me):
    r = create(me, value="20", shop_url="javascript:alert(1)")
    assert r.status_code == 400 and "http://" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0


def test_html_in_der_adresse_wird_nicht_ausgefuehrt(me):
    r = create(me, value="20", shop_url='https://shop.de/"><script>alert(1)</script>')
    assert r.status_code == 400 and "ungültige Zeichen" in r.text
    assert "<script>alert(1)" not in r.text


def test_bearbeiten_und_abhaken(me):
    vid = create_id(me, value="20", shop_url="shop.de", online_only="on")
    page = me.get(f"/vouchers/{vid}/edit").text
    assert 'value="https://shop.de"' in page and "checked" in page
    assert post(me, f"/vouchers/{vid}/edit", title="Testgutschein", value="20").status_code == 303
    row = db_rows("SELECT shop_url, online_only FROM vouchers WHERE id = ?", (vid,))[0]
    assert (row["shop_url"], row["online_only"]) == ("", 0)


def test_erinnerung_nennt_shop_und_online(me, mails):  # noqa: F811
    make(me, title="Hagebau", value="20", shop_url="www.hagebau.de", online_only="on")
    assert reminders.run_once(at(0)) == 1
    body = mails[0][2]
    assert "nur online einlösbar" in body and "Shop: https://www.hagebau.de" in body


def test_rundreise_export_import(me):
    create_id(me, title="Hagebau", value="20", shop_url="www.hagebau.de", online_only="on")
    content, document = exported(me)
    item = document["vouchers"][0]
    assert item["shop_url"] == "https://www.hagebau.de" and item["online_only"] is True
    sql("DELETE FROM vouchers")
    assert "Import abgeschlossen" in import_zip(me, content).text
    row = db_rows("SELECT shop_url, online_only FROM vouchers")[0]
    assert (row["shop_url"], row["online_only"]) == ("https://www.hagebau.de", 1)


def test_alter_export_ohne_neue_felder(me):
    r = import_zip(me, make_zip(doc(entry(title="Alt"), format_version=2)))
    assert "Import abgeschlossen" in r.text
    row = db_rows("SELECT shop_url, online_only FROM vouchers")[0]
    assert (row["shop_url"], row["online_only"]) == ("", 0)


@pytest.mark.parametrize("fields", [
    {"shop_url": "javascript:alert(1)"}, {"shop_url": 5}, {"online_only": "ja"}, {"online_only": 1},
])
def test_import_prueft_neue_felder(me, fields):
    r = import_zip(me, make_zip(doc(entry(title="X", **fields), format_version=3)))
    assert r.status_code == 200 and "unlesbar und übersprungen" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0


def test_csv_hat_neue_spalten(me):
    create_id(me, title="Hagebau", value="20", shop_url="www.hagebau.de", online_only="on")
    r = me.post("/data/export.csv", data={"csrf_token": csrf_value(me)})
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[1][rows[0].index("Nur online")] == "ja"
    assert rows[1][rows[0].index("Shop-Adresse")] == "https://www.hagebau.de"
