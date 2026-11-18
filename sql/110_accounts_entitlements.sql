-- Cache of the Platform Accounts product-entitlements verdict, one row per owner.
--
-- Accounts is the authority for whether a principal may use AgentMeshy; this
-- table is only a cache of its answer, so it stores no PII and nothing
-- commercial: no name, email, role, plan, seat, price, or invoice. Just the
-- verdict, the capability sets, and the deadline (see
-- `crates/meshy-auth/src/entitlements.rs` for why the deadline fails closed).
--
-- `recheck_at` is NOT NULL on purpose. A nullable deadline invites
-- `WHERE recheck_at > NOW()` to quietly drop rows, or a reader to treat NULL as
-- "no expiry" — either way the cache would fail open, which is the one
-- behaviour this design exists to rule out.
CREATE TABLE IF NOT EXISTS accounts_entitlements (
    owner_id               UUID PRIMARY KEY
                             REFERENCES dashboard_users(id) ON DELETE CASCADE,
    -- Which issuer asserted this, matching dashboard_users.accounts_issuer.
    -- Kept so a re-pointed deployment can tell whose verdict it is holding.
    issuer                 TEXT        NOT NULL,
    subject                TEXT        NOT NULL,
    active                 BOOLEAN     NOT NULL,
    capabilities           TEXT[]      NOT NULL DEFAULT '{}',
    principal_capabilities TEXT[]      NOT NULL DEFAULT '{}',
    recheck_at             TIMESTAMPTZ NOT NULL,
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Supports the sweep a background re-check daemon will do (the follow-up that
-- uses the svc_agentmeshy client-credentials grant): "which cached verdicts are
-- about to expire?".
CREATE INDEX IF NOT EXISTS accounts_entitlements_recheck_at_idx
    ON accounts_entitlements (recheck_at);
