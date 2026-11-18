"""The ranking arithmetic of divergence 026, with no database and no model.

  * reciprocal-rank fusion (apps/meshsem/fusion.py): the exact scores for a hand-worked
    case, 1-based ranks, k = 60, nothing (not a penalty) from an arm that did not rank a
    document, deterministic ties, and the refusals that stop one arm counting twice;
  * the method name a response reports, and the degradation sentence;
  * the lexical arm's TF-IDF (apps/meshsem/lexical.LexicalIndex) against an independent
    dict-based implementation of the A/B harness's formula, and its tokenizer against the
    harness's.

Each negative sits beside its positive control (docs/V1-DESIGN-RULES.md C1).

Run:  pytest tests/test_meshsem_fusion.py -q
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.meshsem import fusion, lexical, providers, search  # noqa: E402

L, E = fusion.LEXICAL, fusion.EMBED
ROOT = Path(__file__).resolve().parent.parent


def _r(arm, ids):
    return fusion.Ranking(arm, list(ids), [1.0 - i / 100 for i in range(len(ids))])


# ================================================================= the fusion arithmetic

def test_rrf_scores_a_hand_worked_case_exactly():
    """lexical [a, b, c], embed [b, c, d], k = 60, ranks 1-based:
        a = 1/61            b = 1/62 + 1/61     c = 1/63 + 1/62     d = 1/63
    so the order is b, c, a, d. Kills: 0-based ranks (a would be 1/60), any other k,
    a penalty rank for a missing document, and a max() in place of the sum."""
    fused = fusion.rrf([_r(L, "abc"), _r(E, "bcd")])
    got = {f.src_id: f.score for f in fused}
    assert [f.src_id for f in fused] == ["b", "c", "a", "d"]
    assert got["a"] == 1 / 61 and got["d"] == 1 / 63
    assert got["b"] == pytest.approx(1 / 62 + 1 / 61, abs=0, rel=1e-15)
    assert got["c"] == pytest.approx(1 / 63 + 1 / 62, abs=0, rel=1e-15)
    b = next(f for f in fused if f.src_id == "b")
    assert b.ranks == {L: 2, E: 1} and b.arm_scores == {L: 0.99, E: 1.0}
    a = next(f for f in fused if f.src_id == "a")
    assert a.ranks == {L: 1, E: None} and a.arm_scores[E] is None


def test_the_constant_is_sixty_and_is_the_default():
    assert fusion.RRF_K == 60
    [only] = fusion.rrf([_r(L, "x")])
    assert only.score == 1 / 61
    [other_k] = fusion.rrf([_r(L, "x")], k=10)
    assert other_k.score == 1 / 11, "control: k is actually used"


def test_one_arm_alone_keeps_its_own_order():
    """What a degraded (lexical-only) response serves: that arm's order, unchanged."""
    ids = ["q", "a", "z", "m"]
    assert [f.src_id for f in fusion.rrf([_r(L, ids)])] == ids
    assert [f.src_id for f in fusion.rrf([_r(E, ids)])] == ids
    assert fusion.rrf([]) == []


def test_agreement_beats_one_arm_s_first_place():
    """A document both arms put second outranks one only a single arm put first:
    2/62 > 1/61. This is the property the A/B's recall gain came from."""
    fused = fusion.rrf([_r(L, ["solo", "both"]), _r(E, ["other", "both"])])
    assert fused[0].src_id == "both"


def test_swapped_ranks_tie_exactly_and_break_deterministically():
    """(1, 3) and (3, 1) fuse to the same float. Order: best single-arm rank, then
    src_id, and the same whichever arm is listed first."""
    one = fusion.rrf([_r(L, ["y", "m", "x"]), _r(E, ["x", "m", "y"])])
    two = fusion.rrf([_r(E, ["x", "m", "y"]), _r(L, ["y", "m", "x"])])
    sx = next(f.score for f in one if f.src_id == "x")
    sy = next(f.score for f in one if f.src_id == "y")
    assert sx == sy
    assert [f.src_id for f in one] == [f.src_id for f in two] == ["x", "y", "m"]


