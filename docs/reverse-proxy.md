# Reverse Proxy

Gutscheinkontor spricht im Container nur HTTP (Port 8000). Die Verschlüsselung (HTTPS) übernimmt dein
Reverse Proxy. Die Beispiele unten funktionieren mit Caddy, Traefik, nginx und Nginx Proxy Manager.

## Das Wichtigste vorab

1. **`BASE_URL`** in der `.env` auf die öffentliche Adresse setzen, mit `https://`, z. B.
   `BASE_URL=https://gutscheine.example.de`. Davon hängen Links in Mails (Einladung, Passwort-Reset)
   und die `Secure`-Eigenschaft der Cookies ab.
2. **Den Port nicht ins Internet öffnen.** Die `docker-compose.yml` bindet ihn an `127.0.0.1`. Erreichbar
   sein soll die App nur über den Proxy.
3. **`FORWARDED_ALLOW_IPS`** muss zum Netz passen, aus dem dein Proxy den Container anspricht (siehe unten).
4. **Upload-Größen:** Der Proxy muss Dateien bis `MAX_UPLOAD_MB` (Anhänge, Standard 10 MB) und bis
   `MAX_IMPORT_MB` (Import-ZIP, Standard 200 MB) durchlassen.
5. **HSTS** (`Strict-Transport-Security`) setzt die App nicht selbst, weil sie hinter dem Proxy nicht
   weiß, ob außen HTTPS gilt. Aktiviere es am Proxy, sobald alles läuft.

## FORWARDED_ALLOW_IPS: warum und was eintragen

Die App begrenzt Fehlversuche beim Login pro Besucher-Adresse. Dafür muss sie die Adresse des Besuchers
kennen und nicht die des Proxys. Sie liest sie aus `X-Forwarded-For`, aber **nur**, wenn die Anfrage von
einer Adresse in `FORWARDED_ALLOW_IPS` kommt.

Aus Sicht des Containers kommt jede Anfrage über Docker aus dem Gateway-Netz, auch wenn dein Proxy auf
demselben Host läuft und `127.0.0.1:8000` anspricht (typisch `172.x.0.1`). Der Standard
`172.16.0.0/12` deckt die üblichen Docker-Netze ab. Genauer geht es so:

```bash
docker network inspect gutscheinkontor_default --format '{{(index .IPAM.Config 0).Gateway}}'
```

Trage die ausgegebene Adresse in `FORWARDED_ALLOW_IPS` ein. Läuft dein Proxy im selben Docker-Netz,
nimm dessen Netz (z. B. `172.18.0.0/16`) oder seine feste Adresse.

**Nicht `*` eintragen.** Dann übernimmt die App den ersten Eintrag der Kette, und den kann ein Besucher
selbst mitschicken, sobald dein Proxy Header nur anhängt (nginx und Nginx Proxy Manager tun das). Mit
einem festen Netz nimmt die App den ersten nicht vertrauten Eintrag von rechts, den dein Proxy gesetzt
hat. Der lässt sich nicht fälschen.

Ob es stimmt, siehst du an der Fehlermeldung nach vielen falschen Logins: Sie betrifft nur den Besucher,
der sie ausgelöst hat, nicht alle zugleich.

## Caddy

Caddy auf demselben Host (Port aus `docker-compose.yml`):

```caddy
gutscheine.example.de {
	reverse_proxy 127.0.0.1:8000

	header Strict-Transport-Security "max-age=31536000"
}
```

Caddy holt das Zertifikat selbst, setzt `X-Forwarded-For` und `X-Forwarded-Proto` und hat standardmäßig
kein Upload-Limit. Willst du eines setzen:

```caddy
	request_body {
		max_size 210MB
	}
```

Caddy im selben Compose-Projekt: Entferne bei `gutscheinkontor` den Eintrag `ports` und nutze
`expose: ["8000"]`. Dann zeigt Caddy auf `gutscheinkontor:8000`, und `FORWARDED_ALLOW_IPS` bekommt das
Docker-Netz (`docker network inspect` zeigt es).

## Traefik

Labels am Dienst (mit deinen Namen für Entrypoint und Zertifikats-Resolver):

```yaml
    labels:
      - traefik.enable=true
      - traefik.http.routers.gutscheinkontor.rule=Host(`gutscheine.example.de`)
      - traefik.http.routers.gutscheinkontor.entrypoints=websecure
      - traefik.http.routers.gutscheinkontor.tls.certresolver=letsencrypt
      - traefik.http.services.gutscheinkontor.loadbalancer.server.port=8000
      - traefik.http.middlewares.gk-hsts.headers.stsSeconds=31536000
      - traefik.http.routers.gutscheinkontor.middlewares=gk-hsts
```

Traefik muss im selben Docker-Netz sein. Lass `ports` weg. Traefik überschreibt eingehende
`X-Forwarded-*`-Header, solange du `forwardedHeaders.trustedIPs` nicht setzt.

## nginx

```nginx
server {
    listen 443 ssl;
    server_name gutscheine.example.de;
    # ssl_certificate ... (zum Beispiel von certbot)

    client_max_body_size 210m;          # größer als MAX_IMPORT_MB
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;   # überschreibt, hängt nicht an
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;        # Import und Export großer ZIPs dauern
        proxy_request_buffering off;    # Uploads nicht erst im Proxy zwischenspeichern
    }
}
```

## Nginx Proxy Manager

1. **Proxy Host** anlegen: Domain `gutscheine.example.de`, Scheme `http`, Forward Host/IP die Adresse
   des Hosts (oder der Containername, wenn im selben Docker-Netz), Port `8000`.
2. **SSL**-Reiter: Zertifikat anfordern, „Force SSL“ und „HSTS Enabled“ einschalten.
3. **Advanced**-Reiter, „Custom Nginx Configuration“:

   ```nginx
   client_max_body_size 210m;
   proxy_read_timeout 300s;
   ```

Nginx Proxy Manager **hängt** Adressen an `X-Forwarded-For` an. Deshalb ist hier ein festes Netz in
`FORWARDED_ALLOW_IPS` wichtig und `*` gefährlich (siehe oben).

## Mit Authentifizierung davor (Authelia, Authentik, ...)

Das geht, ist aber nicht nötig: Die App hat eigene Anmeldung mit Zwei-Faktor. Schaltest du Anmeldung
davor, müssen die Pfade `/invite/…` und `/password-reset/…` ohne sie erreichbar bleiben, sonst können
eingeladene Personen und Nutzer mit vergessenem Passwort ihren Link nicht öffnen.
