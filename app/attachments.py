"""Anhänge: prüfen, speichern, löschen.

Geprüft wird der tatsächliche Dateiinhalt (Dateikopf), nicht der Name oder der vom Browser
gemeldete Typ. Das ist keine Virenprüfung. Erlaubt sind nur PDF, PNG, JPEG und WebP.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import get_settings

MAX_ATTACHMENTS_PER_VOUCHER = 10
CHUNK = 1024 * 1024

# Aktive Inhalte in PDFs: JavaScript, Programmstart, eingebettete Dateien, Multimedia.
# Bewusst nur lange Marker: kurze würden in komprimierten Datenströmen zufällig vorkommen.
_PDF_ACTIVE_CONTENT = (b"/JavaScript", b"/Launch", b"/EmbeddedFile", b"/RichMedia")


class UploadError(Exception):
    """Fehler mit einer Meldung, die dem Nutzer angezeigt werden darf."""


@dataclass
class Saved:
    stored_name: str
    original_name: str
    content_type: str
    size_bytes: int


def detect_type(head: bytes) -> Optional[str]:
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


_EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def clean_filename(name: str, content_type: str) -> str:
    """Nur den Dateinamen ohne Pfad behalten, Steuerzeichen und Sonderzeichen entfernen."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')
    base = base.strip(" .")[:100]
    return base or "anhang" + _EXTENSIONS[content_type]


def storage_dir() -> Path:
    return Path(get_settings().attachments_dir)


def path_for(stored_name: str) -> Optional[Path]:
    """Pfad zur gespeicherten Datei. None, falls der Name nicht dem erwarteten Muster entspricht."""
    if not re.fullmatch(r"[0-9a-f]{32}", stored_name):
        return None
    return storage_dir() / stored_name


def delete_files(stored_names: Iterable[str]) -> None:
    for name in stored_names:
        path = path_for(name)
        if path is not None:
            path.unlink(missing_ok=True)


def _has_active_pdf_content(path: Path) -> bool:
    data = path.read_bytes()
    return any(marker in data for marker in _PDF_ACTIVE_CONTENT)


async def save_upload(upload, max_bytes: int) -> Saved:
    """Prüft und speichert eine hochgeladene Datei. Wirft UploadError bei Problemen."""
    head = await upload.read(16)
    content_type = detect_type(head)
    if content_type is None:
        raise UploadError("Erlaubt sind nur PDF-, PNG-, JPEG- und WebP-Dateien.")
    await upload.seek(0)

    storage_dir().mkdir(parents=True, exist_ok=True)
    stored_name = secrets.token_hex(16)
    path = storage_dir() / stored_name
    size = 0
    try:
        with open(path, "xb") as out:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadError(
                        f"Die Datei ist größer als {max_bytes // (1024 * 1024)} MB."
                    )
                out.write(chunk)
        if size == 0:
            raise UploadError("Die Datei ist leer.")
        if content_type == "application/pdf" and _has_active_pdf_content(path):
            raise UploadError("Dieses PDF enthält aktive Inhalte (z. B. JavaScript) und wird abgelehnt.")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Saved(stored_name, clean_filename(upload.filename or "", content_type), content_type, size)
