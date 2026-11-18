"""Gutscheine: Eingaben prüfen, Status berechnen, filtern und speichern.

Bei der erwarteten Größe (einige Dutzend, höchstens wenige Hundert Gutscheine) werden alle
Gutscheine geladen und in Python gefiltert. Das macht die Suche unabhängig von Umlauten und
Groß-/Kleinschreibung (casefold), was SQLite-LIKE nicht kann.
"""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlsplit
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from . import dates, formatting, security

STATUS_LABELS = {
    "aktiv": "Aktiv",
    "teilweise": "Teilweise eingelöst",
    "abgelaufen": "Abgelaufen",
    "eingeloest": "Eingelöst",
    "archiviert": "Archiviert",
}
FILTER_OPTIONS = [
    ("aktuell", "Aktuell"),
    ("aktiv", "Aktiv"),
    ("teilweise", "Teilweise eingelöst"),
    ("abgelaufen", "Abgelaufen"),
    ("eingeloest", "Eingelöst"),
    ("archiviert", "Archiviert"),
    ("alle", "Alle"),
]
SORT_OPTIONS = [("ablauf", "Ablaufdatum"), ("neu", "Zuletzt hinzugefügt"), ("titel", "Name")]
STATE_ACTIONS = ("redeem_all", "reopen", "archive", "unarchive")

MAX_VALUE_CENTS = 1_000_000 * 100
MAX_TAGS = 10
MAX_TAG_LENGTH = 30
MAX_TITLE_LENGTH = 100
MAX_CODE_LENGTH = 500
MAX_NOTES_LENGTH = 2000

MONEY_HINT = "Bitte gib einen Betrag wie 25 oder 25,50 an."

KINDS = (("amount", "Betrag in Euro"), ("percent", "Prozent-Rabatt"), ("other", "Sonstiges"))
KIND_KEYS = tuple(key for key, _ in KINDS)
MAX_BENEFIT_LENGTH = 100
MAX_URL_LENGTH = 500


# --- Eingaben lesen -------------------------------------------------------------


def parse_money(text: str) -> Optional[int]:
    """'25', '25,5', '1.234,50', '12.50 €' -> Cent. Leer -> None. Sonst ValueError."""
    t = text.strip().replace("€", "").replace(" ", "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", t):
        raise ValueError(MONEY_HINT)
    try:
        cents = int((Decimal(t) * 100).to_integral_value())
    except InvalidOperation as exc:
        raise ValueError(MONEY_HINT) from exc
    if cents > MAX_VALUE_CENTS:
        raise ValueError("Der Betrag ist zu groß.")
    return cents


def parse_shop_url(text: str) -> str:
    """Prüft die Shop-Adresse. Nur http und https, ohne Zugangsdaten. 'shop.de' wird zu 'https://shop.de'.

    Leer -> ''. Sonst ValueError. Das Schema wird streng geprüft, weil die Adresse später als Link
    erscheint ('javascript:' und Ähnliches darf nie ankommen).
    """
    t = text.strip()
    if not t:
        return ""
    if len(t) > MAX_URL_LENGTH:
        raise ValueError(f"Höchstens {MAX_URL_LENGTH} Zeichen.")
    if any(ord(c) < 33 or ord(c) == 127 for c in t):
        raise ValueError("Die Adresse darf keine Leerzeichen oder Steuerzeichen enthalten.")
    if any(c in t for c in "<>\"'`\\"):
        raise ValueError("Die Adresse enthält ungültige Zeichen.")
    if "://" not in t:
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t) and not re.match(r"^[^/]*\.[^/]*:\d+(/|$)", t):
            raise ValueError("Bitte gib eine Adresse mit http:// oder https:// an.")
        t = "https://" + t
    try:
        parts = urlsplit(t)
        host = parts.hostname
        parts.port  # prüft den Port
    except ValueError as exc:
        raise ValueError("Das ist keine gültige Adresse.") from exc
    if parts.scheme not in ("http", "https"):
        raise ValueError("Bitte gib eine Adresse mit http:// oder https:// an.")
    if not host or "." not in host or parts.username or parts.password:
        raise ValueError("Das ist keine gültige Adresse.")
    return t


