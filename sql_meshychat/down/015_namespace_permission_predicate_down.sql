-- DOWN SCRIPT for sql_meshychat/015_namespace_permission_predicate.sql (divergence 040, slice
-- 3; written at its integration, integ/r11).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- globs *.sql in that directory only), and nothing runs it but an operator, `sqlmigrate.py
-- --rollback-to meshychat:14` (apps/core/rollback.py) or tests/test_rollback_gate.py's round
-- trip. 015 is on rollback.NO_DOWN_SCRIPT_ALLOWED, so it needs none; it has one because the
-- roll-forward gate refuses, on a database in service, a pending migration without one, and
-- the test server applies 015 after 016, 017, 018, 020 and 021.
--
-- RUN IT IN ONE TRANSACTION, as the owner role:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/015_namespace_permission_predicate_down.sql
-- and AFTER rolling the code back past divergence 040 slice 3. From slice 3 on, check_permission
-- (every memory skill), memory.list with no namespace and /v1/mesh/wait's visibility call
-- meshy_namespace_permission; on the pre-015 schema each of them fails. Pre-040 code never
-- calls it, so the order is: deploy the pre-slice-3 code, then apply this.
--
-- WHAT IS LOST: the two forms of the function and their EXECUTE grants. Nothing stored: the
-- function reads shared_memory_namespace and shared_memory_grant and writes nothing. Before
-- 015 no function of that name existed, so there is nothing to restore.
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 014, 020 and 021 have: DROP FUNCTION waits for no table,
-- but it is stated so that every down script in this directory gives up after 5 s alike.

SET LOCAL lock_timeout = '5s';

DROP FUNCTION IF EXISTS meshy_namespace_permission(text, uuid);
DROP FUNCTION IF EXISTS meshy_namespace_permission(uuid);

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 15;
