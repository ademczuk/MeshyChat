-- DOWN SCRIPT for sql_meshychat/022_call_worker_code_hash.sql (rule B9; divergence 041, the
-- operator's decision of 2026-09-25).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- applies *.sql in that directory only). `sqlmigrate.py --rollback-to meshychat:VERSION` runs
-- it (apps/core/rollback.py), in one transaction with the deletion of ledger row 22, and so can
-- an operator by hand:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/022_call_worker_code_hash_down.sql
-- tests/test_rollback_gate.py holds it to the catalog 022 found.
--
-- ORDER: roll the code back first. Code with 022's tick on the pre-022 schema fails every
-- worker tick's write (UndefinedColumn), which the loop logs and retries; code before it never
-- reads these columns.
--
-- WHAT IS LOST: the last hash each worker recorded; the next tick after 022 is applied again
-- writes it again. Nothing else: both CHECKs go with their columns.
--
-- A BOUNDED WAIT FOR ITS LOCK, as 022 has: ALTER TABLE takes ACCESS EXCLUSIVE on
-- background_job_leases. It gives up after 5 s instead of holding every job runner behind it.

SET LOCAL lock_timeout = '5s';

ALTER TABLE background_job_leases
    DROP COLUMN IF EXISTS worker_code_sha256,
    DROP COLUMN IF EXISTS worker_code_at;

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 22;
