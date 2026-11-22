"""Gutscheinverwaltung: Liste, Formular, Detail, Einlösen, Anhänge, Löschen."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.datastructures import UploadFile

from . import attachments, dates, gifts, mail, vouchers
from .config import get_settings
from .db import get_db, transaction
from .routes_auth import require_active, require_csrf
from .templating import render

router = APIRouter()
settings = get_settings()

UPLOAD_OVERHEAD = 64 * 1024  # Platz für Formularfelder und Multipart-Rahmen
FORM_FIELDS = ("title", "code", "kind", "value", "remaining", "percent", "benefit_text", "min_order",
               "shop_url", "online_only", "expires_on", "owner", "notes", "tags")


# --- Hilfsfunktionen -----------------------------------------------------------


def _owners(db: sqlite3.Connection):
    return db.execute(
        "SELECT id, display_name FROM users WHERE is_active = 1 ORDER BY display_name COLLATE NOCASE"
    ).fetchall()


def _money_input(cents: Optional[int]) -> str:
    return "" if cents is None else f"{cents // 100},{cents % 100:02d}"


def _size_text(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f}".replace(".", ",") + " MB"


def _form_page(request, db, mode, form, errors, status_code=200, voucher=None):
    return render(
        request, "voucher_form.html", status_code=status_code, mode=mode, form=form,
        errors=errors, owners=_owners(db), voucher=voucher, kinds=vouchers.KINDS,
    )


GIFT_NOTICES = {
    "sent": "Der Gutschein wurde per E-Mail weitergegeben.",
    "partial": "Der Gutschein wurde weitergegeben, aber nicht alle Mails konnten verschickt werden. "
               "Prüfe die Zahl der Empfänger unten.",
    "recalled": "Die Weitergabe wurde zurückgeholt. Alle Links sind ungültig.",
    "given": "Der Gutschein ist als verschenkt vermerkt.",
    "undone": "Der Vermerk „verschenkt“ wurde entfernt.",
}


def _render_detail(request: Request, db: sqlite3.Connection, voucher_id: int,
                   status_code: int = 200, **extra: Any):
    voucher = vouchers.get_voucher(db, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404)
    code, readable = vouchers.decrypt_code(voucher)
    view = vouchers.to_view(voucher, dates.today())
    gift = gifts.describe(gifts.latest(db, voucher_id))
    can_give = bool(view["is_open"] and code and (gift is None or gift["status"] != "open"))
    extra.setdefault("gift_notice", GIFT_NOTICES.get(request.query_params.get("gift", "")))
    rows = db.execute(
        "SELECT * FROM voucher_attachments WHERE voucher_id = ? ORDER BY id", (voucher_id,)
    ).fetchall()
    files = [
        dict(r, is_image=r["content_type"].startswith("image/"), size_text=_size_text(r["size_bytes"]))
        for r in rows
    ]
    return render(
        request, "voucher_detail.html", status_code=status_code,
        v=view, code=code, code_readable=readable, gift=gift, can_give=can_give,
        gift_mail_enabled=mail.is_enabled(), max_recipients=gifts.MAX_RECIPIENTS,
        gift_message_max=gifts.MAX_MESSAGE_LENGTH,
        logo_fetch_enabled=settings.logo_fetch_enabled,
        files=files, can_upload=len(files) < attachments.MAX_ATTACHMENTS_PER_VOUCHER,
        max_upload_mb=settings.max_upload_mb, max_files=attachments.MAX_ATTACHMENTS_PER_VOUCHER,
        **extra,
    )


def _redirect_detail(voucher_id: int) -> RedirectResponse:
    return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)


# --- Liste -------------------------------------------------------------------------


@router.get("/")
def voucher_list(
    request: Request,
    q: str = "",
    status: str = "aktuell",
    tag: str = "",
    owner: str = "",
    sort: str = "ablauf",
    ctx=Depends(require_active),
    db: sqlite3.Connection = Depends(get_db),
):
    if status not in {key for key, _ in vouchers.FILTER_OPTIONS}:
        status = "aktuell"
    if sort not in {key for key, _ in vouchers.SORT_OPTIONS}:
        sort = "ablauf"
    q = q.strip()[:100]

    today = dates.today()
    views = [vouchers.to_view(v, today) for v in vouchers.load_all(db)]
    selected = vouchers.select_vouchers(views, q, status, tag.strip(), owner.strip(), sort)
    all_tags = sorted({t for v in views for t in v["tags"]}, key=str.casefold)
    filtered = bool(q or tag or owner or status != "aktuell")
    return render(
        request, "vouchers_list.html", vouchers=selected, total_count=len(views),
        summary=vouchers.summary(views), owners=_owners(db), all_tags=all_tags,
        status_options=vouchers.FILTER_OPTIONS, sort_options=vouchers.SORT_OPTIONS,
        q=q, status=status, tag=tag.strip(), owner=owner.strip(), sort=sort, filtered=filtered,
    )


# --- Anlegen und Bearbeiten ------------------------------------------------------------


@router.get("/vouchers/new")
def new_form(request: Request, ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db)):
    _, user = ctx
    form = {field: "" for field in FORM_FIELDS}
    form["kind"] = "amount"
    form["owner"] = str(user["id"])
    return _form_page(request, db, "new", form, {})


@router.post("/vouchers")
def create(
    request: Request,
    title: str = Form(""), code: str = Form(""), value: str = Form(""),
    remaining: str = Form(""), kind: str = Form("amount"), percent: str = Form(""),
    benefit_text: str = Form(""), min_order: str = Form(""),
    shop_url: str = Form(""), online_only: str = Form(""),
    expires_on: str = Form(""), owner: str = Form(""),
    notes: str = Form(""), tags: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    _, user = ctx
    form = dict(title=title, code=code, value=value, remaining=remaining, kind=kind,
                percent=percent, benefit_text=benefit_text, min_order=min_order,
                shop_url=shop_url, online_only=online_only,
                expires_on=expires_on, owner=owner, notes=notes, tags=tags)
    data, errors = vouchers.validate_form(form, {o["id"] for o in _owners(db)})
    if data is None:
        return _form_page(request, db, "new", form, errors, status_code=400)
    with transaction(db):
        voucher_id = vouchers.create_voucher(db, data, user["id"])
    return _redirect_detail(voucher_id)


@router.get("/vouchers/{voucher_id}/edit")
def edit_form(request: Request, voucher_id: int, ctx=Depends(require_active),
              db: sqlite3.Connection = Depends(get_db)):
    voucher = vouchers.get_voucher(db, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404)
    code, _readable = vouchers.decrypt_code(voucher)
    form = dict(
        title=voucher["title"], code=code, value=_money_input(voucher["value_cents"]),
        remaining=_money_input(voucher["remaining_cents"]), kind=voucher["kind"],
        percent=str(voucher["percent"] or ""), benefit_text=voucher["benefit_text"],
        min_order=_money_input(voucher["min_order_cents"]),
        shop_url=voucher["shop_url"], online_only="on" if voucher["online_only"] else "", expires_on=voucher["expires_on"] or "",
        owner=str(voucher["owner_user_id"] or ""), notes=voucher["notes"],
        tags=", ".join(voucher["tags"]),
    )
    return _form_page(request, db, "edit", form, {}, voucher=voucher)


@router.post("/vouchers/{voucher_id}/edit")
def edit_submit(
    request: Request, voucher_id: int,
    title: str = Form(""), code: str = Form(""), value: str = Form(""),
    remaining: str = Form(""), kind: str = Form("amount"), percent: str = Form(""),
    benefit_text: str = Form(""), min_order: str = Form(""),
    shop_url: str = Form(""), online_only: str = Form(""),
    expires_on: str = Form(""), owner: str = Form(""),
    notes: str = Form(""), tags: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    voucher = vouchers.get_voucher(db, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404)
    form = dict(title=title, code=code, value=value, remaining=remaining, kind=kind,
                percent=percent, benefit_text=benefit_text, min_order=min_order,
                shop_url=shop_url, online_only=online_only,
                expires_on=expires_on, owner=owner, notes=notes, tags=tags)
    data, errors = vouchers.validate_form(form, {o["id"] for o in _owners(db)})
    if data is None:
        return _form_page(request, db, "edit", form, errors, status_code=400, voucher=voucher)
    with transaction(db):
        vouchers.update_voucher(db, voucher_id, data)
    return _redirect_detail(voucher_id)


# --- Detail, Einlösen, Zustand --------------------------------------------------------------


@router.get("/vouchers/{voucher_id}")
def detail(request: Request, voucher_id: int, ctx=Depends(require_active),
           db: sqlite3.Connection = Depends(get_db)):
    return _render_detail(request, db, voucher_id)


@router.post("/vouchers/{voucher_id}/redeem")
def redeem(
    request: Request, voucher_id: int, amount: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    try:
        cents = vouchers.parse_money(amount)
    except ValueError as exc:
        return _render_detail(request, db, voucher_id, status_code=400, redeem_error=str(exc))
    if cents is None:
        return _render_detail(request, db, voucher_id, status_code=400,
                              redeem_error="Bitte gib einen Betrag an.")
    with transaction(db):
        error = vouchers.redeem_amount(db, voucher_id, cents)
    if error:
        return _render_detail(request, db, voucher_id, status_code=400, redeem_error=error)
    return _redirect_detail(voucher_id)


@router.post("/vouchers/{voucher_id}/state")
def change_state(
    request: Request, voucher_id: int, action: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    if action not in vouchers.STATE_ACTIONS:
        raise HTTPException(status_code=400, detail="Unbekannte Aktion.")
    with transaction(db):
        found = vouchers.apply_state_action(db, voucher_id, action)
    if not found:
        raise HTTPException(status_code=404)
    return _redirect_detail(voucher_id)


# --- Löschen ---------------------------------------------------------------------------------


@router.get("/vouchers/{voucher_id}/delete")
def delete_confirm(request: Request, voucher_id: int, ctx=Depends(require_active),
                   db: sqlite3.Connection = Depends(get_db)):
    voucher = vouchers.get_voucher(db, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404)
    return render(request, "voucher_delete.html", v=voucher)


@router.post("/vouchers/{voucher_id}/delete")
def delete(
    request: Request, voucher_id: int, csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    with transaction(db):
        names = [r["stored_name"] for r in db.execute(
            "SELECT stored_name FROM voucher_attachments WHERE voucher_id = ?", (voucher_id,))]
        deleted = db.execute("DELETE FROM vouchers WHERE id = ?", (voucher_id,)).rowcount
    if not deleted:
        raise HTTPException(status_code=404)
    attachments.delete_files(names)  # erst nach dem Commit: schlägt es fehl, bleiben nur Dateireste
    return RedirectResponse("/", status_code=303)


# --- Anhänge -----------------------------------------------------------------------------------


@router.post("/vouchers/{voucher_id}/attachments")
async def upload_attachment(
    request: Request, voucher_id: int,
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    # Ohne File-Parameter parst FastAPI den Body nicht vorab: Login und Größe werden zuerst geprüft.
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > settings.max_upload_bytes + UPLOAD_OVERHEAD:
        return _render_detail(
            request, db, voucher_id, status_code=413,
            upload_error=f"Die Datei ist zu groß (höchstens {settings.max_upload_mb} MB).",
        )

    form = await request.form()
    try:
        require_csrf(request, str(form.get("csrf_token", "")))
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return _render_detail(request, db, voucher_id, status_code=400,
                                  upload_error="Bitte wähle eine Datei aus.")
        if db.execute("SELECT 1 FROM vouchers WHERE id = ?", (voucher_id,)).fetchone() is None:
            raise HTTPException(status_code=404)
        count = db.execute(
            "SELECT COUNT(*) FROM voucher_attachments WHERE voucher_id = ?", (voucher_id,)
        ).fetchone()[0]
        if count >= attachments.MAX_ATTACHMENTS_PER_VOUCHER:
            return _render_detail(
                request, db, voucher_id, status_code=400,
                upload_error=f"Höchstens {attachments.MAX_ATTACHMENTS_PER_VOUCHER} Anhänge pro Gutschein.",
            )
        try:
            saved = await attachments.save_upload(upload, settings.max_upload_bytes)
        except attachments.UploadError as exc:
            return _render_detail(request, db, voucher_id, status_code=400, upload_error=str(exc))
        try:
            with transaction(db):
                db.execute(
                    "INSERT INTO voucher_attachments (voucher_id, stored_name, original_name, "
                    "content_type, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (voucher_id, saved.stored_name, saved.original_name, saved.content_type,
                     saved.size_bytes, vouchers._now_iso()),
                )
        except BaseException:
            attachments.delete_files([saved.stored_name])
            raise
    finally:
        await form.close()
    return _redirect_detail(voucher_id)


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: int, ctx=Depends(require_active),
                        db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM voucher_attachments WHERE id = ?", (attachment_id,)).fetchone()
    path = attachments.path_for(row["stored_name"]) if row else None
    if row is None or path is None or not path.is_file():
        raise HTTPException(status_code=404)
    inline = row["content_type"].startswith("image/")
    return FileResponse(
        path, media_type=row["content_type"], filename=row["original_name"],
        content_disposition_type="inline" if inline else "attachment",
        headers={
            # Hochgeladene Dateien dürfen nie Skripte ausführen oder Ressourcen laden.
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/attachments/{attachment_id}/delete")
def delete_attachment(
    request: Request, attachment_id: int, csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    with transaction(db):
        row = db.execute("SELECT * FROM voucher_attachments WHERE id = ?", (attachment_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        db.execute("DELETE FROM voucher_attachments WHERE id = ?", (attachment_id,))
    attachments.delete_files([row["stored_name"]])
    return _redirect_detail(row["voucher_id"])
