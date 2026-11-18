"""Search over a graph namespace: GET /v1/mesh/search?ns=<namespace> (divergence 060, OI-57 slice 2).

Design: docs/design/wiki-in-graph.md 4.4. The mail search (apps/meshsem/search.search) is untouched:
a request that names no `ns`, or names the mail, is answered by it exactly as before. A request that
names a namespace the graph walker derives (apps/meshsem/graphwalk.walked_namespaces) is answered
here, by the same two arms, the same fusion, the same pool and the same reranker, with three
differences, each because a graph row is chunked:

  * THE VECTOR ARM ranks CHUNKS and a row takes its best chunk's rank and cosine (max over
    chunks). `ChunkIndex` keys every chunk as "<src_id>:<chunk_no>"; `rank_rows` collapses the
    chunk ranking to one entry per row, best chunk first, and remembers which chunk it was.
  * THE LEXICAL ARM is per row, from the whole text (the walker lexes the row, not its chunks).
  * THE HIT is the chunk that decided it: `body` is that chunk's text (at most 1,500 characters),
    `chunk_no` and `chunk_span` say where it sits in the entry, and `namespace` names the
    namespace. A row only the lexical arm found gets the chunk sharing the most query terms. The
    reranker scores that chunk, so it judges the passage that matched, not the entry's opening.

AUTHORIZATION is the namespace's, as for the mail (views.py checks read on the NAMED namespace).
The gate (embed_policy) applies at read time as it does for the mail. The indexes and coverage are
cached per (space, namespace) beside the mail's in apps/meshsem/search, so search.forget() drops
them too.
"""
from __future__ import annotations

import time

import numpy as np
from psycopg.rows import dict_row

from apps.cards import store as card_store
from apps.cards import validation as card_rules
from apps.core import dbconn

from . import fusion, graphtext, lexical, providers, spaces, store
from . import search as S


class ChunkIndex(S.VectorIndex):
    """One space's vectors of one graph namespace, EVERY chunk, keyed "<src_id>:<chunk_no>"."""

    _ID_SQL = "src_id::text || ':' || chunk_no::text"
    _CHUNK_FILTER = ""

    def rank_rows(self, query_unit: np.ndarray) -> tuple[list[str], list[float], dict[str, int]]:
        """(src_ids best first, each row's best cosine, src_id -> its best chunk), at most ARM_DEPTH ROWS.

        Each row's best chunk is taken over ALL its cached chunks FIRST, and the depth cut is applied to rows
        after. The first version cut the chunk ranking at ARM_DEPTH and collapsed what was left, so a few long
        entries filling those slots removed every other entry from the arm (review of #36)."""
        with self.lock:
            if self.n == 0:
                return [], [], {}
            scores = self.buf[:self.n] @ query_unit.astype(np.float32)
            keys = list(self.ids[:self.n])
        best: dict[str, tuple[float, int]] = {}
        for key, score in zip(keys, scores):
            sid, _, n = key.rpartition(":")
            s = float(score)
            if sid not in best or s > best[sid][0]:
                best[sid] = (s, int(n))
        top = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:S.ARM_DEPTH]
        return [sid for sid, _ in top], [v[0] for _, v in top], {sid: v[1] for sid, v in top}


def index_for(space: spaces.Space, namespace: str) -> ChunkIndex:
    with S._registry_lock:
        idx = S._graph_indexes.get((space.name, namespace))
        if idx is None:
            idx = S._graph_indexes[(space.name, namespace)] = ChunkIndex(space, namespace)
        return idx


def lexical_index_for(space: spaces.Space, namespace: str) -> lexical.LexicalIndex:
    with S._registry_lock:
        idx = S._graph_lexical.get((space.name, namespace))
        if idx is None:
            idx = S._graph_lexical[(space.name, namespace)] = lexical.LexicalIndex(space.name, namespace)
        return idx


def _has_content_sql(alias: str) -> str:
    """SQL: the row has a non-blank string in some content-class field (graphtext.row_text is not None)."""
    return "coalesce(" + " OR ".join(
        f"(jsonb_typeof({alias}.value->'{f}') = 'string' AND {alias}.value->>'{f}' ~ '[^[:space:]]')"
        for f in graphtext.CONTENT_CLASS) + ", false)"


