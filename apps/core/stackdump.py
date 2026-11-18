"""Write every thread's stack to a file when asked, so a wedged server leaves evidence (Tue 10 Nov 2026).

On Tue 10 Nov 2026 production stopped answering from 13:12:26 to (time withheld). The process was alive and idle
(0.08 CPU-seconds in 5 s), the port listened, and three database connections sat "idle in transaction". A hung
request is written to the access log only when it finishes, and a restart is the only cure, so nothing recorded
which code was waiting on what. Windows has no SIGUSR1 for `faulthandler.register`, and py-spy is not installed.

This module is the in-process half: a daemon thread polls one directory for a file named `TRIGGER`. When it
appears, the thread removes it and writes `stacks-<UTC stamp>.txt` beside it:
  1. `faulthandler.dump_traceback(all_threads=True)`: the C-level walk that needs nothing from the interpreter
     beyond the GIL, written first so a failure later in this function still leaves it;
  2. the same stacks formatted from `sys._current_frames()` with each thread's NAME (`ThreadPoolExecutor-0_0`,
     the asgiref sync thread, the reactor), which faulthandler does not print.
No frame locals are written: a stack names code, never request data or secrets.

The other half is the operator's watcher (`deploy/hqd/prod/livez_stackdump_watch.py`), which creates the trigger
after consecutive failed /v1/livez probes. Anyone who can write the directory can ask for a dump, which is why it
is a directory under the production logs, not a route: an HTTP endpoint would need the wedged server to answer.

Off unless MESHY_STACKDUMP_DIR is set. The thread only needs the GIL to be free, which today's wedge had (the
process was idle). A wedge that HOLDS the GIL would stop this thread too; that case is out of scope here.
"""
from __future__ import annotations

import datetime as _dt
import faulthandler
import os
import sys
import threading
import traceback

ENV_DIR = "MESHY_STACKDUMP_DIR"
TRIGGER_NAME = "TRIGGER"
POLL_S = 1.0
#: The newest dumps kept; older `stacks-*.txt` are removed after each dump so a stuck trigger cannot fill the disk.
KEEP_DUMPS = 50
THREAD_NAME = "meshy-stackdump"

_started: threading.Thread | None = None
_lock = threading.Lock()


