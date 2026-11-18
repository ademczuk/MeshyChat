"""Retrieval over the mesh: reciprocal-rank fusion of a LEXICAL arm and the EMBEDDING arm,
then reranked, unless a caller turns the reranker off.

READ ONLY. Nothing here writes mesh_embeddings, mesh_lexical, their state row, mesh_edges
or anything else; the job does (apps/meshsem/embedjob.py), and sql_meshychat/008 and 010
make the database refuse a request's write. tests/test_meshsem_routes.py snapshots the
tables around a request and requires them unchanged.

THE DEFAULT RANKING (divergence 026) fuses two arms by reciprocal rank, k = 60
(apps/meshsem/fusion.py):

    lexical  TF-IDF cosine over word tokens (apps/meshsem/lexical.py)
    embed    cosine against the configured space's vectors, raw text by default

Measured basis, the A/B of 2026-09-23 (94 real queries over 929 threaded messages,
thread-membership proxy, paired bootstrap): TF-IDF matched the embedding on MRR and beat
it on recall@5 (0.541 vs 0.424); the fusion kept TF-IDF's quality within noise and added
+0.093 recall@5 over the embedding alone. The fusion is kept rather than the lexical arm
alone because the proxy CANNOT see cross-thread relatedness, which is what the embedding
arm exists for; divergence 026 measures this implementation on the same queries.

RERANKING IS ON BY DEFAULT (`rerank=True`, the routes' `rerank=1`; `rerank=0` turns it
off), as amended in divergence 026. On the same A/B, bge-reranker-v2-m3 over the top 40
LOWERED MRR by 0.10 to 0.12 (q <= 0.015) and cost about 575 ms a query; but that proxy
measures thread recovery, which the graph already does exactly. A live paraphrase test
(two nodes, 12 queries, synthetic text) put the target first in 5/12 by default and 10/12
with the reranker, never worse on any query: n=12, a sign test of p = 0.0625 two-sided,
and the proxy disagrees. The reranker's job is the one the proxy cannot see. A reranker
that is down never fails a request: see `reranked` below; one that fails at the
transport is skipped for providers.RERANK_COOLDOWN_S, so a hung one costs one request its
timeout, not every request.

THE POOL the reranker scores, and whose first k rows are the answer without it, is at most
POOL = 40 candidates in fused order that ALWAYS include each arm's first ARM_GUARANTEE =
10 live rows (`_pool`; divergence 026's pool amendment). Fusion scores by rank alone, so
rows both arms rank can push an arm's first place out of the fused top 40: a paraphrase
the embedder ranked first, matched by no lexical term, fused 42nd and was never seen by
the reranker or returned. A reserved row displaces the last fused row that is not itself
reserved, so the first 20 rows (the default 10 among them) are never displaced, and
`pool.guaranteed` says, per arm, how many rows its reservation added.

EVERY RESPONSE SAYS HOW IT WAS RANKED. `method` is "rrf(lexical,embed)", or the one arm
that ran, with "+rerank" only when a reranker scored the pool (whether or not that changed
its order); `arms` says, per arm, whether it ran, how many documents it ranked and, if it
did not run, why; `degraded` is null or the sentence naming the missing arm. When the
embedder cannot answer (or the space has no reference yet) the lexical arm answers ALONE
AND SAYS SO. When no arm has anything to return, search's answer is the embed arm's typed
503 with the envelope, never an empty 200; `context` answers 200 with `related.absent`
set to that arm's reason (`embed_unavailable` or `space_unreferenced`), because its
anchor, thread and reply chain are still an answer. A forbidden or textless anchor ranks
nothing: `method`, `arms` and `degraded` are null and `related.absent` says why.

THE ANCHOR CARRIES ITS OWN BODY (divergence 027): `body` and `body_truncated`, by the
related hits' rule (`body_fields`), read from the anchor's shared_memory row, so they are
there before the edges job has projected it (`thread_absent: anchor_not_projected`). What
decides whether a caller sees it is memory.read's, the INVOKE PATH's and not only its
handler's: `_anchor_body_withheld`, beside the row, asks for the DPoP proof, the skill,
the usable key and the tenancy gate memory.read's POST would need, then the namespace
predicate, and `_anchor` selects the row memory.read answers with no version (the head).
embed_policy does not hide it, as it does not hide the memo from memory.read: that gate
keeps text from a MODEL, and the anchor's text is still never embedded when it is closed.

THE INDEXES are in-process per space: a float32 matrix of vectors (VectorIndex below)
and a lexical index (apps/meshsem/lexical.LexicalIndex), each refreshed INCREMENTALLY by
its table's (timestamp, src_id), re-reading an OVERLAP window behind the watermark so a
row committed late is still picked up. Both are BOUNDED (B8): at most `ceiling` rows,
newest kept, and every response says whether the ceiling bit. Brute force, not an ANN
index, because the namespace is ~23k rows and one 768-d matrix product over that is
milliseconds; an approximate index would add a recall question this answer does not have.

THE INDEXES ARE NEVER THE AUTHORITY on what may be returned. Head-ness (an UPDATE no
watermark sees), retention deletes (ON DELETE CASCADE removes the row, not the cached
copy) and the embed_policy gate are all applied at READ time by joining the fused
candidates back to shared_memory, so a stale cache can cost a candidate slot and never
returns a superseded, deleted or gated message.

`reranked: true` ONLY WHEN A RERANKER RAN. A known fleet trap is a tool that reports
reranked after silently falling back to first-stage order. Here the flag is derived from
the rerank result itself (providers.Reranked.ran), never from the request.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time

import numpy as np
from psycopg.rows import dict_row

from apps.core import dbconn, env
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from . import embedjob, exactpassage, fullload, fusion, lexical, providers, spaces, store

log = logging.getLogger("meshychat.meshsem.search")

#: Fused candidates handed to the reranker when one is asked for (the brief's "top 40").
POOL = 40
DEFAULT_K = 10
MAX_K = POOL
#: Pool slots RESERVED per arm: the pool always holds the first ARM_GUARANTEE live
#: candidates of each arm's OWN ranking, and the fused order fills the rest (divergence
#: 026, amended). Reciprocal-rank fusion scores by rank alone, so a row BOTH arms rank
#: (1/(60+i) + 1/(60+j)) outranks a row one arm ranks first (1/61), however weak its other
#: match. Measured on the node-test server at 388f28e (128 messages, raw-v1 space): a
#: paraphrase query's target was the embed arm's rank 1 (cosine 0.631) and matched no
#: lexical term, so it fused at rank 42, outside the pool, 31 of whose 40 members had a
#: lexical cosine under 0.05; the reranker never saw it and, k being capped at POOL, no
#: request could return it. Two arms reserve at most 20 of the 40 slots, so the fused
#: order's first 20 rows (DEFAULT_K's 10 among them) are never displaced.
ARM_GUARANTEE = 10
#: How many of an arm's ranked rows one lookup reads, in the arm's order, when its
#: reserved rows run past the candidates the fused walk already looked up. So one arm's
#: walk makes at most ceil(ARM_DEPTH / ARM_CHUNK) = 10 queries, and a request at most 22
#: (divergence 026's worst case: an anchor whose own thread fills both rankings).
ARM_CHUNK = 200
#: Candidates examined against shared_memory per pass, and in total. Filtering (head,
#: gate, other-thread) can reject candidates; this caps how far down the FUSED ranking a
#: request will look for replacements, and the response reports it when it bit. An arm's
#: reservation (ARM_GUARANTEE) walks the arm's own ranking instead, at most ARM_DEPTH deep.
EXAMINE_FIRST = 400
EXAMINE_MAX = 2000
#: How deep each arm ranks before the fusion. A document below this in one arm gets
#: nothing from that arm; 1 / (60 + 2000) is under 4% of a top-ranked contribution.
ARM_DEPTH = EXAMINE_MAX
MAX_QUERY_CHARS = 2000
#: The vector ceiling (B8). ~300 MB of float32 at 768-d. A setting can lower it. The
#: lexical index is bounded by the same number of documents.
DEFAULT_CEILING = 100_000
#: Re-read window behind the index watermark, for vectors committed late.
OVERLAP_S = 60
#: How often the index is reconciled with the table: deleted rows dropped, missing or changed rows loaded by id (a full reload until 29 Oct 2026).
FULL_RELOAD_S = 600
#: Coverage is one pass over the namespace; it is held this long and dated (B5).
COVERAGE_TTL_S = 15.0


def exact_passage_on() -> bool:
    """MESHY_SEARCH_EXACT_PASSAGE: exact-passage promotion on mail search (exactpassage.py). Off by default."""
    return env.env_flag("MESHY_SEARCH_EXACT_PASSAGE", "MESHY_SEARCH_EXACT_PASSAGE")


def _ceiling() -> int:
    raw = env.setting("MESHY_SEMANTIC_MAX_VECTORS", "MESHY_SEMANTIC_MAX_VECTORS",
                      default=DEFAULT_CEILING)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CEILING
    return n if n > 0 else DEFAULT_CEILING


# --------------------------------------------------------------- the resident view
#: Divergence 109. pxx's design A, option (b), on <thread> (msg e00a09 and d4bf7c, 7 Nov 2026). A RESIDENT VIEW
#: keeps in memory only each stored vector's first MESHY_SEMANTIC_RESIDENT_DIMS floats, renormalised: a width the
#: space declares (spaces.Space.views). The view only PICKS the embed arm's candidates. Its first rescore_depth() are
#: rescored at the space's full width from the stored rows, in one read by primary key (_embed_arm). Any pool member
#: past them gets its full-width cosine from at most one more read (_exact_pool). So every cosine that a response, a
#: stored edge or a threshold sees is full width, and only the arm's order past N comes from the view. Unset (the
#: default): the full width, and today's code path, byte for byte and with no extra read.
DEFAULT_RESCORE_DEPTH = 256


def _whole(raw) -> int | None:
    """A setting as a whole number, or None: a non-integral or non-finite float is not one (codex-kxxx-cli's P3s on
    fullload.timeout_ms, #540)."""
    if isinstance(raw, float) and not raw.is_integer():
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return None


def resident_dims(space: spaces.Space) -> int | None:
    """MESHY_SEMANTIC_RESIDENT_DIMS for the mail index, or None for the space's full width. A value that is not one of
    the space's declared views is refused with a warning and keeps the full width."""
    raw = env.setting("MESHY_SEMANTIC_RESIDENT_DIMS", "MESHY_SEMANTIC_RESIDENT_DIMS", default=None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    n = _whole(raw)
    if n == space.dims:
        return None
    if n not in space.views:
        log.warning("MESHY_SEMANTIC_RESIDENT_DIMS=%r is not a view %s declares %s; the index keeps %d dims",
                    raw, space.name, list(space.views), space.dims)
        return None
    return n


def rescore_depth() -> int:
    """MESHY_SEMANTIC_RESCORE_DEPTH: how many of a view's candidates the first read rescores at full width (N). A whole
    number from ARM_GUARANTEE to ARM_DEPTH; anything else keeps DEFAULT_RESCORE_DEPTH."""
    n = _whole(env.setting("MESHY_SEMANTIC_RESCORE_DEPTH", "MESHY_SEMANTIC_RESCORE_DEPTH",
                           default=DEFAULT_RESCORE_DEPTH))
    return n if n is not None and ARM_GUARANTEE <= n <= ARM_DEPTH else DEFAULT_RESCORE_DEPTH


def _unit32(vec) -> np.ndarray | None:
    """A stored vector as the index holds it (VectorIndex._put): float32, unit length. None for a zero or non-finite
    vector, which the index never holds either."""
    norm = float(np.linalg.norm(vec))
    if norm == 0.0 or not np.isfinite(norm):
        return None
    return (vec / norm).astype(np.float32)


#: The rescoring read: one probe of mesh_embeddings_pkey (src_id, space, chunk_no) per id. The LATERAL ... LIMIT 1
#: cannot be flattened, so the plan is a probe per id at any table size. The `src_id = ANY(...)` form it replaces
#: planned on the primary key on production (57,990 mail rows, read-only EXPLAIN, 7 Nov 2026: a bitmap scan of
#: mesh_embeddings_pkey). On a test database's handful of rows it planned on mesh_embeddings_space_ns_idx with the ids
#: as a filter, which at production's size would read the whole namespace (pxx P2 3). This form planned on
#: the primary key on production too (nested loop, an index probe per id). tests/test_query_cost_bounds.py holds the
#: plan, reading this constant from the source.
STORED_VECTORS_SQL = ("SELECT e.src_id::text AS src_id, e.vec FROM unnest(%s::uuid[]) AS want(id) "
                      "CROSS JOIN LATERAL (SELECT m.src_id, m.vec FROM mesh_embeddings m "
                      "WHERE m.src_id = want.id AND m.space = %s AND m.chunk_no = 0 "
                      "AND m.namespace = %s AND m.dims = %s LIMIT 1) e")


def stored_vectors(conn, idx: VectorIndex, ids) -> dict[str, np.ndarray]:
    """{src_id: the stored full-width vector} for these rows of idx's space and namespace: ONE read by primary key.
    `conn` must be inside server_internal, the visibility the index itself loads under. A row that is missing or does
    not decode at the full width is left out."""
    ids = list(dict.fromkeys(str(i) for i in ids))
    if not ids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(STORED_VECTORS_SQL, (ids, idx.space.name, idx.namespace, idx.space.dims))
        rows = cur.fetchall()
    out = {}
    for r in rows:
        try:
            out[str(r["src_id"])] = spaces.decode(r["vec"], idx.space.dims)
        except ValueError:
            continue
    return out


def _query_vector_of(conn, idx: VectorIndex, src_id: str) -> np.ndarray | None:
    """A stored row's vector as a query: the loaded one; on a view (which holds only the first floats) the stored
    full-width row, by primary key, in one short server_internal transaction (`conn` has none open), so the context
    route never calls the embedder for a row it holds. None when neither has it."""
    vec = idx.vector_of(src_id)
    if vec is None and idx.view:
        with dbconn.request_server_internal(conn) as c:
            vec = stored_vectors(c, idx, [src_id]).get(src_id)
    return vec


def _view_read(conn, idx: VectorIndex) -> dict:
    """_embed_arm's `read` keyword on a view, nothing otherwise: with the view off, every call keeps today's shape
    (pxx P2 2: the routes tests' stand-in arm takes no `read`)."""
    return {"read": _reader(conn, idx)} if idx.view else {}


def _reader(conn, idx: VectorIndex):
    """A stored_vectors reader for a caller whose `conn` has NO open transaction: each call is one short
    server_internal transaction. Never called unless idx is a view."""
    def read(ids):
        with dbconn.request_server_internal(conn) as c:
            return stored_vectors(c, idx, ids)
    return read


class VectorIndex:
    """One space's vectors of ONE namespace, unit-normalised, with src_id per row. The namespace is
    the mail's unless named: since sql_meshychat/030 the table holds other namespaces' chunks too, and
    an index that loaded them would rank rows its caller then filters away (divergence 060)."""

    #: What one entry of the index is, as SQL over mesh_embeddings: a mail row's one vector
    #: (chunk 0), keyed by its src_id. apps/meshsem/graphsearch.ChunkIndex keys every chunk.
    _ID_SQL = "src_id::text"
    _CHUNK_FILTER = "AND chunk_no = 0 "

    def __init__(self, space: spaces.Space, namespace: str = MESH_CHAT_NAMESPACE, *, resident: int | None = None):
        if resident is not None and resident != space.dims and resident not in space.views:
            raise ValueError(f"{space.name} declares no {resident}-d view (views: {list(space.views)})")
        self.space = space
        self.namespace = namespace
        #: The width held in memory: the space's own, or one of its declared views (resident_dims). A view
        #: reads only that many floats of each stored vector (sliced in SQL) and only picks candidates.
        self.resident = space.dims if resident is None else resident
        self.view = self.resident < space.dims
        self._vec_sql = "vec" if not self.view else f"substring(vec from 1 for {4 * self.resident}) AS vec"
        self.lock = threading.Lock()
        #: Single-flight refresh (13 Nov 2026, pxx): a request that finds a refresh running serves the
        #: last load instead of queueing behind it. `lock` guards only what rank() reads, and is held only to
        #: apply rows already read and decoded, or to swap in a fully built load.
        self._refresh_lock = threading.Lock()
        self._reset()

    def _blank(self) -> "VectorIndex":
        """An empty index of the same kind, space, namespace and width, for a full load built off to the side."""
        twin = type(self).__new__(type(self))
        twin.space, twin.namespace, twin.resident = self.space, self.namespace, self.resident
        twin.view, twin._vec_sql = self.view, self._vec_sql
        twin._reset()
        return twin

    def _reset(self) -> None:
        self.buf = np.zeros((0, self.resident), dtype=np.float32)
        self.n = 0
        self.ids: list[str] = []
        self.pos: dict[str, int] = {}
        self.stamps: dict = {}         # src_id -> the embedded_at its loaded vector carried
        self.watermark = None          # (embedded_at, src_id) of the newest row loaded
        self.loaded_mono: float | None = None
        self.over_ceiling = False

    def _put(self, src_id: str, vec: np.ndarray, stamp=None) -> None:
        norm = float(np.linalg.norm(vec))
        if norm == 0.0 or not np.isfinite(norm):
            return                      # the job refuses these; never divide by zero here
        unit = (vec / norm).astype(np.float32)
        self.stamps[src_id] = stamp
        i = self.pos.get(src_id)
        if i is not None:
            self.buf[i] = unit
            return
        if self.n == self.buf.shape[0]:
            grown = np.zeros((max(1024, self.n * 2), self.resident), dtype=np.float32)
            grown[:self.n] = self.buf[:self.n]
            self.buf = grown
        self.buf[self.n] = unit
        self.pos[src_id] = self.n
        self.ids.append(src_id)
        self.n += 1

    def _mark(self, row) -> None:
        mark = (row["embedded_at"], str(row["src_id"]))
        if self.watermark is None or mark > self.watermark:
            self.watermark = mark

    def _reconcile(self, cur, ceiling: int) -> bool:
        """The periodic step once older than FULL_RELOAD_S, in place of a full reload: one read of
        the live (id, embedded_at) pairs; drop what is gone; load by id what is missing or whose
        embedded_at differs from the loaded one (a row committed more than OVERLAP_S late, or a
        late-committed update: the full reload was the only thing that reached those, pxx
        P2-2). False, for the full load, when the index is over its ceiling (the full load keeps
        the newest `ceiling` and refills after deletes, P2-1) or the loads would pass it.

        Why: the full reload re-read every vector (about 150 MB at 50,000 rows of 768-d, into a
        buffer regrown by doubling) inside a search request, and on HQD it coincided with
        production livez timeouts while the box was paging (28-29 Oct 2026). On local PG16 at
        50,000 rows the id read took 0.05 s against 1.35 s for the reload (pxx)."""
        if self.over_ceiling:
            return False
        # Reads and decoding first, with no lock: only refresh (single-flight) changes ids, pos and stamps, so
        # reading them here is safe while rank() runs. The changes are then applied under `lock` in one step.
        cur.execute(f"SELECT {self._ID_SQL} AS src_id, embedded_at FROM mesh_embeddings "
                    f"WHERE space = %s AND namespace = %s {self._CHUNK_FILTER}AND dims = %s",
                    (self.space.name, self.namespace, self.space.dims))
        live = {str(r["src_id"]): r["embedded_at"] for r in cur.fetchall()}
        keep = [i for i in range(self.n) if self.ids[i] in live]
        # A dropped row is not in `live`, so `want` is the same computed before or after the drop.
        want = [s for s, at in live.items() if s not in self.pos or self.stamps.get(s) != at]
        if len(keep) + sum(1 for s in want if s not in self.pos) > ceiling:
            return False
        loaded = []
        if want:
            cur.execute(f"SELECT {self._ID_SQL} AS src_id, {self._vec_sql}, embedded_at FROM mesh_embeddings "
                        f"WHERE space = %s AND namespace = %s {self._CHUNK_FILTER}AND dims = %s "
                        f"AND {self._ID_SQL} = ANY(%s::text[]) ORDER BY embedded_at ASC, src_id ASC",
                        (self.space.name, self.namespace, self.space.dims, want))
            loaded = [(r, spaces.decode(r["vec"], self.resident)) for r in cur.fetchall()]
        with self.lock:
            if len(keep) != self.n:
                for i in range(self.n):
                    if self.ids[i] not in live:
                        self.stamps.pop(self.ids[i], None)
                if keep:
                    self.buf[:len(keep)] = self.buf[np.asarray(keep, dtype=np.int64)]
                self.buf[len(keep):self.n] = 0.0
                self.ids = [self.ids[i] for i in keep]
                self.n = len(keep)
                self.pos = {s: i for i, s in enumerate(self.ids)}
            for r, vec in loaded:
                self._put(str(r["src_id"]), vec, r["embedded_at"])
                self._mark(r)
            self.loaded_mono = time.monotonic()
        return True

    def refresh(self, conn, *, ceiling: int, wait: bool = False) -> None:
        """Bring the index up to date. `conn` must be inside server_internal.

        Incremental by default: re-read the OVERLAP window behind the watermark and
        replace by src_id. Once older than FULL_RELOAD_S it is first RECONCILED with the
        table (_reconcile: drop the deleted, load the missing and the changed by id; until
        29 Oct 2026 this was a full reload of every vector). Full when the index is new,
        over its ceiling, or when new rows would push it over (a full load keeps the NEWEST
        `ceiling`).

        NO LOCK ACROSS A READ (13 Nov 2026, pxx): refresh is single-flight. `wait=False` (requests): if a
        refresh is running, return at once and serve the last load, or raise lexical.IndexWarming when there is
        none yet; `wait=True` (background jobs) queues. Every database read and decode runs with no lock; a full
        load is built in a separate index and swapped in; an incremental or reconcile step applies its rows under
        `lock` in one step.
        """
        if not self._refresh_lock.acquire(blocking=wait):
            if self.loaded_mono is None:
                raise lexical.IndexWarming(f"vector index {self.space.name}/{self.namespace} is being built")
            return
        try:
            self._refresh_unlocked(conn, ceiling=ceiling)
        finally:
            self._refresh_lock.release()

    def _refresh_unlocked(self, conn, *, ceiling: int) -> None:
        """refresh()'s policy; the caller holds `_refresh_lock`."""
        t0 = time.monotonic()
        full = None
        with conn.cursor(row_factory=dict_row) as cur:
            rows = None
            reload = self.loaded_mono is None
            if (not reload
                    and time.monotonic() - self.loaded_mono > FULL_RELOAD_S
                    and not self._reconcile(cur, ceiling)):
                reload = True                              # the full load below
            if (not reload
                    and time.monotonic() - self.loaded_mono <= FULL_RELOAD_S):
                wm_at = self.watermark[0] if self.watermark else None
                cur.execute(f"SELECT {self._ID_SQL} AS src_id, {self._vec_sql}, embedded_at FROM mesh_embeddings "
                            f"WHERE space = %s AND namespace = %s {self._CHUNK_FILTER}AND dims = %s "
                            "  AND (%s::timestamptz IS NULL "
                            "  OR embedded_at >= %s::timestamptz - make_interval(secs => %s)) "
                            "ORDER BY embedded_at ASC, src_id ASC",
                            (self.space.name, self.namespace, self.space.dims, wm_at, wm_at,
                             OVERLAP_S))
                rows = cur.fetchall()
                fresh = sum(1 for r in rows if str(r["src_id"]) not in self.pos)
                if self.n + fresh > ceiling:
                    rows = None
            if rows is None:
                # The full load: its one statement runs under fullload.FULL_LOAD_TIMEOUT_MS, not the request's 30 s.
                rows = fullload.fetch_all(
                    cur, f"SELECT {self._ID_SQL} AS src_id, {self._vec_sql}, embedded_at FROM mesh_embeddings "
                         f"WHERE space = %s AND namespace = %s {self._CHUNK_FILTER}AND dims = %s "
                         "ORDER BY embedded_at DESC, src_id DESC LIMIT %s",
                    (self.space.name, self.namespace, self.space.dims, ceiling + 1))
                full = self._blank()                       # built off to the side; rank() keeps the old load
                full.over_ceiling = len(rows) > ceiling
                rows = list(reversed(rows[:ceiling]))
                # Sized once, with headroom: no doubling regrowth on top of the old buffer that keeps serving
                # (pxx P3-1), and the first new message after the load does not pay a regrowth (35bb76).
                full.buf = np.zeros((len(rows) + max(1024, len(rows) // 8), self.resident), dtype=np.float32)
        t1 = time.monotonic()
        decoded = [(r, spaces.decode(r["vec"], self.resident)) for r in rows]
        if full is not None:
            for r, vec in decoded:
                full._put(str(r["src_id"]), vec, r["embedded_at"])
                full._mark(r)
            full.loaded_mono = time.monotonic()
            old_mb = self.buf.nbytes / 2**20
            with self.lock:                                # the swap: one short step
                for name in ("buf", "n", "ids", "pos", "stamps", "watermark", "loaded_mono", "over_ceiling"):
                    setattr(self, name, getattr(full, name))
            # While the new load was built, both buffers were held: the price of serving during a full load.
            log.info("vector full load %s/%s swapped in: %d rows, buffer %.0f MB (the old one, %.0f MB, served "
                     "meanwhile)", self.space.name, self.namespace, self.n, self.buf.nbytes / 2**20, old_mb)
        else:
            with self.lock:
                for r, vec in decoded:
                    self._put(str(r["src_id"]), vec, r["embedded_at"])
                    self._mark(r)
        took = time.monotonic() - t0
        if took >= lexical.SLOW_REFRESH_LOG_S:
            log.warning("vector refresh %s/%s slow: db read %.2fs, %d rows decoded and applied %.2fs (%s)",
                        self.space.name, self.namespace, t1 - t0, len(rows), time.monotonic() - t1,
                        "full load" if full is not None else "incremental")

    def rank(self, query_unit: np.ndarray) -> tuple[list[str], np.ndarray]:
        """(src_ids best first, their cosines). Brute force over every cached vector. On a view, the query's first
        `resident` floats, renormalised, against the view: VIEW cosines, which only order candidates. _embed_arm
        rescores them, so no caller sees one as a score."""
        with self.lock:
            if self.n == 0:
                return [], np.zeros(0, dtype=np.float32)
            if not self.view:
                scores = self.buf[:self.n] @ query_unit.astype(np.float32)
            else:
                q = _unit32(np.asarray(query_unit, dtype=np.float32)[:self.resident])
                if q is None:
                    return [], np.zeros(0, dtype=np.float32)
                scores = self.buf[:self.n] @ q
            top = min(self.n, ARM_DEPTH)
            idx = np.argpartition(-scores, top - 1)[:top]
            idx = idx[np.argsort(-scores[idx], kind="stable")]
            return [self.ids[i] for i in idx], scores[idx]

    def vector_of(self, src_id: str) -> np.ndarray | None:
        """The loaded vector, or None. Always None on a view, which holds only a vector's first floats: a caller
        that needs one as a query reads the stored row (stored_vectors), as it does for a row the ceiling evicted."""
        if self.view:
            return None
        with self.lock:
            i = self.pos.get(src_id)
            return None if i is None else self.buf[i].copy()

    def describe(self, ceiling: int) -> dict:
        with self.lock:
            out = {"vectors": self.n, "ceiling": ceiling, "over_ceiling": self.over_ceiling}
            if self.view:
                out.update({"resident_dims": self.resident, "rescore_depth": rescore_depth()})
            return out


_indexes: dict[str, VectorIndex] = {}
_lexical: dict[str, lexical.LexicalIndex] = {}
#: apps/meshsem/graphsearch's caches, by (space, namespace) (divergence 060).
_graph_indexes: dict[tuple[str, str], VectorIndex] = {}
_graph_lexical: dict[tuple[str, str], lexical.LexicalIndex] = {}
_graph_coverage: dict[tuple[str, str, bool], tuple[float, object]] = {}
_coverage: dict[tuple[str, bool], tuple[float, store.Coverage]] = {}
_registry_lock = threading.Lock()


def index_for(space: spaces.Space) -> VectorIndex:
    with _registry_lock:
        idx = _indexes.get(space.name)
        if idx is None:
            # The mail index alone takes a view (pxx's condition): graphsearch's chunk indexes keep the full width.
            idx = _indexes[space.name] = VectorIndex(space, resident=resident_dims(space))
        return idx


def lexical_index_for(space: spaces.Space) -> lexical.LexicalIndex:
    with _registry_lock:
        idx = _lexical.get(space.name)
        if idx is None:
            idx = _lexical[space.name] = lexical.LexicalIndex(space.name)
        return idx


def forget() -> None:
    """Drop every cached index and coverage figure, and the rerank cooldowns. Tests, and an
    operator after a reset."""
    with _registry_lock:
        _indexes.clear()
        _lexical.clear()
        _coverage.clear()
        _graph_indexes.clear()
        _graph_lexical.clear()
        _graph_coverage.clear()
    providers.forget()


def coverage_for(conn, space: spaces.Space, gate: bool) -> store.Coverage:
    """Coverage, held for COVERAGE_TTL_S. `measured_at` in the answer dates it."""
    key = (space.name, gate)
    now = time.monotonic()
    with _registry_lock:
        hit = _coverage.get(key)
    if hit and now - hit[0] < COVERAGE_TTL_S:
        return hit[1]
    cov = store.coverage(conn, space, gate_open=gate)
    with _registry_lock:
        _coverage[key] = (time.monotonic(), cov)
    return cov


# ---------------------------------------------------------------------- the arms

@dataclasses.dataclass
class Arm:
    """One arm of the ranking, as the response reports it."""

    name: str
    ran: bool
    ranking: fusion.Ranking | None = None
    #: Why it did not run (typed), and the error code a 503 would carry for it.
    absent: str | None = None
    error: str | None = None
    message: str | None = None
    detail: dict = dataclasses.field(default_factory=dict)
    #: A resident view's embed arm only (_embed_arm): the src_ids whose score was rescored at full width, and the
    #: full-width unit query, so _exact_pool can rescore a pool member past them. Never serialised.
    exact: frozenset | None = dataclasses.field(default=None, repr=False, compare=False)
    qunit: object = dataclasses.field(default=None, repr=False, compare=False)

    def as_json(self) -> dict:
        out = {"ran": self.ran, "ranked": len(self.ranking.ids) if self.ranking else 0}
        if not self.ran:
            out.update({"absent": self.absent, "error": self.error})
        out.update(self.detail)
        return out


def _lexical_arm(lex: lexical.LexicalIndex, text: str) -> Arm:
    lexemes, tfs = lexical.terms(text)
    ids, scores = lex.rank(lexemes, tfs, limit=ARM_DEPTH)
    return Arm(fusion.LEXICAL, True,
               fusion.Ranking(fusion.LEXICAL, ids, [float(x) for x in scores]),
               detail={"query_terms": len(lexemes)})


def _embed_arm(idx: VectorIndex, qvec, *, source: str, read=None) -> Arm:
    """The embed arm. On a resident view, `read` (ids -> {src_id: stored full-width vector}, ONE read) rescores the
    view's first rescore_depth() candidates at full width and orders them by that score; the rest keep the view's
    order and score until _exact_pool. A candidate whose stored row is missing or malformed drops out. Ties keep the
    view's order."""
    qv = np.asarray(qvec, dtype=np.float64)
    ids, scores = idx.rank((qv / np.linalg.norm(qv)).astype(np.float32))
    if not idx.view:
        return Arm(fusion.EMBED, True,
                   fusion.Ranking(fusion.EMBED, ids, [float(x) for x in scores]),
                   detail={"query_vector": source})
    if read is None:
        raise RuntimeError("the embed arm over a resident view needs `read` to rescore at full width")
    unit = (qv / np.linalg.norm(qv)).astype(np.float32)
    n = min(len(ids), rescore_depth())
    full = read(ids[:n])
    head = []
    for pos, src in enumerate(ids[:n]):
        v = _unit32(full[src]) if src in full else None
        if v is not None:
            head.append((-float(v @ unit), pos, src))
    head.sort()
    arm = Arm(fusion.EMBED, True,
              fusion.Ranking(fusion.EMBED, [s for _, _, s in head] + list(ids[n:]),
                             [-c for c, _, _ in head] + [float(x) for x in scores[n:]]),
              detail={"query_vector": source, "resident_dims": idx.resident, "rescore_depth": n,
                      "rescored": len(head)})
    arm.exact = frozenset(s for _, _, s in head)
    arm.qunit = unit
    return arm


def _exact_pool(pool: list[Candidate], arm: Arm | None, read) -> list[Candidate]:
    """Every pool member's embed cosine at full width (pxx, condition 2). Without a view (arm.exact None) the
    pool is returned as it is, and nothing is read. A member past the view's rescored head still carries a VIEW
    cosine; its full-width cosine comes from ONE more read of at most the pool's size.

    A member whose stored row is missing or malformed drops out of the embed arm, as a head row does in _embed_arm
    before fusion: cosine None, no embed rank, AND fused_score what fusion.rrf gives without that rank (its embed share
    1/(RRF_K + rank) gone). One that no other arm ranks leaves the pool, since nothing ranks it any more. The pool is
    then re-sorted by the corrected score, ties in their place, so the order without a rerank matches it (GitHub Codex
    P2 on #543, pxx: the share used to stay, and an embed-only row with no full-width vector could still be
    returned)."""
    if arm is None or not arm.ran or arm.exact is None:
        return pool
    todo = {c.src_id for c in pool if c.cosine is not None and c.src_id not in arm.exact}
    arm.detail["rescored_pool"] = len(todo)
    if not todo:
        return pool
    full = read(sorted(todo))
    kept, dropped = [], 0
    for pos, c in enumerate(pool):
        if c.src_id in todo:
            v = _unit32(full[c.src_id]) if c.src_id in full else None
            if v is None:
                ranks = {**c.ranks, fusion.EMBED: None}
                if all(r is None for r in ranks.values()):
                    dropped += 1
                    continue
                # Summed again as fusion.rrf sums it, in its arm order, rather than the share subtracted: a subtraction
                # leaves a hair off the exact score, and an exact tie with another row then sorts the wrong way.
                c = dataclasses.replace(c, cosine=None, ranks=ranks, fused_score=sum(
                    1.0 / (fusion.RRF_K + ranks[a]) for a in fusion.ARMS if ranks.get(a) is not None))
            else:
                c = dataclasses.replace(c, cosine=float(v @ arm.qunit))
        kept.append((pos, c))
    if dropped:
        arm.detail["dropped_pool"] = dropped
    kept.sort(key=lambda pc: (-pc[1].fused_score, pc[0]))   # _pool's order is fused order: unchanged scores stay put
    return [c for _, c in kept]


def _embed_absent(absent: str, error: str, message: str) -> Arm:
    return Arm(fusion.EMBED, False, absent=absent, error=error, message=message)


def _unreferenced(space: spaces.Space) -> Arm:
    return _embed_absent("space_unreferenced", "E_SPACE_UNREFERENCED",
                         f"space {space.name!r} has no reference yet: the "
                         "mesh_embed_catch_up job records it on its first run")


def _ranking_fields(arms: list[Arm] | None, *, reranked: bool) -> dict:
    """`method`, `rrf_k`, `arms` and `degraded`, the fields that say how a response was
    ranked. With no arms (a refusal before anything was ranked) all four are null."""
    if not arms:
        return {"method": None, "rrf_k": None, "arms": None, "degraded": None}
    ran = [a.name for a in arms if a.ran]
    missing = [a for a in arms if not a.ran]
    degraded = None
    if missing:
        why = "; ".join(f"the {a.name} arm did not run ({a.error}: {a.absent})"
                        for a in missing)
        degraded = (f"{' and '.join(ran)} only: {why}" if ran
                    else f"no arm ran: {why}")
    return {"method": fusion.method_name(ran, reranked=reranked),
            "rrf_k": fusion.RRF_K if len(ran) > 1 else None,
            "arms": {a.name: a.as_json() for a in arms},
            "degraded": degraded}


# ------------------------------------------------------------------ candidates

_CANDIDATES = (
    "SELECT sm.id, sm.key, sm.version, sm.created_at, sm.author_label, "
    "  CASE WHEN jsonb_typeof(sm.value->'body') = 'string' THEN sm.value->>'body' END "
    "    AS body, "
    "  (SELECT t.dst FROM mesh_edges t WHERE t.src_id = sm.id "
    "     AND t.edge_type = 'in_thread' ORDER BY t.dst LIMIT 1) AS thread_id "
    "FROM shared_memory sm "
    "WHERE sm.id = ANY(%(ids)s::uuid[]) AND sm.is_head AND sm.namespace = %(ns)s "
    "  AND sm.key LIKE 'msg/%%' AND (%(open)s OR sm.embed_policy = 'allowed')"
)


def _r6(x) -> float | None:
    return None if x is None else round(float(x), 6)


def body_fields(raw) -> tuple[str, bool]:
    """(`body`, `body_truncated`) for a message on this surface: the stripped string body,
    cut at embedjob.MAX_CHARS, and whether it was cut. A row whose body is not a string
    reads '' (the SQL hands it over as NULL). ONE rule, used for every related hit and for
    the context anchor (divergence 027), so the two cannot drift apart."""
    text = (raw or "").strip()
    return text[:embedjob.MAX_CHARS], len(text) > embedjob.MAX_CHARS


@dataclasses.dataclass
class Candidate:
    src_id: str
    key: str
    version: int
    recorded_at: object
    author: str | None
    thread_id: str | None
    body: str
    body_truncated: bool
    fused_score: float
    #: 1-based rank per arm, None where that arm did not rank it.
    ranks: dict
    #: The embed arm's cosine and the lexical arm's TF-IDF cosine, None where that arm
    #: did not rank this candidate.
    cosine: float | None
    lexical_score: float | None
    rerank_score: float | None = None
    #: A graph hit's namespace, and the chunk that decided it with its character span in the
    #: entry's text (divergence 060). None on a mail hit, whose JSON is unchanged.
    namespace: str | None = None
    chunk_no: int | None = None
    chunk_span: tuple[int, int] | None = None
    #: A card hit's `thread_tier` (apps/cards/store.thread_tier): whether its `thread_id` was declared or a
    #: hook inferred it. None, and left out, on every other hit.
    thread_tier: str | None = None
    #: True on a mail search hit whose body holds the whole query (apps/meshsem/exactpassage.py). None, and left
    #: out, unless MESHY_SEARCH_EXACT_PASSAGE is on.
    exact_passage: bool | None = None

    def as_json(self) -> dict:
        out = self._as_json()
        if self.namespace is not None:
            out.update({"namespace": self.namespace, "chunk_no": self.chunk_no,
                        "chunk_span": list(self.chunk_span) if self.chunk_span else None})
        if self.thread_tier is not None:
            out["thread_tier"] = self.thread_tier
        if self.exact_passage is not None:
            out["exact_passage"] = self.exact_passage
        return out

    def _as_json(self) -> dict:
        return {"key": self.key, "version": self.version,
                "recorded_at": self.recorded_at.isoformat(), "author": self.author,
                "thread_id": self.thread_id, "fused_score": _r6(self.fused_score),
                "ranks": dict(self.ranks), "cosine": _r6(self.cosine),
                "lexical_score": _r6(self.lexical_score),
                "rerank_score": _r6(self.rerank_score),
                "body": self.body, "body_truncated": self.body_truncated}


def _pool(conn, fused: list[fusion.Fused], *, gate: bool, exclude_ids: set[str],
          exclude_thread: str | None, lookup=None) -> tuple[list[Candidate], int, dict[str, int]]:
    """The pool every route ranks from (the reranker scores it; without the reranker its
    first k rows are the answer): at most POOL live candidates in fused order, ALWAYS
    including the first ARM_GUARANTEE live candidates of each arm's own ranking, with the
    slots those leave filled in fused order. Returns the pool, how many candidates were
    looked up against shared_memory, and per arm how many rows its reservation ADDED
    beyond the first POOL live candidates in fused order (0 for an arm that did not run
    or ranked nothing; a row both arms reserve counts under each).

    LIVE is what a hit may be: returned by _CANDIDATES (the head, in the namespace, past
    the gate), not in `exclude_ids` and not in `exclude_thread`. Every walk skips a row
    that is not live, and it never counts toward a reservation.

    What that changes. A reserved row the fused top POOL already holds costs nothing; one
    it does not hold takes the slot of the LAST fused row that is not itself reserved. So
    the pool is the fused top POOL up to its first difference and reserved rows after it:
    the no-rerank answer's first k rows are unchanged for every k up to there (never
    fewer than POOL - 2 * ARM_GUARANTEE = 20), and with nothing added the pool is exactly
    the fused top POOL. With one arm the fused order IS that arm's order: nothing is added.
    """
    live: dict[str, dict] = {}
    looked: set[str] = set()

    def look(ids: list[str]) -> None:
        fresh = [sid for sid in ids if sid not in looked]
        if not fresh:
            return
        looked.update(fresh)
        if lookup is not None:          # a graph namespace (apps/meshsem/graphsearch.py)
            rows = lookup(conn, fresh)
        else:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(_CANDIDATES, {"ids": fresh, "ns": MESH_CHAT_NAMESPACE, "open": gate})
                rows = cur.fetchall()
        for r in rows:
            sid = str(r["id"])
            if sid in exclude_ids:
                continue
            if exclude_thread is not None and r["thread_id"] == exclude_thread:
                continue
            live[sid] = r

    # The fused walk: the first POOL live candidates in fused order, no further than
    # EXAMINE_MAX. This alone was the pool before the reservation.
    top: list[int] = []
    for lo, hi in ((0, EXAMINE_FIRST), (EXAMINE_FIRST, EXAMINE_MAX)):
        chunk = fused[lo:hi]
        if not chunk or len(top) >= POOL:
            break
        look([f.src_id for f in chunk])
        for offset, f in enumerate(chunk):
            if f.src_id in live:
                top.append(lo + offset)
                if len(top) >= POOL:
                    break

    # Each arm's reservation, walked down the arm's OWN ranking (read back from the fused
    # ranks), skipping rows that are not live, until ARM_GUARANTEE or the ranking ends.
    ranked: dict[str, list[tuple[int, int]]] = {arm: [] for arm in fusion.ARMS}
    for i, f in enumerate(fused):
        for arm, rank in f.ranks.items():
            if rank is not None:
                ranked[arm].append((rank, i))
    reserved: dict[str, list[int]] = {}
    for arm, pairs in ranked.items():
        order = [i for _, i in sorted(pairs)]
        got: list[int] = []
        for n, i in enumerate(order):
            if len(got) >= ARM_GUARANTEE:
                break
            if fused[i].src_id not in looked:
                look([fused[j].src_id for j in order[n:n + ARM_CHUNK]])
            if fused[i].src_id in live:
                got.append(i)
        reserved[arm] = got

    held = set(top)
    members: set[int] = set().union(*reserved.values())
    for i in top:                                 # the free slots, in fused order
        if len(members) >= POOL:
            break
        members.add(i)
    guaranteed = {arm: sum(1 for i in got if i not in held) for arm, got in reserved.items()}

    out: list[Candidate] = []
    for i in sorted(members):                     # the fused order, reserved rows included
        f = fused[i]
        r = live[f.src_id]
        body, truncated = body_fields(r["body"])
        out.append(Candidate(src_id=f.src_id, key=r["key"], version=r["version"],
                             recorded_at=r["created_at"], author=r["author_label"],
                             thread_id=r["thread_id"], body=body, body_truncated=truncated,
                             fused_score=f.score, ranks=dict(f.ranks),
                             cosine=f.arm_scores[fusion.EMBED],
                             lexical_score=f.arm_scores[fusion.LEXICAL],
                             namespace=r.get("namespace"), chunk_no=r.get("chunk_no"),
                             chunk_span=r.get("chunk_span"), thread_tier=r.get("thread_tier")))
    return out, len(looked), guaranteed


def _rerank(query_text: str, pool: list[Candidate], requested: bool
            ) -> tuple[list[Candidate], providers.Reranked | None]:
    if not requested or not pool:
        return pool, None
    result = providers.rerank(query_text, [c.body for c in pool])
    if not result.ran:
        return pool, result
    for c, s in zip(pool, result.scores):
        c.rerank_score = s
    order = {id(c): i for i, c in enumerate(pool)}      # ties keep the fused order
    ranked = sorted(pool, key=lambda c: (-c.rerank_score, order[id(c)]))
    return ranked, result


#: What a context `related` hit's rerank_score is (divergence 032, amended): the ONE-WAY
#: logit with the anchor as the reranker's query and the hit as its document, "forward"
#: from the anchor. The stored related_to edges decide on pair = mean(forward, reverse),
#: a different quantity, which each hit carries under `edge`. "none" when no reranker
#: scored this request; then `score_absent` says why, as `thread_absent` does for a thread.
SCORE_FORWARD = "forward"
SCORE_NONE = "none"


def _score_fields(rr: providers.Reranked | None, requested: bool) -> dict:
    if rr is not None and rr.ran:
        return {"score_kind": SCORE_FORWARD, "score_absent": None}
    return {"score_kind": SCORE_NONE,
            "score_absent": "rerank_unavailable" if requested else "rerank_not_requested"}


def wire_attempts(attempts) -> list[dict]:
    """Provider attempts as the wire may carry them: id and verdict, never a host.
    apps/modelhost/views.py keeps base_url off the wire; a MUST-8 refusal reason names
    the IP it refused, so it is reduced to its verdict here."""
    out = []
    for a in attempts or ():
        item = {"id": a.get("id"), "verdict": a.get("verdict")}
        for k in ("min_cosine", "ms", "finish_reason"):
            if a.get(k) is not None:
                item[k] = a[k]
        if a.get("verdict") not in ("refused_must8",) and a.get("reason"):
            item["reason"] = a["reason"]
        out.append(item)
    return out


# ---------------------------------------------------------------------- answers

class SemanticUnavailable(Exception):
    """A typed refusal with the envelope every response carries."""

    def __init__(self, status: int, code: str, message: str, envelope: dict, *, retry_after: int | None = None):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message, self.envelope = status, code, message, envelope
        #: Seconds for a Retry-After header, when the refusal is temporary (E_INDEX_WARMING).
        self.retry_after = retry_after

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.envelope}


def _envelope(space, cov, idx_desc, *, embed_id=None, rerank_id=None, reranked=False,
              embed_attempts=(), rerank_attempts=(), arms=None) -> dict:
    return {"space": space.name, "coverage": cov.as_json() if cov else None,
            "index": idx_desc, "providers": {"embed": embed_id, "rerank": rerank_id},
            "reranked": bool(reranked),
            **_ranking_fields(arms, reranked=bool(reranked)),
            "attempts": {"embed": wire_attempts(embed_attempts),
                         "rerank": wire_attempts(rerank_attempts)}}


def _prepare(conn, space):
    """(gate, reference, coverage, vector index, lexical index, ceiling), read in ONE
    short server_internal transaction."""
    gate = store.forbidden_rows_open()
    ceiling = _ceiling()
    idx = index_for(space)
    lex = lexical_index_for(space)
    with dbconn.request_server_internal(conn) as c:
        with c.cursor() as cur:
            state = store.read_state(cur, space)
        cov = coverage_for(c, space, gate)
        try:
            reference = store.reference_of(state, space)
        except store.ReferenceMismatch as exc:
            raise SemanticUnavailable(503, "E_SPACE_MISMATCH", str(exc),
                                      _envelope(space, cov, _describe(idx, lex, ceiling)))
        refresh_or_warming(idx, lex, c, ceiling, lambda: _envelope(space, cov, _describe(idx, lex, ceiling)))
    return gate, reference, cov, idx, lex, ceiling


E_INDEX_WARMING = "E_INDEX_WARMING"


def refresh_or_warming(idx, lex, conn, ceiling: int, envelope) -> None:
    """A request's refresh of both indexes. While another request builds an index for the first time, answer a
    temporary 503 with Retry-After (E_INDEX_WARMING) instead of queueing behind the build (13 Nov 2026 stalls)."""
    try:
        idx.refresh(conn, ceiling=ceiling)
        lex.refresh(conn, ceiling=ceiling)
    except lexical.IndexWarming as exc:
        raise SemanticUnavailable(503, E_INDEX_WARMING, str(exc), envelope(),
                                  retry_after=lexical.WARMING_RETRY_AFTER_S) from exc


def _describe(idx: VectorIndex, lex: lexical.LexicalIndex, ceiling: int) -> dict:
    out = idx.describe(ceiling)
    out["lexical"] = lex.describe(ceiling)
    return out


def search(conn, space: spaces.Space, q: str, *, k: int = DEFAULT_K,
           rerank: bool = True) -> dict:
    """GET /v1/mesh/search. `conn` is a serving connection with NO open transaction."""
    gate, reference, cov, idx, lex, ceiling = _prepare(conn, space)
    lex_arm = _lexical_arm(lex, q)
    got, embed_attempts = None, []
    if reference is None:
        embed_arm = _unreferenced(space)
    else:
        try:
            got = providers.embed_in_space(space, [space.query(q)], reference, budget_s=15.0)
            embed_attempts = got.attempts
            embed_arm = _embed_arm(idx, got.vectors[0], source="embedded", **_view_read(conn, idx))
        except providers.EmbedUnavailable as exc:
            embed_attempts = exc.attempts
            embed_arm = _embed_absent("embed_unavailable", "E_EMBED_UNAVAILABLE", str(exc))
    arms = [lex_arm, embed_arm]
    fused = fusion.rrf([a.ranking for a in arms if a.ran])
    with dbconn.request_server_internal(conn) as c:
        pool, examined, guaranteed = _pool(c, fused, gate=gate, exclude_ids=set(),
                                           exclude_thread=None)
        if idx.view:
            pool = _exact_pool(pool, embed_arm, lambda ids: stored_vectors(c, idx, ids))
    if not pool and not embed_arm.ran:
        # NEVER AN EMPTY SUCCESS. The embedding arm could not run and the lexical arm
        # alone has nothing to return: that is the embed arm's refusal, typed, with the
        # envelope saying the lexical arm ran and found nothing.
        raise SemanticUnavailable(
            503, embed_arm.error,
            f"{embed_arm.message}; the lexical arm ran alone and matched nothing",
            _envelope(space, cov, _describe(idx, lex, ceiling),
                      embed_attempts=embed_attempts, arms=arms))
    ranked, rr = _rerank(q, pool, rerank)
    exact = None
    if rr is not None and rr.ran and exact_passage_on():
        # A copied passage the reranker buried comes back first, when exactly one hit holds it (exactpassage.py).
        ranked, containing, promoted = exactpassage.promote(q, ranked, lambda c: c.body)
        for c in ranked:
            c.exact_passage = False
        for c in containing:
            c.exact_passage = True
        exact = {"matches": len(containing), "promoted": ranked[0].key if promoted else None}
    hits = ranked[:k]
    body = _envelope(space, cov, _describe(idx, lex, ceiling),
                     embed_id=got.provider_id if got else None,
                     rerank_id=rr.provider_id if rr and rr.ran else None,
                     reranked=bool(rr and rr.ran), embed_attempts=embed_attempts,
                     rerank_attempts=rr.attempts if rr else (), arms=arms)
    body.update({
        "k": k, "rerank_requested": bool(rerank),
        "hits": [h.as_json() for h in hits],
        # A prefix is never shown as the whole: more candidates existed than were returned.
        "truncated": len(ranked) > len(hits),
        "pool": {"size": len(pool), "limit": POOL, "examined": examined,
                 "fused": len(fused), "guaranteed": guaranteed,
                 "ranked_vectors": len(embed_arm.ranking.ids) if embed_arm.ran else 0,
                 "ranked_lexical": len(lex_arm.ranking.ids)},
        "query_vector": ({"source": "embedded", "canary_min_cosine": got.canary_min_cosine}
                         if got else None),
    })
    if exact is not None:
        body["exact_passage"] = exact
    return body


# ----------------------------------------------------------------------- context

THREAD_LIMIT = 100
REPLIES_LIMIT = 50
REPLY_DEPTH = 20


def _anchor(conn, key: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, key, version, created_at, author_label, embed_policy, "
            "  CASE WHEN jsonb_typeof(value->'body') = 'string' THEN value->>'body' END AS body,"
            "  (SELECT t.dst FROM mesh_edges t WHERE t.src_id = sm.id "
            "     AND t.edge_type = 'in_thread' ORDER BY t.dst LIMIT 1) AS thread_id, "
            "  EXISTS (SELECT 1 FROM mesh_edges e WHERE e.src_id = sm.id) AS projected "
            "FROM shared_memory sm WHERE namespace = %s AND key = %s AND is_head",
            (MESH_CHAT_NAMESPACE, key))
        row = cur.fetchone()
    return dict(row) if row else None


def _hit_json(h) -> dict:
    return {"key": h.key, "version": h.version, "recorded_at": h.recorded_at.isoformat(),
            "author": h.author, "via": h.src_field}


def _reply_chain(conn, anchor: dict) -> dict:
    """Ancestors by replies_to, one hop at a time, cycle- and depth-bounded; plus the
    direct replies. Each ancestor step says how it ended, so a chain that stops is never
    read as a chain that reached its root."""
    from apps.mesh import graph as mesh_graph        # read-only use of the edge projection

    ancestors, seen, end = [], {anchor["key"]}, "root"
    current_id = anchor["id"]
    with conn.cursor(row_factory=dict_row) as cur:
        for _ in range(REPLY_DEPTH):
            cur.execute("SELECT dst FROM mesh_edges WHERE src_id = %s "
                        "AND edge_type = 'replies_to' ORDER BY dst LIMIT 1", (current_id,))
            edge = cur.fetchone()
            if edge is None:
                end = "root"
                break
            parent = edge["dst"]
            if parent in seen:
                end = "cycle"
                break
            seen.add(parent)
            cur.execute("SELECT id, key, version, created_at, author_label FROM shared_memory "
                        "WHERE namespace = %s AND key = %s AND is_head",
                        (MESH_CHAT_NAMESPACE, parent))
            row = cur.fetchone()
            if row is None:
                ancestors.append({"key": parent, "found": False})
                end = "missing_parent"
                break
            ancestors.append({"key": row["key"], "version": row["version"], "found": True,
                              "recorded_at": row["created_at"].isoformat(),
                              "author": row["author_label"]})
            current_id = row["id"]
        else:
            end = "depth_limit"
    replies = mesh_graph.replies(conn, anchor["key"], limit=REPLIES_LIMIT)
    return {"ancestors": ancestors, "ended": end, "depth_limit": REPLY_DEPTH,
            "replies": [_hit_json(h) for h in replies.hits],
            "replies_truncated": replies.truncated}


def context(conn, space: spaces.Space, key: str, *, k: int = DEFAULT_K,
            rerank: bool = True) -> dict:
    """GET /v1/mesh/context: one message's thread, reply chain, and related messages from
    OTHER threads, ranked by the same fusion as search with the anchor's text (lexical)
    and its vector (embed) as the query. `conn`: serving connection, no transaction.

    NO CALLER, NO ANCHOR BODY. This entry point has nobody to ask memory.read's gates
    about, so the anchor's body is withheld (`body_withheld: "no_caller"`); the route
    calls `context_for` with the authenticated reader (divergence 027)."""
    return context_for(None, conn, space, key, k=k, rerank=rerank)


@dataclasses.dataclass(frozen=True)
class Reader:
    """Who asked for a context, for the anchor body's gate (divergence 027)."""
    #: The AUTHENTICATED peer (apps/peers/auth.authenticate_peer), never a request field.
    peer_id: object
    #: The request proved it holds the key its token is bound to (a DPoP proof, checked
    #: and spent), or DPoP is not enforced: memory.read's POST needs the same.
    possession: bool
    #: The raw public key that proof was checked against (the token's `cnf`), or None
    #: when DPoP is not enforced. It must be a key memory.read would verify under.
    key: bytes | None = None


#: Why an anchor's body is withheld: the first of memory.read's gates, in the order the
#: invoke path applies them, that this caller would fail. Null when the body is shown.
#: The pipeline's own three are apps/calls/pipeline.REFUSED_*.
WITHHELD_NO_CALLER = "no_caller"
WITHHELD_NO_PROOF = "no_proof_of_possession"
WITHHELD_NAMESPACE = "namespace_permission"


def _reader_withheld(conn, reader: Reader | None) -> str | None:
    """The half of memory.read's gate that depends on the READER alone, in the invoke
    path's order: DPoP on the POST (apps/peers/auth.py), then the pipeline's envelope-free
    gates (apps/calls/pipeline.refusal_without_envelope: the skill resolves, the caller
    holds a usable key, the tenancy or skill-grant authorization). None when every one
    passes, else the first that refuses. Its answer is the same for every row this reader
    asks about, so a caller asking about many rows asks it once (divergence 040, slice 2
    review F2)."""
    from apps.calls import pipeline
    from apps.calls import route as routing

    if reader is None:
        return WITHHELD_NO_CALLER
    if not reader.possession:
        return WITHHELD_NO_PROOF
    refused = pipeline.refusal_without_envelope(
        conn, caller_peer_id=reader.peer_id,
        target_peer_id=routing.memory_peer_id_from_settings(), skill_name="memory.read",
        caller_key=reader.key)
    if refused is not None:
        return refused
    return None


def _key_withheld(conn, reader: Reader, key: str) -> str | None:
    """The other half: handle_read's namespace predicate (check_permission, need read),
    asked about `key`, the row's own key, `Key(key)`, and never the namespace as a whole
    (divergence 040, slice 2): the answer is about ONE row, and the day a key-scoped decision
    exists a namespace-wide stand-in would answer a different question. Asked only after
    `_reader_withheld` passed this reader. Asked with NO principal: this gate takes no
    capability token (design 3.4, site 11, tokenless by decision; divergence 040 slice 6), so
    a reader whose only authority is a token is refused here, the fail-closed direction."""
    from apps.memoryskills.errors import HandlerError
    from apps.memoryskills.permissions import Key, check_permission

    try:
        check_permission(MESH_CHAT_NAMESPACE, reader.peer_id, "read", conn,
                         resource=Key(key))
    except HandlerError:
        return WITHHELD_NAMESPACE
    return None


def _anchor_body_withheld(conn, reader: Reader | None, key: str) -> str | None:
    """None when memory.read without a capability would answer this reader for `key` (the
    row's own key) in this namespace, else why not: `_reader_withheld`, then `_key_withheld`
    for `key`. Since divergence 040 slice 6 memory.read also takes a capability token, and a
    token can show a key this gate withholds: the gate has no token (design 3.4, site 11,
    tokenless by decision), so it answers as memory.read WITHOUT one, the fail-closed
    direction (T21). Run in
    the SAME transaction that reads the anchor row, so a grant revoked while the request was
    preparing (the route checks the namespace grant first, in its own transaction, and
    `_prepare` can then take seconds) does not show the body. Every caller passes the row's
    key. handle_read's row rules (the head, a row that exists) are `_anchor`'s query."""
    refused = _reader_withheld(conn, reader)
    if refused is not None:
        return refused
    return _key_withheld(conn, reader, key)


def body_withheld_many(conn, reader: Reader | None, keys) -> dict[str, str | None]:
    """`_anchor_body_withheld` for each of `keys`, by key: whether memory.read without a
    capability would show this reader each one. `_reader_withheld` is asked ONCE (divergence 040, slice 2 review F2): its answer depends on the reader alone, and
    asked per row it cost a full addressed-wait page (50 rows) fifty runs of it, measured at
    about half a second more than one. `_key_withheld` is asked once per distinct key, in
    the order given, so each row's answer is its own. A reader the first half refuses gets
    that refusal for every key. The same answers, key for key, as `_anchor_body_withheld`."""
    refused = _reader_withheld(conn, reader)
    answers: dict[str, str | None] = {}
    for key in keys:
        if key in answers:
            continue
        if refused is not None:
            answers[key] = refused
        else:
            answers[key] = _key_withheld(conn, reader, key)
    return answers


def namespace_body_withheld(conn, reader: Reader | None) -> str | None:
    """027's gate asked about the NAMESPACE, for a route that shows the bodies of a window
    of rows it has not read yet: the summaries' steps 5 and 10 (divergence 037,
    apps/meshsum/views.py). `_reader_withheld`, then the namespace predicate with
    `NAMESPACE_WIDE`: the question the route's own gate asks (divergence 040, slice 2
    review F1; site 12), asked again in the transaction that reads the rows, for 027's
    reason (a grant revoked while the request prepared must not see the bodies). It is not
    a stand-in for a key: no key is known when it is asked, and a per-row rule over the
    window, when one exists, is the design's own (docs/design/thread-summaries.md 2.3,
    `excluded.object_denied`)."""
    from apps.memoryskills.errors import HandlerError
    from apps.memoryskills.permissions import NAMESPACE_WIDE, check_permission

    refused = _reader_withheld(conn, reader)
    if refused is not None:
        return refused
    try:
        check_permission(MESH_CHAT_NAMESPACE, reader.peer_id, "read", conn,
                         resource=NAMESPACE_WIDE)
    except HandlerError:
        return WITHHELD_NAMESPACE
    return None


#: The public name of the gate, for the other surface that shows a mesh message's body only
#: as memory.read without a capability would: a live frame's preview and an addressed wait's
#: (divergence 033, apps/wsgw/mesh_notify.body_withheld; divergence 040 slice 6).
body_withheld = _anchor_body_withheld


def context_for(reader: Reader | None, conn, space: spaces.Space, key: str, *, k: int,
                rerank: bool) -> dict:
    """`context` for an authenticated reader: the anchor's body is shown only when
    memory.read without a capability would show this reader this key (`_anchor_body_withheld`,
    asked about the anchor row's key; the route takes no token, divergence 040). `k` and
    `rerank` have no defaults here: the route passes both, and `context` keeps the
    function defaults."""
    from apps.mesh import graph as mesh_graph

    gate, reference, cov, idx, lex, ceiling = _prepare(conn, space)
    with dbconn.request_server_internal(conn) as c:
        anchor = _anchor(c, key)
        if anchor is None:
            raise SemanticUnavailable(404, "E_NOT_FOUND",
                                      f"no head message {key!r} in {MESH_CHAT_NAMESPACE}",
                                      _envelope(space, cov, _describe(idx, lex, ceiling)))
        withheld = _anchor_body_withheld(c, reader, anchor["key"])
        thread = None
        if anchor["thread_id"] is not None:
            t = mesh_graph.thread(c, anchor["thread_id"], limit=THREAD_LIMIT)
            thread = {"thread_id": anchor["thread_id"],
                      "hits": [_hit_json(h) for h in t.hits], "truncated": t.truncated}
        chain = _reply_chain(c, anchor)

    body, truncated = body_fields(anchor["body"]) if withheld is None else (None, None)
    anchor_json = {"key": anchor["key"], "version": anchor["version"],
                   "recorded_at": anchor["created_at"].isoformat(),
                   "author": anchor["author_label"], "thread_id": anchor["thread_id"],
                   # The thread and reply chain come from mesh_edges (divergence 014). An
                   # anchor the edge projection has not reached yet has no thread HERE,
                   # which is a fact about the projection, not about the message.
                   "edges_projected": bool(anchor["projected"]),
                   # The body is the ROW's (divergence 027), not the projection's, so it
                   # is here before the edges job has run. It is shown only when
                   # memory.read without a capability would show this reader this key
                   # (_anchor_body_withheld,
                   # in the transaction that read the row), and _anchor selects the row
                   # handle_read selects with no version (the head of namespace, key).
                   # Otherwise body and body_truncated are null and body_withheld names
                   # the first gate memory.read would have refused at.
                   "body": body, "body_truncated": truncated, "body_withheld": withheld}

    related: dict = {"hits": [], "absent": None}
    hits: list[Candidate] = []
    embed_id, embed_attempts, rr, arms = None, [], None, None
    anchor_text = embedjob.document_text(anchor["body"])
    forbidden = anchor["embed_policy"] == "forbidden" and not gate
    if forbidden:
        related["absent"] = "anchor_embed_forbidden"
    elif anchor_text is None:
        related["absent"] = "anchor_has_no_text"
    else:
        lex_arm = _lexical_arm(lex, anchor_text)
        if reference is None:
            embed_arm = _unreferenced(space)
        else:
            qvec = _query_vector_of(conn, idx, str(anchor["id"]))
            source = "stored"
            if qvec is None:
                try:
                    got = providers.embed_in_space(space, [space.document(anchor_text)],
                                                   reference, budget_s=15.0)
                    qvec, source = got.vectors[0], "embedded"
                    embed_id, embed_attempts = got.provider_id, got.attempts
                except providers.EmbedUnavailable as exc:
                    embed_attempts = exc.attempts
            embed_arm = (_embed_arm(idx, qvec, source=source, **_view_read(conn, idx)) if qvec is not None
                         else _embed_absent("embed_unavailable", "E_EMBED_UNAVAILABLE",
                                            "no admitted embed provider for the anchor"))
        arms = [lex_arm, embed_arm]
        fused = fusion.rrf([a.ranking for a in arms if a.ran])
        with dbconn.request_server_internal(conn) as c:
            pool, examined, guaranteed = _pool(c, fused, gate=gate,
                                               exclude_ids={str(anchor["id"])},
                                               exclude_thread=anchor["thread_id"])
            if idx.view:
                pool = _exact_pool(pool, embed_arm, lambda ids: stored_vectors(c, idx, ids))
        ranked, rr = _rerank(anchor_text, pool, rerank)
        hits = ranked[:k]
        score = _score_fields(rr, rerank)
        related = {"hits": [{**h.as_json(), **score} for h in hits],
                   # Nothing to show AND an arm missing is that arm's absence, typed;
                   # nothing to show with both arms run is a real empty answer.
                   "absent": (embed_arm.absent if not pool and not embed_arm.ran else None),
                   "truncated": len(ranked) > len(hits),
                   "pool": {"size": len(pool), "limit": POOL, "examined": examined,
                            "fused": len(fused), "guaranteed": guaranteed},
                   "query_vector": {"source": embed_arm.detail.get("query_vector")},
                   "excluded": {"anchor": True, "thread_id": anchor["thread_id"]}}

    body = _envelope(space, cov, _describe(idx, lex, ceiling), embed_id=embed_id,
                     rerank_id=rr.provider_id if rr and rr.ran else None,
                     reranked=bool(rr and rr.ran), embed_attempts=embed_attempts,
                     rerank_attempts=rr.attempts if rr else (), arms=arms)
    body.update({"key": key, "k": k, "rerank_requested": bool(rerank),
                 "anchor": anchor_json, "thread": thread,
                 "thread_absent": None if thread is not None else (
                     "anchor_not_projected" if not anchor["projected"]
                     else "no_in_thread_edge"),
                 "reply_chain": chain, "related": related})
    from . import related_reads              # divergence 032: the persisted edges, BESIDE
    body["related_edges"] = related_reads.for_context(
        conn, space, anchor, gate=gate,
        absent=related["absent"] if forbidden or anchor_text is None else None)
    # Each related hit's stored edge with the anchor, or null: the pair's numbers beside
    # the hit's one-way score (divergence 032, amended). One query for the list.
    edges = related_reads.for_related_hits(conn, space, anchor, [h.src_id for h in hits],
                                           gate=gate)
    for h, out in zip(hits, related["hits"]):
        out["edge"] = edges.get(h.src_id)
    return body


__all__ = ["ARM_DEPTH", "ARM_GUARANTEE", "Arm", "DEFAULT_K", "MAX_K", "MAX_QUERY_CHARS", "POOL", "Reader",
           "SemanticUnavailable", "VectorIndex", "body_fields", "context", "context_for",
           "coverage_for", "forget", "index_for", "lexical_index_for", "search",
           "wire_attempts"]