def coverage(conn, space: spaces.Space, namespace: str, *, gate_open: bool) -> store.Coverage:
    """The mail's five-way partition, for a graph namespace's heads. `skipped_empty` counts the heads
    with no content-class string (the walker's skipped_no_content); `embedded` the heads with at least
    one searchable vector; `skipped_no_schema_version` is always 0 (a graph row has no such rule)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), "
            "  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM mesh_embeddings me WHERE me.src_id = sm.id "
            "       AND me.space = %(space)s) AND (%(open)s OR sm.embed_policy = 'allowed')), "
            "  count(*) FILTER (WHERE NOT %(open)s AND sm.embed_policy = 'forbidden'), "
            f"  count(*) FILTER (WHERE (%(open)s OR sm.embed_policy = 'allowed') AND NOT {_has_content_sql('sm')}), "
            "  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM mesh_lexical ml WHERE ml.src_id = sm.id "
            "       AND ml.space = %(space)s AND ml.derivation = %(derivation)s) "
            "       AND (%(open)s OR sm.embed_policy = 'allowed')), "
            "  now() "
            "FROM shared_memory sm WHERE sm.namespace = %(ns)s AND sm.is_head",
            {"open": gate_open, "space": space.name, "ns": namespace, "derivation": lexical.DERIVATION})
        total, embedded, skipped, empty, lexed, now = cur.fetchone()
    total, embedded, skipped, empty = int(total), int(embedded), int(skipped), int(empty)
    return store.Coverage(embedded=embedded, total=total, skipped_forbidden=skipped, skipped_empty=empty,
                          lexical_indexed=int(lexed), pending=max(0, total - embedded - skipped - empty),
                          gate_open=gate_open, measured_at=now)


def coverage_for(conn, space: spaces.Space, namespace: str, gate: bool) -> store.Coverage:
    key = (space.name, namespace, gate)
    now = time.monotonic()
    with S._registry_lock:
        hit = S._graph_coverage.get(key)
    if hit and now - hit[0] < S.COVERAGE_TTL_S:
        return hit[1]
    cov = coverage(conn, space, namespace, gate_open=gate)
    with S._registry_lock:
        S._graph_coverage[key] = (time.monotonic(), cov)
    return cov


def _best_lexical_chunk(text: str, spans: list[tuple[int, int]], query: str) -> int:
    """The chunk sharing the most distinct query terms (the first on a tie): the passage a row that
    only the lexical arm found is shown and reranked by."""
    wanted = set(lexical.terms(query)[0])
    if not wanted or len(spans) == 1:
        return 0
    best, best_n = 0, -1
    for n, (a, b) in enumerate(spans):
        got = len(wanted & set(lexical.terms(text[a:b])[0]))
        if got > best_n:
            best, best_n = n, got
    return best


def _lookup(namespace: str, gate: bool, query: str, chunk_of: dict[str, int], keep=None):
    """The pool's lookup for a graph namespace: live rows (head, in the namespace, past the gate, with
    text), each as the candidate row search._pool builds a hit from, its body the deciding chunk.

    `keep(key, value)`, when given, decides whether a row is live at all (divergence 091: the card
    filter). A row it drops never enters the pool, so it is never reranked and never takes a slot: the
    pool's walk goes on to the next fused candidate, as it does past any row that is not live."""
    # Cards are rows of the card NAMESPACE, where memory.write validated them. A key that merely starts with `card/`
    # in another graph namespace is an ordinary row and gets no tier (Codex on #221: such a row with an odd `source`
    # made the whole search answer 500).
    cards_here = card_rules.is_card_namespace(namespace)

    def look(conn, ids: list[str]) -> list[dict]:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, key, version, created_at, author_label, value FROM shared_memory "
                        "WHERE id = ANY(%(ids)s::uuid[]) AND is_head AND namespace = %(ns)s "
                        "  AND (%(open)s OR embed_policy = 'allowed')",
                        {"ids": ids, "ns": namespace, "open": gate})
            rows = cur.fetchall()
        out = []
        for r in rows:
            found = graphtext.row_text(r["value"])
            if found is None:
                continue
            if keep is not None and not keep(r["key"], r["value"]):
                continue
            text = found[1]
            spans = graphtext.chunk_spans(text)
            n = chunk_of.get(str(r["id"]))
            if n is None or n >= len(spans):
                n = _best_lexical_chunk(text, spans, query)
            a, b = spans[n]
            # A card hit carries its thread and whether that thread was declared or a hook inferred it: a
            # thread filter accepts both alike, so the hit must say which (pxx, node-awareness item 8).
            tier = card_store.thread_tier(r["value"]) if cards_here and r["key"].startswith("card/") else None
            out.append({"id": r["id"], "key": r["key"], "version": r["version"],
                        "created_at": r["created_at"], "author_label": r["author_label"],
                        "body": text[a:b], "thread_id": r["value"]["thread_id"] if tier else None,
                        "thread_tier": tier, "namespace": namespace,
                        "chunk_no": n, "chunk_span": (a, b)})
        return out
    return look


