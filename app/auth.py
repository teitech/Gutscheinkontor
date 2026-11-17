"""Sessions, TOTP-2FA und Wiederherstellungscodes."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from typing import List, Optional

import pyotp
import segno

from . import security

ACTIVE_TTL = 14 * 24 * 3600  # aktive Sitzung: 14 Tage
PENDING_TTL = 10 * 60  # Zwischenschritte (2FA-Eingabe, 2FA-Einrichtung): 10 Minuten
TOTP_STEP = 30
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # ohne leicht verwechselbare Zeichen


def _now() -> float:
    return time.time()


# --- Sessions ---------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: sqlite3.Connection, user_id: int, stage: str) -> str:
    token = secrets.token_urlsafe(32)
    ttl = ACTIVE_TTL if stage == "active" else PENDING_TTL
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, stage, expires_at) VALUES (?, ?, ?, ?)",
        (hash_token(token), user_id, stage, int(_now()) + ttl),
    )
    return token


def get_session(db: sqlite3.Connection, token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token:
        return None
    row = db.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now():
        delete_session(db, token)
        return None
    return row


def delete_session(db: sqlite3.Connection, token: Optional[str]) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def purge_expired_sessions(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(_now()),))


def count_failed_attempt(db: sqlite3.Connection, session: sqlite3.Row) -> None:
    db.execute(
        "UPDATE sessions SET failed_attempts = failed_attempts + 1 WHERE token_hash = ?",
        (session["token_hash"],),
    )


# --- Passwort zurücksetzen ----------------------------------------------------

RESET_TTL = 60 * 60  # Reset-Link: 1 Stunde gültig


def create_password_reset(db: sqlite3.Connection, user_id: int) -> str:
    """Neuer Reset-Token. Ältere Links desselben Nutzers werden ungültig."""
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
    db.execute(
        "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (hash_token(token), user_id, int(_now()) + RESET_TTL),
    )
    return token


def find_password_reset(db: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    """Prüft einen Token, ohne ihn zu verbrauchen."""
    if not token:
        return None
    return db.execute(
        "SELECT * FROM password_resets WHERE token_hash = ? AND expires_at > ?",
        (hash_token(token), int(_now())),
    ).fetchone()


def consume_password_reset(db: sqlite3.Connection, token: str) -> Optional[int]:
    """Verbraucht den Token genau einmal. Gibt die Nutzer-ID zurück oder None."""
    row = find_password_reset(db, token)
    if row is None:
        return None
    cur = db.execute("DELETE FROM password_resets WHERE token_hash = ?", (row["token_hash"],))
    return row["user_id"] if cur.rowcount == 1 else None


def reset_credentials(db: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    """Neues Passwort setzen und die Zwei-Faktor-Anmeldung zurücksetzen.

    Alle Sitzungen, Wiederherstellungscodes und weiteren Reset-Links werden ungültig.
    Beim nächsten Login muss die 2FA neu eingerichtet werden.
    """
    db.execute(
        "UPDATE users SET password_hash = ?, totp_secret_enc = NULL, totp_enabled = 0, "
        "totp_last_step = 0 WHERE id = ?",
        (password_hash, user_id),
    )
    db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))


# --- TOTP -------------------------------------------------------------------


def get_or_create_totp_secret(db: sqlite3.Connection, user: sqlite3.Row) -> str:
    if user["totp_secret_enc"]:
        return security.decrypt(user["totp_secret_enc"])
    secret = pyotp.random_base32()
    db.execute(
        "UPDATE users SET totp_secret_enc = ? WHERE id = ?",
        (security.encrypt(secret), user["id"]),
    )
    return secret


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Gutscheinkontor")


def qr_svg(uri: str) -> str:
    return segno.make(uri, error="m").svg_inline(
        scale=4, border=2, dark="#000000", light="#ffffff"
    )


def verify_totp(db: sqlite3.Connection, user: sqlite3.Row, code: str) -> bool:
    """Prüft den Code (Toleranz ±1 Zeitfenster). Ein Zeitfenster gilt nur einmal (Replay-Schutz)."""
    if not user["totp_secret_enc"]:
        return False
    code = code.replace(" ", "").strip()
    if len(code) != 6 or not code.isdigit():
        return False
    totp = pyotp.TOTP(security.decrypt(user["totp_secret_enc"]))
    current = int(_now() // TOTP_STEP)
    for step in (current - 1, current, current + 1):
        if step <= user["totp_last_step"]:
            continue
        if hmac.compare_digest(totp.at(step * TOTP_STEP), code):
            db.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user["id"]))
            return True
    return False


# --- Wiederherstellungscodes -------------------------------------------------


def _normalize_code(code: str) -> str:
    return "".join(c for c in code.lower() if c.isalnum())


def _code_hash(normalized: str) -> str:
    key = security.derive(b"gk-recovery-v1")
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_recovery_codes(db: sqlite3.Connection, user_id: int, count: int = 10) -> List[str]:
    """Ersetzt alle bisherigen Codes. Die Klartext-Codes werden nur hier zurückgegeben."""
    db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
        db.execute(
            "INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
            (user_id, _code_hash(raw)),
        )
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def use_recovery_code(db: sqlite3.Connection, user_id: int, code: str) -> bool:
    normalized = _normalize_code(code)
    if len(normalized) != 10:
        return False
    row = db.execute(
        "SELECT id FROM recovery_codes WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
        (user_id, _code_hash(normalized)),
    ).fetchone()
    if row is None:
        return False
    cur = db.execute(
        "UPDATE recovery_codes SET used_at = datetime('now') WHERE id = ? AND used_at IS NULL",
        (row["id"],),
    )
    return cur.rowcount == 1
