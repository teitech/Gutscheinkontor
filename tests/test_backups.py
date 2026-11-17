import asyncio
import collections
import dataclasses
import io
import json
import os
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import auth, backups, cli, dates, security
from app.config import get_settings
from app.db import connect
from tests.conftest import csrf_value, do_setup
from tests.test_transfer import seed, snapshot, stored_files, wipe_vouchers
from tests.test_vouchers import create_id, db_rows

BERLIN = ZoneInfo("Europe/Berlin")


def at(days: int = 0, hour: int = 4, minute: int = 0) -> datetime:
    return datetime(2026, 11, 17, hour, minute, tzinfo=BERLIN) + timedelta(days=days)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))
    backups.status.last_error = None


@pytest.fixture
def enabled(monkeypatch):
    """Automatische Backups eingeschaltet (in Tests sonst aus), 7 behalten, ab 3 Uhr."""
    base = get_settings()
    monkeypatch.setattr(backups, "get_settings",
                        lambda: dataclasses.replace(base, backups_enabled=True, backup_keep=7, backup_hour=3))


@pytest.fixture
def me(client, clock):
    do_setup(client, clock)
    return client


def backup_files():
    return sorted(p.name for p in Path(get_settings().backups_dir).glob("*.zip"))


def zip_db(path: Path, target: Path) -> Path:
    with zipfile.ZipFile(path) as archive:
        target.write_bytes(archive.read(backups.DB_MEMBER))
    return target


def build_backup_zip(db_bytes: bytes, extra=None, manifest="default", skip=()) -> bytes:
    """Baut ein Backup-ZIP von Hand, auch mit absichtlich falschem Inhalt."""
    if manifest == "default":
        manifest = {"format": backups.FORMAT_NAME, "format_version": 1}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if backups.DB_MEMBER not in skip:
            archive.writestr(backups.DB_MEMBER, db_bytes)
        if manifest is not None and backups.MANIFEST_MEMBER not in skip:
            archive.writestr(backups.MANIFEST_MEMBER, manifest if isinstance(manifest, (str, bytes)) else json.dumps(manifest))
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def real_db_bytes(me, tmp_path):
    seed(me)
    path = backups.create_backup(at(0, 3))
    data = zip_db(path, tmp_path / "db.sqlite").read_bytes()
    path.unlink()
    return data


def write_zip(tmp_path, content: bytes, name="x.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


# --- Erstellen ------------------------------------------------------------------------------


def test_backup_inhalt_und_rechte(me, tmp_path):
    seed(me)
    path = backups.create_backup(at(0, 3, 5))
    assert path.name == "gutscheinkontor-backup-2026-11-17_030500.zip"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("backup.json"))
        assert archive.testzip() is None
        attachment_names = {n for n in names if n.startswith("attachments/")}
        assert len(attachment_names) == 2
        for member in attachment_names:  # Dateien sind unverändert
            stored = member.split("/", 1)[1]
            assert archive.read(member) == (Path(get_settings().attachments_dir) / stored).read_bytes()
    assert names == {"gutscheinkontor.db", "backup.json"} | attachment_names
    assert manifest["format"] == "gutscheinkontor-backup" and manifest["format_version"] == 1
    assert manifest["voucher_count"] == 3 and manifest["attachment_count"] == 2
    assert manifest["attachments_missing"] == 0 and manifest["schema_version"] >= 4

    snapshot_db = sqlite3.connect(zip_db(path, tmp_path / "s.db"))
    assert snapshot_db.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0] == 3
    assert snapshot_db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert snapshot_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    snapshot_db.close()
    # Keine Arbeitsordner zurückgelassen.
    assert [p.name for p in Path(get_settings().backups_dir).iterdir()] == [path.name]


def test_backup_ist_ein_abbild_zum_zeitpunkt(me, tmp_path):
    seed(me)
    path = backups.create_backup(at(0, 3))
    wipe_vouchers()
    snap = sqlite3.connect(zip_db(path, tmp_path / "s.db"))
    assert snap.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0] == 3
    snap.close()


def test_fehlende_anhangsdatei_bricht_nicht_ab(me):
    seed(me)
    next(Path(get_settings().attachments_dir).iterdir()).unlink()
    path = backups.create_backup(at(0, 3))
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("backup.json"))
    assert manifest["attachment_count"] == 1 and manifest["attachments_missing"] == 1


