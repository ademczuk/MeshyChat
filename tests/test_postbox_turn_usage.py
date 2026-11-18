"""Seat usage on the turn report, postbox side (kxxx 29cf38, memory-bxxxxx afd116, a reviewer a24851)."""
import datetime as dt
import json
import os

import pytest

from tools.meshy_node import turn as client_turn
from tools.postbox import turn_reporter as reporter
from tools.postbox import usage_source

NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc).timestamp()
INPUT = {
    "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
    "context_window": {"used_percentage": 28, "remaining_percentage": 72},
    "rate_limits": {"seven_day": {"used_percentage": 9, "resets_at": 1794668400},
                    "five_hour": {"used_percentage": 41.5, "resets_at": "2026-11-08T15:00:00+00:00"}},
    "cwd": "C:/secret/project", "transcript_path": "C:/secret/t.jsonl", "session_id": "abc", "cost": {"total_cost_usd": 3},
}


def saved(tmp_path, doc=INPUT, *, age_s=60, raw=None):
    p = tmp_path / "statusline-last-input.json"
    p.write_bytes(raw if raw is not None else json.dumps(doc).encode())
    os.utime(p, (NOW - age_s, NOW - age_s))
    return str(p)


def test_only_the_stored_fields_leave_and_as_of_is_the_save_time(tmp_path):
    u = usage_source.from_statusline(saved(tmp_path), now=NOW)
    assert set(u) == {"as_of", "model", "context_used_pct", "limits"}
    assert u["as_of"] == dt.datetime.fromtimestamp(NOW - 60, dt.timezone.utc).isoformat()
    assert u["model"] == "claude-opus-5-5" and u["context_used_pct"] == 28.0
    assert u["limits"] == [
        {"window": "five_hour", "used_pct": 41.5, "resets_at": "2026-11-08T15:00:00+00:00", "source": "statusline"},
        {"window": "seven_day", "used_pct": 9.0, "resets_at": dt.datetime.fromtimestamp(1794668400, dt.timezone.utc).isoformat(),
         "source": "statusline"}]
    text = json.dumps(u)
    for leak in ("secret", "abc", "cost", "Opus"):
        assert leak not in text
    # The client's own refusal list accepts it as built.
    assert client_turn._usage_refusal(u) is None


def test_missing_stale_or_unconfigured_sends_no_usage(tmp_path):
    assert usage_source.from_statusline(str(tmp_path / "none.json"), now=NOW) is None
    assert usage_source.from_statusline(saved(tmp_path, age_s=usage_source.MAX_AGE_SECONDS + 1), now=NOW) is None
    assert usage_source.read({}, now=NOW) is None
    assert usage_source.read({"usage": {"statusline_file": ""}}, now=NOW) is None


def test_bad_values_are_dropped_not_sent(tmp_path):
    doc = {"model": {"id": "x" * 81}, "context_window": {"used_percentage": 150},
           "rate_limits": {"five_hour": {"used_percentage": True, "resets_at": "no offset 2026-11-08T15:00:00"},
                           "weekly": {"used_percentage": 5}}}
    u = usage_source.from_statusline(saved(tmp_path, doc), now=NOW)
    assert u["model"] is None and u["context_used_pct"] is None
    assert u["limits"] == [{"window": "five_hour", "used_pct": None, "resets_at": None, "source": "statusline"}]


def test_a_malformed_or_odd_source_raises_for_the_caller(tmp_path):
    with pytest.raises(ValueError):
        usage_source.from_statusline(saved(tmp_path, raw=b"{not json"), now=NOW)
    with pytest.raises(ValueError):
        usage_source.from_statusline(saved(tmp_path, raw=b"[1, 2]"), now=NOW)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (usage_source.MAX_FILE_BYTES + 1))
    os.utime(big, (NOW, NOW))
    with pytest.raises(ValueError):
        usage_source.from_statusline(str(big), now=NOW)
    with pytest.raises(ValueError):
        usage_source.from_statusline(str(tmp_path), now=NOW)  # a folder, not a plain file


