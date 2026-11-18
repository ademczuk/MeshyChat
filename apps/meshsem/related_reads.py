"""Reads of the persisted related_to edges (divergence 032). READ ONLY: imports no writer,
and sql_meshychat/013 refuses a request's write (008's split) in any case.

EVERY QUERY HERE FILTERS AT READ TIME, the way apps/meshsem/search.py does, because the
table is never the authority on what may be returned:
  * BOTH ends are joined to shared_memory with is_head and namespace = shared/mesh-chat,
    so a superseded version, a row of another namespace or a deleted row never appears;
  * the embed_policy gate applies to BOTH ends, so closing the gate hides an edge to a
    forbidden row at once, before the next job tick purges it;
  * only the CURRENT calibration's threshold_version and the configured space are served:
    a disabled calibration hides every edge, and a new threshold hides the old edges
    while the job rewrites them;
  * DECLARED WINS: an edge whose two messages are now declared in the same thread, or
    linked by a replies_to (for example after `python -m apps.mesh rebuild`), is hidden
    and counted as `hidden_declared`.

The coverage figures filter the same way: every one joins both ends by namespace and
gate, so an edge the gate hides is counted under no heading, `hidden_superseded` included.

Each hit carries `tier: "inferred"`, `edge_type: "related_to"`, `direction` ("out": the
anchor is the newer message that was scored as the query; "in": the other way), the
scores the job decided on, and the provenance of the decision.

/v1/mesh/context serves them as `related_edges`, BESIDE `related` (for_context below),
never instead of it: `related` is a ranking the caller sizes with `k`, the edges are a
list a calibrated threshold cut, precision first. The first version on this branch returned
these edges instead of `related`. On the calibration's own HQB scores (both runs, older
candidates), its list held 34 of the 63 grade-2 relations across the 20 anchors it would have
answered from edges; the reranked top 10 held 58.

The two are in DIFFERENT UNITS, and each `related` hit now says so (for_related_hits):
its rerank_score is the ONE-WAY logit with the anchor as the query (`score_kind:
"forward"`), an edge is decided on pair = mean(forward, reverse), so each hit also
carries `edge`, the stored edge between the anchor and it (pair, forward, reverse, rank,
direction, threshold_version) or null, read by the rules above in one query for the list.
"""
from __future__ import annotations

import threading
import time

from psycopg.rows import dict_row

from apps.core import dbconn
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.mesh import normalize

from . import relatedcal, search, spaces
from .search import _r6  # search's rounding, so an edge's scores and a hit's agree

LIMIT_DEFAULT = 20
LIMIT_MAX = 100
#: Related coverage is one pass over the namespace and the edge table: held and dated. 60 s since
#: 3 Nov 2026 (was 15): it took 4,837 ms on production as meshychat_app, and every answer carries
#: its measured_at, so the age shows (divergence 107).
COVERAGE_TTL_S = 60.0

_DECLARED = (
    "(EXISTS (SELECT 1 FROM mesh_edges t1 JOIN mesh_edges t2 "
    "          ON t2.edge_type = 'in_thread' AND t2.dst = t1.dst "
    "         WHERE t1.src_id = e.src_id AND t1.edge_type = 'in_thread' "
    "           AND t2.src_id = e.dst_id) "
    " OR EXISTS (SELECT 1 FROM mesh_edges r WHERE r.edge_type = 'replies_to' "
    "            AND ((r.src_id = e.src_id AND r.dst = d.key) "
    "              OR (r.src_id = e.dst_id AND r.dst = s.key))))"
)

_VISIBLE = (
    "JOIN shared_memory s ON s.id = e.src_id AND s.is_head AND s.namespace = %(ns)s "
    "  AND (%(open)s OR s.embed_policy = 'allowed') "
    "JOIN shared_memory d ON d.id = e.dst_id AND d.is_head AND d.namespace = %(ns)s "
    "  AND (%(open)s OR d.embed_policy = 'allowed') "
)

