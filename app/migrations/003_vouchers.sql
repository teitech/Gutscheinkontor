-- Gutscheine. Der Code ist verschlüsselt gespeichert. Beträge in Cent.
-- state: open (offen), redeemed (komplett eingelöst), archived (archiviert).
-- "Abgelaufen" und "teilweise eingelöst" werden bei der Anzeige berechnet.
CREATE TABLE vouchers (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    code_enc        TEXT,
    value_cents     INTEGER CHECK (value_cents IS NULL OR value_cents >= 0),
    remaining_cents INTEGER CHECK (remaining_cents IS NULL OR remaining_cents >= 0),
    expires_on      TEXT,
    state           TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'redeemed', 'archived')),
    owner_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes           TEXT NOT NULL DEFAULT '',
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_vouchers_expires ON vouchers(expires_on);

CREATE TABLE voucher_tags (
    voucher_id INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (voucher_id, tag)
);
CREATE INDEX idx_voucher_tags_tag ON voucher_tags(tag);

-- Dateien liegen im Datenverzeichnis unter attachments/. Der Dateiname dort ist zufällig.
CREATE TABLE voucher_attachments (
    id            INTEGER PRIMARY KEY,
    voucher_id    INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
    stored_name   TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_attachments_voucher ON voucher_attachments(voucher_id);
