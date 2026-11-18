"""Exact-passage promotion for mail search, after the reranker. Off unless MESHY_SEARCH_EXACT_PASSAGE is true.

WHY. The 4B cross-encoder that orders search's pool (F05: nDCG@10 0.476 against 0.353 for the reranker before it, on
120 blind-judged natural-language queries) scores a message LOW for a query that is a passage copied out of that
message: it reads the query as a question the passage does not answer. Measured on production 11 Oct 2026 (txxxx,
C:/Projects/meshy-test/rerank-fusion): 60 target messages, 4 copied passages each, 240 searches. The target came first
in 84 of 240 under the reranker's order, and in 161 with the reranker off. Mixing the reranker with the fused order
(RRF over the arms and the reranker, or a blend with the fused position) lost 0.037 to 0.107 nDCG@10 on F05, so the
reranker's order stays and only an unambiguous copy moves.

THE RULE. When the query has at least MIN_WORDS words and EXACTLY ONE pool candidate's body contains the whole query
(both lower-cased with whitespace collapsed; the body as the reranker saw it, cut at 1,500 characters), that
candidate moves to first place and nothing else moves. With two or more such candidates nothing moves: the copy is
ambiguous (a quoted message, a templated note), so the reranker's order stands. Either way every containing candidate
is reported, so a reader can see which hits hold the passage.

MEASURED, same data: target first in 211 of 240 (MRR 0.895, against 0.463). On F05's 120 queries and on the 48
queries seats have typed into mail search (their transcripts on HQB), the rule moved nothing: no such query contains
an exact passage. So the change is invisible to ordinary searches, which is the point. The context route is NOT
covered: it reranks with a whole message as the query and wants OTHER messages, so a copy is the wrong answer there.

Pure: no Django, no database, no model.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

#: Below this many words a query is a phrase, not a passage, and a phrase may appear in many unrelated messages.
MIN_WORDS = 6


def normalize(text: str | None) -> str:
    """Lower-cased, whitespace collapsed to single spaces, stripped. Punctuation is kept: a passage that differs in
    punctuation is not the same passage, and then the reranker's order stands."""
    return " ".join((text or "").lower().split())


def promote(query: str, ranked: Sequence[T], body_of: Callable[[T], str | None]) -> tuple[list[T], list[T], bool]:
    """(order, containing, promoted). `ranked` is the reranker's order; `containing` are the candidates whose body holds
    the whole query, in that order; `promoted` is True when exactly one did and it now comes first."""
    q = normalize(query)
    order = list(ranked)
    if len(q.split()) < MIN_WORDS:
        return order, [], False
    containing = [c for c in order if q in normalize(body_of(c))]
    if len(containing) != 1:
        return order, containing, False
    first = containing[0]
    return [first] + [c for c in order if c is not first], containing, True
