-- Art des Vorteils: Betrag in Euro (bisher), Prozent oder freier Text. Dazu ein optionaler
-- Mindestbestellwert. Bestehende Gutscheine bleiben Betrags-Gutscheine.
ALTER TABLE vouchers ADD COLUMN kind TEXT NOT NULL DEFAULT 'amount' CHECK (kind IN ('amount', 'percent', 'other'));
ALTER TABLE vouchers ADD COLUMN percent INTEGER CHECK (percent IS NULL OR (percent >= 1 AND percent <= 100));
ALTER TABLE vouchers ADD COLUMN benefit_text TEXT NOT NULL DEFAULT '';
ALTER TABLE vouchers ADD COLUMN min_order_cents INTEGER CHECK (min_order_cents IS NULL OR min_order_cents >= 0);
