-- Einstellungen für die ganze Instanz, die Admins in der Oberfläche ändern.
-- Ohne Eintrag gilt der Wert aus der Umgebungsvariablen.
CREATE TABLE app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
