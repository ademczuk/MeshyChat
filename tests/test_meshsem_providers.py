"""The content chokepoint: MUST-8, proxies, redirects, space failover, rerank, chat.

No database. Every provider is a REAL local HTTP server (tests/fixtures/meshsem_fake.py),
so these tests exercise the transport and parsing the production path uses. Where a test
proves content did NOT go somewhere, the somewhere is a recording server that must see
nothing, and the same test shows a request that DID arrive, so "nothing arrived" cannot
be the fixture failing to listen.

Run:  pytest tests/test_meshsem_providers.py -q
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.meshsem import providers, spaces  # noqa: E402
from apps.modelhost import probes, registry  # noqa: E402
from tests.fixtures.meshsem_fake import FakeModelHost, Recorder  # noqa: E402

DIMS = 64
SPACE = spaces.Space(name="fake-64/64/prov", family="fake", dims=DIMS,
                     document_prompt="title: none | text: {text}",
                     query_prompt="task: search result | query: {text}",
                     canaries=spaces.EMBEDDINGGEMMA_300M.canaries)


@pytest.fixture()
def hosts():
    made = []

    def make(**kw):
        h = FakeModelHost(dims=DIMS, **kw)
        made.append(h)
        return h
    yield make
    for h in made:
        h.close()


@pytest.fixture()
def recorder():
    r = Recorder()
    yield r
    r.close()


def _declare(*entries):
    return override_settings(MESHY_MODEL_PROVIDERS=list(entries))


def _embed(pid, url, space=SPACE.name):
    return {"id": pid, "kind": "embed", "base_url": url, "space": space}


def _reference(host: FakeModelHost) -> np.ndarray:
    """What the job would record from `host`: its canary vectors."""
    with _declare(_embed("ref", host.base_url)):
        got = providers.embed_in_space(SPACE, [], None, record_if_unreferenced=True)
    return got.canary_vectors


# ------------------------------------------------------------------ MUST-8

def test_a_name_that_points_at_a_working_provider_receives_nothing(hosts):
    """MUST-8 end to end. The SAME fake server, reached as 127.0.0.1 (positive control)
    and as `localhost` (a name, refused). The refused spelling must deliver no request."""
    h = hosts(mode="same")
    ref = _reference(h)
    before = len(h.requests)

    with _declare(_embed("by-name", f"http://localhost:{h.port}")):
        with pytest.raises(providers.EmbedUnavailable) as exc:
            providers.embed_in_space(SPACE, [SPACE.query("secret mesh text")], ref)
    assert len(h.requests) == before, "content reached a provider MUST-8 refuses"
    assert exc.value.attempts == [{"id": "by-name", "verdict": "refused_must8",
                                   "reason": exc.value.attempts[0]["reason"]}]

    with _declare(_embed("by-ip", h.base_url)):
        got = providers.embed_in_space(SPACE, [SPACE.query("secret mesh text")], ref)
    assert got.provider_id == "by-ip" and len(h.requests) == before + 1


def test_the_provider_list_refuses_a_non_lan_provider_by_itself(monkeypatch, hosts):
    """The FIRST of the two MUST-8 layers, alone. The chokepoint behind it would refuse
    too, which is why deleting this filter (mutant G5) survived every end-to-end test:
    this asks the filter directly, with the chokepoint made unreachable."""
    h = hosts()
    monkeypatch.setattr(providers, "post_content",
                        lambda *a, **k: pytest.fail("the chokepoint was reached"))
    with _declare(_embed("name", f"http://localhost:{h.port}"),
                  _embed("public", "http://203.0.113.7:7875"),
                  _embed("lan", h.base_url)):
        eligible, refused = providers.content_providers("embed", SPACE.name)
    assert [p.id for p in eligible] == ["lan"], "positive control: the LAN literal passes"
    assert [(r["id"], r["verdict"]) for r in refused] == [("name", "refused_must8"),
                                                          ("public", "refused_must8")]


def test_a_misdeclared_embed_entry_is_named_with_both_spaces_not_skipped_silently(hosts):
    """E20. An embed entry whose declared space is not the configured one used to be
    dropped without a trace, so the job's error read "no embed provider declared" while
    MESHY_MODEL_PROVIDERS plainly held one. The message and the attempts must name every
    such entry, its declared space and the configured space, so the operator can fix the
    config from the message alone. The entry is still never ASKED: naming it is not using
    it (the host behind it must see no request)."""
    h = hosts(mode="same")
    ref = _reference(h)
    before = len(h.requests)
    with _declare(_embed("typo", h.base_url, space="fake-64/64/prov-TYPO"),
                  _embed("no-space", h.base_url, space=""),
                  {"id": "rr", "kind": "rerank", "base_url": h.base_url,
                   "space": "not-an-embed"}):
        with pytest.raises(providers.EmbedUnavailable) as exc:
            providers.embed_in_space(SPACE, [SPACE.query("mesh text")], ref)
    assert len(h.requests) == before, "a misdeclared entry was asked for content"
    msg = str(exc.value)
    for needle in ("'typo'", "'fake-64/64/prov-TYPO'", "'no-space'", "no space",
                   repr(SPACE.name), "MESHY_SEMANTIC_SPACE"):
        assert needle in msg, (needle, msg)
    assert "rr" not in [a["id"] for a in exc.value.attempts], "a non-embed entry is not a skip"
    assert [(a["id"], a["verdict"], a["declared_space"], a["configured_space"])
            for a in exc.value.attempts] == [
        ("typo", "space_mismatch", "fake-64/64/prov-TYPO", SPACE.name),
        ("no-space", "space_mismatch", "", SPACE.name)]
    for a in exc.value.attempts:
        assert repr(SPACE.name) in a["reason"] and "MESHY_SEMANTIC_SPACE" in a["reason"], a

    # Positive control: beside a correctly declared entry the same misdeclared one is
    # still NAMED in the attempts, and the correct one is the one that answers.
    with _declare(_embed("typo", h.base_url, space="fake-64/64/prov-TYPO"),
                  _embed("right", h.base_url)):
        got = providers.embed_in_space(SPACE, [SPACE.query("mesh text")], ref)
    assert got.provider_id == "right" and len(h.requests) == before + 1
    assert [(a["id"], a["verdict"]) for a in got.attempts] == [
        ("typo", "space_mismatch"), ("right", "admitted")]


def test_post_content_refuses_a_public_host_before_opening_anything(monkeypatch, hosts):
    """The chokepoint's own guard, not the provider filter in front of it: called
    directly with a public IP, no request is attempted at all."""
    opened = []

    class _Spy:
        def open(self, req, timeout=None):
            opened.append(req.full_url)
            raise AssertionError("the opener was reached")

    monkeypatch.setattr(providers, "_OPENER", _Spy())
    public = registry.Provider(id="pub", kind="embed", base_url="http://203.0.113.7:7875")
    with pytest.raises(providers.ContentRefused) as exc:
        providers.post_content(public, "/v1/embeddings", {"input": ["mesh text"]}, 1.0)
    assert "MUST-8" in str(exc.value) and opened == []

    # Positive control: the same call to an RFC1918 literal DOES reach the opener.
    lan = registry.Provider(id="lan", kind="embed", base_url="http://192.168.7.99:7875")
    with pytest.raises(AssertionError):
        providers.post_content(lan, "/v1/embeddings", {"input": ["x"]}, 1.0)
    assert opened == ["http://192.168.7.99:7875/v1/embeddings"]


def test_post_content_and_provider_info_fail_the_same_way(monkeypatch):
    """The POST and the GET share one transport-and-parse rule: the same exception class
    and message for each failure, and the opener read when the request is made (a test
    that replaces `providers._OPENER` replaces it for both)."""
    import io
    import urllib.error

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            self.close()

    def _raise(exc):
        def _open(req, timeout=None):
            raise exc
        return _open

    cases = [
        (_raise(urllib.error.HTTPError("u", 503, "x", {}, None)), providers.ProviderError, "HTTP 503"),
        (_raise(urllib.error.URLError("refused")), providers.ProviderUnreachable, "unreachable: refused"),
        (_raise(TimeoutError()), providers.ProviderUnreachable, "transport: TimeoutError"),
        (_raise(ConnectionResetError()), providers.ProviderUnreachable, "transport: ConnectionResetError"),
        (lambda req, timeout=None: _Resp(b"not json"), providers.ProviderError, "response was not JSON"),
        (lambda req, timeout=None: _Resp(b"[1]"), providers.ProviderError,
         "response was not a JSON object"),
    ]
    lan = registry.Provider(id="lan", kind="embed", base_url="http://192.168.7.99:7875")
    for opener, cls, message in cases:
        monkeypatch.setattr(providers, "_OPENER", type("_O", (), {"open": staticmethod(opener)})())
        for call in (lambda: providers.post_content(lan, "/v1/x", {"a": 1}, 1.0),
                     lambda: providers.provider_info(lan, "/props", timeout=1.0)):
            with pytest.raises(providers.ProviderError) as exc:
                call()
            assert type(exc.value) is cls and str(exc.value) == message, (message, exc.value)
    monkeypatch.setattr(providers, "_OPENER", type("_O", (), {"open": staticmethod(
        lambda req, timeout=None: _Resp(b'{"ok": 1}'))})())
    assert providers.post_content(lan, "/v1/x", {}, 1.0) == {"ok": 1}
    assert providers.provider_info(lan, "/props", timeout=1.0) == {"ok": 1}


def test_content_never_goes_through_an_environment_proxy(monkeypatch, hosts, recorder):
    """urlopen honours HTTP_PROXY; the content opener must not. The control shows the
    plain opener WOULD have delivered this request to the proxy."""
    h = hosts(mode="same")
    ref = _reference(h)
    monkeypatch.setenv("HTTP_PROXY", recorder.base_url)
    monkeypatch.setenv("http_proxy", recorder.base_url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    # build_opener(), NOT urlopen(): urlopen installs a process-global opener that keeps
    # this proxy after the test, and every later urlopen in the run (the modelhost
    # probes) would then dial a closed recorder. Measured: that is how the first draft
    # of this file turned a chat-probe test red with "connection refused".
    urllib.request.build_opener().open(urllib.request.Request(
        f"{h.base_url}/v1/embeddings", data=b"{}", method="POST"), timeout=5)
    assert len(recorder.hits) == 1, "control: the default opener uses the env proxy"

    # Build the content opener NOW, with the proxy in the environment, the way a
    # deployment builds it at start-up. The first draft used the opener built at import,
    # when no proxy was set, so deleting the empty ProxyHandler (mutant G6) SURVIVED: a
    # default ProxyHandler reads the environment once, when the opener is built.
    monkeypatch.setattr(providers, "_OPENER", providers._content_opener())
    before = len(h.requests)
    with _declare(_embed("lan", h.base_url)):
        providers.embed_in_space(SPACE, [SPACE.query("secret mesh text")], ref)
    assert len(recorder.hits) == 1, "mesh content was sent through the proxy"
    assert len(h.requests) == before + 1


@pytest.mark.parametrize("code", [302, 303, 307])
def test_a_redirect_is_refused_not_followed(recorder, code):
    """A LAN server answering a redirect must not send this process anywhere else.

    302 and 303 are the cases that matter: urllib FOLLOWS them for a POST, as a GET to
    the Location. The first draft tested only 307, which urllib refuses for a POST by
    itself, so deleting the redirect refusal (mutant G7) SURVIVED it."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):                           # noqa: N802
            # Read the request body first. Answering with it unread and closing makes
            # Windows send a RST, which can reach the client before the 3xx does: the
            # client then sees ConnectionAbortedError, not the redirect (flaky: 7 of 45 runs).
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(code)
            self.send_header("Location", f"{recorder.base_url}/v1/embeddings")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = registry.Provider(id="redir", kind="embed",
                              base_url=f"http://127.0.0.1:{srv.server_address[1]}")
        with pytest.raises(providers.ProviderError) as exc:
            providers.post_content(p, "/v1/embeddings", {"input": ["mesh text"]}, 5.0)
        assert str(code) in str(exc.value)
        assert recorder.hits == [], "the redirect was followed"
        # Control: the recorder is listening and would have recorded the request.
        providers.post_content(registry.Provider(id="r", kind="embed",
                                                 base_url=recorder.base_url),
                               "/v1/embeddings", {"input": ["x"]}, 5.0)
        assert len(recorder.hits) == 1
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ space failover

