<p align="center"><img src="app/static/logo.png" alt="Gutscheinkontor" width="160"></p>

# Gutscheinkontor

> ⚠️ **Privates Projekt, Nutzung auf eigene Gefahr.** Gutscheinkontor entsteht als privates Projekt mit KI-gestützter Entwicklung (Vibe-Coding). Es gibt keine Garantie und keinen Support. Vor dem produktiven Einsatz bitte Sicherheit und Datensicherung selbst prüfen.

Gutscheinkontor verwaltet alle Gutscheine an einem Ort und erinnert rechtzeitig per E-Mail vor dem Ablauf. Die App ist für Homelabs und Familien gedacht und läuft als Docker-Container.

## Status

Der geplante Umfang von Version 1 ist umgesetzt. Die Tests laufen bei jedem Push in GitHub Actions, dort wird das Image gebaut (amd64 und arm64) und unter amd64 wie im Betrieb gestartet und durchgeprüft. Version 0.1.0 ist als Docker-Image veröffentlicht (`ghcr.io/teitech/gutscheinkontor`, amd64 und arm64). Das Projekt ist jung: Rechne mit Ecken und Kanten und lies die Hinweise oben.

## Funktionen

- Gutscheine mit Ablaufdatum, Wert oder Prozent-Rabatt, Mindestbestellwert, Restwert, Schlagwörtern, Notizen und Anhängen, Code verschlüsselt gespeichert
- Erinnerungsmails vor dem Ablauf (standardmäßig 30, 7 und 1 Tag vorher)
- Mehrere Nutzer (z. B. Familie) mit gemeinsamem Bestand, Einladung per Link
- Login mit Passwort und Pflicht-Zwei-Faktor (TOTP), Passwort-Reset per E-Mail
- Import und Export (ZIP mit Anhängen, CSV), tägliche automatische Backups
- Responsives Design mit Dark Mode, Oberfläche auf Deutsch (vorbereitet für weitere Sprachen)
- Keine Tracker, keine externen Ressourcen

## Installation mit Docker Compose

Du brauchst Docker mit dem Compose-Plugin und einen Reverse Proxy, der HTTPS übernimmt (z. B. Caddy).

```bash
git clone https://github.com/teitech/Gutscheinkontor.git
cd Gutscheinkontor
cp .env.example .env
```

Trage in der `.env` mindestens ein:

- `SECRET_KEY`: erzeugen mit `openssl rand -base64 48`. **Sichere ihn getrennt von den Daten.** Ohne ihn sind Gutscheincodes, 2FA-Daten und Backups nicht mehr lesbar.
- `BASE_URL`: die öffentliche Adresse mit `https://`, z. B. `https://gutscheine.example.de`.

Dann starten:

```bash
docker compose up -d
```

Das Image kommt von der GitHub Container Registry. Willst du es selbst bauen, entferne in `docker-compose.yml` das `#` vor `build: .` und starte mit `docker compose up -d --build`.

Die App lauscht auf `127.0.0.1:8000`. Richte deinen Reverse Proxy darauf aus (Beispiele für Caddy, Traefik, nginx und Nginx Proxy Manager in [docs/reverse-proxy.md](docs/reverse-proxy.md)) und öffne die Adresse. Beim ersten Aufruf legst du das Administrator-Konto an und richtest die Zwei-Faktor-Anmeldung ein.

