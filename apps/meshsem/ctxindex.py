"""The transcript-context vector store, ROUTE B (v1.1 step 6b, docs/design/context-v1.1-transcript-ingest.md): no Postgres
image or extension change. Reads mesh_ctx_chunks and mesh_ctx_vectors (sql_meshychat/047), which apps/meshsem/ctxjob.py
writes. Step 7's search route is the caller; nothing here is wired to a route yet.

THE RULES (design 6b, route B, after pxx L4):
  1. A SHARD is (seat, month, [lo, hi]): the chunks of one seat whose time_last falls in one UTC month, in chunk-id
     order, cut every SHARD_MAX rows; its id range is FIXED in the query's plan and the shard is loaded by that range,
     never by an ordinal OFFSET, so a write between planning and loading cannot shift which rows a shard holds (pxx
     a00354 P2-3). Ids only grow, so no later insert lands inside a planned range: a range's row count is its version.
  2. A FILTERED SET OF EXACT_MAX CHUNKS OR FEWER never depends on residency: its vectors are read straight from the
     database and scored exactly. That is the path a filtered old needle takes.
  3. RESIDENT BUDGET: shards stay in memory under BUDGET_BYTES with least-recently-used eviction; at most
     MAX_SHARDS_PER_QUERY are searched for one query, and the rest are SKIPPED, never waited for.
  4. A query across many shards searches the NEWEST first within that bound, and its coverage names the shards searched,
     the shards skipped and the oldest time covered. A skipped shard is never reported as searched.
  5. The size lever (256-d Matryoshka vectors) is the space's dims: nothing here assumes 768.

Filters (seat, src, project, since, until) resolve in SQL on mesh_ctx_chunks BEFORE any ranking, and inside a shard the
same filters apply to the resident rows, so a shard never ranks a row the filter excludes.

EVERY READ IS BOUND TO ONE NAMESPACE (pxx P1): the caller passes the namespace it was authorised for, and no row of
another namespace is counted, planned, loaded or returned. ONLY THE NEWEST GEN of each (seat, src) is served (design: a
rewritten source opens gen+1 and the old gen drops out of retrieval; pxx P2-1); older gens stay stored.

Scores are cosines of unit-normalised vectors (search.VectorIndex's convention). A zero or non-finite vector is skipped.
"""
from __future__ import annotations

import collections
import dataclasses
import datetime as _dt
import logging
import sys
import threading
import time

import numpy as np
from psycopg.rows import dict_row, tuple_row

from apps.core import env

from . import spaces

log = logging.getLogger("meshychat.meshsem.ctxindex")

EXACT_MAX = 20_000
SHARD_MAX = 50_000
BUDGET_BYTES = 2 * 1024 ** 3
#: The floor a configured budget must reach (configured_budget). Divergence 108: MESHY_CTX_INDEX_BUDGET_BYTES sets the
#: per-process budget on a box that cannot keep 2 GiB resident. A full 50,000-row shard holds about 196 MiB in CPython
#: 3.13 (a reviewer, 3 Nov 2026), so one unfiltered query over MAX_SHARDS_PER_QUERY shards needs about 783 MiB; a
#: budget below that evicts a query's own shards while it runs. HQD runs at 1 GiB.
BUDGET_BYTES_MIN = 256 * 1024 ** 2
#: Resident shards at most, whatever their size: the byte budget's companion, so the cache is bounded by count too.
CACHED_SHARDS_MAX = 512
MAX_SHARDS_PER_QUERY = 4
#: Step 6b: skip the per-shard match probe where it cannot change the answer (_matching). False probes every shard, as
#: before 29 Oct 2026; tests compare both.
PROBE_SHORTCUT = True
#: Pre-filter narrowed cold shards on chunk metadata before fetching float32 vectors, without polluting ShardCache with
#: partial slices; also reuse observed shard metadata bounds to skip impossible probes. Tests compare True against False.
PREFILTER_COLD_SHARDS = True
#: A resident shard older than this is reloaded on its next use (retention and replaced batches drop rows).
SHARD_MAX_AGE_S = 600
#: Unique digests fetched per mesh_ctx_vectors primary-key batch: small enough that Postgres always walks
#: mesh_ctx_vectors_pkey in sorted key order instead of choosing a self-evicting full-table Seq Scan + Hash Join.
VECTOR_FETCH_BATCH = 2048
#: Maximum entries in the shard metadata bounds cache (each entry holds frozensets of a complete shard's srcs and
#: projects plus time bounds; independent of float32 ShardCache, evicted LRU past SHARD_META_MAX and after SHARD_MAX_AGE_S).
SHARD_META_MAX = 4096

_ROW_COLS = ("c.id, c.src_id::text AS src_id, c.seat, c.src, c.gen, c.batch, c.project, c.first_ord, c.last_ord, "
             "c.char_start, c.char_end, c.time_first, c.time_last, v.dim, v.vector")
_CHUNK_COLS = ("c.id, c.src_id::text AS src_id, c.seat, c.src, c.gen, c.batch, c.project, c.first_ord, c.last_ord, "
               "c.char_start, c.char_end, c.time_first, c.time_last, c.text_sha256")
_JOIN = "FROM mesh_ctx_chunks c JOIN mesh_ctx_vectors v ON v.space = c.space AND v.text_sha256 = c.text_sha256"


#: The newest gen of the chunk row c's source: the recorded one, else the greatest any chunk of it carries.
_LATEST_GEN_SQL = ("COALESCE((SELECT s.gen FROM ctx_source_gen s WHERE s.space = c.space AND s.namespace = c.namespace "
                   "AND s.seat = c.seat AND s.src = c.src), (SELECT max(g.gen) FROM mesh_ctx_chunks g WHERE g.space = c.space "
                   "AND g.namespace = c.namespace AND g.seat = c.seat AND g.src = c.src))")


