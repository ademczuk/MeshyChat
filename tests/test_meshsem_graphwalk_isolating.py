"""Cases that isolate what tests/test_meshsem_graphwalk.py (29 tests) leaves to chance, from a non-author review of #34 by mutation (kxxx, 2026-09-30).

Each test below kills one of the eight mutants of graphwalk.py and store.py that survived the PR's own file, and each passes on the PR's head:

  G1  a row whose chunk count shrinks keeps its old chunks                     (the `replaced` delete)
  G2  the chunk bound is inclusive                                              (`>` against `>=`)
  G3  another job's reference does not stop the write                           (the canary race)
  G4  the tick's deadline is ignored between calls
  G5  a job starved of a provider passes instead of failing                     (run_graph_job)
  G6  a row is lexed only when its vectors changed
  G7  a row with current vectors and a missing lexical row counts as unchanged
  S1  the digests of a row's chunks are read in no chunk order                  (store.derived_digests)

The fixtures and helpers are the PR's own (tests/test_meshsem_graphwalk.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_meshsem_embedjob import Env, app, env, owner  # noqa: E402,F401  (fixtures; django.setup)
from tests.test_meshsem_graphwalk import LONG, _claim, _lexed, _row, _tick, _vectors  # noqa: E402

from django.test import override_settings  # noqa: E402

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE  # noqa: E402
from apps.meshsem import graphtext, graphwalk, spaces, store  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402


def _up(owner, env):
    env.settings([env.embed("same", env.hosts["same"])], gate=False)


def test_g1_a_row_whose_chunk_count_shrinks_under_a_new_derivation_loses_its_extra_chunks(owner, env, monkeypatch):
    """The derivation of a row's chunks can change while the row does not (a new window size): the old chunks past the new count must go, or the
    row is never 'unchanged' again (its stored digests never equal what it derives to) and is re-embedded every tick, with text the derivation no
    longer produces still searchable."""
    ns, p = _claim(owner)
    _up(owner, env)
    rid = _row(owner, p, ns, "wiki/rechunk", {"body_md": LONG})
    assert _tick(owner, ns).embedded_rows == 1
    before = len(_vectors(owner, env.space, ns)[str(rid)])
    assert before > 2, "control: the entry spans several chunks under the first derivation"
    monkeypatch.setattr(graphtext, "chunks", lambda text: [text[i:i + 4000] for i in range(0, len(text), 4000)])
    _tick(owner, ns)
    text = graphtext.row_text({"body_md": LONG})[1]
    want = len(range(0, len(text), 4000))
    assert len(_vectors(owner, env.space, ns)[str(rid)]) == want < before, "chunks of the old derivation are still stored"
    again = _tick(owner, ns)
    assert (again.embedded_rows, again.unchanged) == (0, 1), "the walk did not converge on the new derivation"


def test_g2_the_chunk_bound_admits_the_row_that_brings_the_tick_to_exactly_the_bound(owner, env):
    ns, p = _claim(owner)
    _up(owner, env)
    for n in range(3):
        _row(owner, p, ns, f"wiki/one-{n}", {"body_md": f"a short entry, number {n}"})
    r = _tick(owner, ns, embed_per_tick=2)
    assert (r.embedded_rows, r.embedded_chunks, r.pending, r.stopped) == (2, 2, 1, "embed_bound"), r


def test_g3_vectors_admitted_against_another_jobs_reference_are_not_written(owner, env, monkeypatch):
    """The space's reference is recorded once, by whichever job embeds first. If the stored one is not the one this tick embedded against, its
    vectors were admitted against the wrong canaries and none of them may be written."""
    ns, p = _claim(owner)
    _up(owner, env)
    _row(owner, p, ns, "wiki/race", {"body_md": "embedded against a reference another job then replaced"})
    monkeypatch.setattr(graphwalk.store, "record_reference", lambda cur, space, vecs, provider: np.zeros_like(np.asarray(vecs, dtype="<f4")))
    r = _tick(owner, ns)
    assert (r.embedded_rows, r.pending) == (0, 1), r
    assert _vectors(owner, env.space, ns) == {}, "vectors were written against a reference that was not the stored one"


def test_g4_a_tick_past_its_deadline_embeds_nothing_and_says_so(owner, env):
    ns, p = _claim(owner)
    _up(owner, env)
    _row(owner, p, ns, "wiki/warm", {"body_md": "records the reference"})
    assert _tick(owner, ns).embedded_rows == 1
    rid = _row(owner, p, ns, "wiki/late", {"body_md": "arrives after the deadline"})
    r = _tick(owner, ns, deadline_s=0.0)
    assert (r.stopped, r.embedded_rows, r.pending, r.more) == ("deadline", 0, 1, True), r
    assert str(rid) not in _vectors(owner, env.space, ns)
    assert str(rid) in _lexed(owner, env.space, ns), "the lexical arm needs no model and does not wait"
    assert _tick(owner, ns).embedded_rows == 1, "control: without the deadline the same row is embedded"


def test_g5_a_job_starved_of_a_provider_raises_after_committing_what_it_could(owner, app, env):
    from apps.jobs import registry
    ns, p = _claim(owner)
    _up(owner, env)
    _row(owner, p, ns, "wiki/warm", {"body_md": "records the reference"})
    _tick(owner, ns)
    env.settings([env.embed("down", env.hosts["down"])], gate=False)
    rid = _row(owner, p, ns, "wiki/starved", {"body_md": "during the outage"})
    job = next(j for j in registry.JOBS if j.name == "mesh_graph_embed")
    with override_settings(MESHY_GRAPH_NAMESPACES=f"{MESH_CHAT_NAMESPACE},{ns}"):
        with pytest.raises(graphwalk.GraphJobFailed) as exc:
            job.run(app, job)
    assert "1 row(s) pending" in str(exc.value) and ns in str(exc.value), str(exc.value)
    assert str(rid) in _lexed(owner, env.space, ns), "the progress that could be made was committed before the error"


def test_g6_and_g7_a_row_with_current_vectors_and_no_lexical_row_is_lexed_and_not_re_embedded(owner, env):
    ns, p = _claim(owner)
    _up(owner, env)
    rid = _row(owner, p, ns, "wiki/lexgap", {"body_md": "a short entry whose lexical row goes missing"})
    assert _tick(owner, ns).embedded_rows == 1
    with owner.cursor() as cur:
        cur.execute("DELETE FROM mesh_lexical WHERE src_id = %s", (rid,))
    r = _tick(owner, ns)
    assert (r.embedded_rows, r.lexed, r.unchanged) == (0, 1, 0), r
    assert str(rid) in _lexed(owner, env.space, ns)
    assert _tick(owner, ns).unchanged == 1, "control: then it is unchanged"


def test_s1_the_digests_of_a_rows_chunks_come_back_in_chunk_order_whatever_the_physical_order(owner, env):
    ns, p = _claim(owner)
    rid = _row(owner, p, ns, "wiki/order", {"body_md": "x"})
    vec = spaces.encode(np.ones(db.DIMS, dtype="float32"))
    scrambled = (3, 1, 5, 0, 4, 2)
    with owner.cursor() as cur:
        for n in scrambled:
            cur.execute("INSERT INTO mesh_embeddings (src_id, src_key, src_version, space, dims, text_sha256, vec, provider_id, recorded_at,"
                        " chunk_no, namespace) VALUES (%s,%s,1,%s,%s,%s,%s,'p',now(),%s,%s)",
                        (rid, "wiki/order", env.space.name, db.DIMS, str(n) * 64, vec, n, ns))
        got = store.derived_digests(cur, env.space, ns)
    assert got[str(rid)][0] == tuple(str(n) * 64 for n in sorted(scrambled)), got[str(rid)][0]
