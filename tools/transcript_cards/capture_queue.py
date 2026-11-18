"""Durable local wake queue for the existing loader; no network or graph writes.

An operator supplies a source configuration. Hook input is never persisted.
Requested and staged generations are separate; neither means server persistence.
The existing loader remains the only transcript parser and redaction policy.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import asdict, dataclass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
import tempfile

import transcript_cards as loader

SCHEMA = "transcript-capture-queue.v1"


class CaptureLimitError(RuntimeError):
    """A bounded staging guard rejected work; the generation stays pending."""


@dataclass(frozen=True)
class Limits:
    # Local staging defaults, not an approved production retention policy.
    source_entries: int = 4096
    source_bytes: int = 64 * 1024 * 1024
    file_bytes: int = 8 * 1024 * 1024
    cards_bytes: int = 8 * 1024 * 1024
    report_bytes: int = 2 * 1024 * 1024
    loader_seconds: int = 60
    jobs_per_pass: int = 128
    spool_entries: int = 16384
    spool_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ValueError("capture limits must be positive integers")


def inventory(root, entry_limit, byte_limit, *, kind=None, file_limit=None):
    """Bound retained spool traversal and reject indirect files.

    This is a pre/postflight size guard, not an OS memory/time quota. Selected
    source files use source_inventory instead. No existing data is deleted.
    """
    root = Path(root)
    entries, total, fingerprints = 0, 0, {}
    folders = [root]
    while folders:
        folder = folders.pop()
        with os.scandir(folder) as children:
            for child in children:
                entries += 1
                if entries > entry_limit:
                    raise CaptureLimitError("directory entry limit exceeded")
                info = child.stat(follow_symlinks=False)
                if child.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise CaptureLimitError("indirect filesystem entry requires review")
                if stat.S_ISDIR(info.st_mode):
                    if kind != "claude":
                        folders.append(Path(child.path))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise CaptureLimitError("non-regular filesystem entry requires review")
                candidate = kind is None or (
                    child.name.lower().endswith(".jsonl")
                    and (kind == "claude" or child.name.startswith("rollout-"))
                    and not loader._FORBIDDEN_NAME.search(child.name))
                if not candidate:
                    continue
                if file_limit is not None and info.st_size > file_limit:
                    raise CaptureLimitError("source file byte limit exceeded")
                total += info.st_size
                if total > byte_limit:
                    raise CaptureLimitError("total byte limit exceeded")
                fingerprints[str(Path(child.path).relative_to(root))] = (
                    info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return total, fingerprints


def source_inventory(config, spool, limits):
    root = Path(config["root"])
    resolved_spool = Path(spool).resolve()
    if root == resolved_spool or root in resolved_spool.parents or resolved_spool in root.parents:
        raise CaptureLimitError("source and spool must be disjoint")
    if len(config["files"]) > limits.source_entries:
        raise CaptureLimitError("selected source entry limit exceeded")
    total, fingerprints = 0, {}
    identities = set()
    for name in config["files"]:
        path, info = selected_file(root, name)
        identity = (info.st_dev, info.st_ino)
        if identity in identities:
            raise CaptureLimitError("selected names refer to the same source file")
        identities.add(identity)
        if info.st_size > limits.file_bytes:
            raise CaptureLimitError("source file byte limit exceeded")
        total += info.st_size
        if total > limits.source_bytes:
            raise CaptureLimitError("selected source byte limit exceeded")
        fingerprints[name] = (info.st_dev, info.st_ino, info.st_size)
    return total, fingerprints


def selected_file(root, name):
    """No discovery: check every selected path component without following links."""
    path = Path(root)
    for part in (None, *Path(name).parts):
        if part is not None:
            path = path / part
        info = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CaptureLimitError("indirect selected source requires review")
        if path != Path(root) / name and not stat.S_ISDIR(info.st_mode):
            raise CaptureLimitError("selected source parent is not a directory")
    if not stat.S_ISREG(info.st_mode):
        raise CaptureLimitError("selected source is not a regular file")
    return path, info


def private_scratch(path):
    """Restrict a newly created directory before writing any raw source bytes."""
    if os.name != "nt":
        path.chmod(0o700)
        return
    import ctypes
    from ctypes import wintypes
    system = Path(os.environ["SystemRoot"]) / "System32"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW, "timeout": 5,
               "stdin": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        result = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                                stdout=subprocess.PIPE, check=True, **options)
        fields = next(csv.reader(result.stdout.decode(errors="strict").splitlines()))
        sid = fields[-1]
        if not re.fullmatch(r"S-1-[0-9-]+", sid):
            raise ValueError("unexpected current-user identity")
        # Replace the DACL: removing inheritance alone leaves explicit defaults
        # installed by Python's Windows mode-0700 mkdir (e.g. Administrators).
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        convert.restype = wintypes.BOOL
        get_dacl = security.GetSecurityDescriptorDacl
        get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        get_dacl.restype = wintypes.BOOL
        set_acl = security.SetNamedSecurityInfoW
        set_acl.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        set_acl.restype = wintypes.DWORD
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        descriptor = ctypes.c_void_p()
        sddl = "D:P(A;OICI;FA;;;" + sid + ")(A;OICI;FA;;;SY)"
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError("could not construct scratch DACL")
        try:
            present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
            if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
                raise OSError("could not extract scratch DACL")
            if not present.value or not acl.value or set_acl(str(path), 1, 0x80000004, None, None, acl, None):
                raise OSError("could not apply protected scratch DACL")
        finally:
            kernel.LocalFree(descriptor)
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        raise CaptureLimitError("private scratch permissions could not be established") from None


def remove_scratch(spool, db, name, device, inode, limits):
    """Delete only a recorded direct child with matching filesystem identity.

    Called while holding the worker's kernel lock. Never discover ownership from
    a filename, PID, or lease. Unrecorded legacy scratch remains for review.
    """
    root = Path(spool).resolve(strict=True)
    if Path(name).name != name or not re.fullmatch(r"\.source-[a-z0-9_]+", name):
        raise CaptureLimitError("invalid scratch ownership record")
    path = root / name
    if path.parent != root:
        raise CaptureLimitError("scratch target escaped the spool")
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        pass  # Previous deletion completed before the ownership checkpoint.
    else:
        if (not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or (str(info.st_dev), str(info.st_ino)) != (device, inode)):
            raise CaptureLimitError("scratch identity changed; deletion requires review")
        # Validate all descendants without following links and bound traversal.
        inventory(path, limits.spool_entries, limits.spool_bytes)
        shutil.rmtree(path)
    with db:
        db.execute("DELETE FROM scratch WHERE name=?", (name,))


def recover_scratch(spool, db, limits):
    """Pause a known owner on cleanup failure; never guess legacy ownership.

    Quarantine is a durable database state, not a move or deletion of the suspect
    path. Global spool traversal/size guards still apply to every retained byte.
    An operator must review a quarantined row; future wakes cannot clear it.
    """
    rows = db.execute("SELECT name,device,inode,job,review FROM scratch ORDER BY name").fetchall()
    for name, device, inode, job, review in rows:
        if job is not None:
            source = db.execute("SELECT source FROM jobs WHERE id=?", (job,)).fetchone()
            if (not re.fullmatch(r"[a-f0-9]{64}", job) or source is None
                    or hashlib.sha256(source[0].encode()).hexdigest() != job):
                raise CaptureLimitError("scratch owner cannot be verified; global review required")
        if review:
            if job is None:
                raise CaptureLimitError("legacy scratch requires global review")
            continue
        try:
            remove_scratch(spool, db, name, device, inode, limits)
        except (CaptureLimitError, OSError):
            if job is None:
                raise  # Old rows have no reliable per-job attribution.
            with db:
                db.execute("UPDATE scratch SET review=1 WHERE name=?", (name,))
                db.execute("UPDATE jobs SET error='ScratchReviewRequired' WHERE id=?", (job,))
    return db.execute("SELECT COUNT(*) FROM scratch WHERE review=1").fetchone()[0]


@contextlib.contextmanager
def source_snapshot(config, spool, limits, db, job):
    """Freeze only selected complete-record prefixes for the unchanged parser.

    Appends after the snapshot stay for the next wake. Prefix replacement,
    rewrite or truncation fails capture. Ownership is committed and private
    permissions established before raw copies. A later worker cleans recorded
    crash remnants under its kernel lock; a crash is not immediate erasure.
    """
    total, fingerprints = source_inventory(config, spool, limits)
    spool_inventory(spool, limits, reserve=total + limits.cards_bytes + 65536)
    snapshot = Path(tempfile.mkdtemp(dir=spool, prefix=".source-"))
    info = snapshot.stat(follow_symlinks=False)
    scratch_identity = (snapshot.name, str(info.st_dev), str(info.st_ino))
    # A crash before this commit can leave only an empty directory, never raw
    # bytes. Register before copying so the next worker can prove ownership.
    with db:
        db.execute("INSERT INTO scratch(name,device,inode,job) VALUES(?,?,?,?)", (*scratch_identity, job))
    try:
        private_scratch(snapshot)
        prefixes = {}
        for name, identity in fingerprints.items():
            path, _ = selected_file(config["root"], name)
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != identity[:2]:
                    raise CaptureLimitError("source replaced before snapshot")
                content = stream.read(identity[2])
            if len(content) != identity[2]:
                raise CaptureLimitError("source truncated during snapshot")
            complete = content[:content.rfind(b"\n") + 1]
            destination = snapshot / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(complete)
            prefixes[name] = (identity[0], identity[1], len(complete), hashlib.sha256(complete).hexdigest())
        yield {**config, "root": str(snapshot)}, prefixes
        for name, prefix in prefixes.items():
            path, info = selected_file(config["root"], name)
            if (info.st_dev, info.st_ino) != prefix[:2] or info.st_size < prefix[2]:
                raise CaptureLimitError("source replaced or truncated during capture")
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != prefix[:2]:
                    raise CaptureLimitError("source replaced during verification")
                digest = hashlib.sha256(stream.read(prefix[2])).hexdigest()
            if digest != prefix[3]:
                raise CaptureLimitError("source complete-record prefix changed during capture")
    finally:
        remove_scratch(spool, db, *scratch_identity, limits)


def spool_inventory(spool, limits, reserve=0):
    total, _ = inventory(spool, limits.spool_entries, limits.spool_bytes)
    if total + reserve > limits.spool_bytes:
        raise CaptureLimitError("spool byte budget exhausted; retained batches require review")
    return total


def bounded_cards(path, limit):
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise CaptureLimitError("cards byte limit exceeded")
    return content


def run_process(command, timeout):
    """The pure local loader has no descendants; reap it on timeout.

    Never emit its stdout/stderr (which can include source paths or text). The
    loader's bounded report is read separately from the generation store.
    """
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with subprocess.Popen(command, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          **options) as process:
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise CaptureLimitError("loader time limit exceeded") from None
    if code:
        raise ValueError("loader failed; inspect the approved local source before retry")


def run_bounded_loader(config, store, limits):
    # The loader uses only stdlib. Avoid Windows venv redirector processes so
    # killing/reaping this child also ends the actual parser owning its files.
    command = [getattr(sys, "_base_executable", sys.executable), str(Path(loader.__file__).resolve()),
               "--seat", config["seat"], "--out", str(store), "--rebuild",
               "--codex-sessions" if config["kind"] == "codex" else "--project-dir",
               config["root"], "--files",
               *(str(Path(config["root"]) / name) for name in config["files"])]
    run_process(command, limits.loader_seconds)
    return json.loads(bounded_cards(store / "report.json", limits.report_bytes))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_config(value):
    if set(value) != {"seat", "kind", "root", "files"}:
        raise ValueError("source requires exactly seat, kind, root and selected files")
    if not isinstance(value["seat"], str) or not value["seat"].strip():
        raise ValueError("source seat must be nonempty")
    if value["kind"] not in ("codex", "claude"):
        raise ValueError("unsupported source dialect")
    files = value["files"]
    if not isinstance(files, list) or not files or any(not isinstance(p, str) or not p for p in files):
        raise ValueError("source requires an operator-selected nonempty file list")
    selected = []
    for name in files:
        path = Path(name)
        posix, windows = PurePosixPath(name), PureWindowsPath(name)
        if (posix.anchor or windows.anchor
                or ".." in posix.parts or ".." in windows.parts
                or path == Path(".")):
            raise ValueError("selected files must be relative descendants of the source root")
        # Check raw components before path normalization hides trailing dots or
        # Windows interprets drive, stream, or device syntax. Both separators
        # are unsafe regardless of the host running this configuration check.
        components = name.replace("\\", "/").split("/")
        if any(":" in component for component in components):
            raise ValueError("selected file component contains a colon")
        if any(component.endswith((".", " ")) for component in components):
            raise ValueError("selected file component has a trailing dot or space")
        # Conservatively refuse device aliases on every host, including zero
        # and superscript port digits. Windows also ignores spaces in the
        # device stem before an extension (for example, CON .jsonl).
        if any(re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|"
                            r"COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3]",
                            component.split(".", 1)[0].rstrip(" "), re.IGNORECASE)
               for component in components):
            raise ValueError("selected file component has a reserved device name")
        if not name.lower().endswith(".jsonl") or loader._FORBIDDEN_NAME.search(path.name):
            raise ValueError("unsupported or forbidden selected file")
        if value["kind"] == "codex" and not path.name.startswith("rollout-"):
            raise ValueError("Codex requires a native rollout filename")
        selected.append(path.as_posix())
    if len({os.path.normcase(name) for name in selected}) != len(selected):
        raise ValueError("duplicate selected file")
    if len({os.path.normcase(Path(name).name) for name in selected}) != len(selected):
        raise ValueError("loader requires distinct selected basenames")
    # Finish every lexical refusal before touching even the source root.
    raw_root = Path(value["root"]).expanduser()
    info = raw_root.stat(follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise CaptureLimitError("indirect source root requires review")
    root = raw_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("source root must be an operator-approved directory")
    return {"seat": value["seat"], "kind": value["kind"], "root": str(root), "files": sorted(selected)}


def database(spool):
    spool = Path(spool)
    spool.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(spool / "capture.sqlite", timeout=2)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, requested INTEGER NOT NULL,
        staged INTEGER NOT NULL DEFAULT 0, batch TEXT, error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS scratch (
        name TEXT PRIMARY KEY, device TEXT NOT NULL, inode TEXT NOT NULL)""")
    if "attempts" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
        db.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    if "observed" not in job_columns:
        db.execute("ALTER TABLE jobs ADD COLUMN observed TEXT")
    if "scan_attempts" not in job_columns:
        db.execute("ALTER TABLE jobs ADD COLUMN scan_attempts INTEGER NOT NULL DEFAULT 0")
    scratch_columns = {row[1] for row in db.execute("PRAGMA table_info(scratch)")}
    if "job" not in scratch_columns:
        db.execute("ALTER TABLE scratch ADD COLUMN job TEXT")
    if "review" not in scratch_columns:
        db.execute("ALTER TABLE scratch ADD COLUMN review INTEGER NOT NULL DEFAULT 0")
    db.commit()
    return db


