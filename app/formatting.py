"""Anzeigeformate auf Deutsch: Beträge, Datum, Restlaufzeit."""
from __future__ import annotations

from datetime import date
from typing import Optional


def format_euro(cents: Optional[int]) -> str:
    if cents is None:
        return ""
    euros, rest = divmod(cents, 100)
    return f"{euros:,}".replace(",", ".") + f",{rest:02d} €"


def format_date(iso: Optional[str]) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso)
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def days_until(expires_on: Optional[str], today: date) -> Optional[int]:
    if not expires_on:
        return None
    return (date.fromisoformat(expires_on) - today).days


def _tage(n: int) -> str:
    return "1 Tag" if n == 1 else f"{n} Tage"


def expiry_text(expires_on: Optional[str], today: date) -> str:
    n = days_until(expires_on, today)
    if n is None:
        return "Kein Ablaufdatum"
    if n < 0:
        return "Seit 1 Tag abgelaufen" if n == -1 else f"Seit {-n} Tagen abgelaufen"
    if n == 0:
        return "Läuft heute ab"
    if n == 1:
        return "Läuft morgen ab"
    return f"Noch {_tage(n)}"


def urgency(expires_on: Optional[str], today: date, finished: bool) -> str:
    """expired, soon (höchstens 7 Tage), ok, none. Erledigte Gutscheine sind immer none."""
    n = days_until(expires_on, today)
    if finished or n is None:
        return "none"
    if n < 0:
        return "expired"
    return "soon" if n <= 7 else "ok"
