"""B5: a request-path query must not scale with a chain it does not bound.

`docs/V1-DESIGN-RULES.md` B5 carries the largest measured incident in the document:
`reconcile_chain_head` COUNTs an entire chain on every grant-bound authorize, O(n^2) per
chain, about 490 GB/day of production disk reads, with the hourly `run_frame_audit`
re-hashing the whole table on top for another 44 GB/day. Its instruction is
"No per-request full-table or full-chain aggregate. A plan that exceeds its declared
bound fails the test."

Nothing enforced it here. Grepped before writing: no test anywhere asserted a query plan,
a cost, or a row estimate.

WHY THIS ASSERTS SCALING AND NOT A COST NUMBER. An EXPLAIN cost bound checked against a
near-empty dev database proves nothing: Postgres correctly prefers a sequential scan on a
tiny table, so every plan looks cheap and every bound passes. Absolute costs are also not
portable across machines, Postgres versions, or planner settings. Scaling is the property
B5 actually names, so scaling is what is measured: plant a chain, double it, and compare.

WHY A TEMPORARY TABLE. The dev database is shared with other sessions running this same
suite. Planting rows into `shared_memory` and running ANALYZE would perturb their plans
mid-run. A TEMP table is session-local and cannot. The cost is stated rather than hidden:
this proves the SHAPE of the query scales as claimed against the real index definitions,
copied from `pg_indexes` on the live table. It does NOT prove anything about production
statistics, and it cannot catch a plan regression caused by real-world data skew.

WHAT IT FOUND, which is why the second test asserts growth rather than forbidding it.
`apps/memoryskills/handlers.py:981` runs, on every windowed-history `memory.query`:

    SELECT key, COUNT(*) FROM shared_memory
    WHERE namespace = %s AND key = ANY(%s::text[]) AND created_at < %s GROUP BY key

The key array is bounded by `limit`, so the number of CHAINS is bounded. The count inside
each chain is not, and `created_at` carries no index, so it is a filter rather than a
bound. That is B5's named shape at smaller scale: a per-request aggregate over a whole
chain. It is recorded here as a measurement rather than reported as a defect, because the
bounded key count makes it a different order of problem from the 490 GB/day incident.

Run:  pytest tests/test_query_cost_bounds.py -v
Needs MESHY_TEST_DSN (defaults to the dev stack database built by sqlmigrate.py).
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

NS = "costbound"

#: Copied verbatim from `pg_indexes` on the live `shared_memory`, 2026-09-21. If the real
#: table's indexes change, this fixture stops representing it and the tests below become a
#: statement about a table that does not exist.
REAL_INDEXES = (
    "CREATE UNIQUE INDEX cb_head_unique ON cb_memory (namespace, key) WHERE is_head",
    "CREATE UNIQUE INDEX cb_nkv_unique ON cb_memory (namespace, key, version)",
    # Deliberately NO index on created_at: the live table has none either, which is
    # exactly why the aggregate below filters rather than bounds.
)


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE cb_memory ("
                " id uuid PRIMARY KEY, namespace text NOT NULL, key text NOT NULL,"
                " version int NOT NULL, is_head boolean NOT NULL,"
                " created_at timestamptz NOT NULL, status text NOT NULL)"
            )
            for stmt in REAL_INDEXES:
                cur.execute(stmt)
        yield c
        # TEMP tables die with the session; dropping is belt and braces for a pooled DSN.
        with c.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS cb_memory")


def plant(conn, *, keys: int, versions: int) -> None:
    """`keys` chains of `versions` rows each. Only the newest version is the head."""
    rows = []
    for k in range(keys):
        for v in range(1, versions + 1):
            rows.append((str(uuid.uuid4()), NS, f"k{k}", v, v == versions,
                         f"2026-01-01T00:00:{v % 60:02d}+00:00", "OPEN"))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO cb_memory (id,namespace,key,version,is_head,created_at,status)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)", rows)
        cur.execute("ANALYZE cb_memory")


def reset(conn) -> None:
    """Empty the fixture between measurements.

    The two measurements are the SAME chains at different lengths, so the second plant
    must replace the first rather than add to it: re-inserting versions 1..N would
    collide with `cb_nkv_unique`, which is the real table's constraint and is copied
    here on purpose.
    """
    with conn.cursor() as cur:
        cur.execute("TRUNCATE cb_memory")


def _walk_rows(node) -> list[float]:
    out = [float(node["Plan Rows"])]
    for child in node.get("Plans", []):
        out.extend(_walk_rows(child))
    return out


def estimated_rows(conn, sql: str, params) -> float:
    """Largest per-node row estimate anywhere in the plan. EXPLAIN only: nothing runs.

    WHY THE MAXIMUM AND NOT THE TOP NODE, learned by getting it wrong here first. The
    top-level `Plan Rows` of a GROUP BY is the number of GROUPS OUT, not the rows read to
    produce them. The first version of this file read the top node, so the chain aggregate
    measured a flat 10 at both chain lengths (ten keys, ten groups, by construction) and
    the sensitivity control below could never have fired. Reading down to the scan node is
    what makes this a measure of WORK rather than of result size.
    """
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + sql, params)
        return max(_walk_rows(cur.fetchone()[0][0]["Plan"]))


def plan_text(conn, sql: str, params) -> str:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + sql, params)
        return json.dumps(cur.fetchone()[0])


# The two real queries, copied from the source rather than paraphrased.
HEAD_LOOKUP = ("SELECT id, version, status FROM cb_memory "
               "WHERE namespace = %s AND key = %s AND is_head = TRUE LIMIT 1")
CHAIN_AGGREGATE = ("SELECT key, COUNT(*) AS n FROM cb_memory "
                   "WHERE namespace = %s AND key = ANY(%s::text[]) "
                   "AND created_at < %s::timestamptz GROUP BY key")


def test_planting_actually_changes_the_table():
    """Control for the fixture itself, run standalone so a broken plant cannot make the
    scaling tests pass by leaving the table empty at both measurements."""
    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TEMP TABLE cb_memory (id uuid PRIMARY KEY, namespace text,"
                        " key text, version int, is_head boolean, created_at timestamptz,"
                        " status text)")
        plant(c, keys=2, versions=5)
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM cb_memory")
            assert cur.fetchone()[0] == 10
            cur.execute("DROP TABLE cb_memory")


def test_the_head_lookup_does_not_scale_with_chain_length(conn):
    """THE BOUND. Reading one key's head must cost the same whatever its history."""
    plant(conn, keys=20, versions=5)
    small = estimated_rows(conn, HEAD_LOOKUP, [NS, "k1"])

    reset(conn)                           # same chains, four times the history
    plant(conn, keys=20, versions=20)

    large = estimated_rows(conn, HEAD_LOOKUP, [NS, "k1"])
    assert large <= small + 1, (
        f"the head lookup's row estimate grew from {small} to {large} when chain length "
        "went from 5 to 20. It is served by a partial unique index on (namespace, key) "
        "WHERE is_head, so it must not scale with version history."
    )


def test_the_head_lookup_uses_the_partial_index_not_a_scan(conn):
    """Shape, not just size. A correct estimate from a sequential scan is luck."""
    plant(conn, keys=50, versions=10)
    plan = plan_text(conn, HEAD_LOOKUP, [NS, "k7"])
    assert "Index Scan" in plan or "Index Only Scan" in plan, (
        f"the head lookup is not using an index at 500 rows. Plan: {plan}"
    )


def test_the_chain_aggregate_does_scale_with_chain_length(conn):
    """SENSITIVITY CONTROL, and a measurement in its own right.

    This is the assertion that makes the bound above mean something. If the instrument
    could not detect growth, the head lookup passing would prove nothing. So a query that
    IS known to scale must be seen to scale.

    It also records the real exposure: `created_at` carries no index on the live table, so
    this aggregate reads every version row of every key it is given. The number of keys is
    bounded by `limit`; the work inside each chain is not.
    """
    keys = [f"k{i}" for i in range(10)]
    cutoff = "2026-06-01T00:00:00+00:00"

    plant(conn, keys=10, versions=5)
    small = estimated_rows(conn, CHAIN_AGGREGATE, [NS, keys, cutoff])

    reset(conn)
    plant(conn, keys=10, versions=20)
    large = estimated_rows(conn, CHAIN_AGGREGATE, [NS, keys, cutoff])

    assert large > small, (
        f"the chain aggregate's estimate did not grow ({small} to {large}) when chain "
        "length quadrupled. Either the planner is not reading the planted statistics or "
        "EXPLAIN is not measuring what this file assumes, and the bound asserted in "
        "test_the_head_lookup_does_not_scale_with_chain_length proves nothing."
    )


