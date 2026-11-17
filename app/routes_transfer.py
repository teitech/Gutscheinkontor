"""Seite "Daten": Export als ZIP oder CSV, Import aus einem ZIP."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from . import dates, transfer
from .config import get_settings
from .db import get_db
from .ratelimit import limiter
from .routes_auth import require_active, require_csrf
from .templating import render

router = APIRouter()
settings = get_settings()

UPLOAD_OVERHEAD = 64 * 1024
IMPORT_WINDOW = 60 * 60
IMPORT_LIMIT = 10


def _page(request: Request, db: sqlite3.Connection, status_code: int = 200, **extra):
    counts = db.execute(
        "SELECT (SELECT COUNT(*) FROM vouchers) AS vouchers, "
        "(SELECT COUNT(*) FROM voucher_attachments) AS files"
    ).fetchone()
    return render(
        request, "data.html", status_code=status_code, voucher_count=counts["vouchers"],
        file_count=counts["files"], max_import_mb=settings.max_import_mb, **extra,
    )


def _iter_and_close(file, chunk: int = 1024 * 1024):
    try:
        while True:
            data = file.read(chunk)
            if not data:
                break
            yield data
    finally:
        file.close()


def _filename(extension: str) -> str:
    return f"gutscheinkontor-{dates.today():%Y%m%d}.{extension}"


@router.get("/data")
def data_page(request: Request, ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db)):
    return _page(request, db)


@router.post("/data/export.zip")
def export_zip(
    request: Request, include_codes: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    archive = transfer.build_export_zip(db, include_codes == "on")
    return StreamingResponse(
        _iter_and_close(archive), media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{_filename("zip")}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/data/export.csv")
def export_csv(
    request: Request, include_codes: str = Form(""), csrf_token: str = Form(""),
    ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    require_csrf(request, csrf_token)
    return Response(
        transfer.build_csv(db, include_codes == "on"), media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_filename("csv")}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/data/import")
async def import_data(
    request: Request, ctx=Depends(require_active), db: sqlite3.Connection = Depends(get_db),
):
    # Ohne File-Parameter parst FastAPI den Body nicht vorab: Login und Größe werden zuerst geprüft.
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > settings.max_import_bytes + UPLOAD_OVERHEAD:
        return _page(request, db, status_code=413,
                     error=f"Die Datei ist zu groß (höchstens {settings.max_import_mb} MB).")

    form = await request.form()
    try:
        require_csrf(request, str(form.get("csrf_token", "")))
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return _page(request, db, status_code=400, error="Bitte wähle eine Datei aus.")
        key = f"import:{ctx[1]['id']}"
        if limiter.blocked(key, IMPORT_LIMIT, IMPORT_WINDOW):
            return _page(request, db, status_code=429, error="Zu viele Importe. Bitte warte eine Weile.")
        limiter.add(key, IMPORT_WINDOW)
        try:
            report = await run_in_threadpool(
                transfer.import_upload, db, upload.file,
                skip_duplicates=form.get("skip_duplicates") == "on",
                dry_run=form.get("dry_run") == "on",
                created_by=ctx[1]["id"],
            )
        except transfer.ImportFormatError as exc:
            return _page(request, db, status_code=400, error=str(exc))
    finally:
        await form.close()
    return _page(request, db, report=report)
