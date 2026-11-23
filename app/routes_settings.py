"""Persönliche Einstellungen: Erinnerungen ein- und ausschalten, Testmail."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import mail, reminders
from .config import get_settings
from .db import get_db, transaction
from .ratelimit import limiter
from .routes_auth import require_active, require_csrf
from .templating import render

router = APIRouter()
settings = get_settings()

TEST_MAIL_WINDOW = 60 * 60
TEST_MAIL_LIMIT = 3


def _days_text(days) -> str:
    return ", ".join(str(d) for d in days)


def _page(request: Request, db: sqlite3.Connection, user_id: int, status_code: int = 200, **extra):
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return render(
        request, "settings.html", status_code=status_code, account=user,
        mail_enabled=mail.is_enabled(), reminders_on=settings.reminders_enabled,
        days_text=_days_text(settings.reminder_days), hour=reminders.reminder_hour(db),
        timezone=settings.timezone, **extra,
    )


@router.get("/settings")
def settings_page(request: Request, ctx=Depends(require_active),
                  db: sqlite3.Connection = Depends(get_db)):
    return _page(request, db, ctx[1]["id"], saved=request.query_params.get("saved") == "1")


@router.post("/settings")
def settings_save(
    request: Request, reminders: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    with transaction(db):
        db.execute(
            "UPDATE users SET reminders_enabled = ? WHERE id = ?",
            (1 if reminders == "on" else 0, ctx[1]["id"]),
        )
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/reminder-hour")
def save_reminder_hour(
    request: Request, hour: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    if not ctx[1]["is_admin"]:
        raise HTTPException(status_code=403, detail="Nur Admins dürfen das ändern.")
    value = hour.strip()
    if not (value.isdigit() and len(value) <= 2 and 0 <= int(value) <= 23):
        return _page(request, db, ctx[1]["id"], status_code=400,
                     hour_error="Bitte eine volle Stunde von 0 bis 23 angeben.")
    with transaction(db):
        reminders.set_reminder_hour(db, int(value))
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/test-mail")
def send_test_mail(
    request: Request, csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    user = ctx[1]
    if not mail.is_enabled():
        return _page(request, db, user["id"], status_code=503,
                     test_error="Der E-Mail-Versand ist auf diesem Server nicht eingerichtet.")
    key = f"test-mail:{user['id']}"
    if limiter.blocked(key, TEST_MAIL_LIMIT, TEST_MAIL_WINDOW):
        return _page(request, db, user["id"], status_code=429,
                     test_error="Zu viele Testmails. Bitte warte eine Weile.")
    limiter.add(key, TEST_MAIL_WINDOW)
    ok = mail.send_mail(
        user["email"], "Gutscheinkontor: Testmail",
        f"Hallo {user['display_name']},\n\n"
        "diese Testmail zeigt, dass der E-Mail-Versand des Gutscheinkontors funktioniert.\n",
    )
    if not ok:
        return _page(request, db, user["id"], status_code=502,
                     test_error="Der Mailserver hat die Nachricht nicht angenommen. Mehr steht im Log des Containers.")
    return _page(request, db, user["id"], test_ok=True)