# --------------------------------------------------------------------------- #
# E16: every request-path aggregate (2026-09-26)
# --------------------------------------------------------------------------- #
#
# The three tests above measure the head lookup against a COPY of the chain aggregate. E16
# (an internal project document (not part of this release)) is that B5 was only partially enforced: the aggregate the rule's own
# measurement found, the windowed-history count in `handle_query`, was recorded and left
# unbounded inside each chain, and nothing stopped a new per-request aggregate from landing.
# Below:
#
#   * the statement `handle_query` ACTUALLY RUNS for a windowed history is captured from a
#     real call (not copied), re-pointed at the temporary table and measured by what it READS
#     at two chain lengths, both beyond the handler's per-key cap: it must not grow;
#   * the instrument that reads is proved able to see growth, on the copied aggregate;
#   * the handler's answer keeps its meaning: exact under the cap, a marked lower bound past it;
#   * every SQL aggregate literal under apps/ is on a ledger, per file, so a new one fails
#     here until it is declared, measured or exempted with a reason.
#
# RED on the unchanged tree: the captured statement is the COUNT(*) ... GROUP BY key, whose
# reads grow with the chain (2,000 rows to 8,000), and HISTORY_WINDOW_COUNT_CAP does not exist.

import ast  # noqa: E402
import datetime as _dt  # noqa: E402
import re  # noqa: E402
import time as _time  # noqa: E402

from tests.test_memory_skills import (  # noqa: E402,F401  (fixtures are used by name)
    _call,
    _claim,
    _ns,
    _peer,
    app_conn,
    owner_conn,
)

ROOT = Path(__file__).resolve().parent.parent

_SCAN_NODES = ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
               "Bitmap Index Scan", "Tid Scan", "Sample Scan")


def _walk_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_nodes(child)


def rows_read(conn, sql: str, params) -> int:
    """Tuples every TABLE or INDEX scan in the plan produced or filtered away, times its loops:
    what the statement READ, measured by running it (EXPLAIN ANALYZE) on the session-local
    table. An estimate cannot measure a LIMIT inside a lateral subquery: the scan node under
    the Limit estimates the whole chain even though execution stops at the limit."""
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
        plan = cur.fetchone()[0][0]["Plan"]
    total = 0.0
    for n in _walk_nodes(plan):
        if n["Node Type"] in _SCAN_NODES:
            loops = n.get("Actual Loops", 1)
            total += (n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0)
                      + n.get("Rows Removed by Index Recheck", 0)) * loops
    return int(total)


