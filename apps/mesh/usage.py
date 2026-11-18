"""Count who uses the mesh read surface, so adoption is a number rather than a hope.

Divergence 018 (d). The graph-chat success metric is inbox adoption against memory.query
volume, and before this nothing counted a single call to /v1/mesh/*. The table is
`sql_meshychat/005_mesh_route_usage.sql`, whose header records why an in-process counter
was rejected (it resets on deploy and is per worker process).

ONE ROW PER (route, UTC day, peer, outcome), incremented by an upsert. Written once per
AUTHENTICATED request, after the request's own read has finished, in a transaction of its
own: a counter that shared the read's transaction could turn a failed count into a failed
read, and a read that failed would roll the count back with it. Unauthenticated requests
(401) are not counted, because there is no peer to attribute them to.

THE COUNTER NEVER FAILS A REQUEST, AND ITS FAILURES ARE NOT SILENT. Same discipline as
apps/memoryskills/permissions.audit_tenancy_denial: an instrument must not turn a served
read into a 500, but a counter that stops counting and says nothing reads as "nobody uses
the inbox", which is the exact misreading this module exists to prevent. So a failed write
is logged at ERROR and added to `WRITE_FAILURES`, and every summary reports that number
beside the counts it may have undercounted.

MEMORY.QUERY IS COUNTED WHERE IT RUNS (STATUS F10; divergence 018 (d), amended 2026-09-26).
Until then this block said memory.query was not dispatched here and reported its count as a
typed null. Step 9 of the call pipeline executes memory calls in-process (apps/calls/route.py,
divergence 017), so a node's `memory.query` DOES run here, in `route._execute`, the one site
that dispatches it; that site calls `record_call` after the handler's own transaction has
ended, into this table under the name MEMORY_QUERY_ROUTE, with this module's never-fail
discipline. `ok` is a query that completed, `denied` one the namespace gate refused
(E_PERMISSION_DENIED, E_NAMESPACE_UNCLAIMED, the codes the graph routes answer 403 for), and
`error` any other failure. Only EXECUTED calls count: an idempotent replay, a call refused
before step 9 and a query sent to another peer never reach `_execute`. `summary` reports it
as a block of its own beside `routes`, with its own `counting_since`, because it is not a
/v1/mesh route and a zero before its first count means "not yet counted".
"""
from __future__ import annotations

import datetime as _dt
import logging

from psycopg.rows import dict_row

from apps.core import dbconn, rls

log = logging.getLogger("meshychat.mesh.usage")

#: The closed route set. Must equal the CHECK in sql_meshychat/005, widened by 013 for
#: `neighbours` (divergence 032), by 017 for the two summary routes (divergence 037),
#: by 035 for `brief`, and by 036 for `unread`; a test compares them.
ROUTES = ("inbox", "owed", "thread", "drift", "wait", "neighbours", "summary_thread",
          "summary_backlog", "mentions", "awaiting", "brief", "unread", "recipients")

#: The name memory.query is counted under (STATUS F10). Not a route: kept out of ROUTES, so
#: `summary` never lists it as one.
MEMORY_QUERY_ROUTE = "memory_query"

#: Notification variants are usage dimensions, not routes. Kept out of ROUTES so `summary`
#: does not present a delivery path as another graph route.
NOTICE_ROUTES = ("brief_notice", "neighbours_notice")

#: The operator routes (divergence 081), counted like the others but kept out of ROUTES, so
#: `summary` does not present them as peer graph routes. Added to the CHECK by 037.
OPERATOR_ROUTES = ("fleet_unread", "fleet_unread_peer")

#: The card routes (issue #238 C1-C2, divergence 101), counted in apps/meshsem/views._route: the card
#: list (GET /v1/cards) and the card search (GET /v1/mesh/search with the card namespace; a search of the
#: mail is not counted here). Kept out of ROUTES, so `summary` does not present them as mail graph routes;
#: `card_summary` and `card_daily` report them, each with its own `counting_since`. Added to the CHECK by 044.
#: Since 045 (issue #238 C3, divergence 103) a read the client makes on its own behalf says so with
#: `purpose=` and is counted under a route of its own (`card_route`), so `cards` and `card_search` keep only
#: the interactive reads: the ones a person or Claude asked for, and any read that names no purpose.
CARD_INTERACTIVE_ROUTES = ("cards", "card_search")

