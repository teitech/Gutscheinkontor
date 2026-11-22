"""Gutscheine weitergeben: Absender (angemeldet) und Empfänger (nur mit persönlichem Link)."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import attachments, dates, gift_pdf, gifts, logos, mail, vouchers
from .config import get_settings
from .db import get_db, transaction
from .ratelimit import limiter
from .routes_auth import client_ip, require_active, require_csrf
from .routes_vouchers import _render_detail
from .templating import render

router = APIRouter()
settings = get_settings()

GIVE_LIMIT = 10             # Weitergaben pro Nutzer ...
GIVE_WINDOW = 60 * 60       # ... pro Stunde
TOKEN_LIMIT_PER_IP = 30     # ungültige Links pro Adresse ...
TOKEN_WINDOW = 15 * 60      # ... in 15 Minuten
NO_INDEX = {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}


def _redirect(voucher_id: int, notice: str) -> RedirectResponse:
    return RedirectResponse(f"/vouchers/{voucher_id}?gift={notice}#weitergeben", status_code=303)


def _voucher_or_404(db: sqlite3.Connection, voucher_id: int) -> Dict[str, Any]:
    voucher = vouchers.get_voucher(db, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404)
    return voucher


def _benefit_lines(view: Dict[str, Any]) -> List[str]:
    """Eckdaten des Gutscheins für Mail und Seite (ohne Code)."""
    lines = []
    if view["kind"] == "percent":
        lines.append(f"Vorteil: {view['percent']} % Rabatt")
    elif view["kind"] == "other":
        lines.append(f"Vorteil: {view['benefit_text']}")
    elif view["value_cents"] is not None:
        lines.append(f"Wert: {view['remaining_text'] or view['value_text']}")
    if view["min_order_cents"] is not None:
        lines.append(f"Mindestbestellwert: {view['min_order_text']}")
    lines.append(f"Gültig bis: {view['expires_text']}" if view["expires_text"] else "Gültig bis: kein Ablaufdatum")
    if view["online_only"]:
        lines.append("Nur online einlösbar")
    if view["shop_url"]:
        lines.append(f"Shop: {view['shop_url']}")
    return lines


def offer_mail(giver: str, view: Dict[str, Any], message: str, count: int, link: str, until: str) -> str:
    lines = [
        "Hallo,", "",
        f"{giver} gibt dir einen Gutschein weiter:", "",
        f"  {view['title']}",
        *[f"  {line}" for line in _benefit_lines(view)],
        "",
    ]
    if message:
        lines += [f"Nachricht von {giver}:", message, ""]
    lines += [
        "So geht es: Öffne den Link und wähle „Gutschein übernehmen“. Erst dann siehst du den Code.",
        "Du brauchst dafür kein Konto.",
    ]
    if count > 1:
        lines.append(
            f"Der Gutschein wurde an {count} Personen geschickt. Wer zuerst übernimmt, bekommt ihn."
        )
    lines += [
        "", f"Dein persönlicher Link (gültig bis {until}):", link, "",
        "Du bekommst diese Mail, weil dir jemand den Gutschein schicken wollte. Möchtest du ihn nicht, "
        "ignoriere die Mail. Deine Adresse wird gelöscht, sobald der Gutschein übernommen wurde oder "
        "die Weitergabe abgelaufen ist.",
    ]
    return "\n".join(lines) + "\n"


def claimed_mail(giver: str, title: str, who: str, link_to_voucher: str) -> str:
    return (
        f"Hallo {giver},\n\n"
        f"dein Gutschein „{title}“ wurde übernommen, und zwar von {who}.\n"
        "Er gilt im Kontor jetzt als weitergegeben, Erinnerungen dazu gibt es nicht mehr.\n\n"
        f"{link_to_voucher}\n"
    )


# --- Absender: weitergeben, zurückholen, verschenkt vermerken, PDF -------------------------


@router.post("/vouchers/{voucher_id}/gift")
def give_by_mail(
    request: Request, voucher_id: int, recipients: str = Form(""), message: str = Form(""),
    csrf_token: str = Form(""), ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    _, user = ctx
    voucher = _voucher_or_404(db, voucher_id)

    def fail(text: str, status: int = 400):
        return _render_detail(request, db, voucher_id, status_code=status, gift_error=text,
                              gift_form_recipients=recipients, gift_form_message=message)

    if not mail.is_enabled():
        return fail("Der E-Mail-Versand ist auf diesem Server nicht eingerichtet.")
    view = vouchers.to_view(voucher, dates.today())
    code, _readable = vouchers.decrypt_code(voucher)
    if not view["is_open"]:
        return fail("Nur offene Gutscheine lassen sich weitergeben.")
    if not code:
        return fail("Ohne lesbaren Code lässt sich der Gutschein nicht weitergeben.")
    emails, error = gifts.parse_recipients(recipients)
    if error:
        return fail(error)
    if len(message.strip()) > gifts.MAX_MESSAGE_LENGTH:
        return fail(f"Die Nachricht darf höchstens {gifts.MAX_MESSAGE_LENGTH} Zeichen lang sein.")
    text = gifts.clean_text(message, gifts.MAX_MESSAGE_LENGTH)

    key = f"gift:{user['id']}"
    if limiter.blocked(key, GIVE_LIMIT, GIVE_WINDOW):
        return fail("Du hast gerade viele Gutscheine weitergegeben. Versuche es später wieder.", 429)
    limiter.add(key, GIVE_WINDOW)

    expires_at = gifts.link_expiry(voucher["expires_on"])
    try:
        with transaction(db):
            if gifts.has_open(db, voucher_id):
                raise gifts.GiftError("Dieser Gutschein ist schon weitergegeben. Hole ihn zuerst zurück.")
            gift_id, targets = gifts.create_link_gift(db, voucher_id, user["id"], emails, text, expires_at)
    except gifts.GiftError as exc:
        return fail(str(exc))

    # Die Mails gehen sofort raus, damit der Absender sieht, ob es geklappt hat. Nach dem ersten
    # Fehler wird nicht weiter versucht: der Server ist dann meist gar nicht erreichbar.
    until = gifts.date_text(expires_at)
    failed: List[int] = []
    broken = False
    for recipient_id, email, token in targets:
        sent = False
        if not broken:
            sent = mail.send_mail(
                email, "Gutscheinkontor: Dir wird ein Gutschein weitergegeben",
                offer_mail(user["display_name"], view, text, len(targets),
                           f"{settings.base_url}/gift/{token}", until),
            )
            broken = not sent
        if not sent:
            failed.append(recipient_id)

    if len(failed) == len(targets):
        with transaction(db):
            gifts.delete_gift(db, gift_id)  # nichts ging raus: Weitergabe zurücknehmen
        return fail("Die Mails konnten nicht verschickt werden. Prüfe den E-Mail-Versand.", 502)
    if failed:
        with transaction(db):
            gifts.drop_recipients(db, gift_id, failed)
        return _redirect(voucher_id, "partial")
    return _redirect(voucher_id, "sent")


@router.post("/vouchers/{voucher_id}/gift/recall")
def recall(
    request: Request, voucher_id: int, csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    _voucher_or_404(db, voucher_id)
    with transaction(db):
        gifts.revoke_open(db, voucher_id)
    return _redirect(voucher_id, "recalled")


@router.post("/vouchers/{voucher_id}/gift/given")
def mark_given(
    request: Request, voucher_id: int, given_to: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    _, user = ctx
    voucher = _voucher_or_404(db, voucher_id)
    view = vouchers.to_view(voucher, dates.today())

    def fail(text: str):
        return _render_detail(request, db, voucher_id, status_code=400, gift_error=text,
                              gift_form_given_to=given_to)

    if not view["is_open"]:
        return fail("Nur offene Gutscheine lassen sich als verschenkt vermerken.")
    name = gifts.clean_text(given_to, gifts.MAX_GIVEN_TO_LENGTH).replace("\n", " ")
    try:
        with transaction(db):
            gifts.mark_given(db, voucher_id, user["id"], name)
    except gifts.GiftError as exc:
        return fail(str(exc))
    return _redirect(voucher_id, "given")


@router.post("/vouchers/{voucher_id}/gift/undo")
def undo_given(
    request: Request, voucher_id: int, csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    _voucher_or_404(db, voucher_id)
    with transaction(db):
        if not gifts.undo_given(db, voucher_id):
            raise HTTPException(status_code=400, detail="Dieser Vermerk lässt sich nicht zurücknehmen.")
    return _redirect(voucher_id, "undone")


def _pdf_response(view: Dict[str, Any], code: str, logo: bytes, name: str) -> Response:
    return Response(
        gift_pdf.build_pdf(view, code, logo), media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/vouchers/{voucher_id}/pdf")
def voucher_pdf(voucher_id: int, ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db)):
    voucher = _voucher_or_404(db, voucher_id)
    code, _readable = vouchers.decrypt_code(voucher)
    return _pdf_response(vouchers.to_view(voucher, dates.today()), code, logos.get(db, voucher_id),
                         "gutschein.pdf")


# --- Empfänger: Link öffnen, übernehmen, Code, PDF, Anhänge -------------------------------------


def _invalid(request: Request, status_code: int = 404):
    return render(request, "gift_invalid.html", status_code=status_code, headers=NO_INDEX)


def _lookup(request: Request, db: sqlite3.Connection, token: str):
    """Link prüfen. Zu viele ungültige Versuche pro Adresse werden gebremst."""
    key = f"gift-token-ip:{client_ip(request)}"
    if limiter.blocked(key, TOKEN_LIMIT_PER_IP, TOKEN_WINDOW):
        return None, 429
    gift = gifts.lookup(db, token)
    if gift is None:
        limiter.add(key, TOKEN_WINDOW)
        return None, 404
    return gift, 200


def _public_context(db: sqlite3.Connection, gift: Dict[str, Any]) -> Dict[str, Any]:
    voucher = vouchers.get_voucher(db, gift["voucher_id"])
    if voucher is None:
        return {}
    view = vouchers.to_view(voucher, dates.today())
    logo = logos.get(db, gift["voucher_id"])
    return dict(
        v=view, facts=_benefit_lines(view), logo=logos.to_data_uri(logo) if logo else None,
        gift=gifts.describe(gift), voucher=voucher,
    )


@router.get("/gift/{token}")
def gift_page(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)):
    gift, status = _lookup(request, db, token)
    if gift is None:
        return _invalid(request, status)
    context = _public_context(db, gift)
    if not context:
        return _invalid(request)
    if gift["phase"] == "offer":
        if context["v"]["status_key"] not in ("aktiv", "teilweise"):
            return _invalid(request)  # inzwischen eingelöst, archiviert oder abgelaufen
        return render(request, "gift_claim.html", token=token, **context, headers=NO_INDEX)

    code, readable = vouchers.decrypt_code(context["voucher"])
    rows = db.execute(
        "SELECT id, original_name, size_bytes FROM voucher_attachments WHERE voucher_id = ? ORDER BY id",
        (gift["voucher_id"],),
    ).fetchall()
    files = [dict(r) for r in rows]
    until = gifts.date_text(gift["claimed_at"] + gifts.ACCESS_TTL)
    return render(request, "gift_code.html", token=token, code=code, code_readable=readable,
                  files=files, until=until, **context, headers=NO_INDEX)


@router.post("/gift/{token}/claim")
def gift_claim(
    request: Request, token: str, background: BackgroundTasks, csrf_token: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    gift, status = _lookup(request, db, token)
    if gift is None:
        return _invalid(request, status)
    if gift["phase"] != "offer":
        return RedirectResponse(f"/gift/{token}", status_code=303)
    with transaction(db):
        won = gifts.claim(db, token, dates.today().isoformat())
    if won is None:
        return _invalid(request)

    # Den Absender informieren (nur, wenn Mail eingerichtet ist). Der Gutscheincode steht nie darin.
    giver = db.execute("SELECT email, display_name FROM users WHERE id = ? AND is_active = 1",
                       (gift["created_by"],)).fetchone()
    voucher = vouchers.get_voucher(db, gift["voucher_id"])
    if giver is not None and voucher is not None and mail.is_enabled():
        background.add_task(
            mail.send_mail, giver["email"], "Gutscheinkontor: Dein Gutschein wurde übernommen",
            claimed_mail(giver["display_name"], voucher["title"], gift["recipient_email"],
                         f"{settings.base_url}/vouchers/{gift['voucher_id']}"),
        )
    return RedirectResponse(f"/gift/{token}", status_code=303)


@router.get("/gift/{token}/pdf")
def gift_pdf_download(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)):
    gift, status = _lookup(request, db, token)
    if gift is None or gift["phase"] != "claimed":
        return _invalid(request, status if gift is None else 404)
    voucher = vouchers.get_voucher(db, gift["voucher_id"])
    if voucher is None:
        return _invalid(request)
    code, _readable = vouchers.decrypt_code(voucher)
    return _pdf_response(vouchers.to_view(voucher, dates.today()), code,
                         logos.get(db, gift["voucher_id"]), "gutschein.pdf")


@router.get("/gift/{token}/files/{attachment_id}")
def gift_attachment(request: Request, token: str, attachment_id: int,
                    db: sqlite3.Connection = Depends(get_db)):
    gift, status = _lookup(request, db, token)
    if gift is None or gift["phase"] != "claimed":
        return _invalid(request, status if gift is None else 404)
    row = db.execute(
        "SELECT * FROM voucher_attachments WHERE id = ? AND voucher_id = ?",
        (attachment_id, gift["voucher_id"]),
    ).fetchone()
    path = attachments.path_for(row["stored_name"]) if row else None
    if row is None or path is None or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type=row["content_type"], filename=row["original_name"],
        content_disposition_type="inline" if row["content_type"].startswith("image/") else "attachment",
        headers={"Content-Security-Policy": "default-src 'none'; sandbox", "Cache-Control": "private, no-store"},
    )