def test_rrf_refuses_what_would_count_one_arm_twice():
    with pytest.raises(ValueError, match="twice"):
        fusion.rrf([_r(L, ["a", "a"])])
    with pytest.raises(ValueError, match="ranked twice"):
        fusion.rrf([_r(L, "ab"), _r(L, "ba")])
    with pytest.raises(ValueError, match="positive"):
        fusion.rrf([_r(L, "a")], k=0)
    with pytest.raises(ValueError, match="unknown arm"):
        fusion.Ranking("bm25", ["a"], [1.0])
    assert fusion.rrf([_r(L, "ab"), _r(E, "ba")]), "control: two arms, each once"


# ============================================================ what a response says

def test_the_method_name_says_what_ran():
    assert fusion.method_name([L, E], reranked=False) == "rrf(lexical,embed)"
    assert fusion.method_name([E, L], reranked=False) == "rrf(lexical,embed)"
    assert fusion.method_name([L], reranked=False) == "lexical"
    assert fusion.method_name([E], reranked=False) == "embed"
    assert fusion.method_name([L, E], reranked=True) == "rrf(lexical,embed)+rerank"
    assert fusion.method_name([L], reranked=True) == "lexical+rerank"
    assert fusion.method_name([], reranked=False) is None


def test_the_degradation_sentence_names_the_missing_arm_and_why():
    """Kills: `degraded` always null, and a method that names an arm that did not run."""
    lex = search.Arm(L, True, _r(L, "ab"), detail={"query_terms": 2})
    emb = search.Arm(E, True, _r(E, "ba"), detail={"query_vector": "embedded"})
    down = search.Arm(E, False, absent="embed_unavailable", error="E_EMBED_UNAVAILABLE")

    both = search._ranking_fields([lex, emb], reranked=False)
    assert both == {"method": "rrf(lexical,embed)", "rrf_k": 60, "degraded": None,
                    "arms": {L: {"ran": True, "ranked": 2, "query_terms": 2},
                             E: {"ran": True, "ranked": 2, "query_vector": "embedded"}}}

    alone = search._ranking_fields([lex, down], reranked=False)
    assert alone["method"] == "lexical" and alone["rrf_k"] is None
    assert alone["degraded"] == ("lexical only: the embed arm did not run "
                                 "(E_EMBED_UNAVAILABLE: embed_unavailable)")
    assert alone["arms"][E] == {"ran": False, "ranked": 0, "absent": "embed_unavailable",
                                "error": "E_EMBED_UNAVAILABLE"}
    assert search._ranking_fields(None, reranked=False) == {
        "method": None, "rrf_k": None, "arms": None, "degraded": None}


def test_the_route_default_is_rerank():
    """The view's default, read directly: the route test proves the behaviour, this pins
    the constant it comes from (divergence 026, as amended: reranking is on by default)."""
    from apps.meshsem import views

    assert views.RERANK_DEFAULT == "1"
    import inspect

    for fn in (search.search, search.context):
        assert inspect.signature(fn).parameters["rerank"].default is True, fn.__name__


def test_the_meshsem_cli_reranks_unless_told_not_to(monkeypatch, capsys):
    """`python -m apps.meshsem search` answers "as the route answers it", so it reranks by
    default too (divergence 026, as amended); `--no-rerank` turns it off and `--rerank`,
    the old spelling, means the default. What the CLI ASKS search() for needs neither a
    database nor a model, so both are stood in for."""
    import contextlib

    from apps.core import dbconn
    from apps.meshsem import __main__ as meshsem_cli
    from apps.meshsem import store

    asked = []

    def fake_search(conn, space, q, *, k, rerank):
        asked.append(rerank)
        return {"space": "s", "method": None, "degraded": None, "providers": {},
                "reranked": False, "coverage": {}, "truncated": False, "hits": []}

    monkeypatch.setattr(search, "search", fake_search)
    monkeypatch.setattr(dbconn, "serving_connection",
                        lambda dsn=None: contextlib.nullcontext(None))
    monkeypatch.setattr(store, "configured_space", lambda: None)
    for flags in ((), ("--rerank",), ("--no-rerank",)):
        assert meshsem_cli.main(["search", "--q", "x", *flags]) == 0, flags
    assert asked == [True, True, False]
    capsys.readouterr()


