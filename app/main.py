"""Gutscheinkontor: Einstiegspunkt der Web-App."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings

BASE_DIR = Path(__file__).resolve().parent
settings = get_settings()

# Keine API-Doku öffentlich ausliefern: kleinere Angriffsfläche.
app = FastAPI(
    title="Gutscheinkontor",
    version=settings.version,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Vorbereitung für Mehrsprachigkeit: Texte laufen über _(). Vorerst nur Deutsch.
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_null_translations()
templates.env.globals["settings"] = settings

# Alles kommt von der eigenen Domain. Keine externen Ressourcen (Datenschutz).
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "version": settings.version})
