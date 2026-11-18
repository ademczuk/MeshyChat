"""Exact-passage promotion on mail search (apps/meshsem/exactpassage.py), and its wiring into search.search.

THE DEFECT (measured on production 11 Oct 2026, txxxx): the 4B reranker scores a message low for a query copied out
of it. With 60 target messages and 4 copied passages each, the target came first in 84 of 240 searches; with the
reranker off, 161. THE RULE: a query of at least six words that EXACTLY ONE pool body contains (lower-cased, whitespace
collapsed) puts that hit first and moves nothing else; two or more such bodies move nothing. Off unless
MESHY_SEARCH_EXACT_PASSAGE is true, and then the response says what matched. Each negative sits beside its positive
control (docs/V1-DESIGN-RULES.md C1).

No database and no model: search.search runs with its reads, its arms, its pool and the reranker stood in for.

Run:  pytest tests/test_meshsem_exactpassage.py -q
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.meshsem import exactpassage, fusion, providers, search  # noqa: E402

PASSAGE = "the gate passed on the merged tree and both heads match"
T0 = _dt.datetime(2026, 10, 11, tzinfo=_dt.timezone.utc)


class C:
    def __init__(self, key, body):
        self.key, self.body = key, body


def _keys(order):
    return [c.key for c in order]


# ================================================================= the rule, alone

def test_one_body_holding_the_passage_comes_first_and_nothing_else_moves():
    ranked = [C("a", "unrelated"), C("b", "also unrelated"), C("t", "Notes. " + PASSAGE + ". Done."), C("d", "x")]
    order, containing, promoted = exactpassage.promote(PASSAGE, ranked, lambda c: c.body)
    assert _keys(order) == ["t", "a", "b", "d"] and _keys(containing) == ["t"] and promoted


def test_no_body_holding_the_passage_moves_nothing():
    ranked = [C("a", "unrelated"), C("b", "the gate passed on the merged tree")]       # a prefix is not the passage
    order, containing, promoted = exactpassage.promote(PASSAGE, ranked, lambda c: c.body)
    assert _keys(order) == ["a", "b"] and containing == [] and not promoted


def test_two_bodies_holding_the_passage_move_nothing_and_both_are_reported():
    ranked = [C("a", "unrelated"), C("q", "> " + PASSAGE), C("t", PASSAGE), C("d", "x")]
    order, containing, promoted = exactpassage.promote(PASSAGE, ranked, lambda c: c.body)
    assert _keys(order) == ["a", "q", "t", "d"] and _keys(containing) == ["q", "t"] and not promoted


def test_a_query_under_six_words_is_never_a_passage():
    five = "the gate passed on main"
    ranked = [C("a", "x"), C("t", "so " + five + " today")]
    order, containing, promoted = exactpassage.promote(five, ranked, lambda c: c.body)
    assert _keys(order) == ["a", "t"] and containing == [] and not promoted
    six = five + " today"                                                            # positive control at six
    order, containing, promoted = exactpassage.promote(six, ranked, lambda c: c.body)
    assert _keys(order) == ["t", "a"] and promoted


def test_case_and_whitespace_do_not_matter_but_punctuation_does():
    body = "THE  gate passed\non the merged tree and both heads match"
    ranked = [C("a", "x"), C("t", body)]
    assert exactpassage.promote(PASSAGE, ranked, lambda c: c.body)[2]
    punct = "the gate passed, on the merged tree and both heads match"
    assert not exactpassage.promote(punct, ranked, lambda c: c.body)[2]


def test_a_missing_body_is_no_match_not_an_error():
    ranked = [C("a", None), C("t", PASSAGE)]
    order, containing, promoted = exactpassage.promote(PASSAGE, ranked, lambda c: c.body)
    assert _keys(order) == ["t", "a"] and promoted


def test_the_hit_already_first_stays_first():
    ranked = [C("t", PASSAGE), C("a", "x")]
    order, _, promoted = exactpassage.promote(PASSAGE, ranked, lambda c: c.body)
    assert _keys(order) == ["t", "a"] and promoted


# ================================================================= wired into search.search

class _Space:
    name = "test-space/1/x"


class _Stub:
    #: A full-width index, as search.VectorIndex is unless MESHY_SEMANTIC_RESIDENT_DIMS sets a view (#543).
    view = False

    def describe(self, ceiling):
        return {}


def _cand(key, body, pos):
    return search.Candidate(src_id=key, key=key, version=1, recorded_at=T0, author="kxxx", thread_id="t",
                            body=body, body_truncated=False, fused_score=1.0 / (60 + pos),
                            ranks={fusion.LEXICAL: pos, fusion.EMBED: None}, cosine=None, lexical_score=0.5)


@pytest.fixture
def wired(monkeypatch):
    """search.search with the database, arms, pool and reranker stood in for. The reranker scores the copied
    passage's own message LOWEST, the measured behavior."""
    bodies = {"m1": "a templated gate note", "m2": "another note", "t": "Before. " + PASSAGE + ". After.",
              "m4": "a fourth message"}
    pool = [_cand(k, b, i + 1) for i, (k, b) in enumerate(bodies.items())]
    scores = {"m1": 0.9, "m2": 0.8, "t": 0.01, "m4": 0.5}
    monkeypatch.setattr(search, "_prepare", lambda conn, space: (False, None, None, _Stub(), _Stub(), 100))
    monkeypatch.setattr(search, "_describe", lambda idx, lex, ceiling: {})
    monkeypatch.setattr(search, "_lexical_arm", lambda lex, q: search.Arm(
        fusion.LEXICAL, True, fusion.Ranking(fusion.LEXICAL, list(bodies), [1.0, 0.9, 0.8, 0.7]),
        detail={"query_terms": 5}))
    monkeypatch.setattr(search.dbconn, "request_server_internal", lambda conn: contextlib.nullcontext(conn))
    monkeypatch.setattr(search, "_pool", lambda c, fused, **kw: (list(pool), len(pool), {}))
    monkeypatch.setattr(search.providers, "rerank", lambda q, docs: providers.Reranked(
        [scores[k] for k in bodies], "rerank-test", [{"id": "rerank-test", "verdict": "ran"}]))
    monkeypatch.delenv("MESHY_SEARCH_EXACT_PASSAGE", raising=False)
    return lambda q, rerank=True: search.search(None, _Space(), q, k=10, rerank=rerank)


