"""Rauchtest gegen eine laufende Instanz, z. B. den Docker-Container in der CI.

Aufruf:  python scripts/smoke_test.py http://127.0.0.1:8000

Die Instanz muss frisch sein (noch kein Konto) und dem Absender dieses Tests als Proxy
vertrauen (FORWARDED_ALLOW_IPS passend zum Docker-Netz bzw. 127.0.0.1 lokal). Der Test geht
den echten Weg über HTTP: Ersteinrichtung, 2FA, Gutschein, Anhang, Export, Import, Backup und die
Begrenzung von Anmeldeversuchen pro Besucher-Adresse hinter einem Proxy.
"""
from __future__ import annotations

import io
import re
import sys
import zipfile

import httpx
import pyotp
from PIL import Image

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASSWORD = "rauchtest-passwort-12345"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FEHLER: {message}")
        sys.exit(1)
    print(f"ok: {message}")


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if match is None:
        print("FEHLER: Das Formular enthält kein CSRF-Token")
        sys.exit(1)
    return match.group(1)


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=60, follow_redirects=False)

    # --- Gesundheit und Header -------------------------------------------------------------
    r = client.get("/health")
    check(r.status_code == 200 and r.json()["status"] == "ok", "/health antwortet")
    check("server" not in r.headers, "Der Server-Header verrät keine Software")
    page = client.get("/setup")
    check(page.status_code == 200, "Ersteinrichtung ist erreichbar")
    check("default-src 'self'" in page.headers.get("content-security-policy", ""), "CSP ist gesetzt")
    check(page.headers.get("cache-control") == "no-store", "Formularseiten werden nicht zwischengespeichert")

    # Tokens in Pfaden. Die CI prüft danach, dass sie nirgends im Container-Log stehen.
    check(client.get("/password-reset/SMOKETOKEN-RESET-1").status_code == 400, "Ungültiger Reset-Link wird abgelehnt")
    check(client.get("/invite/SMOKETOKEN-INVITE-1").status_code == 400, "Ungültige Einladung wird abgelehnt")

    # --- Ersteinrichtung mit 2FA --------------------------------------------------------------
    r = client.post("/setup", data={
        "csrf_token": csrf_of(page.text), "display_name": "Rauchtest",
        "email": "rauchtest@example.de", "password": PASSWORD, "password_repeat": PASSWORD,
    })
    check(r.status_code == 303 and r.headers["location"] == "/2fa/setup", "Konto wird angelegt")
    setup_page = client.get("/2fa/setup")
    secret = re.search(r'id="totp-secret">([^<]+)<', setup_page.text).group(1).replace(" ", "")
    r = client.post("/2fa/setup", data={"csrf_token": csrf_of(setup_page.text), "code": pyotp.TOTP(secret).now()})
    check(r.status_code == 200 and len(re.findall(r"<code>[a-z0-9]{5}-[a-z0-9]{5}</code>", r.text)) == 10,
          "2FA ist eingerichtet, 10 Wiederherstellungscodes erscheinen")
    check(client.get("/").status_code == 200, "Nach der 2FA ist man angemeldet")
    cookie = r.headers.get("set-cookie", "").lower()
    check("httponly" in cookie and "samesite=lax" in cookie, "Sitzungs-Cookie ist HttpOnly und SameSite=Lax")

    def csrf() -> str:
        return csrf_of(client.get("/vouchers/new").text)

    # --- Gutschein mit Anhang ----------------------------------------------------------------------
    r = client.post("/vouchers", data={
        "csrf_token": csrf(), "title": "Rauchtest-Gutschein", "code": "SMOKE-CODE-42", "value": "25",
        "expires_on": "2099-12-31", "tags": "test", "owner": "1",
    })
    check(r.status_code == 303, "Gutschein wird angelegt")
    detail_url = r.headers["location"]
    check("SMOKE-CODE-42" in client.get(detail_url).text, "Code wird angezeigt")
    r = client.post(f"{detail_url}/attachments", data={"csrf_token": csrf()},
                    files={"file": ("scan.png", PNG, "image/png")})
    check(r.status_code == 303, "Anhang wird hochgeladen")
    r = client.get("/attachments/1")
    check(r.content == PNG and r.headers["content-type"] == "image/png", "Anhang wird unverändert ausgeliefert")
    check(r.headers.get("content-security-policy") == "default-src 'none'; sandbox", "Anhang hat Sandbox-Richtlinie")
    r = client.post(f"{detail_url}/attachments", data={"csrf_token": csrf()},
                    files={"file": ("evil.png", b"#!/bin/sh\necho hi\n", "image/png")})
    check(r.status_code == 400, "Ein Skript als .png wird abgelehnt")

    # --- Logo -------------------------------------------------------------------------------------------
    buffer = io.BytesIO()
    Image.new("RGB", (300, 150), (10, 90, 90)).save(buffer, "PNG")
    r = client.post(f"{detail_url}/logo", data={"csrf_token": csrf()}, files={"file": ("logo.png", buffer.getvalue(), "image/png")})
    check(r.status_code == 303, "Logo wird hochgeladen")
    r = client.get(f"{detail_url}/logo")
    check(r.status_code == 200 and r.headers["content-type"] == "image/png"
          and Image.open(io.BytesIO(r.content)).size == (128, 64), "Logo wird verkleinert ausgeliefert")
    r = client.get(f"{detail_url}/pdf")
    check(r.status_code == 200 and r.content.startswith(b"%PDF"), "Gutschein-PDF wird erzeugt")
    r = client.post(f"{detail_url}/logo", data={"csrf_token": csrf()}, files={"file": ("evil.png", b"#!/bin/sh\n", "image/png")})
    check(r.status_code == 400, "Ein Skript als Logo wird abgelehnt")

    # --- Export und Import ----------------------------------------------------------------------------
    r = client.post("/data/export.zip", data={"csrf_token": csrf(), "include_codes": "on"})
    check(r.status_code == 200 and r.headers["content-type"] == "application/zip", "ZIP-Export wird geliefert")
    export = r.content
    with zipfile.ZipFile(io.BytesIO(export)) as archive:
        check(archive.testzip() is None and "export.json" in archive.namelist(), "Export-ZIP ist gültig")
        check(b"SMOKE-CODE-42" in archive.read("export.json"), "Export enthält den Gutschein")
        check(any(n.startswith("logos/") for n in archive.namelist()), "Export enthält das Logo")
    r = client.post("/data/export.csv", data={"csrf_token": csrf()})
    check(r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf"), "CSV-Export mit BOM")

    check(client.post(f"{detail_url}/delete", data={"csrf_token": csrf()}).status_code == 303, "Gutschein wird gelöscht")
    r = client.post("/data/import", data={"csrf_token": csrf(), "dry_run": "on", "skip_duplicates": "on"},
                    files={"file": ("export.zip", export, "application/zip")})
    check(r.status_code == 200 and "Probelauf" in r.text, "Import-Probelauf funktioniert")
    check("Rauchtest-Gutschein" not in client.get("/?status=alle").text, "Probelauf hat nichts gespeichert")
    r = client.post("/data/import", data={"csrf_token": csrf(), "skip_duplicates": "on"},
                    files={"file": ("export.zip", export, "application/zip")})
    check(r.status_code == 200 and "Import abgeschlossen" in r.text, "Import funktioniert")
    check("Rauchtest-Gutschein" in client.get("/").text, "Gutschein ist nach dem Import wieder da")

    # --- Backups ------------------------------------------------------------------------------------------
    r = client.post("/backups/create", data={"csrf_token": csrf()})
    check(r.status_code == 303, "Backup wird per Knopf erstellt")
    names = re.findall(r"/backups/(gutscheinkontor-backup-[0-9_-]+\.zip)", client.get("/data").text)
    check(len(names) >= 2, "Automatisches Backup vom Start und manuelles Backup sind aufgelistet")
    r = client.get(f"/backups/{names[0]}")
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        check(archive.testzip() is None and "gutscheinkontor.db" in archive.namelist(), "Backup lässt sich herunterladen und öffnen")

    # --- Besucher-Adresse hinter dem Proxy -----------------------------------------------------------------
    # Ein Proxy hängt die Adresse des Besuchers rechts an X-Forwarded-For an. Alles links davon stammt vom
    # Besucher selbst und darf die Begrenzung nicht aushebeln.
    visitor = httpx.Client(base_url=BASE, timeout=60)

    def login_attempt(forwarded_for: str, n: int) -> int:
        headers = {"X-Forwarded-For": forwarded_for}
        token = csrf_of(visitor.get("/login", headers=headers).text)
        return visitor.post("/login", headers=headers, data={
            "csrf_token": token, "email": f"niemand{n}@example.de", "password": "falsches-passwort-123",
        }).status_code

    codes = [login_attempt(f"10.9.9.{n}, 198.51.100.20", n) for n in range(21)]  # links gefälscht, rechts echt
    check(codes[:20] == [401] * 20 and codes[20] == 429,
          "Nach 20 Fehlversuchen wird eine Adresse gebremst, auch wenn der Besucher links Adressen erfindet")
    check(login_attempt("198.51.100.21", 99) == 401,
          "Eine andere Besucher-Adresse ist nicht betroffen (X-Forwarded-For wird beachtet)")

    print("Alle Rauchtests bestanden.")


if __name__ == "__main__":
    main()
