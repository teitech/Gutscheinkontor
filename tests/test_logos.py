"""Logos: Bildverarbeitung, Upload, Auswahl, Abruf von Webseiten (mit Schutz vor internen Adressen)."""
import base64
import dataclasses
import io
import threading
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image

from app import dates, logofetch, logos, routes_logos, routes_vouchers
from app.ratelimit import limiter
from tests.conftest import csrf_value, do_setup
from tests.test_kinds import exported
from tests.test_transfer import doc, entry, import_zip, make_zip
from tests.test_vouchers import create_id, db_rows, post
from tests.test_reminders import sql


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))


@pytest.fixture
def me(client, clock):
    do_setup(client, clock)
    return client


def image_bytes(size=(200, 100), color=(200, 30, 30, 255), fmt="PNG", **kwargs):
    buffer = io.BytesIO()
    Image.new("RGBA" if fmt != "JPEG" else "RGB", size, color if fmt != "JPEG" else color[:3]).save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def upload(client, vid, content, name="logo.png", ctype="image/png"):
    return client.post(f"/vouchers/{vid}/logo", data={"csrf_token": csrf_value(client)},
                       files={"file": (name, content, ctype)})


def has_logo(vid):
    return db_rows("SELECT COUNT(*) AS n FROM voucher_logos WHERE voucher_id = ?", (vid,))[0]["n"] == 1


# --- Bildverarbeitung -----------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_formate_werden_zu_kleinem_png(fmt):
    out = logos.process(image_bytes((600, 300), fmt=fmt))
    image = Image.open(io.BytesIO(out))
    assert image.format == "PNG" and image.size == (128, 64)    # Seitenverhältnis bleibt


def test_ico_wird_gelesen():
    buffer = io.BytesIO()
    Image.new("RGBA", (64, 64), (0, 0, 255, 255)).save(buffer, format="ICO", sizes=[(64, 64)])
    assert Image.open(io.BytesIO(logos.process(buffer.getvalue()))).size == (64, 64)


def test_kleine_bilder_werden_nicht_vergroessert():
    assert Image.open(io.BytesIO(logos.process(image_bytes((40, 40))))).size == (40, 40)


def test_metadaten_werden_entfernt():
    exif = Image.Exif()
    exif[0x010E] = "GEHEIM-BESCHREIBUNG"
    out = logos.process(image_bytes((300, 300), fmt="JPEG", exif=exif))
    assert b"GEHEIM" not in out and not Image.open(io.BytesIO(out)).info.get("exif")


@pytest.mark.parametrize("data,message", [
    (b"", "leer"),
    (b"kein bild", "kein lesbares Bild"),
    (b"%PDF-1.4\n%%EOF\n", "kein lesbares Bild"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "kein lesbares Bild"),
    (image_bytes((16, 16)), "zu klein"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "kein lesbares Bild"),
    (image_bytes((300, 300))[:-40], "kein lesbares Bild"),                     # abgeschnitten
])
def test_unbrauchbare_bilder(data, message):
    with pytest.raises(logos.LogoError, match=message):
        logos.process(data)


def test_zu_grosse_datei_und_pixelbombe():
    with pytest.raises(logos.LogoError, match="zu groß"):
        logos.process(b"x" * (logos.MAX_INPUT_BYTES + 1))
    bomb = image_bytes((6000, 6000), fmt="PNG")     # winzig komprimiert, aber 36 Mio. Pixel
    assert len(bomb) < logos.MAX_INPUT_BYTES
    with pytest.raises(logos.LogoError, match="zu viele Pixel"):
        logos.process(bomb)


# --- Upload, Anzeige, Entfernen -----------------------------------------------------------------


