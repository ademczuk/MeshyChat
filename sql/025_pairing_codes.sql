-- Pairing codes: one-time, owner-scoped, short-TTL codes used to bootstrap
-- a fresh desktop peer for an already-authenticated owner.
--
-- Flow:
--  1. Authenticated owner POSTs /v1/auth/onetime to mint a code.
--  2. The web page deep-links into the installed desktop:
--       agentmeshy-mentee://onboard?code=<code>
--  3. Desktop generates an Ed25519 keypair locally and POSTs to
--       /v1/peers with { onetime_code, device_label, public_key }.
--  4. The /v1/peers handler validates the code is fresh + unused, looks
--     up owner_id, creates a peer row + active peer_keys row, marks the
--     code consumed, returns a peer JWT + refresh token.
--
-- TTL: 60 seconds. Single-use enforced by used_at IS NOT NULL after
-- consumption.

CREATE TABLE pairing_codes (
    code                TEXT PRIMARY KEY,
    owner_id            UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    used_at             TIMESTAMPTZ
);

CREATE INDEX pairing_codes_owner_idx
    ON pairing_codes (owner_id);

CREATE INDEX pairing_codes_expires_at_idx
    ON pairing_codes (expires_at)
    WHERE used_at IS NULL;
