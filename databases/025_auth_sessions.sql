DO $$
BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=25) THEN
CREATE TABLE auth_sessions (
    token_hash text PRIMARY KEY,
    username text NOT NULL,
    credential_signature text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at timestamptz NOT NULL
);
CREATE INDEX auth_sessions_username_idx ON auth_sessions(username);
CREATE INDEX auth_sessions_expiry_idx ON auth_sessions(expires_at);

CREATE TABLE auth_login_limits (
    bucket text PRIMARY KEY,
    attempts integer NOT NULL CHECK (attempts > 0),
    resets_at timestamptz NOT NULL
);
INSERT INTO schema_versions(version) VALUES (25);
END IF;
END $$;
