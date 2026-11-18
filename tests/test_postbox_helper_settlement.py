"""Helper settlement across the Popen, attach and release boundary (POSTBOX-448, admin 361a7e and 02f44a P2-2).

Real processes on a tmp_path fake root. A "parent" plays the daemon in its own interpreter and dies with os._exit at the
named boundary; the helper it starts writes a marker file if it ever runs. Every process here is this test's own.
The gate is POSIX only; test_windows_style_start_has_no_gate_and_its_dead_record_is_abandoned covers what is left without
it, and tests/test_postbox_helper_spawning_records.py runs the record rules on every platform.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from tools.postbox import graphline, owedline, postbox, postbox_os

pytestmark = pytest.mark.skipif(postbox_os.IS_WINDOWS, reason="the gate is POSIX only")

MOD = os.path.dirname(os.path.abspath(postbox_os.__file__))
PY = sys.executable


def run_parent(body, *args):
    """Run `body` as a daemon stand-in with postbox_os imported, in a clean environment."""
    code = "import json, os, subprocess, sys, time\nsys.path.insert(0, %r)\nimport postbox_os\n%s" % (MOD, body)
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([PY, "-c", code] + [str(a) for a in args], capture_output=True, text=True, timeout=60, env=env)


def marker_helper(path, hold_s=0.0):
    return [PY, "-I", "-c", "import time; open(%r, 'w').close(); time.sleep(%r)" % (str(path), hold_s)]


def records(hdir):
    return sorted(p for p in hdir.iterdir() if not p.name.startswith(".")) if hdir.exists() else []


def wait_gone(pid, secs=10.0):
    """Until `pid` no longer exists (an orphaned gate is reaped by init)."""
    end = time.time() + secs
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def open_fds():
    return set(os.listdir("/dev/fd"))


class SpyPopen(subprocess.Popen):
    """The real Popen, remembering each process so a test can check that none is left running."""
    seen = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        SpyPopen.seen.append(self)


@pytest.fixture
def spy(monkeypatch):
    SpyPopen.seen = []
    monkeypatch.setattr(postbox_os.subprocess, "Popen", SpyPopen)
    return SpyPopen


def fail_go_write(monkeypatch, exc):
    """Raise `exc` in place of the go byte, the only one-byte b"1" write on a pipe in run_bounded."""
    real = os.write

    def write(fd, data):
        if data == b"1":
            raise exc
        return real(fd, data)

    monkeypatch.setattr(postbox_os.os, "write", write)


DIE_IN_ATTACH_WITH_PASS_FDS = """
postbox_os._attach_helper_process = lambda *a, **k: os._exit(9)
r, w = os.pipe()
postbox_os.run_bounded(json.loads(sys.argv[2]), helper_dir=sys.argv[1], helper_kind=sys.argv[3], pass_fds=(r,), timeout=30)
"""

DIE_AT_GO_WITH_PASS_FDS = """
real_write = os.write
def write(fd, data):
    if data == b"1":
        os._exit(9)
    return real_write(fd, data)
os.write = write
r, w = os.pipe()
postbox_os.run_bounded(json.loads(sys.argv[2]), helper_dir=sys.argv[1], helper_kind=sys.argv[3], pass_fds=(r,), timeout=30)
"""


# The three crash boundaries -------------------------------------------------------------------------------------------

DIE_IN_ATTACH = """
postbox_os._attach_helper_process = lambda *a, **k: os._exit(9)
argv = json.loads(sys.argv[2])
postbox_os.run_bounded(argv, helper_dir=sys.argv[1], helper_kind=sys.argv[3], helper_mutating=sys.argv[3] == 'send_drain', timeout=30)
"""

DIE_AT_GO = """
if sys.argv[4] == 'no_psutil':
    postbox_os._psutil = lambda: None
real_write = os.write
def write(fd, data):
    if data == b"1":
        print(json.dumps(sorted(os.listdir(sys.argv[1]))), flush=True)
        os._exit(9)                                  # the record is attached; the go byte is never written
    return real_write(fd, data)
