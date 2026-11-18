"""Deterministic end-to-end regression and mutant proofs for Issue #58 (multipart notice turn amplification).

Verifies every Issue #58 checklist requirement and recurrence shape against single-turn `tools/postbox/delivery_bank.py`,
`tools/postbox/postbox.py` (`run_codex_delivery` + `CodexTurn`), `tools/postbox/wait_next.py`, and
`tools/meshy_node/node.py` (`note_read` / `note_fetched`):

1. Scenario A (2026-10-04 HZ `f3b36a` four-hour turn timeline):
   - Active Codex turn runs 3h 59m 31s (`14:45:21Z`..`18:44:52Z`) with periodic rollout activity and steer at `14:47:49Z`.
   - Three multipart archive v1 notices (`0953a4`, `8d31c7` +43s, `6d1b48` +73s) arrive within 1s of signing and are
     read/handled mid-turn (`<state>.read.json` via `meshy_node.node.note_read` and `handled_file` via `mark_handled`).
   - Interleaved unread peer request (v1) and mid-turn `version=2` edit of `0953a4` remain unhandled.
   - Zero `codex queue` calls fire during the ~4h active turn; after `task_complete` + settle, exactly ONE bounded
     batch fires carrying only the unread peer request and the `version=2` edit (all v1 read/handled notices omitted,
     each `already_read` skip logged once to `<offset>.skips.jsonl`, `handled_file` never auto-written for skips).
   - Pure 4-Oct control (all 3 read mid-turn, no unread work): 0 `codex queue` calls during or after the 4h turn.
   - Security boundary: unverified fetches in `<state>.fetched.jsonl` (`signature_verified=False` or
     `status_verified=False`) are excluded from `<state>.read.json` and never suppress Codex wakes.

2. Scenario B (2026-10-02 18-part multipart archive amplification across `wait_next.py` and `run_codex_delivery`):
   - 18 delayed multipart archive v1 rows with 15 completed parts in `handled_file`, interleaved unknown peer request,
     `version=2` edit of a completed part, `retention_gap` status error, `listener_exited` supervisor warning, held `cc`
     pre-pruning (`hold_cc_without_ack=True`), and untrusted `preview_unverified` / `body` payloads.
   - Crash/failure injection (`codex queue` exit 1, `atomic_json` `OSError` before `.bank.json` replace, compatibility
     `.offset` failure after `.bank.json` replace) loses zero unread work and never replays committed batches on restart.

3. Mutant / Guard-Removal Proofs (`M1`..`HZ`):
   - Proves each Scenario A/B assertion fails red when its corresponding guard in `delivery_bank.py` or `postbox.py`
     is disabled via `monkeypatch` (read-ledger skip, active-turn hold, exact-version `handled_file`, version-bounded
     `fetched_skip`, `handled_file` suppression, and bounded multi-event batching).
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_postbox_reconcile import ident, pb  # noqa: F401
from tests.test_postbox_delivery_bank import (  # noqa: F401
    PB,
    append,
    bank,
    copy_message,
    dispatch_lines,
    handled as handled_entry,
    mark_handled,
    postbox_os,
    receipt,
)
from tools.meshy_node import node as meshy_node

PEER_ID = "peer-codex-seat-0001"
T_OCT4 = datetime.datetime(2026, 9, 26, 13, 52, 0, tzinfo=datetime.timezone.utc)

K_PART1 = "msg/1790430743459-oxxxxxx-0953a4"
K_PART2 = "msg/1790430786538-oxxxxxx-8d31c7"
K_PART3 = "msg/1790430859148-oxxxxxx-6d1b48"
K_UNREAD = "msg/1790430820000-peer-unread"


def _z(base: datetime.datetime, offset_s: float) -> str:
    dt = base + datetime.timedelta(seconds=offset_s)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _spooled(
    key: str,
    offset_s: float,
    *,
    version: int = 1,
    event: str = "message",
    role: str = "to",
    base: datetime.datetime = T_OCT4,
    **extra,
) -> dict:
    rx = {} if event == "edit" else {"rx_at": _z(base, offset_s)}
    row = {
        "event": event,
        "role": role,
        "key": key,
        "version": version,
        "created_at": _z(base, offset_s - 0.8),
        "from": "oxxxxxx",
        "from_peer_id": "p-oxxxxxx-9999",
        **rx,
        **extra,
    }
    if event == "edit":
        row.setdefault("edited_by", "oxxxxxx")
        row.setdefault("edited_at", _z(base, offset_s - 0.2))
    return row


class ScriptedClock:
    def __init__(self, start_dt: datetime.datetime = T_OCT4):
        self.t = start_dt.timestamp()

    def __call__(self) -> float:
        return self.t

    def iso(self) -> str:
        return datetime.datetime.fromtimestamp(self.t, datetime.timezone.utc).isoformat()


def _write_rollout_event(i: dict, clock: ScriptedClock, *kinds: str, activity: bool = False) -> Path:
    # Keep one rollout file for the session across the 4h turn (started 2026-09-26T13:52:21Z)
    start_day = Path(i["codex_sessions_dir"]) / "2026" / "10" / "04"
    start_day.mkdir(parents=True, exist_ok=True)
    path = start_day / f"rollout-2026-10-04T14-45-21-{i['target']}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        if activity:
            f.write(
                json.dumps(
                    {
                        "timestamp": clock.iso(),
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant"},
                    }
                )
                + "\n"
            )
        for kind in kinds:
            f.write(
                json.dumps(
                    {
                        "timestamp": clock.iso(),
                        "type": "event_msg",
                        "payload": {"type": kind},
                    }
                )
                + "\n"
            )
    os.utime(path, (clock.t, clock.t))
    return path


def _make_codex_seat(tmp_path: Path, **kw) -> dict:
    i = ident(
        tmp_path,
        target="0199aefc-fixture",
        codex_sessions_dir=str(tmp_path / "sessions"),
        **kw,
    )
    Path(i["log"]).touch()
    Path(i["log"] + ".codex.offset").write_text("0", encoding="utf-8")
    Path(i["state"]).write_text(json.dumps({"peer_id": PEER_ID}), encoding="utf-8")
    return i


def _record_client_read(
    i: dict,
    key: str,
    version: int = 1,
    *,
    signature_verified: bool = True,
    status_verified: bool = True,
) -> tuple[bool, bool]:
    state = json.loads(Path(i["state"]).read_text(encoding="utf-8"))
    msg = {
        "key": key,
        "version": version,
        "thread_id": "hz-multipart-archive",
        "signature_verified": signature_verified,
        "status_verified": status_verified,
    }
    wrote_fetched = meshy_node.note_fetched(i["state"], msg)
    wrote_read = meshy_node.note_read(i["state"], state, msg)
    return wrote_fetched, wrote_read


def _skip_records(offset_file: str) -> list[dict]:
    path = Path(offset_file + bank.SKIPS_SUFFIX)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _StopLoop(Exception):
    pass


def _run_oct4_mixed_scenario(pb_mod, monkeypatch, root: Path) -> dict:
    """Execute the 4-Oct HZ f3b36a 4h active-turn timeline and assert all #58 invariants."""
    i = _make_codex_seat(root)
    receipt(i, [])
    clock = ScriptedClock(T_OCT4 + datetime.timedelta(seconds=21))  # 14:45:21Z
    _write_rollout_event(i, clock, "task_started")

    # Turn runs 14:45:21Z -> 18:44:52Z (14,371 seconds = 3h 59m 31s).
    turn_start_t = clock.t
    turn_end_t = (T_OCT4 + datetime.timedelta(hours=3, minutes=59, seconds=52)).timestamp()  # 18:44:52Z
    queued_calls: list[tuple[float, str]] = []
    mid_turn_skips_seen: list[tuple[float, int]] = []
    mid_turn_dispatches_seen: list[int] = []
    emitted: set[str] = set()
    last_heartbeat_t = clock.t
    off_file = i["log"] + ".codex.offset"

    def maybe_emit(tag: str, when_offset_s: float, fn) -> None:
        if tag not in emitted and clock.t >= (T_OCT4 + datetime.timedelta(seconds=when_offset_s)).timestamp():
            emitted.add(tag)
            fn()

    def sleep(seconds: float) -> None:
        nonlocal last_heartbeat_t
        # Fast-forward quiet middle of the 4h turn in 600s steps once all mid-turn events (<= 15:10:00Z) have fired,
        # while still exercising dozens of 2s polls around arrival, read/handled transitions, and turn completion.
        step = seconds
        if (
            seconds == 2
            and "edit_v2" in emitted
            and "turn_complete" not in emitted
            and clock.t + 600 < turn_end_t - 10
        ):
            step = 600.0
        clock.t += step

        # 14:45:23Z: part 1 arrives
        maybe_emit(
            "part1",
            23,
            lambda: append(
                i["log"],
                _spooled(K_PART1, 23, preview_unverified="untrusted part1 preview", body="SECRET-ARCHIVE-1"),
            ),
        )
        # 14:45:53Z: seat reads part 1 mid-turn via meshy_node
        maybe_emit("read1", 53, lambda: _record_client_read(i, K_PART1, 1))
        # 14:46:06Z (+43s after part 1): part 2 arrives
        maybe_emit(
            "part2",
            66,
            lambda: append(
                i["log"],
                _spooled(K_PART2, 66, preview_unverified="untrusted part2 preview", body="SECRET-ARCHIVE-2"),
            ),
        )
        # 14:46:40Z: interleaved actionable peer request arrives (never read mid-turn)
        maybe_emit(
            "unread_req",
            100,
            lambda: append(
                i["log"],
                _spooled(K_UNREAD, 100, **{"from": "bxxxxx", "from_peer_id": "p-bxxxxx-1111"}),
            ),
        )
        # 14:47:19Z (+73s after part 2): part 3 arrives
        maybe_emit(
            "part3",
            139,
            lambda: append(
                i["log"],
                _spooled(K_PART3, 139, preview_unverified="untrusted part3 preview", body="SECRET-ARCHIVE-3"),
            ),
        )
        # 14:47:31Z: seat reads part 2 via meshy_node, and marks part 3 done in handled_file
        maybe_emit("read2", 151, lambda: _record_client_read(i, K_PART2, 1))
        maybe_emit(
            "handle3",
            155,
            lambda: mark_handled.record(
                i,
                [{"key": K_PART3, "version": 1, "signature_verified": True, "status_verified": True}],
                "done",
            ),
        )
        # 14:47:49Z: operator steers active turn (rollout activity)
        maybe_emit("steer", 169, lambda: _write_rollout_event(i, clock, activity=True))
        # 15:10:00Z: sender edits K_PART1 to version=2 (edit rows carry no rx_at)
        maybe_emit(
            "edit_v2",
            25 * 60,
            lambda: append(i["log"], _spooled(K_PART1, 25 * 60, version=2, event="edit")),
        )

        # Active 4h turn keeps writing rollout output every ~20 minutes until 18:44:52Z
        if clock.t < turn_end_t and clock.t - last_heartbeat_t >= 1200:
            _write_rollout_event(i, clock, activity=True)
            last_heartbeat_t = clock.t

        if clock.t < turn_end_t:
            mid_turn_skips_seen.append((clock.t, len(_skip_records(off_file))))
            mid_turn_dispatches_seen.append(len(dispatch_lines(off_file)))

        if "turn_complete" not in emitted and clock.t >= turn_end_t:
            emitted.add("turn_complete")
            _write_rollout_event(i, clock, "task_complete")

        if clock.t > turn_end_t + pb_mod.CODEX_SETTLE_S * 2 + pb_mod.CODEX_MIN_GAP_S + 20:
            raise _StopLoop()

    def fake_run(argv, **_kw):
        queued_calls.append((clock.t, argv[-1]))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with monkeypatch.context() as m:
        m.setattr(pb_mod.time, "sleep", sleep)
        m.setattr(pb_mod.subprocess, "run", fake_run)
        m.setattr(pb_mod, "_codex_clock", clock)
        m.setattr(pb_mod, "graphline", None)
        m.setattr(pb_mod, "owedline", None)
        with pytest.raises(_StopLoop):
            pb_mod.run_codex_delivery(i)

    # 1. Zero codex queue calls during the entire ~4h active turn [14:45:21Z, 18:44:52Z]
    assert [t for t, _ in queued_calls if t < turn_end_t] == [], (
        "active turn must bank all incoming notices without calling codex queue mid-turn"
    )
    assert all(c == 0 for c in mid_turn_dispatches_seen)
    # K_PART1 is read at 14:45:53Z before K_UNREAD arrives (14:46:40Z), so its quiet skip commits once mid-turn;
    # K_PART2 is read at 14:47:31Z while K_UNREAD holds events != [], so its skip is NOT spammed across held polls
    # and commits only once after task_complete.
    read1_t = (T_OCT4 + datetime.timedelta(seconds=53)).timestamp()
    assert all(c == 0 for t, c in mid_turn_skips_seen if t < read1_t)
    assert all(c <= 1 for _, c in mid_turn_skips_seen), "held polls must not write duplicate skip lines before commit"

    # 2. Exactly ONE bounded batch after task_complete + settle_s
    assert len(queued_calls) == 1, f"expected 1 post-turn batch, got {len(queued_calls)}"
    q_time, notice = queued_calls[0]
    assert q_time >= turn_end_t + pb_mod.CODEX_SETTLE_S
    assert turn_end_t - turn_start_t == 14371.0

    # 3. Notice carries ONLY the unread peer request (v1) and the v2 edit of K_PART1; zero v1 multipart parts
    assert K_UNREAD in notice
    assert K_PART1 in notice and "version=2" in notice and "[RECALL]" in notice, (
        "version=2 edit of read part-1 must not be suppressed by v1 read ledger"
    )
    assert K_PART2 not in notice, "already-read part 2 (v1) must not wake Codex"
    assert K_PART3 not in notice, "handled part 3 (v1) must not wake Codex"
    assert "SECRET-ARCHIVE" not in notice and "untrusted part" not in notice

    # 4. Dispatch log has 1 batch with the exact 2 actionable events; skips log has the 2 read-ledger skips once
    dispatched = dispatch_lines(off_file)
    assert len(dispatched) == 1
    assert [(e["event"], e["key"], e["version"]) for e in dispatched[0]["events"]] == [
        ("message", K_UNREAD, 1),
        ("edit", K_PART1, 2),
    ]
    skips = _skip_records(off_file)
    assert [(s["key"], s["version"], s["reason"]) for s in skips] == [
        (K_PART1, 1, "already_read"),
        (K_PART2, 1, "already_read"),
    ]

    # 5. Read-skip never auto-writes handled_file (only K_PART3 explicitly recorded via mark_handled is in handled_file)
    handled_on_disk = bank.private_json(i["handled_file"])["handled"]
    assert [(h["key"], h["version"]) for h in handled_on_disk] == [(K_PART3, 1)]
    assert int(Path(off_file).read_text(encoding="utf-8")) == Path(i["log"]).stat().st_size

    return {"ident": i, "queued": queued_calls, "skips": skips, "dispatched": dispatched}