def test_zu_wenig_speicherplatz(me, monkeypatch):
    seed(me)
    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(backups.shutil, "disk_usage", lambda p: Usage(10, 10, 1))
    with pytest.raises(backups.BackupError, match="Speicherplatz"):
        backups.create_backup(at(0, 3))
    assert list(Path(get_settings().backups_dir).iterdir()) == []  # auch kein Arbeitsordner


def test_aufbewahrung_behaelt_die_neuesten(me, enabled):
    foreign = Path(get_settings().backups_dir)
    foreign.mkdir(parents=True, exist_ok=True)
    (foreign / "notiz.txt").write_text("bleibt")
    (foreign / "gutscheinkontor-backup-kaputt.zip").write_text("bleibt")
    for day in range(9):
        backups.create_backup(at(day, 3))
    names = backup_files()
    assert len(names) == 7 + 1  # 7 Backups plus die fremde Datei mit ähnlichem Namen
    listed = [b.name for b in backups.list_backups()]
    assert listed[0] == "gutscheinkontor-backup-2026-11-25_030000.zip"   # neuestes zuerst
    assert listed[-1] == "gutscheinkontor-backup-2026-11-19_030000.zip"   # die zwei ältesten sind weg
    assert (foreign / "notiz.txt").exists() and (foreign / "gutscheinkontor-backup-kaputt.zip").exists()


def test_alte_arbeitsordner_werden_aufgeraeumt(me, enabled):
    directory = Path(get_settings().backups_dir)
    directory.mkdir(parents=True, exist_ok=True)
    old, new = directory / ".tmp-alt", directory / ".tmp-neu"
    old.mkdir(), new.mkdir()
    long_ago = time.time() - backups.STALE_TMP_SECONDS - 60
    os.utime(old, (long_ago, long_ago))
    backups.prune()
    assert not old.exists() and new.exists()


# --- Täglicher Lauf ----------------------------------------------------------------------------


def test_taeglicher_lauf(me, enabled):
    assert backups.run_once(at(0, 2, 59)) is None          # noch vor der Uhrzeit
    first = backups.run_once(at(0, 3, 0))
    assert first is not None and first.exists()
    assert backups.run_once(at(0, 15)) is None              # heute schon eines da
    assert backups.run_once(at(1, 3, 0)) is not None        # nächster Tag
    assert len(backup_files()) == 2


def test_lauf_ausgeschaltet(me, monkeypatch):
    base = get_settings()
    monkeypatch.setattr(backups, "get_settings", lambda: dataclasses.replace(base, backups_enabled=False))
    assert backups.run_once(at(0, 9)) is None and backup_files() == []


def test_manuelles_backup_zaehlt_als_tagesbackup(me, enabled):
    backups.create_backup(at(0, 14, 30))
    assert backups.run_once(at(0, 15)) is None


def test_planer_laeuft_und_meldet_fehler_ohne_details(me, enabled, monkeypatch, caplog):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("geheim /pfad/zu/daten anna@example.de")

    monkeypatch.setattr(backups, "run_once", boom)

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(backups.scheduler(stop, tick=0.01))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(main())
    assert len(calls) >= 2
    assert backups.status.last_error == "RuntimeError"
    assert "RuntimeError" in caplog.text and "anna@example.de" not in caplog.text and "/pfad" not in caplog.text


