"""tools/backup/run_hidden.py: a scheduled task's command runs with no window and keeps its exit code.

Why it exists is in the file's docstring. What this proves:
  * the command's exit code comes back unchanged (3, and 0 as the control), because the README
    tells the operator to alert on the task's Last Run Result;
  * no command, or --log with no file, is a usage error (2), not a silent success;
  * --log appends stdout and stderr, across runs;
  * on Windows, with the wrapper started DETACHED (no console of its own, as under pythonw.exe
    from Task Scheduler), the command's console has NO window. Without CREATE_NO_WINDOW the
    same command gets a new, visible console there, which is the flash this file exists to stop;
    that direction was proved once by hand (it opens a window, so it is not run here);
  * on Windows, killing the wrapper ends the command, as Stop-ScheduledTask does to the task:
    the command is alive before the kill (the control) and gone within seconds after it. The
    command here is sys.executable, a venv launcher under the repo's venv, and the pid it
    reports is the interpreter the launcher started, so the nested case is the one checked.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUN_HIDDEN = str(ROOT / "tools" / "backup" / "run_hidden.py")


def _wrap(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, RUN_HIDDEN, *args], capture_output=True, text=True,
                          timeout=120, **kw)


def test_exit_code_passes_through():
    assert _wrap(sys.executable, "-c", "raise SystemExit(3)").returncode == 3
    assert _wrap(sys.executable, "-c", "pass").returncode == 0          # control


def test_usage_errors_are_not_success():
    assert _wrap().returncode == 2
    assert _wrap("--log").returncode == 2
    assert _wrap("--log", "x.log").returncode == 2


def test_log_appends_stdout_and_stderr(tmp_path):
    log = tmp_path / "run.log"
    code = "import sys; print('out-line'); print('err-line', file=sys.stderr)"
    for _ in range(2):
        assert _wrap("--log", str(log), sys.executable, "-c", code).returncode == 0
    text = log.read_text()
    assert text.count("out-line") == 2 and text.count("err-line") == 2


@pytest.mark.skipif(os.name != "nt", reason="console windows are a Windows property")
def test_command_gets_no_console_window_even_from_a_consoleless_parent(tmp_path):
    report = tmp_path / "hwnd.txt"
    child = ("import ctypes, sys; open(sys.argv[1], 'w').write("
             "str(ctypes.windll.kernel32.GetConsoleWindow()))")
    r = _wrap(sys.executable, "-c", child, str(report), creationflags=subprocess.DETACHED_PROCESS)
    assert r.returncode == 0, r.stderr
    assert report.read_text() == "0"     # a console with no window; nonzero would be a window


@pytest.mark.skipif(os.name != "nt", reason="job objects are a Windows property")
def test_command_ends_when_the_wrapper_is_killed(tmp_path):
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    pidfile = tmp_path / "pid.txt"
    code = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(120)"
    wrapper = subprocess.Popen([sys.executable, RUN_HIDDEN, sys.executable, "-c", code, str(pidfile)])
    deadline = time.monotonic() + 60
    while not (pidfile.exists() and pidfile.read_text()) and time.monotonic() < deadline:
        time.sleep(0.2)
    pid = int(pidfile.read_text())
    handle = k.OpenProcess(0x00100000 | 0x0001, False, pid)          # SYNCHRONIZE | TERMINATE
    assert handle, "the command's process could not be opened"
    try:
        assert k.WaitForSingleObject(handle, 0) == 258, "the command was not running (control)"
        wrapper.kill()
        wrapper.wait(timeout=30)
        assert k.WaitForSingleObject(handle, 10_000) == 0, "the command outlived its wrapper"
    finally:
        k.TerminateProcess(handle, 1)
        k.CloseHandle(handle)