def parse_date(text: str) -> Optional[str]:
    """'2026-12-31' oder '31.12.2026' -> ISO-Datum. Leer -> None. Sonst ValueError."""
    t = text.strip()
    if not t:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            d = date.fromisoformat(t)
        else:
            d = datetime.strptime(t, "%d.%m.%Y").date()
    except ValueError as exc:
        raise ValueError("Bitte gib ein gültiges Datum an.") from exc
    if not 2000 <= d.year <= 2100:
        raise ValueError("Das Datum liegt außerhalb des erlaubten Bereichs.")
    return d.isoformat()


def parse_tags(text: str) -> List[str]:
    tags: List[str] = []
    seen: Set[str] = set()
    for part in re.split(r"[,;]", text):
        tag = " ".join(part.split())
        if not tag:
            continue
        if any(ord(c) < 32 for c in tag):
            raise ValueError("Schlagwörter dürfen keine Steuerzeichen enthalten.")
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"Ein Schlagwort darf höchstens {MAX_TAG_LENGTH} Zeichen lang sein.")
        if tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"Höchstens {MAX_TAGS} Schlagwörter pro Gutschein.")
    return tags


@dataclass
class VoucherData:
    title: str
    code: str
    value_cents: Optional[int]
    remaining_cents: Optional[int]
    expires_on: Optional[str]
    owner_user_id: Optional[int]
    notes: str
    tags: List[str]
    kind: str = "amount"
    percent: Optional[int] = None
    benefit_text: str = ""
    min_order_cents: Optional[int] = None
    shop_url: str = ""
    online_only: bool = False


def validate_form(
    form: Mapping[str, str], valid_owner_ids: Set[int]
) -> Tuple[Optional[VoucherData], Dict[str, str]]:
    errors: Dict[str, str] = {}

    title = form.get("title", "").strip()
    if not title:
        errors["title"] = "Bitte gib einen Namen oder eine Firma an."
    elif len(title) > MAX_TITLE_LENGTH:
        errors["title"] = f"Höchstens {MAX_TITLE_LENGTH} Zeichen."

    code = form.get("code", "").strip()
    if len(code) > MAX_CODE_LENGTH:
        errors["code"] = f"Höchstens {MAX_CODE_LENGTH} Zeichen."

    kind = form.get("kind", "").strip() or "amount"
    if kind not in KIND_KEYS:
        errors["kind"] = "Bitte wähle eine Art aus der Liste."
        kind = "amount"

    value = remaining = percent = None
    benefit_text = ""
    if kind == "amount":
        try:
            value = parse_money(form.get("value", ""))
        except ValueError as exc:
            errors["value"] = str(exc)
        try:
            remaining = parse_money(form.get("remaining", ""))
        except ValueError as exc:
            errors["remaining"] = str(exc)
        if "value" not in errors and "remaining" not in errors:
            if value is None and remaining is not None:
                errors["remaining"] = "Ein Restwert geht nur zusammen mit einem Wert."
            elif value is not None and remaining is None:
                remaining = value
            elif value is not None and remaining is not None and remaining > value:
                errors["remaining"] = "Der Restwert darf nicht größer als der Wert sein."
    elif kind == "percent":
        percent_text = form.get("percent", "").strip().rstrip("%").strip()
        if not re.fullmatch(r"\d{1,3}", percent_text) or not 1 <= int(percent_text) <= 100:
            errors["percent"] = "Bitte gib eine ganze Zahl von 1 bis 100 an."
        else:
            percent = int(percent_text)
    else:
        benefit_text = " ".join(form.get("benefit_text", "").split())
        if not benefit_text:
            errors["benefit_text"] = "Bitte beschreibe den Vorteil kurz."
        elif len(benefit_text) > MAX_BENEFIT_LENGTH:
            errors["benefit_text"] = f"Höchstens {MAX_BENEFIT_LENGTH} Zeichen."

    min_order = None
    try:
        min_order = parse_money(form.get("min_order", "")) or None  # 0 bedeutet: kein Mindestwert
    except ValueError as exc:
        errors["min_order"] = str(exc)

    shop_url = ""
    try:
        shop_url = parse_shop_url(form.get("shop_url", ""))
    except ValueError as exc:
        errors["shop_url"] = str(exc)
    online_only = form.get("online_only", "").strip().lower() in ("on", "1", "true")

    expires_on = None
    try:
        expires_on = parse_date(form.get("expires_on", ""))
    except ValueError as exc:
        errors["expires_on"] = str(exc)

    owner = None
    owner_text = form.get("owner", "").strip()
    if owner_text:
        if owner_text.isdigit() and int(owner_text) in valid_owner_ids:
            owner = int(owner_text)
        else:
            errors["owner"] = "Bitte wähle eine Person aus der Liste."

    notes = form.get("notes", "").strip()
    if len(notes) > MAX_NOTES_LENGTH:
        errors["notes"] = f"Höchstens {MAX_NOTES_LENGTH} Zeichen."

    tags: List[str] = []
    try:
        tags = parse_tags(form.get("tags", ""))
    except ValueError as exc:
        errors["tags"] = str(exc)

    if errors:
        return None, errors
    return (
        VoucherData(title, code, value, remaining, expires_on, owner, notes, tags,
                    kind, percent, benefit_text, min_order, shop_url, online_only),
        errors,
    )


