-- DOWN SCRIPT for sql_meshychat/026_dead_root_grant_frame_retention.sql (STATUS D08; divergence
-- 052).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- applies *.sql in that directory only). `sqlmigrate.py --rollback-to meshychat:25` runs it
-- (apps/core/rollback.py), in one transaction with the deletion of ledger row 26, and so can an
-- operator by hand:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/026_dead_root_grant_frame_retention_down.sql
-- tests/test_rollback_gate.py holds it to the catalog 026 found, except what it KEEPS.
--
-- ORDER: roll the code back first. Code with the `frame_retention` job on the pre-026 schema
-- fails every tick (UndefinedTable invocation_frame_retention_policy), which the runner records
-- and retries; it deletes nothing. chain_head.py's ledger probe fails its reconcile of an
-- unanchored chain the same way, so the code must go back before the schema does.
--
-- WHAT IS KEPT, AND WHY:
--   * invocation_chain_retention: the only record of every chain retention deleted, with the
--     signed anchor each one had. Dropping it would turn "pruned by retention" into an absence
--     nobody can explain, which is the failure 026 exists to prevent. A chain left 'pruning'
--     has no anchor; code before 026 anchors its linked prefix from its root, a true statement.
--   * invocation_frame_retention_policy: the owner's window. Dropped, a re-apply of 026 would
--     turn chain retention back ON at the default even if the owner had turned it off.
-- 026 creates both with IF NOT EXISTS, so it applies again over them.
-- KEEPS: invocation_chain_retention
-- KEEPS: invocation_frame_retention_policy
--
-- WHAT IS LOST: the two functions, so nothing can retire a chain or prune a standalone frame.
-- Frames already deleted are not restored; they are named in the kept ledger.

SET LOCAL lock_timeout = '5s';

DROP FUNCTION IF EXISTS retire_invocation_chain(UUID, BIGINT, BIGINT, INTEGER);
DROP FUNCTION IF EXISTS prune_standalone_invocation_frames(INTEGER);

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 26;