def test_failover_skips_a_different_model_and_lands_inside_the_space(hosts):
    """HQB-first, local-second, where the FIRST declared provider is a different model at
    the same width. It is refused as not admitted; the second (same model, another GPU)
    is used. Vectors come back in input order although the fake reverses the wire order."""
    ref_host, other, near = hosts(mode="same"), hosts(mode="other"), hosts(mode="near")
    ref = _reference(ref_host)
    texts = [SPACE.document("alpha beta"), SPACE.document("gamma delta")]
    with _declare(_embed("hqb-wrong", other.base_url), _embed("local", near.base_url)):
        got = providers.embed_in_space(SPACE, texts, ref)
    assert got.provider_id == "local"
    assert [a["verdict"] for a in got.attempts] == ["not_admitted", "admitted"]
    assert got.attempts[0]["min_cosine"] < 0.9 and got.canary_min_cosine >= 0.999
    for text, vec in zip(texts, got.vectors):
        want = FakeModelHost(dims=DIMS, mode="near").vector(text)
        assert float(np.dot(vec, want)) == pytest.approx(1.0, abs=1e-5)


def test_the_same_model_with_the_wrong_prompt_style_is_a_different_space(hosts):
    ref = _reference(hosts(mode="same"))
    wrong_prompt = hosts(mode="noprompt")
    with _declare(_embed("noprompt", wrong_prompt.base_url)):
        with pytest.raises(providers.EmbedUnavailable) as exc:
            providers.embed_in_space(SPACE, [SPACE.query("x")], ref)
    assert exc.value.attempts[0]["verdict"] == "not_admitted"


