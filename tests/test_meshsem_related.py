"""The persisted related_to edges: the job, the table and its rules. Divergence 032.

A real database, the job running as meshychat_app (dbconn.connect), and the deterministic fake
model hosts of tests/fixtures/meshsem_fake.py over real HTTP. No live model is touched.

THE RERANKER IN THESE TESTS answers a SCRIPTED table (`rerank_table`, exact query and
document strings) and -10 for every other pair, so each test decides exactly which pairs
cross the threshold, including asymmetric ones (A->B high, B->A low), and no row another
test wrote can ever cross it. The calibration each test runs under is built by
apps/meshsem/relatedcal.build from THIS host's canary answers, so the canary check admits
it; a host whose answers differ (`shifted`) is refused.

ISOLATION. The namespace is shared by the whole run. Each test has its own embedding space
(its vectors, its related state row, its edges), drains the declared projection and the
embed walk, and then STARTS the related walk at the embed watermark (`_start_here`): rows
written before this test are other tests' history, and relating all of them would only
cost time. Every row a test writes is dated a few milliseconds after that watermark,
never later: a row dated in the future would sit ahead of every later test's rows and
the keyset walks would skip them.

Each module this feature adds is imported inside the tests (`_mod`), so on a tree without
it every test fails on its own ImportError or assertion rather than the file failing to
collect. That is what makes the red-proof per test.

Run:  pytest tests/test_meshsem_related.py -q
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as _dt
import importlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.core import dbconn  # noqa: E402
from apps.jobs import registry, runner  # noqa: E402
from apps.mesh import projection as mesh_projection  # noqa: E402
from apps.meshsem import embedjob, providers, search, spaces  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures.meshsem_related import (  # noqa: E402
    Scene, World, edges as _edges, pair as _pair, pairs as _pairs, prepare as _prepare,
    relate as _relate, runs as _runs, start_here as _start_here, state as _state)

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
JOB = "mesh_related_edges"


def _mod(name: str):
    return importlib.import_module(f"apps.meshsem.{name}")


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        db.ensure_namespace(c)
        yield c


@pytest.fixture()
def app():
    """meshychat_app, verified: the role the registered job runs as."""
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    w.close()


# ======================================================================= T1 calibration

def test_t1_without_a_calibration_the_job_writes_nothing_and_calls_no_reranker(
        owner, app, world, tmp_path):
    related = _mod("related")
    world.declare(cal_path=tmp_path / "absent.json")
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _prepare(app)
    r = related.catch_up(app)
    assert r.status == "not_calibrated" and r.rows_processed == 0, r
    assert world.rr.calls("/rerank") == 0, "an uncalibrated job called the reranker"
    assert _state(owner, world.space)["last_status"] == "not_calibrated"
    # A committed-but-disabled calibration is the same state.
    world.calibrate(enabled=False)
    assert related.catch_up(app).status == "not_calibrated"
    assert world.rr.calls("/rerank") == 0
    # Positive control: calibrated, the same rows are related.
    world.calibrate(threshold=1.0, k=3)
    assert _relate(app)[-1].status == "ok"
    assert world.rr.calls("/rerank") > 0


def test_t1b_an_invalid_calibration_is_an_error_not_a_quiet_off(owner, app, world, tmp_path):
    """A calibration file that is present and WRONG is not "no calibration": the tick says
    `not_calibrated` with why, calls no model, and the registered job RAISES (E1), so a
    corrupted file cannot read as a feature that is merely switched off."""
    related = _mod("related")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    world.declare(cal_path=bad)
    r = related.catch_up(app)
    assert r.status == "not_calibrated" and r.detail.startswith("invalid calibration"), r
    with pytest.raises(related.RelatedJobFailed):
        related.run_job(app, registry.by_name(JOB))
    assert world.rr.calls("/rerank") == 0
    # Control: an ABSENT file is the quiet off, and the job does not raise.
    world.declare(cal_path=tmp_path / "absent.json")
    assert related.run_job(app, registry.by_name(JOB)).rows == 0


# ============================================================= T2 threshold, top-k, schema

def _insert_edge(cur, src, dst, **over):
    cols = dict(src_id=src["id"], src_key=src["key"], src_version=1, dst_id=dst["id"],
                dst_key=dst["key"], dst_version=1, space="s", embed_provider_id="e",
                rerank_provider_id="r", rerank_model="m", rerank_reference="a" * 64,
                threshold_version="b" * 16, score_fwd=2.0, score_rev=2.0, pair_score=2.0,
                threshold=1.0, rank=1, fused_rank=1, recorded_at=src["created_at"],
                dst_recorded_at=dst["created_at"])
    cols.update(over)
    cur.execute(f"INSERT INTO mesh_related_edges ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})", list(cols.values()))


def test_t2_edges_are_above_threshold_within_top_k_with_full_provenance(owner, app, world):
    rc = _mod("relatedcal")
    s = Scene(owner)
    _start_here(owner, world, app)
    for n in ("a1", "d1", "d2", "d3", "d4"):
        s.add(n)
    s.add("b1")
    _pair(world.rr, s, "b1", "a1", 3.0, 1.0)     # pair 2.0
    _pair(world.rr, s, "b1", "d1", 5.0, -4.0)    # pair 0.5: a high FORWARD alone is not enough
    _pair(world.rr, s, "b1", "d2", 1.5, 1.5)     # pair 1.5
    _pair(world.rr, s, "b1", "d3", 1.2, 1.2)     # pair 1.2
    _pair(world.rr, s, "b1", "d4", 1.1, 1.1)     # pair 1.1: above threshold, cut by k=3
    _prepare(app)
    reports = _relate(app)
    assert reports[-1].status == "ok", reports[-1]
    rows = _edges(owner, world.space, [s.id("b1")])
    assert [(str(r["dst_id"]), r["rank"]) for r in rows] == [
        (s.id("a1"), 1), (s.id("d2"), 2), (s.id("d3"), 3)]
    top = rows[0]
    assert (top["score_fwd"], top["score_rev"], top["pair_score"]) == (3.0, 1.0, 2.0)
    assert top["threshold"] == 1.0 and top["threshold_version"] == world.cal[
        "threshold_version"]
    assert top["edge_type"] == "related_to" and top["space"] == world.space.name
    assert top["embed_provider_id"] == "embed-a"
    assert (top["rerank_provider_id"], top["rerank_model"]) == ("rerank-a", "bge-fake")
    assert top["rerank_reference"] == world.cal["rerank_reference"]
    assert len(top["rerank_reference"]) == 64 and top["fused_rank"] >= 1
    assert (top["src_key"], top["dst_key"]) == (s.rows["b1"]["key"], s.rows["a1"]["key"])
    utc = _dt.timezone.utc                    # instants: the two reads may come back in different session zones
    assert top["recorded_at"].astimezone(utc) == s.rows["b1"]["created_at"].astimezone(utc)
    assert top["dst_recorded_at"].astimezone(utc) == s.rows["a1"]["created_at"].astimezone(utc)
    assert _runs(owner, world.space, [s.id("b1")])[0]["outcome"] == "edges"
    assert rc.load(world.cal_path).threshold_version == top["threshold_version"]

    # THE SCHEMA refuses what the job must never write, beside a legal row (control).
    with owner.cursor() as cur:
        _insert_edge(cur, s.rows["d4"], s.rows["d2"], space="schema-" + s.uid)
        for bad in (dict(pair_score=0.5),                       # below its threshold
                    dict(score_fwd=float("nan")),               # not a number
                    dict(rank=21)):                             # past the rank bound
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert_edge(cur, s.rows["d4"], s.rows["d1"], space="schema-" + s.uid,
                             **bad)
        with pytest.raises(psycopg.errors.CheckViolation):      # a self edge
            _insert_edge(cur, s.rows["d4"], s.rows["d4"], space="schema-" + s.uid)
        with pytest.raises(psycopg.errors.CheckViolation):      # older -> newer
            _insert_edge(cur, s.rows["d1"], s.rows["d4"], space="schema-" + s.uid)
        cur.execute("DELETE FROM mesh_related_edges WHERE space = %s", ("schema-" + s.uid,))


def test_t2b_every_chosen_target_gone_before_the_write_is_not_none_above_threshold(
        owner, app, world, monkeypatch):
    """The pair crossed the threshold; its target was superseded between the scoring and
    the write, so no edge is inserted. The ledger says `targets_gone`, not
    `none_above_threshold`, which would be a false statement about the scores."""
    related = _mod("related")
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("a")
    s.add("src")
    _pair(world.rr, s, "src", "a", 3.0, 3.0)
    _prepare(app)
    real = providers.rerank
    done = {}

    def superseding(query, documents, **kw):
        if query == s.text["src"] and not done:    # src's forward call: a is edited now
            with owner.cursor() as cur:
                cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s",
                            (s.rows["a"]["id"],))
                done["rows"] = cur.rowcount
        return real(query, documents, **kw)

    monkeypatch.setattr(providers, "rerank", superseding)
    r = related.catch_up(app)
    assert done == {"rows": 1} and r.status == "ok", (done, r)
    run = _runs(owner, world.space, [s.id("src")])
    assert [(x["outcome"], x["edges"]) for x in run] == [("targets_gone", 0)], run
    assert r.sources_targets_gone == 1 and r.edges_written == 0, r
    assert _state(owner, world.space)["sources_targets_gone"] == 1


# =================================================================== T3 exclusions

def test_t3_the_own_thread_the_reply_parent_and_the_children_are_excluded(
        owner, app, world):
    s = Scene(owner)
    _start_here(owner, world, app)
    src_rid = uuid.uuid4()
    later = s.base + _dt.timedelta(milliseconds=50)
    src_key = f"msg/{int(later.timestamp() * 1000)}-{src_rid.hex}"
    s.add("parent")                                   # its own thread
    s.add("sibling", thread=f"t-{s.uid}-src")         # the source's thread
    # A child OLDER than the message it answers (a sender clock): still a declared child.
    s.add("child", reply_to=src_key)
    s.add("control")
    s.add("src", thread=f"t-{s.uid}-src", reply_to="parent", when=later, rid=src_rid,
          key=src_key)
    for other in ("parent", "sibling", "child", "control"):
        _pair(world.rr, s, "src", other, 3.0, 3.0)
    _prepare(app)
    _relate(app)
    assert {str(r["dst_id"]) for r in _edges(owner, world.space, [s.id("src")])} == {
        s.id("control")}, "only the undeclared message may become a related_to edge"


# =================================================================== T4 strictly older

def test_t4_candidates_are_strictly_older_so_each_pair_is_scored_once(owner, app, world):
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("u")
    s.add("v")
    _pair(world.rr, s, "v", "u", 3.0, 3.0)
    _prepare(app)
    _relate(app)
    assert _pairs(_edges(owner, world.space, [s.id("u"), s.id("v")])) == {
        (s.id("v"), s.id("u"))}
    both = [p for p in world.rr.rerank_requests()
            if {s.text["u"], s.text["v"]} <= {p["query"], *p["documents"]}]
    # v's forward call holds u; u's reverse call scores v. u's own forward call never
    # holds v (newer), so the pair is scored once in each direction and no more.
    assert len(both) == 2, both
    assert sum(1 for p in both if p["query"] == s.text["v"]) == 1
    assert [p["documents"] for p in both if p["query"] == s.text["u"]] == [[s.text["v"]]]


# ============================================================ T5 the pool is context's

def test_t5_the_jobs_pool_is_the_context_routes_pool_restricted(owner, app, world):
    s = Scene(owner)
    _start_here(owner, world, app)
    for n in ("p1", "p2", "p3"):
        s.add(n)
    s.add("same1", thread=f"t-{s.uid}-anchor")
    s.add("anchor", thread=f"t-{s.uid}-anchor")
    _prepare(app)
    # On demand FIRST (no edge exists yet), unreranked, the whole pool as hits.
    ctx = search.context(app, world.space, s.rows["anchor"]["key"], k=search.POOL,
                         rerank=False)
    on_demand = [h["body"] for h in ctx["related"]["hits"]]
    assert s.text["same1"] not in on_demand and on_demand, ctx["related"]
    _relate(app)
    # The forward requests are the ones queried with the anchor (a reverse request queries with a candidate).
    # Since 2026-10-07 they come in chunks of at most RERANK_CHUNK; joined in order they must be the pool.
    # Filtering by "more than one document" would drop a last chunk of one.
    chunks = [p for p in world.rr.rerank_requests() if p["query"] == s.text["anchor"]]
    assert chunks and all(len(p["documents"]) <= _mod("related").RERANK_CHUNK for p in chunks), chunks
    forward = [{**chunks[0], "documents": [d for p in chunks for d in p["documents"]]}]
    # The anchor is the newest row, and has no reply neighbours, so nothing is removed.
    assert forward[0]["documents"] == on_demand


# ===================================================== T6/T7 the reranker fails mid-tick

def _nothing_written(owner, world, s, names, wm):
    ids = [s.id(n) for n in names]
    assert _edges(owner, world.space, ids) == []
    assert _runs(owner, world.space, ids) == []
    st = _state(owner, world.space)
    assert (st["watermark_at"], str(st["watermark_id"])) == (wm[0], str(wm[1])), \
        "the walk moved past rows it could not relate"
    return st


def test_t6_a_reranker_that_goes_down_mid_tick_writes_nothing(
        owner, app, world):
    related = _mod("related")
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    for n in ("a", "b", "c"):
        s.add(n)
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _pair(world.rr, s, "c", "a", 3.0, 3.0)
    _prepare(app)
    rc = _mod("relatedcal")
    # The canaries pass, the first source's forward call and one reverse call land,
    # then every call fails (503, an OUTAGE): the first source is partly scored, and a
    # partly scored source keeps nothing; b and c were never reached.
    world.rr.rerank_fail_after = len(rc.CANARY_SETS) + 2
    with pytest.raises(related.RelatedJobFailed):
        related.run_job(app, registry.by_name(JOB))
    st = _nothing_written(owner, world, s, ("a", "b", "c"), wm)
    assert st["last_status"] == "rerank_unavailable", st   # committed BEFORE the raise
    # Positive control: the reranker back, the same tick relates them.
    world.rr.rerank_fail_after = None
    assert _relate(app)[-1].status == "ok"
    assert _pairs(_edges(owner, world.space, [s.id("b"), s.id("c")])) == {
        (s.id("b"), s.id("a")), (s.id("c"), s.id("a"))}


def test_t6b_a_failed_reverse_call_refuses_the_tick_rather_than_deciding_on_one_direction(
        owner, app, world, monkeypatch):
    """Every forward call lands and every REVERSE call (the candidate as the query, the
    source as the one document) fails. Deciding an edge on the forward logit alone would
    be an unreranked write in one direction, on exactly the asymmetry the mean exists
    for: the whole tick is refused instead, and nothing is written."""
    related = _mod("related")
    rc = _mod("relatedcal")
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("x")                 # so b's FORWARD pool always holds two documents (a and x)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    real = providers.rerank
    canary_queries = {q for q, _docs in rc.CANARY_SETS}
    # A reverse call scores ONE document, the source; a forward call for a or b holds an
    # older row that is not a or b (x, at least), so the two cannot be confused.
    reversed_sources = {s.text["a"], s.text["b"]}
    failed = []

    def reverse_down(query, documents, **kw):
        if (query not in canary_queries and len(documents) == 1
                and documents[0] in reversed_sources):   # a reverse call
            failed.append(query)
            return providers.Reranked(None, None, [{"id": "rerank-a", "verdict": "failed",
                                                    "reason": "HTTP 503"}])
        return real(query, documents, **kw)

    monkeypatch.setattr(providers, "rerank", reverse_down)
    r = related.catch_up(app)
    assert failed, "control: a reverse call was made and failed"
    assert r.status == "rerank_unavailable", r
    assert not any(q == s.text["b"] for q in failed), "b's forward call was not failed"
    # A 503 is an OUTAGE: the tick stops AT a, the first source whose reverse call
    # failed. x, completed before it, is kept; a and b are not decided at all.
    assert _edges(owner, world.space, [s.id(n) for n in "xab"]) == []
    assert {str(x["src_id"]) for x in _runs(owner, world.space,
                                             [s.id(n) for n in "xab"])} == {s.id("x")}
    assert str(_state(owner, world.space)["watermark_id"]) == s.id("x")
    # Positive control: both directions answering, the same rows relate.
    monkeypatch.setattr(providers, "rerank", real)
    assert _relate(app)[-1].status == "ok"
    assert _pairs(_edges(owner, world.space, [s.id("b")])) == {(s.id("b"), s.id("a"))}


def _refuse_pairs(host, scene, pairs):
    """The fake answers HTTP 500 to any call holding one of `pairs` as (query, document),
    the way HQB answers a pair too large for its physical batch: every time."""
    bad = {(scene.text[q], scene.text[d]) for q, d in pairs}
    host.rerank_refuse = lambda query, docs: any((query, d) in bad for d in docs)


def test_t6c_a_pair_the_reranker_refuses_is_left_out_and_the_walk_moves_on(
        owner, app, world):
    """HQB's reranker answers HTTP 500 ("input (2270 tokens) is too large to process ...
    current batch size: 2048") for a pair of two dense 1,500-character texts, every time,
    and one such document fails the WHOLE forward call it is in (measured 2026-09-23,
    synthetic base64). That used to refuse every tick at the same source, so the walk
    never moved again. Now the forward call is retried one document at a time and a
    reverse call once; a pair refused again has no score and is NEVER an edge, not even
    on its forward logit alone; it is counted; the source completes on the pairs that
    were scored, and the walk moves on."""
    related = _mod("related")
    s = Scene(owner)
    _start_here(owner, world, app)
    for n in ("a", "dense", "rev_only", "src"):
        s.add(n)
    for n in ("a", "dense", "rev_only"):
        _pair(world.rr, s, "src", n, 3.0, 3.0)
    # dense: refused both ways; rev_only: forward answered, REVERSE refused.
    _refuse_pairs(world.rr, s, [("src", "dense"), ("dense", "src"), ("rev_only", "src")])
    _prepare(app)
    reports = _relate(app)
    assert reports[-1].status == "ok", reports[-1]
    assert _pairs(_edges(owner, world.space, [s.id("src")])) == {(s.id("src"), s.id("a"))}
    run = _runs(owner, world.space, [s.id("src")])
    assert [(x["outcome"], x["edges"], x["pairs_refused"]) for x in run] == [
        ("edges", 1, 2)], run
    assert sum(r.pairs_refused for r in reports) == 2, reports
    st = _state(owner, world.space)
    assert str(st["watermark_id"]) == s.id("src") and st["pairs_refused"] == 2, st
    sent = world.rr.rerank_requests()
    assert [p["documents"] for p in sent if p["query"] == s.text["src"]
            and p["documents"] == [s.text["dense"]]] == [[s.text["dense"]]], (
        "control: the refused forward call was retried one document at a time")
    assert sum(1 for p in sent if p["query"] == s.text["rev_only"]
               and p["documents"] == [s.text["src"]]) == 2, "control: reverse retried once"


def test_t6d_a_reranker_that_refuses_everything_is_an_outage_not_refused_pairs(
        owner, app, world):
    """The safety half of t6c. A pair is counted as refused and walked past ONLY when the
    pinned reranker then passes its canaries again. One that answers HTTP 500 to every
    call (a broken model, not a large input) fails them, so the tick stops at the first
    source as an outage: nothing written, the walk unmoved, the job raises."""
    related = _mod("related")
    rc = _mod("relatedcal")
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    world.rr.rerank_fail_after = len(rc.CANARY_SETS)
    world.rr.rerank_fail_mode = "refuse"        # 500 to everything, the canaries too
    with pytest.raises(related.RelatedJobFailed):
        related.run_job(app, registry.by_name(JOB))
    st = _nothing_written(owner, world, s, ("a", "b"), wm)
    assert st["last_status"] == "rerank_unavailable", st
    # Positive control: the reranker healthy again, the same rows relate.
    world.rr.rerank_fail_after = None
    assert _relate(app)[-1].status == "ok"
    assert _pairs(_edges(owner, world.space, [s.id("b")])) == {(s.id("b"), s.id("a"))}


def test_t6e_an_outage_keeps_the_sources_completed_before_it(owner, app, world, monkeypatch):
    """The reranker goes down at c's forward call, after b was fully scored. b's decision
    is complete and was made by the pinned reranker, so it is written; the tick stops AT
    c (the watermark on b), reports rerank_unavailable, and the job raises. Before, b's
    finished work was thrown away with the tick and, while the failure lasted, every tick
    was refused with the walk unmoved."""
    related = _mod("related")
    s = Scene(owner)
    _start_here(owner, world, app)
    for n in ("a", "b", "c"):
        s.add(n)
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _pair(world.rr, s, "c", "a", 3.0, 3.0)
    _prepare(app)
    real = providers.rerank

    def down_at_c(query, documents, **kw):
        if query == s.text["c"]:
            return providers.Reranked(None, None, [{"id": "rerank-a", "verdict": "failed",
                                                    "reason": "HTTP 503"}])
        return real(query, documents, **kw)

    monkeypatch.setattr(providers, "rerank", down_at_c)
    with pytest.raises(related.RelatedJobFailed):
        related.run_job(app, registry.by_name(JOB))
    names = [s.id(n) for n in "abc"]
    assert _pairs(_edges(owner, world.space, names)) == {(s.id("b"), s.id("a"))}
    assert {str(x["src_id"]) for x in _runs(owner, world.space, names)} == {
        s.id("a"), s.id("b")}
    st = _state(owner, world.space)
    assert (str(st["watermark_id"]), st["last_status"]) == (s.id("b"), "rerank_unavailable")
    monkeypatch.setattr(providers, "rerank", real)
    assert _relate(app)[-1].status == "ok"
    assert (s.id("c"), s.id("a")) in _pairs(_edges(owner, world.space, names))


def test_t7_a_short_rerank_answer_refuses_the_whole_tick(owner, app, world):
    related = _mod("related")
    rc = _mod("relatedcal")
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    world.rr.rerank_fail_after = len(rc.CANARY_SETS)
    world.rr.rerank_fail_mode = "short"
    r = related.catch_up(app)
    assert r.status == "rerank_unavailable", r
    _nothing_written(owner, world, s, ("a", "b"), wm)


# ==================================================== T8/T9 admission and the provider pin

def test_t8_a_reranker_with_shifted_canaries_is_refused_and_failover_admits(
        owner, app, world):
    related = _mod("related")
    rc = _mod("relatedcal")
    shifted = world.host(rerank_mode="shifted")
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _pair(shifted, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    canary_queries = {q for q, _docs in rc.CANARY_SETS}

    world.declare(rerankers=[("rerank-shifted", shifted)])
    r = related.catch_up(app)
    assert r.status == "rerank_not_admitted", r
    assert {a["verdict"] for a in r.attempts} == {"not_admitted"}, r.attempts
    assert {p["query"] for p in shifted.rerank_requests()} <= canary_queries, (
        "mesh text was sent to a reranker that failed its canaries")
    _nothing_written(owner, world, s, ("a", "b"), wm)

    world.declare(rerankers=[("rerank-shifted", shifted), ("rerank-a", world.rr)])
    r = _relate(app)[-1]
    assert r.status == "ok" and r.rerank_provider_id == "rerank-a", r
    rows = _edges(owner, world.space, [s.id("b")])
    assert [x["rerank_provider_id"] for x in rows] == ["rerank-a"]
    assert {p["query"] for p in shifted.rerank_requests()} <= canary_queries


def test_t9_no_failover_inside_a_tick_a_provider_switch_aborts_it(
        owner, app, world, monkeypatch):
    related = _mod("related")
    rc = _mod("relatedcal")
    backup = world.host()
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _pair(backup, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    world.declare(rerankers=[("rerank-a", world.rr), ("rerank-b", backup)])
    world.rr.rerank_fail_after = len(rc.CANARY_SETS) + 1
    r = related.catch_up(app)
    assert r.status == "rerank_unavailable" and r.rerank_provider_id == "rerank-a", r
    assert backup.calls("/rerank") == 0, "the tick failed over to a provider it never admitted"
    _nothing_written(owner, world, s, ("a", "b"), wm)

    # The defensive branch: an answer attributed to a provider other than the pinned one.
    world.rr.rerank_fail_after = None
    real = providers.rerank
    canary_queries = {q for q, _docs in rc.CANARY_SETS}

    def impostor(query, documents, **kw):
        got = real(query, documents, **kw)
        if query in canary_queries:
            return got
        return dataclasses.replace(got, provider_id="rerank-b")

    monkeypatch.setattr(providers, "rerank", impostor)
    r = related.catch_up(app)
    assert r.status == "rerank_provider_changed", r
    _nothing_written(owner, world, s, ("a", "b"), wm)


# ============================================================ T10 the walk's upper bound

def test_t10_the_walk_never_passes_either_watermark(owner, app, world):
    related = _mod("related")
    # The world's space has never been embedded: no vector walk, nothing to relate.
    r = related.catch_up(app)
    assert r.status == "embed_not_run" and r.rows_processed == 0, r
    assert world.rr.calls("/rerank") == 0

    s = Scene(owner)
    # One row of history, embedded, so the embed watermark EXISTS: without it the job
    # answers embed_not_run and "read nothing" below would pass for the wrong reason (the
    # mutation run caught exactly that when this test ran alone on an empty namespace).
    s.add("history")
    _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    for _ in range(50):                        # projected, NOT embedded
        if not mesh_projection.catch_up(app).more:
            break
    r = related.catch_up(app)
    assert r.status == "ok", r                 # control: the walk ran, and...
    assert r.rows_read == 0, "the walk passed the embed watermark"
    for _ in range(50):                        # embedded; c is embedded but NOT projected
        if not embedjob.catch_up(app).more:
            break
    s.add("c")
    _pair(world.rr, s, "c", "a", 3.0, 3.0)
    for _ in range(50):
        r = embedjob.catch_up(app)
        if not r.more or r.rows_processed == 0:
            break
    _relate(app)
    assert _pairs(_edges(owner, world.space, [s.id("b"), s.id("c")])) == {
        (s.id("b"), s.id("a"))}, "the walk passed the mesh_edges watermark"

    with owner.cursor() as cur:
        cur.execute("SELECT watermark_at, watermark_id FROM mesh_edges_projection "
                    "WHERE projection = 'mesh_edges'")
        saved = cur.fetchone()
        cur.execute("UPDATE mesh_edges_projection SET watermark_at = NULL, "
                    "watermark_id = NULL WHERE projection = 'mesh_edges'")
    try:
        r = related.catch_up(app)
        assert r.status == "projection_not_run" and r.rows_processed == 0, r
    finally:
        with owner.cursor() as cur:
            cur.execute("UPDATE mesh_edges_projection SET watermark_at = %s, "
                        "watermark_id = %s WHERE projection = 'mesh_edges'", saved)


# ================================================================== T11/T12 re-runs

def _snapshot(rows):
    keep = ("src_id", "dst_id", "rank", "fused_rank", "score_fwd", "score_rev",
            "pair_score", "threshold", "threshold_version", "rerank_provider_id")
    return sorted(tuple(str(r[k]) for k in keep) for r in rows)


def test_t11_a_rerun_is_idempotent(owner, app, world):
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    for n in ("a", "b", "c"):
        s.add(n)
    _pair(world.rr, s, "c", "a", 3.0, 2.0)
    _pair(world.rr, s, "c", "b", 1.5, 1.5)
    _pair(world.rr, s, "b", "a", 1.2, 1.4)
    _prepare(app)
    _relate(app)
    ids = [s.id(n) for n in ("a", "b", "c")]
    first = _snapshot(_edges(owner, world.space, ids))
    assert len(first) == 3
    with owner.cursor() as cur:                      # walk the same rows again
        cur.execute("UPDATE mesh_related_projection SET watermark_at = %s, "
                    "watermark_id = %s WHERE space = %s", (wm[0], wm[1], world.space.name))
    _relate(app)
    assert _snapshot(_edges(owner, world.space, ids)) == first
    assert len(_runs(owner, world.space, ids)) == 3


def test_t12_a_concurrent_walker_makes_the_tick_write_nothing(
        owner, app, world, monkeypatch):
    related = _mod("related")
    rc = _mod("relatedcal")
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    real = providers.rerank
    canary_queries = {q for q, _docs in rc.CANARY_SETS}
    moved = {}

    def racing(query, documents, **kw):
        if query not in canary_queries and not moved:
            with owner.cursor() as cur:           # another walker advances the walk
                # From a NULL start too (D21, 2026-09-27): NULL + 1 us is NULL, so run alone
                # (-k t12_a, a fresh space whose embed walk has no watermark yet) this
                # "race" changed nothing and the tick rightly did not raise. One clock:
                # the database's.
                cur.execute("UPDATE mesh_related_projection SET watermark_at = "
                            "COALESCE(watermark_at, clock_timestamp()) "
                            "+ interval '1 microsecond', "
                            "watermark_id = COALESCE(watermark_id, gen_random_uuid()) "
                            "WHERE space = %s RETURNING watermark_at", (world.space.name,))
                moved["to"] = cur.fetchone()[0]
        return real(query, documents, **kw)

    monkeypatch.setattr(providers, "rerank", racing)
    with pytest.raises(related.ConcurrentWalk):
        related.catch_up(app)
    assert moved and moved["to"] is not None, f"control: the race moved the walk: {moved}"
    assert _edges(owner, world.space, [s.id("b")]) == []
    assert _runs(owner, world.space, [s.id("a"), s.id("b")]) == []
    st = _state(owner, world.space)
    assert st["last_status"] == "concurrent_walk" and st["watermark_at"] == moved["to"]


def test_t12b_a_source_deleted_under_the_tick_rolls_it_back_as_concurrent_walk(
        owner, app, world, monkeypatch):
    """Retention (or any delete) removes a SOURCE between its scoring and the write. The
    write's insert then names a message that no longer exists: the ForeignKeyViolation
    rolls the WHOLE tick back (no edge, no ledger row, the walk unmoved), the tick is
    recorded as concurrent_walk and raises, and the next tick recomputes without it."""
    related = _mod("related")
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    real = providers.rerank
    gone = {}

    def deleting(query, documents, **kw):
        if query == s.text["b"] and not gone:      # b's forward call: b is deleted now
            with owner.cursor() as cur:
                cur.execute("DELETE FROM shared_memory WHERE id = %s", (s.rows["b"]["id"],))
                gone["rows"] = cur.rowcount
        return real(query, documents, **kw)

    monkeypatch.setattr(providers, "rerank", deleting)
    with pytest.raises(related.ConcurrentWalk):
        related.catch_up(app)
    assert gone == {"rows": 1}, "control: the delete was injected"
    # a was decided in the same tick; its ledger row went with the rollback.
    st = _nothing_written(owner, world, s, ("a",), wm)
    assert st["last_status"] == "concurrent_walk", st
    monkeypatch.setattr(providers, "rerank", real)
    assert _relate(app)[-1].status == "ok"
    assert len(_runs(owner, world.space, [s.id("a")])) == 1
    assert _edges(owner, world.space, [s.id("a")]) == []



@pytest.mark.parametrize("column,value", [("forbidden_rows_open", True),
                                          ("threshold_version", "f" * 24)])
def test_t12c_a_walker_under_another_gate_or_calibration_makes_the_tick_write_nothing(
        owner, app, world, monkeypatch, column, value):
    """Two walkers that both restarted (a gate flip, a new calibration) share a NULL
    watermark, so the watermark comparison cannot see the other one; the write must also
    compare the gate and the threshold_version it read. Simulated with the watermark
    left where it was: only those two comparisons can refuse the write."""
    related = _mod("related")
    rc = _mod("relatedcal")
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    real = providers.rerank
    canary_queries = {q for q, _docs in rc.CANARY_SETS}
    moved = {}

    def racing(query, documents, **kw):
        if query not in canary_queries and not moved:
            with owner.cursor() as cur:
                cur.execute(f"UPDATE mesh_related_projection SET {column} = %s "
                            "WHERE space = %s", (value, world.space.name))
                moved["rows"] = cur.rowcount
        return real(query, documents, **kw)

    monkeypatch.setattr(providers, "rerank", racing)
    with pytest.raises(related.ConcurrentWalk):
        related.catch_up(app)
    assert moved == {"rows": 1}, "control: the other walker's state was injected"
    st = _nothing_written(owner, world, s, ("a", "b"), wm)
    assert st["last_status"] == "concurrent_walk", st


# ==================================================================== T13 the gate

def _anchor(app, key):
    from apps.core import dbconn as _dbc
    with _dbc.request_server_internal(app) as c:
        return search._anchor(c, key)


def test_t13_closing_the_gate_hides_at_once_purges_next_tick_and_opening_restarts(
        owner, app, world):
    related = _mod("related")
    reads = _mod("related_reads")
    world.declare(gate=True)
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("f", policy="forbidden")
    s.add("a")
    s.add("src")
    _pair(world.rr, s, "src", "f", 3.0, 3.0)
    _pair(world.rr, s, "src", "a", 2.0, 2.0)
    _prepare(app)
    _relate(app)
    assert _pairs(_edges(owner, world.space, [s.id("src")])) == {
        (s.id("src"), s.id("f")), (s.id("src"), s.id("a"))}
    anchor = _anchor(app, s.rows["src"]["key"])
    open_view = reads.for_context(app, world.space, anchor, gate=True)
    assert {h["key"] for h in open_view["hits"]} == {s.rows["f"]["key"], s.rows["a"]["key"]}

    world.declare(gate=False)
    # HIDDEN AT ONCE: the read filters both ends by the gate before any job runs.
    closed_view = reads.for_context(app, world.space, anchor, gate=False)
    assert {h["key"] for h in closed_view["hits"]} == {s.rows["a"]["key"]}
    assert len(_edges(owner, world.space, [s.id("src")])) == 2, "control: not yet purged"
    # PURGED by the next tick, and the walk restarts.
    r = related.catch_up(app)
    assert r.gate_changed and not r.gate_open and r.purged >= 1, r
    assert _pairs(_edges(owner, world.space, [s.id("src")])) == {(s.id("src"), s.id("a"))}
    assert _state(owner, world.space)["forbidden_rows_open"] is False

    world.declare(gate=True)
    r = related.catch_up(app)
    assert r.gate_changed and r.gate_open and r.purged == 0, r
    assert _state(owner, world.space)["forbidden_rows_open"] is True


def test_t13b_a_gate_closed_under_a_warm_index_never_sends_a_forbidden_row(
        owner, app, world):
    """The gate closes while the job process still holds a forbidden row's vector in its
    in-process index (search.VectorIndex drops a deleted vector only when pruned, every
    FULL_RELOAD_S). The embed walk has purged the row from the TABLES, so the job's own
    gate check is then the only thing between that row's text and the reranker: it is
    never sent, as a source or as a candidate, and no edge or ledger row touches it."""
    from apps.core import rls
    related = _mod("related")
    world.declare(gate=True)
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    s.add("f", policy="forbidden")
    s.add("a")
    _prepare(app)                                  # f is embedded while the gate is OPEN
    idx = search.index_for(world.space)
    with rls.server_internal(app) as c:
        idx.refresh(c, ceiling=search._ceiling())
        search.lexical_index_for(world.space).refresh(c, ceiling=search._ceiling())
    assert idx.vector_of(s.id("f")) is not None, "control: f is in the warm index"
    s.add("src")
    s.add("f2", policy="forbidden")                # a forbidden SOURCE, the newest row
    _pair(world.rr, s, "src", "f", 3.0, 3.0)
    _pair(world.rr, s, "src", "a", 2.0, 2.0)
    _pair(world.rr, s, "f2", "a", 3.0, 3.0)
    # CLOSE the gate without World.declare, which would drop the in-process indexes.
    with override_settings(MESHY_EMBED_FORBIDDEN_ROWS=False):
        _prepare(app)                              # the embed walk purges f, restarts
        with owner.cursor() as cur:                # the related walk resumes at wm, closed
            cur.execute("UPDATE mesh_related_projection SET forbidden_rows_open = FALSE, "
                        "watermark_at = %s, watermark_id = %s WHERE space = %s",
                        (wm[0], wm[1], world.space.name))
        before = len(world.rr.rerank_requests())
        reports = _relate(app)
    assert reports[-1].status == "ok", reports[-1]
    assert idx.vector_of(s.id("f")) is not None, "control: the index was still warm"
    sent = world.rr.rerank_requests()[before:]
    assert any(p["query"] == s.text["src"] for p in sent), "control: src was reranked"
    for p in sent:
        for name in ("f", "f2"):
            assert s.text[name] != p["query"] and s.text[name] not in p["documents"], (
                f"the text of forbidden row {name} was sent to the reranker")
    assert _pairs(_edges(owner, world.space, [s.id("src"), s.id("f2")])) == {
        (s.id("src"), s.id("a"))}
    assert _runs(owner, world.space, [s.id("f"), s.id("f2")]) == []
    assert sum(r.skipped_forbidden for r in reports) == 2, reports



def _related_under_open_gate(owner, app, world, s):
    """f (forbidden, oldest), then src: src -> f written while the gate is OPEN."""
    world.declare(gate=True)
    _start_here(owner, world, app)
    s.add("f", policy="forbidden")
    s.add("src")
    _pair(world.rr, s, "src", "f", 3.0, 3.0)
    _prepare(app)
    _relate(app)
    assert _pairs(_edges(owner, world.space, [s.id("src")])) == {
        (s.id("src"), s.id("f"))}, "control: the edge exists"


def test_t13c_closing_the_gate_purges_even_with_the_calibration_off(owner, app, world):
    """The purge is level-triggered and runs BEFORE the calibration check: a calibration
    that is off (so the tick returns not_calibrated) does not keep rows derived from a
    forbidden message's content."""
    related = _mod("related")
    s = Scene(owner)
    _related_under_open_gate(owner, app, world, s)
    world.calibrate(enabled=False)
    world.declare(gate=False)
    r = related.catch_up(app)
    assert r.status == "not_calibrated" and r.purged == 1, r
    assert _edges(owner, world.space, [s.id("src")]) == []
    assert world.rr.calls("/rerank") > 0                    # control: it ran while open