def search(conn, space: spaces.Space, namespace: str, q: str, *, k: int = S.DEFAULT_K,
           rerank: bool = True, keep=None) -> dict:
    """GET /v1/mesh/search?ns=<namespace>. `conn` is a serving connection with NO open transaction.
    `keep(key, value)`: see `_lookup`."""
    gate = store.forbidden_rows_open()
    ceiling = S._ceiling()
    idx = index_for(space, namespace)
    lex = lexical_index_for(space, namespace)
    with dbconn.request_server_internal(conn) as c:
        with c.cursor() as cur:
            state = store.read_state(cur, space)
        cov = coverage_for(c, space, namespace, gate)
        try:
            reference = store.reference_of(state, space)
        except store.ReferenceMismatch as exc:
            raise S.SemanticUnavailable(503, "E_SPACE_MISMATCH", str(exc),
                                        dict(S._envelope(space, cov, S._describe(idx, lex, ceiling)),
                                             namespace=namespace))
        S.refresh_or_warming(idx, lex, c, ceiling,
                             lambda: dict(S._envelope(space, cov, S._describe(idx, lex, ceiling)), namespace=namespace))

    lex_arm = S._lexical_arm(lex, q)
    got, embed_attempts, chunk_of = None, [], {}
    if reference is None:
        embed_arm = S._unreferenced(space)
    else:
        try:
            got = providers.embed_in_space(space, [space.query(q)], reference, budget_s=15.0)
            embed_attempts = got.attempts
            qv = np.asarray(got.vectors[0], dtype=np.float64)
            ids, scores, chunk_of = idx.rank_rows((qv / np.linalg.norm(qv)).astype(np.float32))
            embed_arm = S.Arm(fusion.EMBED, True, fusion.Ranking(fusion.EMBED, ids, scores),
                              detail={"query_vector": "embedded", "ranked_chunks": min(idx.n, S.ARM_DEPTH)})
        except providers.EmbedUnavailable as exc:
            embed_attempts = exc.attempts
            embed_arm = S._embed_absent("embed_unavailable", "E_EMBED_UNAVAILABLE", str(exc))
    arms = [lex_arm, embed_arm]
    fused = fusion.rrf([a.ranking for a in arms if a.ran])
    with dbconn.request_server_internal(conn) as c:
        pool, examined, guaranteed = S._pool(c, fused, gate=gate, exclude_ids=set(), exclude_thread=None,
                                             lookup=_lookup(namespace, gate, q, chunk_of, keep))
    if not pool and not embed_arm.ran:
        raise S.SemanticUnavailable(
            503, embed_arm.error, f"{embed_arm.message}; the lexical arm ran alone and matched nothing",
            dict(S._envelope(space, cov, S._describe(idx, lex, ceiling), embed_attempts=embed_attempts,
                             arms=arms), namespace=namespace))
    ranked, rr = S._rerank(q, pool, rerank)
    hits = ranked[:k]
    body = S._envelope(space, cov, S._describe(idx, lex, ceiling),
                       embed_id=got.provider_id if got else None,
                       rerank_id=rr.provider_id if rr and rr.ran else None,
                       reranked=bool(rr and rr.ran), embed_attempts=embed_attempts,
                       rerank_attempts=rr.attempts if rr else (), arms=arms)
    body.update({
        "namespace": namespace, "k": k, "rerank_requested": bool(rerank),
        "hits": [h.as_json() for h in hits],
        "truncated": len(ranked) > len(hits),
        "pool": {"size": len(pool), "limit": S.POOL, "examined": examined, "fused": len(fused),
                 "guaranteed": guaranteed,
                 "ranked_vectors": len(embed_arm.ranking.ids) if embed_arm.ran else 0,
                 "ranked_lexical": len(lex_arm.ranking.ids)},
        "query_vector": ({"source": "embedded", "canary_min_cosine": got.canary_min_cosine} if got else None),
    })
    return body


__all__ = ["ChunkIndex", "coverage", "coverage_for", "index_for", "lexical_index_for", "search"]
