-- Refresh tokens: SHA-256 hash of a 32-byte random token, bound to peer +
-- owner. Issued at redeem time and rotated on /v1/auth/refresh.

CREATE TABLE refresh_tokens (
    token_hash          BYTEA PRIMARY KEY,
    peer_id             UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    owner_id            UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    issued_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ
);

CREATE INDEX refresh_tokens_peer_id_idx ON refresh_tokens (peer_id);
CREATE INDEX refresh_tokens_expires_at_idx ON refresh_tokens (expires_at) WHERE revoked_at IS NULL;