def test_moved_triggers_on_five_points_model_or_window_change():
    base = {"model": "m", "context_used_pct": 20.0, "limits": [{"window": "five_hour", "used_pct": 10.0}]}
    assert usage_source.moved(None, base)
    assert not usage_source.moved(base, None)
    assert not usage_source.moved(base, {**base, "context_used_pct": 24.9})
    assert usage_source.moved(base, {**base, "context_used_pct": 25.0})
    assert usage_source.moved(base, {**base, "model": "n"})
    assert not usage_source.moved(base, {**base, "limits": [{"window": "five_hour", "used_pct": 14.0}]})
    assert usage_source.moved(base, {**base, "limits": [{"window": "five_hour", "used_pct": 15.0}]})
    assert usage_source.moved(base, {**base, "limits": []})
    assert usage_source.moved(base, {**base, "limits": [{"window": "five_hour", "used_pct": None}]})


def record(path, stamp, event="task_complete"):
    with path.open("ab") as out:
        out.write(json.dumps({"type": "event_msg", "timestamp": stamp, "payload": {"type": event}}).encode() + b"\n")


class Clock:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    def snapshot(self, ctx):
        doc = json.loads(json.dumps(INPUT))
        doc["context_window"]["used_percentage"] = ctx
        p = self.tmp_path / "sl.json"
        p.write_text(json.dumps(doc))
        return str(p)


def test_tick_carries_usage_and_sends_early_only_on_a_move(tmp_path, monkeypatch):
    src = tmp_path / "rollout.jsonl"
    record(src, "2026-11-08T11:59:00Z")
    clock = Clock(tmp_path)
    ident = {"usage": {"statusline_file": clock.snapshot(20)}}
    sent = []
    send = lambda i, payload, usage=None: sent.append((dict(payload), usage)) or True
    r = reporter.Reporter()
    r.tick(ident, [str(src)], now=1000.0, send=send)
    assert len(sent) == 1 and sent[0][1]["context_used_pct"] == 20.0
    clock.snapshot(23)  # under 5 points: waits for the refresh
    r.tick(ident, [str(src)], now=1031.0, send=send)
    assert len(sent) == 1
    clock.snapshot(26)  # 6 points from what was sent: goes now, inside the tick
    r.tick(ident, [str(src)], now=1062.0, send=send)
    assert len(sent) == 2 and sent[1][1]["context_used_pct"] == 26.0
    r.tick(ident, [str(src)], now=1062.0 + reporter.REFRESH_SECONDS + 31, send=send)
    assert len(sent) == 3  # the 120 s refresh still goes


def test_a_failed_usage_read_still_sends_the_turn_pair(tmp_path):
    src = tmp_path / "rollout.jsonl"
    record(src, "2026-11-08T11:59:00Z")
    bad = tmp_path / "sl.json"
    bad.write_text("{oops")
    sent, statuses = [], []
    r = reporter.Reporter()
    r.tick({"usage": {"statusline_file": str(bad)}}, [str(src)], now=1000.0,
           send=lambda i, payload: sent.append(payload) or True, status=lambda **kw: statuses.append(kw))
    assert len(sent) == 1
    assert {"usage_error": "JSONDecodeError"} in statuses
    assert all(str(tmp_path) not in json.dumps(s) for s in statuses)


@pytest.mark.parametrize("model,kept", [
    ("claude-opus-5-5", True), ("claude-opus-5-5[1m]", True), ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", True),
    ("claude-sonnet-4@20250514", False), ("anthropic/claude-opus", False), ("Claude Opus 5.5", False)])
def test_a_model_id_the_server_refuses_goes_as_null(tmp_path, model, kept):
    """pxx 1f0dcb P2-1: apps/peers/turn.py _USAGE_MODEL refuses the whole report for these."""
    from apps.peers import turn as server_turn
    doc = {**INPUT, "model": {"id": model}}
    u = usage_source.from_statusline(saved(tmp_path, doc), now=NOW)
    assert (u["model"] == model) is kept
    assert (u["model"] is None) is (not kept)
    assert bool(server_turn._USAGE_MODEL.fullmatch(model)) is kept  # the same rule as the server's