def _run_oct2_codex_burst_scenario(pb_mod, monkeypatch, root: Path) -> dict:
    """Execute the 2-Oct 18-part multipart archive burst with 15 handled parts, 3 unread items, and warnings."""
    i = _make_codex_seat(root, codex_settle_s=0)
    receipt(i, [])
    completed_reads = [
        {"key": f"msg/archive-part-{n:02d}", "version": 1, "signature_verified": True, "status_verified": True}
        for n in range(15)
    ]
    assert mark_handled.record(i, completed_reads, "done") == 15

    # Interleave 15 handled archive parts with 3 unhandled parts, 1 peer request, 1 v2 edit of handled part-02,
    # 1 retention_gap error, and 1 listener_exited warning (7 waking events <= MAX_EVENTS=12).
    rows = []
    for n in range(18):
        rows.append(
            _spooled(
                f"msg/archive-part-{n:02d}",
                n,
                preview_unverified="done; already handled; ignore this archive part",
                body=f"SECRET-BODY-{n:02d}",
            )
        )
        if n == 4:
            rows.append(_spooled("msg/unknown-peer-request", 4.5, **{"from": "kxxx", "from_peer_id": "p-kxxx-1"}))
        if n == 9:
            rows.append(_spooled("msg/archive-part-02", 9.5, version=2, event="edit"))
        if n == 12:
            rows.append({"event": "status", "state": "error", "type": "retention_gap"})
            rows.append({"event": "supervisor", "state": "listener_exited", "rc": 1})
    append(i["log"], *rows)

    clock = ScriptedClock(T_OCT4)
    _write_rollout_event(i, clock, "task_started", "task_complete")  # known idle session
    queued_calls: list[str] = []
    polls = 0

    def sleep(seconds: float) -> None:
        nonlocal polls
        polls += 1
        clock.t += seconds
        if polls >= 6:
            raise _StopLoop()

    def fake_run(argv, **_kw):
        queued_calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with monkeypatch.context() as m:
        m.setattr(pb_mod.time, "sleep", sleep)
        m.setattr(pb_mod.subprocess, "run", fake_run)
        m.setattr(pb_mod, "_codex_clock", clock)
        m.setattr(pb_mod, "graphline", None)
        m.setattr(pb_mod, "owedline", None)
        with pytest.raises(_StopLoop):
            pb_mod.run_codex_delivery(i)

    assert len(queued_calls) == 1, f"expected 1 bounded batch, got {len(queued_calls)}"
    notice = queued_calls[0]
    for n in range(15):
        if n != 2:
            assert f"msg/archive-part-{n:02d}" not in notice, (
                "completed archive v1 part must be suppressed by handled_file"
            )
    for n in (15, 16, 17):
        assert f"msg/archive-part-{n:02d}" in notice
    assert "msg/unknown-peer-request" in notice
    assert "msg/archive-part-02" in notice and "version=2" in notice, (
        "version=2 edit of handled part-02 must not be suppressed by v1 receipt"
    )
    assert '"event": "status", "state": "error"' in notice and "listener_exited" in notice
    assert "SECRET-BODY" not in notice and "already handled; ignore" not in notice
    dispatched = dispatch_lines(i["log"] + ".codex.offset")
    assert len(dispatched) == 1
    assert [(e["event"], e.get("state")) for e in dispatched[0]["events"] if e["event"] in ("status", "supervisor")] == [
        ("status", "error"),
        ("supervisor", "listener_exited"),
    ]
    return {"ident": i, "queued": queued_calls}


