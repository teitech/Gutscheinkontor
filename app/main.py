"""Gutscheinkontor: Einstiegspunkt der Web-App."""
from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import reminders, security
from .config import get_settings
from .db import init_db
from .routes_auth import require_active, router as auth_router
from .routes_reset import router as reset_router
from .routes_settings import router as settings_router
from .routes_transfer import router as transfer_router
from .routes_vouchers import router as voucher_router
from .templating import BASE_DIR, Redirect, render

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()  # ohne gültigen SECRET_KEY startet die App nicht
    init_db()
    # Erinnerungen prüft ein Hintergrundlauf im selben Prozess (die App läuft mit einem Worker).
    stop = asyncio.Event()
    task = asyncio.create_task(reminders.scheduler(stop)) if settings.reminders_enabled else None
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            await task


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
app.include_router(reset_router)
app.include_router(voucher_router)
app.include_router(settings_router)
app.include_router(transfer_router)

# Alles kommt von der eigenen Domain. Keine externen Ressourcen (Datenschutz).
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


@app.exception_handler(Redirect)
async def redirect_handler(request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    # Fehlerseiten auf Deutsch statt JSON. Starlette-Standardtexte sind englisch.
    if exc.status_code == 404:
        message = "Diese Seite oder dieser Eintrag existiert nicht."
    else:
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else "Die Anfrage war nicht möglich."
        if message in ("Forbidden", "Bad Request", "Method Not Allowed"):
            message = "Die Anfrage war nicht möglich."
    return render(request, "error.html", status_code=exc.status_code, message=message)


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
    # setdefault: Anhänge setzen eine eigene, strengere Richtlinie (sandbox).
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "version": settings.version})