#: The purposes the client sends, per card route, and the route each is counted under. Only the pairs the
#: client makes exist: the session-start briefing and the seat-freshness reads, the read beside a send and the
#: reads before a card write are card lists; the question dialog's look for an earlier card is a search.
CARD_PURPOSE_ROUTES = {
    "cards": {"briefing": "cards_briefing", "send": "cards_send", "write": "cards_write"},
    "card_search": {"dialog": "card_search_dialog"},
}

#: The purpose a read with no `purpose` has, and the one that is not counted at all (a status poll, #238 C3).
CARD_PURPOSE_INTERACTIVE = "interactive"
CARD_PURPOSE_UNCOUNTED = "status"

#: The longest purpose read as one: anything longer is an unknown purpose, never refused.
CARD_PURPOSE_MAX_CHARS = 32

#: Automatic reads, then the reads under a purpose this server does not know: counted, never refused, and
#: never folded into the interactive count.
CARD_AUTOMATIC_ROUTES = ("cards_briefing", "cards_send", "cards_write", "card_search_dialog",
                         "cards_unknown", "card_search_unknown")
CARD_ROUTES = CARD_INTERACTIVE_ROUTES + CARD_AUTOMATIC_ROUTES

#: Transcript-context searches on GET /v1/mesh/search?ns=<context namespace> (TKT-4629), counted in
#: apps/meshsem/views._route with counts only (never query text). Interactive searches (`transcript_search`)
#: are kept apart from the reads the client makes on its own behalf (`purpose=brief|consensus|context`), and
#: any other non-empty `purpose` (including `status`) lands in `transcript_search_unknown` rather than None so
#: the sum across TRANSCRIPT_ROUTES matches authenticated access-log requests for the context namespace.
#: Added to the CHECK by 058.
TRANSCRIPT_INTERACTIVE_ROUTES = ("transcript_search",)
TRANSCRIPT_PURPOSE_ROUTES = {
    "brief": "transcript_search_brief",
    "consensus": "transcript_search_consensus",
    "context": "transcript_search_context",
}
TRANSCRIPT_AUTOMATIC_ROUTES = (
    "transcript_search_brief",
    "transcript_search_consensus",
    "transcript_search_context",
    "transcript_search_unknown",
)
TRANSCRIPT_ROUTES = TRANSCRIPT_INTERACTIVE_ROUTES + TRANSCRIPT_AUTOMATIC_ROUTES

#: Every name the table accepts: must equal 058's CHECK.
COUNTED = ROUTES + (MEMORY_QUERY_ROUTE,) + NOTICE_ROUTES + OPERATOR_ROUTES + CARD_ROUTES + TRANSCRIPT_ROUTES

#: The skills `record_call` counts, each under MEMORY_QUERY_ROUTE (one skill today).
COUNTED_SKILLS = frozenset({"memory.query"})

#: The HandlerError codes that make a counted skill's outcome `denied`: the namespace gate's,
#: the same two the graph routes answer 403 for (apps/mesh/views.py `_DENIED` is this set).
DENIED_CODES = frozenset({"E_PERMISSION_DENIED", "E_NAMESPACE_UNCLAIMED"})

#: The closed outcome set, also a CHECK in sql_meshychat/005.
#:   ok      the route answered (200, or 204 for an idle wait)
#:   denied  refused by the shared/mesh-chat read gate (403)
#:   error   any other refusal after authentication (422 on a bad limit, 500)
OUTCOMES = ("ok", "denied", "error")

#: Days kept by `prune`. Stated in sql_meshychat/005 as the table's retention policy.
RETENTION_DAYS = 90

#: Days `summary` reports by default. Seven, to sit beside the seven-day memory.query
#: volume divergence 014 was measured on.
WINDOW_DAYS = 7

