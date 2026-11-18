-- DOWN SCRIPT for sql_meshychat/023_commit_order_sequence.sql (branch fin/w2-jobs; STATUS D19,
-- D09, E10, F09).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- applies *.sql in that directory only). `sqlmigrate.py --rollback-to meshychat:22` runs it
-- (apps/core/rollback.py), in one transaction with the deletion of ledger row 23, and so can an
-- operator by hand:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/023_commit_order_sequence_down.sql
-- tests/test_rollback_gate.py holds it to the catalog 023 found.
--
-- ORDER: roll the code back first. Code with 023's walks on the pre-023 schema fails every
-- mesh_edges and embed tick (UndefinedColumn watermark_seq), which the runner records and
-- retries; code before 023 never reads these objects and keeps its (created_at, id) walks.
--
-- WHAT IS LOST: the commit order (rebuilt from (created_at, id) if 023 is applied again, which
-- is the order the old walks use), each walk's watermark_seq (its watermark_at and
-- watermark_id stay, and the old code walks from them), the job cursors (the frame audit and
-- the chain-head reconcile start again from id 0 when 023 returns; the anchors themselves are
-- in invocation_chain_heads, which 023 does not own), and the running-call heartbeats (the
-- next tick after 023 returns starts them again).
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 023 has: dropping the trigger and the foreign keys takes
-- locks on shared_memory and skill_calls; give up after 5 s rather than queue every writer.

SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS shared_memory_mesh_commit_order ON shared_memory;
DROP FUNCTION IF EXISTS meshy_mesh_commit_stamp();
DROP TABLE IF EXISTS mesh_commit_order;

ALTER TABLE mesh_edges_projection DROP COLUMN IF EXISTS watermark_seq;
ALTER TABLE mesh_embeddings_projection DROP COLUMN IF EXISTS watermark_seq;

DROP TABLE IF EXISTS background_job_cursors;
DROP TABLE IF EXISTS skill_call_heartbeats;

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 23;