#: The raw space's prompts (none) and canaries, at the fake's width.
RAW_SPACE = spaces.Space(**{**spaces.EMBEDDINGGEMMA_300M_RAW.__dict__,
                            "name": "fake-64/64/prov-raw", "family": "fake", "dims": DIMS})


def test_a_prompting_provider_is_refused_from_the_raw_space(hosts):
    """Divergence 026 made the RAW-text space the default from 2026-09-23 to 2026-09-26, and
    it stays declared; the prompt style is still part of its identity, whichever space is
    the default. A server that applies EmbeddingGemma's document prompt itself is the
    same model with the wrong prompt style, so its canaries miss the raw reference and it
    is refused, never mixed in. Control: the raw provider is admitted to the same
    reference."""
    raw, prompted = hosts(mode="same"), hosts(mode="prompted")
    with _declare(_embed("ref", raw.base_url, space=RAW_SPACE.name)):
        ref = providers.embed_in_space(RAW_SPACE, [], None,
                                       record_if_unreferenced=True).canary_vectors
        ok = providers.embed_in_space(RAW_SPACE, [RAW_SPACE.query("x")], ref)
    assert ok.canary_min_cosine >= spaces.ADMISSION_COSINE, "control: raw is admitted"
    assert raw.embed_inputs()[-1] == "x", "the raw space sends the text with no prompt"
    with _declare(_embed("prompted", prompted.base_url, space=RAW_SPACE.name)):
        with pytest.raises(providers.EmbedUnavailable) as exc:
            providers.embed_in_space(RAW_SPACE, [RAW_SPACE.query("x")], ref)
    assert exc.value.attempts[0]["verdict"] == "not_admitted"
    assert exc.value.attempts[0]["min_cosine"] < 0.9


