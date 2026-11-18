"""The operating-system seams of tools/postbox, so one set of files runs on Windows (HQB, HQD)
and on macOS (the pxx and oxxxxxx seats, onboarded 2026-09-28).

Added 2026-09-28 by kxxx, with the author's (bxxxxx's) three conditions:

  (a) WINDOWS BEHAVIOUR IS UNCHANGED. Every Windows branch below is the code it replaced, moved
      here verbatim: the same msvcrt byte lock, the same console test, the same PowerShell command
      text byte for byte, the same CREATE_NO_WINDOW flag.
  (b) A LIVE DAEMON IS DETECTED FROM THE PROCESS TABLE ON POSIX, never by taking its lock: a probe
      that holds postbox.lock for an instant can make a daemon that is starting exit ("another
      postbox holds postbox.lock"), and nothing restarts it until launchd's next attempt.
  (c) ON POSIX A LISTENER DIES WITH ITS DAEMON, however the daemon dies. Windows gets that from the
      kill-on-close job object (postbox.tie_to_daemon). POSIX has no such object, and macOS has no
      PR_SET_PDEATHSIG, so each long-lived listener runs under postbox_child.py, which watches the
      daemon and kills the listener within about a second of the daemon going away (a SIGKILL of
      the daemon included).
"""
import contextlib
import datetime
import errno
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref

IS_WINDOWS = os.name == "nt"
_STDLIB_RUN = subprocess.run
MAX_CONFIG_BYTES, MAX_CLI_SOURCE_BYTES, MAX_CACHE_BYTES = 1048576, 1048576, 2 * 1048576
MAX_PS_SCAN_BYTES, MAX_PGID_PS_BYTES = 4 * 1048576, 2 * 1048576
MAX_HELPER_RECORDS, MAX_HELPER_DIR_SCAN, MAX_APPEND_ARCHIVES = 256, 16384, 16
#: A non-mutating spawning record with no proof either way, as code from before the gate wrote it, is dropped this long
#: past its own timeout_s. A read helper has ended by then or can do no harm, and the hold exists to stop lookups piling
#: up, which one per expiry does not.
HELPER_SPAWNING_EXPIRY_MARGIN_S = 300.0
REPARSE_POINT_ATTR = 0x400


class _PathLock:
    def __init__(self):
        self.lock = threading.RLock()


_APPEND_LOCKS, _APPEND_LOCKS_GUARD = weakref.WeakValueDictionary(), threading.Lock()
_PEER_MOD = sys.modules.get("tools.postbox.postbox_os" if __name__ == "postbox_os" else "postbox_os")
if _PEER_MOD is not None and hasattr(_PEER_MOD, "_REFUSED_APPENDS"):
    _REFUSED_APPENDS = _PEER_MOD._REFUSED_APPENDS
    _REFUSED_APPENDS_LOCK = _PEER_MOD._REFUSED_APPENDS_LOCK
else:
    _REFUSED_APPENDS, _REFUSED_APPENDS_LOCK = {}, threading.Lock()
# Records this daemon could not mark uncertain on disk (a failed write, a full disk), by absolute path: settlement must not
# read them as this daemon's own healthy helpers. Shared across both import names, like _REFUSED_APPENDS.
_UNCERTAIN_UNWRITTEN = getattr(_PEER_MOD, "_UNCERTAIN_UNWRITTEN", None) if _PEER_MOD is not None else None
if _UNCERTAIN_UNWRITTEN is None:
    _UNCERTAIN_UNWRITTEN = set()


def _append_lock(path):
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _APPEND_LOCKS_GUARD:
        holder = _APPEND_LOCKS.get(key)
        if holder is None:
            holder = _PathLock()
            _APPEND_LOCKS[key] = holder
        return holder


def refused_append_counts():
    """Snapshot `{abs_path: count}` of files whose `append_regular_private` calls were refused in this process."""
    with _REFUSED_APPENDS_LOCK:
        return {p: c for p, c in _REFUSED_APPENDS.items() if c > 0}


def refused_append_count(path):
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _REFUSED_APPENDS_LOCK:
        for p, c in _REFUSED_APPENDS.items():
            if os.path.normcase(p) == key:
                return c
        return 0


def reset_refused_append_counts():
    with _REFUSED_APPENDS_LOCK:
        _REFUSED_APPENDS.clear()


def _record_refused_append(path, *, archives_full=False, max_archives=MAX_APPEND_ARCHIVES):
    abs_path = os.path.abspath(os.fspath(path))
    norm_key = os.path.normcase(abs_path)
    with _REFUSED_APPENDS_LOCK:
        existing_key = next((p for p in _REFUSED_APPENDS if os.path.normcase(p) == norm_key), abs_path)
        mem_count = _REFUSED_APPENDS.get(existing_key, 0) + 1
        _REFUSED_APPENDS[existing_key] = mem_count
    if archives_full:
        sat_path = abs_path + ".saturated.json"
        base = os.path.basename(abs_path)
        prev_doc = None
        with contextlib.suppress(Exception):
            prev_doc = read_bounded_json(sat_path, max_bytes=16384)
        prev_n = prev_doc.get("refused_count", 0) if isinstance(prev_doc, dict) else 0
        prev_at = prev_doc.get("saturated_at") if isinstance(prev_doc, dict) else None
        disk_n = max(mem_count, (prev_n if type(prev_n) is int and prev_n >= 0 else 0) + 1)
        with contextlib.suppress(Exception):
            write_private_json_atomic(sat_path, {
                "path": base,
                "reason": "archive_slots_full",
                "max_archives": max_archives,
                "refused_count": disk_n,
                "saturated_at": prev_at if isinstance(prev_at, str) and prev_at else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "action": "archive or delete %s.1 .. %s.%d to resume rotation" % (base, base, max_archives),
            }, prefix=".sat-", max_bytes=16384)


def _clear_refused_append_if_recovered(path, *, rotated, max_archives=MAX_APPEND_ARCHIVES):
    abs_path = os.path.abspath(os.fspath(path))
    sat_path = abs_path + ".saturated.json"
    if not rotated:
        if not os.path.lexists(sat_path):
            return
        if all(os.path.lexists("%s.%d" % (abs_path, idx)) for idx in range(1, max_archives + 1)):
            return
    norm_key = os.path.normcase(abs_path)
    with _REFUSED_APPENDS_LOCK:
        for k in [p for p in _REFUSED_APPENDS if os.path.normcase(p) == norm_key]:
            _REFUSED_APPENDS.pop(k, None)
    with contextlib.suppress(OSError):
        if os.path.lexists(sat_path) and not is_link_or_reparse(os.lstat(sat_path)):
            os.unlink(sat_path)


