"""Identify the code this process is actually running.

`docs/V1-DESIGN-RULES.md` B9: "Health exercises the work path and reports a content hash of
the running code", with the incident "a bind-mounted daemon ran stale code that no hash could
identify; health was 200 while memory skills timed out."

WHAT HEALTH REPORTS TODAY AND WHY IT CANNOT SATISFY THAT. `meshychat/urls.py:198` reports
`os.environ.get("MESHYCHAT_GIT_SHA") or "unknown"`. That is a BUILD-TIME environment variable. It
describes the commit someone built an image from, and it keeps saying so no matter what code
the process later loads. A bind mount that shadows the image's files changes the running code
and cannot change that string, which is precisely the incident. The value is still worth
reporting, it just answers a different question than the rule asks. (Since 2026-09-25 the
route also reports `running()` below as `code_sha256`, beside the call worker's recorded hash:
sql_meshychat/022, divergence 041's dated paragraph.)

TWO MEASUREMENTS HERE, because the incident has two halves and one digest cannot cover both.

`fingerprint()` answers "which tree is on disk". A digest over the served source files, read as
BYTES and never newline-translated, for the same reason `sqlmigrate.discover` reads bytes: a
CRLF checkout must not hash the same as an LF one.

`loaded_outside_tree()` answers "is this process running code from somewhere else". It walks
`sys.modules` for our own packages and reports any whose `__file__` resolves outside the
repository root. A shadowing install, a stale copy earlier on `sys.path`, or a bind mount
pointed at another directory all show up here, and none of them shows up in a disk digest of
the tree you THINK you are running.

STATED LIMIT, because it is the difference between this being useful and being believed too
far. `fingerprint()` reads the disk NOW. It does not read the bytes the interpreter compiled,
so a process that imported a file and kept running after that file changed underneath it will
report the NEW digest while executing the OLD code. Catching that needs the compiled bytes,
which CPython does not expose for timestamp-based pyc files. So the honest claim is: this
identifies the tree and detects code loaded from outside it, and it does not prove the loaded
bytes match the disk.
"""
from __future__ import annotations

import hashlib
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

#: The packages this server serves requests from. Declared rather than discovered, so adding
#: a package is a decision and a fingerprint cannot silently start covering less.
SERVED_PACKAGES: tuple[str, ...] = ("apps", "meshychat")

#: Not code this process runs. `tests` is excluded deliberately: a fingerprint that moved when
#: a test changed would report a different running server for an identical server.
EXCLUDED_DIR_NAMES = frozenset({"__pycache__", ".venv", ".git", "tests", "node_modules"})


def served_files() -> list[Path]:
    """Every .py file under the served packages, sorted, excluding the directories above.

    Sorted because a digest over an unordered walk is not reproducible, and an
    irreproducible fingerprint is worse than none: it reports a change on every call.
    """
    out: list[Path] = []
    for package in SERVED_PACKAGES:
        base = ROOT / package
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if EXCLUDED_DIR_NAMES.intersection(path.relative_to(ROOT).parts):
                continue
            out.append(path)
    return sorted(out)


def fingerprint(*, short: bool = True) -> str:
    """Digest of the served source on disk. Same tree in, same string out.

    The relative path is hashed alongside each file's bytes, so moving a file changes the
    fingerprint even when its content does not. Without that, a rename is invisible.
    """
    h = hashlib.sha256()
    for path in served_files():
        rel = path.relative_to(ROOT).as_posix()
        body = path.read_bytes()  # bytes: a CRLF checkout must not hash as an LF one
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(len(body).to_bytes(8, "big"))
        h.update(body)
    digest = h.hexdigest()
    return digest[:12] if short else digest


_RUNNING: str | None = None
_RUNNING_LOCK = threading.Lock()


def running() -> str:
    """The FULL fingerprint of the served tree as THIS PROCESS first read it, memoized.

    Rule B9 as the operator decided it on 2026-09-25 (divergence 041's dated paragraph):
    `run_call_worker` records this on every tick (sql_meshychat/022), and `--health` and
    `/v1/health` compare it with their own. It is computed once, at the first call, which
    apps/calls/apps.py makes at django.setup(), so it describes the tree the process started
    from rather than the disk now: a worker that kept running after the tree changed keeps
    reporting the OLD hash, and the checker, started on the new tree, sees them differ.
    `fingerprint()` re-reads the disk every call and could never show that. The stated limit
    above still holds: this is the disk at start, not the bytes the interpreter compiled, so a
    module first imported after the tree changed is not described by it."""
    global _RUNNING
    with _RUNNING_LOCK:
        if _RUNNING is None:
            _RUNNING = fingerprint(short=False)
        return _RUNNING


def file_count() -> int:
    """How many files the fingerprint covered.

    Reported beside the digest on purpose. A digest alone cannot distinguish "hashed the
    whole tree" from "hashed nothing and returned the digest of an empty stream", and the
    empty-stream digest is a perfectly stable, perfectly useless string.
    """
    return len(served_files())


def loaded_outside_tree() -> dict[str, str]:
    """{module name: file} for loaded modules of ours that live outside the repository.

    Empty is the healthy answer. A non-empty result means this process is executing code
    from a path other than the tree `fingerprint()` just described, which is the shape of
    the incident B9 names and is invisible to any digest of the intended tree.
    """
    out: dict[str, str] = {}
    for name, module in list(sys.modules.items()):
        if name.split(".", 1)[0] not in SERVED_PACKAGES:
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            continue  # namespace package, nothing to locate
        try:
            resolved = Path(origin).resolve()
        except OSError:  # pragma: no cover - unresolvable path is itself suspicious
            out[name] = origin
            continue
        if not resolved.is_relative_to(ROOT):
            out[name] = str(resolved)
    return out


def report() -> dict:
    """Everything above in one shape, for a caller that wants to log or serve it."""
    return {
        "code_fingerprint": fingerprint(),
        "code_files": file_count(),
        "code_loaded_outside_tree": loaded_outside_tree(),
    }
