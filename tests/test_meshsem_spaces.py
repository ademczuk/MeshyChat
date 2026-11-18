"""Embedding-space admission and the MUST-8 locality verdict. No database, no network.

Divergence 019. The two checks here decide what may be COMPARED (space admission) and
where mesh content may GO (locality). Each negative has its positive control beside it,
and the admission threshold is pinned from both sides so a mutation to the comparison
or the constant turns a test red.

Run:  pytest tests/test_meshsem_spaces.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.meshsem import spaces  # noqa: E402
from apps.modelhost import locality  # noqa: E402

DIMS = 64


def _space(dims: int = DIMS) -> spaces.Space:
    return spaces.Space(name=f"fake/{dims}/unit", family="fake", dims=dims,
                        document_prompt="title: none | text: {text}",
                        query_prompt="task: search result | query: {text}",
                        canaries=spaces.EMBEDDINGGEMMA_300M.canaries)


def _unit_rows(n: int, dims: int, seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal((n, dims))
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _at_cosine(ref: np.ndarray, target: float, seed: int) -> np.ndarray:
    """Rows each at EXACTLY `target` cosine to the matching reference row: target*r +
    sqrt(1-target^2)*o, with o a unit vector orthogonal to r."""
    out = []
    rng = np.random.default_rng(seed)
    for r in ref:
        o = rng.standard_normal(r.shape[0])
        o -= o.dot(r) * r
        o /= np.linalg.norm(o)
        out.append(target * r + np.sqrt(1.0 - target * target) * o)
    return np.asarray(out)


# ------------------------------------------------------------------ admission

def test_same_canaries_are_admitted_and_a_different_768d_model_is_refused():
    """THE POSITIVE CONTROL AND THE CASE THE SPACE EXISTS FOR, at the real width.

    Same model on another GPU (cosine ~0.9997, the measured HQB/HQD distance) is
    admitted; a different model that also returns 768-d unit vectors - which
    apps/modelhost's embed probe would pass - is refused as a different space.
    """
    space = spaces.EMBEDDINGGEMMA_300M
    n = len(space.canaries)
    ref = _unit_rows(n, space.dims, seed=1)
    same_model_other_gpu = _at_cosine(ref, 0.9997, seed=2).astype(np.float32)
    other_model = _unit_rows(n, space.dims, seed=99).astype(np.float32)

    ok = spaces.admit(space, same_model_other_gpu, ref)
    assert ok.ok, ok.reason
    assert ok.min_cosine == pytest.approx(0.9997, abs=1e-5)

    refused = spaces.admit(space, other_model, ref)
    assert not refused.ok and "DIFFERENT space" in refused.reason
    assert refused.min_cosine < 0.5


def test_the_threshold_is_pinned_from_both_sides():
    """0.9990005 admitted, 0.9989995 refused, 0.998 refused, 0.9995 admitted.

    Kills: `>=` flipped to `<`, the constant lowered (0.99, 0.9) or raised (0.9995+), and
    `min` replaced by `mean` (the one-bad-canary case below).
    """
    space = _space()
    ref = _unit_rows(len(space.canaries), DIMS, seed=3)
    for target, expect in ((0.9995, True), (0.9990005, True), (0.9989995, False),
                           (0.998, False)):
        got = spaces.admit(space, _at_cosine(ref, target, seed=4), ref)
        assert got.ok is expect, (target, got)


def test_one_bad_canary_refuses_even_when_the_mean_would_pass():
    """Four canaries identical to the reference and one at 0.996: the MEAN is 0.9992,
    above the threshold, and the provider must still be refused. The first draft put
    the bad canary at 0.99, where the mean (0.998) also refused, so a `min` -> `mean`
    mutation survived it; the numbers are now chosen so the two disagree."""
    space = _space()
    ref = _unit_rows(len(space.canaries), DIMS, seed=5)
    cand = ref.copy()
    assert spaces.admit(space, cand, ref).ok, "positive control: all canaries match"
    cand[2] = _at_cosine(ref[2:3], 0.996, seed=7)[0]
    cos = np.sum(cand * ref, axis=1)
    assert cos.mean() >= spaces.ADMISSION_COSINE > cos.min(), "the case must split them"
    assert not spaces.admit(space, cand, ref).ok


def test_malformed_canaries_are_refused_before_any_comparison():
    space = _space()
    ref = _unit_rows(len(space.canaries), DIMS, seed=8)
    good = _at_cosine(ref, 0.9999, seed=9)
    assert spaces.admit(space, good, ref).ok
    wrong_width = _unit_rows(len(space.canaries), DIMS + 1, seed=10)
    assert "expected" in spaces.admit(space, wrong_width, ref).reason
    zeros = good.copy()
    zeros[0] = 0.0
    assert "no direction" in spaces.admit(space, zeros, ref).reason
    nan = good.copy()
    nan[1, 3] = np.nan
    assert "NaN" in spaces.admit(space, nan, ref).reason
    assert "expected" in spaces.admit(space, good[:-1], ref).reason


def test_a_constant_embedder_may_not_become_the_reference():
    """The first provider records the reference; a provider returning one vector for
    everything would make every later constant provider 'match'."""
    space = _space()
    distinct = _unit_rows(len(space.canaries), DIMS, seed=11)
    assert spaces.may_become_reference(space, distinct).ok, "positive control"
    constant = np.tile(distinct[0], (len(space.canaries), 1))
    got = spaces.may_become_reference(space, constant)
    assert not got.ok and "discriminate" in got.reason


def test_the_real_canaries_prove_both_prompt_styles_and_their_digest_moves_with_them():
    space = spaces.EMBEDDINGGEMMA_300M
    inputs = space.canary_inputs()
    assert sum(i.startswith("title: none | text: ") for i in inputs) == 3
    assert sum(i.startswith("task: search result | query: ") for i in inputs) == 2
    edited = spaces.Space(**{**space.__dict__,
                             "canaries": space.canaries[:-1] + (("query", "edited"),)})
    assert edited.canary_sha256() != space.canary_sha256()
    assert spaces.get(space.name) is space
    with pytest.raises(KeyError):
        spaces.get("embeddinggemma-300m/768/retrieval-v2")


def test_vectors_round_trip_as_float32_little_endian():
    v = np.linspace(-1, 1, DIMS).astype(np.float32)
    raw = spaces.encode(v)
    assert len(raw) == 4 * DIMS and raw[:4] == np.float32(-1.0).tobytes()
    assert np.array_equal(spaces.decode(raw, DIMS), v)
    with pytest.raises(ValueError):
        spaces.decode(raw, DIMS + 1)


# ------------------------------------------------------------------ MUST-8 locality

@pytest.mark.parametrize("url,kind", [
    ("http://127.0.0.1:8100", "loopback"),
    ("http://127.9.9.9", "loopback"),
    ("http://[::1]:8103", "loopback"),
    ("http://192.168.50.10:7875", "rfc1918"),
    ("https://10.1.2.3:443", "rfc1918"),
    ("http://172.16.0.1", "rfc1918"),
    ("http://172.31.255.254", "rfc1918"),
])
def test_loopback_and_rfc1918_hosts_may_receive_content(url, kind):
    got = locality.classify(url)
    assert got.content_ok and got.kind == kind, got


@pytest.mark.parametrize("url,why", [
    ("http://8.8.8.8", "neither"),
    ("https://203.0.113.7:443", "neither"),
    ("http://172.32.0.1", "neither"),           # one past 172.16/12
    ("http://100.64.0.1", "neither"),           # carrier NAT: is_private, not RFC1918
    ("http://169.254.1.1", "neither"),          # link-local
    ("http://[fd00::1]", "neither"),            # IPv6 ULA
    ("http://localhost:8100", "a name"),        # names are resolved after the check
    ("http://api.example.com", "a name"),
    ("http://2130706433/", "a name"),           # integer spelling of 127.0.0.1
    ("http://user:pw@10.0.0.1", "credentials"),
    ("ftp://10.0.0.1", "scheme"),
    ("http://10.0.0.1:notaport", "parse"),
    ("", "empty"),
])
def test_everything_else_is_refused_with_its_reason(url, why):
    got = locality.classify(url)
    assert not got.content_ok and got.kind == "refused" and why in got.reason, got


def test_the_default_space_is_retrieval_and_the_raw_space_stays_a_different_space():
    """Divergence 026, amended 2026-09-26 (the operator's decision on F03 and F04): the
    default space is the RETRIEVAL-prompt space, documents "title: none | text: ...",
    queries "task: search result | query: ...". The raw-text space, the default from
    2026-09-23 to 2026-09-26, stays declared, so its vectors and a deployment that pins
    MESHY_SEMANTIC_SPACE to it keep working (positive control: it still resolves, and
    still sends the text with no prompt). The prompt style stays part of the identity: a
    different name AND a different canary digest, so a reference recorded under one can
    never vouch for the other."""
    raw, retrieval = spaces.EMBEDDINGGEMMA_300M_RAW, spaces.EMBEDDINGGEMMA_300M
    assert spaces.DEFAULT_SPACE == retrieval.name == "embeddinggemma-300m/768/retrieval-v1"
    assert retrieval.query("q") == "task: search result | query: q"
    assert retrieval.document("d") == "title: none | text: d"
    assert raw.name == "embeddinggemma-300m/768/raw-v1" and raw.name != spaces.DEFAULT_SPACE
    assert raw.canary_inputs() == [sentence for _, sentence in raw.canaries]
    assert raw.query("q") == "q" and raw.document("d") == "d"
    assert (raw.family, raw.dims, raw.canaries) == (retrieval.family, retrieval.dims,
                                                    retrieval.canaries)
    assert raw.canary_sha256() != retrieval.canary_sha256()
    assert spaces.get(raw.name) is raw and spaces.get(retrieval.name) is retrieval
