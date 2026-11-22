-- Gutscheine weitergeben. Ein Eintrag pro Weitergabe.
--   kind:   link (per E-Mail-Link an mehrere Personen) oder manual (persönlich übergeben, nur ein Vermerk)
--   status: open (Links laufen), claimed (übernommen bzw. verschenkt), revoked (zurückgeholt), expired (nicht übernommen)
-- Pro Gutschein darf höchstens eine Weitergabe offen sein.
CREATE TABLE voucher_gifts (
    id              INTEGER PRIMARY KEY,
    voucher_id      INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('link', 'manual')),
    status          TEXT NOT NULL CHECK (status IN ('open', 'claimed', 'revoked', 'expired')),
    message         TEXT NOT NULL DEFAULT '',
    recipient_count INTEGER NOT NULL DEFAULT 0,
    given_to        TEXT NOT NULL DEFAULT '',
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    claimed_at      INTEGER
);
CREATE INDEX idx_gifts_voucher ON voucher_gifts(voucher_id);
CREATE UNIQUE INDEX idx_gifts_one_open ON voucher_gifts(voucher_id) WHERE status = 'open';

-- Empfänger mit eigenem Link. Gespeichert wird nur der Hash des Links. Die Adressen werden
-- gelöscht, sobald die Weitergabe beendet ist (außer der Adresse der Person, die übernommen hat).
CREATE TABLE gift_recipients (
    id         INTEGER PRIMARY KEY,
    gift_id    INTEGER NOT NULL REFERENCES voucher_gifts(id) ON DELETE CASCADE,
    email      TEXT NOT NULL COLLATE NOCASE,
    token_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_gift_recipients_gift ON gift_recipients(gift_id);
