-- 024_mesh_route_usage_memory_query.sql
-- `memory.query` is counted where it runs (STATUS F10; divergence 018 (d), amended 2026-09-26).
--
-- WHY. The graph-chat success metric is inbox adoption against memory.query volume, and 018
-- (d) could count only the first half: "memory.query is not dispatched by this server". Since
-- step 9 of the call pipeline executes memory calls in-process (apps/calls/route.py, divergence
-- 017) it is, so apps/calls/route.py counts every executed memory.query into
-- mesh_route_usage, one row per (route, UTC day, peer, outcome), as the mesh routes are
-- counted. The table's route set is a CLOSED CHECK (005, widened by 013 and 017); this adds
-- the one name, `memory_query`. apps/mesh/usage.COUNTED carries the same list and
-- tests/test_mesh_usage_memory_query.py compares the two.
--
-- NUMBERING. The burndown pre-assigned 024 to W2-mesh (and 023 to W2-jobs). It named the file
-- 024_mesh_sequence, for a delivery sequence; W2-mesh draws none of its own: the commit order
-- is 023's `mesh_commit_order`, and this file carries the batch's only schema change.
--
-- A BOUNDED WAIT FOR ITS LOCK, as 014 and 020-023 have: ALTER TABLE takes ACCESS EXCLUSIVE on
-- mesh_route_usage, which every mesh request writes; queued behind a long transaction it
-- would hold every count behind itself. It gives up after 5 s instead; retry. Validating
-- the widened CHECK reads the table (small: routes x peers x outcomes per day, 90 days).
--
-- Reversal: sql_meshychat/down/024_mesh_route_usage_memory_query_down.sql. DEPLOY ORDER:
-- migrate, then roll the code (code that counts memory_query on the pre-024 CHECK loses each
-- count, logged and counted in WRITE_FAILURES, and serves every query); back: roll the code
-- back, then run the down script.

SET LOCAL lock_timeout = '5s';

ALTER TABLE mesh_route_usage DROP CONSTRAINT IF EXISTS mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query'));
