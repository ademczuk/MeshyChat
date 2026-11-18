"""The value pilot for related edges in a graph namespace (OI-57, the operator's ruling of 2026-09-30).

Before anyone labels a calibration for wiki related edges, the operator judges whether such links are
USEFUL at all: a sample of entries, each shown with its top candidate related ENTRIES, rated useful or
not. If most are useful, the per-namespace related job and a calibration page follow; if not, the
slice closes as search only. This module builds that page. It changes nothing in the database.

    python -m apps.meshsem graph-pilot --ns shared/wiki [--entries 20] [--k 3] [--seed 1] --out FILE

WHAT IS COMPARED. Entries, not chunks (the review of the plan: a reader wants related ENTRIES, and
chunk-to-chunk links would list one entry several times). Two entries' score is the best cosine over
their chunk pairs, from the vectors the graph walker stored (mesh_embeddings of the namespace). An
entry is never compared with itself, so its own adjacent chunks, which trivially resemble each other,
cannot fill its list. Only current heads that pass the gate are sampled or proposed.

THE SAMPLE is stratified by length (short, medium, long thirds of the embedded entries), in turn, with a
fixed seed, so the page covers the shapes the calibration would have to handle and can be rebuilt
exactly.

WHAT THE PAGE HOLDS. Wiki text: each proposed pair shows the best-matching chunk of each side, cut to
PREVIEW characters. It is built where the graph walker has embedded the namespace, since the vectors
exist only there: on production, read only, after the wiki backfill (the last step of the gated rollout).
The rehearsal's restore copy cannot serve: the walker never runs on it and it is dropped afterwards. The
page is a working file for the operator, not something to post anywhere.
"""
from __future__ import annotations

import datetime as _dt
import random

import numpy as np
from psycopg.rows import dict_row

from . import graphtext, lexical, spaces, store

PREVIEW = 400


def _vectors(conn, space: spaces.Space, namespace: str, gate: bool) -> tuple[list[dict], np.ndarray, np.ndarray]:
    """(the embedded head entries, one unit vector per chunk, the entry index of each chunk)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT sm.id, sm.key, sm.value, me.chunk_no, me.vec FROM mesh_embeddings me "
            "JOIN shared_memory sm ON sm.id = me.src_id "
            "WHERE me.space = %(space)s AND me.namespace = %(ns)s AND me.dims = %(dims)s AND sm.is_head "
            "  AND (%(open)s OR sm.embed_policy = 'allowed') "
            "ORDER BY sm.key, me.chunk_no",
            {"space": space.name, "ns": namespace, "dims": space.dims, "open": gate})
        rows = cur.fetchall()
    entries: list[dict] = []
    at: dict[str, int] = {}
    vecs, owner = [], []
    for r in rows:
        sid = str(r["id"])
        if sid not in at:
            found = graphtext.row_text(r["value"])
            if found is None:
                continue
            at[sid] = len(entries)
            entries.append({"id": sid, "key": r["key"], "text": found[1],
                            "spans": graphtext.chunk_spans(found[1])})
        v = spaces.decode(r["vec"], space.dims).astype(np.float64)
        n = np.linalg.norm(v)
        if n == 0 or not np.isfinite(n):
            continue
        vecs.append(v / n)
        owner.append((at[sid], int(r["chunk_no"])))
    if not vecs:
        return entries, np.zeros((0, space.dims)), np.zeros((0, 2), dtype=int)
    return entries, np.vstack(vecs), np.asarray(owner, dtype=int)


def _sample(entries: list[dict], n: int, seed: int) -> list[int]:
    """n entry indexes, stratified by text length into thirds and taken in turn, with a fixed seed."""
    order = sorted(range(len(entries)), key=lambda i: (len(entries[i]["text"]), entries[i]["key"]))
    third = max(1, len(order) // 3)
    strata = [order[:third], order[third:2 * third], order[2 * third:]]
    rng = random.Random(seed)
    for s in strata:
        rng.shuffle(s)
    picked: list[int] = []
    while len(picked) < min(n, len(entries)):
        for s in strata:
            if s and len(picked) < n:
                picked.append(s.pop())
    return picked


def related(entries, vecs, owner, i: int, k: int) -> list[tuple[int, float, int, int]]:
    """Entry i's top k other entries: (entry, best cosine, i's chunk, its chunk), best first.

    Every other entry's best chunk pair is taken over ALL pairs first, and k is applied to entries after. The first
    version kept the top 50 * k chunk matches and collapsed those, so an entry with many self-similar chunks saw only
    itself there and got no link (review of #37)."""
    mine = np.where(owner[:, 0] == i)[0]
    if len(mine) == 0:
        return []
    sims = vecs[mine] @ vecs.T                                  # my chunks x every chunk
    col_best = sims.max(axis=0)                                 # each chunk's best match among my chunks
    col_from = mine[sims.argmax(axis=0)]                        # ... and which of my chunks that was
    best: dict[int, tuple[float, int, int]] = {}
    for b in range(len(owner)):
        j = int(owner[b, 0])
        if j == i:
            continue                                            # never the entry itself
        s = float(col_best[b])
        if j not in best or s > best[j][0]:
            best[j] = (s, int(owner[col_from[b], 1]), int(owner[b, 1]))
    top = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:k]
    return [(j, s, ca, cb) for j, (s, ca, cb) in top]


