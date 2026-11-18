-- 043_tenancy_denials.sql
--
-- IG6: a durable, queryable audit trail of tenancy-isolation DENIALS. Records
-- the cross-tenant invoke denial (no active grant) from the meshy-server invoke
-- gate (authorize.rs), and - wired separately by the mem-daemon - the
-- product-plane shared/* write and grant denials (I2). It deliberately does NOT
-- record meshy_memory cross-tenant READ denials: those return NotFound as an
-- existence-oracle defense and are high-volume / low-signal, so auditing every
-- one would recreate the oracle internally and flood the trail with benign
-- 404s carrying no actor-intent signal.
--
-- No foreign keys, by design: an audit trail must survive owner/peer deletion
-- (a denial that references a since-removed owner is still valid evidence), and
-- the invoke denial fires BEFORE any skill_calls row exists (authorize precedes
-- the call insert), so there is no call_id to reference. The writes are
-- best-effort and fail-open at the call sites: a failed audit insert must never
-- turn a policy denial into a 500.
CREATE TABLE tenancy_denials (
    id                BIGSERIAL PRIMARY KEY,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    denial_class      TEXT NOT NULL,   -- no_active_grant | product_no_shared_write | product_no_shared_grant
    op                TEXT NOT NULL,   -- skill.invoke | memory.write | memory.grant
    caller_owner_id   UUID,
    caller_peer_id    UUID,
    target_owner_id   UUID,
    target_peer_id    UUID,            -- the granter peer for an invoke denial
    skill_name        TEXT,
    namespace         TEXT,            -- for memory.write / memory.grant denials
    detail            TEXT
);

CREATE INDEX tenancy_denials_target_time_idx ON tenancy_denials (target_owner_id, occurred_at DESC);
CREATE INDEX tenancy_denials_caller_time_idx ON tenancy_denials (caller_owner_id, occurred_at DESC);