#: Failed counter writes in THIS process since it started. Never reset in-process;
#: monitoring reads deltas (the AUDIT_FAILURES convention).
WRITE_FAILURES = 0

_UPSERT = (
    "INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) "
    "VALUES (%s, (now() AT TIME ZONE 'UTC')::date, %s, %s, 1) "
    "ON CONFLICT (route, day, peer_id, outcome) "
    "DO UPDATE SET calls = mesh_route_usage.calls + 1, last_at = now()"
)


def record(conn, route: str, peer_id, outcome: str) -> bool:
    """Count one request. Returns True when the count landed, False when it did not.

    `conn` must be an autocommit connection with NO open transaction (the request's read
    block has already exited); the count gets a `server_internal` transaction of its own.
    """
    global WRITE_FAILURES
    try:
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute(_UPSERT, (route, str(peer_id), outcome))
        return True
    except Exception as exc:  # an instrument must never fail the request it measures
        WRITE_FAILURES += 1
        log.error("mesh route usage count FAILED (request still served; the count is "
                  "LOST) route=%s outcome=%s failures_total=%d: %s",
                  route, outcome, WRITE_FAILURES, exc, exc_info=True)
        return False


def record_on_own_connection(route: str, peer_id, outcome: str) -> bool:
    """`record` for a caller that holds no connection any more (the long-poll closes its
    request connection before parking). Opening the connection is inside the same
    never-raise contract: a database that went away mid-wait loses the count, logged and
    counted, and does not replace the wait's own response with a 500."""
    global WRITE_FAILURES
    try:
        with dbconn.serving_connection() as conn:
            return record(conn, route, peer_id, outcome)
    except Exception as exc:  # the same contract as record()
        WRITE_FAILURES += 1
        log.error("mesh route usage count FAILED to connect (request still served; the "
                  "count is LOST) route=%s outcome=%s failures_total=%d: %s",
                  route, outcome, WRITE_FAILURES, exc, exc_info=True)
        return False


def record_call(conn, skill_name: str, peer_id, error_code: str | None) -> bool | None:
    """Count one EXECUTED call of a counted skill (COUNTED_SKILLS), after its handler's own
    transaction has ended; None, and nothing written, for any other skill. `error_code` is
    the handler's typed code, or None when the call completed. `conn`: as `record`'s. Never
    raises. MEMORY_QUERY_ROUTE is read at call time, so a test can point it at a name the
    CHECK refuses and watch the failure be counted rather than raised."""
    if skill_name not in COUNTED_SKILLS:
        return None
    outcome = ("ok" if error_code is None
               else "denied" if error_code in DENIED_CODES else "error")
    return record(conn, MEMORY_QUERY_ROUTE, peer_id, outcome)


def _counts(r) -> dict:
    return {
        "calls": int(r["ok_calls"]) if r else 0,
        "peers": int(r["ok_peers"]) if r else 0,
        "denied_calls": int(r["denied_calls"]) if r else 0,
        "denied_peers": int(r["denied_peers"]) if r else 0,
        "error_calls": int(r["error_calls"]) if r else 0,
    }


