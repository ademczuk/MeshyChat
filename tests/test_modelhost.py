"""Node-local model capabilities, and the rule that health is not capability.

THE CASE THIS FILE IS BUILT AROUND IS REAL AND WAS MEASURED, not imagined. On this node a
llama.cpp server hosts a RERANKER at 127.0.0.1:8100, started `--reranking --pooling rank`.
It answers `GET /health` with `{"status":"ok"}` and it answers `POST /v1/embeddings` with
HTTP 200 and a full-length vector of signed zeros and nulls, because the model has no
embedding head and the server replies anyway.

A registry that advertised "this node can embed" on the strength of that 200 would have
every consumer indexing zero vectors. Cosine similarity over zeros is not an error - it is
a number - so nothing downstream would ever report a fault.

Every test therefore runs against a FAKE provider served by a real local HTTP server, so
the probe exercises its actual transport, parsing and decision path. The fakes reproduce
the measured shapes, including the degenerate one, byte for byte.

Run: .venv/Scripts/python.exe -m pytest tests/test_modelhost.py -q
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from apps.modelhost import probes, registry  # noqa: E402

DIM = 128


def _vec(seed: float, dim: int = DIM) -> list[float]:
    """A deterministic unit-ish vector. Two nearby seeds give two similar vectors, which is
    what lets the fake embedder be genuinely discriminating rather than merely non-zero."""
    return [math.sin(seed + i * 0.05) for i in range(dim)]


class _Handler(BaseHTTPRequestHandler):
    behaviour = "good"

    def log_message(self, *_args):            # keep the test output readable
        pass

    def _send(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):                        # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        b = type(self).behaviour

        if self.path.endswith("/rerank"):
            docs = payload.get("documents") or []
            if b == "rerank_constant":
                # Every document scored identically. A shape check passes this.
                return self._send({"results": [{"index": i, "relevance_score": 0.5}
                                               for i in range(len(docs))]})
            if b == "rerank_inverted":
                # Rank the FIRST document top. The probe puts the relevant one LAST, so a
                # server that echoes input order is caught.
                return self._send({"results": [
                    {"index": i, "relevance_score": float(len(docs) - i)}
                    for i in range(len(docs))]})
            if b == "rerank_malformed":
                return self._send({"results": [{"index": i} for i in range(len(docs))]})
            # GOOD: score by whether the doc is the RELEVANT one for the case being
            # asked. The probe now sends two cases with different subjects, so a fake that
            # keys on one word ("key") passes case 1 and fails case 2 - which is exactly the
            # discrimination the real probe is testing for, so the fake has to have it too.
            relevant = {rel for _q, rel, _d in probes._RERANK_CASES}
            out = []
            for i, doc in enumerate(docs):
                score = 5.0 if doc in relevant else -5.0
                out.append({"index": i, "relevance_score": score})
            return self._send({"results": out})

        if self.path.endswith("/v1/embeddings"):
            text = payload.get("input") or ""
            if b == "embed_zeros":
                # THE MEASURED FAILURE: right shape, signed zeros, a null in the middle.
                v = [-0.0] * DIM
                v[24] = None
                return self._send({"data": [{"embedding": v}]})
            if b == "embed_constant":
                return self._send({"data": [{"embedding": [0.7] * DIM}]})
            if b == "embed_undiscriminating":
                return self._send({"data": [{"embedding": _vec(1.0)}]})   # same for all
            if b == "embed_short":
                return self._send({"data": [{"embedding": [0.1, 0.2, 0.3]}]})
            seed = {probes.EMBED_ANCHOR: 1.0, probes.EMBED_RELATED: 1.02,
                    probes.EMBED_UNRELATED: 3.4}.get(text, 2.0)
            return self._send({"data": [{"embedding": _vec(seed)}]})

        if self.path.endswith("/v1/chat/completions"):
            if b == "generate_empty":
                return self._send({"choices": [{"message": {"content": "   "}}]})
            return self._send({"choices": [{"message": {"content": "ready"}}]})

        return self._send({"error": "not found"}, status=404)


@pytest.fixture(autouse=True)
def _no_cached_verdict_between_tests():
    """Clear the registry's cache around EVERY test.

    The rate cache is module-level, which is global state, and global state shared between
    tests is how one test's verdict silently becomes another's fixture. It bit immediately:
    adding the cache turned four passing tests red because they inherited a snapshot taken
    by whichever test ran first.

    Clearing BOTH sides - before and after - so the order of this file cannot matter and so
    a test that deliberately populates the cache cannot leak into the next one.
    """
    registry.forget()
    yield
    registry.forget()


@pytest.fixture()
def fake():
    """A real HTTP server, so the probe's transport and parsing are exercised too."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Ctl:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        @staticmethod
        def behave(mode):
            _Handler.behaviour = mode

    Ctl.behave("good")
    yield Ctl
    server.shutdown()
    server.server_close()
    _Handler.behaviour = "good"