os.write = write
argv = json.loads(sys.argv[2])
postbox_os.run_bounded(argv, helper_dir=sys.argv[1], helper_kind=sys.argv[3], helper_mutating=sys.argv[3] == 'send_drain', timeout=30)
"""


def test_death_before_attach_leaves_a_gated_record_and_the_helper_never_runs(tmp_path):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    p = run_parent(DIE_IN_ATTACH, hdir, json.dumps(marker_helper(ran)), "owed")
    assert p.returncode == 9, p.stderr
    (rec,) = records(hdir)
    doc = json.loads(rec.read_text())
    assert doc["state"] == "spawning" and doc["gated"] is True and doc["pid"] is None
    time.sleep(1.0)                                   # far longer than a started helper needs for its marker
    assert not ran.exists()
    s = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")
    assert s["settled"] is True and records(hdir) == []
    assert s["reaped"] == [{"kind": "owed", "pid": None, "state": "spawning_gated_never_ran"}]


@pytest.mark.parametrize("kind", ["owed", "send_drain"])
@pytest.mark.parametrize("psutil_mode", ["psutil", "no_psutil"])
def test_death_after_attach_before_release_the_helper_never_runs_and_the_record_settles(tmp_path, kind, psutil_mode):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    p = run_parent(DIE_AT_GO, hdir, json.dumps(marker_helper(ran)), kind, psutil_mode)
    assert p.returncode == 9, p.stderr
    (rec,) = records(hdir)
    doc = json.loads(rec.read_text())
    assert doc["state"] == "running" and type(doc["pid"]) is int and not doc.get("uncertain")
    assert wait_gone(doc["pid"])                      # EOF on the gate pipe ends the launcher by itself
    assert not ran.exists()
    s = postbox_os.settle_helper_dir(str(hdir), only_kind=kind)
    assert s["settled"] is True and records(hdir) == []
    assert s["reaped"] == [{"kind": kind, "pid": doc["pid"], "state": "already_exited"}]


@pytest.mark.parametrize("body,state", [(DIE_IN_ATTACH_WITH_PASS_FDS, "spawning"), (DIE_AT_GO_WITH_PASS_FDS, "running")],
                         ids=["before-attach", "after-attach"])
def test_pass_fds_gets_the_same_crash_proof_as_any_other_start(tmp_path, body, state):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    p = run_parent(body, hdir, json.dumps(marker_helper(ran)), "owed")
    assert p.returncode == 9, p.stderr
    (rec,) = records(hdir)
    doc = json.loads(rec.read_text())
    assert doc["state"] == state and doc["gated"] is True
    if doc["pid"] is not None:
        assert wait_gone(doc["pid"])
    time.sleep(1.0)                                   # far longer than a started helper needs for its marker
    assert not ran.exists()
    s = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")
    assert s["settled"] is True and records(hdir) == []


def test_a_mutating_record_from_before_attach_stays_held(tmp_path):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    assert run_parent(DIE_IN_ATTACH, hdir, json.dumps(marker_helper(ran)), "send_drain").returncode == 9
    s = postbox_os.settle_helper_dir(str(hdir), only_kind="send_drain")
    assert s["settled"] is False and len(records(hdir)) == 1
    assert any("mutating_helper_spawning_unsettled" in i for i in s["issues"])


# Failures in the daemon that keeps running ---------------------------------------------------------------------------

def test_go_write_failure_ends_the_launcher_and_clears_the_record(tmp_path, monkeypatch, spy):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    fail_go_write(monkeypatch, BrokenPipeError(32, "Broken pipe"))
    before = open_fds()
    with pytest.raises(BrokenPipeError):
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    monkeypatch.undo()
    assert records(hdir) == []
    assert spy.seen and all(p.returncode is not None for p in spy.seen)
    time.sleep(0.5)
    assert not ran.exists()
    assert open_fds() == before


def test_an_interrupt_at_the_go_write_is_treated_as_unknown_and_the_gate_still_shuts(tmp_path, monkeypatch, spy):
    """Interrupted before the byte left: nothing is killed, the closed pipe ends the gate, the record says uncertain."""
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    monkeypatch.setattr(postbox_os, "_GATE_SRC", "import os, sys, time\ntime.sleep(0.5)\n"
                                                 "os._exit(125 if os.read(int(sys.argv[1]), 1) != b'1' else 0)\n")
    fail_go_write(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    monkeypatch.undo()
    (rec,) = records(hdir)
    assert json.loads(rec.read_text())["uncertain"] is True
    assert spy.seen[-1].wait(timeout=10) == postbox_os.GATE_EXIT and not ran.exists()
    s = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")
    assert s["settled"] is True and records(hdir) == []


def test_an_interrupt_just_after_the_go_byte_never_kills_the_released_helper(tmp_path, monkeypatch, spy):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    real = os.write

    def write_then_interrupt(fd, data):
        n = real(fd, data)
        if data == b"1":
            raise KeyboardInterrupt()
        return n

    monkeypatch.setattr(postbox_os.os, "write", write_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        postbox_os.run_bounded(marker_helper(ran, 2.0), helper_dir=str(hdir), helper_kind="send_drain",
                               helper_mutating=True, timeout=20)
    monkeypatch.undo()
    proc = spy.seen[-1]
    assert proc.poll() is None                        # released: it goes on, as it would without the gate
    (rec,) = records(hdir)
    doc = json.loads(rec.read_text())
    assert doc["state"] == "running" and doc["uncertain"] is True and doc["pid"] == proc.pid
    end = time.time() + 10
    while not ran.exists() and time.time() < end:
        time.sleep(0.02)
    assert ran.exists()
    proc.wait(timeout=10)


def test_launcher_that_really_exited_gives_a_real_broken_pipe_and_no_stale_record(tmp_path, monkeypatch, spy):
    """The admin's case with no injected fault: the gate is gone before the go byte, so the write gets a real EPIPE."""
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    gone = tmp_path / "read-end-closed"
    monkeypatch.setattr(postbox_os, "_GATE_SRC", "import os, sys\nos.close(int(sys.argv[1]))\nopen(%r, 'w').close()\n"
                                                 "os._exit(7)\n" % str(gone))
    real_attach, seen = postbox_os._attach_helper_process, []

    def attach_then_wait_for_the_read_end(rec_path, rec, **kw):
        out = real_attach(rec_path, rec, **kw)
        end = time.time() + 10
        while not gone.exists() and time.time() < end:
            time.sleep(0.01)
        seen.append(json.loads(open(rec_path).read()))
        return out

    monkeypatch.setattr(postbox_os, "_attach_helper_process", attach_then_wait_for_the_read_end)
    with pytest.raises(BrokenPipeError):
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    assert seen and seen[-1]["state"] == "running" and gone.exists()
    assert records(hdir) == [] and not ran.exists()
    assert spy.seen[-1].returncode is not None


