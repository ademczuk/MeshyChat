"""The summary cache and the measurement record that switches summaries on.

Divergence 037, design 2.7, 5.6 and 7.8 (critique C3). No database.

THE CACHE holds the MODEL'S PART ONLY: the validated sentences (by the model's numbers),
the drop list and the model's provenance. Never `facts`, `scope`, `window`, `covered` or
anything else computed for a caller: a whole-response cache would hand reader A's owed
acks to reader B. It is keyed on (scope kind, prompt digest, summary_version, model pin,
provider), bounded (256 entries) with a 15-minute TTL (B8), and single-flight: concurrent
requests for one key wait for the first one's call instead of each making it. Only a
VALIDATED answer is cached; a failure is handed to the waiters and not kept. A leader
refused by its OWN budget does not refuse its waiters: one of them takes the call over.

THE RECORD (7.8): the server serves summaries only for a (summary_version, model_pin)
that a committed measurement record passed, verified by its own content digest. No
record, a failed one, one for another version, or one whose digest does not verify is
503 E_SUMMARY_DISABLED.

Run:  pytest tests/test_meshsum_cache.py -q     (needs no database)
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.meshsum import cache as C  # noqa: E402
from apps.meshsum import prompt as P  # noqa: E402
from apps.meshsum import record as R  # noqa: E402
from apps.meshsum import validate as V  # noqa: E402


def _part(tag="a"):
    return C.ModelPart(kept=(V.Sentence(1, f"otter wrote {tag}.", (1,)),), dropped=(),
                       total=1, provenance={"model_reported": "m", "finish_reason": "stop"})


def _key(digest="sha256:" + "1" * 64, **kw):
    base = dict(kind="thread", prompt_digest=digest, summary_version=P.SUMMARY_VERSION,
                model_pin="sha256:pin", provider_id="chat-fake")
    base.update(kw)
    return C.cache_key(**base)


# ------------------------------------------------------------------- what it holds

def test_the_cache_holds_the_models_part_and_nothing_a_caller_owns():
    fields = {f.name for f in dataclasses.fields(C.ModelPart)}
    assert fields == {"kept", "dropped", "total", "provenance"}
    for caller_owned in ("facts", "scope", "window", "covered", "excluded", "owed_by_you"):
        assert caller_owned not in fields


def test_every_part_of_the_key_separates_entries():
    k = _key()
    others = [_key(kind="backlog"), _key(digest="sha256:" + "2" * 64),
              _key(summary_version="sum-v1+000000000000"), _key(model_pin="sha256:other"),
              _key(provider_id="chat-other")]
    assert len({k, *others}) == 6


def test_ttl_and_bound():
    now = [0.0]
    c = C.SummaryCache(max_entries=3, ttl_s=900, clock=lambda: now[0])
    c.put(_key("sha256:a"), _part())
    now[0] = 899.0
    assert c.get(_key("sha256:a")) is not None
    now[0] = 901.0
    assert c.get(_key("sha256:a")) is None, "past its TTL"
    for d in ("b", "c", "d", "e"):
        c.put(_key(f"sha256:{d}"), _part(d))
    assert c.get(_key("sha256:b")) is None, "the least recently used went first"
    assert c.get(_key("sha256:e")) is not None and len(c) == 3
    assert C.MAX_ENTRIES == 256 and C.TTL_S == 900


# ------------------------------------------------------------------- single flight

def test_concurrent_requests_for_one_key_make_one_call():
    c = C.SummaryCache()
    calls = []
    gate = threading.Event()

    def compute():
        calls.append(1)
        gate.wait(5)
        return _part()

    results = []

    def go():
        results.append(c.single_flight(_key(), compute, wait_s=10))

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(calls) == 1
    hows = sorted(how for _part_, how in results)
    assert hows == ["miss", "shared", "shared", "shared", "shared"]
    assert c.single_flight(_key(), compute, wait_s=1)[1] == "hit" and len(calls) == 1


def test_a_failure_is_handed_to_the_waiters_and_never_cached():
    c = C.SummaryCache()
    gate = threading.Event()

    class Down(Exception):
        pass

    def fail():
        gate.wait(5)
        raise Down("provider down")

    errors = []

    def go():
        try:
            c.single_flight(_key(), fail, wait_s=10)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(errors) == 3
    assert sum(isinstance(e, Down) for e in errors) == 1, "the leader's own"
    assert all(isinstance(e, C.LeaderFailed) and isinstance(e.cause, Down)
               for e in errors if not isinstance(e, Down))
    assert c.get(_key()) is None
    assert c.single_flight(_key(), _part, wait_s=1)[1] == "miss", "a failure was cached"


def test_a_leader_refused_by_its_own_budget_does_not_refuse_its_waiters():
    c = C.SummaryCache()
    gate = threading.Event()

    class OwnBudget(Exception):
        pass

    state = {"first": True}

    def compute():
        if state["first"]:
            state["first"] = False
            gate.wait(5)
            raise OwnBudget()
        return _part("waiter")

    out = []

    def go():
        try:
            out.append(c.single_flight(_key(), compute, wait_s=10,
                                       retry_on=lambda e: isinstance(e, OwnBudget))[1])
        except OwnBudget:
            out.append("refused")

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert sorted(out) == ["miss", "refused"], out


def test_a_waiter_that_waits_too_long_gets_a_timeout_not_an_answer():
    c = C.SummaryCache()
    gate = threading.Event()
    t = threading.Thread(target=lambda: c.single_flight(_key(), lambda: (gate.wait(5),
                                                                          _part())[1],
                                                        wait_s=10))
    t.start()
    time.sleep(0.2)
    with pytest.raises(C.WaitTimedOut):
        c.single_flight(_key(), _part, wait_s=0.2)
    gate.set()
    t.join(10)


# --------------------------------------------------------------------- the record

def _record(tmp_path, **over):
    body = {"schema": R.SCHEMA, "summary_version": P.SUMMARY_VERSION,
            "model_pin": "sha256:pinned", "provider_id": "chat-hqb-5090", "passed": True,
            "floors": {"F1": {"point": 0.97, "passed": True}}, "measured_at": "2026-10-01"}
    body.update(over)
    body["content_sha256"] = R.content_sha256(body)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path, body


def test_a_passing_verified_record_for_this_version_enables(tmp_path):
    path, body = _record(tmp_path)
    with override_settings(MESHY_SUMMARY_RECORD_PATH=str(path)):
        rec, why = R.current()
    assert why is None and rec.model_pin == "sha256:pinned"
    assert rec.provider_id == "chat-hqb-5090"


@pytest.mark.parametrize("change,reason", [
    ({"passed": False}, "failed"),
    ({"summary_version": "sum-v1+000000000000"}, "other_version"),
])
def test_a_failed_or_other_version_record_disables(tmp_path, change, reason):
    path, _body = _record(tmp_path, **change)
    with override_settings(MESHY_SUMMARY_RECORD_PATH=str(path)):
        rec, why = R.current()
    assert rec is None and why == reason


def test_a_record_edited_after_it_was_digested_disables(tmp_path):
    path, body = _record(tmp_path)
    body["model_pin"] = "sha256:another"          # the digest no longer covers it
    path.write_text(json.dumps(body), encoding="utf-8")
    with override_settings(MESHY_SUMMARY_RECORD_PATH=str(path)):
        rec, why = R.current()
    assert rec is None and why == "not_verified"


def test_no_record_and_an_unreadable_record_disable(tmp_path):
    with override_settings(MESHY_SUMMARY_RECORD_PATH=str(tmp_path / "absent.json")):
        assert R.current() == (None, "no_record")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with override_settings(MESHY_SUMMARY_RECORD_PATH=str(bad)):
        assert R.current() == (None, "unreadable")


def test_the_repository_ships_no_record():
    """Built is not shipped (design 8): until a measurement passes, the committed default
    path holds no record, so every deployment answers E_SUMMARY_DISABLED."""
    assert not R.DEFAULT_PATH.exists()
    with override_settings(MESHY_SUMMARY_RECORD_PATH=None):
        assert R.current() == (None, "no_record")
