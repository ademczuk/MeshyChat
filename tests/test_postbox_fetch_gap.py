"""Not-fetched tracking (ASDLC OI-76 P1): delivered mail that nobody READS must become visible.

Liveness checks see a listener that is alive and delivering. They cannot see a notice queued into a Codex thread nobody
reads (OI-77) or a waiter that swallows its wake (OI-75): the mail arrives and sits. So the client records each message it
reads in `<state file>.fetched.jsonl` (keys only), and the postbox compares the spool's ADDRESSED mail since tracking began
with those keys. The age of the oldest unread message goes to status.json, and the HQB pager pages on it.

Run:  .venv/Scripts/python.exe -m pytest tests/test_postbox_fetch_gap.py -q -p no:cacheprovider --noconftest
Mutants (each must fail at least one test here):
  F1 cc counts as waiting                      caught by test_only_addressed_mail_waits_and_a_read_clears_it
  F2 mail from before tracking began counts    caught by test_mail_before_the_opt_in_is_not_counted
  F3 a read clears nothing                     caught by test_only_addressed_mail_waits_and_a_read_clears_it
  F4 the trim keeps the old tracking start     caught by test_a_trim_moves_the_window_so_no_read_is_lost
  F5 the client writes without the opt-in file caught by test_the_client_records_a_read_only_when_tracking_is_on
  F6 the trim cuts by date alone               caught by test_a_trim_never_moves_the_window_past_an_unread_message
  F7 the sidecar is created, then `since` written caught by test_a_read_during_creation_cannot_take_the_first_line
  F8 the client appends without the lock          caught by test_a_read_recorded_during_a_trim_is_not_lost
  F9 the trim does not take the lock               caught by test_a_read_recorded_during_a_trim_is_not_lost
  F10 the trim runs without the lock               caught by test_a_trim_waits_for_the_lock_and_skips_without_it
  F11 a read waits for the lock forever            caught by test_a_read_does_not_hang_on_a_held_lock
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
sys.path.insert(0, str(ROOT))

T0 = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def pb(tmp_path):
    spec = importlib.util.spec_from_file_location("pbtest_fetch_gap_postbox", PB / "postbox.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATUS = str(tmp_path / "status.json")
    mod.ERR = str(tmp_path / "postbox.err.log")
    mod._status.clear()
    return mod


def _ident(tmp_path):
    return {"name": "seat-prod", "log": str(tmp_path / "seat-prod.log"),
            "state": str(tmp_path / "seat-prod-state.json"), "track_fetches": True}


def _spool(ident, *rows):
    with open(ident["log"], "a", encoding="utf-8") as f:
        for key, role, at in rows:
            f.write(json.dumps({"event": "message", "type": "mesh_message", "key": key, "role": role,
                                "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n")


def _read(ident, key, at):
    with open(ident["state"] + ".fetched.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "version": 1, "at": at.isoformat()}) + "\n")


def test_only_addressed_mail_waits_and_a_read_clears_it(pb, tmp_path):
    ident = _ident(tmp_path)
    assert pb.fetch_gap(ident, clock=lambda: T0) == (0, None)          # the opt-in starts at T0
    m = dt.timedelta(minutes=1)
    _spool(ident, ("msg/1-a", "to", T0 + m), ("msg/2-b", "cc", T0 + 2 * m), ("msg/3-c", "to", T0 + 3 * m))
    assert pb.fetch_gap(ident, clock=lambda: T0 + 10 * m) == (2, T0 + m)
    status = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))["seat-prod"]
    assert status["unfetched"] == 2 and status["oldest_unfetched_at"] == (T0 + m).isoformat()
    _read(ident, "msg/1-a", T0 + 11 * m)
    assert pb.fetch_gap(ident, clock=lambda: T0 + 12 * m) == (1, T0 + 3 * m)
    _read(ident, "msg/3-c", T0 + 13 * m)
    assert pb.fetch_gap(ident, clock=lambda: T0 + 14 * m) == (0, None)
    status = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))["seat-prod"]
    assert status["unfetched"] == 0 and status["oldest_unfetched_at"] is None


def test_mail_before_the_opt_in_is_not_counted(pb, tmp_path):
    """Turning tracking on must not page for months of mail read before the sidecar existed."""
    ident = _ident(tmp_path)
    _spool(ident, ("msg/old", "to", T0 - dt.timedelta(days=30)))
    assert pb.fetch_gap(ident, clock=lambda: T0) == (0, None)
    _spool(ident, ("msg/new", "to", T0 + dt.timedelta(seconds=5)))         # CONTROL: new mail counts
    assert pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(hours=1))[0] == 1


def test_both_time_spellings_compare_as_times(pb, tmp_path):
    """The listener writes "...Z", reconcile writes the inbox's "+00:00" with microseconds."""
    ident = _ident(tmp_path)
    pb.fetch_gap(ident, clock=lambda: T0)
    with open(ident["log"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "message", "key": "msg/r", "role": "to", "recovered": True,
                            "created_at": (T0 + dt.timedelta(seconds=1)).isoformat()}) + "\n")
    assert pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(minutes=5)) == (1, T0 + dt.timedelta(seconds=1))


