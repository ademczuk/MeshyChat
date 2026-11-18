-- DOWN SCRIPT for sql_meshychat/024_mesh_route_usage_memory_query.sql (STATUS F10; divergence
-- 018 (d), amended 2026-09-26).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- applies *.sql in that directory only). `sqlmigrate.py --rollback-to meshychat:23` runs it
-- (apps/core/rollback.py), in one transaction with the deletion of ledger row 24, and so can an
-- operator by hand:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/024_mesh_route_usage_memory_query_down.sql
-- tests/test_rollback_gate.py holds it to the catalog 024 found.
--
-- ORDER: roll the code back first. Code that counts memory_query on the pre-024 CHECK loses
-- each count (CheckViolation, logged and counted in WRITE_FAILURES) and serves every query.
--
-- WHAT IS LOST: every memory_query count. The narrower CHECK cannot be added while a row
-- names the route, so they are deleted first; the mesh routes' counts are untouched.
--
-- A BOUNDED WAIT FOR ITS LOCK, as 024 has.

SET LOCAL lock_timeout = '5s';

DELETE FROM mesh_route_usage WHERE route = 'memory_query';
ALTER TABLE mesh_route_usage DROP CONSTRAINT IF EXISTS mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog'));

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 24;