def enqueue(spool, config):
    config = source_config(config)
    text = canonical(config)
    key = hashlib.sha256(text.encode()).hexdigest()
    with contextlib.closing(database(spool)) as db, db:
        db.execute("""INSERT INTO jobs(id,source,requested) VALUES(?,?,1)
            ON CONFLICT(id) DO UPDATE SET requested=requested+1""", (key, text))
        generation = db.execute("SELECT requested FROM jobs WHERE id=?", (key,)).fetchone()[0]
    return {"schema": SCHEMA, "job": key, "requested": generation, "phase": "queued"}


def capture_mode(db, mode):
    """Bind a staging worker to the spool format before it can complete wakes.

    Enqueue, scan and wake_enrolled only request work; they select no format.
    """
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS capture_mode (id INTEGER PRIMARY KEY, mode TEXT NOT NULL)")
        current = db.execute("SELECT mode FROM capture_mode WHERE id=1").fetchone()
        if current and current[0] != mode:
            raise CaptureLimitError("capture mode mismatch; use a separately enrolled spool")
        if not current:
            if mode != "snapshot" and db.execute("SELECT 1 FROM jobs WHERE staged>0 OR batch IS NOT NULL LIMIT 1").fetchone():
                raise CaptureLimitError("snapshot spool requires explicit migration review")
            db.execute("INSERT OR IGNORE INTO capture_mode VALUES(1,?)", (mode,))


