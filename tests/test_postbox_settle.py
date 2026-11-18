"""The settle window (postbox_os.settle_hold; the mesh-optim matrix's item G1, action 1B): with `channel_settle_s` or
`wait_settle_s` set, ordinary mail waits that many seconds from the ARRIVAL of the first message of a burst and is
delivered in one notice; a directive, an urgent message, an edit or a problem wakes at once; a batch that stopped at
the bank's cap is delivered at once; the wait has a cap; and it is off unless set. Nothing is committed while it waits.

The window holds no state: it is judged by each message's own `rx_at` against the wall clock, so a restart or a new
waiter process cannot start it again (txxxx aeee9c).

Server-free and database-free: the channel loop runs against a spool file with a recorded sleep and a fake clock.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from tests.test_postbox_channel_lock_retry import PB, Stop, channel, kinds, rig  # noqa: F401  (the fixtures)
from tests.test_postbox_delivery_bank import append, message, setup
from tests.test_postbox_wait_next_once import _append, _msg, wn  # noqa: F401  (the subprocess fixture)

W0 = 1_800_000_000.0                                                  # a wall-clock second in 2027


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def cc(key, **kw):
    return dict(message(key, **kw), role="cc")


def at(t, key, **kw):
    """A plain `to` message that arrived at wall time t."""
    return message(key, rx_at=iso(t), **kw)


def clocked(channel, monkeypatch, r, seconds):
    """Turn the window on for the rig's seat and drive the wall clock from the loop's own one-second sleeps."""
    if seconds is not None:
        r.i["channel_settle_s"] = seconds
    r.clock = [W0]
    monkeypatch.setattr(channel.postbox_os, "settle_now", lambda: r.clock[0])


def run(channel, r, script, stop_after):
    """Run tail(); `script[n]` runs when the loop has slept n times; each sleep of 1 s advances the clock by 1 s."""
    def on_sleep(s):
        n = len(r.sleeps)
        if s == 1:
            r.clock[0] += 1
        if n in script:
            script[n]()
        if n >= stop_after:
            raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(r.i)


def offset(r):
    return int(Path(r.i["log"] + ".channel.offset").read_text().split()[0])


def delivered(r):
    """The key of every message pushed so far, in order, whether it went alone or in a batch."""
    out = []
    for _content, meta in r.pushed:
        if meta.get("kind") == "message":
            out.append(meta["key"])
        elif meta.get("kind") == "batch":
            out += [m["key"] for m in json.loads(meta["items"])]
    return out


# ------------------------------------------------------------------------------------------------ the pure parts

@pytest.mark.parametrize("value, want", [
    (0, 0), (1, 1), (20, 20), (60, 60), (61, 0), (3600, 0), (-1, 0), (True, 0), (False, 0), ("20", 0), (20.0, 0),
    (None, 0), ([20], 0),
])
def test_only_a_whole_number_of_seconds_up_to_the_cap_is_a_window(channel, value, want):
    said = []
    got = channel.postbox_os.settle_seconds({"name": "seat", "channel_settle_s": value}, "channel_settle_s", said.append)
    assert got == want
    valid = type(value) is int and 0 <= value <= channel.postbox_os.SETTLE_MAX_S
    assert bool(said) == (not valid)
    if said:
        assert "channel_settle_s" in said[0] and "mail is not delayed" in said[0]


def test_the_window_is_off_when_the_setting_is_absent_and_a_failing_report_never_raises(channel):
    pos = channel.postbox_os
    assert pos.settle_seconds({"name": "seat"}) == 0 and pos.settle_seconds({"name": "seat"}, "wait_settle_s") == 0
    assert pos.SETTLE_MAX_S == 60

    def boom(_text):
        raise OSError("disk full")
    assert pos.settle_seconds({"name": "seat", "channel_settle_s": "soon"}, "channel_settle_s", boom) == 0


@pytest.mark.parametrize("events, now", [
    ([message("msg/a")], False),
    ([message("msg/a"), cc("msg/b")], False),
    ([cc("msg/a", urgent=True)], False),                                # urgent is a `to` flag only
    ([message("msg/a", directive=True)], True),
    ([message("msg/a", directive=1)], True),
    ([message("msg/a"), message("msg/b", urgent=True)], True),
    ([message("msg/a", recovered=True)], True),
    ([{"event": "edit", "role": "to", "key": "msg/a", "version": 2}], True),
    ([{"event": "status", "state": "error"}], True),
    ([{"event": "supervisor", "state": "crashed"}], True),
    ([{"event": "something new"}], True),
    ([message("msg/a"), "not a dict"], True),
])
def test_only_a_batch_of_plain_messages_may_wait(channel, events, now):
    assert channel.postbox_os.settle_bypass(events, {"name": "seat"}) is now


def test_the_window_runs_from_the_oldest_arrival_and_holds_no_state(channel):
    hold = channel.postbox_os.settle_hold
    one, two = [message("msg/a")], [message("msg/a"), message("msg/b")]
    assert hold(one, [iso(W0)], {}, 20, now=W0) is True
    assert hold(one, [iso(W0)], {}, 20, now=W0 + 19.9) is True
    assert hold(one, [iso(W0)], {}, 20, now=W0 + 20) is False                    # exactly the window: deliver
    # a later arrival does not extend it: the OLDEST decides, whatever the order
    assert hold(two, [iso(W0), iso(W0 + 19)], {}, 20, now=W0 + 20) is False
    assert hold(two, [iso(W0 + 19), iso(W0)], {}, 20, now=W0 + 20) is False
    assert hold(two, [iso(W0 + 5), iso(W0 + 19)], {}, 20, now=W0 + 20) is True
    # no state: the same call gives the same answer however often and from whatever "process" it is made
    assert [hold(one, [iso(W0)], {}, 20, now=W0 + 30) for _ in range(3)] == [False, False, False]
    assert hold(one, [iso(W0 - 3600)], {}, 60, now=W0) is False                  # an old backlog never waits


@pytest.mark.parametrize("kw, why", [
    (dict(seconds=0), "the window is off"),
    (dict(cut=True), "the bank stopped at its batch cap"),
    (dict(received=[None]), "an arrival time is missing"),
    (dict(received=["yesterday"]), "an arrival time is unreadable"),
    (dict(received=["2027-01-15T08:00:00"]), "an arrival time has no zone"),
    # the same instant written as this box's LOCAL time with no zone: read as local it would be 1 s old and wait
    (dict(received=[dt.datetime.fromtimestamp(W0).strftime("%Y-%m-%dT%H:%M:%S")]), "a local time with no zone"),
    (dict(events=[message("msg/a"), message("msg/b")], received=[iso(W0), None]), "one of two times is missing"),
    (dict(events=[message("msg/a"), message("msg/b")], received=[None, iso(W0)]), "the other of two is missing"),
    (dict(received=[7]), "an arrival time is not text"),
    (dict(received=[]), "fewer times than events"),
    (dict(received=None), "no times at all"),
    (dict(received=[iso(W0), iso(W0)]), "more times than events"),
    (dict(received=[iso(W0 + 5)]), "the clock ran backwards (the message is from the future)"),
    # nxxxxx 0b7aac P3-2: an older arrival must not hide a future one in the same batch, in either order
    (dict(events=[message("msg/a"), message("msg/b")], received=[iso(W0), iso(W0 + 5)]), "past then future"),
    (dict(events=[message("msg/a"), message("msg/b")], received=[iso(W0 + 5), iso(W0)]), "future then past"),
    (dict(events=[]), "nothing to deliver"),
    (dict(events=[message("msg/a", directive=True)]), "a directive"),
])
def test_everything_that_is_not_a_plain_young_batch_is_delivered_at_once(channel, kw, why):
    base = dict(events=[message("msg/a")], received=[iso(W0)], seconds=20, cut=False)
    assert channel.postbox_os.settle_hold(base["events"], base["received"], {}, base["seconds"], now=W0 + 1) is True
    base.update(kw)
    got = channel.postbox_os.settle_hold(base["events"], base["received"], {}, base["seconds"], cut=base["cut"], now=W0 + 1)
    assert got is False, why


def test_the_clock_seam_is_the_wall_clock(channel):
    import time
    assert abs(channel.postbox_os.settle_now() - time.time()) < 5
    fresh = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert channel.postbox_os.settle_hold([message("msg/a")], [fresh], {}, 60) is True      # no `now`: the real clock


# ------------------------------------------------------------------------------------------------ the bank's part

def test_the_bank_says_when_a_batch_is_not_all_the_waiting_mail_and_gives_each_arrival_time(tmp_path):
    i, b = setup(tmp_path)
    wanted = lambda d: True                                            # noqa: E731
    append(i["log"], *[at(W0 + n, "msg/%02d" % n) for n in range(3)])
    end, events = b.collect(wanted)
    assert [e["key"] for e in events] == ["msg/00", "msg/01", "msg/02"]
    assert b.cut is False and b.received() == [iso(W0), iso(W0 + 1), iso(W0 + 2)]
    b.commit(end, events)
    assert b.received() == []
    append(i["log"], *[at(W0 + n, "msg/b%02d" % n) for n in range(20)], message("msg/no-time"))
    end, events = b.collect(wanted)
    assert len(events) == 12 and b.cut is True and len(b.received()) == 12
    b.commit(end, events)
    end, events = b.collect(wanted)
    assert len(events) == 9 and b.cut is False and b.received()[-1] is None      # the line with no rx_at


# ------------------------------------------------------------------------------------------------ the channel loop

def test_a_burst_becomes_one_notice_after_the_window_and_nothing_is_committed_while_it_waits(channel, rig, monkeypatch):
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, 20)
    start = offset(r)
    seen = {}

    def note(n):
        seen[n] = (kinds(r), offset(r))
    run(channel, r, {
        1: lambda: append(r.i["log"], at(r.clock[0], "msg/a")),                              # arrives at W0+1
        4: lambda: append(r.i["log"], at(r.clock[0], "msg/b"), dict(cc("msg/c"), rx_at=iso(r.clock[0]))),
        20: lambda: note(20), 23: lambda: note(23),
    }, stop_after=23)
    assert seen[20] == (["channel_open"], start)                        # 19 s after msg/a arrived: nothing pushed or saved
    assert seen[23][0] == ["channel_open", "batch"] and seen[23][1] > start
    _content, meta = r.pushed[1]
    assert int(meta["count"]) == 3 and delivered(r) == ["msg/a", "msg/b", "msg/c"]


def test_without_the_setting_each_message_wakes_at_once(channel, rig, monkeypatch):
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, None)
    run(channel, r, {1: lambda: append(r.i["log"], at(r.clock[0], "msg/a")),
                     3: lambda: append(r.i["log"], at(r.clock[0], "msg/b"))}, stop_after=5)
    assert kinds(r) == ["channel_open", "message", "message"]


@pytest.mark.parametrize("waker", [
    message("msg/now", directive=True),
    message("msg/now", urgent=True),
    {"event": "edit", "role": "to", "key": "msg/old", "version": 2},
    {"event": "status", "state": "error"},
])
def test_a_directive_urgent_mail_an_edit_or_a_problem_wakes_at_once_with_what_was_waiting(channel, rig, monkeypatch, waker):
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, 20)
    seen = {}
    run(channel, r, {
        1: lambda: append(r.i["log"], at(r.clock[0], "msg/a")),
        3: lambda: seen.setdefault("before", kinds(r)),
        4: lambda: append(r.i["log"], dict(waker, rx_at=iso(r.clock[0]))),
        6: lambda: seen.setdefault("after", kinds(r)),
    }, stop_after=6)
    assert seen["before"] == ["channel_open"]
    assert seen["after"] == ["channel_open", "batch"], seen             # 3 s in, not 20
    assert int(r.pushed[1][1]["count"]) == 2


def test_twenty_messages_at_once_are_all_delivered_inside_the_cap(channel, rig, monkeypatch):
    """txxxx aeee9c P2-1: the bank hands over 12 a pass. The first 12 used to wait 60 s and the other 8 a second
    60 s (121 s). Now a cut batch goes at once and the rest by the cap, counted from when they ARRIVED."""
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, 60)
    when = {}

    def burst():
        when["t0"] = r.clock[0]
        append(r.i["log"], *[at(r.clock[0], "msg/%02d" % n) for n in range(20)])

    def note(n):
        when[n] = (len(delivered(r)), r.clock[0] - when["t0"])
    run(channel, r, {1: burst, 3: lambda: note(3), 59: lambda: note(59), 62: lambda: note(62)}, stop_after=62)
    assert when[3][0] == 12                                             # the cut batch: at once
    assert when[59] == (12, 58)                                         # 58 s after arrival: the other 8 still wait
    assert when[62] == (20, 61)                                         # delivered on the pass at 60 s after arrival
    assert delivered(r) == ["msg/%02d" % n for n in range(20)]


def test_a_directive_behind_twelve_ordinary_messages_does_not_wait(channel, rig, monkeypatch):
    """txxxx aeee9c P2-2: a directive at position 15 is not in the first batch of 12, so the batch must not be held."""
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, 60)
    seen = {}

    def burst():
        append(r.i["log"], *[at(r.clock[0], "msg/%02d" % n) for n in range(14)],
               message("msg/directive", directive=True, rx_at=iso(r.clock[0])))
    run(channel, r, {1: burst, 4: lambda: seen.setdefault("n", delivered(r))}, stop_after=4)
    assert len(seen["n"]) == 15 and seen["n"][-1] == "msg/directive"    # all of it within three passes, not 60 s


def test_a_message_with_no_arrival_time_is_delivered_at_once(channel, rig, monkeypatch):
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, 20)
    run(channel, r, {1: lambda: append(r.i["log"], message("msg/a"))}, stop_after=3)
    assert kinds(r) == ["channel_open", "message"]


def test_a_malformed_setting_is_said_once_and_delays_nothing(channel, rig, monkeypatch):
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, "20")
    run(channel, r, {1: lambda: append(r.i["log"], at(r.clock[0], "msg/a"))}, stop_after=4)
    assert kinds(r).count("message") == 1
    problems = [c for c, m in r.pushed if "channel_settle_s" in c]
    assert len(problems) == 1 and "mail is not delayed" in problems[0], [k for k in kinds(r)]


def test_a_window_above_the_cap_is_no_window(channel, rig, monkeypatch):
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, 600)
    run(channel, r, {1: lambda: append(r.i["log"], at(r.clock[0], "msg/a"))}, stop_after=4)
    assert kinds(r).count("message") == 1


def test_with_no_window_set_the_bank_is_asked_nothing_new(channel, rig, monkeypatch):
    """Off must mean untouched: the notice tests drive the channel with stand-in banks that have no received()."""
    r = rig
    r.release()
    clocked(channel, monkeypatch, r, None)

    def never(self):
        raise AssertionError("received() was called with no window set")
    monkeypatch.setattr(channel.delivery_bank.Bank, "received", never)
    run(channel, r, {1: lambda: append(r.i["log"], at(r.clock[0], "msg/a"))}, stop_after=3)
    assert kinds(r) == ["channel_open", "message"]


# ------------------------------------------------------------------------------------------------ the waiter

def _waiter(tmp_path, monkeypatch, argv, settle):
    spec = importlib.util.spec_from_file_location("wait_next_settle_under_test", PB / "wait_next.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log = tmp_path / "seat.log"
    ident = {"name": "seat", "log": log.as_posix(), "client": tmp_path.as_posix(), "python": sys.executable,
             "state": (tmp_path / "state.json").as_posix()}
    if settle is not None:
        ident["wait_settle_s"] = settle
    (tmp_path / "config.json").write_text(json.dumps({"base_url": "http://127.0.0.1:1", "identities": [ident]}),
                                          encoding="utf-8")
    append(log.as_posix(), message("msg/1-old"))
    monkeypatch.setattr(mod, "HERE", str(tmp_path))
    monkeypatch.setattr(mod, "stdout_discarded", lambda: False)
    monkeypatch.setattr(mod, "postbox_running", lambda: True)
    monkeypatch.setattr(mod, "graphline", None, raising=False)
    monkeypatch.setattr(mod, "owedline", None, raising=False)
    monkeypatch.setattr(sys, "argv", ["wait_next.py", "seat", *argv])
    return mod, log


def _keys(out):
    return [json.loads(l)["key"] for l in out.splitlines() if l.startswith("{") and json.loads(l).get("event") == "message"]


def test_the_blocking_waiter_delivers_a_burst_together_after_its_window(tmp_path, monkeypatch, capsys):
    mod, log = _waiter(tmp_path, monkeypatch, [], settle=6)
    t, sleeps = [W0], []
    monkeypatch.setattr(mod.postbox_os, "settle_now", lambda: t[0])

    def sleep(seconds):
        sleeps.append(seconds)
        t[0] += 2
        if len(sleeps) == 1:
            append(log.as_posix(), at(t[0], "msg/2"))                   # arrives at W0+2
        if len(sleeps) == 2:
            append(log.as_posix(), at(t[0], "msg/3"))
        assert len(sleeps) < 12, "the waiter never woke"
    monkeypatch.setattr(mod.time, "sleep", sleep)
    assert mod.main() == 0
    assert _keys(capsys.readouterr().out) == ["msg/2", "msg/3"]
    assert len(sleeps) == 4, sleeps          # quiet pass; seen at W0+2; held at +4 and +6; delivered at +8 (6 s after it arrived)


@pytest.mark.parametrize("setting, warned", [("60s", True), (600, True), (-5, True), (True, True), (60, False),
                                             (0, False), (None, False)])
def test_the_waiter_says_a_malformed_window_once_at_its_start_and_never_delays_for_it(tmp_path, monkeypatch, capsys,
                                                                                    setting, warned):
    """nxxxxx 0b7aac P3-1: the waiter read the setting with no report, so a typo was silent."""
    mod, log = _waiter(tmp_path, monkeypatch, [], settle=setting)
    t, sleeps = [W0], []
    monkeypatch.setattr(mod.postbox_os, "settle_now", lambda: t[0])

    def sleep(seconds):
        sleeps.append(seconds)
        t[0] += 2
        if len(sleeps) == 1:
            append(log.as_posix(), at(t[0] - 100, "msg/2"))             # old enough to pass any real window
        assert len(sleeps) < 6, "the waiter never woke"
    monkeypatch.setattr(mod.time, "sleep", sleep)
    assert mod.main() == 0
    out = capsys.readouterr()
    assert _keys(out.out) == ["msg/2"] and len(sleeps) == 1
    said = (out.out + out.err).count("wait_settle_s")
    assert said == (1 if warned else 0), (setting, out.err[-300:], out.out[-300:])


def test_a_malformed_window_does_not_delay_a_fresh_message_in_the_waiter(tmp_path, monkeypatch, capsys):
    mod, log = _waiter(tmp_path, monkeypatch, [], settle="60")
    t, sleeps = [W0], []
    monkeypatch.setattr(mod.postbox_os, "settle_now", lambda: t[0])

    def sleep(seconds):
        sleeps.append(seconds)
        t[0] += 2
        if len(sleeps) == 1:
            append(log.as_posix(), at(t[0], "msg/2"))                   # fresh: a real 60 s window would hold it
        assert len(sleeps) < 6, "the waiter held mail on a malformed setting"
    monkeypatch.setattr(mod.time, "sleep", sleep)
    assert mod.main() == 0
    assert _keys(capsys.readouterr().out) == ["msg/2"] and len(sleeps) == 1


def test_the_blocking_waiter_delivers_a_batch_cut_at_the_banks_cap_at_once(tmp_path, monkeypatch, capsys):
    """txxxx aeee9c, the waiter's side: 14 fresh messages; the bank hands over 12 and says more is waiting."""
    mod, log = _waiter(tmp_path, monkeypatch, [], settle=60)
    t, sleeps = [W0], []
    monkeypatch.setattr(mod.postbox_os, "settle_now", lambda: t[0])

    def sleep(seconds):
        sleeps.append(seconds)
        t[0] += 2
        if len(sleeps) == 1:
            append(log.as_posix(), *[at(t[0], "msg/%02d" % n) for n in range(14)])
        assert len(sleeps) < 5, "the waiter held a cut batch"
    monkeypatch.setattr(mod.time, "sleep", sleep)
    assert mod.main() == 0
    assert _keys(capsys.readouterr().out) == ["msg/%02d" % n for n in range(12)]
    assert len(sleeps) == 1, sleeps                                    # the pass that saw them delivered them