# --- Status ----------------------------------------------------------------------


def status_key(v: Mapping[str, Any], today: date) -> str:
    if v["state"] == "archived":
        return "archiviert"
    if v["state"] == "redeemed":
        return "eingeloest"
    if v["expires_on"] and v["expires_on"] < today.isoformat():
        return "abgelaufen"
    value, rest = v["value_cents"], v["remaining_cents"]
    if value is not None and rest is not None and 0 < rest < value:
        return "teilweise"
    return "aktiv"


def shop_host(url: str) -> str:
    """Nur der Rechnername für die Anzeige, z. B. 'www.hagebau.de'."""
    return urlsplit(url).hostname or "" if url else ""


def benefit_label(v: Mapping[str, Any]) -> str:
    """Kurztext zum Vorteil, z. B. '25,00 €', '10 %' oder der freie Text."""
    kind = v["kind"]
    if kind == "percent":
        return f"{v['percent']} %"
    if kind == "other":
        return v["benefit_text"]
    return formatting.format_euro(v["remaining_cents"] if v["remaining_cents"] is not None else v["value_cents"])


def to_view(v: Mapping[str, Any], today: date) -> Dict[str, Any]:
    key = status_key(v, today)
    view = dict(v)
    view.update(
        status_key=key,
        status_label=STATUS_LABELS[key],
        value_text=formatting.format_euro(v["value_cents"]),
        benefit_label=benefit_label(v),
        min_order_text=formatting.format_euro(v["min_order_cents"]),
        shop_host=shop_host(v["shop_url"]),
        remaining_text=formatting.format_euro(v["remaining_cents"]),
        expires_text=formatting.format_date(v["expires_on"]),
        expiry_text=formatting.expiry_text(v["expires_on"], today),
        urgency=formatting.urgency(v["expires_on"], today, key in ("eingeloest", "archiviert")),
        is_open=key in ("aktiv", "teilweise"),
    )
    return view


def matches_filter(view: Mapping[str, Any], status: str) -> bool:
    if status == "alle":
        return True
    if status == "aktuell":
        return view["status_key"] in ("aktiv", "teilweise")
    return view["status_key"] == status