def test_upload_anzeigen_und_entfernen(me):
    vid = create_id(me, title="Hagebau")
    assert me.get(f"/vouchers/{vid}/logo").status_code == 404
    r = upload(me, vid, image_bytes((500, 250)))
    assert r.status_code == 303 and has_logo(vid)
    shown = me.get(f"/vouchers/{vid}/logo")
    assert shown.status_code == 200 and shown.headers["content-type"] == "image/png"
    assert "private" in shown.headers["cache-control"] and Image.open(io.BytesIO(shown.content)).size == (128, 64)
    assert f"/vouchers/{vid}/logo?v=" in me.get("/").text and f"/vouchers/{vid}/logo?v=" in me.get(f"/vouchers/{vid}").text
    assert post(me, f"/vouchers/{vid}/logo/delete").status_code == 303 and not has_logo(vid)
    assert f"/vouchers/{vid}/logo?v=" not in me.get("/").text


def test_neues_logo_ersetzt_das_alte(me):
    vid = create_id(me)
    upload(me, vid, image_bytes((100, 100), (255, 0, 0, 255)))
    first = me.get(f"/vouchers/{vid}/logo").content
    upload(me, vid, image_bytes((100, 100), (0, 0, 255, 255)))
    assert me.get(f"/vouchers/{vid}/logo").content != first
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_logos")[0]["n"] == 1


@pytest.mark.parametrize("content,status,message", [
    (b"kein bild", 400, "kein lesbares Bild"),
    (b"", 400, "Bitte wähle ein Bild"),
    (b"x" * (logos.MAX_INPUT_BYTES + 100_000), 413, "zu groß"),
])
def test_upload_fehler(me, content, status, message):
    vid = create_id(me)
    r = upload(me, vid, content) if content else me.post(
        f"/vouchers/{vid}/logo", data={"csrf_token": csrf_value(me)}, files={"file": ("", b"", "image/png")})
    assert r.status_code == status and message in r.text and not has_logo(vid)


def test_upload_braucht_csrf_und_anmeldung(me, client):
    vid = create_id(me)
    r = me.post(f"/vouchers/{vid}/logo", data={"csrf_token": "falsch"}, files={"file": ("a.png", image_bytes(), "image/png")})
    assert r.status_code in (400, 403) and not has_logo(vid)
    upload(me, vid, image_bytes())
    me.post("/logout", data={"csrf_token": csrf_value(me)})
    assert me.get(f"/vouchers/{vid}/logo", follow_redirects=False).status_code in (303, 401)


def test_logo_verschwindet_mit_dem_gutschein(me):
    vid = create_id(me)
    upload(me, vid, image_bytes())
    assert post(me, f"/vouchers/{vid}/delete").status_code == 303
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_logos")[0]["n"] == 0


def data_uri(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


def test_auswahl_speichert_und_prueft_erneut(me):
    vid = create_id(me)
    good = logos.process(image_bytes((300, 300)))
    assert post(me, f"/vouchers/{vid}/logo/choose", image=data_uri(good)).status_code == 303 and has_logo(vid)
    sql("DELETE FROM voucher_logos")
    for bad in ["", "irgendwas", data_uri(b"kein bild"), "data:image/png;base64,%%%%",
                data_uri(image_bytes((16, 16))), "data:image/png;base64," + "A" * (logos.MAX_DATA_FIELD + 1),
                "http://127.0.0.1/x.png"]:
        r = post(me, f"/vouchers/{vid}/logo/choose", image=bad)
        assert r.status_code == 400 and not has_logo(vid), bad[:30]


# --- Schutz vor internen Adressen --------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://10.0.0.1/", "http://192.168.1.5/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data", "http://[::1]/",
    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://100.64.0.1/", "http://[fd00::1]/",
])
def test_interne_adressen_sind_gesperrt(url):
    with pytest.raises(logofetch.FetchError, match="nicht abgerufen werden"):
        logofetch._validated_target(url)


@pytest.mark.parametrize("url,message", [
    ("ftp://example.com/", "http"), ("file:///etc/passwd", "http"), ("javascript:alert(1)", "http"),
    ("https://user:pw@example.com/", "Zugangsdaten"), ("http://example.com:8080/", "Ports"),
    ("http://example.com:22/", "Ports"), ("https://example.com:abc/", "Port"), ("http:///x", "http"),
])
def test_unerlaubte_adressen(url, message):
    with pytest.raises(logofetch.FetchError, match=message):
        logofetch._validated_target(url)


