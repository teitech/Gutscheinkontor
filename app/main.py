"""Gutscheinkontor: Einstiegspunkt der Web-App."""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import security
from .config import get_settings
from .db import init_db
from .routes_auth import require_active, router as auth_router
from .templating import BASE_DIR, Redirect, render

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()  # ohne gültigen SECRET_KEY startet die App nicht
    init_db()
    yield


# Keine API-Doku öffentlich ausliefern: kleinere Angriffsfläche.
app = FastAPI(
    title="Gutscheinkontor",
    version=settings.version,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(auth_router)

# Alles kommt von der eigenen Domain. Keine externen Ressourcen (Datenschutz).
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


@app.exception_handler(Redirect)
async def redirect_handler(request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


@app.middleware("http")
async def csrf_and_security_headers(request: Request, call_next):
    # CSRF: Zufalls-Token im Cookie, das Formularfeld enthält den davon abgeleiteten HMAC.
    token = request.cookies.get(settings.csrf_cookie)
    fresh = not token or len(token) < 32
    if fresh:
        token = secrets.token_urlsafe(32)
    request.state.csrf = security.csrf_field_value(token)

    response = await call_next(request)

    if fresh:
        response.set_cookie(
            settings.csrf_cookie, token, httponly=True,
            secure=settings.secure_cookies, samesite="lax", path="/",
        )
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.get("/")
def index(request: Request, ctx=Depends(require_active)):
    return render(request, "index.html")


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "version": settings.version})
