"""Export und Import: ZIP mit JSON und Anhängen (vollständig), CSV (zum Ansehen).

Der ZIP-Export ist die Sicherung. Er enthält Gutscheine, Schlagwörter und Anhänge, aber keine
Nutzerkonten, Passwörter oder 2FA-Daten. Personen werden über ihre E-Mail-Adresse zugeordnet.

Der Import nimmt nur dieses eigene Format an und fügt nur hinzu: Bestehende Gutscheine werden
nie verändert oder gelöscht. Weil die Datei von außen kommt, wird sie wie eine fremde Eingabe
behandelt: Größen, Anzahl und Typen werden begrenzt, Namen aus der JSON-Datei werden nie als
Pfad verwendet, und jede Datei durchläuft dieselbe Prüfung wie ein normaler Upload.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from . import attachments, dates, formatting, logos, vouchers
from .config import get_settings
from .db import transaction

FORMAT_NAME = "gutscheinkontor-export"
FORMAT_VERSION = 4  # 2: Art und Mindestbestellwert, 3: Shop-Adresse und "nur online", 4: Logos
JSON_MEMBER = "export.json"

MAX_JSON_BYTES = 20 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_VOUCHERS = 10_000
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # Obergrenze für alle entpackten Daten zusammen
MAX_REPORT_LINES = 50

LOGO_MEMBER_RE = re.compile(r"^logos/\d{4}\.png$")
ATTACHMENT_MEMBER_RE = re.compile(r"^attachments/\d{4}_\d{2}\.(pdf|png|jpg|webp)$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
VALID_STATES = ("open", "redeemed", "archived")


class ImportFormatError(Exception):
    """Die Datei ist nicht importierbar. Die Meldung darf dem Nutzer angezeigt werden."""


class _DryRun(Exception):
    """Interne Unterbrechung, damit ein Probelauf zurückgerollt wird."""


def _money_text(cents: Optional[int]) -> str:
    return "" if cents is None else f"{cents // 100},{cents % 100:02d}"


# --- Export ----------------------------------------------------------------------------


def _load_for_export(db: sqlite3.Connection):
    rows = db.execute(
        "SELECT v.*, u.email AS owner_email FROM vouchers v "
        "LEFT JOIN users u ON u.id = v.owner_user_id ORDER BY v.id"
    ).fetchall()
    tags: Dict[int, List[str]] = {}
    for r in db.execute("SELECT voucher_id, tag FROM voucher_tags ORDER BY tag COLLATE NOCASE"):
        tags.setdefault(r["voucher_id"], []).append(r["tag"])
    files: Dict[int, List[sqlite3.Row]] = {}
    for r in db.execute("SELECT * FROM voucher_attachments ORDER BY voucher_id, id"):
        files.setdefault(r["voucher_id"], []).append(r)
    return rows, tags, files


def build_export_zip(db: sqlite3.Connection, include_codes: bool):
    """Baut das ZIP in einer temporären Datei. Der Aufrufer schließt sie nach dem Senden."""
    rows, tags, files = _load_for_export(db)
    logo_images = logos.all_logos(db)
    tmp = tempfile.TemporaryFile()
    items = []
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, v in enumerate(rows, start=1):
            code = ""
            if include_codes:
                code, readable = vouchers.decrypt_code(v)
                if not readable:
                    code = ""
            listed = []
            logo_member = None
            if v["id"] in logo_images:
                logo_member = f"logos/{index:04d}.png"
                archive.writestr(logo_member, logo_images[v["id"]], compress_type=zipfile.ZIP_STORED)
            for n, a in enumerate(files.get(v["id"], []), start=1):
                path = attachments.path_for(a["stored_name"])
                if path is None or not path.is_file():
                    continue  # Datei fehlt auf der Platte: nicht exportieren statt abzubrechen
                member = f"attachments/{index:04d}_{n:02d}{attachments.EXTENSIONS[a['content_type']]}"
                # Bilder und PDFs sind schon komprimiert: nur ablegen, nicht erneut packen.
                archive.write(path, member, compress_type=zipfile.ZIP_STORED)
                listed.append({
                    "file": member,
                    "original_name": a["original_name"],
                    "content_type": a["content_type"],
                    "size_bytes": a["size_bytes"],
                })
            items.append({
                "title": v["title"],
                "code": code if include_codes else None,
                "value_cents": v["value_cents"],
                "remaining_cents": v["remaining_cents"],
                "kind": v["kind"],
                "percent": v["percent"],
                "benefit_text": v["benefit_text"],
                "min_order_cents": v["min_order_cents"],
                "shop_url": v["shop_url"],
                "online_only": bool(v["online_only"]),
                "expires_on": v["expires_on"],
                "state": v["state"],
                "owner_email": v["owner_email"],
                "notes": v["notes"],
                "tags": tags.get(v["id"], []),
                "created_at": v["created_at"],
                "updated_at": v["updated_at"],
                "logo": logo_member,
                "attachments": listed,
            })
        document = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "app_version": get_settings().version,
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "includes_codes": include_codes,
            "vouchers": items,
        }
        archive.writestr(JSON_MEMBER, json.dumps(document, ensure_ascii=False, indent=2),
                         compress_type=zipfile.ZIP_DEFLATED)
    tmp.seek(0)
    return tmp


CSV_HEADER = ["Name", "Code", "Art", "Wert", "Restwert", "Prozent", "Vorteil (Text)",
              "Mindestbestellwert", "Nur online", "Shop-Adresse", "Ablaufdatum", "Status", "Zugehörig zu",
              "Schlagwörter", "Notizen", "Angelegt am"]


def _csv_safe(text: str) -> str:
    """Schützt vor Formeln: Excel und Co. führen Zellen aus, die mit = + - @ beginnen."""
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def build_csv(db: sqlite3.Connection, include_codes: bool) -> bytes:
    """UTF-8 mit BOM und Semikolon, damit Excel (deutsch) Umlaute und Spalten richtig liest."""
    today = dates.today()
    rows, tags, _files = _load_for_export(db)
    owner_names = {
        r["id"]: r["display_name"] for r in db.execute("SELECT id, display_name FROM users")
    }
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for v in rows:
        code = ""
        if include_codes:
            code, readable = vouchers.decrypt_code(v)
            code = code if readable else ""
        writer.writerow([_csv_safe(c) for c in [
            v["title"],
            code,
            dict(vouchers.KINDS)[v["kind"]],
            _money_text(v["value_cents"]),
            _money_text(v["remaining_cents"]),
            str(v["percent"] or ""),
            v["benefit_text"],
            _money_text(v["min_order_cents"]),
            "ja" if v["online_only"] else "",
            v["shop_url"],
            formatting.format_date(v["expires_on"]),
            vouchers.STATUS_LABELS[vouchers.status_key(v, today)],
            owner_names.get(v["owner_user_id"], ""),
            ", ".join(tags.get(v["id"], [])),
            v["notes"].replace("\r\n", "\n"),
            formatting.format_date(v["created_at"][:10]),
        ]])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


# --- Import ----------------------------------------------------------------------------


@dataclass
class Report:
    dry_run: bool = False
    imported: int = 0
    duplicates: int = 0
    attachments_imported: int = 0
    invalid: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    invalid_count: int = 0
    warning_count: int = 0
    # Neu angelegte Dateien. Werden bei Probelauf oder Fehler wieder gelöscht.
    saved_files: List[str] = field(default_factory=list, repr=False)

    def add_invalid(self, text: str) -> None:
        self.invalid_count += 1
        if len(self.invalid) < MAX_REPORT_LINES:
            self.invalid.append(text)

    def add_warning(self, text: str) -> None:
        self.warning_count += 1
        if len(self.warnings) < MAX_REPORT_LINES:
            self.warnings.append(text)


def _read_limited(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Liest ein ZIP-Mitglied und bricht bei mehr als `limit` Bytes ab, egal was der Kopf behauptet."""
    with archive.open(name) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise ImportFormatError(f"{name} ist zu groß.")
    return data


