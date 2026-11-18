"""codex_front.queue_at_front and the postbox's codex_send_priority, against a fake app-server (no Codex, no seat)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "postbox"))
import codex_front  # noqa: E402

FAKE = str(ROOT / "tests" / "fake_codex_app_server.py")


def fake(tmp_path, mode, initial=("O1", "O2", "O3")):
    rec = tmp_path / ("calls-%s.jsonl" % mode)
    return [sys.executable, FAKE, mode, json.dumps(list(initial)), str(rec)], rec


def calls(rec):
    return [json.loads(l)["method"] for l in rec.read_text(encoding="utf-8").splitlines()] if rec.exists() else []


def test_the_notice_goes_ahead_of_three_already_queued(tmp_path):
    cmd, rec = fake(tmp_path, "ok")
    r = codex_front.queue_at_front(cmd, "t-1", "[PRIORITY] D", "postbox-priority-k1", timeout=20)
    assert r["added"] is True and r["front"] is True and r["ahead_before"] == 3, r
    m = calls(rec)
    assert m[:3] == ["initialize", "initialized", "thread/queue/add"] and "thread/queue/reorder" in m
    assert m.count("thread/queue/add") == 1                    # added exactly once
    assert not {"thread/resume", "turn/start", "turn/steer", "thread/queue/start"} & set(m)   # never a competing runtime


def test_an_empty_queue_needs_no_reorder(tmp_path):
    cmd, rec = fake(tmp_path, "ok", initial=())
    r = codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)
    assert r["front"] is True and r["ahead_before"] == 0 and "thread/queue/reorder" not in calls(rec)


def test_a_refused_experimental_api_raises_so_the_caller_falls_back_and_nothing_was_added(tmp_path):
    cmd, rec = fake(tmp_path, "no_experimental")
    with pytest.raises(codex_front.FrontError, match="initialize refused"):
        codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)
    assert "thread/queue/add" not in calls(rec)


def test_a_helper_that_cannot_start_raises_front_error(tmp_path):
    with pytest.raises(codex_front.FrontError, match="did not start"):
        codex_front.queue_at_front([str(tmp_path / "no-such-codex.exe"), "app-server", "--stdio"], "t", "D", "c",
                                   timeout=5)


def test_an_add_that_was_sent_but_never_answered_is_unknown_never_a_fallback(tmp_path):
    cmd, rec = fake(tmp_path, "exit_after_add")
    r = codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)
    assert r["added"] is None and "unknown" in r["why"] and calls(rec).count("thread/queue/add") == 1


def test_a_stale_reorder_is_read_again_once_and_then_succeeds(tmp_path):
    cmd, rec = fake(tmp_path, "reorder_fail_once")
    r = codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)
    assert r["front"] is True and calls(rec).count("thread/queue/reorder") == 2 and calls(rec).count("thread/queue/add") == 1


def test_an_add_without_an_id_stays_at_the_back_and_is_not_added_again(tmp_path):
    cmd, rec = fake(tmp_path, "no_id")
    r = codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)
    assert r["added"] is True and r["front"] is False and calls(rec).count("thread/queue/add") == 1


def _broken_pipe_on(monkeypatch, method):
    """Make the helper's write of `method` raise BrokenPipeError, as a server that closed its stdin does. A real
    closed pipe can't be forced deterministically from a fake on Windows: the venv launcher keeps its own handle."""
    real = codex_front._Session.send

    def send(self, msg):
        if msg.get("method") == method:
            raise BrokenPipeError(32, "Broken pipe")
        return real(self, msg)
    monkeypatch.setattr(codex_front._Session, "send", send)


def test_a_broken_pipe_on_the_add_is_uncertain_so_nothing_is_appended(tmp_path, monkeypatch):
    """pxx 2627ce P2: a BrokenPipeError from send() escaped both callers. Now it is caught; and a failed write of the
    ADD counts as uncertain (codex-bxxxxx-cli 445bac: never append after an uncertain send), so the result is
    unknown and the caller does not fall back."""
    _broken_pipe_on(monkeypatch, "thread/queue/add")
    cmd, rec = fake(tmp_path, "ok")
    r = codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)
    assert r["added"] is None and "unknown" in r["why"]


def test_a_helper_that_stops_reading_cannot_block_a_big_write_past_the_deadline(tmp_path):
    """pxx 9709c8 P2: writes had no deadline. A notice bigger than the pipe buffer to a server that no longer reads
    blocked forever, and with it the seat's delivery loop. Now the write is bounded; the add is uncertain."""
    import time
    cmd, rec = fake(tmp_path, "no_read_after_init")
    t0 = time.monotonic()
    r = codex_front.queue_at_front(cmd, "t-1", "x" * 300_000, "c", timeout=4)
    assert time.monotonic() - t0 < 12                     # the deadline plus close(); never "forever"
    assert r["added"] is None and "unknown" in r["why"]


def test_an_item_missing_from_the_list_is_unknown_never_a_success(tmp_path):
    """codex-bxxxxx-cli 445bac P2: on the real runtime the helper said front=True ("it was next") while dispatch ran
    O1, O2, O3, D. Missing from the list is no evidence of position."""
    cmd, rec = fake(tmp_path, "vanish")
    r = codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)
    assert r["added"] is True and r["front"] is None and "UNKNOWN" in r["why"]
    assert codex_front._in_front([("O1", "")], "D") is None