def summary(conn, *, days: int = WINDOW_DAYS) -> dict:
    """Per route over the last `days` UTC days, today included. Reads under the caller's
    context: the route wrapper already holds `server_internal` (see projection.state).

    Every route in ROUTES appears, zeros included, because a route missing from the answer
    and a route nobody called are different facts and only one of them is a count.
    `counting_since` is the oldest UTC day the table holds: before it, zero means "not yet
    counted", not "not used". Both reads are ranges on `day`, which
    `mesh_route_usage_day_idx` serves (B5: this runs on every /v1/mesh/drift request, so
    it must not aggregate the whole table; min(day) is answerable from the index's
    first entry).
    """
    days = max(1, int(days))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT route, "
            "  COALESCE(sum(calls) FILTER (WHERE outcome = 'ok'), 0)     AS ok_calls, "
            "  count(DISTINCT peer_id) FILTER (WHERE outcome = 'ok')     AS ok_peers, "
            "  COALESCE(sum(calls) FILTER (WHERE outcome = 'denied'), 0) AS denied_calls, "
            "  count(DISTINCT peer_id) FILTER (WHERE outcome = 'denied') AS denied_peers, "
            "  COALESCE(sum(calls) FILTER (WHERE outcome = 'error'), 0)  AS error_calls "
            "FROM mesh_route_usage "
            "WHERE day > (now() AT TIME ZONE 'UTC')::date - %s "
            "GROUP BY route",
            (days,),
        )
        rows = {r["route"]: r for r in cur.fetchall()}
        # The routes' first day: min(day), answered from `mesh_route_usage_day_idx`'s first
        # entries (a memory_query row there is stepped over; the routes were counted first).
        cur.execute("SELECT min(day) AS since FROM mesh_route_usage WHERE route <> %s",
                    (MEMORY_QUERY_ROUTE,))
        since = cur.fetchone()["since"]
        # memory_query's first day: ONE probe of the primary key's (route, day) prefix, its
        # first entry for that route (review finding 2 of E13: not a second aggregate).
        cur.execute("SELECT day AS since FROM mesh_route_usage WHERE route = %s "
                    "ORDER BY day LIMIT 1", (MEMORY_QUERY_ROUTE,))
        first = cur.fetchone()
        mq_since = first["since"] if first else None
    routes = {route: _counts(rows.get(route)) for route in ROUTES}
    return {
        "window_days": days,
        "counting_since": since.isoformat() if isinstance(since, _dt.date) else None,
        "routes": routes,
        "write_failures_this_process": WRITE_FAILURES,
        "memory_query": {
            **_counts(rows.get(MEMORY_QUERY_ROUTE)),
            "counting_since": mq_since.isoformat() if isinstance(mq_since, _dt.date) else None,
            "counted_at": "the call pipeline's step 9 (apps/calls/route.py), every executed "
                          "memory.query; before counting_since a zero means not yet counted",
        },
    }


#: Days `card_daily` may cover. The table keeps RETENTION_DAYS; a longer window would claim days it no
#: longer holds.
CARD_DAILY_MAX_DAYS = RETENTION_DAYS


def _first_day(cur, route: str):
    """A route's first counted UTC day: one probe of the primary key's (route, day) prefix, its first
    entry (review finding 2 of E13: not an aggregate). None when the route was never counted."""
    cur.execute("SELECT day AS since FROM mesh_route_usage WHERE route = %s ORDER BY day LIMIT 1",
                (route,))
    first = cur.fetchone()
    return first["since"] if first else None


def card_route(base: str, purpose) -> str | None:
    """The route a card read is counted under (issue #238 C3, divergence 103): `base` is "cards" or
    "card_search", `purpose` the raw `purpose` parameter (None when absent). No purpose, an empty one or
    "interactive" keeps `base`; "status" is not counted (None); a purpose this route knows gets its own route;
    anything else is `<base>_unknown`. Never raises: the frame asks before anything is validated, and a
    counter's question must never fail the request it counts."""
    p = purpose.strip() if isinstance(purpose, str) else ""
    if not p or p == CARD_PURPOSE_INTERACTIVE:
        return base
    if p == CARD_PURPOSE_UNCOUNTED:
        return None
    known = CARD_PURPOSE_ROUTES.get(base, {})
    if len(p) <= CARD_PURPOSE_MAX_CHARS and p in known:
        return known[p]
    return base + "_unknown"


