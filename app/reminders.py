"""Erinnerungsmails vor dem Ablauf von Gutscheinen.

Regeln:
- Stufen kommen aus REMINDER_DAYS (Standard 30, 7 und 1 Tag vor Ablauf). Pro Gutschein gilt
  immer nur die aktuelle Stufe: die kleinste Stufe, die noch nicht überschritten ist. Wer nach
  einer Pause (z. B. Server aus) wieder startet, bekommt also eine Mail und keinen Schwall.
- Pro Gutschein, Empfänger, Stufe und Ablaufdatum wird höchstens eine Mail verschickt.
  Wird das Ablaufdatum geändert, beginnen die Stufen für das neue Datum von vorn.
- Eine Stufe, die schon vorbei war, als das Ablaufdatum gesetzt wurde, entfällt. Wer einen
  Gutschein mit 20 Tagen Restlaufzeit anlegt, bekommt keine 30-Tage-Mail, sondern die späteren.
- Empfänger: die Person, der der Gutschein zugeordnet ist. Ohne Zuordnung alle Nutzer.
  Wer Erinnerungen abgeschaltet hat, bekommt nichts.
- Mehrere Gutscheine für denselben Empfänger gehen in einer Sammelmail raus.
- Der Gutscheincode steht nie in der Mail.
- Jede Mail wird erst vermerkt, wenn der Mailserver sie angenommen hat. Sonst klappt der
  nächste Lauf wieder.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from starlette.concurrency import run_in_threadpool

from . import formatting, mail
from .config import get_settings
from .db import connect, transaction

logger = logging.getLogger("gutscheinkontor.reminders")

TICK_SECONDS = 30 * 60  # alle 30 Minuten prüfen, ob etwas zu tun ist


@dataclass
class Item:
    voucher_id: int
    title: str
    expires_on: str
    days_left: int
    remaining_cents: Optional[int]
    stage: int


def current_stage(days_left: int, stages: Tuple[int, ...]) -> Optional[int]:
    """Kleinste Stufe, die noch nicht überschritten ist. None, wenn noch zu früh."""
    return next((s for s in sorted(stages) if days_left <= s), None)


def pending(db: sqlite3.Connection, today: date, stages: Tuple[int, ...]):
    """Fällige Erinnerungen, gruppiert nach Nutzer: {user_id: (Nutzerzeile, [Item, ...])}."""
    users = {
        u["id"]: u
        for u in db.execute(
            "SELECT id, email, display_name FROM users WHERE is_active = 1 AND reminders_enabled = 1"
        )
    }
    result: Dict[int, Tuple[sqlite3.Row, List[Item]]] = {}
    rows = db.execute(
        "SELECT id, title, expires_on, expiry_set_on, remaining_cents, owner_user_id "
        "FROM vouchers WHERE state = 'open' AND expires_on IS NOT NULL"
    ).fetchall()
    for v in rows:
        expires = date.fromisoformat(v["expires_on"])
        days_left = (expires - today).days
        if days_left < 0:
            continue
        stage = current_stage(days_left, stages)
        if stage is None:
            continue
        set_on = date.fromisoformat(v["expiry_set_on"]) if v["expiry_set_on"] else date.min
        if expires - timedelta(days=stage) <= set_on:
            continue  # Diese Stufe war schon vorbei, als das Datum gesetzt wurde.

        if v["owner_user_id"] is not None:
            recipients = [v["owner_user_id"]] if v["owner_user_id"] in users else []
        else:
            recipients = list(users)
        for user_id in recipients:
            already = db.execute(
                "SELECT 1 FROM reminders_sent WHERE voucher_id = ? AND user_id = ? "
                "AND stage_days = ? AND expires_on = ?",
                (v["id"], user_id, stage, v["expires_on"]),
            ).fetchone()
            if already:
                continue
            item = Item(v["id"], v["title"], v["expires_on"], days_left, v["remaining_cents"], stage)
            result.setdefault(user_id, (users[user_id], []))[1].append(item)
    for _, items in result.values():
        items.sort(key=lambda i: (i.days_left, i.title.casefold()))
    return result


def build_mail(display_name: str, items: List[Item], today: date) -> Tuple[str, str]:
    """Betreff und Text. Im Betreff steht nichts aus Nutzereingaben (keine Header-Einschleusung)."""
    settings = get_settings()
    if len(items) == 1:
        subject = "Gutscheinkontor: Ein Gutschein läuft bald ab"
        intro = "dieser Gutschein läuft bald ab:"
    else:
        subject = f"Gutscheinkontor: {len(items)} Gutscheine laufen bald ab"
        intro = "diese Gutscheine laufen bald ab:"
    lines = [f"Hallo {display_name},", "", intro, ""]
    for item in items:
        rest = (
            f", Restwert {formatting.format_euro(item.remaining_cents)}"
            if item.remaining_cents is not None else ""
        )
        sentence = formatting.expiry_sentence(item.expires_on, today)
        lines.append(f"- {item.title}: {sentence} ({formatting.format_date(item.expires_on)}){rest}")
        lines.append(f"  {settings.base_url}/vouchers/{item.voucher_id}")
    lines += [
        "",
        "Aus Sicherheitsgründen steht der Gutscheincode nicht in dieser Mail.",
        f"Erinnerungen abschalten: {settings.base_url}/settings",
    ]
    return subject, "\n".join(lines) + "\n"


def _claim(db: sqlite3.Connection, user_id: int, items: List[Item]) -> List[Item]:
    """Vermerkt die Erinnerungen vor dem Versand. Wer sie zuerst vermerkt, darf sie schicken."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    claimed = []
    with transaction(db):
        for item in items:
            cur = db.execute(
                "INSERT OR IGNORE INTO reminders_sent "
                "(voucher_id, user_id, stage_days, expires_on, sent_at) VALUES (?, ?, ?, ?, ?)",
                (item.voucher_id, user_id, item.stage, item.expires_on, now),
            )
            if cur.rowcount == 1:
                claimed.append(item)
    return claimed