def wake_enrolled(spool, job):
    """Durably wake one existing enrollment without transcript IO or discovery.

    The wake is format-neutral, like enqueue/scan: the selected staging worker
    checks capture_mode before advancing staged. A wake neither selects nor
    changes the spool format.
    This does not dispatch a worker or clear pending review/rejection state.
    Never create or migrate a spool here: a missing enrollment is a failure,
    not permission to choose a transcript from a lifecycle hook's input.
    """
    if not isinstance(job, str) or not re.fullmatch(r"[a-f0-9]{64}", job):
        raise ValueError("existing enrolled job identity required")
    spool = Path(spool)
    path = spool / "capture.sqlite"
    for candidate, directory in ((spool, True), (path, False)):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CaptureLimitError("indirect enrollment spool requires review")
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise CaptureLimitError("existing enrollment spool required")
    # mode=rw refuses a missing DB. The operator-owned spool and its SQLite
    # sidecars must already have the host's reviewed private access controls.
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw",
                                           uri=True, timeout=2)) as db, db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT source,requested FROM jobs WHERE id=?", (job,)).fetchone()
        if row is None:
            raise ValueError("job is not enrolled")
        source, requested = row
        if not isinstance(source, str) or hashlib.sha256(source.encode()).hexdigest() != job:
            raise ValueError("enrollment identity mismatch")
        if type(requested) is not int or not 1 <= requested < 2**63 - 1:
            raise CaptureLimitError("enrollment generation requires review")
        generation = requested + 1
        db.execute("UPDATE jobs SET requested=? WHERE id=?", (generation, job))
    # Returning only after commit prevents an interrupted write from claiming
    # a durable wake. Staging, server persistence and indexing are separate.
    return {"schema": SCHEMA, "job": job, "requested": generation,
            "phase": "queued", "server_persistence": "unverified",
            "index_visibility": "unverified"}