def select_vouchers(
    views: List[Dict[str, Any]], q: str, status: str, tag: str, owner: str, sort: str
) -> List[Dict[str, Any]]:
    words = [w.casefold() for w in q.split() if w]
    result = []
    for v in views:
        if not matches_filter(v, status):
            continue
        if tag and tag.casefold() not in (t.casefold() for t in v["tags"]):
            continue
        if owner == "none" and v["owner_user_id"] is not None:
            continue
        if owner.isdigit() and v["owner_user_id"] != int(owner):
            continue
        haystack = " ".join([v["title"], v["notes"], v.get("owner_name") or "", *v["tags"]]).casefold()
        if not all(w in haystack for w in words):
            continue
        result.append(v)

    if sort == "neu":
        result.sort(key=lambda v: (v["created_at"], v["id"]), reverse=True)
    elif sort == "titel":
        result.sort(key=lambda v: (v["title"].casefold(), v["id"]))
    else:  # ablauf: früheste zuerst, ohne Datum ans Ende
        result.sort(key=lambda v: (v["expires_on"] is None, v["expires_on"] or "", v["title"].casefold()))
    return result


def summary(views: List[Mapping[str, Any]]) -> Dict[str, Any]:
    open_ones = [v for v in views if v["status_key"] in ("aktiv", "teilweise")]
    total = sum(v["remaining_cents"] for v in open_ones if v["remaining_cents"] is not None)
    return {
        "open_count": len(open_ones),
        "remaining_total": formatting.format_euro(total),
        "has_values": any(v["remaining_cents"] is not None for v in open_ones),
    }


# --- Datenbank --------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SELECT = (
    "SELECT v.*, u.display_name AS owner_name, "
    "(SELECT COUNT(*) FROM voucher_attachments a WHERE a.voucher_id = v.id) AS attachment_count, "
    "(SELECT updated_at FROM voucher_logos l WHERE l.voucher_id = v.id) AS logo_version "
    "FROM vouchers v LEFT JOIN users u ON u.id = v.owner_user_id"
)


def load_all(db: sqlite3.Connection) -> List[Dict[str, Any]]:
    tags: Dict[int, List[str]] = defaultdict(list)
    for row in db.execute("SELECT voucher_id, tag FROM voucher_tags ORDER BY tag COLLATE NOCASE"):
        tags[row["voucher_id"]].append(row["tag"])
    return [dict(r, tags=tags[r["id"]]) for r in db.execute(_SELECT)]


def get_voucher(db: sqlite3.Connection, voucher_id: int) -> Optional[Dict[str, Any]]:
    row = db.execute(_SELECT + " WHERE v.id = ?", (voucher_id,)).fetchone()
    if row is None:
        return None
    tags = [r["tag"] for r in db.execute(
        "SELECT tag FROM voucher_tags WHERE voucher_id = ? ORDER BY tag COLLATE NOCASE", (voucher_id,))]
    return dict(row, tags=tags)


def decrypt_code(v: Mapping[str, Any]) -> Tuple[str, bool]:
    """Gibt (Code, lesbar) zurück. Nicht lesbar, wenn der SECRET_KEY geändert wurde."""
    if not v["code_enc"]:
        return "", True
    try:
        return security.decrypt(v["code_enc"]), True
    except Exception:  # noqa: BLE001 - falscher Schlüssel oder beschädigte Daten
        return "", False


def set_tags(db: sqlite3.Connection, voucher_id: int, tags: List[str]) -> None:
    db.execute("DELETE FROM voucher_tags WHERE voucher_id = ?", (voucher_id,))
    for tag in tags:
        db.execute("INSERT INTO voucher_tags (voucher_id, tag) VALUES (?, ?)", (voucher_id, tag))


def _state_after_edit(current: str, value: Optional[int], remaining: Optional[int]) -> str:
    """Passt den Zustand an, wenn der Restwert von Hand geändert wurde."""
    if current == "archived" or value is None:
        return current
    if remaining == 0:
        return "redeemed"
    return "open" if current == "redeemed" else current


def create_voucher(db: sqlite3.Connection, data: VoucherData, created_by: int) -> int:
    now = _now_iso()
    state = _state_after_edit("open", data.value_cents, data.remaining_cents)
    cur = db.execute(
        "INSERT INTO vouchers (title, code_enc, value_cents, remaining_cents, expires_on, state, "
        "owner_user_id, notes, created_by, created_at, updated_at, expiry_set_on, "
        "kind, percent, benefit_text, min_order_cents, shop_url, online_only) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (data.title, security.encrypt(data.code) if data.code else None, data.value_cents,
         data.remaining_cents, data.expires_on, state, data.owner_user_id, data.notes,
         created_by, now, now, dates.today().isoformat(),
         data.kind, data.percent, data.benefit_text, data.min_order_cents,
         data.shop_url, int(data.online_only)),
    )
    set_tags(db, cur.lastrowid, data.tags)
    return cur.lastrowid


