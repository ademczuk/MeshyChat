-- MeshyChat, branch fin/w2-jobs (STATUS D19, D09, E10, F09): a commit-ordered sequence for the
-- shared/mesh-chat walks, the cursors of the frame audit and the chain-head anchor, and the
-- running-call heartbeat.
--
-- NUMBERING. The burndown pre-assigned 023 to W2-jobs (and 024 to W2-mesh); origin/main holds
-- sql_meshychat 001-022. One migration carries the batch's three schema needs.
--
-- ============================================================ 1. D19, the commit order
--
-- THE DEFECT. The mesh_edges projection and the embed job walk shared/mesh-chat by the keyset
-- (created_at, id). `created_at` is the writer's TRANSACTION START (now()), so a transaction
-- that starts first and commits last writes a row BELOW a row the walk has already passed:
-- that row is never visited (divergence 014's blind spot; 019 and 031 name it too). A
-- sequence drawn at INSERT has the same flaw: it orders the draws, not the commits.
--
-- THE MECHANISM. A DEFERRED constraint trigger on shared/mesh-chat inserts runs at COMMIT
-- time. It takes one transaction-scoped advisory lock and only then draws the next value of
-- `mesh_commit_order.seq`, writing (seq, the row's id). The lock is held until that
-- transaction has committed (a transaction-scoped lock is released after the commit makes the
-- transaction visible), so the next stamping transaction draws only after the previous one is
-- visible. Therefore a reader that sees seq N sees every committed seq below N, and nothing
-- that commits later can take a seq below one already seen: the walk keyed on seq visits
-- every row once, in commit order. What it costs: mesh-chat INSERT transactions serialize for
-- the instant between their deferred triggers and their commit (the commit record's flush),
-- at 0.3 writes a second measured on the mesh (projection.py). No writer is changed: the
-- trigger fires for every writer of the table, this build's and upstream's meshy-server alike.
--
-- The lock is the TWO-integer form, (0x6D636F6D 'mcom', 1), whose key space does not overlap
-- the single-bigint keys (apps/calls/pipeline.py hashtext(call_id), apps/authapi/bootstrap.py,
-- upstream's per-chain frame lock) nor meshsum's (0x6D73756D, 1).
--
-- A row deleted before its own transaction commits (inserted and deleted in one transaction)
-- has nothing to stamp: the function looks the row up first. A later INSERT of the same id is
-- a no-op here (ON CONFLICT), and the foreign key removes a stamp with its row.
--
-- HISTORY. Every existing mesh-chat row is stamped here in the walks' OLD order, (created_at,
-- id), after the trigger exists: CREATE TRIGGER takes SHARE ROW EXCLUSIVE on shared_memory, so
-- every insert either committed before it (and is stamped by the backfill) or waits for this
-- file's commit (and is stamped by the trigger). Each walk's saved watermark is translated to
-- the seq of the last row at or below it, which in backfill order is exactly the set it had
-- walked, so no walk re-reads or skips a row because of this file.
--
-- READ SIDE. `mesh_commit_order` is readable under app.server_internal only (the walks run
-- under projection_writer, which includes it); meshy_app may not write it at all: the trigger
-- function, SECURITY DEFINER, is its only writer. RLS is ENABLED and not FORCED on purpose:
-- the definer (the migration role) must write it whatever its RLS standing, and meshy_app,
-- which is not the owner, is bound by the policy either way.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS mesh_commit_order (
    seq         bigserial   PRIMARY KEY,
    memory_id   uuid        NOT NULL UNIQUE REFERENCES shared_memory(id) ON DELETE CASCADE,
    stamped_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE mesh_commit_order ENABLE ROW LEVEL SECURITY;
CREATE POLICY mesh_commit_order_read ON mesh_commit_order
    FOR SELECT USING (current_setting('app.server_internal', true) = '1');
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mesh_commit_order FROM meshy_app;

CREATE OR REPLACE FUNCTION meshy_mesh_commit_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    -- Held to this transaction's end: the next stamper draws after this one is visible.
    PERFORM pg_advisory_xact_lock(1835233133, 1);
    INSERT INTO mesh_commit_order (memory_id)
    SELECT NEW.id WHERE EXISTS (SELECT 1 FROM shared_memory WHERE id = NEW.id)
    ON CONFLICT (memory_id) DO NOTHING;
    RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION meshy_mesh_commit_stamp() FROM PUBLIC;

DROP TRIGGER IF EXISTS shared_memory_mesh_commit_order ON shared_memory;
CREATE CONSTRAINT TRIGGER shared_memory_mesh_commit_order
    AFTER INSERT ON shared_memory
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.namespace = 'shared/mesh-chat')
    EXECUTE FUNCTION meshy_mesh_commit_stamp();

INSERT INTO mesh_commit_order (memory_id)
SELECT id FROM shared_memory
 WHERE namespace = 'shared/mesh-chat'
 ORDER BY created_at, id
ON CONFLICT (memory_id) DO NOTHING;

ALTER TABLE mesh_edges_projection ADD COLUMN IF NOT EXISTS watermark_seq bigint;
ALTER TABLE mesh_embeddings_projection ADD COLUMN IF NOT EXISTS watermark_seq bigint;

UPDATE mesh_edges_projection p
   SET watermark_seq = (SELECT o.seq FROM mesh_commit_order o
                          JOIN shared_memory sm ON sm.id = o.memory_id
                         WHERE (sm.created_at, sm.id) <= (p.watermark_at, p.watermark_id)
                         ORDER BY o.seq DESC LIMIT 1)
 WHERE p.watermark_at IS NOT NULL;
UPDATE mesh_embeddings_projection p
   SET watermark_seq = (SELECT o.seq FROM mesh_commit_order o
                          JOIN shared_memory sm ON sm.id = o.memory_id
                         WHERE (sm.created_at, sm.id) <= (p.watermark_at, p.watermark_id)
                         ORDER BY o.seq DESC LIMIT 1)
 WHERE p.watermark_at IS NOT NULL;

-- ============================================================ 2. D09, E10: job cursors
--
-- One row per named cursor of a background job: the frame audit's two (`frame_audit.new`, the
-- id of the last frame audited on arrival; `frame_audit.recheck`, the rolling re-verification,
-- with its lap count) and the chain-head anchor's two (`chain_head.frames`, the last frame id
-- its reconcile has covered; `chain_head.anchors`, the rolling verification of the anchors, by
-- chain_id). Written only by the job bodies, under the job's lease; like
-- background_job_leases, the runner's own table, it carries no row-level security and holds
-- nothing a peer wrote.

CREATE TABLE IF NOT EXISTS background_job_cursors (
    cursor_name  text        PRIMARY KEY,
    position     bigint      NOT NULL DEFAULT 0,
    position_key text,
    laps         integer     NOT NULL DEFAULT 0,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ============================================================ 3. F09, the running heartbeat
--
-- One row per call the heartbeat job has seen `running`: `beat_at` is the last time the
-- call's TARGET held a live presence row (hub_connections, divergence 041 S2: a socket whose
-- worker beats every 10 s), `lost_at` when it was first seen without one for longer than
-- apps/calls/heartbeat.LOST_AFTER_SECS. The job writes it; nothing else does. A row goes with
-- its call (ON DELETE CASCADE). server_internal for every command, the shape 016 gives
-- hub_connections: a request that could write a beat could keep a dead call looking alive.

CREATE TABLE IF NOT EXISTS skill_call_heartbeats (
    call_id  text        PRIMARY KEY REFERENCES skill_calls(call_id) ON DELETE CASCADE,
    beat_at  timestamptz NOT NULL,
    lost_at  timestamptz
);

ALTER TABLE skill_call_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_call_heartbeats FORCE ROW LEVEL SECURITY;
CREATE POLICY skill_call_heartbeats_read ON skill_call_heartbeats
    FOR SELECT USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY skill_call_heartbeats_insert ON skill_call_heartbeats
    FOR INSERT WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY skill_call_heartbeats_update ON skill_call_heartbeats
    FOR UPDATE USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY skill_call_heartbeats_delete ON skill_call_heartbeats
    FOR DELETE USING (current_setting('app.server_internal', true) = '1');