def test_the_meshsem_cli_catch_up_drains_and_exits_as_documented(monkeypatch, capsys):
    """`python -m apps.meshsem catch-up [--until-done]`: one tick, or ticks until one makes
    no progress; exit 1 on a tick with no admitted provider, after printing it and the
    refused attempts. Neither a database nor a model: the tick is stood in for."""
    import contextlib
    import types

    from apps.core import dbconn
    from apps.meshsem import __main__ as meshsem_cli
    from apps.meshsem import embedjob, store

    def report(n, *, more, stopped=None, attempts=()):
        return types.SimpleNamespace(
            space="s", rows_read=n, rows_processed=n, embedded=n, unchanged=0,
            skipped_forbidden=0, skipped_empty=0, skipped_no_schema_version=0, stopped=stopped,
            more=more, gate_open=False, gate_changed=False, purged=0, lexed=0,
            derivation_changed=False, reference_recorded=False, providers_used=(),
            attempts=attempts)

    script = []
    monkeypatch.setattr(embedjob, "catch_up", lambda conn, *, space, max_batch: script.pop(0))
    monkeypatch.setattr(dbconn, "serving_connection",
                        lambda dsn=None: contextlib.nullcontext(None))
    monkeypatch.setattr(store, "configured_space", lambda: None)

    def run(*flags, reports):
        script[:] = reports
        capsys.readouterr()
        rc = meshsem_cli.main(["catch-up", *flags])
        return rc, len(reports) - len(script), capsys.readouterr().out

    assert run(reports=[report(2, more=True), report(1, more=False)])[:2] == (0, 1)
    assert run("--until-done", reports=[report(2, more=True), report(1, more=False),
                                        report(9, more=True)])[:2] == (0, 2)
    assert run("--until-done", reports=[report(2, more=True), report(0, more=True),
                                        report(9, more=True)])[:2] == (0, 2)
    refused = ({"id": "a", "verdict": "admitted"}, {"id": "b", "verdict": "refused", "reason": "canary"})
    rc, ticks, out = run("--until-done", reports=[
        report(1, more=True, stopped="provider_unavailable", attempts=refused),
        report(9, more=False)])
    assert (rc, ticks) == (1, 1)
    assert out.splitlines()[0].endswith(", STOPPED: provider_unavailable, MORE PENDING"), out
    assert out.splitlines()[-1] == "  provider b: refused canary", out
    assert "provider a" not in out


# ================================================= the written rules are the code's rules

#: Every place the ranking and degradation rules are written down. A rule restated in one
#: place and not the others is how two of them drifted from the code (review of c66d7af).
RANKING_DOCS = ("apps/meshsem/fusion.py", "apps/meshsem/search.py",
                "apps/meshsem/README.md", "docs/DIVERGENCES.md")
DEGRADATION_DOCS = ("apps/meshsem/search.py", "apps/meshsem/README.md",
                    "docs/DIVERGENCES.md")


def _flat(rel: str) -> str:
    """The file's text, lowercased, every run of whitespace one space: a phrase is found
    wherever its line breaks fall."""
    return " ".join((ROOT / rel).read_text(encoding="utf-8").split()).lower()


def _candidate(key: str) -> search.Candidate:
    return search.Candidate(src_id=key, key=key, version=1, recorded_at=None, author=None,
                            thread_id=None, body=key, body_truncated=False, fused_score=0.0,
                            ranks={}, cosine=None, lexical_score=None)


def test_rerank_in_the_method_means_a_reranker_scored_the_pool_not_that_it_moved_it(
        monkeypatch):
    """`+rerank` follows `reranked`, which is providers.Reranked.ran: a reranker SCORED the
    pool. Scores that keep the fused order still earn it (the order is then the reranker's
    as well). Four documents said "+rerank only when a reranker reordered the pool", a
    condition the code never checks; they now state the rule it runs. Control, one variable
    apart: rerank not asked, no reranker is called and the method does not say rerank."""
    pool = [_candidate(k) for k in ("a", "b", "c")]
    called = []

    def scores_in_the_fused_order(query, documents, **_kw):
        called.append(list(documents))
        return providers.Reranked([0.9, 0.5, 0.1], "rerank-x",
                                  [{"id": "rerank-x", "verdict": "ran"}])

    monkeypatch.setattr(search.providers, "rerank", scores_in_the_fused_order)
    arms = [search.Arm(L, True, _r(L, "abc")), search.Arm(E, True, _r(E, "abc"))]

    ranked, rr = search._rerank("q", list(pool), True)
    assert [c.key for c in ranked] == ["a", "b", "c"], "the reranker kept the fused order"
    assert rr.ran and called == [["a", "b", "c"]]
    assert search._ranking_fields(arms, reranked=rr.ran)["method"] == (
        "rrf(lexical,embed)+rerank")

    ranked, rr = search._rerank("q", list(pool), False)
    assert rr is None and len(called) == 1, "control: not asked, not called"
    assert search._ranking_fields(arms, reranked=False)["method"] == "rrf(lexical,embed)"

    for rel in RANKING_DOCS:
        text = _flat(rel)
        assert "reordered the pool" not in text, f"{rel} states the old rule"
        assert "only when a reranker scored the pool" in text, f"{rel} lacks the rule"


