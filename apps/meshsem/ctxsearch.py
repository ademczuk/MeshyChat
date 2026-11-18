"""Search over the transcript-context index (v1.1 step 7, docs/design/context-v1.1-transcript-ingest.md):
GET /v1/mesh/search?ns=<the context namespace>&q=...[&seat=&src=&project=&since=&until=&collapse=&k=&rerank=0].

Reads apps/meshsem/ctxindex (route B) over what apps/meshsem/ctxjob writes. The route's permission check is the one every
search takes (views._route: memory.read's check on the namespace), so a seat that may not read the namespace never gets
here.

WHAT A HIT CARRIES (the provenance pick): seat, src, gen, batch, first_ord..last_ord, the chunk's char span inside its
turn text, the time range, and the chunk TEXT itself. The text is rebuilt from the batch's stored items by the same
deterministic rule the walker cut it with (ctxjob.turn_chunks), so the response quotes exactly what was embedded and a
reader can open the exact turns by their ordinals. A hit whose items are no longer readable (a deleted row) says so
instead of quoting stale text.

FILTERS resolve in SQL before ranking (ctxindex.Filters). Note that `mesh_ctx_chunks` (sql_meshychat/047..055) does not
store `item_kinds` as a SQL column (kinds are reconstructed in `_texts` from `shared_memory` item payloads for the
final candidate pool, and one turn chunk often spans multiple kinds such as `user_text` + `assistant_text`), so no
post-pool `kinds` filter is offered; instead, when reranking runs, `collapse` (`item` or `session`) is applied AFTER
cross-encoder reranking over a diverse per-group candidate pool so `assistant_text` / `user_text` answers naturally
outrank `[tool_exchange]` JSON blobs within the same session or item. `collapse`: chunk (default, every hit), item (one
hit per (seat, src, gen, first_ord)), session (one hit per (seat, src)). Two arms are fused by reciprocal rank, as the
mail search is: the vector arm (ctxindex) and the LEXICAL arm (mesh_ctx_lexical, sql_meshychat/048: the chunks holding
a query term, inside the namespace and filters, scored by a saturated TF-IDF over those candidates), with each arm's
top `ARM_GUARANTEE` hits reserved into the pool. Either arm may run alone (including when the lexical arm hits its
savepoint statement timeout), and `arms` says which did.

`timings` (design step 7, read-only instrumentation): milliseconds for embed, store, body join, rerank.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import logging
import math
import threading
import time

import numpy as np
import psycopg
from psycopg.rows import dict_row

from apps.context import validation as ctx_validation
from apps.core import dbconn, env
from apps.peers import errors

from . import ctxindex, ctxjob, fusion, lexical, providers, spaces, store

MAX_K = 50
#: Candidates fetched before collapse: enough for k groups in the common case; when collapse leaves fewer than k groups
#: and the pool was full, the response says so (pool.truncated), separately from chunk coverage (pxx P2-2).
POOL = 100
POOL_PER_GROUP = 20
#: Pool slots reserved per arm that ran (matching apps/meshsem/search.py divergence 026) so a top single-arm paraphrase
#: or rare identifier is never crowded out of the candidate pool by dual-arm keyword noise before reranking.
ARM_GUARANTEE = 10
#: Best fused chunks per collapse group sent to the cross-encoder before final group collapse (rerank=1, collapse!=chunk).
RERANK_PER_GROUP = 3
#: Per-STATEMENT timeout (ms) inside a SAVEPOINT around the lexical arm; a QueryCanceled degrades to vector-only.
#: The prefiltered arm runs bounded chunk-window reads plus ceil(candidates / LEX_FETCH_BATCH) PK-probe statements
#: (up to ~26 statements at LEX_NARROW_MAX = 50,000), so this caps individual statements rather than whole-arm wall time:
#: the arm as a whole is bounded by LEX_BUDGET_S, checked between its statements.
LEX_TIMEOUT_MS = 5000

# ------------------------------------------------------------------ admission and time budgets (7 Nov 2026)
# Production on HQD stopped answering from 08:16 to 08:27Z on 7 Nov 2026: sweeps over every seat's transcripts left 14
# searches running at once, each holding up to EXACT_MAX chunk rows and vectors in daphne, on a box at 95% RAM. They sat
# idle in transaction after their vector fetch for up to 22 minutes while Python decoded, so no statement_timeout fired.
# The gate bounds how many run at once; the budgets bound how long each arm runs, checked between statements.

#: Transcript searches admitted at once in this process, from the embed to the end of the store phase.
#: MESHY_CTX_SEARCH_CONCURRENCY overrides it; a value that is not a whole number of at least 1 keeps this.
STORE_CONCURRENCY = 2
#: Seconds a search waits for a slot before it is refused: 503 E_CTX_SEARCH_BUSY with Retry-After.
STORE_QUEUE_S = 3.0
#: The Retry-After of that refusal, whole seconds.
BUSY_RETRY_AFTER_S = 5
#: Seconds the vector arm may run. Past it the arm stops at its next vector batch or cold shard (ctxindex.Deadline), and
#: the lexical arm answers alone: arms.vector.timed_out, and the response is truncated.
VECTOR_BUDGET_S = 15.0
#: Seconds the lexical arm may run, checked before each of its probe batches. Past it the vector arm answers alone.
LEX_BUDGET_S = 10.0

log = logging.getLogger("meshychat.meshsem.ctxsearch")


E_BUSY = "E_CTX_SEARCH_BUSY"


def busy(slots: int, retry_after: int) -> errors.ApiError:
    """Every slot is taken: 503 E_CTX_SEARCH_BUSY, carrying `retry_after`, which the search route sends as Retry-After
    (apps/meshsem/views.py). A plain ApiError, not a subclass: apps/core/jsonerrors.py recognises the type by NAME."""
    err = errors.ApiError(503, E_BUSY,
                          f"{slots} transcript search(es) already running on this server; retry in {retry_after} s")
    err.retry_after = int(retry_after)
    return err


class _Gate:
    """A bounded number of transcript searches at once. A slot is always given back, whatever the search raised."""

    def __init__(self, slots: int):
        self.slots = slots
        self._sem = threading.BoundedSemaphore(slots)

    @contextlib.contextmanager
    def admit(self, wait_s: float):
        if not self._sem.acquire(timeout=max(0.0, float(wait_s))):
            raise busy(self.slots, BUSY_RETRY_AFTER_S)
        try:
            yield
        finally:
            self._sem.release()


def configured_concurrency() -> int:
    """MESHY_CTX_SEARCH_CONCURRENCY as a whole number of at least 1, else STORE_CONCURRENCY (logged once, here)."""
    raw = env.setting("MESHY_CTX_SEARCH_CONCURRENCY", "MESHY_CTX_SEARCH_CONCURRENCY", default=None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return STORE_CONCURRENCY
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        log.warning("ctxsearch: MESHY_CTX_SEARCH_CONCURRENCY=%r is not a whole number of at least 1; it stays %d",
                    raw, STORE_CONCURRENCY)
        return STORE_CONCURRENCY
    return n


_gate = _Gate(configured_concurrency())
#: Card-only parameters: refused on this namespace by name, not ignored (pxx P2-3).
REFUSED_PARAMS = ("type", "owner", "status", "thread")
COLLAPSE = ("chunk", "item", "session")
FILTER_PARAMS = ("seat", "src", "since", "until")
#: Not offered yet: no writer sends a manifest `project`, so the filter would match nothing (Codex on #390). The column
#: and its index stay; the parameter is refused by name until the optional manifest field lands.
NOT_YET_PARAMS = ("project",)


def is_context_namespace(ns: str) -> bool:
    return ns == ctx_validation.namespace()


def _instant(raw: str | None, name: str) -> _dt.datetime | None:
    if raw is None or raw == "":
        return None
    try:
        when = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise errors.ApiError(422, errors.E_VALIDATION, f"{name} must be an ISO 8601 time") from None
    if when.tzinfo is None:
        raise errors.ApiError(422, errors.E_VALIDATION, f"{name} must carry a timezone (Z or an offset)")
    return when.astimezone(_dt.timezone.utc)


def filters_from(get) -> ctxindex.Filters:
    """The request's filters, validated. `get` is request.GET (or a dict for tests)."""
    named = [p for p in REFUSED_PARAMS if p in get]
    if named:
        raise errors.ApiError(422, errors.E_VALIDATION, f"{', '.join(named)} filter the card namespace only; "
                              f"this namespace takes {', '.join(FILTER_PARAMS)}")
    if any(p in get for p in NOT_YET_PARAMS):
        raise errors.ApiError(422, errors.E_VALIDATION, "project is not offered yet: no writer sends a manifest "
                              "project, so the filter would match nothing")
    def one(name):
        v = get.get(name)
        return None if v is None or str(v).strip() == "" else str(v).strip()
    f = ctxindex.Filters(seat=one("seat"), src=one("src"),
                         since=_instant(get.get("since"), "since"), until=_instant(get.get("until"), "until"))
    if f.since is not None and f.until is not None and f.since >= f.until:
        raise errors.ApiError(422, errors.E_VALIDATION, "since must be before until")
    return f


