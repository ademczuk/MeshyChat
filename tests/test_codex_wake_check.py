"""tools/postbox/codex_wake_check.py: did a Codex seat take the mail the postbox handed it (wake design v1, a26d51).

No database and no server: each test builds a seat in a temporary folder (a Codex home with its sessions tree and,
where the case needs one, a queue_1.sqlite; a postbox spool with its codex offset and dispatch log) and judges it at a
fixed `now`.

Run:  pytest tests/test_codex_wake_check.py -q
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "postbox"))

import codex_wake_check as cw  # noqa: E402

T = "a8081472-2503-7f40-8e57-3a7ca2249dd0"
OTHER = "01a1c000-0000-7000-8000-000000000001"
NOW = dt.datetime(2026, 11, 5, 11, 39, tzinfo=dt.timezone.utc).timestamp()
SEAT_DIR = os.path.normpath("/seats/admin")


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def ago(minutes):
    return NOW - minutes * 60


class Seat:
    """A seat on disk: write its rollout, dispatch log, spool and queue, then judge it."""

    def __init__(self, tmp: Path, seat_dir=SEAT_DIR):
        self.home = tmp / "home"
        self.sessions = self.home / "sessions"
        self.day = self.sessions / "2026" / "11" / "08"
        self.day.mkdir(parents=True)
        (tmp / "spool").mkdir()
        self.log = str(tmp / "spool" / "seat.log")
        self.ident = {"name": "seat", "target": T, "log": self.log, "codex_sessions_dir": str(self.sessions)}
        if seat_dir:
            self.ident["seat_dir"] = seat_dir
        self.rollout = self.day / f"rollout-2026-11-08T07-00-00-{T}.jsonl"
        self.records = [self._rec(ago(600), "session_meta", {"id": T, "cwd": SEAT_DIR})]
        self.status = None                                  # the identity's status.json entry, when a case sets one

    @staticmethod
    def _rec(t, kind, payload, top="event_msg"):
        if kind == "session_meta":
            return {"timestamp": iso(t), "type": "session_meta", "payload": payload}
        return {"timestamp": iso(t), "type": top, "payload": {"type": kind, **payload}}

    def started(self, t):
        self.records.append(self._rec(t, "task_started", {}))

    def completed(self, t, limit=False):
        self.records.append(self._rec(t, "task_complete", {"codex_error_info": "usage_limit_exceeded"} if limit else {}))

    def aborted(self, t, limit=False):
        self.records.append(self._rec(t, "turn_aborted", {"codex_error_info": "usage_limit_exceeded"} if limit else {}))

    def user(self, t, text):
        self.records.append(self._rec(t, "message", {"role": "user", "content": [{"type": "input_text", "text": text}]},
                                      top="response_item"))

    def assistant(self, t):
        self.records.append(self._rec(t, "message", {"role": "assistant", "content": []}, top="response_item"))

    def parked(self, t, kind="exec_approval_request"):
        self.records.append(self._rec(t, kind, {}))

    def limits(self, t, used, resets):
        self.records.append(self._rec(t, "token_count", {"rate_limits": {"primary": {"used_percent": used,
                                                                                     "resets_at": resets}}}))

    def dispatch(self, t, events, consumer=None):
        line = {"at": iso(t), "identity": "seat", "consumer": consumer or "codex:" + T, "events": events}
        with open(self.log + ".codex.offset.dispatch.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def queue(self, rows):
        con = sqlite3.connect(self.home / "queue_1.sqlite")
        con.execute("CREATE TABLE IF NOT EXISTS queued_items (id INTEGER PRIMARY KEY, thread_id TEXT, "
                    "payload_json TEXT, queue_order INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)")
        for thread, t in rows:
            con.execute("INSERT INTO queued_items (thread_id, payload_json, queue_order, created_at_ms, updated_at_ms) "
                        "VALUES (?, '{}', 0, ?, ?)", (thread, int(t * 1000), int(t * 1000)))
        con.commit()
        con.close()

    def judge(self, written=None, now=NOW):
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in self.records), encoding="utf-8")
        mtime = written if written is not None else max(_epoch(r["timestamp"]) for r in self.records)
        os.utime(self.rollout, (mtime, mtime))
        return cw.judge(self.ident, now, self.status)


def _epoch(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def ev(key, role="to", ack=True):
    return {"event": "message", "key": key, "role": role, "requires_ack": ack}


K = "msg/1793859540000-oxxxxxx-94438f"


def test_a_message_taken_inside_the_grace_and_answered_is_ok(tmp_path):
    seat = Seat(tmp_path)
    seat.started(ago(40) - 5)
    seat.user(ago(40) - 10, f"notice: {K}")                # the turn began 10 s before the hand-off was logged
    seat.completed(ago(39))
    seat.dispatch(ago(40), [ev(K)])
    assert seat.judge()["verdict"] == "OK"


def test_a_message_taken_six_minutes_late_is_ok(tmp_path):
    seat = Seat(tmp_path)
    seat.dispatch(ago(60), [ev(K)])
    seat.started(ago(54))
    seat.user(ago(54), f"notice: {K}")
    seat.completed(ago(50))
    assert seat.judge()["verdict"] == "OK"


def test_a_message_nobody_took_for_half_an_hour_is_stuck(tmp_path):
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    seat.dispatch(ago(31), [ev(K)])
    out = seat.judge()
    assert (out["verdict"], out["keys"], out["waited_s"]) == ("STUCK", [K], 31 * 60)
    assert cw.EXIT[out["verdict"]] == 2
    # One minute younger, it is still inside the wait: nothing to say yet.
    seat2 = Seat(tmp_path / "b")
    seat2.started(ago(120))
    seat2.completed(ago(119))
    seat2.dispatch(ago(29), [ev(K)])
    assert seat2.judge()["verdict"] == "OK"


def test_cc_mail_without_an_ack_never_alarms_and_everything_else_must_be_taken(tmp_path):
    """a reviewer ca5875, pxx c29b75: a seat must take every message addressed TO it, ack or not, and a cc that asks
    for a reply. Only a cc without one may be held by design (postbox_os.held_by_default) and never alarms. Requiring
    requires_ack on "to" as well left 33 of HQB's 35 dispatches in a day unchecked."""
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    seat.dispatch(ago(90), [ev(K, role="cc", ack=False)])
    assert seat.judge()["verdict"] == "OK"
    for role, ack in (("to", False), ("cc", True)):           # neither may be left untaken past the wait
        other = Seat(tmp_path / f"{role}-{ack}")
        other.started(ago(120))
        other.completed(ago(119))
        other.dispatch(ago(90), [ev(K, role=role, ack=ack)])
        out = other.judge()
        assert (out["verdict"], out["keys"]) == ("STUCK", [K]), (role, ack)