class _Killed(BaseException):
    """The waiter's process ending inside its settle window."""


def test_a_fresh_waiter_that_ends_inside_its_window_still_shows_the_deferred_mail_next_time(tmp_path, monkeypatch,
                                                                                         capsys):
    """The GitHub Codex review of 3fdf617b (wait_next.py:166): no saved position, mail arrives between arming and
    the first pass, the pass defers it, and the waiter ends. Its next run started at the new end and skipped it."""
    mod, log = _waiter(tmp_path, monkeypatch, [], settle=60)
    t, locks = [W0], []
    monkeypatch.setattr(mod.postbox_os, "settle_now", lambda: t[0])
    real_lock = mod.postbox_os.try_lock
    monkeypatch.setattr(mod.postbox_os, "try_lock", lambda fd: locks.append(fd) or real_lock(fd))
    real_refresh, calls = mod.postbox_os.refresh_hold_options, []

    def refresh(*a, **kw):
        calls.append(1)
        if len(calls) == 2:                                              # armed; the first pass has not collected yet
            append(log.as_posix(), at(t[0], "msg/2"))
        return real_refresh(*a, **kw)
    monkeypatch.setattr(mod.postbox_os, "refresh_hold_options", refresh)

    def killed(seconds):
        raise _Killed()
    monkeypatch.setattr(mod.time, "sleep", killed)
    start = os.path.getsize(log)
    with pytest.raises(_Killed):
        mod.main()
    assert _keys(capsys.readouterr().out) == []                          # deferred, not shown
    off_file = [p for p in os.listdir(tmp_path) if p.startswith("seat.log.wait-") and p.endswith(".offset")]
    assert len(off_file) == 1
    assert mod.delivery_bank.legacy_offset((tmp_path / off_file[0]).as_posix(), log.as_posix()) == start
    for fd in locks:
        os.close(fd)                                                     # the process ended: its lock is gone
    # the next run, after the window: the deferred message is still ahead of the saved position
    monkeypatch.setattr(mod.postbox_os, "refresh_hold_options", real_refresh)
    t[0] += 100

    def never(seconds):
        raise AssertionError("the next run did not find the deferred message")
    monkeypatch.setattr(mod.time, "sleep", never)
    assert mod.main() == 0
    assert _keys(capsys.readouterr().out) == ["msg/2"]