def _unclaim(db: sqlite3.Connection, user_id: int, items: List[Item]) -> None:
    with transaction(db):
        for item in items:
            db.execute(
                "DELETE FROM reminders_sent WHERE voucher_id = ? AND user_id = ? "
                "AND stage_days = ? AND expires_on = ?",
                (item.voucher_id, user_id, item.stage, item.expires_on),
            )


def run_once(now: Optional[datetime] = None) -> int:
    """Ein Prüflauf. Gibt die Zahl der verschickten Mails zurück."""
    settings = get_settings()
    if not settings.reminders_enabled or not mail.is_enabled():
        return 0
    now = now or datetime.now(ZoneInfo(settings.timezone))
    if now.hour < settings.reminder_hour:
        return 0
    today = now.date()

    sent = 0
    db = connect()
    try:
        for user_id, (user, items) in pending(db, today, settings.reminder_days).items():
            claimed = _claim(db, user_id, items)
            if not claimed:
                continue
            subject, body = build_mail(user["display_name"], claimed, today)
            if mail.send_mail(user["email"], subject, body):
                sent += 1
            else:
                _unclaim(db, user_id, claimed)  # beim nächsten Lauf erneut versuchen
    finally:
        db.close()
    return sent


async def scheduler(stop: asyncio.Event, tick: float = TICK_SECONDS) -> None:
    """Prüft regelmäßig auf fällige Erinnerungen, bis `stop` gesetzt wird."""
    if not mail.is_enabled():
        logger.info("E-Mail-Versand ist nicht eingerichtet: Erinnerungen sind ausgeschaltet.")
        return
    while not stop.is_set():
        try:
            await run_in_threadpool(run_once)
        except Exception as exc:  # noqa: BLE001 - der Planer darf nie abstürzen
            logger.error("Erinnerungslauf fehlgeschlagen (%s).", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass
