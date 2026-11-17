-- Nutzer. TOTP-Geheimnis ist verschlüsselt gespeichert.
CREATE TABLE users (
    id              INTEGER PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    totp_secret_enc TEXT,
    totp_enabled    INTEGER NOT NULL DEFAULT 0,
    totp_last_step  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Serverseitige Sessions. Gespeichert wird nur der Hash des Cookie-Tokens.
-- stage: pending_2fa (Passwort ok, Code fehlt), pending_setup (2FA noch einzurichten), active.
CREATE TABLE sessions (
    token_hash      TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage           TEXT NOT NULL CHECK (stage IN ('pending_2fa', 'pending_setup', 'active')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      INTEGER NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

-- Einmalige Wiederherstellungscodes (nur als Hash).
CREATE TABLE recovery_codes (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at   TEXT
);
CREATE INDEX idx_recovery_user ON recovery_codes(user_id);