def test_the_docs_say_context_answers_200_where_search_answers_503():
    """With the embed arm unable to run and the lexical arm matching nothing, search answers
    the embed arm's typed 503, and context answers 200 with `related.absent` =
    "embed_unavailable": its anchor, thread and reply chain are still an answer. The route
    tests hold the code to both (tests/test_meshsem_routes.py,
    test_context_with_no_embedder_and_no_lexical_match_says_why_related_is_empty and
    test_an_unavailable_embedder_is_a_typed_503_with_coverage); this holds the docs to the
    code. Divergence 026 said context "degrades the same way" as search."""
    for rel in DEGRADATION_DOCS:
        text = _flat(rel)
        assert "degrades the same way" not in text, f"{rel}: context degrades like search"
        assert "`context` answers 200 with `related.absent`" in text, (
            f"{rel} does not say what context answers when nothing is related")


# ========================================================= the lexical arm's TF-IDF

def _reference_tfidf(docs: dict[str, dict[str, int]], query: dict[str, int]) -> dict:
    """The A/B harness's formula (scratchpad ab_eval.tfidf_vectors), written again with
    dicts: tf * (ln((1+n)/(1+df)) + 1), L2 per document, cosine with the query."""
    n = len(docs)
    df: dict[str, int] = {}
    for tf in docs.values():
        for w in tf:
            df[w] = df.get(w, 0) + 1

    def vec(tf):
        v = {w: c * (math.log((1 + n) / (1 + df.get(w, 0))) + 1.0) for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}

    q = vec(query)
    return {d: sum(q[w] * x for w, x in vec(tf).items() if w in q) for d, tf in docs.items()}


DOCS = {
    "d1": {"lease": 2, "renewal": 1, "handover": 1},
    "d2": {"lease": 1, "uxxx": 3},
    "d3": {"uxxx": 1, "coast": 1, "the": 4},
    "d4": {"the": 2, "node": 1, "restart": 1},
    "d5": {"unrelated": 1},
}


def _index(docs=DOCS) -> lexical.LexicalIndex:
    idx = lexical.LexicalIndex("unit")
    for d, tf in docs.items():
        idx.put(d, f"sha-{d}", list(tf), list(tf.values()))
    return idx


def test_the_lexical_scores_equal_the_ab_harness_formula():
    query = {"lease": 1, "the": 1, "uxxx": 2}
    want = _reference_tfidf(DOCS, query)
    ids, scores = _index().rank(list(query), list(query.values()), limit=10)
    got = dict(zip(ids, scores.tolist()))
    assert set(got) == {d for d, s in want.items() if s > 0}, "zero scores are not ranked"
    for d, s in got.items():
        assert s == pytest.approx(want[d], rel=1e-12), d
    assert ids == sorted(got, key=lambda d: -got[d])
    assert "d5" not in ids, "a document sharing no term is not ranked by this arm"


def test_idf_downweights_a_common_word():
    """'the' is in two of five documents, 'restart' in one: a query of both prefers the
    rare word's document even though the common word appears twice as often there."""
    ids, _ = _index().rank(["the", "restart"], [1, 1], limit=10)
    assert ids[0] == "d4"
    ids, _ = _index().rank(["the"], [1], limit=10)
    assert ids == ["d3", "d4"], "control: on 'the' alone, the doc with more of it leads"


