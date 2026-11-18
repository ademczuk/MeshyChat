-- Minimal peer + owner schema.
-- Subsequent migrations layer on the full server schema.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE dashboard_users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT,
    role            TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner','admin')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE peers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    display_name    TEXT,
    tier            TEXT NOT NULL DEFAULT 'free'
                    CHECK (tier IN ('free','starter','pro','enterprise','partner')),
    primary_signing_kid TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended','deleted')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX peers_owner_idx ON peers (owner_id) WHERE status = 'active';
-- dashboard_users.email is already UNIQUE; that constraint creates an implicit
-- B-tree index. No explicit (email) index is needed.

-- updated_at trigger
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dashboard_users_updated_at
    BEFORE UPDATE ON dashboard_users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER peers_updated_at
    BEFORE UPDATE ON peers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
