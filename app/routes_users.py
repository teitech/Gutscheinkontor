"""Nutzer einladen und verwalten (Administratoren) und Einladung annehmen (öffentlicher Link)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import auth, mail, security
from .config import get_settings
from .db import get_db, transaction
from .ratelimit import limiter
from .routes_auth import (EMAIL_RE, client_ip, require_admin, require_csrf,
                          session_cookie_token, set_session_cookie)
from .templating import render

router = APIRouter()
settings = get_settings()

MAX_USERS = 50
INVITE_WINDOW = 60 * 60
INVITE_LIMIT = 20
TOKEN_WINDOW = 15 * 60
TOKEN_LIMIT_PER_IP = 10
USER_ACTIONS = ("disable", "enable", "make-admin", "remove-admin")
INVALID_INVITE = (
    "Diese Einladung ist ungültig, abgelaufen oder wurde schon benutzt. "
    "Bitte den Administrator um eine neue."
)


# --- Hilfsfunktionen -----------------------------------------------------------------


def _fmt_date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, ZoneInfo(settings.timezone)).strftime("%d.%m.%Y")


def _page(request: Request, db: sqlite3.Connection, status_code: int = 200, **extra):
    users = db.execute(
        "SELECT id, display_name, email, is_admin, is_active, totp_enabled FROM users "
        "ORDER BY is_active DESC, display_name COLLATE NOCASE"
    ).fetchall()
    now = int(auth._now())
    pending = [
        dict(r, expires_text=_fmt_date(r["expires_at"]))
        for r in db.execute(
            "SELECT i.id, i.email, i.expires_at, u.display_name AS inviter FROM invitations i "
            "LEFT JOIN users u ON u.id = i.invited_by WHERE i.expires_at > ? ORDER BY i.id DESC", (now,))
    ]
    return render(
        request, "users.html", status_code=status_code, users=users, pending=pending,
        self_id=request.state.user["id"], mail_enabled=mail.is_enabled(),
        invite_days=auth.INVITE_TTL // 86400, **extra,
    )


def _active_admins_besides(db: sqlite3.Connection, user_id: int) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1 AND id != ?", (user_id,)
    ).fetchone()[0]


def _check_may_remove(db: sqlite3.Connection, actor_id: int, target: sqlite3.Row, what: str) -> None:
    """Gemeinsame Schutzregeln für Sperren, Löschen und Entziehen der Admin-Rechte."""
    if target["id"] == actor_id:
        raise HTTPException(status_code=400, detail=f"{what} geht bei deinem eigenen Konto nicht.")
    if target["is_admin"] and target["is_active"] and _active_admins_besides(db, target["id"]) == 0:
        raise HTTPException(status_code=400, detail="Es muss mindestens ein aktiver Administrator übrig bleiben.")


def _get_user(db: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404)
    return row


def _invite_mail_body(inviter: str, link: str) -> str:
    return (
        "Hallo,\n\n"
        f"{inviter} lädt dich ins Gutscheinkontor ein, die gemeinsame Verwaltung von Gutscheinen.\n"
        f"Über diesen Link legst du dein Konto an (gültig {auth.INVITE_TTL // 86400} Tage, nur einmal nutzbar):\n\n"
        f"{link}\n\n"
        "Du denkst dir ein Passwort aus und richtest danach eine Zwei-Faktor-Anmeldung mit einer "
        "Authenticator-App ein.\n\n"
        "Wenn du diese Einladung nicht erwartet hast, ignoriere die E-Mail.\n"
    )


# --- Verwaltung (nur Administratoren) -----------------------------------------------------


@router.get("/users")
def users_page(request: Request, ctx=Depends(require_admin), db: sqlite3.Connection = Depends(get_db)):
    return _page(request, db)


@router.post("/users/invite")
def invite(
    request: Request, email: str = Form(""), send_mail: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_admin), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    admin = ctx[1]
    email = email.strip()

    key = f"invite:{admin['id']}"
    if limiter.blocked(key, INVITE_LIMIT, INVITE_WINDOW):
        return _page(request, db, status_code=429, invite_email_value=email,
                     invite_error="Zu viele Einladungen in kurzer Zeit. Bitte warte eine Weile.")
    limiter.add(key, INVITE_WINDOW)

    error = None
    token = None
    with transaction(db):
        auth.purge_expired_invitations(db)
        if len(email) > 254 or not EMAIL_RE.match(email):
            error = "Bitte gib eine gültige E-Mail-Adresse an."
        elif db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            error = "Zu dieser E-Mail-Adresse gibt es schon ein Konto."
        elif db.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= MAX_USERS:
            error = f"Es sind höchstens {MAX_USERS} Konten möglich."
        elif db.execute(
            "SELECT COUNT(*) FROM invitations WHERE email != ?", (email,)
        ).fetchone()[0] >= auth.MAX_PENDING_INVITATIONS:
            error = f"Es können höchstens {auth.MAX_PENDING_INVITATIONS} Einladungen offen sein."
        else:
            token = auth.create_invitation(db, email, admin["id"])
    if error:
        return _page(request, db, status_code=400, invite_error=error, invite_email_value=email)

    link = f"{settings.base_url}/invite/{token}"
    sent = None
    if send_mail == "on" and mail.is_enabled():
        sent = mail.send_mail(email, "Einladung zum Gutscheinkontor",
                              _invite_mail_body(admin["display_name"], link))
    # Direkt als Antwort anzeigen, nicht per Weiterleitung: Der Link steht nie in einer URL.
    return _page(request, db, invite_link=link, invite_email=email, invite_mail_sent=sent)


@router.post("/users/invitations/{invitation_id}/revoke")
def revoke_invitation(
    request: Request, invitation_id: int, csrf_token: str = Form(""),
    ctx=Depends(require_admin), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    with transaction(db):
        deleted = db.execute("DELETE FROM invitations WHERE id = ?", (invitation_id,)).rowcount
    if not deleted:
        raise HTTPException(status_code=404)
    return RedirectResponse("/users", status_code=303)


@router.get("/users/{user_id}/delete")
def delete_confirm(request: Request, user_id: int, ctx=Depends(require_admin),
                   db: sqlite3.Connection = Depends(get_db)):
    return render(request, "user_delete.html", target=_get_user(db, user_id))


@router.post("/users/{user_id}/delete")
def delete_user(
    request: Request, user_id: int, csrf_token: str = Form(""),
    ctx=Depends(require_admin), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    with transaction(db):
        target = _get_user(db, user_id)
        _check_may_remove(db, ctx[1]["id"], target, "Löschen")
        # Gutscheine bleiben erhalten (Zuordnung wird leer), Sitzungen und Reset-Links verschwinden.
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/{action}")
def change_user(
    request: Request, user_id: int, action: str, csrf_token: str = Form(""),
    ctx=Depends(require_admin), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    if action not in USER_ACTIONS:
        raise HTTPException(status_code=404)
    actor_id = ctx[1]["id"]
    with transaction(db):
        target = _get_user(db, user_id)
        if action == "disable":
            _check_may_remove(db, actor_id, target, "Sperren")
            db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))      # sofort abgemeldet
            db.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        elif action == "enable":
            db.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
        elif action == "make-admin":
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
        else:  # remove-admin
            _check_may_remove(db, actor_id, target, "Das Entziehen der Admin-Rechte")
            db.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_id,))
    return RedirectResponse("/users", status_code=303)


# --- Einladung annehmen (öffentlich, über den Link) --------------------------------------------------


def _invalid(request: Request, status_code: int = 400):
    return render(request, "error.html", status_code=status_code, message=INVALID_INVITE)


@router.get("/invite/{token}")
def accept_form(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)):
    ip_key = f"invite-token-ip:{client_ip(request)}"
    if limiter.blocked(ip_key, TOKEN_LIMIT_PER_IP, TOKEN_WINDOW):
        return _invalid(request, 429)
    invitation = auth.find_invitation(db, token)
    if invitation is None:
        limiter.add(ip_key, TOKEN_WINDOW)
        return _invalid(request)
    return render(request, "invite.html", token=token, email=invitation["email"])


@router.post("/invite/{token}")
def accept_submit(
    request: Request, token: str, display_name: str = Form(""), password: str = Form(""),
    password_repeat: str = Form(""), csrf_token: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    ip_key = f"invite-token-ip:{client_ip(request)}"
    if limiter.blocked(ip_key, TOKEN_LIMIT_PER_IP, TOKEN_WINDOW):
        return _invalid(request, 429)
    invitation = auth.find_invitation(db, token)
    if invitation is None:
        limiter.add(ip_key, TOKEN_WINDOW)
        return _invalid(request)

    display_name = display_name.strip()
    error = None
    if not display_name or len(display_name) > 100:
        error = "Bitte gib einen Namen an (höchstens 100 Zeichen)."
    else:
        error = security.password_error(password, password_repeat)
    if error:
        return render(request, "invite.html", status_code=400, token=token,
                      email=invitation["email"], display_name=display_name, error=error)

    password_hash = security.hash_password(password)
    with transaction(db):
        accepted = auth.consume_invitation(db, token)  # nur ein Aufruf gewinnt
        if accepted is None:
            return _invalid(request)
        if db.execute("SELECT 1 FROM users WHERE email = ?", (accepted["email"],)).fetchone():
            return render(request, "error.html", status_code=400,
                          message="Zu dieser E-Mail-Adresse gibt es schon ein Konto.")
        cur = db.execute(
            "INSERT INTO users (email, display_name, password_hash, is_admin) VALUES (?, ?, ?, 0)",
            (accepted["email"], display_name, password_hash),
        )
        auth.delete_session(db, session_cookie_token(request))  # keine alte Sitzung mitnehmen
        session_token = auth.create_session(db, cur.lastrowid, "pending_setup")
    # Weiter wie bei der Ersteinrichtung: Zwei-Faktor-Anmeldung ist Pflicht.
    response = RedirectResponse("/2fa/setup", status_code=303)
    set_session_cookie(response, session_token, "pending_setup")
    return response