def _subset_summary(conn, *, routes: tuple[str, ...], interactive_routes: tuple[str, ...],
                    automatic_routes: tuple[str, ...], days: int = WINDOW_DAYS) -> dict:
    """Shared per-route summary over a named subset of routes (`CARD_ROUTES` or `TRANSCRIPT_ROUTES`),
    in `summary`'s shape: every route in `routes` appears, zeros included, with its own `counting_since`."""
    days = max(1, int(days))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT route, "
            "  COALESCE(sum(calls) FILTER (WHERE outcome = 'ok'), 0)     AS ok_calls, "
            "  count(DISTINCT peer_id) FILTER (WHERE outcome = 'ok')     AS ok_peers, "
            "  COALESCE(sum(calls) FILTER (WHERE outcome = 'denied'), 0) AS denied_calls, "
            "  count(DISTINCT peer_id) FILTER (WHERE outcome = 'denied') AS denied_peers, "
            "  COALESCE(sum(calls) FILTER (WHERE outcome = 'error'), 0)  AS error_calls "
            "FROM mesh_route_usage "
            "WHERE route = ANY(%s) AND day > (now() AT TIME ZONE 'UTC')::date - %s "
            "GROUP BY route",
            (list(routes), days),
        )
        rows = {r["route"]: r for r in cur.fetchall()}
        firsts = {route: _first_day(cur, route) for route in routes}
    return {
        "window_days": days,
        "routes": {route: {**_counts(rows.get(route)),
                           "counting_since": firsts[route].isoformat()
                           if isinstance(firsts[route], _dt.date) else None}
                   for route in routes},
        "interactive_routes": list(interactive_routes),
        "automatic_routes": list(automatic_routes),
        "write_failures_this_process": WRITE_FAILURES,
    }


def _subset_daily(conn, *, routes: tuple[str, ...], interactive_routes: tuple[str, ...],
                  automatic_routes: tuple[str, ...], days: int = WINDOW_DAYS,
                  max_days: int = RETENTION_DAYS) -> dict:
    """Shared per-seat, per-UTC-day, per-route breakdown over a named subset of routes, grouped by
    `peer_id` as well as `seat` so two peers sharing a `peers.name` stay two rows rather than one sum."""
    days = min(max(1, int(days)), max_days)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT u.day, u.peer_id, COALESCE(p.name, 'peer-' || left(u.peer_id::text, 8)) AS seat, u.route, "
            "  COALESCE(sum(u.calls) FILTER (WHERE u.outcome = 'ok'), 0)     AS ok, "
            "  COALESCE(sum(u.calls) FILTER (WHERE u.outcome = 'denied'), 0) AS denied, "
            "  COALESCE(sum(u.calls) FILTER (WHERE u.outcome = 'error'), 0)  AS error "
            "FROM mesh_route_usage u LEFT JOIN peers p ON p.id = u.peer_id "
            "WHERE u.route = ANY(%s) AND u.day > (now() AT TIME ZONE 'UTC')::date - %s "
            "GROUP BY u.day, u.peer_id, seat, u.route "
            "ORDER BY u.day, seat, u.peer_id, u.route",
            (list(routes), days),
        )
        rows = cur.fetchall()
        firsts = [d for d in (_first_day(cur, route) for route in routes) if d is not None]
    since = min(firsts) if firsts else None
    return {
        "window_days": days,
        "counting_since": since.isoformat() if isinstance(since, _dt.date) else None,
        "routes": list(routes),
        "interactive_routes": list(interactive_routes),
        "automatic_routes": list(automatic_routes),
        "rows": [{"day": r["day"].isoformat(), "seat": r["seat"], "peer_id": str(r["peer_id"]), "route": r["route"],
                  "ok": int(r["ok"]), "denied": int(r["denied"]), "error": int(r["error"])}
                 for r in rows],
        "write_failures_this_process": WRITE_FAILURES,
    }


def card_summary(conn, *, days: int = WINDOW_DAYS) -> dict:
    """The card routes over the last `days` UTC days, today included, in `summary`'s shape: every card
    route appears, zeros included, and each carries its OWN `counting_since`, because the table-wide one
    would date these routes weeks before 044 started counting them (issue #238 C1). Reads under the
    caller's context, like `summary`."""
    return _subset_summary(conn, routes=CARD_ROUTES, interactive_routes=CARD_INTERACTIVE_ROUTES,
                           automatic_routes=CARD_AUTOMATIC_ROUTES, days=days)