def test_a_launcher_that_survives_cleanup_leaves_an_uncertain_record_the_same_daemon_settles(tmp_path, monkeypatch):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    monkeypatch.setattr(postbox_os, "_GATE_SRC", "import time\ntime.sleep(8)\n")     # ignores EOF, ends by itself

    class Unkillable(subprocess.Popen):
        def kill(self):
            pass

        def wait(self, timeout=None):
            if timeout is not None and timeout <= 2:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return super().wait(timeout=timeout)

    monkeypatch.setattr(postbox_os.subprocess, "Popen", Unkillable)
    monkeypatch.setattr(postbox_os, "terminate_owned_tree", lambda *a, **k: False)
    monkeypatch.setattr(postbox_os, "_cleanup_session_group", lambda *a, **k: None)
    fail_go_write(monkeypatch, BrokenPipeError(32, "Broken pipe"))
    with pytest.raises(BrokenPipeError):
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    monkeypatch.undo()
    (rec,) = records(hdir)
    doc = json.loads(rec.read_text())
    assert doc["state"] == "running" and doc["uncertain"] is True and doc["daemon_pid"] == os.getpid()
    s = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")   # not skipped as this daemon's own running helper
    assert s["settled"] is True and records(hdir) == []
    assert s["reaped"] == [{"kind": "owed", "pid": doc["pid"], "state": "terminated"}]
    assert not ran.exists()


