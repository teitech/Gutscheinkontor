# Gutscheinkontor

> ⚠️ **Privates Projekt, Nutzung auf eigene Gefahr.** Gutscheinkontor entsteht als privates Projekt mit KI-gestützter Entwicklung (Vibe-Coding). Es gibt keine Garantie und keinen Support. Vor dem produktiven Einsatz bitte Sicherheit und Datensicherung selbst prüfen.

Gutscheinkontor verwaltet alle Gutscheine an einem Ort und erinnert rechtzeitig per E-Mail vor dem Ablauf. Die App ist für Homelabs und Familien gedacht und läuft als Docker-Container.

## Status

Frühe Entwicklungsphase. Fertig sind das Anwendungsgerüst, die SQLite-Datenbank mit Migrationen und der Login: Ersteinrichtung, Passwort, Zwei-Faktor-Anmeldung (TOTP) mit Wiederherstellungscodes und Abmelden. Ebenfalls fertig: E-Mail-Versand über einen eigenen SMTP-Server, der Passwort-Reset per E-Mail-Link, die Gutscheinverwaltung, die Erinnerungsmails, Import/Export, automatische Backups und die Einladung weiterer Nutzer. Damit ist der geplante Umfang von Version 1 umgesetzt. Was noch fehlt, sind der Praxistest des Docker-Images und der Pipeline sowie eine Veröffentlichung als Release.

## Nutzer und Einladungen

Die erste Person, die die Ersteinrichtung abschließt, wird Administrator. Weitere Konten entstehen nur per Einladung, eine offene Registrierung gibt es nicht:

1. Ein Administrator lädt unter „Einstellungen → Nutzer einladen und verwalten“ eine E-Mail-Adresse ein. Mit eingerichtetem Mailserver geht der Link per Mail raus. Der Link wird dem Administrator **nur einmal** angezeigt, weil in der Datenbank nur sein Hash steht.
2. Die Person öffnet den Link (7 Tage gültig, einmal nutzbar), legt Namen und Passwort fest (die E-Mail-Adresse steht fest) und richtet danach die Zwei-Faktor-Anmeldung ein.
3. Alle Nutzer sehen und bearbeiten denselben Gutschein-Bestand. Nur Administratoren verwalten Nutzer und Backups.

Administratoren können Nutzer sperren (sofort abgemeldet, Anmeldung nicht mehr möglich), entsperren, zu Administratoren machen oder Admin-Rechte entziehen und Konten löschen. Gutscheine einer gelöschten Person bleiben erhalten und sind danach niemandem zugeordnet. Sich selbst kann man weder sperren noch löschen, und es bleibt immer mindestens ein aktiver Administrator. Setze `BASE_URL` auf die öffentliche Adresse, sonst zeigen Einladungslinks auf `localhost`.

## Backups

Die App legt täglich ab 3 Uhr (`BACKUP_HOUR`) ein Backup unter `data/backups/` an und behält die letzten 7 (`BACKUP_KEEP`). Ein Backup ist ein ZIP mit einem konsistenten Abbild der Datenbank (auch bei laufendem Betrieb) und allen Anhängen. Es wird erst nach einer Integritätsprüfung unter seinem Namen abgelegt und hat die Rechte 600. Administratoren sehen und laden Backups unter „Export / Import“ und können dort jederzeit eines erstellen.

**Wichtig:**
- Ein Backup enthält die **ganze Datenbank** mit Konten, Passwort-Hashes und verschlüsselten 2FA-Geheimnissen (anders als der Export). Es funktioniert nur mit dem passenden `SECRET_KEY`. Sichere den Schlüssel getrennt von den Daten.
- Die Backups liegen im selben Volume wie die Daten. Sie schützen vor Bedienfehlern und Datenbankschäden, **nicht vor dem Verlust des Datenträgers**. Sichere `data/` zusätzlich außerhalb, z. B. mit Restic oder Duplicati.
- Plane den Speicherplatz ein: Anhänge stecken in jedem der 7 Backups. Die App bricht ein Backup ab, wenn der Platz nicht reicht, und zeigt das Administratoren an.

### Wiederherstellen

Nur bei gestoppter App, auf der Kommandozeile:

