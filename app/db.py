"""SQLite-Zugriff und einfache Migrationen (nummerierte SQL-Dateien)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


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


def init_db() -> None:
    """Datenverzeichnis anlegen und fehlende Migrationen einspielen."""
    settings = get_settings()
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
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
