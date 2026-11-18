-- 018: owner API keys (PATs) + peer metadata for onboarding (SP6 Stage A).
--
-- Programmatic owner credential. Mirrors migration 017's session-token
-- discipline: the DB stores ONLY the SHA-256 hash; the raw token exists only
-- in the client. A DB read (SQL injection, leaked backup, read replica) yields
-- nothing usable as a credential. The raw token is shown to the caller exactly
-- once, at creation, and never persisted or logged.
--
-- This is a SEPARATE credential class from the fail-closed Bearer-JWT gate: a
-- PAT is resolved by hash lookup against this table (a third branch in the
-- AuthenticatedOwner extractor, ordered before the JWT fallback), so adding it
-- does not touch or weaken that gate.
--
-- No explicit GRANTs: migration 010's `ALTER DEFAULT PRIVILEGES IN SCHEMA
-- public ... GRANT ... ON TABLES TO meshy_app` already covers tables created by
-- the migration-running user after it (same precedent as owner_sessions in 015),
-- so the production meshy_app role can read/write this table.
CREATE TABLE owner_api_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id      UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL,                       -- hex(sha256(raw))
    token_prefix  TEXT NOT NULL,                       -- "meshypat_AbCd" (display only)
    scopes        TEXT[] NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,                          -- NULL = no expiry
    revoked_at    TIMESTAMPTZ                           -- NULL = active
);

-- The hash is the lookup key on every PAT-authenticated request; UNIQUE both
-- enforces no two keys share a hash and provides the index for the hot path.
CREATE UNIQUE INDEX owner_api_keys_token_hash_idx ON owner_api_keys (token_hash);

-- Partial index for the per-owner "list my active keys" read.
CREATE INDEX owner_api_keys_owner_active_idx
    ON owner_api_keys (owner_id) WHERE revoked_at IS NULL;

-- Optional structured metadata captured at peer creation (e.g. agent model,
-- purpose). Nullable + additive; no backfill. Created in this migration so the
-- onboarding create-peer path (later SP6 stage) has somewhere to store it.
ALTER TABLE peers ADD COLUMN metadata JSONB;