```bash
docker compose stop
docker compose run --rm gutscheinkontor python -m app.cli restore-backup gutscheinkontor-backup-2026-11-17_030000.zip --yes
docker compose up -d
```

Vor dem Einspielen prüft die App die Datei (Aufbau, erlaubte Namen, Größe, Integrität der Datenbank, Version) und ob der `SECRET_KEY` zum Backup passt. Sie bricht bei Problemen ab, ohne etwas zu verändern. Der bisherige Stand wird nicht gelöscht, sondern nach `data/restore-undo-<Zeit>/` verschoben. Lösche den Ordner erst, wenn du geprüft hast, dass alles stimmt. Ältere Backups werden beim Einspielen auf die aktuelle Datenbankversion gebracht.

## Export und Import

Unter „Export / Import“ gibt es zwei Exporte:

- **ZIP (Sicherung):** Alle Gutscheine mit Schlagwörtern und Anhängen als `export.json` plus Dateien. Lässt sich wieder importieren, auch in eine andere Instanz.
- **CSV (Tabelle):** Zum Ansehen in Excel oder LibreOffice (UTF-8 mit BOM, Semikolon, deutsches Zahlenformat), ohne Anhänge, nicht importierbar. Zellen, die mit `=`, `+`, `-` oder `@` beginnen, werden entschärft, damit Tabellenprogramme sie nicht als Formel ausführen.

**Die Exporte enthalten standardmäßig die Gutscheincodes im Klartext**, damit eine Sicherung auch in einer Instanz mit anderem `SECRET_KEY` funktioniert. Bewahre sie sicher auf. Du kannst die Codes beim Export weglassen. Nutzerkonten, Passwörter und 2FA-Daten werden nie exportiert. Personen werden über ihre E-Mail-Adresse zugeordnet. Gibt es sie in der Ziel-Instanz nicht, ist der Gutschein danach niemandem zugeordnet.

Der Import nimmt nur dieses ZIP-Format an und **fügt nur hinzu**: Bestehende Gutscheine werden nie verändert oder gelöscht. Duplikate (gleicher Name, Code, Ablaufdatum und Wert) werden auf Wunsch übersprungen, und ein Probelauf zeigt vorab, was passieren würde. Die Datei wird wie eine fremde Eingabe behandelt: Jeder Eintrag durchläuft dieselbe Prüfung wie ein Formular, jeder Anhang dieselbe Prüfung wie ein Upload, und Größe, Anzahl und entpackter Umfang sind begrenzt (`MAX_IMPORT_MB`). Unlesbare Einträge werden übersprungen und im Bericht aufgeführt.

## Erinnerungen

Die App schickt Mails vor dem Ablauf, standardmäßig 30, 7 und 1 Tag vorher (`REMINDER_DAYS`), ab 8 Uhr morgens (`REMINDER_HOUR`, Zeitzone `TIMEZONE`). Sie braucht dafür den SMTP-Versand und läuft als Hintergrundaufgabe im selben Prozess. Unter „Einstellungen“ gibt es eine Testmail, mit der du den Versand prüfst.

- **Eine Mail pro Stufe:** Für jeden Gutschein, jede Person und jede Stufe kommt höchstens eine Mail. Mehrere Gutscheine für dieselbe Person landen in einer Sammelmail.
- **Nach einer Pause:** War die App aus, kommt nach dem Start nur die aktuelle Stufe, kein Schwall verpasster Mails.
- **Gerade angelegt:** Stufen, die schon vorbei waren, als du das Ablaufdatum gesetzt hast, entfallen. Ein Gutschein, den du mit 20 Tagen Restlaufzeit anlegst, löst keine 30-Tage-Mail aus. Ändert sich das Ablaufdatum, zählen die Stufen für das neue Datum von vorn.
- **Wer bekommt sie:** Ist ein Gutschein einer Person zugeordnet, nur diese. Sonst alle Nutzer. Jeder kann Erinnerungen für sich unter „Einstellungen“ abschalten.
- **Keine Erinnerung** gibt es für eingelöste, archivierte und abgelaufene Gutscheine und für solche ohne Ablaufdatum.
- **Datenschutz:** Der Gutscheincode steht nie in der Mail, nur Name, Datum, Restwert und ein Link. Schlägt der Versand fehl, versucht die App es alle 30 Minuten erneut.

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