def test_a_provider_declared_for_another_space_is_never_asked(hosts):
    h = hosts(mode="same")
    ref = _reference(h)
    before = len(h.requests)
    with _declare(_embed("elsewhere", h.base_url, space="some-other/64/v1")):
        with pytest.raises(providers.EmbedUnavailable):
            providers.embed_in_space(SPACE, [SPACE.query("x")], ref)
    assert len(h.requests) == before


def test_a_request_without_a_reference_never_calls_a_model(hosts):
    """The job records a reference; the request path gets a typed refusal."""
    h = hosts(mode="same")
    with _declare(_embed("x", h.base_url)):
        with pytest.raises(providers.EmbedUnavailable) as exc:
            providers.embed_in_space(SPACE, [SPACE.query("x")], None)
    assert "no recorded reference" in str(exc.value) and h.requests == []


def test_degenerate_vectors_are_refused(hosts):
    ref = _reference(hosts(mode="same"))
    with _declare(_embed("zeros", hosts(mode="zeros").base_url)):
        with pytest.raises(providers.EmbedUnavailable):
            providers.embed_in_space(SPACE, [SPACE.query("x")], ref)


# ------------------------------------------------------------------ rerank

def _rerank(pid, url):
    return {"id": pid, "kind": "rerank", "base_url": url}


