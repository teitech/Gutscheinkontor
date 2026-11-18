-- Shop-Adresse (nur http/https) und der Hinweis "nur online einlösbar".
ALTER TABLE vouchers ADD COLUMN shop_url TEXT NOT NULL DEFAULT '';
ALTER TABLE vouchers ADD COLUMN online_only INTEGER NOT NULL DEFAULT 0 CHECK (online_only IN (0, 1));