def fsync_dir(dir_path):
    if (IS_WINDOWS and os.name == "nt") or not dir_path:
        return
    dir_fd = os.open(os.fspath(dir_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class OversizeFileError(ValueError):
    def __init__(self, size, cap):
        super().__init__("size %d bytes exceeds %d-byte limit" % (size, cap))
        self.size, self.cap = size, cap


def is_link_or_reparse(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT_ATTR)


def open_regular_read(path, *, require_private=False):
    path = os.fspath(path)
    st = os.lstat(path)
    if is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode):
        raise ValueError("not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd, ok = os.open(path, flags), False
    try:
        info = os.fstat(fd)
        if is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode) or os.path.islink(path):
            raise ValueError("not a regular file")
        if require_private and not IS_WINDOWS and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("file must be owned by this account with mode 600")
        ok = True
        return fd, info
    finally:
        if not ok:
            with contextlib.suppress(OSError):
                os.close(fd)


def read_bounded_bytes(path, max_bytes=MAX_CONFIG_BYTES, *, require_private=False):
    fd, info = open_regular_read(path, require_private=require_private)
    try:
        if info.st_size > max_bytes:
            raise OversizeFileError(info.st_size, max_bytes)
        chunks, total = [], 0
        while True:
            step = min(65536, (max_bytes + 1) - total)
            chunk = os.read(fd, step) if step > 0 else b""
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise OversizeFileError(total, max_bytes)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def read_bounded_json_with_stat(path, max_bytes=MAX_CONFIG_BYTES, *, require_private=False):
    raw, info = read_bounded_bytes(path, max_bytes=max_bytes, require_private=require_private)
    return json.loads(raw.decode("utf-8")), (info.st_mtime_ns, info.st_size)


def read_bounded_json(path, max_bytes=MAX_CONFIG_BYTES, *, require_private=False):
    return read_bounded_json_with_stat(path, max_bytes=max_bytes, require_private=require_private)[0]


def write_private_json_atomic(path, value, *, prefix=".tmp-", max_bytes=MAX_CACHE_BYTES):
    path = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if is_link_or_reparse(os.lstat(parent)):
        raise ValueError("target directory is a symlink or reparse point")
    target_st = None
    with contextlib.suppress(FileNotFoundError):
        target_st = os.lstat(path)
    if target_st is not None and (is_link_or_reparse(target_st) or not stat.S_ISREG(target_st.st_mode)):
        raise ValueError("target path is not a regular file")
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if max_bytes is not None and len(encoded) > max_bytes:
        raise OversizeFileError(len(encoded), max_bytes)
    fd, tmp = tempfile.mkstemp(prefix=prefix, dir=parent)
    try:
        if not IS_WINDOWS and hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(encoded); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(parent)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def append_regular_private(path, payload_bytes, *, max_bytes=None, max_archives=MAX_APPEND_ARCHIVES, _fsync_fn=None):
    """Append `payload_bytes` to regular private file `path`, rotating into the first free `.1`..`.max_archives` slot.

    Assumes one writer process per file (threads in this process serialize on `_append_lock(path)`). On POSIX, rotation
    links `path -> cand` via `os.link` before `os.unlink` so even an unexpected second writer process fails with EEXIST
    rather than overwriting an existing archive slot (falling back to `os.replace` only when hardlinks are unsupported).
    When all archive slots are occupied, returns False without overwriting history, increments `refused_append_counts`,
    and writes `<path>.saturated.json` naming the manual prune step (`archive or delete <name>.1 .. <name>.16`).
    """
    path = os.fspath(path)
    is_win = IS_WINDOWS and os.name == "nt"
    fsync_directory = _fsync_fn or fsync_dir
    holder = _append_lock(path)
    with holder.lock:
        try:
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                st = None
            created_new, rotated = st is None, False
            if st is not None:
                if is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode):
                    _record_refused_append(path, max_archives=max_archives)
                    return False
                if not is_win and st.st_uid != os.getuid():
                    _record_refused_append(path, max_archives=max_archives)
                    return False
                alias_rot = None
                if not is_win and getattr(st, "st_ino", 0) and (
                    getattr(st, "st_nlink", 1) > 1 or (max_bytes is not None and st.st_size > max_bytes)
                ):
                    for idx in range(1, max_archives + 1):
                        cand = "%s.%d" % (path, idx)
                        try:
                            cst = os.lstat(cand)
                        except FileNotFoundError:
                            continue
                        if not is_link_or_reparse(cst) and stat.S_ISREG(cst.st_mode) and (cst.st_dev, cst.st_ino) == (st.st_dev, st.st_ino):
                            os.unlink(path)
                            alias_rot = cand
                            break
                if alias_rot is not None or (max_bytes is not None and st.st_size > max_bytes):
                    rot = alias_rot
                    if rot is None:
                        for idx in range(1, max_archives + 1):
                            cand = "%s.%d" % (path, idx)
                            if os.path.lexists(cand):
                                continue
                            if not is_win and hasattr(os, "link"):
                                try:
                                    os.link(path, cand)
                                except FileExistsError:
                                    continue
                                except OSError as exc:
                                    if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOSYS, errno.EACCES):
                                        raise
                                    if os.path.lexists(cand):
                                        continue
                                    os.replace(path, cand)
                                else:
                                    os.unlink(path)
                            else:
                                os.replace(path, cand)
                            rot = cand
                            break
                    if rot is None:
                        _record_refused_append(path, archives_full=True, max_archives=max_archives)
                        return False
                    if not is_win:
                        with contextlib.suppress(OSError):
                            os.chmod(rot, 0o600)
                    rotated = created_new = True
            flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            fd = os.open(path, flags, 0o600)
            try:
                info = os.fstat(fd)
                if is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode) or os.path.islink(path):
                    _record_refused_append(path, max_archives=max_archives)
                    return False
                if not is_win:
                    if info.st_uid != os.getuid():
                        _record_refused_append(path, max_archives=max_archives)
                        return False
                    if stat.S_IMODE(info.st_mode) != 0o600 and hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o600)
                view = memoryview(payload_bytes)
                written = 0
                while written < len(view):
                    n = os.write(fd, view[written:])
                    if n <= 0:
                        _record_refused_append(path, max_archives=max_archives)
                        return False
                    written += n
                if rotated and not is_win:
                    os.fsync(fd)
            finally:
                os.close(fd)
            if rotated or created_new:
                with contextlib.suppress(OSError):
                    fsync_directory(os.path.dirname(os.path.abspath(path)))
            _clear_refused_append_if_recovered(path, rotated=rotated, max_archives=max_archives)
            return True
        except OSError:
            _record_refused_append(path, max_archives=max_archives)
            return False


def no_window_flag(osname=None):
    """subprocess creationflags that keep a console from appearing on Windows. POSIX Popen
    refuses any non-zero creationflags, so it gets 0 there."""
    return 0x08000000 if (osname or os.name) == "nt" else 0


NO_WINDOW = no_window_flag()


# --------------------------------------------------------------------------- locks

def try_lock(fd):
    """A non-blocking exclusive lock held by this process on the open file `fd`. True when taken,
    False when another process holds it. Windows: msvcrt's one-byte lock, as before. POSIX: flock
    on the whole file (a whole-file flock is not dropped when some other descriptor of the same
    file is closed, which a POSIX fcntl/lockf lock would be)."""
    if IS_WINDOWS:
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(fd):
    if IS_WINDOWS:
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- processes (kimi delivery, 24 Oct 2026)

# Process names whose command line MUST be readable for the session scan to call a session free (pxx #4):
# kimi-code runs as node, and a kimi shim as kimi. Any other unreadable process is not a kimi turn.
SCAN_STRICT_NAMES = ("node", "node.exe", "kimi", "kimi.exe")


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def processes_naming(text, exclude=(), strict_names=SCAN_STRICT_NAMES):
    """[(pid, name)] of every process whose command line contains `text`, never this process or `exclude`.

    Raises when the process table cannot be read, AND when a process named in `strict_names` has a command line that
    cannot be read (access denied, a zombie), so a caller that must not act on doubt (the kimi delivery, which may
    not write beside an open session) refuses instead of reading "unreadable" as "free". psutil when present;
    otherwise PowerShell or ps, with `text` passed through the environment so the scanner's own command line cannot
    contain it and match itself."""
    import subprocess
    mine = {os.getpid(), *exclude}
    strict = {n.lower() for n in strict_names}
    out, unreadable = [], []
    psutil = _psutil()
    if psutil is not None:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            pid, pname, argv = p.info["pid"], p.info.get("name") or "?", p.info.get("cmdline")
            if pid in mine:
                continue
            if argv is None:
                if pname.lower() in strict:
                    unreadable.append((pid, pname))
                continue
            if text in " ".join(argv):
                out.append((pid, pname))
    elif IS_WINDOWS:
        names = ",".join("'%s'" % n for n in strict)
        script = ("$t = $env:MESHY_SCAN_TEXT; $strict = @(%s); Get-CimInstance Win32_Process | ForEach-Object { "
                  "if ($_.CommandLine) { if ($_.CommandLine.Contains($t)) { '{0}|{1}' -f $_.ProcessId, $_.Name } } "
                  "elseif ($strict -contains $_.Name.ToLower()) { 'UNREADABLE|{0}|{1}' -f $_.ProcessId, $_.Name } }"
                  % names)
        r = run_bounded(["powershell", "-NoProfile", "-Command", script], max_stdout=MAX_PS_SCAN_BYTES,
                        max_stderr=65536, text=True, env=dict(os.environ, MESHY_SCAN_TEXT=text),
                        timeout=60, creationflags=NO_WINDOW, run_fn=subprocess.run, _capture_ctime=False)
        if r.returncode != 0:
            raise RuntimeError("process scan rc=%d: %s" % (r.returncode, (r.stderr or "")[-200:]))
        if getattr(r, "stdout_truncated", False):
            raise RuntimeError("process scan output exceeded %d bytes" % MAX_PS_SCAN_BYTES)
        for line in r.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) == 3 and parts[0] == "UNREADABLE" and parts[1].isdigit() and int(parts[1]) not in mine:
                unreadable.append((int(parts[1]), parts[2]))
            elif len(parts) == 2 and parts[0].isdigit() and int(parts[0]) not in mine:
                out.append((int(parts[0]), parts[1] or "?"))
    else:
        # -ww: procps cuts piped output at 80 columns, so a session token past column 80 was never seen
        # (found under WSL, 24 Oct 2026); BSD and macOS ps take -ww too
        r = run_bounded(["ps", "-ww", "-axo", "pid=,comm=,args="], max_stdout=MAX_PS_SCAN_BYTES,
                        max_stderr=65536, text=True, timeout=60, run_fn=subprocess.run, _capture_ctime=False)
        if r.returncode != 0:
            raise RuntimeError("ps rc=%d" % r.returncode)
        if getattr(r, "stdout_truncated", False):
            raise RuntimeError("ps output exceeded %d bytes" % MAX_PS_SCAN_BYTES)
        for line in r.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and text in parts[2] and parts[0].isdigit() and int(parts[0]) not in mine:
                out.append((int(parts[0]), os.path.basename(parts[1])))
    if unreadable:
        raise RuntimeError("cannot read the command line of %s" % ", ".join("pid %s (%s)" % u for u in unreadable))
    return out


def process_create_time(pid):
    """The process's creation time in epoch seconds, or None when no such process exists. Raises when it cannot be
    told (no psutil and the fallback failed), so a caller can keep a record instead of guessing."""
    import subprocess
    psutil = _psutil()
    if psutil is not None:
        try:
            return psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            return None
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.OpenProcess.restype = wintypes.HANDLE
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k.GetExitCodeProcess.restype = wintypes.BOOL
            k.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            k.GetProcessTimes.restype = wintypes.BOOL
            h = k.OpenProcess(0x1000, False, int(pid))
            if not h:
                if ctypes.get_last_error() == 87:
                    return None
            else:
                try:
                    code = wintypes.DWORD()
                    if k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:
                        return None
                    c_ft, e_ft, k_ft, u_ft = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
                    if k.GetProcessTimes(h, ctypes.byref(c_ft), ctypes.byref(e_ft), ctypes.byref(k_ft), ctypes.byref(u_ft)):
                        ticks = (c_ft.dwHighDateTime << 32) | c_ft.dwLowDateTime
                        return (ticks - 116444736000000000) / 10000000.0
                finally:
                    k.CloseHandle(h)
        except Exception:
            pass
        script = ("$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $env:MESHY_PID); "
                  "if ($p) { [DateTimeOffset]::new($p.CreationDate).ToUnixTimeMilliseconds() }")
        r = run_bounded(["powershell", "-NoProfile", "-Command", script], max_stdout=65536, max_stderr=65536,
                        text=True, env=dict(os.environ, MESHY_PID=str(int(pid))), timeout=60,
                        creationflags=NO_WINDOW, run_fn=subprocess.run, _capture_ctime=False)
        if r.returncode != 0 or getattr(r, "stdout_truncated", False):
            raise RuntimeError("process query rc=%d" % r.returncode)
        value = r.stdout.strip()
        return int(value) / 1000.0 if value else None
    # `etime` ([[dd-]hh:]mm:ss) is in both Linux and macOS ps; `etimes` is Linux only (pxx #6). Whole seconds.
    r = run_bounded(["ps", "-o", "etime=", "-p", str(int(pid))], max_stdout=65536, max_stderr=65536,
                    text=True, timeout=10, run_fn=subprocess.run, _capture_ctime=False, _cleanup_pgid=False)
    if getattr(r, "stdout_truncated", False):
        raise RuntimeError("ps etime truncated for pid %s" % pid)
    value = r.stdout.strip()
    if not value:
        if r.returncode != 0:
            return None
        raise RuntimeError("ps printed no elapsed time for pid %s" % pid)
    import time
    return round(time.time() - parse_etime(value))


