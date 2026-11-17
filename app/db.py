"""SQLite-Zugriff und einfache Migrationen (nummerierte SQL-Dateien)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
TMP_MAX_AGE_SECONDS = 3600  # Temp-Reste werden erst nach einer Stunde aufgeräumt


def connect() -> sqlite3.Connection:
    settings = get_settings()
    # isolation_level=None: Autocommit, Transaktionen steuern wir selbst.
    conn = sqlite3.connect(
        settings.db_path, timeout=10, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Schreibtransaktion. BEGIN IMMEDIATE verhindert Wettläufe bei parallelen Schreibzugriffen."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def prepare_data_dir(clean_tmp: bool = False) -> None:
    """Legt das Datenverzeichnis an, prüft die Schreibrechte und richtet ein eigenes Temp-Verzeichnis ein.

    Das Temp-Verzeichnis liegt im Datenverzeichnis statt in /tmp: Im Container ist /tmp ein kleiner
    Arbeitsspeicher-Datenträger, große Exporte und Importe würden sonst den RAM füllen.
    Aufgeräumt wird nur beim Start des Servers (clean_tmp), nie von Kommandozeilenbefehlen, die
    neben einer laufenden App ausgeführt werden.
    """
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".schreibtest"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Das Datenverzeichnis {data_dir} ist nicht beschreibbar ({type(exc).__name__}). "
            f"Der Prozess läuft als Benutzer {os.getuid()}:{os.getgid()}. Bei einem Bind-Mount muss der "
            f"Ordner diesem Benutzer gehören, z. B.: sudo chown -R {os.getuid()}:{os.getgid()} <Ordner>. "
            "Ein benanntes Docker-Volume braucht das nicht."
        ) from exc
    Path(settings.attachments_dir).mkdir(parents=True, exist_ok=True)

    tmp = data_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    if clean_tmp:  # Reste eines abgebrochenen Laufs, aber nie etwas Frisches
        for leftover in tmp.iterdir():
            try:
                if time.time() - leftover.stat().st_mtime < TMP_MAX_AGE_SECONDS:
                    continue
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink(missing_ok=True)
            except OSError:
                continue
    tempfile.tempdir = str(tmp)


def init_db(clean_tmp: bool = False) -> None:
    """Datenverzeichnis vorbereiten und fehlende Migrationen einspielen."""
    prepare_data_dir(clean_tmp)
    conn = connect()
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            number = int(path.name.split("_", 1)[0])
            if number <= current:
                continue
            script = f"BEGIN;\n{path.read_text(encoding='utf-8')}\nPRAGMA user_version = {number};\nCOMMIT;"
            try:
                conn.executescript(script)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI-Dependency: eine Verbindung pro Anfrage."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
