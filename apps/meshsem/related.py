"""The related_to job: persist the related messages context used to compute and discard.
Divergence 032. Registered in apps/jobs/registry.py as `mesh_related_edges`, after
mesh_embed_catch_up; runnable by hand as `python -m apps.meshsem.related_cli catch-up`.
The ONLY writer of mesh_related_edges, mesh_related_runs and mesh_related_projection
(sql_meshychat/013).

WHAT AN EDGE IS. For a head mesh message (the SOURCE), the candidate pool is the one
/v1/mesh/context builds (apps/meshsem/search.py: RRF of the lexical arm and the embedding
arm, the source's STORED vector as the query, head rows, the embed_policy gate, the
source's own thread excluded), restricted further to messages STRICTLY OLDER than the
source and not its DECLARED reply parent or children. HQB's reranker scores the pool with
the source as the query (forward) and the top `reverse_depth` again the other way round
(reverse); the pair's score is the mean of the two, and the pairs at or above the
CALIBRATED threshold, best `k`, are the source's edges (apps/meshsem/relatedcal
.select_edges, the same function the calibration measures). Strictly older means every
pair is scored exactly once, by its newer member.

ONE TICK, in order:
  0. While the embed_policy gate is CLOSED, every tick first deletes every edge, in ANY
     space, with a forbidden row at EITHER end, and every forbidden source's ledger row
     (projection writer, a transaction of its own). Level-triggered, so neither a
     calibration that is off nor a space that is no longer configured keeps rows derived
     from a forbidden message's content; the reads hide them before that in any case.
     Then the calibration (relatedcal.load). None, or calibrated for another space:
     record `not_calibrated` and return 0, a configuration state, not an error. No
     reranker is called: a fresh install stays off until an operator commits one.
  1. (transaction, projection writer) The state row. A changed embed_policy gate restarts
     the walk. A changed threshold_version deletes the space's edges and ledger rows of
     other versions and restarts. The walk's UPPER BOUND
     is the lower of the embed walk's watermark for the space (vectors decided, under the
     CURRENT gate) and the mesh_edges watermark (so "not in the source's thread" means
     DECLARED not in it, not "not projected yet": the auditor's ProjectionNotRun rule).
     Either missing: `embed_not_run` / `projection_not_run`, return 0. Read up to
     `max_batch` head rows after the watermark, at or below the bound.
  2. (no transaction) Decide each row: forbidden while the gate is closed
     (skipped_forbidden), no vector in the space (skipped_no_vector), or to relate. At most
     `relate_per_tick` are related; NOTHING to relate calls no model at all.
  3. ADMIT THE RERANKER: each declared rerank provider, in order, reranks the calibration's
     canary sets (one multi-document, one single-document: the job makes both call
     shapes); the first whose logits are all within the calibration's tolerance of the
     reference is PINNED for the tick. No provider reachable: `rerank_unavailable`;
     reachable but none admitted: `rerank_not_admitted`. Either way the status is
     committed, nothing else is written, and the registered job raises (E1).
  4. (short transaction, server_internal) Build each source's pool from the in-process
     indexes (search.index_for / lexical_index_for, refreshed incrementally). No embed
     call is ever made: the source's vector is the stored one.
  5. (no transaction) Rerank, source by source, every call pinned to the admitted
     provider. The deadline is checked BETWEEN sources, so a stop lands on a source
     boundary (`deadline`, not an error). A failure stops the tick AT the source it hit
     (see FAILURES): the sources completed before it are written in step 6, it and
     everything after it are not, and the job raises after the write.
  6. (transaction, projection writer) Re-read the state FOR UPDATE; if the watermark,
     the gate or the threshold version moved, another walker ran: ConcurrentWalk, nothing
     written. Otherwise, per completed source: delete its outgoing edges in the space,
     insert the new ones FROM shared_memory where the target is still a head row of the
     namespace that passes the gate (a target superseded, gated or deleted meanwhile is
     simply not written), upsert its ledger row; advance the watermark past the prefix of
     final rows. A ForeignKeyViolation (a message deleted under the insert) rolls the
     tick back as `concurrent_walk`; the next tick recomputes.

FAILURES, and why one message cannot stall the walk. A failed call is one of two kinds.
  * An OUTAGE says nothing about the input: the reranker was unreachable, timed out, the
    source's budget was spent, it answered 408/429/502/503/504, or the answer came from
    another provider. The tick stops at that source (`rerank_unavailable` /
    `rerank_provider_changed`): the sources completed before it are written, fully
    reranked by the pinned provider, and it and every later row wait for the next tick.
    A partially scored source keeps nothing.
  * A REFUSAL is the reranker ANSWERING that it will not score this input: any other HTTP
    status, or an answer of the wrong shape. HQB's llama.cpp reranker answers HTTP 500
    ("input (2270 tokens) is too large to process ... current batch size: 2048") for a
    pair of two dense 1,500-character texts (base64, measured 2026-09-23 on synthetic
    text), every time, and ONE such document fails the whole forward call it is in. A
    refused forward call is retried one document at a time; a refused reverse call is
    retried once. A pair refused again has NO score, so it can never be an edge (an edge
    needs both directions); it is counted (`pairs_refused` on the ledger row and the
    state row) and the source completes on the pairs that were scored (`rerank_refused`
    when none was). A source with a refused pair is final only if the reranker then
    passes its canaries again; if not, the refusals were an outage and the tick stops
    there. Before this rule a single such pair refused every tick at the same source.

NO INFERENCE IS EVIDENCE FOR ANOTHER. mesh_edges is read only to EXCLUDE candidates, and
only its declared rows. mesh_inferred_edges is never read, and mesh_related_edges only
deleted-then-rewritten per source (tests/test_meshsem_related.py T18 scans this file).

IDEMPOTENT PER SNAPSHOT. The reranker repeated a score with delta 0.0 on HQB, and a
source's edges are deleted then rewritten, so a re-run of a tick writes the same edges.
Across ticks the lexical idf drifts as the corpus grows, so a pool can shift slightly.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import re
import time

import psycopg
from psycopg.rows import dict_row

from apps.core import rls
from apps.jobs.result import TickResult
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from . import embedjob, fusion, providers, relatedcal, search, spaces, store

JOB_NAME = "mesh_related_edges"
STATE_TABLE = "mesh_related_projection"
#: Rows READ per tick: the job's declared cost bound (B5).
DEFAULT_BATCH = 256
#: Sources RERANKED per tick: about 0.6 s each on HQB (a 40-document forward call, median
#: 305 ms, plus ten one-document reverse calls, 27 ms each; synthetic text, 2026-09-23).
RELATE_PER_TICK = 24
#: One source's calls share this budget, so a slow GPU costs at most one source past the
#: tick's deadline (45 s + 20 s stays well inside the 140 s lease).
SOURCE_BUDGET_S = 20.0
#: Documents per forward rerank request (2026-10-07). The rerank server (:7876, -np 2) serves this job and
#: interactive search FIFO. A source's whole pool sent as one request made a search that arrived behind it wait
#: for all of it: txxxx measured 2,714 ms behind one 150-pair request and 371 ms behind the same pairs sent as
#: 10-document requests. Each score depends only on its own (query, document) pair, so the edges are identical.
RERANK_CHUNK = 10
#: Statuses the registered job reports as an ERROR (E1: a failure must not read as a pass).
ERROR_STATUSES = frozenset({"rerank_unavailable", "rerank_not_admitted",
                            "rerank_provider_changed", "concurrent_walk"})
#: HTTP answers that say the reranker is UNAVAILABLE, not that it refuses an input.
OUTAGE_HTTP = frozenset({408, 429, 502, 503, 504})
#: The budget of the canary re-check that makes a source with a refused pair final.
CONFIRM_BUDGET_S = 10.0

#: The clock the tick's deadline reads. A seam for the deadline test, nothing else.
_clock = time.monotonic


class RelatedJobFailed(RuntimeError):
    """Raised by the job body AFTER the tick's status is committed."""


