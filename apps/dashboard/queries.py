"""SQL projections for dashboard handlers.

Kept out of `views.py` so the HTTP layer stays readable, and kept as ONE module so the
projections that must agree with each other (`MEMORY_OBJ` is consumed by the list,
search, export and duplicates routes) are side by side.

TWO MECHANICAL RULES FOR EVERYTHING IN HERE
-------------------------------------------
1. Placeholders are psycopg NAMED style, `%(owner)s`. A value may occur five to seven
   times per statement; positional `%s` would require passing the same value that many
   times in the right order, and a mis-ordered bind in a visibility CTE is a leak that
   still returns rows.
2. Because named placeholders are in use, EVERY literal percent sign must be doubled.
   `LIKE 'msg/%'` is written `LIKE 'msg/%%'`, and the four tag patterns in `MEMORY_OBJ`
    are written `'%%-side'`, `'pattern:%%'`, `'chain:%%'`, `'project:%%'`. Getting this
    wrong does not raise - it changes which rows match.

QUERY SHAPE AND WHY:
`tools_sql` replaces three correlated subqueries per skill with one grouped pass.
`skill_calls` has NO index on `skill_id` (measured: `pg_indexes` lists eight indexes on
that table and none of them is on `skill_id`), so three per-skill sequential scans of the
whole call table on every dashboard load would repeat a "COUNT(*) over a whole chain on
every request" cost. The plan is one pass bounded by the visible skill ids.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# peers
# ---------------------------------------------------------------------------
#: Interpolated (not bound) because it is a module constant, never
#: caller input.
DEFAULT_STALE_AFTER_SECS = 20

# --- measured liveness (SD 356, 9 Nov 2026) -------------------------------------------------------------------
# Bxxxxx's view showed almost no seat as active because it showed only what seats PUBLISH: `ws_online` came from
# Redis, which HQD production does not run (so it was false for everyone), and `heartbeat_live` from a beat the
# seat chose to send. Two MEASURED fields now stand beside the published ones, kept apart (pxx):
#   reachable  the seat's listener holds a socket a worker beat within ONLINE_WINDOW_SECS (hub_connections, read
#              through hub_presence.online_condition, the rule call routing uses). A postbox keeps this socket open
#              whether or not an agent session runs, so it is never "live" on its own.
#   active     the seat SENT a mesh message within ACTIVE_WINDOW_SECS, or PUBLISHED state `active` (last_beat)
#              within BEAT_WINDOW_SECS with a TTL that has not run out. Not an idle or asleep beat, and not an old beat with a long
#              TTL (up to a day, apps/peers/presence.py): a session that died after one would read active for a day
#              (pxx P2-2). A seat that keeps beating, every 50 min with a 3600 s TTL, reads active only
#              while it really beats. Not mesh_presence.last_activity: every invoke stamps that, and in the hour to 02:10Z on
#              9 Nov 335 of 391 writes were the transcript uploader's (ctx/ctxm), a daemon (kxxx).
# `liveness` is active, reachable_idle or offline. `presence_stale` marks a published state with no TTL older than
# PRESENCE_NO_TTL_STALE_SECS, or one whose TTL has lapsed: either reads as stale, never as its last state.
# Every age here is from mp.last_beat, the PUBLISHED beat. mp.updated_at is not: apps/calls/reads.touch_last_activity
# updates the row on every invoke, the transcript uploader's included, and creates one (state 'active', last_beat
# NULL) for a peer that never published. A row with no last_beat was never published: self_reported and
# presence_stale are null for it and its state never counts (GitHub Codex :83 :89 :127, pxx).
ACTIVE_WINDOW_SECS = 30 * 60
#: A published beat counts for this long: past the fleet's re-publish rule of at least once an hour (oxxxxxx
#: 0f3d5e), so a seat beating on schedule (every 50 min, TTL 3600) never drops out between beats (pxx).
BEAT_WINDOW_SECS = 65 * 60
PRESENCE_NO_TTL_STALE_SECS = 6 * 3600
#: Mesh threads that only daemons write (tools/ census in tests/test_dashboard_liveness.py): a send there is not a
#: seat's session at work.
DAEMON_THREADS = ("wake-alerts",)

PEERS_SQL = """
{vis}
SELECT json_build_object(
    'id', p.id,
    'name', p.name,
    'display_name', p.display_name,
    'tier', p.tier,
    'status', p.status,
    'registration_status', p.status,
    'trust_level', NULL,
    'last_seen_at', mp.last_beat,
    'presence_age_secs', mon.presence_age_secs,
    'heartbeat_live', COALESCE(mon.heartbeat_live, false),
    'monitor_state', CASE
        WHEN mp.peer_id IS NULL THEN NULL
        WHEN mon.heartbeat_live THEN 'active'
        ELSE 'stale'
    END,
    'monitor_live', CASE
        WHEN mp.peer_id IS NULL THEN NULL
        ELSE mon.heartbeat_live
    END,
    'reachable', hc.reachable,
    'active', act.active,
    'last_sent_at', ls.last_sent_at,
    'liveness', CASE WHEN act.active THEN 'active' WHEN hc.reachable THEN 'reachable_idle' ELSE 'offline' END,
    'presence_stale', CASE
        WHEN mp.peer_id IS NULL OR mp.last_beat IS NULL THEN NULL
        WHEN mp.state_expires_at IS NULL THEN mp.last_beat < NOW() - make_interval(secs => {no_ttl_stale})
        ELSE mp.state_expires_at <= NOW()
    END,
    'self_reported', CASE
        WHEN mp.peer_id IS NULL OR mp.last_beat IS NULL THEN NULL
        ELSE json_build_object('state', mp.state, 'note', mp.note, 'expires_at', mp.state_expires_at,
                               'published_at', mp.last_beat)
    END,
    'agent_type', p.metadata->>'agent_type',
    'tools', (SELECT COUNT(*) FROM skills sk
              WHERE sk.peer_id = p.id AND sk.is_current AND sk.deleted_at IS NULL),
    'models', (SELECT COUNT(*) FROM models md
               WHERE md.peer_id = p.id AND md.is_current AND md.deleted_at IS NULL),
    'created_at', p.created_at,
    'updated_at', p.updated_at
) AS obj
FROM peers p
LEFT JOIN mesh_presence mp ON mp.peer_id = p.id
LEFT JOIN LATERAL (
    SELECT
        CASE
            WHEN mp.last_beat IS NULL THEN NULL
            ELSE EXTRACT(EPOCH FROM (NOW() - mp.last_beat))::bigint
        END AS presence_age_secs,
        CASE
            WHEN mp.last_beat IS NULL THEN NULL
            ELSE EXTRACT(EPOCH FROM (NOW() - mp.last_beat))::bigint <= {stale}
        END AS heartbeat_live
) mon ON TRUE
LEFT JOIN LATERAL (
    -- the fleet's own last-send read (apps/mesh/work.py LAST_SENT_SQL): a narrow key range on the head index,
    -- message keys being msg/<epoch ms>-..., then the writer. Only a successful write is a head
    SELECT max(sm.created_at) AS last_sent_at
    FROM shared_memory sm
    WHERE sm.namespace = %(mesh_ns)s AND sm.is_head
      AND sm.key >= %(sent_from)s AND sm.key < 'msg/9'
      AND sm.created_by_peer_id = p.id
      AND sm.created_at >= NOW() - make_interval(secs => {active_secs})
      AND NOT (COALESCE(sm.value->>'thread_id', '') = ANY (%(daemon_threads)s))
) ls ON TRUE
LEFT JOIN LATERAL (SELECT {online} AS reachable) hc ON TRUE
LEFT JOIN LATERAL (
    SELECT (ls.last_sent_at IS NOT NULL
            OR (mp.last_beat IS NOT NULL AND mp.state = 'active'
                AND mp.state_expires_at IS NOT NULL AND mp.state_expires_at > NOW()
                AND mp.last_beat >= NOW() - make_interval(secs => {beat_secs}))) AS active
) act ON TRUE
WHERE p.status <> 'deleted'
  AND (
    p.owner_id = %(owner)s
    OR p.id IN (SELECT created_by_peer_id FROM vis_peers)
  )
