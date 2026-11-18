"""Run one long-lived listener, and kill it if the postbox daemon that started it dies.

    postbox_child.py <daemon_pid> -- <listener argv...>

POSIX only in use (postbox_os.py, condition c): Windows ties a listener to its daemon with the
kill-on-close job object instead, and macOS has no parent-death signal, so without this a daemon
killed by SIGKILL, a crash or a launchd restart would leave its listener running on the identity,
and the next daemon would refuse to start a second reader ("blocked").

The shim must be started BY the daemon (postbox_os.listener_argv does that). It detects the
daemon's death without any third-party module and without trusting a pid, which can be reused:
  POSIX    the shim's own parent pid changes (it is reparented when the daemon dies);
  Windows  a handle to the daemon, opened at start, becomes signalled (kept for the tests; not
           used in production, where the job object does this).

The listener inherits the shim's stdin, stdout and stderr, so the daemon reads its lines exactly as
before. Every POLL_S the shim checks the daemon: once it is gone, the listener gets SIGTERM, then a
kill after GRACE_S, and the shim exits 4. A SIGTERM or SIGINT to the shim (launchd stopping the job)
is passed on the same way. When the listener exits on its own, the shim exits with its code. A
daemon that is not the shim's parent, or is already gone, means nothing is started (exit 3).
"""
import os
import signal
import subprocess
import sys
import threading
import time

POLL_S = 1.0
GRACE_S = 5.0
MAX_PS_BYTES = 2 * 1048576


def daemon_watch(daemon_pid):
    """A function answering True once the daemon is gone, or None when it is gone already or is not
    this process's parent."""
    if os.name == "nt":
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        synchronize = 0x00100000
        handle = k.OpenProcess(synchronize, False, daemon_pid)
        if not handle or k.WaitForSingleObject(handle, 0) == 0:       # 0 = WAIT_OBJECT_0: exited
            return None
        return lambda: k.WaitForSingleObject(handle, 0) == 0
    if os.getppid() != daemon_pid:
        return None
    return lambda: os.getppid() != daemon_pid


def _run_ps_bounded(argv, *, max_bytes=MAX_PS_BYTES, timeout_s=30.0):
    """Run `argv` with bounded stdout/stderr drain threads; return decoded stdout or None on error/truncation."""
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except Exception:
        return None
    out_chunks, err_chunks = [], []
    out_state = {"seen": 0, "truncated": False, "error": None}
    err_state = {"seen": 0, "truncated": False, "error": None}

    def _drain(pipe, sink, cap, st):
        if pipe is None:
            return
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                if st["seen"] <= cap:
                    keep = (cap + 1) - st["seen"]
                    if len(chunk) > keep:
                        st["truncated"] = True
                        sink.append(chunk[:keep])
                    else:
                        sink.append(chunk)
                else:
                    st["truncated"] = True
                st["seen"] += len(chunk)
        except BaseException as exc:
            st["error"] = exc
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_chunks, max_bytes, out_state), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_chunks, 65536, err_state), daemon=True)
    t_out.start()
    t_err.start()
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        return None
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    if (rc != 0 or t_out.is_alive() or t_err.is_alive()
            or out_state["truncated"] or out_state["error"] is not None or err_state["error"] is not None):
        return None
    return b"".join(out_chunks).decode("utf-8", "replace")


def ps_descendants(pid):
    """[pid] of every process below `pid` in the parent tree, read from `ps -A -o pid=,ppid=` (Linux and macOS), so
    a descendant that started its own session or process group is included. None when ps cannot be read. Kept in
    this file on purpose: the shim imports nothing but the standard library."""
    table = _run_ps_bounded(["ps", "-A", "-o", "pid=,ppid="])
    if table is None:
        return None
    return tree_below(pid, table)


def tree_below(pid, table):
    """The pids below `pid` in a `pid ppid` table, one pair per line; lines that are not two integers are skipped."""
    below = {}
    for line in table.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            below.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], [int(pid)]
    while todo:
        for kid in below.get(todo.pop(), []):
            if kid != pid and kid not in found:
                found.append(kid)
                todo.append(kid)
    return found


def descendants(pid):
    """The child's descendants, collected while the child still lives (once it exits they are reparented and no
    longer findable from it): psutil Process objects when psutil imports, else pids from the POSIX process table
    (pxx: without psutil this used to find none, and a setsid grandchild survived). None when neither can be
    read."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return psutil.Process(pid).children(recursive=True)
        except Exception:
            pass
    if os.name == "nt":
        return None
    return ps_descendants(pid)


def _signal_pid(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def terminate(child):
    """End the child and everything it started (24 Oct 2026, pxx #2: a kimi turn's detached grandchild used
    to survive its daemon). A listener starts nothing, so for it this is the old SIGTERM, grace, kill. Descendants
    known only by pid are STOPPED before the child is ended and killed after, so a stopped one cannot exit during the
    grace and hand its pid to an unrelated process. When no descendant list can be read at all, a shim that leads its
    own session (a kimi turn, started with start_new_session; never a listener) ends its whole process group, itself
    included, since it exits next anyway."""
    kids = descendants(child.pid) if child.poll() is None else []
    pids = [k for k in kids or [] if isinstance(k, int)]
    for k in pids:
        _signal_pid(k, signal.SIGSTOP)
    clean = True
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                child.wait(timeout=GRACE_S)
            except subprocess.TimeoutExpired:
                clean = False
    for k in kids or []:
        if isinstance(k, int):
            _signal_pid(k, signal.SIGKILL)
            continue
        try:
            k.kill()
        except Exception:
            pass
    if kids is None and os.name != "nt" and os.getsid(0) == os.getpid():
        try:
            os.killpg(os.getpid(), signal.SIGKILL)
        except OSError:
            pass
    return clean


def main(argv):
    if len(argv) < 4 or argv[2] != "--":
        sys.stderr.write("usage: postbox_child.py <daemon_pid> -- <listener argv...>\n")
        return 2
    gone = daemon_watch(int(argv[1]))
    if gone is None:
        return 3
    child = subprocess.Popen(argv[3:])

    def on_signal(signum, frame):
        ok = terminate(child)
        sys.exit((128 + signum) if ok else 5)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    while True:
        rc = child.poll()
        if rc is not None:
            return rc
        if gone():
            ok = terminate(child)
            return 4 if ok else 5
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