def scan(spool, limits=None):
    """Bounded metadata catch-up for enrolled files; no discovery or parsing.

    The first observation queues a wake, even if a hook already queued one.
    A changed observation and its wake commit together. This is a wake hint,
    not proof of unchanged bytes: an adversarial metadata-preserving rewrite
    is outside its contract. Capture still validates the complete prefix.
    """
    limits = limits or Limits()
    results, failures = [], []
    with contextlib.closing(database(spool)) as db:
        rows = db.execute("""SELECT id,source FROM jobs
                          ORDER BY scan_attempts,id LIMIT ?""",
                          (limits.jobs_per_pass,)).fetchall()
        for key, text in rows:
            with db:
                db.execute("UPDATE jobs SET scan_attempts=scan_attempts+1 WHERE id=?", (key,))
            try:
                if hashlib.sha256(text.encode()).hexdigest() != key:
                    raise CaptureLimitError("enrolled source identity requires review")
                config = source_config(json.loads(text))
                if len(config["files"]) > limits.source_entries:
                    raise CaptureLimitError("selected source entry limit exceeded")
                metadata = []
                for name in config["files"]:
                    _, info = selected_file(config["root"], name)
                    metadata.append((name, info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns, info.st_ctime_ns))
                observation = hashlib.sha256(canonical(metadata).encode()).hexdigest()
                # Compare under the SQLite writer lock. Two overlapping scans
                # must not both queue a wake for the same observation.
                db.execute("BEGIN IMMEDIATE")
                try:
                    changed = db.execute("""UPDATE jobs SET observed=?,requested=requested+1
                                          WHERE id=? AND observed IS NOT ?""",
                                         (observation, key, observation)).rowcount
                    generation = db.execute("SELECT requested FROM jobs WHERE id=?", (key,)).fetchone()[0]
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                results.append({"job": key, "changed": bool(changed), "requested": generation,
                                "phase": "queued" if changed else "unchanged"})
            except Exception as exc:
                with db:
                    db.execute("UPDATE jobs SET error='CaptureScanError' WHERE id=?", (key,))
                failures.append(exc)
    if failures:
        raise failures[0]
    return results