def test_a_refused_usage_report_still_sends_the_turn_pair_and_backs_off_usage(tmp_path):
    """pxx 1f0dcb P2-2: e.g. a client without --usage-file (before #525) or a stricter server."""
    src = tmp_path / "rollout.jsonl"
    record(src, "2026-11-08T11:59:00Z")
    clock = Clock(tmp_path)
    ident = {"usage": {"statusline_file": clock.snapshot(20)}}
    calls, statuses = [], []

    def send(i, payload, usage=None):
        calls.append(usage)
        return usage is None  # anything carrying usage is refused

    r = reporter.Reporter()
    st = lambda **kw: statuses.append(kw)
    r.tick(ident, [str(src)], now=1000.0, send=send, status=st)
    assert [c is None for c in calls] == [False, True]  # tried with usage, then the same pair without
    assert r.sent is not None and r.failures == 0
    assert any(s.get("usage_error") == "usage_report_failed" for s in statuses)
    clock.snapshot(40)  # a big move, but usage is backing off: no usage-bearing try
    r.tick(ident, [str(src)], now=1000.0 + reporter.REFRESH_SECONDS + 1, send=send, status=st)
    assert calls[2:] == [None]
    r.tick(ident, [str(src)], now=1000.0 + reporter.USAGE_BACKOFF_SECONDS + 1, send=send, status=st)
    assert calls[3] is not None and calls[4] is None  # retried after the backoff, refused again
    assert r.usage_retry_at == pytest.approx(1000.0 + reporter.USAGE_BACKOFF_SECONDS + 1 + 2 * reporter.USAGE_BACKOFF_SECONDS)


def test_an_unreachable_server_is_still_a_report_failure(tmp_path):
    src = tmp_path / "rollout.jsonl"
    record(src, "2026-11-08T11:59:00Z")
    clock = Clock(tmp_path)
    r = reporter.Reporter()
    r.tick({"usage": {"statusline_file": clock.snapshot(20)}}, [str(src)], now=1000.0,
           send=lambda i, payload, usage=None: False)
    assert r.failures == 1 and r.sent is None


def test_an_outage_does_not_grow_the_usage_backoff(tmp_path):
    """The #559 follow-up: when the bare pair fails too, usage was not the cause, so usage keeps its count and its
    retry time; only the turn backoff grows. RED before: three outage ticks pushed usage_failures to 3, so usage
    stayed off for 4 x 300 s after the server came back. CONTROL: once the server answers again but refuses usage,
    usage backs off from its first step."""
    src = tmp_path / "rollout.jsonl"
    record(src, "2026-11-08T11:59:00Z")
    clock = Clock(tmp_path)
    ident = {"usage": {"statusline_file": clock.snapshot(20)}}
    r = reporter.Reporter()
    down = lambda i, payload, usage=None: False
    now = 1000.0
    for _ in range(3):
        r.tick(ident, [str(src)], now=now, send=down)
        now = r.next_try + 1
    assert r.failures == 3 and r.usage_failures == 0 and r.usage_retry_at <= 1000.0
    calls = []

    def refuses_usage(i, payload, usage=None):
        calls.append(usage)
        return usage is None

    r.tick(ident, [str(src)], now=now, send=refuses_usage)
    assert [c is None for c in calls] == [False, True]
    assert r.usage_failures == 1 and r.failures == 0
    assert r.usage_retry_at == pytest.approx(now + reporter.USAGE_BACKOFF_SECONDS)


def test_run_child_passes_a_private_usage_file_and_removes_it(tmp_path, monkeypatch):
    seen = {}

    def fake(argv, **kw):
        i = argv.index("--usage-file")
        path = argv[i + 1]
        seen["path"] = path
        with open(path, encoding="utf-8") as fh:
            seen["usage"] = json.load(fh)

        class R:
            returncode = 0
            stdout = json.dumps({"ok": True, "applied": True})
        return R()

    monkeypatch.setattr(reporter.postbox_os, "run_bounded", fake)
    ident = {"base_url": "http://x", "state": "s.json", "client": str(tmp_path)}
    usage = {"as_of": "2026-11-08T12:00:00+00:00", "model": None, "context_used_pct": 1.0, "limits": []}
    assert reporter.run_child(ident, {"state": "closed", "item_at": "2026-11-08T11:59:00+00:00"}, usage)
    assert seen["usage"] == usage
    assert not os.path.exists(seen["path"]) and not os.path.exists(os.path.dirname(seen["path"]))
    # Without usage the argv carries no --usage-file at all.
    monkeypatch.setattr(reporter.postbox_os, "run_bounded",
                        lambda argv, **kw: (_ for _ in ()).throw(AssertionError) if "--usage-file" in argv
                        else type("R", (), {"returncode": 0, "stdout": json.dumps({"ok": True, "applied": True})})())
    assert reporter.run_child(ident, {"state": "closed", "item_at": "2026-11-08T11:59:00+00:00"})