def parse_etime(value):
    """Seconds from ps's etime: [[dd-]hh:]mm:ss."""
    days, _, rest = value.strip().rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return int(days or 0) * 86400 + h * 3600 + m * 60 + s


def kill_tree(pid):
    """Kill a process and every descendant it has (a timed-out kimi turn: node plus the tools it started), and
    return True only when none of them is left alive (pxx #5). With psutil the descendants are collected
    BEFORE the kill, so one that started its own session or process group is still killed. Without psutil:
    Windows `taskkill /T /F` with its exit code checked, POSIX a process-group kill (weaker: a descendant that left
    the group survives), and the process itself is checked afterwards. Never raises."""
    import subprocess
    import time
    psutil = _psutil()
    if psutil is not None:
        try:
            root = psutil.Process(pid)
            procs = root.children(recursive=True) + [root]
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            procs = []
        for p in procs:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=15)
        return not alive and bool(procs)
    try:
        if IS_WINDOWS:
            r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=60,
                               creationflags=NO_WINDOW)
            ok = r.returncode == 0
        else:
            import signal
            os.killpg(pid, signal.SIGKILL)   # the turn was started with start_new_session, so pid is the group
            ok = True
    except Exception:
        ok = False
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if process_create_time(pid) is None:
                return ok
        except Exception:
            return False
        time.sleep(0.5)
    return False


