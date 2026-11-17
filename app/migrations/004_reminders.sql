-- Erinnerungen: pro Nutzer abschaltbar, pro Gutschein wird das Datum gemerkt,
-- an dem das Ablaufdatum gesetzt oder geändert wurde.
ALTER TABLE users ADD COLUMN reminders_enabled INTEGER NOT NULL DEFAULT 1;

ALTER TABLE vouchers ADD COLUMN expiry_set_on TEXT;
UPDATE vouchers SET expiry_set_on = substr(created_at, 1, 10);

-- Welche Erinnerung wurde wem schon geschickt? Das Ablaufdatum gehört zum Schlüssel:
-- Wird es geändert, gelten die Stufen für das neue Datum von vorn.
CREATE TABLE reminders_sent (
    voucher_id INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage_days INTEGER NOT NULL,
    expires_on TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    PRIMARY KEY (voucher_id, user_id, stage_days, expires_on)
);