ORDER BY p.created_at
"""
# The two per-peer COUNT(*)s above are bounded by
# `skills_one_current_per_name btree (peer_id, name) WHERE is_current AND deleted_at IS
# NULL` and the matching `models_one_current_per_name` (measured in pg_indexes), so each
# is an index range read over one peer's rows - not a table aggregate.


def peers_sql(vis: str) -> str:
    from apps.wsgw.hub_presence import online_condition
    # The store's own rule text; its one placeholder is the window, bound here by name (this query's style).
    online = online_condition("p.id").replace("%s", "%(online_secs)s")
    return PEERS_SQL.format(vis=vis, stale=DEFAULT_STALE_AFTER_SECS, online=online,
                            active_secs=ACTIVE_WINDOW_SECS, beat_secs=BEAT_WINDOW_SECS,
                            no_ttl_stale=PRESENCE_NO_TTL_STALE_SECS)


def peers_params(base: dict, now_ms: int) -> dict:
    """The peers query's own parameters beside the visibility scope's."""
    from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
    from apps.wsgw.hub_presence import ONLINE_WINDOW_SECS
    return {**base, "online_secs": ONLINE_WINDOW_SECS, "mesh_ns": MESH_CHAT_NAMESPACE,
            "sent_from": "msg/%d" % (now_ms - ACTIVE_WINDOW_SECS * 1000), "daemon_threads": list(DAEMON_THREADS)}


# ---------------------------------------------------------------------------
# dispatch nodes
# ---------------------------------------------------------------------------
DISPATCH_NODES_SQL = """
{vis}
SELECT json_build_object(
    'label', COALESCE(NULLIF(p.display_name, ''), p.name, p.id::text),
    'peer_id', p.id,
    'last_seen_secs', CASE
        WHEN mp.last_beat IS NULL THEN NULL
        ELSE EXTRACT(EPOCH FROM (NOW() - mp.last_beat))::bigint
    END,
    'state', CASE
        WHEN mp.state IN ('active', 'idle', 'asleep') THEN mp.state
        ELSE 'unknown'
    END,
    'live', COALESCE(mp.last_beat > NOW() - INTERVAL '120 seconds', false),
    'dispatch', CASE
        WHEN mon.id IS NULL THEN NULL
        ELSE json_build_object(
            'state', CASE
                WHEN mon.value->>'state' IN ('queued', 'running', 'done', 'failed', 'parked')
                    THEN mon.value->>'state'
                ELSE 'unknown'
            END,
            'live',
                (mon.value->>'state' IN ('queued', 'running')
                 AND mon.created_at > NOW() - INTERVAL '300 seconds'),
            'age_secs', CASE
                WHEN mon.created_at IS NULL THEN NULL
                ELSE EXTRACT(EPOCH FROM (NOW() - mon.created_at))::bigint
            END,
            'dispatch_id', mon.value->>'dispatch_id',
            'reason', mon.value->>'reason',
            'cli', mon.value->>'cli',
            'brain_state', mon.value->>'brain_state',
            'at', mon.value->>'at'
        )
    END
) AS obj
FROM peers p
LEFT JOIN mesh_presence mp ON mp.peer_id = p.id
LEFT JOIN LATERAL (
    SELECT sm.id, sm.value, sm.created_at
    FROM shared_memory sm
    WHERE sm.namespace = 'shared/monitor'
      AND sm.namespace IN (SELECT namespace FROM vis)
      AND sm.is_head
      AND sm.key IN (p.name, p.display_name)
    ORDER BY sm.created_at DESC
    LIMIT 1
) mon ON TRUE
WHERE p.status <> 'deleted'
  AND (
    p.owner_id = %(owner)s
    OR p.id IN (SELECT created_by_peer_id FROM vis_peers)
  )