def card_daily(conn, *, days: int = WINDOW_DAYS) -> dict:
    """Per seat, per UTC day, per card route: the calls of each outcome (issue #238 C4, the per-seat and
    per-day view the operator approved on 12 Oct 2026). A seat is named by its peers.name (its mesh
    label), so a seat that went silent shows as days with no row beside the seats that kept reading.
    Rows are per peer_id, which each row carries: peers.name is not unique, so two peers sharing a label
    stay two rows rather than one sum (GitHub Codex on #301, usage.py:278).
    Days with no call at all are not listed: `window_days` and `counting_since` say which days the
    answer covers, so an absent day inside them is a zero, and a day before `counting_since` is "not
    counted yet". Rows are ordered by day, then seat, then route. Reads under the caller's context."""
    return _subset_daily(conn, routes=CARD_ROUTES, interactive_routes=CARD_INTERACTIVE_ROUTES,
                         automatic_routes=CARD_AUTOMATIC_ROUTES, days=days, max_days=CARD_DAILY_MAX_DAYS)


#: Days `transcript_daily` may cover (same 90-day retention horizon as `card_daily`).
TRANSCRIPT_DAILY_MAX_DAYS = RETENTION_DAYS


def transcript_route(purpose) -> str:
    """The route a transcript-context search is counted under (TKT-4629): no purpose, an empty one or
    "interactive" keeps `transcript_search`; a known client purpose gets its own route; anything else
    (including "status", another case, or over CARD_PURPOSE_MAX_CHARS) is `transcript_search_unknown`.
    Never returns None and never raises: every authenticated GET /v1/mesh/search on the context namespace
    lands in TRANSCRIPT_ROUTES so the usage sum reconciles with the access log."""
    p = purpose.strip() if isinstance(purpose, str) else ""
    if not p or p == CARD_PURPOSE_INTERACTIVE:
        return "transcript_search"
    if len(p) <= CARD_PURPOSE_MAX_CHARS and p in TRANSCRIPT_PURPOSE_ROUTES:
        return TRANSCRIPT_PURPOSE_ROUTES[p]
    return "transcript_search_unknown"


def transcript_summary(conn, *, days: int = WINDOW_DAYS) -> dict:
    """The transcript-search routes over the last `days` UTC days, today included, in `card_summary`'s shape:
    every route in TRANSCRIPT_ROUTES appears, zeros included, with its own `counting_since`, plus `window_total`
    summing ok, denied and error across all TRANSCRIPT_ROUTES for direct access-log comparison."""
    base = _subset_summary(conn, routes=TRANSCRIPT_ROUTES, interactive_routes=TRANSCRIPT_INTERACTIVE_ROUTES,
                           automatic_routes=TRANSCRIPT_AUTOMATIC_ROUTES, days=days)
    per_route = base["routes"]
    ok_sum = sum(v["calls"] for v in per_route.values())
    denied_sum = sum(v["denied_calls"] for v in per_route.values())
    error_sum = sum(v["error_calls"] for v in per_route.values())
    return {
        **base,
        "window_total": {
            "ok": ok_sum,
            "denied": denied_sum,
            "error": error_sum,
            "total": ok_sum + denied_sum + error_sum,
        },
    }