def update_voucher(db: sqlite3.Connection, voucher_id: int, data: VoucherData) -> bool:
    current = db.execute(
        "SELECT state, expires_on, expiry_set_on FROM vouchers WHERE id = ?", (voucher_id,)
    ).fetchone()
    if current is None:
        return False
    state = _state_after_edit(current["state"], data.value_cents, data.remaining_cents)
    # Wird das Ablaufdatum geändert, zählen die Erinnerungsstufen ab heute neu.
    expiry_set_on = current["expiry_set_on"]
    if data.expires_on != current["expires_on"]:
        expiry_set_on = dates.today().isoformat()
    db.execute(
        "UPDATE vouchers SET title=?, code_enc=?, value_cents=?, remaining_cents=?, expires_on=?, "
        "state=?, owner_user_id=?, notes=?, updated_at=?, expiry_set_on=?, "
        "kind=?, percent=?, benefit_text=?, min_order_cents=?, shop_url=?, online_only=? WHERE id=?",
        (data.title, security.encrypt(data.code) if data.code else None, data.value_cents,
         data.remaining_cents, data.expires_on, state, data.owner_user_id, data.notes,
         _now_iso(), expiry_set_on, data.kind, data.percent, data.benefit_text,
         data.min_order_cents, data.shop_url, int(data.online_only), voucher_id),
    )
    set_tags(db, voucher_id, data.tags)
    return True


def redeem_amount(db: sqlite3.Connection, voucher_id: int, amount_cents: int) -> Optional[str]:
    """Zieht einen Betrag vom Restwert ab. Gibt eine Fehlermeldung zurück oder None."""
    row = db.execute(
        "SELECT state, value_cents, remaining_cents FROM vouchers WHERE id = ?", (voucher_id,)
    ).fetchone()
    if row is None:
        return "Gutschein nicht gefunden."
    if row["state"] != "open":
        return "Dieser Gutschein ist nicht offen."
    if row["value_cents"] is None or row["remaining_cents"] is None:
        return "Ohne Wert kann kein Betrag eingelöst werden."
    if amount_cents <= 0:
        return "Der Betrag muss größer als 0 sein."
    if amount_cents > row["remaining_cents"]:
        return f"Es sind nur noch {formatting.format_euro(row['remaining_cents'])} übrig."
    remaining = row["remaining_cents"] - amount_cents
    db.execute(
        "UPDATE vouchers SET remaining_cents = ?, state = ?, updated_at = ? WHERE id = ?",
        (remaining, "redeemed" if remaining == 0 else "open", _now_iso(), voucher_id),
    )
    return None


def apply_state_action(db: sqlite3.Connection, voucher_id: int, action: str) -> bool:
    row = db.execute(
        "SELECT state, value_cents, remaining_cents FROM vouchers WHERE id = ?", (voucher_id,)
    ).fetchone()
    if row is None or action not in STATE_ACTIONS:
        return False
    value, remaining, state = row["value_cents"], row["remaining_cents"], row["state"]
    if action == "redeem_all":
        state = "redeemed"
        if value is not None:
            remaining = 0
    elif action == "reopen":
        state = "open"
        if value is not None and remaining == 0:
            remaining = value  # Einlösung rückgängig: der volle Wert steht wieder zur Verfügung
    elif action == "archive":
        state = "archived"
    elif action == "unarchive":
        state = "redeemed" if value is not None and remaining == 0 else "open"
    db.execute(
        "UPDATE vouchers SET state = ?, remaining_cents = ?, updated_at = ? WHERE id = ?",
        (state, remaining, _now_iso(), voucher_id),
    )
    return True