def test_a_turn_running_and_still_writing_is_busy(tmp_path):
    seat = Seat(tmp_path)
    seat.dispatch(ago(10), [ev(K)])
    seat.started(ago(20))
    seat.assistant(ago(1))
    assert seat.judge()["verdict"] == "BUSY"


def test_a_turn_waiting_on_a_person_is_parked(tmp_path):
    seat = Seat(tmp_path)
    seat.started(ago(100))
    seat.parked(ago(95))
    seat.limits(ago(94), 10.0, NOW + 3600)                  # a token_count after the request does not hide it
    seat.dispatch(ago(60), [ev(K)])
    out = seat.judge()
    assert (out["verdict"], out["waiting_on"]) == ("PARKED", "exec_approval_request")


def test_a_newer_session_of_the_seat_is_a_stale_target(tmp_path):
    seat = Seat(tmp_path)
    seat.started(ago(300))
    seat.completed(ago(299))
    seat.dispatch(ago(60), [ev(K)])
    newer = seat.day / f"rollout-2026-11-08T10-00-00-{OTHER}.jsonl"
    newer.write_text(json.dumps({"timestamp": iso(ago(100)), "type": "session_meta",
                                 "payload": {"id": OTHER, "cwd": SEAT_DIR}}) + "\n", encoding="utf-8")
    os.utime(newer, (ago(5), ago(5)))
    out = seat.judge()
    assert (out["verdict"], out["candidate_thread"]) == ("STALE_TARGET", OTHER)


def test_another_seats_session_in_a_shared_home_is_not_a_stale_target(tmp_path):
    """a reviewer 64ccb5: two seats can share one CODEX_HOME. A newer session from another folder proves nothing,
    and without a seat_dir it is only named."""
    for seat_dir, expect_named in ((SEAT_DIR, False), (None, True)):
        sub = tmp_path / ("with" if seat_dir else "without")
        seat = Seat(sub, seat_dir=seat_dir)
        seat.started(ago(300))
        seat.completed(ago(299))
        seat.dispatch(ago(60), [ev(K)])
        newer = seat.day / f"rollout-2026-11-08T10-00-00-{OTHER}.jsonl"
        newer.write_text(json.dumps({"timestamp": iso(ago(100)), "type": "session_meta",
                                     "payload": {"id": OTHER, "cwd": os.path.normpath("/seats/builder")}}) + "\n",
                         encoding="utf-8")
        os.utime(newer, (ago(5), ago(5)))
        out = seat.judge()
        assert out["verdict"] == "STUCK", seat_dir
        assert ("newer_other_thread" in out) is expect_named, seat_dir


def test_a_limit_reached_with_its_reset_ahead_is_rate_limited_and_a_passed_one_is_not(tmp_path):
    seat = Seat(tmp_path)
    seat.started(ago(100))
    seat.limits(ago(99), 100.0, NOW + 3600)
    seat.completed(ago(99))
    seat.dispatch(ago(60), [ev(K)])
    assert seat.judge()["verdict"] == "RATE_LIMITED"
    seat2 = Seat(tmp_path / "b")
    seat2.started(ago(100))
    seat2.limits(ago(99), 100.0, NOW - 60)
    seat2.completed(ago(99))
    seat2.dispatch(ago(60), [ev(K)])
    assert seat2.judge()["verdict"] == "STUCK"
    # pxx's 1 Nov check: a turn that ended on a usage limit, with no turn after it, when token_count says nothing.
    seat3 = Seat(tmp_path / "c")
    seat3.started(ago(100))
    seat3.completed(ago(99), limit=True)
    seat3.dispatch(ago(60), [ev(K)])
    assert seat3.judge()["verdict"] == "RATE_LIMITED"


def test_two_rollouts_for_the_target_or_none_is_unknown(tmp_path):
    seat = Seat(tmp_path)
    seat.dispatch(ago(60), [ev(K)])
    (seat.day / f"rollout-2026-11-08T08-00-00-{T}.jsonl").write_text("", encoding="utf-8")
    out = seat.judge()
    assert (out["verdict"], cw.EXIT[out["verdict"]]) == ("UNKNOWN", 3)
    empty = Seat(tmp_path / "b")
    empty.dispatch(ago(60), [ev(K)])
    assert cw.judge(empty.ident, NOW)["verdict"] == "UNKNOWN"     # no rollout written at all


def test_a_dispatch_log_that_cannot_be_read_is_unknown_never_ok(tmp_path):
    seat = Seat(tmp_path)
    os.mkdir(seat.log + ".codex.offset.dispatch.jsonl")      # a folder where the log should be
    assert seat.judge()["verdict"] == "UNKNOWN"


def test_an_unrelated_turn_after_the_dispatch_does_not_take_it(tmp_path):
    seat = Seat(tmp_path)
    seat.dispatch(ago(60), [ev(K)])
    seat.started(ago(50))
    seat.user(ago(50), "something else entirely")
    seat.completed(ago(49))
    assert seat.judge()["verdict"] == "STUCK"


def test_taken_and_then_nothing_is_taken_not_answered(tmp_path):
    seat = Seat(tmp_path)
    seat.dispatch(ago(60), [ev(K)])
    seat.user(ago(59), f"notice: {K}")
    out = seat.judge(written=ago(59))
    assert (out["verdict"], cw.EXIT[out["verdict"]]) == ("TAKEN_NOT_ANSWERED", 2)


def test_a_long_turn_working_on_the_message_is_not_taken_not_answered(tmp_path):
    seat = Seat(tmp_path)
    seat.dispatch(ago(60), [ev(K)])
    seat.started(ago(59))
    seat.user(ago(59), f"notice: {K}")                      # taken, and the turn is still at it (tools, no answer yet)
    assert seat.judge(written=ago(1))["verdict"] == "OK"


def test_a_dispatch_to_an_earlier_target_is_not_this_threads(tmp_path):
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    seat.dispatch(ago(60), [ev(K)], consumer="codex:" + OTHER)
    assert seat.judge()["verdict"] == "OK"