def test_dns_auf_interne_adresse_wird_erkannt(monkeypatch):
    monkeypatch.setattr(logofetch, "_resolve", lambda host, port: ["10.0.0.5"])
    with pytest.raises(logofetch.FetchError):
        logofetch._validated_target("https://harmlos.example.de/")
    monkeypatch.setattr(logofetch, "_resolve", lambda host, port: ["93.184.216.34", "127.0.0.1"])  # eine böse reicht
    with pytest.raises(logofetch.FetchError):
        logofetch._validated_target("https://gemischt.example.de/")
    monkeypatch.setattr(logofetch, "_resolve", lambda host, port: ["93.184.216.34"])
    assert logofetch._validated_target("https://ok.example.de/a?b=1")[1:] == ("ok.example.de", 443, "93.184.216.34", "/a?b=1")


# --- Abruf von einer (lokalen) Webseite ----------------------------------------------------------------------


ICON = image_bytes((180, 180), (10, 90, 90, 255))
BIG = image_bytes((900, 600), (200, 100, 0, 255), fmt="JPEG")
TINY = image_bytes((16, 16))
PAGE = (b'<html><head>'
        b'<link rel="icon" href="/favicon-tiny.png" sizes="16x16">'
        b'<link rel="apple-touch-icon" href="/icon.png" sizes="180x180">'
        b'<link rel="icon" type="image/svg+xml" href="/logo.svg">'
        b'<meta property="og:image" content="/big.jpg">'
        b'<link rel="icon" href="//127.0.0.1:1/never.png">'
        b'</head><body>Shop</body></html>')


@pytest.fixture
def shop(monkeypatch):
    """Lokaler Webserver als 'Shop'. Die Sperre für interne Adressen wird nur für 127.0.0.1 gelockert."""
    routes = {
        "/": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE),
        "/icon.png": (200, {"Content-Type": "image/png"}, ICON),
        "/big.jpg": (200, {"Content-Type": "image/jpeg"}, BIG),
        "/favicon-tiny.png": (200, {"Content-Type": "image/png"}, TINY),
        "/logo.svg": (200, {"Content-Type": "image/svg+xml"}, b"<svg/>"),
        "/apple-touch-icon.png": (404, {}, b""),
        "/favicon.ico": (404, {}, b""),
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            status, headers, body = routes.get(self.path.split("?")[0], (404, {}, b""))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(logofetch, "ALLOWED_PORTS", {port})
    monkeypatch.setattr(logofetch, "_ip_allowed", lambda ip: ip == "127.0.0.1")
    monkeypatch.setattr(logofetch, "_resolve", lambda host, p: ["10.1.2.3"] if host == "intern.test" else ["127.0.0.1"])
    yield type("Shop", (), {"routes": routes, "port": port, "url": f"http://shop.test:{port}", "requests": requests})
    server.shutdown()


def test_suche_findet_logos_in_guter_reihenfolge(shop):
    found = logofetch.search(shop.url + "/")
    sizes = [Image.open(io.BytesIO(png)).size for png in found]
    assert sizes == [(128, 128), (128, 85)]          # Touch-Icon zuerst, dann das Vorschaubild; 16 px und SVG fehlen
    paths = [p for p, _ in shop.requests]
    assert "/logo.svg" not in paths and "/never.png" not in paths
    headers = shop.requests[0][1]
    assert headers["User-Agent"] == logofetch.USER_AGENT and headers["Accept-Encoding"] == "identity"
    assert headers["Host"] == f"shop.test:{shop.port}"
    assert not any(k.lower() in ("cookie", "authorization", "referer") for k in headers)


def test_weiterleitung_zu_internem_ziel_wird_blockiert(shop):
    shop.routes["/"] = (302, {"Location": f"http://intern.test:{shop.port}/geheim"}, b"")
    with pytest.raises(logofetch.FetchError, match="nicht abgerufen werden"):
        logofetch.search(shop.url + "/")
    assert "/geheim" not in [p for p, _ in shop.requests]