ORDER BY
    COALESCE(mp.last_beat > NOW() - INTERVAL '120 seconds', false) DESC,
    CASE
        WHEN mp.state = 'active' THEN 0
        WHEN mp.state = 'idle' THEN 1
        WHEN mp.state = 'asleep' THEN 2
        ELSE 3
    END,
    COALESCE(NULLIF(p.display_name, ''), p.name, p.id::text)
"""


def dispatch_nodes_sql(vis: str) -> str:
    return DISPATCH_NODES_SQL.format(vis=vis)


# ---------------------------------------------------------------------------
# namespaces
# ---------------------------------------------------------------------------
NAMESPACES_SQL = """
{vis}
SELECT json_build_object(
    'id', n.namespace,
    'name', n.namespace,
    'display_name', n.namespace,
    'memory_count', (
        SELECT COUNT(*) FROM shared_memory s
        WHERE s.namespace = n.namespace AND s.is_head
    ),
    'visibility', 'shared',
    'namespace_type', 'shared',
    'owner_peer_id', n.owner_peer_id
) AS obj
FROM shared_memory_namespace n
WHERE n.namespace IN (SELECT namespace FROM vis)
ORDER BY n.namespace
"""
# memory_count is an index range read on
# `shared_memory_head_unique btree (namespace, key) WHERE is_head` - one namespace's
# heads, not the table.


def namespaces_sql(vis: str) -> str:
    return NAMESPACES_SQL.format(vis=vis)


# ---------------------------------------------------------------------------
# memories and their shared projection
# ---------------------------------------------------------------------------
MEMORY_OBJ = """json_build_object(
    'id', sm.id,
    'tier', CASE WHEN sm.status = 'CONFIRMED' THEN 1 WHEN sm.status IN ('OPEN','PROPOSED_FIX') THEN 2 WHEN sm.status IN ('REFUTED','WONTFIX') THEN 4 ELSE 3 END,
    'category', COALESCE( (SELECT t FROM unnest(sm.tags) AS t WHERE t NOT IN ('wiki','confirmed','reconciled','meta') AND t NOT LIKE '%%-side' AND t NOT LIKE 'pattern:%%' AND t NOT LIKE 'chain:%%' AND t NOT LIKE 'project:%%' LIMIT 1), sm.status, 'note'),
    'title', COALESCE(sm.value->>'title', sm.key),
    'content', COALESCE(sm.value->>'content', sm.value->>'body', sm.value->>'text', sm.value::text),
    'tags', to_jsonb(sm.tags),
    'source_peer_id', sm.created_by_peer_id,
    'confidence', CASE WHEN sm.status = 'CONFIRMED' THEN '0.9' WHEN sm.status = 'REFUTED' THEN '0.1' WHEN sm.status = 'OPEN' THEN '0.5' ELSE '0.6' END,
    'access_count', CASE WHEN sm.value->>'access_count' ~ '^[0-9]+$' THEN (sm.value->>'access_count')::int ELSE 0 END,
    'confirmed_by', COALESCE(sm.value->'confirmed_by', '0'::jsonb),
    'created_at', sm.created_at
) AS obj"""

MEMORIES_SQL = """
{vis}
SELECT {obj}
FROM shared_memory sm
WHERE sm.is_head
  AND sm.namespace IN (SELECT namespace FROM vis)
  AND (%(namespace_id)s::text IS NULL OR sm.namespace = %(namespace_id)s)
  AND (%(source_peer_id)s::uuid IS NULL OR sm.created_by_peer_id = %(source_peer_id)s)
  AND (%(category)s::text IS NULL OR sm.tags @> ARRAY[%(category)s]::text[])
ORDER BY sm.created_at DESC
LIMIT {limit}
"""
# One projection is used here so list/search/export/duplicates cannot drift apart.


def memories_sql(vis: str, limit: int) -> str:
    return MEMORIES_SQL.format(vis=vis, obj=MEMORY_OBJ, limit=int(limit))


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------
MESSAGES_SQL = """
{vis}
SELECT json_build_object(
    'id', sm.id,
    'from_peer_id', sm.created_by_peer_id,
    'to_peer_id', COALESCE(sm.value->>'to', ''),
    'to_peer_ids', COALESCE(sm.value->'to_peer_ids', '[]'::jsonb),
    'title', COALESCE(sm.value->>'title', sm.value->>'subject'),
    'subject', COALESCE(sm.value->>'title', sm.value->>'subject'),
    'body', COALESCE(sm.value->>'body', sm.value->>'content', sm.value->>'text', ''),
    'content_type', sm.value->>'content_type',
    'thread_id', sm.value->>'thread_id',
    'reply_to_id', sm.value->>'reply_to_id',
    'read_at', dms.read_at,
    'archived_at', dms.archived_at,
    'created_at', sm.created_at
) AS obj
FROM shared_memory sm
LEFT JOIN dashboard_message_state dms
  ON dms.message_id = sm.id AND dms.owner_id = %(owner)s
WHERE sm.is_head
  AND sm.key LIKE 'msg/%%'
  AND sm.namespace IN (SELECT namespace FROM vis)
  AND dms.deleted_at IS NULL
  AND (%(thread_id)s::text IS NULL OR sm.value->>'thread_id' = %(thread_id)s)