def test_t13d_closing_the_gate_purges_another_spaces_edges(owner, app, world, monkeypatch):
    """A tick for the CONFIGURED space purges forbidden-derived rows in every space: a
    space that is no longer configured (its walk never runs again) keeps none."""
    related = _mod("related")
    s = Scene(owner)
    _related_under_open_gate(owner, app, world, s)
    other = db.make_space("rel2", raw=True)
    monkeypatch.setitem(spaces.SPACES, other.name, other)
    world.declare(gate=False)
    r = related.catch_up(app, space=other)           # calibrated for world.space, not this
    assert r.status == "not_calibrated" and r.purged == 1, r
    assert _edges(owner, world.space, [s.id("src")]) == []


def test_t13e_closing_the_gate_purges_a_forbidden_sources_edges_and_ledger_row(
        owner, app, world):
    """T13 closes the gate on a forbidden TARGET. The SOURCE end too: a forbidden newer
    message's outgoing edges and its ledger row go with the first closed tick."""
    related = _mod("related")
    world.declare(gate=True)
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("old")
    s.add("f2", policy="forbidden")
    _pair(world.rr, s, "f2", "old", 3.0, 3.0)
    _prepare(app)
    _relate(app)
    assert _pairs(_edges(owner, world.space, [s.id("f2")])) == {(s.id("f2"), s.id("old"))}
    assert len(_runs(owner, world.space, [s.id("f2")])) == 1, "control: f2 was decided"
    world.declare(gate=False)
    r = related.catch_up(app)
    assert r.gate_changed and r.purged == 1, r
    assert _edges(owner, world.space, [s.id("f2")]) == []
    assert _runs(owner, world.space, [s.id("f2")]) == [], "a forbidden source's ledger row"
    assert len(_runs(owner, world.space, [s.id("old")])) == 1, "an allowed source's row stays"


