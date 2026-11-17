"""Kommandozeilen-Werkzeuge für Betreiber, z. B. wenn kein Mailserver eingerichtet ist.

Beispiel (Docker):
    docker compose exec gutscheinkontor python -m app.cli reset-password anna@example.de
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import backups
from .auth import RESET_TTL, create_password_reset
from .config import get_settings
from .db import connect, init_db, transaction


def create_reset_link(email: str) -> Optional[str]:
    """Erzeugt einen Reset-Link für das Konto. Gibt None zurück, wenn es das Konto nicht gibt."""
    settings = get_settings()
    settings.validate()
    init_db()
    db = connect()
    try:
        user = db.execute(
            "SELECT id FROM users WHERE email = ? AND is_active = 1", (email.strip(),)
        ).fetchone()
        if user is None:
            return None
        with transaction(db):
            token = create_password_reset(db, user["id"])
        return f"{settings.base_url}/password-reset/{token}"
    finally:
        db.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-password", help="Reset-Link für ein Konto erzeugen")
    reset.add_argument("email")
    sub.add_parser("backup", help="Jetzt ein Backup erstellen")
    restore = sub.add_parser(
        "restore-backup",
        help="Backup einspielen (nur bei gestoppter App!). Der bisherige Stand wird zur Seite gelegt.",
    )
    restore.add_argument("file", help="Dateiname im Backup-Ordner oder voller Pfad")
    restore.add_argument("--yes", action="store_true", help="Bestätigt, dass die App gestoppt ist")
    restore.add_argument("--force", action="store_true",
                         help="Auch bei nicht passendem SECRET_KEY einspielen (Codes und 2FA bleiben unlesbar)")
    args = parser.parse_args(argv)

    if args.command == "backup":
        get_settings().validate()
        init_db()
        try:
            path = backups.create_backup()
        except backups.BackupError as exc:
            print(f"Backup fehlgeschlagen: {exc}", file=sys.stderr)
            return 1
        print(f"Backup erstellt: {path}")
        return 0

    if args.command == "restore-backup":
        get_settings().validate()
        if not args.yes:
            print("Das ersetzt alle Daten durch den Stand aus dem Backup.\n"
                  "Stoppe zuerst die App (docker compose stop) und starte den Befehl dann mit --yes.",
                  file=sys.stderr)
            return 2
        path = Path(args.file)
        if not path.is_absolute() and not path.exists():
            path = backups.backups_dir() / args.file
        if not path.is_file():
            print(f"Datei nicht gefunden: {args.file}", file=sys.stderr)
            return 1
        try:
            result = backups.restore_backup(path, force=args.force)
        except backups.BackupError as exc:
            print(f"Wiederherstellung abgebrochen: {exc}", file=sys.stderr)
            return 1
        print(f"Wiederhergestellt: {result['vouchers']} Gutscheine, {result['users']} Nutzer.")
        print(f"Der bisherige Stand liegt unter {result['undo']} und kann nach einer Prüfung gelöscht werden.")
        return 0

    if args.command == "reset-password":
        link = create_reset_link(args.email)
        if link is None:
            print("Kein aktives Konto mit dieser E-Mail-Adresse gefunden.", file=sys.stderr)
            return 1
        print(f"Reset-Link (einmal nutzbar, {RESET_TTL // 60} Minuten gültig):")
        print(link)
        print("Beim Zurücksetzen wird die Zwei-Faktor-Anmeldung des Kontos zurückgesetzt.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