ORDER BY sm.created_at DESC
LIMIT {limit} OFFSET {offset}
"""
# `dms.deleted_at IS NULL` on a LEFT JOIN is deliberate: a NULL from the absent join row
# satisfies IS NULL, so an un-deleted message
# with no state row still matches.


def messages_sql(vis: str, limit: int, offset: int) -> str:
    return MESSAGES_SQL.format(vis=vis, limit=int(limit), offset=int(offset))


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
STATS_SQL = """
{vis}
SELECT
  (SELECT COUNT(*) FROM peers p
     WHERE p.status <> 'deleted'
       AND (p.owner_id = %(owner)s
            OR p.id IN (SELECT created_by_peer_id FROM vis_peers))) AS peers,
  (SELECT COUNT(*) FROM shared_memory
     WHERE is_head AND namespace IN (SELECT namespace FROM vis)) AS memories,
  (SELECT COUNT(*) FROM shared_memory
     WHERE is_head AND key LIKE 'msg/%%'
       AND namespace IN (SELECT namespace FROM vis)) AS messages,
  (SELECT COUNT(*) FROM skills s JOIN peers p ON p.id = s.peer_id
     WHERE s.is_current AND s.deleted_at IS NULL AND p.status <> 'deleted'
       AND (p.owner_id = %(owner)s
            OR s.peer_id IN (SELECT created_by_peer_id FROM vis_peers))) AS tools,
  (SELECT COUNT(*) FROM models md JOIN peers p ON p.id = md.peer_id
     WHERE md.is_current AND md.deleted_at IS NULL AND p.status <> 'deleted'
       AND (p.owner_id = %(owner)s
            OR md.peer_id IN (SELECT created_by_peer_id FROM vis_peers))) AS models
"""


def stats_sql(vis: str) -> str:
    return STATS_SQL.format(vis=vis)


# ---------------------------------------------------------------------------
# tools, with grouped call statistics.
# ---------------------------------------------------------------------------
TOOLS_SQL = """
{vis},
visible_skills AS (
    SELECT s.id, s.name, s.description, s.version, s.peer_id, s.mode, s.share_mode,
           s.input_schema, s.runtime_profile, s.runtime_status, s.tags,
           s.created_at, s.updated_at,
           COALESCE(p.display_name, p.name) AS peer_name
    FROM skills s
    JOIN peers p ON p.id = s.peer_id
    WHERE s.is_current AND s.deleted_at IS NULL
      AND p.status <> 'deleted'
      AND (
        p.owner_id = %(owner)s
        OR s.peer_id IN (SELECT created_by_peer_id FROM vis_peers)
      )
),
call_stats AS (
    SELECT c.skill_id,
           COUNT(*) AS total_calls,
           CASE WHEN COUNT(*) = 0 THEN 0
                ELSE ROUND(100.0 * COUNT(*) FILTER (WHERE c.status = 'completed') / COUNT(*))
           END AS success_rate,
           COALESCE(ROUND(AVG(
               EXTRACT(EPOCH FROM (c.completed_at - c.started_at)) * 1000
           ) FILTER (WHERE c.status = 'completed'
                       AND c.completed_at IS NOT NULL
                       AND c.started_at IS NOT NULL)), 0) AS avg_latency_ms
    FROM skill_calls c
    WHERE c.skill_id IN (SELECT id FROM visible_skills)
    GROUP BY c.skill_id
)
SELECT json_build_object(
    'id', s.id,
    'name', s.name,
    'description', s.description,
    'version', s.version,
    'peer_id', s.peer_id,
    'peer_name', s.peer_name,
    'category', COALESCE(NULLIF(s.tags[1], ''), s.mode),
    'mode', s.mode,
    'share_mode', s.share_mode,
    'input_schema', s.input_schema,
    'runtime_profile', s.runtime_profile,
    'runtime_status', s.runtime_status,
    'total_calls', COALESCE(cs.total_calls, 0),
    'success_rate', COALESCE(cs.success_rate, 0),
    'avg_latency_ms', COALESCE(cs.avg_latency_ms, 0),
    'created_at', s.created_at,
    'updated_at', s.updated_at
) AS obj
FROM visible_skills s
LEFT JOIN call_stats cs ON cs.skill_id = s.id
ORDER BY s.name, s.version
"""
# `total_calls` is 0 for a skill with no calls because the LEFT JOIN misses and COALESCE
# supplies 0. `success_rate` is likewise 0 for a zero-call skill. `avg_latency_ms` applies
# its three-part completed-row filter in a FILTER clause, allowing all statistics to share
# the single pass.


def tools_sql(vis: str) -> str:
    return TOOLS_SQL.format(vis=vis)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
MODELS_SQL = """
{vis}
SELECT json_build_object(
    'id', m.id,
    'name', COALESCE(m.display_name, m.name),
    'provider', m.provider,
    'peer_id', m.peer_id,
    'peer_name', COALESCE(p.display_name, p.name),
    'capabilities', to_jsonb(m.capabilities),
    'context_window', m.context_window,
    'cost_per_1k_input', m.cost_per_1k_input,
    'cost_per_1k_output', m.cost_per_1k_output,
    'vram_gb', m.vram_gb,
    'port', CASE WHEN p.owner_id = %(owner)s OR (SELECT is_admin FROM caller_admin)
                 THEN m.port ELSE NULL END,
    'runtime_profile', m.runtime_profile,
    'runtime_status', m.runtime_status,
    'health', (
        SELECT sm.value
        FROM shared_memory sm
        WHERE sm.namespace = 'shared/model-health'
          AND sm.key = 'model/' || m.id::text
          AND sm.is_head
        LIMIT 1
    ),
    'created_at', m.created_at,
    'updated_at', m.updated_at
) AS obj
FROM models m
JOIN peers p ON p.id = m.peer_id
WHERE m.is_current AND m.deleted_at IS NULL
  AND p.status <> 'deleted'
  AND (
    p.owner_id = %(owner)s
    OR m.peer_id IN (SELECT created_by_peer_id FROM vis_peers)
  )
