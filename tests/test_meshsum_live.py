"""The summary pipeline against HQB's REAL chat server. Skipped unless MESHY_LIVE_MODELS=1.

Divergence 037. SYNTHETIC TEXT ONLY: no mesh message is read or sent. This confirms what
the design left to the build (docs/design/thread-summaries.md 1.2, 3.2 item 3, 5.4, 5.5):

  * the pin's every field is reported by the running server (a missing one is refused);
  * the system message fits the budget table's `system_max` on the model's OWN tokenizer
    (the design's "about 450" was an inference; this pins it);
  * llama-server b10566 honours the `json_schema` field on the chat route: with it, the
    answer is one strict JSON object in the schema, and the validator reads it;
  * two identical requests with temperature 0 and a fixed seed give the same answer on an
    idle server (the design's inference in 1.2, with its rival: batch composition under
    load can change the arithmetic; this is one observation, not a guarantee).

Run:  MESHY_LIVE_MODELS=1 MESHY_LIVE_CHAT_URL=http://<hqb>:7872 pytest tests/test_meshsum_live.py -q
      MESHY_LIVE_CHAT_URL is REQUIRED and names exactly ONE chat server (on this LAN,
      HQB's at port 7872): no default, and the run is REFUSED without it
      (tests/live_env.py).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.meshsem import providers  # noqa: E402
from apps.meshsum import pin, prompt as P, validate as V, window as W  # noqa: E402
from apps.modelhost import registry  # noqa: E402
from tests import live_env  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("MESHY_LIVE_MODELS") != "1",
                                reason="live HQB test: set MESHY_LIVE_MODELS=1")

T0 = _dt.datetime(2026, 9, 24, 2, 0, tzinfo=_dt.timezone.utc)

#: Written for this test. Not mesh text.
SYNTHETIC = [
    ("otter", "Should the nightly backfill wait until the index rebuild finishes? "
              "Last run took 45 minutes."),
    ("heron", "No. The backfill runs first; the rebuild starts at 10:30."),
    ("otter", "Agreed, I will move the rebuild job to 10:30."),
]


def _provider() -> registry.Provider:
    """The ONE chat server named for this run, or REFUSED, before anything is sent."""
    return registry.Provider(id="chat-live", kind="chat",
                             base_url=live_env.url("MESHY_LIVE_CHAT_URL", "chat server"),
                             model="gemma-4-26b-a4b")


def _rows() -> list:
    out = []
    for i, (author, body) in enumerate(SYNTHETIC):
        row = W.Row(id=uuid.uuid4(), key=f"msg/{1790000000000 + i}-live{i:04d}", version=1,
                    created_at=T0 + _dt.timedelta(minutes=i), author=author,
                    author_peer_id=uuid.uuid4(), value={"thread_id": "live/test"},
                    embed_policy="allowed", raw_body=body)
        out.append(W._shown(row))
    return out


def _compose(provider):
    rows = _rows()
    cands = W.Candidates(kind="thread", rows=list(reversed(rows)),
                         excluded={"embed_forbidden": 0, "no_text": 0}, in_scope=3,
                         declared_total=3)
    counter = W.TokenCounter(provider, f"live-{uuid.uuid4().hex}",
                             deadline=time.monotonic() + 60)
    packed = W.pack(cands, counter)
    composed = W.compose(packed.rows, "thread", names={}, thread_id="live/test")
    ctx = V.Context(sources=composed.sources,
                    known_labels=frozenset({"otter", "heron"}),
                    system_text=composed.messages[0]["content"], canary=composed.canary,
                    nonce=composed.nonce)
    return packed, composed, ctx, counter


def _ask(provider, composed, n):
    with override_settings(MESHY_MODEL_PROVIDERS=[{"id": provider.id, "kind": "chat",
                                                   "base_url": provider.base_url,
                                                   "model": provider.model}]):
        return providers.chat(composed.messages, provider_ids={provider.id},
                              max_tokens=P.REQUEST["max_tokens"], params=P.REQUEST,
                              json_schema=P.json_schema(n), accept_empty=True,
                              budget_s=45.0, timeout_s=40.0)


def test_the_running_server_reports_every_pinned_field():
    got = pin.read_pin(_provider())
    assert got.fields["n_ctx"] == P.BUDGET["n_ctx"]
    assert got.fields["total_slots"] >= 1 and got.fields["size"] > 0
    print("\nmodel pin", got.digest, json.dumps(got.fields, sort_keys=True))


def test_the_system_message_fits_its_budget_line_on_the_models_tokenizer():
    n = providers.tokenize(_provider(), P.system_text(W.PLACEHOLDER_CANARY), timeout=10)
    print(f"\nsystem message: {n} tokens (budget line {P.BUDGET['system_max']})")
    assert n <= P.BUDGET["system_max"]


def test_json_schema_is_honoured_and_the_validator_reads_the_answer():
    provider = _provider()
    packed, composed, ctx, counter = _compose(provider)
    reply = _ask(provider, composed, len(packed.rows))
    print("\nanswer:", reply.text)
    print("finish:", reply.finish_reason, "usage:", reply.usage, "model:", reply.model)
    assert reply.finish_reason == "stop"
    assert V._strict_object(reply.text) is not None, "not one strict JSON object"
    verdict = V.validate(reply.text, reply.finish_reason, ctx)
    print("kept:", [s.text for s in verdict.kept], "dropped:", verdict.histogram(),
          "refused:", verdict.refused)
    assert verdict.refused is None
    # The packing arithmetic on the real tokenizer: the prompt the server counted leaves
    # the answer its max_tokens inside n_ctx.
    print("prompt tokens:", reply.usage["prompt_tokens"], "packed estimate:",
          packed.tokens, "of budget", packed.budget)
    assert reply.usage["prompt_tokens"] <= P.BUDGET["n_ctx"] - P.BUDGET["max_tokens"]


def test_two_identical_requests_on_an_idle_server_answer_the_same():
    provider = _provider()
    packed, composed, ctx, counter = _compose(provider)
    a = _ask(provider, composed, len(packed.rows)).text
    b = _ask(provider, composed, len(packed.rows)).text
    print("\nfirst:", a, "\nsecond:", b)
    assert a == b