def test_a_queued_row_nobody_took_is_stuck_whatever_the_dispatch_log_says(tmp_path):
    """F5, a split home: the message sits in the queue the postbox wrote, and no rollout ever shows it."""
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    seat.queue([(T, ago(45)), (OTHER, ago(5000)), (OTHER, ago(4000))])
    out = seat.judge()
    assert (out["verdict"], out["queue_rows"], out["queue_debris"]) == ("STUCK", 1, 2)
    assert out["queued_at"] == ago(45)
    # The first seat has neither a dispatch log nor a spool, and the queue still gave the verdict.
    assert not Path(seat.log).exists() and not Path(seat.log + ".codex.offset.dispatch.jsonl").exists()
    # Rows of other threads alone are old debris: reported, never the verdict.
    seat2 = Seat(tmp_path / "b")
    seat2.started(ago(120))
    seat2.completed(ago(119))
    Path(seat2.log + ".codex.offset.dispatch.jsonl").write_text("", encoding="utf-8")
    seat2.queue([(OTHER, ago(5000))])
    out2 = seat2.judge()
    assert (out2["verdict"], out2["queue_debris"]) == ("OK", 1)
    # A row created after `now` (a replay) does not count.
    seat3 = Seat(tmp_path / "c")
    Path(seat3.log + ".codex.offset.dispatch.jsonl").write_text("", encoding="utf-8")
    seat3.queue([(T, NOW + 600)])
    assert seat3.judge()["verdict"] == "OK"
    # With no queue verdict and neither log, nothing can be said.
    seat4 = Seat(tmp_path / "d")
    assert seat4.judge()["verdict"] == "UNKNOWN"


def test_a_queue_that_cannot_be_read_says_so_and_the_rest_still_judges(tmp_path):
    seat = Seat(tmp_path)
    (seat.home / "queue_1.sqlite").write_bytes(b"not a database at all")
    seat.started(ago(120))
    seat.completed(ago(119))
    seat.dispatch(ago(60), [ev(K)])
    out = seat.judge()
    assert out["verdict"] == "STUCK" and out["queue"].startswith("unreadable")


def test_with_no_dispatch_log_the_spool_below_the_offset_stands_in(tmp_path):
    """a reviewer 64ccb5: a postbox older than the delivery bank writes no dispatch log (the HZ builder's)."""
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    rows = [{"event": "message", "key": K, "role": "to", "requires_ack": True, "rx_at": iso(ago(70))},
            {"event": "status", "state": "connected", "at": iso(ago(69))}]
    head = "".join(json.dumps(r) + "\n" for r in rows)
    later = json.dumps({"event": "message", "key": "msg/1793869539159-pxx-bbbbbb", "role": "to",
                        "requires_ack": True, "rx_at": iso(ago(1))}) + "\n"
    Path(seat.log).write_bytes((head + later).encode("utf-8"))                    # bytes: the offset is in bytes
    Path(seat.log + ".codex.offset").write_bytes(str(len(head.encode("utf-8"))).encode())
    out = seat.judge()
    assert (out["verdict"], out["source"], out["keys"]) == ("STUCK", "spool_offset", [K])
    # The hand-off time is rx_at plus the pacing allowance (settle 30 s + gap 300 s by default).
    assert out["dispatch_at"] == ago(70) + 330
    # A row past the offset was never handed over: the POSTBOX is stalled (F7), not the seat. And past the offset,
    # it is never taken for a hand-off the seat failed to take: read already (the skips file), it is nothing at all.
    pending_key = "msg/1793859540002-pxx-cccccc"
    for skipped, expect in ((False, "DELIVERY_STALLED"), (True, "OK")):
        sub = Seat(tmp_path / ("skipped" if skipped else "stalled"))
        sub.started(ago(64))
        sub.user(ago(64), f"notice: {K}")
        sub.completed(ago(63))
        handed = json.dumps({"event": "message", "key": K, "role": "to", "requires_ack": True,
                             "rx_at": iso(ago(70))}) + "\n"
        pending = json.dumps({"event": "message", "key": pending_key, "role": "to", "requires_ack": True,
                              "rx_at": iso(ago(200))}) + "\n"
        Path(sub.log).write_bytes((handed + pending).encode("utf-8"))
        Path(sub.log + ".codex.offset").write_bytes(str(len(handed.encode("utf-8"))).encode())
        if skipped:
            Path(sub.log + ".codex.offset.skips.jsonl").write_text(
                json.dumps({"key": pending_key, "reason": "already_read"}) + "\n", encoding="utf-8")
        out = sub.judge()
        assert out["verdict"] == expect, skipped
        if not skipped:
            assert (out["keys"], out["arrived_at"], out["waiting"]) == ([pending_key], ago(200), 1)


def _spool(seat, rows):
    Path(seat.log).write_bytes("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"))


def _done(key, version=1, **over):
    """A handled entry the bank counts (delivery_bank.entry_faults): authenticated terminal work."""
    return {"key": key, "version": version, "disposition": "done", "signature_verified": True,
            "status_verified": True, **over}


def _receipts(seat, path, entries, identity="seat"):
    """The seat's handled file as the bank reads it (delivery_bank.handled): owner-only, the identity's own, and on
    Windows attested by the identity's handled_private_acl_verified."""
    Path(path).write_text(json.dumps({"schema_version": 1, "identity": identity, "handled": entries}),
                          encoding="utf-8")
    os.chmod(path, 0o600)
    seat.ident["handled_file"] = str(path)
    seat.ident["handled_private_acl_verified"] = True