def terminate_owned_tree(pid, expected_create_time=None, grace_s=0.2):
    """Terminate `pid` and its descendants after verifying `expected_create_time` (when given). Never raises."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    psutil = _psutil()
    if psutil is not None:
        try:
            root = psutil.Process(pid)
            if expected_create_time is not None and abs(root.create_time() - expected_create_time) > 2.0:
                return False
            procs = root.children(recursive=True) + [root]
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            return False
        for p in procs:
            with contextlib.suppress(psutil.Error):
                p.terminate()
        _, alive = psutil.wait_procs(procs, timeout=max(0.0, float(grace_s)))
        if alive:
            for p in list(alive):
                with contextlib.suppress(psutil.Error):
                    alive.extend(c for c in p.children(recursive=True) if c not in alive)
            for p in alive:
                with contextlib.suppress(psutil.Error):
                    p.kill()
            _, alive = psutil.wait_procs(alive, timeout=5)
        return not alive
    if expected_create_time is not None:
        try:
            cur_ct = process_create_time(pid)
        except Exception:
            return False
        if cur_ct is None or abs(cur_ct - expected_create_time) > 2.0:
            return cur_ct is None
    try:
        if IS_WINDOWS:
            r = _STDLIB_RUN(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15,
                            creationflags=NO_WINDOW)
            return r.returncode == 0 or process_create_time(pid) is None
        import signal
        own_group = False
        with contextlib.suppress(OSError):
            own_group = os.getpgid(pid) == pid and pid != os.getpgrp()
        sig_fn = os.killpg if own_group else os.kill
        sig_fn(pid, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, float(grace_s))
        while time.monotonic() < deadline:
            if process_create_time(pid) is None:
                return True
            time.sleep(0.02)
        sig_fn(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def helper_dir_for_log(log_path):
    return os.fspath(log_path) + ".helpers"


def _group_has_live_member(pgid):
    if IS_WINDOWS or type(pgid) is not int or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


def _cap_completed_process(res, max_stdout, max_stderr, text, encoding, errors):
    out_val, err_val = res.stdout, res.stderr
    out_trunc, err_trunc = bool(getattr(res, "stdout_truncated", False)), bool(getattr(res, "stderr_truncated", False))
    if out_val is not None:
        raw_out = out_val.encode(encoding, errors) if isinstance(out_val, str) else bytes(out_val)
        if len(raw_out) > max_stdout:
            out_trunc, raw_out = True, raw_out[:max_stdout + 1]
        out_val = raw_out.decode(encoding, errors) if text else raw_out
    if err_val is not None:
        raw_err = err_val.encode(encoding, errors) if isinstance(err_val, str) else bytes(err_val)
        if len(raw_err) > max_stderr:
            err_trunc, raw_err = True, raw_err[:max_stderr + 1]
        err_val = raw_err.decode(encoding, errors) if text else raw_err
    capped = subprocess.CompletedProcess(res.args, res.returncode, out_val, err_val)
    capped.stdout_truncated, capped.stderr_truncated, capped.used_injected_runner = out_trunc, err_trunc, True
    return capped


def _pgid_members(pgid):
    if IS_WINDOWS or type(pgid) is not int or pgid <= 1:
        return True, []
    psutil, my_uid, out = _psutil(), os.getuid(), []
    if psutil is not None:
        for p in psutil.process_iter(["pid"]):
            mpid = p.info["pid"]
            try:
                if os.getpgid(mpid) != pgid:
                    continue
                uids = p.uids() if hasattr(p, "uids") else None
                if uids is not None and uids.real != my_uid:
                    return False, []
                out.append((mpid, p.create_time()))
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (OSError, psutil.Error):
                return False, []
        return True, out
    try:
        r = run_bounded(["ps", "-axo", "pid=,pgid=,uid="], max_stdout=MAX_PGID_PS_BYTES, max_stderr=65536,
                        text=True, timeout=15, stdin=subprocess.DEVNULL, _capture_ctime=False, _cleanup_pgid=False)
        if r.returncode != 0 or getattr(r, "stdout_truncated", False):
            return False, []
        for pts in (ln.split() for ln in r.stdout.splitlines()):
            if len(pts) == 3 and all(x.isdigit() for x in pts) and int(pts[1]) == pgid:
                if int(pts[2]) != my_uid:
                    return False, []
                ct = process_create_time(int(pts[0]))
                if ct is not None:
                    out.append((int(pts[0]), ct))
        return True, out
    except Exception:
        return False, []


_SELF_CTIME_CACHE, _SELF_CTIME_LOCK = {}, threading.Lock()
_HELPER_KIND_PREFIX_RE = re.compile(r"^h-([A-Za-z][A-Za-z0-9_]*)-\d+-\d+\.json$")


def _safe_helper_kind(kind):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(kind or "helper")).strip("_") or "helper"


def _cached_self_create_time():
    my_pid = os.getpid()
    with _SELF_CTIME_LOCK:
        if my_pid in _SELF_CTIME_CACHE:
            return my_pid, _SELF_CTIME_CACHE[my_pid]
    my_ctime = None
    with contextlib.suppress(Exception):
        my_ctime = process_create_time(my_pid)
    with _SELF_CTIME_LOCK:
        if my_ctime is not None:
            _SELF_CTIME_CACHE[my_pid] = my_ctime
    return my_pid, my_ctime


# POSTBOX-448 admin 361a7e and 02f44a P2-2: on POSIX a recorded helper starts behind a gate. The gate is a tiny launcher
# that waits for one byte on an inherited pipe and only then execs the helper's argv in the same process (same pid, same
# start time); the daemon writes that byte only after the record names the pid. A daemon that dies first closes the pipe:
# the gate reads EOF and exits GATE_EXIT without running anything, so a "spawning" record with the gate flag proves that
# the helper never ran, and a "running" record whose gate was never released ends as an exited process.
# The gate leaves the helper what a direct Popen would: it puts back the two variables interpreter startup changes (PEP
# 538 LC_CTYPE; __CF_USER_TEXT_ENCODING on macOS) and the default SIGPIPE and SIGXFSZ, and an exec failure comes back over
# a close-on-exec pipe as its errno, so run_bounded raises the same OSError that Popen would have raised.
# What the gate covers is argv only: Windows has no gate (the record says gated None, so settlement never reads it as
# proof), and run_bounded refuses the Popen modes the gate cannot carry (_refuse_ungateable).
_GATE_SRC = ("import json, os, signal, sys\n"
             "go = os.read(int(sys.argv[1]), 1)\n"
             "if go != b'1':\n"
             "    os._exit(125)\n"
             "os.close(int(sys.argv[1]))\n"
             "for k, v in json.loads(sys.argv[3]).items():\n"
             "    if v is None:\n"
             "        os.environ.pop(k, None)\n"
             "    else:\n"
             "        os.environ[k] = v\n"
             "for s in ('SIGPIPE', 'SIGXFSZ'):\n"
             "    signal.signal(getattr(signal, s), signal.SIG_DFL)\n"
             "st = int(sys.argv[2])\n"
             "os.set_inheritable(st, False)\n"
             "try:\n"
             "    os.execvp(sys.argv[4], sys.argv[4:])\n"
             "except OSError as exc:\n"
             "    os.write(st, b'%d' % (exc.errno or 0))\n"
             "    os._exit(127)\n")
GATE_EXIT = 125
_GATE_RESTORED_ENV = ("LC_CTYPE", "__CF_USER_TEXT_ENCODING")


def _refuse_ungateable(argv, kw):
    """Refuse, before any record exists, what the gate cannot carry; preexec_fn would also run before the release."""
    if not isinstance(argv, (list, tuple)):
        raise TypeError("run_bounded: a recorded helper needs an argv list, not %s" % type(argv).__name__)
    if not argv:
        raise IndexError("run_bounded: a recorded helper needs a command")     # what Popen([]) raises
    if kw.get("shell"):
        raise ValueError("run_bounded: a recorded helper does not support shell")
    for name in ("executable", "preexec_fn"):
        if kw.get(name) is not None:
            raise ValueError("run_bounded: a recorded helper does not support %s" % name)
    for name in ("close_fds", "restore_signals"):
        if name in kw and not kw[name]:
            raise ValueError("run_bounded: a recorded helper does not support a false %s" % name)


def _gate_env_json(env):
    src = os.environ if env is None else {os.fsdecode(k): os.fsdecode(v) for k, v in env.items()}
    return json.dumps({k: src.get(k) for k in _GATE_RESTORED_ENV})


def _begin_helper_record(helper_dir, *, kind, mutating, timeout, spawn_ts, gated=False):
    my_pid, my_ctime = _cached_self_create_time()
    kind_s, safe_kind = str(kind or "helper"), _safe_helper_kind(kind)
    rec = {"schema": 1, "state": "spawning", "kind": kind_s, "mutating": bool(mutating),
           "gated": None if gated is None else bool(gated), "pid": None,
           "pgid": None, "create_time": None, "create_time_known": False, "daemon_pid": my_pid,
           "daemon_create_time": my_ctime, "started_at": spawn_ts,
           "timeout_s": float(timeout) if timeout is not None else None}
    rec_path = os.path.join(os.fspath(helper_dir), "h-%s-%d-%d.json" % (safe_kind, my_pid, time.monotonic_ns()))
    write_private_json_atomic(rec_path, rec, prefix=".helper-", max_bytes=65536)
    return rec_path, rec


def _attach_helper_process(rec_path, rec, *, pid, pgid, proc_ctime):
    updated = dict(rec, state="running", pid=int(pid),
                   pgid=int(pgid) if type(pgid) is int and pgid > 0 else None,
                   create_time=proc_ctime, create_time_known=proc_ctime is not None)
    write_private_json_atomic(rec_path, updated, prefix=".helper-", max_bytes=65536)
    return updated


def _finalize_helper_record(rec_path, rec, proc, own_session):
    if not rec_path:
        return
    pgid = rec.get("pgid") if own_session and type(rec.get("pgid")) is int else None
    abs_path = os.path.abspath(rec_path)
    try:
        live = proc.poll() is None or (pgid is not None and _group_has_live_member(pgid))
    except Exception:
        live = True                                # not known to be gone: keep the record and mark it uncertain
    except BaseException:
        _UNCERTAIN_UNWRITTEN.add(abs_path)         # an interrupt here must not leave it reading as a healthy helper
        raise
    if not live:
        with contextlib.suppress(OSError):
            os.unlink(rec_path)
        return
    _UNCERTAIN_UNWRITTEN.add(abs_path)             # first, and dropped only once the mark is on disk: nothing below can lose it
    extra = {"uncertain": True, "updated_at": time.time()}
    if pgid is not None and type(rec.get("pid")) is int:
        with contextlib.suppress(Exception):
            cur_ct, exp_ct = process_create_time(rec["pid"]), rec.get("create_time")
            if cur_ct is None or (isinstance(exp_ct, (int, float)) and abs(cur_ct - exp_ct) <= 2.0):
                ok_m, members = _pgid_members(pgid)
                floor = (exp_ct if isinstance(exp_ct, (int, float)) else rec.get("started_at", 0.0)) - 2.0
                if ok_m:
                    extra["group_members"] = [{"pid": m, "create_time": ct} for m, ct in members if ct >= floor]
    with contextlib.suppress(Exception):
        write_private_json_atomic(rec_path, dict(rec, **extra), prefix=".helper-", max_bytes=65536)
        _UNCERTAIN_UNWRITTEN.discard(abs_path)


def _spawning_record_expired(rec):
    """Past its own timeout_s plus the margin, by the times it recorded. A record that gives no bound never expires."""
    started, limit = rec.get("started_at"), rec.get("timeout_s")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (started, limit)):
        return False
    return time.time() - started > limit + HELPER_SPAWNING_EXPIRY_MARGIN_S


def settle_helper_dir(helper_dir, *, only_kind=None):
    """Inspect and settle orphaned detached helper records in `helper_dir` without signaling unverified PIDs/groups."""
    helper_dir = os.fspath(helper_dir)
    try:
        st = os.lstat(helper_dir)
    except FileNotFoundError:
        return {"settled": True, "active_mutating": [], "issues": [], "reaped": []}
    except OSError as exc:
        return {"settled": False, "active_mutating": [], "issues": ["helper_dir_stat:%s" % type(exc).__name__], "reaped": []}
    if is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
        return {"settled": False, "active_mutating": [], "issues": ["helper_dir_not_regular_dir"], "reaped": []}
    safe_only = _safe_helper_kind(only_kind) if only_kind is not None else None
    issues, active_mutating, reaped, my_pid, scanned, raw_seen, entries = [], [], [], os.getpid(), 0, 0, []
    try:
        with os.scandir(helper_dir) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                raw_seen += 1
                if raw_seen > MAX_HELPER_DIR_SCAN:
                    issues.append(
                        "helper_dir_scan_cap_exceeded (inspect and archive or delete settled records in %s)"
                        % (os.path.basename(os.path.normpath(helper_dir)) or "helpers")
                    )
                    break
                if safe_only is not None:
                    m = _HELPER_KIND_PREFIX_RE.match(entry.name)
                    if m is not None and m.group(1) != safe_only:
                        continue
                scanned += 1
                if scanned > MAX_HELPER_RECORDS:
                    issues.append("helper_record_cap_exceeded"); break
                entries.append(entry.path)
    except OSError as exc:
        return {"settled": False, "active_mutating": [], "issues": ["helper_dir_scan:%s" % type(exc).__name__], "reaped": []}
    for rec_path in sorted(entries):
        base_name = os.path.basename(rec_path)
        try:
            rec = read_bounded_json(rec_path, max_bytes=65536)
        except FileNotFoundError:
            continue                               # finalized or settled since the scan: that is not corruption
        except Exception as exc:
            issues.append("corrupt_helper_record:%s:%s" % (base_name, type(exc).__name__)); continue
        if not isinstance(rec, dict):
            issues.append("invalid_helper_record:%s" % base_name); continue
        if os.path.abspath(rec_path) in _UNCERTAIN_UNWRITTEN:
            rec = dict(rec, uncertain=True)
        kind = str(rec.get("kind") or "helper")
        if only_kind is not None and kind != only_kind:
            continue
        raw_pid, state_s = rec.get("pid"), rec.get("state")
        spawning = state_s == "spawning" and raw_pid is None
        if not spawning and (type(raw_pid) is not int or raw_pid <= 0):
            issues.append("invalid_helper_record:%s" % base_name); continue
        pid, mutating = (None if spawning else raw_pid), bool(rec.get("mutating"))
        pgid = rec.get("pgid") if type(rec.get("pgid")) is int and rec.get("pgid") > 1 else None
        d_pid, d_ct, parent_alive = rec.get("daemon_pid"), rec.get("daemon_create_time"), False
        if type(d_pid) is int and d_pid > 0:
            if d_pid == my_pid and not rec.get("uncertain"):
                parent_alive = True
            elif d_pid != my_pid:
                try:
                    cur_d_ct = process_create_time(d_pid)
                except Exception:
                    parent_alive = True
                else:
                    parent_alive = cur_d_ct is not None and (d_ct is None or abs(cur_d_ct - d_ct) <= 2.0)
        if parent_alive and (d_pid != my_pid or not rec.get("uncertain")):
            if mutating:
                active_mutating.append({"kind": kind, "pid": pid, "record": rec_path})
            continue
        if spawning:
            if mutating:
                active_mutating.append({"kind": kind, "pid": None, "record": rec_path, "uncertain": True})
                issues.append("mutating_helper_spawning_unsettled:%s:%s" % (kind, base_name))
            elif rec.get("gated") is True:
                # the record was never attached, so the gate never got its byte and the helper never ran
                with contextlib.suppress(OSError):
                    os.unlink(rec_path)
                reaped.append({"kind": kind, "pid": None, "state": "spawning_gated_never_ran"})
            elif "gated" in rec and rec["gated"] is None:
                # No gate on this platform (Windows), so this proves nothing about the helper. It is a read helper of a dead
                # daemon, and holding it would stop this kind's lookups for good after one crash (a reviewer's rule on #507).
                with contextlib.suppress(OSError):
                    os.unlink(rec_path)
                reaped.append({"kind": kind, "pid": None, "state": "spawning_abandoned"})
            elif _spawning_record_expired(rec):
                with contextlib.suppress(OSError):
                    os.unlink(rec_path)
                reaped.append({"kind": kind, "pid": None, "state": "spawning_expired"})
            else:
                # a record from code before the gate proves nothing either: keep the intent and surface it until it expires
                issues.append("helper_spawning_unsettled:%s:%s" % (kind, base_name))
            continue
        try:
            cur_ct = process_create_time(pid)
        except Exception as exc:
            issues.append("helper_liveness_unknown:%s:%d:%s" % (kind, pid, type(exc).__name__))
            if mutating:
                active_mutating.append({"kind": kind, "pid": pid, "record": rec_path, "uncertain": True})
            continue
        expected_ct = rec.get("create_time")
        ct_known = bool(rec.get("create_time_known")) and isinstance(expected_ct, (int, float))
        if (cur_ct is not None) and ct_known and abs(cur_ct - expected_ct) > 2.0:
            with contextlib.suppress(OSError):
                os.unlink(rec_path)
            reaped.append({"kind": kind, "pid": pid, "state": "already_exited"}); continue
        pid_matches = cur_ct is not None
        group_alive = (not IS_WINDOWS and pgid is not None and pgid == pid
                       and pgid != os.getpgrp() and _group_has_live_member(pgid))
        if not pid_matches and not group_alive:
            with contextlib.suppress(OSError):
                os.unlink(rec_path)
            reaped.append({"kind": kind, "pid": pid, "state": "already_exited"}); continue
        if mutating:
            active_mutating.append({"kind": kind, "pid": pid, "record": rec_path})
            issues.append("mutating_helper_unsettled:%s:%d" % (kind, pid)); continue
        if pid_matches and not ct_known:
            issues.append("helper_create_time_unknown:%s:%d" % (kind, pid)); continue
        if pid_matches and ct_known:
            ok_m, grp_m = _pgid_members(pgid) if group_alive else (True, [])
            cleaned = terminate_owned_tree(pid, expected_ct, grace_s=0.3) and ok_m
            for mpid, mct in grp_m:
                if mpid != pid and mct >= expected_ct - 2.0:
                    cleaned = terminate_owned_tree(mpid, mct, grace_s=0.2) and cleaned
            cleaned = cleaned and not (group_alive and _group_has_live_member(pgid))
        else:
            owned = {m["pid"]: m["create_time"] for m in (rec.get("group_members") or [])
                     if isinstance(m, dict) and type(m.get("pid")) is int and isinstance(m.get("create_time"), (int, float))}
            ok_m, live_m = _pgid_members(pgid)
            if not ok_m or not live_m or any(mpid not in owned or abs(mct - owned[mpid]) > 2.0 for mpid, mct in live_m):
                issues.append("helper_group_unverified:%s:%d" % (kind, pgid)); continue
            cleaned = all(terminate_owned_tree(mpid, owned[mpid], grace_s=0.2) for mpid, _ in live_m) and not _group_has_live_member(pgid)
        if cleaned:
            with contextlib.suppress(OSError):
                os.unlink(rec_path)
            reaped.append({"kind": kind, "pid": pid, "state": "terminated"})
        else:
            issues.append("helper_cleanup_failed:%s:%d" % (kind, pid))
    _UNCERTAIN_UNWRITTEN.difference_update([p for p in list(_UNCERTAIN_UNWRITTEN) if not os.path.lexists(p)])
    return {"settled": not issues and not active_mutating, "active_mutating": active_mutating, "issues": issues, "reaped": reaped}


def _cleanup_session_group(proc, pgid, proc_ctime, spawn_ts, *, allow_pgid_scan=True):
    if IS_WINDOWS or type(pgid) is not int or pgid <= 1 or pgid == os.getpgrp():
        return
    import signal
    if getattr(proc, "returncode", None) is None:
        with contextlib.suppress(OSError):
            os.killpg(pgid, getattr(signal, "SIGKILL", 9))
        return
    if not allow_pgid_scan:
        return
    floor = (proc_ctime if isinstance(proc_ctime, (int, float)) else spawn_ts) - 2.0
    ok_m, grp_m = _pgid_members(pgid)
    if ok_m:
        for mpid, mct in grp_m:
            if mct >= floor:
                terminate_owned_tree(mpid, mct, grace_s=0.1)


def run_bounded(argv, *, max_stdout=1048576, max_stderr=65536, text=False, timeout=None, run_fn=None,
                helper_dir=None, helper_kind="helper", helper_mutating=False, _capture_ctime=True,
                _cleanup_pgid=True, **kw):
    """Run `argv` with pipe readers capped at `max_stdout + 1` and `max_stderr + 1` bytes, draining overflow to EOF.

    With `helper_dir` on POSIX the helper starts behind the gate (see _GATE_SRC), and _refuse_ungateable names the modes
    that are refused. On Windows there is no gate: the record says gated None and proves nothing about a crash.
    """
    encoding, errors = kw.pop("encoding", None) or "utf-8", kw.pop("errors", None) or "replace"
    if run_fn is not None and run_fn is not _STDLIB_RUN:
        kw.pop("capture_output", None)
        r = (run_fn(argv, capture_output=True, text=True, encoding=encoding, errors=errors, timeout=timeout, **kw)
             if text else run_fn(argv, capture_output=True, text=False, timeout=timeout, **kw))
        return _cap_completed_process(r, max_stdout, max_stderr, text, encoding, errors)
    for drop in ("capture_output", "stdout", "stderr", "universal_newlines"):
        kw.pop(drop, None)
    own_session = not IS_WINDOWS and "start_new_session" not in kw and "preexec_fn" not in kw
    if own_session:
        kw["start_new_session"] = True
    deadline = None if timeout is None else (time.monotonic() + max(0.0, float(timeout)))
    spawn_ts = time.time()
    rec_path, rec, popen_argv, gate_fds = None, {}, argv, []
    gated = helper_dir is not None and not IS_WINDOWS
    go_r = go_w = st_r = st_w = None

    def _close_gate(*fds):
        for fd in fds:
            if fd in gate_fds:
                gate_fds.remove(fd)
                with contextlib.suppress(OSError):
                    os.close(fd)

    if gated:
        _refuse_ungateable(argv, kw)
    proc, pgid, proc_ctime, released = None, None, None, False
    out_buf, err_buf, st_buf, pipe_errors, threads, unowned_pipes = bytearray(), bytearray(), bytearray(), [], [], []

    def _drain(label, pipe, limit, buf):
        rem, rd = limit + 1, getattr(pipe, "read1", None) or pipe.read
        try:
            for chunk in iter(lambda: rd(65536), b""):
                if rem > 0:
                    take = min(len(chunk), rem)
                    buf.extend(chunk[:take])
                    rem -= take
        except BaseException as exc:
            pipe_errors.append((label, exc))
        finally:
            with contextlib.suppress(Exception):
                pipe.close()

    def _start_drains():
        drains = [("stdout", proc.stdout, max_stdout, out_buf), ("stderr", proc.stderr, max_stderr, err_buf)]
        if st_r is not None:
            drains.append(("gate", os.fdopen(st_r, "rb", 0), 32, st_buf))
            gate_fds.remove(st_r)                                 # the file object owns it now
        unowned_pipes.extend(d[1] for d in drains)
        for lbl, p, lim, b in drains:
            t = threading.Thread(target=_drain, args=(lbl, p, lim, b), daemon=True)
            t.start()
            threads.append(t)
            unowned_pipes.remove(p)                               # a started drain closes its own pipe

    def _abort_unreleased():
        """Nothing past the gate has run: close it, end the launcher, drop the pipes. The finally settles the record."""
        _close_gate(*list(gate_fds))
        with contextlib.suppress(Exception):
            if own_session:
                _cleanup_session_group(proc, pgid, proc_ctime, spawn_ts, allow_pgid_scan=_cleanup_pgid)
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                terminate_owned_tree(proc.pid, proc_ctime, grace_s=0.1)
        for fn in (lambda: proc.kill(), lambda: proc.wait(timeout=2)):
            with contextlib.suppress(Exception):
                fn()
        for t in threads:
            t.join(timeout=0.2)
        for pipe in unowned_pipes or ([] if threads else [proc.stdout, proc.stderr]):
            with contextlib.suppress(Exception):
                pipe.close()

    _rem = lambda: None if deadline is None else max(0.0, deadline - time.monotonic())  # noqa: E731
    _vals = lambda: (((bytes(out_buf).decode(encoding, errors), bytes(err_buf).decode(encoding, errors))  # noqa: E731
                      if text else (bytes(out_buf), bytes(err_buf))), bytes(out_buf), bytes(err_buf))
    try:
        # Everything up to the go byte happens here, and the helper's argv cannot start until that byte is written, so a
        # failure anywhere in this block ends a launcher that ran nothing. The finally below covers all of it.
        try:
            if gated:
                go_r, go_w = os.pipe()
                gate_fds.extend((go_r, go_w))
                st_r, st_w = os.pipe()
                gate_fds.extend((st_r, st_w))
                popen_argv = [sys.executable, "-I", "-S", "-c", _GATE_SRC, str(go_r), str(st_w),
                              _gate_env_json(kw.get("env"))] + list(argv)
                kw["pass_fds"] = tuple(kw.get("pass_fds") or ()) + (go_r, st_w)
            if helper_dir is not None:
                rec_path, rec = _begin_helper_record(
                    helper_dir, kind=helper_kind, mutating=helper_mutating, timeout=timeout, spawn_ts=spawn_ts,
                    gated=None if IS_WINDOWS else gated)
            proc = subprocess.Popen(popen_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
            pgid = proc.pid if own_session else None
            _close_gate(go_r, st_w)
            if own_session:
                with contextlib.suppress(OSError):
                    pgid = os.getpgid(proc.pid)
            has_psutil = _psutil() is not None
            if _capture_ctime and has_psutil:
                with contextlib.suppress(Exception):
                    proc_ctime = process_create_time(proc.pid)
            if rec_path is not None:
                rec = _attach_helper_process(rec_path, rec, pid=proc.pid, pgid=pgid, proc_ctime=proc_ctime)
                if _capture_ctime and not has_psutil and proc_ctime is None:
                    with contextlib.suppress(Exception):
                        proc_ctime = process_create_time(proc.pid)
                    if proc_ctime is not None:
                        rec = _attach_helper_process(rec_path, rec, pid=proc.pid, pgid=pgid, proc_ctime=proc_ctime)
            _start_drains()
            released = True                                       # from here the helper may be running
            if go_w is not None:                                  # the record names the pid now: let the helper run
                try:
                    os.write(go_w, b"1")
                except OSError:                                   # refused: no byte went out, so nothing ran
                    released = False
                    raise
        except BaseException:
            if proc is not None and not released:                 # a released helper is never killed here
                _abort_unreleased()
            raise
        try:
            for t in threads:
                t.join(timeout=_rem())
                if t.is_alive():
                    raise subprocess.TimeoutExpired(argv, timeout)
            rc = proc.wait(timeout=_rem())
        except subprocess.TimeoutExpired:
            if own_session:
                _cleanup_session_group(proc, pgid, proc_ctime, spawn_ts, allow_pgid_scan=_cleanup_pgid)
            if proc.poll() is None:
                if proc_ctime is None and _cleanup_pgid:
                    with contextlib.suppress(Exception):
                        proc_ctime = process_create_time(proc.pid)
                terminate_owned_tree(proc.pid, proc_ctime, grace_s=0.1)
            if proc.poll() is None:
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
            for t in threads:
                t.join(timeout=0.2)
            (out_val, err_val), _, _ = _vals()
            raise subprocess.TimeoutExpired(argv, timeout, output=out_val, stderr=err_val)
    finally:
        try:
            _close_gate(*list(gate_fds))
        finally:
            if proc is not None:
                _finalize_helper_record(rec_path, rec, proc, own_session)
            elif rec_path:                                        # no process was started
                with contextlib.suppress(OSError):
                    os.unlink(rec_path)
    if st_buf:                                                    # the gate could not exec: what Popen would have raised
        eno = int(bytes(st_buf))
        raise OSError(eno, os.strerror(eno), argv[0])
    if pipe_errors:
        label, exc = pipe_errors[0]
        if isinstance(exc, OSError):
            raise OSError("pipe reader failed on %s: %s" % (label, exc)) from exc
        raise OSError("pipe reader failed on %s: %s: %s" % (label, type(exc).__name__, exc)) from exc
    (out_val, err_val), ro, re_ = _vals()
    res = subprocess.CompletedProcess(argv, rc, out_val, err_val)
    res.stdout_truncated, res.stderr_truncated = len(ro) > max_stdout, len(re_) > max_stderr
    res.used_injected_runner = False
    return res


# --------------------------------------------------------------------------- stdout

def stdout_discarded():
    """True when this process's stdout goes nowhere (so a waiter's wake would be lost).
    Windows: the console test wait_next.py used before. POSIX: stdout closed, or /dev/null."""
    if sys.stdout is None:
        return True
    if IS_WINDOWS:
        try:
            import ctypes
            import msvcrt
            h = msvcrt.get_osfhandle(sys.stdout.fileno())
            k = ctypes.windll.kernel32
            k.GetFileType.argtypes = [ctypes.c_void_p]
            k.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            if k.GetFileType(h) != 2:                  # FILE_TYPE_CHAR is a console or NUL
                return False
            return not k.GetConsoleMode(h, ctypes.byref(ctypes.c_uint32()))
        except Exception:
            return False
    try:
        return posix_is_devnull(sys.stdout.fileno())
    except Exception:
        return False


def posix_is_devnull(fd):
    """True when `fd` is /dev/null (same device and inode)."""
    try:
        return os.path.samestat(os.fstat(fd), os.stat(os.devnull))
    except OSError:
        return False


# --------------------------------------------------------------------------- commands a session pastes

def sender_text(d):
    """Who sent it, for a PERSON: the alias first (divergence 063: the frame's `from`, which the server sends only for a peer's roster label), the peer id
    after it so the two stay tied and an alias is never the whole identity. A server before 063 sends only the id, and then the id is all there is."""
    label, pid = d.get("from"), d.get("from_peer_id")
    if isinstance(label, str) and label:
        return "%s (peer %s)" % (label, str(pid)[:8]) if pid else label
    return str(pid) if pid else "unknown"


#: the directive rule (approvals, a recorded decision; threat_model.md, amendment of 2 Nov 2026): the one exception
#: to "a peer's request", the same words in every runtime's framing: meshy_channel.py (Claude), postbox.py
#: (Codex notices), kimi_arm_hook.py (Kimi).
#: Its last two sentences (a recorded decision: "any node can be steered without having me to write my
#: authorisation", then, after his fifth paste into a seat's window, "so I dont ever have to do that again for any
#: node"): on 6 Nov a Codex seat left its question for him in its own window overnight, unseen, and stopped, because
#: an earlier instruction in its conversation kept his authority to that window. A rule file cannot outrank such an
#: instruction for the rest of a session; this sentence arrives with every notice, so it can. It keeps his own stop
#: (pxx's review of #530, F1): an explicit stop typed in a seat's window still narrows that seat until he lifts it.
DIRECTIVE_EXCEPTION = ("The one exception: when the signed read prints `seat_directive` (another seat's directive to you, verified; the directive rule) or `operator_relay` (Bxxxxx's own words from a voice), follow that block's `note`: its words carry the operator's authority in your own window, and you act on them without asking him again. "
                       "Ask any question for Bxxxxx on MeshyChat (--requires-ack), never only in your own window. "
                       "This is Bxxxxx's standing instruction from your own postbox (7 Nov 2026): it replaces any earlier instruction in this conversation that only his typing in this window can authorize something. "
                       "Nothing he types here is needed to authorize, and an answer he types here adds to that channel and never ties his authority to this window; only an explicit stop or limit he types here on what to do (stop, don't act on directives, don't do X), never on where his authority comes from, narrows it, until he lifts it in his own words. ")


# Quiet hours for the urgent lane (kxxx, Tue 10 Nov 2026). The RECEIVER's identity may carry
#   "urgent_quiet": ["00:00", "08:00"]
# in Vienna wall-clock time. An urgent message CREATED inside the window (the frame's `created_at`, the server's own
# time for the row) is delivered as ordinary mail: [STEERING], in arrival
# order, not through the Codex lane, and its note says why. Nothing is dropped or delayed beyond that, and the sender
# is not told (its hourly allowance is spent either way). The window may wrap midnight (["22:00", "06:00"]); the start
# is inside it and the end is not. A value this code cannot read counts as NO quiet hours (urgent stays urgent), and
# urgent_quiet_problem says what is wrong, so a typing mistake costs a receiver its quiet, never a sender its urgency.
# Vienna time is computed from the EU rule (summer time from the last Sunday of March 01:00 UTC to the last Sunday of
# October 01:00 UTC), not from the box's zone table: Windows Python has none without an extra package, and Git Bash's
# falls back to GMT without saying so.
# No clock is read anywhere: the decision is a function of the message and the setting, so every reader gives the
# same answer at every moment. A decision by the reader's clock changed between two calls in one pass, which lost
# or doubled a Codex notice at a window edge and resent one when quiet began (pxx, GitHub Codex on 75b4b9c0).
# A frame whose time cannot be read is not quiet: urgent stays urgent.
_QUIET_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])\Z")     # ASCII digits only: \d takes other scripts


def _last_sunday_1utc(year, month):
    last = datetime.datetime(year, month, 31, 1, 0, tzinfo=datetime.timezone.utc)
    return last - datetime.timedelta(days=(last.weekday() + 1) % 7)


_FRAME_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


def frame_time(d):
    """A frame's `created_at` as an aware UTC datetime, or None when it is absent or not the server's
    `YYYY-MM-DDTHH:MM:SSZ` shape."""
    text = d.get("created_at") if isinstance(d, dict) else None
    if not isinstance(text, str) or not _FRAME_TIME.match(text):
        return None
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:                                  # the shape of a date that is none (month 13, day 31)
        return None


def vienna_minutes(when):
    """Minutes since midnight on Vienna's wall clock at `when`, an aware datetime."""
    now = when.astimezone(datetime.timezone.utc)
    summer = _last_sunday_1utc(now.year, 3) <= now < _last_sunday_1utc(now.year, 10)
    local = now + datetime.timedelta(hours=2 if summer else 1)
    return local.hour * 60 + local.minute