ORDER BY m.name, m.version
"""
# `caller_admin` is referenced by the port-redaction arm, so this handler REQUIRES
# `vis_cte()` and would fail to compile against `content_vis_cte()`, which has no such
# CTE. That is a useful accident: the port redaction cannot be silently moved onto the
# content scope.


def models_sql(vis: str) -> str:
    return MODELS_SQL.format(vis=vis)


# ---------------------------------------------------------------------------
# usage. THE RECORDED LEAK.
# ---------------------------------------------------------------------------
# `%(global_mode)s`: TRUE short-circuits the tenant filter to a
# no-op. The DEFAULT IS FALSE. Shipping global-by-default "leaked
# confidential per-owner cost/usage data across tenants to any authenticated dashboard
# owner, including anonymously self-minted product accounts". The env var is opt-IN,
# its name is MESHYCHAT_ALLOW_GLOBAL_USAGE, and the old inverted flag
# (AGENTMESHY_USAGE_PER_TENANT, OFF = global) is deliberately NOT honoured here: a
# deployment that still sets the old name must not silently get the old behaviour.
USAGE_SQL = """
WITH expected AS (
    SELECT owner_owner_id AS owner_id
      FROM shared_memory_namespace WHERE namespace = 'shared/agent-usage'
       AND (%(global_mode)s::boolean OR meshychat_same_tenant_or_devgroup(%(owner)s, owner_owner_id))
    UNION
    SELECT grantee_owner_id
      FROM shared_memory_grant
     WHERE namespace = 'shared/agent-usage' AND revoked = false
       AND (expires_at IS NULL OR expires_at > NOW())
       AND (%(global_mode)s::boolean OR meshychat_same_tenant_or_devgroup(%(owner)s, grantee_owner_id))
),
rows AS (
    SELECT created_by_owner_id AS owner_id, key, value, created_at, created_by_peer_id
      FROM shared_memory
     WHERE namespace = 'shared/agent-usage' AND is_head AND key LIKE 'usage/%%'
       AND (%(global_mode)s::boolean OR meshychat_same_tenant_or_devgroup(%(owner)s, created_by_owner_id))
)
SELECT json_build_object(
    'network', (SELECT json_build_object(
        'est_cost_usd',    COALESCE(SUM((value->'totals'->>'est_cost_usd')::numeric), 0),
        'output_tokens',   COALESCE(SUM((value->'totals'->>'output')::numeric), 0),
        'input_tokens',    COALESCE(SUM((value->'totals'->>'input')::numeric), 0),
        'cache_read',      COALESCE(SUM((value->'totals'->>'cache_read')::numeric), 0),
        'cache_write',     COALESCE(SUM((value->'totals'->>'cache_write')::numeric), 0),
        'turns',           COALESCE(SUM((value->'totals'->>'turns')::numeric), 0),
        'reporting_peers', (SELECT COUNT(*) FROM rows),
        'total_peers',     (SELECT COUNT(*) FROM expected)
    ) FROM rows),
    'peers', COALESCE((
        SELECT json_agg(json_build_object(
            'peer',        COALESCE(NULLIF(split_part(r.key, '/', 2), ''), pp.peer_name),
            'peer_name',   pp.peer_name,
            'peer_id',     COALESCE(r.created_by_peer_id, pp.peer_id),
            'owner_id',    e.owner_id,
            'reported',    (r.owner_id IS NOT NULL),
            'updated_at',  r.created_at,
            'window_days', r.value->'window_days',
            'totals',      r.value->'totals',
            'per_model',   r.value->'per_model',
            'per_day',     r.value->'per_day',
            'headroom',    CASE WHEN mp.note ~ 'q:hr=' THEN json_build_object(
                'headroom',    (regexp_match(mp.note, 'hr=([0-9.]+)'))[1]::numeric,
                'status',      (regexp_match(mp.note, 'st=([a-z_]+)'))[1],
                'session_5h',  (regexp_match(mp.note, '5h=([0-9.]+)'))[1]::numeric,
                'weekly',      (regexp_match(mp.note, 'wk=([0-9.]+)'))[1]::numeric,
                'eta_minutes', (regexp_match(mp.note, 'eta=([0-9.]+)'))[1]::numeric
            ) ELSE NULL END,
            'attribution', r.value->'attribution'
        ) ORDER BY pp.peer_name NULLS LAST)
        FROM expected e
        LEFT JOIN rows r ON r.owner_id = e.owner_id
        LEFT JOIN LATERAL (
            SELECT id AS peer_id, COALESCE(display_name, name) AS peer_name
              FROM peers
             WHERE owner_id = e.owner_id AND status <> 'deleted'
             ORDER BY (id = r.created_by_peer_id) DESC NULLS LAST, created_at
             LIMIT 1
        ) pp ON TRUE
        LEFT JOIN mesh_presence mp ON mp.peer_id = COALESCE(r.created_by_peer_id, pp.peer_id)
    ), '[]'::json)
) AS obj
"""


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------
NOTIFICATIONS_SQL = """
{vis},
owned_labels AS (
    SELECT lower(p.name) AS label
    FROM peers p
    WHERE p.owner_id = %(owner)s AND p.status <> 'deleted'
    UNION
    SELECT lower(p.display_name) AS label
    FROM peers p
    WHERE p.owner_id = %(owner)s AND p.status <> 'deleted' AND p.display_name IS NOT NULL
)
SELECT json_build_object(
    'id', sm.id,
    'message_id', sm.id,
    'namespace', sm.namespace,
    'key', sm.key,
    'type', COALESCE(sm.value->>'type', 'message'),
    'from', COALESCE(sm.value->>'from', sm.author_label, ''),
    'to', COALESCE(sm.value->>'to', ''),
    'body', COALESCE(sm.value->>'body', sm.value->>'content', sm.value->>'text', ''),
    'thread_id', sm.value->>'thread_id',
    'requires_ack', true,
    'read_at', ns.read_at,
    'created_at', sm.created_at
) AS obj
FROM shared_memory sm
LEFT JOIN dashboard_notification_state ns
       ON ns.owner_id = %(owner)s AND ns.message_id = sm.id
WHERE sm.is_head
  AND sm.key LIKE 'msg/%%'
  AND sm.namespace IN (SELECT namespace FROM vis)
  AND sm.created_by_owner_id <> %(owner)s
  AND sm.value->>'requires_ack' = 'true'
  AND ns.dismissed_at IS NULL
  AND lower(COALESCE(sm.value->>'to', '')) IN (
      SELECT label FROM owned_labels
      UNION SELECT 'all'
  )
  {unread_clause}
