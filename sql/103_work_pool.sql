-- R5: competing-consumers work queue (proposal / propose-only).
-- A pool job lives here while unclaimed; a claim materializes a normal skill_call
-- (target = claimer) and runs the existing start_call pipeline. See
-- docs/design/r5-competing-consumers-work-queue.md for the full design and the
-- adversarial-review fixes (M1..M9) folded in.
--
-- Additive only: no change to skill_calls, its RLS, authorize.rs, route.rs, or the
-- existing janitor. Gated at the route layer behind MESHY_POOL_ENABLED (default false).

CREATE TABLE work_pool (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue                TEXT NOT NULL,
    -- server-derived image of the tenancy keystone (dev-plane: trust_group_id;
    -- product-plane: owner_id). Never taken from the request body. [FIX M7]
    tenant_key           TEXT NOT NULL,
    caller_peer_id       UUID NOT NULL REFERENCES peers(id),
    -- audit denormalization only; the keystone re-derives owners live at claim [S9]
    caller_owner_id      UUID NOT NULL REFERENCES dashboard_users(id),
    required_skill       TEXT NOT NULL,
    required_version     TEXT,
    -- the FULL signed skill.pool_call envelope (payload Value + signature + kid), so the
    -- claim can re-verify via verify_stored_payload(reference_time = None). [FIX M1, M2]
    envelope             JSONB NOT NULL,
    -- args extracted from the verified envelope, returned to the claimer
    args                 JSONB NOT NULL,
    args_attachments     UUID[] NOT NULL DEFAULT '{}',
    priority             SMALLINT NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued','claimed','done','failed','expired','cancelled')),
    attempts             SMALLINT NOT NULL DEFAULT 0,
    max_attempts         SMALLINT NOT NULL DEFAULT 3,
    claimed_by_peer_id   UUID REFERENCES peers(id),
    -- deterministic id||':'||attempts, written in the SAME claim UPDATE, so a crash-retry
    -- re-runs start_call with the same call_id and the idempotency check replays. [FIX M4]
    materialized_call_id TEXT,
    -- = materialized call deadline_at + bounded grace, so the pool never requeues a call
    -- its own janitor has not yet timed out. [FIX M5]
    claim_deadline_at    TIMESTAMPTZ,
    deadline_at          TIMESTAMPTZ NOT NULL,
    idempotency_key      TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedupe LIVE posts only. Scoped to non-terminal statuses so a terminal row does not
-- occupy the key forever and block a legitimate re-post after expiry/cancel. [S4]
CREATE UNIQUE INDEX work_pool_idem_uidx
    ON work_pool (tenant_key, queue, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND status IN ('queued','claimed');

-- Claim hot path. required_skill is covered so a mixed-skill queue does not SKIP LOCKED
-- walk-and-drop (and briefly lock) non-matching rows. [S6]
CREATE INDEX work_pool_claimable_idx
    ON work_pool (queue, tenant_key, required_skill, priority DESC, created_at)
    WHERE status = 'queued';

-- queued -> expired sweep: its own partial index (the claimable idx is queue/tenant-leading
-- and cannot range-seek a global status='queued' AND deadline_at < now()). [S5]
CREATE INDEX work_pool_expire_idx
    ON work_pool (deadline_at)
    WHERE status = 'queued';

-- claimed -> requeue sweep.
CREATE INDEX work_pool_claim_sweep_idx
    ON work_pool (claim_deadline_at)
    WHERE status = 'claimed';

-- RLS. NOTE: the live daemon connects as the rolbypassrls superuser (see migration 039),
-- so the APPLICATION-LAYER tenancy keystone in the claim SQL is the load-bearing control.
-- This RLS is defense-in-depth that becomes load-bearing when the daemon moves to meshy_app.
-- Do NOT use FOR ALL USING(true): Postgres OR-combines it into an always-true SELECT and
-- reintroduces the 039/096 read leak. [FIX M8]
ALTER TABLE work_pool ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_pool FORCE ROW LEVEL SECURITY;

CREATE POLICY work_pool_caller_select ON work_pool
    FOR SELECT
    USING (caller_peer_id::text = current_setting('app.current_peer_id', true));

CREATE POLICY work_pool_server_internal_select ON work_pool
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');

CREATE POLICY work_pool_internal_insert ON work_pool
    FOR INSERT WITH CHECK (true);
CREATE POLICY work_pool_internal_update ON work_pool
    FOR UPDATE USING (true) WITH CHECK (true);
CREATE POLICY work_pool_internal_delete ON work_pool
    FOR DELETE USING (true);

-- DOWN (manual rollback):
--   DROP TABLE work_pool CASCADE;