def urgent_quiet_problem(ident):
    """None when the identity's "urgent_quiet" is absent or readable, else what is wrong with it."""
    value = ident.get("urgent_quiet") if isinstance(ident, dict) else None
    if value is None:
        return None
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value)):
        return 'urgent_quiet must be a list of two "HH:MM" strings'
    if not all(_QUIET_HHMM.match(v) for v in value):
        return 'urgent_quiet times must be "HH:MM", 00:00 to 23:59'
    if value[0] == value[1]:
        return "urgent_quiet start and end are the same: that names no window"
    return None


def urgent_quiet_report(ident):
    """The one line a delivery loop reports at its start when the setting cannot be read, else None."""
    problem = urgent_quiet_problem(ident)
    if not problem:
        return None
    return "%s: %s; no quiet hours apply (urgent stays urgent) until it is fixed" % (ident.get("name"), problem)


def urgent_quiet_window(ident):
    """(start, end) in minutes since Vienna midnight, or None when the identity sets none or it cannot be read."""
    if not isinstance(ident, dict) or ident.get("urgent_quiet") is None or urgent_quiet_problem(ident):
        return None
    (h1, m1), (h2, m2) = (v.split(":") for v in ident["urgent_quiet"])
    return int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)


def urgent_quiet(ident, when):
    """Whether `when` (an aware datetime, or None for unknown) is inside this identity's quiet hours. Unknown is
    not quiet."""
    window = urgent_quiet_window(ident)
    if window is None or when is None:
        return False
    start, end = window
    minute = vienna_minutes(when)
    return start <= minute < end if start < end else (minute >= start or minute < end)


