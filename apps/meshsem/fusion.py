"""Reciprocal-rank fusion of the search arms, and the name of the method a response used.
Divergence 026. Pure arithmetic: no database, no model, no numpy.

    fused(d) = sum over the arms that RAN and ranked d of  1 / (k + rank_arm(d))

with rank 1-based and k = 60, the constant of Cormack, Clarke and Buettcher (SIGIR 2009),
NOT tuned: the A/B of 2026-09-23 used 60 untuned, and tuning it on 94 queries would fit
the thread-membership proxy rather than search. A document an arm did not rank gets
nothing from that arm (not a penalty rank), so a lexical miss neither helps nor hurts it.

Measured on that A/B (94 queries, paired bootstrap): RRF of TF-IDF and the no-prompt
embedding scored MRR 0.552 and recall@5 0.517, equal to TF-IDF alone within noise and
+0.093 recall@5 [+0.033, +0.157] over the embedding alone. Divergence 026 has the same
figures for THIS implementation.

TIES. Two documents at ranks (i, j) and (j, i) fuse to exactly the same float (IEEE
addition is commutative). They are ordered by their best single-arm rank, then by
src_id: deterministic, and independent of which arm is listed first.
"""
from __future__ import annotations

import dataclasses

#: The RRF constant. Not tuned; see the module docstring.
RRF_K = 60

#: Arm names, in the order a method name lists them.
LEXICAL = "lexical"
EMBED = "embed"
ARMS = (LEXICAL, EMBED)


@dataclasses.dataclass(frozen=True)
class Ranking:
    """One arm's output: src_ids best first, with the arm's own score for each."""

    arm: str
    ids: list[str]
    scores: list[float]

    def __post_init__(self):
        if self.arm not in ARMS:
            raise ValueError(f"unknown arm {self.arm!r}; known: {ARMS}")
        if len(self.ids) != len(self.scores):
            raise ValueError(f"{self.arm}: {len(self.ids)} ids but {len(self.scores)} scores")


@dataclasses.dataclass
class Fused:
    src_id: str
    score: float
    #: 1-based rank per arm; None when that arm did not rank this document (or did not run).
    ranks: dict[str, int | None]
    #: The arm's own score (cosine for both arms); None where `ranks` is None.
    arm_scores: dict[str, float | None]


def rrf(rankings: list[Ranking], k: int = RRF_K) -> list[Fused]:
    """Fuse `rankings` (only the arms that RAN; an arm that did not run is simply absent).
    Best first. Refuses a src_id listed twice by one arm, and two rankings of one arm:
    either would count one arm's opinion twice."""
    if k <= 0:
        raise ValueError(f"RRF constant must be positive, got {k}")
    names = [r.arm for r in rankings]
    if len(set(names)) != len(names):
        raise ValueError(f"an arm was ranked twice: {names}")
    acc: dict[str, Fused] = {}
    for r in rankings:
        for rank, (sid, score) in enumerate(zip(r.ids, r.scores), start=1):
            f = acc.get(sid)
            if f is None:
                f = acc[sid] = Fused(sid, 0.0, {a: None for a in ARMS},
                                     {a: None for a in ARMS})
            if f.ranks[r.arm] is not None:
                raise ValueError(f"{r.arm} ranked {sid} twice")
            f.ranks[r.arm] = rank
            f.arm_scores[r.arm] = float(score)
            f.score += 1.0 / (k + rank)
    return sorted(acc.values(),
                  key=lambda f: (-f.score,
                                 min(x for x in f.ranks.values() if x is not None),
                                 f.src_id))


def method_name(arms_ran: list[str], *, reranked: bool) -> str | None:
    """What a response reports as `method`. None when no arm ran (nothing was ranked).

    "rrf(lexical,embed)" when both arms ran, the one arm's name when only one did, and
    "+rerank" appended only when a reranker scored the pool, whether or not its scores
    changed the order (the same rule as `reranked`, apps/meshsem/providers.Reranked.ran)."""
    ran = [a for a in ARMS if a in arms_ran]
    if not ran:
        return None
    base = f"rrf({','.join(ran)})" if len(ran) > 1 else ran[0]
    return base + ("+rerank" if reranked else "")


__all__ = ["ARMS", "EMBED", "Fused", "LEXICAL", "RRF_K", "Ranking", "method_name", "rrf"]
