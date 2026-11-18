-- Promote the per-owner in-flight partial index to a permanent migration.
--
-- The concurrent-quota gate counts a caller's in-flight skill_calls on every
-- skill.call: SELECT COUNT(*) FROM skill_calls WHERE caller_owner_id = $1 AND
-- status NOT IN ('completed','failed','timeout','abandoned'). Without a partial
-- index on caller_owner_id that count seq-scans the whole table. On 2026-06-23 it
-- ran about 70s under load and stalled meshy-server, cascading to 502/530 across
-- the mesh. The fix was an ad-hoc index created live on the box, which a
-- main-postgres volume rebuild or a fresh deploy would silently lose, re-arming
-- the same outage.
--
-- This makes that index permanent. Idempotent (CREATE INDEX IF NOT EXISTS) so it
-- is a no-op on the live DB that already carries the ad-hoc index, and recreates
-- it on any fresh or rebuilt DB, matching the 067-085 idempotent siblings. The
-- partial predicate matches the quota query so the planner uses it as an Index
-- Only Scan (measured cost about 8, versus the seq-scan cost about 15290).
CREATE INDEX IF NOT EXISTS skill_calls_owner_inflight_idx
    ON skill_calls (caller_owner_id)
    WHERE status NOT IN ('completed', 'failed', 'timeout', 'abandoned');
