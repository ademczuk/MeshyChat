"""Cases that isolate what tests/test_meshsem_graphsearch.py (12 tests) leaves to chance, from a non-author review of #36 by mutation (kxxx, 2026-09-30).

Each test below kills one of the seven mutants of graphsearch.py that survived the PR's own file, and each passes on the PR's head:

  R2  no depth cut on rows                                        (the PR's depth test has exactly ARM_DEPTH rows, so nothing is ever cut)
  R3  every graph namespace shares one index                      (only one graph namespace is ever searched in a process)
  R4  on a tie the lexical chunk is the last, not the first
  R5  a chunk number past the entry's text is not caught          (a vector of an older derivation)
  R6  the coverage cache ignores the gate
  R7  a blank entry counts as content                             (`~ '[^[:space:]]'` against `~ '.'`)
  R9  the hit lookup takes an entry that is no longer head        (between a new version and the walker's next tick)

The fixtures and helpers are the PR's own.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_meshsem_routes import _bearer, _member, client, owner, world  # noqa: E402,F401  (fixtures)
from tests.test_meshsem_graphsearch import LATE, LONG, _claim, _get, _grant, _row, _walk, graph  # noqa: E402,F401

from apps.core import rls  # noqa: E402
from apps.meshsem import graphsearch, graphtext, search  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402


def test_r2_the_depth_cut_keeps_only_arm_depth_rows(monkeypatch):
    monkeypatch.setattr(search, "ARM_DEPTH", 3)
    sp = db.make_space("chunkcut")
    idx = graphsearch.ChunkIndex(sp, "test/x")
    q = np.zeros(db.DIMS, dtype=np.float32)
    q[0] = 1.0

    def put(key, x):
        v = np.zeros(db.DIMS, dtype=np.float32)
        v[0], v[1] = x, (1 - x * x) ** 0.5
        idx._put(key, v)
    for n in range(5):
        put(f"long:{n}", 0.9 - n * 0.01)
    put("other:0", 0.5)
    put("third:0", 0.4)
    put("fourth:0", 0.3)                              # the fourth row: below the cut
    ids, scores, chunk = idx.rank_rows(q)
    assert ids == ["long", "other", "third"] and len(scores) == 3 and set(chunk) == set(ids), (ids, chunk)


def test_r3_each_graph_namespace_has_its_own_indexes():
    sp = db.make_space("twons")
    search.forget()
    a, b = graphsearch.index_for(sp, "test/a"), graphsearch.index_for(sp, "test/b")
    assert a is not b and (a.namespace, b.namespace) == ("test/a", "test/b")
    la, lb = graphsearch.lexical_index_for(sp, "test/a"), graphsearch.lexical_index_for(sp, "test/b")
    assert la is not lb and (la.namespace, lb.namespace) == ("test/a", "test/b")
    assert graphsearch.index_for(sp, "test/a") is a, "control: the same namespace gets the same index back"
    search.forget()


def test_r4_a_tie_between_chunks_is_the_first():
    text = "alpha bravo alpha bravo"
    spans = [(0, 11), (12, 23)]                       # two chunks with the same words
    assert graphsearch._best_lexical_chunk(text, spans, "alpha") == 0


def test_r5_a_chunk_number_past_the_text_falls_back_to_the_best_passage(owner, world, graph):
    ns, holder = graph
    rid = _row(owner, holder, ns, "wiki/stale", {"body_md": LONG})
    look = graphsearch._lookup(ns, True, "lighthouse keeper lens", {str(rid): 99})       # a vector of an older, longer derivation
    with rls.server_internal(owner):
        rows = look(owner, [str(rid)])
    spans = graphtext.chunk_spans(graphtext.row_text({"body_md": LONG})[1])
    assert len(rows) == 1 and 0 < rows[0]["chunk_no"] < len(spans) and LATE in rows[0]["body"], rows


def test_r6_the_coverage_cache_is_kept_per_gate_state(owner, world, graph):
    ns, holder = graph
    _row(owner, holder, ns, "wiki/cov", {"body_md": "a covered entry"}, policy="forbidden")
    sp = db.make_space("covgate")
    with rls.server_internal(owner):
        opened = graphsearch.coverage_for(owner, sp, ns, True)
        closed = graphsearch.coverage_for(owner, sp, ns, False)
    assert (opened.gate_open, closed.gate_open) == (True, False)
    assert (opened.skipped_forbidden, closed.skipped_forbidden) == (0, 1), (opened, closed)
    search.forget()


def test_r7_a_blank_entry_counts_as_empty_and_not_as_content(owner, world, graph):
    ns, holder = graph
    _row(owner, holder, ns, "wiki/real", {"body_md": "some real text"})
    _row(owner, holder, ns, "wiki/blank", {"body_md": "   \n  "})
    _row(owner, holder, ns, "wiki/data", {"count": 3})
    sp = db.make_space("covblank")
    with rls.server_internal(owner):
        cov = graphsearch.coverage(owner, sp, ns, gate_open=True)
    assert (cov.total, cov.skipped_empty) == (3, 2), cov


def test_r9_a_superseded_entry_that_still_holds_vectors_is_not_a_hit(client, owner, world, graph):
    ns, holder = graph
    reader = _member(owner, "gssuper")
    _grant(owner, ns, holder, reader)
    word = "zqsuper" + uuid.uuid4().hex[:6]
    first = _row(owner, holder, ns, "wiki/versioned", {"body_md": f"first text {word}"})
    assert _walk(owner, ns).pending == 0
    found = _get(client, reader, word, ns, rerank=0).json()
    assert [h["key"] for h in found["hits"]] == ["wiki/versioned"], "control: found while it is the head"
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (first,))
    v2 = uuid.uuid4()
    sr.insert(owner, sr.row(holder["peer_id"], holder["owner_id"], ns, "wiki/versioned", {"body_md": "second text, nothing alike"}, rid=v2,
                            call_id=f"call-{v2}", version=2, parent_id=first, is_head=True, author_label=holder["label"], embed_policy="allowed"))
    search.forget()                                    # the index reloads from the table, where version 1's vectors still are (the walker has not ticked)
    after = _get(client, reader, word, ns, rerank=0).json()
    assert [h["key"] for h in after["hits"]] == [], after["hits"]