@contextlib.contextmanager
def worker_lock(spool):
    """Kernel-owned lock survives a process crash without a stale PID/lease guess."""
    path = Path(spool) / "worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def durable_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".capture-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stage(spool, run_loader=None, before_commit=None, limits=None):
    """One-pass drain. A wake arriving during capture remains pending.

    Batch manifest is durable before the staged checkpoint. A failed run leaves
    the requested generation pending. Error messages contain no transcript text.
    run_loader/before_commit are seams for synthetic crash/concurrency proofs.
    """
    spool = Path(spool)
    limits = limits or Limits()
    results = []
    failures = []
    with worker_lock(spool), contextlib.closing(database(spool)) as db:
        capture_mode(db, "snapshot")
        # No live worker owns a raw snapshot while this lock is held. Clean only
        # recorded snapshots, including when there is no pending capture job.
        if recover_scratch(spool, db, limits):
            failures.append(CaptureLimitError("scratch quarantined; affected jobs require operator review"))
        rows = db.execute("""SELECT id,source,requested FROM jobs WHERE requested>staged
                          AND NOT EXISTS (SELECT 1 FROM scratch WHERE scratch.job=jobs.id AND review=1)
                          ORDER BY attempts,id LIMIT ?""",
                          (limits.jobs_per_pass,)).fetchall()
        for key, text, generation in rows:
            # Commit scheduling progress before dispatch, including crashes. A
            # failed low-hash job cannot monopolize a bounded pass across wakes.
            with db:
                db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (key,))
            try:
                config = source_config(json.loads(text))
                # Reserve room for a cards batch plus its small manifest before
                # parsing. Loader temporary/output growth is checked afterwards,
                # not hard-limited by these preflight guards.
                spool_inventory(spool, limits, reserve=limits.cards_bytes + 65536)
                # Full rescan is deliberate in this first staging path:
                # the loader's separate file replacements are not a transaction.
                # A power loss must never leave a new cursor beside an old card file.
                store = spool / "stores" / key / str(generation)
                with source_snapshot(config, spool, limits, db, key) as (frozen, prefixes):
                    if run_loader is None:
                        report = run_bounded_loader(frozen, store, limits)
                    else:
                        report = run_loader(
                            frozen["root"] if config["kind"] == "claude" else None,
                            config["seat"], str(store),
                            codex_dir=frozen["root"] if config["kind"] == "codex" else None,
                            files=[str(Path(frozen["root"]) / name) for name in config["files"]],
                            rebuild=True)
                spool_inventory(spool, limits)
                if report.get("bad_lines"):
                    raise ValueError("malformed complete transcript records require review")
                if not report.get("files"):
                    raise ValueError("no supported transcript selected; capture remains pending")
                content = bounded_cards(store / "cards.jsonl", limits.cards_bytes)
                digest = hashlib.sha256(content).hexdigest()
                batch_dir = spool / "batches" / key / str(generation)
                manifest = {"schema": SCHEMA, "job": key, "generation": generation,
                            "seat": config["seat"], "dialect": config["kind"],
                            "loader_version": loader.LOADER_VERSION,
                            "loader_sha256": hashlib.sha256(Path(loader.__file__).read_bytes()).hexdigest(),
                            "cards_sha256": digest, "report": report, "limits": asdict(limits),
                            "phase": "staged", "server_persistence": "unverified",
                            "index_visibility": "unverified"}
                manifest_bytes = canonical(manifest).encode()
                spool_inventory(spool, limits, reserve=len(content) + len(manifest_bytes))
                durable_file(batch_dir / "cards.jsonl", content)
                durable_file(batch_dir / "manifest.json", manifest_bytes)
                spool_inventory(spool, limits)
                if before_commit:
                    before_commit(key, generation)
                with db:
                    db.execute("UPDATE jobs SET staged=?,batch=?,error=NULL WHERE id=?",
                               (generation, str(batch_dir), key))
                results.append({"job": key, "staged": generation, "phase": "staged"})
            except Exception as exc:
                with db:
                    db.execute("UPDATE jobs SET error=? WHERE id=?", (type(exc).__name__, key))
                failures.append(exc)
    if failures:
        # Preserve the failure for callers after independent jobs got their turn.
        # Successful checkpoints and failed pending generations are durable.
        raise failures[0]
    return results