def urgent_shown(d, ident=None):
    """True for a `to` frame the server flagged urgent that was not created in the receiver's quiet hours: the one
    test the tag, the note, the burst order and the Codex lane share. It reads no clock."""
    return (isinstance(d, dict) and d.get("role") == "to" and d.get("urgent") is True
            and not urgent_quiet(ident, frame_time(d)))


def kind_tag(d, ident=None):
    """The first word of a notice, for a PERSON scanning a one-line list (operator, 2026-10-01; divergence 067):
      [STEERING]  a message with this seat in its `to` line: the sender wants THIS seat to act;
      [MESSAGE]   a message this seat is only copied on (`cc`) or that went to everyone (`broadcast`);
      [RECALL]    a message this seat already holds was EDITED: what it read is no longer what the message says.
    A notice that is not about a message at all (a listener or configuration failure) is tagged `[PROBLEM]` by both clients, and the Claude channel's own
    open and busy notices `[CHANNEL]`; those are written where the notice is built, not here.
    The tag is ROUTING, read from the frame's `event` and `role`, which the server sets per recipient. It is not authority: a [STEERING] message is still a
    peer's request, and the notice goes on saying so."""
    if d.get("event") == "edit":
        return "[RECALL]"
    # The urgent lane (kxxx): [URGENT] is delivery ORDER only, from the server's `to`-frame flag; a cc or a
    # broadcast copy of the same message never carries it, and neither does one that arrives in the receiver's
    # quiet hours (`ident`'s "urgent_quiet"; without an identity there are none).
    if urgent_shown(d, ident):
        return "[URGENT]"
    return "[STEERING]" if d.get("role") == "to" else "[MESSAGE]"


