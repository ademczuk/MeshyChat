"""The postbox's crash handler must survive its own bookkeeping failing, and `--liveness` must not trust status.json.

What happened (2026-10-01, HQB). The disk filled at 10:47:00Z. Two listener threads (dxxxxx-prod, vxxxxxxxxxx-prod) hit
ENOSPC in set_status() from inside their read loop. postbox.guarded() caught that, logged "restarting in 60 s", and then
called set_status() AGAIN inside its except block; that raised the same error, left the handler, and ended the thread for
good. Nothing restarted them. status.json kept saying `listener: running` for a pid that no longer existed, because
set_status() updates its in-memory entry before it writes the file, so the failed write left the stale entry for the
next successful write by another thread to persist. Both identities were served only by the five minute reconcile for
five hours, with every health field green. uxxx found it by reading the process table; dxxxxx found the cause.

Two layers are fixed and tested separately:
  1. note() and set_status() are best effort and never raise (what the crash handler and the read loop call).
  2. guarded() wraps both in quietly(), so even a bookkeeping call that raises something else cannot end the thread.
And `postbox.py --liveness` compares each recorded listener_pid with the process table (and checks the process really is
a `tools.meshy_node listen --monitor`, since a pid is reused), exit 1 when one is not alive. `--status` is unchanged.

Run:  .venv/Scripts/python.exe -m pytest tests/test_postbox_guarded.py -q -p no:cacheprovider --noconftest

Mutants (each must fail at least one test here; the harness that ran them is in the PR description):
  M1 set_status re-raises an OSError  caught by test_set_status_never_raises_on_a_full_disk...
  M2 note re-raises an OSError  caught by test_note_never_raises...
  M3 guarded does not wrap note()  caught by test_guard_survives_both_bookkeeping_calls_failing
  M4 guarded does not wrap set_status() (crash)  caught by test_guard_survives_both_bookkeeping_calls_failing
  M5 guarded never writes the "restarted" state  caught by test_the_status_goes_crashed_then_restarted
  HZ liveness trusts the pid alone (no cmdline)  caught by test_a_live_process_that_is_not_a_listener_is_not_a_listener
  M9 a missing psutil is reported as dead  caught by test_without_psutil_liveness_cannot_check...
  M10 an unreadable pid (AccessDenied) is dead caught by test_an_unreadable_pid_is_unknown_not_dead
  M11 every failed status write is logged      caught by test_a_failing_status_write_is_logged_once_and_so_is_the_recovery
  M12 the recovery is never logged             caught by test_a_failing_status_write_is_logged_once_and_so_is_the_recovery
  M13 unknown outranks dead in the exit code   caught by test_a_dead_listener_outranks_an_unreadable_one
  M7 liveness calls a dead pid alive  caught by test_liveness_flags_a_dead_pid...
  M8 --liveness always exits 0  caught by test_the_cli_exits_one_when_a_listener_is_dead
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"


def _enospc(*_a, **_k):
    raise OSError(28, "No space left on device")


@pytest.fixture
def pb(tmp_path):
    """postbox.py loaded fresh, with its status file and error log pointed into tmp_path."""
    spec = importlib.util.spec_from_file_location("pbtest_guarded_postbox", PB / "postbox.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATUS = str(tmp_path / "status.json")
    mod.ERR = str(tmp_path / "postbox.err.log")
    mod._status.clear()
    return mod


# A small stand-in for psutil, so every liveness test runs WITHOUT it (the gate's Python has none, and the gate refuses a
# skipped test). It models only what listener_alive uses: Process(pid) raises NoSuchProcess for an absent pid, .cmdline()
# raises AccessDenied for a pid this account may not read (as the real library does), .is_running(). The process table is
# TABLE: pid maps to its command line (a list), or "denied". In a child process it is read from the FAKE_PSUTIL_TABLE environment
# variable (JSON), so the real command can be driven end to end.
FAKE_PSUTIL = """
import json, os
class Error(Exception): pass
class NoSuchProcess(Error): pass
class AccessDenied(Error): pass
TABLE = {int(k): v for k, v in json.loads(os.environ.get("FAKE_PSUTIL_TABLE", "{}")).items()}
class Process:
    def __init__(self, pid):
        if pid not in TABLE:
            raise NoSuchProcess(pid)
        self.pid = pid
    def cmdline(self):
        if TABLE[self.pid] == "denied":
            raise AccessDenied(self.pid)
        return list(TABLE[self.pid])
    def is_running(self):
        return True
