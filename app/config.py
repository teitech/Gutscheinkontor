"""Konfiguration aus Umgebungsvariablen."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SMTP_SECURITY_MODES = ("starttls", "ssl", "none")


def _read_env(name: str) -> str:
    """Wert direkt oder aus einer Datei lesen (NAME_FILE, z. B. für Docker Secrets)."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get(name, "").strip()


@dataclass(frozen=True)
class Settings:
    # Wird beim Docker-Build gesetzt (Git-Tag und Commit), lokal "dev".
    version: str
    git_sha: str
    # Öffentliche URL hinter dem Reverse Proxy, z. B. https://gutscheine.example.de
    base_url: str
    data_dir: str
    # Hauptschlüssel. Daraus werden CSRF-, Verschlüsselungs- und Code-Schlüssel abgeleitet.
    secret_key: str
    # Optional: Schutz der Ersteinrichtung, falls die Instanz schon öffentlich erreichbar ist.
    setup_token: str
    # E-Mail-Versand (SMTP). Ohne smtp_host und smtp_from bleibt der Versand ausgeschaltet.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_security: str = "starttls"
    repo_url: str = "https://github.com/teitech/Gutscheinkontor"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.lower().startswith("https://")

    @property
    def cookie_prefix(self) -> str:
        # __Host- erzwingt Secure, Path=/ und keine Domain. Nur mit HTTPS möglich.
        return "__Host-" if self.secure_cookies else ""

    @property
    def session_cookie(self) -> str:
        return self.cookie_prefix + "gk_session"

    @property
    def csrf_cookie(self) -> str:
        return self.cookie_prefix + "gk_csrf"

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "gutscheinkontor.db")

    def validate(self) -> None:
        if len(self.secret_key) < 32:
            raise RuntimeError(
                "SECRET_KEY fehlt oder ist zu kurz (mindestens 32 Zeichen). "
                "Erzeugen mit: openssl rand -base64 48"
            )
        if self.smtp_security not in SMTP_SECURITY_MODES:
            raise RuntimeError(
                f"SMTP_SECURITY muss einer von {', '.join(SMTP_SECURITY_MODES)} sein."
            )


@lru_cache
def get_settings() -> Settings:
    security = (os.environ.get("SMTP_SECURITY") or "starttls").strip().lower()
    default_port = 465 if security == "ssl" else 587
    return Settings(
        version=os.environ.get("APP_VERSION", "dev"),
        git_sha=os.environ.get("GIT_SHA", ""),
        base_url=os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/"),
        data_dir=os.environ.get("DATA_DIR", "./data"),
        secret_key=_read_env("SECRET_KEY"),
        setup_token=os.environ.get("SETUP_TOKEN", "").strip(),
        smtp_host=os.environ.get("SMTP_HOST", "").strip(),
        smtp_port=int(os.environ.get("SMTP_PORT") or default_port),
        smtp_user=os.environ.get("SMTP_USER", "").strip(),
        smtp_password=_read_env("SMTP_PASSWORD"),
        smtp_from=os.environ.get("SMTP_FROM", "").strip(),
        smtp_security=security,
    )