def test_a_waiter_with_a_saved_position_writes_nothing_while_it_defers(tmp_path, monkeypatch, capsys):
    """The start-up save is for a consumer with no position only: a deferral is otherwise not a commit."""
    mod, log = _waiter(tmp_path, monkeypatch, [], settle=6)
    t, sleeps, commits = [W0], [], []
    monkeypatch.setattr(mod.postbox_os, "settle_now", lambda: t[0])
    real_commit = mod.delivery_bank.Bank.commit

    def commit(self, end, events, shown=None):
        commits.append((len(sleeps), len(events)))
        return real_commit(self, end, events, shown)
    monkeypatch.setattr(mod.delivery_bank.Bank, "commit", commit)

    def sleep(seconds):
        sleeps.append(seconds)
        t[0] += 2
        if len(sleeps) == 1:
            append(log.as_posix(), at(t[0], "msg/2"))
        assert len(sleeps) < 12, "the waiter never woke"
    monkeypatch.setattr(mod.time, "sleep", sleep)
    assert mod.main() == 0
    assert _keys(capsys.readouterr().out) == ["msg/2"]
    # the quiet first pass saves the start (no events); nothing more is written until the delivery
    assert commits[0] == (0, 0) and commits[-1][1] == 1 and len(commits) == 2, commits


