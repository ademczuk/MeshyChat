"""tools/postbox/meshy_channel.py: a session that finds the channel lock busy keeps trying it (issue #153, 2026-10-10).

Seen on the HZ, 2026-10-07: two stale copies of a Claude session held <log>.channel.lock, the new session got the busy
notice, the operator stopped the stale copies, and the new session still heard nothing: tail() tried the lock once and
returned. It now tries again every 15 s for 10 minutes, then every 60 s, for the life of the session
(take_channel_lock): ONE busy notice when it starts waiting, nothing per try, and ONE open event ("now open in this
session") when it takes the lock, after which it resumes from the stored offset like any start.

The holder here is a second open of the same lock file in this process, taken with postbox_os.try_lock: a whole-file
flock (POSIX) or an msvcrt byte lock (Windows) on another open file conflicts within one process just as across two,
so the conflict is the real one. Every try the session makes goes through the real primitive too (the test only counts
the calls), so "never two holders" is the operating system's answer, not a stub's.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX= <venv>/bin/python -m pytest tests/test_postbox_channel_lock_retry.py
      -q -p no:cacheprovider --noconftest
Mutants (each a small break of meshy_channel.py, each applied and seen to fail the named test):
  L1 the retry removed: busy notice, then tail returns (the old code)   test_a_lock_freed_mid_session_is_taken_once_and_mail_flows
  L2 a busy notice on every try                                         test_a_lock_that_stays_busy_gives_one_notice_and_no_mail
  L3 gives up after the fast window and opens WITHOUT the lock          test_a_lock_that_stays_busy_gives_one_notice_and_no_mail
  L4 the cadence never slows to BUSY_RETRY_SLOW_S                       test_a_lock_that_stays_busy_gives_one_notice_and_no_mail
  L5 no wait between tries (a busy spin)                                test_a_lock_that_stays_busy_gives_one_notice_and_no_mail
  L6 a separate "now open" notice before the usual open event           test_a_lock_freed_mid_session_is_taken_once_and_mail_flows
  L7 the busy notice does not name the lock file                        test_a_lock_freed_mid_session_is_taken_once_and_mail_flows
  L8 a session that waited starts at the log's end, not the offset      test_a_lock_freed_mid_session_is_taken_once_and_mail_flows
  L9 a free lock is announced as busy and waited for                    test_a_free_lock_opens_at_once_with_the_usual_event
"""
from __future__ import annotations

import importlib.util
import os
import shlex
from pathlib import Path

import pytest

from tests.test_postbox_delivery_bank import PB, append, message
from tests.test_postbox_reconcile import ident

import postbox_os  # noqa: E402  (tests.test_postbox_delivery_bank put tools/postbox on the path)

REAL_TRY_LOCK = postbox_os.try_lock


class Stop(Exception):
    pass