class Deadline(Exception):
    """A search arm ran past its time budget (apps/meshsem/ctxsearch.py VECTOR_BUDGET_S, LEX_BUDGET_S). Raised BETWEEN
    statements, where a statement_timeout cannot reach: on 7 Nov 2026, 14 searches sat idle in transaction after their
    vector fetch for up to 22 minutes while Python decoded under memory pressure, and production on HQD stopped
    answering from 08:16 to 08:27Z. The transaction is left clean (no statement failed), so the caller may go on."""


def check_deadline(deadline: float | None, what: str) -> None:
    """Raise Deadline when the monotonic `deadline` has passed; None means no budget (every caller before 7 Nov)."""
    if deadline is not None and time.monotonic() >= deadline:
        raise Deadline(f"stopped before {what}: the time budget ran out")


@dataclasses.dataclass(frozen=True)
class Filters:
    namespace: str | None = None                 # set by search(); required there
    seat: str | None = None
    src: str | None = None
    project: str | None = None
    since: _dt.datetime | None = None
    until: _dt.datetime | None = None

    def sql(self, space: spaces.Space) -> tuple[str, list]:
        if not self.namespace:
            raise ValueError("a context index read must name its namespace")
        where, args = ["c.space = %s", "c.namespace = %s"], [space.name, self.namespace]
        for col in ("seat", "src", "project"):
            val = getattr(self, col)
            if val is not None:
                where.append(f"c.{col} = %s")
                args.append(val)
        if self.since is not None:
            where.append("c.time_last >= %s")
            args.append(self.since)
        if self.until is not None:
            where.append("c.time_first < %s")
            args.append(self.until)
        return " AND ".join(where), args

    def sql_latest(self, space: spaces.Space) -> tuple[str, list]:
        """`sql` plus: only the newest INDEXED gen of each (seat, src) in this namespace: ctx_source_gen's, which the
        walker records for every manifest, chunks or not (pxx), else the greatest gen any chunk carries (rows
        written before that record, and tests that write chunks directly).

        `c.current` first (sql_meshychat/051, switch-on blocker 5): the walker keeps it true exactly on the newest
        indexed gen's rows, and the partial indexes on current rows mean the count, the shard probes and the lexical
        arm never walk superseded generations. Those indexes lead on space since sql_meshychat/052: without it the
        planner could AND in the non-partial digest index for c.space and read every row of the space (pxx).
        The latest-gen check stays beside it for rows written without the flag.

        The max(gen) fallback holds only for rows written WITHOUT the walker (tests that insert chunks directly): the
        walker upserts ctx_source_gen in the same transaction as every chunk write since 047, so no walker-written
        source lacks the row. On such a source 051 and the pre-051 predicate can disagree after its newest gen is
        deleted (codex-pxx-cli TASK AR): the older gen stays not current, where max(gen) would serve it."""
        where, args = self.sql(space)
        return (where + " AND c.current AND c.gen = " + _LATEST_GEN_SQL), args

    def keeps(self, row: dict, latest: dict | None = None) -> bool:
        """The same filter on a resident row (shard path); `latest` maps (seat, src) to its newest gen."""
        if latest is not None and latest.get((row["seat"], row["src"])) != row["gen"]:
            return False
        for col in ("seat", "src", "project"):
            val = getattr(self, col)
            if val is not None and row[col] != val:
                return False
        if self.since is not None and row["time_last"] < self.since:
            return False
        if self.until is not None and row["time_first"] >= self.until:
            return False
        return True


@dataclasses.dataclass(frozen=True)
class Hit:
    chunk_id: int
    src_id: str
    seat: str
    src: str
    gen: int
    batch: int
    first_ord: int
    last_ord: int
    char_start: int
    char_end: int
    time_first: _dt.datetime
    time_last: _dt.datetime
    score: float


@dataclasses.dataclass
class Coverage:
    path: str                                   # "exact" or "sharded"
    subset: int                                 # chunks the filters select (at least this many, see subset_at_least)
    scored: int                                 # chunks actually scored
    truncated: bool                             # scored < subset
    shards_searched: list[tuple[str, str, int]] = dataclasses.field(default_factory=list)
    shards_skipped: list[tuple[str, str, int]] = dataclasses.field(default_factory=list)
    oldest_covered: _dt.datetime | None = None
    #: The count stopped at _subset_cap(): more chunks than any search can score, so `truncated` is true regardless.
    subset_at_least: bool = False

    def as_dict(self) -> dict:
        return {"path": self.path, "subset": self.subset, "subset_at_least": self.subset_at_least,
                "scored": self.scored, "truncated": self.truncated,
                "shards_searched": [list(s) for s in self.shards_searched],
                "shards_skipped": [list(s) for s in self.shards_skipped],
                "oldest_covered": None if self.oldest_covered is None else self.oldest_covered.isoformat()}


