"""Sicherheitsbausteine: Passwort-Hashing, Schlüsselableitung, Verschlüsselung, CSRF."""
from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet

from .config import get_settings

_hasher = PasswordHasher()  # Argon2id mit empfohlenen Standardwerten
# Wird geprüft, wenn die E-Mail unbekannt ist: gleiche Antwortzeit, keine Nutzer-Erkennung.
DUMMY_HASH = _hasher.hash("gutscheinkontor-dummy-passwort")

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def derive(label: bytes) -> bytes:
    """Aus dem Hauptschlüssel einen eigenen Teilschlüssel pro Verwendungszweck ableiten."""
    secret = get_settings().secret_key.encode("utf-8")
    return hmac.new(secret, label, hashlib.sha256).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive(b"gk-encryption-v1")))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def csrf_field_value(cookie_token: str) -> str:
    """Wert für das versteckte Formularfeld, abgeleitet vom Cookie-Token."""
    return hmac.new(derive(b"gk-csrf-v1"), cookie_token.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_valid(expected: str, submitted: str) -> bool:
    return bool(submitted) and hmac.compare_digest(expected, submitted)


def origin_ok(headers, base_url: str) -> bool:
    """Zusätzlicher Schutz: Origin oder Referer muss zur eigenen Adresse passen."""
    origin = headers.get("origin")
    if not origin:
        referer = headers.get("referer")
        if not referer:
            return True  # manche Browser/Datenschutz-Einstellungen senden nichts, das Token schützt weiter
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}"
    netloc = urlparse(origin).netloc
    allowed = {headers.get("host", ""), urlparse(base_url).netloc}
    return netloc in allowed