ORDER BY sm.created_at DESC
LIMIT 200
"""


def notifications_sql(vis: str, unread_only: bool) -> str:
    """The COUNT endpoint reports UNREAD only; the list
    returns every non-dismissed row. One builder, one predicate difference, so the two
    endpoints cannot disagree about what a notification is."""
    return NOTIFICATIONS_SQL.format(
        vis=vis, unread_clause="AND ns.read_at IS NULL" if unread_only else "")


# ---------------------------------------------------------------------------
# bridges
# ---------------------------------------------------------------------------
BRIDGES_SQL = """
{vis}
SELECT json_build_object(
    'id', b.id,
    'source_namespace_id', b.source_namespace_id,
    'target_namespace_id', b.target_namespace_id,
    'bridge_type', b.bridge_type,
    'filters', b.filters,
    'created_by', b.created_by_owner_id,
    'active', b.active,
    'created_at', b.created_at
) AS obj
FROM dashboard_memory_bridges b
WHERE b.source_namespace_id IN (SELECT namespace FROM vis)
  AND b.target_namespace_id IN (SELECT namespace FROM vis)
ORDER BY b.created_at DESC
"""


def bridges_sql(vis: str) -> str:
    return BRIDGES_SQL.format(vis=vis)


# ---------------------------------------------------------------------------
# memory explorer
# ---------------------------------------------------------------------------
MEMORY_PEERS_SQL = """
{vis}
SELECT DISTINCT p.id, p.name, p.display_name
FROM shared_memory sm
JOIN peers p ON p.id = sm.created_by_peer_id
WHERE sm.is_head AND sm.namespace IN (SELECT namespace FROM vis)
ORDER BY p.name
"""


def memory_peers_sql(vis: str) -> str:
    return MEMORY_PEERS_SQL.format(vis=vis)


MEMORY_SEARCH_SQL = """
{vis}
SELECT {obj}
FROM shared_memory sm
WHERE sm.is_head
  AND sm.namespace IN (SELECT namespace FROM vis)
  AND (%(namespace_id)s::text IS NULL OR sm.namespace = %(namespace_id)s)
  AND (%(source_peer_id)s::uuid IS NULL OR sm.created_by_peer_id = %(source_peer_id)s)
  AND (%(category)s::text IS NULL OR sm.tags @> ARRAY[%(category)s]::text[])
  AND (%(q)s::text IS NULL OR sm.id IN (
        SELECT id FROM shared_memory WHERE meshychat_jsonb_values_text(value) ILIKE '%%' || %(q)s || '%%'
        UNION
        SELECT id FROM shared_memory
         WHERE EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ILIKE '%%' || %(q)s || '%%')
  ))
ORDER BY sm.created_at DESC
LIMIT {limit}
"""
# The UNION is load-bearing and is NOT a stylistic choice to tidy into an OR:
# Postgres can only BitmapOr when every arm is indexable, and the tags arm is not served
# by `idx_shared_memory_value_trgm`, so the OR is slower. `meshychat_jsonb_values_text`
# matches VALUES only, never JSON KEY NAMES - matching `value::text` instead made
# `q=title` return every row that HAS a title field (126 rows against 35).


def memory_search_sql(vis: str, limit: int) -> str:
    return MEMORY_SEARCH_SQL.format(vis=vis, obj=MEMORY_OBJ, limit=int(limit))


MEMORY_EXPORT_SQL = """
{vis}
SELECT {obj}
FROM shared_memory sm
WHERE sm.is_head AND sm.namespace IN (SELECT namespace FROM vis)
ORDER BY sm.created_at DESC
LIMIT 5000
"""


def memory_export_sql(vis: str) -> str:
    return MEMORY_EXPORT_SQL.format(vis=vis, obj=MEMORY_OBJ)


MEMORY_DUPLICATES_SQL = """
{vis},
heads AS (
    SELECT * FROM shared_memory sm0
    WHERE sm0.is_head AND sm0.namespace IN (SELECT namespace FROM vis)
),
dups AS (
    SELECT lower(btrim(COALESCE(value->>'title', key))) AS norm
    FROM heads
    GROUP BY 1
    HAVING count(*) > 1
)
SELECT {obj}
FROM heads sm
WHERE lower(btrim(COALESCE(sm.value->>'title', sm.key))) IN (SELECT norm FROM dups)
ORDER BY lower(btrim(COALESCE(sm.value->>'title', sm.key))), sm.created_at DESC
LIMIT {limit}
"""


def memory_duplicates_sql(vis: str, limit: int) -> str:
    return MEMORY_DUPLICATES_SQL.format(vis=vis, obj=MEMORY_OBJ, limit=int(limit))


# ---------------------------------------------------------------------------
# node logs. Owner-scoped, NOT co-author-broadened.
# ---------------------------------------------------------------------------
LOGS_LIST_SQL = """
SELECT json_build_object(
    'id', l.id,
    'peer_id', l.peer_id,
    'peer_name', COALESCE(p.display_name, p.name),
    'level', l.level,
    'source', l.source,
    'message', l.message,
    'meta', l.meta,
    'created_at', l.created_at
) AS obj
FROM dashboard_node_logs l
JOIN peers p ON p.id = l.peer_id
WHERE l.owner_id = %(owner)s
  AND (%(peer_id)s::uuid IS NULL OR l.peer_id = %(peer_id)s)
  AND (%(level)s::text IS NULL OR l.level = %(level)s)
  AND (%(pattern)s::text IS NULL
       OR l.message ILIKE %(pattern)s ESCAPE '\\'
       OR COALESCE(l.source, '') ILIKE %(pattern)s ESCAPE '\\')
  AND (%(before)s::timestamptz IS NULL OR l.created_at < %(before)s)
