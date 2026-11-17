# Gutscheinkontor

> ⚠️ **Privates Projekt, Nutzung auf eigene Gefahr.** Gutscheinkontor entsteht als privates Projekt mit KI-gestützter Entwicklung (Vibe-Coding). Es gibt keine Garantie und keinen Support. Vor dem produktiven Einsatz bitte Sicherheit und Datensicherung selbst prüfen.

Gutscheinkontor verwaltet alle Gutscheine an einem Ort und erinnert rechtzeitig per E-Mail vor dem Ablauf. Die App ist für Homelabs und Familien gedacht und läuft als Docker-Container.

## Status

Frühe Planungsphase. Der Funktionsumfang steht im Plan, die Umsetzung hat noch nicht begonnen.

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
