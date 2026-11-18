"""v1.1 step 6b acceptance at scale: the context vector store (apps/meshsem/ctxindex.py, route B) under N synthetic vectors.

docs/design/context-v1.1-transcript-ingest.md step 6b asks for p95 filtered search under 1.5 s with 15 million synthetic
vectors loaded, recall@10 within 2 points of exact scoring, and a vector older than 180 days gone while its item still
reads by key. tests/test_ctx_vector_store.py proves the rules on a handful of rows and says the latency acceptance is a
separate HQB measurement: this is that measurement.

What it does, in phases (--phase, default all):
  load       N chunks with their vectors in a space and namespace of this run's own, spread over seats and months with
             a skew (one busy seat), plus the ctx_source_gen row the walker writes for every source, so retrieval takes
             production's path and not the max(gen) fallback. The chunk trigger stays ON, as in production.
  disk       right after the load (also its own phase on a loaded run): each table the index writes, its main fork,
             TOAST and every index by name, and a linear projection of those bytes to --project-to vectors (default
             15 million). It leaves out WAL and the in-process shard cache, which is memory.
  search     query classes (unfiltered, busy seat, busy seat last 30 days, sparse seat one month, one src), each with Q
             query vectors drawn near a real chunk of that class's subset. Every search runs through ctxindex.search on
             the APP role inside rls.server_internal, as the route does. Reports p50/p95 cold (cache forgotten) and warm,
             the coverage block, and recall@10 against exact top-10 over the class's WHOLE filtered subset (streamed
             from the database, one pass per class).
  retention  one run_retention_job tick, timed, with the share of rows older than 180 days that the load planted;
             then a second tick when nothing is old (the worst case: what the DELETE scans to find nothing), with the
             EXPLAIN of its subquery. The job is production's, so it is not scoped to this run: it deletes every
             space's chunks older than 180 days in the scratch database, which is one more reason it is scratch only.
  clean      delete this run's chunks, vectors, ctx_source_gen and ctx_chunk_changes rows.

Scratch only: both DSNs must name a database whose name starts with "bench" or "scratch", or nothing connects. The load
is heavy (15 million 768-d vectors is about 46 GB of vector bytes plus the chunk rows): run it on HQB's scratch cluster,
never on HQD beside production.

Run:  python tools/ctxbench/bench_ctx_store.py --owner-dsn ... --app-dsn ... --vectors 15000000 --json-out report.json

Use a FRESH scratch database for each load that the disk phase measures: deleted rows keep their file space. --app-dsn
must log in as a role that may SET ROLE meshychat_app (dbconn.connect does); the owner DSN for both works on scratch.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
UTC = _dt.timezone.utc
#: A database name the harness may load into. Anything else is refused before any connection is made.
SCRATCH_DB = re.compile(r"^(bench|scratch)[a-z0-9_]*$")
CHUNKS_PER_SRC = 200
CHUNKS_PER_BATCH = 1000
COMMIT_EVERY = 50_000
CENTROIDS = 1000
OLD_SHARE = 0.01              # rows planted older than RETENTION_DAYS, so the first retention tick has work
#: chunk_no of after_write's rows: above any load's run-wide counter (a load is far below a billion rows), so the
#: (src_id, space, chunk_no) key stays unique.
WRITE_CHUNK_NO = 1_000_000_000


#: HQD serves production: a bench* database on its cluster would still load tens of GB beside it (pxx on #410). Its LAN
#: address is refused as a DSN host, and the harness refuses to run on the machine itself (any host, socket included).
PRODUCTION_HOSTS = frozenset({"192.168.50.20"})
PRODUCTION_MACHINES = frozenset({"desktop-3k517q2"})


def _clock() -> float:
    """Every timed span reads this, so a test can drive it."""
    return time.perf_counter()


class Refused(SystemExit):
    pass


def scratch_only(dsn: str) -> str:
    """The database name of `dsn`, or Refused when it is not a scratch database or names production's host."""
    from psycopg.conninfo import conninfo_to_dict
    try:
        parts = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001 - any unparsable DSN is refused, not guessed at
        raise Refused(f"REFUSED: the DSN does not parse ({type(exc).__name__})") from None
    name = parts.get("dbname") or ""
    if not SCRATCH_DB.match(name):
        raise Refused(f"REFUSED: database {name!r} is not a scratch database (bench* or scratch*); nothing connected")
    hosts = {h.strip().lower() for key in ("host", "hostaddr")
             for h in str(parts.get(key) or "").split(",") if h.strip()}
    if hosts & PRODUCTION_HOSTS:
        raise Refused("REFUSED: the DSN names production's host; nothing connected")
    return name


def _machine() -> str:
    import socket
    return socket.gethostname()


def off_production(machine: str) -> None:
    """Refused on a machine that serves production, whatever the DSN says."""
    if machine.strip().lower() in PRODUCTION_MACHINES:
        raise Refused(f"REFUSED: {machine} serves production; run the harness on HQB's scratch cluster")


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (p in 0..100) of a non-empty list."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, int(np.ceil(p / 100.0 * len(ordered))))
    return ordered[rank - 1]


def recall_at(found: list[int], truth: list[int], k: int = 10) -> float:
    """|found[:k] & truth[:k]| / min(k, len(truth)); 1.0 when there is no truth to find."""
    want = truth[:k]
    if not want:
        return 1.0
    return len(set(found[:k]) & set(want)) / len(want)


@dataclasses.dataclass(frozen=True)
class Plan:
    """How N chunks are spread: per seat, per month (newest month index 0)."""
    vectors: int
    seats: int
    months: int
    counts: tuple[tuple[int, ...], ...]          # counts[seat][month]

    @property
    def total(self) -> int:
        return sum(sum(row) for row in self.counts)

    def seat_total(self, seat: int) -> int:
        return sum(self.counts[seat])


