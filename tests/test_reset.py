import dataclasses
import re

import pytest

from app import auth, cli, mail, routes_reset
from app.config import get_settings
from tests.conftest import EMAIL, csrf_from, csrf_value, do_login_password, do_setup

NEW_PASSWORD = "ein-ganz-neues-passwort-456"


@pytest.fixture
def outbox(monkeypatch):
    """Mailversand eingeschaltet, aber statt SMTP wird in eine Liste geschrieben."""
    sent = []
    monkeypatch.setattr(mail, "is_enabled", lambda: True)
    monkeypatch.setattr(mail, "send_mail", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def request_reset(client, email=EMAIL):
    token = csrf_from(client.get("/password-reset").text)
    return client.post("/password-reset", data={"csrf_token": token, "email": email})


def link_token(outbox):
    return re.search(r"/password-reset/([A-Za-z0-9_-]+)", outbox[-1][2]).group(1)


def submit_new_password(client, token, password=NEW_PASSWORD, repeat=None):
    csrf = csrf_value(client)
    return client.post(f"/password-reset/{token}", data={
        "csrf_token": csrf, "password": password,
        "password_repeat": password if repeat is None else repeat,
    })


# --- Anforderung --------------------------------------------------------------


def test_antwort_ist_fuer_bekannte_und_unbekannte_adresse_gleich(client, clock, outbox):
    do_setup(client, clock)
    client.cookies.clear()
    known = request_reset(client)
    unknown = request_reset(client, "niemand@example.de")
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text  # identischer Inhalt, nichts verrät das Konto
    assert len(outbox) == 1
    assert outbox[0][0] == EMAIL
    assert "Passwort zurücksetzen" in outbox[0][1]


def test_mail_enthaelt_link_mit_base_url(client, clock, outbox):
    do_setup(client, clock)
    request_reset(client)
    assert get_settings().base_url + "/password-reset/" in outbox[0][2]


def test_anforderung_begrenzt(client, clock, outbox):
    do_setup(client, clock)
    for _ in range(6):
        assert request_reset(client).status_code == 200
    assert len(outbox) == routes_reset.REQUEST_LIMIT_PER_EMAIL


def test_ohne_mailserver_hinweis_und_kein_versand(client, clock, monkeypatch):
    do_setup(client, clock)
    sent = []
    monkeypatch.setattr(mail, "is_enabled", lambda: False)
    monkeypatch.setattr(mail, "send_mail", lambda *a: sent.append(a))
    page = client.get("/password-reset")
    assert "nicht eingerichtet" in page.text
    r = client.post("/password-reset", data={"csrf_token": csrf_value(client), "email": EMAIL})
    assert r.status_code == 503 and not sent


def test_anforderung_braucht_csrf(client, clock, outbox):
    do_setup(client, clock)
    client.get("/password-reset")
    r = client.post("/password-reset", data={"email": EMAIL})
    assert r.status_code == 403 and not outbox


def test_login_seite_hat_link(client, clock, outbox):
    do_setup(client, clock)
    client.cookies.clear()
    assert "/password-reset" in client.get("/login").text


# --- Zurücksetzen --------------------------------------------------------------


def test_kompletter_ablauf(client, clock, outbox):
    secret, codes = do_setup(client, clock)  # eingeloggte Sitzung bleibt bestehen
    request_reset(client)
    r = submit_new_password(client, link_token(outbox))
    assert r.status_code == 303 and r.headers["location"] == "/login?reset=1"

    # Alte Sitzung ist beendet.
    assert client.get("/").status_code == 303
    # Hinweis auf der Login-Seite.
    assert "Dein Passwort wurde geändert" in client.get("/login?reset=1").text
    # Altes Passwort geht nicht mehr, das neue schon.
    client.cookies.clear()
    assert do_login_password(client).status_code == 401
    token = csrf_from(client.get("/login").text)
    r = client.post("/login", data={"csrf_token": token, "email": EMAIL, "password": NEW_PASSWORD})
    # 2FA wurde zurückgesetzt: es geht direkt in die Neu-Einrichtung.
    assert r.status_code == 303 and r.headers["location"] == "/2fa/setup"
    new_secret = re.search(r'id="totp-secret">([^<]+)<', client.get("/2fa/setup").text).group(1)
    assert new_secret.replace(" ", "") != secret
    # Hinweis-Mail über die Änderung.
    assert any("Passwort geändert" in m[1] for m in outbox)


def test_alte_wiederherstellungscodes_sind_ungueltig(client, clock, outbox):
    _, codes = do_setup(client, clock)
    request_reset(client)
    submit_new_password(client, link_token(outbox))
    db = auth.sqlite3.connect(get_settings().db_path)
    assert db.execute("SELECT COUNT(*) FROM recovery_codes").fetchone()[0] == 0


def test_link_nur_einmal_nutzbar(client, clock, outbox):
    do_setup(client, clock)
    request_reset(client)
    token = link_token(outbox)
    assert submit_new_password(client, token).status_code == 303
    assert client.get(f"/password-reset/{token}").status_code == 400
    assert submit_new_password(client, token, "noch-ein-anderes-pw-789").status_code == 400


def test_link_laeuft_ab(client, clock, outbox):
    do_setup(client, clock)
    request_reset(client)
    token = link_token(outbox)
    clock.t += auth.RESET_TTL + 1
    assert client.get(f"/password-reset/{token}").status_code == 400


def test_neuer_link_entwertet_alten(client, clock, outbox):
    do_setup(client, clock)
    request_reset(client)
    first = link_token(outbox)
    request_reset(client)
    second = link_token(outbox)
    assert first != second
    assert client.get(f"/password-reset/{first}").status_code == 400
    assert client.get(f"/password-reset/{second}").status_code == 200


def test_ungueltiger_token(client, clock, outbox):
    do_setup(client, clock)
    assert client.get("/password-reset/gibt-es-nicht").status_code == 400


def test_schwaches_passwort_laesst_link_gueltig(client, clock, outbox):
    do_setup(client, clock)
    request_reset(client)
    token = link_token(outbox)
    assert submit_new_password(client, token, "kurz").status_code == 400
    assert submit_new_password(client, token, NEW_PASSWORD, "anderes-passwort-123").status_code == 400
    assert client.get(f"/password-reset/{token}").status_code == 200


def test_reset_braucht_csrf(client, clock, outbox):
    do_setup(client, clock)
    request_reset(client)
    token = link_token(outbox)
    client.get(f"/password-reset/{token}")
    r = client.post(f"/password-reset/{token}", data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD})
    assert r.status_code == 403