# --------------------------------------------------------------------- rerank

def test_a_working_reranker_is_advertised(fake):
    """POSITIVE CONTROL FIRST. Every refusal below is only meaningful against this."""
    result = probes.probe_rerank(fake.base_url)
    assert result.ok, result.reason
    # BOTH cases must have ranked the gold document first - a probe that checked only the
    # first case would be the one-coin-flip version this replaced.
    assert result.detail["case1_top"] == result.detail["case1_gold"]
    assert result.detail["case2_top"] == result.detail["case2_gold"]


def test_a_constant_scoring_reranker_is_refused(fake):
    """Identical scores are not a ranking. This is the shape-valid failure a response
    schema check cannot see: two results, both with an index and a float."""
    fake.behave("rerank_constant")
    result = probes.probe_rerank(fake.base_url)
    assert not result.ok
    assert "first" in result.reason


def test_a_reranker_that_prefers_the_irrelevant_document_is_refused(fake):
    fake.behave("rerank_inverted")
    assert not probes.probe_rerank(fake.base_url).ok


def test_a_reranker_missing_its_scores_is_refused(fake):
    fake.behave("rerank_malformed")
    result = probes.probe_rerank(fake.base_url)
    assert not result.ok and "relevance_score" in result.reason


# ---------------------------------------------------------------------- embed

def test_a_working_embedder_is_advertised(fake):
    result = probes.probe_embed(fake.base_url)
    assert result.ok, result.reason
    assert result.detail["cos_related"] > result.detail["cos_unrelated"]
    assert result.detail["dim"] == DIM


def test_the_measured_zero_vector_response_is_refused(fake):
    """THE ONE THAT MATTERS. This is the live reranker's /v1/embeddings reply reproduced:
    HTTP 200, correct length, signed zeros, a null at index 24.

    It must be refused, and the reason must name the nulls rather than something vaguer,
    because an operator reading `capabilities` needs to know the endpoint is answering with
    rubbish rather than that it is down - those have different fixes.
    """
    fake.behave("embed_zeros")
    result = probes.probe_embed(fake.base_url)
    assert not result.ok
    assert "null" in result.reason, result.reason


def test_a_constant_embedding_is_refused(fake):
    """No nulls, no zeros, right length - and still meaningless, because every dimension
    carries the same value so every pair of texts is identical."""
    fake.behave("embed_constant")
    result = probes.probe_embed(fake.base_url)
    assert not result.ok
    assert "constant" in result.reason or "discriminate" in result.reason


def test_an_embedder_that_returns_one_vector_for_every_input_is_refused(fake):
    """The subtlest failure: well-formed, varied, non-degenerate vectors that simply do not
    depend on the input. Only the discrimination check rejects this."""
    fake.behave("embed_undiscriminating")
    result = probes.probe_embed(fake.base_url)
    assert not result.ok
    assert "discriminate" in result.reason


def test_an_undersized_embedding_is_refused(fake):
    fake.behave("embed_short")
    result = probes.probe_embed(fake.base_url)
    assert not result.ok and "below" in result.reason


# ------------------------------------------------------------------- generate

def test_generation_probe_accepts_text_and_refuses_whitespace(fake):
    assert probes.probe_generate(fake.base_url).ok
    fake.behave("generate_empty")
    result = probes.probe_generate(fake.base_url)
    assert not result.ok and "empty" in result.reason


# ------------------------------------------------------------- unreachable

def test_an_unreachable_provider_is_refused_and_says_so_without_raising():
    """A probe must never raise: it would take the registry down with the thing it is
    measuring, and a node that cannot report its capabilities is worse than one with none.
    The reason must distinguish unreachable from wrong, so an operator knows whether to
    start a process or fix a model."""
    for probe in (probes.probe_rerank, probes.probe_embed, probes.probe_generate):
        result = probe("http://127.0.0.1:9")        # discard port: refuses connections
        assert not result.ok
        assert "unreachable" in result.reason or "transport" in result.reason, result.reason
        assert result.latency_ms is not None