def plan(vectors: int, seats: int, months: int) -> Plan:
    """Seat 0 is busy (40%), seat seats-1 is sparse (0.5%), the rest share what is left; each seat's rows spread evenly
    over the months, remainders to the newest. Counts sum to exactly `vectors`."""
    if seats < 4 or months < 1 or vectors < seats * months:
        # From 4 seats each middle seat's share (0.595 / (seats - 2)) is under the busy seat's 0.40.
        raise ValueError("need at least 4 seats, 1 month and one row per seat-month")
    shares = [0.40] + [0.595 / (seats - 2)] * (seats - 2) + [0.005]
    per_seat = [int(vectors * s) for s in shares]
    per_seat[1] += vectors - sum(per_seat)
    counts = []
    for n in per_seat:
        base, extra = divmod(n, months)
        counts.append(tuple(base + (1 if m < extra else 0) for m in range(months)))
    # Every query class reads a populated seat-month: the sparse seat's 0.5% must cover every month, or the
    # sparse-seat-old-month class draws from an empty subset and the run aborts before its report, retention and clean
    # (Codex on #410: --vectors 1000 with 12 seats and 6 months gave the sparse seat (1, 1, 1, 1, 1, 0)).
    empty = [(seat, m) for seat, row in enumerate(counts) for m, c in enumerate(row) if c == 0]
    if empty:
        raise ValueError(f"seat-months with no rows {empty[:3]}: the sparse seat gets 0.5% of the vectors, so this "
                         f"shape needs at least {200 * months} vectors (it was given {vectors})")
    return Plan(vectors, seats, months, tuple(counts))


def month_start(now: _dt.datetime, back: int) -> _dt.datetime:
    y, m = now.year, now.month - back
    while m <= 0:
        y, m = y - 1, m + 12
    return _dt.datetime(y, m, 1, tzinfo=UTC)


def seat_name(i: int) -> str:
    return f"bench-seat-{i:02d}"


# ------------------------------------------------------------------ database phases (scratch only)

def verify_server(conn) -> str:
    """The check that cannot be talked past: on the server itself, after connecting. The DSN check above is only a
    cheap first gate; libpq also connects through hostaddr, PGHOST, PGHOSTADDR or any DNS name for a host (pxx on
    #410). Refused unless the connected database's own name is a scratch name and the server's address is not
    production's; the connection is closed on refusal. A unix-socket connection has no address, and the machine check
    (off_production) already refuses HQD itself."""
    db, addr = conn.execute("SELECT current_database(), host(inet_server_addr())").fetchone()
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 - an autocommit connection has nothing to roll back
        pass
    if not SCRATCH_DB.match(str(db)) or (addr is not None and str(addr) in PRODUCTION_HOSTS):
        conn.close()
        raise Refused(f"REFUSED: connected to database {db!r} at {addr!r}, which is not a scratch database off "
                      "production; closed before any read or write")
    return str(db)


def _connect(dsn: str, **kw):
    import psycopg
    conn = psycopg.connect(dsn, **kw)
    verify_server(conn)
    return conn


def _setup_django():
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
    import django
    django.setup()


def _manifest_row(owner, ns: str) -> str:
    """One signed manifest row in `ns`: the shared_memory parent every synthetic chunk names (the FK needs one)."""
    from tests import test_mesh_authorship as mesh
    from tests.fixtures import signed_rows as sr
    author = mesh._peer(owner, "ctxbench")
    mesh._admit(owner, ns, author)
    return str(sr.insert(owner, sr.row(author["peer_id"], author["owner_id"], ns,
                                       f"ctxm/bench/{uuid.uuid4().hex}/0001/000001",
                                       {"schema": "context.v1", "kind": "manifest"},
                                       author_label=author["fallback"])))