def _chunk_text(entry: dict, chunk: int) -> str:
    a, b = entry["spans"][min(chunk, len(entry["spans"]) - 1)]
    return " ".join(entry["text"][a:b].split())


def _cut(text: str, against: str) -> str:
    """At most PREVIEW characters of `text`: the window that best matches `against`, so the operator sees the
    passage that made the two chunks close. A chunk is up to 1,500 characters and its matching sentence can sit
    anywhere in it, where a plain first-PREVIEW cut would show only what does not match.

    A window scores the number of distinct words it shares with `against`, the first best on a tie. It is a
    display aid, not a ranking: the pair was chosen by cosine, and any word weighting here would only be a guess
    at what drove that cosine."""
    if len(text) <= PREVIEW:
        return text
    last = len(text) - PREVIEW
    starts = sorted(set(range(0, last + 1, 50)) | {last})       # the window flush with the end included
    windows = [set(lexical.terms(text[at:at + PREVIEW])[0]) for at in starts]
    wanted = set(lexical.terms(against)[0])
    best_at, best_score = 0, -1
    for at, w in zip(starts, windows):
        score = len(w & wanted)
        if score > best_score:
            best_at, best_score = at, score
    return ("[...] " if best_at else "") + text[best_at:best_at + PREVIEW] + (" [...]" if best_at < last else "")


def build(conn, space: spaces.Space, namespace: str, *, entries_n: int = 20, k: int = 3, seed: int = 1) -> str:
    """The pilot page as Markdown. `conn` inside server_internal."""
    gate = store.forbidden_rows_open()
    entries, vecs, owner = _vectors(conn, space, namespace, gate)
    picked = _sample(entries, entries_n, seed)
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = [f"# Related entries pilot: {namespace}", "",
           f"Built {now} from space `{space.name}`: {len(entries)} embedded entries, {len(vecs)} chunks; "
           f"{len(picked)} entries sampled by length (seed {seed}), top {k} related entries each, scored by "
           "the best cosine over chunk pairs, the entry itself excluded.", "",
           "For each proposed link, mark whether a reader of the first entry would want the second one "
           "linked: `[x] useful`, `[x] not useful`, or `[x] unsure`. The question is usefulness, not whether "
           "the two are about the same thing.", ""]
    for n, i in enumerate(picked, 1):
        e = entries[i]
        out += [f"## {n}. `{e['key']}` ({len(e['text'])} chars, {len(e['spans'])} chunk(s))", ""]
        links = related(entries, vecs, owner, i, k)
        if not links:
            out += ["No other entry to propose.", ""]
            continue
        for rank, (j, s, ca, cb) in enumerate(links, 1):
            f = entries[j]
            mine, theirs = _chunk_text(e, ca), _chunk_text(f, cb)
            out += [f"### {n}.{rank} to `{f['key']}` (cosine {s:.3f})", "",
                    f"> this entry, chunk {ca}: {_cut(mine, theirs)}", ">",
                    f"> related entry, chunk {cb}: {_cut(theirs, mine)}", "",
                    "- [ ] useful  - [ ] not useful  - [ ] unsure", ""]
    return "\n".join(out) + "\n"


__all__ = ["PREVIEW", "build", "related"]