def test_token_nicht_im_klartext_in_db(client, clock, outbox):
    do_setup(client, clock)
    request_reset(client)
    token = link_token(outbox)
    db = auth.sqlite3.connect(get_settings().db_path)
    dump = " ".join(str(r) for r in db.execute("SELECT * FROM password_resets"))
    assert token not in dump


def test_token_raten_ist_begrenzt(client, clock, outbox):
    do_setup(client, clock)
    for _ in range(routes_reset.TOKEN_LIMIT_PER_IP):
        assert client.get("/password-reset/falsch").status_code == 400
    assert client.get("/password-reset/falsch").status_code == 429


# --- Kommandozeile ------------------------------------------------------------


def test_cli_erzeugt_funktionierenden_link(client, clock, outbox, capsys):
    do_setup(client, clock)
    assert cli.main(["reset-password", EMAIL]) == 0
    link = capsys.readouterr().out.splitlines()[1]
    token = link.rsplit("/", 1)[1]
    assert link.startswith(get_settings().base_url + "/password-reset/")
    assert client.get(f"/password-reset/{token}").status_code == 200


def test_cli_unbekanntes_konto(client, clock, capsys):
    do_setup(client, clock)
    assert cli.main(["reset-password", "niemand@example.de"]) == 1


# --- SMTP-Versand -------------------------------------------------------------


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.args = (host, port)
        self.calls = []
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        assert context is not None  # Zertifikatsprüfung aktiv
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.sent = msg


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)

    def configure(**overrides):
        base = dataclasses.replace(
            get_settings(), smtp_host="mail.example.de", smtp_port=587,
            smtp_user="user", smtp_password="pw", smtp_from="Gutscheinkontor <noreply@example.de>",
            smtp_security="starttls",
        )
        monkeypatch.setattr(mail, "get_settings", lambda: dataclasses.replace(base, **overrides))

    configure()
    return configure


def test_smtp_starttls_mit_login(smtp):
    assert mail.send_mail("anna@example.de", "Betreff", "Text") is True
    server = FakeSMTP.instances[0]
    assert server.args == ("mail.example.de", 587)
    assert server.calls == ["ehlo", "starttls", "ehlo", ("login", "user", "pw")]
    assert server.sent["To"] == "anna@example.de"
    assert server.sent["Subject"] == "Betreff"
    assert server.sent["From"] == "Gutscheinkontor <noreply@example.de>"
    assert server.sent["Message-ID"].endswith("@example.de>")


def test_smtp_ssl_ohne_starttls(smtp):
    smtp(smtp_security="ssl", smtp_port=465)
    mail.send_mail("anna@example.de", "Betreff", "Text")
    assert "starttls" not in FakeSMTP.instances[0].calls


def test_smtp_ohne_anmeldung(smtp):
    smtp(smtp_user="", smtp_security="none")
    mail.send_mail("anna@example.de", "Betreff", "Text")
    calls = FakeSMTP.instances[0].calls
    assert calls == ["ehlo"]


def test_smtp_fehler_wird_nicht_weitergegeben(smtp, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise OSError("Verbindung abgelehnt zu anna@example.de")

    monkeypatch.setattr(mail.smtplib, "SMTP", boom)
    assert mail.send_mail("anna@example.de", "Betreff", "Text") is False  # wirft nicht
    assert "OSError" in caplog.text
    assert "anna@example.de" not in caplog.text  # keine Adresse im Log


def test_header_injection_wird_abgewiesen(smtp):
    with pytest.raises(ValueError):
        mail.send_mail("anna@example.de\nBcc: evil@example.de", "Betreff", "Text")
