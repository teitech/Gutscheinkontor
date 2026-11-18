"""Logos: hochladen, von der Webseite des Shops holen, anzeigen, entfernen."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from . import logofetch, logos, vouchers
from .config import get_settings
from .db import get_db, transaction
from .ratelimit import limiter
from .routes_auth import require_active, require_csrf
from .routes_vouchers import _redirect_detail, _render_detail
from .templating import render

router = APIRouter()
settings = get_settings()

UPLOAD_OVERHEAD = 64 * 1024
SEARCH_LIMIT = 10          # Suchen pro Nutzer ...
SEARCH_WINDOW = 10 * 60    # ... in diesen Sekunden


def _voucher_or_404(db: sqlite3.Connection, voucher_id: int):
    voucher = vouchers.get_voucher(db, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404)
    return voucher


@router.get("/vouchers/{voucher_id}/logo")
def show_logo(voucher_id: int, ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db)):
    png = logos.get(db, voucher_id)
    if png is None:
        raise HTTPException(status_code=404)
    # Die Adresse enthält die Version (?v=), deshalb darf der Browser lange zwischenspeichern.
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/vouchers/{voucher_id}/logo")
async def upload_logo(request: Request, voucher_id: int, ctx=Depends(require_active),
                      db: sqlite3.Connection = Depends(get_db)):
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > logos.MAX_INPUT_BYTES + UPLOAD_OVERHEAD:
        return _render_detail(request, db, voucher_id, status_code=413,
                              logo_error="Das Bild ist zu groß (höchstens 2 MB).")
    form = await request.form()
    try:
        require_csrf(request, str(form.get("csrf_token", "")))
        _voucher_or_404(db, voucher_id)
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return _render_detail(request, db, voucher_id, status_code=400,
                                  logo_error="Bitte wähle ein Bild aus.")
        data = await upload.read(logos.MAX_INPUT_BYTES + 1)
        try:
            png = logos.process(data)
        except logos.LogoError as exc:
            return _render_detail(request, db, voucher_id, status_code=400, logo_error=str(exc))
        with transaction(db):
            logos.save(db, voucher_id, png)
    finally:
        await form.close()
    return _redirect_detail(voucher_id)


@router.post("/vouchers/{voucher_id}/logo/delete")
def delete_logo(request: Request, voucher_id: int, csrf_token: str = Form(""),
                ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db)):
    require_csrf(request, csrf_token)
    _voucher_or_404(db, voucher_id)
    with transaction(db):
        logos.delete(db, voucher_id)
    return _redirect_detail(voucher_id)


@router.post("/vouchers/{voucher_id}/logo/search")
def search_logo(request: Request, voucher_id: int, csrf_token: str = Form(""),
                ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db)):
    require_csrf(request, csrf_token)
    _, user = ctx
    voucher = _voucher_or_404(db, voucher_id)
    if not settings.logo_fetch_enabled:
        return _render_detail(request, db, voucher_id, status_code=400,
                              logo_error="Die Logo-Suche ist ausgeschaltet (LOGO_FETCH).")
    if not voucher["shop_url"]:
        return _render_detail(request, db, voucher_id, status_code=400,
                              logo_error="Trag zuerst die Webshop-Adresse ein.")
    key = f"logo-search:{user['id']}"
    if limiter.blocked(key, SEARCH_LIMIT, SEARCH_WINDOW):
        return _render_detail(request, db, voucher_id, status_code=429,
                              logo_error="Zu viele Suchen. Bitte warte einige Minuten.")
    limiter.add(key, SEARCH_WINDOW)
    try:
        found = logofetch.search(voucher["shop_url"])
    except logofetch.FetchError as exc:
        return _render_detail(request, db, voucher_id, status_code=502, logo_error=str(exc))
    except Exception:  # noqa: BLE001 - nie mit einer Fehlerseite enden, nie Details verraten
        logofetch.logger.error("Logo-Suche fehlgeschlagen.")
        return _render_detail(request, db, voucher_id, status_code=502,
                              logo_error="Die Suche ist fehlgeschlagen.")
    return render(request, "logo_choose.html", v=voucher, host=vouchers.shop_host(voucher["shop_url"]),
                  candidates=[logos.to_data_uri(png) for png in found])


@router.post("/vouchers/{voucher_id}/logo/choose")
def choose_logo(request: Request, voucher_id: int, image: str = Form(""), csrf_token: str = Form(""),
                ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db)):
    require_csrf(request, csrf_token)
    _voucher_or_404(db, voucher_id)
    try:
        png = logos.from_form_field(image)   # wird wie ein Upload erneut geprüft und neu erzeugt
    except logos.LogoError as exc:
        return _render_detail(request, db, voucher_id, status_code=400, logo_error=str(exc))
    with transaction(db):
        logos.save(db, voucher_id, png)
    return _redirect_detail(voucher_id)