def test_a_failed_uncertain_write_still_keeps_the_record_from_reading_as_healthy(tmp_path, monkeypatch):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    monkeypatch.setattr(postbox_os, "_GATE_SRC", "import time\ntime.sleep(8)\n")

    class Unkillable(subprocess.Popen):
        def kill(self):
            pass

        def wait(self, timeout=None):
            if timeout is not None and timeout <= 2:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return super().wait(timeout=timeout)

    real_write = postbox_os.write_private_json_atomic

    def full_disk_on_uncertain(path, doc, **kw):
        if doc.get("uncertain"):
            raise OSError(28, "No space left on device")
        return real_write(path, doc, **kw)

    monkeypatch.setattr(postbox_os.subprocess, "Popen", Unkillable)
    monkeypatch.setattr(postbox_os, "terminate_owned_tree", lambda *a, **k: False)
    monkeypatch.setattr(postbox_os, "_cleanup_session_group", lambda *a, **k: None)
    monkeypatch.setattr(postbox_os, "write_private_json_atomic", full_disk_on_uncertain)
    fail_go_write(monkeypatch, BrokenPipeError(32, "Broken pipe"))
    with pytest.raises(BrokenPipeError):
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    monkeypatch.undo()
    (rec,) = records(hdir)
    doc = json.loads(rec.read_text())
    assert doc["state"] == "running" and "uncertain" not in doc and doc["daemon_pid"] == os.getpid()
    other = tmp_path / "other.helpers"
    other.mkdir()
    postbox_os.settle_helper_dir(str(other))          # settling another folder must not forget this record
    assert str(rec) in postbox_os._UNCERTAIN_UNWRITTEN
    s = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")   # still not skipped as this daemon's own healthy helper
    assert s["reaped"] == [{"kind": "owed", "pid": doc["pid"], "state": "terminated"}] and records(hdir) == []
    assert postbox_os._UNCERTAIN_UNWRITTEN == set()
    assert not ran.exists()


class ExitedProc:
    """A helper that has exited; what is left is its group."""
    pid = 1

    def poll(self):
        return 0


