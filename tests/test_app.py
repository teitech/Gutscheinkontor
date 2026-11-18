def test_startseite_leitet_zur_einrichtung(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/login").headers["location"] == "/setup"


def test_seite_zeigt_hinweis_und_version(client):
    r = client.get("/setup")
    assert r.status_code == 200
    assert "Nutzung auf eigene Gefahr" in r.text
    assert "Version" in r.text


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_security_header_gesetzt(client):
    r = client.get("/setup")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_api_doku_ist_abgeschaltet(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_keine_externen_ressourcen(client):
    r = client.get("/setup")
    assert "http://" not in r.text.replace("http://localhost", "")
    assert "cdn" not in r.text.lower()


def test_logo_und_symbole_werden_ausgeliefert(client):
    page = client.get("/setup").text
    assert '/static/logo.png' in page and 'rel="apple-touch-icon"' in page
    for name, kind in [("logo.png", "image/png"), ("favicon.ico", "image/"), ("icon-192.png", "image/png"),
                       ("apple-touch-icon.png", "image/png")]:
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind) and len(r.content) > 500