# ================================================================= T14 retention

def test_t14_a_delete_of_either_end_cascades_as_the_app_role_in_a_request(
        owner, app, world):
    s = Scene(owner)
    _start_here(owner, world, app)
    for n in ("d1", "d2", "src"):
        s.add(n)
    _pair(world.rr, s, "src", "d1", 3.0, 3.0)
    _pair(world.rr, s, "src", "d2", 2.0, 2.0)
    _prepare(app)
    _relate(app)
    assert len(_edges(owner, world.space, [s.id("src")])) == 2
    assert len(_runs(owner, world.space, [s.id("src")])) == 1
    with dbconn.request_server_internal(app) as c, c.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (s.rows["d1"]["id"],))
        assert cur.rowcount == 1
    assert _pairs(_edges(owner, world.space, [s.id("src")])) == {(s.id("src"), s.id("d2"))}
    with dbconn.request_server_internal(app) as c, c.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (s.rows["src"]["id"],))
    assert _edges(owner, world.space, [s.id("src")]) == []
    assert _runs(owner, world.space, [s.id("src")]) == []


# ======================================================== T15 a new threshold version

def test_t15_a_new_threshold_version_drops_the_old_edges_and_restarts(owner, app, world):
    related = _mod("related")
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    _relate(app)
    old = world.cal["threshold_version"]
    assert [r["threshold_version"] for r in _edges(owner, world.space, [s.id("b")])] == [old]
    world.calibrate(threshold=1.5, k=3)
    assert world.cal["threshold_version"] != old
    r = related.catch_up(app)
    assert r.threshold_changed, r
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM mesh_related_edges WHERE space = %s "
                    "AND threshold_version = %s", (world.space.name, old))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM mesh_related_runs WHERE space = %s "
                    "AND threshold_version = %s", (world.space.name, old))
        assert cur.fetchone()[0] == 0
    assert _state(owner, world.space)["threshold_version"] == world.cal["threshold_version"]


