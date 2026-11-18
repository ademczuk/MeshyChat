-- DOWN SCRIPT for sql_meshychat/025_shared_memory_signed_bytes_required.sql (divergence 051,
-- B02 A2). Design docs/design/signed-bytes-status.md (fin/design-signed-bytes) 4.3, "A2 down:
-- restore the NULL admission".
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- applies *.sql in that directory only). `sqlmigrate.py --rollback-to meshychat:24` runs it
-- (apps/core/rollback.py), in one transaction with the deletion of ledger row 25, and so can
-- an operator by hand:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/025_shared_memory_signed_bytes_required_down.sql
-- tests/test_rollback_gate.py holds it to the catalog 025 found; tests/test_signed_bytes_a2.py
-- runs it and 014's after it.
--
-- ORDER: run it BEFORE any code rollback past A1 (design 4.3). While 025 is live, code that
-- writes no bytes fails every write. And before 014's down script: 025's trigger pins the
-- signed_bytes column (its WHEN), so 014's down refuses until this has run.
--
-- WHAT COMES BACK: A1's admission, exactly as 014 wrote it (014's function was never
-- changed): a NULL signed_bytes is refused from this build's marked meshy_app sessions and
-- admitted from every other session. WHAT IS LOST: nothing; no row is changed. A row that
-- 025 refused was never written.
--
-- A BOUNDED WAIT FOR ITS LOCK, as 025 has: DROP TRIGGER takes ACCESS EXCLUSIVE on
-- shared_memory.

SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS shared_memory_signed_bytes_required_trg ON shared_memory;
DROP FUNCTION IF EXISTS shared_memory_signed_bytes_required();

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 25;
