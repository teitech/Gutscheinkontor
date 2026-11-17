import os
import re
import tempfile
import time
from pathlib import Path

# Muss vor dem Import der App gesetzt werden.
os.environ["SECRET_KEY"] = "test-secret-key-test-secret-key-1234567890"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gk-test-")
os.environ["BASE_URL"] = "http://testserver"
os.environ.pop("SETUP_TOKEN", None)
# Der Hintergrundlauf für Erinnerungen bleibt in Tests aus. Tests rufen run_once() selbst auf.
os.environ["REMINDERS_ENABLED"] = "0"
os.environ["BACKUPS_ENABLED"] = "0"  # ebenso der Backup-Lauf: Tests rufen create_backup() selbst auf

import pyotp  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.ratelimit import limiter  # noqa: E402

PASSWORD = "ein-langes-passwort-123"
EMAIL = "anna@example.de"


class Clock:
    """Steuerbare Uhr, damit mehrere TOTP-Codes nacheinander gültig sind."""

    def __init__(self):
        self.t = time.time()

    def advance_step(self):
        self.t += 30


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(auth, "_now", lambda: c.t)
    return c


@pytest.fixture
def client(clock):
    db_file = Path(get_settings().db_path)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_file) + suffix).unlink(missing_ok=True)
    limiter.reset_all()
    attachments_dir = Path(get_settings().attachments_dir)
    if attachments_dir.exists():
        for leftover in attachments_dir.iterdir():
            leftover.unlink()
    # Backups und Wiederherstellungs-Reste aus früheren Tests entfernen.
    import shutil
    data_dir = Path(get_settings().data_dir)
    shutil.rmtree(get_settings().backups_dir, ignore_errors=True)
    for leftover in data_dir.glob("restore-undo-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    for leftover in data_dir.glob(".restore-staging-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    with TestClient(app, follow_redirects=False) as c:
        yield c


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def csrf_value(client) -> str:
    """CSRF-Wert aus dem Cookie ableiten, unabhängig davon, welche Seite gerade Formulare hat."""
    from app import security

    if "gk_csrf" not in client.cookies:
        client.get("/health")
    return security.csrf_field_value(client.cookies.get("gk_csrf"))


def secret_from(html: str) -> str:
    return re.search(r'id="totp-secret">([^<]+)<', html).group(1).replace(" ", "")


def do_setup(client, clock):
    """Ersteinrichtung bis zum fertigen Login. Gibt (TOTP-Geheimnis, Codes) zurück."""
    token = csrf_from(client.get("/setup").text)
    r = client.post("/setup", data={
        "csrf_token": token, "display_name": "Anna", "email": EMAIL,
        "password": PASSWORD, "password_repeat": PASSWORD,
    })
    assert r.status_code == 303 and r.headers["location"] == "/2fa/setup"

    page = client.get("/2fa/setup")
    secret = secret_from(page.text)
    code = pyotp.TOTP(secret).at(clock.t)
    r = client.post("/2fa/setup", data={"csrf_token": csrf_from(page.text), "code": code})
    assert r.status_code == 200
    codes = re.findall(r"<code>([a-z0-9]{5}-[a-z0-9]{5})</code>", r.text)
    assert len(codes) == 10
    clock.advance_step()
    return secret, codes


def do_login_password(client):
    token = csrf_from(client.get("/login").text)
    return client.post("/login", data={"csrf_token": token, "email": EMAIL, "password": PASSWORD})