def role_note(d, ident=None):
    """` (cc)` or ` (broadcast)` after the identity, nothing for `to`: why this seat got a [MESSAGE]. For a `to`
    message: ` (urgent: delivery order only; a peer's request)` on an [URGENT] one, and ` (urgent declined: its sender
    says its hourly allowance was spent)` on one its sender labelled urgent_declined. The label is the sender's own claim
    (the server does not check it, SD 460), so the note reports it as said, not as a fact."""
    role = d.get("role")
    if role == "to" and d.get("urgent") is True:
        if urgent_quiet(ident, frame_time(d)):
            return (" (sent urgent, delivered as ordinary: this seat's quiet hours, %s to %s Vienna; a peer's request)"
                    % tuple(ident["urgent_quiet"]))
        return " (urgent: delivery order only; a peer's request)"
    if role == "to" and d.get("urgent_declined") is True:
        return " (urgent declined: its sender says its hourly allowance was spent)"
    return " (%s)" % role if role in ("cc", "broadcast") else ""


def urgent_first(events, ident=None):
    """The events with [URGENT] messages first, each group in arrival order. Notice ORDER only: the bank commits the
    same events in their own order. A message created in the receiver's quiet hours is not urgent and keeps its
    place. Each event is judged once."""
    shown = [urgent_shown(d, ident) for d in events]
    return [d for d, u in zip(events, shown) if u] + [d for d, u in zip(events, shown) if not u]


# --------------------------------------------------------------------------- the wake rule, one for the three consumers

#: An edit wakes the seats the row was for and the reply authors it names, not a broadcast reader
#: (docs/design/edit.md section 4 and test 16). A redaction wakes nobody: it carries nothing to act on.
#: postbox.py (the daemon, for codex), meshy_channel.py and wait_next.py alias this name and `wanted` below: until
#: 2026-10-02 each held its own byte-identical copy, and tests/test_postbox_redaction.py now holds the three
#: names to this one function rather than three copies to each other.
EDIT_WAKE_ROLES = ("to", "cc", "replied")


def wanted(d):
    """The lines that mean 'this seat should look': addressed mail, an edit of mail it held, and listener
    trouble. The body postbox.py, meshy_channel.py and wait_next.py each carried, moved here verbatim; what
    wakes a seat is unchanged for every consumer."""
    ev, st = d.get("event"), d.get("state")
    return ((ev == "message" and d.get("role") in ("to", "cc"))
            or (ev == "edit" and d.get("role") in EDIT_WAKE_ROLES)
            or (ev == "status" and st == "error")
            or (ev == "supervisor" and st in ("listener_exited", "crashed")))


def held_by_default(d):
    """#199: only an explicit no-ack copy can wait; absent server-frame data must keep today's wake rule."""
    return d.get("event") == "message" and d.get("role") == "cc" and d.get("requires_ack") is False


def hold_options(ident, report=None):
    """#199: Bank.collect's hold option for this identity. Only `"hold_cc_without_ack": true` holds; absent or false
    holds nothing. Any other value ("true", 1) holds nothing and is reported through `report`, so a typo neither turns
    the hold on nor passes unseen (the follow-up to #289). Consumers call this once at start, not every pass."""
    value = ident.get("hold_cc_without_ack", False)
    if value is True:
        return {"hold": held_by_default}
    if value is not False and report is not None:
        try:
            report("%s: hold_cc_without_ack=%r is not true or false; nothing is held" % (ident.get("name"), value))
        except Exception:  # noqa: BLE001 - the report is bookkeeping; a failed write must never stop delivery starting
            pass
    return {}


# --------------------------------------------------------------------------- the settle window (wake debounce)
#
# A burst of mail to one seat made one wake per message: about 20 turns for 20 messages that one turn could have
# read together (mesh-optim matrix, item G1; Bxxxxx, a recorded decision point 5: "Go, with nxxxxx's safeguards.
# Directives and urgent mail still wake at once, and the delay has a cap"). With `channel_settle_s` (the Claude
# channel) or `wait_settle_s` (the waiter) set for an identity in config.json, ordinary mail waits that many seconds,
# counted from the ARRIVAL of the first message of the burst, and everything that arrived meanwhile goes in one notice.
# It is OFF unless set. It only delays the NOTICE: the spool already holds the mail, nothing is committed while it
# waits, and the seat still makes its own signed read when it handles the notice.

#: The longest a settle window may be. A larger or malformed setting is no window at all (and is reported), never
#: a longer wait: a typo must not hold a seat's mail.
SETTLE_MAX_S = 60


def settle_seconds(ident, key="channel_settle_s", report=None):
    """This identity's settle window in whole seconds, 0 when off. Only an int from 0 to SETTLE_MAX_S counts; any
    other value (a string, a bool, a float, a negative, a larger number) is 0 and is reported through `report`."""
    value = ident.get(key, 0)
    if type(value) is int and 0 <= value <= SETTLE_MAX_S:
        return value
    if report is not None:
        try:
            report("%s: %s=%r is not a whole number of seconds from 0 to %d; mail is not delayed"
                   % (ident.get("name"), key, value, SETTLE_MAX_S))
        except Exception:  # noqa: BLE001 - the report is bookkeeping; a failed write must never stop delivery
            pass
    return 0


def settle_bypass(events, ident=None):
    """True when this batch must wake NOW: anything that is not a plain message (an edit, a listener or supervisor
    problem, a line this code does not know), a seat directive, an urgent message outside the receiver's quiet
    hours, or a message recovered after a gap. Unknown shapes wake at once: the window never hides trouble."""
    for d in events:
        if not isinstance(d, dict) or d.get("event") != "message":
            return True
        if d.get("directive") or d.get("recovered") or urgent_shown(d, ident):
            return True
    return False


def settle_now():
    """The wall clock the window is judged by (a seam for tests). The spool's `rx_at` is this box's own wall time."""
    return time.time()


