-- Index the four grants list reads by their owner column (STATUS C47, RFQ:383-389).
--
-- WHY THIS IS A DIVERGENCE. Upstream's schema gives `skill_access_requests` only its
-- primary key and the partial unique index `skill_access_requests_pending_unique`
-- (requester_owner_id, granter_peer_id, skill_name) WHERE status = 'pending'. So
-- `GET /v1/access-requests/received` (WHERE granter_owner_id = $1) can use no index at
-- all, and `/sent` can use one only for its pending rows: the RFQ measured a Seq Scan
-- at cost 1e10 with sequential scans priced out. `skill_grants` has two partial indexes
-- WHERE revoked_at IS NULL, so `/v1/grants/issued|held?include_revoked=true` scans the
-- table as well. Every one of these reads is keyed on ONE owner and ordered newest
-- first (apps/grants/repository.py, list_* functions), so each gets a btree on
-- (owner column, order column DESC): an index range that also returns rows in the
-- order the route answers in, with no Sort over the owner's history.
--
-- NOT A BOUND. These reads remain unpaginated, as upstream's are; an owner with a
-- large history still gets all of it in one response. Bounding them changes the wire,
-- and that is a separate decision (STATUS C47).
--
-- WRITE COST. None of the indexed columns is updated after insert (the hot UPDATEs set
-- used_executions, revoked_at, status and decided_*), so heap-only updates are not
-- lost; each INSERT maintains two more btrees on each table.
--
-- tests/test_grants.py::test_every_list_read_is_an_index_range_on_its_owner EXPLAINs
-- the statement each list function really executes and requires an index condition on
-- its owner column.
--
-- WRITE LOCK (2026-09-25, review of 4cbbc9e5). Each CREATE INDEX below takes SHARE on its
-- table, and the runner applies this file in ONE transaction, so every INSERT and UPDATE
-- on skill_grants and skill_access_requests waits until all four are built. The review
-- measured 5.33 s for this body on a scratch database with 500,000 rows in each table;
-- a concurrent UPDATE of either table failed its 500 ms lock_timeout for that long, and
-- a SELECT went through. On tables of the size a dev or test database holds it is
-- milliseconds; production's sizes were not measured.
--   * What this file does about it: `lock_timeout`, as 014 does. Without it, a CREATE
--     INDEX queued behind a long transaction on the table holds every later writer
--     behind ITSELF for as long as that transaction lasts; with it, the migration fails
--     after 5 s and is retried (tests/test_grants.py::
--     test_020_gives_up_on_a_held_table_lock_instead_of_queueing_writers_behind_it).
--   * What it does not do: build without blocking writes. That is CREATE INDEX
--     CONCURRENTLY, which upstream uses for indexes on existing tables one per file
--     (sql/092, 095, 101). The runner applies a CONCURRENTLY file outside a
--     transaction, but four such statements in one file still run as one implicit
--     transaction and fail, so it needs four migration numbers where this batch holds
--     one. OPEN QUESTION for the integrator: split 020 into four CONCURRENTLY files, or
--     keep it transactional and schedule the apply for a quiet window?
--   * Reversal: sql_meshychat/down/020_grants_list_indexes_down.sql.

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS skill_grants_issued_idx
    ON skill_grants (grantor_owner_id, granted_at DESC);

CREATE INDEX IF NOT EXISTS skill_grants_held_idx
    ON skill_grants (grantee_owner_id, granted_at DESC);

CREATE INDEX IF NOT EXISTS skill_access_requests_sent_idx
    ON skill_access_requests (requester_owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS skill_access_requests_received_idx
    ON skill_access_requests (granter_owner_id, created_at DESC);
