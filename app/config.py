"""Konfiguration aus Umgebungsvariablen."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Tuple
from zoneinfo import ZoneInfo

SMTP_SECURITY_MODES = ("starttls", "ssl", "none")


def _read_env(name: str) -> str:
    """Wert direkt oder aus einer Datei lesen (NAME_FILE, z. B. für Docker Secrets)."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get(name, "").strip()


def _parse_days(text: str) -> Tuple[int, ...]:
    """'30, 7, 1' -> (30, 7, 1). Ungültiges ergibt ein leeres Tupel, validate() meldet das."""
    try:
        days = {int(part) for part in text.replace(";", ",").split(",") if part.strip()}
    except ValueError:
        return ()
    return tuple(sorted(days, reverse=True))


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
    # Zeitzone für "heute" (Ablaufdaten, später Erinnerungen).
    timezone: str = "Europe/Berlin"
    # Größtes erlaubtes Anhang-Format in MB. Der Reverse Proxy muss gleich große Uploads erlauben.
    max_upload_mb: int = 10
    # Erinnerungsmails: so viele Tage vor Ablauf, ab dieser Uhrzeit (in der Zeitzone oben).
    reminders_enabled: bool = True
    reminder_days: Tuple[int, ...] = (30, 7, 1)
    reminder_hour: int = 8
    # Größte erlaubte Import-Datei (ZIP mit Anhängen) in MB.
    max_import_mb: int = 200
    # Automatische Backups im Datenverzeichnis: täglich ab dieser Uhrzeit, die letzten N behalten.
    backups_enabled: bool = True
    backup_keep: int = 7
    backup_hour: int = 3
    # Logos von der Webseite des Shops holen (ruft fremde Server auf, deshalb standardmäßig aus).
    logo_fetch_enabled: bool = False
    repo_url: str = "https://github.com/teitech/Gutscheinkontor"

    @property
    def max_import_bytes(self) -> int:
        return self.max_import_mb * 1024 * 1024

    @property
    def backups_dir(self) -> str:
        return str(Path(self.data_dir) / "backups")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def attachments_dir(self) -> str:
        return str(Path(self.data_dir) / "attachments")

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
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"TIMEZONE ist unbekannt: {self.timezone}") from exc
        if not 1 <= self.max_upload_mb <= 200:
            raise RuntimeError("MAX_UPLOAD_MB muss zwischen 1 und 200 liegen.")
        if not self.reminder_days or not all(1 <= d <= 365 for d in self.reminder_days):
            raise RuntimeError("REMINDER_DAYS braucht Werte zwischen 1 und 365, z. B. 30,7,1.")
        if not 0 <= self.reminder_hour <= 23:
            raise RuntimeError("REMINDER_HOUR muss zwischen 0 und 23 liegen.")
        if not 1 <= self.max_import_mb <= 2000:
            raise RuntimeError("MAX_IMPORT_MB muss zwischen 1 und 2000 liegen.")
        if not 1 <= self.backup_keep <= 365:
            raise RuntimeError("BACKUP_KEEP muss zwischen 1 und 365 liegen.")
        if not 0 <= self.backup_hour <= 23:
            raise RuntimeError("BACKUP_HOUR muss zwischen 0 und 23 liegen.")


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
        timezone=os.environ.get("TIMEZONE", "Europe/Berlin").strip(),
        max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB") or 10),
        reminders_enabled=os.environ.get("REMINDERS_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        reminder_days=_parse_days(os.environ.get("REMINDER_DAYS", "30,7,1")),
        reminder_hour=int(os.environ.get("REMINDER_HOUR") or 8),
        max_import_mb=int(os.environ.get("MAX_IMPORT_MB") or 200),
        backups_enabled=os.environ.get("BACKUPS_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        backup_keep=int(os.environ.get("BACKUP_KEEP") or 7),
        backup_hour=int(os.environ.get("BACKUP_HOUR") or 3),
        logo_fetch_enabled=os.environ.get("LOGO_FETCH", "0").strip().lower() in ("1", "true", "yes", "on"),
    )
