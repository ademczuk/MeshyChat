-- MeshyChat divergence 018: a per-route usage counter for the mesh read surface.
--
-- WHY. The graph-chat success metric is ADOPTION: are nodes asking /v1/mesh/inbox instead
-- of paging the namespace with memory.query (175,859 memory.query calls in seven days,
-- measured for divergence 014)? Nothing counted inbox usage, so the metric could not be
-- read at all. An in-process counter was the cheaper option and was rejected: it resets
-- on every deploy and each worker process holds its own, so an operator reading it would
-- see one worker's count since that worker's last restart. That is a number, not an
-- answer, and it could not be compared with a seven-day query volume.
--
-- WHAT IT IS. One row per (route, UTC day, peer, outcome) holding a call count, upserted
-- once per authenticated request by apps/mesh/usage.py. Per PEER because adoption is a
-- question about how many nodes use the route, not only how many calls it serves; per
-- OUTCOME because a request refused by the namespace read gate (divergence 018, same
-- change) is exactly the call an operator needs to see after a new gate lands.
--
-- WHAT IT IS NOT. It is not a projection of shared_memory and nothing is derived from it.
-- The rule "no request path writes a projection table" (divergence 014) is about
-- mesh_edges, which must be rebuildable from the log; this table is a primary record of
-- requests and has no log to be rebuilt from. It stores no message content, no label and
-- no query parameter: route name, day, peer id, outcome, count.
--
-- COST. One upsert per request on a surface measured at ~0.3 QPS. Growth is bounded by
-- routes x active peers x outcomes per day (5 x ~30 x 3 at fleet size, ~450 rows/day).
--
-- RETENTION, stated in the same change as the table (V1-DESIGN-RULES E7). Rows older than
-- 90 days are deleted by `python -m apps.mesh usage --prune`, which apps/mesh/usage.py
-- implements. Nothing schedules that command yet, which is the same honest state
-- docs/STATUS.md records for `manage.py run_jobs`.
--
-- RLS. Same policy shape as sql_meshychat/004: readable and writable only under
-- app.server_internal = '1', ENABLE and FORCE. meshy_app receives its grants through the
-- default privileges declared in sql/010, so no GRANT appears here.

CREATE TABLE IF NOT EXISTS mesh_route_usage (
    -- A CLOSED set, like mesh_edges.edge_type: a route added to apps/mesh/urls.py without
    -- a migration here fails its first count loudly (CheckViolation, logged and counted)
    -- instead of inventing a name.
    route      TEXT        NOT NULL CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait')),
    day        DATE        NOT NULL,
    peer_id    UUID        NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    outcome    TEXT        NOT NULL CHECK (outcome IN ('ok', 'denied', 'error')),
    calls      BIGINT      NOT NULL DEFAULT 0 CHECK (calls >= 0),
    first_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (route, day, peer_id, outcome)
);

-- The operator read is "every route over the last N days" and pruning is "every row
-- before day D": both are ranges on day, which the primary key (leading with route)
-- cannot serve without scanning every route's slice.
CREATE INDEX IF NOT EXISTS mesh_route_usage_day_idx ON mesh_route_usage (day);

ALTER TABLE mesh_route_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_route_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_route_usage_server_internal ON mesh_route_usage
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

COMMENT ON TABLE mesh_route_usage IS
    'Per-route, per-day, per-peer call counts for the /v1/mesh read surface (apps/mesh/usage.py). A primary record of requests, not a projection. Retention 90 days via python -m apps.mesh usage --prune.';