def _stamp(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S%fZ")


def write_dump(directory: str, now: _dt.datetime | None = None) -> str:
    """Write one dump file into `directory` and return its path. Raises OSError if the file cannot be opened."""
    path = os.path.join(directory, f"stacks-{_stamp(now)}.txt")
    # O_EXCL: a file or link already planted at this name is refused, never followed or overwritten (pxx P3).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with open(fd, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"pid {os.getpid()} at {_dt.datetime.now(_dt.timezone.utc).isoformat()}\n")
        fh.write("== faulthandler (all threads) ==\n")
        fh.flush()
        faulthandler.dump_traceback(file=fh, all_threads=True)
        fh.write("\n== named stacks (sys._current_frames) ==\n")
        names = {t.ident: (t.name, t.daemon) for t in threading.enumerate()}
        for ident, frame in sys._current_frames().items():
            name, daemon = names.get(ident, ("<unknown>", None))
            fh.write(f"\n-- thread {ident} {name!r} daemon={daemon}\n")
            fh.write("".join(traceback.format_stack(frame)))
    _prune(directory)
    return path


def _prune(directory: str) -> None:
    try:
        dumps = sorted(n for n in os.listdir(directory) if n.startswith("stacks-") and n.endswith(".txt"))
    except OSError:
        return
    for name in dumps[:-KEEP_DUMPS]:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def poll_once(directory: str) -> str | None:
    """One poll: if the trigger exists, remove it and write a dump. Returns the dump's path, or None.

    The trigger is removed BEFORE the dump so a dump that fails cannot be retried every second forever; the
    watcher sees no new file and may create the trigger again."""
    trigger = os.path.join(directory, TRIGGER_NAME)
    try:
        os.remove(trigger)
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"stackdump: could not remove {TRIGGER_NAME}: {exc}", file=sys.stderr, flush=True)
        return None
    try:
        path = write_dump(directory)
    except Exception as exc:  # noqa: BLE001 - the watcher thread must outlive any one failed dump
        print(f"stackdump: dump failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None
    print(f"stackdump: wrote {os.path.basename(path)}", file=sys.stderr, flush=True)
    return path


def _loop(directory: str, stop: threading.Event, poll_s: float) -> None:
    while not stop.wait(poll_s):
        poll_once(directory)


#: The GIL watchdog (pxx P3): a stall longer than this, with the GIL held, writes every thread's stack.
GIL_WATCH_ENV = "MESHY_GIL_WATCH_S"
GIL_WATCH_S = 60.0
GIL_FILE_PREFIX = "gil-stall-"
_gil_started: threading.Thread | None = None


def install_gil_watch(directory: str, watch_s: float = GIL_WATCH_S, stop: threading.Event | None = None,
                      ) -> threading.Thread | None:
    """The case the trigger thread cannot cover: a wedge that HOLDS the GIL stops every Python thread, this module's
    poller included. `faulthandler.dump_traceback_later` arms a C watchdog that needs no GIL; a heartbeat thread
    re-arms it every watch_s / 3 seconds. While Python threads get the GIL, the timer never fires. When one thread
    holds it for longer than watch_s, the heartbeat cannot run, the timer fires, and every thread's stack is appended
    to gil-stall-<pid>.txt. Expect a false alarm on a single GIL-bound call longer than watch_s, which is why the
    default is a minute. One timer per process (faulthandler's own limit): nothing else in the server arms one.
    Never raises; returns None when it cannot open its file."""
    global _gil_started
    if watch_s <= 0:
        return None
    with _lock:
        if _gil_started is not None and _gil_started.is_alive():
            return _gil_started
        path = os.path.join(directory, f"{GIL_FILE_PREFIX}{os.getpid()}.txt")
        try:
            # Never follow a link planted at this name (GitHub Codex and pxx on #590): refuse one outright,
            # and open with O_NOFOLLOW where the platform has it, so a link made between the check and the open
            # fails too. Append-only, owner-only.
            if os.path.islink(path):
                raise OSError(f"{path} is a link; not followed")
            flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0))
            fh = open(os.open(path, flags, 0o600), "a", encoding="utf-8")
        except OSError as exc:
            print(f"stackdump: GIL watch not started: {exc}", file=sys.stderr, flush=True)
            return None
        stop = stop or threading.Event()

        def beat():
            try:
                while True:
                    faulthandler.dump_traceback_later(watch_s, repeat=False, file=fh, exit=False)
                    if stop.wait(watch_s / 3):
                        break
            finally:
                faulthandler.cancel_dump_traceback_later()
                fh.close()

        thread = threading.Thread(target=beat, name=THREAD_NAME + "-gil", daemon=True)
        thread.start()
        _gil_started = thread
        return thread


def install(environ=None, poll_s: float = POLL_S, stop: threading.Event | None = None) -> threading.Thread | None:
    """Start the watcher thread once per process if MESHY_STACKDUMP_DIR is set. Never raises: a dump helper must
    not stop the server from booting, so a directory that cannot be created is reported and the thread is not
    started."""
    global _started
    directory = (environ if environ is not None else os.environ).get(ENV_DIR, "").strip()
    if not directory:
        return None
    with _lock:
        if _started is not None and _started.is_alive():
            return _started
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            print(f"stackdump: not started, {ENV_DIR} unusable: {exc}", file=sys.stderr, flush=True)
            return None
        thread = threading.Thread(target=_loop, args=(directory, stop or threading.Event(), poll_s),
                                  name=THREAD_NAME, daemon=True)
        thread.start()
        _started = thread
    env = environ if environ is not None else os.environ
    try:
        watch_s = float(env.get(GIL_WATCH_ENV, "") or GIL_WATCH_S)
    except ValueError:
        watch_s = GIL_WATCH_S
    install_gil_watch(directory, watch_s, stop=stop)
    return thread
