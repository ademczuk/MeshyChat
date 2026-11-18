-- Admin action audit trail (SOC2 P0). Append-only log of privileged admin
-- mutations: role/tier changes, session revocation, tier-capability
-- overrides, entitlement changes. See crates/meshy-api/src/audit.rs for the
-- writer (`record_admin_action`); no reader/mutator besides that helper is
-- expected to exist for now (a dashboard viewer app is a follow-up).
--
-- Immutability: `meshy_app` (migration 010) receives blanket
-- SELECT/INSERT/UPDATE/DELETE via `ALTER DEFAULT PRIVILEGES`, so newly
-- created tables are NOT append-only by default. This migration explicitly
-- REVOKEs UPDATE/DELETE from meshy_app right after creation so the audit trail
-- is enforced at the DB layer, not just by "nothing in the app calls UPDATE/
-- DELETE on this table" discipline. INSERT + SELECT remain granted (the app
-- needs to write rows and, eventually, list them).
--
-- Idempotent per tools/ci/MIGRATIONS.md (078+ rule).

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor       UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE RESTRICT,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    before      JSONB,
    after       JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_ip   INET
);

-- Read path: most callers want "recent actions" or "actions by this actor",
-- newest first.
CREATE INDEX IF NOT EXISTS admin_audit_log_occurred_idx
    ON admin_audit_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_log_actor_occurred_idx
    ON admin_audit_log (actor, occurred_at DESC);

-- ON DELETE RESTRICT (not CASCADE): deleting an admin's dashboard_users row
-- must not silently erase their audit trail. In practice dashboard_users rows
-- are not hard-deleted today; if that ever changes, the audit rows must be
-- preserved (e.g. reassign to a tombstone actor) rather than cascaded away.

REVOKE UPDATE, DELETE ON admin_audit_log FROM meshy_app;
