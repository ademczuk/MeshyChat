"""The calibration of persisted related_to edges, as arithmetic. Divergence 032.

No database and no network: everything here is a pure function over the committed synthetic
fixture (apps/meshsem/calibration/related_judged_v1.json) or over a scripted score table.

What is held:
  * the labels are FROZEN: their digest is the one committed before any scoring code
    existed (git history: the fixture commit precedes every scoring commit);
  * the development / held-out split is by INCIDENT, deterministic and disjoint, so pairs
    from one incident never sit on both sides (they share vocabulary);
  * every pair the fixture claims shares no token really shares none, under the SAME
    tokenizer the lexical arm uses (apps/meshsem/lexical.terms);
  * the metric arithmetic, on a table small enough to check by hand;
  * the selection rule is the one fixed in advance (lowest threshold on a 0.5-logit grid
    whose Wilson 95% lower bound of precision at k=10 is >= 0.70, then the smallest k
    keeping 95% of k=10's grade-2 recall), not whatever looks best afterwards;
  * the ship gate compares against the no-model LEXICAL floor at the same edge count;
  * the whole decision (relatedcal.decide, which the harness runs) takes its threshold
    and k from the DEVELOPMENT split only, and an under-determined threshold ships off;
  * the committed calibration's digests match its content, and a hand edit of anything
    in it (enabled, status and the report included) is refused;
  * the committed calibration is for the space a deployment serves by default
    (apps/meshsem/spaces.DEFAULT_SPACE), so the default cannot change without it.

Each module is imported inside the test that needs it, so against a tree without the
feature every test fails on its own ImportError rather than the file failing to collect.

Run:  pytest tests/test_meshsem_related_calibration.py -q
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "apps" / "meshsem" / "calibration" / "related_judged_v1.json"
LABELS = ROOT / "apps" / "meshsem" / "calibration" / "related_judged_v1.labels.sha256"
#: The digest committed in 82bba03, the commit that froze the labels before any scoring.
FROZEN_LABELS = "90420cd8ccb03a6bd75433a37f159da72ec926a9c927fd430c1ab0d47a1c23ee"


def _cal():
    return importlib.import_module("apps.meshsem.relatedcal")


# ================================================================= the frozen fixture

def test_the_label_digest_is_frozen():
    rc = _cal()
    fx = rc.load_fixture()
    assert LABELS.read_text(encoding="utf-8").strip() == FROZEN_LABELS
    assert rc.labels_digest(fx) == FROZEN_LABELS, (
        "the labels changed after they were frozen: a new label set is a NEW fixture "
        "(related_judged_v2), never an edit of v1")
    # C2: the digest must move when one grade moves, or it guards nothing.
    anchor = next(a for a, pairs in fx["labels"]["anchors"].items() if pairs)
    first = fx["labels"]["anchors"][anchor][0]
    first["grade"] = 3 - first["grade"]                  # 2 <-> 1
    assert rc.labels_digest(fx) != FROZEN_LABELS


def test_the_fixture_is_what_the_design_says():
    rc = _cal()
    fx = rc.load_fixture()
    msgs = fx["messages"]
    assert len(msgs) == 96
    incidents = {m["incident"] for m in msgs}
    real = {i for i in incidents if not i.startswith("neg-")}
    assert len(real) == 12 and len(incidents - real) == 8
    for inc in real:
        assert len({m["thread"] for m in msgs if m["incident"] == inc}) >= 2, inc
    by_id = {m["id"]: m for m in msgs}
    anchors = fx["labels"]["anchors"]
    assert len(anchors) >= 32
    for a, pairs in anchors.items():
        for p in pairs:
            d = by_id[p["dst"]]
            assert d["t"] < by_id[a]["t"] and d["thread"] != by_id[a]["thread"], (a, p)
            assert p["grade"] in (1, 2) and p["relation"] in rc.RELATIONS, (a, p)


def test_every_zero_overlap_claim_is_true_under_the_lexical_tokenizer():
    rc = _cal()
    lexical = importlib.import_module("apps.meshsem.lexical")
    fx = rc.load_fixture()
    pairs = rc.zero_overlap_pairs(fx)
    assert len(pairs) >= 8, "the design asks for at least 8 zero-overlap grade-2 pairs"
    text = {m["id"]: m["text"] for m in fx["messages"]}
    for a, d in pairs:
        shared = set(lexical.terms(text[a])[0]) & set(lexical.terms(text[d])[0])
        assert not shared, (a, d, shared)
    # Control: an ordinary grade-2 pair DOES share tokens, so the check can see overlap.
    assert set(lexical.terms(text["L4"])[0]) & set(lexical.terms(text["L1"])[0])


def test_the_split_is_by_incident_deterministic_and_disjoint():
    rc = _cal()
    fx = rc.load_fixture()
    dev, held = rc.anchors_in(fx, "dev"), rc.anchors_in(fx, "held_out")
    assert dev and held and not set(dev) & set(held)
    assert set(dev) | set(held) == set(fx["labels"]["anchors"])
    inc = {m["id"]: m["incident"] for m in fx["messages"]}
    assert not {inc[a] for a in dev} & {inc[a] for a in held}, "an incident on both sides"
    # Deterministic: the rule is sha256(incident) parity, recomputed here independently.
    import hashlib
    for a in dev:
        assert int(hashlib.sha256(inc[a].encode()).hexdigest(), 16) % 2 == 0
    assert rc.anchors_in(rc.load_fixture(), "dev") == dev


# ================================================================ metric arithmetic

def _mini():
    """Two anchors, hand-checkable. a1 (thread A2) is judged against x1, x2, x3."""
    msgs = [
        {"id": "x1", "thread": "T1", "incident": "i1", "author": "p", "t": 1, "text": "one"},
        {"id": "x2", "thread": "T2", "incident": "i2", "author": "q", "t": 2, "text": "two"},
        {"id": "x3", "thread": "T3", "incident": "i3", "author": "p", "t": 3, "text": "three"},
        {"id": "a1", "thread": "TA", "incident": "i1", "author": "q", "t": 4, "text": "four"},
        {"id": "a2", "thread": "TB", "incident": "i2", "author": "p", "t": 5, "text": "five"},
    ]
    labels = {"a1": [{"dst": "x1", "grade": 2, "relation": "same_incident"},
                     {"dst": "x3", "grade": 1, "relation": "follow_up"}],
              "a2": [{"dst": "x2", "grade": 2, "relation": "answers"},
                     {"dst": "x1", "grade": 2, "relation": "same_incident"}]}
    return {"messages": msgs, "labels": {"anchors": labels}}


#: anchor -> pool, forward order: (dst, forward, reverse, fused_rank)
SCORED = {
    "a1": [{"dst": "x1", "fwd": 3.0, "rev": 1.0, "fused_rank": 1},   # pair 2.0  grade 2
           {"dst": "x2", "fwd": 2.0, "rev": -1.0, "fused_rank": 2},  # pair 0.5  grade 0
           {"dst": "x3", "fwd": 1.0, "rev": 1.0, "fused_rank": 3}],  # pair 1.0  grade 1
    "a2": [{"dst": "x2", "fwd": 0.0, "rev": 0.0, "fused_rank": 1},   # pair 0.0  grade 2
           {"dst": "x3", "fwd": 4.0, "rev": 0.0, "fused_rank": 2},   # pair 2.0  grade 0
           {"dst": "x1", "fwd": -3.0, "rev": -3.0, "fused_rank": 3}],  # pair -3  grade 2
}


def test_the_metric_arithmetic_on_a_scripted_table():
    rc = _cal()
    fx = _mini()
    m = rc.evaluate(SCORED, fx, ["a1", "a2"], threshold=1.0, k=10)
    # Written: a1->x1 (2.0, g2), a1->x3 (1.0, g1), a2->x3 (2.0, g0).
    assert m["written"] == 3 and m["correct_ge1"] == 2 and m["correct_g2"] == 1
    assert m["precision_ge1"] == pytest.approx(2 / 3)
    assert m["eligible_g2"] == 3 and m["recall_g2"] == pytest.approx(1 / 3)
    # x3 is written by both anchors: a hub of in-degree 2.
    assert m["max_in_degree"] == 2 and m["out_degree"] == {"a1": 2, "a2": 1}
    # k cuts by pair score, ties by fused order: at k=1 a1 keeps x1 only.
    m1 = rc.evaluate(SCORED, fx, ["a1", "a2"], threshold=1.0, k=1)
    assert m1["written"] == 2 and m1["correct_ge1"] == 1
    # The aggregator is the MEAN of both directions; forward-only is reported, never
    # shipped, and here it would write a2->x3 for a forward score of 4 and nothing else.
    fwd = rc.evaluate(SCORED, fx, ["a2"], threshold=1.0, k=10, aggregator="forward")
    assert fwd["written"] == 1 and fwd["correct_ge1"] == 0
    # reverse_depth: a candidate never reverse-scored cannot be written.
    shallow = rc.evaluate(SCORED, fx, ["a1"], threshold=0.9, k=10, reverse_depth=1)
    assert shallow["written"] == 1
    # A repeated anchor (a bootstrap resample) counts twice.
    twice = rc.evaluate(SCORED, fx, ["a1", "a1"], threshold=1.0, k=10)
    assert twice["written"] == 4 and twice["eligible_g2"] == 2


def test_wilson_lower_bound():
    rc = _cal()
    assert rc.wilson_lower(0, 0) == 0.0
    assert rc.wilson_lower(10, 10) == pytest.approx(0.7225, abs=1e-3)
    assert rc.wilson_lower(40, 50) == pytest.approx(0.6696, abs=1e-3)
    assert rc.wilson_lower(5, 10) < rc.wilson_lower(50, 100) < 0.5


def test_the_selection_rule_is_the_one_fixed_in_advance():
    """Lowest grid threshold whose Wilson LB at k=10 is >= 0.70; then the smallest k with
    >= 95% of k=10's grade-2 recall. Built so a HIGHER threshold looks better on
    precision: the rule must still take the lowest that qualifies."""
    rc = _cal()
    msgs, labels, scored = [], {}, {}
    for i in range(30):
        msgs.append({"id": f"d{i}", "thread": f"D{i}", "incident": f"j{i}", "author": "p",
                     "t": i, "text": "x"})
        msgs.append({"id": f"n{i}", "thread": f"N{i}", "incident": f"k{i}", "author": "p",
                     "t": 100 + i, "text": "x"})
    for i in range(30):
        a = f"a{i}"
        msgs.append({"id": a, "thread": f"A{i}", "incident": f"j{i}", "author": "q",
                     "t": 200 + i, "text": "x"})
        labels[a] = [{"dst": f"d{i}", "grade": 2, "relation": "same_incident"}]
        # the true pair scores 3.0; a false pair scores 1.2 on a third of the anchors
        scored[a] = [{"dst": f"d{i}", "fwd": 3.0, "rev": 3.0, "fused_rank": 1}]
        if i % 3 == 0:
            scored[a].append({"dst": f"n{i}", "fwd": 1.2, "rev": 1.2, "fused_rank": 2})
    fx = {"messages": msgs, "labels": {"anchors": labels}}
    anchors = sorted(labels)
    # At t <= 1.0: 30 correct of 40 written (LB ~0.60): refused. At t in (1.2, 3.0]: 30/30.
    assert rc.wilson_lower(30, 40) < 0.70 <= rc.wilson_lower(30, 30)
    t = rc.select_threshold(scored, fx, anchors)
    assert t == 1.5, t
    assert rc.select_k(scored, fx, anchors, t) == 1
    # And a table where nothing qualifies selects nothing (the feature stays off).
    bad = {a: [{"dst": f"n{int(a[1:])}", "fwd": 5, "rev": 5, "fused_rank": 1}]
           for a in anchors}
    assert rc.select_threshold(bad, fx, anchors) is None


def _split_names(rc, n: int) -> tuple[list[str], list[str]]:
    """n incident names that fall on each side of the split."""
    dev, held, i = [], [], 0
    while len(dev) < n or len(held) < n:
        name = f"inc{i}"
        (dev if rc.split_of(name) == "dev" else held).append(name)
        i += 1
    return dev[:n], held[:n]


def _decide_world(rc, *, held_false: float, n: int = 30) -> tuple[dict, dict]:
    """DEVELOPMENT anchors: two grade-2 pairs (3.0, 2.9) and two false pairs at 1.2, so
    the development split alone selects threshold 1.5 and k 2. HELD-OUT anchors: one
    grade-2 pair (3.0) and two false pairs at `held_false`: at 2.2 the held-out split
    alone would select 2.5, and k 1 at 1.5; at 1.2 it agrees with development."""
    dev_inc, held_inc = _split_names(rc, n)
    msgs, labels, scored = [], {}, {}
    clock = [0]

    def msg(mid, inc):
        clock[0] += 1
        msgs.append({"id": mid, "thread": f"T{mid}", "incident": inc, "author": "p",
                     "t": clock[0], "text": "x"})

    for part, incs, false in (("d", dev_inc, 1.2), ("h", held_inc, held_false)):
        for i, inc in enumerate(incs):
            g2 = [f"{part}g{i}_{j}" for j in range(2 if part == "d" else 1)]
            neg = [f"{part}n{i}_{j}" for j in range(2)]
            for m in g2 + neg:
                msg(m, inc)
            a = f"{part}a{i}"
            msg(a, inc)
            labels[a] = [{"dst": d, "grade": 2, "relation": "same_incident"} for d in g2]
            scored[a] = ([{"dst": d, "fwd": 3.0 - 0.1 * r, "rev": 3.0 - 0.1 * r,
                           "fused_rank": r + 1} for r, d in enumerate(g2)]
                         + [{"dst": d, "fwd": false, "rev": false,
                             "fused_rank": len(g2) + r + 1} for r, d in enumerate(neg)])
    return {"messages": msgs, "labels": {"anchors": labels}}, scored


def test_the_decision_is_taken_on_the_development_split_only():
    """The harness's decision (relatedcal.decide) selects the threshold AND k on the
    development split; the held-out split is only the ship gate. Built so the held-out
    split alone would pick another threshold (2.5) and another k (1)."""
    rc = _cal()
    fx, scored = _decide_world(rc, held_false=2.2)
    held = rc.anchors_in(fx, "held_out")
    assert rc.select_threshold(scored, fx, held) == 2.5, "control: held-out disagrees"
    assert rc.select_k(scored, fx, held, 1.5) == 1, "control: held-out disagrees on k"
    out = rc.decide(scored, fx, bootstrap_n=20)
    d = out["decision"]
    assert (d["threshold"], d["k"]) == (1.5, 2), d
    # ...and the held-out gate then refuses what development chose (30 of 60 correct).
    assert (d["enabled"], d["status"]) == (False, "failed_held_out_gate"), d
    assert out["held_out"]["written"] == 60 and out["held_out"]["correct_ge1"] == 30


def test_an_under_determined_threshold_ships_disabled(monkeypatch):
    """A 1,000-resample incident bootstrap whose 90% interval is WIDER than 2 logits is
    under-determined: the calibration ships `enabled: false` whatever the gate says. A
    width of exactly 2.0 is not wider (the committed run sits on it)."""
    rc = _cal()
    fx, scored = _decide_world(rc, held_false=1.2)
    d = rc.decide(scored, fx, bootstrap_n=20)["decision"]
    assert (d["enabled"], d["status"], d["threshold"]) == (True, "calibrated", 1.5), d
    for width, enabled in ((2.0, True), (2.5, False)):
        monkeypatch.setattr(rc, "bootstrap_threshold",
                            lambda *a, w=width, **k: {"n": 20, "selected": 20, "none": 0,
                                                      "p05": 0.5, "p95": 0.5 + w,
                                                      "width": w, "median": 1.5})
        d = rc.decide(scored, fx, bootstrap_n=20)["decision"]
        assert (d["enabled"], d["status"]) == (
            (True, "calibrated") if enabled else (False, "under_determined")), (width, d)


def test_the_ship_gate_compares_against_the_lexical_floor():
    rc = _cal()
    good = {"written": 40, "correct_ge1": 36, "recall_g2": 0.60}
    floor = {"written": 40, "recall_g2": 0.50}
    ok, why = rc.ship_gate(good, floor)
    assert ok, why
    # Precision fine, recall BELOW the no-model floor at the same edge count: refused.
    ok, why = rc.ship_gate({**good, "recall_g2": 0.45}, floor)
    assert not ok and any("lexical floor" in w for w in why), why
    # Recall fine, the precision lower bound under 0.60: refused.
    ok, why = rc.ship_gate({**good, "correct_ge1": 25}, floor)
    assert not ok and any("lower bound" in w for w in why), why


def test_the_lexical_floor_takes_the_same_number_of_edges():
    rc = _cal()
    fx = rc.load_fixture()
    held = rc.anchors_in(fx, "held_out")
    f = rc.lexical_floor(fx, held, n_edges=30, k=5)
    assert f["written"] == 30
    assert 0.0 <= f["recall_g2"] <= 1.0 and f["eligible_g2"] > 0
    assert rc.lexical_floor(fx, held, n_edges=10, k=5)["written"] == 10


# ============================================================ the calibration document

def test_a_calibration_document_digests_its_decision(tmp_path):
    rc = _cal()
    refs = [[float(i) for i in range(len(docs))] for _q, docs in rc.CANARY_SETS]
    doc = rc.build(space="s", threshold=1.5, k=4, canary_references=refs,
                   rerank_model="m")
    assert doc["threshold_version"] == rc.threshold_version_of(doc)
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    cal = rc.load(path)
    assert (cal.threshold, cal.k, cal.space) == (1.5, 4, "s")
    # A hand edit of the threshold without a new digest is refused, not believed.
    doc2 = dict(doc, threshold=0.5)
    path.write_text(json.dumps(doc2), encoding="utf-8")
    with pytest.raises(rc.CalibrationInvalid):
        rc.load(path)
    # Two thresholds are two versions.
    assert rc.build(space="s", threshold=2.0, k=4, canary_references=refs,
                    rerank_model="m")["threshold_version"] != doc["threshold_version"]
    # Disabled and absent both read as "no calibration".
    path.write_text(json.dumps(rc.build(space="s", threshold=1.5, k=4,
                                        canary_references=refs, rerank_model="m",
                                        enabled=False)), encoding="utf-8")
    assert rc.load(path) is None
    assert rc.load(tmp_path / "absent.json") is None


def test_a_hand_edit_of_enabled_status_or_the_report_is_refused(tmp_path):
    """threshold_version digests what decides WHICH edges are written; `enabled`, `status`
    and the report sit outside it, so flipping a calibration that FAILED its gate to
    `enabled: true` used to load. The whole document is digested now (content_sha256).
    Switching one OFF by hand still reads as off: that edit fails closed."""
    rc = _cal()
    refs = [[float(i) for i in range(len(docs))] for _q, docs in rc.CANARY_SETS]
    failed = rc.build(space="s", threshold=1.5, k=4, canary_references=refs,
                      rerank_model="m", enabled=False, status="failed_held_out_gate",
                      report={"decision": {"enabled": False}})
    path = tmp_path / "cal.json"
    flipped = dict(failed, enabled=True, status="calibrated")
    assert rc.threshold_version_of(flipped) == failed["threshold_version"], (
        "control: threshold_version cannot see the flip")
    path.write_text(json.dumps(flipped), encoding="utf-8")
    with pytest.raises(rc.CalibrationInvalid):
        rc.load(path)
    good = rc.build(space="s", threshold=1.5, k=4, canary_references=refs,
                    rerank_model="m", report={"held_out": {"recall_g2": 0.3}})
    path.write_text(json.dumps(dict(good, report={"held_out": {"recall_g2": 0.9}})),
                    encoding="utf-8")
    with pytest.raises(rc.CalibrationInvalid):
        rc.load(path)
    path.write_text(json.dumps(good), encoding="utf-8")
    assert rc.load(path) is not None                        # control: unedited, it loads
    path.write_text(json.dumps(dict(good, enabled=False)), encoding="utf-8")
    assert rc.load(path) is None                            # a hand OFF is still off


def test_a_canary_edit_with_recomputed_digests_but_a_stale_reference_is_refused(tmp_path):
    """rerank_reference, stamped on every edge, digests the canary sentences and their
    reference logits. A canary edit that recomputes both document digests but not it is
    refused: the admission check would otherwise run against logits no edge names."""
    rc = _cal()
    refs = [[float(i) for i in range(len(docs))] for _q, docs in rc.CANARY_SETS]
    doc = rc.build(space="s", threshold=1.5, k=4, canary_references=refs, rerank_model="m")
    doc["canaries"]["sets"][0]["reference"][0] += 0.5
    doc["threshold_version"] = rc.threshold_version_of(doc)
    doc["content_sha256"] = rc.content_digest(doc)
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(rc.CalibrationInvalid, match="rerank_reference"):
        rc.load(path)


def test_a_reverse_depth_below_k_is_refused(tmp_path):
    """An edge needs both directions scored, so the job must reverse-score at least k."""
    rc = _cal()
    refs = [[float(i) for i in range(len(docs))] for _q, docs in rc.CANARY_SETS]
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(rc.build(space="s", threshold=1.5, k=5, reverse_depth=3,
                                        canary_references=refs, rerank_model="m")),
                    encoding="utf-8")
    with pytest.raises(rc.CalibrationInvalid, match="reverse_depth"):
        rc.load(path)


def test_the_committed_calibration_matches_its_content():
    rc = _cal()
    assert rc.CALIBRATION_PATH.exists(), "the HQB calibration run must be committed"
    doc = json.loads(rc.CALIBRATION_PATH.read_text(encoding="utf-8"))
    cal = rc.load(rc.CALIBRATION_PATH)
    assert cal is not None and cal.threshold_version == doc["threshold_version"]
    fx = rc.load_fixture()
    assert doc["fixture"]["sha256"] == rc.fixture_digest(fx)
    assert doc["fixture"]["labels_sha256"] == FROZEN_LABELS
    assert doc["threshold_version"] == rc.threshold_version_of(doc)
    assert doc["aggregator"] == "mean"
    assert len(doc["canaries"]["sets"]) == len(rc.CANARY_SETS)
    for got, (q, docs) in zip(doc["canaries"]["sets"], rc.CANARY_SETS):
        assert got["query"] == q and got["documents"] == list(docs)
        assert len(got["reference"]) == len(docs)
    if doc["enabled"]:
        rep = doc["report"]
        assert rep["held_out"]["precision_ge1_lb"] >= 0.60
        assert rep["held_out"]["recall_g2"] >= rep["lexical_floor"]["recall_g2"]
        assert rep["bootstrap"]["width"] <= 2.0


def test_the_committed_calibration_is_for_the_space_served_by_default(monkeypatch):
    """The job and the reads use a calibration only when its `space` is the configured
    space (apps/meshsem/related.py, related_reads.py); otherwise every source is
    `not_calibrated` and no related_to edge is written or served. So the committed
    document must be for the DEFAULT space, and changing one without the other ships the
    links switched off. Divergence 026, amended 2026-09-26: both are the retrieval-prompt
    space. Held three ways: the document's field, what `load` hands the job, and what an
    unconfigured deployment resolves."""
    from django.test import override_settings

    rc = _cal()
    spaces = importlib.import_module("apps.meshsem.spaces")
    store = importlib.import_module("apps.meshsem.store")
    doc = json.loads(rc.CALIBRATION_PATH.read_text(encoding="utf-8"))
    assert doc["space"] == spaces.DEFAULT_SPACE, (doc["space"], spaces.DEFAULT_SPACE)
    cal = rc.load(rc.CALIBRATION_PATH)
    assert cal is not None, "the committed calibration for the default space is enabled"
    assert cal.space == spaces.DEFAULT_SPACE
    monkeypatch.delenv("MESHY_SEMANTIC_SPACE", raising=False)
    with override_settings(MESHY_SEMANTIC_SPACE=None):
        assert store.configured_space().name == cal.space


# ============================================================== relation typing (off)

def _typing_set(n_incidents=10):
    """Per incident one pair of each of three relations; the heuristic floor says
    same_incident every time, so it is right on a third of them."""
    gold, heur, inc = [], [], []
    for i in range(n_incidents):
        for rel in ("same_incident", "follow_up", "answers"):
            gold.append(rel)
            heur.append("same_incident")
            inc.append(f"i{i}")
    return gold, heur, inc


def test_the_typing_gate_is_the_one_fixed_in_advance():
    """Gemma relation typing ships only when ALL hold on its evaluation set: the incident
    bootstrap 95% lower bound of (its macro-F1 - the best floor's) is above 0, at most 2%
    of its answers are invalid or truncated, and when it says `contradicts` it is right
    at least 80% of the time. The floors are the majority class and the no-model
    heuristic. A point estimate above the floors is NOT enough."""
    rc = _cal()
    gold, heur, inc = _typing_set()
    v = rc.typing_verdict(gold, list(gold), heur, inc)
    assert v["enabled"] is True and v["why"] == [], v            # control: a model that knows
    assert v["macro_f1"]["model"] == pytest.approx(1.0)
    assert v["macro_f1"]["heuristic"] == pytest.approx(1 / 6)
    assert v["macro_f1"]["majority"] == pytest.approx(1 / 6)
    assert v["bootstrap_lb95_model_minus_best_floor"] > 0
    # No better than the heuristic: refused.
    v = rc.typing_verdict(gold, list(heur), heur, inc)
    assert v["enabled"] is False and any("lower bound" in w for w in v["why"]), v
    # One invalid answer in 30 is 3.3% > 2%: refused, and the bootstrap cannot save it.
    pred = list(gold)
    pred[0] = None
    v = rc.typing_verdict(gold, pred, heur, inc)
    assert v["invalid_or_truncated_rate"] == pytest.approx(1 / 30)
    assert v["enabled"] is False and any("invalid" in w for w in v["why"]), v
    # `contradicts` said once, wrongly: precision 0 < 0.8, refused.
    pred = list(gold)
    pred[1] = "contradicts"
    v = rc.typing_verdict(gold, pred, heur, inc)
    assert v["contradicts_emitted"] == 1 and v["contradicts_precision"] == 0.0
    assert v["enabled"] is False and any("contradicts" in w for w in v["why"]), v
    # Better on ONE incident of three and no better on the other two: the point estimate
    # beats the floor, but about 30% of resamples hold no pair of that incident, so the
    # lower bound is 0. The lower bound, not the point estimate, decides.
    g3, h3, i3 = _typing_set(n_incidents=3)
    pred = g3[:3] + h3[3:]
    v = rc.typing_verdict(g3, pred, h3, i3)
    assert v["macro_f1"]["model"] > v["macro_f1"]["heuristic"] + 0.3
    assert v["bootstrap_lb95_model_minus_best_floor"] <= 0
    assert v["enabled"] is False and any("lower bound" in w for w in v["why"]), v
    # Nothing to judge is not a pass.
    assert rc.typing_verdict([], [], [], [])["enabled"] is False


def test_the_committed_calibration_keeps_relation_typing_off():
    """The HQB run's typing report is below its bar, and nothing in the code types an
    edge: no table holds a type and the job writes none."""
    rc = _cal()
    doc = json.loads(rc.CALIBRATION_PATH.read_text(encoding="utf-8"))
    typing = doc["report"].get("typing")
    assert typing is not None, "the HQB run measured typing (--typing)"
    assert typing["enabled"] is False
    related_src = (ROOT / "apps" / "meshsem" / "related.py").read_text(encoding="utf-8")
    assert "mesh_related_types" not in related_src and "typing" not in related_src