def test_mail_the_postbox_never_handed_over_is_delivery_stalled(tmp_path):
    """F7, what happened to the HZ admin on 8 Nov: the delivery loop crashed every 60 s (a bank file a retarget left
    at 0644), mail piled up in the spool, and nothing was ever queued to Codex."""
    row = {"event": "message", "key": K, "role": "to", "requires_ack": True, "rx_at": iso(ago(40))}
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    _spool(seat, [row, {**row, "key": "msg/1793859540003-pxx-dddddd", "role": "cc", "requires_ack": False}])
    seat.dispatch(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])    # the log exists; no line names K
    out = seat.judge()
    assert (out["verdict"], out["keys"], out["waiting"]) == ("DELIVERY_STALLED", [K], 1)   # the cc row never stalls
    assert cw.EXIT["DELIVERY_STALLED"] == 2
    # Handed over, even to an earlier target, it is not stalled (that thread's own verdicts apply).
    seat2 = Seat(tmp_path / "b")
    seat2.started(ago(120))
    seat2.completed(ago(119))
    _spool(seat2, [row])
    seat2.dispatch(ago(38), [ev(K)], consumer="codex:" + OTHER)
    assert seat2.judge()["verdict"] == "OK"
    # Younger than the wait plus the pacing (30 min + 330 s), it may still be on its way.
    seat3 = Seat(tmp_path / "c")
    seat3.started(ago(120))
    seat3.completed(ago(119))
    _spool(seat3, [{**row, "rx_at": iso(ago(34))}])
    seat3.dispatch(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])
    assert seat3.judge()["verdict"] == "OK"
    # Recorded as handled by the seat: the bank never hands it over, and nothing is stalled.
    seat4 = Seat(tmp_path / "d")
    seat4.started(ago(120))
    seat4.completed(ago(119))
    _spool(seat4, [row])
    seat4.dispatch(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])
    _receipts(seat4, tmp_path / "handled.json", [_done(K)])
    assert seat4.judge()["verdict"] == "OK"


def test_a_bank_file_that_is_not_owner_only_is_mode_drift_before_any_mail_waits(tmp_path, monkeypatch):
    """oxxxxxx 1355a7: a manual retarget left the admin's bank file 0644, and the bank refused it on every pass for
    13 hours. The mode is read through cw._bank_mode (None on Windows, where the bits mean nothing)."""
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    Path(seat.log + ".codex.offset.dispatch.jsonl").write_text("", encoding="utf-8")
    _bank(seat)
    monkeypatch.setattr(cw, "_bank_mode", lambda path: 0o644 if path.endswith(".codex.offset.bank.json") else None)
    out = seat.judge()
    assert (out["verdict"], out["bank_mode"], cw.EXIT["MODE_DRIFT"]) == ("MODE_DRIFT", "0o644", 2)
    assert out["why"] == "bank checkpoint has mode 0o644"
    monkeypatch.setattr(cw, "_bank_mode", lambda path: 0o600)
    assert seat.judge()["verdict"] == "OK"


def test_mail_held_behind_a_running_turn_is_busy_not_stalled(tmp_path):
    """The postbox banks mail while the target's turn runs, for as long as the rollout keeps being written (up to
    its 2 h stale bound): that wait is the postbox's design, not a stall."""
    row = {"event": "message", "key": K, "role": "to", "requires_ack": True, "rx_at": iso(ago(90))}
    seat = Seat(tmp_path)
    seat.started(ago(100))
    seat.assistant(ago(2))
    _spool(seat, [row])
    seat.dispatch(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])
    out = seat.judge()
    assert (out["verdict"], out["held"]) == ("BUSY", 1)
    # The same with no dispatch log (an older postbox): the row past the codex offset is held, not a hand-off the seat
    # failed to take, so it is named under "held" and never reads as untaken mail.
    old = Seat(tmp_path / "spool")
    old.started(ago(100))
    old.assistant(ago(2))
    before = json.dumps({"event": "status", "state": "connected", "at": iso(ago(300))}) + "\n"
    Path(old.log).write_bytes((before + json.dumps(row) + "\n").encode("utf-8"))
    Path(old.log + ".codex.offset").write_bytes(str(len(before.encode("utf-8"))).encode())
    out = old.judge()
    assert (out["verdict"], out["held"], out["source"]) == ("BUSY", 1, "spool_offset")
    # A turn silent past the postbox's own 2 h bound no longer holds anything: stalled.
    seat2 = Seat(tmp_path / "b")
    seat2.started(ago(300))
    _spool(seat2, [row])
    seat2.dispatch(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])
    assert seat2.judge(written=ago(130))["verdict"] == "DELIVERY_STALLED"


def test_the_command_exits_by_the_worst_verdict(tmp_path, capsys):
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    seat.dispatch(ago(31), [ev(K)])
    seat.judge()                                            # writes the rollout
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"identities": [seat.ident, {"name": "no-codex", "log": "x"}]}), encoding="utf-8")
    assert cw.main(["--config", str(config), "--now", str(NOW)]) == 2
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(x["seat"], x["verdict"]) for x in lines] == [("seat", "STUCK")]
    assert cw.main(["--config", str(tmp_path / "missing.json")]) == 3


def _quiet(seat):
    """Nothing owed: one finished turn and an empty dispatch log."""
    seat.started(ago(120))
    seat.completed(ago(119))
    Path(seat.log + ".codex.offset.dispatch.jsonl").write_text("", encoding="utf-8")


def _bank_text(seat, text):
    """The checkpoint as the bank writes it: owner-only. Written plainly, umask 022 leaves it 0644, which is F7 itself
    (pxx 2df782: 26 of 27 under umask 022 before this)."""
    path = Path(seat.log + ".codex.offset.bank.json")
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


def _bank(seat, **fields):
    data = {"schema_version": 2, "consumer": "codex:" + T, "identity": "seat", "offset": 0, "delivered": [], **fields}
    return _bank_text(seat, json.dumps(data))


def test_a_checkpoint_the_bank_would_refuse_is_delivery_down_before_any_mail_waits(tmp_path):
    """The postbox derives the bank's consumer from the target (codex:<target>), so a retarget makes the bank refuse
    its checkpoint on every pass until it is rewritten (delivery_bank.Bank: consumer, identity, schema_version)."""
    seat = Seat(tmp_path)
    _quiet(seat)
    _bank(seat)
    assert seat.judge()["verdict"] == "OK"                  # the checkpoint the postbox writes for this target
    for fields, word in (({"consumer": "codex:" + OTHER}, "consumer"), ({"consumer": T}, "consumer"),
                         ({"identity": "other-seat"}, "identity"), ({"schema_version": 3}, "schema_version")):
        _bank(seat, **fields)
        out = seat.judge()
        assert (out["verdict"], word in out["why"]) == ("DELIVERY_DOWN", True), fields
    assert cw.EXIT["DELIVERY_DOWN"] == 2
    _bank_text(seat, "{not json")
    assert seat.judge()["why"] == "bank checkpoint invalid: not JSON"
    os.remove(seat.log + ".codex.offset.bank.json")
    assert seat.judge()["verdict"] == "OK"                  # no checkpoint yet: a fresh bank