class ConcurrentWalk(RuntimeError):
    """The walk's state moved between this tick's read and its write: nothing written."""


class _Refused(Exception):
    """A rerank call inside the tick failed as an OUTAGE: the tick stops at this source."""

    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


class _Answered(_Refused):
    """The pinned reranker ANSWERED this call with a refusal of its input. Where the caller
    cannot tell a refusal from an outage (score_source with no `confirm`) it is one."""

    def __init__(self, detail: str):
        super().__init__("rerank_unavailable", detail)


@dataclasses.dataclass(frozen=True)
class Report:
    space: str
    #: ok, deadline, relate_bound, or why the tick did nothing (see the module docstring).
    status: str
    detail: str | None = None
    rows_read: int = 0
    #: Rows whose outcome is final this tick; the watermark moved past exactly these.
    rows_processed: int = 0
    sources_related: int = 0
    sources_none_above: int = 0
    sources_no_candidates: int = 0
    #: Scored above the threshold, and every chosen target vanished before the write.
    sources_targets_gone: int = 0
    #: A non-empty pool none of whose pairs could be scored both ways (see FAILURES).
    sources_rerank_refused: int = 0
    #: Pairs the reranker refused twice this tick: never scored, never an edge.
    pairs_refused: int = 0
    skipped_forbidden: int = 0
    skipped_no_vector: int = 0
    edges_written: int = 0
    gate_open: bool = False
    gate_changed: bool = False
    #: Edges deleted this tick because the gate is CLOSED (in any space).
    purged: int = 0
    threshold_changed: bool = False
    threshold_version: str | None = None
    rerank_provider_id: str | None = None
    attempts: tuple = ()
    #: None, "deadline" or "relate_bound": the tick stopped before relating every row read.
    stopped: str | None = None
    more: bool = False
    watermark_at: _dt.datetime | None = None
    watermark_id: str | None = None
    #: {"resident_dims", "rescore_depth"} when the mail index is a resident view (search.resident_dims): the pools
    #: were picked by the view and rescored at full width, so a slow or odd tick can be traced. None otherwise.
    resident_view: dict | None = None


