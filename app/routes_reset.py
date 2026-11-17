"""Passwort vergessen: Reset-Link per E-Mail."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse

from . import auth, mail, security
from .config import get_settings
from .db import get_db, transaction
from .ratelimit import limiter
from .routes_auth import client_ip, require_csrf
from .templating import render

router = APIRouter()
settings = get_settings()

REQUEST_WINDOW = 60 * 60
REQUEST_LIMIT_PER_EMAIL = 3
REQUEST_LIMIT_PER_IP = 10
TOKEN_WINDOW = 15 * 60
TOKEN_LIMIT_PER_IP = 10


def reset_link(token: str) -> str:
    return f"{settings.base_url}/password-reset/{token}"


def reset_mail_body(display_name: str, link: str) -> str:
    return (
        f"Hallo {display_name},\n\n"
        "für dein Konto im Gutscheinkontor wurde ein neues Passwort angefordert.\n"
        "Über diesen Link kannst du es festlegen (eine Stunde gültig, einmal nutzbar):\n\n"
        f"{link}\n\n"
        "Aus Sicherheitsgründen wird dabei die Zwei-Faktor-Anmeldung zurückgesetzt. "
        "Du richtest sie beim nächsten Login neu ein.\n\n"
        "Wenn du das nicht warst, ignoriere diese E-Mail. Dein Passwort bleibt dann unverändert.\n"
    )


def changed_mail_body(display_name: str) -> str:
    return (
        f"Hallo {display_name},\n\n"
        "das Passwort deines Kontos im Gutscheinkontor wurde soeben geändert, "
        "und die Zwei-Faktor-Anmeldung wurde zurückgesetzt.\n\n"
        "Warst du das nicht, wende dich an die Person, die diese Instanz betreibt.\n"
    )


@router.get("/password-reset")
def request_form(request: Request):
    return render(request, "password_reset_request.html")


@router.post("/password-reset")
def request_submit(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(""),
    csrf_token: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    if not mail.is_enabled():
        return render(
            request, "password_reset_request.html", status_code=503,
            error="Der E-Mail-Versand ist auf diesem Server nicht eingerichtet.",
        )

    email = email.strip()
    ip_key = f"reset-ip:{client_ip(request)}"
    mail_key = f"reset-mail:{email.lower()}"
    blocked = limiter.blocked(ip_key, REQUEST_LIMIT_PER_IP, REQUEST_WINDOW) or limiter.blocked(
        mail_key, REQUEST_LIMIT_PER_EMAIL, REQUEST_WINDOW
    )
    limiter.add(ip_key, REQUEST_WINDOW)
    limiter.add(mail_key, REQUEST_WINDOW)

    # Die Antwort ist immer gleich, egal ob es das Konto gibt oder das Limit greift.
    if not blocked:
        user = db.execute(
            "SELECT * FROM users WHERE email = ? AND is_active = 1", (email,)
        ).fetchone()
        if user is not None:
            token = auth.create_password_reset(db, user["id"])
            background.add_task(
                mail.send_mail,
                user["email"],
                "Gutscheinkontor: Passwort zurücksetzen",
                reset_mail_body(user["display_name"], reset_link(token)),
            )
    return render(request, "password_reset_sent.html")


def _invalid(request: Request, status_code: int = 400):
    return render(request, "password_reset_invalid.html", status_code=status_code)


@router.get("/password-reset/{token}")
def reset_form(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)):
    ip_key = f"reset-token-ip:{client_ip(request)}"
    if limiter.blocked(ip_key, TOKEN_LIMIT_PER_IP, TOKEN_WINDOW):
        return _invalid(request, 429)
    if auth.find_password_reset(db, token) is None:
        limiter.add(ip_key, TOKEN_WINDOW)
        return _invalid(request)
    return render(request, "password_reset_form.html", token=token)


@router.post("/password-reset/{token}")
def reset_submit(
    request: Request,
    token: str,
    background: BackgroundTasks,
    password: str = Form(""),
    password_repeat: str = Form(""),
    csrf_token: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    ip_key = f"reset-token-ip:{client_ip(request)}"
    if limiter.blocked(ip_key, TOKEN_LIMIT_PER_IP, TOKEN_WINDOW):
        return _invalid(request, 429)
    if auth.find_password_reset(db, token) is None:
        limiter.add(ip_key, TOKEN_WINDOW)
        return _invalid(request)

    error = security.password_error(password, password_repeat)
    if error:
        return render(request, "password_reset_form.html", status_code=400, token=token, error=error)

    password_hash = security.hash_password(password)
    with transaction(db):
        user_id = auth.consume_password_reset(db, token)  # nur ein Aufruf gewinnt
        if user_id is None:
            return _invalid(request)
        auth.reset_credentials(db, user_id, password_hash)
        user = db.execute("SELECT email, display_name FROM users WHERE id = ?", (user_id,)).fetchone()

    background.add_task(
        mail.send_mail, user["email"], "Gutscheinkontor: Passwort geändert",
        changed_mail_body(user["display_name"]),
    )
    response = RedirectResponse("/login?reset=1", status_code=303)
    response.delete_cookie(settings.session_cookie, path="/")
    return response