def test_a_real_checkpoint_file_left_at_0644_is_mode_drift(tmp_path):
    """The F7 shape on a real file (pxx 2df782). On POSIX the bits are read. On Windows they mean nothing, and
    _bank_mode says so with None, so the same file is OK there."""
    seat = Seat(tmp_path)
    _quiet(seat)
    path = _bank(seat)
    os.chmod(path, 0o644)
    out = seat.judge()
    if os.name == "nt":
        assert (cw._bank_mode(path), out["verdict"]) == (None, "OK")
    else:
        assert (out["verdict"], out["bank_mode"]) == ("MODE_DRIFT", "0o644")
    os.chmod(path, 0o600)
    assert seat.judge()["verdict"] == "OK"


def test_a_crashed_delivery_loop_is_delivery_down_at_once_and_a_restarted_one_is_not(tmp_path, monkeypatch):
    """postbox.guarded sets run_codex_delivery "crashed" on every crash and "restarted" 60 s later, so a crash loop reads
    crashed nearly all the time (the HZ admin's: 1,610 crashes in 13 h) and a loop that came back reads restarted."""
    seat = Seat(tmp_path)
    _quiet(seat)
    seat.status = {"run_codex_delivery": "crashed", "codex": "ok", "last_poll_ok_at": iso(ago(1))}
    out = seat.judge()
    assert (out["verdict"], out["why"], out["confirm_runs"]) == (
        "DELIVERY_DOWN", "status.json: run_codex_delivery crashed", 2)    # one read cannot tell a loop from one crash
    seat.status = {**seat.status, "last_poll_ok_at": iso(ago(20))}
    out = seat.judge()                                      # with a stale poll stamp it is down at once
    assert (out["why"], "confirm_runs" in out) == ("status.json: no successful poll for 20 min", False)
    seat.status = {**seat.status, "last_poll_ok_at": iso(ago(1))}
    _bank(seat)
    monkeypatch.setattr(cw, "_bank_mode", lambda path: 0o644)
    assert seat.judge()["verdict"] == "MODE_DRIFT"          # the cause, when it is known, before its effect
    monkeypatch.setattr(cw, "_bank_mode", lambda path: None)
    seat.status = {**seat.status, "run_codex_delivery": "restarted"}
    assert seat.judge()["verdict"] == "OK"


def test_a_postbox_that_stopped_polling_is_delivery_down(tmp_path):
    """last_poll_ok_at is stamped on each successful inbox poll (every poll_s, 5 min by default); a dead daemon leaves
    it frozen (postbox.run_poll). Three missed polls are down."""
    seat = Seat(tmp_path)
    _quiet(seat)
    seat.status = {"codex": "ok", "last_poll_ok_at": iso(ago(16))}
    out = seat.judge()
    assert (out["verdict"], out["why"], "confirm_runs" in out) == (
        "DELIVERY_DOWN", "status.json: no successful poll for 16 min", False)
    seat.status = {"codex": "ok", "last_poll_ok_at": iso(ago(14))}
    assert seat.judge()["verdict"] == "OK"
    seat.ident["poll_s"] = 900                              # the identity's own interval: three misses are 45 min
    seat.status = {"codex": "ok", "last_poll_ok_at": iso(ago(30))}
    assert seat.judge()["verdict"] == "OK"
    seat.status = {"codex": "ok", "last_poll_ok_at": iso(ago(46))}
    assert seat.judge()["verdict"] == "DELIVERY_DOWN"


def test_while_codex_queue_keeps_failing_mail_stalls_after_five_minutes(tmp_path):
    """status.json codex "retrying" (codex queue rc != 0, tried again every 60 s) or "hold" (a helper holds delivery):
    an expects-reply row not handed over QUICK_S plus the pacing after it arrived is stalled; otherwise WAIT_S applies."""
    row = {"event": "message", "key": K, "role": "to", "requires_ack": True, "rx_at": iso(ago(12))}
    for codex in ("retrying", "hold"):                      # literal: a narrowed cw.FAILING must fail here
        seat = Seat(tmp_path / codex)
        _quiet(seat)
        _spool(seat, [row])
        seat.status = {"codex": codex, "last_poll_ok_at": iso(ago(1))}
        out = seat.judge()
        assert (out["verdict"], out["keys"], out["postbox_codex"]) == ("DELIVERY_STALLED", [K], codex)
        seat.status = {"codex": "ok", "last_poll_ok_at": iso(ago(1))}
        assert seat.judge()["verdict"] == "OK"              # delivering normally, it may still be on its way