"""

LISTENER = ["python", "-m", "tools.meshy_node", "listen", "--monitor", "--base-url", "http://x"]
ABSENT = 2 ** 22 + 4242                                    # a pid that is in no table


@pytest.fixture
def fake_psutil(monkeypatch):
    """Install the fake as `psutil` for this test and return its process table (a dict the test fills)."""
    mod = types.ModuleType("psutil")
    exec(FAKE_PSUTIL, mod.__dict__)
    monkeypatch.setitem(sys.modules, "psutil", mod)
    return mod.TABLE


def _fake_dir(tmp_path):
    d = tmp_path / "fakepsutil"
    d.mkdir(exist_ok=True)
    (d / "psutil.py").write_text(FAKE_PSUTIL, encoding="utf-8")
    return d


# ------------------------------------------------------------------------------------------ layer 1: best effort

def test_set_status_never_raises_on_a_full_disk_and_the_next_write_persists_it(pb, monkeypatch):
    """The call that started the incident. A failed write returns False and keeps the entry in memory; the next write that
    succeeds puts BOTH updates in the file. CONTROL: the same call writes the file when the disk is fine."""
    assert pb.set_status("x", listener="running", listener_pid=111) is True
    assert json.loads(Path(pb.STATUS).read_text())["x"]["listener_pid"] == 111

    monkeypatch.setattr(pb.os, "replace", _enospc)                      # ENOSPC at the final step of the write
    assert pb.set_status("x", last_message="msg/1") is False            # did not raise
    assert "28" in pb._status_write_error
    assert pb._status["x"]["last_message"] == "msg/1"                   # still in memory
    assert "last_message" not in json.loads(Path(pb.STATUS).read_text())["x"]   # not on disk yet

    monkeypatch.undo()
    assert pb.set_status("y", listener="running") is True               # a later write, by anything
    on_disk = json.loads(Path(pb.STATUS).read_text())
    assert on_disk["x"]["last_message"] == "msg/1" and "y" in on_disk
    assert pb._status_write_error is None


def test_set_status_never_raises_when_the_file_cannot_even_be_opened(pb, tmp_path):
    pb.STATUS = str(tmp_path / "no" / "such" / "dir" / "status.json")
    assert pb.set_status("x", listener="running") is False
    assert pb._status["x"]["listener"] == "running"


def test_note_never_raises_when_the_log_cannot_be_written(pb, tmp_path):
    """CONTROL: it does write when it can."""
    assert pb.note("hello") is True and "hello" in Path(pb.ERR).read_text()
    pb.ERR = str(tmp_path / "no" / "such" / "dir" / "postbox.err.log")
    assert pb.note("lost") is False


# ------------------------------------------------------------------------------------------ layer 2: the guard

def test_guard_survives_both_bookkeeping_calls_failing(pb):
    """The incident, exactly: fn raises ENOSPC, and note() and set_status() raise ENOSPC too. The thread must restart fn."""
    pb.note = _enospc
    pb.set_status = _enospc
    calls = []

    def fn(_ident):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")

    pb.guarded(fn, {"name": "t"}, pause_s=0, sleep=lambda _s: None)
    assert len(calls) == 2, "guarded() let a bookkeeping error end the thread, so fn never restarted"


def test_the_status_goes_crashed_then_restarted(pb):
    """CONTROL for the test above, with a healthy disk: the record says crashed, then restarted, and the log says why."""
    calls = []

    def fn(_ident):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    slept = []
    pb.guarded(fn, {"name": "t"}, pause_s=7, sleep=slept.append)
    assert slept == [7] and len(calls) == 2
    assert pb._status["t"]["fn"] == "restarted"
    log = Path(pb.ERR).read_text()
    assert "t: fn crashed, restarting in 7 s" in log and "RuntimeError: boom" in log


def test_the_status_records_crashed_before_the_pause(pb):
    """Between the crash and the restart the record must say crashed (it is what a person reads during the pause)."""
    seen = []
    pb.set_status = lambda name, **kw: seen.append(kw.get("fn"))

    def fn(_ident):
        raise RuntimeError("boom")

    def stop_after_first_pause(_s):
        raise SystemExit                                              # end the for-ever loop after the first pause

    with pytest.raises(SystemExit):
        pb.guarded(fn, {"name": "t"}, pause_s=1, sleep=stop_after_first_pause)
    assert seen == ["crashed"]


# ------------------------------------------------------------------------------------------ --liveness

def test_liveness_flags_a_dead_pid_and_passes_a_live_listener(pb, fake_psutil):
    """CONTROL: a live process that looks like the listener is alive, so a dead one reads as NOT ALIVE for a reason."""
    fake_psutil[101] = LISTENER
    rows = {n: (ok, line) for n, ok, line in pb.liveness({
        "live": {"listener": "running", "listener_pid": 101},
        "dead": {"listener": "running", "listener_pid": ABSENT, "run_identity": "crashed"},
        "idle": {"listener": "exited"},
    })}
    assert rows["live"][0] is True and "alive" in rows["live"][1] and "NOT ALIVE" not in rows["live"][1]
    assert rows["dead"][0] is False and "NOT ALIVE" in rows["dead"][1] and "run_identity crashed" in rows["dead"][1]
    assert rows["idle"][0] is True                                      # claims nothing running: nothing to compare


def test_a_live_process_that_is_not_a_listener_is_not_a_listener(pb, fake_psutil):
    """A pid is reused: a recorded pid that now belongs to some other live process must not read as alive. Each marker
    of the listener's command line decides on its own."""
    fake_psutil[201] = ["python", "-c", "import time"]                  # alive, but no listener markers
    fake_psutil[202] = ["python", "-m", "tools.meshy_node", "listen", "--base-url", "http://x"]       # no --monitor
    fake_psutil[203] = ["python", "-m", "tools.meshy_node", "send", "--monitor"]                      # not `listen`
    fake_psutil[204] = LISTENER
    assert pb.listener_alive(201) is False and pb.listener_alive(202) is False and pb.listener_alive(203) is False
    assert pb.listener_alive(204) is True                               # CONTROL: the markers are what decides


