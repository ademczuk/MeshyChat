-- DOWN for sql_meshychat/031 (divergence 061). `sqlmigrate.py --rollback-to meshychat:30` runs it (the rollback gate, apps/core/rollback.py), newest first.
-- tests/test_rollback_gate.py holds it to the catalog 031 found.
--
-- WHAT COMES BACK: nothing is restored, the record table had no binding before 031. Records written while the trigger was in force stay as they are.

SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS shared_memory_redaction_bound_trg ON shared_memory_redaction;
DROP FUNCTION IF EXISTS shared_memory_redaction_bound_to_row();

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 31;