def collapse_of(get) -> str:
    c = (get.get("collapse") or "chunk").strip()
    if c not in COLLAPSE:
        raise errors.ApiError(422, errors.E_VALIDATION, f"collapse is one of {', '.join(COLLAPSE)}")
    return c


def _group_key(h: ctxindex.Hit, how: str) -> tuple:
    # An item is (seat, src, gen, ordinal): two seats can share a src name (a reviewer on #390).
    return (h.seat, h.src, h.gen, h.first_ord) if how == "item" else (h.seat, h.src)


def _collapse(hits: list[ctxindex.Hit], how: str) -> list[ctxindex.Hit]:
    if how == "chunk":
        return hits
    seen, out = set(), []
    for h in hits:                                   # hits arrive best first, so the first of a group is its best
        key = _group_key(h, how)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def _fused_pool(
    fused: list[fusion.Fused], by_id: dict[int, ctxindex.Hit], limit: int
) -> tuple[list[ctxindex.Hit], dict[str, int], set[int]]:
    """At most `limit` candidates in fused order, reserving up to ARM_GUARANTEE top candidates per arm (divergence 026)."""
    limit = max(0, int(limit))
    top = list(range(min(len(fused), limit)))
    per_arm_cap = min(max(0, int(ARM_GUARANTEE)), limit // 2)
    ranked_by_arm: dict[str, list[tuple[int, int]]] = {arm: [] for arm in fusion.ARMS}
    for i, item in enumerate(fused):
        for arm, rnk in item.ranks.items():
            if rnk is not None:
                ranked_by_arm[arm].append((rnk, i))
    reserved: dict[str, list[int]] = {
        arm: [i for _rnk, i in sorted(pairs)[:per_arm_cap]]
        for arm, pairs in ranked_by_arm.items()
    }
    held = set(top)
    members: set[int] = set().union(*reserved.values())
    for i in top:
        if len(members) >= limit:
            break
        members.add(i)
    guaranteed = {arm: sum(1 for i in got if i not in held) for arm, got in reserved.items()}
    reserved_ids = {int(fused[i].src_id) for got in reserved.values() for i in got}
    out = [dataclasses.replace(by_id[int(fused[i].src_id)], score=fused[i].score) for i in sorted(members)]
    return out, guaranteed, reserved_ids


def _rerank_candidates(
    ranked: list[ctxindex.Hit], how: str, *, k: int, limit: int, per_group: int, reserved_ids: set[int]
) -> list[ctxindex.Hit]:
    """Pre-rerank slice in fused order when `how != 'chunk'`: guarantees the top chunk of the first `k` groups, keeps
    arm-guaranteed hits, and adds up to `per_group` best fused chunks per group in round-robin rank order up to `limit`."""
    if how == "chunk" or not ranked or limit <= 0:
        return ranked[: max(0, limit)]
    per_group = max(1, int(per_group))
    by_pass: list[list[int]] = [[] for _ in range(per_group)]
    group_counts: dict[tuple, int] = {}
    for idx, h in enumerate(ranked):
        g = _group_key(h, how)
        n = group_counts.get(g, 0)
        if n < per_group:
            by_pass[n].append(idx)
        group_counts[g] = n + 1

    chosen: set[int] = set()
    for idx in by_pass[0][:k]:
        if len(chosen) >= limit:
            break
        chosen.add(idx)
    for idx, h in enumerate(ranked):
        if len(chosen) >= limit:
            break
        if h.chunk_id in reserved_ids:
            chosen.add(idx)
    for bucket in by_pass:
        for idx in bucket:
            if len(chosen) >= limit:
                break
            chosen.add(idx)
        if len(chosen) >= limit:
            break
    return [ranked[idx] for idx in sorted(chosen)]


ITEM_FETCH_BATCH = 2048


def _rebuild_manifest_group(
    found: dict[str, dict],
    group_manifests: list[tuple[str, list[str]]],
    by_manifest: dict[str, list[ctxindex.Hit]],
    out: dict[int, str | None],
    kinds: dict[int, list[str] | None],
) -> None:
    for src_id, keys in group_manifests:
        items: list[dict] = []
        chunks = None
        if all(k in found for k in keys):
            try:
                items = [ctxjob._validated_item(found[k]) for k in keys]
                chunks = ctxjob.turn_chunks(items)
            except ctxjob.Unreadable:
                chunks = None
        by_ord = {} if chunks is None else {int(v["ordinal"]): str(v["item"]) if ctxjob.in_turn_text(v) else None
                                            for v in items}
        for h in by_manifest[src_id]:
            match = None
            if chunks is not None:
                match = next((c for c in chunks if c.first_ord == h.first_ord and c.last_ord == h.last_ord
                              and c.char_start == h.char_start and c.char_end == h.char_end), None)
            out[h.chunk_id] = None if match is None else match.text
            kinds[h.chunk_id] = (None if match is None else
                                 [by_ord[o] for o in range(h.first_ord, h.last_ord + 1) if o in by_ord])


def _fetch_and_rebuild_group(
    cur,
    ns: str,
    group_manifests: list[tuple[str, list[str]]],
    group_keys: set[str],
    by_manifest: dict[str, list[ctxindex.Hit]],
    out: dict[int, str | None],
    kinds: dict[int, list[str] | None],
) -> None:
    found: dict[str, dict] = {}
    sorted_keys = sorted(group_keys)
    for start in range(0, len(sorted_keys), ITEM_FETCH_BATCH):
        batch = sorted_keys[start:start + ITEM_FETCH_BATCH]
        cur.execute("SELECT key, value, embed_policy FROM shared_memory "
                    "WHERE namespace = %s AND key = ANY(%s::text[]) AND is_head", (ns, batch))
        for r in cur.fetchall():
            found[r["key"]] = r
    _rebuild_manifest_group(found, group_manifests, by_manifest, out, kinds)
    found.clear()


def _texts(cur, ns: str, hits: list[ctxindex.Hit]) -> tuple[dict[int, str | None], dict[int, list[str] | None]]:
    """(chunk_id -> the chunk's text, chunk_id -> the item kinds of its first_ord..last_ord in ordinal order), both rebuilt
    from the batch's items; None when they cannot be read any more. The kinds (user_text, assistant_text, thinking,
    tool_exchange) let a reader tell the seat's own words from a slice of tool output without a read per hit: a chunk's
    text is cut from inside a turn, so it rarely starts at an item's [kind] label (resume-brief, #480 P3). An item the
    text never holds (omitted, or blank; ctxjob.in_turn_text) is None, so the list stays one entry per item key while a
    reader can walk the labels in step with it.

    Walks manifests in bounded groups of up to ITEM_FETCH_BATCH item keys so one query fetches ~8 full manifests while
    live parsed item JSON stays capped at one group (`found` is dropped after each group's chunks are rebuilt).
    """
    if not hits:
        return {}, {}
    out: dict[int, str | None] = {}
    kinds: dict[int, list[str] | None] = {}
    by_manifest: dict[str, list[ctxindex.Hit]] = {}
    for h in hits:
        by_manifest.setdefault(h.src_id, []).append(h)
    # Bound to the requested namespace (pxx P1): a manifest of another namespace is never read here.
    cur.execute("SELECT id::text AS src_id, value FROM shared_memory WHERE namespace = %s AND id = ANY(%s::uuid[])",
                (ns, sorted(by_manifest)))
    manifests = {r["src_id"]: r["value"] for r in cur.fetchall()}

    group_manifests: list[tuple[str, list[str]]] = []
    group_keys: set[str] = set()
    for src_id in sorted(by_manifest):
        value = manifests.get(src_id)
        if value is None:
            for h in by_manifest[src_id]:
                out[h.chunk_id] = None
                kinds[h.chunk_id] = None
            continue
        try:
            keys = ctxjob._item_keys(value)
        except ctxjob.Unreadable:
            for h in by_manifest[src_id]:
                out[h.chunk_id] = None
                kinds[h.chunk_id] = None
            continue
        key_set = set(keys)
        if group_manifests and len(group_keys | key_set) > ITEM_FETCH_BATCH:
            _fetch_and_rebuild_group(cur, ns, group_manifests, group_keys, by_manifest, out, kinds)
            group_manifests = []
            group_keys = set()
        group_manifests.append((src_id, keys))
        group_keys.update(key_set)

    if group_manifests:
        _fetch_and_rebuild_group(cur, ns, group_manifests, group_keys, by_manifest, out, kinds)
    return out, kinds


#: Manifests committed longer ago than this and not yet walked make the answer say the index is behind.
WALK_GRACE_S = 120

#: The manifest's gen as an int, or NULL when it is missing or not a small non-negative integer (a manifest can be
#: dead-lettered precisely for a malformed gen; casting it blindly would fail the search).
_SAFE_GEN = ("CASE WHEN jsonb_typeof(sm.value->'gen') = 'number' AND (sm.value->>'gen') ~ '^[0-9]{1,9}$' "
             "THEN (sm.value->>'gen')::int END")


#: Dead-letter rows the unindexed-gen count reads at most per request (pxx's switch-on condition): past it the
#: count is a lower bound, and the answer says so.
DEAD_SCAN_MAX = 1000


def _unindexed_newer(cur, space: spaces.Space, ns: str, f: ctxindex.Filters) -> tuple[int, bool]:
    """(n, capped): how many (seat, src) in the namespace (and under the seat/src filters) have a dead-lettered
    manifest, in THIS space, of the gen retrieval serves for them or a newer one (a reviewer on #390: a missing batch
    of the served gen leaves it incomplete too), or of a gen that cannot be read. Rows still awaiting replay count;
    replayed ones do not. At most DEAD_SCAN_MAX rows are read; `capped` says n is then only "at least"."""
    where = ["d.space = %s", "sm.namespace = %s", "d.state IN ('dead', 'replay')"]
    args: list = [space.name, ns]
    if f.seat is not None:
        where.append("sm.value->>'seat' = %s")
        args.append(f.seat)
    if f.src is not None:
        where.append("sm.value->>'src' = %s")
        args.append(f.src)
    cur.execute("SELECT count(DISTINCT (x.seat, x.src)) AS n, count(*) AS rows FROM ("
                "SELECT sm.value->>'seat' AS seat, sm.value->>'src' AS src FROM ctx_dead_letter d "
                "JOIN shared_memory sm ON sm.id = d.memory_id LEFT JOIN ctx_source_gen s ON s.space = d.space "
                "AND s.namespace = sm.namespace AND s.seat = sm.value->>'seat' AND s.src = sm.value->>'src' "
                f"WHERE {' AND '.join(where)} AND (s.gen IS NULL OR {_SAFE_GEN} IS NULL OR {_SAFE_GEN} >= s.gen) "
                "LIMIT %s) x", [*args, DEAD_SCAN_MAX + 1])
    row = cur.fetchone()
    return int(row["n"]), int(row["rows"]) > DEAD_SCAN_MAX


def _walk_behind(cur, space: spaces.Space, ns: str) -> bool:
    """True when a manifest of the namespace committed over WALK_GRACE_S ago is past the space's watermark (and not
    dead-lettered): initial catch-up, a re-walk after the gate opened or the lexical rows were rebuilt, or a walk that
    has stalled. The answer then says it is incomplete (a reviewer on #390)."""
    cur.execute("SELECT EXISTS (SELECT 1 FROM ctx_commit_order o JOIN shared_memory sm ON sm.id = o.memory_id "
                "WHERE o.seq > COALESCE((SELECT watermark_seq FROM ctx_index_state WHERE space = %s), 0) "
                "AND o.kind = 'write' AND sm.namespace = %s AND o.stamped_at < now() - make_interval(secs => %s) "
                "AND NOT EXISTS (SELECT 1 FROM ctx_dead_letter d WHERE d.space = %s AND d.seq = o.seq "
                "AND d.state IN ('dead', 'replay'))) AS behind", (space.name, ns, WALK_GRACE_S, space.name))
    return bool(cur.fetchone()["behind"])


def _hit_json(h: ctxindex.Hit, text: str | None, score: float, rerank_score: float | None,
              item_kinds: list[str] | None = None) -> dict:
    return {"seat": h.seat, "src": h.src, "gen": h.gen, "batch": h.batch, "first_ord": h.first_ord,
            "last_ord": h.last_ord, "char_start": h.char_start, "char_end": h.char_end,
            "time_first": h.time_first.isoformat(), "time_last": h.time_last.isoformat(),
            "item_keys": [f"ctx/{h.seat}/{h.src}/{h.gen:04d}/{h.batch:06d}/{o:010d}"
                          for o in range(h.first_ord, h.last_ord + 1)],
            "item_kinds": item_kinds,
            "text": text, "text_available": text is not None, "score": round(float(score), 6),
            "rerank_score": None if rerank_score is None else round(float(rerank_score), 6)}


#: A matching set this size or smaller is scored exactly, in SQL (design step 7; pxx point 2).
LEX_EXACT_MAX = 20000
#: Chunks holding a query term the arm reads at most per request, before its pre-order (pxx's switch-on
#: condition): past it an arbitrary subset is pre-ordered, and the arm says it is truncated.
LEX_SCAN_MAX = 200_000
#: Filtered chunk subsets up to this size resolve mesh_ctx_chunks first via partial current indexes and probe
#: mesh_ctx_lexical_pkey in sorted digest batches instead of walking global GIN -> non-partial mesh_ctx_chunks_digest.
LEX_NARROW_MAX = 50_000
LEX_FETCH_BATCH = 2048
#: Width of each backward mesh_ctx_chunks_pkey window when scanning unfiltered / cross-seat lexical candidates.
LEX_ID_WINDOW = 75_000
#: Of the scored set, how many best-first hits the arm hands to fusion.
LEX_POOL = 2000
_LEX_COLS = ("c.id, c.src_id::text AS src_id, c.seat, c.src, c.gen, c.batch, c.project, c.first_ord, c.last_ord, "
             "c.char_start, c.char_end, c.time_first, c.time_last")
_M_COLS = ("m.id, m.src_id, m.seat, m.src, m.gen, m.batch, m.project, m.first_ord, m.last_ord, m.char_start, "
           "m.char_end, m.time_first, m.time_last")


def _lexical_partial(cur, space: spaces.Space, ns: str) -> bool:
    """True while the walk is rebuilding the space's lexical rows (sql_meshychat/048's BACKFILL note): a space indexed
    before this derivation, not yet reset, or reset and not yet walked back to where it had been. "Back" means no
    manifest of the namespace up to lexical_rewalk_until is still to walk; dead-lettered ones are not walked, so they
    do not hold the arm partial for good (Codex on #390)."""
    cur.execute("SELECT watermark_seq, lexical_derivation, lexical_rewalk_until FROM ctx_index_state WHERE space = %s",
                (space.name,))
    st = cur.fetchone()
    if st is None:
        return False
    if st["lexical_derivation"] != lexical.DERIVATION:
        return int(st["watermark_seq"] or 0) > 0
    if st["lexical_rewalk_until"] is None or int(st["watermark_seq"] or 0) >= int(st["lexical_rewalk_until"]):
        return False
    cur.execute("SELECT EXISTS (SELECT 1 FROM ctx_commit_order o JOIN shared_memory sm ON sm.id = o.memory_id "
                "WHERE o.seq > %s AND o.seq <= %s AND o.kind = 'write' AND sm.namespace = %s "
                "AND NOT EXISTS (SELECT 1 FROM ctx_dead_letter d WHERE d.space = %s AND d.seq = o.seq "
                "AND d.state IN ('dead', 'replay'))) AS pending",
                (int(st["watermark_seq"] or 0), int(st["lexical_rewalk_until"]), ns, space.name))
    return bool(cur.fetchone()["pending"])


def _score_lexical_chunks(cur, space: spaces.Space, chunk_rows: list[dict], terms: list[str], partial: bool, *,
                          chunk_capped: bool = False, deadline: float | None = None) -> tuple[list[ctxindex.Hit], dict]:
    """Exact saturated TF-IDF over `chunk_rows` via sorted mesh_ctx_lexical_pkey point probes (identical math to SQL).
    `deadline` is checked before every probe batch (ctxindex.Deadline)."""
    if not chunk_rows:
        truncated = chunk_capped or partial
        detail: dict = {"ran": True, "candidates": 0, "scored_exactly": not chunk_capped,
                        "truncated": truncated, "partial": partial}
        if partial:
            detail["reason"] = "the lexical rows are being rebuilt by the index walk"
        return [], detail
    digests = sorted({r["text_sha256"] for r in chunk_rows})
    term_set = set(terms)
    lex_by_digest: dict[str, tuple[list[tuple[str, int]], float]] = {}
    for start in range(0, len(digests), LEX_FETCH_BATCH):
        ctxindex.check_deadline(deadline, f"lexical batch {start // LEX_FETCH_BATCH + 1}")
        batch = digests[start:start + LEX_FETCH_BATCH]
        # LATERAL + LIMIT 1 forces mesh_ctx_lexical_pkey point lookups in sorted digest order and prevents planner
        # fallback to global GIN bitmap scans or TOAST-detoasting full-table Seq Scans at batch=2048.
        cur.execute(
            "SELECT l.text_sha256, l.lexemes, l.tfs "
            "FROM (SELECT %s::text AS sp, %s::text AS deriv) p, "
            "unnest(%s::text[]) AS u(sha), "
            "LATERAL (SELECT text_sha256, lexemes, tfs FROM mesh_ctx_lexical "
            "WHERE space = p.sp AND text_sha256 = u.sha AND derivation = p.deriv LIMIT 1) l "
            "WHERE COALESCE(l.lexemes && %s::text[], false)",
            (space.name, lexical.DERIVATION, batch, terms),
        )
        for lr in cur.fetchall():
            pairs = [(t, int(tf)) for t, tf in zip(lr["lexemes"], lr["tfs"]) if t in term_set]
            if pairs:
                pre = sum(tf / (tf + 1.2) for _t, tf in pairs)
                lex_by_digest[lr["text_sha256"]] = (pairs, pre)

    matching = []
    for r in chunk_rows:
        hit_lex = lex_by_digest.get(r["text_sha256"])
        if hit_lex is not None:
            matching.append((r, hit_lex[0], hit_lex[1]))

    scanned_capped = len(matching) > LEX_SCAN_MAX
    if scanned_capped:
        matching = matching[:LEX_SCAN_MAX]
    matched = len(matching)
    truncated = chunk_capped or scanned_capped or matched > LEX_EXACT_MAX
    if matched > LEX_EXACT_MAX:
        matching.sort(key=lambda item: (-item[2], int(item[0]["id"])))
        m = matching[:LEX_EXACT_MAX]
    else:
        m = matching
    n = len(m)
    hits: list[ctxindex.Hit] = []
    if n > 0:
        df: dict[str, int] = {}
        for _r, pairs, _pre in m:
            for t, _tf in pairs:
                df[t] = df.get(t, 0) + 1
        idf = {t: math.log((1.0 + n) / (1.0 + c)) + 1.0 for t, c in df.items()}
        for r, pairs, _pre in m:
            score = sum((tf / (tf + 1.2)) * idf[t] for t, tf in pairs)
            if score > 0.0:
                hits.append(ctxindex._hit(r, score))
    ordered = ctxindex._order(hits)[:LEX_POOL]
    detail = {"ran": True, "candidates": n, "scored_exactly": not truncated, "truncated": truncated or partial,
              "partial": partial}
    if partial:
        detail["reason"] = "the lexical rows are being rebuilt by the index walk"
    return ordered, detail


def _lexical_arm_sql(cur, space: spaces.Space, where: str, args: list, terms: list[str], partial: bool
                     ) -> tuple[list[ctxindex.Hit], dict]:
    """Legacy SQL GIN join path over mesh_ctx_lexical; reached only when ctxindex.PREFILTER_COLD_SHARDS is False
    (or LEX_NARROW_MAX <= 0)."""
    cur.execute(
        f"WITH cand AS (SELECT {_LEX_COLS}, l.lexemes, l.tfs "
        f" FROM mesh_ctx_chunks c JOIN mesh_ctx_lexical l ON l.space = c.space AND l.text_sha256 = c.text_sha256 "
        f" WHERE {where} AND l.derivation = %s AND l.lexemes && %s::text[] LIMIT %s), "
        f"all_m AS (SELECT cand.*, (SELECT sum(u.tf::float8 / (u.tf + 1.2)) FROM unnest(cand.lexemes, cand.tfs) "
        f"  AS u(t, tf) WHERE u.t = ANY(%s::text[])) AS pre FROM cand ORDER BY pre DESC, cand.id LIMIT %s), "
        f"m AS (SELECT * FROM all_m ORDER BY pre DESC, id LIMIT %s), "
        f"n AS (SELECT count(*) AS n, (SELECT count(*) FROM all_m) AS matched, (SELECT count(*) FROM cand) AS scanned "
        f"      FROM m), "
        f"t AS (SELECT m.id, u.t, u.tf FROM m, unnest(m.lexemes, m.tfs) AS u(t, tf) WHERE u.t = ANY(%s::text[])), "
        f"df AS (SELECT t.t, count(*) AS df FROM t GROUP BY t.t), "
        f"s AS (SELECT t.id, sum((t.tf::float8 / (t.tf + 1.2)) * (ln((1 + n.n)::float8 / (1 + df.df)) + 1)) AS score "
        f"      FROM t JOIN df ON df.t = t.t CROSS JOIN n GROUP BY t.id) "
        f"SELECT {_M_COLS}, s.score, n.n, n.matched, n.scanned FROM s JOIN m ON m.id = s.id CROSS JOIN n "
        f"ORDER BY s.score DESC, m.id LIMIT %s",
        [*args, lexical.DERIVATION, terms, LEX_SCAN_MAX + 1, terms, LEX_EXACT_MAX + 1, LEX_EXACT_MAX, terms, LEX_POOL])
    rows = cur.fetchall()
    n = int(rows[0]["n"]) if rows else 0
    matched = int(rows[0]["matched"]) if rows else 0
    scanned = int(rows[0]["scanned"]) if rows else 0
    hits = [ctxindex._hit(r, float(r["score"])) for r in rows if r["score"] and r["score"] > 0]
    truncated = matched > LEX_EXACT_MAX or scanned > LEX_SCAN_MAX
    detail = {"ran": True, "candidates": n, "scored_exactly": not truncated, "truncated": truncated or partial,
              "partial": partial}
    if partial:
        detail["reason"] = "the lexical rows are being rebuilt by the index walk"
    return ctxindex._order(hits), detail


def _collect_lexical_chunks(cur, space: spaces.Space, ns: str, f: ctxindex.Filters, narrow_cap: int, *,
                            deadline: float | None = None) -> tuple[list[dict], bool]:
    """Read up to `narrow_cap` newest current latest-gen chunks matching `f` without correlated per-row SubPlans or
    external merge sorts. `deadline` (#538) is checked before every chunk read: the fallback read, each shard and each
    window (ctxindex.Deadline), so LEX_BUDGET_S bounds the collection as well as the probes.

    - Seat-filtered (`f.seat` set): walks the seat's shards from `ctx_shard_plan` newest-first using the same bounded
      `(time_last, id)` shard predicates as `ctxindex._load_shard` (zero SQL sort), filtering latest gens via
      `ctxindex._latest_gens` (`O(unique (seat, src))`).
    - Cross-seat / unfiltered (`f.seat` None): bounds `(min_lo, max_hi)` from `ctx_shard_plan` and walks backward
      `mesh_ctx_chunks_pkey` windows of `LEX_ID_WINDOW` IDs (`ORDER BY c.id DESC LIMIT remaining`), avoiding the
      planner's RLS-0.005-selectivity Bitmap + external-disk-sort trap on multi-million-row tables.
    """
    f_ns = dataclasses.replace(f, namespace=ns)
    cur.execute("SELECT shard_max FROM ctx_shard_setting WHERE one")
    if cur.fetchone() is None:
        where_fb, args_fb = f_ns.sql_latest(space)
        ctxindex.check_deadline(deadline, "the lexical chunk read")
        cur.execute(
            f"SELECT {_LEX_COLS}, c.text_sha256 FROM mesh_ctx_chunks c WHERE {where_fb} ORDER BY c.id DESC LIMIT %s",
            [*args_fb, narrow_cap + 1],
        )
        rows = [dict(r) for r in cur.fetchall()]
        if len(rows) > narrow_cap:
            return rows[:narrow_cap], True
        return rows, False

    max_windows = max(1, int(ctxindex.MAX_SHARDS_PER_QUERY))
    if f_ns.seat is not None:
        where_seat, args_seat = dataclasses.replace(f_ns, since=None).sql(space)
        if f_ns.since is not None:
            since_utc = f_ns.since.astimezone(_dt.timezone.utc)
            since_month = _dt.datetime(since_utc.year, since_utc.month, 1, tzinfo=_dt.timezone.utc)
            cur.execute(
                "SELECT month, lo, hi FROM ctx_shard_plan WHERE space = %s AND namespace = %s AND seat = %s "
                "AND month >= %s ORDER BY month DESC, shard_no DESC",
                (space.name, ns, f_ns.seat, since_month),
            )
        else:
            cur.execute(
                "SELECT month, lo, hi FROM ctx_shard_plan WHERE space = %s AND namespace = %s AND seat = %s "
                "ORDER BY month DESC, shard_no DESC",
                (space.name, ns, f_ns.seat),
            )
        shards = cur.fetchall()
        if not shards:
            return [], False
        kept: list[dict] = []
        for idx, sh in enumerate(shards):
            if idx >= max_windows:
                return kept, True
            sh_start, sh_end = ctxindex._month_bounds(sh["month"])
            eff_start = max(sh_start, f_ns.since) if f_ns.since is not None else sh_start
            ctxindex.check_deadline(deadline, f"lexical shard {idx + 1}")
            cur.execute(
                f"SELECT {_LEX_COLS}, c.text_sha256 FROM mesh_ctx_chunks c WHERE {where_seat} "
                f"AND c.time_last >= %s AND c.time_last < %s AND c.id BETWEEN %s AND %s AND c.current",
                [*args_seat, eff_start, sh_end, int(sh["lo"]), int(sh["hi"])],
            )
            raw = [dict(r) for r in cur.fetchall()]
            if raw:
                latest = ctxindex._latest_gens(cur, space, ns, {(r["seat"], r["src"]) for r in raw})
                valid = [r for r in raw if latest.get((r["seat"], r["src"])) == int(r["gen"])]
                valid.sort(key=lambda r: -int(r["id"]))
                kept.extend(valid)
                if len(kept) > narrow_cap:
                    return kept[:narrow_cap], True
        return kept, False

    where_c, args_c = f_ns.sql(space)
    if f_ns.since is not None:
        since_utc = f_ns.since.astimezone(_dt.timezone.utc)
        since_month = _dt.datetime(since_utc.year, since_utc.month, 1, tzinfo=_dt.timezone.utc)
        cur.execute(
            "SELECT lo, hi FROM ctx_shard_plan WHERE space = %s AND namespace = %s AND month >= %s",
            (space.name, ns, since_month),
        )
    else:
        cur.execute(
            "SELECT lo, hi FROM ctx_shard_plan WHERE space = %s AND namespace = %s",
            (space.name, ns),
        )
    plan_ranges = cur.fetchall()
    if not plan_ranges:
        return [], False
    min_lo = min(int(r["lo"]) for r in plan_ranges)
    max_hi = max(int(r["hi"]) for r in plan_ranges)
    if max_hi <= 0 or max_hi < min_lo:
        return [], False

    kept = []
    win_hi = max_hi
    win_step = max(1, int(LEX_ID_WINDOW))
    windows_done = 0
    while win_hi >= min_lo:
        if windows_done >= max_windows:
            return kept, True
        win_lo = max(min_lo, win_hi - win_step + 1)
        need = (narrow_cap + 1) - len(kept)
        ctxindex.check_deadline(deadline, f"lexical window {windows_done + 1}")
        cur.execute(
            f"SELECT {_LEX_COLS}, c.text_sha256 FROM mesh_ctx_chunks c WHERE {where_c} AND c.current "
            f"AND c.id BETWEEN %s AND %s ORDER BY c.id DESC LIMIT %s",
            [*args_c, win_lo, win_hi, need],
        )
        raw = [dict(r) for r in cur.fetchall()]
        if raw:
            latest = ctxindex._latest_gens(cur, space, ns, {(r["seat"], r["src"]) for r in raw})
            valid = [r for r in raw if latest.get((r["seat"], r["src"])) == int(r["gen"])]
            kept.extend(valid)
            if len(kept) > narrow_cap:
                return kept[:narrow_cap], True
        if len(raw) == need and int(raw[-1]["id"]) > win_lo:
            win_hi = int(raw[-1]["id"]) - 1
        else:
            win_hi = win_lo - 1
            windows_done += 1
    return kept, False


def _lexical_arm(cur, space: spaces.Space, ns: str, q: str, f: ctxindex.Filters, *,
                 exact_chunks: list[dict] | None = None,
                 cov: ctxindex.Coverage | None = None,
                 deadline: float | None = None) -> tuple[list[ctxindex.Hit], dict]:
    """(hits best first, detail). The chunks holding any query term, scored by a saturated TF-IDF whose idf is taken
    over the scored set: sum over query terms of tf/(tf + 1.2) * (ln((1 + n)/(1 + df)) + 1).

    A matching set of LEX_EXACT_MAX or fewer is scored whole, so the best chunk is never dropped before scoring (pxx
    34b9c6 point 2). A larger set is cut to LEX_EXACT_MAX by a relevance pre-order, the sum of tf/(tf + 1.2) over the
    query terms (the score without idf, which needs the whole set), ties by chunk id; `detail.truncated` says so. When
    PREFILTER_COLD_SHARDS is enabled, chunk subsets past LEX_NARROW_MAX pre-select the newest current chunks via
    `ctx_shard_plan` bounds and mark `detail.truncated = True`, mirroring ctxindex's newest-shard bound; `_lexical_arm_sql`
    runs only when PREFILTER_COLD_SHARDS is disabled.
    Of the scored set the best LEX_POOL go to fusion. Same tokenizer as the job (lexical.terms)."""
    lexemes, _tfs = lexical.terms(q)
    if not lexemes:
        return [], {"ran": True, "candidates": 0, "truncated": False, "reason": "the query has no terms"}
    partial = _lexical_partial(cur, space, ns)
    terms = list(lexemes)
    narrow_cap = int(LEX_NARROW_MAX)
    if ctxindex.PREFILTER_COLD_SHARDS and narrow_cap > 0:
        if exact_chunks is not None:
            if len(exact_chunks) <= narrow_cap:
                return _score_lexical_chunks(cur, space, exact_chunks, terms, partial, deadline=deadline)
            capped_rows = sorted(exact_chunks, key=lambda r: -int(r["id"]))[:narrow_cap]
            return _score_lexical_chunks(cur, space, capped_rows, terms, partial, chunk_capped=True,
                                         deadline=deadline)
        chunk_rows, chunk_capped = _collect_lexical_chunks(cur, space, ns, f, narrow_cap, deadline=deadline)
        return _score_lexical_chunks(cur, space, chunk_rows, terms, partial, chunk_capped=chunk_capped,
                                     deadline=deadline)
    ctxindex.check_deadline(deadline, "the lexical query")
    where, args = dataclasses.replace(f, namespace=ns).sql_latest(space)
    return _lexical_arm_sql(cur, space, where, args, terms, partial)


def _run_lexical_arm(c, space: spaces.Space, ns: str, q: str, f: ctxindex.Filters, *,
                     exact_chunks: list[dict] | None = None,
                     cov: ctxindex.Coverage | None = None,
                     deadline: float | None = None) -> tuple[list[ctxindex.Hit], dict]:
    """Run `_lexical_arm` inside a SAVEPOINT with a statement_timeout cap so QueryCanceled degrades to vector-only, and
    with `deadline` checked between its batches (ctxindex.Deadline), which degrades the same way. Raising out of the
    `with c.transaction()` block rolls the savepoint back, which also undoes the SET LOCAL timeout."""
    try:
        with c.transaction():
            with c.cursor(row_factory=dict_row) as cur:
                prev_ms: int | None = None
                if LEX_TIMEOUT_MS > 0:
                    cur.execute("SELECT setting::int AS ms FROM pg_settings WHERE name = 'statement_timeout'")
                    row = cur.fetchone()
                    cur_ms = int(row["ms"]) if row is not None else 0
                    if cur_ms <= 0 or cur_ms > int(LEX_TIMEOUT_MS):
                        prev_ms = max(0, cur_ms)
                        cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{int(LEX_TIMEOUT_MS)}ms",))
                out = _lexical_arm(cur, space, ns, q, f, exact_chunks=exact_chunks, cov=cov, deadline=deadline)
                if prev_ms is not None:
                    cur.execute("SELECT set_config('statement_timeout', %s, true)",
                                ("0" if prev_ms == 0 else f"{prev_ms}ms",))
                return out
    except psycopg.errors.QueryCanceled:
        return [], {"ran": False, "candidates": 0, "truncated": True, "partial": False,
                    "timed_out": True, "reason": "lexical query timed out"}
    except ctxindex.Deadline as exc:
        return [], {"ran": False, "candidates": 0, "truncated": True, "partial": False,
                    "timed_out": True, "reason": f"lexical arm {exc} ({LEX_BUDGET_S:g} s)"}


def search(conn, space: spaces.Space, ns: str, q: str, f: ctxindex.Filters, *, k: int = 10, rerank: bool = True,
           collapse: str = "chunk") -> dict:
    """The response body. `conn` is a serving connection with NO open transaction.

    Two arms, fused by reciprocal rank (apps/meshsem/fusion.rrf, the mail search's method): the vector arm
    (ctxindex, route B) and the lexical arm (mesh_ctx_lexical, sql_meshychat/048). Either may run alone: an embedder
    outage leaves the lexical arm, a lexical timeout leaves the vector arm, and the response says which ran. When
    reranking runs, candidate texts are scored by the cross-encoder BEFORE final group collapse."""
    k = max(1, min(int(k), MAX_K))
    timings: dict[str, float] = {}
    if not store.forbidden_rows_open():
        # The embed_policy gate is CLOSED: every context row is stamped 'forbidden', and the mail search hides such rows
        # while the gate is closed. So does this, on the read path too (Codex on #390): rows indexed while it was open
        # are not served after it closes. Nothing is embedded, read or counted, on either arm.
        return {"namespace": ns, "space": space.name, "k": k, "collapse": collapse, "gate_open": False,
                "hits": [], "truncated": True, "reason": "the embed_policy gate is closed (MESHY_EMBED_FORBIDDEN_ROWS)"}
    pool = POOL if collapse == "chunk" else max(POOL, k * POOL_PER_GROUP)
    vec_hits: list[ctxindex.Hit] = []
    cov = None
    exact_chunks: list[dict] | None = None
    vector_arm: dict = {"ran": False}

    t = time.monotonic()
    # The gate holds from the embed to the end of the store phase: a search refused here has cost HQB no embed, and the
    # rows it would have held never reach daphne. The bodies and the rerank below run outside it.
    with _gate.admit(STORE_QUEUE_S):
        timings["queue_ms"] = (time.monotonic() - t) * 1000
        t = time.monotonic()
        with dbconn.request_server_internal(conn) as c:
            with c.cursor(row_factory=dict_row) as cur:
                reference = store.reference_of(store.read_state(cur, space), space)
        got = None
        if reference is None:
            vector_arm = {"ran": False, "reason": "the space has no reference yet"}
        else:
            try:
                got = providers.embed_in_space(space, [space.query(q)], reference, budget_s=15.0)
            except providers.EmbedUnavailable as exc:
                vector_arm = {"ran": False, "reason": f"embed unavailable: {exc}"}
        timings["embed_ms"] = (time.monotonic() - t) * 1000

        t = time.monotonic()
        with dbconn.request_server_internal(conn) as c:
            if got is not None:
                qv = np.asarray(got.vectors[0], dtype=np.float64)
                try:
                    vec_hits, cov, exact_chunks = ctxindex._search_core(
                        c, space, ns, qv, f, k=max(pool, LEX_POOL), want_exact_chunks=True,
                        deadline=time.monotonic() + VECTOR_BUDGET_S,
                    )
                    vector_arm = {"ran": True, "provider": got.provider_id}
                except ctxindex.Deadline as exc:
                    # No statement failed, so the transaction is clean and the lexical arm still runs in it.
                    vec_hits, cov, exact_chunks = [], None, None
                    vector_arm = {"ran": False, "timed_out": True,
                                  "reason": f"vector arm {exc} ({VECTOR_BUDGET_S:g} s)"}
            lex_hits, lex_detail = _run_lexical_arm(c, space, ns, q, f, exact_chunks=exact_chunks, cov=cov,
                                                    deadline=time.monotonic() + LEX_BUDGET_S)
        timings["store_ms"] = (time.monotonic() - t) * 1000
    if not vector_arm["ran"] and not lex_detail["ran"]:
        raise errors.ApiError(503, "E_EMBED_UNAVAILABLE",
                              f"{vector_arm.get('reason')}; {lex_detail.get('reason') or 'the lexical arm did not run'}")
    if not vector_arm["ran"] and not lex_hits:
        raise errors.ApiError(503, "E_EMBED_UNAVAILABLE",
                              f"{vector_arm.get('reason')}; the lexical arm ran alone and matched nothing")

    by_id = {h.chunk_id: h for h in lex_hits}
    by_id.update({h.chunk_id: h for h in vec_hits})
    rankings = []
    if vector_arm["ran"]:
        rankings.append(fusion.Ranking(fusion.EMBED, [str(h.chunk_id) for h in vec_hits], [h.score for h in vec_hits]))
    if lex_detail["ran"]:
        rankings.append(fusion.Ranking(fusion.LEXICAL, [str(h.chunk_id) for h in lex_hits], [h.score for h in lex_hits]))
    fused = fusion.rrf(rankings)
    ranked, guaranteed, reserved_ids = _fused_pool(fused, by_id, pool)
    collapsed_all = _collapse(ranked, collapse)
    pool_truncated = len(ranked) >= pool and len(collapsed_all) < k
    if rerank:
        rerank_limit = max(k, POOL)
        cands = (ranked[:rerank_limit] if collapse == "chunk"
                 else _rerank_candidates(ranked, collapse, k=k, limit=rerank_limit, per_group=RERANK_PER_GROUP,
                                         reserved_ids=reserved_ids))
    else:
        cands = collapsed_all[:k]

    t = time.monotonic()
    with dbconn.request_server_internal(conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            texts, kinds = _texts(cur, ns, cands)
            stale, stale_capped = _unindexed_newer(cur, space, ns, f)
            behind = _walk_behind(cur, space, ns)
    timings["body_ms"] = (time.monotonic() - t) * 1000

    rr = None
    rr_by_chunk: dict[int, float] = {}
    t = time.monotonic()
    if rerank and cands:
        readable = [h for h in cands if texts.get(h.chunk_id) is not None]
        unreadable = [h for h in cands if texts.get(h.chunk_id) is None]
        if readable:
            rr = providers.rerank(q, [texts[h.chunk_id] or "" for h in readable])
        if rr is not None and rr.ran:
            rr_by_chunk = {h.chunk_id: float(s) for h, s in zip(readable, rr.scores)}
            ordered_cands = ([readable[i] for i in sorted(range(len(readable)), key=lambda i: (-rr.scores[i], i))]
                             + unreadable)
            final_hits = _collapse(ordered_cands, collapse)
        else:
            final_hits = collapsed_all
    else:
        final_hits = collapsed_all
    timings["rerank_ms"] = (time.monotonic() - t) * 1000

    shown = final_hits[:k]
    coverage = cov.as_dict() if cov is not None else {"path": None, "subset": None, "subset_at_least": False,
                                                      "scored": 0, "truncated": True,
                                                      "shards_searched": [], "shards_skipped": [],
                                                      "oldest_covered": None}
    return {
        "namespace": ns, "space": space.name, "k": k, "collapse": collapse,
        "filter": {"seat": f.seat, "src": f.src,
                   "since": None if f.since is None else f.since.isoformat(),
                   "until": None if f.until is None else f.until.isoformat()},
        "hits": [_hit_json(h, texts.get(h.chunk_id), h.score,
                           rr_by_chunk.get(h.chunk_id) if rr is not None and rr.ran else None,
                           kinds.get(h.chunk_id)) for h in shown],
        "coverage": coverage,
        "arms": {"vector": vector_arm, "lexical": lex_detail,
                 "rerank": {"requested": bool(rerank), "ran": bool(rr and rr.ran),
                            "provider": rr.provider_id if rr and rr.ran else None}},
        "method": fusion.method_name([a.arm for a in rankings], reranked=bool(rr and rr.ran)),
        "pool": {"size": len(ranked), "limit": pool, "truncated": pool_truncated, "guaranteed": guaranteed},
        "gate_open": True,
        # Sources whose newest committed manifest was dead-lettered: their older gen is still served, so a hit from
        # them may be out of date (pxx).
        "sources_with_unindexed_newer_gen": stale,
        "sources_with_unindexed_newer_gen_at_least": stale_capped,     # DEAD_SCAN_MAX rows read: a lower bound
        # Committed manifests over WALK_GRACE_S old are not walked yet: catch-up, a re-walk, or a stalled walk.
        "index_behind": behind,
        "truncated": (coverage["truncated"] or lex_detail.get("truncated", False) or pool_truncated
                      or len(collapsed_all) > len(shown) or len(final_hits) > len(shown) or stale > 0 or behind),
        "timings": {name: round(ms, 1) for name, ms in timings.items()},
    }


__all__ = ["ARM_GUARANTEE", "BUSY_RETRY_AFTER_S", "COLLAPSE", "E_BUSY", "FILTER_PARAMS", "LEX_BUDGET_S", "LEX_POOL",
           "LEX_TIMEOUT_MS", "MAX_K", "RERANK_PER_GROUP", "STORE_CONCURRENCY", "STORE_QUEUE_S", "VECTOR_BUDGET_S",
           "busy", "collapse_of", "configured_concurrency", "filters_from", "is_context_namespace", "search"]
