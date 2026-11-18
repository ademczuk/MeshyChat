-- WebAuthn credentials: passkeys bound to dashboard_users. One owner can
-- have many credentials (multi-device). credential_id is the WebAuthn
-- credential ID (raw bytes, base64url-decoded), used as PK so we can look
-- up by the credential ID the authenticator sends back at assertion time.
--
-- public_key is the COSE_Key encoded public key from the attestation. We
-- store it raw; webauthn-rs reconstructs the parsed key from the bytes at
-- verify time.
--
-- sign_count starts at 0 and is bumped on every successful assertion. A
-- regression (assertion sign_count <= stored sign_count) is a cloning
-- signal; webauthn-rs flags it for us.

CREATE TABLE webauthn_credentials (
    credential_id       BYTEA PRIMARY KEY,
    owner_id            UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    public_key          BYTEA NOT NULL,
    sign_count          BIGINT NOT NULL DEFAULT 0,
    transports          TEXT[] NOT NULL DEFAULT '{}',
    aaguid              UUID,
    name                TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at        TIMESTAMPTZ
);

CREATE INDEX webauthn_credentials_owner_idx
    ON webauthn_credentials (owner_id);
