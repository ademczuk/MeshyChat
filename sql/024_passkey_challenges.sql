-- Passkey challenges: short-lived storage between options/verify halves of
-- the WebAuthn register and authenticate ceremonies.
--
-- challenge is the 32-byte random challenge webauthn-rs minted. We store
-- the FULL serialized PasskeyRegistration / PasskeyAuthentication state
-- (state_json) because webauthn-rs's verify_* APIs need the original
-- state back. challenge is the PK and is the lookup key — clients send
-- it back inside the response.clientDataJSON's challenge field, which we
-- decode and match.
--
-- For register: owner_id is NULL during signup (no owner yet), populated
-- when adding a passkey to an existing owner. signup_token is a
-- short-lived bearer that ties the register half to a downstream
-- redemption flow (so a join-link visitor can register a passkey before
-- they have an owner row).
--
-- For auth: owner_id is NULL (we discover the owner from the assertion's
-- credential ID after verify). signup_token is unused.

CREATE TABLE passkey_challenges (
    challenge           BYTEA PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('register', 'auth')),
    owner_id            UUID REFERENCES dashboard_users(id) ON DELETE CASCADE,
    signup_token        TEXT,
    state_json          JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX passkey_challenges_expires_at_idx
    ON passkey_challenges (expires_at);
