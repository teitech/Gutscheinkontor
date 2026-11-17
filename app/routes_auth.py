"""Einrichtung, Login mit 2FA und Logout."""
from __future__ import annotations

import hmac
import re
import sqlite3
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from . import auth, security
from .config import get_settings
from .db import get_db, transaction
from .ratelimit import limiter
from .templating import Redirect, render

router = APIRouter()
settings = get_settings()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOGIN_WINDOW = 15 * 60
LOGIN_LIMIT_PER_EMAIL = 5
LOGIN_LIMIT_PER_IP = 20
MAX_2FA_ATTEMPTS = 5
MAX_SETUP_ATTEMPTS = 10


# --- Hilfsfunktionen ----------------------------------------------------------


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unbekannt"


def require_csrf(request: Request, submitted: str) -> None:
    ok = security.csrf_valid(getattr(request.state, "csrf", ""), submitted)
    if not ok or not security.origin_ok(request.headers, settings.base_url):
        raise HTTPException(status_code=403, detail="Ungültige Anfrage (CSRF-Prüfung fehlgeschlagen).")


def set_session_cookie(response: Response, token: str, stage: str) -> None:
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=auth.ACTIVE_TTL if stage == "active" else None,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def user_count(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def session_cookie_token(request: Request) -> Optional[str]:
    return request.cookies.get(settings.session_cookie)


def require_stage(stage: str):
    """Dependency: Session muss in genau diesem Zustand sein, sonst Umleitung zum Login."""

    def dependency(
        request: Request, db: sqlite3.Connection = Depends(get_db)
    ) -> Tuple[sqlite3.Row, sqlite3.Row]:
        session = auth.get_session(db, session_cookie_token(request))
        if session is None or session["stage"] != stage:
            raise Redirect("/login")
        user = db.execute(
            "SELECT * FROM users WHERE id = ? AND is_active = 1", (session["user_id"],)
        ).fetchone()
        if user is None:
            raise Redirect("/login")
        if stage == "active":
            request.state.user = user
        return session, user

    return dependency


require_active = require_stage("active")


# --- Ersteinrichtung ---------------------------------------------------------


@router.get("/setup")
def setup_form(request: Request, db: sqlite3.Connection = Depends(get_db)):
    if user_count(db) > 0:
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html", needs_token=bool(settings.setup_token))


@router.post("/setup")
def setup_submit(
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    password_repeat: str = Form(""),
    setup_token: str = Form(""),
    csrf_token: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    if user_count(db) > 0:
        return RedirectResponse("/login", status_code=303)

    display_name = display_name.strip()
    email = email.strip()
    error = None
    if settings.setup_token and not hmac.compare_digest(setup_token.strip(), settings.setup_token):
        error = "Das Einrichtungs-Token ist falsch."
    elif not display_name or len(display_name) > 100:
        error = "Bitte gib einen Namen an (höchstens 100 Zeichen)."
    elif len(email) > 254 or not EMAIL_RE.match(email):
        error = "Bitte gib eine gültige E-Mail-Adresse an."
    else:
        error = security.password_error(password, password_repeat)
    if error:
        return render(
            request, "setup.html", status_code=400, error=error,
            display_name=display_name, email=email, needs_token=bool(settings.setup_token),
        )

    password_hash = security.hash_password(password)
    with transaction(db):
        # Erneut prüfen: zwei gleichzeitige Anfragen dürfen nicht beide einen Admin anlegen.
        if user_count(db) > 0:
            return RedirectResponse("/login", status_code=303)
        cur = db.execute(
            "INSERT INTO users (email, display_name, password_hash, is_admin) VALUES (?, ?, ?, 1)",
            (email, display_name, password_hash),
        )
        token = auth.create_session(db, cur.lastrowid, "pending_setup")
    response = RedirectResponse("/2fa/setup", status_code=303)
    set_session_cookie(response, token, "pending_setup")
    return response


# --- Login ------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request, db: sqlite3.Connection = Depends(get_db)):
    session = auth.get_session(db, session_cookie_token(request))
    if session is not None and session["stage"] == "active":
        return RedirectResponse("/", status_code=303)
    if user_count(db) == 0:
        return RedirectResponse("/setup", status_code=303)
    notice = None
    if request.query_params.get("reset") == "1":  # feste Meldung, kein Text aus der URL
        notice = (
            "Dein Passwort wurde geändert. Melde dich an und richte danach "
            "die Zwei-Faktor-Anmeldung neu ein."
        )
    return render(request, "login.html", notice=notice)


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    email = email.strip()
    ip_key = f"login-ip:{client_ip(request)}"
    mail_key = f"login-mail:{email.lower()}"

    if limiter.blocked(ip_key, LOGIN_LIMIT_PER_IP, LOGIN_WINDOW) or limiter.blocked(
        mail_key, LOGIN_LIMIT_PER_EMAIL, LOGIN_WINDOW
    ):
        return render(
            request, "login.html", status_code=429, email=email,
            error="Zu viele Anmeldeversuche. Bitte warte einige Minuten.",
        )

    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    # Auch bei unbekannter E-Mail wird ein Hash geprüft (gleiche Antwortzeit).
    password_ok = security.verify_password(
        user["password_hash"] if user else security.DUMMY_HASH, password[: security.MAX_PASSWORD_LENGTH]
    )
    if user is None or not password_ok or not user["is_active"]:
        limiter.add(ip_key, LOGIN_WINDOW)
        limiter.add(mail_key, LOGIN_WINDOW)
        return render(
            request, "login.html", status_code=401, email=email,
            error="E-Mail oder Passwort ist falsch.",
        )

    limiter.clear(mail_key)
    if security.needs_rehash(user["password_hash"]):
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (security.hash_password(password), user["id"]),
        )

    auth.purge_expired_sessions(db)
    auth.delete_session(db, session_cookie_token(request))  # alte Sitzung nie weiterverwenden
    stage = "pending_2fa" if user["totp_enabled"] else "pending_setup"
    token = auth.create_session(db, user["id"], stage)
    response = RedirectResponse(
        "/login/2fa" if stage == "pending_2fa" else "/2fa/setup", status_code=303
    )
    set_session_cookie(response, token, stage)
    return response


@router.get("/login/2fa")
def twofa_form(request: Request, ctx=Depends(require_stage("pending_2fa"))):
    return render(request, "twofa_verify.html")


@router.post("/login/2fa")
def twofa_submit(
    request: Request,
    code: str = Form(""),
    csrf_token: str = Form(""),
    ctx=Depends(require_stage("pending_2fa")),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    session, user = ctx
    if session["failed_attempts"] >= MAX_2FA_ATTEMPTS:
        auth.delete_session(db, session_cookie_token(request))
        return RedirectResponse("/login", status_code=303)

    # Versuch vor der Prüfung zählen, damit parallele Anfragen das Limit nicht umgehen.
    auth.count_failed_attempt(db, session)
    code = code.strip()
    with transaction(db):
        if len(code.replace(" ", "")) == 6 and code.replace(" ", "").isdigit():
            ok = auth.verify_totp(db, user, code)
        else:
            ok = auth.use_recovery_code(db, user["id"], code)
    if not ok:
        return render(request, "twofa_verify.html", status_code=401,
                      error="Der Code ist ungültig oder bereits verwendet.")

    auth.delete_session(db, session_cookie_token(request))
    token = auth.create_session(db, user["id"], "active")
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, token, "active")
    return response


# --- 2FA einrichten ---------------------------------------------------------


def _group(secret: str) -> str:
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


@router.get("/2fa/setup")
def twofa_setup_form(
    request: Request,
    ctx=Depends(require_stage("pending_setup")),
    db: sqlite3.Connection = Depends(get_db),
):
    _, user = ctx
    secret = auth.get_or_create_totp_secret(db, user)
    qr = auth.qr_svg(auth.totp_uri(secret, user["email"]))
    return render(request, "twofa_setup.html", qr=qr, secret=_group(secret))


@router.post("/2fa/setup")
def twofa_setup_submit(
    request: Request,
    code: str = Form(""),
    csrf_token: str = Form(""),
    ctx=Depends(require_stage("pending_setup")),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    session, user = ctx
    if session["failed_attempts"] >= MAX_SETUP_ATTEMPTS:
        auth.delete_session(db, session_cookie_token(request))
        return RedirectResponse("/login", status_code=303)

    auth.count_failed_attempt(db, session)
    with transaction(db):
        ok = auth.verify_totp(db, user, code)
        if ok:
            db.execute("UPDATE users SET totp_enabled = 1 WHERE id = ?", (user["id"],))
            codes = auth.generate_recovery_codes(db, user["id"])
            auth.delete_session(db, session_cookie_token(request))
            token = auth.create_session(db, user["id"], "active")
    if not ok:
        secret = auth.get_or_create_totp_secret(db, user)
        qr = auth.qr_svg(auth.totp_uri(secret, user["email"]))
        return render(request, "twofa_setup.html", status_code=401, qr=qr,
                      secret=_group(secret), error="Der Code stimmt nicht. Bitte erneut versuchen.")

    response = render(request, "recovery_codes.html", codes=codes)
    set_session_cookie(response, token, "active")
    return response


# --- Logout -----------------------------------------------------------------


@router.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    auth.delete_session(db, session_cookie_token(request))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie, path="/")
    return response
