-- Mehrfach einlösbare Gutscheine (Prozent und Freitext): Gesamtzahl und übrige Verwendungen.
-- Bestehende Gutscheine sind einmal einlösbar; schon eingelöste haben keine Verwendung mehr übrig.
ALTER TABLE vouchers ADD COLUMN uses_total INTEGER NOT NULL DEFAULT 1 CHECK (uses_total >= 1 AND uses_total <= 99);
ALTER TABLE vouchers ADD COLUMN uses_left INTEGER NOT NULL DEFAULT 1 CHECK (uses_left >= 0 AND uses_left <= 99);
UPDATE vouchers SET uses_left = 0 WHERE state = 'redeemed';
