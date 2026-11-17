import re
import sqlite3

import pyotp

from app.config import get_settings
from tests.conftest import EMAIL, PASSWORD, csrf_from, do_login_password, do_setup


def test_einrichtung_bis_dashboard(client, clock):
    do_setup(client, clock)
    r = client.get("/")
    assert r.status_code == 200
    assert "Hallo Anna" in r.text


def test_einrichtung_nur_einmal(client, clock):
    do_setup(client, clock)
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/").text)})
    assert client.get("/setup").headers["location"] == "/login"
    token = csrf_from(client.get("/login").text)
    r = client.post("/setup", data={
        "csrf_token": token, "display_name": "Mallory", "email": "m@example.de",
        "password": PASSWORD, "password_repeat": PASSWORD,
    })
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_passwortregeln(client):
    token = csrf_from(client.get("/setup").text)
    r = client.post("/setup", data={
        "csrf_token": token, "display_name": "Anna", "email": EMAIL,
        "password": "kurz", "password_repeat": "kurz",
    })
    assert r.status_code == 400
    assert "mindestens 12 Zeichen" in r.text


def test_login_braucht_zweiten_faktor(client, clock):
    secret, _ = do_setup(client, clock)
    client.cookies.clear()

    r = do_login_password(client)
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa"
    # Mit nur dem Passwort kommt man nicht auf das Dashboard.
    assert client.get("/").status_code == 303

    page = client.get("/login/2fa")
    token = csrf_from(page.text)
    r = client.post("/login/2fa", data={"csrf_token": token, "code": "000000"})
    assert r.status_code == 401

    code = pyotp.TOTP(secret).at(clock.t)
    r = client.post("/login/2fa", data={"csrf_token": token, "code": code})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_totp_code_nur_einmal_gueltig(client, clock):
    secret, _ = do_setup(client, clock)
    client.cookies.clear()
    do_login_password(client)
    token = csrf_from(client.get("/login/2fa").text)
    code = pyotp.TOTP(secret).at(clock.t)
    assert client.post("/login/2fa", data={"csrf_token": token, "code": code}).status_code == 303

    # Zweiter Login im selben Zeitfenster mit demselben Code scheitert (Replay-Schutz).
    client.cookies.clear()
    do_login_password(client)
    token = csrf_from(client.get("/login/2fa").text)
    r = client.post("/login/2fa", data={"csrf_token": token, "code": code})
    assert r.status_code == 401


def test_wiederherstellungscode_nur_einmal(client, clock):
    _, codes = do_setup(client, clock)
    client.cookies.clear()

    do_login_password(client)
    token = csrf_from(client.get("/login/2fa").text)
    r = client.post("/login/2fa", data={"csrf_token": token, "code": codes[0]})
    assert r.status_code == 303 and r.headers["location"] == "/"

    client.cookies.clear()
    do_login_password(client)
    token = csrf_from(client.get("/login/2fa").text)
    r = client.post("/login/2fa", data={"csrf_token": token, "code": codes[0]})
    assert r.status_code == 401
    r = client.post("/login/2fa", data={"csrf_token": token, "code": codes[1]})
    assert r.status_code == 303


def test_2fa_versuche_begrenzt(client, clock):
    do_setup(client, clock)
    client.cookies.clear()
    do_login_password(client)
    token = csrf_from(client.get("/login/2fa").text)
    for _ in range(5):
        assert client.post("/login/2fa", data={"csrf_token": token, "code": "000000"}).status_code == 401
    # Danach ist die Sitzung gesperrt, es geht zurück zum Login.
    r = client.post("/login/2fa", data={"csrf_token": token, "code": "000000"})
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_falsches_passwort_und_rate_limit(client, clock):
    do_setup(client, clock)
    client.cookies.clear()
    token = csrf_from(client.get("/login").text)
    data = {"csrf_token": token, "email": EMAIL, "password": "falsches-passwort-1"}
    for _ in range(5):
        assert client.post("/login", data=data).status_code == 401
    assert client.post("/login", data=data).status_code == 429
    # Auch das richtige Passwort wird jetzt vorübergehend abgewiesen.
    data["password"] = PASSWORD
    assert client.post("/login", data=data).status_code == 429


def test_unbekannte_email_gleiche_meldung(client, clock):
    do_setup(client, clock)
    client.cookies.clear()
    token = csrf_from(client.get("/login").text)
    r1 = client.post("/login", data={"csrf_token": token, "email": "nobody@example.de", "password": "xxxxxxxxxxxx"})
    r2 = client.post("/login", data={"csrf_token": token, "email": EMAIL, "password": "yyyyyyyyyyyy"})
    assert r1.status_code == r2.status_code == 401
    assert "E-Mail oder Passwort ist falsch." in r1.text and "E-Mail oder Passwort ist falsch." in r2.text


def test_csrf_pflicht(client, clock):
    do_setup(client, clock)
    client.cookies.clear()
    client.get("/login")
    r = client.post("/login", data={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 403
    r = client.post("/login", data={"csrf_token": "falsch", "email": EMAIL, "password": PASSWORD})
    assert r.status_code == 403


def test_fremder_origin_wird_abgelehnt(client, clock):
    do_setup(client, clock)
    client.cookies.clear()
    token = csrf_from(client.get("/login").text)
    r = client.post("/login", data={"csrf_token": token, "email": EMAIL, "password": PASSWORD},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_logout_beendet_sitzung(client, clock):
    do_setup(client, clock)
    token = csrf_from(client.get("/").text)
    assert client.post("/logout", data={"csrf_token": token}).status_code == 303
    assert client.get("/").status_code == 303


def test_cookies_haben_schutz_flags(client, clock):
    token = csrf_from(client.get("/setup").text)
    r = client.post("/setup", data={
        "csrf_token": token, "display_name": "Anna", "email": EMAIL,
        "password": PASSWORD, "password_repeat": PASSWORD,
    })
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_geheimnisse_nicht_im_klartext_in_der_db(client, clock):
    secret, codes = do_setup(client, clock)
    db = sqlite3.connect(get_settings().db_path)
    row = db.execute("SELECT password_hash, totp_secret_enc FROM users").fetchone()
    assert row[0].startswith("$argon2id$")
    assert secret not in row[1]
    dump = " ".join(str(r) for r in db.execute("SELECT * FROM recovery_codes"))
    assert not any(code.replace("-", "") in dump for code in codes)
    sessions = " ".join(str(r) for r in db.execute("SELECT * FROM sessions"))
    assert client.cookies.get("gk_session") not in sessions


def test_qr_code_ohne_inline_style(client):
    # Die CSP verbietet style-Attribute. Der QR-Code darf keine enthalten.
    token = csrf_from(client.get("/setup").text)
    client.post("/setup", data={
        "csrf_token": token, "display_name": "Anna", "email": EMAIL,
        "password": PASSWORD, "password_repeat": PASSWORD,
    })
    page = client.get("/2fa/setup").text
    svg = re.search(r"<svg.*?</svg>", page, re.S).group(0)
    assert "style=" not in svg and "<style" not in svg
