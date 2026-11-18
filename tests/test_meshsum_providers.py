"""What a summary needs from the provider layer: a PINNED chat call, the tokenizer, the pin.

Divergence 037, design 2.6 and 5.5. No database. Every provider is a real local HTTP
server (tests/fixtures/meshsum_fake.py), so the transport and parsing are the production
path's. Where a test proves something did NOT reach a host, the same test shows a request
that DID, so "nothing arrived" cannot be the fixture failing to listen.

  * `providers.chat(provider_ids=...)` asks ONLY the pinned provider: chat's default is to
    try each provider until one answers, which would let an unmeasured model answer.
  * every sampler setting is sent as given, with the JSON schema, and the reply carries the
    answer's `usage` and `model` (the provenance needs both).
  * `providers.tokenize` leaves through `post_content`, so MUST-8 holds for the tokenizer
    as for the chat call: it is the model's own server.
  * the model pin covers the file size, the chat template, the sampler defaults, n_ctx
    and the slot count, not only path and build (critique C5), and a missing field is
    refused, not assumed.

Run:  pytest tests/test_meshsum_providers.py -q     (needs no database)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.meshsem import providers  # noqa: E402
from apps.meshsum import pin  # noqa: E402
from apps.modelhost import registry  # noqa: E402
from tests.fixtures.meshsem_fake import Recorder  # noqa: E402
from tests.fixtures.meshsum_fake import FakeSummaryModel  # noqa: E402


@pytest.fixture()
def models():
    made = []

    def make(**kw):
        m = FakeSummaryModel(**kw)
        made.append(m)
        return m
    yield make
    for m in made:
        m.close()
    pin.forget()


def _declare(*entries):
    return override_settings(MESHY_MODEL_PROVIDERS=list(entries))


MSGS = [{"role": "system", "content": "sys"},
        {"role": "user", "content": "[1] from otter at 2026-09-24T02:20:11Z\nhi"}]


# ------------------------------------------------------------------ the pinned call

def test_chat_with_provider_ids_asks_only_the_pinned_provider(models):
    first, pinned = models(), models()
    with _declare(first.provider(pid="chat-first"), pinned.provider(pid="chat-pinned")):
        reply = providers.chat(MSGS, provider_ids={"chat-pinned"}, max_tokens=640)
        assert reply.provider_id == "chat-pinned"
        assert first.chats() == 0 and pinned.chats() == 1
        # Control: without the pin, declaration order decides and the first answers.
        assert providers.chat(MSGS, max_tokens=640).provider_id == "chat-first"
    assert first.chats() == 1


def test_a_pinned_provider_that_is_down_is_not_replaced_by_another(models):
    other, pinned = models(), models()
    pinned.down = True
    with _declare(other.provider(pid="chat-other"), pinned.provider(pid="chat-pinned")):
        with pytest.raises(providers.ChatUnavailable) as exc:
            providers.chat(MSGS, provider_ids={"chat-pinned"}, max_tokens=640)
    assert other.chats() == 0
    assert [a["id"] for a in exc.value.attempts] == ["chat-pinned"]


def test_chat_sends_every_setting_and_the_schema_and_returns_usage_and_model(models):
    m = models()
    params = {"temperature": 0.0, "seed": 7, "top_k": 1, "dry_multiplier": 0.0,
              "chat_template_kwargs": {"enable_thinking": False}}
    schema = {"type": "object"}
    with _declare(m.provider(pid="chat-pinned")):
        reply = providers.chat(MSGS, provider_ids={"chat-pinned"}, max_tokens=640,
                               params=params, json_schema=schema)
    sent = m.payloads[-1]
    for k, v in params.items():
        assert sent[k] == v, k
    assert sent["json_schema"] == schema and sent["max_tokens"] == 640
    assert reply.model == "gemma-4-26b-a4b"
    assert reply.usage["prompt_tokens"] > 0 and reply.usage["completion_tokens"] > 0
    # Control: the old call shape still sends its own temperature and no schema.
    with _declare(m.provider(pid="chat-pinned")):
        providers.chat(MSGS, temperature=0.2)
    assert m.payloads[-1]["temperature"] == 0.2 and "json_schema" not in m.payloads[-1]


def test_accept_empty_returns_an_empty_answer_instead_of_failing_over(models):
    m = models()
    m.empty = True
    with _declare(m.provider(pid="chat-pinned")):
        with pytest.raises(providers.ChatUnavailable):
            providers.chat(MSGS, provider_ids={"chat-pinned"})
        reply = providers.chat(MSGS, provider_ids={"chat-pinned"}, accept_empty=True)
    assert reply.text == "" and reply.finish_reason == "stop"


# ------------------------------------------------------------------ the tokenizer

def test_tokenize_counts_through_the_chokepoint(models):
    m = models(chars_per_token=4)
    p = registry.Provider(id="chat-pinned", kind="chat", base_url=m.base_url)
    assert providers.tokenize(p, "x" * 10, timeout=5.0) == 3
    assert m.tokenized == ["x" * 10]
    assert providers.tokenize(p, "", timeout=5.0) == 0


def test_tokenize_refuses_a_named_host_before_sending_anything(models):
    m = models()
    named = registry.Provider(id="n", kind="chat", base_url=f"http://localhost:{m.port}")
    with pytest.raises(providers.ContentRefused):
        providers.tokenize(named, "secret mesh text", timeout=5.0)
    assert m.tokenized == [], "mesh text reached a host MUST-8 refuses"
    lan = registry.Provider(id="l", kind="chat", base_url=m.base_url)
    providers.tokenize(lan, "control", timeout=5.0)
    assert m.tokenized == ["control"]


def test_tokenize_refuses_a_malformed_answer():
    rec = Recorder()                     # answers {} to everything
    try:
        p = registry.Provider(id="r", kind="chat", base_url=rec.base_url)
        with pytest.raises(providers.ProviderError):
            providers.tokenize(p, "text", timeout=5.0)
    finally:
        rec.close()


def test_provider_info_is_a_get_behind_the_same_guard(models):
    m = models()
    lan = registry.Provider(id="l", kind="chat", base_url=m.base_url)
    assert providers.provider_info(lan, "/props", timeout=5.0)["total_slots"] == 6
    named = registry.Provider(id="n", kind="chat", base_url=f"http://localhost:{m.port}")
    before = list(m.gets)
    with pytest.raises(providers.ContentRefused):
        providers.provider_info(named, "/props", timeout=5.0)
    assert m.gets == before


# ------------------------------------------------------------------------ the pin

def _pin(m):
    return pin.read_pin(registry.Provider(id="c", kind="chat", base_url=m.base_url))


@pytest.mark.parametrize("change", [
    lambda m: m.props.__setitem__("chat_template", m.props["chat_template"] + " "),
    lambda m: m.meta.__setitem__("size", m.meta["size"] + 1),
    lambda m: m.props["default_generation_settings"]["params"].__setitem__("top_k", 40),
    lambda m: m.props["default_generation_settings"].__setitem__("n_ctx", 8192),
    lambda m: m.props.__setitem__("total_slots", 2),
    lambda m: m.props.__setitem__("build_info", "b10999-000000000"),
    lambda m: m.props.__setitem__("model_path", "C:\\other.gguf"),
    lambda m: m.meta.__setitem__("ftype", "Q8_0"),
])
def test_the_pin_moves_with_every_part_it_covers(models, change):
    m = models()
    before = _pin(m)
    assert before.digest.startswith("sha256:") and _pin(m).digest == before.digest
    change(m)
    assert _pin(m).digest != before.digest


def test_a_changed_is_sleeping_flag_does_not_move_the_pin(models):
    m = models()
    before = _pin(m)
    m.props["is_sleeping"] = True
    assert _pin(m).digest == before.digest


@pytest.mark.parametrize("drop", [
    lambda m: m.props.pop("chat_template"),
    lambda m: m.props["default_generation_settings"].pop("params"),
    lambda m: m.meta.pop("size"),
    lambda m: m.props.pop("total_slots"),
])
def test_a_missing_field_is_refused_not_assumed(models, drop):
    m = models()
    drop(m)
    with pytest.raises(pin.PinIncomplete):
        _pin(m)


def test_an_unreachable_provider_is_unavailable_not_changed(models):
    m = models()
    m.down = True
    with pytest.raises(pin.PinUnavailable):
        _pin(m)


def test_the_pin_is_read_at_most_once_a_minute(models):
    m = models()
    p = registry.Provider(id="c", kind="chat", base_url=m.base_url)
    now = [1000.0]
    first = pin.current_pin(p, clock=lambda: now[0])
    gets = len(m.gets)
    now[0] += 59
    assert pin.current_pin(p, clock=lambda: now[0]).digest == first.digest
    assert len(m.gets) == gets, "re-read inside the minute"
    m.props["total_slots"] = 3
    now[0] += 2
    assert pin.current_pin(p, clock=lambda: now[0]).digest != first.digest
    assert len(m.gets) == gets + 2