# -------------------------------------------------------------------- registry

def _providers(monkeypatch, entries):
    monkeypatch.setenv("MESHY_MODEL_PROVIDERS", json.dumps(entries))
    from django.test import override_settings
    return override_settings(MESHY_MODEL_PROVIDERS=None)


def test_a_declared_but_broken_provider_is_never_advertised(monkeypatch, fake):
    """DECLARING is not ADVERTISING, and this is the whole contract.

    The registry is given one embed provider that is up, answering 200, and useless. It
    must appear in `providers` (so the operator can see it was tried and why it failed) and
    must NOT appear in `capabilities` (so a consumer routing on that list cannot reach it).
    """
    fake.behave("embed_zeros")
    with _providers(monkeypatch, [{"id": "e1", "kind": "embed",
                                   "base_url": fake.base_url}]):
        snap = registry.snapshot()
    assert snap["declared_count"] == 1
    assert snap["capabilities"] == [], "a broken provider reached the routable list"
    assert snap["providers"][0]["available"] is False
    assert "null" in snap["providers"][0]["evidence"]["reason"]

    # POSITIVE CONTROL: the identical declaration against a working endpoint IS advertised,
    # so the exclusion above is about the probe and not about the wiring.
    #
    # The cache must be dropped first, because this test CHANGES THE WORLD half way
    # through: without it the control reads the verdict taken while the provider was still
    # broken, and the test fails for a reason that has nothing to do with what it asserts.
    # That is the cache's honest cost, and it is why the response carries `age_ms`.
    registry.forget()
    fake.behave("good")
    with _providers(monkeypatch, [{"id": "e1", "kind": "embed",
                                   "base_url": fake.base_url}]):
        snap = registry.snapshot()
    assert snap["capabilities"] == ["embed"]
    assert snap["providers"][0]["available"] is True


def test_configuration_errors_are_reported_rather_than_swallowed(monkeypatch):
    """A provider an operator believes exists, which the registry cannot parse, is exactly
    the capability most worth surfacing - so errors are returned, not logged away."""
    with _providers(monkeypatch, [
        {"id": "a", "kind": "telepathy", "base_url": "http://x"},   # unknown kind
        {"id": "b", "kind": "embed"},                                # missing base_url
        {"id": "c", "kind": "embed", "base_url": "http://x"},
        {"id": "c", "kind": "rerank", "base_url": "http://y"},       # duplicate id
    ]):
        snap = registry.snapshot()
    joined = " | ".join(snap["config_errors"])
    assert "telepathy" in joined
    assert "base_url" in joined
    assert "duplicate" in joined
    assert snap["declared_count"] == 1, "only the one valid provider should be declared"


def test_no_providers_declared_is_distinguishable_from_all_probes_failing(monkeypatch):
    """`capabilities: []` has two very different causes and an operator must be able to
    tell them apart, so the count is stated rather than inferred."""
    monkeypatch.delenv("MESHY_MODEL_PROVIDERS", raising=False)
    from django.test import override_settings
    with override_settings(MESHY_MODEL_PROVIDERS=None):
        snap = registry.snapshot()
    assert snap["capabilities"] == [] and snap["declared_count"] == 0
    assert snap["config_errors"] == []


# ----------------------------------------------------------------- the route

def test_the_route_is_served_by_the_real_urlconf_and_leaks_no_internals(monkeypatch, fake):
    """Resolved through `meshychat.urls`, not a pinned URLconf - a route test that supplies
    its own routing table proves the view and says nothing about the wiring.

    Also asserts what must NOT be on the wire: a base_url would invite a consumer to
    connect to a model port directly, and the arbiter's control URL must never be published
    by an unauthenticated route.
    """
    from django.conf import settings as dj_settings
    from django.test import Client, override_settings

    assert dj_settings.ROOT_URLCONF == "meshychat.urls"

    monkeypatch.setenv("MESHY_MODEL_PROVIDERS",
                       json.dumps([{"id": "r1", "kind": "rerank",
                                    "base_url": fake.base_url, "arbiter_id": "reranker"}]))
    with override_settings(MESHY_MODEL_PROVIDERS=None, ALLOWED_HOSTS=["*"]):
        response = Client().get("/v1/modelhost/capabilities")
    assert response.status_code == 200, response.content
    body = json.loads(response.content)
    assert body["capabilities"] == ["rerank"]

    text = response.content.decode()
    assert fake.base_url not in text, "the provider's base_url reached the wire"
    assert "7878" not in text, "the arbiter control port reached the wire"
    for secret in ("change-me", "postgresql://", "C:\\\\"):
        assert secret not in text

    # Compact JSON, like every other body in this project.
    assert b", " not in response.content


