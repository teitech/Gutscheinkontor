import re
import sqlite3
from datetime import date

import pytest

from app import auth, dates, mail, routes_users
from app.config import get_settings
from app.db import connect
from tests.conftest import (EMAIL, PASSWORD, csrf_from, csrf_value, do_login_password, do_setup,
                            finish_2fa_setup)
from tests.test_vouchers import create_id, db_rows, post

NEW_EMAIL = "ben@example.de"
NEW_PASSWORD = "ben-hat-ein-langes-passwort-9"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))


@pytest.fixture
def admin(client, clock):
    do_setup(client, clock)
    return client


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "is_enabled", lambda: True)
    monkeypatch.setattr(mail, "send_mail", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


def invite(client, email=NEW_EMAIL, send_mail=True):
    data = {"csrf_token": csrf_value(client), "email": email}
    if send_mail:
        data["send_mail"] = "on"
    return client.post("/users/invite", data=data)


def link_from(response) -> str:
    return re.search(r'id="invite-link">([^<]+)<', response.text).group(1)


def token_from(link: str) -> str:
    return link.rsplit("/", 1)[1]


def accept(client, token, name="Ben", password=NEW_PASSWORD, repeat=None, **extra):
    data = {"csrf_token": csrf_value(client), "display_name": name, "password": password,
            "password_repeat": password if repeat is None else repeat, **extra}
    return client.post(f"/invite/{token}", data=data)


def user_row(email):
    rows = db_rows("SELECT * FROM users WHERE email = ?", (email,))
    return rows[0] if rows else None


def invited_member(client, clock, email=NEW_EMAIL):
    """Der Administrator lädt ein, die Person nimmt an und richtet 2FA ein. Der Client ist danach
    als neues Mitglied angemeldet."""
    token = token_from(link_from(invite(client, email)))
    client.cookies.clear()
    r = accept(client, token)
    assert r.status_code == 303 and r.headers["location"] == "/2fa/setup"
    finish_2fa_setup(client, clock)
    return token


# --- Einladung erstellen ---------------------------------------------------------------------


def test_einladung_zeigt_link_einmalig(admin):
    r = invite(admin)
    assert r.status_code == 200 and "Einladung für ben@example.de erstellt" in r.text
    link = link_from(r)
    assert link.startswith(get_settings().base_url + "/invite/")
    token = token_from(link)
    assert len(token) >= 40
    # Der Link steht nirgends sonst: nicht in der Datenbank, nicht auf der nächsten Seitenansicht.
    conn = sqlite3.connect(get_settings().db_path)
    assert token not in " ".join(str(row) for row in conn.execute("SELECT * FROM invitations"))
    conn.close()
    page = admin.get("/users")
    assert token not in page.text and "ben@example.de" in page.text and "Offene Einladungen" in page.text


def test_einladung_per_mail(admin, outbox):
    r = invite(admin)
    assert "per E-Mail verschickt" in r.text
    to, subject, body = outbox[0]
    assert to == NEW_EMAIL and subject == "Einladung zum Gutscheinkontor"
    assert link_from(r) in body and "Anna" in body and "7 Tage" in body


def test_einladung_ohne_mail_wenn_nicht_gewuenscht_oder_kein_server(admin, outbox, monkeypatch):
    invite(admin, "a@example.de", send_mail=False)
    monkeypatch.setattr(mail, "is_enabled", lambda: False)
    r = invite(admin, "b@example.de")
    assert outbox == [] and "nicht eingerichtet" in r.text and "Link" in r.text


def test_fehlgeschlagene_mail_wird_gemeldet(admin, monkeypatch):
    monkeypatch.setattr(mail, "is_enabled", lambda: True)
    monkeypatch.setattr(mail, "send_mail", lambda *a: False)
    r = invite(admin)
    assert "nicht verschickt werden" in r.text and link_from(r)


def test_einladung_fehler(admin):
    assert invite(admin, "keine-adresse").status_code == 400
    assert invite(admin, "x" * 250 + "@example.de").status_code == 400
    r = invite(admin, EMAIL.upper())  # gibt es schon, Groß-/Kleinschreibung egal
    assert r.status_code == 400 and "schon ein Konto" in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM invitations")[0]["n"] == 0


def test_neue_einladung_ersetzt_die_alte(admin):
    first = token_from(link_from(invite(admin)))
    second = token_from(link_from(invite(admin)))
    assert first != second and db_rows("SELECT COUNT(*) AS n FROM invitations")[0]["n"] == 1
    assert admin.get(f"/invite/{first}").status_code == 400
    assert admin.get(f"/invite/{second}").status_code == 200


def test_grenzen(admin, monkeypatch):
    monkeypatch.setattr(auth, "MAX_PENDING_INVITATIONS", 2)
    invite(admin, "a@example.de"), invite(admin, "b@example.de")
    r = invite(admin, "c@example.de")
    assert r.status_code == 400 and "höchstens 2 Einladungen" in r.text
    assert invite(admin, "a@example.de").status_code == 200   # dieselbe Adresse ersetzt, zählt nicht doppelt
    monkeypatch.setattr(routes_users, "MAX_USERS", 1)
    assert "höchstens 1 Konten" in invite(admin, "d@example.de").text


def test_einladungen_sind_begrenzt(admin):
    for i in range(routes_users.INVITE_LIMIT):
        assert invite(admin, f"p{i}@example.de").status_code in (200, 400)
    assert invite(admin, "zuviel@example.de").status_code == 429


def test_einladung_zurueckziehen(admin):
    token = token_from(link_from(invite(admin)))
    invitation_id = db_rows("SELECT id FROM invitations")[0]["id"]
    assert post(admin, f"/users/invitations/{invitation_id}/revoke").status_code == 303
    assert admin.get(f"/invite/{token}").status_code == 400
    assert post(admin, f"/users/invitations/{invitation_id}/revoke").status_code == 404


def test_abgelaufene_einladung(admin, clock):
    token = token_from(link_from(invite(admin)))
    clock.t += auth.INVITE_TTL + 1
    assert admin.get(f"/invite/{token}").status_code == 400
    assert accept(admin, token).status_code == 400
    assert "Offene Einladungen" not in admin.get("/users").text
    assert user_row(NEW_EMAIL) is None


# --- Einladung annehmen -------------------------------------------------------------------------


def test_annehmen_bis_zum_fertigen_konto(admin, clock):
    invited_member(admin, clock)
    me = user_row(NEW_EMAIL)
    assert me["display_name"] == "Ben" and me["is_admin"] == 0 and me["is_active"] == 1
    assert me["totp_enabled"] == 1 and me["reminders_enabled"] == 1
    assert security_ok(me["password_hash"])
    assert admin.get("/").status_code == 200                    # als Ben angemeldet
    assert "Ben" in admin.get("/vouchers/new").text             # Person erscheint in der Auswahl
    assert db_rows("SELECT COUNT(*) AS n FROM invitations")[0]["n"] == 0


def security_ok(password_hash):
    from app import security
    return security.verify_password(password_hash, NEW_PASSWORD)


def test_formular_zeigt_feste_email(admin):
    token = token_from(link_from(invite(admin)))
    admin.cookies.clear()
    page = admin.get(f"/invite/{token}")
    assert page.status_code == 200 and f'value="{NEW_EMAIL}" readonly' in page.text


def test_email_laesst_sich_nicht_aendern(admin):
    token = token_from(link_from(invite(admin)))
    admin.cookies.clear()
    accept(admin, token, email="admin-konto@example.de", display_name_extra="x")
    assert user_row(NEW_EMAIL) is not None and user_row("admin-konto@example.de") is None


def test_link_nur_einmal_nutzbar(admin):
    token = token_from(link_from(invite(admin)))
    admin.cookies.clear()
    assert accept(admin, token).status_code == 303
    admin.cookies.clear()
    assert admin.get(f"/invite/{token}").status_code == 400
    assert accept(admin, token, name="Zweiter", password="noch-ein-anderes-pw-77").status_code == 400
    assert db_rows("SELECT COUNT(*) AS n FROM users")[0]["n"] == 2


def test_falsche_eingaben_lassen_den_link_gueltig(admin):
    token = token_from(link_from(invite(admin)))
    admin.cookies.clear()
    assert accept(admin, token, password="kurz").status_code == 400
    assert accept(admin, token, repeat="etwas-anderes-langes-pw").status_code == 400
    assert accept(admin, token, name="  ").status_code == 400
    assert accept(admin, token, name="x" * 101).status_code == 400
    assert user_row(NEW_EMAIL) is None
    assert accept(admin, token).status_code == 303


def test_neues_konto_braucht_zwei_faktor(admin, clock):
    token = token_from(link_from(invite(admin)))
    admin.cookies.clear()
    accept(admin, token)
    # Nach dem Anlegen ist man noch nicht angemeldet, bevor die 2FA eingerichtet ist.
    assert admin.get("/").status_code == 303
    assert admin.get("/2fa/setup").status_code == 200


def test_konto_existiert_inzwischen(admin):
    token = token_from(link_from(invite(admin)))
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("INSERT INTO users (email, display_name, password_hash) VALUES (?, 'Schneller', 'x')", (NEW_EMAIL,))
    conn.commit()
    conn.close()
    admin.cookies.clear()
    r = accept(admin, token)
    assert r.status_code == 400 and "schon ein Konto" in r.text


def test_ungueltige_links_sind_begrenzt(admin):
    admin.cookies.clear()
    for _ in range(routes_users.TOKEN_LIMIT_PER_IP):
        assert admin.get("/invite/falsch").status_code == 400
    assert admin.get("/invite/falsch").status_code == 429


def test_annehmen_braucht_csrf(admin):
    token = token_from(link_from(invite(admin)))
    admin.cookies.clear()
    admin.get(f"/invite/{token}")
    r = admin.post(f"/invite/{token}", data={"display_name": "Ben", "password": NEW_PASSWORD,
                                             "password_repeat": NEW_PASSWORD})
    assert r.status_code == 403 and user_row(NEW_EMAIL) is None


def test_angemeldeter_nimmt_keine_alte_sitzung_mit(admin):
    token = token_from(link_from(invite(admin)))
    old_sessions = db_rows("SELECT COUNT(*) AS n FROM sessions WHERE stage = 'active'")[0]["n"]
    assert accept(admin, token).status_code == 303   # Cookie des Admins noch gesetzt
    assert db_rows("SELECT COUNT(*) AS n FROM sessions WHERE stage = 'active'")[0]["n"] == old_sessions - 1


# --- Mitglieder haben keine Admin-Rechte -----------------------------------------------------------


def test_mitglied_ist_kein_admin(admin, clock):
    invited_member(admin, clock)
    for method, url in [("get", "/users"), ("get", "/users/1/delete"), ("post", "/users/invite"),
                        ("post", "/users/1/disable"), ("post", "/users/1/make-admin"),
                        ("post", "/users/1/delete"), ("get", "/backups/x"), ("post", "/backups/create")]:
        r = getattr(admin, method)(url) if method == "get" else admin.post(url, data={"csrf_token": csrf_value(admin)})
        assert r.status_code in (403, 404), url
    assert "Nutzer einladen" not in admin.get("/settings").text
    assert admin.get("/users").status_code == 403 and "Administratoren" in admin.get("/users").text
    assert user_row(EMAIL)["is_active"] == 1 and user_row(EMAIL)["is_admin"] == 1


def test_mitglied_sieht_und_bearbeitet_gemeinsamen_bestand(admin, clock):
    vid = create_id(admin, title="Annas Gutschein", value="20")
    invited_member(admin, clock)
    assert "Annas Gutschein" in admin.get("/").text
    assert post(admin, f"/vouchers/{vid}/redeem", amount="5").status_code == 303
    assert db_rows("SELECT remaining_cents FROM vouchers")[0]["remaining_cents"] == 1500


def test_verwaltung_ohne_login_und_csrf(admin):
    token = token_from(link_from(invite(admin)))
    assert admin.post("/users/invite", data={"email": "x@example.de"}).status_code == 403
    assert admin.post("/users/1/disable").status_code == 403
    assert admin.post("/users/1/delete").status_code == 403
    admin.cookies.clear()
    for method, url in [("get", "/users"), ("post", "/users/invite"), ("post", "/users/2/disable"),
                        ("get", "/users/2/delete")]:
        r = getattr(admin, method)(url)
        assert r.status_code == 303 and r.headers["location"] == "/login", url
    assert token


# --- Verwaltung -------------------------------------------------------------------------------------------


def make_second_admin(client, clock):
    invited_member(client, clock)
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("UPDATE users SET is_admin = 1 WHERE email = ?", (NEW_EMAIL,))
    conn.commit()
    conn.close()


def login_as(client, clock, email, password):
    """Meldet einen vorhandenen Nutzer neu an, ohne 2FA-Code (Sitzung direkt anlegen)."""
    conn = connect()
    uid = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
    token = auth.create_session(conn, uid, "active")
    conn.close()
    client.cookies.clear()
    client.cookies.set("gk_session", token)


def test_nutzerseite_listet_nutzer(admin, clock):
    invited_member(admin, clock)
    login_as(admin, clock, EMAIL, PASSWORD)
    page = admin.get("/users").text
    assert "Anna" in page and "Ben" in page and "Administrator" in page and "Du" in page
    assert page.count("Sperren") >= 1 and "Löschen" in page


def test_sperren_beendet_sitzungen_und_verhindert_login(admin, clock):
    invited_member(admin, clock)                      # Ben ist angemeldet
    ben = user_row(NEW_EMAIL)
    assert db_rows("SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (ben["id"],))[0]["n"] >= 1
    login_as(admin, clock, EMAIL, PASSWORD)
    assert post(admin, f"/users/{ben['id']}/disable").status_code == 303
    assert user_row(NEW_EMAIL)["is_active"] == 0
    assert db_rows("SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (ben["id"],))[0]["n"] == 0
    assert "Gesperrt" in admin.get("/users").text

    admin.cookies.clear()
    token = csrf_from(admin.get("/login").text)
    r = admin.post("/login", data={"csrf_token": token, "email": NEW_EMAIL, "password": NEW_PASSWORD})
    assert r.status_code == 401

    login_as(admin, clock, EMAIL, PASSWORD)
    assert post(admin, f"/users/{ben['id']}/enable").status_code == 303
    assert user_row(NEW_EMAIL)["is_active"] == 1
    admin.cookies.clear()
    token = csrf_from(admin.get("/login").text)
    r = admin.post("/login", data={"csrf_token": token, "email": NEW_EMAIL, "password": NEW_PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa"


def test_gesperrter_kann_nichts_mehr_und_bekommt_keine_erinnerung(admin, clock):
    invited_member(admin, clock)
    ben = user_row(NEW_EMAIL)
    ben_cookie = admin.cookies.get("gk_session")
    login_as(admin, clock, EMAIL, PASSWORD)
    post(admin, f"/users/{ben['id']}/disable")
    admin.cookies.clear()
    admin.cookies.set("gk_session", ben_cookie)       # alter Cookie funktioniert nicht mehr
    assert admin.get("/").status_code == 303
    from app import reminders
    login_as(admin, clock, EMAIL, PASSWORD)
    conn = connect()
    users = reminders.pending(conn, date(2026, 11, 17), (30, 7, 1))
    conn.close()
    assert ben["id"] not in users


def test_admin_rechte_vergeben_und_entziehen(admin, clock):
    invited_member(admin, clock)
    ben = user_row(NEW_EMAIL)
    login_as(admin, clock, EMAIL, PASSWORD)
    assert post(admin, f"/users/{ben['id']}/make-admin").status_code == 303
    assert user_row(NEW_EMAIL)["is_admin"] == 1
    assert post(admin, f"/users/{ben['id']}/remove-admin").status_code == 303
    assert user_row(NEW_EMAIL)["is_admin"] == 0


def test_schutz_vor_selbstaussperrung(admin):
    me = user_row(EMAIL)
    for action in ("disable", "remove-admin"):
        r = post(admin, f"/users/{me['id']}/{action}")
        assert r.status_code == 400 and "eigenen Konto" in r.text
    assert post(admin, f"/users/{me['id']}/delete").status_code == 400
    assert user_row(EMAIL)["is_active"] == 1 and user_row(EMAIL)["is_admin"] == 1


def test_admins_verwalten_sich_gegenseitig_aber_nie_alle_zugleich(admin, clock):
    make_second_admin(admin, clock)
    anna, ben = user_row(EMAIL), user_row(NEW_EMAIL)
    login_as(admin, clock, NEW_EMAIL, NEW_PASSWORD)
    assert post(admin, f"/users/{anna['id']}/disable").status_code == 303   # Ben sperrt Anna
    # Ben ist jetzt der einzige aktive Admin und kann sich weder sperren noch Rechte entziehen.
    assert post(admin, f"/users/{ben['id']}/disable").status_code == 400
    assert post(admin, f"/users/{ben['id']}/remove-admin").status_code == 400
    assert db_rows("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1 AND is_active = 1")[0]["n"] == 1


def test_schutzregel_letzter_aktiver_admin_unabhaengig_vom_ausfuehrenden(admin):
    """Zusatzschutz: Selbst ein anderer Ausführender darf den einzigen aktiven Admin nicht entfernen."""
    only_admin = db_rows("SELECT * FROM users WHERE email = ?", (EMAIL,))[0]
    conn = connect()
    with pytest.raises(routes_users.HTTPException) as caught:
        routes_users._check_may_remove(conn, actor_id=9999, target=only_admin, what="Sperren")
    conn.close()
    assert caught.value.status_code == 400 and "mindestens ein aktiver Administrator" in caught.value.detail


def test_regel_letzter_admin_direkt(admin):
    """Ein zweiter, bereits gesperrter Admin zählt nicht als Reserve."""
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute("INSERT INTO users (email, display_name, password_hash, is_admin, is_active) "
                 "VALUES ('weg@example.de', 'Weg', 'x', 1, 0)")
    conn.commit()
    conn.close()
    other = user_row("weg@example.de")
    # Der gesperrte Admin darf gelöscht werden, der einzige aktive nicht.
    assert post(admin, f"/users/{other['id']}/delete").status_code == 303
    assert user_row("weg@example.de") is None


def test_loeschen_behaelt_gutscheine(admin, clock):
    invited_member(admin, clock)
    ben = user_row(NEW_EMAIL)
    vid = create_id(admin, title="Bens Gutschein", owner=str(ben["id"]))     # als Ben angelegt und ihm zugeordnet
    assert db_rows("SELECT created_by, owner_user_id FROM vouchers")[0]["owner_user_id"] == ben["id"]
    login_as(admin, clock, EMAIL, PASSWORD)

    page = admin.get(f"/users/{ben['id']}/delete")
    assert page.status_code == 200 and "endgültig gelöscht" in page.text and "Ben" in page.text
    assert post(admin, f"/users/{ben['id']}/delete").status_code == 303
    assert user_row(NEW_EMAIL) is None
    row = db_rows("SELECT * FROM vouchers WHERE id = ?", (vid,))[0]
    assert row["owner_user_id"] is None and row["created_by"] is None and row["title"] == "Bens Gutschein"
    assert db_rows("SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (ben["id"],))[0]["n"] == 0
    assert post(admin, f"/users/{ben['id']}/delete").status_code == 404


def test_unbekannte_aktion_und_nutzer(admin):
    assert post(admin, "/users/1/loeschen").status_code == 404
    assert post(admin, "/users/999/disable").status_code == 404
    assert admin.get("/users/999/delete").status_code == 404


def test_einstellungsseite_verlinkt_nutzerverwaltung(admin):
    assert "/users" in admin.get("/settings").text