**Proxy auf einem anderen Rechner?** Dann musst du den Port in der `docker-compose.yml` ändern (`"8000:8000"`, dazu eine Firewall-Regel nur für den Proxy). Binde ihn besser nicht an eine feste Adresse des Hosts: Ist die Adresse beim Start von Docker noch nicht da, etwa nach einem Neustart oder Backup, startet der Container nicht und bleibt aus. Details und Ausnahmen stehen in [docs/reverse-proxy.md](docs/reverse-proxy.md#port-an-eine-feste-adresse-binden-proxy-auf-einem-anderen-rechner).

**Aktualisieren:** `docker compose pull && docker compose up -d`. Willst du eine feste Version, trage in der `.env` `GK_IMAGE=ghcr.io/teitech/gutscheinkontor:0.1.0` ein. Die Datenbank wird beim Start automatisch auf den neuen Stand gebracht. Erstelle vorher ein Backup (Export / Import → Jetzt ein Backup erstellen).

**Daten:** Datenbank, Anhänge und Backups liegen im benannten Docker-Volume `gutscheinkontor-data`. Ein Bind-Mount ist möglich (Hinweise in `docker-compose.yml`), dann muss der Ordner dem Benutzer 10001 gehören, sonst bricht die App beim Start mit einer klaren Meldung ab.

**Logs:** Die App schreibt keine Zugriffsprotokolle, weil Einladungs- und Reset-Links ihren Token im Pfad tragen. Der Reverse Proxy führt sein eigenes Log, achte dort auf die Aufbewahrung (Datenschutz).

## Nutzer und Einladungen

Die erste Person, die die Ersteinrichtung abschließt, wird Administrator. Weitere Konten entstehen nur per Einladung, eine offene Registrierung gibt es nicht:

1. Ein Administrator lädt unter „Einstellungen → Nutzer einladen und verwalten“ eine E-Mail-Adresse ein. Mit eingerichtetem Mailserver geht der Link per Mail raus. Der Link wird dem Administrator **nur einmal** angezeigt, weil in der Datenbank nur sein Hash steht.
2. Die Person öffnet den Link (7 Tage gültig, einmal nutzbar), legt Namen und Passwort fest (die E-Mail-Adresse steht fest) und richtet danach die Zwei-Faktor-Anmeldung ein.
3. Alle Nutzer sehen und bearbeiten denselben Gutschein-Bestand. Nur Administratoren verwalten Nutzer und Backups.

Administratoren können Nutzer sperren (sofort abgemeldet, Anmeldung nicht mehr möglich), entsperren, zu Administratoren machen oder Admin-Rechte entziehen und Konten löschen. Gutscheine einer gelöschten Person bleiben erhalten und sind danach niemandem zugeordnet. Sich selbst kann man weder sperren noch löschen, und es bleibt immer mindestens ein aktiver Administrator.

## Gutscheine

- Pro Gutschein: Name oder Firma, Code (verschlüsselt), Art und Wert (siehe unten), Restwert, Ablaufdatum, zugehörige Person, Schlagwörter, Notizen und Anhänge.
- Drei Arten: **Betrag in Euro**, **Prozent-Rabatt** (z. B. „10 % auf den nächsten Einkauf“) und **Sonstiges** mit freiem Text (z. B. „Gratis-Versand“). Bei Prozent und Sonstiges gibt es keinen Restwert, der Gutschein ist offen oder eingelöst.
- Optional lässt sich eine **Webshop-Adresse** hinterlegen (nur `http`/`https`, der Link öffnet in einem neuen Tab) und mit **„Nur online einlösbar“** markieren. Der Hinweis erscheint in Liste, Detailansicht und Erinnerungsmail.
- Jeder Gutschein kann ein **Logo** haben: ein eigenes Bild hochladen (PNG, JPEG, WebP, GIF, ICO) oder, wenn eingeschaltet, von der Webshop-Adresse holen und aus den gefundenen Bildern auswählen. Die App speichert immer ein selbst erzeugtes, verkleinertes PNG, nie das Original.
- Optional hat jeder Gutschein einen **Mindestbestellwert**. Er steht in Liste, Detailansicht und Erinnerungsmail.
- Teilbeträge lassen sich einlösen (bei Betrags-Gutscheinen). Der Restwert wird mitgeführt, bei 0 gilt der Gutschein als eingelöst.
- „Abgelaufen“ und „teilweise eingelöst“ werden aus Datum und Restwert berechnet. Gutscheine lassen sich archivieren oder löschen.
- Suche über Name, Notizen und Schlagwörter (unabhängig von Groß- und Kleinschreibung), Filter nach Status, Schlagwort und Person, Sortierung nach Ablaufdatum.
- Es gibt einen gemeinsamen Bestand: Alle Nutzer einer Instanz sehen und bearbeiten alle Gutscheine.

### Anhänge

Erlaubt sind PDF, PNG, JPEG und WebP bis 10 MB (`MAX_UPLOAD_MB`), höchstens 10 pro Gutschein. Geprüft wird der tatsächliche Dateiinhalt, nicht der Dateiname. PDFs mit JavaScript oder eingebetteten Dateien werden abgelehnt, PDFs werden nur als Download ausgeliefert. **Das ist keine Virenprüfung.** Der Reverse Proxy muss Uploads in dieser Größe erlauben (bei nginx `client_max_body_size`, Caddy hat standardmäßig kein Limit).

### Logos

Zu jedem Gutschein lässt sich ein Logo hinzufügen. Hochladen geht immer (PNG, JPEG, WebP, GIF, ICO, bis 2 MB). Die App liest das Bild vollständig ein und speichert ein selbst erzeugtes PNG mit höchstens 128 Pixeln. Metadaten und Originaldatei bleiben nicht erhalten.

Mit `LOGO_FETCH=1` gibt es zusätzlich den Knopf **„Logo von der Webseite holen“**: Die App ruft die Webshop-Adresse des Gutscheins ab, sucht dort Touch-Icons, Favicons und Vorschaubilder und zeigt sie zur Auswahl. Der Abruf läuft nur auf Knopfdruck (höchstens 10 Suchen pro Nutzer in 10 Minuten) und ist gegen Missbrauch gehärtet: nur http/https auf den Ports 80 und 443, interne Adressen (Heimnetz, localhost, Cloud-Metadaten) sind gesperrt, auch nach Weiterleitungen und DNS-Tricks, dazu Grenzen für Größe und Zeit. Ohne `LOGO_FETCH` ruft die App nie fremde Server auf.

## Erinnerungen

Die App schickt Mails vor dem Ablauf, standardmäßig 30, 7 und 1 Tag vorher (`REMINDER_DAYS`), ab 8 Uhr morgens (`REMINDER_HOUR`, Zeitzone `TIMEZONE`). Sie braucht dafür den SMTP-Versand und läuft als Hintergrundaufgabe im selben Prozess. Unter „Einstellungen“ gibt es eine Testmail, mit der du den Versand prüfst.

- **Eine Mail pro Stufe:** Für jeden Gutschein, jede Person und jede Stufe kommt höchstens eine Mail. Mehrere Gutscheine für dieselbe Person landen in einer Sammelmail.
- **Nach einer Pause:** War die App aus, kommt nach dem Start nur die aktuelle Stufe, kein Schwall verpasster Mails.
- **Gerade angelegt:** Stufen, die schon vorbei waren, als du das Ablaufdatum gesetzt hast, entfallen. Ein Gutschein, den du mit 20 Tagen Restlaufzeit anlegst, löst keine 30-Tage-Mail aus. Ändert sich das Ablaufdatum, zählen die Stufen für das neue Datum von vorn.
- **Wer bekommt sie:** Ist ein Gutschein einer Person zugeordnet, nur diese. Sonst alle Nutzer. Jeder kann Erinnerungen für sich unter „Einstellungen“ abschalten.
- **Keine Erinnerung** gibt es für eingelöste, archivierte und abgelaufene Gutscheine und für solche ohne Ablaufdatum.
- **Datenschutz:** Der Gutscheincode steht nie in der Mail, nur Name, Datum, Restwert bzw. Vorteil, Mindestbestellwert und ein Link. Schlägt der Versand fehl, versucht die App es alle 30 Minuten erneut.

## E-Mail und Passwort-Reset

Der Versand läuft über einen SMTP-Server deiner Wahl (Einstellungen in `.env.example`). Wer das Passwort vergisst, bekommt einen Link, der eine Stunde gilt und nur einmal funktioniert. Beim Zurücksetzen werden alle Sitzungen beendet, und die Zwei-Faktor-Anmeldung wird zurückgesetzt. Beim nächsten Login richtet man sie neu ein.

Das heißt auch: **Wer Zugriff auf das Postfach eines Nutzers hat, kann dessen Konto übernehmen.** Sichere die E-Mail-Konten der Nutzer entsprechend ab.

Ohne eingerichteten Mailserver erzeugt der Betreiber den Link auf der Kommandozeile:

```bash
docker compose exec gutscheinkontor python -m app.cli reset-password anna@example.de
```

## Export und Import

Unter „Export / Import“ gibt es zwei Exporte:

- **ZIP (Sicherung):** Alle Gutscheine mit Schlagwörtern und Anhängen als `export.json` plus Dateien. Lässt sich wieder importieren, auch in eine andere Instanz.
- **CSV (Tabelle):** Zum Ansehen in Excel oder LibreOffice (UTF-8 mit BOM, Semikolon, deutsches Zahlenformat), ohne Anhänge, nicht importierbar. Zellen, die mit `=`, `+`, `-` oder `@` beginnen, werden entschärft, damit Tabellenprogramme sie nicht als Formel ausführen.

**Die Exporte enthalten standardmäßig die Gutscheincodes im Klartext**, damit eine Sicherung auch in einer Instanz mit anderem `SECRET_KEY` funktioniert. Bewahre sie sicher auf. Du kannst die Codes beim Export weglassen. Nutzerkonten, Passwörter und 2FA-Daten werden nie exportiert. Personen werden über ihre E-Mail-Adresse zugeordnet. Gibt es sie in der Ziel-Instanz nicht, ist der Gutschein danach niemandem zugeordnet.

Der Import nimmt nur dieses ZIP-Format an und **fügt nur hinzu**: Bestehende Gutscheine werden nie verändert oder gelöscht. Duplikate (gleicher Name, Code, Ablaufdatum und Vorteil) werden auf Wunsch übersprungen, und ein Probelauf zeigt vorab, was passieren würde. Die Datei wird wie eine fremde Eingabe behandelt: Jeder Eintrag durchläuft dieselbe Prüfung wie ein Formular, jeder Anhang dieselbe Prüfung wie ein Upload, und Größe, Anzahl und entpackter Umfang sind begrenzt (`MAX_IMPORT_MB`). Unlesbare Einträge werden übersprungen und im Bericht aufgeführt.

## Backups

Die App legt täglich ab 3 Uhr (`BACKUP_HOUR`) ein Backup im Daten-Volume unter `backups/` an und behält die letzten 7 (`BACKUP_KEEP`). Ein Backup ist ein ZIP mit einem konsistenten Abbild der Datenbank (auch bei laufendem Betrieb) und allen Anhängen. Es wird erst nach einer Integritätsprüfung unter seinem Namen abgelegt und hat die Rechte 600. Administratoren sehen und laden Backups unter „Export / Import“ und können dort jederzeit eines erstellen.

**Wichtig:**
- Ein Backup enthält die **ganze Datenbank** mit Konten, Passwort-Hashes und verschlüsselten 2FA-Geheimnissen (anders als der Export). Es funktioniert nur mit dem passenden `SECRET_KEY`. Sichere den Schlüssel getrennt von den Daten.
- Die Backups liegen im selben Volume wie die Daten. Sie schützen vor Bedienfehlern und Datenbankschäden, **nicht vor dem Verlust des Datenträgers**. Sichere das Volume zusätzlich außerhalb. Einfachster Weg: Lade das neueste Backup als Administrator herunter. Für eine automatische Sicherung der Dateien taugt z. B.:

  ```bash
  docker run --rm -v gutscheinkontor-data:/data:ro -v "$PWD":/sicherung alpine \
    tar czf /sicherung/gutscheinkontor-daten.tar.gz -C /data .
  ```

  Das lässt sich mit Restic oder Duplicati kombinieren.
- Plane den Speicherplatz ein: Anhänge stecken in jedem der 7 Backups. Die App bricht ein Backup ab, wenn der Platz nicht reicht, und zeigt das Administratoren an.

### Wiederherstellen

Nur bei gestoppter App, auf der Kommandozeile:

```bash
docker compose stop
docker compose run --rm gutscheinkontor python -m app.cli restore-backup gutscheinkontor-backup-2026-11-17_030000.zip --yes
docker compose up -d
```

Vor dem Einspielen prüft die App die Datei (Aufbau, erlaubte Namen, Größe, Integrität der Datenbank, Version) und ob der `SECRET_KEY` zum Backup passt. Sie bricht bei Problemen ab, ohne etwas zu verändern. Der bisherige Stand wird nicht gelöscht, sondern in den Ordner `restore-undo-<Zeit>/` im Volume verschoben. Lösche ihn erst, wenn du geprüft hast, dass alles stimmt. Ältere Backups werden beim Einspielen auf die aktuelle Datenbankversion gebracht.

## Sicherheit

- Passwörter werden mit Argon2id gehasht, mindestens 12 Zeichen.
- Zwei-Faktor-Anmeldung ist Pflicht. Die TOTP-Geheimnisse liegen verschlüsselt in der Datenbank, die Wiederherstellungscodes nur als Hash.
- Sitzungen liegen serverseitig. Das Cookie ist `HttpOnly`, `SameSite=Lax` und bei HTTPS `Secure`.
- CSRF-Schutz auf allen Formularen, Begrenzung von Fehlversuchen beim Login und bei der 2FA (pro Konto und pro Besucher-Adresse).
- Strenge Content-Security-Policy, keine externen Ressourcen. Hochgeladene Dateien werden mit einer Sandbox-Richtlinie ausgeliefert.
- Der Container läuft ohne Root, mit schreibgeschütztem Dateisystem, ohne zusätzliche Rechte (`cap_drop: ALL`) und ohne Zugriffsprotokoll (siehe oben).
- Die App läuft mit genau einem Worker, weil die Fehlversuchs-Zähler im Arbeitsspeicher liegen.
- Hinter einem Proxy zählt für die Begrenzung die Besucher-Adresse aus `X-Forwarded-For`. Setze `FORWARDED_ALLOW_IPS` passend zu deinem Docker-Netz und niemals auf `*` ([Erklärung](docs/reverse-proxy.md)).

## Datenschutz

Gutscheinkontor lädt keine externen Ressourcen, nutzt keine Tracker und keine Analytics. Die einzige externe Verbindung ist der SMTP-Server, den du selbst konfigurierst. Nur wenn du `LOGO_FETCH=1` setzt, ruft die App zusätzlich auf Knopfdruck die Webseite des jeweiligen Shops ab (siehe „Logos“). Die App schreibt keine Zugriffsprotokolle und keine Adressen oder Gutscheindaten ins Log.

## Entwicklung

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
export SECRET_KEY="$(openssl rand -base64 48)"
.venv/bin/uvicorn app.main:app --reload
```

Beim ersten Aufruf leitet die App auf die Ersteinrichtung weiter. Die GitHub-Pipeline (`.github/workflows/ci.yml`) führt die Tests aus und startet das gebaute Image mit `docker compose`. Dort läuft `scripts/smoke_test.py`, der die App über HTTP durchgeht: Einrichtung mit 2FA, Gutschein, Anhang, Export, Import, Backup und die Begrenzung pro Besucher-Adresse. Außerdem prüft die Pipeline Härtung, Neustart, schnelles Beenden und dass keine Tokens im Log landen.

### Release veröffentlichen

Ein Tag wie `v1.0.0` löst `.github/workflows/release.yml` aus. Die Pipeline führt die Tests aus, baut das Image für amd64 und arm64, veröffentlicht es unter `ghcr.io/teitech/gutscheinkontor` (mit den Tags `1.0.0`, `1.0` und bei stabilen Versionen `latest`) und legt ein GitHub-Release an. Bei `v0.1.0` war das Paket nach der Veröffentlichung ohne Anmeldung abrufbar.

## Lizenz

[GNU Affero General Public License v3.0](LICENSE)
