"""Datum von "heute" in der konfigurierten Zeitzone."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import get_settings


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()
