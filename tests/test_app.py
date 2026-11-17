from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_startseite_zeigt_hinweis_und_version():
    r = client.get("/")
    assert r.status_code == 200
    assert "Gutscheinkontor" in r.text
    assert "Nutzung auf eigene Gefahr" in r.text
    assert "Version" in r.text


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_security_header_gesetzt():
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_api_doku_ist_abgeschaltet():
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_keine_externen_ressourcen():
    r = client.get("/")
    assert "http://" not in r.text.replace("http://localhost", "")
    assert "cdn" not in r.text.lower()