_EDGES_OF = (
    "SELECT e.score_fwd, e.score_rev, e.pair_score, e.rank, e.fused_rank, e.cosine, "
    "  e.lexical_score, e.rerank_provider_id, e.rerank_model, e.threshold_version, "
    "  e.inferred_at, e.space, "
    "  CASE WHEN e.src_id = %(a)s THEN 'out' ELSE 'in' END AS direction, "
    "  o.key, o.version, o.created_at, o.author_label, "
    "  CASE WHEN jsonb_typeof(o.value->'body') = 'string' THEN o.value->>'body' END AS body, "
    "  (SELECT t.dst FROM mesh_edges t WHERE t.src_id = o.id AND t.edge_type = 'in_thread' "
    "     ORDER BY t.dst LIMIT 1) AS thread_id "
    "FROM mesh_related_edges e " + _VISIBLE +
    "JOIN shared_memory o ON o.id = CASE WHEN e.src_id = %(a)s THEN e.dst_id "
    "                                    ELSE e.src_id END "
    "WHERE (e.src_id = %(a)s OR e.dst_id = %(a)s) AND e.space = %(space)s "
    "  AND e.threshold_version = %(tv)s AND NOT " + _DECLARED + " "
    "ORDER BY e.pair_score DESC, e.rank ASC, o.created_at DESC, o.id "
    "LIMIT %(lim)s"
)

_HIDDEN_DECLARED_OF = (
    "SELECT count(*) FROM mesh_related_edges e " + _VISIBLE +
    "WHERE (e.src_id = %(a)s OR e.dst_id = %(a)s) AND e.space = %(space)s "
    "  AND e.threshold_version = %(tv)s AND " + _DECLARED
)

#: The edge between the anchor and each of a LIST of messages, either direction, under
#: the same rules as _EDGES_OF (both ends visible, this space, the current version,
#: declared wins). Edges are stored newer -> older only, so a pair has at most one.
_BETWEEN = (
    "SELECT CASE WHEN e.src_id = %(a)s THEN e.dst_id ELSE e.src_id END AS other_id, "
    "  e.pair_score, e.score_fwd, e.score_rev, e.rank, e.threshold_version, "
    "  CASE WHEN e.src_id = %(a)s THEN 'out' ELSE 'in' END AS direction "
    "FROM mesh_related_edges e " + _VISIBLE +
    "WHERE ((e.src_id = %(a)s AND e.dst_id = ANY(%(ids)s::uuid[])) "
    "    OR (e.dst_id = %(a)s AND e.src_id = ANY(%(ids)s::uuid[]))) "
    "  AND e.space = %(space)s AND e.threshold_version = %(tv)s AND NOT " + _DECLARED
)


def current(space: spaces.Space) -> relatedcal.Calibration | None:
    """The calibration reads serve, or None. An invalid file serves nothing (the job
    reports it as an error; a read must not fail over it)."""
    try:
        cal = relatedcal.load()
    except relatedcal.CalibrationInvalid:
        return None
    return cal if cal is not None and cal.space == space.name else None


def _hit(r: dict, *, with_body: bool) -> dict:
    h = {"key": r["key"], "version": r["version"],
         "recorded_at": r["created_at"].isoformat(), "author": r["author_label"],
         "thread_id": r["thread_id"], "tier": "inferred", "edge_type": "related_to",
         "direction": r["direction"],
         "scores": {"pair": _r6(r["pair_score"]), "forward": _r6(r["score_fwd"]),
                    "reverse": _r6(r["score_rev"]), "cosine": _r6(r["cosine"]),
                    "lexical": _r6(r["lexical_score"]), "fused_rank": r["fused_rank"],
                    "rank": r["rank"]},
         "inferred": {"space": r["space"], "rerank_provider_id": r["rerank_provider_id"],
                      "rerank_model": r["rerank_model"],
                      "threshold_version": r["threshold_version"],
                      "inferred_at": r["inferred_at"].isoformat()}}
    if with_body:
        # The related hits' body rule (search.body_fields): stripped, cut at MAX_CHARS.
        h["body"], h["body_truncated"] = search.body_fields(r["body"])
    return h


def edges_of(conn, space: spaces.Space, anchor_id, cal: relatedcal.Calibration, *,
             gate: bool, limit: int, with_body: bool) -> tuple[list[dict], bool, int]:
    """(hits best first, truncated, hidden_declared) for one message, both directions.
    `conn` inside server_internal."""
    params = {"a": str(anchor_id), "space": space.name, "tv": cal.threshold_version,
              "ns": MESH_CHAT_NAMESPACE, "open": gate, "lim": int(limit) + 1}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_EDGES_OF, params)
        rows = cur.fetchall()
        cur.execute(_HIDDEN_DECLARED_OF, params)
        hidden = int(cur.fetchone()["count"])
    truncated = len(rows) > limit
    return [_hit(r, with_body=with_body) for r in rows[:limit]], truncated, hidden