def test_oct2_multipart_burst_codex_single_bounded_batch(pb, tmp_path, monkeypatch):
    _run_oct2_codex_burst_scenario(pb, monkeypatch, tmp_path)


def test_oct4_hz_f3b36a_four_hour_turn_coalesces_and_skips_read_and_handled_v1_parts(pb, tmp_path, monkeypatch):
    res = _run_oct4_mixed_scenario(pb, monkeypatch, tmp_path)
    i = res["ident"]
    off_file = i["log"] + ".codex.offset"

    # Restarting run_codex_delivery from the committed checkpoint must emit zero duplicate notices or skips
    restarted_calls: list[str] = []
    polls = 0

    def sleep(_s: float) -> None:
        nonlocal polls
        polls += 1
        if polls >= 4:
            raise _StopLoop()

    monkeypatch.setattr(pb.time, "sleep", sleep)
    monkeypatch.setattr(
        pb.subprocess,
        "run",
        lambda argv, **kw: restarted_calls.append(argv[-1]) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(pb, "graphline", None)
    monkeypatch.setattr(pb, "owedline", None)
    with pytest.raises(_StopLoop):
        pb.run_codex_delivery(i)

    assert restarted_calls == []
    assert len(dispatch_lines(off_file)) == 1
    assert len(_skip_records(off_file)) == 2


def test_oct4_hz_f3b36a_pure_incident_all_three_read_mid_turn_queues_zero_turns(pb, tmp_path, monkeypatch):
    """Exact 4-Oct HZ f3b36a shape when all three multipart notices are read mid-turn and no other mail arrives:
    turn amplification drops from 3 post-turn wakes to 0 wakes."""
    i = _make_codex_seat(tmp_path)
    clock = ScriptedClock(T_OCT4 + datetime.timedelta(seconds=21))
    _write_rollout_event(i, clock, "task_started")
    turn_end_t = (T_OCT4 + datetime.timedelta(hours=3, minutes=59, seconds=52)).timestamp()

    emitted: set[str] = set()
    last_hb = clock.t
    queued: list[str] = []

    def emit_at(tag: str, sec: float, fn) -> None:
        if tag not in emitted and clock.t >= (T_OCT4 + datetime.timedelta(seconds=sec)).timestamp():
            emitted.add(tag)
            fn()

    def sleep(seconds: float) -> None:
        nonlocal last_hb
        step = 600.0 if (seconds == 2 and "read3" in emitted and clock.t + 600 < turn_end_t - 10) else seconds
        clock.t += step
        emit_at("p1", 23, lambda: append(i["log"], _spooled(K_PART1, 23)))
        emit_at("r1", 53, lambda: _record_client_read(i, K_PART1, 1))
        emit_at("p2", 66, lambda: append(i["log"], _spooled(K_PART2, 66)))
        emit_at("p3", 139, lambda: append(i["log"], _spooled(K_PART3, 139)))
        emit_at("r2", 151, lambda: _record_client_read(i, K_PART2, 1))
        emit_at("read3", 190, lambda: _record_client_read(i, K_PART3, 1))

        if clock.t < turn_end_t and clock.t - last_hb >= 1200:
            _write_rollout_event(i, clock, activity=True)
            last_hb = clock.t
        if "done" not in emitted and clock.t >= turn_end_t:
            emitted.add("done")
            _write_rollout_event(i, clock, "task_complete")
        if clock.t >= turn_end_t + 120:
            raise _StopLoop()

    monkeypatch.setattr(pb.time, "sleep", sleep)
    monkeypatch.setattr(
        pb.subprocess,
        "run",
        lambda argv, **kw: queued.append(argv[-1]) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(pb, "_codex_clock", clock)
    monkeypatch.setattr(pb, "graphline", None)
    monkeypatch.setattr(pb, "owedline", None)

    with pytest.raises(_StopLoop):
        pb.run_codex_delivery(i)

    assert queued == [], "all three multipart notices read mid-turn must produce zero Codex wakes"
    off_file = i["log"] + ".codex.offset"
    assert int(Path(off_file).read_text(encoding="utf-8")) == Path(i["log"]).stat().st_size
    assert dispatch_lines(off_file) == []
    skips = _skip_records(off_file)
    assert [(s["key"], s["version"], s["reason"]) for s in skips] == [
        (K_PART1, 1, "already_read"),
        (K_PART2, 1, "already_read"),
        (K_PART3, 1, "already_read"),
    ]
    st = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[i["name"]]
    assert st["codex_wake_skips"] == 3
    assert st.get("codex_hold") is None


def test_unverified_fetched_sidecar_never_suppresses_codex_wake(pb, tmp_path, monkeypatch):
    """`meshy_node.node.note_fetched` appends to `<state>.fetched.jsonl` even when signature/status verification fails
    (for unread-alert suppression only), whereas `note_read` writes `<state>.read.json` ONLY when both
    `signature_verified` and `status_verified` are True. Verify unverified fetches never suppress Codex wakes."""
    i = _make_codex_seat(tmp_path, codex_settle_s=0)
    fetched_sidecar = Path(i["state"] + meshy_node.FETCHED_SUFFIX)
    fetched_sidecar.write_text("", encoding="utf-8")

    assert _record_client_read(i, "msg/bad-sig", 1, signature_verified=False, status_verified=False) == (True, False)
    assert _record_client_read(i, "msg/bad-status", 1, signature_verified=True, status_verified=False) == (True, False)
    assert _record_client_read(i, "msg/verified-read", 1, signature_verified=True, status_verified=True) == (True, True)

    sidecar_keys = [json.loads(ln)["key"] for ln in fetched_sidecar.read_text(encoding="utf-8").splitlines()]
    assert sidecar_keys == ["msg/bad-sig", "msg/bad-status", "msg/verified-read"]

    append(
        i["log"],
        _spooled("msg/bad-sig", 1),
        _spooled("msg/bad-status", 2),
        _spooled("msg/verified-read", 3),
    )

    queued: list[str] = []

    def sleep(_s: float) -> None:
        if queued:
            raise _StopLoop()

    monkeypatch.setattr(pb.time, "sleep", sleep)
    monkeypatch.setattr(
        pb.subprocess,
        "run",
        lambda argv, **kw: queued.append(argv[-1]) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(pb, "graphline", None)
    monkeypatch.setattr(pb, "owedline", None)
    with pytest.raises(_StopLoop):
        pb.run_codex_delivery(i)

    assert len(queued) == 1
    assert "msg/bad-sig" in queued[0]
    assert "msg/bad-status" in queued[0]
    assert "msg/verified-read" not in queued[0]
    skips = _skip_records(i["log"] + ".codex.offset")
    assert [s["key"] for s in skips] == ["msg/verified-read"]


def test_oct2_multipart_amplification_wait_next_single_batch_crash_and_restart(tmp_path, monkeypatch, capsys):
    """Scenario B on `wait_next.py`:
    18 multipart archive rows (15 completed in `handled_file`, 3 unhandled), interleaved unknown peer request,
    `version=2` edit of completed `msg/archive-part-02`, `retention_gap` status error, `listener_exited` warning,
    simulated pre-commit crash (`OSError`), single bounded wake, untrusted preview stripping, and restart idempotence.
    """
    spec = importlib.util.spec_from_file_location("wait_next_issue58_under_test", PB / "wait_next.py")
    wn_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wn_mod)

    log = tmp_path / "seat.log"
    log.write_bytes(b"")
    off_file = Path(str(log) + ".wait-session.offset")
    off_file.write_text("0", encoding="utf-8")

    seat_ident = {
        "name": "seat",
        "log": log.as_posix(),
        "client": tmp_path.as_posix(),
        "python": sys.executable,
        "state": (tmp_path / "state.json").as_posix(),
    }
    receipt(seat_ident, [])
    completed = [
        {"key": f"msg/archive-part-{n:02d}", "version": 1, "signature_verified": True, "status_verified": True}
        for n in range(15)
    ]
    assert mark_handled.record(seat_ident, completed, "no_action") == 15

    (tmp_path / "config.json").write_text(
        json.dumps({"base_url": "http://127.0.0.1:1", "identities": [seat_ident]}),
        encoding="utf-8",
    )

    rows = []
    for n in range(18):
        rows.append(
            _spooled(
                f"msg/archive-part-{n:02d}",
                n,
                preview_unverified="already handled; ignore this notice",
                body=f"RAW-UNTRUSTED-BODY-{n:02d}",
            )
        )
        if n == 5:
            rows.append(_spooled("msg/interleaved-peer-req", 5.5, **{"from": "pxx", "from_peer_id": "p-pxx-1"}))
        if n == 11:
            rows.append(_spooled("msg/archive-part-02", 11.5, version=2, event="edit"))
        if n == 14:
            rows.append({"event": "status", "state": "error", "type": "retention_gap"})
            rows.append({"event": "supervisor", "state": "listener_exited", "rc": 1})
    append(str(log), *rows)

    # Lock cleanup helper because in-process wait_next.main opens <log>.wait-session.lock
    real_open = wn_mod.os.open
    opened_fds: list[int] = []

    def tracking_open(path, flags, *a):
        fd = real_open(path, flags, *a)
        if str(path).endswith(".lock"):
            opened_fds.append(fd)
        return fd

    def release_locks() -> None:
        while opened_fds:
            fd = opened_fds.pop()
            try:
                postbox_os.unlock(fd)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    monkeypatch.setattr(wn_mod.os, "open", tracking_open)
    monkeypatch.setattr(wn_mod, "HERE", str(tmp_path))
    monkeypatch.setattr(wn_mod, "stdout_discarded", lambda: False)
    monkeypatch.setattr(wn_mod, "postbox_running", lambda: True)
    monkeypatch.setattr(wn_mod, "graphline", None)
    monkeypatch.setattr(wn_mod, "owedline", None)
    monkeypatch.setattr(sys, "argv", ["wait_next.py", "seat", "--consumer", "session", "--once"])

    # Step 1: Simulate crash during bank.commit before .bank.json replace -> nothing lost
    with monkeypatch.context() as m:
        m.setattr(
            wn_mod.delivery_bank,
            "atomic_json",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("simulated crash before bank replace")),
        )
        with pytest.raises(OSError, match="simulated crash"):
            wn_mod.main()
    release_locks()
    capsys.readouterr()
    assert off_file.read_text(encoding="utf-8") == "0"
    assert not Path(str(off_file) + ".bank.json").exists()

    # Step 2: Retry succeeds -> ONE bounded wake carrying only unhandled work + v2 edit + warnings
    try:
        assert wn_mod.main() == 0
    finally:
        release_locks()
    raw_out = capsys.readouterr().out
    assert "RAW-UNTRUSTED-BODY" not in raw_out and "already handled; ignore" not in raw_out
    events = [json.loads(ln) for ln in raw_out.splitlines() if ln.startswith("{")]

    delivered_msgs = [(e["event"], e.get("key"), e.get("version")) for e in events if e["event"] in ("message", "edit")]
    assert delivered_msgs == [
        ("message", "msg/interleaved-peer-req", 1),
        ("edit", "msg/archive-part-02", 2),
        ("message", "msg/archive-part-15", 1),
        ("message", "msg/archive-part-16", 1),
        ("message", "msg/archive-part-17", 1),
    ]
    assert [e.get("type") for e in events if e["event"] == "status"] == ["retention_gap"]
    assert [e.get("state") for e in events if e["event"] == "supervisor"] == ["listener_exited"]
    assert [e["key"] for e in events if e["event"] == "read_with"] == [k for _, k, _ in delivered_msgs]
    woke_tail = [e for e in events if e["event"] == "waiter"]
    assert len(woke_tail) == 1 and woke_tail[0]["state"] == "woke" and woke_tail[0]["count"] == 5

    # Step 3: Re-arm / restart after commit -> zero duplicate wakes
    try:
        assert wn_mod.main() == 0
    finally:
        release_locks()
    again = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    assert [e for e in again if e["event"] in ("message", "edit", "status", "supervisor")] == []
    assert [e["state"] for e in again if e["event"] == "waiter"] == ["empty"]


def test_oct2_multipart_amplification_codex_delivery_queue_failure_crash_and_cc_preprune(pb, tmp_path, monkeypatch):
    """Scenario B on `run_codex_delivery`:
    - `hold_cc_without_ack=True`: 3 held `cc` copies (`msg/cc-handled` later handled, `msg/cc-read` later read,
      `msg/cc-keep` unread) are pre-pruned so only `msg/cc-keep` releases with the batch.
    - 15 completed multipart archive v1 parts (`handled_file`) + 2 read v1 parts (`<state>.read.json`) are suppressed,
      while unhandled `msg/archive-part-17`, `msg/interleaved-req`, `version=2` edit of `msg/archive-part-02`,
      `retention_gap`, and `listener_exited` coalesce into ONE bounded batch.
    - `codex queue` exit 1, `.bank.json` `OSError` crash before commit, and compatibility `.codex.offset` write failure
      lose zero unread work and never replay committed batches on restart.
    """
    i = _make_codex_seat(tmp_path, codex_settle_s=0, hold_cc_without_ack=True)
    receipt(i, [])
    off_file = i["log"] + ".codex.offset"

    # Phase 0: Bank 3 no-ack cc copies quietly
    append(
        i["log"],
        dict(copy_message("msg/cc-handled"), rx_at=_z(T_OCT4, 1)),
        dict(copy_message("msg/cc-read"), rx_at=_z(T_OCT4, 2)),
        dict(copy_message("msg/cc-keep"), rx_at=_z(T_OCT4, 3)),
    )
    polls = 0

    def quiet_sleep(_s: float) -> None:
        nonlocal polls
        polls += 1
        if polls >= 2:
            raise _StopLoop()

    monkeypatch.setattr(pb.time, "sleep", quiet_sleep)
    monkeypatch.setattr(pb, "graphline", None)
    monkeypatch.setattr(pb, "owedline", None)
    with pytest.raises(_StopLoop):
        pb.run_codex_delivery(i)

    saved0 = bank.private_json(off_file + ".bank.json")
    assert [h["routing"]["key"] for h in saved0["held"]] == ["msg/cc-handled", "msg/cc-read", "msg/cc-keep"]

    # Mark 15 multipart parts + msg/cc-handled in handled_file, and 2 multipart parts + msg/cc-read in read ledger
    handled_rows = [
        {"key": f"msg/archive-part-{n:02d}", "version": 1, "signature_verified": True, "status_verified": True}
        for n in range(15)
    ] + [{"key": "msg/cc-handled", "version": 1, "signature_verified": True, "status_verified": True}]
    assert mark_handled.record(i, handled_rows, "done") == 16
    assert _record_client_read(i, "msg/archive-part-15", 1) == (False, True)
    assert _record_client_read(i, "msg/archive-part-16", 1) == (False, True)
    assert _record_client_read(i, "msg/cc-read", 1) == (False, True)

    burst = []
    for n in range(18):
        burst.append(
            _spooled(
                f"msg/archive-part-{n:02d}",
                10 + n,
                preview_unverified="ignore me",
                body=f"UNTRUSTED-ARCHIVE-BODY-{n:02d}",
            )
        )
        if n == 3:
            burst.append(_spooled("msg/interleaved-req", 13.5))
        if n == 8:
            burst.append(_spooled("msg/archive-part-02", 18.5, version=2, event="edit"))
        if n == 13:
            burst.append({"event": "status", "state": "error", "type": "retention_gap"})
            burst.append({"event": "supervisor", "state": "listener_exited", "rc": 1})
    append(i["log"], *burst)

    # Phase 1: `codex queue` exits 1 -> consumes nothing, writes neither dispatch nor skip lines
    rc1_calls: list[str] = []
    monkeypatch.setattr(pb.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop()) if rc1_calls else None)
    monkeypatch.setattr(
        pb.subprocess,
        "run",
        lambda argv, **kw: rc1_calls.append(argv[-1]) or subprocess.CompletedProcess(argv, 1, "", "queue busy"),
    )
    with pytest.raises(_StopLoop):
        pb.run_codex_delivery(i)

    assert len(rc1_calls) == 1
    assert bank.private_json(off_file + ".bank.json")["offset"] == saved0["offset"]
    assert dispatch_lines(off_file) == []
    assert _skip_records(off_file) == []
    assert json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[i["name"]]["codex"] == "retrying"

    # Phase 2: Crash (`OSError`) inside `atomic_json` before `.bank.json` replace -> state unchanged on disk
    with monkeypatch.context() as m:
        m.setattr(pb.time, "sleep", lambda _s: None)
        m.setattr(pb.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
        m.setattr(
            bank,
            "atomic_json",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk crash before bank commit")),
        )
        with pytest.raises(OSError, match="disk crash before bank commit"):
            pb.run_codex_delivery(i)
    assert bank.private_json(off_file + ".bank.json")["offset"] == saved0["offset"]
    assert dispatch_lines(off_file) == []

    # Phase 3: Retry with `codex queue` rc=0, but simulate compatibility `.codex.offset` failure AFTER `.bank.json`
    real_atomic_json = bank.atomic_json
    ok_calls: list[str] = []

    def fail_compat_offset_only(path, value):
        if path == off_file:
            raise OSError("compatibility offset write failed after bank commit")
        return real_atomic_json(path, value)

    with monkeypatch.context() as m:
        m.setattr(pb.time, "sleep", lambda _s: None)
        m.setattr(
            pb.subprocess,
            "run",
            lambda argv, **kw: ok_calls.append(argv[-1]) or subprocess.CompletedProcess(argv, 0, "", ""),
        )
        m.setattr(bank, "atomic_json", fail_compat_offset_only)
        with pytest.raises(OSError, match="compatibility offset write failed"):
            pb.run_codex_delivery(i)

    assert len(ok_calls) == 1
    notice = ok_calls[0]
    assert "msg/cc-keep" in notice
    assert "msg/cc-handled" not in notice and "msg/cc-read" not in notice
    for n in range(17):
        if n != 2:
            assert f"msg/archive-part-{n:02d}" not in notice
    assert "msg/archive-part-17" in notice
    assert "msg/interleaved-req" in notice
    assert "msg/archive-part-02" in notice and "version=2" in notice
    assert '"event": "status", "state": "error"' in notice and "listener_exited" in notice
    assert "UNTRUSTED-ARCHIVE-BODY" not in notice and "ignore me" not in notice

    # Phase 4: Restart after compatibility-offset failure -> `.bank.json` wins, ZERO replay of committed batch
    replay_calls: list[str] = []
    polls = 0

    def post_restart_sleep(_s: float) -> None:
        nonlocal polls
        polls += 1
        if polls >= 3:
            raise _StopLoop()

    monkeypatch.setattr(pb.time, "sleep", post_restart_sleep)
    monkeypatch.setattr(
        pb.subprocess,
        "run",
        lambda argv, **kw: replay_calls.append(argv[-1]) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    with pytest.raises(_StopLoop):
        pb.run_codex_delivery(i)

    assert replay_calls == [], "committed .bank.json checkpoint must prevent duplicate replay on restart"
    dispatched = dispatch_lines(off_file)
    assert len(dispatched) == 1
    assert [(e["event"], e.get("key"), e.get("version")) for e in dispatched[0]["events"]] == [
        ("message", "msg/cc-keep", 1),
        ("message", "msg/interleaved-req", 1),
        ("edit", "msg/archive-part-02", 2),
        ("status", None, None),
        ("supervisor", None, None),
        ("message", "msg/archive-part-17", 1),
    ]
    assert [(e["event"], e.get("state")) for e in dispatched[0]["events"] if e["event"] in ("status", "supervisor")] == [
        ("status", "error"),
        ("supervisor", "listener_exited"),
    ]
    skips = _skip_records(off_file)
    assert sorted((s["key"], s["version"]) for s in skips) == [
        ("msg/archive-part-15", 1),
        ("msg/archive-part-16", 1),
        ("msg/cc-read", 1),
    ]


@pytest.mark.parametrize(
    "mutant,expected_match",
    [
        ("M1_disable_read_ledger_skip", r"already-read part 2 \(v1\) must not wake Codex"),
        ("M2_disable_active_turn_hold", r"active turn must bank all incoming notices without calling codex queue mid-turn"),
        ("M3_bare_key_handled_suppression", r"version=2 edit of handled part-02 must not be suppressed by v1 receipt"),
        ("M4_version_insensitive_read_ledger", r"version=2 edit of read part-1 must not be suppressed by v1 read ledger"),
        ("M5_disable_handled_receipt_suppression", r"completed archive v1 part must be suppressed by handled_file"),
        ("HZ_unbounded_per_notice_turn_amplification", r"expected 1 post-turn batch, got 2"),
    ],
)
def test_issue58_guards_fail_red_under_in_memory_mutants(pb, tmp_path, monkeypatch, mutant, expected_match):
    """Self-verifying mutant proof: each #58 guard in `delivery_bank.py` / `postbox.py` is disabled in memory via
    `monkeypatch`, and the corresponding Scenario A or Scenario B harness MUST fail with the exact expected
    `AssertionError`."""
    sub = tmp_path / mutant
    sub.mkdir()

    real_handled = bank.handled

    with monkeypatch.context() as m:
        if mutant == "M1_disable_read_ledger_skip":
            m.setattr(bank, "fetched_skip", lambda *a, **kw: None)
            runner = lambda: _run_oct4_mixed_scenario(pb, m, sub)
        elif mutant == "M2_disable_active_turn_hold":
            m.setattr(pb.CodexTurn, "update", lambda self, clock=None: (None, None))
            runner = lambda: _run_oct4_mixed_scenario(pb, m, sub)
        elif mutant == "M3_bare_key_handled_suppression":
            # Issue #58's explicitly documented prototype bug: suppressing bare `key` across versions hides v2 edits
            def bare_key_handled(ident_obj, *a, **kw):
                pairs = real_handled(ident_obj, *a, **kw)
                return {(k, v) for k, _ in pairs for v in (1, 2, 3)}

            m.setattr(bank, "handled", bare_key_handled)
            runner = lambda: _run_oct2_codex_burst_scenario(pb, m, sub)
        elif mutant == "M4_version_insensitive_read_ledger":
            # Treating an older v1 read in <state>.read.json as covering a newer v2 edit
            def any_version_fetched_skip(d, rx_at, ledger_map):
                v = bank.version(d)
                if d.get("event") not in ("message", "edit") or not isinstance(d.get("key"), str) or v is None:
                    return None
                reads = ledger_map.get(d["key"], ())
                if not reads:
                    return None
                return {
                    "key": d["key"],
                    "version": v,
                    "reason": bank.SKIP_FETCHED,
                    "spooled_at": rx_at if isinstance(rx_at, str) else None,
                    "fetched_at": reads[0][2],
                }

            m.setattr(bank, "fetched_skip", any_version_fetched_skip)
            runner = lambda: _run_oct4_mixed_scenario(pb, m, sub)
        elif mutant == "M5_disable_handled_receipt_suppression":
            m.setattr(bank, "handled", lambda *a, **kw: set())
            runner = lambda: _run_oct2_codex_burst_scenario(pb, m, sub)
        elif mutant == "HZ_unbounded_per_notice_turn_amplification":
            m.setattr(bank, "MAX_EVENTS", 1)
            runner = lambda: _run_oct4_mixed_scenario(pb, m, sub)
        else:  # pragma: no cover
            raise AssertionError(f"unknown mutant {mutant}")

        with pytest.raises(AssertionError, match=expected_match):
            runner()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