def test_planer_zeigt_eigene_fehlermeldungen(me, enabled, monkeypatch):
    def full():
        raise backups.BackupError("Nicht genug freier Speicherplatz für ein Backup.")

    monkeypatch.setattr(backups, "run_once", full)

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(backups.scheduler(stop, tick=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(main())
    assert backups.status.last_error == "Nicht genug freier Speicherplatz für ein Backup."


def test_planer_startet_nicht_wenn_ausgeschaltet(monkeypatch):
    base = get_settings()
    monkeypatch.setattr(backups, "get_settings", lambda: dataclasses.replace(base, backups_enabled=False))

    async def main():
        await asyncio.wait_for(backups.scheduler(asyncio.Event(), tick=0.01), timeout=2)

    asyncio.run(main())


def test_fehlerstatus_wird_nach_erfolg_geloescht(me, enabled):
    backups.status.last_error = "alt"
    backups.run_once(at(0, 4))
    assert backups.status.last_error is None


def test_einstellungen_pruefen():
    for bad in ({"backup_keep": 0}, {"backup_keep": 366}, {"backup_hour": 24}):
        with pytest.raises(RuntimeError):
            dataclasses.replace(get_settings(), **bad).validate()


# --- Wiederherstellen ------------------------------------------------------------------------------


def test_wiederherstellung_rundreise(me):
    seed(me)
    before = snapshot()
    files_before = stored_files()
    path = backups.create_backup(at(0, 3))

    wipe_vouchers()
    create_id(me, title="Danach entstanden")
    assert snapshot() != before

    result = backups.restore_backup(path)
    assert result["vouchers"] == 3 and result["users"] == 1
    assert snapshot() == before and stored_files() == files_before
    assert me.get("/attachments/1").status_code == 200       # Anhänge wirklich da
    assert me.get("/").status_code == 200                     # Sitzung und Konto sind zurück

    # Der bisherige Stand liegt unverändert zur Seite.
    undo = Path(result["undo"])
    old = sqlite3.connect(undo / "gutscheinkontor.db")
    assert [r[0] for r in old.execute("SELECT title FROM vouchers")] == ["Danach entstanden"]
    old.close()
    assert not list(Path(get_settings().data_dir).glob(".restore-staging-*"))


def test_wiederherstellung_in_neuer_instanz_mit_login(me):
    """Nach dem Einspielen funktionieren Passwort und 2FA-Daten (derselbe SECRET_KEY)."""
    seed(me)
    path = backups.create_backup(at(0, 3))
    backups.restore_backup(path)
    row = db_rows("SELECT password_hash, totp_secret_enc, totp_enabled FROM users")[0]
    assert security.verify_password(row["password_hash"], "ein-langes-passwort-123")
    assert row["totp_enabled"] == 1 and security.decrypt(row["totp_secret_enc"])


def test_aelteres_backup_wird_migriert(me, tmp_path):
    """Ein Backup aus einer Version vor den Erinnerungen (Stand 3) wird beim Einspielen aktualisiert."""
    db_file = tmp_path / "alt.db"
    conn = sqlite3.connect(db_file)
    for number in (1, 2, 3):  # genau die Migrationen, die es damals gab
        script = next(backups.MIGRATIONS_DIR.glob(f"{number:03d}_*.sql")).read_text(encoding="utf-8")
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO users (email, display_name, password_hash) VALUES ('alt@example.de','Alt','x')")
    conn.execute("INSERT INTO vouchers (title, created_at, updated_at) VALUES "
                 "('Alter Gutschein', '2026-05-01T10:00:00Z', '2026-05-01T10:00:00Z')")
    conn.commit()
    conn.close()

    backups.restore_backup(write_zip(tmp_path, build_backup_zip(db_file.read_bytes())))
    assert db_rows("PRAGMA user_version")[0][0] == backups._latest_schema()
    user = db_rows("SELECT * FROM users")[0]
    assert user["email"] == "alt@example.de" and user["reminders_enabled"] == 1   # neue Spalte mit Standardwert
    voucher = db_rows("SELECT * FROM vouchers")[0]
    assert voucher["title"] == "Alter Gutschein" and voucher["expiry_set_on"] == "2026-05-01"
    assert db_rows("SELECT COUNT(*) AS n FROM reminders_sent")[0]["n"] == 0


@pytest.mark.parametrize("content,message", [
    (b"kein zip", "keine gültige Backup-Datei"),
    (b"", "keine gültige Backup-Datei"),
])
def test_restore_lehnt_kaputte_dateien_ab(me, tmp_path, content, message):
    with pytest.raises(backups.RestoreError, match=message):
        backups.restore_backup(write_zip(tmp_path, content))


def test_restore_lehnt_unpassende_inhalte_ab(me, tmp_path, real_db_bytes):
    cases = [
        (build_backup_zip(real_db_bytes, manifest=None), "kein Backup des Gutscheinkontors"),
        (build_backup_zip(real_db_bytes, skip=(backups.DB_MEMBER,)), "kein Backup des Gutscheinkontors"),
        (build_backup_zip(real_db_bytes, manifest={"format": "anderes"}), "kein Backup des Gutscheinkontors"),
        (build_backup_zip(real_db_bytes, manifest="[]"), "kein Backup des Gutscheinkontors"),
        (build_backup_zip(real_db_bytes, manifest="{kaputt"), "nicht lesbar"),
        (build_backup_zip(real_db_bytes, manifest={"format": backups.FORMAT_NAME, "format_version": 2}), "neueres Format"),
        (build_backup_zip(real_db_bytes, manifest={"format": backups.FORMAT_NAME, "format_version": "1"}), "unbekanntes"),
        (build_backup_zip(b"das ist keine datenbank " * 100), "beschädigt"),
    ]
    for index, (content, message) in enumerate(cases):
        with pytest.raises(backups.RestoreError, match=message):
            backups.restore_backup(write_zip(tmp_path, content, f"{index}.zip"))


@pytest.mark.parametrize("evil", [
    "../evil", "/etc/passwd", "attachments/../../evil", "attachments/", "attachments/kurz",
    "attachments/" + "g" * 32, "attachments/" + "a" * 33, "anderes.txt", "attachments/" + "a" * 32 + "/x",
])
def test_restore_lehnt_boese_dateinamen_ab(me, tmp_path, real_db_bytes, evil):
    before = snapshot()
    content = build_backup_zip(real_db_bytes, extra={evil: b"x"})
    with pytest.raises(backups.RestoreError, match="unerwartete Dateien"):
        backups.restore_backup(write_zip(tmp_path, content))
    assert snapshot() == before                                 # nichts verändert
    assert not list(Path(get_settings().data_dir).glob(".restore-staging-*"))
    assert not list(Path(get_settings().data_dir).glob("restore-undo-*"))
    assert not (Path(get_settings().data_dir) / "evil").exists() and not Path("/etc/evil").exists()


def test_restore_lehnt_neueres_schema_ab(me, tmp_path, real_db_bytes):
    db_file = tmp_path / "neu.db"
    db_file.write_bytes(real_db_bytes)
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(backups.RestoreError, match="neueren Version"):
        backups.restore_backup(write_zip(tmp_path, build_backup_zip(db_file.read_bytes())))


def test_restore_prueft_den_schluessel(me, tmp_path, real_db_bytes, monkeypatch):
    archive = write_zip(tmp_path, build_backup_zip(real_db_bytes))
    before = snapshot()

    def wrong_key(token):
        raise ValueError("falscher Schlüssel")

    monkeypatch.setattr(backups.security, "decrypt", wrong_key)
    with pytest.raises(backups.RestoreError, match="SECRET_KEY"):
        backups.restore_backup(archive)
    monkeypatch.undo()  # die Attrappe wirkt auch auf snapshot(), das Codes entschlüsselt
    assert snapshot() == before
    assert backups.restore_backup(archive, force=True)["vouchers"] == 3  # bewusst erzwungen


def test_restore_begrenzt_groesse_und_anzahl(me, tmp_path, real_db_bytes, monkeypatch):
    archive = write_zip(tmp_path, build_backup_zip(real_db_bytes, extra={"attachments/" + "a" * 32: b"x" * 1000}))
    monkeypatch.setattr(backups, "MAX_RESTORE_BYTES", 500)
    with pytest.raises(backups.RestoreError, match="zu groß"):
        backups.restore_backup(archive)
    assert not list(Path(get_settings().data_dir).glob(".restore-staging-*"))
    monkeypatch.undo()
    monkeypatch.setattr(backups, "MAX_RESTORE_MEMBERS", 1)
    with pytest.raises(backups.RestoreError, match="zu viele Dateien"):
        backups.restore_backup(archive)


# --- Oberfläche -------------------------------------------------------------------------------------


def make_member(admin=False):
    """Zweiten Nutzer anlegen und seine Sitzung zurückgeben."""
    conn = connect()
    cur = conn.execute(
        "INSERT INTO users (email, display_name, password_hash, is_admin, totp_enabled) VALUES (?,?,?,?,1)",
        ("ben@example.de", "Ben", "x", 1 if admin else 0),
    )
    token = auth.create_session(conn, cur.lastrowid, "active")
    conn.close()
    return token


def test_seite_zeigt_backups_fuer_admins(me):
    assert "Noch kein Backup vorhanden" in me.get("/data").text
    backups.create_backup(at(0, 3))
    page = me.get("/data").text
    assert "Automatische Backups" in page and "17.11.2026 03:00" in page
    assert "gutscheinkontor-backup-2026-11-17_030000.zip" in page
    assert "restore-backup" in page and "SECRET_KEY" in page


def test_seite_zeigt_fehler_und_ausgeschaltet(me):
    backups.status.last_error = "Nicht genug freier Speicherplatz für ein Backup."
    page = me.get("/data").text
    assert "fehlgeschlagen" in page and "Nicht genug freier Speicherplatz" in page
    assert "ausgeschaltet" in page  # in Tests sind die automatischen Backups aus


def test_normale_mitglieder_sehen_keine_backups(me):
    path = backups.create_backup(at(0, 3))
    token = make_member(admin=False)
    me.cookies.clear()
    me.cookies.set("gk_session", token)
    page = me.get("/data")
    assert page.status_code == 200 and "Automatische Backups" not in page.text
    assert me.get(f"/backups/{path.name}").status_code == 403
    assert me.post("/backups/create", data={"csrf_token": csrf_value(me)}).status_code == 403
    assert backup_files() == [path.name]


def test_weiterer_admin_darf(me):
    path = backups.create_backup(at(0, 3))
    token = make_member(admin=True)
    me.cookies.clear()
    me.cookies.set("gk_session", token)
    assert me.get(f"/backups/{path.name}").status_code == 200


def test_backup_per_knopf(me):
    r = me.post("/backups/create", data={"csrf_token": csrf_value(me)})
    assert r.status_code == 303 and r.headers["location"] == "/data?backup=1"
    assert len(backup_files()) == 1
    assert "Das Backup wurde erstellt" in me.get("/data?backup=1").text


def test_backup_knopf_begrenzt_und_braucht_csrf(me):
    assert me.post("/backups/create").status_code == 403
    assert backup_files() == []
    for _ in range(5):
        assert me.post("/backups/create", data={"csrf_token": csrf_value(me)}).status_code == 303
    assert me.post("/backups/create", data={"csrf_token": csrf_value(me)}).status_code == 429


def test_backup_knopf_zeigt_fehler(me, monkeypatch):
    def fail(*a, **k):
        raise backups.BackupError("Nicht genug freier Speicherplatz für ein Backup.")

    monkeypatch.setattr(backups, "create_backup", fail)
    r = me.post("/backups/create", data={"csrf_token": csrf_value(me)})
    assert r.status_code == 500 and "Nicht genug freier Speicherplatz" in r.text


def test_backup_herunterladen(me):
    path = backups.create_backup(at(0, 3))
    r = me.get(f"/backups/{path.name}")
    assert r.status_code == 200 and r.content == path.read_bytes()
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("name", [
    "gutscheinkontor-backup-2026-11-17_999999.zip",   # gibt es nicht
    "gutscheinkontor-backup-x.zip", "..%2F..%2Fgutscheinkontor.db", "gutscheinkontor.db",
    "notiz.txt", "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
])
def test_download_nur_echte_backups(me, name):
    backups.create_backup(at(0, 3))
    (Path(get_settings().backups_dir) / "notiz.txt").write_text("geheim")
    assert me.get(f"/backups/{name}").status_code == 404


def test_ohne_login_kein_zugriff(me):
    path = backups.create_backup(at(0, 3))
    me.cookies.clear()
    assert me.get(f"/backups/{path.name}").headers["location"] == "/login"
    assert me.post("/backups/create").headers["location"] == "/login"


# --- Kommandozeile --------------------------------------------------------------------------------------


def test_cli_backup_und_wiederherstellung(me, capsys):
    seed(me)
    before = snapshot()
    assert cli.main(["backup"]) == 0
    name = backup_files()[0]
    assert name in capsys.readouterr().out

    wipe_vouchers()
    assert cli.main(["restore-backup", name]) == 2                   # ohne --yes passiert nichts
    assert snapshot() == []
    assert cli.main(["restore-backup", name, "--yes"]) == 0           # Dateiname genügt
    assert snapshot() == before
    out = capsys.readouterr()
    assert "3 Gutscheine, 1 Nutzer" in out.out and "restore-undo-" in out.out


def test_cli_restore_fehler(me, capsys, tmp_path):
    assert cli.main(["restore-backup", "gibt-es-nicht.zip", "--yes"]) == 1
    bad = write_zip(tmp_path, b"kein zip")
    assert cli.main(["restore-backup", str(bad), "--yes"]) == 1
    assert "keine gültige Backup-Datei" in capsys.readouterr().err