def edges_between(conn, space: spaces.Space, anchor_id, other_ids,
                  cal: relatedcal.Calibration, *, gate: bool) -> dict[str, dict]:
    """{other message's id: its edge with the anchor} for every one of `other_ids` that
    has a VISIBLE edge, in ONE query for the whole list (_BETWEEN); the rest are absent
    from the map. `direction` is edges_of's: "out" when the anchor is the edge's source.
    `conn` inside server_internal."""
    ids = [str(i) for i in other_ids]
    if not ids:
        return {}
    params = {"a": str(anchor_id), "ids": ids, "space": space.name,
              "tv": cal.threshold_version, "ns": MESH_CHAT_NAMESPACE, "open": gate}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_BETWEEN, params)
        rows = cur.fetchall()
    return {str(r["other_id"]): {"pair": _r6(r["pair_score"]), "forward": _r6(r["score_fwd"]),
                                 "reverse": _r6(r["score_rev"]), "rank": r["rank"],
                                 "direction": r["direction"],
                                 "threshold_version": r["threshold_version"]}
            for r in rows}


def for_related_hits(conn, space: spaces.Space, anchor: dict, hit_ids, *,
                     gate: bool) -> dict[str, dict]:
    """/v1/mesh/context's `edge` on each `related` hit: the stored edge between the anchor
    and that hit, keyed by the hit's id (edges_between), so a caller reads the pair's
    numbers beside the hit's ONE-WAY rerank_score instead of comparing the two. Empty with
    no calibration to serve (`related_edges.absent` then says `not_calibrated`). `conn`:
    the serving connection with NO open transaction; this opens its own short one, after
    `related` was ranked, so the edge reads' gates apply to the rows as they are then.
    No model call."""
    ids = list(hit_ids)
    if not ids:
        return {}
    cal = current(space)
    if cal is None:
        return {}
    with dbconn.request_server_internal(conn) as c:
        return edges_between(c, space, anchor["id"], ids, cal, gate=gate)


def for_context(conn, space: spaces.Space, anchor: dict, *, gate: bool,
                absent: str | None = None) -> dict:
    """/v1/mesh/context's `related_edges`: the anchor's persisted related_to edges, both
    directions, best first, up to LIMIT_DEFAULT (`truncated` says when more exist).
    Served BESIDE context's `related`, never instead of it. `absent` is context's reason
    for ranking nothing (a forbidden or textless anchor): then no edge is read either.
    With no calibration to serve, `absent: "not_calibrated"`. `edges.processed_at` is
    null until the job has decided the anchor as a source. `conn`: the serving
    connection with NO open transaction; this opens its own short one. No model call."""
    if absent is not None:
        return {"hits": [], "absent": absent, "truncated": False}
    cal = current(space)
    if cal is None:
        return {"hits": [], "absent": "not_calibrated", "truncated": False}
    with dbconn.request_server_internal(conn) as c:
        hits, truncated, hidden = edges_of(c, space, anchor["id"], cal, gate=gate,
                                           limit=LIMIT_DEFAULT, with_body=True)
        with c.cursor() as cur:
            cur.execute("SELECT processed_at FROM mesh_related_runs WHERE src_id = %s "
                        "AND space = %s AND threshold_version = %s",
                        (anchor["id"], space.name, cal.threshold_version))
            row = cur.fetchone()
    return {"hits": hits, "absent": None, "truncated": truncated,
            "edges": {"threshold_version": cal.threshold_version,
                      "threshold": cal.threshold, "k": cal.k,
                      "processed_at": row[0].isoformat() if row else None},
            "excluded": {"hidden_declared": hidden}}


# ------------------------------------------------------------------------ coverage

_cov_cache: dict[tuple, tuple[float, dict]] = {}
_cov_lock = threading.Lock()


