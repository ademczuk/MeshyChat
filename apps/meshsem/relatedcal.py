"""Calibration of the persisted related_to edges: the rule that decides an edge, the
metrics, the selection rule fixed in advance, and the committed calibration document.
Divergence 032. PURE: no database and no model call happens in this module.

THE EDGE RULE (`select_edges`), used by the job (apps/meshsem/related.py) and by every
metric below, so what is measured is what ships:

  1. the pool is the fused candidate list, each with a FORWARD logit (the source as the
     reranker's query);
  2. the top `reverse_depth` by forward score (ties: fused order) are scored in REVERSE
     (the candidate as the query, the source as the one document);
  3. pair = mean(forward, reverse). Fixed before any data: HQB's reranker is strongly
     asymmetric (A->B +2.545, B->A -1.342 on one paraphrase pair), so one direction would
     decide edges largely on which message happened to be the query. `min` and
     `forward` are computed for the report and never shipped;
  4. keep pair >= threshold; the top `k` by pair (ties: fused order) are the edges.

THE SELECTION RULE (`decide`, which the harness runs). Written before the HQB run by its
author; what the record itself shows is narrower (docs/DIVERGENCES.md 032 "Provenance"):
the run's own report stamps these constants, so the code that decided used them, and the
code was committed eleven minutes after the run.
  * the fixture is split by INCIDENT (sha256(incident) parity), never by pair: pairs of one
    incident share vocabulary, and a pair split would leak it across the split;
  * on the DEVELOPMENT split: the lowest threshold on a 0.5-logit grid whose Wilson 95%
    lower bound of precision (grade >= 1, k = 10) is >= 0.70; then k = the smallest k whose
    grade-2 recall is >= 0.95 x the grade-2 recall at k = 10;
  * a 1,000-resample INCIDENT bootstrap on the development split reports the spread of the
    selected threshold; a 90% interval wider than 2 logits is `under_determined` and the
    calibration ships DISABLED;
  * the SHIP GATE, on HELD-OUT: the Wilson lower bound of precision (grade >= 1) >= 0.60,
    AND grade-2 recall >= the no-model LEXICAL floor's at the same number of edges (the
    lexical arm alone, top k per anchor, the globally best-scoring pairs up to the model's
    edge count). Failing either ships `enabled: false`: the job then records
    `not_calibrated` and writes nothing.

THE CALIBRATION DOCUMENT is committed JSON (apps/meshsem/calibration/
related_calibration.json). `threshold_version` is a digest of every field that decides
WHICH edges are written (threshold, k, reverse depth, aggregator, the reranker canaries
with their reference logits and tolerance, the fixture and label digests, the space, the
lexical derivation, the RRF constant and the pool size), the way
apps/meshaudit/prompt.PROMPT_VERSION is a digest of its template; it is stamped on every
edge. `content_sha256` digests the WHOLE document, so `enabled`, `status` and the report,
which decide WHETHER edges are written, are covered too. `load` refuses a document either
digest does not match: a hand edit that does not recompute them is refused, except one
that only switches the calibration off, which reads as off. These are digests, not
signatures: they catch an edit, not an editor who recomputes them.

WHAT `load` DOES NOT CHECK: the fixture. The document carries the fixture's and the
labels' digests (inside threshold_version), but `load` runs on every read and does not
re-read the 96-message fixture. That the committed calibration digests the committed
fixture is checked by tests/test_meshsem_related_calibration.py; the harness refuses to
score a fixture whose labels do not match the frozen digest.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from pathlib import Path

from apps.core import env

HERE = Path(__file__).resolve().parent
FIXTURE_PATH = HERE / "calibration" / "related_judged_v1.json"
LABELS_PATH = HERE / "calibration" / "related_judged_v1.labels.sha256"
CALIBRATION_PATH = HERE / "calibration" / "related_calibration.json"

SCHEMA = "meshychat/related-calibration/v1"
#: The setting (or environment variable) that points at another calibration file. Tests
#: use it. A deployment needs it only when it pins MESHY_SEMANTIC_SPACE to a space other
#: than the committed calibration's (apps/meshsem/README.md, "OPERATIONS NOTE").
SETTING = "MESHY_RELATED_CALIBRATION"

RELATIONS = ("same_incident", "follow_up", "answers", "contradicts")
AGGREGATORS = ("mean", "min", "forward")
SHIPPED_AGGREGATOR = "mean"

#: -12 .. +12 logits in 0.5 steps. HQB's reranker put unrelated pairs near -11 and a
#: paraphrase near +2.5, so the grid spans both with room.
GRID = tuple(x / 2 for x in range(-24, 25))
K_MAX = 10
K_RECALL_KEEP = 0.95
MIN_LB_DEV = 0.70
MIN_LB_HELD_OUT = 0.60
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260923
BOOTSTRAP_MAX_WIDTH = 2.0
DEFAULT_REVERSE_DEPTH = 10
#: How far (in logits) a reranker's canary answers may sit from the reference and still be
#: the same model. Measured on HQB: repeats and reorderings 0.0; one document alone versus
#: inside a batch 0.0038. 0.05 is 13x that, and far under a different model's distance.
RERANK_CANARY_TOL = 0.05
#: Hard bound on edges per source (the table's rank CHECK is 1..20).
K_CEILING = 20

#: The reranker's identity check: fixed SYNTHETIC sentences, one multi-document set and one
#: single-document set, because the two call shapes measured 0.0038 apart on HQB and the
#: job makes both (forward: many documents; reverse: one).
CANARY_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("the nightly job lost its lease because the renewal shared a slow connection",
     ("renewal of the job lease timed out behind a slow statement",
      "heartbeats moved to their own connection so leases stop expiring",
      "the tide comes in twice a day on most coastlines",
      "a peer rotates its signing key with a co-signed handover",
      "croissants bake longer when the batch is bigger",
      "which tenant can read a row under row level security")),
    ("the gateway certificate expired at midnight",
     ("the TLS certificate on the gateway lapsed overnight",)),
)

#: Characters of each message the typing prompt carries (divergence 021's context budget:
#: two texts plus the instructions stay near 1,800 tokens of gemma-4's 2,816).
TYPING_CHARS = 700


class CalibrationInvalid(RuntimeError):
    """The calibration file exists and is not a calibration this code may use."""


# ------------------------------------------------------------------ digests and the fixture

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def sha256(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def load_fixture(path: Path = FIXTURE_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def labels_digest(fx: dict) -> str:
    """The FROZEN digest: grades and relations, plus each message's incident and thread
    (the split and the closed world depend on both). Parsed JSON, not file bytes, so a
    checkout that rewrote line endings still verifies."""
    return sha256({"anchors": fx["labels"]["anchors"],
                   "incident_of": {m["id"]: m["incident"] for m in fx["messages"]},
                   "thread_of": {m["id"]: m["thread"] for m in fx["messages"]}})


def fixture_digest(fx: dict) -> str:
    return sha256(fx)


def split_of(incident: str) -> str:
    return "dev" if int(hashlib.sha256(incident.encode("utf-8")).hexdigest(), 16) % 2 == 0 \
        else "held_out"


def anchors_in(fx: dict, part: str) -> list[str]:
    inc = {m["id"]: m["incident"] for m in fx["messages"]}
    return sorted(a for a in fx["labels"]["anchors"] if split_of(inc[a]) == part)


def grades(fx: dict) -> dict[str, dict[str, int]]:
    """anchor -> {dst: grade}; closed world, so a dst not listed is grade 0."""
    return {a: {p["dst"]: int(p["grade"]) for p in pairs}
            for a, pairs in fx["labels"]["anchors"].items()}


def relations(fx: dict) -> dict[tuple[str, str], str]:
    return {(a, p["dst"]): p["relation"]
            for a, pairs in fx["labels"]["anchors"].items() for p in pairs}


def zero_overlap_pairs(fx: dict) -> list[tuple[str, str]]:
    return [(a, p["dst"]) for a, pairs in fx["labels"]["anchors"].items() for p in pairs
            if p.get("zero_overlap")]


# ------------------------------------------------------------------------ the edge rule

def pair_value(fwd: float, rev: float | None, aggregator: str = SHIPPED_AGGREGATOR
               ) -> float | None:
    if aggregator == "forward":
        return float(fwd)
    if rev is None:
        return None
    if aggregator == "mean":
        return (float(fwd) + float(rev)) / 2.0
    if aggregator == "min":
        return min(float(fwd), float(rev))
    raise ValueError(f"unknown aggregator {aggregator!r}; known: {AGGREGATORS}")


def reverse_targets(pool: list[dict], depth: int) -> list[int]:
    """Indexes into `pool` to score in reverse: the top `depth` by forward score, ties by
    fused order. The job asks the reranker for exactly these."""
    order = sorted(range(len(pool)), key=lambda i: (-float(pool[i]["fwd"]),
                                                    int(pool[i]["fused_rank"])))
    return order[:max(0, int(depth))]


def select_edges(pool: list[dict], *, threshold: float, k: int,
                 aggregator: str = SHIPPED_AGGREGATOR,
                 reverse_depth: int | None = None) -> list[dict]:
    """THE rule. `pool`: dicts with `fwd`, `rev` (None when not reverse-scored) and
    `fused_rank`. Returns the chosen candidates, best first, each with `pair` and `rank`
    added (1-based). `reverse_depth` SIMULATES the job's depth on a pool where every
    candidate was reverse-scored (the calibration does that to measure what the depth
    costs); the job passes None because it only reverse-scores the depth it uses."""
    allowed = None if reverse_depth is None else set(reverse_targets(pool, reverse_depth))
    kept = []
    for i, c in enumerate(pool):
        rev = c.get("rev")
        if allowed is not None and i not in allowed:
            rev = None
        p = pair_value(c["fwd"], rev, aggregator)
        if p is None or not math.isfinite(p) or p < threshold:
            continue
        kept.append(dict(c, pair=p))
    kept.sort(key=lambda c: (-c["pair"], int(c["fused_rank"])))
    out = kept[:max(0, int(k))]
    for r, c in enumerate(out, start=1):
        c["rank"] = r
    return out


# ------------------------------------------------------------------------ the metrics

def wilson_lower(successes: int, n: int, z: float = 1.959964) -> float:
    """Wilson score interval, lower end. 0 for an empty sample."""
    if n <= 0:
        return 0.0
    p = successes / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + z2 / n))


def _length_bucket(text: str) -> str:
    n = len(text or "")
    return "short(<80)" if n < 80 else ("medium(<160)" if n < 160 else "long")


def evaluate(scored: dict, fx: dict, anchors: list[str], *, threshold: float, k: int,
             aggregator: str = SHIPPED_AGGREGATOR,
             reverse_depth: int | None = None) -> dict:
    """Precision and recall of the edges `select_edges` writes for `anchors` (a list: a
    bootstrap resample repeats anchors and each repeat counts)."""
    g = grades(fx)
    text = {m["id"]: m.get("text", "") for m in fx["messages"]}
    zero = set(zero_overlap_pairs(fx))
    written = correct1 = correct2 = eligible2 = zero_eligible = zero_hit = 0
    out_degree: dict[str, int] = {}
    in_degree: dict[str, int] = {}
    by_len: dict[str, dict[str, int]] = {}
    for a in anchors:
        chosen = select_edges(scored.get(a, []), threshold=threshold, k=k,
                              aggregator=aggregator, reverse_depth=reverse_depth)
        truth = g.get(a, {})
        got = {c["dst"] for c in chosen}
        written += len(chosen)
        out_degree[a] = len(chosen)
        bucket = by_len.setdefault(_length_bucket(text.get(a, "")),
                                   {"written": 0, "correct_ge1": 0})
        for c in chosen:
            gr = truth.get(c["dst"], 0)
            correct1 += gr >= 1
            correct2 += gr == 2
            in_degree[c["dst"]] = in_degree.get(c["dst"], 0) + 1
            bucket["written"] += 1
            bucket["correct_ge1"] += gr >= 1
        for dst, gr in truth.items():
            if gr == 2:
                eligible2 += 1
                if (a, dst) in zero:
                    zero_eligible += 1
                    zero_hit += dst in got
    return {
        "anchors": len(anchors), "threshold": threshold, "k": k, "aggregator": aggregator,
        "written": written, "correct_ge1": correct1, "correct_g2": correct2,
        "precision_ge1": correct1 / written if written else 0.0,
        "precision_ge1_lb": wilson_lower(correct1, written),
        "precision_g2": correct2 / written if written else 0.0,
        "eligible_g2": eligible2, "recall_g2": correct2 / eligible2 if eligible2 else 0.0,
        "zero_overlap_eligible": zero_eligible,
        "zero_overlap_recall": zero_hit / zero_eligible if zero_eligible else 0.0,
        "out_degree": out_degree,
        "max_in_degree": max(in_degree.values()) if in_degree else 0,
        "by_length": by_len,
    }


def select_threshold(scored: dict, fx: dict, anchors: list[str], *, grid=GRID,
                     k: int = K_MAX, min_lb: float = MIN_LB_DEV,
                     aggregator: str = SHIPPED_AGGREGATOR,
                     reverse_depth: int | None = None) -> float | None:
    """The LOWEST grid threshold whose Wilson lower bound of precision (grade >= 1) at `k`
    reaches `min_lb`. None when no threshold does: the feature stays off."""
    for t in sorted(grid):
        m = evaluate(scored, fx, anchors, threshold=t, k=k, aggregator=aggregator,
                     reverse_depth=reverse_depth)
        if m["written"] and m["precision_ge1_lb"] >= min_lb:
            return t
    return None


def select_k(scored: dict, fx: dict, anchors: list[str], threshold: float, *,
             kmax: int = K_MAX, keep: float = K_RECALL_KEEP,
             aggregator: str = SHIPPED_AGGREGATOR,
             reverse_depth: int | None = None) -> int:
    """The smallest k whose grade-2 recall is at least `keep` x the recall at `kmax`."""
    top = evaluate(scored, fx, anchors, threshold=threshold, k=kmax,
                   aggregator=aggregator, reverse_depth=reverse_depth)["recall_g2"]
    for k in range(1, kmax + 1):
        r = evaluate(scored, fx, anchors, threshold=threshold, k=k, aggregator=aggregator,
                     reverse_depth=reverse_depth)["recall_g2"]
        if r >= keep * top - 1e-12:
            return k
    return kmax


def bootstrap_threshold(scored: dict, fx: dict, anchors: list[str], *,
                        n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED,
                        reverse_depth: int | None = None) -> dict:
    """Resample the development INCIDENTS with replacement and re-run the selection; the
    spread of the chosen threshold says whether the fixture determines it."""
    import numpy as np

    inc = {m["id"]: m["incident"] for m in fx["messages"]}
    by_inc: dict[str, list[str]] = {}
    for a in anchors:
        by_inc.setdefault(inc[a], []).append(a)
    keys = sorted(by_inc)
    rng = np.random.default_rng(seed)
    picks: list[float] = []
    none = 0
    for _ in range(n):
        sample = [a for i in rng.integers(0, len(keys), len(keys)) for a in by_inc[keys[i]]]
        t = select_threshold(scored, fx, sample, reverse_depth=reverse_depth)
        if t is None:
            none += 1
        else:
            picks.append(t)
    if not picks:
        return {"n": n, "selected": 0, "none": none, "p05": None, "p95": None,
                "width": None}
    arr = np.asarray(picks)
    p05, p95 = float(np.percentile(arr, 5)), float(np.percentile(arr, 95))
    return {"n": n, "selected": len(picks), "none": none, "p05": p05, "p95": p95,
            "width": p95 - p05, "median": float(np.median(arr))}


def lexical_floor(fx: dict, anchors: list[str], *, n_edges: int, k: int) -> dict:
    """The NO-MODEL floor: the lexical arm alone (TF-IDF cosine over the fixture's own
    texts, apps/meshsem/lexical), the same candidate rules as the job (strictly older,
    another thread, not a declared reply neighbour), top `k` per anchor, then the
    `n_edges` best-scoring pairs overall: the same number of edges the model wrote."""
    from . import lexical
    from .embedjob import document_text

    msgs = {m["id"]: m for m in fx["messages"]}
    idx = lexical.LexicalIndex("floor")
    for m in fx["messages"]:
        lx, tf = lexical.terms(document_text(m["text"]) or "")
        idx.put(m["id"], sha256(m["text"]), lx, tf)
    g = grades(fx)
    replies = {(m["id"], m.get("reply_to")) for m in fx["messages"] if m.get("reply_to")}
    cands = []
    eligible2 = 0
    for a in anchors:
        src = msgs[a]
        lx, tf = lexical.terms(document_text(src["text"]) or "")
        ids, scores = idx.rank(lx, tf, limit=len(msgs))
        kept = []
        for dst, s in zip(ids, scores):
            d = msgs[dst]
            if (d["t"] >= src["t"] or d["thread"] == src["thread"]
                    or (a, dst) in replies or (dst, a) in replies):
                continue
            kept.append((float(s), a, dst))
            if len(kept) >= k:
                break
        cands.extend(kept)
        eligible2 += sum(1 for gr in g.get(a, {}).values() if gr == 2)
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    chosen = cands[:max(0, int(n_edges))]
    c1 = sum(1 for _s, a, d in chosen if g.get(a, {}).get(d, 0) >= 1)
    c2 = sum(1 for _s, a, d in chosen if g.get(a, {}).get(d, 0) == 2)
    return {"written": len(chosen), "correct_ge1": c1, "correct_g2": c2,
            "precision_ge1": c1 / len(chosen) if chosen else 0.0,
            "precision_ge1_lb": wilson_lower(c1, len(chosen)),
            "eligible_g2": eligible2, "recall_g2": c2 / eligible2 if eligible2 else 0.0}


def decide(scored: dict, fx: dict, *, bootstrap_n: int = BOOTSTRAP_N,
           reverse_depth: int = DEFAULT_REVERSE_DEPTH) -> dict:
    """THE SELECTION RULE, end to end, as the harness (tools/related_calibration.py) runs
    it: the threshold and then k from the DEVELOPMENT split only; the incident bootstrap
    of the threshold over the development split; the ship gate on HELD-OUT against the
    lexical floor at the same edge count. The decision ships enabled only when the gate
    passes and the bootstrap's 90% interval is at most BOOTSTRAP_MAX_WIDTH logits wide.
    Returns {split, bootstrap, dev, held_out, lexical_floor, decision, held_out_edges}."""
    dev = anchors_in(fx, "dev")
    held = anchors_in(fx, "held_out")
    t = select_threshold(scored, fx, dev, reverse_depth=reverse_depth)
    boot = bootstrap_threshold(scored, fx, dev, n=bootstrap_n, reverse_depth=reverse_depth)
    out: dict = {"split": {"dev_anchors": len(dev), "held_out_anchors": len(held)},
                 "bootstrap": boot}
    if t is None:
        out.update(dev=evaluate(scored, fx, dev, threshold=GRID[-1], k=K_MAX,
                                reverse_depth=reverse_depth),
                   held_out={}, lexical_floor={}, held_out_edges=[])
        out["decision"] = {"enabled": False, "status": "no_threshold_qualified",
                           "threshold": GRID[-1], "k": K_MAX,
                           "why": ["no grid threshold reached the development "
                                   "precision lower bound"]}
        return out
    k = select_k(scored, fx, dev, t, reverse_depth=reverse_depth)
    out["dev"] = evaluate(scored, fx, dev, threshold=t, k=k, reverse_depth=reverse_depth)
    ho = evaluate(scored, fx, held, threshold=t, k=k, reverse_depth=reverse_depth)
    out["held_out"] = ho
    floor = lexical_floor(fx, held, n_edges=ho["written"], k=k)
    out["lexical_floor"] = floor
    ok, why = ship_gate(ho, floor)
    under = boot["width"] is None or boot["width"] > BOOTSTRAP_MAX_WIDTH
    if under:
        why = why + [f"bootstrap 90% interval width {boot['width']} > "
                     f"{BOOTSTRAP_MAX_WIDTH} logits: under-determined"]
    status = "calibrated" if ok and not under else (
        "under_determined" if under else "failed_held_out_gate")
    out["decision"] = {"enabled": bool(ok and not under), "status": status,
                       "threshold": t, "k": k, "why": why}
    out["held_out_edges"] = [
        (a, c["dst"]) for a in held
        for c in select_edges(scored.get(a, []), threshold=t, k=k,
                              reverse_depth=reverse_depth)]
    return out


def ship_gate(held_out: dict, floor: dict) -> tuple[bool, list[str]]:
    why = []
    lb = wilson_lower(int(held_out["correct_ge1"]), int(held_out["written"]))
    if lb < MIN_LB_HELD_OUT:
        why.append(f"held-out precision lower bound {lb:.3f} < {MIN_LB_HELD_OUT} "
                   f"({held_out['correct_ge1']}/{held_out['written']})")
    if held_out["recall_g2"] < floor["recall_g2"]:
        why.append(f"held-out grade-2 recall {held_out['recall_g2']:.3f} < the lexical "
                   f"floor's {floor['recall_g2']:.3f} at the same edge count")
    return (not why), why


# --------------------------------------------------------------- the calibration document

#: The fields a threshold_version digests. Everything that changes WHICH edges are written.
DECISION_FIELDS = ("schema", "space", "aggregator", "threshold", "k", "reverse_depth",
                   "pool", "rrf_k", "lexical_derivation", "canaries", "fixture")


def threshold_version_of(doc: dict) -> str:
    return sha256({f: doc.get(f) for f in DECISION_FIELDS})[:24]


def content_digest(doc: dict) -> str:
    """The digest of the WHOLE document except this field itself."""
    return sha256({f: v for f, v in doc.items() if f != "content_sha256"})


def canary_digest(sets: list[dict]) -> str:
    """What the reranker was admitted under: the sentences and their reference logits."""
    return sha256([{"query": s["query"], "documents": list(s["documents"]),
                    "reference": [float(x) for x in s["reference"]]} for s in sets])


def build(*, space: str, threshold: float, k: int, canary_references, rerank_model: str,
          reverse_depth: int = DEFAULT_REVERSE_DEPTH, enabled: bool = True,
          status: str = "calibrated", aggregator: str = SHIPPED_AGGREGATOR,
          tolerance: float = RERANK_CANARY_TOL, report: dict | None = None,
          canary_sets=CANARY_SETS, fixture: dict | None = None) -> dict:
    from . import fusion, lexical, search

    if len(canary_references) != len(canary_sets):
        raise ValueError("one reference list per canary set")
    sets = []
    for (q, docs), ref in zip(canary_sets, canary_references):
        if len(ref) != len(docs):
            raise ValueError(f"canary {q!r}: {len(docs)} documents, {len(ref)} references")
        sets.append({"query": q, "documents": list(docs),
                     "reference": [float(x) for x in ref]})
    if fixture is None:
        fx = load_fixture()
        fixture = {"path": "apps/meshsem/calibration/related_judged_v1.json",
                   "sha256": fixture_digest(fx), "labels_sha256": labels_digest(fx)}
    doc = {"schema": SCHEMA, "enabled": bool(enabled), "status": status, "space": space,
           "aggregator": aggregator, "threshold": float(threshold), "k": int(k),
           "reverse_depth": int(reverse_depth), "pool": search.POOL,
           "rrf_k": fusion.RRF_K, "lexical_derivation": lexical.DERIVATION,
           "rerank_model": rerank_model,
           "canaries": {"tolerance": float(tolerance), "sets": sets},
           "rerank_reference": canary_digest(sets), "fixture": fixture,
           "report": report or {}}
    doc["threshold_version"] = threshold_version_of(doc)
    doc = json.loads(json.dumps(doc, default=str))       # exactly what the file will hold
    doc["content_sha256"] = content_digest(doc)
    return doc


@dataclasses.dataclass(frozen=True)
class Calibration:
    space: str
    threshold: float
    k: int
    reverse_depth: int
    aggregator: str
    tolerance: float
    #: ((query, documents, reference logits), ...)
    canary_sets: tuple
    threshold_version: str
    rerank_reference: str
    rerank_model: str
    doc: dict


def configured_path() -> Path:
    return Path(str(env.setting(SETTING, SETTING, default=str(CALIBRATION_PATH))))


def read_document(path: Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CalibrationInvalid(f"{p}: not JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise CalibrationInvalid(f"{p}: not a JSON object")
    return doc


def canary_sets_of(doc: dict) -> tuple:
    return tuple((s["query"], tuple(s["documents"]), tuple(float(x) for x in s["reference"]))
                 for s in doc["canaries"]["sets"])


def load(path: Path | None = None) -> Calibration | None:
    """The calibration the job and the reads run under, or None when there is none to use:
    no file, or a file that says `enabled: false`. A file that is present and WRONG raises
    CalibrationInvalid: a corrupted calibration must not read as a quiet "off", and an
    enabled one whose content_sha256 or threshold_version does not digest it (a hand
    edit: `enabled`, `status` and the report included) is wrong."""
    p = Path(path) if path is not None else configured_path()
    doc = read_document(p)
    if doc is None:
        return None
    if doc.get("schema") != SCHEMA:
        raise CalibrationInvalid(f"{p}: schema {doc.get('schema')!r}, expected {SCHEMA!r}")
    if not doc.get("enabled"):
        return None
    if doc.get("content_sha256") != content_digest(doc):
        raise CalibrationInvalid(f"{p}: content_sha256 does not digest the document "
                                 "(enabled, status and the report included); a calibration "
                                 "is regenerated, never hand-edited")
    if doc.get("threshold_version") != threshold_version_of(doc):
        raise CalibrationInvalid(f"{p}: threshold_version does not digest the content; "
                                 "a calibration is regenerated, never hand-edited")
    if doc.get("aggregator") != SHIPPED_AGGREGATOR:
        raise CalibrationInvalid(f"{p}: aggregator {doc.get('aggregator')!r} is reported, "
                                 "never shipped")
    try:
        sets = canary_sets_of(doc)
        cal = Calibration(space=str(doc["space"]), threshold=float(doc["threshold"]),
                          k=int(doc["k"]), reverse_depth=int(doc["reverse_depth"]),
                          aggregator=doc["aggregator"],
                          tolerance=float(doc["canaries"]["tolerance"]), canary_sets=sets,
                          threshold_version=doc["threshold_version"],
                          rerank_reference=str(doc["rerank_reference"]),
                          rerank_model=str(doc.get("rerank_model") or ""), doc=doc)
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationInvalid(f"{p}: malformed ({exc})") from None
    if not math.isfinite(cal.threshold) or not 1 <= cal.k <= K_CEILING:
        raise CalibrationInvalid(f"{p}: threshold {cal.threshold} / k {cal.k} out of range")
    if cal.reverse_depth < cal.k:
        raise CalibrationInvalid(f"{p}: reverse_depth {cal.reverse_depth} < k {cal.k}: "
                                 "an edge needs both directions scored")
    if not sets or any(len(d) != len(r) or not d for _q, d, r in sets):
        raise CalibrationInvalid(f"{p}: canary sets malformed")
    if canary_digest(doc["canaries"]["sets"]) != cal.rerank_reference:
        raise CalibrationInvalid(f"{p}: rerank_reference does not digest the canaries")
    return cal


# ------------------------------------------------------------------ relation typing (off)

TYPING_SYSTEM = (
    "You label how two chat messages from an engineering team relate. Choose exactly one "
    "label from this closed list: same_incident, follow_up, answers, contradicts.\n"
    "same_incident: both are about the same problem or event.\n"
    "follow_up: the NEWER message continues or acts on the OLDER one.\n"
    "answers: the NEWER message answers a question the OLDER one asked.\n"
    "contradicts: the NEWER message says the OLDER one's claim is wrong.\n"
    "Reply with one JSON object and nothing else: {\"relation\": \"<label>\"}")


def typing_messages(newer: str, older: str) -> list[dict]:
    return [{"role": "system", "content": TYPING_SYSTEM},
            {"role": "user", "content": f"OLDER message:\n{older[:TYPING_CHARS]}\n\n"
                                        f"NEWER message:\n{newer[:TYPING_CHARS]}"}]


def parse_typing(text: str | None) -> str | None:
    """Strict: ONE JSON object, exactly {"relation": <a listed label>}. Anything else is no
    label, never a guess."""
    if not isinstance(text, str):
        return None
    try:
        obj = json.loads(text.strip())
    except ValueError:
        return None
    if not isinstance(obj, dict) or set(obj) != {"relation"}:
        return None
    rel = obj["relation"]
    return rel if rel in RELATIONS else None


def heuristic_relation(newer_author: str, older_author: str, older_text: str) -> str:
    """The no-model floor: same author = follow_up; an older message that asks = answers;
    otherwise same_incident."""
    if newer_author == older_author:
        return "follow_up"
    if "?" in (older_text or ""):
        return "answers"
    return "same_incident"


def macro_f1(gold: list[str], pred: list[str | None]) -> float:
    labels = sorted(set(gold))
    if not labels:
        return 0.0
    f1s = []
    for lab in labels:
        tp = sum(1 for g, p in zip(gold, pred) if g == lab and p == lab)
        fp = sum(1 for g, p in zip(gold, pred) if g != lab and p == lab)
        fn = sum(1 for g, p in zip(gold, pred) if g == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / len(f1s)


#: The typing gate, fixed in advance (divergence 032) like the edge rule's.
TYPING_MAX_INVALID = 0.02
TYPING_MIN_CONTRADICTS_PRECISION = 0.8


def typing_verdict(gold: list[str], pred: list[str | None], heur: list[str],
                   incidents: list[str], *, n: int = BOOTSTRAP_N,
                   seed: int = BOOTSTRAP_SEED) -> dict:
    """Whether a model's relation labels may ship, by the rule fixed in advance: ALL of
      * the incident-bootstrap 95% LOWER bound of (the model's macro-F1 - the best floor's
        macro-F1) is above 0; the floors are the majority class of `gold` and the
        no-model heuristic (`heur`, heuristic_relation's answers), recomputed on every
        resample. A point estimate above the floors is not enough;
      * at most TYPING_MAX_INVALID of the answers are invalid or truncated (None);
      * each time the model says `contradicts` it is right at least
        TYPING_MIN_CONTRADICTS_PRECISION of the time (vacuous when it never says it).
    The four lists are parallel, one entry per judged pair. Nothing to judge is refused."""
    import numpy as np

    if not (len(gold) == len(pred) == len(heur) == len(incidents)):
        raise ValueError("gold, pred, heur and incidents must be parallel lists")
    if not gold:
        return {"pairs": 0, "enabled": False, "why": ["no pair to judge"]}
    majority = max(RELATIONS, key=gold.count)
    f_model = macro_f1(gold, pred)
    f_major = macro_f1(gold, [majority] * len(gold))
    f_heur = macro_f1(gold, heur)
    invalid = sum(1 for p in pred if p is None) / len(pred)
    said = [g for g, p in zip(gold, pred) if p == "contradicts"]
    c_prec = (sum(1 for g in said if g == "contradicts") / len(said)) if said else None
    rows_of: dict[str, list[int]] = {}
    for j, x in enumerate(incidents):
        rows_of.setdefault(x, []).append(j)
    keys = sorted(rows_of)
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(n):
        sel = [j for i in rng.integers(0, len(keys), len(keys)) for j in rows_of[keys[i]]]
        g = [gold[j] for j in sel]
        floor = max(macro_f1(g, [majority] * len(g)), macro_f1(g, [heur[j] for j in sel]))
        diffs.append(macro_f1(g, [pred[j] for j in sel]) - floor)
    lb = float(np.percentile(np.asarray(diffs), 2.5))
    why = []
    if not lb > 0:
        why.append(f"bootstrap 95% lower bound of (model - best floor) macro-F1 is "
                   f"{lb:.3f}, not above 0 ({len(keys)} incidents, {len(gold)} pairs)")
    if invalid > TYPING_MAX_INVALID:
        why.append(f"invalid or truncated answers {invalid:.3f} > {TYPING_MAX_INVALID}")
    if c_prec is not None and c_prec < TYPING_MIN_CONTRADICTS_PRECISION:
        why.append(f"contradicts precision {c_prec:.3f} < "
                   f"{TYPING_MIN_CONTRADICTS_PRECISION} ({len(said)} said)")
    return {"pairs": len(gold), "incidents": len(keys),
            "macro_f1": {"model": f_model, "majority": f_major, "heuristic": f_heur},
            "majority_label": majority, "invalid_or_truncated_rate": invalid,
            "contradicts_emitted": len(said), "contradicts_precision": c_prec,
            "bootstrap_lb95_model_minus_best_floor": lb,
            "gold_counts": {r: gold.count(r) for r in RELATIONS},
            "pred_counts": {str(r): pred.count(r) for r in sorted(set(pred), key=str)},
            "enabled": not why, "why": why}


__all__ = ["AGGREGATORS", "BOOTSTRAP_MAX_WIDTH", "CALIBRATION_PATH", "CANARY_SETS",
           "Calibration", "CalibrationInvalid", "DEFAULT_REVERSE_DEPTH", "FIXTURE_PATH",
           "GRID", "K_MAX", "LABELS_PATH", "MIN_LB_DEV", "MIN_LB_HELD_OUT", "RELATIONS",
           "RERANK_CANARY_TOL", "SETTING", "TYPING_MAX_INVALID",
           "TYPING_MIN_CONTRADICTS_PRECISION", "anchors_in", "bootstrap_threshold",
           "build", "canary_digest", "canary_sets_of", "configured_path",
           "content_digest", "decide", "evaluate",
           "fixture_digest", "heuristic_relation", "labels_digest", "lexical_floor",
           "load", "load_fixture", "macro_f1", "pair_value", "parse_typing",
           "read_document", "reverse_targets", "select_edges", "select_k",
           "select_threshold", "ship_gate", "split_of", "threshold_version_of",
           "typing_messages", "typing_verdict", "wilson_lower", "zero_overlap_pairs"]