def status(spool):
    with contextlib.closing(database(spool)) as db:
        rows = db.execute("SELECT id,requested,staged,batch,error FROM jobs ORDER BY id").fetchall()
        reviewed = dict(db.execute("SELECT job,COUNT(*) FROM scratch WHERE review=1 GROUP BY job"))
        blocked = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='incremental_pending'").fetchone():
            for job, state in db.execute("SELECT job,state FROM incremental_pending ORDER BY job,source"):
                blocked.setdefault(job, []).append(json.loads(state))
    return [{"job": row[0], "requested": row[1], "staged": row[2],
             "pending": row[1] > row[2], "batch": row[3], "error": row[4],
             "scratch_review_count": reviewed.get(row[0], 0),
             "blocked_records": blocked.get(row[0], []),
             "server_persistence": "unverified", "index_visibility": "unverified"} for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("enqueue", "wake", "scan", "stage", "incremental", "status"))
    parser.add_argument("--spool", required=True)
    parser.add_argument("--source", help="operator-owned JSON file with seat/kind/root/files")
    parser.add_argument("--job", help="existing enrolled identity (wake only)")
    parser.add_argument("--limits", help="operator-owned JSON overrides for local staging limits")
    parser.add_argument("--window", help="operator-owned JSON bounded reader overrides (incremental only)")
    args = parser.parse_args()
    if args.command == "wake":
        if not args.job or args.source or args.limits or args.window:
            parser.error("wake requires --job and accepts no source/reader overrides")
        try:
            result = wake_enrolled(args.spool, args.job)
        except Exception as exc:
            print(canonical({"phase": "pending", "error": type(exc).__name__}), file=sys.stderr)
            raise SystemExit(1) from None
    elif args.job:
        parser.error("--job is only valid with wake")
    elif args.command == "enqueue":
        if not args.source:
            parser.error("enqueue requires --source")
        result = enqueue(args.spool, json.loads(Path(args.source).read_text(encoding="utf-8")))
    elif args.command == "incremental":
        # Lazy import: incremental_store uses private_scratch from this module.
        from incremental_queue import stage_incremental
        from native_window import Budget
        try:
            limits = Limits(**json.loads(Path(args.limits).read_text(encoding="utf-8"))) if args.limits else Limits()
            budget = Budget(**json.loads(Path(args.window).read_text(encoding="utf-8"))) if args.window else None
            result = stage_incremental(args.spool, limits=limits, budget=budget)
        except Exception as exc:
            print(canonical({"phase": "pending", "error": type(exc).__name__}), file=sys.stderr)
            raise SystemExit(1) from None
    elif args.command in ("scan", "stage"):
        limits = Limits(**json.loads(Path(args.limits).read_text(encoding="utf-8"))) if args.limits else Limits()
        result = (scan if args.command == "scan" else stage)(args.spool, limits=limits)
    else:
        result = status(args.spool)
    print(canonical(result))


if __name__ == "__main__":
    main()
