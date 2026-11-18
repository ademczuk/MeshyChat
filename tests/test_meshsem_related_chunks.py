"""The related_to job sends its forward rerank in requests of at most RERANK_CHUNK documents (2026-10-07).

An interactive search shares the rerank server with this job, first in first out, so one request carrying a
source's whole pool made a search behind it wait for all of it (txxxx measured 2,714 ms against 371 ms). Each
score depends only on its own pair, so chunking must leave every score, and so every edge, unchanged."""
from __future__ import annotations

import pytest

from apps.meshsem import providers, related, search


def _pool(n: int) -> list[search.Candidate]:
    return [search.Candidate(src_id=f"id-{i}", key=f"msg/{i}", version=1, recorded_at=None, author="a",
                             thread_id="t", body=f"document number {i}", body_truncated=False,
                             fused_score=1.0 / (i + 1), ranks={}, cosine=None, lexical_score=None)
            for i in range(n)]


def _score(query: str, doc: str) -> float:
    return float((sum(map(ord, query)) * 7 + sum(map(ord, doc)) * 13) % 1000) / 100.0


def _fake(calls: list):
    def rerank(query, documents, *, budget_s=20.0, provider_ids=None):
        calls.append(len(documents))
        return providers.Reranked([_score(query, d) for d in documents], "rerank-a", [])
    return rerank


@pytest.mark.parametrize("n", [1, 9, 10, 11, 25, 40])
def test_the_forward_rerank_goes_in_chunks_and_scores_are_unchanged(monkeypatch, n):
    calls: list = []
    monkeypatch.setattr(providers, "rerank", _fake(calls))
    pool = _pool(n)
    cands, refused = related.score_source("the source text", pool, pinned="rerank-a", reverse_depth=0)
    forward = calls
    assert all(size <= related.RERANK_CHUNK for size in forward), calls
    assert sum(forward) == n
    assert len(forward) == -(-n // related.RERANK_CHUNK)
    assert refused == 0
    # positive control: the same scores a single whole-pool call would give, in pool order
    assert [c["fwd"] for c in cands] == [_score("the source text", c.body) for c in pool]
    assert [c["candidate"].key for c in cands] == [c.key for c in pool]


def test_a_chunk_that_is_refused_falls_back_to_one_document_at_a_time(monkeypatch):
    """The second chunk is refused (the model answered, but not with scores): the existing fallback asks every
    document alone, as it did when the single call was refused, so no score is lost or shifted."""
    calls: list = []
    real = _fake(calls)

    def refusing(query, documents, *, budget_s=20.0, provider_ids=None):
        if len(calls) == 1 and len(documents) == related.RERANK_CHUNK:
            calls.append(len(documents))
            return providers.Reranked(None, None, [{"id": "rerank-a", "verdict": "refused",
                                                    "reason": "input too long", "http": 400}])
        return real(query, documents, budget_s=budget_s, provider_ids=provider_ids)

    monkeypatch.setattr(providers, "rerank", refusing)
    monkeypatch.setattr(related, "refused_input", lambda attempt: attempt.get("verdict") == "refused")
    pool = _pool(25)
    cands, refused = related.score_source("q", pool, pinned="rerank-a", reverse_depth=0, confirm=lambda: None)
    assert refused == 0
    assert [c["fwd"] for c in cands] == [_score("q", c.body) for c in pool]