# ================================================================ T16 the runner

def test_t16_the_registered_job_runs_through_the_runner_as_the_app_role(owner, app, world):
    related = _mod("related")
    base = registry.by_name(JOB)
    assert (base.interval_secs, base.timeout_secs, base.max_batch) == (
        60, 45, related.DEFAULT_BATCH)
    assert base.run is related.run_job
    names = [j.name for j in registry.JOBS]
    assert names.index(JOB) == names.index("mesh_embed_catch_up") + 1
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    job = registry.Job(name="test_related_" + uuid.uuid4().hex[:10],
                       interval_secs=base.interval_secs, timeout_secs=base.timeout_secs,
                       jitter_frac=base.jitter_frac, max_batch=base.max_batch, run=base.run)

    def connect(**kw):
        return dbconn.connect(DSN, **kw)

    try:
        out = runner.run_due(connect, jobs=(job,), holder="t")[0]
        assert out.result == "ok" and out.rows >= 2, out
        assert _pairs(_edges(owner, world.space, [s.id("b")])) == {(s.id("b"), s.id("a"))}
    finally:
        with owner.cursor() as cur:
            cur.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job.name,))


# ================================================================ T17 the deadline

def test_t17_the_deadline_stops_the_tick_on_a_source_boundary(owner, app, world, monkeypatch):
    related = _mod("related")
    s = Scene(owner)
    _start_here(owner, world, app)
    for n in ("a", "b", "c"):
        s.add(n)
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _pair(world.rr, s, "c", "a", 3.0, 3.0)
    _prepare(app)
    late = {"now": False}
    monkeypatch.setattr(related, "_clock", lambda: 1e9 if late["now"] else 0.0)
    real = providers.rerank

    def slow(query, documents, **kw):
        got = real(query, documents, **kw)
        if query == s.text["b"]:
            late["now"] = True                   # the deadline passes DURING source b
        return got

    monkeypatch.setattr(providers, "rerank", slow)
    r = related.catch_up(app, deadline_s=10.0)
    assert r.status == "deadline" and r.more, r
    # a (no older candidate that crosses) and b completed; c was never started.
    assert _pairs(_edges(owner, world.space, [s.id("b"), s.id("c")])) == {
        (s.id("b"), s.id("a"))}
    assert {str(x["src_id"]) for x in _runs(owner, world.space,
                                             [s.id(n) for n in "abc"])} == {
        s.id("a"), s.id("b")}
    assert str(_state(owner, world.space)["watermark_id"]) == s.id("b")
    late["now"] = False
    _relate(app)
    assert (s.id("c"), s.id("a")) in _pairs(_edges(owner, world.space, [s.id("c")]))