def test_the_route_refuses_the_verbs_that_would_imply_it_changes_something(fake):
    """GET and HEAD only. A POST here would read as "make it so", and this app deliberately
    never starts a model - that is the VRAM arbiter's job, on a shared lease."""
    from django.test import Client, override_settings

    with override_settings(ALLOWED_HOSTS=["*"]):
        client = Client()
        assert client.get("/v1/modelhost/capabilities").status_code == 200
        assert client.post("/v1/modelhost/capabilities").status_code == 405
        assert client.delete("/v1/modelhost/capabilities").status_code == 405


# ------------------------------------------------------------------- bounds

def test_the_sweep_is_bounded_and_says_which_providers_it_skipped(monkeypatch):
    """A SLOW provider must not become an unbounded request.

    Measured before this bound existed: one provider pointed at a blackholed host held the
    request 8,064 ms. The route is unauthenticated, so that was a hold anyone could repeat.

    The skipped entries must say they were SKIPPED rather than be reported absent: "we did
    not look" and "we looked and it is not there" have different fixes, and quietly
    reporting the second is how a registry lies politely.
    """
    import time as _t

    from apps.modelhost import registry as reg

    reg.forget()
    entries = [{"id": f"slow{i}", "kind": "embed", "base_url": "http://10.255.255.1:8100"}
               for i in range(4)]
    monkeypatch.setenv("MESHY_MODEL_PROVIDERS", json.dumps(entries))
    from django.test import override_settings

    started = _t.monotonic()
    with override_settings(MESHY_MODEL_PROVIDERS=None):
        snap = reg.snapshot(budget_s=1.0, ttl_s=0.0)
    elapsed = _t.monotonic() - started

    # THE BUDGET IS THE BOUND, asserted tightly, because a loose assertion would have
    # passed the weaker version this replaced. Measured against three blackholed providers:
    # 24.0s with no bound, 16.1s when the deadline was only checked BETWEEN providers, and
    # 10.0s once each probe was given only the remaining budget. A `< TIMEOUT_S + 2` style
    # assertion is green for all three, which makes it evidence of nothing.
    assert elapsed < 1.0 + 1.5, f"sweep took {elapsed:.1f}s against a 1.0s budget"
    assert snap["budget_exhausted"] is True
    skipped = [p for p in snap["providers"]
               if "not probed" in p["evidence"]["reason"]]
    assert skipped, "nothing was reported as skipped, so the budget did not engage"
    assert snap["declared_count"] == 4, "every declared provider must still be listed"
    reg.forget()


def test_a_provider_the_budget_skipped_reports_the_fields_a_probed_one_does(fake):
    """A skipped entry and a probed entry are ONE shape in one key order: a consumer
    reading `providers` sees the same fields whether or not this sweep reached the
    provider, and a field added for one (space, locality, arbiter_id) is reported for the
    other. Until `_report` held the one literal, `probe_one` and `_not_probed` each
    spelled their own and nothing compared them."""
    provider = registry.Provider(id="r1", kind="rerank", base_url=fake.base_url,
                                 model="bge-reranker-v2-m3", arbiter_id="reranker")
    why = "not probed: the 10s sweep budget was spent on earlier providers"
    probed = registry.probe_one(provider)
    skipped = registry._not_probed(provider, why)
    assert list(probed) == list(skipped), "the two entries differ in fields or their order"
    assert {k for k in probed if probed[k] == skipped[k]} == \
        set(probed) - {"available", "evidence"}
    assert probed["available"] is True, probed["evidence"]
    assert skipped["available"] is False
    assert skipped["evidence"] == {"ok": False, "reason": why}
    assert probed["arbiter_id"] == skipped["arbiter_id"] == "reranker"
    assert "base_url" not in probed, "the provider's base_url reached the entry"