def test_weiterleitung_zu_erlaubtem_ziel_und_schleife(shop):
    shop.routes["/alt"] = (301, {"Location": "/"}, b"")
    assert logofetch.search(shop.url + "/alt")
    shop.routes["/loop"] = (302, {"Location": "/loop"}, b"")
    with pytest.raises(logofetch.FetchError, match="Weiterleitungen"):
        logofetch.search(shop.url + "/loop")


def test_grenzen_fuer_groesse_kompression_und_fehler(shop, monkeypatch):
    shop.routes["/riesig"] = (200, {"Content-Type": "text/html"}, b"x" * (logofetch.MAX_PAGE_BYTES + 10))
    with pytest.raises(logofetch.FetchError, match="zu groß"):
        logofetch.search(shop.url + "/riesig")
    shop.routes["/gzip"] = (200, {"Content-Type": "text/html", "Content-Encoding": "gzip"}, b"\x1f\x8b")
    with pytest.raises(logofetch.FetchError, match="komprimiert"):
        logofetch.search(shop.url + "/gzip")
    with pytest.raises(logofetch.FetchError, match="Fehler 404"):
        logofetch.search(shop.url + "/gibtsnicht")
    shop.routes["/icon.png"] = (200, {"Content-Type": "image/png"}, b"x" * (logofetch.MAX_IMAGE_BYTES + 10))
    assert len(logofetch.search(shop.url + "/")) == 1     # nur das Vorschaubild bleibt


def test_nichts_gefunden(shop):
    shop.routes["/"] = (200, {"Content-Type": "text/html"}, b"<html>nichts</html>")
    assert logofetch.search(shop.url + "/") == []


def test_unerreichbar(monkeypatch):
    monkeypatch.setattr(logofetch, "_ip_allowed", lambda ip: True)
    monkeypatch.setattr(logofetch, "_resolve", lambda host, p: ["127.0.0.1"])
    monkeypatch.setattr(logofetch, "ALLOWED_PORTS", {1})
    with pytest.raises(logofetch.FetchError, match="nicht erreichbar"):
        logofetch.search("http://nirgends.test:1/")


def test_adressen_auf_der_seite():
    html = ('<link rel="shortcut icon" href="favicon.ico"><link rel="apple-touch-icon" sizes="152x152" href="a.png">'
            '<link rel="apple-touch-icon" sizes="180x180" href="b.png"><meta name="twitter:image" content="https://cdn.x.de/t.jpg">'
            '<link rel="icon" href="javascript:alert(1)"><link rel="icon" href="x.svg">')
    urls = logofetch.candidate_urls("https://shop.de/pfad/seite", html)
    assert urls[:3] == ["https://shop.de/pfad/b.png", "https://shop.de/pfad/a.png", "https://shop.de/pfad/favicon.ico"]
    assert "https://cdn.x.de/t.jpg" in urls and "https://shop.de/favicon.ico" in urls
    assert not any("javascript" in u or u.endswith(".svg") for u in urls)
    assert len(urls) <= logofetch.MAX_CANDIDATES


# --- Suche in der Oberfläche ------------------------------------------------------------------------------


@pytest.fixture
def fetch_on(monkeypatch):
    on = dataclasses.replace(routes_logos.settings, logo_fetch_enabled=True)
    monkeypatch.setattr(routes_logos, "settings", on)
    monkeypatch.setattr(routes_vouchers, "settings", dataclasses.replace(routes_vouchers.settings, logo_fetch_enabled=True))
    limiter.reset_all()


def test_suche_ist_standardmaessig_aus(me):
    vid = create_id(me, shop_url="shop.example.de")
    assert "Logo von der Webseite holen" not in me.get(f"/vouchers/{vid}").text
    r = post(me, f"/vouchers/{vid}/logo/search")
    assert r.status_code == 400 and "ausgeschaltet" in r.text


def test_suche_braucht_adresse(me, fetch_on):
    vid = create_id(me)
    assert "Logo von der Webseite holen" not in me.get(f"/vouchers/{vid}").text
    assert post(me, f"/vouchers/{vid}/logo/search").status_code == 400