def test_t17b_a_tick_reads_at_most_max_batch_and_reranks_at_most_relate_per_tick(
        owner, app, world):
    """The two per-tick bounds (B5): `max_batch` caps the rows READ, `relate_per_tick` the
    sources RERANKED. A tick stopped by the second says `relate_bound`, not an error, and
    the watermark moves past exactly the rows it finished."""
    related = _mod("related")
    s = Scene(owner)
    _start_here(owner, world, app)
    for n in ("a", "b", "c", "d"):
        s.add(n)
    for n in ("b", "c", "d"):
        _pair(world.rr, s, n, "a", 3.0, 3.0)
    _prepare(app)
    names = [s.id(n) for n in "abcd"]
    r = related.catch_up(app, max_batch=2)
    assert (r.status, r.rows_read, r.rows_processed, r.more) == ("ok", 2, 2, True), r
    assert str(_state(owner, world.space)["watermark_id"]) == s.id("b")
    r = related.catch_up(app, relate_per_tick=1)
    assert (r.status, r.stopped, r.rows_read, r.rows_processed, r.more) == (
        "relate_bound", "relate_bound", 2, 1, True), r
    assert {str(x["src_id"]) for x in _runs(owner, world.space, names)} == {
        s.id("a"), s.id("b"), s.id("c")}, "d was reranked past the bound"
    st = _state(owner, world.space)
    assert (str(st["watermark_id"]), st["last_status"]) == (s.id("c"), "relate_bound")
    _relate(app)
    assert _pairs(_edges(owner, world.space, names)) == {
        (s.id(n), s.id("a")) for n in "bcd"}