def _unit(vec: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0 or not np.isfinite(norm):
        return None
    return (vec / norm).astype(np.float32)


def _hit(row: dict, score: float) -> Hit:
    return Hit(int(row["id"]), str(row["src_id"]), row["seat"], row["src"], int(row["gen"]), int(row["batch"]),
               int(row["first_ord"]), int(row["last_ord"]), int(row["char_start"]), int(row["char_end"]),
               row["time_first"], row["time_last"], float(score))


def _order(hits: list[Hit]) -> list[Hit]:
    """Best first; equal scores by chunk id, so the order is stable (pxx INFO)."""
    return sorted(hits, key=lambda h: (-h.score, h.chunk_id))


def _top(rows: list[dict], mat: np.ndarray, qunit: np.ndarray, k: int) -> list[Hit]:
    if not rows:
        return []
    scores = mat @ qunit
    top = min(len(rows), k)
    idx = np.argpartition(-scores, top - 1)[:top]
    return _order([_hit(rows[i], scores[i]) for i in idx])


def _matrix(rows: list[dict], dims: int) -> tuple[list[dict], np.ndarray]:
    """Unit vectors for the rows, and the rows WITHOUT their raw vector bytes (pxx P2-4: a resident shard must
    not keep both). A row whose stored dim is not the space's, or whose vector is zero or not finite, is skipped, not
    a reason to fail the query."""
    kept, vecs = [], []
    for r in rows:
        raw = r.pop("vector")
        if int(r.pop("dim")) != dims:
            continue
        try:
            u = _unit(spaces.decode(raw, dims))
        except ValueError:
            continue
        if u is not None:
            kept.append(r)
            vecs.append(u)
    return kept, (np.vstack(vecs) if vecs else np.zeros((0, dims), dtype=np.float32))


def _fetch_vectors_by_digest(cur, space: spaces.Space, chunk_rows: list[dict], *,
                             deadline: float | None = None) -> tuple[list[dict], np.ndarray]:
    """Fetch and unit-normalise vectors for `chunk_rows` in sorted unique-digest batches over mesh_ctx_vectors_pkey.

    Joining mesh_ctx_chunks directly to mesh_ctx_vectors lets the planner choose a full-table Seq Scan + Hash Join over
    multi-GB mesh_ctx_vectors (self-evicting through Postgres's 256 KB BAS_BULKREAD ring buffer on every request) and
    transfers/decodes duplicate digests repeatedly. Sorted primary-key batches of VECTOR_FETCH_BATCH unique digests
    walk mesh_ctx_vectors_pkey monotonically, decode each digest once, and strip text_sha256 from the kept row dicts.

    `deadline` is checked before every batch (Deadline): the decode between two fetches is where the time went on 7 Nov.
    """
    if not chunk_rows:
        return [], np.zeros((0, space.dims), dtype=np.float32)
    digests = sorted({r["text_sha256"] for r in chunk_rows})
    unit_by_digest: dict[str, np.ndarray] = {}
    for start in range(0, len(digests), VECTOR_FETCH_BATCH):
        check_deadline(deadline, f"vector batch {start // VECTOR_FETCH_BATCH + 1}")
        batch = digests[start:start + VECTOR_FETCH_BATCH]
        cur.execute("SELECT text_sha256, dim, vector FROM mesh_ctx_vectors WHERE space = %s AND text_sha256 = ANY(%s::text[])",
                    (space.name, batch))
        for vr in cur.fetchall():
            if int(vr["dim"]) != space.dims:
                continue
            try:
                u = _unit(spaces.decode(vr["vector"], space.dims))
            except ValueError:
                continue
            if u is not None:
                unit_by_digest[vr["text_sha256"]] = u
    kept, vecs = [], []
    for r in chunk_rows:
        sha = r.pop("text_sha256")
        u = unit_by_digest.get(sha)
        if u is not None:
            kept.append(r)
            vecs.append(u)
    return kept, (np.vstack(vecs) if vecs else np.zeros((0, space.dims), dtype=np.float32))


@dataclasses.dataclass(frozen=True)
class _ShardMeta:
    min_time_first: _dt.datetime | None
    max_time_last: _dt.datetime | None
    srcs: frozenset[str]
    projects: frozenset[str]
    rows: int
    updates: int
    loaded: float


_meta_lock = threading.Lock()
_shard_meta: collections.OrderedDict[tuple, _ShardMeta] = collections.OrderedDict()


def _put_shard_meta(key: tuple, loaded_rows: list[dict], rows: int, updates: int) -> None:
    meta = _ShardMeta(
        min_time_first=min((r["time_first"] for r in loaded_rows), default=None),
        max_time_last=max((r["time_last"] for r in loaded_rows), default=None),
        srcs=frozenset(r["src"] for r in loaded_rows),
        projects=frozenset(r["project"] for r in loaded_rows if r["project"] is not None),
        rows=rows,
        updates=updates,
        loaded=time.monotonic(),
    )
    with _meta_lock:
        if key in _shard_meta:
            _shard_meta.pop(key)
        _shard_meta[key] = meta
        while len(_shard_meta) > SHARD_META_MAX:
            _shard_meta.popitem(last=False)


def _get_shard_meta(key: tuple, rows: int, updates: int) -> _ShardMeta | None:
    with _meta_lock:
        meta = _shard_meta.get(key)
        if meta is None:
            return None
        if meta.rows != rows or meta.updates != updates or time.monotonic() - meta.loaded > SHARD_MAX_AGE_S:
            _shard_meta.pop(key, None)
            return None
        _shard_meta.move_to_end(key)
        return meta


def _meta_excludes(meta: _ShardMeta, f: Filters) -> bool:
    """True only when cached full-shard metadata proves zero rows in the shard can match `f` (including an empty shard;
    conservative, never excludes a shard that holds a matching row)."""
    if meta.max_time_last is None or meta.min_time_first is None:
        return True
    if f.since is not None and meta.max_time_last < f.since:
        return True
    if f.until is not None and meta.min_time_first >= f.until:
        return True
    if f.src is not None and f.src not in meta.srcs:
        return True
    if f.project is not None and f.project not in meta.projects:
        return True
    return False


def _retained_bytes(rows: list[dict], mat: np.ndarray) -> int:
    """What a resident shard actually holds: the matrix, each row's dict and its values (measured, not a constant)."""
    n = mat.nbytes + sys.getsizeof(rows)
    for r in rows:
        n += sys.getsizeof(r) + sum(sys.getsizeof(v) for v in r.values())
    return n


# ------------------------------------------------------------------ the resident shards

class _Shard:
    def __init__(self, key, rows: list[dict], mat: np.ndarray, version: tuple):
        self.key, self.rows, self.mat = key, rows, mat
        #: (rows in the shard's fixed [lo, hi] id range, the space's chunk UPDATE count) at planning. Ids only grow, so
        #: an insert never lands inside a planned range and a delete lowers the count (pxx, a00354); an UPDATE
        #: keeps both, so ctx_chunk_changes.updates is folded in (sql_meshychat/049, codex-pxx-cli round 2 P2-3).
        self.version = version
        self.complete = True
        self.loaded = time.monotonic()
        self.nbytes = _retained_bytes(rows, mat)


def configured_budget() -> int:
    """MESHY_CTX_INDEX_BUDGET_BYTES as whole bytes, or BUDGET_BYTES when it is unset, not a whole number, or below
    BUDGET_BYTES_MIN (each of the last two logged once, at the cache's creation). Divergence 108."""
    raw = env.setting("MESHY_CTX_INDEX_BUDGET_BYTES", "MESHY_CTX_INDEX_BUDGET_BYTES", default=None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return BUDGET_BYTES
    try:
        n = int(raw)
    except (TypeError, ValueError):
        log.warning("ctxindex: MESHY_CTX_INDEX_BUDGET_BYTES is not a whole number of bytes; the budget stays %d",
                    BUDGET_BYTES)
        return BUDGET_BYTES
    if n < BUDGET_BYTES_MIN:
        log.warning("ctxindex: MESHY_CTX_INDEX_BUDGET_BYTES=%d is below the floor of %d; the budget stays %d",
                    n, BUDGET_BYTES_MIN, BUDGET_BYTES)
        return BUDGET_BYTES
    return n


class ShardCache:
    """LRU of resident shards under a byte budget (rule 3). Thread-safe."""

    def __init__(self, budget_bytes: int | None = None):
        self.budget = configured_budget() if budget_bytes is None else budget_bytes
        if self.budget != BUDGET_BYTES:
            log.info("ctxindex: resident shard budget %d bytes (default %d)", self.budget, BUDGET_BYTES)
        self._lock = threading.Lock()
        self._shards: collections.OrderedDict = collections.OrderedDict()
        self.bytes = 0

    def get(self, key, version_now: tuple):
        with self._lock:
            s = self._shards.get(key)
            if s is None:
                return None
            if s.version != version_now or time.monotonic() - s.loaded > SHARD_MAX_AGE_S:
                self._drop(key)
                return None
            self._shards.move_to_end(key)
            return s

    def put(self, shard: _Shard) -> bool:
        """Cache the shard, evicting least recently used ones until the total is within the budget. A shard larger
        than the whole budget is NOT cached (the caller still searches it once): the budget is a cap (pxx)."""
        with self._lock:
            if shard.key in self._shards:
                self._drop(shard.key)
            if shard.nbytes > self.budget:
                return False
            self._shards[shard.key] = shard
            self.bytes += shard.nbytes
            while self._shards and (len(self._shards) > CACHED_SHARDS_MAX or self.bytes > self.budget):
                oldest = next(iter(self._shards))            # least recently used first
                self.bytes -= self._shards.pop(oldest).nbytes
            return True

    def _drop(self, key) -> None:
        s = self._shards.pop(key)
        self.bytes -= s.nbytes

    def keys(self) -> list:
        with self._lock:
            return list(self._shards)

    def clear(self) -> None:
        with self._lock:
            self._shards.clear()
            self.bytes = 0


_cache = ShardCache()


def forget() -> None:
    _cache.clear()
    _plans.clear()
    with _meta_lock:
        _shard_meta.clear()


def _month_bounds(month: _dt.datetime) -> tuple[_dt.datetime, _dt.datetime]:
    start = month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, nxt


#: The shard plan's GROUP BY reads every chunk of the namespace, so a request does not run it: it is cached per
#: (space, namespace) and recomputed when the space's chunk change counter moves (sql_meshychat/049: bumped at the
#: commit of every transaction that inserts, updates or deletes a chunk of the space, cascades included) or the entry
#: is older than PLAN_TTL_S (a backstop for TRUNCATE, which fires no row trigger). A plan made before a delete that
#: has not committed yet leaves a shard short, which the loader reports (`complete` False, so truncated).
PLAN_TTL_S = 300
#: The aggregate as the plan when SHARD_MAX differs from the stored plan's shard size. OFF in production: see _plan_rows.
PLAN_AGGREGATE_FALLBACK = False
#: The drift check's own statement timeout, which its job (ctx_shard_plan_check) also takes as its timeout, so the
#: job's lease (3 x timeout + 5) outlives the scan (pxx P2-3).
PLAN_CHECK_TIMEOUT_S = 600
_plans: dict[tuple[str, str], tuple[int, float, list, int]] = {}


def _chunk_changes(cur, space: spaces.Space) -> tuple[int, int]:
    """One primary-key row: (changes, updates), the plan's freshness stamp and the resident shards' UPDATE count. No
    row yet means no chunk of the space was ever written."""
    cur.execute("SELECT changes, updates FROM ctx_chunk_changes WHERE space = %s", (space.name,))
    row = cur.fetchone()
    return (0, 0) if row is None else (int(row["changes"]), int(row["updates"]))


def _shard_plan_with_stamp(
    cur, space: spaces.Space, f: Filters
) -> tuple[list[tuple[str, _dt.datetime, int, int, int]], int, int]:
    key = (space.name, f.namespace)
    stamp, updates = _chunk_changes(cur, space)
    hit = _plans.get(key)
    if hit is not None and hit[0] == stamp and time.monotonic() - hit[1] < PLAN_TTL_S:
        rows = hit[2]
    else:
        rows = _plan_rows(cur, space, f.namespace)
        _plans[key] = (stamp, time.monotonic(), rows, updates)
    plan = []
    for seat, month, lo, hi, n in rows:
        if f.seat is not None and seat != f.seat:
            continue
        start_, end_ = _month_bounds(month)
        # Shards are months of time_last, so only `since` (time_last >= since) can rule a month out. `until` filters
        # time_first, and a chunk that starts before `until` can end in a later month, so no month is skipped for it
        # (a reviewer on #390); the SQL filter inside the shard applies it.
        if f.since is not None and end_ <= f.since:
            continue
        plan.append((seat, start_, lo, hi, n))
    return plan, updates, stamp


def _shard_plan(cur, space: spaces.Space, f: Filters) -> tuple[list[tuple[str, _dt.datetime, int, int, int]], int]:
    """([(seat, month start, lo id, hi id, rows)], the space's chunk UPDATE count), newest month first, then seat, then
    newest shard first.

    Shards are numbered over the WHOLE (seat, month), never over the time-filtered subset (b12893ca): the time filter
    only drops months that cannot overlap [since, until), and applies to resident rows like src and project. Each
    shard's [lo, hi] id range is fixed here, so loading cannot shift it. The plan over ALL seats is cached (_plans);
    the seat filter and the since cut are applied to it here, per request."""
    plan, updates, _stamp = _shard_plan_with_stamp(cur, space, f)
    return plan, updates


def _plan_rows(cur, space: spaces.Space, namespace: str) -> list[tuple[str, _dt.datetime, int, int, int]]:
    """The whole namespace's shards, every seat: the aggregate _shard_plan caches. CURRENT rows only (sql_meshychat/051):
    superseded generations are never served, so they take no shard and no probe; a flip is an UPDATE, which moves the
    change counter, so the plan is rebuilt after every new gen.

    Read from ctx_shard_plan (sql_meshychat/053), which its triggers keep equal to the aggregate below at every commit:
    O(shards), where the aggregate reads every current chunk of the namespace (15M rows passed the 30 s statement
    timeout, memory-bxxxxx).

    A SHARD_MAX other than the stored plan's shard size never runs the aggregate in the search path (pxx point
    1, cfdf14 point 2): the search plans nothing, so it answers truncated (the exact path is unaffected), and the
    mismatch is logged. Only PLAN_AGGREGATE_FALLBACK, which the tests that shrink SHARD_MAX turn on, computes it."""
    cur.execute("SELECT shard_max FROM ctx_shard_setting WHERE one")
    setting = cur.fetchone()
    if setting is not None and int(setting["shard_max"]) == SHARD_MAX:
        cur.execute("SELECT seat, month, lo, hi, n FROM ctx_shard_plan WHERE space = %s AND namespace = %s "
                    "ORDER BY month DESC, seat, shard_no DESC", (space.name, namespace))
        return [(r["seat"], r["month"].astimezone(_dt.timezone.utc), int(r["lo"]), int(r["hi"]), int(r["n"]))
                for r in cur.fetchall()]
    if PLAN_AGGREGATE_FALLBACK:
        return _plan_rows_aggregate(cur, space, namespace)
    log.warning("ctxindex: the stored shard plan was cut at %s rows and SHARD_MAX is %s; planning nothing for %s "
                "(run meshy_ctx_shard_plan_rebuild after setting ctx_shard_setting)",
                None if setting is None else setting["shard_max"], SHARD_MAX, namespace)
    return []


def _plan_rows_aggregate(cur, space: spaces.Space, namespace: str) -> list[tuple[str, _dt.datetime, int, int, int]]:
    """The plan computed from the chunks: what ctx_shard_plan must equal (tests/test_ctx_shard_plan.py)."""
    where, args = Filters(namespace=namespace).sql(space)
    where += " AND c.current"
    cur.execute(
        "SELECT seat, month, min(id) AS lo, max(id) AS hi, count(*) AS n FROM ("
        " SELECT c.seat, c.id, date_trunc('month', c.time_last AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS month,"
        "  (row_number() OVER (PARTITION BY c.seat, date_trunc('month', c.time_last AT TIME ZONE 'UTC') ORDER BY c.id)"
        "   - 1) / %s AS shard"
        f" FROM mesh_ctx_chunks c WHERE {where}) s"
        " GROUP BY seat, month, shard ORDER BY month DESC, seat, shard DESC", [SHARD_MAX, *args])
    return [(r["seat"], r["month"].astimezone(_dt.timezone.utc), int(r["lo"]), int(r["hi"]), int(r["n"]))
            for r in cur.fetchall()]


def _matching(cur, space: spaces.Space, f: Filters, plan: list) -> set[int]:
    """The plan positions whose [lo, hi] range holds at least one row the filters keep, newest gen only (Codex on #387):
    a src, project or until filter, or a superseded gen, can leave a whole shard with nothing to score, and such a shard
    must not spend MAX_SHARDS_PER_QUERY. One EXISTS probe per planned shard, in one statement; each is a range of the
    chunk indexes under the request's filters, stopping at the first row."""
    if not plan:
        return set()
    # Step 6b (pxx): a shard matches by construction, so it is not probed, when the request has no src, project or
    # until filter and the shard's month starts at or after `since` (or there is none). Its plan row exists only for
    # CURRENT rows in [lo, hi] (ctx_shard_plan, 051/053), the plan already applied the seat, and every such row then
    # passes the month range and `since`; c.gen = the latest gen differs from c.current only for a source with no
    # ctx_source_gen row, which only rows written outside the walker can lack (0 of 6 current pairs on production,
    # 29 Oct 2026). Such a shard is then searched, scores nothing and spends a slot, and coverage says so. At 15M rows
    # an unfiltered search probed about 330 shards to score 4 (warm 27.7 s, memory-bxxxxx the recorded rule).
    unprobed = PROBE_SHORTCUT and f.src is None and f.project is None and f.until is None
    probe = [i for i, (_seat, month, _lo, _hi, _n) in enumerate(plan)
             if not (unprobed and (f.since is None or month >= f.since))]
    matched = set(range(len(plan))) - set(probe)
    if PROBE_SHORTCUT and PREFILTER_COLD_SHARDS and probe and f.namespace:
        cached_plan = _plans.get((space.name, f.namespace))
        # Validated against plan-time `updates`: the sole chunk UPDATE flips `current = false` (ctxjob.py:384), so any
        # mid-TTL retirement between planning and here leaves `meta` a conservative superset that never false-excludes.
        updates = cached_plan[3] if cached_plan is not None and len(cached_plan) >= 4 else None
        if updates is not None:
            still_probe = []
            for i in probe:
                seat, month, lo, hi, n = plan[i]
                skey = (space.name, f.namespace, seat, month.strftime("%Y-%m"), lo, hi)
                meta = _get_shard_meta(skey, n, updates)
                if meta is not None and _meta_excludes(meta, f):
                    continue
                still_probe.append(i)
            probe = still_probe
    if not probe:
        return matched
    where, args = f.sql_latest(space)
    sub = [plan[i] for i in probe]
    bounds = [_month_bounds(month) for _seat, month, _lo, _hi, _n in sub]
    cur.execute("SELECT p.i FROM unnest(%s::text[], %s::timestamptz[], %s::timestamptz[], %s::bigint[], %s::bigint[]) "
                "WITH ORDINALITY AS p(seat, start_, end_, lo, hi, i) WHERE EXISTS (SELECT 1 FROM mesh_ctx_chunks c "
                f"WHERE {where} AND c.seat = p.seat AND c.time_last >= p.start_ AND c.time_last < p.end_ "
                "AND c.id BETWEEN p.lo AND p.hi)",
                [[e[0] for e in sub], [b[0] for b in bounds], [b[1] for b in bounds], [e[2] for e in sub],
                 [e[3] for e in sub], *args])
    return matched | {probe[int(r["i"]) - 1] for r in cur.fetchall()}


def _narrowed_shard(f: Filters, month: _dt.datetime) -> bool:
    """True when `f` narrows the rows inside `(seat, month)` below the whole shard (mid-month `since`, `until`, `src`,
    or `project`). Loading all 50,000 float32 vectors of a cold shard only to discard most of them on `f.keeps` wastes
    tens of MB of random vector reads and evicts hot full shards under the 1 GiB budget."""
    return (f.src is not None or f.project is not None or f.until is not None
            or (f.since is not None and f.since > month))


def _load_shard(cur, space: spaces.Space, namespace: str, seat: str, month: _dt.datetime, lo: int, hi: int,
                rows: int, updates: int = 0, *, deadline: float | None = None) -> _Shard:
    start, end = _month_bounds(month)
    skey = (space.name, namespace, seat, start.strftime("%Y-%m"), lo, hi)
    if PREFILTER_COLD_SHARDS:
        cur.execute(f"SELECT {_CHUNK_COLS} FROM mesh_ctx_chunks c WHERE c.space = %s AND c.namespace = %s AND c.seat = %s "
                    f"AND c.time_last >= %s AND c.time_last < %s AND c.id BETWEEN %s AND %s AND c.current ORDER BY c.id",
                    (space.name, namespace, seat, start, end, lo, hi))
        loaded = cur.fetchall()
        complete = len(loaded) == rows               # a delete since planning leaves the range short
        if complete:
            _put_shard_meta(skey, loaded, rows, updates)
        kept, mat = _fetch_vectors_by_digest(cur, space, loaded, deadline=deadline)
    else:
        cur.execute(f"SELECT {_ROW_COLS} {_JOIN} WHERE c.space = %s AND c.namespace = %s AND c.seat = %s "
                    f"AND c.time_last >= %s AND c.time_last < %s AND c.id BETWEEN %s AND %s AND c.current ORDER BY c.id",
                    (space.name, namespace, seat, start, end, lo, hi))
        loaded = cur.fetchall()
        complete = len(loaded) == rows
        kept, mat = _matrix(loaded, space.dims)
    shard = _Shard(skey, kept, mat, (rows, updates))
    shard.complete = complete
    return shard


def _load_shard_filtered(cur, space: spaces.Space, namespace: str, seat: str, month: _dt.datetime, lo: int, hi: int,
                         rows: int, updates: int, f: Filters, *, stamp: int, deadline: float | None = None) -> _Shard:
    """Load only the current chunk metadata inside `(seat, month, [lo, hi])` matching `f`, then fetch only those rows'
    unique vectors in sorted PK batches. Returned shard is ephemeral (outside ShardCache.budget so partial slices never
    evict full hot shards; MAX_SHARDS_PER_QUERY bounds one sharded request over subsets > EXACT_MAX, while concurrent
    sharded requests hold their own ephemeral arrays until request return)."""
    start, end = _month_bounds(month)
    eff_start = max(start, f.since) if f.since is not None else start
    where_f, args_f = f.sql(space)
    cur.execute(f"SELECT {_CHUNK_COLS} FROM mesh_ctx_chunks c WHERE {where_f} AND c.seat = %s "
                f"AND c.time_last >= %s AND c.time_last < %s AND c.id BETWEEN %s AND %s AND c.current ORDER BY c.id",
                [*args_f, seat, eff_start, end, lo, hi])
    loaded = cur.fetchall()
    kept, mat = _fetch_vectors_by_digest(cur, space, loaded, deadline=deadline)
    skey = (space.name, namespace, seat, start.strftime("%Y-%m"), lo, hi)
    shard = _Shard(skey, kept, mat, (rows, updates))
    now_stamp, now_updates = _chunk_changes(cur, space)
    if now_stamp != stamp:
        if now_updates != updates or PLAN_AGGREGATE_FALLBACK:
            shard.complete = False
        else:
            cur.execute("SELECT n FROM ctx_shard_plan WHERE space = %s AND namespace = %s AND seat = %s "
                        "AND month = %s AND lo = %s AND hi = %s",
                        (space.name, namespace, seat, start, lo, hi))
            prow = cur.fetchone()
            shard.complete = prow is not None and int(prow["n"]) == rows
    return shard


# ------------------------------------------------------------------ the search

def _subset_cap() -> int:
    """Where the subset count stops (the switch-on scaling condition, pxx/e2f3ee): one past the most rows any
    search can score, MAX_SHARDS_PER_QUERY full shards, and past EXACT_MAX so the path choice is unchanged. A subset
    that reaches it is larger than anything scored, so `truncated` is exactly as it was with a full count. It caps the
    CURRENT rows counted, not the rows read: since sql_meshychat/051 the predicate reads `c.current` through partial
    indexes, so superseded generations are not walked; rows a src, project or until filter rejects in the walked ranges
    still are (pxx)."""
    return max(EXACT_MAX, MAX_SHARDS_PER_QUERY * SHARD_MAX) + 1


def _latest_gens(cur, space: spaces.Space, namespace: str, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """The newest indexed gen of each (seat, src) in `pairs`, the sources of the shards this search loaded: one
    ctx_source_gen primary-key row each, else one backward range of mesh_ctx_chunks_seat_src_gen (as _LATEST_GEN_SQL).
    Not the whole namespace's sources: that GROUP BY read every chunk of the namespace on every sharded request."""
    if not pairs:
        return {}
    seats, srcs = zip(*sorted(pairs))
    cur.execute("SELECT p.seat, p.src, COALESCE((SELECT s.gen FROM ctx_source_gen s WHERE s.space = %s "
                "AND s.namespace = %s AND s.seat = p.seat AND s.src = p.src), (SELECT max(g.gen) FROM mesh_ctx_chunks g "
                "WHERE g.space = %s AND g.namespace = %s AND g.seat = p.seat AND g.src = p.src)) AS gen "
                "FROM unnest(%s::text[], %s::text[]) AS p(seat, src)",
                (space.name, namespace, space.name, namespace, list(seats), list(srcs)))
    return {(r["seat"], r["src"]): r["gen"] for r in cur.fetchall()}


def _search_core(conn, space: spaces.Space, namespace: str, qvec, f: Filters | None = None, *, k: int = 50,
                 cache: ShardCache | None = None, want_exact_chunks: bool = False, deadline: float | None = None
                 ) -> tuple[list[Hit], Coverage, list[dict] | None]:
    """`deadline` (time.monotonic(); None for none) is checked after the count, before every vector batch and before
    every cold shard load, and raises Deadline: nothing partial is returned, and nothing partial is cached."""
    if not namespace:
        raise ValueError("a context index read must name its namespace")
    f = dataclasses.replace(f or Filters(), namespace=namespace)
    cache = cache or _cache
    qunit = _unit(np.asarray(qvec, dtype=np.float64))
    if qunit is None:
        raise ValueError("the query vector is zero or not finite")
    where, args = f.sql_latest(space)
    with conn.cursor(row_factory=dict_row) as cur:
        cap = _subset_cap()
        cur.execute(f"SELECT count(*) AS n FROM (SELECT 1 FROM mesh_ctx_chunks c WHERE {where} LIMIT %s) s",
                    [*args, cap])
        subset = int(cur.fetchone()["n"])
        check_deadline(deadline, "the chunk read")
        if subset <= EXACT_MAX:                                               # rule 2
            if PREFILTER_COLD_SHARDS:
                cur.execute(f"SELECT {_CHUNK_COLS} FROM mesh_ctx_chunks c WHERE {where}", args)
                raw_chunks = cur.fetchall()
                exact_chunks = [dict(r) for r in raw_chunks] if want_exact_chunks else None
                rows, mat = _fetch_vectors_by_digest(cur, space, raw_chunks, deadline=deadline)
            else:
                cur.execute(f"SELECT {_ROW_COLS} {_JOIN} WHERE {where}", args)
                rows, mat = _matrix(cur.fetchall(), space.dims)
                exact_chunks = None
            oldest = min((r["time_first"] for r in rows), default=None)  # rows: only those with a usable vector
            return (_top(rows, mat, qunit, k),
                    Coverage("exact", subset, len(rows), len(rows) < subset, oldest_covered=oldest),
                    exact_chunks)
        plan, updates, stamp = _shard_plan_with_stamp(cur, space, f)
        matching = _matching(cur, space, f, plan)
        cov = Coverage("sharded", subset, 0, False, subset_at_least=subset >= cap)
        best: list[Hit] = []
        changed = False
        loaded: list[_Shard] = []
        for i, (seat, month, lo, hi, rows) in enumerate(plan):
            if i not in matching:                        # nothing in it the filters keep: neither searched nor skipped
                continue
            key = (space.name, namespace, seat, month.strftime("%Y-%m"), lo, hi)
            if len(cov.shards_searched) >= MAX_SHARDS_PER_QUERY:              # rule 3: skipped, never waited for
                cov.shards_skipped.append(key[2:])
                continue
            shard = cache.get(key, (rows, updates))
            if shard is None:
                check_deadline(deadline, f"loading shard {key[2:]}")
                if PREFILTER_COLD_SHARDS and _narrowed_shard(f, month):
                    shard = _load_shard_filtered(cur, space, namespace, seat, month, lo, hi, rows, updates, f,
                                                 stamp=stamp, deadline=deadline)
                    if not shard.complete:
                        changed = True
                else:
                    shard = _load_shard(cur, space, namespace, seat, month, lo, hi, rows, updates, deadline=deadline)
                    if shard.complete:
                        cache.put(shard)
                    else:
                        changed = True                   # rows left the range since planning: say so, cache nothing
            loaded.append(shard)
            cov.shards_searched.append(key[2:])
        latest = _latest_gens(cur, space, namespace, {(r["seat"], r["src"]) for s in loaded for r in s.rows})
        for shard in loaded:
            keep = [i for i, r in enumerate(shard.rows) if f.keeps(r, latest)]
            if keep:
                rows = [shard.rows[i] for i in keep]
                best.extend(_top(rows, shard.mat[keep], qunit, k))
                cov.scored += len(rows)
                first = min(r["time_first"] for r in rows)
                cov.oldest_covered = first if cov.oldest_covered is None else min(cov.oldest_covered, first)
        cov.truncated = cov.scored < subset or bool(cov.shards_skipped) or changed
    seen, unique = set(), []
    for h in _order(best):                               # dedupe by chunk id before the cut (pxx P2-3)
        if h.chunk_id not in seen:
            seen.add(h.chunk_id)
            unique.append(h)
    return unique[:k], cov, None


def search(conn, space: spaces.Space, namespace: str, qvec, f: Filters | None = None, *, k: int = 50,
           cache: ShardCache | None = None) -> tuple[list[Hit], Coverage]:
    """The k best chunks for a query vector under the filters, in ONE namespace (the one the caller was authorised for),
    newest gen only, with an honest coverage block. `conn` must be inside server_internal."""
    hits, cov, _exact_chunks = _search_core(conn, space, namespace, qvec, f, k=k, cache=cache, want_exact_chunks=False)
    return hits, cov


# ------------------------------------------------------------------ retention (D4)

RETENTION_DAYS = 180
RETENTION_BATCH = 5000


def run_retention_job(conn, job):
    """Delete chunk OCCURRENCES whose time_last is older than RETENTION_DAYS, in bounded batches; ctxjob's orphan sweep
    then removes the vectors nothing names. Items are kept (D4): read by key still covers everything."""
    from apps.core import rls
    from apps.jobs.result import TickResult
    # One space at a time, each in its own transaction that takes ctxjob's space lock BEFORE it deletes a row: the
    # stored shard plan's triggers take that lock too, and a delete that locked rows first could deadlock with ctxjob,
    # which holds the lock and then wants the same rows (pxx P2-2, sql_meshychat/053's LOCKS).
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=tuple_row) as cur:
            # The spaces, by a loose scan of mesh_ctx_chunks_digest (space, text_sha256): one index probe per space.
            cur.execute("WITH RECURSIVE s AS (SELECT min(space) AS sp FROM mesh_ctx_chunks UNION ALL "
                        "SELECT (SELECT min(c.space) FROM mesh_ctx_chunks c WHERE c.space > s.sp) FROM s "
                        "WHERE s.sp IS NOT NULL) SELECT sp FROM s WHERE sp IS NOT NULL")
            names = [r[0] for r in cur.fetchall()]
    n = 0
    for name in names:
        if n >= int(job.max_batch):
            break
        with rls.projection_writer(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(1835233134, hashtext(%s))", (name,))
                cur.execute("DELETE FROM mesh_ctx_chunks WHERE id IN (SELECT id FROM mesh_ctx_chunks "
                            "WHERE space = %s AND time_last < now() - make_interval(days => %s) LIMIT %s)",
                            (name, RETENTION_DAYS, int(job.max_batch) - n))
                n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return TickResult(rows=n, more_work_pending=n >= int(job.max_batch))


def run_shard_plan_check_job(conn, job):
    """The ctx_shard_plan_check job: check_shard_plan once a tick, on a lane and a lease of its own. Inside retention's
    30 s job its 600 s scan could outlive the 95 s lease, and a second worker could take the job over mid-scan (GitHub
    Codex on 01a0f9aa, pxx P2-3)."""
    from apps.core import rls
    from apps.jobs.result import TickResult
    with rls.projection_writer(conn):
        drift = check_shard_plan(conn)
    return TickResult(rows=len(drift), more_work_pending=False)


def check_shard_plan(conn) -> list[tuple[str, str, str, _dt.datetime]]:
    """The (space, namespace, seat, month) groups whose stored plan differs from their current chunks, recomputed.

    A check that does not depend on a search's subset size (pxx point 2): the subset count stops at
    _subset_cap(), so past it a missed insert, whose rows lie outside every stored range and leave no shard short, would
    never show. This compares each group's max(id) and count of current chunks with its plan's last hi and sum(n),
    groups missing on either side included, and has the 053 helper recompute every group that differs. It reads every
    current chunk, so it runs only in its own job (ctx_shard_plan_check, hourly, own lane), never in a request."""
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{PLAN_CHECK_TIMEOUT_S}s",))
        cur.execute(
            "SELECT coalesce(a.space, p.space), coalesce(a.namespace, p.namespace), coalesce(a.seat, p.seat), "
            "coalesce(a.month, p.month) FROM (SELECT c.space, c.namespace, c.seat, meshy_ctx_month(c.time_last) AS month, "
            "max(c.id) AS hi, count(*) AS n FROM mesh_ctx_chunks c WHERE c.current GROUP BY 1, 2, 3, 4) a "
            "FULL JOIN (SELECT space, namespace, seat, month, max(hi) AS hi, sum(n) AS n FROM ctx_shard_plan "
            "GROUP BY 1, 2, 3, 4) p ON a.space = p.space AND a.namespace = p.namespace AND a.seat = p.seat "
            "AND a.month = p.month WHERE a.hi IS DISTINCT FROM p.hi OR a.n IS DISTINCT FROM p.n ORDER BY 1, 2, 3, 4")
        drift = [tuple(r) for r in cur.fetchall()]
        if drift:
            log.warning("ctxindex: %d shard plan group(s) differed from their chunks; recomputing", len(drift))
            cur.execute("SELECT meshy_ctx_shard_plan_groups(%s, %s, %s, %s)",
                        ([d[0] for d in drift], [d[1] for d in drift], [d[2] for d in drift], [d[3] for d in drift]))
    return drift


__all__ = ["BUDGET_BYTES", "BUDGET_BYTES_MIN", "Coverage", "Deadline", "EXACT_MAX", "Filters", "Hit",
           "MAX_SHARDS_PER_QUERY", "RETENTION_BATCH", "check_deadline",
           "RETENTION_DAYS", "SHARD_MAX", "ShardCache", "check_shard_plan", "configured_budget", "forget",
           "run_retention_job",
           "run_shard_plan_check_job", "search"]