def transcript_daily(conn, *, days: int = WINDOW_DAYS) -> dict:
    """Per seat, per UTC day, per transcript-search route, plus per-seat window totals (`seats`) and overall
    `window_total` (TKT-4629). Both `rows` and `seats` group by `peer_id` as well as `seat`, so two peers
    sharing a `peers.name` stay two rows rather than one sum."""
    base = _subset_daily(conn, routes=TRANSCRIPT_ROUTES, interactive_routes=TRANSCRIPT_INTERACTIVE_ROUTES,
                         automatic_routes=TRANSCRIPT_AUTOMATIC_ROUTES, days=days,
                         max_days=TRANSCRIPT_DAILY_MAX_DAYS)
    daily_rows = base["rows"]
    interactive_set = frozenset(TRANSCRIPT_INTERACTIVE_ROUTES)
    by_peer: dict[tuple[str, str], dict] = {}
    for row in daily_rows:
        key = (row["seat"], row["peer_id"])
        acc = by_peer.get(key)
        if acc is None:
            acc = {
                "seat": row["seat"],
                "peer_id": row["peer_id"],
                "interactive_ok": 0,
                "automatic_ok": 0,
                "ok": 0,
                "denied": 0,
                "error": 0,
                "total": 0,
            }
            by_peer[key] = acc
        if row["route"] in interactive_set:
            acc["interactive_ok"] += row["ok"]
        else:
            acc["automatic_ok"] += row["ok"]
        acc["ok"] += row["ok"]
        acc["denied"] += row["denied"]
        acc["error"] += row["error"]
        acc["total"] += row["ok"] + row["denied"] + row["error"]
    seats = [by_peer[k] for k in sorted(by_peer)]
    ok_sum = sum(s["ok"] for s in seats)
    denied_sum = sum(s["denied"] for s in seats)
    error_sum = sum(s["error"] for s in seats)
    return {
        "window_days": base["window_days"],
        "counting_since": base["counting_since"],
        "routes": base["routes"],
        "interactive_routes": base["interactive_routes"],
        "automatic_routes": base["automatic_routes"],
        "seats": seats,
        "rows": daily_rows,
        "window_total": {
            "ok": ok_sum,
            "denied": denied_sum,
            "error": error_sum,
            "total": ok_sum + denied_sum + error_sum,
        },
        "write_failures_this_process": base["write_failures_this_process"],
    }


def prune(conn, *, keep_days: int = RETENTION_DAYS) -> int:
    """Delete rows older than `keep_days`. The retention policy in sql_meshychat/005.
    Enters its own `server_internal`, like projection.rebuild; returns rows deleted."""
    keep_days = max(1, int(keep_days))
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mesh_route_usage "
                        "WHERE day <= (now() AT TIME ZONE 'UTC')::date - %s", (keep_days,))
            return cur.rowcount


def render(summary_: dict) -> list[str]:
    """Say it in words, for `python -m apps.mesh usage`. The caveats are lines of output,
    not footnotes, because the operator reading this is deciding whether the inbox is
    adopted, and both caveats change that answer."""
    since = summary_["counting_since"] or "never (no request has been counted)"
    lines = [f"mesh route usage, last {summary_['window_days']} day(s); counting since {since}"]
    for route, r in summary_["routes"].items():
        lines.append(f"  {route:7} {r['calls']:>8} call(s) from {r['peers']:>4} peer(s); "
                     f"denied {r['denied_calls']} from {r['denied_peers']} peer(s); "
                     f"errors {r['error_calls']}")
    lines.append(f"  counter write failures in this process: "
                 f"{summary_['write_failures_this_process']}")
    mq = summary_["memory_query"]
    lines.append(f"  memory.query {mq['calls']:>8} call(s) from {mq['peers']:>4} peer(s); "
                 f"denied {mq['denied_calls']} from {mq['denied_peers']} peer(s); "
                 f"errors {mq['error_calls']}; counting since "
                 f"{mq['counting_since'] or 'never (none counted yet)'}")
    return lines


def render_transcript_usage(daily_: dict) -> list[str]:
    """Plain-text rendering of `transcript_daily` for `python -m apps.mesh transcript-usage`."""
    since = daily_["counting_since"] or "never (no transcript search has been counted)"
    wt = daily_["window_total"]
    lines = [
        f"transcript search usage, last {daily_['window_days']} day(s); counting since {since}",
        f"  window total: {wt['total']} call(s) (ok {wt['ok']}, denied {wt['denied']}, errors {wt['error']})",
    ]
    for s in daily_["seats"]:
        lines.append(
            f"  seat {s['seat']} ({s['peer_id']}): total {s['total']} "
            f"(interactive_ok {s['interactive_ok']}, automatic_ok {s['automatic_ok']}, "
            f"denied {s['denied']}, errors {s['error']})"
        )
    if not daily_["seats"]:
        lines.append("  no transcript searches counted in this window")
    lines.append(f"  counter write failures in this process: {daily_['write_failures_this_process']}")
    return lines
