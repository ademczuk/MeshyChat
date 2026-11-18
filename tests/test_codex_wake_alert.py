"""tools/postbox/codex_wake_alert.py: one note per wake-check episode, to someone awake (wake design 1700d1, SD 283).

No database, no server and no client process. step() is driven with the checker's verdicts as plain dicts, through a
fake sender that also proves the intent was on disk before each send. main() runs with a fake subprocess runner.

Run:  pytest tests/test_codex_wake_alert.py -q
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "postbox"))

import codex_wake_alert as wa  # noqa: E402

T = "a8081472-2503-7f40-8e57-3a7ca2249dd0"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def verdict(v, **extra):
    return {"seat": "seat", "target": T, "verdict": v, **extra}


class Sender:
    """A fake `send`: records each call, checks that the intent it carries is already on disk, and fails the first
    `fail` calls (returns None, as a failed client send does)."""

    def __init__(self, path, fail=0):
        self.calls, self.path, self.fail = [], path, fail

    def __call__(self, body, op, ack, reply_to=None):
        on_disk = json.loads(Path(self.path).read_text(encoding="utf-8"))
        assert on_disk["pending"]["op"] == op, "the intent must be on disk before the send"
        self.calls.append({"body": body, "op": op, "ack": ack, "reply_to": reply_to})
        if self.fail:
            self.fail -= 1
            return None
        return f"msg/1794130{len(self.calls):06d}-kxxx-abcdef"


def seat(tmp_path):
    ident = {"name": "seat", "target": T, "log": str(tmp_path / "seat.log")}
    return ident, wa.state_path(ident)


def test_an_ok_seat_sends_nothing_and_keeps_no_state(tmp_path):
    ident, path = seat(tmp_path)
    send = Sender(path)
    for v in ("OK", "BUSY", "RATE_LIMITED"):
        line, ok = wa.step(ident, verdict(v), NOW, send)
        assert ok and line["actions"] == []
    assert send.calls == [] and not os.path.exists(path)


def test_a_failure_opens_one_note_with_an_ack_and_no_more_while_it_lasts(tmp_path):
    ident, path = seat(tmp_path)
    send = Sender(path)
    line, ok = wa.step(ident, verdict("STUCK", waited_s=1900), NOW, send)
    assert ok and len(send.calls) == 1
    first = send.calls[0]
    assert (first["ack"], first["reply_to"], first["op"]) == (True, None, f"wake-seat-{int(NOW)}")
    assert "STUCK" in first["body"] and "automated" in first["body"] and '"waited_s": 1900' in first["body"]
    for later in (300, 600, 3700):                          # the same verdict, later, even past the hour: nothing
        wa.step(ident, verdict("STUCK"), NOW + later, send)
    assert len(send.calls) == 1
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    assert (state["note_key"], state["noted"], state["seen"], "pending" in state) == (
        "msg/1794130000001-kxxx-abcdef", "STUCK", 4, False)


def test_the_episode_closes_with_one_reply_and_the_state_clears(tmp_path):
    ident, path = seat(tmp_path)
    send = Sender(path)
    wa.step(ident, verdict("DELIVERY_STALLED"), NOW, send)
    line, ok = wa.step(ident, verdict("OK"), NOW + 600, send)
    assert ok and len(send.calls) == 2
    close = send.calls[1]
    assert (close["ack"], close["reply_to"], close["op"]) == (
        False, "msg/1794130000001-kxxx-abcdef", f"wake-seat-{int(NOW)}-end")
    assert "DELIVERY_STALLED" in close["body"] and "is over" in close["body"]
    assert not os.path.exists(path)
    wa.step(ident, verdict("OK"), NOW + 900, send)          # and nothing more after it
    assert len(send.calls) == 2


def test_a_crash_read_once_sends_nothing_and_twice_in_a_row_sends_one_note(tmp_path):
    """pxx 2df782: a crash the loop recovered from reads `crashed` for a minute, like a crash loop; the checker marks
    that DELIVERY_DOWN with confirm_runs 2."""
    ident, path = seat(tmp_path)
    send = Sender(path)
    down = verdict("DELIVERY_DOWN", why="status.json: run_codex_delivery crashed", confirm_runs=2)
    wa.step(ident, down, NOW, send)
    wa.step(ident, verdict("OK"), NOW + 300, send)          # recovered: the episode ends unannounced
    assert send.calls == [] and not os.path.exists(path)
    wa.step(ident, down, NOW + 600, send)
    assert send.calls == []
    wa.step(ident, down, NOW + 900, send)                   # the second run in a row: now it is told
    assert len(send.calls) == 1 and send.calls[0]["op"] == f"wake-seat-{int(NOW + 600)}"
    other = verdict("STUCK")                                # a verdict without confirm_runs is told at once
    ident2, path2 = seat(tmp_path / "b")
    os.makedirs(tmp_path / "b")
    send2 = Sender(path2)
    wa.step(ident2, other, NOW, send2)
    assert len(send2.calls) == 1


def test_a_failed_send_keeps_its_intent_and_the_next_run_retries_the_very_same_operation(tmp_path):
    """The #549 lesson (a reviewer 64ccb5): the intent is written before the send, and a send that fails (or a run
    that dies) leaves it for the next run, which sends the SAME op id and the SAME body, whatever it reads now."""
    ident, path = seat(tmp_path)
    send = Sender(path, fail=1)
    line, ok = wa.step(ident, verdict("STUCK"), NOW, send)
    assert not ok and line["actions"] == [{"failed": "open", "op": f"wake-seat-{int(NOW)}"}]
    pending = json.loads(Path(path).read_text(encoding="utf-8"))["pending"]
    assert (pending["kind"], pending["op"]) == ("open", f"wake-seat-{int(NOW)}")
    line, ok = wa.step(ident, verdict("DELIVERY_STALLED"), NOW + 300, send)   # the seat reads otherwise now
    assert ok and len(send.calls) == 2
    assert send.calls[1] == send.calls[0]                   # the same operation, body included
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    assert (state["note_key"], state["noted"], "pending" in state) == ("msg/1794130000002-kxxx-abcdef", "STUCK", False)


def test_a_changed_verdict_is_noted_again_only_an_hour_after_the_last_note(tmp_path):
    ident, path = seat(tmp_path)
    send = Sender(path)
    wa.step(ident, verdict("STUCK"), NOW, send)
    wa.step(ident, verdict("DELIVERY_STALLED"), NOW + 600, send)
    wa.step(ident, verdict("STUCK"), NOW + 900, send)
    wa.step(ident, verdict("DELIVERY_STALLED"), NOW + 1200, send)
    assert len(send.calls) == 1                             # alternating verdicts inside the hour: nothing more
    wa.step(ident, verdict("DELIVERY_STALLED"), NOW + 3601, send)
    assert len(send.calls) == 2
    changed = send.calls[1]
    assert (changed["ack"], changed["reply_to"]) == (True, "msg/1794130000001-kxxx-abcdef")
    assert "now reads DELIVERY_STALLED (was STUCK)" in changed["body"]


def test_the_state_file_is_owner_only_and_replaced_whole(tmp_path):
    ident, path = seat(tmp_path)
    wa.step(ident, verdict("STUCK"), NOW, Sender(path))
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    Path(path).write_text("{not json", encoding="utf-8")    # a state it cannot use repeats a note, never hides one
    assert wa.load_state(path) == {}
    Path(path).write_text(json.dumps({"schema": 2, "note_key": "msg/1794130000001-kxxx-abcdef"}), encoding="utf-8")
    assert wa.load_state(path) == {}                        # nor one of another schema


def test_a_dry_run_plans_and_touches_nothing(tmp_path):
    ident, path = seat(tmp_path)
    send = Sender(path)
    line, ok = wa.step(ident, verdict("STUCK"), NOW, send, dry_run=True)
    assert ok and line["actions"] == [{"would_send": "open", "op": f"wake-seat-{int(NOW)}"}]
    assert send.calls == [] and not os.path.exists(path)


def test_op_ids_fit_the_clients_rule():
    op = wa.op_id("wake", "a seat/with:odd*chars", 1794120000, "DELIVERY_DOWN", "x" * 200)
    assert len(op) == 128 and op.startswith("wake-a-seat-with-odd-chars-1794120000-DELIVERY_DOWN-")
    assert all(c.isalnum() or c in "._-" for c in op)


def _config(tmp_path):
    log = tmp_path / "seat.log"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"identities": [{"name": "seat", "target": T, "log": str(log)}]}), encoding="utf-8")
    return config


def test_the_command_sends_through_the_client_to_every_seat(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    monkeypatch.setattr(wa.cw, "judge", lambda ident, now, status=None: verdict("STUCK"))
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"key": "msg/1794130000009-kxxx-abcdef",
                                                                 "status": "completed"}) + "\n", stderr="")

    argv = ["--config", str(config), "--base-url", "http://mesh.invalid:6310", "--state-file", "sender-state.json",
            "--to", "oxxxxxx", "--to", "pxx", "--client-dir", str(tmp_path), "--python", "py", "--now", str(NOW)]
    assert wa.main(argv, run=run) == 0
    cmd, kw = seen[0]
    assert cmd[:4] == ["py", "-m", "tools.meshy_node", "send"] and kw["cwd"] == str(tmp_path)
    pairs = list(zip(cmd, cmd[1:]))
    assert ("--to", "oxxxxxx") in pairs and ("--to", "pxx") in pairs and ("--thread", "wake-alerts") in pairs
    assert ("--state-file", "sender-state.json") in pairs and ("--op-id", f"wake-seat-{int(NOW)}") in pairs
    assert "--requires-ack" in cmd and "--in-reply-to" not in cmd
    line = json.loads(capsys.readouterr().out)
    assert line["actions"] == [{"sent": "open", "op": f"wake-seat-{int(NOW)}", "key": "msg/1794130000009-kxxx-abcdef"}]


def test_the_command_exits_1_when_a_send_does_not_complete(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    monkeypatch.setattr(wa.cw, "judge", lambda ident, now, status=None: verdict("STUCK"))
    argv = ["--config", str(config), "--base-url", "http://mesh.invalid:6310", "--state-file", "s.json",
            "--to", "oxxxxxx", "--to", "pxx", "--now", str(NOW)]
    done = json.dumps({"key": "msg/1794130000009-kxxx-abcdef", "status": "completed"}) + "\n"
    for rc, stdout in ((1, ""), (1, done),                  # a failed exit is a failed send, whatever it printed
                       (0, json.dumps({"key": "msg/1794130000009-kxxx-abcdef", "status": "failed"}) + "\n"),
                       (0, "not json\n")):
        def run(cmd, rc=rc, stdout=stdout, **kw):
            return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")
        assert wa.main(argv, run=run) == 1, (rc, stdout)
        warning = json.loads(capsys.readouterr().err.splitlines()[-1])   # a stuck send is said, not only exit 1
        assert (warning["warning"], warning["seat"], warning["op"]) == ("send_failed", "seat", f"wake-seat-{int(NOW)}")
    assert wa.main(["--config", str(tmp_path / "missing.json"), "--base-url", "u", "--state-file", "s",
                    "--to", "x"]) == 3