def load(owner_dsn: str, space_name: str, ns: str, dims: int, p: Plan, seed: int, now: _dt.datetime) -> dict:
    from apps.meshsem import spaces
    rng = np.random.default_rng(seed)
    centroids = rng.normal(size=(CENTROIDS, dims)).astype(np.float32)
    started = time.monotonic()
    n = 0
    with _connect(owner_dsn, autocommit=False) as owner:
        src_id = _manifest_row(owner, ns)
        owner.commit()
        sources = set()
        for seat in range(p.seats):
            for month in range(p.months):
                count = p.counts[seat][month]
                lo = month_start(now, month)
                hi = now if month == 0 else month_start(now, month - 1)
                span = (hi - lo).total_seconds()
                done = 0
                while done < count:
                    take = min(COMMIT_EVERY, count - done)
                    pick = rng.integers(0, CENTROIDS, size=take)
                    vecs = centroids[pick] + 0.5 * rng.normal(size=(take, dims)).astype(np.float32)
                    offs = np.sort(rng.random(take)) * span
                    old = rng.random(take) < OLD_SHARE
                    whens = [lo + _dt.timedelta(seconds=float(offs[i])) if not old[i]
                             else now - _dt.timedelta(days=200 + int((done + i) % 30)) for i in range(take)]
                    with owner.cursor() as cur:
                        # A planted old row's vector is as old as its chunk, so the orphan sweep's grace does not hide it.
                        with cur.copy("COPY mesh_ctx_vectors (space, text_sha256, dim, vector, created_at) "
                                      "FROM STDIN") as cp:
                            for i in range(take):
                                cp.write_row((space_name, f"{n + i:064x}", dims, spaces.encode(vecs[i]),
                                              whens[i] if old[i] else now))
                        cols = ("src_id, space, namespace, chunk_no, seat, src, gen, batch, first_ord, last_ord, "
                                "char_start, char_end, time_first, time_last, text_sha256")
                        with cur.copy(f"COPY mesh_ctx_chunks ({cols}) FROM STDIN") as cp:
                            for i in range(take):
                                k = done + i
                                src = f"m{month}-s{k // CHUNKS_PER_SRC}"
                                sources.add((seat_name(seat), src))
                                # chunk_no is the run-wide counter: every chunk names the one manifest, and 047 keeps
                                # (src_id, space, chunk_no) UNIQUE (pxx's AY-4 on #410).
                                cp.write_row((src_id, space_name, ns, n + i, seat_name(seat), src, 1,
                                              k // CHUNKS_PER_BATCH, k, k, 0, 1, whens[i], whens[i], f"{n + i:064x}"))
                    owner.commit()
                    done += take
                    n += take
        with owner.cursor() as cur:
            with cur.copy("COPY ctx_source_gen (space, namespace, seat, src, gen) FROM STDIN") as cp:
                for seat, src in sorted(sources):
                    cp.write_row((space_name, ns, seat, src, 1))
        owner.commit()
        owner.execute("ANALYZE mesh_ctx_chunks")
        owner.execute("ANALYZE mesh_ctx_vectors")
        owner.commit()
    return {"rows": n, "sources": len(sources), "seconds": round(time.monotonic() - started, 1), "src_id": src_id}


@dataclasses.dataclass(frozen=True)
class QueryClass:
    name: str
    seat: str | None = None
    src: str | None = None
    since: _dt.datetime | None = None
    until: _dt.datetime | None = None
    #: Where query vectors are drawn: "subset" (anywhere the filters select) or "searched" (inside the shards a search
    #: of this class scores, the shape of step 8's resumption questions about recent work).
    draw: str = "subset"


def classes(p: Plan, now: _dt.datetime) -> list[QueryClass]:
    sparse = seat_name(p.seats - 1)
    return [QueryClass("unfiltered"),
            QueryClass("busy-seat", seat=seat_name(0)),
            QueryClass("busy-seat-recent", seat=seat_name(0), draw="searched"),
            QueryClass("busy-seat-30d", seat=seat_name(0), since=now - _dt.timedelta(days=30)),
            QueryClass("sparse-seat-old-month", seat=sparse, since=month_start(now, p.months - 1),
                       until=month_start(now, p.months - 2) if p.months > 1 else now),
            QueryClass("one-src", seat=seat_name(0), src=one_src(p))]


def one_src(p: Plan) -> str:
    """A source the load creates on the busy seat: its fourth in the month before this one (the shape step 8 asks
    about), or the last there is when the plan is too small for four (Codex on #410: a hardcoded m0-s3 does not exist at
    --vectors 1000 --seats 4 --months 1, and an empty subset aborted the run before retention and clean)."""
    month = 1 if p.months > 1 and p.counts[0][1] > 0 else 0
    return f"m{month}-s{min(3, (p.counts[0][month] - 1) // CHUNKS_PER_SRC)}"


def _where(qc: QueryClass, space_name: str, ns: str) -> tuple[str, list]:
    from apps.meshsem import ctxindex
    from apps.meshsem import spaces
    sp = spaces.Space(name=space_name, family="bench", dims=1, document_prompt="{text}", query_prompt="{text}",
                      canaries=())
    f = ctxindex.Filters(namespace=ns, seat=qc.seat, src=qc.src, since=qc.since, until=qc.until)
    return f.sql_latest(sp)


def shard_clause(shards) -> tuple[str, list]:
    """SQL selecting exactly the rows of the searched shards, each reported as (seat, "YYYY-MM", lo id, hi id): the rows
    a sharded search scored, before the filters inside it. Empty when nothing was searched."""
    parts, args = [], []
    for seat, month, lo, hi in shards:
        y, m = (int(x) for x in str(month).split("-"))
        start = _dt.datetime(y, m, 1, tzinfo=UTC)
        end = _dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
        parts.append("(c.seat = %s AND c.time_last >= %s AND c.time_last < %s AND c.id BETWEEN %s AND %s)")
        args.extend([seat, start, end, int(lo), int(hi)])
    return ("(" + " OR ".join(parts) + ")" if parts else "FALSE"), args


def _exact_topk(owner, where: str, args: list, queries: np.ndarray, dims: int, k: int) -> list[list[int]]:
    """Exact top-k chunk ids over the rows `where` selects, one streamed pass for all the queries."""
    qn = queries / np.linalg.norm(queries, axis=1, keepdims=True)
    best_s = np.full((len(qn), k), -np.inf, dtype=np.float32)
    best_i = np.full((len(qn), k), -1, dtype=np.int64)
    # A named (server-side) cursor needs a transaction block; the owner connection is autocommit (pxx's AY-5 on #410).
    with owner.transaction(), owner.cursor(name=f"exact_{uuid.uuid4().hex[:8]}") as cur:
        cur.itersize = 20_000
        cur.execute("SELECT c.id, v.vector FROM mesh_ctx_chunks c JOIN mesh_ctx_vectors v ON v.space = c.space "
                    f"AND v.text_sha256 = c.text_sha256 WHERE {where}", args)
        while True:
            rows = cur.fetchmany(20_000)
            if not rows:
                break
            rows = [r for r in rows if len(bytes(r[1])) == 4 * dims]       # as _matrix: a wrong-dim row is skipped
            if not rows:
                continue
            ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            mat = np.frombuffer(b"".join(bytes(r[1]) for r in rows), dtype="<f4").reshape(-1, dims)
            mat = mat / np.linalg.norm(mat, axis=1, keepdims=True)
            scores = qn @ mat.T
            for q in range(len(qn)):
                s = np.concatenate([best_s[q], scores[q]])
                i = np.concatenate([best_i[q], ids])
                top = np.argsort(-s, kind="stable")[:k]
                best_s[q], best_i[q] = s[top], i[top]
    return [[int(x) for x in row if x >= 0] for row in best_i]


def _query_vectors(owner, where: str, args: list, dims: int, count: int, rng, name: str) -> np.ndarray:
    """`count` query vectors, each a random chunk of `where` plus noise, so the rows hold a true neighbour. Ids are
    sampled first and only those rows are joined to their vectors (pxx on #410: not a join of the whole subset)."""
    ids = [r[0] for r in owner.execute(f"SELECT c.id FROM mesh_ctx_chunks c WHERE {where} ORDER BY random() LIMIT %s",
                                       [*args, count]).fetchall()]
    if not ids:
        raise SystemExit(f"query class {name} selects no rows: the load plan and the classes disagree")
    rows = owner.execute("SELECT v.vector FROM mesh_ctx_chunks c JOIN mesh_ctx_vectors v ON v.space = c.space AND "
                         "v.text_sha256 = c.text_sha256 WHERE c.id = ANY(%s)", [ids]).fetchall()
    base = np.stack([np.frombuffer(bytes(r[0]), dtype="<f4") for r in rows])
    base = base[rng.integers(0, len(base), size=count)]
    return (base + 0.05 * rng.normal(size=base.shape)).astype(np.float32)


def _timings(name: str, values: list[float]) -> dict:
    return {f"{name}_p50_s": round(percentile(values, 50), 4), f"{name}_p95_s": round(percentile(values, 95), 4),
            f"{name}_max_s": round(max(values), 4)}


#: How each statement a search runs is labelled in `plans` (a substring of its SQL; the first that matches wins).
STATEMENT_LABELS = (
    ("subset_count", "count(*) AS n FROM (SELECT 1"),
    ("shard_probes", "WITH ORDINALITY AS p(seat, start_, end_, lo, hi, i)"),
    ("latest_gen", "AS p(seat, src)"),
    ("shard_plan", "FROM ctx_shard_plan"),
    ("shard_load", "c.id BETWEEN %s AND %s AND c.current ORDER BY c.id"),
)
_SCAN_TYPES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Index Scan", "Bitmap Heap Scan"}


def label_of(sql: str) -> str:
    for name, needle in STATEMENT_LABELS:
        if needle in sql:
            return name
    return "other"


def chunk_scans(plan: dict) -> dict:
    """What one EXPLAIN (ANALYZE, FORMAT JSON) plan does to mesh_ctx_chunks: every scan node on the table or one of
    its indexes, rows read (returned plus removed, times loops), and whether any is a BITMAP scan, which reads every
    matching index entry before a LIMIT applies (CG-R3 on #436: 40 entries read for 11 rows). The subset count must
    stop at its cap at 15M; this is what the 6b rerun records to show whether it does."""
    nodes, rows, bitmap, stack = [], 0, False, [plan]
    while stack:
        n = stack.pop()
        stack.extend(n.get("Plans", []))
        if n.get("Node Type") not in _SCAN_TYPES:
            continue
        rel, idx = n.get("Relation Name"), n.get("Index Name") or ""
        if rel != "mesh_ctx_chunks" and not idx.startswith("mesh_ctx_chunks"):
            continue
        loops = n.get("Actual Loops", 1) or 0
        read = (n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0)
                + n.get("Rows Removed by Index Recheck", 0)) * loops
        rows += read
        bitmap = bitmap or n["Node Type"].startswith("Bitmap")
        nodes.append(f"{n['Node Type']} {idx or rel} read={int(read)} loops={loops}")
    return {"nodes": sorted(nodes), "chunk_rows_read": int(rows), "bitmap": bitmap}


class _Recording:
    """A connection that records every statement ctxindex.search runs (sql, params), so the plans recorded are those
    of the real search, not SQL rebuilt beside it."""

    def __init__(self, conn):
        self._conn, self.statements = conn, []

    def cursor(self, *a, **kw):
        return _RecordingCursor(self, self._conn.cursor(*a, **kw))

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _RecordingCursor:
    def __init__(self, rec, cur):
        self._rec, self._cur = rec, cur

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)

    def execute(self, sql, params=None, *a, **kw):
        self._rec.statements.append((sql, params))
        return self._cur.execute(sql, params, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def search_plans(app, sp, ns: str, q, f, k: int) -> list[dict]:
    """EXPLAIN (ANALYZE) of every statement one search runs, on the app connection under server_internal (the
    request's RLS), summarised per statement for mesh_ctx_chunks only. The plan cache is cleared first, so the plan
    read itself is recorded too. A statement whose EXPLAIN fails (at 15M, most likely the statement timeout) is
    recorded as {"statement", "error"} and the run goes on: that row is itself a finding (memory-bxxxxx)."""
    import psycopg
    from psycopg.rows import dict_row
    from apps.core import rls
    from apps.meshsem import ctxindex
    rec = _Recording(app)
    ctxindex._plans.clear()
    with rls.server_internal(app):
        ctxindex.search(rec, sp, ns, q, f, k=k)
    app.commit()
    out = []
    for sql, params in rec.statements:
        if "mesh_ctx_chunks" not in sql and "ctx_shard_plan" not in sql:
            continue
        try:
            with rls.server_internal(app):
                with app.cursor(row_factory=dict_row) as cur:
                    cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
                    plan = next(iter(cur.fetchone().values()))[0]["Plan"]
            app.commit()
        except psycopg.Error as e:
            app.rollback()
            out.append({"statement": label_of(sql), "error": f"{type(e).__name__}: {str(e).strip()[:300]}"})
            continue
        out.append({"statement": label_of(sql), **chunk_scans(plan)})
    return out


def search(owner_dsn: str, app_dsn: str, space_name: str, ns: str, dims: int, p: Plan, queries: int, seed: int,
           now: _dt.datetime, k: int = 10) -> dict:
    """Every class, three ways (pxx on #410, P2-2):
      warm_seq    the class's queries one after another with nothing cleared: residency and LRU eviction as they are;
      plan_stale  only the plan cache cleared before each query, shards kept: production's steady state with F3
                  ingesting, where every chunk commit moves ctx_chunk_changes and the next request re-plans;
      cold        plan and shards cleared before each query;
      after_write one committed chunk written before each query in the busy seat's newest month, as F3 ingesting does:
                  the change counter moves, the plan is rebuilt and the newest shard's row count changes, so it reloads
                  (pxx on #410). The write itself is not timed; `write_in_searched` counts the queries whose searched
                  shards include the row just written.
    The timed span is ctxindex.search, the VECTOR ARM only: the route adds lexical, fusion, rerank and the body join.
    Recall twice (P2-1): `scoring` against exact top-k over the rows the search scored (its searched shards, or the
    whole subset on the exact path), `coverage` against exact top-k over the class's whole filtered subset."""
    from psycopg.rows import dict_row
    from apps.core import dbconn, rls
    from apps.meshsem import ctxindex, spaces
    sp = spaces.Space(name=space_name, family="bench", dims=dims, document_prompt="{text}", query_prompt="{text}",
                      canaries=())
    rng = np.random.default_rng(seed + 1)
    out = {"timed_span": "ctxindex.search (vector arm only)", "classes": []}
    with _connect(owner_dsn, autocommit=True) as owner:
        app = dbconn.connect(app_dsn)
        verify_server(app)
        # Truth and query vectors come from the owner connection; the timed searches run on the app one. Both must be
        # the same database, or an empty database's latency is reported as the scale result (Codex on #410).
        loaded, timed = server_identity(owner), server_identity(app)
        if not same_server(loaded, timed):
            app.close()
            raise Refused(f"REFUSED: --app-dsn reaches {timed}, but the run is loaded in {loaded}")

        def one(q, f):
            t = _clock()
            with rls.server_internal(app):
                hits, cov = ctxindex.search(app, sp, ns, q, f, k=k)
            app.commit()
            return _clock() - t, hits, cov

        src_id = owner.execute("SELECT src_id::text FROM mesh_ctx_chunks WHERE space = %s LIMIT 1",
                               (space_name,)).fetchone()[0]
        # Continue after any earlier search's writes on this run: digests and chunk_no stay unique (pxx).
        last = owner.execute("SELECT max(chunk_no) FROM mesh_ctx_chunks WHERE space = %s AND chunk_no >= %s",
                             (space_name, WRITE_CHUNK_NO)).fetchone()[0]
        written = [0 if last is None else int(last) - WRITE_CHUNK_NO]

        def write_one() -> int:
            """One chunk and vector in the busy seat's newest month, committed: the shape of one F3 ingest."""
            written[0] += 1
            digest = f"{0xBE7C << 200 | written[0]:064x}"[-64:]
            when = _dt.datetime.now(UTC)
            owner.execute("INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, %s, %s, %s)",
                          (space_name, digest, dims, spaces.encode(rng.normal(size=dims).astype(np.float32))))
            return owner.execute(
                "INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, first_ord, "
                "last_ord, char_start, char_end, time_first, time_last, text_sha256) VALUES (%s, %s, %s, %s, %s, 'm0-s0', "
                "1, 0, 0, 0, 0, 1, %s, %s, %s) RETURNING id",
                (src_id, space_name, ns, WRITE_CHUNK_NO + written[0], seat_name(0), when, when, digest)).fetchone()[0]

        try:
            ctxindex.forget()
            t = _clock()
            with rls.server_internal(app):
                with app.cursor(row_factory=dict_row) as cur:
                    plan_rows = ctxindex._plan_rows(cur, sp, ns)
            app.commit()
            out["plan_rows_s"], out["plan_shards"] = round(_clock() - t, 3), len(plan_rows)
            for qc in classes(p, now):
                f = ctxindex.Filters(seat=qc.seat, src=qc.src, since=qc.since, until=qc.until)
                where, args = _where(qc, space_name, ns)
                draw_where, draw_args = where, args
                if qc.draw == "searched":
                    probe = rng.normal(size=dims).astype(np.float32)
                    _s, _h, cov = one(probe, f)
                    if cov.path != "sharded":
                        out["classes"].append({"class": qc.name, "draw": qc.draw, "skipped":
                                               f"the probe took the {cov.path} path: no searched shards to draw inside"})
                        continue
                    clause, cargs = shard_clause(cov.shards_searched)
                    draw_where, draw_args = f"{where} AND {clause}", [*args, *cargs]
                qs = _query_vectors(owner, draw_where, draw_args, dims, queries, rng, qc.name)
                ctxindex.forget()
                warm_seq, found, covs = [], [], []
                for q in qs:
                    s, hits, cov = one(q, f)
                    warm_seq.append(s)
                    found.append([h.chunk_id for h in hits])
                    covs.append(cov)
                plan_stale, cold = [], []
                for q in qs:
                    ctxindex._plans.clear()
                    plan_stale.append(one(q, f)[0])
                for q in qs:
                    ctxindex.forget()
                    cold.append(one(q, f)[0])
                coverage_truth = _exact_topk(owner, where, args, qs, dims, k)     # before the writes below
                scoring_truth: list[list[int] | None] = [None] * len(qs)
                groups: dict[tuple, list[int]] = {}
                for i, cov in enumerate(covs):
                    if cov.path == "exact":
                        scoring_truth[i] = coverage_truth[i]
                    else:
                        groups.setdefault(tuple(tuple(s) for s in cov.shards_searched), []).append(i)
                for shards, idx in groups.items():
                    clause, cargs = shard_clause(shards)
                    for i, truth in zip(idx, _exact_topk(owner, f"{where} AND {clause}", [*args, *cargs],
                                                         qs[idx], dims, k)):
                        scoring_truth[i] = truth
                after_write, in_searched = [], 0
                ctxindex.forget()
                one(qs[0], f)                                    # resident first, so only the write's effect is timed
                for q in qs:
                    new_id = write_one()
                    s, _hits, cov = one(q, f)
                    after_write.append(s)
                    in_searched += any(st == seat_name(0) and int(lo) <= new_id <= int(hi)
                                       for st, _m, lo, hi in cov.shards_searched)
                scoring = [recall_at(fd, tr, k) for fd, tr in zip(found, scoring_truth)]
                coverage = [recall_at(fd, tr, k) for fd, tr in zip(found, coverage_truth)]
                out["classes"].append({
                    "class": qc.name, "draw": qc.draw, "queries": len(qs),
                    **{f"vector_arm_{k_}": v for k_, v in _timings("warm_seq", warm_seq).items()},
                    **{f"vector_arm_{k_}": v for k_, v in _timings("plan_stale", plan_stale).items()},
                    **{f"vector_arm_{k_}": v for k_, v in _timings("cold", cold).items()},
                    **{f"vector_arm_{k_}": v for k_, v in _timings("after_write", after_write).items()},
                    "write_in_searched": in_searched,
                    "recall_at_10_scoring_mean": round(float(np.mean(scoring)), 4),
                    "recall_at_10_scoring_min": round(float(np.min(scoring)), 4),
                    "recall_at_10_coverage_mean": round(float(np.mean(coverage)), 4),
                    "paths": sorted({c.path for c in covs}),
                    "plans": search_plans(app, sp, ns, qs[0], f, k),
                    "queries_truncated": sum(1 for c in covs if c.truncated),
                    "coverage_per_query": [{"path": c.path, "subset": c.subset, "subset_at_least": c.subset_at_least,
                                            "scored": c.scored, "truncated": c.truncated,
                                            "searched": [list(x) for x in c.shards_searched],
                                            "skipped": [list(x) for x in c.shards_skipped],
                                            "oldest_covered": None if c.oldest_covered is None
                                            else c.oldest_covered.isoformat()}
                                           for c in covs]})
        finally:
            app.close()
    return out


def retention(owner_dsn: str, space_name: str, ns: str) -> dict:
    from apps.meshsem import ctxindex
    job = type("Job", (), {"max_batch": ctxindex.RETENTION_BATCH})()
    out = {}
    with _connect(owner_dsn, autocommit=False) as owner:
        old = owner.execute("SELECT count(*) FROM mesh_ctx_chunks WHERE space = %s AND time_last < now() - "
                            "make_interval(days => %s)", (space_name, ctxindex.RETENTION_DAYS)).fetchone()[0]
        owner.commit()
        out["old_rows_before"] = int(old)
        for tick in ("first", "until_empty"):
            t = _clock()
            rows = 0
            while True:
                r = ctxindex.run_retention_job(owner, job)
                owner.commit()
                rows += r.rows
                if tick == "first" or not r.more_work_pending:
                    break
            out[f"{tick}_rows"], out[f"{tick}_s"] = rows, round(_clock() - t, 3)
        t = _clock()
        r = ctxindex.run_retention_job(owner, job)
        owner.commit()
        out["nothing_old_tick_rows"], out["nothing_old_tick_s"] = r.rows, round(_clock() - t, 3)
        plan_rows = owner.execute("EXPLAIN SELECT id FROM mesh_ctx_chunks WHERE time_last < now() - "
                                  "make_interval(days => %s) LIMIT %s",
                                  (ctxindex.RETENTION_DAYS, ctxindex.RETENTION_BATCH)).fetchall()
        owner.commit()
        out["nothing_old_plan"] = [r[0] for r in plan_rows]
        out.update(_sweep(owner, space_name))
    return out


def _sweep(owner, space_name: str) -> dict:
    """What retention leaves: run_retention_job deletes chunk rows only, and ctxjob's orphan sweep removes the vectors
    nothing names, ORPHAN_SWEEP at a time, at most once per SWEEP_EVERY_S (pxx's AY-3 on #410). One real sweep, its clock
    reset first, timed; the orphans before and after; and the hours the backlog takes at production's rate. Synthetic
    rows have no items, so "the item still reads by key" is not measured here (tests/test_ctx_vector_store.py has it)."""
    from psycopg.rows import dict_row
    from apps.meshsem import ctxjob, spaces
    sp = spaces.Space(name=space_name, family="bench", dims=1, document_prompt="{text}", query_prompt="{text}",
                      canaries=())
    count_sql = ("SELECT count(*) FROM mesh_ctx_vectors x WHERE x.space = %s AND NOT EXISTS (SELECT 1 FROM "
                 "mesh_ctx_chunks c WHERE c.space = x.space AND c.text_sha256 = x.text_sha256)")
    t = _clock()
    before = int(owner.execute(count_sql, (space_name,)).fetchone()[0])
    owner.commit()
    count_s = round(_clock() - t, 3)
    owner.execute("UPDATE ctx_index_state SET swept_at = NULL WHERE space = %s", (space_name,))
    owner.commit()
    t = _clock()
    with owner.cursor(row_factory=dict_row) as cur:
        swept = ctxjob._sweep_orphans(cur, sp)
    owner.commit()
    sweep_s = round(_clock() - t, 3)
    after = int(owner.execute(count_sql, (space_name,)).fetchone()[0])
    owner.commit()
    # Only what the sweep may delete now: past ORPHAN_GRACE_S and not spared by kept_at (sql_meshychat/050).
    eligible = int(owner.execute(
        count_sql + " AND x.created_at <= now() - make_interval(secs => %s) AND NOT EXISTS (SELECT 1 FROM "
        "ctx_index_state s WHERE s.space = x.space AND s.kept_at IS NOT NULL AND x.created_at >= s.kept_at)",
        (space_name, ctxjob.ORPHAN_GRACE_S)).fetchone()[0])
    owner.commit()
    # A full batch leaves swept_at alone (ctxjob._sweep_orphans), so the backlog clears one batch per job TICK, not one
    # per SWEEP_EVERY_S (pxx). Ticks are reported; the reader multiplies by the job's own interval.
    return {"orphan_vectors_before_sweep": before, "orphan_count_s": count_s, "sweep_vectors": swept,
            "sweep_s": sweep_s, "orphan_vectors_after_sweep": after,
            "sweep_removed_what_it_reported": before - after == swept,
            "orphans_eligible_now": eligible,
            "job_ticks_to_clear_eligible": -(-eligible // ctxjob.ORPHAN_SWEEP)}


DISK_TABLES = ("mesh_ctx_chunks", "mesh_ctx_vectors", "ctx_source_gen", "ctx_chunk_changes")
# A counter the chunk trigger UPDATEs on every write: its dead tuples are old versions of one row, not deleted rows
# (memory-bxxxxx: 71 dead beside 1 live on a fresh load). Only its deletes count against it.
UPDATE_COUNTERS = frozenset({"ctx_chunk_changes"})
# Heap plus TOAST over the vectors' own stored bytes: 1.40 on a fresh load (pxx, memory-bxxxxx: 4,310 B
# over 3,072), 2.8 after a reload into the same database. Above this, the files hold more than this run's vectors.
VECTOR_OVERHEAD_MAX = 2.0
TOAST_SIZED = 2032            # stored bytes per vector from which Postgres moves it out of line (TOAST_TUPLE_THRESHOLD)
PROJECT_TO = 15_000_000


def measure_disk(owner, space_name: str) -> dict:
    """What each table the index writes takes on disk now: the main fork, TOAST plus the free-space and visibility maps
    (a 768-d float32 vector is 3 KB, past the TOAST threshold), and every index by name. Rows are counted for the whole
    table and for this run's space, so a projection can say how much of the table is this run's.

    File sizes include the space of deleted rows until a VACUUM FULL, and a COPY appends rather than reusing it: an
    earlier run cleaned out of the same database inflates every bytes-per-row figure (memory-bxxxxx, 9 KB per
    3 KB vector). A VACUUM zeroes the dead count but does not shrink the files (pxx: 8,602 B per row after
    one), so each table, and its TOAST relation, also reports the rows ever DELETED from it since the statistics were
    reset, which a VACUUM leaves alone. These are the statistics collector's counts and can lag about a second. The
    vector table also reports the stored size of this run's vectors themselves, which no dead space inflates."""
    stat = ("pg_stat_get_dead_tuples({r}), pg_stat_get_tuples_deleted({r})")
    out = {}
    for t in DISK_TABLES:
        main_b, table_b, rows_total, dead, deleted, toast_dead, toast_deleted = owner.execute(
            f"SELECT pg_relation_size(c.oid), pg_table_size(c.oid), (SELECT count(*) FROM {t}), "
            f"{stat.format(r='c.oid')}, {stat.format(r='c.reltoastrelid')} FROM pg_class c WHERE c.oid = %s::regclass",
            (t,)).fetchone()
        rows_run = owner.execute(f"SELECT count(*) FROM {t} WHERE space = %s", (space_name,)).fetchone()[0]
        idx = owner.execute("SELECT indexrelid::regclass::text, pg_relation_size(indexrelid) FROM pg_index "
                            "WHERE indrelid = %s::regclass ORDER BY 1", (t,)).fetchall()
        out[t] = {"main_bytes": int(main_b), "toast_and_maps_bytes": int(table_b) - int(main_b),
                  "indexes": {str(name): int(size) for name, size in idx},
                  "rows_total": int(rows_total), "rows_run": int(rows_run), "dead_tuples": int(dead or 0),
                  "deleted_since_stats_reset": int(deleted or 0), "toast_dead_tuples": int(toast_dead or 0),
                  "toast_deleted_since_stats_reset": int(toast_deleted or 0)}
    out["mesh_ctx_vectors"]["vector_datum_bytes_run"] = int(owner.execute(
        "SELECT coalesce(sum(pg_column_size(vector)), 0) FROM mesh_ctx_vectors WHERE space = %s",
        (space_name,)).fetchone()[0])
    owner.rollback()
    return out


def project(measured: dict, run_vectors: int, target: int = PROJECT_TO) -> dict:
    """Linear projection of `measured` to `target` vectors: each table's bytes per row (whole table) times this run's
    rows per vector times `target`. Linear is right for heap, TOAST and btree leaves to within their fill factors; it
    leaves out WAL, the in-process shard cache (BUDGET_BYTES, memory, not disk) and anything outside DISK_TABLES."""
    if run_vectors <= 0 or target <= 0:
        raise ValueError("run_vectors and target must be positive")
    tables, total, warnings = {}, 0, []
    for t, m in measured.items():
        if m["rows_total"] == 0 or m["rows_run"] == 0:
            tables[t] = {"projected_bytes": None, "why": "no rows of this run to scale from"}
            continue
        share = m["rows_run"] / m["rows_total"]
        if share < 0.9:
            warnings.append(f"{t}: this run is {share:.0%} of the table's rows, so bytes per row average over other "
                            "runs' rows too")
        dead = 0 if t in UPDATE_COUNTERS else max(m.get("dead_tuples", 0), m.get("toast_dead_tuples", 0))
        gone = max(dead, m.get("deleted_since_stats_reset", 0), m.get("toast_deleted_since_stats_reset", 0))
        if gone > 0.01 * m["rows_total"]:
            warnings.append(f"{t}: {gone} rows deleted or dead beside {m['rows_total']} live (dead tuples, or deletes "
                            "since the statistics reset, a VACUUM does not shrink the files): bytes per row read HIGH; "
                            "measure in a fresh database")
        scale = target * (m["rows_run"] / run_vectors) / m["rows_total"]
        heap = (m["main_bytes"] + m["toast_and_maps_bytes"]) * scale
        idx = {name: size * scale for name, size in m["indexes"].items()}
        projected = heap + sum(idx.values())
        tables[t] = {"rows_per_vector": round(m["rows_run"] / run_vectors, 4),
                     "bytes_per_row": round((m["main_bytes"] + m["toast_and_maps_bytes"] + sum(m["indexes"].values()))
                                            / m["rows_total"], 1),
                     "projected_bytes": int(projected), "projected_heap_and_toast_bytes": int(heap),
                     "projected_index_bytes": {name: int(v) for name, v in idx.items()}}
        if m.get("vector_datum_bytes_run"):
            # What the vectors themselves take, without page, TOAST-chunk or dead-row overhead: a floor under the line.
            tables[t]["projected_vector_datum_bytes"] = int(m["vector_datum_bytes_run"] / m["rows_run"] * target
                                                            * (m["rows_run"] / run_vectors))
            # The statistics can lag or be reset; the ratio of file bytes to stored bytes cannot (pxx).
            ratio = (m["main_bytes"] + m["toast_and_maps_bytes"]) / m["rows_total"] / (
                m["vector_datum_bytes_run"] / m["rows_run"])
            tables[t]["heap_and_toast_over_datum"] = round(ratio, 2)
            # Only where the vector dominates the row: at a few dims the tuple header does, and 1.4x does not apply.
            if ratio > VECTOR_OVERHEAD_MAX and m["vector_datum_bytes_run"] / m["rows_run"] >= TOAST_SIZED:
                warnings.append(f"{t}: heap and TOAST are {ratio:.2f}x the vectors' stored bytes (a fresh load is about "
                                "1.4x): the files hold more than this run's vectors; measure in a fresh database")
        total += projected
    return {"target_vectors": target, "from_vectors": run_vectors, "tables": tables,
            "projected_total_bytes": int(total), "projected_total_gib": round(total / 2**30, 1), "warnings": warnings,
            "assumptions": [
                "a FLOOR: measured right after a fresh load, with no dead tuples yet from current flips, re-walks or "
                "retention deletes (pxx)",
                "every table scales at THIS run's rows per vector: for ctx_source_gen and ctx_chunk_changes that is the "
                "synthetic sources-per-vector mix, an assumption about production, not a measurement",
                "excludes WAL, ctx_index_state, the manifest rows and the in-process shard cache (memory, not disk)"]}


def disk(owner_dsn: str, space_name: str, planned_vectors: int, target: int = PROJECT_TO) -> dict:
    """Rows per vector divide by the vectors this run's space HOLDS (mesh_ctx_vectors), not the plan's total, so a load
    that stopped short still projects right (pxx); a mismatch with the plan is reported."""
    with _connect(owner_dsn, autocommit=False) as owner:
        measured = measure_disk(owner, space_name)
    held = measured["mesh_ctx_vectors"]["rows_run"]
    if held <= 0:
        raise Refused(f"REFUSED: space {space_name} holds no vectors to project from")
    out = {"measured": measured, "projection": project(measured, held, target)}
    if held != planned_vectors:
        out["projection"]["warnings"].append(f"the space holds {held} vectors, the plan named {planned_vectors}: "
                                             "projected from what it holds")
    return out


def clean(owner_dsn: str, space_name: str, ns: str) -> dict:
    with _connect(owner_dsn, autocommit=False) as owner:
        n = owner.execute("DELETE FROM mesh_ctx_chunks WHERE space = %s", (space_name,)).rowcount
        v = owner.execute("DELETE FROM mesh_ctx_vectors WHERE space = %s", (space_name,)).rowcount
        g = owner.execute("DELETE FROM ctx_source_gen WHERE space = %s", (space_name,)).rowcount
        owner.commit()
        owner.execute("DELETE FROM ctx_chunk_changes WHERE space = %s", (space_name,))   # after the chunks: they bump it
        owner.commit()
    return {"chunks": n, "vectors": v, "source_gens": g,
            "left": "the manifest shared_memory row and the bench peer _manifest_row made (shared_memory is immutable); "
                    "drop the scratch database to remove them"}


RUN_KEYS = ("run_id", "vectors", "seats", "months", "dims", "seed", "now")


def server_identity(conn) -> dict:
    """Which database this is: its name, the server's address and port, and the cluster's system identifier where the
    role may read it. A run file carries the load's, and a later phase refuses a different one (pxx)."""
    db, addr, port = conn.execute("SELECT current_database(), host(inet_server_addr()), inet_server_port()").fetchone()
    try:
        sysid = conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
    except Exception:  # noqa: BLE001 - pg_control_system needs pg_monitor or superuser; name and address still bind
        conn.rollback()
        sysid = None
    conn.rollback()
    return {"database": str(db), "address": None if addr is None else str(addr),
            "port": None if port is None else int(port), "system_identifier": sysid}


def same_server(recorded: dict, now: dict, *, keep_identifier: bool = False) -> bool:
    """Database, address and port always; the cluster's system identifier when BOTH sides could read it. Only a role
    with pg_monitor or superuser reads pg_control_system(): the app connection runs as meshychat_app, which reads None,
    so requiring equality refused two connections to the same database before any timing (Codex on #410).

    `keep_identifier` (the run file's check): an identifier the load recorded must be read again and match, so a
    scratch cluster recreated under the same host, port and name cannot pass by a later phase not reading it (pxx
    81f56c)."""
    if any(recorded.get(k) != now.get(k) for k in ("database", "address", "port")):
        return False
    a, b = recorded.get("system_identifier"), now.get("system_identifier")
    if keep_identifier and a is not None:
        return a == b
    return a is None or b is None or a == b


def run_params(a, run_file: Path) -> dict:
    """The run's shape. A load (or all) takes it from the command line and records it; every later phase reads it back
    from the run file, never from today's defaults, so a 15M load is searched as 15M and its month boundaries and
    "now" stay the load's (pxx's AY-6 on #410)."""
    if a.phase in ("all", "load"):
        run_id = a.run_id or uuid.uuid4().hex[:8]
        return {"run_id": run_id, "vectors": a.vectors, "seats": a.seats, "months": a.months, "dims": a.dims,
                "seed": a.seed, "now": _dt.datetime.now(UTC).replace(microsecond=0).isoformat()}
    if not a.run_id:
        raise Refused("REFUSED: --run-id is required to search, retain or clean an earlier load")
    if not run_file.is_file():
        raise Refused(f"REFUSED: no run file {run_file}: search, retention and clean read the load's shape from it")
    got = json.loads(run_file.read_text(encoding="utf-8"))
    missing = [k for k in RUN_KEYS if k not in got]
    if missing or got["run_id"] != a.run_id:
        raise Refused(f"REFUSED: run file {run_file} is not this run's (missing {missing})")
    return {**{k: got[k] for k in RUN_KEYS}, "server": got.get("server")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner-dsn", required=True)
    ap.add_argument("--app-dsn", required=True)
    ap.add_argument("--phase", choices=("all", "load", "disk", "search", "retention", "clean"), default="all")
    ap.add_argument("--vectors", type=int, default=100_000)
    ap.add_argument("--seats", type=int, default=12)
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--dims", type=int, default=768)
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--run-id", default=None, help="reuse a loaded run (search/retention/clean phases)")
    ap.add_argument("--run-dir", default=".", help="where the load records its run file and later phases read it")
    ap.add_argument("--project-to", type=int, default=PROJECT_TO, help="vectors the disk projection scales to")
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args(argv)
    for dsn in (a.owner_dsn, a.app_dsn):
        scratch_only(dsn)
    off_production(_machine())
    run_file = Path(a.run_dir) / f"ctxbench-{a.run_id}.json" if a.run_id else None
    params = run_params(a, run_file or Path(a.run_dir) / "ctxbench-none.json")
    run_id = params["run_id"]
    run_file = Path(a.run_dir) / f"ctxbench-{run_id}.json"
    _setup_django()
    dims, seed = int(params["dims"]), int(params["seed"])
    space_name = f"bench-{dims}/{dims}/ctxbench-{run_id}"
    ns = f"shared/ctxbench-{run_id}"
    now = _dt.datetime.fromisoformat(params["now"])
    p = plan(int(params["vectors"]), int(params["seats"]), int(params["months"]))
    report = {"run_id": run_id, "space": space_name, "namespace": ns, "vectors": p.total, "seats": p.seats,
              "months": p.months, "dims": dims, "queries_per_class": a.queries, "load_now": now.isoformat(),
              "phase": a.phase,
              "notes": [f"{OLD_SHARE:.0%} of the rows are planted older than 180 days and are still present during the "
                        "search phase: retention runs after it",
                        "timings are the vector arm (ctxindex.search) only, not the route",
                        "synthetic recall is a proxy: step 6b's recall line names the step 8 questions",
                        "--dims generates its own corpus: runs at two dims are two corpora, not Matryoshka truncations "
                        "of one, so they do not compare for step 6b's dimension choice"]}
    with _connect(a.owner_dsn, autocommit=False) as probe:
        server = server_identity(probe)
    if a.phase in ("all", "load"):
        params["server"] = server
    elif not same_server(params.get("server") or {}, server, keep_identifier=True):
        raise Refused(f"REFUSED: run {run_id} was loaded into {params.get('server')}, this is {server}")
    report["server"] = server
    from apps.meshsem import ctxindex
    report["constants"] = {"EXACT_MAX": ctxindex.EXACT_MAX, "SHARD_MAX": ctxindex.SHARD_MAX,
                           "MAX_SHARDS_PER_QUERY": ctxindex.MAX_SHARDS_PER_QUERY, "BUDGET_BYTES": ctxindex.BUDGET_BYTES}
    if a.phase in ("all", "load"):
        report["load"] = load(a.owner_dsn, space_name, ns, dims, p, seed, now)
        run_file.write_text(json.dumps(params) + "\n", encoding="utf-8")
        report["run_file"] = str(run_file)
    if a.phase in ("all", "load", "disk"):
        # Right after the load, before search writes its after_write rows and retention deletes the planted old ones.
        report["disk"] = disk(a.owner_dsn, space_name, p.total, a.project_to)
    if a.phase in ("all", "search"):
        report["search"] = search(a.owner_dsn, a.app_dsn, space_name, ns, dims, p, a.queries, seed, now)
    if a.phase in ("all", "retention"):
        report["retention"] = retention(a.owner_dsn, space_name, ns)
    if a.phase in ("all", "clean"):
        report["clean"] = clean(a.owner_dsn, space_name, ns)
    text = json.dumps(report, indent=2, default=str)
    if a.json_out:
        Path(a.json_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
