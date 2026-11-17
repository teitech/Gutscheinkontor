"""Konfiguration aus Umgebungsvariablen."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _read_secret() -> str:
    """SECRET_KEY direkt oder aus einer Datei (Docker Secret) lesen."""
    path = os.environ.get("SECRET_KEY_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get("SECRET_KEY", "").strip()


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


@lru_cache
def get_settings() -> Settings:
    return Settings(
        version=os.environ.get("APP_VERSION", "dev"),
        git_sha=os.environ.get("GIT_SHA", ""),
        base_url=os.environ.get("BASE_URL", "http://localhost:8000"),
        data_dir=os.environ.get("DATA_DIR", "./data"),
        secret_key=_read_secret(),
        setup_token=os.environ.get("SETUP_TOKEN", "").strip(),
    )
