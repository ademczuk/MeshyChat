"""tools/postbox: a starting daemon is not turned away by a lock probe (2026-09-28).

Several things ask "is a postbox running?" by taking postbox.lock and releasing it at once:
wait_next.py, kimi_arm_hook.py, each seat's wait.py, and two fleet PowerShell checks. A daemon that
started inside that instant found the lock held, logged "another postbox holds postbox.lock" and
exited, and nothing restarted it until the next logon. The daemon now retries for about two
seconds (acquire_daemon_lock), which outlasts any probe and does not outlast a real daemon.

The holder in these tests is a SEPARATE PROCESS taking the lock the way the probers and the daemon
do (postbox_os.try_lock), so the conflict is the real one on either platform.

Run:  .venv/Scripts/python.exe -m pytest tests/test_postbox_lock.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"


@pytest.fixture
def pb():
    spec = importlib.util.spec_from_file_location("pbtest_lock_postbox", PB / "postbox.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def holder(lock_path, hold_s):
    """A second process that takes the lock, says so on stdout, holds it hold_s, then releases."""
    code = textwrap.dedent("""
        import os, sys, time
        sys.path.insert(0, sys.argv[1])
        import postbox_os
        fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT)
        assert postbox_os.try_lock(fd)
        print("HELD", flush=True)
        time.sleep(float(sys.argv[3]))
        postbox_os.unlock(fd)
    """)
    p = subprocess.Popen([sys.executable, "-c", code, str(PB), str(lock_path), str(hold_s)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "HELD"
    return p


def open_lock(path):
    return os.open(str(path), os.O_RDWR | os.O_CREAT)


def test_a_brief_probe_does_not_turn_the_daemon_away(pb, tmp_path):
    lock = tmp_path / "postbox.lock"
    h = holder(lock, 0.5)
    fd = open_lock(lock)
    try:
        assert pb.postbox_os.try_lock(fd) is False          # the old single attempt loses here
        assert pb.acquire_daemon_lock(fd) is True
    finally:
        os.close(fd)
        h.wait(timeout=10)


def test_a_real_daemon_still_turns_a_second_one_away(pb, tmp_path):
    # The control: the same call against a holder that outlasts the retry window.
    lock = tmp_path / "postbox.lock"
    h = holder(lock, 5)
    fd = open_lock(lock)
    try:
        t0 = time.monotonic()
        assert pb.acquire_daemon_lock(fd, tries=3, gap_s=0.1) is False
        assert time.monotonic() - t0 < 2
    finally:
        os.close(fd)
        h.kill()
        h.wait(timeout=10)


def test_retry_schedule(pb, monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(pb.postbox_os, "try_lock", lambda fd: next(answers))
    sleeps = []
    assert pb.acquire_daemon_lock(0, tries=5, gap_s=0.2, sleep=sleeps.append) is True
    assert sleeps == [0.2, 0.2]


def test_gives_up_after_its_tries_without_a_trailing_sleep(pb, monkeypatch):
    monkeypatch.setattr(pb.postbox_os, "try_lock", lambda fd: False)
    sleeps = []
    assert pb.acquire_daemon_lock(0, tries=4, gap_s=0.2, sleep=sleeps.append) is False
    assert sleeps == [0.2, 0.2, 0.2]


def test_main_takes_the_lock_through_the_retry(pb, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pb, "HERE", str(tmp_path))
    monkeypatch.setattr(pb, "note", lambda msg: calls.append(("note", msg)))
    monkeypatch.setattr(pb, "acquire_daemon_lock", lambda fd: calls.append(("acquire",)) or False)
    monkeypatch.setattr(sys, "argv", ["postbox.py"])
    assert pb.main() == 2
    assert calls[0] == ("acquire",)
    assert "another postbox holds postbox.lock" in calls[1][1]