def test_without_psutil_liveness_cannot_check_and_says_so_instead_of_calling_everything_dead(tmp_path):
    """Runs everywhere (psutil is made unimportable). A missing psutil is 'cannot check' (exit 2, said in words), never
    'every listener is NOT ALIVE'. CONTROL: the same file with no --liveness flag prints status.json as usual."""
    for name in ("postbox.py", "postbox_os.py", "delivery_bank.py"):
        shutil.copy(PB / name, tmp_path / name)
    (tmp_path / "status.json").write_text(json.dumps({"a": {"listener": "running", "listener_pid": 4}}), encoding="utf-8")
    script = str(tmp_path / "postbox.py")
    code = ("import sys, runpy; sys.modules['psutil'] = None; sys.argv = [%r, '--liveness']; "
            "runpy.run_path(%r, run_name='__main__')" % (script, script))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "cannot check liveness" in r.stdout and "NOT ALIVE" not in r.stdout


def test_a_failing_status_write_is_logged_once_and_so_is_the_recovery(pb, monkeypatch):
    """_status_write_error is the failure STREAK: the first failure is logged, the following ones are not (a full disk
    would otherwise flood the log that is also on the full disk), and the end of the streak is logged once.
    CONTROL: a healthy write logs nothing."""
    assert pb.set_status("x", listener="running") is True
    assert not Path(pb.ERR).exists()

    monkeypatch.setattr(pb.os, "replace", _enospc)
    for _ in range(3):
        assert pb.set_status("x", n=1) is False
    log = Path(pb.ERR).read_text()
    assert log.count("status.json write failed") == 1 and "28" in log

    monkeypatch.undo()
    assert pb.set_status("x", n=2) is True and pb.set_status("x", n=3) is True
    log = Path(pb.ERR).read_text()
    assert log.count("status.json writes work again") == 1
    assert pb._status_write_error is None