# ------------------------------------------------------------------ the state row

def _ensure_state(cur, space: spaces.Space) -> None:
    cur.execute(f"INSERT INTO {STATE_TABLE} (space) VALUES (%s) "
                "ON CONFLICT (space) DO NOTHING", (space.name,))


def _read_state(cur, space: spaces.Space, *, for_update: bool = False) -> dict:
    lock = " FOR UPDATE" if for_update else ""
    with cur.connection.cursor(row_factory=dict_row) as c:
        c.execute(f"SELECT * FROM {STATE_TABLE} WHERE space = %s{lock}", (space.name,))
        return dict(c.fetchone())


def _set_status(cur, space: spaces.Space, status: str, detail: str | None) -> None:
    cur.execute(f"UPDATE {STATE_TABLE} SET last_status = %s, last_status_at = now(), "
                "last_detail = %s, updated_at = now() WHERE space = %s",
                (status, (detail or None) and str(detail)[:2000], space.name))


def _record_status(conn, space: spaces.Space, status: str, detail: str | None) -> None:
    """Commit a status in a transaction of its own: a refused tick still says why."""
    with rls.projection_writer(conn):
        with conn.cursor() as cur:
            _ensure_state(cur, space)
            _set_status(cur, space, status, detail)


def _edges_watermark(cur) -> tuple | None:
    cur.execute("SELECT watermark_at, watermark_id FROM mesh_edges_projection "
                "WHERE projection = 'mesh_edges'")
    row = cur.fetchone()
    if row is None:
        return None
    at, wid = (row["watermark_at"], row["watermark_id"]) if isinstance(row, dict) else row
    return None if at is None else (at, wid)


# ------------------------------------------------------------------ reading rows

_SELECT_ROWS = (
    "SELECT sm.id, sm.key, sm.version, sm.embed_policy, sm.created_at, "
    "  CASE WHEN jsonb_typeof(sm.value->'body') = 'string' THEN sm.value->>'body' END "
    "    AS body, "
    "  me.provider_id AS vec_provider, "
    "  (SELECT t.dst FROM mesh_edges t WHERE t.src_id = sm.id "
    "     AND t.edge_type = 'in_thread' ORDER BY t.dst LIMIT 1) AS thread_id, "
    # The DECLARED reply neighbours, resolved to head ids by key: what it answers...
    "  ARRAY(SELECT h.id::text FROM mesh_edges e "
    "        JOIN shared_memory h ON h.namespace = sm.namespace AND h.key = e.dst "
    "         AND h.is_head "
    "        WHERE e.src_id = sm.id AND e.edge_type = 'replies_to') AS parent_ids, "
    # ...and what answers it.
    "  ARRAY(SELECT h.id::text FROM mesh_edges e "
    "        JOIN shared_memory c ON c.id = e.src_id "
    "        JOIN shared_memory h ON h.namespace = c.namespace AND h.key = c.key "
    "         AND h.is_head "
    "        WHERE e.edge_type = 'replies_to' AND e.dst = sm.key) AS child_ids "
    "FROM shared_memory sm "
    "LEFT JOIN mesh_embeddings me ON me.src_id = sm.id AND me.space = %(space)s AND me.chunk_no = 0 "
    "WHERE sm.namespace = %(ns)s AND sm.key LIKE 'msg/%%' AND sm.is_head "
    "  AND (%(after_at)s::timestamptz IS NULL "
    "       OR (sm.created_at, sm.id) > (%(after_at)s::timestamptz, %(after_id)s::uuid)) "
    "  AND (sm.created_at, sm.id) <= (%(up_at)s::timestamptz, %(up_id)s::uuid) "
    "ORDER BY sm.created_at ASC, sm.id ASC "
    "LIMIT %(limit)s"
)


# ---------------------------------------------------------- admission and scoring

def _canaries(pid: str, canary_sets, *, budget_s: float) -> tuple[float, str | None]:
    """(the largest canary logit difference, None) or (so far, why no answer), for ONE
    provider. The canaries are fixed synthetic sentences; no mesh content is sent."""
    worst = 0.0
    for query, docs, reference in canary_sets:
        got = providers.rerank(query, list(docs), provider_ids={pid}, budget_s=budget_s)
        if not got.ran:
            last = got.attempts[-1] if got.attempts else {}
            return worst, last.get("reason") or last.get("verdict") or "no answer"
        worst = max([worst] + [abs(a - b) for a, b in zip(got.scores, reference)])
    return worst, None


