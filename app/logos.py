"""Logos: Bilder prüfen und zu kleinen PNGs umrechnen.

Jedes Bild, egal ob hochgeladen oder von einer Webseite geholt, wird vollständig dekodiert und als
neues PNG gespeichert. Gespeichert wird nie das Original. So kommen weder Metadaten noch
eingebettete Inhalte mit, und ein beschädigtes oder bösartiges Bild scheitert hier.
"""
from __future__ import annotations

import base64
import io
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional

from PIL import Image

MAX_INPUT_BYTES = 2 * 1024 * 1024   # größte erlaubte Bilddatei
MAX_PIXELS = 25_000_000             # Schutz vor Bildern, die im Speicher riesig würden
OUT_SIZE = 128                      # längste Kante des gespeicherten Logos
MIN_SIZE = 32                       # kleinere Bilder (z. B. 16x16-Favicons) sehen verwaschen aus
FORMATS = ["PNG", "JPEG", "WEBP", "GIF", "ICO"]
MAX_DATA_FIELD = 400_000            # größte Länge eines Base64-Felds aus dem Auswahlformular


class LogoError(Exception):
    """Fehler mit einer Meldung, die dem Nutzer angezeigt werden darf."""


def process(data: bytes) -> bytes:
    """Bild prüfen und als PNG (längste Kante höchstens OUT_SIZE) zurückgeben."""
    if not data:
        raise LogoError("Die Datei ist leer.")
    if len(data) > MAX_INPUT_BYTES:
        raise LogoError("Das Bild ist zu groß (höchstens 2 MB).")
    try:
        image = Image.open(io.BytesIO(data), formats=FORMATS)
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise LogoError("Das Bild hat zu viele Pixel.")
        if max(width, height) < MIN_SIZE:
            raise LogoError(f"Das Bild ist zu klein (mindestens {MIN_SIZE} Pixel).")
        image.load()
        image = image.convert("RGBA")
    except LogoError:
        raise
    except Exception as exc:  # noqa: BLE001 - jedes Problem beim Lesen heißt: kein brauchbares Bild
        raise LogoError("Das ist kein lesbares Bild (erlaubt: PNG, JPEG, WebP, GIF, ICO).") from exc
    image.thumbnail((OUT_SIZE, OUT_SIZE), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def to_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def from_form_field(value: str) -> bytes:
    """Liest ein Base64-Feld aus dem Auswahlformular und verarbeitet es wie jeden Upload."""
    prefix = "data:image/png;base64,"
    if not value.startswith(prefix) or len(value) > MAX_DATA_FIELD:
        raise LogoError("Das Bild ist ungültig.")
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
    except Exception as exc:  # noqa: BLE001
        raise LogoError("Das Bild ist ungültig.") from exc
    return process(raw)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(db: sqlite3.Connection, voucher_id: int, png: bytes) -> None:
    db.execute(
        "INSERT INTO voucher_logos (voucher_id, png, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(voucher_id) DO UPDATE SET png = excluded.png, updated_at = excluded.updated_at",
        (voucher_id, png, _now()),
    )


def get(db: sqlite3.Connection, voucher_id: int) -> Optional[bytes]:
    row = db.execute("SELECT png FROM voucher_logos WHERE voucher_id = ?", (voucher_id,)).fetchone()
    return bytes(row["png"]) if row else None


def delete(db: sqlite3.Connection, voucher_id: int) -> None:
    db.execute("DELETE FROM voucher_logos WHERE voucher_id = ?", (voucher_id,))


def all_logos(db: sqlite3.Connection) -> Dict[int, bytes]:
    return {r["voucher_id"]: bytes(r["png"]) for r in db.execute("SELECT voucher_id, png FROM voucher_logos")}
