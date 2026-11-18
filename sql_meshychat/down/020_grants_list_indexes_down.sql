-- DOWN SCRIPT for sql_meshychat/020_grants_list_indexes.sql (STATUS C47).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- applies *.sql in that directory only). Since the rollback gate (apps/core/rollback.py,
-- 2026-09-25) `sqlmigrate.py --rollback-to meshychat:VERSION` runs it, in one transaction
-- with the deletion of ledger row 20; tests/test_grants.py::
-- test_the_020_down_script_removes_it_and_020_applies_again_over_the_result and the round
-- trip in tests/test_rollback_gate.py run it too.
--
-- BY HAND, RUN IT IN ONE TRANSACTION, as the owner role:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/020_grants_list_indexes_down.sql
-- Code at any commit runs on either schema: nothing reads these indexes by name, so the
-- only effect of removing them is that the four list reads plan as they did before 020.
--
-- WHAT IS LOST: the four indexes, which 020 rebuilds from the tables. Nothing else. The
-- ledger row goes too, so the next migrate applies 020 again; with the indexes still
-- present and the row missing it would also re-apply cleanly (IF NOT EXISTS).
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 020 and 014 have: DROP INDEX takes ACCESS EXCLUSIVE on
-- the table, and queued behind a long reader it would hold every later reader and writer
-- of the table behind it. It gives up after 5 s instead; retry.

SET LOCAL lock_timeout = '5s';

DROP INDEX IF EXISTS skill_grants_issued_idx;
DROP INDEX IF EXISTS skill_grants_held_idx;
DROP INDEX IF EXISTS skill_access_requests_sent_idx;
DROP INDEX IF EXISTS skill_access_requests_received_idx;

DELETE FROM meshychat_migrations WHERE version = 20;