def test_a_trim_moves_the_window_so_no_read_is_lost(pb, tmp_path, monkeypatch):
    """Past FETCH_SIDECAR_MAX the sidecar keeps the last FETCH_KEEP_DAYS of reads, and the tracking start moves up to the
    same point: a message whose read line was trimmed is older than the window, so it cannot come back as unread."""
    ident = _ident(tmp_path)
    pb.fetch_gap(ident, clock=lambda: T0)
    old, recent = T0 + dt.timedelta(hours=1), T0 + dt.timedelta(days=9)
    _spool(ident, ("msg/old", "to", old), ("msg/recent", "to", recent))
    _read(ident, "msg/old", old + dt.timedelta(minutes=1))
    _read(ident, "msg/recent", recent + dt.timedelta(minutes=1))
    monkeypatch.setattr(pb, "FETCH_SIDECAR_MAX", 1)                    # force the trim
    now = T0 + dt.timedelta(days=10)
    assert pb.fetch_gap(ident, clock=lambda: now) == (0, None)
    lines = Path(ident["state"] + ".fetched.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"since": (now - dt.timedelta(days=pb.FETCH_KEEP_DAYS)).isoformat()}
    assert [json.loads(x)["key"] for x in lines[1:]] == ["msg/recent"]


def test_a_trim_never_moves_the_window_past_an_unread_message(pb, tmp_path, monkeypatch):
    """uxxx, 2026-10-03: a trim by date alone moved `since` past a message unread for 9 days, so it went from
    (1, oldest) to (0, None) and the alert went quiet on exactly the mail it exists for. The cut stops at the
    oldest unread message; reads older than that are still dropped, and once it is read the cut catches up."""
    ident = _ident(tmp_path)
    pb.fetch_gap(ident, clock=lambda: T0)
    stuck, early, late = T0 + dt.timedelta(hours=1), T0 + dt.timedelta(minutes=30), T0 + dt.timedelta(days=9)
    _spool(ident, ("msg/early", "to", early), ("msg/stuck", "to", stuck), ("msg/late", "to", late))
    _read(ident, "msg/early", early + dt.timedelta(minutes=1))
    _read(ident, "msg/late", late + dt.timedelta(minutes=1))
    monkeypatch.setattr(pb, "FETCH_SIDECAR_MAX", 1)                    # force the trim
    now = T0 + dt.timedelta(days=10)
    assert pb.fetch_gap(ident, clock=lambda: now) == (1, stuck)        # still counted after the trim
    lines = Path(ident["state"] + ".fetched.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"since": stuck.isoformat()}       # the cut stopped at the unread message
    assert [json.loads(x)["key"] for x in lines[1:]] == ["msg/late"]  # the read before it was still trimmed
    assert pb.fetch_gap(ident, clock=lambda: now) == (1, stuck)        # and stays counted on the next poll
    _read(ident, "msg/stuck", now)                                     # CONTROL: reading it lets the cut catch up
    assert pb.fetch_gap(ident, clock=lambda: now) == (0, None)
    lines = Path(ident["state"] + ".fetched.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"since": (now - dt.timedelta(days=pb.FETCH_KEEP_DAYS)).isoformat()}


def test_a_read_during_creation_cannot_take_the_first_line(pb, tmp_path, monkeypatch):
    """uxxx, 2026-10-03: the client appends to any sidecar that EXISTS. A daemon that created the file and then
    wrote `since` left a window where a read could land first and damage the first line or be lost. So: at every
    file step of the creation, the moment the sidecar exists it must already start with `since`, and a read the
    client makes at that first moment must count."""
    from tools.meshy_node import node

    ident = _ident(tmp_path)
    sidecar = ident["state"] + ".fetched.jsonl"
    _spool(ident, ("msg/raced", "to", T0 + dt.timedelta(seconds=1)))
    real_open, real_replace = open, os.replace
    seen = []

    def client_looks():
        if os.path.exists(sidecar) and not seen:
            seen.append(Path(sidecar).read_text(encoding="utf-8"))
            node.note_fetched(ident["state"], {"key": "msg/raced", "version": 1})

    def spying_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        client_looks()
        return f

    def spying_replace(src, dst):
        real_replace(src, dst)
        client_looks()
    monkeypatch.setattr(pb, "open", spying_open, raising=False)
    monkeypatch.setattr(pb.os, "replace", spying_replace)
    pb.fetch_gap(ident, clock=lambda: T0)
    monkeypatch.undo()
    assert seen and json.loads(seen[0].splitlines()[0]) == {"since": T0.isoformat()}   # never exists without it
    assert pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(minutes=5)) == (0, None)  # the read counted


def test_a_sidecar_without_a_tracking_start_is_an_error_not_silence(pb, tmp_path):
    ident = _ident(tmp_path)
    Path(ident["state"] + ".fetched.jsonl").write_text('{"key": "msg/x"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        pb.fetch_gap(ident, clock=lambda: T0)


def test_the_client_records_a_read_only_when_tracking_is_on(tmp_path):
    from tools.meshy_node import node

    state = tmp_path / "seat-prod-state.json"
    message = {"key": "msg/1-a", "version": 2}
    assert node.note_fetched(state, message) is False                    # no opt-in file: nothing written
    assert not Path(f"{state}.fetched.jsonl").exists()
    Path(f"{state}.fetched.jsonl").write_text('{"since": "2026-10-03T12:00:00+00:00"}\n', encoding="utf-8")
    assert node.note_fetched(state, message) is True
    assert node.note_fetched(state, {"key": "wiki/not-mail", "version": 1}) is False
    rows = [json.loads(x) for x in Path(f"{state}.fetched.jsonl").read_text(encoding="utf-8").splitlines()[1:]]
    assert [(r["key"], r["version"]) for r in rows] == [("msg/1-a", 2)]
    assert set(rows[0]) == {"key", "version", "at"}                       # keys only: no body, no sender


def test_a_read_through_the_client_lands_in_the_sidecar(tmp_path, monkeypatch):
    """read() itself records the read, after the message verified and assembled."""
    from tools.meshy_node import node, wire

    state_path = tmp_path / "seat-prod-state.json"
    Path(f"{state_path}.fetched.jsonl").write_text('{"since": "2026-10-03T12:00:00+00:00"}\n', encoding="utf-8")
    memo = {"namespace": wire.MESH_NAMESPACE, "key": "msg/9-z", "version": 1, "is_head": True,
            "created_at": "2026-10-03 12:01:00+00:00", "author_label": "peer", "value": {"body": "hi"},
            "created_by_peer_id": "11111111-1111-1111-1111-111111111111"}
    monkeypatch.setattr(node, "require_peer", lambda *a, **k: None)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {
        "status": "completed", "result_signature_verified": True, "result": {"memo": memo}})
    monkeypatch.setattr(wire, "author_signature_ok", lambda m, keys: (True, None))
    monkeypatch.setattr(wire, "status_check", lambda m, v, w: {"status_verified": True})
    message = node.read(state_path, {"base_url": "http://x"}, key="msg/9-z")
    assert message["key"] == "msg/9-z"
    lines = Path(f"{state_path}.fetched.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["key"] == "msg/9-z"


def test_a_read_recorded_during_a_trim_is_not_lost(pb, tmp_path, monkeypatch):
    """kxxx's review of #73: the trim reads the sidecar and replaces it, and a read the client appended in between was
    lost. The client and the trim share a lock beside the sidecar, so a read that arrives mid-trim waits and lands in
    the new file. Here the client reads in a thread just before the replace and is given half a second, which a
    client without the lock uses to append to the old file."""
    from tools.meshy_node import node

    ident = _ident(tmp_path)
    pb.fetch_gap(ident, clock=lambda: T0)
    late = T0 + dt.timedelta(days=9)
    _spool(ident, ("msg/late", "to", late))
    _read(ident, "msg/late", late + dt.timedelta(minutes=1))
    monkeypatch.setattr(pb, "FETCH_SIDECAR_MAX", 1)                    # force the trim
    real_replace = os.replace
    reader = []

    def replace_after_a_read(src, dst):
        if str(dst).endswith(".fetched.jsonl") and not reader:
            t = threading.Thread(target=node.note_fetched, args=(ident["state"], {"key": "msg/raced", "version": 1}))
            t.start()
            reader.append(t)
            t.join(0.5)
        real_replace(src, dst)
    monkeypatch.setattr(pb.os, "replace", replace_after_a_read)
    pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(days=10))
    monkeypatch.undo()
    assert reader, "the trim did not run"
    reader[0].join(10)
    lines = Path(ident["state"] + ".fetched.jsonl").read_text(encoding="utf-8").splitlines()
    assert "msg/raced" in [json.loads(x).get("key") for x in lines[1:]]


def _hold(path):
    """Take the sidecar's lock as another process would, and return a function that lets it go."""
    from tools.meshy_node import node

    fd = node._sidecar_lock(path + ".lock", 1.0)
    assert fd is not None
    return lambda: node._sidecar_unlock(fd)


def test_a_trim_waits_for_the_lock_and_skips_without_it(pb, tmp_path, monkeypatch):
    """A lock not taken in time means no trim this round: the reads are still counted, the file is not rewritten."""
    ident = _ident(tmp_path)
    pb.fetch_gap(ident, clock=lambda: T0)
    late = T0 + dt.timedelta(days=9)
    _spool(ident, ("msg/late", "to", late), ("msg/waiting", "to", late))
    _read(ident, "msg/late", late + dt.timedelta(minutes=1))
    monkeypatch.setattr(pb, "FETCH_SIDECAR_MAX", 1)
    monkeypatch.setattr(pb, "FETCH_LOCK_WAIT", 0.3)
    before = Path(ident["state"] + ".fetched.jsonl").read_text(encoding="utf-8")
    release = _hold(ident["state"] + ".fetched.jsonl")
    try:
        assert pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(days=10)) == (1, late)
        assert Path(ident["state"] + ".fetched.jsonl").read_text(encoding="utf-8") == before   # not trimmed
    finally:
        release()
    pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(days=10))                          # CONTROL: now it trims
    assert Path(ident["state"] + ".fetched.jsonl").read_text(encoding="utf-8") != before


def test_a_read_does_not_hang_on_a_held_lock(tmp_path, monkeypatch):
    """A read waits at most FETCH_LOCK_WAIT for a trim, then appends anyway: a lost line only makes the alert fire."""
    from tools.meshy_node import node

    state = tmp_path / "seat-prod-state.json"
    Path(f"{state}.fetched.jsonl").write_text('{"since": "2026-10-03T12:00:00+00:00"}\n', encoding="utf-8")
    monkeypatch.setattr(node, "FETCH_LOCK_WAIT", 0.3)
    release = _hold(f"{state}.fetched.jsonl")
    try:
        started = time.monotonic()
        assert node.note_fetched(state, {"key": "msg/1-a", "version": 1}) is True
        assert time.monotonic() - started < 3
    finally:
        release()
    assert "msg/1-a" in Path(f"{state}.fetched.jsonl").read_text(encoding="utf-8")
