"""Grade the related_to link job's REAL-text edges (design 9.1), separately from summaries.

The operator asked whether the link job should stay on as inferred hints; design 9.1 keeps
it on (it is labelled `tier: "inferred"`, a wrong hint costs a reader one message, and it
is one field from off) and grades the 237 edges it already wrote, now, against 032's own
held-out precision rule, fixed before any label:

  * 60 edges, frozen and digested before judging: 30 whose source body is under 80
    characters and 30 at or above (99 of 237 edges came from short sources, a length the
    synthetic calibration's held-out set never exercised); within each, edges into a hub
    (in-degree 10 or more) in proportion to their share;
  * blind: each judge sees the two messages and 032's rubric (grade 2, 1 or 0), never a
    score or a rank; two judges (design 7.3, so Q2 gates this), each first validated on
    10 planted pairs, 5 related and 5 not, of which it must grade 9 correctly;
  * a pair is related only when BOTH judges grade it 1 or 2; the gate is the Wilson 95%
    lower bound of the related share over the 60 at least 0.60 (44 of 60); each stratum is
    reported with its own interval, deciding nothing (30 would need 24 alone);
  * fail: set `enabled: false` in the calibration (032 provides it). Pass overall with the
    short stratum below its own bound: a minimum source length is a NEW calibration (a new
    threshold_version) measured on its own, never a hand edit to the served one.
  * 032's second clause, grade-2 recall against the lexical floor, needs closed-world
    labels and cannot be measured from a sample of written edges: it is NOT claimed.

The sample reads ids, lengths and in-degrees only (READ ONLY); packets read the two
bodies of each sampled pair and are written OUTSIDE the repository.
"""
from __future__ import annotations

import random

from psycopg.rows import dict_row

from . import stats

SCHEMA = "meshsum-linkgrade-sample/1"
N_PER_STRATUM = 30
SHORT_CHARS = 80
HUB_IN_DEGREE = 10
GATE_LOWER = 0.60
PLANTED_PAIRS = 10
JUDGE_MIN_PLANTED = 9

RUBRIC = ("Grade how related the second message is to the first, for a reader of the "
          "first: 2 = directly about the same specific matter (one answers, continues or "
          "depends on the other); 1 = about the same topic or task, useful context; 0 = "
          "unrelated, or related only by shared generic words.")


def sample_edges(conn, *, seed: int) -> dict:
    """60 edges, stratified by source length and hub share. Ids, keys, lengths only."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT e.src_id, e.dst_id, s.key AS src_key, d.key AS dst_key, "
            "  length(CASE WHEN jsonb_typeof(s.value->'body') = 'string' "
            "       THEN s.value->>'body' ELSE '' END) AS src_len, "
            "  (SELECT count(*) FROM mesh_related_edges x WHERE x.dst_id = e.dst_id) AS indeg "
            "FROM mesh_related_edges e "
            "JOIN shared_memory s ON s.id = e.src_id AND s.is_head "
            "JOIN shared_memory d ON d.id = e.dst_id AND d.is_head "
            "ORDER BY e.src_id, e.dst_id")
        edges = cur.fetchall()
    return {"schema": SCHEMA, "seed": seed, "edges": stratify(edges, seed=seed)}


def stratify(edges: list[dict], *, seed: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for name, keep in (("short", lambda e: e["src_len"] < SHORT_CHARS),
                       ("long", lambda e: e["src_len"] >= SHORT_CHARS)):
        pool = [e for e in edges if keep(e)]
        hubs = [e for e in pool if e["indeg"] >= HUB_IN_DEGREE]
        rest = [e for e in pool if e["indeg"] < HUB_IN_DEGREE]
        n_hub = round(N_PER_STRATUM * len(hubs) / len(pool)) if pool else 0
        n_hub = min(n_hub, len(hubs))
        pick = rng.sample(hubs, n_hub) + rng.sample(rest, min(len(rest),
                                                             N_PER_STRATUM - n_hub))
        for e in pick:
            out.append({"stratum": name, "hub": e["indeg"] >= HUB_IN_DEGREE,
                        "src_id": str(e["src_id"]), "dst_id": str(e["dst_id"]),
                        "src_key": e["src_key"], "dst_key": e["dst_key"],
                        "src_len": int(e["src_len"]), "in_degree": int(e["indeg"])})
    return out


def pair_items(conn, sample: dict, planted: list[dict], *, seed: int) -> list[dict]:
    """What a judge grades: each sampled edge's two bodies, read by id (READ ONLY), and
    the planted pairs beside them in the same shape ({id, first, second}, nothing that
    says which is which), in ONE seeded shuffle."""
    items = []
    with conn.cursor() as cur:
        for e in sample["edges"]:
            cur.execute("SELECT id, value->>'body' FROM shared_memory "
                        "WHERE id = ANY(%s::uuid[])", ([e["src_id"], e["dst_id"]],))
            bodies = {str(i): b for i, b in cur.fetchall()}
            items.append({"id": f"{e['src_id']}>{e['dst_id']}",
                          "first": bodies.get(e["src_id"]),
                          "second": bodies.get(e["dst_id"])})
    items += [{"id": p["id"], "first": p["first"], "second": p["second"]}
              for p in planted]
    random.Random(seed).shuffle(items)
    return items


def score(sample: dict, labels_a: dict, labels_b: dict, planted: dict) -> dict:
    """`labels_*`: {pair id: grade} per judge, pairs keyed `<src_id>><dst_id>` and planted
    pairs by their own ids; `planted`: {planted id: True (related) / False}."""
    validation = {}
    for judge, labels in (("a", labels_a), ("b", labels_b)):
        right = sum(1 for pid, related in planted.items()
                    if pid in labels and (labels[pid] >= 1) == related)
        validation[judge] = {"correct": right, "of": len(planted),
                             "ok": len(planted) == PLANTED_PAIRS
                             and right >= JUDGE_MIN_PLANTED}
    related = []
    for e in sample["edges"]:
        pid = f"{e['src_id']}>{e['dst_id']}"
        related.append((e["stratum"], labels_a.get(pid, 0) >= 1 and labels_b.get(pid, 0) >= 1))
    k, n = sum(r for _, r in related), len(related)
    lower, upper = stats.wilson(k, n)
    strata = {}
    for name in ("short", "long"):
        ks = [r for s, r in related if s == name]
        lo, hi = stats.wilson(sum(ks), len(ks))
        strata[name] = {"related": sum(ks), "n": len(ks), "lower": lo, "upper": hi,
                        "clears_alone": lo >= GATE_LOWER}
    valid = all(v["ok"] for v in validation.values())
    passed = valid and n == 2 * N_PER_STRATUM and lower >= GATE_LOWER
    if not valid:
        action = "void: a judge failed validation; replace it and grade again"
    elif not passed:
        action = "set enabled: false in the related_to calibration (032)"
    elif not strata["short"]["clears_alone"]:
        action = ("keep on; a minimum source length is a NEW calibration (a new "
                  "threshold_version), measured on its own")
    else:
        action = "keep on as the inferred tier it is"
    return {"related": k, "n": n, "lower": lower, "upper": upper, "gate": GATE_LOWER,
            "passed": passed, "validation": validation, "strata": strata,
            "action": action, "recall_claimed": False}


__all__ = ["GATE_LOWER", "RUBRIC", "SCHEMA", "pair_items", "sample_edges", "score",
           "stratify"]
