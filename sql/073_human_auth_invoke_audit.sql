-- 073_human_auth_invoke_audit.sql
-- TASK-30 AC#3/#4: live human-consent replay ledger and dual-key invoke audit.
--
-- `skill_calls.args_signing_kid` already records the autonomous agent key that
-- signed the call envelope. A human-required call also needs to record which
-- owner human-authorization key signed the per-call consent, plus the consent
-- hash for receipt/audit joins. Denials happen before a skill_calls row exists,
-- so tenancy_denials gets the same key/hash fields for the fail-closed gate.

ALTER TABLE skill_calls
    ADD COLUMN human_auth_kid TEXT,
    ADD COLUMN human_consent_hash TEXT;

ALTER TABLE skill_calls
    ADD CONSTRAINT skill_calls_human_consent_hash_fmt
    CHECK (human_consent_hash IS NULL OR human_consent_hash ~ '^[0-9a-f]{64}$');

ALTER TABLE tenancy_denials
    ADD COLUMN agent_signing_kid TEXT,
    ADD COLUMN human_auth_kid TEXT,
    ADD COLUMN human_consent_hash TEXT;

ALTER TABLE tenancy_denials
    ADD CONSTRAINT tenancy_denials_human_consent_hash_fmt
    CHECK (human_consent_hash IS NULL OR human_consent_hash ~ '^[0-9a-f]{64}$');

-- A fresh HumanConsent carries its own nonce. The gate records the first
-- presentation per owner key; a second presentation inside the freshness window
-- is a replay and fails closed. Keyed by (owner, kid, nonce) so separate human
-- devices have independent nonce spaces.
CREATE TABLE human_consent_nonce_seen (
    owner_id   UUID        NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    kid        TEXT        NOT NULL,
    nonce      TEXT        NOT NULL,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (owner_id, kid, nonce)
);

CREATE INDEX human_consent_nonce_seen_expires_idx
    ON human_consent_nonce_seen (expires_at);