ORDER BY l.created_at DESC, l.id DESC
LIMIT %(limit)s
"""

LOGS_RETENTION_READ_SQL = (
    "SELECT retention_days::bigint AS days FROM dashboard_log_settings "
    "WHERE owner_id = %(owner)s"
)

#: Lazy retention on the READ path keeps the window without a
#: background sweeper. Bounded by `dashboard_node_logs_owner_created_idx (owner_id,
#: created_at DESC)`, so it is an index range delete for one owner, not a table sweep.
LOGS_EXPIRE_SQL = (
    "DELETE FROM dashboard_node_logs "
    "WHERE owner_id = %(owner)s "
    "  AND created_at < NOW() - make_interval(days => %(days)s::int)"
)


# ---------------------------------------------------------------------------
# tier-info
# ---------------------------------------------------------------------------
LINKED_AGENTS_SQL = (
    "SELECT COUNT(*) AS linked FROM peers WHERE owner_id = %(owner)s AND status <> 'deleted'"
)

#: The effective tier is `dashboard_users.tier`, and a missing row is an error rather than
#: a default. That distinction survives here: the
#: view raises rather than inventing "free" for an owner that does not exist.
OWNER_TIER_SQL = "SELECT tier FROM dashboard_users WHERE id = %(owner)s"

#: The override-then-tier-default lookup is one statement. A NULL `value` means neither
#: source had a row; the row is always returned (the `names` CTE is a
#: literal), so "no row at all" and "no cap defined" cannot be confused.
#:
#: This constant exists so there is exactly ONE spelling of cap resolution on this
#: surface. `views._resolve_cap` is its only caller and the entitlements gate lives
#: there; a second hand-rolled copy of this query is how `/logs/settings` came to answer
#: 200 for an owner Accounts had revoked.
RESOLVE_CAP_SQL = """
WITH names AS (SELECT %(cap)s::text AS capability)
SELECT COALESCE(o.value, t.value) AS value
FROM names n
LEFT JOIN owner_tier_overrides o
       ON o.owner_id = %(owner)s AND o.capability = n.capability
LEFT JOIN tier_capabilities t
       ON t.tier = %(tier)s AND t.capability = n.capability
"""

#: The capability snapshot is ONE statement: resolving a cap plus current usage per
#: capability would otherwise make ~4xN sequential DB round trips on a hot dashboard endpoint.
#: The cycle start is the subscription's current_period_start
#: when the subscription is live, else the first of the current month, IN UTC.
#: `%(cycle)s` is `apps.files.quota._cycle_start`, the one function that also decides which cycle
#: a usage unit is COUNTED in. This query used to compute its own month with
#: `date_trunc('month', CURRENT_DATE)`, the database session's LOCAL date, while the counter is
#: written under the UTC month: for the hours between local and UTC midnight at every month's
#: start (two a month at UTC+2) the snapshot read a cycle with no usage and reported 0. Found by
#: test_owner_tier failing across local midnight into October; it failed the same way on main.
CAPS_SNAPSHOT_SQL = """
WITH cycle AS (
    SELECT %(cycle)s::date AS cycle_start
),
names AS (
    SELECT capability FROM tier_capabilities
    UNION
    SELECT capability FROM owner_tier_overrides WHERE owner_id = %(owner)s
)
SELECT n.capability,
       COALESCE(o.value, t.value) AS value,
       COALESCE(u.counter, 0)     AS current
FROM names n
LEFT JOIN owner_tier_overrides o
       ON o.owner_id = %(owner)s AND o.capability = n.capability
LEFT JOIN tier_capabilities t
       ON t.tier = %(tier)s AND t.capability = n.capability
LEFT JOIN owner_usage u
       ON u.owner_id = %(owner)s AND u.capability = n.capability
      AND u.cycle_start = (SELECT cycle_start FROM cycle)
ORDER BY n.capability
"""

# ---------------------------------------------------------------------------
# agents. Owner-scoped, no vis CTE.
# ---------------------------------------------------------------------------
AGENTS_SQL = """
SELECT json_build_object(
    'id', id,
    'peer_id', id,
    'name', name,
    'display_name', display_name,
    'tier', tier,
    'status', status,
    'linked_at', created_at
) AS obj
FROM peers
WHERE owner_id = %(owner)s AND status <> 'deleted'
ORDER BY created_at DESC
"""


# ---------------------------------------------------------------------------
# dashboard documents. Routes apply owner_id in code; these tables have no RLS.
# ---------------------------------------------------------------------------
CLIENTS_SQL = """
SELECT json_build_object('id',id,'name',name,'company',company,'email',email,
    'address',address,'city',city,'state',state,'country',country,'region',region,
    'notes',notes,'created_at',created_at) AS obj
FROM dashboard_clients WHERE owner_id=%(owner)s ORDER BY created_at DESC
"""

INVOICE_OBJ = """json_build_object(
    'id',i.id,'number',i.number,'client_id',i.client_id,
    'client_name',c.name,'client_company',c.company,'client_country',c.country,'client_region',c.region,
    'status',i.status,'issue_date',i.issue_date,'due_date',i.due_date,
    'subtotal_cents',i.subtotal_cents,'tax_rate',i.tax_rate,'tax_cents',i.tax_cents,'total_cents',i.total_cents,
    'notes',i.notes,'stripe_payment_link',i.stripe_payment_link,'paid_at',i.paid_at,'sent_at',i.sent_at,
    'created_at',i.created_at,'updated_at',i.updated_at)"""

INVOICES_SQL = f"""
SELECT {INVOICE_OBJ} AS obj FROM dashboard_invoices i
LEFT JOIN dashboard_clients c ON c.id = i.client_id
WHERE i.owner_id=%(owner)s AND (%(status)s::text IS NULL OR i.status=%(status)s)
ORDER BY i.created_at DESC
"""

#: The ownership predicate is the second clause, not an optional
#: extra: `dashboard_invoice_items` carries NO owner column, so an invoice-id predicate
#: alone would list any owner's line items for a guessed invoice id.
#:
#: A non-owned invoice returns 200 with an empty `items` array, not 404. A 404 here would
#: leak invoice-id existence.
#:
#: `ORDER BY sort_order, ctid` uses a physical-location tiebreak, not a stable key. Rows
#: that share a sort_order come back in physical order, and a different tiebreak would
#: change their order.
INVOICE_ITEMS_SQL = """
SELECT json_build_object('id',id,'invoice_id',invoice_id,'description',description,
    'quantity',quantity,'unit_price_cents',unit_price_cents,
    'amount_cents',amount_cents,'sort_order',sort_order) AS obj