def test_a_broken_pipe_after_the_add_leaves_it_where_it_is(tmp_path, monkeypatch):
    _broken_pipe_on(monkeypatch, "thread/queue/reorder")
    cmd, rec = fake(tmp_path, "ok")
    r = codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)
    assert r["added"] is True and r["front"] is False and "left where it is" in r["why"]
    assert calls(rec).count("thread/queue/add") == 1


def test_a_broken_pipe_on_initialized_is_a_clean_fallback(tmp_path, monkeypatch):
    _broken_pipe_on(monkeypatch, "initialized")
    cmd, rec = fake(tmp_path, "ok")
    with pytest.raises(codex_front.FrontError, match="could not send initialized"):
        codex_front.queue_at_front(cmd, "t-1", "D", "c", timeout=20)


def test_an_earlier_priority_notice_keeps_its_place_ahead_of_ours(tmp_path):
    """pxx 2627ce P3: FIFO among priority notices, all of them ahead of ordinary items."""
    cmd, rec = fake(tmp_path, "ok", initial=("P1|postbox-priority-a", "O1", "O2"))
    r = codex_front.queue_at_front(cmd, "t-1", "D", "postbox-priority-b", timeout=20)
    assert r["front"] is True and r["ahead_before"] == 2, r                 # moved past O1 and O2 only


def test_front_order_puts_ours_after_leading_priority_items_only():
    items = [("P1", "postbox-priority-a"), ("O1", ""), ("P2", "postbox-priority-c"), ("D", "postbox-priority-d")]
    assert codex_front.front_order(items, "D") == (["P1", "D", "O1", "P2"], 1)
    assert codex_front.front_order([("O1", ""), ("D", "x")], "D") == (["D", "O1"], 0)


# ---- the postbox side: opt-in, fallback, no double send (pb: postbox.py loaded fresh, status and error log in
# tmp_path, the fixture the other postbox tests use)

from tests.test_postbox_reconcile import pb  # noqa: E402,F401


def test_without_priority_front_the_old_append_runs_and_the_helper_never_starts(pb, tmp_path):
    appended = []
    ident = {"priority_lane": True, "target": "t-1", "codex_app_server": [str(tmp_path / "never-run")]}
    ok, detail = pb.codex_send_priority(ident, "[PRIORITY] D", "c", lambda text: appended.append(text) or 0)
    assert ok and appended == ["[PRIORITY] D"] and "appended" in detail


def test_priority_front_needs_the_lane_too(pb):
    assert pb.codex_priority_front_on({"priority_front": True}) is False
    assert pb.codex_priority_front_on({"priority_front": True, "priority_lane": True}) is True
    assert pb.codex_priority_front_on({"priority_front": "yes", "priority_lane": True}) is False


def test_with_priority_front_the_helper_runs_and_the_append_does_not(pb, tmp_path):
    cmd, rec = fake(tmp_path, "ok")
    appended = []
    ident = {"priority_lane": True, "priority_front": True, "target": "t-1", "codex_app_server": cmd}
    ok, detail = pb.codex_send_priority(ident, "[PRIORITY] D", "c", lambda text: appended.append(text) or 0)
    assert ok and appended == [] and detail.startswith("FRONT:"), detail


def test_a_refused_api_falls_back_to_the_append_once(pb, tmp_path):
    cmd, rec = fake(tmp_path, "no_experimental")
    appended = []
    ident = {"priority_lane": True, "priority_front": True, "target": "t-1", "codex_app_server": cmd}
    ok, detail = pb.codex_send_priority(ident, "[PRIORITY] D", "c", lambda text: appended.append(text) or 0)
    assert ok and appended == ["[PRIORITY] D"] and "front helper unavailable" in detail


def test_the_helper_gets_the_seats_own_codex_home(pb, tmp_path, monkeypatch):
    seen = {}

    def fake_front(cmd, thread, text, client_id, *, env=None, timeout=60.0):
        seen["home"] = (env or {}).get("CODEX_HOME")
        return {"added": True, "id": "x", "front": True, "ahead_before": 0, "why": "ok"}
    monkeypatch.setattr(pb.codex_front, "queue_at_front", fake_front)
    base = {"priority_lane": True, "priority_front": True, "target": "t-1"}
    pb.codex_send_priority(dict(base, codex_home=str(tmp_path / "h1")), "D", "c", lambda t: 0)
    assert seen["home"] == str(tmp_path / "h1")
    pb.codex_send_priority(dict(base, codex_sessions_dir=str(tmp_path / "h2" / "sessions")), "D", "c", lambda t: 0)
    assert seen["home"] == str(tmp_path / "h2")


def test_an_unknown_add_is_not_appended_too(pb, tmp_path):
    cmd, rec = fake(tmp_path, "exit_after_add")
    appended = []
    ident = {"priority_lane": True, "priority_front": True, "target": "t-1", "codex_app_server": cmd}
    ok, detail = pb.codex_send_priority(ident, "[PRIORITY] D", "c", lambda text: appended.append(text) or 0)
    assert ok is False and appended == [] and "unknown" in detail