def admit_reranker(canary_sets, *, tolerance: float, budget_s: float = 30.0
                   ) -> tuple[str | None, list[dict]]:
    """(the first declared rerank provider whose canary logits all sit within `tolerance`
    of the reference, attempts). The canaries are fixed synthetic sentences; no mesh
    content is sent here."""
    eligible, refused = providers.content_providers("rerank")
    attempts: list[dict] = list(refused)
    for p in eligible:
        worst, failed = _canaries(p.id, canary_sets, budget_s=budget_s)
        if failed is not None:
            attempts.append({"id": p.id, "verdict": "failed", "reason": failed})
            continue
        if worst <= tolerance:
            attempts.append({"id": p.id, "verdict": "admitted", "max_delta": round(worst, 6),
                             "model": p.model})
            return p.id, attempts
        attempts.append({"id": p.id, "verdict": "not_admitted", "max_delta": round(worst, 6),
                         "reason": f"canary logits off by {worst:.4f} > {tolerance}: "
                                   "a different reranker behind this provider"})
    return None, attempts


def _older(conn, fused: list[fusion.Fused], src: dict) -> list[fusion.Fused]:
    """The fused candidates strictly older than the source, in fused order."""
    head = fused[:search.EXAMINE_MAX]
    if not head:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT id::text FROM shared_memory WHERE id = ANY(%s::uuid[]) "
                    "AND (created_at, id) < (%s, %s)",
                    ([f.src_id for f in head], src["created_at"], src["id"]))
        keep = {r[0] for r in cur.fetchall()}
    return [f for f in head if f.src_id in keep]


def build_pool(conn, space: spaces.Space, src: dict, *, gate: bool,
               idx: search.VectorIndex, lex) -> list[search.Candidate]:
    """context's pool for `src`, restricted to older rows that are not declared reply
    neighbours. `conn` inside server_internal; the indexes already refreshed."""
    text = embedjob.document_text(src["body"])
    vec = idx.vector_of(str(src["id"]))
    if vec is None:                         # evicted by the ceiling, or a resident view (search.VectorIndex.view): read it
        with conn.cursor() as cur:
            cur.execute("SELECT vec FROM mesh_embeddings WHERE src_id = %s AND space = %s "
                        "AND chunk_no = 0",
                        (src["id"], space.name))
            row = cur.fetchone()
        if row is None:
            return []
        vec = spaces.decode(row[0], space.dims)
    # On a view, the embed arm and the pool are rescored at full width (search._embed_arm, search._exact_pool), so
    # the cosine stored on an edge is the full-width one. `conn` is already inside server_internal.
    read = lambda ids: search.stored_vectors(conn, idx, ids)  # noqa: E731
    embed = search._embed_arm(idx, vec, source="stored", **({"read": read} if idx.view else {}))
    arms = [search._lexical_arm(lex, text).ranking, embed.ranking]
    fused = _older(conn, fusion.rrf(arms), src)
    exclude = {str(src["id"]), *src.get("parent_ids", []), *src.get("child_ids", [])}
    pool, _examined, _guaranteed = search._pool(conn, fused, gate=gate,
                                                exclude_ids=exclude,
                                                exclude_thread=src.get("thread_id"))
    return search._exact_pool(pool, embed, read) if idx.view else pool


def refused_input(attempt: dict) -> bool:
    """True when a failed rerank attempt is the reranker ANSWERING that it refuses this
    input (an HTTP status outside OUTAGE_HTTP, or an answer of the wrong shape); False
    for an outage (unreachable, a transport error or timeout, a spent budget, 408, 429,
    502, 503, 504). Anything this does not recognise is an outage: stopping is safe,
    counting a pair as refused is not."""
    if attempt.get("verdict") != "failed":
        return False
    reason = str(attempt.get("reason") or "")
    status = re.fullmatch(r"HTTP (\d{3})", reason)
    if status:
        return int(status.group(1)) not in OUTAGE_HTTP
    return reason.startswith(("rerank ", "response was not"))


def _rerank_pinned(query: str, docs: list[str], pinned: str, budget_s: float) -> list[float]:
    got = providers.rerank(query, docs, provider_ids={pinned}, budget_s=max(0.1, budget_s))
    if not got.ran:
        last = got.attempts[-1] if got.attempts else {}
        detail = f"{pinned}: {last.get('verdict')} {last.get('reason', '')}".strip()
        if refused_input(last):
            raise _Answered(detail)
        raise _Refused("rerank_unavailable", detail)
    if got.provider_id != pinned:
        raise _Refused("rerank_provider_changed",
                       f"answered by {got.provider_id!r}, admitted {pinned!r}")
    return got.scores


def _rerank_chunked(query: str, docs: list[str], pinned: str, left) -> list[float]:
    """`_rerank_pinned` over `docs` in requests of at most RERANK_CHUNK, the scores joined in document
    order. providers.rerank refuses any answer that does not score every document of its request exactly
    once, so a chunk cannot shift the scores of the next. A refusal or an outage in any chunk raises as
    the single call did."""
    out: list[float] = []
    for i in range(0, len(docs), RERANK_CHUNK):
        out.extend(_rerank_pinned(query, docs[i:i + RERANK_CHUNK], pinned, left()))
    return out