@pytest.fixture
def channel(monkeypatch):
    monkeypatch.setenv("MESHY_IDENTITY", "")
    spec = importlib.util.spec_from_file_location("meshy_channel_lock_retry_under_test", PB / "meshy_channel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "graphline", None)      # routing context is not under test, and makes no call here
    monkeypatch.setattr(mod, "owedline", None)
    return mod


@pytest.fixture
def rig(channel, tmp_path, monkeypatch):
    """The seat, a holder fd that takes the channel lock the way a session does, a record of every push and sleep,
    and every try_lock call the session makes (through the real primitive). Closes the session's and the holder's
    fds at the end, so no lock outlives its test."""
    i = ident(tmp_path, address="seat-address")
    append(i["log"], message("msg/old"))
    Path(i["log"] + ".channel.offset").write_text(str(Path(i["log"]).stat().st_size))   # where the last holder stopped
    lock_path = i["log"] + ".channel.lock"
    r = type("Rig", (), {})()
    r.i, r.lock_path, r.pushed, r.sleeps, r.tries, r.session_fds = i, lock_path, [], [], [], set()
    r.holder = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    r.on_sleep = lambda s: None

    def release():
        # Closing the fd releases a lock it holds on every OS; an explicit unlock of a holder that never took the lock
        # raises on Windows (msvcrt, Errno 13) where flock does nothing (memory-bxxxxx's HQB run of #205).
        if r.holder is not None:
            os.close(r.holder)
            r.holder = None
    r.release = release

    def send(obj):                                   # the real push builds the notification; record what it writes
        assert obj["method"] == "notifications/claude/channel"
        r.pushed.append((obj["params"]["content"], obj["params"]["meta"]))

    def sleep(s):
        r.sleeps.append(s)
        r.on_sleep(s)

    def try_lock(fd):
        r.session_fds.add(fd)
        r.tries.append(REAL_TRY_LOCK(fd))
        if len(r.tries) > 200:                       # a busy spin (L5) ends here instead of hanging the run
            raise Stop("try_lock called %d times" % len(r.tries))
        return r.tries[-1]
    monkeypatch.setattr(channel, "send", send)
    monkeypatch.setattr(channel.time, "sleep", sleep)
    monkeypatch.setattr(channel.postbox_os, "try_lock", try_lock)
    try:
        yield r
    finally:
        release()
        for fd in r.session_fds:
            try:
                os.close(fd)
            except OSError:
                pass


def kinds(r):
    return [m.get("kind") for _c, m in r.pushed]


def test_a_lock_freed_mid_session_is_taken_once_and_mail_flows(channel, rig):
    """Busy, then the holder goes: one busy notice naming the lock file, nothing per try, one open event that says
    "now open in this session", then the mail from the stored offset: the mail that arrived while it waited, and the
    mail after. MUTANTS L1 (no retry), L6 (two open events), L7 (no lock path), L8 (starts at the log's end)."""
    r = rig
    assert REAL_TRY_LOCK(r.holder) is True                 # another session holds the channel

    def on_sleep(s):
        if len(r.sleeps) == 3:                             # two tries lost, then the holder goes away
            assert kinds(r) == ["channel_busy"]            # nothing pushed per try while it waited
            r.release()
            append(r.i["log"], message("msg/gap"))         # arrived after the holder went, before this session took over
        elif len(r.sleeps) == 4:
            append(r.i["log"], message("msg/new"))
        elif len(r.sleeps) > 4:
            raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(r.i)
    assert kinds(r) == ["channel_busy", "channel_open", "message", "message"]
    (busy, busy_meta), (opened, open_meta) = r.pushed[0], r.pushed[1]
    assert busy.startswith("[CHANNEL] MeshyChat channel for seat-prod is already open in another session")
    assert r.lock_path in busy and busy_meta["lock"] == r.lock_path
    assert "keeps trying" in busy
    if not postbox_os.IS_WINDOWS:
        assert "`lsof %s`" % shlex.quote(r.lock_path) in busy   # paste-ready even with a space in the path
    assert opened.startswith("[CHANNEL] MeshyChat channel for seat-prod (seat-address) is now open in this session")
    assert "after about 45 s waiting" in opened and open_meta["waited_s"] == "45"
    assert opened.endswith("New mail arrives here as events.")
    assert [m["key"] for _c, m in r.pushed[2:]] == ["msg/gap", "msg/new"]   # from the offset: msg/old was the last holder's
    assert r.sleeps[:3] == [channel.BUSY_RETRY_FAST_S] * 3
    assert r.tries == [False, False, False, True]          # the lock taken by the try after it was freed, through try_lock


def test_a_lock_that_stays_busy_gives_one_notice_and_no_mail(channel, rig):
    """Held for the whole run (past the 10-minute fast window): one busy notice and nothing else, however many tries
    and whatever mail arrives; the cadence slows after the window; the offset and bank are never touched, so two
    sessions never race one cursor. MUTANTS L2 (a notice per try), L3 (opens without the lock), L4 (never slows),
    L5 (no wait between tries)."""
    r = rig
    assert REAL_TRY_LOCK(r.holder) is True
    fast_tries = channel.BUSY_RETRY_FAST_FOR_S // channel.BUSY_RETRY_FAST_S
    assert fast_tries == 40
    offset_before = Path(r.i["log"] + ".channel.offset").read_bytes()
    files_before = sorted(os.listdir(Path(r.i["log"]).parent))

    def on_sleep(_s):
        if len(r.sleeps) % 10 == 0:
            append(r.i["log"], message("msg/while-busy-%d" % len(r.sleeps)))
        if len(r.sleeps) >= fast_tries + 5:
            raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(r.i)
    assert kinds(r) == ["channel_busy"]                    # one notice, no open, no mail
    assert r.sleeps == [channel.BUSY_RETRY_FAST_S] * fast_tries + [channel.BUSY_RETRY_SLOW_S] * 5
    assert len(r.tries) == fast_tries + 5 and not any(r.tries)
    assert Path(r.i["log"] + ".channel.offset").read_bytes() == offset_before
    assert sorted(os.listdir(Path(r.i["log"]).parent)) == files_before


def test_a_free_lock_opens_at_once_with_the_usual_event(channel, rig):
    """The control: a free lock is taken by the first try, with no busy notice, no wait, and the open event's text as
    it was before #153. MUTANT L9."""
    r = rig
    r.release()                                            # nobody holds the channel
    append(r.i["log"], message("msg/first"))

    def on_sleep(_s):
        raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(r.i)
    assert kinds(r) == ["channel_open", "message"]
    opened, meta = r.pushed[0]
    assert opened == "[CHANNEL] MeshyChat channel open for seat-prod (seat-address). New mail arrives here as events."
    assert "waited_s" not in meta
    assert r.tries == [True] and r.sleeps == [1]           # the one sleep is the delivery loop's, after the mail
    assert r.pushed[1][1]["key"] == "msg/first"
