"""Kommandozeilen-Werkzeuge für Betreiber, z. B. wenn kein Mailserver eingerichtet ist.

Beispiel (Docker):
    docker compose exec gutscheinkontor python -m app.cli reset-password anna@example.de
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

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
    args = parser.parse_args(argv)

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
