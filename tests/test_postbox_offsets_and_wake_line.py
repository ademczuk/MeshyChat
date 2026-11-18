"""ASDLC OI-76 P7 and P4.

P7: the three consumers (the Claude channel, the Codex queue, the waiter) read their saved numeric offset through ONE
helper, `delivery_bank.legacy_offset`. Before it, the waiter treated a damaged file as 'from now' while the channel and
the Codex queue raised ValueError and crash-looped. Since kxxx's ruling on #78 (2026-10-03) an ABSENT file is 'from
now' and a DAMAGED one replays from the start, so mail queued behind a lost position is not skipped. A bank
checkpoint, when one exists, still overrides the legacy copy.

P4: the waiter's LAST line names what woke it, because a harness that shows only the tail of a finished task
(Claude's task notice, Kimi's background wake) otherwise shows instructions and no keys.

Run:  .venv/Scripts/python.exe -m pytest tests/test_postbox_offsets_and_wake_line.py -q -p no:cacheprovider --noconftest
Mutants (each must fail a test here):
  O1 legacy_offset accepts a negative number      caught by test_one_reading_of_a_saved_offset
  O2 the channel parses the file itself again     caught by test_a_damaged_offset_replays_the_channel_from_the_start
  O3 the Codex queue parses the file itself again caught by test_a_damaged_offset_replays_the_codex_queue_from_the_start
  O5 a damaged offset is 'from now' again         caught by test_one_reading_of_a_saved_offset and both replay tests
  O6 a damaged offset replays the whole log       caught by test_a_damaged_offset_replays_only_the_last_day
  O7 a saved 0 is capped as if damaged            caught by test_a_saved_zero_is_never_moved
  O10 created_at judged before the receipt        caught by test_a_row_received_late_counts_by_its_receipt
  O11 an oversized line ends the scan             caught by test_an_oversized_line_does_not_end_the_scan
  O12 the oversized test ignores a line's newline caught by test_the_oversized_boundary_is_the_spool_readers
  O13 a line of exactly MAX_LINE bytes is skipped caught by test_the_oversized_boundary_is_the_spool_readers
  O4 the woke line drops the keys                 caught by test_the_last_line_names_the_keys_that_woke_it
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from tests.test_postbox_reconcile import pb, ident  # noqa: F401
from tests.test_postbox_wait_next_once import wn  # noqa: F401
from tests.test_postbox_delivery_bank import append, message, PB

import delivery_bank as bank  # noqa: E402  (tests.test_postbox_delivery_bank put tools/postbox on the path)


@pytest.mark.parametrize("text, expected", [("12", 12), ("12\n", 12), (" 0 ", 0), ("", 0), ("not a number", 0),
                                            ("-3", 0), ("1.5", 0)])
def test_one_reading_of_a_saved_offset(tmp_path, text, expected):
    f = tmp_path / "x.offset"
    f.write_text(text)
    assert bank.legacy_offset(str(f)) == expected


def test_an_absent_offset_is_none(tmp_path):
    assert bank.legacy_offset(str(tmp_path / "missing.offset")) is None


@pytest.mark.parametrize("raw", [b"\xff\xfe\x81 garbage", b"\x81", "\u0661\u0662".encode("utf-8")])
def test_an_offset_holding_bytes_no_codec_reads_is_damaged_not_an_error(tmp_path, raw):
    """Codex and kxxx on #83: the file was read as text, so b"\\xff\\xfe\\x81 garbage" raised UnicodeDecodeError out of
    legacy_offset and stopped delivery, where main returned None. Non-ASCII digits are not an offset either.

    MUTANT: read the file in text mode again; the first case raises."""
    f = tmp_path / "x.offset"
    f.write_bytes(raw)
    assert bank.legacy_offset(str(f)) == 0


def test_an_offset_that_exists_but_cannot_be_read_is_damaged(tmp_path):
    (tmp_path / "x.offset").mkdir()                     # exists, and open() refuses it
    assert bank.legacy_offset(str(tmp_path / "x.offset")) == 0


NOW_S = 1791100000.0                                   # 2026-10-04T08:26:40Z


def _stamp(seconds_ago):
    return _dt.datetime.fromtimestamp(NOW_S - seconds_ago, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_damaged_offset_replays_only_the_last_day(tmp_path):
    """kxxx, 2026-10-04: a consumer with no bank replayed the WHOLE log after a damaged offset. It now starts at the
    first line stamped within 24 h. Every stamp spelling counts, and an unstamped line is passed over.

    MUTANT: drop the age test; the replay starts at the two-day-old line."""
    log, off = tmp_path / "x.log", tmp_path / "x.offset"
    append(str(log), message("msg/old", created_at=_stamp(2 * 86400)), {"event": "status", "at": _stamp(30 * 3600)},
           message("msg/unstamped"))
    start = log.stat().st_size
    append(str(log), message("msg/recent", rx_at=_stamp(3600)), message("msg/newer", created_at=_stamp(60)))
    off.write_text("garbage")
    assert bank.legacy_offset(str(off), str(log), NOW_S) == start
    assert bank.legacy_offset(str(off)) == 0                                     # no log to look at: from 0


def test_a_row_received_late_counts_by_its_receipt(tmp_path):
    """Codex and kxxx on #84: a row created two days ago but received a minute ago (a catch-up or a backfill) was
    judged by its created_at, so the replay started past it and it was never delivered. The receipt comes first.

    MUTANT: put created_at before rx_at again; the replay starts after the late row."""
    log, off = tmp_path / "x.log", tmp_path / "x.offset"
    append(str(log), message("msg/old", created_at=_stamp(3 * 86400), rx_at=_stamp(3 * 86400)))
    start = log.stat().st_size
    append(str(log), message("msg/late", created_at=_stamp(2 * 86400), rx_at=_stamp(60)))
    off.write_text("garbage")
    assert bank.legacy_offset(str(off), str(log), NOW_S) == start


def test_an_oversized_line_does_not_end_the_scan(tmp_path, monkeypatch):
    """kxxx on #84: a line longer than MAX_LINE ended the scan, so the replay started there or at 0. It is skipped.

    MUTANT: stop at an oversized line again; the replay starts at 0."""
    monkeypatch.setattr(bank, "MAX_LINE", 200)
    log, off = tmp_path / "x.log", tmp_path / "x.offset"
    append(str(log), message("msg/old", created_at=_stamp(3 * 86400)), {"event": "noise", "pad": "x" * 500},
           message("msg/old2", created_at=_stamp(2 * 86400)))
    start = log.stat().st_size
    append(str(log), message("msg/recent", rx_at=_stamp(60)))
    off.write_text("garbage")
    assert bank.legacy_offset(str(off), str(log), NOW_S) == start


def _sized(size, **fields):
    """One JSON line of exactly `size` bytes, its newline included."""
    line = json.dumps({**fields, "pad": ""}).encode() + b"\n"
    line = json.dumps({**fields, "pad": "x" * (size - len(line))}).encode() + b"\n"
    assert len(line) == size
    return line


@pytest.mark.parametrize("size, skipped", [(201, True), (200, False)])
def test_the_oversized_boundary_is_the_spool_readers(tmp_path, monkeypatch, size, skipped):
    """Codex round 2 and kxxx on #84: a line of exactly MAX_LINE + 1 bytes WITH its newline passed the oversized
    test, since it ends in a newline, so a replay could start on a line Bank.collect then refuses ('spool line size
    limit'). replay_start now uses collect's rule, len(raw) > MAX_LINE with the newline counted. A line of exactly
    MAX_LINE bytes is still a line and can be the start.

    MUTANTS: test the newline again (the 201-byte line becomes the start); use >= (the 200-byte line is skipped)."""
    monkeypatch.setattr(bank, "MAX_LINE", 200)
    log, off = tmp_path / "x.log", tmp_path / "x.offset"
    append(str(log), message("msg/old", created_at=_stamp(3 * 86400)))
    edge = log.stat().st_size
    with open(log, "ab") as f:
        f.write(_sized(size, event="message", key="msg/edge", rx_at=_stamp(120)))
    after = log.stat().st_size
    append(str(log), message("msg/recent", rx_at=_stamp(60)))
    off.write_text("garbage")
    assert bank.legacy_offset(str(off), str(log), NOW_S) == (after if skipped else edge)


def test_a_log_with_nothing_recent_replays_nothing_and_an_unstamped_one_replays_whole(tmp_path):
    log, off = tmp_path / "x.log", tmp_path / "x.offset"
    off.write_text("")
    append(str(log), message("msg/old", created_at=_stamp(3 * 86400)))
    assert bank.legacy_offset(str(off), str(log), NOW_S) == log.stat().st_size  # all older: from now
    bare = tmp_path / "bare.log"
    append(str(bare), message("msg/a"), message("msg/b"))
    assert bank.legacy_offset(str(off), str(bare), NOW_S) == 0                   # no stamp: cannot judge, replay all


def test_a_saved_zero_is_never_moved(tmp_path):
    log, off = tmp_path / "x.log", tmp_path / "x.offset"
    append(str(log), message("msg/old", created_at=_stamp(3 * 86400)))
    off.write_text("0")
    assert bank.legacy_offset(str(off), str(log), NOW_S) == 0


def test_a_damaged_offset_replays_the_channel_from_the_start(tmp_path, monkeypatch):
    """A damaged .channel.offset with no bank checkpoint is not a ValueError, and (kxxx's ruling on #78) the mail queued
    behind it is pushed after the open notice instead of skipped."""
    spec = importlib.util.spec_from_file_location("channel_p7_under_test", PB / "meshy_channel.py")
    channel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(channel)
    i = ident(tmp_path)
    append(i["log"], message("msg/old"))
    size = Path(i["log"]).stat().st_size
    Path(i["log"] + ".channel.offset").write_text("not a number")
    assert size > 0

    class Stop(Exception):
        pass

    pushed = []

    def record(content, **meta):
        pushed.append((meta.get("kind"), content))
        if len(pushed) >= 2:
            raise Stop()
    naps = []

    def nap(_s):                                       # a few idle polls, then stop: never a hang
        naps.append(1)
        if len(naps) > 5:
            raise Stop()
    monkeypatch.setattr(channel, "push", record)
    monkeypatch.setattr(channel.time, "sleep", nap)
    with pytest.raises(Stop):
        channel.tail(i)
    assert pushed and pushed[0][0] == "channel_open"
    assert len(pushed) >= 2, "nothing was pushed after the open notice: the queued mail was skipped"
    assert "msg/old" in pushed[1][1]                       # the queued mail, not skipped


def test_a_damaged_offset_replays_the_codex_queue_from_the_start(pb, tmp_path, monkeypatch):
    """Same for the Codex queue: the mail queued behind a damaged offset is delivered; and a bank checkpoint still wins
    over a damaged legacy copy. (No settle and the shortest gap: this counts the loop's sleeps, and the gap after the
    queued turn ends each run; test_postbox_codex_pacing covers pacing.)"""
    i = ident(tmp_path, target="fixture-session", codex_settle_s=0, codex_min_gap_s=60)
    append(i["log"], message("msg/before"))
    mark = Path(i["log"]).stat().st_size
    Path(i["log"] + ".codex.offset").write_text("")
    calls = []
    rounds = []

    class Stop(Exception):
        pass

    def sleep_twice(_seconds):
        rounds.append(1)
        if len(rounds) > 1:
            raise Stop()
    monkeypatch.setattr(pb.time, "sleep", sleep_twice)
    monkeypatch.setattr(pb.subprocess, "run", lambda argv, **kw: (calls.append(argv)
                        or subprocess.CompletedProcess(argv, 0, stdout="", stderr="")))
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert len(calls) == 1 and "msg/before" in calls[0][-1]                          # the queued mail, not skipped
    assert bank.private_json(i["log"] + ".codex.offset.bank.json")["offset"] == mark
    # CONTROL: the bank checkpoint wins over a damaged legacy copy. Only mail after the mark is delivered.
    append(i["log"], message("msg/after"))
    Path(i["log"] + ".codex.offset").write_text("garbage")
    calls.clear()
    rounds.clear()
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert len(calls) == 1 and "msg/after" in calls[0][-1] and "msg/before" not in calls[0][-1]


def test_the_last_line_names_the_keys_that_woke_it(wn):
    run, log, offset = wn
    offset.write_text("0")
    append(log, message("msg/1-a"), message("msg/2-b"))
    events = run("--consumer", "cron", "--once")
    last = events[-1]
    assert last["event"] == "waiter" and last["state"] == "woke"
    assert last["count"] == 2 and last["keys"] == ["msg/1-a", "msg/2-b"]
