"""Run a console command with NO window and exit with its exit code. Start it with pythonw.exe.

    pythonw.exe run_hidden.py [--log FILE] PROGRAM [ARG ...]

For a scheduled task that runs as the logged-on user (LogonType Interactive, no stored password):
Task Scheduler starts the action in that user's desktop session, so a console program there
opens a window. Measured on HQB, 2026-09-27, with a throwaway task per arm:
  * powershell.exe (or cmd.exe, or a venv's Scripts\\python.exe) shows a console window;
  * -WindowStyle Hidden still flashes one, because it hides the window after it exists;
  * conhost.exe --headless shows none, but the task's Last Run Result is 0 whatever the command
    returned, which hides the result codes the offsite-copy section tells you to alert on;
  * pythonw.exe plus this file shows none and keeps the command's exit code (3 in, 3 recorded).
pythonw.exe has no console, and the child is started with CREATE_NO_WINDOW, which gives it a
console that has no window; everything the child starts in turn shares that hidden console, so
nothing below it can open one either.

Stopping the task (Stop-ScheduledTask, or its time limit) terminates only this process. So on
Windows the child is put in a job that is killed when this process ends; without it the
measured result was the export server still listening after Stop-ScheduledTask. A venv
launcher child keeps its own job for the interpreter it starts, so that one ends too.

The child's stdin is empty and its output is discarded, unless --log FILE is given, in which case
stdout and stderr are appended to FILE. The exit code is the child's; 2 is a usage error here.
"""
import contextlib
import os
import subprocess
import sys

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 0 off Windows, where there is no window


def _end_with_this_process(proc):
    """Put proc in a kill-on-close job whose only handle is ours, so it ends when we do.

    Returns the job handle (keep it referenced) or None when that is not possible, in which case
    the command still runs, only without the tie.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000             # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9: extended
        return None
    if not k.AssignProcessToJobObject(job, int(proc._handle)):
        return None
    return job


def main(argv):
    log = None
    if argv[:1] == ["--log"]:
        if len(argv) < 2:
            return 2
        log, argv = argv[1], argv[2:]
    if not argv:
        return 2
    with (open(log, "ab") if log is not None else contextlib.nullcontext()) as out:
        sink = subprocess.DEVNULL if out is None else out
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sink,
                                stderr=subprocess.DEVNULL if out is None else subprocess.STDOUT,
                                creationflags=NO_WINDOW)
        job = _end_with_this_process(proc)
        code = proc.wait()
        del job
        return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
