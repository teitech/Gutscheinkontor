-- Einladungen: Der Administrator lädt eine E-Mail-Adresse ein, die Person legt über einen
-- einmal nutzbaren Link ihr Konto an. Gespeichert wird nur der Hash des Links.
CREATE TABLE invitations (
    id         INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    email      TEXT NOT NULL COLLATE NOCASE,
    invited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX idx_invitations_email ON invitations(email);
