-- Tiers + billing schema.
-- Adds dashboard_users.tier column + five tables for caps, overrides,
-- usage, Stripe subscription state, and webhook idempotency.

-- 1. dashboard_users.tier column (backfills to 'free' for existing rows).
ALTER TABLE dashboard_users
    ADD COLUMN tier TEXT NOT NULL DEFAULT 'free'
        CHECK (tier IN ('free','pro','enterprise'));

-- 2. tier_capabilities — Free + Pro caps live here. Enterprise defaults
--    are seeded as -1 (unlimited); per-owner overrides land in owner_tier_overrides.
CREATE TABLE tier_capabilities (
    tier         TEXT NOT NULL CHECK (tier IN ('free','pro','enterprise')),
    capability   TEXT NOT NULL,
    value        BIGINT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tier, capability)
);

CREATE TRIGGER tier_capabilities_set_updated_at BEFORE UPDATE ON tier_capabilities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Seed Free + Pro defaults. Numbers from spec §5.2; tune live via
-- `UPDATE tier_capabilities SET value = N WHERE tier=$1 AND capability=$2`.
INSERT INTO tier_capabilities (tier, capability, value) VALUES
    ('free', 'local_memory_keys',     10000),
    ('free', 'meshy_memory_keys',      5000),
    ('free', 'file_bytes',            1073741824),    -- 1 GiB
    ('free', 'calls_per_day',         1000),
    ('free', 'concurrent_calls',      5),
    ('free', 'peers_per_owner',       5),
    ('pro',  'local_memory_keys',     100000),
    ('pro',  'meshy_memory_keys',      50000),
    ('pro',  'file_bytes',            26843545600),   -- 25 GiB
    ('pro',  'calls_per_day',         100000),
    ('pro',  'concurrent_calls',      50),
    ('pro',  'peers_per_owner',       50),
    ('enterprise', 'local_memory_keys',  -1),
    ('enterprise', 'meshy_memory_keys',   -1),
    ('enterprise', 'file_bytes',         -1),
    ('enterprise', 'calls_per_day',      -1),
    ('enterprise', 'concurrent_calls',   500),
    ('enterprise', 'peers_per_owner',    -1);

-- 3. owner_tier_overrides — Enterprise per-owner customizations.
--    Lookup order: overrides → tier_capabilities. -1 still means unlimited.
CREATE TABLE owner_tier_overrides (
    owner_id     UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    capability   TEXT NOT NULL,
    value        BIGINT NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, capability)
);

CREATE TRIGGER owner_tier_overrides_set_updated_at BEFORE UPDATE ON owner_tier_overrides
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 4. owner_subscriptions — Stripe state for paid customers.
--    Free users have no row. Webhook handler is the sole writer.
CREATE TABLE owner_subscriptions (
    owner_id                UUID PRIMARY KEY REFERENCES dashboard_users(id) ON DELETE CASCADE,
    stripe_customer_id      TEXT NOT NULL UNIQUE,
    stripe_subscription_id  TEXT,
    status                  TEXT NOT NULL DEFAULT 'incomplete',
    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    cancel_at_period_end    BOOLEAN NOT NULL DEFAULT FALSE,
    price_id                TEXT,
    metadata                JSONB NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER owner_subscriptions_set_updated_at BEFORE UPDATE ON owner_subscriptions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 5. owner_usage — rolling counters per billing cycle.
--    cycle_start: DATE_TRUNC('day', current_period_start) for paid; first-of-month for free.
CREATE TABLE owner_usage (
    owner_id      UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    capability    TEXT NOT NULL,
    cycle_start   DATE NOT NULL,
    counter       BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, capability, cycle_start)
);

CREATE TRIGGER owner_usage_set_updated_at BEFORE UPDATE ON owner_usage
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Index for the "what's this owner's current usage across all capabilities?"
-- query used by GET /v1/billing/me.
CREATE INDEX owner_usage_owner_cycle_idx
    ON owner_usage(owner_id, cycle_start);

-- 6. stripe_webhook_events — idempotency table for the webhook handler.
--    Stripe retries on non-2xx for 3 days; this table dedups replays.
CREATE TABLE stripe_webhook_events (
    stripe_event_id  TEXT PRIMARY KEY,
    event_type       TEXT NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at     TIMESTAMPTZ,
    payload          JSONB NOT NULL
);
