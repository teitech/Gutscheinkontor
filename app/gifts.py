"""Gutscheine weitergeben.

Ablauf:
- Der Gutschein geht per E-Mail an eine oder mehrere Personen. Jede bekommt einen eigenen Link,
  aber noch keinen Code. Wer den Link öffnet und "übernehmen" wählt, bekommt den Code. Wer zuerst
  übernimmt, hat ihn; alle anderen Links werden damit ungültig (Datenbank-Update mit Bedingung,
  nur ein Aufruf gewinnt).
- Ein Link läuft nach 14 Tagen ab, spätestens aber am Ablaufdatum des Gutscheins.
- Übernimmt niemand, bleibt der Gutschein im Kontor offen. Er wird nur mit einem Hinweis versehen
  und kann jederzeit zurückgeholt werden (alle Links erlöschen sofort).
- Daneben gibt es einen reinen Vermerk "verschenkt" für persönliche Übergaben.

Datenschutz: Die Adressen der Empfänger werden gelöscht, sobald die Weitergabe beendet ist.
Nur die Adresse der Person, die übernommen hat, bleibt am Vermerk stehen (der Absender hat sie
selbst eingegeben). Links werden nur als Hash gespeichert.

Das Modul kennt nur die Datenbank, damit `vouchers` es ohne Importschleife nutzen kann.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import get_settings
from .db import transaction

LINK_TTL = 14 * 24 * 3600       # Link zum Übernehmen: 14 Tage
ACCESS_TTL = 30 * 24 * 3600     # Nach der Übernahme bleibt die Code-Seite 30 Tage erreichbar
MAX_RECIPIENTS = 10
MAX_MESSAGE_LENGTH = 500
MAX_GIVEN_TO_LENGTH = 100
MAX_EMAIL_LENGTH = 254

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GiftError(Exception):
    """Eine Weitergabe ist nicht möglich. Der Text ist für Nutzer gedacht."""


def _now() -> int:
    return int(time.time())


def _hash(token: str) -> str:
    # Gleiche Ableitung wie bei den Sitzungen: nur der Hash liegt in der Datenbank.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- Eingaben prüfen ---------------------------------------------------------------


def parse_recipients(text: str) -> Tuple[List[str], Optional[str]]:
    """Zerlegt die Eingabe in E-Mail-Adressen. Gibt (Adressen, Fehlertext oder None) zurück."""
    emails: List[str] = []
    seen = set()
    for part in re.split(r"[\s,;]+", text.strip()):
        if not part:
            continue
        if len(part) > MAX_EMAIL_LENGTH or not EMAIL_RE.match(part) or any(ord(c) < 33 for c in part):
            return [], f"„{part[:60]}“ ist keine gültige E-Mail-Adresse."
        if part.casefold() in seen:
            continue
        seen.add(part.casefold())
        emails.append(part)
    if not emails:
        return [], "Bitte gib mindestens eine E-Mail-Adresse an."
    if len(emails) > MAX_RECIPIENTS:
        return [], f"Höchstens {MAX_RECIPIENTS} Adressen auf einmal."
    return emails, None


def clean_text(text: str, limit: int) -> str:
    """Freitext: Steuerzeichen raus (Zeilenumbrüche bleiben), Länge begrenzen."""
    cleaned = "".join(c for c in text.replace("\r\n", "\n").replace("\r", "\n") if c == "\n" or ord(c) >= 32)
    return cleaned.strip()[:limit]


def link_expiry(voucher_expires_on: Optional[str], now: Optional[int] = None) -> int:
    """Ende des Links: 14 Tage ab jetzt, spätestens Ende des Ablauftags des Gutscheins."""
    now = _now() if now is None else now
    end = now + LINK_TTL
    if voucher_expires_on:
        day = date.fromisoformat(voucher_expires_on)
        zone = ZoneInfo(get_settings().timezone)
        end_of_day = datetime.combine(day, dtime(23, 59, 59), tzinfo=zone).timestamp()
        end = min(end, int(end_of_day))
    return end


# --- Abfragen ------------------------------------------------------------------------


def _effective_status(row: Dict[str, Any], now: int) -> str:
    if row["status"] == "open" and row["expires_at"] <= now:
        return "expired"
    return row["status"]


def latest(db: sqlite3.Connection, voucher_id: int) -> Optional[Dict[str, Any]]:
    """Jüngste Weitergabe des Gutscheins, mit berechnetem Status und Name des Absenders."""
    row = db.execute(
        "SELECT g.*, u.display_name AS giver_name FROM voucher_gifts g "
        "LEFT JOIN users u ON u.id = g.created_by WHERE g.voucher_id = ? ORDER BY g.id DESC LIMIT 1",
        (voucher_id,),
    ).fetchone()
    if row is None:
        return None
    gift = dict(row)
    gift["status"] = _effective_status(gift, _now())
    return gift


def has_open(db: sqlite3.Connection, voucher_id: int) -> bool:
    gift = latest(db, voucher_id)
    return gift is not None and gift["status"] == "open"


def lookup(db: sqlite3.Connection, token: str) -> Optional[Dict[str, Any]]:
    """Findet die Weitergabe zu einem Link. None, wenn der Link unbekannt oder nicht mehr gültig ist.

    Das Ergebnis enthält `phase`: "offer" (kann noch übernommen werden) oder "claimed"
    (dieser Link gehört der Person, die übernommen hat).
    """
    if not token or len(token) > 200:
        return None
    row = db.execute(
        "SELECT r.id AS recipient_id, r.email AS recipient_email, g.*, "
        "u.display_name AS giver_name, u.id AS giver_id "
        "FROM gift_recipients r JOIN voucher_gifts g ON g.id = r.gift_id "
        "LEFT JOIN users u ON u.id = g.created_by WHERE r.token_hash = ?",
        (_hash(token),),
    ).fetchone()
    if row is None:
        return None
    gift = dict(row)
    now = _now()
    if gift["status"] == "open" and gift["expires_at"] > now:
        gift["phase"] = "offer"
    elif gift["status"] == "claimed" and gift["claimed_at"] and gift["claimed_at"] + ACCESS_TTL > now:
        gift["phase"] = "claimed"
    else:
        return None
    return gift


# --- Ändern --------------------------------------------------------------------------


def _delete_recipients(db: sqlite3.Connection, gift_ids: List[int], keep: Optional[int] = None) -> None:
    for gift_id in gift_ids:
        db.execute(
            "DELETE FROM gift_recipients WHERE gift_id = ? AND id IS NOT ?", (gift_id, keep)
        )


def sweep(db: sqlite3.Connection) -> None:
    """Räumt auf: abgelaufene Weitergaben beenden, nicht mehr gebrauchte Adressen löschen."""
    now = _now()
    with transaction(db):
        db.execute(
            "UPDATE voucher_gifts SET status = 'expired' WHERE status = 'open' AND expires_at <= ?", (now,)
        )
        db.execute(
            "DELETE FROM gift_recipients WHERE gift_id IN "
            "(SELECT id FROM voucher_gifts WHERE status IN ('revoked', 'expired'))"
        )
        db.execute(
            "DELETE FROM gift_recipients WHERE gift_id IN (SELECT id FROM voucher_gifts "
            "WHERE status = 'claimed' AND claimed_at IS NOT NULL AND claimed_at + ? <= ?)",
            (ACCESS_TTL, now),
        )


def create_link_gift(
    db: sqlite3.Connection, voucher_id: int, user_id: int, emails: List[str], message: str,
    expires_at: int,
) -> Tuple[int, List[Tuple[int, str, str]]]:
    """Legt eine offene Weitergabe an. Aufruf innerhalb einer Transaktion.

    Gibt (gift_id, [(recipient_id, email, token), ...]) zurück. Die Tokens gibt es nur hier im Klartext.
    """
    now = _now()
    # Eine abgelaufene, noch als offen vermerkte Weitergabe darf keine neue blockieren.
    stale = [r["id"] for r in db.execute(
        "SELECT id FROM voucher_gifts WHERE voucher_id = ? AND status = 'open' AND expires_at <= ?",
        (voucher_id, now),
    )]
    if stale:
        db.execute(
            "UPDATE voucher_gifts SET status = 'expired' WHERE voucher_id = ? AND status = 'open' "
            "AND expires_at <= ?", (voucher_id, now),
        )
        _delete_recipients(db, stale)
    try:
        cur = db.execute(
            "INSERT INTO voucher_gifts (voucher_id, kind, status, message, recipient_count, created_by, "
            "created_at, expires_at) VALUES (?, 'link', 'open', ?, ?, ?, ?, ?)",
            (voucher_id, message, len(emails), user_id, now, expires_at),
        )
    except sqlite3.IntegrityError as exc:
        raise GiftError("Dieser Gutschein ist schon weitergegeben. Hole ihn zuerst zurück.") from exc
    gift_id = cur.lastrowid
    recipients = []
    for email in emails:
        token = secrets.token_urlsafe(32)
        rcur = db.execute(
            "INSERT INTO gift_recipients (gift_id, email, token_hash) VALUES (?, ?, ?)",
            (gift_id, email, _hash(token)),
        )
        recipients.append((rcur.lastrowid, email, token))
    return gift_id, recipients


def drop_recipients(db: sqlite3.Connection, gift_id: int, recipient_ids: List[int]) -> None:
    """Entfernt Empfänger, an die die Mail nicht ging, und passt die Zahl an."""
    for rid in recipient_ids:
        db.execute("DELETE FROM gift_recipients WHERE id = ? AND gift_id = ?", (rid, gift_id))
    count = db.execute("SELECT COUNT(*) FROM gift_recipients WHERE gift_id = ?", (gift_id,)).fetchone()[0]
    db.execute("UPDATE voucher_gifts SET recipient_count = ? WHERE id = ?", (count, gift_id))


def delete_gift(db: sqlite3.Connection, gift_id: int) -> None:
    db.execute("DELETE FROM voucher_gifts WHERE id = ?", (gift_id,))  # Empfänger fallen per CASCADE weg


def revoke_open(db: sqlite3.Connection, voucher_id: int) -> bool:
    """Holt eine offene Weitergabe zurück: alle Links erlöschen. Gibt zurück, ob es eine gab."""
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM voucher_gifts WHERE voucher_id = ? AND status = 'open'", (voucher_id,)
    )]
    if not ids:
        return False
    db.execute("UPDATE voucher_gifts SET status = 'revoked' WHERE voucher_id = ? AND status = 'open'",
               (voucher_id,))
    _delete_recipients(db, ids)
    return True


def claim(db: sqlite3.Connection, token: str, today_iso: str) -> Optional[Dict[str, Any]]:
    """Übernimmt den Gutschein für den Inhaber des Links. Aufruf innerhalb einer Transaktion.

    Nur ein Aufruf gewinnt. Gibt die Weitergabe zurück oder None, wenn es nicht (mehr) geht.
    """
    gift = lookup(db, token)
    if gift is None or gift["phase"] != "offer":
        return None
    now = _now()
    cur = db.execute(
        "UPDATE voucher_gifts SET status = 'claimed', claimed_at = ?, given_to = ? "
        "WHERE id = ? AND status = 'open' AND expires_at > ? AND EXISTS ("
        "SELECT 1 FROM vouchers v WHERE v.id = voucher_gifts.voucher_id AND v.state = 'open' "
        "AND (v.expires_on IS NULL OR v.expires_on >= ?))",
        (now, gift["recipient_email"], gift["id"], now, today_iso),
    )
    if cur.rowcount != 1:
        return None
    _delete_recipients(db, [gift["id"]], keep=gift["recipient_id"])
    gift["phase"] = "claimed"
    gift["status"] = "claimed"
    gift["claimed_at"] = now
    return gift


def mark_given(db: sqlite3.Connection, voucher_id: int, user_id: int, given_to: str) -> None:
    """Vermerk "verschenkt" für eine persönliche Übergabe. Aufruf innerhalb einer Transaktion."""
    if has_open(db, voucher_id):
        raise GiftError("Es läuft noch eine Weitergabe per Link. Hole sie zuerst zurück.")
    now = _now()
    db.execute(
        "INSERT INTO voucher_gifts (voucher_id, kind, status, given_to, created_by, created_at, "
        "expires_at, claimed_at) VALUES (?, 'manual', 'claimed', ?, ?, ?, ?, ?)",
        (voucher_id, given_to, user_id, now, now, now),
    )


def undo_given(db: sqlite3.Connection, voucher_id: int) -> bool:
    """Nimmt den Vermerk "verschenkt" zurück. Nur bei persönlicher Übergabe, nie nach einer Übernahme per Link."""
    gift = latest(db, voucher_id)
    if gift is None or gift["kind"] != "manual" or gift["status"] != "claimed":
        return False
    delete_gift(db, gift["id"])
    return True


# --- Anzeige -------------------------------------------------------------------------


def date_text(timestamp: Optional[int]) -> str:
    if not timestamp:
        return ""
    local = datetime.fromtimestamp(timestamp, ZoneInfo(get_settings().timezone))
    return f"{local.day:02d}.{local.month:02d}.{local.year}"


def describe(gift: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Weitergabe mit Datumstexten für die Anzeige."""
    if gift is None:
        return None
    view = dict(gift)
    view["created_text"] = date_text(gift["created_at"])
    view["expires_text"] = date_text(gift["expires_at"])
    view["claimed_text"] = date_text(gift["claimed_at"])
    return view
