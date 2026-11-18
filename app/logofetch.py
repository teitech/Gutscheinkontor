"""Logos von der Webseite eines Shops holen.

Das ist die einzige Stelle, an der die App von sich aus fremde Server aufruft. Sie ist deshalb
standardmäßig aus (LOGO_FETCH=1 schaltet sie ein) und gegen Missbrauch gehärtet:

* Nur http/https auf den Ports 80 und 443.
* Der Rechnername wird aufgelöst und die Verbindung geht direkt an die geprüfte Adresse. So lässt
  sich der Abruf nicht per DNS-Trick auf das Heimnetz, localhost oder Cloud-Metadaten umlenken
  (SSRF). Gesperrt ist alles, was keine öffentliche Internetadresse ist.
* Jede Weiterleitung wird erneut geprüft (höchstens 3).
* Harte Grenzen für Größe, Zeit und Anzahl der Bilder. Keine Kompression (kein Dekompressionsangriff).
* Gespeichert wird nie das Original, sondern das von logos.process() erzeugte PNG.
"""
from __future__ import annotations

import http.client
import ipaddress
import logging
import socket
import ssl
import time
from html.parser import HTMLParser
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from . import logos

logger = logging.getLogger("gutscheinkontor")

ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 3
MAX_PAGE_BYTES = 512 * 1024
MAX_IMAGE_BYTES = logos.MAX_INPUT_BYTES
MAX_CANDIDATES = 8        # so viele Bilder werden höchstens heruntergeladen
MAX_RESULTS = 6           # so viele Treffer werden höchstens angezeigt
SOCKET_TIMEOUT = 5
TOTAL_SECONDS = 25
USER_AGENT = "Gutscheinkontor-Logo-Abruf"


class FetchError(Exception):
    """Fehler mit einer Meldung, die dem Nutzer angezeigt werden darf."""


# --- Schutz vor Zugriffen auf interne Adressen --------------------------------------------------


def _ip_allowed(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _resolve(host: str, port: int) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("Die Adresse ließ sich nicht auflösen.") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def _validated_target(url: str) -> Tuple[str, str, int, str, str]:
    """Gibt (Schema, Rechnername, Port, geprüfte IP, Pfad mit Query) zurück."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("Nur http- und https-Adressen sind erlaubt.")
    if parts.username or parts.password:
        raise FetchError("Adressen mit Zugangsdaten sind nicht erlaubt.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise FetchError("Ungültiger Port.") from exc
    if port not in ALLOWED_PORTS:
        raise FetchError("Nur die Ports 80 und 443 sind erlaubt.")
    host = parts.hostname
    ips = _resolve(host, port)
    if not ips or not all(_ip_allowed(ip) for ip in ips):
        # Bewusst eine allgemeine Meldung: Die App soll nicht verraten, was im Heimnetz existiert.
        raise FetchError("Diese Adresse darf nicht abgerufen werden.")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, ips[0], path


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str):
        super().__init__(host, port, timeout=SOCKET_TIMEOUT)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), SOCKET_TIMEOUT)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str):
        super().__init__(host, port, timeout=SOCKET_TIMEOUT, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), SOCKET_TIMEOUT)
        # Das Zertifikat wird gegen den Rechnernamen geprüft, die Verbindung geht an die geprüfte IP.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch(url: str, max_bytes: int, deadline: float) -> Tuple[str, str, bytes]:
    """Lädt eine Adresse. Gibt (endgültige Adresse, Content-Type, Inhalt) zurück."""
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, ip, path = _validated_target(url)
        conn = (_PinnedHTTPS if scheme == "https" else _PinnedHTTP)(host, port, ip)
        try:
            try:
                conn.request("GET", path, headers={
                    "User-Agent": USER_AGENT, "Accept": "text/html,image/*;q=0.8,*/*;q=0.5",
                    "Accept-Encoding": "identity",
                })
                response = conn.getresponse()
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                raise FetchError("Die Webseite ist nicht erreichbar.") from exc

            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise FetchError("Ungültige Weiterleitung.")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise FetchError(f"Die Webseite antwortet mit Fehler {response.status}.")
            if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                raise FetchError("Die Webseite liefert komprimierte Daten, die nicht erlaubt sind.")
            length = response.getheader("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise FetchError("Die Datei ist zu groß.")

            chunks, total = [], 0
            try:
                while True:
                    if time.monotonic() > deadline:
                        raise FetchError("Die Webseite antwortet zu langsam.")
                    chunk = response.read(16 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError("Die Datei ist zu groß.")
                    chunks.append(chunk)
            except (OSError, http.client.HTTPException) as exc:
                raise FetchError("Die Webseite ist nicht erreichbar.") from exc
            return url, (response.getheader("Content-Type") or "").lower(), b"".join(chunks)
        finally:
            conn.close()
    raise FetchError("Zu viele Weiterleitungen.")


# --- Bilder auf der Seite finden ---------------------------------------------------------------


def _largest_size(sizes: str) -> int:
    best = 0
    for token in sizes.lower().split():
        left, _, right = token.partition("x")
        if left.isdigit() and right.isdigit():
            best = max(best, min(int(left), int(right)))
    return best


class _IconParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: List[Tuple[int, str]] = []   # (Rang, Adresse): kleiner = besser

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "link" and a.get("href"):
            rel = a.get("rel", "").lower().split()
            size = _largest_size(a.get("sizes", ""))
            if "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
                self.found.append((0 - size, a["href"]))      # Touch-Icons sind meist am schönsten, größere zuerst
            elif "icon" in rel:
                self.found.append((1000 - size, a["href"]))
        elif tag == "meta":
            name = (a.get("property") or a.get("name") or "").lower()
            if name in ("og:image", "og:image:url", "twitter:image") and a.get("content"):
                self.found.append((3000, a["content"]))
            elif name == "msapplication-tileimage" and a.get("content"):
                self.found.append((2000, a["content"]))


def candidate_urls(page_url: str, html: str) -> List[str]:
    parser = _IconParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - kaputtes HTML: nimm, was bis dahin gefunden wurde
        pass
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(page_url))
    found = sorted(parser.found, key=lambda item: item[0])
    found += [(4000, origin + "/apple-touch-icon.png"), (5000, origin + "/favicon.ico")]
    urls: List[str] = []
    for _, href in found:
        absolute = urljoin(page_url, href.strip())
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or parts.path.lower().endswith(".svg"):
            continue
        if absolute not in urls:
            urls.append(absolute)
    return urls[:MAX_CANDIDATES]


def search(shop_url: str) -> List[bytes]:
    """Sucht auf der Shop-Webseite nach Logos. Gibt fertige PNGs zurück (höchstens MAX_RESULTS)."""
    deadline = time.monotonic() + TOTAL_SECONDS
    final_url, content_type, body = fetch(shop_url, MAX_PAGE_BYTES, deadline)
    html = body.decode("utf-8", errors="replace") if "html" in content_type or not content_type else ""
    results: List[bytes] = []
    for url in candidate_urls(final_url, html):
        if len(results) >= MAX_RESULTS or time.monotonic() > deadline:
            break
        try:
            _, _, data = fetch(url, MAX_IMAGE_BYTES, deadline)
            png = logos.process(data)
        except (FetchError, logos.LogoError):
            continue
        if png not in results:
            results.append(png)
    return results