FROM dashboard_invoice_items
WHERE invoice_id = %(invoice_id)s
  AND invoice_id IN (SELECT id FROM dashboard_invoices WHERE owner_id = %(owner)s)
ORDER BY sort_order, ctid
"""

DOCUMENTS_SQL = """
SELECT json_build_object(
    'id',d.id,'number',d.number,'type',d.type,'client_id',d.client_id,
    'client_name',c.name,'client_company',c.company,
    'status',d.status,'template_version',d.template_version,'fields',d.fields,
    'signed_at',d.signed_at,'expires_at',d.expires_at,'created_at',d.created_at,'updated_at',d.updated_at) AS obj
FROM dashboard_documents d
LEFT JOIN dashboard_clients c ON c.id = d.client_id
WHERE d.owner_id=%(owner)s AND (%(doc_type)s::text IS NULL OR d.type=%(doc_type)s)
ORDER BY d.created_at DESC
"""

EXPENSES_SQL = """
SELECT json_build_object('id',id,'date',date,'vendor',vendor,'description',description,
    'category',category,'amount_cents',amount_cents,'currency',currency,'client_id',client_id,
    'invoice_id',invoice_id,'is_billable',is_billable,'is_deductible',is_deductible,
    'receipt_url',receipt_url,'notes',notes,'created_at',created_at) AS obj
FROM dashboard_expenses
WHERE owner_id=%(owner)s
  AND (%(from)s::date IS NULL OR date >= %(from)s::date)
  AND (%(to)s::date IS NULL OR date <= %(to)s::date)
  AND (%(category)s::text IS NULL OR category=%(category)s)
ORDER BY date DESC, created_at DESC
"""

PAYOUTS_SQL = """
SELECT json_build_object(
    'id',id,'contractor',contractor,'period_start',period_start,'period_end',period_end,'status',status,
    'americas_gross_cents',americas_gross_cents,'americas_deductions_cents',americas_deductions_cents,
    'americas_net_cents',americas_net_cents,'americas_platform_share_cents',americas_platform_share_cents,
    'americas_contractor_share_cents',americas_contractor_share_cents,
    'europe_gross_cents',europe_gross_cents,'europe_deductions_cents',europe_deductions_cents,
    'europe_net_cents',europe_net_cents,'europe_platform_share_cents',europe_platform_share_cents,
    'europe_contractor_share_cents',europe_contractor_share_cents,
    'asia_gross_cents',asia_gross_cents,'asia_deductions_cents',asia_deductions_cents,
    'asia_net_cents',asia_net_cents,'asia_platform_share_cents',asia_platform_share_cents,
    'asia_contractor_share_cents',asia_contractor_share_cents,
    'total_owed_cents',total_owed_cents,'paid_at',paid_at,'payment_method',payment_method,
    'notes',notes,'created_at',created_at) AS obj
FROM dashboard_payouts WHERE owner_id=%(owner)s ORDER BY created_at DESC
"""

TAX_REPORT_SQL = """
WITH params AS (
    SELECT COALESCE(%(year)s::int, EXTRACT(YEAR FROM CURRENT_DATE)::int) AS yr
),
inv AS (
    SELECT COALESCE(SUM(total_cents),0)::bigint AS invoiced,
           COALESCE(SUM(total_cents) FILTER (WHERE paid_at IS NOT NULL),0)::bigint AS collected,
           COUNT(*)::bigint AS cnt
    FROM dashboard_invoices, params
    WHERE owner_id=%(owner)s AND EXTRACT(YEAR FROM issue_date)::int = params.yr
),
exp_cat AS (
    SELECT category,
           COALESCE(SUM(amount_cents),0)::bigint AS total,
           COUNT(*)::bigint AS cnt,
           COALESCE(SUM(amount_cents) FILTER (WHERE is_deductible),0)::bigint AS deductible
    FROM dashboard_expenses, params
    WHERE owner_id=%(owner)s AND EXTRACT(YEAR FROM date)::int = params.yr
    GROUP BY category
),
exp_tot AS (
    SELECT COALESCE(SUM(amount_cents),0)::bigint AS total,
           COALESCE(SUM(amount_cents) FILTER (WHERE is_deductible),0)::bigint AS deductible
    FROM dashboard_expenses, params
    WHERE owner_id=%(owner)s AND EXTRACT(YEAR FROM date)::int = params.yr
),
pay AS (
    SELECT COALESCE(SUM(total_owed_cents),0)::bigint AS total
    FROM dashboard_payouts, params
    WHERE owner_id=%(owner)s AND EXTRACT(YEAR FROM period_start)::int = params.yr
)
SELECT json_build_object(
    'year', (SELECT yr FROM params)::text,
    'income', json_build_object(
        'total_invoiced_cents', inv.invoiced,
        'total_collected_cents', inv.collected,
        'outstanding_cents', inv.invoiced - inv.collected,
        'invoice_count', inv.cnt
    ),
    'expenses_by_category', COALESCE((
        SELECT json_agg(json_build_object(
            'category', category,
            'total_cents', total,
            'count', cnt,
            'deductible_cents', deductible,
            'deductible_rate', CASE WHEN total > 0 THEN round(deductible::numeric / total, 4) ELSE 0 END
        ) ORDER BY total DESC) FROM exp_cat
    ), '[]'::json),
    'total_expenses_cents', exp_tot.total,
    'total_deductible_cents', exp_tot.deductible,
    'contractor_payouts_cents', pay.total,
    'estimated_taxable_cents', GREATEST(inv.collected - exp_tot.deductible - pay.total, 0)
) AS obj
FROM inv, exp_tot, pay
"""