def test_t17c_a_spent_source_budget_is_an_outage_never_a_refused_pair(
        owner, app, world, monkeypatch):
    """One source's calls SHARE its budget. A reranker too slow to finish a source inside
    it (every call answers, each within its own timeout) stops the tick at that source
    as an outage: nothing written for it, no pair counted as refused, the walk unmoved.
    Budget 1 s, each call 0.3 s, a source makes 11 calls."""
    related = _mod("related")
    rc = _mod("relatedcal")
    s = Scene(owner)
    wm = _start_here(owner, world, app)
    s.add("a")
    s.add("src")
    _pair(world.rr, s, "src", "a", 3.0, 3.0)
    _prepare(app)
    canary_queries = {q for q, _docs in rc.CANARY_SETS}
    real = world.rr._rerank

    def slow(h, payload):
        if payload.get("query") not in canary_queries:
            time.sleep(0.3)
        return real(h, payload)

    world.rr._rerank = slow
    monkeypatch.setattr(related, "SOURCE_BUDGET_S", 1.0)
    r = related.catch_up(app)
    assert r.status == "rerank_unavailable" and r.pairs_refused == 0, r
    _nothing_written(owner, world, s, ("a", "src"), wm)


# ====================================================== T18 no inference is evidence

def test_t18_an_inferred_thread_does_not_exclude_and_other_edges_shape_nothing(
        owner, app, world):
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("x")
    s.add("y")
    s.add("cand")
    s.add("src")
    _pair(world.rr, s, "src", "cand", 3.0, 3.0)
    _prepare(app)
    with owner.cursor() as cur:
        # The auditor INFERRED that cand is in src's thread: not a declaration.
        cur.execute("INSERT INTO mesh_inferred_edges (src_id, src_key, src_version, "
                    "edge_type, dst, model, provider_id, prompt_version, recorded_at) "
                    "VALUES (%s, %s, 1, 'in_thread', %s, 'm', 'p', 'v', %s)",
                    (s.rows["cand"]["id"], s.rows["cand"]["key"], s.rows["src"]["thread"],
                     s.rows["cand"]["created_at"]))
        # And another related_to edge, which must neither steer nor be touched.
        _insert_edge(cur, s.rows["y"], s.rows["x"], space=world.space.name,
                     threshold_version=world.cal["threshold_version"],
                     pair_score=9.0, score_fwd=9.0, score_rev=9.0)
    _relate(app)
    assert _pairs(_edges(owner, world.space, [s.id("src")])) == {
        (s.id("src"), s.id("cand"))}, "an inferred thread excluded a candidate"
    y = _edges(owner, world.space, [s.id("y")])
    # y was processed as a source, so ITS outgoing edges were rewritten from scores; the
    # planted 9.0 is gone because nothing supports it, not because anything read it.
    assert all(r["pair_score"] != 9.0 for r in y)