@pytest.mark.parametrize("where", ["_group_has_live_member", "_pgid_members"])
def test_an_interrupt_inside_finalize_still_keeps_the_record_from_reading_as_healthy(tmp_path, monkeypatch, where):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()
    rec_path = hdir / "h-owed-1-1.json"
    pid = dead_pid()
    rec = {"schema": 1, "state": "running", "kind": "owed", "mutating": False, "gated": True, "pid": pid, "pgid": pid,
           "create_time": 1.0, "create_time_known": True, "daemon_pid": os.getpid(), "daemon_create_time": 1.0,
           "started_at": 1.0, "timeout_s": 30.0}
    rec_path.write_text(json.dumps(rec))

    def interrupted(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(postbox_os, "_group_has_live_member", interrupted if where == "_group_has_live_member" else lambda *a: True)
    monkeypatch.setattr(postbox_os, "process_create_time", lambda *a: None)
    monkeypatch.setattr(postbox_os, "_pgid_members", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            postbox_os._finalize_helper_record(str(rec_path), rec, ExitedProc(), True)
        monkeypatch.undo()
        assert str(rec_path) in postbox_os._UNCERTAIN_UNWRITTEN and "uncertain" not in json.loads(rec_path.read_text())
        s = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")      # looked at, not skipped as a healthy helper
        assert s["reaped"] == [{"kind": "owed", "pid": pid, "state": "already_exited"}] and records(hdir) == []
    finally:
        postbox_os._UNCERTAIN_UNWRITTEN.clear()


def test_a_written_uncertain_mark_leaves_nothing_in_memory(tmp_path, monkeypatch):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()
    rec_path = hdir / "h-owed-1-1.json"
    rec = {"schema": 1, "state": "running", "kind": "owed", "pid": 1, "pgid": None}
    rec_path.write_text(json.dumps(rec))

    class Alive:
        def poll(self):
            return None

    postbox_os._finalize_helper_record(str(rec_path), rec, Alive(), False)
    assert json.loads(rec_path.read_text())["uncertain"] is True and postbox_os._UNCERTAIN_UNWRITTEN == set()


def test_an_interrupt_while_the_gate_closes_still_finalizes_the_record(tmp_path, monkeypatch):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    real_close, state = os.close, {"armed": False, "fired": False}
    real_finalize = postbox_os._finalize_helper_record
    finalized = []

    def close(fd):
        if state["armed"] and not state["fired"]:
            state["fired"] = True
            real_close(fd)
            raise KeyboardInterrupt()
        real_close(fd)

    real_write = os.write

    def write(fd, data):
        n = real_write(fd, data)
        if data == b"1":
            state["armed"] = True                     # the next close in run_bounded is the one in its finally
        return n

    monkeypatch.setattr(postbox_os.os, "close", close)
    monkeypatch.setattr(postbox_os.os, "write", write)
    monkeypatch.setattr(postbox_os, "_finalize_helper_record", lambda *a: (finalized.append(a[0]), real_finalize(*a)))
    with pytest.raises(KeyboardInterrupt):
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    monkeypatch.undo()
    assert state["fired"] and len(finalized) == 1 and records(hdir) == []


def test_the_uncertain_fallback_is_shared_by_both_import_names():
    code = ("import sys\nsys.path.insert(0, %r)\nsys.path.insert(0, %r)\n"
            "import postbox_os\nfrom tools.postbox import postbox_os as other\n"
            "assert postbox_os is not other\n"
            "assert postbox_os._UNCERTAIN_UNWRITTEN is other._UNCERTAIN_UNWRITTEN\n" % (os.path.dirname(os.path.dirname(MOD)), MOD))
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
    assert p.returncode == 0, p.stderr


def test_an_interrupt_right_after_popen_is_inside_the_guard(tmp_path, monkeypatch, spy):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()

    def interrupted(_pid):
        raise KeyboardInterrupt()

    monkeypatch.setattr(postbox_os.os, "getpgid", interrupted)
    before = open_fds()
    with pytest.raises(KeyboardInterrupt):
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    monkeypatch.undo()
    assert records(hdir) == [] and all(p.returncode is not None for p in spy.seen)
    time.sleep(0.5)
    assert not ran.exists() and open_fds() == before


def test_popen_failure_clears_the_record_and_the_gate_pipes(tmp_path, monkeypatch):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()

    def boom(*a, **k):
        assert len(records(hdir)) == 1
        raise OSError("exec failed before fork completed")

    monkeypatch.setattr(postbox_os.subprocess, "Popen", boom)
    before = open_fds()
    with pytest.raises(OSError, match="exec failed"):
        postbox_os.run_bounded(["nope"], helper_dir=str(hdir), helper_kind="send_drain", helper_mutating=True)
    assert records(hdir) == [] and open_fds() == before


def test_an_unknown_liveness_answer_keeps_the_record_and_marks_it_uncertain(tmp_path, monkeypatch):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()
    real = postbox_os._group_has_live_member

    def boom(pgid):
        raise RuntimeError("no answer")

    monkeypatch.setattr(postbox_os, "_group_has_live_member", boom)
    postbox_os.run_bounded([PY, "-I", "-c", "pass"], helper_dir=str(hdir), helper_kind="owed", timeout=20)
    monkeypatch.setattr(postbox_os, "_group_has_live_member", real)
    (rec,) = records(hdir)
    assert json.loads(rec.read_text())["uncertain"] is True


def test_attach_failure_ends_the_launcher_and_the_helper_never_runs(tmp_path, monkeypatch, spy):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(postbox_os, "_attach_helper_process", boom)
    before = open_fds()
    with pytest.raises(OSError, match="No space"):
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    assert records(hdir) == [] and all(p.returncode is not None for p in spy.seen)
    time.sleep(0.5)
    assert not ran.exists() and open_fds() == before


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_drain_thread_start_failure_happens_before_release(tmp_path, monkeypatch, spy, fail_at):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    real_thread, real_write, count, go_bytes = threading.Thread, os.write, [], []

    class Limited(real_thread):
        def start(self):
            count.append(1)
            if len(count) == fail_at:
                assert go_bytes == []                 # the failure must land while the gate is still shut
                raise RuntimeError("can't start new thread")
            super().start()

    def write(fd, data):
        if data == b"1":
            go_bytes.append(fd)
        return real_write(fd, data)

    monkeypatch.setattr(postbox_os.threading, "Thread", Limited)
    monkeypatch.setattr(postbox_os.os, "write", write)
    before = open_fds()
    with pytest.raises(RuntimeError) as held:         # the held traceback keeps every local alive: nothing is left to GC
        postbox_os.run_bounded(marker_helper(ran), helper_dir=str(hdir), helper_kind="owed", timeout=20)
    monkeypatch.undo()
    assert records(hdir) == [] and all(p.returncode is not None for p in spy.seen)
    time.sleep(0.5)
    assert not ran.exists()
    time.sleep(0.3)                                   # a started drain closes its own pipe when it sees EOF
    assert open_fds() == before
    assert held.value is not None


def test_timeout_after_release_ends_the_helper_and_clears_the_record(tmp_path):
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    with pytest.raises(subprocess.TimeoutExpired):
        postbox_os.run_bounded(marker_helper(ran, 30.0), helper_dir=str(hdir), helper_kind="owed", timeout=1.0)
    assert ran.exists() and records(hdir) == []


# The helper sees what a direct Popen would give it -------------------------------------------------------------------

def both(tmp_path, argv, **kw):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir(exist_ok=True)
    direct = postbox_os.run_bounded(argv, text=True, timeout=20, **kw)
    gated = postbox_os.run_bounded(argv, text=True, timeout=20, helper_dir=str(hdir), helper_kind="owed", **kw)
    assert records(hdir) == []
    return direct, gated


def test_a_normal_helper_keeps_the_recorded_pid_and_its_output(tmp_path, monkeypatch):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()
    real, pids = postbox_os._attach_helper_process, []

    def attach(rec_path, rec, **kw):
        pids.append(kw["pid"])
        return real(rec_path, rec, **kw)

    monkeypatch.setattr(postbox_os, "_attach_helper_process", attach)
    r = postbox_os.run_bounded([PY, "-I", "-c", "import os; print(os.getpid())"], helper_dir=str(hdir),
                               helper_kind="owed", timeout=20, text=True)
    assert r.returncode == 0 and int(r.stdout) == pids[-1] and records(hdir) == []


@pytest.mark.parametrize("env", [
    {"PATH": "/usr/bin:/bin"},
    {"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    {"PATH": "/usr/bin:/bin", "LC_CTYPE": "C"},
    {"PATH": "/usr/bin:/bin", "LC_CTYPE": "en_US.UTF-8", "__CF_USER_TEXT_ENCODING": "0x0:0x0:0x0"},
    {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "SECRET_LOOKING": "kept as given"},
], ids=["bare", "lc_all", "lc_ctype_c", "lc_set", "lang"])
def test_environment_is_exactly_the_callers(tmp_path, env):
    direct, gated = both(tmp_path, ["/usr/bin/env"], env=env)
    assert sorted(gated.stdout.split("\n")) == sorted(direct.stdout.split("\n"))


def test_inherited_environment_matches_a_direct_start(tmp_path):
    direct, gated = both(tmp_path, ["/usr/bin/env"])
    assert sorted(gated.stdout.split("\n")) == sorted(direct.stdout.split("\n"))


def test_sigpipe_is_default_for_the_helper(tmp_path):
    direct, gated = both(tmp_path, ["/bin/sh", "-c", "yes | head -1"], env={"PATH": "/usr/bin:/bin"})
    assert direct.stderr == "" and gated.stderr == "" and gated.stdout == direct.stdout == "y\n"


def test_sigxfsz_is_default_for_the_helper(tmp_path):
    big = tmp_path / "big"
    script = "ulimit -f 1; head -c 2000 /dev/zero > %s; echo rc=$?" % big
    direct, gated = both(tmp_path, ["/bin/sh", "-c", script], env={"PATH": "/usr/bin:/bin"})
    assert gated.stdout == direct.stdout and gated.returncode == direct.returncode


def test_the_gate_leaves_the_helper_no_extra_descriptor(tmp_path):
    direct, gated = both(tmp_path, ["/bin/ls", "/dev/fd"], stdin=subprocess.DEVNULL)
    assert gated.stdout == direct.stdout


def test_cwd_stdin_and_exit_codes_pass_through(tmp_path):
    argv = ["/bin/sh", "-c", "pwd; cat; exit 37"]
    direct, gated = both(tmp_path, argv, cwd=str(tmp_path), stdin=subprocess.DEVNULL)
    assert (gated.returncode, gated.stdout) == (direct.returncode, direct.stdout) == (37, direct.stdout)


def test_caller_pass_fds_stay_with_the_gate(tmp_path):
    r, w = os.pipe()
    try:
        os.write(w, b"from the caller")
        os.close(w)
        code = "import os, sys; print(os.read(int(sys.argv[1]), 100).decode())"
        hdir = tmp_path / "log.helpers"
        hdir.mkdir()
        res = postbox_os.run_bounded([PY, "-I", "-c", code, str(r)], helper_dir=str(hdir), helper_kind="owed",
                                     pass_fds=(r,), timeout=20, text=True)
        assert res.returncode == 0 and res.stdout == "from the caller\n"
    finally:
        os.close(r)


@pytest.mark.parametrize("argv,etype", [
    (["/nonexistent/helper"], FileNotFoundError),
    (["no-such-helper-on-path"], FileNotFoundError),
    (["/etc/hosts"], PermissionError),
])
def test_exec_failure_raises_what_popen_raises(tmp_path, argv, etype):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()
    with pytest.raises(etype) as direct:
        postbox_os.run_bounded(argv, timeout=20)
    with pytest.raises(etype) as gated:
        postbox_os.run_bounded(argv, helper_dir=str(hdir), helper_kind="send_drain", helper_mutating=True, timeout=20)
    assert str(gated.value) == str(direct.value) and records(hdir) == []


class FalsyCallable:
    def __bool__(self):
        return False

    def __call__(self):
        raise AssertionError("ran before the release")


def test_empty_argv_raises_what_popen_raises(tmp_path):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()
    with pytest.raises(IndexError):
        subprocess.Popen([])
    with pytest.raises(IndexError, match="recorded helper"):
        postbox_os.run_bounded([], helper_dir=str(hdir), helper_kind="owed", timeout=5)
    assert list(hdir.iterdir()) == []


@pytest.mark.parametrize("argv,kw,etype", [
    ("echo hi", {"shell": True}, TypeError),
    (["echo hi"], {"shell": True}, ValueError),
    (["/bin/sh", "-c", "true"], {"executable": "/bin/sh"}, ValueError),
    (["/usr/bin/true"], {"preexec_fn": lambda: None}, ValueError),
    (["/usr/bin/true"], {"close_fds": False}, ValueError),
    (["/usr/bin/true"], {"restore_signals": False}, ValueError),
    ("/usr/bin/true", {}, TypeError),
    (["/bin/sh", "-c", "true"], {"executable": ""}, ValueError),
    (["/usr/bin/true"], {"preexec_fn": FalsyCallable()}, ValueError),
    (["/usr/bin/true"], {"close_fds": 0}, ValueError),
    (["/usr/bin/true"], {"restore_signals": 0}, ValueError),
], ids=["shell-str", "shell-list", "executable", "preexec_fn", "close_fds", "restore_signals", "str-argv",
        "executable-empty", "preexec_fn-falsy", "close_fds-0", "restore_signals-0"])
def test_modes_the_gate_cannot_carry_are_refused_before_any_record(tmp_path, monkeypatch, argv, kw, etype):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()

    def no_popen(*a, **k):
        raise AssertionError("Popen reached")

    monkeypatch.setattr(postbox_os.subprocess, "Popen", no_popen)
    with pytest.raises(etype, match="recorded helper"):
        postbox_os.run_bounded(argv, helper_dir=str(hdir), helper_kind="owed", timeout=5, **kw)
    assert list(hdir.iterdir()) == []


def test_those_modes_are_untouched_without_a_record():
    assert postbox_os.run_bounded("echo hi", shell=True, text=True, timeout=20).stdout == "hi\n"


def test_windows_style_start_has_no_gate_and_its_dead_record_is_abandoned(tmp_path):
    """No gate: the helper does run after the crash, so the record proves nothing and says so with gated None. A read
    helper's record is still dropped, because holding it would stop that kind's lookups for good."""
    hdir, ran = tmp_path / "log.helpers", tmp_path / "ran"
    hdir.mkdir()
    body = "postbox_os.IS_WINDOWS = True\n" + DIE_IN_ATTACH
    assert run_parent(body, hdir, json.dumps(marker_helper(ran)), "owed").returncode == 9
    (rec,) = records(hdir)
    assert json.loads(rec.read_text())["gated"] is None
    end = time.time() + 5
    while not ran.exists() and time.time() < end:
        time.sleep(0.02)
    assert ran.exists()
    s = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")
    assert s["settled"] is True and records(hdir) == []
    assert s["reaped"] == [{"kind": "owed", "pid": None, "state": "spawning_abandoned"}]


# Settlement results reach the callers ---------------------------------------------------------------------------------

def dead_pid():
    p = subprocess.Popen([PY, "-I", "-c", "pass"])
    p.wait()
    return p.pid


def test_an_ungated_pidless_record_is_kept_and_reported(tmp_path):
    hdir = tmp_path / "log.helpers"
    hdir.mkdir()
    pid = dead_pid()
    rec = hdir / ("h-owed-%d-1.json" % pid)
    rec.write_text(json.dumps({"schema": 1, "state": "spawning", "kind": "owed", "mutating": False, "pid": None, "pgid": None,
                               "create_time": None, "create_time_known": False, "daemon_pid": pid, "daemon_create_time": 1.0,
                               "started_at": time.time(), "timeout_s": 30.0}))
    s = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")
    assert s["settled"] is False and rec.exists()
    assert s["issues"] == ["helper_spawning_unsettled:owed:%s" % rec.name]


def unverifiable_records(tmp_path):
    hdir = tmp_path / "log.jsonl.helpers"
    hdir.mkdir()
    pid, sleeper = dead_pid(), subprocess.Popen([PY, "-I", "-c", "import time; time.sleep(4)"])
    for kind in ("owed", "neighbours"):
        (hdir / ("h-%s-%d-2.json" % (kind, pid))).write_text(json.dumps({
            "schema": 1, "state": "running", "kind": kind, "mutating": False, "pid": sleeper.pid, "pgid": None,
            "create_time": None, "create_time_known": False, "daemon_pid": pid, "daemon_create_time": 1.0,
            "started_at": time.time(), "timeout_s": 30.0}))
    return hdir, sleeper


def test_a_record_that_ends_during_the_scan_is_not_corruption_and_refuses_no_lookup(tmp_path, monkeypatch):
    hdir = tmp_path / "log.jsonl.helpers"
    hdir.mkdir()
    rec = hdir / "h-owed-1-1.json"
    rec.write_text("{}")
    real_read = postbox_os.read_bounded_json

    def finalized_first(path, **kw):
        os.unlink(path)                               # another thread's finalizer gets there between the scan and the read
        return real_read(path, **kw)

    monkeypatch.setattr(postbox_os, "read_bounded_json", finalized_first)
    assert postbox_os.settle_helper_dir(str(hdir), only_kind="owed") == {
        "settled": True, "active_mutating": [], "issues": [], "reaped": []}
    rec.write_text("{}")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"owed": []}', "")

    ident = {"name": "t", "python": PY, "base_url": "http://127.0.0.1:9", "state": str(tmp_path / "s.json"),
             "log": str(tmp_path / "log.jsonl"), "client": str(tmp_path)}
    owedline._call(ident, "2026-11-03T00:00:00Z", fake_run)
    assert len(calls) == 1


def test_owedline_and_graphline_start_no_lookup_over_an_unsettled_one(tmp_path):
    hdir, sleeper = unverifiable_records(tmp_path)
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"owed": [], "hits": []}', "")

    ident = {"name": "t", "python": PY, "base_url": "http://127.0.0.1:9", "state": str(tmp_path / "s.json"),
             "log": str(tmp_path / "log.jsonl"), "client": str(tmp_path)}
    try:
        assert owedline._call(ident, "2026-11-03T00:00:00Z", fake_run) is None
        assert graphline._lookup(ident, "msg/1-x-1", fake_run) == (None, "an earlier lookup is not settled")
        assert calls == [] and sleeper.poll() is None and len(records(hdir)) == 2
    finally:
        sleeper.wait()


def test_settle_aux_helpers_returns_and_records_the_unsettled_kinds(tmp_path, monkeypatch):
    hdir = tmp_path / "log.jsonl.helpers"
    hdir.mkdir()
    (hdir / ("h-owed-%d-3.json" % dead_pid())).write_text("{not json")
    seen = []
    monkeypatch.setattr(postbox, "set_status", lambda name, **kw: seen.append(kw))
    got = postbox.settle_aux_helpers({"name": "t", "log": str(tmp_path / "log.jsonl")})
    assert list(got) == ["owed"] and "corrupt_helper_record" in got["owed"][0]
    assert seen == [{"aux_helpers_unsettled": got}]


def test_settle_aux_helpers_also_surfaces_a_kept_reconcile_intent(tmp_path, monkeypatch):
    hdir = tmp_path / "log.jsonl.helpers"
    hdir.mkdir()
    name = "h-reconcile-%d-3.json" % dead_pid()
    (hdir / name).write_text(json.dumps({"schema": 1, "state": "spawning", "kind": "reconcile", "mutating": False, "pid": None,
                                         "daemon_pid": dead_pid(), "daemon_create_time": 1.0, "started_at": 1.0}))
    seen = []
    monkeypatch.setattr(postbox, "set_status", lambda name, **kw: seen.append(kw))
    got = postbox.settle_aux_helpers({"name": "t", "log": str(tmp_path / "log.jsonl")})
    assert got == {"reconcile": ["helper_spawning_unsettled:reconcile:%s" % name]} and seen == [{"aux_helpers_unsettled": got}]
    assert (hdir / name).exists()


def test_settle_aux_helpers_without_a_log_returns_an_empty_result():
    assert postbox.settle_aux_helpers({"name": "t"}) == {}