def _text(value: Any, label: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise ValueError(f"{label} hat einen falschen Typ.")


def _cents(value: Any, label: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} muss eine ganze Zahl in Cent sein.")
    return _money_text(value)


def _flag(value: Any, label: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} muss wahr oder falsch sein.")


def _percent(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("percent muss eine ganze Zahl sein.")
    return str(value)


def _entry_to_form(entry: Dict[str, Any], owners: Dict[str, int]) -> Tuple[Dict[str, str], Optional[str]]:
    """Wandelt einen JSON-Eintrag in Formularfelder, damit dieselbe Prüfung wie in der Oberfläche greift."""
    tags = entry.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("tags muss eine Liste von Texten sein.")
    owner_email = _text(entry.get("owner_email"), "owner_email").strip()
    owner_id = owners.get(owner_email.casefold()) if owner_email else None
    owner_note = None
    if owner_email and owner_id is None:
        owner_note = f"Person {owner_email} gibt es hier nicht, der Gutschein ist niemandem zugeordnet."
    form = {
        "title": _text(entry.get("title"), "title"),
        "code": _text(entry.get("code"), "code"),
        "value": _cents(entry.get("value_cents"), "value_cents"),
        "remaining": _cents(entry.get("remaining_cents"), "remaining_cents"),
        "kind": _text(entry.get("kind"), "kind") or "amount",  # Dateien aus Version 0.1 haben keine Art
        "percent": _percent(entry.get("percent")),
        "benefit_text": _text(entry.get("benefit_text"), "benefit_text"),
        "min_order": _cents(entry.get("min_order_cents"), "min_order_cents"),
        "shop_url": _text(entry.get("shop_url"), "shop_url"),
        "online_only": "on" if _flag(entry.get("online_only"), "online_only") else "",
        "expires_on": _text(entry.get("expires_on"), "expires_on"),
        "owner": str(owner_id) if owner_id else "",
        "notes": _text(entry.get("notes"), "notes"),
        "tags": ", ".join(tags),
    }
    return form, owner_note


def _duplicate_key(title: str, code: str, expires_on: Optional[str], value_cents: Optional[int],
                   kind: str = "amount", percent: Optional[int] = None, benefit_text: str = ""):
    return (title.casefold(), code, expires_on or "", value_cents, kind, percent, benefit_text.casefold())


def _existing_keys(db: sqlite3.Connection) -> Set[Tuple[Any, ...]]:
    keys = set()
    for v in db.execute("SELECT title, code_enc, expires_on, value_cents, kind, percent, benefit_text FROM vouchers"):
        code, _readable = vouchers.decrypt_code(v)
        keys.add(_duplicate_key(v["title"], code, v["expires_on"], v["value_cents"],
                                v["kind"], v["percent"], v["benefit_text"]))
    return keys


def _import_logo(db, archive, names, entry, idx, voucher_id, report) -> None:
    member = entry.get("logo")
    if member is None:
        return
    # Nur Namen im festen Muster, die es im ZIP wirklich gibt. Nie als Pfad verwendet.
    if not isinstance(member, str) or not LOGO_MEMBER_RE.match(member) or member not in names:
        report.add_warning(f"Eintrag {idx}: Das Logo fehlt oder hat einen ungültigen Namen und wird übersprungen.")
        return
    try:
        png = logos.process(_read_limited(archive, member, logos.MAX_INPUT_BYTES))
    except (logos.LogoError, ImportFormatError):
        report.add_warning(f"Eintrag {idx}: Das Logo ist kein brauchbares Bild und wird übersprungen.")
        return
    except Exception:  # noqa: BLE001 - beschädigtes ZIP-Mitglied
        report.add_warning(f"Eintrag {idx}: Das Logo ließ sich nicht lesen und wird übersprungen.")
        return
    logos.save(db, voucher_id, png)


def _import_attachments(db, archive, names, entry, idx, voucher_id, report, state) -> None:
    listed = entry.get("attachments", [])
    if not isinstance(listed, list):
        report.add_warning(f"Eintrag {idx}: Anhänge haben ein ungültiges Format und werden übersprungen.")
        return
    settings = get_settings()
    if len(listed) > attachments.MAX_ATTACHMENTS_PER_VOUCHER:
        report.add_warning(
            f"Eintrag {idx}: Es werden nur die ersten {attachments.MAX_ATTACHMENTS_PER_VOUCHER} Anhänge übernommen."
        )
        listed = listed[: attachments.MAX_ATTACHMENTS_PER_VOUCHER]
    for item in listed:
        member = item.get("file") if isinstance(item, dict) else None
        # Nur Namen im festen Muster, die es im ZIP wirklich gibt. Nie als Pfad verwendet.
        if not isinstance(member, str) or not ATTACHMENT_MEMBER_RE.match(member) or member not in names:
            report.add_warning(f"Eintrag {idx}: Ein Anhang fehlt oder hat einen ungültigen Namen und wird übersprungen.")
            continue
        original = item.get("original_name") if isinstance(item.get("original_name"), str) else ""
        try:
            with archive.open(member) as stream:
                saved = attachments.save_stream(stream, original, settings.max_upload_bytes)
        except attachments.UploadError as exc:
            report.add_warning(f"Eintrag {idx}: Anhang {member} übersprungen. {exc}")
            continue
        except Exception:  # noqa: BLE001 - beschädigtes oder verschlüsseltes ZIP-Mitglied
            report.add_warning(f"Eintrag {idx}: Anhang {member} ließ sich nicht lesen und wird übersprungen.")
            continue
        report.saved_files.append(saved.stored_name)
        db.execute(
            "INSERT INTO voucher_attachments (voucher_id, stored_name, original_name, content_type, "
            "size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (voucher_id, saved.stored_name, saved.original_name, saved.content_type,
             saved.size_bytes, vouchers._now_iso()),
        )
        report.attachments_imported += 1


def _import_entry(db, archive, names, entry, idx, owners, existing, skip_duplicates, created_by, report) -> None:
    if not isinstance(entry, dict):
        report.add_invalid(f"Eintrag {idx}: ist kein Objekt.")
        return
    label = entry.get("title") if isinstance(entry.get("title"), str) else ""
    label = f"Eintrag {idx}" + (f" ({label[:40]})" if label else "")
    try:
        form, owner_note = _entry_to_form(entry, owners)
    except ValueError as exc:
        report.add_invalid(f"{label}: {exc}")
        return
    data, errors = vouchers.validate_form(form, set(owners.values()))
    state = entry.get("state", "open")
    if state not in VALID_STATES:
        errors["state"] = "Der Zustand ist unbekannt."
    if data is None or errors:
        report.add_invalid(f"{label}: " + "; ".join(errors.values()))
        return

    key = _duplicate_key(data.title, data.code, data.expires_on, data.value_cents,
                         data.kind, data.percent, data.benefit_text)
    if skip_duplicates and key in existing:
        report.duplicates += 1
        return
    existing.add(key)
    if owner_note:
        report.add_warning(f"{label}: {owner_note}")

    voucher_id = vouchers.create_voucher(db, data, created_by)
    # Zustand und Zeitstempel aus der Datei übernehmen, soweit sie plausibel sind.
    remaining = data.remaining_cents
    if data.value_cents is not None:
        if state == "redeemed":
            remaining = 0
        elif state == "open" and remaining == 0:
            state = "redeemed"
    created_at = entry.get("created_at")
    updated_at = entry.get("updated_at")
    now = vouchers._now_iso()
    db.execute(
        "UPDATE vouchers SET state = ?, remaining_cents = ?, created_at = ?, updated_at = ? WHERE id = ?",
        (state, remaining,
         created_at if isinstance(created_at, str) and TIMESTAMP_RE.match(created_at) else now,
         updated_at if isinstance(updated_at, str) and TIMESTAMP_RE.match(updated_at) else now,
         voucher_id),
    )
    report.imported += 1
    _import_logo(db, archive, names, entry, idx, voucher_id, report)
    _import_attachments(db, archive, names, entry, idx, voucher_id, report, state)


def import_upload(db: sqlite3.Connection, source, *, skip_duplicates: bool, dry_run: bool,
                  created_by: int) -> Report:
    """Importiert aus einem Upload. Kopiert ihn erst in eine echte Datei, weil zipfile eine
    durchsuchbare Datei braucht (SpooledTemporaryFile kann das erst ab Python 3.11)."""
    with tempfile.TemporaryFile() as copy:
        source.seek(0)
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            copy.write(chunk)
        copy.seek(0)
        return import_zip(db, copy, skip_duplicates=skip_duplicates, dry_run=dry_run,
                          created_by=created_by)


def import_zip(db: sqlite3.Connection, fileobj, *, skip_duplicates: bool, dry_run: bool,
               created_by: int) -> Report:
    """Liest ein Export-ZIP und fügt die Gutscheine hinzu. Bei dry_run wird nichts gespeichert."""
    report = Report(dry_run=dry_run)
    try:
        archive = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise ImportFormatError("Das ist keine gültige ZIP-Datei.") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ImportFormatError("Das ZIP enthält zu viele Dateien.")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise ImportFormatError("Das ZIP ist entpackt zu groß.")
        names = {i.filename for i in infos if not i.is_dir()}
        if JSON_MEMBER not in names:
            raise ImportFormatError("Das ist kein Export des Gutscheinkontors (export.json fehlt).")

        try:
            document = json.loads(_read_limited(archive, JSON_MEMBER, MAX_JSON_BYTES).decode("utf-8"))
        except ImportFormatError:
            raise
        except (UnicodeDecodeError, ValueError, RecursionError, zipfile.BadZipFile,
                RuntimeError, NotImplementedError) as exc:
            raise ImportFormatError("export.json ist nicht lesbar.") from exc
        if not isinstance(document, dict) or document.get("format") != FORMAT_NAME:
            raise ImportFormatError("Das ist kein Export des Gutscheinkontors.")
        version = document.get("format_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ImportFormatError("Die Versionsangabe im Export fehlt oder ist ungültig.")
        if version > FORMAT_VERSION:
            raise ImportFormatError("Dieser Export stammt aus einer neueren Version. Bitte aktualisiere das Gutscheinkontor.")
        entries = document.get("vouchers")
        if not isinstance(entries, list):
            raise ImportFormatError("Im Export fehlt die Liste der Gutscheine.")
        if len(entries) > MAX_VOUCHERS:
            raise ImportFormatError("Der Export enthält zu viele Gutscheine.")

        owners = {
            r["email"].casefold(): r["id"]
            for r in db.execute("SELECT id, email FROM users WHERE is_active = 1")
        }
        existing = _existing_keys(db)
        try:
            with transaction(db):
                for idx, entry in enumerate(entries, start=1):
                    _import_entry(db, archive, names, entry, idx, owners, existing,
                                  skip_duplicates, created_by, report)
                if dry_run:
                    raise _DryRun()
        except _DryRun:
            pass
        except BaseException:
            attachments.delete_files(report.saved_files)
            raise
        if dry_run:
            attachments.delete_files(report.saved_files)
    return report