def test_an_unreadable_pid_is_unknown_not_dead(pb, fake_psutil):
    """psutil.AccessDenied means 'it exists and I may not look', which is not 'it is dead'. CONTROL: a plainly absent pid
    is still False."""
    fake_psutil[12345] = "denied"
    assert pb.listener_alive(12345) is None
    rows = pb.liveness({"a": {"listener": "running", "listener_pid": 12345}})
    assert rows[0][1] is None and "UNKNOWN" in rows[0][2] and "NOT ALIVE" not in rows[0][2]
    assert pb.listener_alive(ABSENT) is False                           # no such process


def _run_liveness(pb, monkeypatch, capsys, status):
    Path(pb.STATUS).write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["postbox.py", "--liveness"])
    code = pb.main()
    return code, capsys.readouterr().out


def test_a_dead_listener_outranks_an_unreadable_one(pb, fake_psutil, monkeypatch, capsys):
    """Exit codes: only unreadable pids is 2 ('cannot tell'); one DEAD listener is 1 even next to an unreadable one,
    because a definite problem must not be hidden behind a doubt. CONTROL: all alive is 0."""
    fake_psutil[111] = "denied"
    unreadable = {"u": {"listener": "running", "listener_pid": 111}}
    code, out = _run_liveness(pb, monkeypatch, capsys, unreadable)
    assert code == 2 and "UNKNOWN" in out and "liveness: 1 identities, 0 not alive, 1 unknown" in out

    both = {"u": {"listener": "running", "listener_pid": 111},
            "d": {"listener": "running", "listener_pid": ABSENT}}      # no such process
    code, out = _run_liveness(pb, monkeypatch, capsys, both)
    assert code == 1 and "d: listener running, pid %d NOT ALIVE" % ABSENT in out

    code, out = _run_liveness(pb, monkeypatch, capsys, {"i": {"listener": "exited"}})
    assert code == 0 and "liveness: 1 identities, 0 not alive" in out


def _copy_postbox(tmp_path):
    for name in ("postbox.py", "postbox_os.py", "delivery_bank.py"):
        shutil.copy(PB / name, tmp_path / name)


def _cli(tmp_path, *args, table=None):
    env = dict(os.environ)
    if table is not None:                       # the fake psutil first on the path, whether or not a real one is installed
        env["PYTHONPATH"] = str(_fake_dir(tmp_path)) + os.pathsep + env.get("PYTHONPATH", "")
        env["FAKE_PSUTIL_TABLE"] = json.dumps(table)
    return subprocess.run([sys.executable, str(tmp_path / "postbox.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=60, env=env)


def test_the_cli_exits_one_when_a_listener_is_dead(tmp_path):
    """End to end through the real command, in its own process. CONTROL: exit 0 when every claimed listener is alive."""
    _copy_postbox(tmp_path)
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"a": {"listener": "running", "listener_pid": 101}}), encoding="utf-8")
    ok = _cli(tmp_path, "--liveness", table={"101": LISTENER})
    assert ok.returncode == 0 and "liveness: 1 identities, 0 not alive" in ok.stdout, ok.stdout + ok.stderr

    status.write_text(json.dumps({"a": {"listener": "running", "listener_pid": 101},
                                  "b": {"listener": "running", "listener_pid": ABSENT}}), encoding="utf-8")
    bad = _cli(tmp_path, "--liveness", table={"101": LISTENER})
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "b: listener running, pid %d NOT ALIVE" % ABSENT in bad.stdout
    assert "liveness: 2 identities, 1 not alive" in bad.stdout


def test_status_still_prints_the_file_unchanged(tmp_path):
    """--status is parsed by other tools: adding --liveness must not change a byte of it."""
    _copy_postbox(tmp_path)
    text = json.dumps({"a": {"listener": "running", "listener_pid": 1}}, indent=1)
    (tmp_path / "status.json").write_text(text, encoding="utf-8")
    out = _cli(tmp_path, "--status")
    assert out.returncode == 0 and out.stdout.rstrip("\r\n") == text