def _sql_literals(path: Path) -> list[str]:
    """Every string literal in the module EXCEPT docstrings: prose may name a table it
    says is never read; code may not read it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docs.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_t18_the_job_reads_no_inference_and_the_auditor_never_names_the_table():
    lits = _sql_literals(ROOT / "apps" / "meshsem" / "related.py")
    assert any("mesh_related_edges" in x for x in lits), "control: the scan sees the SQL"
    for x in lits:
        assert "mesh_inferred_edges" not in x, x
        if x == JOB:
            continue             # the registry name of the job, not SQL
        if "mesh_related_edges" in x:
            head = x.lstrip().split(None, 1)[0].upper()
            assert head in ("DELETE", "INSERT"), (
                f"related.py may only DELETE or INSERT its own edges, never read them: {x}")
    for p in sorted((ROOT / "apps" / "meshaudit").rglob("*.py")):
        assert "mesh_related_edges" not in p.read_text(encoding="utf-8"), p


def test_an_edge_hit_takes_its_body_and_rounding_from_search(monkeypatch):
    """ONE body rule and ONE rounding for every related hit, edge or ranked (divergence
    027): related_reads uses search.body_fields and search._r6 rather than copies that
    could drift. Kills: a re-implemented body rule or a local _r6 in related_reads."""
    reads = _mod("related_reads")
    assert reads._r6 is search._r6
    monkeypatch.setattr(search, "body_fields", lambda raw: (f"<{raw}>", "sentinel"))
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    row = {"key": "msg/x", "version": 1, "created_at": now, "author_label": "a",
           "thread_id": None, "direction": "out", "pair_score": 1.23456789,
           "score_fwd": None, "score_rev": 0.5, "cosine": None, "lexical_score": None,
           "fused_rank": None, "rank": 1, "space": "s", "rerank_provider_id": "r",
           "rerank_model": "m", "threshold_version": "t", "inferred_at": now, "body": " b "}
    h = reads._hit(row, with_body=True)
    assert (h["body"], h["body_truncated"]) == ("< b >", "sentinel")
    assert h["scores"]["pair"] == 1.234568 and h["scores"]["forward"] is None
    assert "body" not in reads._hit(row, with_body=False)


# ======================================================================= the CLI

def test_the_cli_status_and_show_run_as_the_app_role(owner, app, world, capsys):
    cli = _mod("related_cli")
    s = Scene(owner)
    _start_here(owner, world, app)
    s.add("a")
    s.add("b")
    _pair(world.rr, s, "b", "a", 3.0, 3.0)
    _prepare(app)
    assert cli.main(["--dsn", DSN, "catch-up", "--until-done"]) == 0
    assert cli.main(["--dsn", DSN, "status"]) == 0
    assert cli.main(["--dsn", DSN, "show", "--key", s.rows["b"]["key"]]) == 0
    out = capsys.readouterr().out
    assert world.cal["threshold_version"] in out and s.rows["a"]["key"] in out, out