def coverage(conn, space: spaces.Space, *, gate: bool,
             cal: relatedcal.Calibration | None) -> dict:
    """The denominators of the inferred tier, held COVERAGE_TTL_S and dated.
    eligible: head messages with a vector in the space that pass the gate; processed and
    with_edges: of those, decided by the job under the current calibration (and with at
    least one edge); pairs_refused: pairs the reranker refused while deciding them (never
    scored, never an edge); edges: visible edges; hidden_superseded / hidden_declared:
    edges the reads hide because an end is no longer a head row / the pair is now
    declared. Every figure joins both ends by namespace and gate."""
    tv = cal.threshold_version if cal else None
    key = (space.name, gate, tv)
    now = time.monotonic()
    with _cov_lock:
        hit = _cov_cache.get(key)
    if hit and now - hit[0] < COVERAGE_TTL_S:
        return hit[1]
    p = {"space": space.name, "ns": MESH_CHAT_NAMESPACE, "open": gate, "tv": tv}
    with conn.cursor(row_factory=dict_row) as cur:
        # JIT compiled this statement on every miss: its estimated cost (about 250k on production)
        # is over jit_above_cost, and compiling took 881 ms of its 4,837 (3 Nov 2026, divergence 107).
        # Off for this one statement, then back to what the transaction had.
        cur.execute("SELECT current_setting('jit') AS jit")
        jit_was = cur.fetchone()["jit"]
        cur.execute("SET LOCAL jit = off")
        cur.execute(
            "SELECT "
            " (SELECT count(*) FROM shared_memory sm JOIN mesh_embeddings me "
            "    ON me.src_id = sm.id AND me.space = %(space)s "
            "  WHERE sm.namespace = %(ns)s AND sm.is_head AND sm.key LIKE 'msg/%%' "
            "    AND (%(open)s OR sm.embed_policy = 'allowed')) AS eligible, "
            " (SELECT count(*) FROM mesh_related_runs r JOIN shared_memory sm "
            "    ON sm.id = r.src_id AND sm.is_head "
            "   AND (%(open)s OR sm.embed_policy = 'allowed') "
            "  WHERE r.space = %(space)s AND r.threshold_version = %(tv)s) AS processed, "
            " (SELECT count(*) FROM mesh_related_runs r JOIN shared_memory sm "
            "    ON sm.id = r.src_id AND sm.is_head "
            "   AND (%(open)s OR sm.embed_policy = 'allowed') "
            "  WHERE r.space = %(space)s AND r.threshold_version = %(tv)s "
            "    AND r.outcome = 'edges') AS with_edges, "
            " (SELECT coalesce(sum(r.pairs_refused), 0) FROM mesh_related_runs r "
            "    JOIN shared_memory sm ON sm.id = r.src_id AND sm.is_head "
            "   AND (%(open)s OR sm.embed_policy = 'allowed') "
            "  WHERE r.space = %(space)s AND r.threshold_version = %(tv)s) "
            "    AS pairs_refused, "
            " (SELECT count(*) FROM mesh_related_edges e " + _VISIBLE +
            "  WHERE e.space = %(space)s AND e.threshold_version = %(tv)s "
            "    AND NOT " + _DECLARED + ") AS edges, "
            " (SELECT count(*) FROM mesh_related_edges e " + _VISIBLE +
            "  WHERE e.space = %(space)s AND e.threshold_version = %(tv)s "
            "    AND " + _DECLARED + ") AS hidden_declared, "
            # Hidden BECAUSE an end is superseded: both ends in the namespace and past
            # the gate, as everywhere else, so a gated edge is counted under no heading.
            " (SELECT count(*) FROM mesh_related_edges e "
            "    JOIN shared_memory s ON s.id = e.src_id AND s.namespace = %(ns)s "
            "     AND (%(open)s OR s.embed_policy = 'allowed') "
            "    JOIN shared_memory d ON d.id = e.dst_id AND d.namespace = %(ns)s "
            "     AND (%(open)s OR d.embed_policy = 'allowed') "
            "  WHERE e.space = %(space)s AND e.threshold_version = %(tv)s "
            "    AND NOT (s.is_head AND d.is_head)) AS hidden_superseded, "
            " now() AS measured_at", p)
        row = dict(cur.fetchone())
        cur.execute("SELECT set_config('jit', %s, true)", (jit_was,))
        cur.execute("SELECT threshold_version, last_status, last_status_at "
                    "FROM mesh_related_projection WHERE space = %s", (space.name,))
        st = cur.fetchone() or {}
    out = {k: int(row[k]) for k in ("eligible", "processed", "with_edges", "pairs_refused",
                                     "edges", "hidden_superseded", "hidden_declared")}
    out.update({"threshold_version": tv, "calibrated": cal is not None,
                "walk_threshold_version": st.get("threshold_version"),
                "last_status": st.get("last_status"),
                "last_status_at": (st["last_status_at"].isoformat()
                                   if st.get("last_status_at") else None),
                "forbidden_rows_open": gate,
                "measured_at": row["measured_at"].isoformat()})
    with _cov_lock:
        _cov_cache[key] = (time.monotonic(), out)
    return out


