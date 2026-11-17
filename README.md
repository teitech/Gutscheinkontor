# Gutscheinkontor

> ⚠️ **Privates Projekt, Nutzung auf eigene Gefahr.** Gutscheinkontor entsteht als privates Projekt mit KI-gestützter Entwicklung (Vibe-Coding). Es gibt keine Garantie und keinen Support. Vor dem produktiven Einsatz bitte Sicherheit und Datensicherung selbst prüfen.

Gutscheinkontor verwaltet alle Gutscheine an einem Ort und erinnert rechtzeitig per E-Mail vor dem Ablauf. Die App ist für Homelabs und Familien gedacht und läuft als Docker-Container.

## Status

Frühe Entwicklungsphase. Fertig sind das Anwendungsgerüst, die SQLite-Datenbank mit Migrationen und der Login: Ersteinrichtung, Passwort, Zwei-Faktor-Anmeldung (TOTP) mit Wiederherstellungscodes und Abmelden. Ebenfalls fertig: E-Mail-Versand über einen eigenen SMTP-Server, der Passwort-Reset per E-Mail-Link und die Gutscheinverwaltung. Noch nicht fertig: Einladungen, Erinnerungsmails, Import/Export, Backups.

## Gutscheine

- Pro Gutschein: Name oder Firma, Code (verschlüsselt), Wert, Restwert, Ablaufdatum, zugehörige Person, Schlagwörter, Notizen und Anhänge.
- Teilbeträge lassen sich einlösen. Der Restwert wird mitgeführt, bei 0 gilt der Gutschein als eingelöst.
- „Abgelaufen“ und „teilweise eingelöst“ werden aus Datum und Restwert berechnet. Gutscheine lassen sich archivieren oder löschen.
- Suche über Name, Notizen und Schlagwörter (unabhängig von Groß- und Kleinschreibung), Filter nach Status, Schlagwort und Person, Sortierung nach Ablaufdatum.
- Es gibt einen gemeinsamen Bestand: Alle Nutzer einer Instanz sehen und bearbeiten alle Gutscheine.

### Anhänge

Erlaubt sind PDF, PNG, JPEG und WebP bis 10 MB (`MAX_UPLOAD_MB`), höchstens 10 pro Gutschein. Geprüft wird der tatsächliche Dateiinhalt, nicht der Dateiname. PDFs mit JavaScript oder eingebetteten Dateien werden abgelehnt, PDFs werden nur als Download ausgeliefert. **Das ist keine Virenprüfung.** Der Reverse Proxy muss Uploads in dieser Größe erlauben (bei nginx `client_max_body_size`, Caddy hat standardmäßig kein Limit).

## E-Mail und Passwort-Reset

Der Versand läuft über einen SMTP-Server deiner Wahl (Einstellungen in `.env.example`). Wer das Passwort vergisst, bekommt einen Link, der eine Stunde gilt und nur einmal funktioniert. Beim Zurücksetzen werden alle Sitzungen beendet, und die Zwei-Faktor-Anmeldung wird zurückgesetzt. Beim nächsten Login richtet man sie neu ein.

Das heißt auch: **Wer Zugriff auf das Postfach eines Nutzers hat, kann dessen Konto übernehmen.** Sichere die E-Mail-Konten der Nutzer entsprechend ab.

Ohne eingerichteten Mailserver erzeugt der Betreiber den Link auf der Kommandozeile:

```bash
docker compose exec gutscheinkontor python -m app.cli reset-password anna@example.de
```

## Lokal ausprobieren

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
export SECRET_KEY="$(openssl rand -base64 48)"
.venv/bin/uvicorn app.main:app --reload
```

Beim ersten Aufruf leitet die App auf die Ersteinrichtung weiter. Dort entsteht das Admin-Konto, danach richtest du die Zwei-Faktor-Anmeldung ein.

## Sicherheit

- Passwörter werden mit Argon2id gehasht, mindestens 12 Zeichen.
- Zwei-Faktor-Anmeldung ist Pflicht. Die TOTP-Geheimnisse liegen verschlüsselt in der Datenbank, die Wiederherstellungscodes nur als Hash.
- Sitzungen liegen serverseitig. Das Cookie ist `HttpOnly`, `SameSite=Lax` und bei HTTPS `Secure`.
- CSRF-Schutz auf allen Formularen, Begrenzung von Fehlversuchen beim Login und bei der 2FA.
- Die App läuft mit genau einem Worker, weil die Fehlversuchs-Zähler im Arbeitsspeicher liegen.

Mit Docker: `cp .env.example .env`, in `docker-compose.yml` die Zeile `build: .` aktivieren und `docker compose up -d --build`.

## Geplante Funktionen (Version 1)

- Login mit Passwort und 2-Faktor-Authentisierung (TOTP) mit Wiederherstellungscodes
- Passwort-Wiederherstellung per E-Mail
- Einladungslinks für weitere Haushaltsmitglieder
- Gutscheine mit Ablaufdatum, Wert, Restwert, Anhängen und Tags
- Verschlüsselte Speicherung der Gutscheincodes
- Erinnerungsmails 30, 7 und 1 Tag vor Ablauf
- Suche und Filter
- Import und Export (ZIP mit JSON und Anhängen, CSV)
- Tägliche automatische Backups
- Responsives Design mit Dark Mode
- Oberfläche auf Deutsch, vorbereitet für weitere Sprachen

## Datenschutz

Gutscheinkontor lädt keine externen Ressourcen, nutzt keine Tracker und keine Analytics. Die einzige externe Verbindung ist der SMTP-Server, den du selbst konfigurierst.

## Lizenz

[GNU Affero General Public License v3.0](LICENSE)