def test_off_by_default_the_reranker_order_stands_and_nothing_is_added(wired):
    body = wired(PASSAGE)
    assert [h["key"] for h in body["hits"]] == ["m1", "m2", "m4", "t"]
    assert "exact_passage" not in body and all("exact_passage" not in h for h in body["hits"])


@override_settings(MESHY_SEARCH_EXACT_PASSAGE=True)
def test_on_the_copied_passage_comes_back_first_and_the_response_says_so(wired):
    body = wired(PASSAGE)
    assert [h["key"] for h in body["hits"]] == ["t", "m1", "m2", "m4"]
    assert body["exact_passage"] == {"matches": 1, "promoted": "t"}
    assert [h["exact_passage"] for h in body["hits"]] == [True, False, False, False]


@override_settings(MESHY_SEARCH_EXACT_PASSAGE=True)
def test_on_an_ordinary_question_changes_nothing(wired):
    body = wired("which reranker does search use these days")
    assert [h["key"] for h in body["hits"]] == ["m1", "m2", "m4", "t"]
    assert body["exact_passage"] == {"matches": 0, "promoted": None}


@override_settings(MESHY_SEARCH_EXACT_PASSAGE=True)
def test_on_with_rerank_off_nothing_is_promoted_since_the_fused_order_already_holds(wired):
    body = wired(PASSAGE, rerank=False)
    assert [h["key"] for h in body["hits"]] == ["m1", "m2", "t", "m4"] and "exact_passage" not in body


def test_the_flag_reads_the_environment_too(monkeypatch):
    monkeypatch.delenv("MESHY_SEARCH_EXACT_PASSAGE", raising=False)
    assert search.exact_passage_on() is False
    monkeypatch.setenv("MESHY_SEARCH_EXACT_PASSAGE", "true")
    assert search.exact_passage_on() is True