def test_an_unknown_query_term_changes_no_order_and_limit_bites():
    base_ids, base = _index().rank(["lease", "uxxx"], [1, 1], limit=10)
    more_ids, more = _index().rank(["lease", "uxxx", "zzunknown"], [1, 1, 3], limit=10)
    assert base_ids == more_ids and all(m < b for m, b in zip(more, base))
    assert _index().rank(["lease", "uxxx"], [1, 1], limit=1)[0] == base_ids[:1]
    assert _index().rank([], [], limit=10)[0] == []
    assert lexical.LexicalIndex("empty").rank(["lease"], [1], limit=10)[0] == []


def test_a_changed_digest_replaces_a_document_and_an_unchanged_one_is_a_noop():
    idx = _index()
    assert idx.rank(["coast"], [1], limit=10)[0] == ["d3"]
    idx.put("d3", "sha-d3", ["other"], [1])            # same digest: nothing changes
    assert idx.rank(["coast"], [1], limit=10)[0] == ["d3"]
    idx.put("d3", "sha-d3-v2", ["other"], [1])         # new digest: replaced
    assert idx.rank(["coast"], [1], limit=10)[0] == []
    assert idx.rank(["other"], [1], limit=10)[0] == ["d3"] and idx.n == len(DOCS)
    with pytest.raises(ValueError):
        idx.put("d9", "sha", ["a", "b"], [1])


# ======================================================================= the tokenizer

def test_terms_are_the_ab_harness_tokens_with_their_frequencies():
    """lexical.terms is the A/B harness's tokenizer ([a-z0-9_]{2,} on lower()) on ASCII
    text, which is what made its TF-IDF 0.572 / 0.541; Postgres to_tsvector('simple')
    measured 0.545 / 0.504 on the same queries (divergence 026). Pinned: lowercase, no
    stemming, one-character words dropped, snake_case whole, paths and dotted names split
    into their parts, frequencies counted."""
    import re

    text = ("Running TESTS: see apps/meshsem/search.py and mesh_embed_catch_up; "
            "a b x1 v0.27.0 tests e-mail tests")
    lexemes, tfs = lexical.terms(text)
    assert dict(zip(lexemes, tfs)) == {
        "running": 1, "tests": 3, "see": 1, "apps": 1, "meshsem": 1, "search": 1,
        "py": 1, "and": 1, "mesh_embed_catch_up": 1, "x1": 1, "v0": 1, "27": 1,
        "mail": 1}
    assert lexemes == sorted(lexemes)
    harness = re.compile(r"[a-z0-9_]{2,}").findall(text.lower())
    assert sorted(w for w, n in zip(lexemes, tfs) for _ in range(n)) == sorted(harness)
    assert lexical.terms("") == ([], []) and lexical.terms("- . !") == ([], [])
    assert lexical.terms("Café naïve") == (["café", "naïve"], [1, 1]), (
        "non-ASCII letters are words here (the harness dropped them)")


def test_the_026_amendment_says_what_its_evidence_shows_and_what_it_does_not():
    """The review of fin/read found three things the amendment left unsaid: WHICH sign test
    "not significant on its own" meant (5 wins, 0 losses: p = 0.0625 two-sided, 0.031
    one-sided), that nothing behind the live figures is committed, and that 026's Proof
    and Not-changed paragraphs are history too, not only its title and one bullet. And the
    cooldown it now documents is the constant the code uses. Each is read from the text a
    reader reads; the unqualified claim must be gone, not merely joined by the new one.
    Kills: any of those sentences reverted."""
    import re

    def flat(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    doc = (ROOT / "docs" / "DIVERGENCES.md").read_text(encoding="utf-8")
    entry = flat(doc[doc.index("\n## 026,"):doc.index("\n## 027,")])
    assert "a sign test of p = 0.0625 two-sided (0.031 one-sided" in entry
    assert "not significant on its own (0 losses, 5 wins)" not in entry
    assert "its script, its 12 queries and its log are not committed" in entry
    assert ("The title above, the \"Reranking is OPT-IN\" bullet, and the **Proof** and "
            "**Not changed** paragraphs") in entry
    assert "(`providers.RERANK_COOLDOWN_S`, 30 s)" in entry
    assert providers.RERANK_COOLDOWN_S == 30.0
    readme = flat((ROOT / "apps" / "meshsem" / "README.md").read_text(encoding="utf-8"))
    for text in (readme, flat(search.__doc__)):
        assert "p = 0.0625 two-sided" in text
        assert "not significant on its own" not in text