def _retried(query: str, doc: str, pinned: str, left) -> float | None:
    """One document, asked once more: its score, or None when refused again. An outage
    still raises."""
    try:
        return float(_rerank_pinned(query, [doc], pinned, left())[0])
    except _Answered:
        return None


def score_source(text: str, pool: list[search.Candidate], *, pinned: str,
                 reverse_depth: int, budget_s: float | None = None,
                 confirm=None) -> tuple[list[dict], int]:
    """([{fwd, rev|None, fused_rank, candidate}] in pool order, pairs refused). Forward:
    `text` against the whole pool, in requests of at most RERANK_CHUNK. Reverse: each of the top `reverse_depth` by
    forward score against `text` alone. The calls share `budget_s` (SOURCE_BUDGET_S).

    An OUTAGE raises _Refused. With `confirm` (the job's canary re-check: None when the
    pinned reranker still passes, else why not), a REFUSAL is retried (the forward call
    one document at a time, a reverse call once) and a pair refused again is left out
    and counted; if any pair was, `confirm()` must then pass or the refusals were an
    outage. Without `confirm` (the calibration harness) a refusal raises like an outage."""
    end = time.monotonic() + (SOURCE_BUDGET_S if budget_s is None else budget_s)

    def left() -> float:
        return end - time.monotonic()

    docs = [c.body for c in pool]
    try:
        fwd: list = _rerank_chunked(text, docs, pinned, left)
    except _Answered:
        if confirm is None:
            raise
        fwd = [_retried(text, d, pinned, left) for d in docs]
    refused = sum(1 for s in fwd if s is None)
    cands = [{"fwd": float(s), "rev": None, "fused_rank": j + 1, "candidate": c}
             for j, (c, s) in enumerate(zip(pool, fwd)) if s is not None]
    for j in relatedcal.reverse_targets(cands, reverse_depth):
        body = cands[j]["candidate"].body
        try:
            cands[j]["rev"] = float(_rerank_pinned(body, [text], pinned, left())[0])
        except _Answered:
            if confirm is None:
                raise
            cands[j]["rev"] = _retried(body, text, pinned, left)
            refused += cands[j]["rev"] is None
    if refused and confirm is not None:
        why = confirm()
        if why is not None:
            raise _Refused("rerank_unavailable", f"{refused} pair(s) refused, then the "
                                                 f"reranker failed its canaries: {why}")
    return cands, refused


# ------------------------------------------------------------------------- one tick

_INSERT_EDGE = (
    "INSERT INTO mesh_related_edges (src_id, src_key, src_version, dst_id, dst_key, "
    "  dst_version, space, embed_provider_id, rerank_provider_id, rerank_model, "
    "  rerank_reference, threshold_version, score_fwd, score_rev, pair_score, threshold, "
    "  rank, fused_rank, cosine, lexical_score, recorded_at, dst_recorded_at) "
    "SELECT %(src_id)s, %(src_key)s, %(src_version)s, d.id, d.key, d.version, %(space)s, "
    "  %(embed_provider_id)s, %(rerank_provider_id)s, %(rerank_model)s, "
    "  %(rerank_reference)s, %(threshold_version)s, %(fwd)s, %(rev)s, %(pair)s, "
    "  %(threshold)s, %(rank)s, %(fused_rank)s, %(cosine)s, %(lexical)s, "
    "  %(recorded_at)s, d.created_at "
    "FROM shared_memory d WHERE d.id = %(dst_id)s AND d.is_head AND d.namespace = %(ns)s "
    "  AND (%(open)s OR d.embed_policy = 'allowed')"
)

_UPSERT_RUN = (
    "INSERT INTO mesh_related_runs (src_id, space, threshold_version, outcome, pool_size, "
    "  reverse_scored, edges, rerank_provider_id, pairs_refused) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (src_id, space, threshold_version) DO UPDATE SET "
    "  outcome = EXCLUDED.outcome, pool_size = EXCLUDED.pool_size, "
    "  reverse_scored = EXCLUDED.reverse_scored, edges = EXCLUDED.edges, "
    "  rerank_provider_id = EXCLUDED.rerank_provider_id, "
    "  pairs_refused = EXCLUDED.pairs_refused, processed_at = now()"
)


