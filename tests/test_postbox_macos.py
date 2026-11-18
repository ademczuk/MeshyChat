"""tools/postbox on macOS: the OS seams (postbox_os.py) and the listener shim (postbox_child.py).

2026-09-28: two seats joined production on Macs (pxx, oxxxxxx), and the postbox, its Claude channel
and the waiter were Windows-only (msvcrt imported at module level, PowerShell read commands, a
Windows job object tying listeners to the daemon). The author's three conditions (postbox_os.py):
(a) Windows behaviour unchanged; (b) on POSIX a live daemon is found in the process table, never by
taking its lock; (c) on POSIX a listener dies with its daemon, however the daemon dies.

These run on the Windows gate. The POSIX-only branches are exercised where they can be (a POSIX
command is plain text; the shim is tested for real on this OS); /dev/null detection is POSIX-only.

Run:  .venv/Scripts/python.exe -m pytest tests/test_postbox_macos.py -q -p no:cacheprovider
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
SCRIPTS = ("postbox.py", "meshy_channel.py", "wait_next.py")


def _load(name):
    spec = importlib.util.spec_from_file_location("pbtest_" + name, PB / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pos():
    return _load("postbox_os")


IDENT = {"client": "C:/Users/user/.meshychat/bxxxxx/client adopt", "python": "C:/Py 312/python.exe",
         "base_url": "http://192.168.50.20:6310", "state": "C:/Users/user/.meshychat/prod/x state.json"}
KEY = "msg/1790304389167-kxxx-e5a44a"


# ------------------------------------------------------------------ importable on macOS

@pytest.mark.parametrize("script", SCRIPTS)
def test_no_windows_only_module_imported_at_module_level(script):
    """On macOS `import msvcrt` at the top of a script is an ImportError before anything runs."""
    tree = ast.parse((PB / script).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in top if isinstance(n, ast.ImportFrom)}
    assert not names & {"msvcrt", "ctypes", "winreg"}, (script, names & {"msvcrt", "ctypes", "winreg"})


# ------------------------------------------------------------------ (a) Windows text byte for byte

def test_windows_read_command_is_byte_identical(pos):
    old = ('Set-Location "%s"; & "%s" -m tools.meshy_node read --base-url %s --state-file "%s" --key %s'
           % (IDENT["client"], IDENT["python"], IDENT["base_url"], IDENT["state"], KEY))
    assert pos.read_command(IDENT, KEY, windows=True) == old


def test_windows_send_command_is_byte_identical(pos):
    old = ('Set-Location "%s"; & "%s" -m tools.meshy_node send --base-url %s --state-file "%s" '
           '--to <the read\'s "from"> --thread <the read\'s "thread_id"> --type answer --in-reply-to %s '
           '--body-file <a file holding your reply>'
           % (IDENT["client"], IDENT["python"], IDENT["base_url"], IDENT["state"], KEY))
    assert pos.send_command(IDENT, KEY, windows=True) == old


def test_no_window_flag_per_os(pos):
    assert pos.no_window_flag("nt") == 0x08000000
    assert pos.no_window_flag("posix") == 0              # POSIX Popen refuses a non-zero creationflags


# ------------------------------------------------------------------ POSIX commands a Mac session pastes

def test_posix_read_command_parses_back_to_the_intended_argv(pos):
    cmd = pos.read_command(IDENT, KEY, windows=False)
    assert "Set-Location" not in cmd and "& \"" not in cmd
    toks = shlex.split(cmd)
    assert toks[:3] == ["cd", IDENT["client"], "&&"]
    assert toks[3:] == [IDENT["python"], "-m", "tools.meshy_node", "read", "--base-url", IDENT["base_url"],
                        "--state-file", IDENT["state"], "--key", KEY]


def test_posix_send_command_quotes_paths_and_keeps_placeholders(pos):
    cmd = pos.send_command(IDENT, KEY, windows=False)
    assert cmd.startswith("cd " + shlex.quote(IDENT["client"]) + " && " + shlex.quote(IDENT["python"]))
    assert "--state-file " + shlex.quote(IDENT["state"]) in cmd
    assert "--in-reply-to " + shlex.quote(KEY) in cmd
    assert "<the read's \"from\">" in cmd and "<a file holding your reply>" in cmd


def test_listener_argv_is_unchanged_on_windows_and_shimmed_on_posix(pos, monkeypatch):
    argv = ["py", "-m", "tools.meshy_node", "listen", "--monitor"]
    monkeypatch.setattr(pos, "IS_WINDOWS", True)
    assert pos.listener_argv(argv, "/pb") == argv
    monkeypatch.setattr(pos, "IS_WINDOWS", False)
    wrapped = pos.listener_argv(argv, "/pb")
    assert wrapped[1] == os.path.join("/pb", "postbox_child.py")
    assert wrapped[2] == str(os.getpid()) and wrapped[3] == "--" and wrapped[4:] == argv


# ------------------------------------------------------------------ locks exclude another process

def _probe(path):
    code = ("import sys, os; sys.path.insert(0, %r); import postbox_os as p;"
            "fd = os.open(%r, os.O_RDWR | os.O_CREAT); print(p.try_lock(fd))" % (str(PB), str(path)))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout.strip()


def test_try_lock_excludes_another_process_until_unlocked(pos, tmp_path):
    path = tmp_path / "postbox.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        assert pos.try_lock(fd) is True
        assert _probe(path) == "False"                    # held: another process cannot take it
        pos.unlock(fd)
        assert _probe(path) == "True"                     # released: now it can
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name == "nt", reason="/dev/null detection is the POSIX branch")
def test_posix_devnull_is_detected(pos):
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        assert pos.posix_is_devnull(fd) is True
    finally:
        os.close(fd)
    assert pos.posix_is_devnull(sys.stderr.fileno()) in (False, True)   # a terminal or a pipe: no error


# ------------------------------------------------------------------ (c) the shim: the listener dies with its daemon

def _alive(pid):
    if os.name == "nt":
        import ctypes
        k = ctypes.WinDLL("kernel32")
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259                         # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                                 # a zombie is not alive
        return not os.waitpid(pid, os.WNOHANG)[0]
    except ChildProcessError:
        return True


def _wait_for(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def _fake_daemon(tmp_path, listener_code):
    """A stand-in daemon that starts the shim the way postbox_os.listener_argv does, then sleeps.
    The listener writes its pid to a file so the test can watch it."""
    pidfile = tmp_path / "listener.pid"
    listener = [sys.executable, "-c", listener_code % str(pidfile)]
    daemon_code = textwrap.dedent("""
        import os, subprocess, sys, time
        shim = [sys.executable, %r, str(os.getpid()), "--"] + %r
        p = subprocess.Popen(shim)
        open(%r, "w").write(str(p.pid))
        time.sleep(120)
    """) % (str(PB / "postbox_child.py"), listener, str(tmp_path / "shim.pid"))
    daemon = subprocess.Popen([sys.executable, "-c", daemon_code])
    assert _wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), 20), "listener never started"
    return daemon, int(pidfile.read_text()), int((tmp_path / "shim.pid").read_text())


SLEEPER = "import os, time; open(%r, 'w').write(str(os.getpid())); time.sleep(120)"


def test_shim_kills_the_listener_when_the_daemon_is_killed(tmp_path):
    daemon, listener_pid, shim_pid = _fake_daemon(tmp_path, SLEEPER)
    try:
        assert _alive(listener_pid) and _alive(shim_pid)
        daemon.kill()                                    # the abrupt case: no clean exit, no signal handler
        daemon.wait()
        assert _wait_for(lambda: not _alive(listener_pid), 15), "listener outlived its daemon"
        assert _wait_for(lambda: not _alive(shim_pid), 15), "shim outlived its daemon"
    finally:
        for pid in (listener_pid, shim_pid):
            if _alive(pid):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass


def test_shim_passes_the_listeners_exit_code_through(tmp_path):
    code = textwrap.dedent("""
        import os, subprocess, sys
        r = subprocess.run([sys.executable, %r, str(os.getpid()), "--", sys.executable, "-c", "raise SystemExit(7)"])
        print(r.returncode)
    """) % str(PB / "postbox_child.py")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "7"


def test_shim_starts_nothing_for_a_daemon_that_is_not_its_parent(tmp_path):
    marker = tmp_path / "started"
    listener = [sys.executable, "-c", "open(%r, 'w').write('x')" % str(marker)]
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        if os.name == "nt":
            other.kill(); other.wait()                   # Windows checks liveness: a dead daemon
        rc = subprocess.run([sys.executable, str(PB / "postbox_child.py"), str(other.pid), "--"] + listener,
                            timeout=30).returncode
        assert rc == 3 and not marker.exists()
    finally:
        if other.poll() is None:
            other.kill()