class _Recording:
    """The request's connection, recording every statement its cursors execute."""

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[tuple[str, list]] = []

    def cursor(self, *args, **kwargs):
        return _RecordingCursor(self._conn.cursor(*args, **kwargs), self.statements)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _RecordingCursor:
    def __init__(self, cur, sink):
        self._cur, self._sink = cur, sink

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)

    def execute(self, sql, params=None, **kwargs):
        self._sink.append((str(sql), list(params or [])))
        return self._cur.execute(sql, params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def _write(app_conn, peer, ns, key, n):
    """`n` versions of `key`; the created_at of each, as the handler reports it."""
    out = []
    for i in range(n):
        res = _call(app_conn, peer, "memory.write",
                    {"namespace": ns, "key": key, "value": {"body_md": f"v{i} {uuid.uuid4()}"}})
        out.append(_dt.datetime.fromisoformat(res["created_at"]))
        _time.sleep(0.02)     # created_at is the transaction's clock; keep versions apart
    return out


def _windowed(app_conn, peer, ns, since: _dt.datetime, conn=None):
    return _call(conn or app_conn, peer, "memory.query",
                 {"namespace": ns, "include_history": True, "key_prefix": "k",
                  "since": since.isoformat()})


def _before_since_statement(app_conn, owner_conn):
    """The statement a real windowed-history memory.query runs to count what `since` withheld,
    captured from the handler, with the values it was given."""
    peer = _peer(owner_conn, "e16cap")
    ns = _ns("e16cap")
    _claim(app_conn, peer, ns)
    stamps = _write(app_conn, peer, ns, "k0", 2)
    since = stamps[1] - _dt.timedelta(microseconds=1)
    rec = _Recording(app_conn)
    out = _windowed(app_conn, peer, ns, since, conn=rec)
    assert out["count"] == 1, out                 # CONTROL: the window returned a row
    found = [(s, p) for s, p in rec.statements if "created_at <" in s]
    assert len(found) == 1, ("expected exactly one before-since statement", rec.statements)
    sql, params = found[0]
    return sql, params, {"namespace": ns, "keys": ["k0"], "since": since.isoformat()}


def _repoint(sql: str, params: list, seen: dict, *, keys: list[str], cutoff: str):
    """The captured statement against the temporary table, with the planted chains' values in
    place of the captured ones. Values are replaced BY IDENTITY with what the handler was
    given, so this works whatever order or number of parameters the statement has."""
    assert "shared_memory" in sql and "cb_memory" not in sql, sql
    out = []
    for p in params:
        if p == seen["namespace"]:
            out.append(NS)
        elif p == seen["keys"]:
            out.append(keys)
        elif p == seen["since"]:
            out.append(cutoff)
        else:
            out.append(p)
    return re.sub(r"\bshared_memory\b", "cb_memory", sql), out


def test_the_rows_read_instrument_sees_a_chain_aggregate_grow(conn):
    """SENSITIVITY CONTROL for `rows_read`: the copied whole-chain COUNT must be seen to read
    more when the chains are longer, or the bound below proves nothing."""
    keys = [f"k{i}" for i in range(10)]
    cutoff = "2026-06-01T00:00:00+00:00"
    plant(conn, keys=10, versions=200)
    small = rows_read(conn, CHAIN_AGGREGATE, [NS, keys, cutoff])
    reset(conn)
    plant(conn, keys=10, versions=800)
    large = rows_read(conn, CHAIN_AGGREGATE, [NS, keys, cutoff])
    assert small >= 2000 and large >= 4 * small * 0.9, (small, large)


def test_the_windowed_history_count_does_not_scale_with_chain_length(conn, app_conn,
                                                                     owner_conn):
    """THE E16 BOUND, on the statement the handler runs. Both chain lengths are past the
    per-key cap, so a bounded statement reads the same at both; the whole-chain COUNT it
    replaced read four times as much at the second."""
    sql, params, seen = _before_since_statement(app_conn, owner_conn)
    keys = [f"k{i}" for i in range(10)]
    cutoff = "2026-06-01T00:00:00+00:00"
    q, p = _repoint(sql, params, seen, keys=keys, cutoff=cutoff)

    plant(conn, keys=10, versions=200)
    small = rows_read(conn, q, p)
    reset(conn)
    plant(conn, keys=10, versions=800)
    large = rows_read(conn, q, p)

    assert large <= small + len(keys), (
        f"the windowed-history count read {small} rows at 200 versions a key and {large} at "
        f"800: it scales with the chain. Statement: {sql}")
    from apps.memoryskills import handlers
    cap = handlers.HISTORY_WINDOW_COUNT_CAP
    assert 200 > cap + 1, "the chains must be longer than the cap for this to measure it"
    assert large <= len(keys) * (cap + 1) + 2 * len(keys), (
        f"read {large} rows for {len(keys)} keys; the declared bound is keys x (cap+1)")


def test_the_windowed_history_count_is_exact_under_the_cap_and_marked_past_it(
        app_conn, owner_conn, monkeypatch):
    """What the bound costs, and that it is said. Five versions, `since` between the third and
    the fourth: three withheld, exact. With the cap lowered to two, the same query answers
    three (cap + 1) and marks it a lower bound, and the window is still incomplete."""
    from apps.memoryskills import handlers
    peer = _peer(owner_conn, "e16sem")
    ns = _ns("e16sem")
    _claim(app_conn, peer, ns)
    stamps = _write(app_conn, peer, ns, "k0", 5)
    since = stamps[2] + (stamps[3] - stamps[2]) / 2

    exact = _windowed(app_conn, peer, ns, since)
    assert [m["version"] for m in exact["results"]] == [5, 4], exact
    assert [m["versions_before_since"] for m in exact["results"]] == [3, 3]
    assert all("versions_before_since_is_lower_bound" not in m for m in exact["results"])
    assert exact["history_window"]["complete"] is False
    assert "count_cap" not in exact["history_window"]

    # POSITIVE CONTROL: `since` before every version withholds nothing.
    whole = _windowed(app_conn, peer, ns, stamps[0] - _dt.timedelta(seconds=1))
    assert [m["versions_before_since"] for m in whole["results"]] == [0] * 5
    assert whole["history_window"]["complete"] is True

    monkeypatch.setattr(handlers, "HISTORY_WINDOW_COUNT_CAP", 2)
    capped = _windowed(app_conn, peer, ns, since)
    assert [m["versions_before_since"] for m in capped["results"]] == [3, 3]
    assert all(m["versions_before_since_is_lower_bound"] is True for m in capped["results"])
    assert capped["history_window"]["complete"] is False
    assert capped["history_window"]["count_cap"] == 2

    # CONTROL at the cap's edge: two withheld with the cap at two is exact, not marked.
    at_edge = _windowed(app_conn, peer, ns, stamps[1] + (stamps[2] - stamps[1]) / 2)
    assert [m["versions_before_since"] for m in at_edge["results"]] == [2, 2, 2]
    assert all("versions_before_since_is_lower_bound" not in m for m in at_edge["results"])


# ---- the ledger: every SQL aggregate literal under apps/ ------------------------------

_AGG = re.compile(r"\b(count|sum|avg|min|max|array_agg|jsonb_agg|json_agg|string_agg|"
                  r"bool_or|bool_and)\s*\(|\bgroup\s+by\b|\bover\s*\(", re.I)
_SQL = re.compile(r"\b(select|update|delete|insert|with)\b", re.I)


def aggregate_literals(source: str) -> list[int]:
    """Line numbers of the string literals in `source` that are SQL carrying an aggregate.
    Docstrings are skipped (they quote SQL to explain it); f-string parts count."""
    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    return sorted(n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and id(n) not in docs and _AGG.search(n.value) and _SQL.search(n.value))


#: file -> the number of aggregate-bearing SQL literals in it, 2026-09-26 (6d70836 + E16).
#: MEASURED: apps/memoryskills/handlers.py's windowed-history count (the test above).
#: EXEMPT, with the reason: handlers.py's memory.prune (an admin-gated, namespace-wide bulk
#: delete by design; its min(version) is one probe of the (namespace, key, version) index per
#: candidate) and apps/calls/janitor.py (a background sweep, not a request).
#: EXEMPT since integ/r13 (fin/w1-jobs beside this ledger): apps/jobs/sweeps.py, two job
#: bodies, never a request: the usage retention's min(day) on mesh_route_usage (one read of its
#: day index a tick) and the file janitor's count(*) of rows past their window, bounded by its
#: own LIMIT FILE_SWEEP_CAP + 1 and read only when the jobs process has no blob backend.
#: UNMEASURED, recorded so they are counted rather than forgotten: every other entry. Several
#: are on a request path (dashboard, mesh graph coverage, meshsem related reads, meshsum facts,
#: MeshGuard's pending count); each needs its own scaling test before B5 is whole.
#: apps/mesh/graph.py `coverage` and apps/meshsem/related_reads.py `coverage`, MEASURED on production
#: (3 Nov 2026, as meshychat_app) but still without a scaling test: each is one pass over the
#: namespace's heads by design (it is a total). The mail pair took 3,665 ms per inbox, owed,
#: awaiting, brief, unread, fleet-unread and mentions answer (about 880 an hour), and 1,253 ms once
#: rewritten with EXISTS. The related figures took 4,837 ms, 881 ms of it JIT. Since divergence 107
#: each is held COVERAGE_TTL_S (60 s) per process and per key, so the cost is per process per
#: minute, not per request.
#: apps/meshsem/graphsearch.py (divergence 060): a graph namespace's coverage for /v1/mesh/search?ns=,
#: the same shape as store.coverage (one pass over the namespace's heads, about 1,000 wiki rows on
#: production) and held COVERAGE_TTL_S the same way; unmeasured like the mail's.
#: apps/mesh/graph.py, three more on request paths since divergence 072, UNMEASURED: `_OWED_FIRST`'s
#: min(created_at) (/v1/mesh/owed and /v1/mesh/brief, one range of the (namespace, key, version)
#: unique index per candidate ask, that key's versions), and `thread_brief`'s two (/v1/mesh/brief):
#: `caller_last`'s max(recorded_at) GROUP BY thread is NOT windowed, it reads the in_thread edge of
#: every message the caller ever authored (one row per thread it wrote in), while the outer count
#: and max read only other-party messages at or after `since`.
#: apps/mesh/drift.py, one, ADMIN: drift-report's per-seat-day aggregate (count, percentile_cont,
#: jsonb_object_agg) over a window of completed days, run by an operator, never on a request path (086).
#: apps/mesh/graph.py, one more since divergence 084, UNMEASURED: `unanswered_from`'s count(DISTINCT
#: src_id) and max(created_at) (/v1/mesh/recipients, per recipient label of one send, at most 10), each a
#: range of mesh_edges_dst_idx (authored_by and addressed_to by label, recorded_at >= the unread window).
#: apps/mesh/work.py, one new on-demand aggregate: last_sent_at reads head rows in the last seven days of msg/<ms>
#: keys through the (namespace,key) range, then filters the authenticated writer and server timestamp. It scales with
#: fleet messages IN that window, not the namespace's older history or just this writer's sends. Held 60 s per process.
#: test_fleet_last_sent_reads_only_the_message_key_window measures the actual SQL with an unbounded sensitivity
#: control. Indexed time is one backward (namespace,seat,time_last) range per alias, LIMIT 1 after a live-head join;
#: head-rejected candidates remain UNMEASURED, so this is not claimed as a constant-work bound for tombstoned history.
#: apps/mesh/work.py, a second per-request aggregate (f38d28 item 5, 10 Nov 2026): ACTIVE_SENT_SQL's max(created_at)
#: GROUP BY writer for /v1/mesh/recipients, ONE statement per call for all its peers (pxx 9059b9 P2-1). The same shape as LAST_SENT_SQL over a
#: 30-minute key window (msg/<ms> keys, the (namespace,key) range first), then the writer and the daemon-thread filter.
#: It scales with fleet messages in the last 30 minutes, not with history:
#: test_recipients_active_sent_reads_only_the_thirty_minute_key_window measures it with the same sensitivity control.
#: apps/mesh/graph.py, one more since divergence 074, UNMEASURED: `unread`'s count(*) and
#: min(version_at), window aggregates over one caller's addressed head mail at or after `since`
#: (a window the view caps below the call retention), with its reads from ONE range of
#: skill_calls_caller_created_idx (this caller, created_at >= since), not a per-message lookup.
#: apps/mesh/graph.py, one more since #493 (3 Nov 2026), MEASURED: `unread`'s `cleared`, max(...) per kind of read
#: GROUP BY key over the `reads` CTE, folded into one jsonb object. It reads ONE range of skill_calls_caller_created_idx
#: (this reader, created_at >= since) and holds one entry per distinct key the reader read in the window; it does not
#: grow with the reader's calls before `since`, with other callers' calls, or with the pending mail, which makes one
#: lookup per message in the object (test_the_unread_cleared_aggregate_reads_one_readers_window_and_grows_with_it_alone;
#: tests/test_mesh_unread.py holds that the reads are scanned once in the route's own context).
#: apps/cards/store.py (divergences 091, 092), two array_agg in ONE statement (counted as one), on a request path, UNMEASURED: `_AGG`'s
#: array_agg of the cards naming a key and, filtered, of the redacted ones among them, which
#: /v1/cards and a filtered card search run once per request. One pass over the card namespace's HEADS (its
#: (namespace, key) range, is_head), unnesting each `supersedes` array, at most 16 keys a card (validation
#: MAX_SUPERSEDES). Planned for 10,000 cards fleet-wide; the rehearsal copy measures it before production derives
#: the namespace (docs/design/decision-cards.md 7). Since 2026-10-10 (091, amended) each array is sliced in the
#: statement to store.NAMED_CAP (32) keys and its cardinality before the slice is the count, so a row of `s`, and
#: so what the request fetches per card, holds at most 96 keys whatever the fan-in (tests/test_cards_named_cap.py).
#: MEASURED since divergence 110 (sql_meshychat/057): every pass reads the narrow head record `card_heads`, never a
#: value, and the passes have different scopes. In /v1/cards `_AGG`, the one aggregate, runs over the edges naming the
#: page's own cards only. The edge pass `_EDGES` is still one read of the whole namespace's namer records (about 1,200
#: buffers at 21,000 heads), and the status filter and the type walk with its two DISTINCT passes (the walk's edges,
#: and the keys it reached) read it whole; `superseders` and `replacers`, for the card search, read the same `_EDGES`
#: over the whole namespace. None of those is an aggregate, so the count stays 1. Scaling test
#: tests/test_cards_head_record.py::test_a_page_grows_with_the_namespace_at_a_fraction_of_the_old_rate (1,000 against
#: 21,000 production-like heads, limit 1 and 100, growth bounded against the old code's growth in the same run, and no
#: plan node reading more shared_memory rows than the page holds).
#: apps/cards/validation.py (#476), one max ... GROUP BY on a request path (memory.write of a Context card, the
#: issue-#297 one-active-head rule), UNMEASURED by a test: one pass over the card namespace's HEADS (its
#: (namespace, key) range, is_head; LIKE 'card/<seat>/%' does not narrow that range without a pattern-ops index),
#: unnesting each head's `supersedes`, at most 16 keys a card (validation MAX_SUPERSEDES), into (target, latest
#: superseding time). It replaced a correlated NOT EXISTS that was quadratic in a seat's cards and passed the
#: statement timeout on every Context write at 2,410 heads (prod, 1 Nov); read-only on prod the new form answered
#: for that seat in about 1.2 s wall, interpreter start included.
#: apps/mesh/usage.py, two more since divergence 101 (#238) and TKT-4629, on a request path, UNMEASURED: `_subset_summary`'s
#: sum/count(DISTINCT) GROUP BY route and `_subset_daily`'s sum GROUP BY day, seat, route (serving GET /v1/cards/usage over
#: 045's eight card routes and GET /v1/mesh/transcript-usage over 058's five transcript routes). Both read only the
#: named route subset (`route = ANY(%s)`) inside a window of days capped at RETENTION_DAYS (90), so at most
#: 90 days x seats x 8 routes x 3 outcomes rows, one row per (route, day, peer_id, outcome), read as one range of
#: that primary key per route. Each route's first day is an ORDER BY day LIMIT 1 read, not an aggregate.
#: apps/meshsem/ctxindex.py (v1.1 step 6b), four on a request path (the fourth is `_latest_gens` max(gen), per loaded source), two MEASURED below: GET /v1/mesh/search?ns= the
#: transcript-context namespace (ctxsearch.search calls ctxindex.search per request; off while MESHY_CONTEXT_INDEX is
#: false). `_LATEST_GEN_SQL`'s max(gen), per candidate chunk, only when ctx_source_gen has no row for its source: one
#: range of mesh_ctx_chunks_seat_src_gen (namespace, seat, src, gen). The filtered subset's count(*), which picks the
#: exact or the sharded path: one range of mesh_ctx_chunks_current_space_seat_time (space, namespace, seat, time_last)
#: WHERE current (sql_meshychat/052; 047's full seat/time index is dropped by 059) under the filters,
#: CAPPED ON CURRENT ROWS since the scaling follow-up: it stops at _subset_cap() (one past the most rows a search can
#: score) and says `subset_at_least` past it, and `_latest_gens` reads only the loaded shards' sources (it was a
#: GROUP BY over the whole namespace per sharded request). tests/test_ctx_vector_store.py's
#: test_a_sharded_requests_count_and_gen_reads_do_not_grow_with_the_namespace holds both to the namespace's size,
#: on a namespace of CURRENT-generation rows. Superseded generations (switch-on blocker 5, codex-pxx-cli TASK AQ, pxx
#: b62e37): since sql_meshychat/051 the predicate reads `c.current` through partial indexes, and the plan and the
#: shard loads take current rows only, so the count and the probes no longer walk older generations;
#: test_a_sharded_requests_count_and_probes_do_not_grow_with_superseded_history holds both to that, against the
#: pre-051 predicate as its growing control. Still UNMEASURED: rows a src, project or until filter rejects inside the
#: ranges walked (the LIMIT caps the rows that survive the filters, not the rows read).
#: `_plan_rows` reads ctx_shard_plan (sql_meshychat/053), one primary-key range per (space, namespace): O(shards).
#: Its statement triggers keep it equal to the aggregate `_plan_rows_aggregate` computes, whose count/min/max GROUP BY
#: seat, month reads EVERY current chunk of the namespace (15M rows passed the 30 s statement timeout, memory-bxxxxx
#: e4ba2c). The aggregate now runs only when ctxindex.SHARD_MAX differs from the stored plan's shard size (tests that
#: shrink it), and per chunk write the triggers recompute at most the (seat, month) groups the write touched.
#: tests/test_ctx_shard_plan.py holds the stored plan to the aggregate, and its read to zero chunk tuples. The plan is
#: still cached per (space, namespace) by the change counter (sql_meshychat/049) or PLAN_TTL_S, so a request pays one
#: primary-key row of ctx_chunk_changes; tests/test_ctx_vector_store.py pins that
#: (test_the_shard_plan_is_computed_once_per_index_change).
#: `check_shard_plan` (BACKGROUND, its own job ctx_shard_plan_check only: hourly, own lane, 600 s): max(id) and count per
#: (space, namespace, seat, month) of every current chunk, FULL JOINed with the stored plan's per-group sums, the drift
#: check that does not depend on a search's subset cap (pxx b80f02 point 2). It reads every current chunk, under its
#: own 600 s statement timeout, and never runs in a request.
#: `run_retention_job`'s space list (BACKGROUND, the retention job only): a recursive min(space) loose scan of
#: mesh_ctx_chunks_digest (space, text_sha256), one index probe per space, so retention can delete one space at a time
#: under ctxjob's space lock (pxx 21a658 P2-2). Bounded by the number of spaces, never by the chunks.
#: `_matching`'s EXISTS per planned shard (Codex on #387), one statement per sharded search: each probe is a range
#: of the chunk indexes under the request's filters and stops at its first row it KEEPS, so the probes number the
#: plan's shards (seats x months x shards under retention), and every planned shard is probed before
#: MAX_SHARDS_PER_QUERY applies. Superseded generations take no shard since 051 (the plan is of current rows).
#: UNMEASURED: a shard whose current rows a src or until filter all rejects is walked whole. Not an aggregate.
#: apps/meshsem/ctxsearch.py (v1.1 step 7), on the same request path, CAPPED (pxx ab9411's switch-on condition):
#: `_unindexed_newer`'s count(DISTINCT (seat, src)) over this space's dead letters in state dead or replay, one range
#: of ctx_dead_letter's primary key (space, seq), reads at most DEAD_SCAN_MAX (1,000) rows in a LIMITed subquery and
#: marks the answer "at least" past it. The lexical arm (sql_meshychat/048), one GIN range of mesh_ctx_lexical_terms:
#: at most LEX_SCAN_MAX (200,000) chunks holding a query term are read (truncated past it), their pre-order sum is
#: computed and sorted; the scored set's count/sum with GROUP BY term and chunk then runs over at most LEX_EXACT_MAX
#: (20,000) of them.
#: apps/memoryskills/handlers.py `_enforce_directive_rate` (the directive rule, on the directive write path), BOUNDED: one range of
#: the (namespace, key) indexes over the last hour of msg/<epoch-ms> keys, capped by LIMIT DIRECTIVE_RATE_PER_HOUR + 1,
#: so it reads at most one hour of mesh messages and never the table's history
#: (test_the_directive_rate_count_does_not_scale_with_history).
#: apps/memoryskills/handlers.py `_urgent_count` (the urgent lane, kxxx ef884a, on the urgent write path), BOUNDED: the
#: same one hour of msg/<epoch-ms> keys, capped by LIMIT URGENT_RATE_PER_HOUR + 1
#: (test_the_urgent_rate_count_does_not_scale_with_history).
#: apps/mesh/graph.py, one more since divergence 081 batch follow-up, MEASURED: `_FLEET_UNREAD_SQL`'s
#: `cleared` + `checked` aggregate (`jsonb_object_agg` of per-(reader, key) maxima over `reads` for up to
#: `FLEET_PEER_CAP` candidate peers in ONE statement, then `count(*)` / `min(version_at)` `GROUP BY peer_id`
#: over `addressed` on `mesh_edges_dst_idx` with `recorded_at >= since`). Replaces up to 200 per-peer
#: `_UNREAD_SQL` round-trips on `/v1/mesh/fleet/unread`; `reads` + `cleared` reads only covered callers'
#: window ranges on `skill_calls_caller_created_idx` and does not grow with pre-window history or
#: non-covered callers (`test_the_fleet_unread_cleared_aggregate_reads_covered_readers_window_alone`).
#: apps/mesh/graph.py `to_close` (/v1/mesh/brief, PR #636), UNMEASURED (no SQL aggregate or GROUP BY, so
#: `AGGREGATE_LEDGER["apps/mesh/graph.py"]` stays 9; recorded here because its `LEFT JOIN LATERAL` runs per
#: candidate row): `candidates` reads the caller's own authored `owes_ack` edges with `recorded_at >= since`
#: (scales with the caller's own asks in the `since` window, never namespace history or other seats' asks),
#: probing `closes`, `declines`, and the newest non-redacted `replies_to` head (`ORDER BY sr.created_at DESC,
#: rr.src_id DESC LIMIT 1`) per candidate ask, capped at `LIMIT limit + 1` (at most 101 rows).
AGGREGATE_LEDGER = {
    "apps/authapi/bootstrap.py": 1,
    "apps/board/projection.py": 1,
    "apps/calls/janitor.py": 1,
    "apps/cards/store.py": 1,
    "apps/cards/validation.py": 1,
    "apps/core/backup_manifest.py": 1,
    "apps/dashboard/queries.py": 8,
    "apps/grants/db.py": 1,
    "apps/jobs/sweeps.py": 2,
    "apps/memoryskills/handlers.py": 4,
    "apps/mesh/drift.py": 1,
    "apps/mesh/graph.py": 9,
    "apps/mesh/usage.py": 4,
    "apps/mesh/work.py": 2,
    "apps/meshaudit/store.py": 3,
    "apps/meshguard/repository.py": 1,
    "apps/meshsem/ctxindex.py": 6,
    "apps/meshsem/ctxsearch.py": 2,
    "apps/meshsem/graphsearch.py": 2,
    "apps/meshsem/related_reads.py": 5,
    "apps/meshsem/store.py": 1,
    "apps/meshsum/admission.py": 6,
    "apps/meshsum/facts.py": 2,
    "apps/meshsum/window.py": 2,
    "apps/peers/keys.py": 1,
    "apps/wsgw/call_notify.py": 1,
    "apps/wsgw/caller_frames.py": 1,
    "apps/wsgw/target_frames.py": 1,
}


def test_the_ledger_scanner_catches_a_planted_aggregate():
    """The scanner's positive control, so an empty ledger diff cannot mean a blind scanner."""
    planted = ('"""SELECT count(*) FROM t in a docstring"""\n'
               'Q = "SELECT key, COUNT(*) FROM shared_memory GROUP BY key"\n'
               'R = f"SELECT max(v) FROM {T}"\n'
               'S = "SELECT id FROM t"\n'
               'n = max(1, 2)\n')
    assert aggregate_literals(planted) == [2, 3]


def test_every_sql_aggregate_under_apps_is_on_the_ledger():
    found = {}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        n = len(aggregate_literals(path.read_text(encoding="utf-8")))
        if n:
            found[path.relative_to(ROOT).as_posix()] = n
    assert found, "the scan found no aggregate at all; the scanner is broken"
    assert found == AGGREGATE_LEDGER, (
        "an SQL aggregate was added, moved or removed. B5: a per-request aggregate needs a "
        "declared bound and a scaling test in this file; a background or admin one needs its "
        "reason on the ledger. Then update AGGREGATE_LEDGER. Differences: "
        + str({k: (AGGREGATE_LEDGER.get(k), found.get(k))
               for k in set(found) | set(AGGREGATE_LEDGER)
               if found.get(k) != AGGREGATE_LEDGER.get(k)}))


def _heap_rows_read(conn, sql: str, params) -> int:
    """rows_read without counting a Bitmap Index Scan under its Bitmap Heap Scan: the heap scan reports the same rows
    again, so adding both doubles a bitmap plan's count (pxx d2a139, measured on Postgres 16.15: 20 recent rows took an
    Index Scan, 40 and 80 a bitmap plan, so the count jumped 20 -> 80 -> 160 for a linear read). Production may choose
    either plan, so the plan is not forced; only the count is taken once per row."""
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
        plan = cur.fetchone()[0][0]["Plan"]
    total = 0.0

    def walk(node, parent_type):
        nonlocal total
        t = node["Node Type"]
        if t in _SCAN_NODES and not (t == "Bitmap Index Scan" and parent_type in ("Bitmap Heap Scan", "BitmapAnd",
                                                                                   "BitmapOr")):
            total += (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
                      + node.get("Rows Removed by Index Recheck", 0)) * node.get("Actual Loops", 1)
        for child in node.get("Plans", []):
            walk(child, t)
    walk(plan, None)
    return int(total)


def test_fleet_last_sent_reads_only_the_message_key_window():
    """Actual fleet SQL: older history stays outside the read; window traffic costs linearly.

    Temporary table carries the production namespace/key and partial head indexes, not a writer index.
    Removing the key bound is the sensitivity control, since created_at alone has no index.
    """
    import datetime as _d
    from apps.mesh.work import LAST_SENT_SQL
    sql = LAST_SENT_SQL.replace("shared_memory", "fw_memory")
    now = _d.datetime.now(_d.timezone.utc)
    since = now - _d.timedelta(days=7)
    peer = str(uuid.uuid4())
    params = ["shared/mesh-chat", f"msg/{int(since.timestamp() * 1000)}", peer, since]
    control = sql.split("AND key ", 1)[0] + "AND created_by_peer_id = %s"
    assert control != sql
    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TEMP TABLE fw_memory (namespace text NOT NULL, key text NOT NULL, version int NOT NULL,"
                        " is_head boolean NOT NULL, created_by_peer_id uuid NOT NULL, created_at timestamptz NOT NULL)")
            cur.execute("CREATE UNIQUE INDEX fw_nkv ON fw_memory (namespace, key, version)")
            cur.execute("CREATE UNIQUE INDEX fw_head ON fw_memory (namespace, key) WHERE is_head")
            def measure(old, recent):
                cur.execute("TRUNCATE fw_memory")
                stamps = ([since - _d.timedelta(days=1, seconds=i) for i in range(old)]
                          + [now - _d.timedelta(seconds=i) for i in range(recent)])
                cur.executemany("INSERT INTO fw_memory VALUES ('shared/mesh-chat', %s, 1, true, %s::uuid, %s)",
                                [(f"msg/{int(at.timestamp() * 1000)}-{i}", peer, at) for i, at in enumerate(stamps)])
                cur.execute("ANALYZE fw_memory")
                bounded = _heap_rows_read(c, sql, params)
                unbounded = _heap_rows_read(c, control, [params[0], peer])
                cur.execute(sql, params)
                assert cur.fetchone()[0] == now
                return bounded, unbounded, plan_text(c, sql, params)
            small, ctl_small, _ = measure(2000, 20)
            large, ctl_large, shape = measure(8000, 20)
            more_recent, _, _ = measure(2000, 40)
    assert "Index" in shape, shape
    assert large <= small + 10, (small, large)
    assert ctl_large >= ctl_small + 2000, (ctl_small, ctl_large)
    assert 1.6 * small <= more_recent <= 2.4 * small + 10, (small, more_recent)


def test_recipients_active_sent_reads_only_the_thirty_minute_key_window():
    """f38d28 item 5: the recipients route's measured-activity read. Older history stays outside the read; traffic in
    the window costs linearly. Same harness and sensitivity control as the fleet last-send test above."""
    import datetime as _d
    from apps.mesh.work import ACTIVE_SENT_SQL, ACTIVE_WINDOW_SECS, DAEMON_THREADS
    sql = ACTIVE_SENT_SQL.replace("shared_memory", "fa_memory")
    now = _d.datetime.now(_d.timezone.utc)
    start = now - _d.timedelta(seconds=ACTIVE_WINDOW_SECS)
    peer = str(uuid.uuid4())
    params = ["shared/mesh-chat", ACTIVE_WINDOW_SECS, [peer], ACTIVE_WINDOW_SECS, list(DAEMON_THREADS)]
    # the same statement without its key bound, still grouped (a GROUP BY-less control raises GroupingError: Codex on #583)
    control = (sql.split("AND key ", 1)[0] + "AND created_by_peer_id = %s::uuid GROUP BY created_by_peer_id")
    assert control != sql
    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TEMP TABLE fa_memory (namespace text NOT NULL, key text NOT NULL, version int NOT NULL,"
                        " is_head boolean NOT NULL, created_by_peer_id uuid NOT NULL, created_at timestamptz NOT NULL,"
                        " value jsonb NOT NULL DEFAULT '{}')")
            cur.execute("CREATE UNIQUE INDEX fa_nkv ON fa_memory (namespace, key, version)")
            cur.execute("CREATE UNIQUE INDEX fa_head ON fa_memory (namespace, key) WHERE is_head")

            def measure(old, recent):
                cur.execute("TRUNCATE fa_memory")
                stamps = ([start - _d.timedelta(hours=2, seconds=i) for i in range(old)]
                          + [now - _d.timedelta(seconds=i) for i in range(recent)])
                cur.executemany("INSERT INTO fa_memory (namespace, key, version, is_head, created_by_peer_id, created_at)"
                                " VALUES ('shared/mesh-chat', %s, 1, true, %s::uuid, %s)",
                                [(f"msg/{int(at.timestamp() * 1000)}-{i}", peer, at) for i, at in enumerate(stamps)])
                cur.execute("ANALYZE fa_memory")
                bounded = _heap_rows_read(c, sql, params)
                unbounded = _heap_rows_read(c, control, [params[0], peer])
                cur.execute(sql, params)
                assert cur.fetchone()[1] == now
                return bounded, unbounded, plan_text(c, sql, params)
            small, ctl_small, _ = measure(2000, 20)
            large, ctl_large, shape = measure(8000, 20)
            more_recent, _, _ = measure(2000, 40)
    assert "Index" in shape, shape
    assert large <= small + 10, (small, large)
    assert ctl_large >= ctl_small + 2000, (ctl_small, ctl_large)
    assert 1.6 * small <= more_recent <= 2.4 * small + 10, (small, more_recent)


DIRECTIVE_RATE = (
    "SELECT count(*) FROM (SELECT 1 FROM dr_memory WHERE namespace = %s AND key >= %s AND key < %s "
    "AND created_by_peer_id = %s::uuid AND created_at > now() - interval '1 hour' AND value ? 'directive' "
    "LIMIT %s) AS recent")


def _plant_mesh(cur, *, old: int, recent: int, peer: str) -> None:
    import datetime as _d
    now_ms = int(_d.datetime.now(_d.timezone.utc).timestamp() * 1000)
    rows = []
    for i in range(old):                                       # history, days old: outside the key range
        rows.append((f"msg/{now_ms - 86_400_000 * 3 - i * 1000}-x-{i:06x}", peer, "2026-01-01T00:00:00+00:00"))
    for i in range(recent):
        rows.append((f"msg/{now_ms - i * 1000}-x-{i:06x}", peer, _d.datetime.now(_d.timezone.utc).isoformat()))
    cur.executemany("INSERT INTO dr_memory (namespace, key, version, created_by_peer_id, created_at, value) "
                    "VALUES ('shared/mesh-chat', %s, 1, %s::uuid, %s, '{\"directive\": {}}'::jsonb)", rows)
    cur.execute("ANALYZE dr_memory")


def test_the_directive_rate_count_does_not_scale_with_history():
    """the directive rule's rate count is one hour of msg/ keys on the (namespace, key) index: four times the history leaves
    its row estimate where it was, and the plan uses that index, not a scan."""
    peer = str(uuid.uuid4())
    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TEMP TABLE dr_memory (namespace text NOT NULL, key text NOT NULL, version int NOT NULL,"
                        " created_by_peer_id uuid NOT NULL, created_at timestamptz NOT NULL, value jsonb NOT NULL)")
            cur.execute("CREATE UNIQUE INDEX dr_nkv ON dr_memory (namespace, key, version)")
            import datetime as _d
            now_ms = int(_d.datetime.now(_d.timezone.utc).timestamp() * 1000)
            params = ["shared/mesh-chat", f"msg/{now_ms - 3_600_000}", f"msg/{now_ms + 600_000}", peer, 31]

            def est():
                cur.execute("EXPLAIN (FORMAT JSON) " + DIRECTIVE_RATE, params)
                plan = cur.fetchone()[0]
                return max(_walk_rows(plan[0]["Plan"])), json.dumps(plan)
            _plant_mesh(cur, old=2000, recent=20, peer=peer)
            small, _ = est()
            cur.execute("TRUNCATE dr_memory")
            _plant_mesh(cur, old=8000, recent=20, peer=peer)
            large, plan = est()
            cur.execute("DROP TABLE dr_memory")
    assert large <= small * 1.5 + 5, f"the directive rate count's estimate grew from {small} to {large} with history"
    assert "Index" in plan, f"the directive rate count is not using the (namespace, key) index: {plan}"


URGENT_RATE = (
    "SELECT count(*) FROM (SELECT 1 FROM dr_memory WHERE namespace = %s AND key >= %s AND key < %s "
    "AND created_by_peer_id = %s::uuid AND created_at > now() - interval '1 hour' "
    "AND (value ? 'urgent' OR value ? 'redacted') "
    "LIMIT %s) AS recent")


def test_the_urgent_rate_count_does_not_scale_with_history():
    """The urgent lane's allowance count (handlers._urgent_count): the same key range as the directive count, so four
    times the history leaves its estimate where it was, on the (namespace, key) index."""
    peer = str(uuid.uuid4())
    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TEMP TABLE dr_memory (namespace text NOT NULL, key text NOT NULL, version int NOT NULL,"
                        " created_by_peer_id uuid NOT NULL, created_at timestamptz NOT NULL, value jsonb NOT NULL)")
            cur.execute("CREATE UNIQUE INDEX dr_nkv ON dr_memory (namespace, key, version)")
            import datetime as _d
            now_ms = int(_d.datetime.now(_d.timezone.utc).timestamp() * 1000)
            params = ["shared/mesh-chat", f"msg/{now_ms - 3_600_000}", f"msg/{now_ms + 600_000}", peer, 5]

            def est():
                cur.execute("EXPLAIN (FORMAT JSON) " + URGENT_RATE, params)
                plan = cur.fetchone()[0]
                return max(_walk_rows(plan[0]["Plan"])), json.dumps(plan)
            _plant_mesh(cur, old=2000, recent=20, peer=peer)
            small, _ = est()
            cur.execute("TRUNCATE dr_memory")
            _plant_mesh(cur, old=8000, recent=20, peer=peer)
            large, plan = est()
            cur.execute("DROP TABLE dr_memory")
    assert large <= small * 1.5 + 5, f"the urgent rate count's estimate grew from {small} to {large} with history"
    assert "Index" in plan, f"the urgent rate count is not using the (namespace, key) index: {plan}"


def test_the_server_runs_the_bounded_urgent_rate_count():
    """Pins the server's query to the measured shape above."""
    src = (Path(__file__).resolve().parent.parent / "apps/memoryskills/handlers.py").read_text(encoding="utf-8")
    body = src[src.index("def _urgent_count"):src.index("def _enforce_urgent")]
    assert "key >= %s AND key < %s" in body and "LIMIT %s) AS recent" in body
    assert "(value ? 'urgent' OR value ? 'redacted')" in body


#: Copied verbatim from `pg_indexes` on production's skill_calls, 2026-11-03.
UNREAD_CALLS_INDEX = "CREATE INDEX uc_caller_created ON uc_calls USING btree (caller_peer_id, created_at DESC)"


def _unread_cleared_statement() -> str:
    """The `reads` and `cleared` CTEs of the statement graph.unread RUNS (taken from graph._UNREAD_SQL, not copied),
    ended with a read of the object and re-pointed at the session-local table. Parameters: reader, since, namespace."""
    from apps.mesh import graph
    sql = graph._UNREAD_SQL
    head = sql[:sql.index("pending AS (")].rstrip().rstrip(",")
    assert "skill_calls sc" in head and "cleared AS MATERIALIZED (" in head, head
    return re.sub(r"\bskill_calls\b", "uc_calls", head) + " SELECT by_key FROM cleared"


def _plant_reads(cur, *, reader: str, keys: int, per_key: int, history: int, others: int, since) -> None:
    """`keys` x `per_key` completed mesh reads by `reader` inside the window; `history` of its reads from before
    `since`; `others` reads by other callers inside the window. Every read is the stored shape: the signed payload
    with the memory.read args under `args`, the result with the version it returned."""
    rows = []

    def read(caller, key, at):
        rows.append((caller, at, json.dumps({"args": {"namespace": "shared/mesh-chat", "key": key}}),
                     json.dumps({"op": "memory.read", "memo": {"version": 1}})))
    for k in range(keys):
        for j in range(per_key):
            read(reader, f"msg/{k}", since + _dt.timedelta(minutes=1 + j))
    for h in range(history):
        read(reader, f"msg/old{h}", since - _dt.timedelta(days=1, seconds=h))
    for o in range(others):
        read(str(uuid.uuid4()) if o % 50 == 0 else OTHER_READER, f"msg/{o}", since + _dt.timedelta(seconds=o))
    cur.executemany("INSERT INTO uc_calls (caller_peer_id, created_at, skill_name, status, args, result) "
                    "VALUES (%s::uuid, %s, 'memory.read', 'completed', %s::jsonb, %s::jsonb)", rows)
    cur.execute("ANALYZE uc_calls")


OTHER_READER = str(uuid.uuid4())


def test_the_unread_cleared_aggregate_reads_one_readers_window_and_grows_with_it_alone():
    """#493 (3 Nov 2026): `cleared`'s max ... GROUP BY key is per request (every unread, recipients and fleet backlog
    call). Its declared bound: what it reads is ONE range of skill_calls_caller_created_idx, the reader's own completed
    calls since `since`, folded into one entry per distinct key the reader read in the window. It does not grow with
    the reader's calls before the window, with other callers' calls, or with the pending mail (the pending side makes one
    lookup per message in the result; tests/test_mesh_unread.py holds that it never rescans the reads).

    Measured by what the statement READS (`rows_read`, EXPLAIN ANALYZE), on the server's own CTE text against a temp
    table with production's index:
      - four times the history before the window and four times the other callers' reads: the same rows read;
      - twice the distinct keys read in the window: about twice the rows read, linear and not more;
      - CONTROLS: the object holds exactly the planted keys at each size, and the same instrument, with the window
        bound taken out of the statement, does see the history grow (so a flat reading is a reading)."""
    import datetime as _d
    sql = _unread_cleared_statement()
    reader = str(uuid.uuid4())
    since = _d.datetime.now(_d.timezone.utc) - _d.timedelta(days=2)
    params = [reader, since, "shared/mesh-chat"]

    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TEMP TABLE uc_calls (caller_peer_id uuid NOT NULL, created_at timestamptz NOT NULL,"
                        " skill_name text NOT NULL, status text NOT NULL, args jsonb, result jsonb)")
            cur.execute(UNREAD_CALLS_INDEX)

            def measure(**sizes):
                cur.execute("TRUNCATE uc_calls")
                _plant_reads(cur, reader=reader, since=since, **sizes)
                n = rows_read(c, sql, params)
                cur.execute(sql, params)
                return n, cur.fetchone()[0]

            base, obj = measure(keys=100, per_key=2, history=1000, others=4000)
            assert len(obj) == 100, f"the object must hold the 100 planted keys, held {len(obj)}"
            shape = plan_text(c, sql, params)
            assert "uc_caller_created" in shape, f"the reads are not one range of the (caller, created_at) index: {shape}"
            more_history, obj = measure(keys=100, per_key=2, history=4000, others=16000)
            assert len(obj) == 100, len(obj)
            more_keys, obj = measure(keys=200, per_key=2, history=1000, others=4000)
            assert len(obj) == 200, len(obj)

            unbounded = sql.replace("AND sc.created_at >= %s ", "AND sc.created_at >= %s - interval '30 days' ")
            assert unbounded != sql, "the window bound was not found in the statement"
            cur.execute("TRUNCATE uc_calls")
            _plant_reads(cur, reader=reader, since=since, keys=100, per_key=2, history=1000, others=4000)
            ctl_small = rows_read(c, unbounded, params)
            cur.execute("TRUNCATE uc_calls")
            _plant_reads(cur, reader=reader, since=since, keys=100, per_key=2, history=4000, others=16000)
            ctl_large = rows_read(c, unbounded, params)
            cur.execute("DROP TABLE uc_calls")

    assert ctl_large >= ctl_small + 2000, (
        f"CONTROL: without the window bound the reads of history must be seen to grow ({ctl_small} to {ctl_large}); "
        "the instrument cannot measure this statement")
    assert more_history <= base + 10, (
        f"`cleared` read {base} rows, then {more_history} with four times the history and the other callers' reads: "
        f"it is not bounded to the reader's window. Statement: {sql}")
    assert 1.6 * base <= more_keys <= 2.4 * base + 10, (
        f"`cleared` read {base} rows for 100 keys and {more_keys} for 200: the bound is linear in the reader's reads in "
        "the window")


def _fleet_unread_cleared_statement() -> str:
    """The `reads` and `cleared` CTEs of `graph._FLEET_UNREAD_SQL`, re-pointed at `uc_calls`."""
    from apps.mesh import graph
    sql = graph._FLEET_UNREAD_SQL
    start = sql.index("reads AS MATERIALIZED (")
    head = "WITH " + sql[start:sql.index("addressed AS (")].rstrip().rstrip(",")
    assert "skill_calls sc" in head and "cleared AS MATERIALIZED (" in head, head
    return re.sub(r"\bskill_calls\b", "uc_calls", head) + " SELECT by_peer FROM cleared"


def test_the_fleet_unread_cleared_aggregate_reads_covered_readers_window_alone():
    """B5 scaling test for `graph._FLEET_UNREAD_SQL`'s `reads` + `cleared` CTEs across a batch of covered
    readers: uses `uc_caller_created`, does not grow with 4x pre-window history or 4x non-covered callers'
    reads, and grows linearly when covered readers' distinct keys in the window double."""
    import datetime as _d
    sql = _fleet_unread_cleared_statement()
    readers = [str(uuid.uuid4()) for _ in range(4)]
    since = _d.datetime.now(_d.timezone.utc) - _d.timedelta(days=2)
    params = [readers, since, "shared/mesh-chat"]

    def plant_batch(cur, *, keys_per_reader: int, history_per_reader: int, others: int) -> None:
        rows = []

        def add(caller, key, at):
            rows.append((caller, at, json.dumps({"args": {"namespace": "shared/mesh-chat", "key": key}}),
                         json.dumps({"op": "memory.read", "memo": {"version": 1}})))
        for r in readers:
            for k in range(keys_per_reader):
                add(r, f"msg/{k}", since + _d.timedelta(minutes=1 + k))
            for h in range(history_per_reader):
                add(r, f"msg/old-{h}", since - _d.timedelta(days=1, seconds=h))
        for o in range(others):
            add(str(uuid.uuid4()) if o % 50 == 0 else OTHER_READER, f"msg/o-{o}",
                since + _d.timedelta(seconds=o))
        cur.executemany("INSERT INTO uc_calls (caller_peer_id, created_at, skill_name, status, args, result) "
                        "VALUES (%s::uuid, %s, 'memory.read', 'completed', %s::jsonb, %s::jsonb)", rows)
        cur.execute("ANALYZE uc_calls")

    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TEMP TABLE uc_calls (caller_peer_id uuid NOT NULL, created_at timestamptz NOT NULL,"
                        " skill_name text NOT NULL, status text NOT NULL, args jsonb, result jsonb)")
            cur.execute(UNREAD_CALLS_INDEX)

            def measure(**sizes):
                cur.execute("TRUNCATE uc_calls")
                plant_batch(cur, **sizes)
                n = _heap_rows_read(c, sql, params)
                cur.execute(sql, params)
                return n, cur.fetchone()[0]

            base, obj = measure(keys_per_reader=40, history_per_reader=400, others=4000)
            assert set(obj) == set(readers) and all(len(v) == 40 for v in obj.values()), len(obj)
            shape = plan_text(c, sql, params)
            assert "uc_caller_created" in shape, shape
            more_history, obj = measure(keys_per_reader=40, history_per_reader=1600, others=16000)
            assert set(obj) == set(readers) and all(len(v) == 40 for v in obj.values())
            more_keys, obj = measure(keys_per_reader=80, history_per_reader=400, others=4000)
            assert set(obj) == set(readers) and all(len(v) == 80 for v in obj.values())

            unbounded = sql.replace("AND sc.created_at >= %s ", "AND sc.created_at >= %s - interval '30 days' ")
            assert unbounded != sql
            cur.execute("TRUNCATE uc_calls")
            plant_batch(cur, keys_per_reader=40, history_per_reader=400, others=4000)
            ctl_small = _heap_rows_read(c, unbounded, params)
            cur.execute("TRUNCATE uc_calls")
            plant_batch(cur, keys_per_reader=40, history_per_reader=1600, others=16000)
            ctl_large = _heap_rows_read(c, unbounded, params)
            cur.execute("DROP TABLE uc_calls")

    assert ctl_large >= ctl_small + 3000, (ctl_small, ctl_large)
    assert more_history <= base + 16, (base, more_history)
    assert 1.6 * base <= more_keys <= 2.4 * base + 16, (base, more_keys)


def test_the_server_runs_the_bounded_directive_rate_count():
    """The scaling test above measures DIRECTIVE_RATE; this pins that the server's query IS that bounded shape (a key
    range plus LIMIT), so a later edit back to a whole-table count fails here, not in production."""
    src = (Path(__file__).resolve().parent.parent / "apps/memoryskills/handlers.py").read_text(encoding="utf-8")
    body = src[src.index("def _enforce_directive_rate"):src.index("def handle_write")]
    assert "key >= %s AND key < %s" in body and "LIMIT %s) AS recent" in body


# ---- the resident view's rescoring reads (pxx's design A, option (b), condition 1: d4bf7c, 7 Nov 2026) ----------

def _source_constant(rel: str, name: str) -> str:
    """A module-level string constant, read from the source with ast: this file boots no Django."""
    import ast
    tree = ast.parse((Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module-level constant of {rel}")


#: The statement search.stored_vectors runs, read from the source rather than copied.
STORED_VECTORS = _source_constant("apps/meshsem/search.py", "STORED_VECTORS_SQL")


MAIL_SPACE = "embeddinggemma-300m/768/retrieval-v1"
#: Both reads a query on a resident view may make (search._embed_arm, then search._exact_pool) are STORED_VECTORS.
RESCORE_SHAPES = [("the view's top N (DEFAULT_RESCORE_DEPTH)", 256),
                  ("the pool members past N (at most POOL plus the guarantees)", 60)]


@pytest.fixture()
def seeded_embeddings():
    """A session-local copy of mesh_embeddings: the real table's columns, checks and indexes (the primary key and
    mesh_embeddings_space_ns_idx among them; LIKE copies no foreign key), shadowing the real one for this session
    only, since pg_temp is searched first. Seeded with 4,000 rows of the mail space and namespace and 1,000 of
    another namespace, with incompressible 768-d vectors as production stores them, then ANALYZEd. On the shared
    table's handful of rows the planner had chosen the (space, namespace) index with the ids as a filter (pxx
    31f18f P2 3), which at production's size would read the whole namespace."""
    with psycopg.connect(DSN) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TEMP TABLE mesh_embeddings (LIKE public.mesh_embeddings INCLUDING ALL)")
            rows = [(str(uuid.uuid4()), f"msg/{i}", MAIL_SPACE, "0" * 64, os.urandom(4 * 768),
                     "shared/mesh-chat" if i < 4000 else "shared/decisions") for i in range(5000)]
            cur.executemany(
                "INSERT INTO mesh_embeddings (src_id, src_key, src_version, space, dims, text_sha256, vec, "
                "provider_id, recorded_at, namespace) VALUES (%s, %s, 1, %s, 768, %s, %s, 'costbound', now(), %s)",
                rows)
            cur.execute("ANALYZE mesh_embeddings")
        yield c, [r[0] for r in rows[:4000]]
        with c.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS pg_temp.mesh_embeddings")


def _plan_nodes(plan: dict) -> list[dict]:
    return [plan] + [n for child in plan.get("Plans", []) for n in _plan_nodes(child)]


@pytest.mark.parametrize("shape, count", RESCORE_SHAPES)
def test_the_rescoring_reads_probe_the_primary_key(seeded_embeddings, shape, count):
    """The plan: every index it uses is mesh_embeddings_pkey (src_id, space, chunk_no), and nothing scans the table."""
    conn, ids = seeded_embeddings
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + STORED_VECTORS, [ids[:count], MAIL_SPACE, "shared/mesh-chat", 768])
        plan = cur.fetchone()[0][0]["Plan"]
    nodes = _plan_nodes(plan)
    names = {n["Index Name"] for n in nodes if "Index Name" in n}
    assert names == {"mesh_embeddings_pkey"} and not any(n["Node Type"] == "Seq Scan" for n in nodes), (
        f"{shape}: the rescoring read is not a probe of the primary key. Plan: {json.dumps(plan)[:3000]}")


@pytest.mark.parametrize("shape, count", RESCORE_SHAPES)
def test_the_rescoring_reads_touch_no_more_rows_than_ids(seeded_embeddings, shape, count):
    """Run, the read touches at most one row per id asked for (rows returned plus rows its filters removed, over
    every loop), whatever the namespace holds, and finds every id it was given."""
    conn, ids = seeded_embeddings
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + STORED_VECTORS,
                    [ids[:count], MAIL_SPACE, "shared/mesh-chat", 768])
        plan = cur.fetchone()[0][0]["Plan"]
    on_table = [n for n in _plan_nodes(plan) if n.get("Relation Name") == "mesh_embeddings"]
    touched = sum((n["Actual Rows"] + n.get("Rows Removed by Filter", 0)) * n["Actual Loops"] for n in on_table)
    assert on_table and touched <= count, (
        f"{shape}: the read touched {touched} rows for {count} ids. Plan: {json.dumps(plan)[:3000]}")
    assert plan["Actual Rows"] == count, f"{shape}: found {plan['Actual Rows']} of {count} ids"


def test_the_rescoring_read_has_no_aggregate():
    """It joins no ledger: a new aggregate here would fail test_every_sql_aggregate_under_apps_is_on_the_ledger."""
    assert not _AGG.search(STORED_VECTORS) and _SQL.search(STORED_VECTORS)