def test_rerank_falls_back_and_reports_ran_only_with_scores(hosts):
    down, good = hosts(rerank="down"), hosts(rerank="good")
    docs = ["bananas ripen", "which tenant can read a row", "tides"]
    with _declare(_rerank("hqb", down.base_url), _rerank("local", good.base_url)):
        got = providers.rerank("which tenant can read", docs)
    assert got.ran and got.provider_id == "local"
    assert int(np.argmax(got.scores)) == 1
    assert [a["verdict"] for a in got.attempts] == ["failed", "ran"]

    with _declare(_rerank("hqb", down.base_url)):
        none = providers.rerank("q", docs)
    assert not none.ran and none.scores is None and none.provider_id is None



def test_when_a_rerank_cooldown_ends_one_request_asks_again_not_every_request(hosts,
                                                                             monkeypatch):
    """A transport failure starts the provider's cooldown (07e40ba). At 07e40ba the
    cooldown simply lapsed, so EVERY request in flight when it ended asked the provider
    again, and with a hung reranker every one of them waited RERANK_TIMEOUT_S: under
    concurrent requests (the served app runs sync views on a thread each, Django's
    ThreadSensitiveContext under daphne) a hang still cost every request in each probe
    window, not "one request its timeout". Now ONE request (the probe) asks again; the
    rest skip it, typed, until that answer is in. A probe that fails at the transport
    arms the cooldown again; one that answers clears it, and then concurrent requests all
    rerank (single-flight applies to a provider that is cooling down, never to a healthy
    one). Timeout 1 s, cooldown 0.5 s. Control, one variable apart: the same wave with the
    provider answering.
    Kills: the probe claim not taken when the cooldown lapses (every request asks: the
    07e40ba behaviour); the claim not released when the probe answers (the provider
    stays skipped although it is back)."""
    import threading
    import time

    monkeypatch.setattr(providers, "RERANK_TIMEOUT_S", 1.0)
    monkeypatch.setattr(providers, "RERANK_COOLDOWN_S", 0.5)
    host = hosts(rerank="hang")
    docs = ["alpha tenant", "beta tenant", "gamma"]
    wave_size = 6

    def wave() -> list[tuple[float, dict]]:
        start, out, lock = threading.Barrier(wave_size), [], threading.Lock()

        def one():
            start.wait()
            began = time.monotonic()
            got = providers.rerank("which tenant", docs)
            with lock:
                out.append((time.monotonic() - began, got.attempts[-1]))

        threads = [threading.Thread(target=one) for _ in range(wave_size)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        assert len(out) == wave_size, out
        return out

    def asked() -> int:
        return sum(1 for path, _ in host.requests if path.endswith("/rerank"))

    providers.forget()
    try:
        with _declare(_rerank("r", host.base_url)):
            first = providers.rerank("which tenant", docs)
            assert first.attempts[-1]["verdict"] == "failed", first.attempts
            assert first.attempts[-1]["reason"] == "transport: TimeoutError", first.attempts
            assert asked() == 1
            time.sleep(0.7)                  # the cooldown has ended

            got = wave()
            failed = [a for _, a in got if a["verdict"] == "failed"]
            skipped = [(secs, a) for secs, a in got if a["verdict"] == "not_tried"]
            assert len(failed) == 1 and len(skipped) == wave_size - 1, got
            assert asked() == 2, f"{asked() - 1} requests asked the hung reranker again"
            for secs, a in skipped:
                assert a["reason"].startswith("cooling down: transport: TimeoutError"), a
                assert secs < 0.5, (secs, a, "waited on the hung reranker")
            after = providers.rerank("which tenant", docs)
            assert after.attempts[-1]["verdict"] == "not_tried", (
                "the probe failed at the transport: the cooldown is armed again")

            host.rerank_mode = "good"        # the reranker is back
            time.sleep(0.7)
            probe = providers.rerank("which tenant", docs)
            assert probe.ran and probe.attempts[-1]["verdict"] == "ran", probe.attempts
            back = wave()
            assert all(a["verdict"] == "ran" for _, a in back), (
                "control: once the probe answered, every concurrent request reranks", back)
    finally:
        providers.forget()

def test_a_rerank_that_skips_a_document_is_refused_not_padded(hosts):
    short = hosts(rerank="short")
    with _declare(_rerank("short", short.base_url)):
        got = providers.rerank("q", ["a", "b", "c"])
    assert not got.ran and "one result per document" in got.attempts[0]["reason"]


# ------------------------------------------------------------------ chat

def test_chat_grants_a_reasoning_model_its_budget_and_returns_content_only(hosts):
    h = hosts(chat="reasoning")
    with _declare({"id": "chat", "kind": "chat", "base_url": h.base_url}):
        got = providers.chat([{"role": "user", "content": "hi"}], max_tokens=16)
    assert got.text == "ready" and got.reasoning_chars > 0
    assert h.requests[-1][1]["max_tokens"] == providers.CHAT_MIN_TOKENS


def test_chat_falls_through_a_provider_that_only_reasoned(hosts, monkeypatch):
    starved, good = hosts(chat="reasoning"), hosts(chat="reasoning")
    monkeypatch.setattr(starved, "_chat", lambda h, p: h._send({"choices": [{
        "finish_reason": "length",
        "message": {"content": "", "reasoning_content": "thinking..."}}]}))
    with _declare({"id": "a", "kind": "chat", "base_url": starved.base_url},
                  {"id": "b", "kind": "chat", "base_url": good.base_url}):
        got = providers.chat([{"role": "user", "content": "hi"}])
    assert got.provider_id == "b"
    assert "spent its budget thinking" in got.attempts[0]["reason"]


def test_the_chat_probe_passes_a_reasoning_model_the_generate_probe_refuses(hosts):
    """The measured gemma-4-26b-a4b shape: 16 tokens yields reasoning and empty content."""
    h = hosts(chat="reasoning")
    starved = probes.probe_generate(h.base_url)
    assert not starved.ok and starved.reason == "completion returned empty text", (
        "control: the 16-token generate probe reached the model and was starved")
    got = probes.probe_chat(h.base_url)
    assert got.ok, got.reason
    assert got.detail["reasoning_chars"] > 0
    assert "chat" in registry.KNOWN_KINDS


def test_the_chat_probe_never_reads_reasoning_as_the_answer(hosts, monkeypatch):
    """Starve the chat probe the way the generate probe is starved: it must refuse and
    SAY the model spent its budget thinking, not pass on the reasoning text."""
    h = hosts(chat="reasoning")
    monkeypatch.setattr(probes, "CHAT_MAX_TOKENS", 16)
    got = probes.probe_chat(h.base_url)
    assert not got.ok and "spent its budget thinking" in got.reason
    assert got.detail["finish_reason"] == "length"
    monkeypatch.setattr(probes, "CHAT_MAX_TOKENS", 512)
    assert probes.probe_chat(h.base_url).ok, "positive control: the budget is the cause"


def test_capabilities_report_must8_eligibility_without_the_host(hosts, monkeypatch):
    h = hosts()
    monkeypatch.setenv("MESHY_MODEL_PROVIDERS", json.dumps([
        _embed("lan", h.base_url), _embed("pub", "http://203.0.113.9:1")]))
    registry.forget()
    try:
        with override_settings(MESHY_MODEL_PROVIDERS=None):
            snap = registry.snapshot(budget_s=3.0)
    finally:
        registry.forget()
    by_id = {p["id"]: p for p in snap["providers"]}
    assert by_id["lan"]["mesh_content_eligible"] is True
    assert by_id["lan"]["locality"] == "loopback" and by_id["lan"]["space"] == SPACE.name
    assert by_id["pub"]["mesh_content_eligible"] is False
    assert "203.0.113.9" not in json.dumps(snap)


def test_an_unset_space_setting_resolves_to_the_retrieval_space(monkeypatch):
    """The deployment default (divergence 026, amended 2026-09-26): nothing configured
    means the retrieval-prompt space. Control: a deployment that pins the raw space, the
    default from 2026-09-23 to 2026-09-26, still gets it."""
    from apps.meshsem import store

    monkeypatch.delenv("MESHY_SEMANTIC_SPACE", raising=False)
    with override_settings(MESHY_SEMANTIC_SPACE=None):
        assert store.configured_space() is spaces.EMBEDDINGGEMMA_300M
        assert store.configured_space().name == "embeddinggemma-300m/768/retrieval-v1"
    with override_settings(MESHY_SEMANTIC_SPACE=spaces.EMBEDDINGGEMMA_300M_RAW.name):
        assert store.configured_space() is spaces.EMBEDDINGGEMMA_300M_RAW, "control"