def forget() -> None:
    with _cov_lock:
        _cov_cache.clear()


# ------------------------------------------------------------------ the graph route

class AnchorNotFound(LookupError):
    pass


def neighbours(conn, space: spaces.Space | None, key: str, *, include_inferred: bool, limit: int,
               gate: bool) -> dict:
    """GET /v1/mesh/neighbours: one message's neighbours, one hop, no bodies. Declared by
    default (replies_to parent, replied_by, the thread and its size); with
    `include_inferred`, the related_to edges in both directions (each with the
    neighbour's thread, so a client walks across threads one hop per call), the
    auditor's inferred replies_to, and related coverage. `thread.size` counts only
    projected messages, so an unprojected anchor is not included in it. `space` is read only
    with `include_inferred`, and may be None without it. `conn` inside server_internal."""
    if include_inferred and space is None:
        raise ValueError("include_inferred needs the configured space")
    from apps.mesh import graph as mesh_graph        # read-only use of the edge projection

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, key, version, created_at, author_label, value, embed_policy, "
            "  value->>'done_when' AS done_when, "
            "  (SELECT t.dst FROM mesh_edges t WHERE t.src_id = sm.id "
            "     AND t.edge_type = 'in_thread' ORDER BY t.dst LIMIT 1) AS thread_id "
            "  , EXISTS (SELECT 1 FROM mesh_edges e WHERE e.src_id = sm.id) AS projected "
            "FROM shared_memory sm WHERE namespace = %s AND key = %s AND is_head",
            (MESH_CHAT_NAMESPACE, key))
        anchor = cur.fetchone()
        if anchor is None:
            raise AnchorNotFound(key)
        projected = bool(anchor["projected"])
        if projected:
            thread_id = anchor["thread_id"]
            cur.execute("SELECT e.dst, h.version, h.created_at, h.author_label "
                        "FROM mesh_edges e LEFT JOIN shared_memory h ON h.namespace = %s "
                        "  AND h.key = e.dst AND h.is_head "
                        "WHERE e.src_id = %s AND e.edge_type = 'replies_to' "
                        "ORDER BY e.dst LIMIT 1", (MESH_CHAT_NAMESPACE, anchor["id"]))
            parent = cur.fetchone()
            cur.execute("SELECT edge_type, dst FROM mesh_edges WHERE src_id = %s "
                        "AND edge_type IN ('owes_ack', 'mentions', 'closes', 'declines') ORDER BY edge_type, dst",
                        (anchor["id"],))
            extras = cur.fetchall()
        else:
            # A head whose value is JSON null, a scalar or an array is data the projection skips
            # (normalize.edges_for), so catch-up never moves it out of this branch, and the readers
            # below take an object: it has no thread, parent or edges (codex on #93).
            value = anchor["value"] if isinstance(anchor["value"], dict) else {}
            thread_id, _, _ = normalize.thread_of(value)
            reply_to, _, _ = normalize.reply_to_of(value)
            # Mentions are body-derived: the same embed_policy gate the projection applies
            # (apps/mesh/projection.py _project), or this fallback would serve, before catch-up, the
            # mentions a forbidden row never gets once it is projected.
            mentions_allowed = gate or anchor.get("embed_policy") == "allowed"
            projected_edges = normalize.project_row(anchor, mentions_allowed=mentions_allowed).projected.edges
            extras = sorted(({"edge_type": edge.edge_type, "dst": edge.dst}
                             for edge in projected_edges
                             if edge.edge_type in ("owes_ack", "mentions", "closes", "declines")),
                            key=lambda edge: (edge["edge_type"], edge["dst"]))
            parent = None
            if reply_to is not None:
                cur.execute("SELECT %s AS dst, h.version, h.created_at, h.author_label "
                            "FROM shared_memory h WHERE h.namespace = %s AND h.key = %s "
                            "AND h.is_head", (reply_to, MESH_CHAT_NAMESPACE, reply_to))
                parent = cur.fetchone()
                if parent is None:
                    parent = {"dst": reply_to, "version": None, "created_at": None,
                              "author_label": None}
        size = None
        if thread_id is not None:
            cur.execute("SELECT count(*) FROM mesh_edges e JOIN shared_memory sm "
                        "  ON sm.id = e.src_id AND sm.is_head "
                        "WHERE e.edge_type = 'in_thread' AND e.dst = %s",
                        (thread_id,))
            size = int(cur.fetchone()["count"])
    replies = mesh_graph.replies(conn, key, limit=limit)
    out = {
        "key": key, "limit": limit,
        "anchor": {"key": anchor["key"], "version": anchor["version"],
                    "recorded_at": anchor["created_at"].isoformat(),
                    "author": anchor["author_label"], "thread_id": thread_id,
                    "projected": projected,
                    "thread_source": "edges" if projected else "value",
                    "owes_ack": [r["dst"] for r in extras if r["edge_type"] == "owes_ack"],
                    "done_when": anchor["done_when"]},
        "declared": {
            "replies_to": None if parent is None else {
                "key": parent["dst"], "found": parent["version"] is not None,
                "version": parent["version"], "author": parent["author_label"],
                "recorded_at": (parent["created_at"].isoformat()
                                 if parent["created_at"] else None),
                "tier": "declared" if projected else "declared_unprojected"},
            "replied_by": [{"key": h.key, "version": h.version,
                            "recorded_at": h.recorded_at.isoformat(), "author": h.author,
                            "via": h.src_field, "tier": "declared"} for h in replies.hits],
            "replied_by_truncated": replies.truncated,
            "thread": None if thread_id is None else {
                "thread_id": thread_id, "size": size, "size_is_projected_count": True},
            # body-derived: under the same embed_policy gate as graph.mentions, read now (codex on #89);
            # the unprojected fallback applied it already, when it derived them
            "mentions": [r["dst"] for r in extras if r["edge_type"] == "mentions"
                         and (gate or anchor["embed_policy"] == "allowed")],
            "closes": [r["dst"] for r in extras if r["edge_type"] == "closes"],
            "declines": [r["dst"] for r in extras if r["edge_type"] == "declines"],
        },
        "inferred": {"included": False},
    }
    if not include_inferred:
        return out
    cal = current(space)
    related, truncated, hidden = [], False, 0
    if cal is not None:
        related, truncated, hidden = edges_of(conn, space, anchor["id"], cal, gate=gate,
                                              limit=limit, with_body=False)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT i.dst, i.edge_type, i.model, i.provider_id, i.prompt_version, "
                    "  i.inferred_at FROM mesh_inferred_edges i "
                    "JOIN shared_memory sm ON sm.id = i.src_id AND sm.is_head "
                    "WHERE i.src_id = %s AND i.edge_type = 'replies_to' "
                    "ORDER BY i.dst LIMIT %s", (anchor["id"], int(limit)))
        auditor = [{"dst": r["dst"], "edge_type": r["edge_type"], "tier": "inferred",
                    "inferred": {"model": r["model"], "provider_id": r["provider_id"],
                                 "prompt_version": r["prompt_version"],
                                 "inferred_at": r["inferred_at"].isoformat()}}
                   for r in cur.fetchall()]
    out["inferred"] = {"included": True, "related_to": related, "truncated": truncated,
                       "hidden_declared": hidden, "replies_to": auditor,
                       "coverage": coverage(conn, space, gate=gate, cal=cal)}
    return out


__all__ = ["AnchorNotFound", "COVERAGE_TTL_S", "LIMIT_DEFAULT", "LIMIT_MAX", "coverage",
           "current", "edges_between", "edges_of", "for_context", "for_related_hits",
           "forget", "neighbours"]