def test_the_command_reads_the_status_file_beside_the_config(tmp_path, capsys):
    seat = Seat(tmp_path)
    _quiet(seat)
    seat.judge()                                            # writes the rollout
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"identities": [seat.ident]}), encoding="utf-8")
    assert cw.main(["--config", str(config), "--now", str(NOW)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "absent"     # an older postbox: nothing to read
    (tmp_path / "status.json").write_text(json.dumps({"seat": {"run_codex_delivery": "crashed"}}), encoding="utf-8")
    assert cw.main(["--config", str(config), "--now", str(NOW)]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "DELIVERY_DOWN"
    (tmp_path / "status.json").write_text(json.dumps({"another": {}}), encoding="utf-8")
    assert cw.main(["--config", str(config), "--now", str(NOW)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no entry"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"seat": {"run_codex_delivery": "crashed"}}), encoding="utf-8")
    assert cw.main(["--config", str(config), "--status", str(elsewhere), "--now", str(NOW)]) == 2
    capsys.readouterr()


def test_a_checkpoint_file_the_banks_own_read_refuses_is_mode_drift(tmp_path, monkeypatch):
    """pxx 4999b1: the bank opens its checkpoint through open_regular_read(require_private=True), which refuses a
    link (even to a good 0600 checkpoint), anything but a regular file, and on POSIX another account's file. A sudo
    retarget leaves a root-owned 0600 file; stat() and the mode alone saw nothing wrong."""
    seat = Seat(tmp_path)
    _quiet(seat)
    good = _bank(seat)
    assert seat.judge()["verdict"] == "OK"
    real = good + ".real"
    os.replace(good, real)
    os.symlink(real, good)                                  # a link to a valid checkpoint for this very target
    out = seat.judge()
    assert (out["verdict"], out["why"]) == ("MODE_DRIFT", "bank checkpoint is a link")
    os.remove(good)
    os.mkdir(good)
    assert seat.judge()["why"] == "bank checkpoint is not a regular file"
    os.rmdir(good)
    os.replace(real, good)
    owner = os.lstat(good).st_uid
    monkeypatch.setattr(cw, "_owner_uid", lambda: owner + 1)       # as if another account wrote it
    assert seat.judge()["why"] == "bank checkpoint is owned by another account"
    monkeypatch.setattr(cw, "_owner_uid", lambda: owner)
    assert seat.judge()["verdict"] == "OK"


def test_with_no_dispatch_log_a_notice_taken_quickly_counts_as_taken(tmp_path):
    """GitHub Codex on #554: from the spool, a hand-off lies between rx_at and rx_at plus the pacing. A take counts from
    rx_at, and only the alarm waits for the pacing; before, a notice taken in its first minute was never seen as
    taken and ended as STUCK."""
    seat = Seat(tmp_path)
    seat.started(ago(69))
    seat.user(ago(69), f"notice: {K}")                      # taken one minute after it arrived, before rx_at + 300 s
    seat.completed(ago(68))
    row = json.dumps({"event": "message", "key": K, "role": "to", "requires_ack": True, "rx_at": iso(ago(70))}) + "\n"
    Path(seat.log).write_bytes(row.encode("utf-8"))
    Path(seat.log + ".codex.offset").write_bytes(str(len(row.encode("utf-8"))).encode())
    out = seat.judge()
    assert (out["verdict"], out["source"], out["checked"]) == ("OK", "spool_offset", 1)


def test_with_no_dispatch_log_a_row_the_bank_passed_over_is_not_owed_a_take(tmp_path):
    """GitHub Codex on #554, second round: the bank also moves the offset past a row it skips as already read (writing
    a skip receipt, and no notice), so without a dispatch log a row below the offset is not always a hand-off. A
    healthy seat whose only mail was skipped read STUCK after the wait. The same holds for a row recorded as handled."""
    row = json.dumps({"event": "message", "key": K, "role": "to", "requires_ack": True, "rx_at": iso(ago(70))}) + "\n"
    for how in ("skips", "handled"):
        seat = Seat(tmp_path / how)
        seat.started(ago(120))
        seat.completed(ago(119))
        Path(seat.log).write_bytes(row.encode("utf-8"))
        Path(seat.log + ".codex.offset").write_bytes(str(len(row.encode("utf-8"))).encode())   # moved past it
        if how == "skips":
            Path(seat.log + ".codex.offset.skips.jsonl").write_text(
                json.dumps({"key": K, "reason": "already_read"}) + "\n", encoding="utf-8")
        else:
            _receipts(seat, tmp_path / how / "handled.json", [_done(K)])
        out = seat.judge()
        assert (out["verdict"], out["source"], out["checked"]) == ("OK", "spool_offset", 0), how


def test_a_turn_aborted_before_any_answer_is_taken_not_answered(tmp_path):
    """GitHub Codex on #554: turn_aborted was read as the end of a turn like task_complete, so a notice whose turn was
    aborted before any assistant message read as answered, OK for good. On a limit it is RATE_LIMITED."""
    seat = Seat(tmp_path)
    seat.dispatch(ago(60), [ev(K)])
    seat.started(ago(59))
    seat.user(ago(59), f"notice: {K}")
    seat.aborted(ago(58))
    out = seat.judge()
    assert (out["verdict"], out["taken_at"]) == ("TAKEN_NOT_ANSWERED", ago(59))
    limited = Seat(tmp_path / "b")
    limited.dispatch(ago(60), [ev(K)])
    limited.started(ago(59))
    limited.user(ago(59), f"notice: {K}")
    limited.aborted(ago(58), limit=True)
    assert limited.judge()["verdict"] == "RATE_LIMITED"
    completed = Seat(tmp_path / "c")                        # a turn that completed still answers
    completed.dispatch(ago(60), [ev(K)])
    completed.started(ago(59))
    completed.user(ago(59), f"notice: {K}")
    completed.completed(ago(58))
    assert completed.judge()["verdict"] == "OK"


def _dispatch_line(t, events):
    return json.dumps({"at": iso(t), "identity": "seat", "consumer": "codex:" + T, "events": events}) + "\n"


def _archive(path, text, written):
    Path(path).write_text(text, encoding="utf-8")
    os.utime(path, (written, written))


def test_after_a_rotation_the_archives_still_count(tmp_path):
    """The dispatch and skips logs rotate at 4 MiB into .1 .. .16, lowest free slot first (README "Status and
    liveness"). Right after a rotation, the lines that show a row was handed over, skipped or still owed a take sit in
    an archive. Reading the active files alone turned a handed-over row into a false DELIVERY_STALLED."""
    filler = _dispatch_line(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])
    row = {"event": "message", "key": K, "role": "to", "requires_ack": True, "rx_at": iso(ago(40))}

    def rotated(name):
        s = Seat(tmp_path / name)
        s.started(ago(120))
        s.completed(ago(119))
        Path(s.log + ".codex.offset.dispatch.jsonl").write_text(filler, encoding="utf-8")   # the fresh active file
        return s, s.log + ".codex.offset.dispatch.jsonl"

    handed, dispatch = rotated("handed")                    # handed over and taken; the line sits in slot 3
    _spool(handed, [row])
    _archive(dispatch + ".3", _dispatch_line(ago(39), [ev(K)]), ago(20))
    handed.started(ago(39))
    handed.user(ago(39), f"notice: {K}")
    handed.completed(ago(38))
    assert handed.judge()["verdict"] == "OK"
    untaken, dispatch = rotated("untaken")                  # handed over, never taken: the archive still names it
    _archive(dispatch + ".1", _dispatch_line(ago(45), [ev(K)]), ago(44))
    assert (untaken.judge()["verdict"], untaken.judge()["keys"]) == ("STUCK", [K])
    skipped, _ = rotated("skipped")                         # never handed over, but skipped as already read
    _spool(skipped, [{**row, "rx_at": iso(ago(200))}])
    _archive(skipped.log + ".codex.offset.skips.jsonl.2", json.dumps({"key": K, "reason": "already_read"}) + "\n",
             ago(150))
    assert skipped.judge()["verdict"] == "OK"
    stale, dispatch = rotated("stale")                      # an archive last written before the lookback is not read
    _archive(dispatch + ".5", _dispatch_line(ago(45), [ev(K)]), ago(60 * 48))
    assert stale.judge()["verdict"] == "OK"
    order, dispatch = rotated("order")                      # the oldest untaken hand-off is named, across files
    older = "msg/1793859540004-pxx-ffffff"
    _archive(dispatch + ".1", _dispatch_line(ago(90), [ev(older)]), ago(89))
    with open(dispatch, "a", encoding="utf-8") as f:
        f.write(_dispatch_line(ago(50), [ev(K)]))
    assert order.judge()["keys"] == [older]


def test_the_spool_fallback_reads_a_bounded_window_below_the_offset(tmp_path, monkeypatch):
    """pxx 4999b1: the spool fallback read the whole handed-over history on every run; now at most MAX_SPOOL_BYTES
    below the codex offset, from a whole line."""
    seat = Seat(tmp_path)
    seat.started(ago(120))
    seat.completed(ago(119))
    old = json.dumps({"event": "message", "key": "msg/1793839540000-pxx-aaaaaa", "role": "to", "requires_ack": True,
                      "rx_at": iso(ago(300))}) + "\n"
    filler = json.dumps({"event": "status", "state": "connected", "at": iso(ago(200))}) + "\n"
    recent = json.dumps({"event": "message", "key": K, "role": "to", "requires_ack": True,
                         "rx_at": iso(ago(70))}) + "\n"
    data = (old + filler + recent).encode("utf-8")
    Path(seat.log).write_bytes(data)
    Path(seat.log + ".codex.offset").write_bytes(str(len(data)).encode())
    monkeypatch.setattr(cw, "MAX_SPOOL_BYTES", len(recent) + len(filler) // 2)   # starts mid-filler
    out = seat.judge()
    assert (out["verdict"], out["keys"]) == ("STUCK", [K])   # the old row lies outside the window, unread


def test_a_turn_that_went_silent_after_the_take_is_not_answered_for_good(tmp_path):
    """GitHub Codex on #554, third round (pxx 3c91c4): a running turn exempted a taken notice with no age limit. A Codex
    that took a notice and died before answering leaves task_started as its last turn event and its rollout stops
    growing, and that read OK for good, so no episode ever opened. The exemption now lasts while the rollout is
    written within the wait, the bound BUSY uses."""
    def taken(name, written, then=None):
        s = Seat(tmp_path / name)
        s.dispatch(ago(190), [ev(K)])
        s.started(ago(189))
        s.user(ago(189), f"notice: {K}")
        if then:
            then(s)
        return s.judge(written=written)

    out = taken("dead", ago(180))                           # silent for three hours
    assert (out["verdict"], out["taken_at"], cw.EXIT[out["verdict"]]) == ("TAKEN_NOT_ANSWERED", ago(189), 2)
    assert taken("edge", ago(30))["verdict"] == "TAKEN_NOT_ANSWERED"       # silent for exactly the wait
    assert taken("working", ago(29))["verdict"] == "OK"     # still written inside the wait: at work on it
    # Silent while it waits on a person: that is what it waits on.
    out = taken("parked", ago(180), lambda s: s.parked(ago(188)))
    assert (out["verdict"], out["waiting_on"], out["taken_at"]) == ("PARKED", "exec_approval_request", ago(189))
    # Silent on a limit whose reset is still ahead: limited, not dead.
    assert taken("limited", ago(180), lambda s: s.limits(ago(188), 100, NOW + 3600))["verdict"] == "RATE_LIMITED"


def test_a_receipt_for_an_older_version_does_not_hide_a_newer_one(tmp_path):
    """GitHub Codex on #554, third round (pxx 3c91c4): skip receipts, handled receipts and hand-offs were matched by key
    alone, while the bank decides by version (delivery_bank Bank.collect): it skips a row the seat read at that version
    or later (fetched_skip), and a handled receipt or a dispatch token covers its exact version. A version 1 skip
    receipt hid a version 2 row recovered into the spool and never handed over: OK where the mail had stalled."""
    v2 = {"event": "message", "key": K, "version": 2, "role": "to", "requires_ack": True, "rx_at": iso(ago(40))}

    def seat_with(name, skips=(), handled=None, dispatched=None, dispatched_as="message"):
        s = Seat(tmp_path / name)
        s.started(ago(120))
        s.completed(ago(119))
        _spool(s, [v2])
        s.dispatch(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])   # the log exists
        if dispatched is not None:                          # handed over at that version, taken and answered
            s.dispatch(ago(39), [{**ev(K), "event": dispatched_as, "version": dispatched}])
            s.started(ago(39))
            s.user(ago(39), f"notice: {K}")
            s.completed(ago(38))
        if skips:
            Path(s.log + ".codex.offset.skips.jsonl").write_text(
                "".join(json.dumps({"key": K, "version": v, "reason": "already_read"}) + "\n" for v in skips),
                encoding="utf-8")
        if handled is not None:
            _receipts(s, tmp_path / name / "handled.json", [handled])
        return s.judge()["verdict"]

    assert seat_with("skip-1", skips=(1,)) == "DELIVERY_STALLED"
    assert seat_with("skip-2", skips=(2,)) == "OK"
    assert seat_with("skip-3", skips=(3,)) == "OK"          # read at a later version: the bank skips this one too
    assert seat_with("skip-1-2", skips=(1, 2)) == "OK"      # the highest version read counts, in either order
    assert seat_with("skip-2-1", skips=(2, 1)) == "OK"
    assert seat_with("handled-1", handled=_done(K, 1)) == "DELIVERY_STALLED"
    assert seat_with("handled-2", handled=_done(K, 2)) == "OK"
    assert seat_with("handled-3", handled=_done(K, 3)) == "DELIVERY_STALLED"     # its exact version only
    bare = {k: v for k, v in _done(K).items() if k != "version"}
    assert seat_with("handled-bare", handled=bare) == "DELIVERY_STALLED"         # no version: the bank skips the entry
    assert seat_with("dispatched-1", dispatched=1) == "DELIVERY_STALLED"
    assert seat_with("dispatched-2", dispatched=2) == "OK"
    assert seat_with("edit-2", dispatched=2, dispatched_as="edit") == "DELIVERY_STALLED"     # another token
    # With no dispatch log (an older postbox), the same rule. Past the codex offset, the version 2 row stalled; below
    # it, the row was handed over and is owed a take.
    for where, skip, expect in (("past", 1, "DELIVERY_STALLED"), ("past", 2, "OK"),
                                ("below", 1, "STUCK"), ("below", 2, "OK")):
        s = Seat(tmp_path / f"fallback-{where}-{skip}")
        s.started(ago(120))
        s.completed(ago(119))
        line = json.dumps({**v2, "rx_at": iso(ago(70))}) + "\n"
        Path(s.log).write_bytes(line.encode("utf-8"))
        Path(s.log + ".codex.offset").write_bytes(b"0" if where == "past" else str(len(line.encode("utf-8"))).encode())
        Path(s.log + ".codex.offset.skips.jsonl").write_text(
            json.dumps({"key": K, "version": skip, "reason": "already_read"}) + "\n", encoding="utf-8")
        out = s.judge()
        assert (out["verdict"], out["source"]) == (expect, "spool_offset"), (where, skip)


def test_a_version_reads_as_the_bank_reads_it():
    """delivery_bank.version: a line without a version predates versions and is version 1; anything but a positive int
    is no version at all, which no receipt covers."""
    assert [cw._version(d) for d in ({}, {"version": 2}, {"version": 0}, {"version": -1}, {"version": True},
                                      {"version": "2"}, {"version": 2.0})] == [1, 2, None, None, None, None, None]
    assert cw.passed_over({"log": "/nonexistent/seat.log"}, 0)("msg/x", None) is False


def test_only_the_taking_turn_answers_a_take(tmp_path):
    """GitHub Codex on #554, round 4 (pxx d3c949; deferred to this follow-up): any assistant message or completed turn
    after the take counted as its answer, so a later, unrelated turn closed a take whose own turn had aborted. Only
    the taking turn answers now, between the take and the next task_started, and a later turn that is still running
    says nothing about the take."""
    def seat_with(name, then, written=None):
        s = Seat(tmp_path / name)
        s.dispatch(ago(60), [ev(K)])
        s.started(ago(59))
        s.user(ago(59), f"notice: {K}")
        then(s)
        return s.judge(written=written)

    def aborted_then_another_turn(s):                       # Codex's reproduction
        s.aborted(ago(58))
        s.started(ago(50))
        s.user(ago(50), "an unrelated request")
        s.assistant(ago(49))
        s.completed(ago(48))

    out = seat_with("later-answer", aborted_then_another_turn)
    assert (out["verdict"], out["taken_at"]) == ("TAKEN_NOT_ANSWERED", ago(59))

    def aborted_then_running(s):
        s.aborted(ago(58))
        s.started(ago(50))
        s.user(ago(50), "an unrelated request")

    assert seat_with("later-running", aborted_then_running, written=ago(1))["verdict"] == "TAKEN_NOT_ANSWERED"

    def answered_in_its_turn(s):                            # later turns, even an aborted one, change nothing
        s.assistant(ago(58))
        s.completed(ago(57))
        s.started(ago(50))
        s.user(ago(50), "an unrelated request")
        s.aborted(ago(49))

    assert seat_with("own-answer", answered_in_its_turn)["verdict"] == "OK"

    def completed_without_a_message(s):                     # its own turn completing answers too
        s.completed(ago(58))
        s.started(ago(50))
        s.user(ago(50), "an unrelated request")
        s.aborted(ago(49))

    assert seat_with("own-complete", completed_without_a_message)["verdict"] == "OK"


def test_a_handled_entry_the_bank_does_not_trust_suppresses_nothing(tmp_path):
    """GitHub Codex on #554, round 4 (pxx d3c949; deferred to this follow-up): a handled entry with the row's key and
    version suppressed the alarm even when it was not authenticated terminal work, while the bank
    (delivery_bank.handled with skip_bad) leaves such a row to be delivered. The checker now reads the receipts with
    the bank's own reader."""
    k2 = "msg/1793859540005-pxx-abcdef"
    rows = [{"event": "message", "key": k, "role": "to", "requires_ack": True, "rx_at": iso(ago(40))} for k in (K, k2)]

    def verdict(name, entries, identity="seat"):
        s = Seat(tmp_path / name)
        s.started(ago(120))
        s.completed(ago(119))
        _spool(s, rows)
        s.dispatch(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])
        _receipts(s, tmp_path / name / "handled.json", entries, identity)
        out = s.judge()
        return out["verdict"], out.get("keys")

    assert verdict("trusted", [_done(K), _done(k2)]) == ("OK", None)
    for fault in ({"disposition": "in_progress"}, {"signature_verified": False}, {"status_verified": None}):
        # The bad entry suppresses nothing, and the good one beside it still does.
        assert verdict("bad-" + next(iter(fault)), [_done(K, **fault), _done(k2)]) == ("DELIVERY_STALLED", [K]), fault
    # A file the bank refuses (another identity's) suppresses nothing at all.
    assert verdict("other-identity", [_done(K), _done(k2)], identity="someone-else") == ("DELIVERY_STALLED", [K, k2])


def test_the_checker_starts_where_no_delivery_bank_sits_beside_it(tmp_path, monkeypatch):
    """pxx on #555: the HZ builder's postbox predates the delivery bank, so a copy of the checker there has no
    delivery_bank.py beside it. Imported at module level, the bank stopped the checker, and the alert caller with it,
    before either started. It is loaded on first use now, and where it is missing nothing is suppressed: a postbox
    without a bank has no receipts the bank trusts."""
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copyfile(ROOT / "tools" / "postbox" / "codex_wake_check.py", alone / "codex_wake_check.py")
    run = subprocess.run([sys.executable, "-I", str(alone / "codex_wake_check.py"), "--help"],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0 and "--config" in run.stdout, run.stderr[-400:]

    def no_bank():
        raise ImportError("No module named 'delivery_bank'")

    monkeypatch.setattr(cw, "_delivery_bank", no_bank)
    s = Seat(tmp_path / "seat")
    s.started(ago(120))
    s.completed(ago(119))
    _spool(s, [{"event": "message", "key": K, "role": "to", "requires_ack": True, "rx_at": iso(ago(40))}])
    s.dispatch(ago(300), [ev("msg/1793839540000-pxx-eeeeee", role="cc", ack=False)])
    _receipts(s, tmp_path / "seat" / "handled.json", [_done(K)])
    assert s.judge()["verdict"] == "DELIVERY_STALLED"