def _rx_epoch(value):
    """A spool line's `rx_at` as an epoch, or None when it is absent, unreadable or carries no zone."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.timestamp() if t.tzinfo is not None else None


def settle_hold(events, received, ident, seconds, cut=False, now=None):
    """True: keep waiting (push nothing, commit nothing). False: deliver now.

    The window is judged by the OLDEST message's own arrival time (`received`: one `rx_at` per event, from the
    bank), so it holds no state: a restarted session or a new waiter process cannot start it again, a message
    never waits longer than `seconds` after it arrived, and later arrivals never extend it (txxxx P2-1: a
    window counted from a process's first sight gave the second batch of a burst a second window, 121 s against
    the 60 s cap).

    It delivers at once when: the window is off; the batch must wake now (settle_bypass); the bank stopped at its
    batch cap (`cut`: more mail is behind this batch, and a directive or urgent line could be among it, txxxx
    aeee9c P2-2); any arrival time is missing or unreadable; or the clock ran backwards (any arrival time later
    than now)."""
    if not events or seconds <= 0 or cut or settle_bypass(events, ident):
        return False
    if not isinstance(received, (list, tuple)) or len(received) != len(events):
        return False
    times = [_rx_epoch(r) for r in received]
    if any(t is None for t in times):
        return False
    at = settle_now() if now is None else now
    if any(t > at for t in times):           # ANY arrival in the future: the clock stepped back; an older one
        return False                         # must not hide it (nxxxxx P3-2)
    return at - min(times) < seconds


def config_stat(path):
    """The `(mtime_ns, size)` signature of `path` (`config.json`), or None when absent, non-regular, or unreadable."""
    try:
        st = os.lstat(path)
        if is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode):
            return None
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def refresh_hold_options(path, ident, stat_sig, options, report=None):
    """Re-read `hold_cc_without_ack` for `ident` from `path` (`config.json`) ONLY when its `(mtime_ns, size)`
    changes (#199). When the file is absent, unchanged, or unreadable/unparseable (e.g. mid-write), the existing
    `(stat_sig, options)` are returned with no report and no JSON read on unchanged passes. When the identity's
    `hold_cc_without_ack` setting changes on disk, `ident` is updated in place and `hold_options(ident, report)`
    recomputes `options` once for the new value."""
    now_sig = config_stat(path)
    if now_sig is None or now_sig == stat_sig:
        return stat_sig, options
    try:
        cfg = read_bounded_json(path, max_bytes=MAX_CONFIG_BYTES)
        if not isinstance(cfg, dict) or not isinstance(cfg.get("identities"), list):
            return stat_sig, options
        entry = next((i for i in cfg["identities"] if isinstance(i, dict) and i.get("name") == ident.get("name")), None)
    except (OSError, ValueError, TypeError):
        return stat_sig, options
    if entry is None:
        ident["_config_stat"] = now_sig
        return now_sig, options
    had = "hold_cc_without_ack" in ident
    old_val = ident.get("hold_cc_without_ack")
    has_new = "hold_cc_without_ack" in entry
    new_val = entry.get("hold_cc_without_ack")
    if has_new:
        ident["hold_cc_without_ack"] = new_val
    else:
        ident.pop("hold_cc_without_ack", None)
    ident["_config_stat"] = now_sig
    if had != has_new or old_val != new_val or type(old_val) is not type(new_val):
        options = hold_options(ident, report)
    return now_sig, options


def read_command(ident, key, windows=None):
    """The signed read of one message, paste-ready in the seat's own shell. Windows: PowerShell,
    the text read_command/read_cmd produced before, byte for byte. POSIX: sh/zsh."""
    if IS_WINDOWS if windows is None else windows:
        return ('Set-Location "%s"; & "%s" -m tools.meshy_node read --base-url %s --state-file "%s" --key %s'
                % (ident["client"], ident["python"], ident["base_url"], ident["state"], key))
    return ("cd %s && %s -m tools.meshy_node read --base-url %s --state-file %s --key %s"
            % (shlex.quote(ident["client"]), shlex.quote(ident["python"]), shlex.quote(ident["base_url"]),
               shlex.quote(ident["state"]), shlex.quote(key)))


def send_command(ident, key, windows=None):
    """The exact reply command, the three <...> filled from the signed read. Windows: the text
    postbox.send_command produced before, byte for byte. POSIX: sh/zsh, the placeholders left bare
    so a reader sees them as placeholders, not quoted literals."""
    if IS_WINDOWS if windows is None else windows:
        return ('Set-Location "%s"; & "%s" -m tools.meshy_node send --base-url %s --state-file "%s" '
                '--to <the read\'s "from"> --thread <the read\'s "thread_id"> --type answer --in-reply-to %s '
                '--body-file <a file holding your reply>'
                % (ident["client"], ident["python"], ident["base_url"], ident["state"], key))
    return ("cd %s && %s -m tools.meshy_node send --base-url %s --state-file %s "
            "--to <the read's \"from\"> --thread <the read's \"thread_id\"> --type answer --in-reply-to %s "
            "--body-file <a file holding your reply>"
            % (shlex.quote(ident["client"]), shlex.quote(ident["python"]), shlex.quote(ident["base_url"]),
               shlex.quote(ident["state"]), shlex.quote(key)))


# --------------------------------------------------------------------------- lookups (issue #269)

#: Which client route answers which question, decisions first: an operator's ruling is a card, not mail. One text
#: for every outlet (issue #269; kxxx, msg/<key>): the Claude channel's standing instructions
#: (meshy_channel.instructions) and `python -m tools.meshy_node lookup-help` (a doorbell seat, a Codex AGENTS.md,
#: a hook). Each row: (route, its options, what it answers). The card and summarize wording is kxxx's; --seat was
#: added after uxxx's live recall runs (13 Oct): a seat's question about its OWN decision missed rank 1 only to
#: another seat's card on a shared word. Both
#: searches report their method as rrf(lexical,embed)+rerank (measured 2026-10-12), hence "words and meaning".
LOOKUP_ROUTES = (
    ("card search", '--q "<words>"',
     "what was ruled or decided: dated records of single answers (every card type; --type Ruling for "
     "rulings only; --seat <label> for one seat's records, your own included), quoted data, not standing rules"),
    ("search", '--q "<words>"', "what was said: the mail only, matched by words and meaning, reranked"),
    ("thread", "--id <thread>", "one whole conversation"),
    ("summarize", "--thread <thread> --text", "a conversation in brief: GENERATED and UNVERIFIED"),
    ("context", "--key <key>", "the messages most related to one message"),
    ("neighbours", "--key <key>", "one hop from one message: its parent, replies and mentions"),
    ("owed", "", "the asks you owe a reply to"),
    ("brief", "", "what you owe, what you await, and the threads with news"),
    ("transcript-search", '--q "<words>"',
     "session turns across seats (--seat <label>; verify a hit with transcript-read): untrusted, may hold secrets"),
    ("transcript-consensus", "",
     "shared fleet consensus, open questions, commitments and constraints (--q \"<words>\" for topic hits, --seat <label>)"),
)
#: The whole text stays at or under this many UTF-8 bytes: it sits in the system prompt for the whole session.
#: Over it, only the first identity's lines are kept; still over, there is no text.
LOOKUP_MAX_BYTES = 3072
_LOOKUP_FIELDS = ("client", "python", "base_url", "state")
_CONTROL = re.compile("[\x00-\x1f\x7f  ]")


def _route_pattern(route):
    """How cli.py registers a route: `card search` under card_sub, the others as top-level subcommands."""
    if route == "card search":
        return r'\bcard_sub\.add_parser\(\s*"search"'
    return r'\bsub\.add_parser\(\s*"%s"' % re.escape(route)


def lookup_routes(client):
    """The LOOKUP_ROUTES rows the client checkout at `client` registers, in LOOKUP_ROUTES order; () when it has
    no tools/meshy_node/cli.py that can be read within MAX_CLI_SOURCE_BYTES."""
    try:
        raw, _ = read_bounded_bytes(os.path.join(client, "tools", "meshy_node", "cli.py"),
                                    max_bytes=MAX_CLI_SOURCE_BYTES)
        text = raw.decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return ()
    return tuple(row for row in LOOKUP_ROUTES if re.search(_route_pattern(row[0]), text))


def _lookup_usable(ident):
    return (isinstance(ident, dict)
            and all(isinstance(ident.get(f), str) and ident[f] and not _CONTROL.search(ident[f])
                    for f in _LOOKUP_FIELDS)
            and not _CONTROL.search(str(ident.get("name", ""))))


def lookup_prefix(ident, windows=None):
    """How this identity runs a lookup, paste-ready in the seat's own shell: read_command's text with <route> in
    place of `read` and <options> in place of `--key <key>`. Windows: both shells, each labeled, as read_cmd
    does (a Claude seat on Windows has a PowerShell tool and a bash tool)."""
    posix = ("cd %s && %s -m tools.meshy_node <route> --base-url %s --state-file %s <options>"
             % (shlex.quote(ident["client"]), shlex.quote(ident["python"]), shlex.quote(ident["base_url"]),
                shlex.quote(ident["state"])))
    if not (IS_WINDOWS if windows is None else windows):
        return [posix]
    return ['PowerShell: Set-Location "%s"; & "%s" -m tools.meshy_node <route> --base-url %s --state-file "%s" '
            '<options>' % (ident["client"], ident["python"], ident["base_url"], ident["state"]),
            "bash: " + posix]


def lookup_text(idents, windows=None):
    """The LOOKUP text for these identities (issue #269), or None.

    It describes routes only: it runs nothing and calls no server. None when no identity has the four fields
    (client, python, base_url, state) free of control characters, or when no route is registered by every
    usable identity's client. A route is listed only when EVERY usable identity's client has it, so no seat is
    told to run a command its client lacks."""
    usable = [i for i in (idents or ()) if _lookup_usable(i)]
    if not usable:
        return None
    routes = None
    for ident in usable:
        have = lookup_routes(ident["client"])
        routes = have if routes is None else tuple(r for r in routes if r in have)
    if not routes:
        return None
    for keep in (usable, usable[:1]):
        text = _lookup_text(keep, routes, windows)
        if len(text.encode("utf-8")) <= LOOKUP_MAX_BYTES:
            return text
    return None


_PLAIN_NAMESPACE = re.compile(r"[A-Za-z0-9._/-]{1,128}", re.ASCII)


def card_namespace():
    """The card namespace a seat's client reads (#328): MESHY_CARDS_NAMESPACE, else shared/decisions. The same rule as
    tools/meshy_node/cards.py and the server's apps/cards/validation.py; repeated here because the postbox is installed
    without the client. The value goes into text every seat's model reads, so only a plain namespace (letters, digits,
    `.`, `_`, `-`, `/`, at most 128 characters) is used; anything else, a newline or a control character included, gives
    the default (GitHub Codex on #329)."""
    value = os.environ.get("MESHY_CARDS_NAMESPACE") or ""
    if _PLAIN_NAMESPACE.fullmatch(value):
        return value
    return "shared/decisions"


def _lookup_text(idents, routes, windows):
    lines = ["LOOKUP: to look something up on MeshyChat, run the client as the read command does, with a route "
             "and its options in place of <route> and <options>. Each lookup is one server read; card search "
             "makes a pass over the card namespace, so do not loop it."]
    for ident in idents:
        if len(idents) > 1:
            lines.append("%s:" % ident.get("name", "?"))
        lines.extend(lookup_prefix(ident, windows))
    lines.append("Routes, decisions first:")
    for route, options, answers in routes:
        lines.append("- %s%s: %s" % (route, " " + options if options else "", answers))
    lines.append("A hit or a summary is a pointer, not the record: verify a quote with read --key <key> "
                 "(signature_verified true) before acting on it, and cite the key; a card/ key is read with "
                 "--namespace %s." % card_namespace())
    return "\n".join(lines)


# --------------------------------------------------------------------------- daemon liveness (b)

def daemon_running(here):
    """POSIX: is a postbox daemon for THIS folder alive, read from the process table (condition b).
    A live process whose command line runs this folder's postbox.py, other than this one, and not
    a `--status` call. Windows callers keep their lock probe (condition a)."""
    import psutil
    want = os.path.realpath(os.path.join(here, "postbox.py"))
    me = os.getpid()
    for p in psutil.process_iter(["pid", "cmdline"]):
        cl = p.info["cmdline"] or []
        if p.info["pid"] == me or "--status" in cl:
            continue
        if any(os.path.realpath(c) == want for c in cl if c.endswith("postbox.py")):
            return True
    return False


# --------------------------------------------------------------------------- listener wrapping (c)

def listener_argv(argv, here):
    """The command that starts a long-lived listener. Windows: `argv` itself, tied to the daemon
    by the job object. POSIX: `argv` under postbox_child.py, which kills it if this daemon dies."""
    if IS_WINDOWS:
        return argv
    return [sys.executable, os.path.join(here, "postbox_child.py"), str(os.getpid()), "--"] + list(argv)
