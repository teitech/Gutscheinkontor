-- Logo pro Gutschein: ein kleines PNG, das die App selbst erzeugt (nie das Original).
-- Eigene Tabelle, damit die Gutschein-Liste die Bilddaten nicht mitlädt.
CREATE TABLE voucher_logos (
    voucher_id INTEGER PRIMARY KEY REFERENCES vouchers(id) ON DELETE CASCADE,
    png        BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
