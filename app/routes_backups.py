"""Backups: nur für Administratoren (sie enthalten Konten, Hashes und verschlüsselte Geheimnisse)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import backups
from .ratelimit import limiter
from .routes_auth import require_active, require_csrf
from .templating import render

router = APIRouter()

CREATE_WINDOW = 60 * 60
CREATE_LIMIT = 5


def require_admin(ctx=Depends(require_active)):
    if not ctx[1]["is_admin"]:
        raise HTTPException(status_code=403, detail="Nur Administratoren dürfen Backups verwalten.")
    return ctx


@router.post("/backups/create")
def create_backup(request: Request, csrf_token: str = Form(""), ctx=Depends(require_admin)):
    require_csrf(request, csrf_token)
    key = f"backup:{ctx[1]['id']}"
    if limiter.blocked(key, CREATE_LIMIT, CREATE_WINDOW):
        raise HTTPException(status_code=429, detail="Zu viele Backups in kurzer Zeit. Bitte warte eine Weile.")
    limiter.add(key, CREATE_WINDOW)
    try:
        backups.create_backup()
    except backups.BackupError as exc:
        return render(request, "error.html", status_code=500, message=f"Das Backup ist fehlgeschlagen: {exc}")
    return RedirectResponse("/data?backup=1", status_code=303)


@router.get("/backups/{name}")
def download_backup(name: str, ctx=Depends(require_admin)):
    path = backups.backup_path(name)  # prüft das Namensmuster: keine Pfade, keine fremden Dateien
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="application/zip", filename=name, content_disposition_type="attachment",
        headers={"Cache-Control": "no-store"},
    )
