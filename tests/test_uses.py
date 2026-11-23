"""Mehrfach einlösbare Gutscheine: Zähler für Prozent und Freitext."""
import csv
import io
import json
import zipfile
from datetime import date

import pytest

from app import dates
from tests.conftest import csrf_value, do_setup
from tests.test_reminders import sql
from tests.test_transfer import doc, entry, import_zip, make_zip
from tests.test_vouchers import create, create_id, db_rows, post


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))


@pytest.fixture
def me(client, clock):
    do_setup(client, clock)
    return client


def row(vid):
    return db_rows("SELECT * FROM vouchers WHERE id = ?", (vid,))[0]


def state(client, vid, action):
    return post(client, f"/vouchers/{vid}/state", action=action)


def make(me, uses="3", **kw):
    return create_id(me, title="Claude", kind="percent", percent="100", uses=uses, **kw)


def edit(me, vid, **fields):
    base = dict(title="Claude", kind="percent", percent="100")
    base.update(fields)
    return post(me, f"/vouchers/{vid}/edit", **base)


def test_standard_ist_einmal(me):
    vid = create_id(me, kind="percent", percent="10")
    assert (row(vid)["uses_total"], row(vid)["uses_left"]) == (1, 1)
    assert "Einmal verwendet" not in me.get(f"/vouchers/{vid}").text


def test_betrag_ignoriert_verwendungen(me):
    vid = create_id(me, value="25", uses="5")
    assert (row(vid)["uses_total"], row(vid)["uses_left"]) == (1, 1)


@pytest.mark.parametrize("uses", ["0", "100", "abc", "-1", "1,5"])
def test_ungueltige_verwendungen(me, uses):
    r = create(me, kind="percent", percent="10", uses=uses)
    assert r.status_code == 400 and "von 1 bis 99" in r.text


def test_einmal_verwendet_zaehlt_herunter_bis_eingeloest(me):
    vid = make(me)
    page = me.get(f"/vouchers/{vid}").text
    assert "3 von 3 übrig" in page and "Einmal verwendet" in page
    assert state(me, vid, "use_once").status_code == 303
    r = row(vid)
    assert (r["uses_left"], r["state"]) == (2, "open")
    assert "Teilweise eingelöst" in me.get(f"/vouchers/{vid}").text
    assert "2/3" in me.get("/").text
    assert "Claude" in me.get("/?status=teilweise").text
    state(me, vid, "use_once")
    state(me, vid, "use_once")
    r = row(vid)
    assert (r["uses_left"], r["state"]) == (0, "redeemed")
    assert state(me, vid, "use_once").status_code == 404  # nichts mehr übrig


def test_use_once_nur_fuer_zaehlbare_gutscheine(me):
    vid = create_id(me, value="25")
    assert state(me, vid, "use_once").status_code == 404
    assert row(vid)["state"] == "open"


def test_komplett_einloesen_und_wieder_oeffnen(me):
    vid = make(me)
    state(me, vid, "use_once")
    state(me, vid, "redeem_all")
    assert (row(vid)["uses_left"], row(vid)["state"]) == (0, "redeemed")
    state(me, vid, "reopen")
    assert (row(vid)["uses_left"], row(vid)["state"]) == (1, "open")


def test_archiv_und_zurueck_behaelt_zaehler(me):
    vid = make(me)
    state(me, vid, "use_once")
    state(me, vid, "archive")
    state(me, vid, "unarchive")
    assert (row(vid)["uses_left"], row(vid)["state"]) == (2, "open")
    state(me, vid, "use_once")
    state(me, vid, "use_once")
    state(me, vid, "archive")
    state(me, vid, "unarchive")
    assert row(vid)["state"] == "redeemed"


def test_bearbeiten_verbrauchte_bleiben_verbraucht(me):
    vid = make(me)
    state(me, vid, "use_once")  # 2 von 3
    assert edit(me, vid, uses="5").status_code == 303
    assert (row(vid)["uses_total"], row(vid)["uses_left"]) == (5, 4)
    edit(me, vid, uses="1")
    r = row(vid)
    assert (r["uses_total"], r["uses_left"], r["state"]) == (1, 0, "redeemed")


def test_bearbeiten_eingeloest_bleibt_eingeloest(me):
    vid = make(me, uses="1")
    state(me, vid, "use_once")
    edit(me, vid, uses="4")
    r = row(vid)
    assert (r["uses_total"], r["uses_left"], r["state"]) == (4, 0, "redeemed")


def test_angebrochener_gutschein_bleibt_weitergebbar(me):
    vid = make(me, expires_on="2099-01-01")
    state(me, vid, "use_once")
    assert (row(vid)["uses_total"], row(vid)["uses_left"]) == (3, 2)
    assert "weitergeben" in me.get(f"/vouchers/{vid}").text.lower()


def test_rundreise_export_import(me):
    vid = make(me)
    state(me, vid, "use_once")
    r = me.post("/data/export.zip", data={"csrf_token": csrf_value(me), "include_codes": "on"})
    document = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("export.json"))
    assert document["vouchers"][0]["uses_total"] == 3 and document["vouchers"][0]["uses_left"] == 2
    sql("DELETE FROM vouchers")
    assert "Import abgeschlossen" in import_zip(me, r.content).text
    new = db_rows("SELECT * FROM vouchers")[0]
    assert (new["uses_total"], new["uses_left"], new["state"]) == (3, 2, "open")


def test_import_alter_dateien_gilt_als_einmal(me):
    r = import_zip(me, make_zip(doc(entry(title="Alt", kind="percent", percent=10), format_version=5)))
    assert "Import abgeschlossen" in r.text
    new = db_rows("SELECT * FROM vouchers")[0]
    assert (new["uses_total"], new["uses_left"]) == (1, 1)


@pytest.mark.parametrize("fields", [
    {"uses_total": 0}, {"uses_total": 100}, {"uses_total": "3"}, {"uses_total": True},
    {"uses_total": 3, "uses_left": -1}, {"uses_total": 3, "uses_left": "1"},
])
def test_import_prueft_verwendungen(me, fields):
    r = import_zip(me, make_zip(doc(entry(title="X", kind="percent", percent=10, **fields), format_version=6)))
    assert "unlesbar und übersprungen" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 0


def test_import_ohne_uebrige_wird_eingeloest(me):
    import_zip(me, make_zip(doc(entry(title="X", kind="percent", percent=10, uses_total=2, uses_left=0),
                                format_version=6)))
    new = db_rows("SELECT * FROM vouchers")[0]
    assert (new["uses_left"], new["state"]) == (0, "redeemed")


def test_csv_spalten(me):
    vid = make(me)
    state(me, vid, "use_once")
    r = me.post("/data/export.csv", data={"csrf_token": csrf_value(me)})
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[1][rows[0].index("Verwendungen gesamt")] == "3"
    assert rows[1][rows[0].index("Verwendungen übrig")] == "2"


def test_bestehende_gutscheine_sind_einmal_einloesbar(me):
    sql("INSERT INTO vouchers (title, state, created_at, updated_at) VALUES ('Alt', 'open', 'x', 'x')")
    assert tuple(db_rows("SELECT uses_total, uses_left FROM vouchers")[0]) == (1, 1)