def catch_up(conn, *, space: spaces.Space | None = None, max_batch: int = DEFAULT_BATCH,
             relate_per_tick: int = RELATE_PER_TICK, deadline_s: float | None = 45.0,
             namespace: str = MESH_CHAT_NAMESPACE) -> Report:
    """One bounded tick. `conn`: autocommit, no open transaction (rls.py)."""
    space = space or store.configured_space()
    gate = store.forbidden_rows_open()
    deadline = None if deadline_s is None else _clock() + deadline_s

    # ---- 0. the gate's purge, then the calibration -----------------------------------
    purged = 0
    if not gate:
        # CLOSED: edges touching a forbidden row go, from EITHER end, in EVERY space, and
        # every tick (level-triggered): whatever the calibration says and whichever space
        # is configured. Reads already hide them (related_reads.py); this makes the
        # tables agree. Driven from the edge table: it holds at most k rows a source.
        with rls.projection_writer(conn):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mesh_related_edges e WHERE EXISTS (SELECT 1 "
                            "FROM shared_memory s WHERE s.id IN (e.src_id, e.dst_id) "
                            "AND s.embed_policy = 'forbidden')")
                purged = max(0, cur.rowcount or 0)
                cur.execute("DELETE FROM mesh_related_runs r WHERE EXISTS (SELECT 1 "
                            "FROM shared_memory s WHERE s.id = r.src_id "
                            "AND s.embed_policy = 'forbidden')")
    try:
        cal = relatedcal.load()
    except relatedcal.CalibrationInvalid as exc:
        detail = f"invalid calibration: {exc}"
        _record_status(conn, space, "not_calibrated", detail)
        return Report(space=space.name, status="not_calibrated", detail=detail,
                      gate_open=gate, purged=purged)
    if cal is None or cal.space != space.name:
        detail = ("no enabled calibration (" + str(relatedcal.configured_path()) + ")"
                  if cal is None else
                  f"calibrated for space {cal.space!r}, configured {space.name!r}")
        _record_status(conn, space, "not_calibrated", detail)
        return Report(space=space.name, status="not_calibrated", detail=detail,
                      gate_open=gate, purged=purged)

    # ---- 1. restart rules, the upper bound, the rows --------------------------------
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            _ensure_state(cur, space)
            state = _read_state(cur, space)
            gate_changed = bool(state["forbidden_rows_open"]) != gate
            threshold_changed = state["threshold_version"] != cal.threshold_version
            if threshold_changed:
                # Two calibrations never share a space's edges.
                cur.execute("DELETE FROM mesh_related_edges WHERE space = %s "
                            "AND threshold_version <> %s", (space.name, cal.threshold_version))
                cur.execute("DELETE FROM mesh_related_runs WHERE space = %s "
                            "AND threshold_version <> %s", (space.name, cal.threshold_version))
            if gate_changed or threshold_changed:
                # OPENING or CLOSING, and a new calibration: the walk restarts, because
                # rows decided under the old rule were walked past.
                cur.execute(f"UPDATE {STATE_TABLE} SET forbidden_rows_open = %s, "
                            "threshold_version = %s, watermark_at = NULL, "
                            "watermark_id = NULL, rows_seen = 0, sources_related = 0, "
                            "sources_none_above = 0, sources_no_candidates = 0, "
                            "sources_targets_gone = 0, sources_rerank_refused = 0, "
                            "pairs_refused = 0, "
                            "rows_skipped_forbidden = 0, rows_skipped_no_vector = 0, "
                            "edges_written = 0, updated_at = now() WHERE space = %s",
                            (gate, cal.threshold_version, space.name))
                state = _read_state(cur, space)
            after_at, after_id = state["watermark_at"], state["watermark_id"]
            base = dict(space=space.name, gate_open=gate, gate_changed=gate_changed,
                        purged=purged, threshold_changed=threshold_changed,
                        threshold_version=cal.threshold_version, watermark_at=after_at,
                        watermark_id=None if after_id is None else str(after_id))

            emb = store.read_state(cur, space)
            if (not emb or emb["watermark_at"] is None
                    or bool(emb["forbidden_rows_open"]) != gate):
                detail = ("the embed walk has not decided any row in this space"
                          if not emb or emb["watermark_at"] is None else
                          "the embed walk has not restarted under the current gate yet")
                _set_status(cur, space, "embed_not_run", detail)
                return Report(status="embed_not_run", detail=detail, **base)
            edges_wm = _edges_watermark(cur)
            if edges_wm is None:
                detail = ("mesh_edges has no watermark: run the mesh_edges_catch_up job "
                          "(or `python -m apps.mesh catch-up`) first")
                _set_status(cur, space, "projection_not_run", detail)
                return Report(status="projection_not_run", detail=detail, **base)
            up_at, up_id = min((emb["watermark_at"], emb["watermark_id"]), edges_wm)
            cur.execute(_SELECT_ROWS, {"space": space.name, "ns": namespace,
                                       "after_at": after_at, "after_id": after_id,
                                       "up_at": up_at, "up_id": up_id,
                                       "limit": int(max_batch)})
            rows = cur.fetchall()

    # ---- 2. decide ------------------------------------------------------------------
    plan: list[tuple[str, dict]] = []
    for r in rows:
        if r["embed_policy"] == "forbidden" and not gate:
            plan.append(("skipped_forbidden", r))
        elif r["vec_provider"] is None or embedjob.document_text(r["body"]) is None:
            plan.append(("skipped_no_vector", r))
        else:
            plan.append(("relate", r))
    to_relate = [i for i, (v, _r) in enumerate(plan) if v == "relate"]
    bounded = to_relate[:max(0, int(relate_per_tick))]
    stopped = "relate_bound" if len(to_relate) > len(bounded) else None

    pinned, attempts, model = None, [], None
    results: dict[int, tuple] = {}
    failure: _Refused | None = None
    # Step 4 assigns the index only when there is something to relate; the report reads it either way (pxx P2 1).
    idx = None
    if bounded:
        # ---- 3. admit the reranker -------------------------------------------------
        pinned, attempts = admit_reranker(cal.canary_sets, tolerance=cal.tolerance)
        if pinned is None:
            status = ("rerank_not_admitted"
                      if any(a.get("verdict") == "not_admitted" for a in attempts)
                      else "rerank_unavailable")
            detail = "; ".join(f"{a.get('id')}: {a.get('verdict')} {a.get('reason', '')}"
                               for a in attempts) or "no rerank provider declared"
            _record_status(conn, space, status, detail)
            return Report(status=status, detail=detail, rows_read=len(rows),
                          attempts=tuple(attempts), **base)
        model = next((a.get("model") for a in attempts if a.get("id") == pinned), None) \
            or cal.rerank_model or "rerank"

        # ---- 4. pools ----------------------------------------------------------------
        idx, lex = search.index_for(space), search.lexical_index_for(space)
        ceiling = search._ceiling()
        pools: dict[int, list] = {}
        with rls.server_internal(conn) as c:
            idx.refresh(c, ceiling=ceiling, wait=True)     # a background tick: queue behind a running refresh
            lex.refresh(c, ceiling=ceiling, wait=True)
            for i in bounded:
                pools[i] = build_pool(c, space, plan[i][1], gate=gate, idx=idx, lex=lex)

        def confirm() -> str | None:
            """None when the pinned reranker still passes its canaries, else why not."""
            worst, failed = _canaries(pinned, cal.canary_sets, budget_s=CONFIRM_BUDGET_S)
            if failed is not None:
                return failed
            if worst > cal.tolerance:
                return f"canary logits off by {worst:.4f} > {cal.tolerance}"
            return None

        # ---- 5. rerank; a failure stops the tick at its source (FAILURES) -----------
        for n, i in enumerate(bounded):
            if n and deadline is not None and _clock() >= deadline:
                stopped = "deadline"
                break
            pool = pools[i]
            if not pool:
                results[i] = ("no_candidates", [], 0, 0, 0)
                continue
            text = embedjob.document_text(plan[i][1]["body"])
            try:
                cands, refused = score_source(text, pool, pinned=pinned,
                                              reverse_depth=cal.reverse_depth,
                                              budget_s=SOURCE_BUDGET_S, confirm=confirm)
            except _Refused as exc:
                failure = exc
                break
            chosen = relatedcal.select_edges(cands, threshold=cal.threshold, k=cal.k)
            reversed_n = sum(1 for c in cands if c["rev"] is not None)
            outcome = ("edges" if chosen else
                       "rerank_refused" if not reversed_n else "none_above_threshold")
            results[i] = (outcome, chosen, len(pool), reversed_n, refused)

    # The final prefix: every skipped row, and each related row up to the first one that
    # was not completed.
    done: list[int] = []
    for i, (verdict, _r) in enumerate(plan):
        if verdict == "relate" and i not in results:
            break
        done.append(i)

    # ---- 6. write ---------------------------------------------------------------------
    counts = {"edges": 0, "none_above_threshold": 0, "no_candidates": 0,
              "targets_gone": 0, "rerank_refused": 0}
    written = refused_total = 0
    try:
        with rls.projection_writer(conn):
            with conn.cursor(row_factory=dict_row) as cur:
                now_state = _read_state(cur, space, for_update=True)
                if ((now_state["watermark_at"], now_state["watermark_id"]) != (after_at, after_id)
                        or bool(now_state["forbidden_rows_open"]) != gate
                        or now_state["threshold_version"] != cal.threshold_version):
                    raise ConcurrentWalk(f"space {space.name!r}: the related walk moved "
                                         "during this tick; another walker is running")
                for i in done:
                    verdict, r = plan[i]
                    if verdict != "relate":
                        continue
                    outcome, chosen, pool_size, reversed_n, refused = results[i]
                    cur.execute("DELETE FROM mesh_related_edges WHERE space = %s "
                                "AND src_id = %s", (space.name, r["id"]))
                    inserted = 0
                    for c in chosen:
                        cand = c["candidate"]
                        cur.execute(_INSERT_EDGE, {
                            "src_id": r["id"], "src_key": r["key"],
                            "src_version": r["version"], "space": space.name,
                            "embed_provider_id": r["vec_provider"],
                            "rerank_provider_id": pinned, "rerank_model": model,
                            "rerank_reference": cal.rerank_reference,
                            "threshold_version": cal.threshold_version,
                            "fwd": c["fwd"], "rev": c["rev"], "pair": c["pair"],
                            "threshold": cal.threshold, "rank": c["rank"],
                            "fused_rank": c["fused_rank"], "cosine": cand.cosine,
                            "lexical": cand.lexical_score, "recorded_at": r["created_at"],
                            "dst_id": cand.src_id, "ns": namespace, "open": gate})
                        inserted += max(0, cur.rowcount or 0)
                    if inserted:
                        outcome = "edges"
                    elif outcome == "edges":
                        # Scored above the threshold; every chosen target was superseded,
                        # gated or deleted before this write. Not "none above".
                        outcome = "targets_gone"
                    counts[outcome] += 1
                    written += inserted
                    refused_total += refused
                    cur.execute(_UPSERT_RUN, (r["id"], space.name, cal.threshold_version,
                                              outcome, pool_size, reversed_n, inserted,
                                              pinned, refused))
                skipped_f = sum(1 for i in done if plan[i][0] == "skipped_forbidden")
                skipped_v = sum(1 for i in done if plan[i][0] == "skipped_no_vector")
                status = failure.status if failure else (stopped or "ok")
                if done:
                    last = plan[done[-1]][1]
                    wm_at, wm_id = last["created_at"], last["id"]
                else:
                    wm_at, wm_id = after_at, after_id
                cur.execute(
                    f"UPDATE {STATE_TABLE} SET watermark_at = %s, watermark_id = %s, "
                    "rows_seen = rows_seen + %s, sources_related = sources_related + %s, "
                    "sources_none_above = sources_none_above + %s, "
                    "sources_no_candidates = sources_no_candidates + %s, "
                    "sources_targets_gone = sources_targets_gone + %s, "
                    "sources_rerank_refused = sources_rerank_refused + %s, "
                    "pairs_refused = pairs_refused + %s, "
                    "rows_skipped_forbidden = rows_skipped_forbidden + %s, "
                    "rows_skipped_no_vector = rows_skipped_no_vector + %s, "
                    "edges_written = edges_written + %s, last_status = %s, "
                    "last_status_at = now(), last_detail = %s, updated_at = now() "
                    "WHERE space = %s",
                    (wm_at, wm_id, len(done), counts["edges"],
                     counts["none_above_threshold"], counts["no_candidates"],
                     counts["targets_gone"], counts["rerank_refused"], refused_total,
                     skipped_f, skipped_v, written, status,
                     failure.detail[:2000] if failure else None, space.name))
    except (ConcurrentWalk, psycopg.errors.ForeignKeyViolation) as exc:
        _record_status(conn, space, "concurrent_walk", str(exc))
        if isinstance(exc, ConcurrentWalk):
            raise
        raise ConcurrentWalk(f"space {space.name!r}: a message was deleted under this "
                             f"tick's write ({exc}); the next tick recomputes") from exc

    return Report(
        status=status, detail=failure.detail if failure else None, rows_read=len(rows),
        rows_processed=len(done),
        sources_related=counts["edges"], sources_none_above=counts["none_above_threshold"],
        sources_no_candidates=counts["no_candidates"],
        sources_targets_gone=counts["targets_gone"],
        sources_rerank_refused=counts["rerank_refused"], pairs_refused=refused_total,
        skipped_forbidden=skipped_f,
        skipped_no_vector=skipped_v, edges_written=written, rerank_provider_id=pinned,
        attempts=tuple(attempts), stopped=stopped,
        more=(len(rows) >= max_batch or stopped is not None or failure is not None),
        resident_view=({"resident_dims": idx.resident, "rescore_depth": search.rescore_depth()}
                       if idx is not None and idx.view else None),
        **{**base, "watermark_at": wm_at,
           "watermark_id": None if wm_id is None else str(wm_id)})


def run_job(conn, job) -> TickResult:
    """The registered job body. Returns rows PROCESSED and the report's own `more`, which
    is true when the tick stopped on RELATE_PER_TICK or its deadline with fewer than
    `job.max_batch` rows read (STATUS D18). A refused tick raises AFTER its status is
    committed (E1)."""
    report = catch_up(conn, max_batch=job.max_batch, deadline_s=job.timeout_secs)
    invalid = report.status == "not_calibrated" and (report.detail or "").startswith(
        "invalid calibration")
    if report.status in ERROR_STATUSES or invalid:
        raise RelatedJobFailed(f"{report.space}: {report.status}: {report.detail}")
    return TickResult(rows=report.rows_processed, more_work_pending=report.more)


__all__ = ["CONFIRM_BUDGET_S", "ConcurrentWalk", "DEFAULT_BATCH", "ERROR_STATUSES",
           "JOB_NAME", "OUTAGE_HTTP", "RELATE_PER_TICK", "RelatedJobFailed", "Report",
           "SOURCE_BUDGET_S", "admit_reranker", "build_pool", "catch_up", "refused_input",
           "run_job", "score_source"]
