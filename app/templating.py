"""Gemeinsame Template-Umgebung."""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import mail
from .config import get_settings

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Vorbereitung für Mehrsprachigkeit: Texte laufen über _(). Vorerst nur Deutsch.
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_null_translations()
templates.env.globals["settings"] = get_settings()


class Redirect(Exception):
    """Wird in Dependencies geworfen, um auf eine andere Seite umzuleiten."""

    def __init__(self, url: str) -> None:
        self.url = url


def render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("csrf", getattr(request.state, "csrf", ""))
    context.setdefault("user", getattr(request.state, "user", None))
    context.setdefault("mail_enabled", mail.is_enabled())
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    # Seiten mit Formularen oder persönlichen Daten nicht zwischenspeichern.
    response.headers["Cache-Control"] = "no-store"
    return response