def test_suche_auswahl_und_speichern(me, fetch_on, shop):
    vid = create_id(me, shop_url=shop.url + "/")
    assert "Logo von der Webseite holen" in me.get(f"/vouchers/{vid}").text
    r = post(me, f"/vouchers/{vid}/logo/search")
    assert r.status_code == 200 and r.text.count('name="image"') == 2 and "Klicke auf das passende Bild" in r.text
    chosen = [c for c in r.text.split('name="image" value="')[1:]][0].split('"')[0]
    assert post(me, f"/vouchers/{vid}/logo/choose", image=chosen).status_code == 303 and has_logo(vid)
    # Kein Abruf, ohne dass der Nutzer die Suche auslöst
    before = len(shop.requests)
    me.get("/"); me.get(f"/vouchers/{vid}")
    assert len(shop.requests) == before


def test_suche_meldet_fehler_verstaendlich(me, fetch_on, shop):
    shop.routes["/"] = (200, {"Content-Type": "text/html"}, b"nichts")
    vid = create_id(me, shop_url=shop.url + "/")
    r = post(me, f"/vouchers/{vid}/logo/search")
    assert r.status_code == 200 and "kein brauchbares Logo" in r.text
    shop.routes["/"] = (500, {}, b"")
    r = post(me, f"/vouchers/{vid}/logo/search")
    assert r.status_code == 502 and "Fehler 500" in r.text


def test_suche_nach_internem_ziel_verraet_nichts(me, fetch_on):
    vid = create_id(me, shop_url="http://192.168.1.10/")
    r = post(me, f"/vouchers/{vid}/logo/search")
    assert r.status_code == 502 and "nicht abgerufen werden" in r.text


def test_suche_ist_begrenzt(me, fetch_on, shop):
    shop.routes["/"] = (200, {"Content-Type": "text/html"}, b"nichts")
    vid = create_id(me, shop_url=shop.url + "/")
    codes = [post(me, f"/vouchers/{vid}/logo/search").status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


# --- Export und Import -------------------------------------------------------------------------------------------


def test_rundreise_mit_logo(me):
    vid = create_id(me, title="Hagebau")
    create_id(me, title="Ohne Logo")
    upload(me, vid, image_bytes((300, 300), (0, 120, 0, 255)))
    before = me.get(f"/vouchers/{vid}/logo").content
    content, document = exported(me)
    by_title = {v["title"]: v for v in document["vouchers"]}
    assert by_title["Hagebau"]["logo"].startswith("logos/") and by_title["Ohne Logo"]["logo"] is None
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        assert by_title["Hagebau"]["logo"] in z.namelist()
    sql("DELETE FROM vouchers")
    assert "Import abgeschlossen" in import_zip(me, content).text
    new_id = db_rows("SELECT id FROM vouchers WHERE title = 'Hagebau'")[0]["id"]
    assert me.get(f"/vouchers/{new_id}/logo").content == before
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_logos")[0]["n"] == 1


@pytest.mark.parametrize("member,content,warning", [
    ("../x.png", None, "ungültigen Namen"),
    ("logos/0001.png", None, "ungültigen Namen"),            # nicht im ZIP vorhanden
    ("logos/0001.png", b"kein bild", "kein brauchbares Bild"),
    ("logos/0001.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, "kein brauchbares Bild"),
    (5, None, "ungültigen Namen"),
])
def test_import_prueft_logos(me, member, content, warning):
    members = {member: content} if content is not None and isinstance(member, str) else None
    r = import_zip(me, make_zip(doc(entry(title="X", logo=member), format_version=4), members=members))
    assert "Import abgeschlossen" in r.text and warning in r.text
    assert db_rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"] == 1
    assert db_rows("SELECT COUNT(*) AS n FROM voucher_logos")[0]["n"] == 0


def test_import_ohne_logo_feld_aus_aelterer_version(me):
    r = import_zip(me, make_zip(doc(entry(title="Alt"), format_version=3)))
    assert "Import abgeschlossen" in r.text and db_rows("SELECT COUNT(*) AS n FROM voucher_logos")[0]["n"] == 0