def test_a_fresh_waiter_saves_its_start_once_however_many_passes_it_defers(tmp_path, monkeypatch, capsys):
    mod, log = _waiter(tmp_path, monkeypatch, [], settle=6)
    t, sleeps, commits = [W0], [], []
    monkeypatch.setattr(mod.postbox_os, "settle_now", lambda: t[0])
    real_commit = mod.delivery_bank.Bank.commit

    def commit(self, end, events, shown=None):
        commits.append(len(events))
        return real_commit(self, end, events, shown)
    monkeypatch.setattr(mod.delivery_bank.Bank, "commit", commit)
    real_refresh, calls = mod.postbox_os.refresh_hold_options, []

    def refresh(*a, **kw):
        calls.append(1)
        if len(calls) == 2:                                              # before the first pass collects
            append(log.as_posix(), at(t[0], "msg/2"))
        return real_refresh(*a, **kw)
    monkeypatch.setattr(mod.postbox_os, "refresh_hold_options", refresh)

    def sleep(seconds):
        sleeps.append(seconds)
        t[0] += 2
        assert len(sleeps) < 12, "the waiter never woke"
    monkeypatch.setattr(mod.time, "sleep", sleep)
    assert mod.main() == 0
    assert _keys(capsys.readouterr().out) == ["msg/2"]
    assert len(sleeps) == 3 and commits == [0, 1], (sleeps, commits)     # three deferring passes, ONE start save


def test_a_one_pass_waiter_never_waits(wn, tmp_path):
    """--once is one pass with no next pass of its own: a window there would add the caller's interval to the wait.
    Each poll is its own process, as a cron runs it; the message is fresh, so only --once keeps it from waiting."""
    run_, log, _offset = wn
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    cfg["identities"][0]["wait_settle_s"] = 60
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    _append(log, _msg("msg/1-old"))
    run_("--once")                                                      # arms at the end of the log
    fresh = dict(_msg("msg/2"), rx_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    _append(log, fresh)
    got = run_("--once")
    assert [e["key"] for e in got if e.get("event") == "message"] == ["msg/2"]