def test_repeat_requests_do_not_multiply_probe_work(monkeypatch, fake):
    """The RATE bound. Without it, request volume multiplies GPU work on a single card."""
    from apps.modelhost import registry as reg

    calls = {"n": 0}
    real = reg.probe_one
    def counting(provider, *args, **kwargs):
        calls["n"] += 1
        return real(provider, *args, **kwargs)

    monkeypatch.setattr(reg, "probe_one", counting)

    reg.forget()
    monkeypatch.setenv("MESHY_MODEL_PROVIDERS",
                       json.dumps([{"id": "r", "kind": "rerank", "base_url": fake.base_url}]))
    from django.test import override_settings
    with override_settings(MESHY_MODEL_PROVIDERS=None):
        first = reg.snapshot(ttl_s=30.0)
        second = reg.snapshot(ttl_s=30.0)
        third = reg.snapshot(ttl_s=30.0)

    assert calls["n"] == 1, f"probed {calls['n']} times for three requests"
    assert first["capabilities"] == second["capabilities"] == third["capabilities"]

    # POSITIVE CONTROL: the cache is not a permanent freeze - a zero TTL probes again, so
    # the count above reflects caching rather than a probe that silently stopped running.
    with override_settings(MESHY_MODEL_PROVIDERS=None):
        reg.snapshot(ttl_s=0.0)
    assert calls["n"] == 2
    reg.forget()


def test_every_response_is_dated_so_a_cached_claim_can_be_judged(monkeypatch, fake):
    """A cache turns the answer into a claim about the past. That is only honest if the
    caller can see HOW far past - otherwise this app reproduces the very failure it exists
    to prevent, one layer up."""
    from django.test import Client, override_settings

    from apps.modelhost import registry as reg

    reg.forget()
    monkeypatch.setenv("MESHY_MODEL_PROVIDERS",
                       json.dumps([{"id": "r", "kind": "rerank", "base_url": fake.base_url}]))
    with override_settings(MESHY_MODEL_PROVIDERS=None, ALLOWED_HOSTS=["*"]):
        body = json.loads(Client().get("/v1/modelhost/capabilities").content)
    assert "measured_at" in body and body["measured_at"].endswith("+00:00")
    assert isinstance(body["age_ms"], int) and body["age_ms"] >= 0
    reg.forget()


def test_capabilities_answer_while_the_database_is_unreachable(monkeypatch, fake):
    """THE CLAIM THIS APP MAKES IN ITS OWN DOCSTRING, now actually tested.

    `apps/modelhost` holds no database handle, and the stated reason is that an operator
    most wants to know what a node can still do exactly when Postgres is down. That was an
    assertion in a comment until this test existed - and the request path has a middleware
    in front of the view, so "this module imports no psycopg" would not have settled it.

    THE CONTROL IS THE LOAD-BEARING HALF: a route that genuinely needs the database must
    FAIL under the same settings. Without that, this test passes just as happily when the
    database is perfectly healthy and proves nothing at all.
    """
    from django.test import Client, override_settings

    # `connect_timeout=1` IS LOAD-BEARING, not tidiness. Measured: a closed localhost port
    # on this platform does NOT refuse, it TIMES OUT, so an unbounded DSN made this single
    # test take 130 s - and a two-minute test is one somebody eventually marks skip.
    dead = "postgresql://nobody:nothing@127.0.0.1:1/dead?connect_timeout=1"
    monkeypatch.setenv("MESHY_MODEL_PROVIDERS",
                       json.dumps([{"id": "r", "kind": "rerank",
                                    "base_url": fake.base_url}]))
    with override_settings(MESHY_DSN=dead, MESHY_MODEL_PROVIDERS=None,
                           ALLOWED_HOSTS=["*"]):
        client = Client()

        control = client.get("/v1/peers/presence")
        assert control.status_code != 200, (
            "control failed: a database-backed route answered 200 while pointed at a dead "
            f"DSN, so the database was reachable and this test proves nothing "
            f"(got {control.status_code})")

        response = client.get("/v1/modelhost/capabilities")

    assert response.status_code == 200, response.content
    body = json.loads(response.content)
    assert body["capabilities"] == ["rerank"], body
    # And the dead DSN must not leak into the body on the way past.
    assert "nobody:nothing" not in response.content.decode()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
