"""Automatische Backups und Wiederherstellung.

Ein Backup ist ein ZIP im Datenverzeichnis (backups/) mit:
- einem konsistenten Abbild der Datenbank (SQLite-Backup-Schnittstelle, auch bei laufendem Betrieb),
- allen Anhängen, die in diesem Abbild stehen,
- backup.json mit Version und Zählern.

Anders als der Export enthält ein Backup die ganze Datenbank, also auch Konten, Passwort-Hashes und
verschlüsselte 2FA-Geheimnisse. Es lässt sich nur mit dem passenden SECRET_KEY nutzen. Backups
dürfen deshalb nur Administratoren sehen. Sie liegen im selben Volume wie die Daten und schützen
vor Bedienfehlern und Datenbankschäden, nicht vor dem Verlust des Datenträgers: Sichere das
Datenverzeichnis zusätzlich außerhalb (z. B. mit Restic oder Duplicati).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from starlette.concurrency import run_in_threadpool

from . import attachments, security
from .config import get_settings
from .db import MIGRATIONS_DIR, connect, init_db

logger = logging.getLogger("gutscheinkontor.backups")

FORMAT_NAME = "gutscheinkontor-backup"
FORMAT_VERSION = 1
MANIFEST_MEMBER = "backup.json"
DB_MEMBER = "gutscheinkontor.db"
ATTACHMENT_PREFIX = "attachments/"
NAME_RE = re.compile(r"^gutscheinkontor-backup-(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})\.zip$")
STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}$")

TICK_SECONDS = 30 * 60
STALE_TMP_SECONDS = 6 * 3600
MAX_RESTORE_MEMBERS = 100_000
MAX_RESTORE_BYTES = 8 * 1024 ** 3


class BackupError(Exception):
    """Fehler mit einer Meldung, die angezeigt werden darf."""


class RestoreError(BackupError):
    """Das Backup lässt sich nicht wiederherstellen."""


class Status:
    """Letzter Stand des automatischen Laufs (nur zur Anzeige für Administratoren)."""

    last_error: Optional[str] = None


status = Status()


@dataclass
class BackupInfo:
    name: str
    created: datetime  # lokale Zeit laut Dateiname
    size_bytes: int


def backups_dir() -> Path:
    return Path(get_settings().backups_dir)


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


# --- Auflisten und Aufräumen -------------------------------------------------------------


def list_backups() -> List[BackupInfo]:
    """Alle Backups, das neueste zuerst."""
    directory = backups_dir()
    if not directory.is_dir():
        return []
    result = []
    for path in directory.iterdir():
        match = NAME_RE.match(path.name)
        if match and path.is_file():
            created = datetime(*(int(g) for g in match.groups()))
            result.append(BackupInfo(path.name, created, path.stat().st_size))
    result.sort(key=lambda b: b.created, reverse=True)
    return result


def backup_path(name: str) -> Optional[Path]:
    """Pfad zu einem Backup nach Name. None, wenn der Name nicht dem Muster entspricht oder fehlt."""
    if not NAME_RE.match(name):
        return None
    path = backups_dir() / name
    return path if path.is_file() else None


def prune(keep: Optional[int] = None) -> None:
    """Behält die neuesten `keep` Backups und räumt liegen gebliebene Arbeitsordner auf."""
    keep = keep if keep is not None else get_settings().backup_keep
    for old in list_backups()[keep:]:
        (backups_dir() / old.name).unlink(missing_ok=True)
    directory = backups_dir()
    if directory.is_dir():
        for path in directory.iterdir():
            if path.is_dir() and path.name.startswith(".tmp-"):
                if time.time() - path.stat().st_mtime > STALE_TMP_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)


# --- Erstellen ----------------------------------------------------------------------------------


def create_backup(now: Optional[datetime] = None) -> Path:
    """Erstellt ein Backup und räumt danach alte auf. Gibt den Pfad zurück."""
    settings = get_settings()
    now = now or _local_now()
    directory = backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    work = Path(tempfile.mkdtemp(prefix=".tmp-", dir=directory))  # gleiche Platte: Umbenennen ist atomar
    source = connect()
    snapshot = None
    try:
        snapshot_path = work / "snapshot.db"
        snapshot = sqlite3.connect(snapshot_path)
        source.backup(snapshot)  # konsistentes Abbild, auch während andere schreiben
        snapshot.row_factory = sqlite3.Row
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("Die Datenbank hat die Integritätsprüfung nicht bestanden.")
        schema_version = snapshot.execute("PRAGMA user_version").fetchone()[0]
        voucher_count = snapshot.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0]
        files = [r["stored_name"] for r in snapshot.execute("SELECT stored_name FROM voucher_attachments")]
        snapshot.close()
        snapshot = None

        # Platz prüfen, bevor wir viel schreiben.
        needed = snapshot_path.stat().st_size
        present = []
        for name in files:
            path = attachments.path_for(name)
            if path is not None and path.is_file():
                present.append((name, path))
                needed += path.stat().st_size
        free = shutil.disk_usage(directory).free
        if free < needed * 1.1 + 10 * 1024 * 1024:
            raise BackupError("Nicht genug freier Speicherplatz für ein Backup.")

        archive_path = work / "backup.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(snapshot_path, DB_MEMBER, compress_type=zipfile.ZIP_DEFLATED)
            for name, path in present:
                archive.write(path, ATTACHMENT_PREFIX + name, compress_type=zipfile.ZIP_STORED)
            manifest = {
                "format": FORMAT_NAME,
                "format_version": FORMAT_VERSION,
                "app_version": settings.version,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "schema_version": schema_version,
                "voucher_count": voucher_count,
                "attachment_count": len(present),
                "attachments_missing": len(files) - len(present),
            }
            archive.writestr(MANIFEST_MEMBER, json.dumps(manifest, indent=2),
                             compress_type=zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(archive_path) as check:
            if check.testzip() is not None:
                raise BackupError("Das fertige Backup ist beschädigt.")

        final = directory / f"gutscheinkontor-backup-{now:%Y-%m-%d_%H%M%S}.zip"
        os.chmod(archive_path, 0o600)  # enthält Passwort-Hashes und verschlüsselte Geheimnisse
        os.replace(archive_path, final)
    finally:
        if snapshot is not None:
            snapshot.close()
        source.close()
        shutil.rmtree(work, ignore_errors=True)
    prune(settings.backup_keep)
    return final


def run_once(now: Optional[datetime] = None) -> Optional[Path]:
    """Legt das tägliche Backup an, falls ab der Uhrzeit heute noch keines existiert."""
    settings = get_settings()
    if not settings.backups_enabled:
        return None
    now = now or _local_now()
    if now.hour < settings.backup_hour:
        return None
    if any(b.created.date() == now.date() for b in list_backups()):
        return None
    path = create_backup(now)
    status.last_error = None
    logger.info("Backup erstellt.")
    return path


async def scheduler(stop: asyncio.Event, tick: float = TICK_SECONDS) -> None:
    """Prüft regelmäßig, ob das tägliche Backup fällig ist, bis `stop` gesetzt wird."""
    if not get_settings().backups_enabled:
        logger.info("Automatische Backups sind ausgeschaltet.")
        return
    while not stop.is_set():
        try:
            await run_in_threadpool(run_once)
        except Exception as exc:  # noqa: BLE001 - der Planer darf nie abstürzen
            # Eigene Meldungen sind unkritisch, bei allem anderen nur der Typ (keine Pfade, keine Daten).
            detail = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
            status.last_error = detail
            logger.error("Backup fehlgeschlagen (%s).", detail)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass


# --- Wiederherstellen -------------------------------------------------------------------------------


def _latest_schema() -> int:
    return max(int(p.name.split("_", 1)[0]) for p in MIGRATIONS_DIR.glob("*.sql"))


def _key_matches(db: sqlite3.Connection) -> bool:
    """Prüft mit einem verschlüsselten Wert aus dem Backup, ob der SECRET_KEY dazu passt."""
    for query in ("SELECT totp_secret_enc FROM users WHERE totp_secret_enc IS NOT NULL LIMIT 1",
                  "SELECT code_enc FROM vouchers WHERE code_enc IS NOT NULL LIMIT 1"):
        row = db.execute(query).fetchone()
        if row is not None:
            try:
                security.decrypt(row[0])
            except Exception:  # noqa: BLE001
                return False
            return True
    return True  # nichts Verschlüsseltes vorhanden


def _extract_limited(archive: zipfile.ZipFile, member: str, target: Path, budget: List[int]) -> None:
    """Entpackt ein Mitglied und zählt die tatsächlich gelesenen Bytes (nicht den Kopfangaben trauen)."""
    with archive.open(member) as source, open(target, "xb") as out:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise RestoreError("Das Backup ist entpackt zu groß.")
            out.write(chunk)


def restore_backup(path: Path, *, force: bool = False) -> Dict[str, Any]:
    """Ersetzt Datenbank und Anhänge durch den Stand aus dem Backup.

    NUR bei gestoppter App aufrufen. Der bisherige Stand wird nicht gelöscht, sondern nach
    restore-undo-<Zeit>/ im Datenverzeichnis verschoben.
    """
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise RestoreError("Das ist keine gültige Backup-Datei.") from exc

    staging = data_dir / f".restore-staging-{datetime.now():%Y%m%d-%H%M%S}"
    staged_db = staging / DB_MEMBER
    staged_files = staging / "attachments"
    try:
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_RESTORE_MEMBERS:
                raise RestoreError("Das Backup enthält zu viele Dateien.")
            for info in infos:
                name = info.filename
                allowed = name in (DB_MEMBER, MANIFEST_MEMBER) or (
                    name.startswith(ATTACHMENT_PREFIX) and STORED_NAME_RE.match(name[len(ATTACHMENT_PREFIX):])
                )
                if not allowed:
                    raise RestoreError("Das Backup enthält unerwartete Dateien und wird abgelehnt.")
            names = {i.filename for i in infos}
            if MANIFEST_MEMBER not in names or DB_MEMBER not in names:
                raise RestoreError("Das ist kein Backup des Gutscheinkontors.")
            try:
                with archive.open(MANIFEST_MEMBER) as member:
                    raw = member.read(1024 * 1024 + 1)  # nie mehr als 1 MB einlesen
                if len(raw) > 1024 * 1024:
                    raise ValueError("zu groß")
                manifest = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError, zipfile.BadZipFile) as exc:
                raise RestoreError("backup.json ist nicht lesbar.") from exc
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
                raise RestoreError("Das ist kein Backup des Gutscheinkontors.")
            version = manifest.get("format_version")
            if isinstance(version, bool) or not isinstance(version, int) or version > FORMAT_VERSION:
                raise RestoreError("Dieses Backup hat ein unbekanntes oder neueres Format.")

            staging.mkdir(parents=True)
            staged_files.mkdir()
            budget = [MAX_RESTORE_BYTES]
            _extract_limited(archive, DB_MEMBER, staged_db, budget)
            for info in infos:
                if info.filename.startswith(ATTACHMENT_PREFIX):
                    _extract_limited(archive, info.filename,
                                     staged_files / info.filename[len(ATTACHMENT_PREFIX):], budget)

        check = sqlite3.connect(staged_db)
        try:
            check.row_factory = sqlite3.Row
            try:
                intact = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                schema = check.execute("PRAGMA user_version").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise RestoreError("Die Datenbank im Backup ist beschädigt.") from exc
            if not intact:
                raise RestoreError("Die Datenbank im Backup ist beschädigt.")
            if schema > _latest_schema():
                raise RestoreError("Das Backup stammt aus einer neueren Version. Bitte aktualisiere das Gutscheinkontor.")
            if not force and not _key_matches(check):
                raise RestoreError(
                    "Der SECRET_KEY passt nicht zu diesem Backup. Mit dem falschen Schlüssel wären "
                    "Gutscheincodes und 2FA nicht lesbar. Nutze den ursprünglichen Schlüssel (oder --force)."
                )
            vouchers = check.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0]
            users = check.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            check.close()

        # Austausch: bisherigen Stand zur Seite legen, dann das Backup einsetzen.
        undo = data_dir / f"restore-undo-{datetime.now():%Y%m%d-%H%M%S}"
        undo.mkdir()
        db_path = Path(settings.db_path)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                shutil.move(str(candidate), undo / candidate.name)
        attachments_dir = Path(settings.attachments_dir)
        if attachments_dir.exists():
            shutil.move(str(attachments_dir), undo / "attachments")
        shutil.move(str(staged_db), str(db_path))
        shutil.move(str(staged_files), str(attachments_dir))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    init_db()  # ältere Backups auf den aktuellen Stand der Tabellen bringen
    return {"vouchers": vouchers, "users": users, "undo": str(undo)}
