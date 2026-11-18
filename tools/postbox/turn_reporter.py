"""Independent, bounded turn-report loop beside context uploads; no transcript bytes leave the box."""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import time

try:
    from . import context_worker, postbox_os, turnstate, unsurfaced, usage_source
except ImportError:  # postbox.py also runs directly from its installed directory
    import context_worker
    import postbox_os
    import turnstate
    import unsurfaced
    import usage_source

CHECK_SECONDS = 30.0
REFRESH_SECONDS = 120.0
CHILD_TIMEOUT_SECONDS = 15.0
MAX_BACKOFF_SECONDS = 300.0
# After a report carrying usage fails and the same turn pair then goes without it, usage alone waits this long,
# doubling to an hour (pxx P2-2: a usage failure must not cost the turn report, a24851 3).
USAGE_BACKOFF_SECONDS = 300.0
USAGE_MAX_BACKOFF_SECONDS = 3600.0
MAX_SOURCES = 256
MAX_LEDGERS = 512
UNSURFACED_DIALECTS = ("claude", "codex")


def _is_kimi_subagent_path(path: str) -> bool:
    parts = [part.lower() for part in path.replace("\\", "/").split("/") if part]
    if len(parts) < 3 or parts[-1] != "wire.jsonl":
        return False
    return parts[-3] == "subagents" or (parts[-3] == "agents" and parts[-2] != "main")


def _header_excludes_subagent(path: str, first: dict, handle) -> bool:
    """Return True when verified initial records or Kimi layout mark a child session."""
    payload = first.get("payload") if first.get("type") == "session_meta" else None
    source = payload.get("source") if isinstance(payload, dict) else None
    if isinstance(source, dict) and "subagent" in source:
        return True
    step_index = first.get("step_index")
    if isinstance(step_index, int) and not isinstance(step_index, bool) and isinstance(first.get("created_at"), str):
        return first.get("type") != "USER_INPUT"
    if first.get("type") == "metadata" and isinstance(first.get("protocol_version"), str):
        if _is_kimi_subagent_path(path):
            return True
        if "created_at" not in first:
            return False
        second = handle.readline(turnstate.TAIL_BYTES)
        if not second:
            return False
        if not second.endswith(b"\n"):
            raise ValueError("source_header")
        second_obj = json.loads(second)
        if not isinstance(second_obj, dict):
            raise ValueError("source_header")
        if "agentId" in second_obj and second_obj.get("agentId") != "main":
            return True
    return False


def reservations(identities, store_root) -> dict:
    """Read-only inode ownership view; never invoke the uploader's enrollment upgrades."""
    ledgers = {os.path.join((ident.get("context") or {}).get("store_dir") or
                           os.path.join(store_root, ident["name"]), "enrollment.json")
               for ident in identities}
    if len(ledgers) > MAX_LEDGERS:
        raise ValueError("ledger_limit")
    if os.path.isdir(store_root):
        with os.scandir(store_root) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    ledgers.add(os.path.join(entry.path, "enrollment.json"))
                if len(ledgers) > MAX_LEDGERS:
                    raise ValueError("ledger_limit")
    owners = {}
    for path in ledgers:
        ledger = context_worker.read_ledger(path)
        for entry in ledger["files"].values():
            seat = entry.get("seat")
            for inode in context_worker._entry_reserved_inodes(entry):
                owners[inode] = seat if seat and (inode not in owners or owners[inode] == seat) else "__CONFLICT__"
        for inode, seat in context_worker._superseded_reserved_inodes(ledger.get("superseded")):
            owners[inode] = seat if seat and (inode not in owners or owners[inode] == seat) else "__CONFLICT__"
    return owners


def run_child(ident: dict, payload: dict, usage: dict | None = None) -> bool:
    """One proved CLI request, windowless with bounded time and output. Never pass a transcript path.

    A usage snapshot goes in a private temporary file (--usage-file), removed when the child is done."""
    tmp = None
    try:
        if usage is not None:
            tmp = tempfile.mkdtemp(prefix="turn-usage-")
            usage_path = os.path.join(tmp, "usage.json")
            with open(usage_path, "x", encoding="utf-8") as fh:
                json.dump(usage, fh)
        return _run_child(ident, payload, None if tmp is None else usage_path)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _run_child(ident: dict, payload: dict, usage_path: str | None) -> bool:
    argv = [ident.get("python") or sys.executable, "-m", "tools.meshy_node", "turn",
            "--base-url", ident["base_url"], "--state-file", ident["state"],
            "--state", payload["state"], "--item-at", payload["item_at"]]
    if payload.get("unsurfaced") is not None:
        argv.extend(["--unsurfaced", str(payload["unsurfaced"]), "--unsurfaced-since", payload["unsurfaced_since"]])
        if payload.get("unsurfaced_oldest_at") is not None:
            argv.extend(["--unsurfaced-oldest-at", payload["unsurfaced_oldest_at"]])
    if usage_path is not None:
        argv.extend(["--usage-file", usage_path])
    result = postbox_os.run_bounded(
        argv, cwd=ident["client"], stdin=subprocess.DEVNULL, creationflags=postbox_os.NO_WINDOW,
        timeout=CHILD_TIMEOUT_SECONDS, max_stdout=8192, max_stderr=4096,
        text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
    if result.returncode != 0:
        return False
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        return False
    return isinstance(answer, dict) and answer.get("ok") is True and answer.get("applied") is True


class Reporter:
    """One identity, bounded by its current enrolled sources and MAX_SOURCES.

    Only sizes and record timestamps select a main session. A bounded first-record read
    excludes Codex subagents even after their metadata leaves the tail. Marker-free
    supported output is open: a new turn start may have left the tail between checks.
    """
    def __init__(self, tracker=None):
        self.tracker = tracker
        self.files = {}
        self.sent = None
        self.sent_at = None
        self.sent_usage = None  # the last usage snapshot the server applied
        self.usage_failures = 0
        self.usage_retry_at = 0.0
        self.next_try = 0.0
        self.failures = 0

    def observe(self, sources) -> dict | None:
        if len(sources) > MAX_SOURCES:
            raise ValueError("source_limit")
        self.files = {path: value for path, value in self.files.items() if path in sources}
        for path in sources:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or context_worker._is_reparse_or_link(before):
                raise ValueError("source_identity")
            signature = (before.st_dev, before.st_ino, before.st_size)
            old = self.files.get(path)
            if old is not None and old[0] == signature:
                continue
            with open(path, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != signature[:2]:
                    raise ValueError("source_identity")
                # Child transcripts across Codex (session_meta), Antigravity (line 0 non-USER_INPUT),
                # and Kimi (/subagents/, /agents/<non-main>/, or line 1 agentId != main) cannot override
                # a parent session even after their startup records scroll past the tail.
                header = handle.readline(turnstate.TAIL_BYTES)
                if not header.endswith(b"\n"):
                    raise ValueError("source_header")
                first = json.loads(header)
                if not isinstance(first, dict):
                    raise ValueError("source_header")
                if _header_excludes_subagent(path, first, handle):
                    self.files[path] = (signature, None, None)
                    continue
                handle.seek(max(0, before.st_size - turnstate.TAIL_BYTES))
                tail = handle.read(turnstate.TAIL_BYTES)
            state, newest = turnstate.classify(tail)
            # A replaced/truncated source cannot inherit a previous session's closed state.
            previous = old if old and old[0][:2] == signature[:2] and before.st_size >= old[0][2] else None
            if state is None and newest is not None:
                state = "open"  # marker-free supported output can hide a new turn start
            if newest is None and previous:
                state, newest = previous[1:]
            self.files[path] = (signature, state, newest)
        candidates = [(value[2], value[1]) for value in self.files.values()
                      if value[1] in ("open", "closed") and value[2] is not None]
        if not candidates:
            return None
        # In a timestamp tie, an open session wins conservatively, never inventing idle.
        newest, state = max(candidates, key=lambda item: (item[0], item[1] == "open"))
        return {"state": state, "item_at": newest.isoformat()}

    def tick(self, ident: dict, sources, *, now: float, send=run_child, status=lambda **kw: None) -> None:
        if now < self.next_try:
            return
        try:
            payload = self.observe(sources)
            if payload is None:
                status(turn_report_result="no_supported_record")
                return
            if self.tracker is not None:
                eligible = [path for path, value in self.files.items() if value[1] in ("open", "closed")]
                try:
                    observation = self.tracker.update(ident, eligible, state=payload["state"],
                                  now=dt.datetime.now(dt.timezone.utc), status=status,
                                  expected_inodes={path: self.files[path][0][:2] for path in eligible})
                except Exception as exc:
                    # Observation failure must not suppress the independent turn pair.
                    # Exception text may contain private paths; retain only its type.
                    status(unsurfaced_error=type(exc).__name__)
                else:
                    payload.update(observation)
                    status(unsurfaced_error=None)
            # Usage rides on the turn report (kxxx, memory-bxxxxx). Its read is isolated like the
            # tracker's: a failed read sends the turn pair without usage, and the server keeps the stored snapshot.
            usage = None
            try:
                usage = usage_source.read(ident)
            except Exception as exc:
                status(usage_error=type(exc).__name__)  # the type only: the text may hold a private path
            else:
                if usage is not None and now < self.usage_retry_at:
                    usage = None  # usage is backing off after a refused report; the turn pair still goes
                else:
                    status(usage_error=None)
            if (payload == self.sent and self.sent_at is not None and now - self.sent_at < REFRESH_SECONDS
                    and not usage_source.moved(self.sent_usage, usage)):
                return
            at = dt.datetime.now(dt.timezone.utc).isoformat()
            status(turn_report_attempt_at=at)
            ok = send(ident, payload) if usage is None else send(ident, payload, usage)
            if not ok and usage is not None:
                # The report carrying usage failed: send the same turn pair without it in this tick. Only if THAT
                # goes was usage the cause (an old client without --usage-file, a stricter server), and only then
                # does usage back off. When the bare pair fails too, the server or the network is down: the turn
                # backoff below covers it, and usage keeps its count, so an outage cannot push usage to the hour
                # cap (the #559 follow-up).
                usage = None
                ok = send(ident, payload)
                if ok:
                    self.usage_failures += 1
                    self.usage_retry_at = now + min(USAGE_MAX_BACKOFF_SECONDS,
                                                    USAGE_BACKOFF_SECONDS * 2 ** min(self.usage_failures - 1, 4))
                    status(usage_error="usage_report_failed", usage_retry_in_s=self.usage_retry_at - now)
            if not ok:
                raise RuntimeError("report_failed")
        except Exception as exc:
            self.failures += 1
            self.next_try = now + min(MAX_BACKOFF_SECONDS, CHECK_SECONDS * 2 ** min(self.failures - 1, 4))
            status(turn_report_result="failed", turn_report_error=type(exc).__name__,
                   turn_report_retry_in_s=self.next_try - now)
            return
        self.sent, self.sent_at = payload, now
        if usage is not None:
            self.sent_usage = usage
            self.usage_failures = 0
        self.failures, self.next_try = 0, now + CHECK_SECONDS
        status(turn_report_at=at, turn_report_result="ok", turn_report_state=payload["state"],
               turn_item_at=payload["item_at"], turn_report_error=None, turn_report_retry_in_s=0,
               usage_as_of=None if usage is None else usage["as_of"])


def run(identities, *, store_root, all_identities, set_status, stopped, sleep):
    """An independent daemon-thread target; slow context uploads cannot hold its checks.

    Reuses enrollment validation without running discovery or enrolling any file here.
    Unsupported dialects report nothing. No periodic mail reads or presence beats.
    """
    enabled = context_worker.context_enabled_identities(identities)
    box_identities = context_worker.context_enabled_identities(all_identities)
    dirs, clashes = context_worker._seat_store_dirs(box_identities or enabled, store_root)
    reporters = {
        ident["name"]: Reporter(
            unsurfaced.Tracker(os.path.join(dirs[ident["name"]], "unsurfaced.json"))
            if (ident.get("context", {}).get("dialect") or ident.get("cli")) in UNSURFACED_DIALECTS
            else None
        )
        for ident in enabled
        if ident["name"] not in clashes
        and (ident.get("context", {}).get("dialect") or ident.get("cli")) in turnstate.SUPPORTED_DIALECTS
    }
    while not stopped():
        for ident in enabled:
            if stopped():
                return
            name = ident["name"]
            ctx = ident["context"]
            dialect = ctx.get("dialect") or ident.get("cli")
            status = lambda **kw: set_status(name, **kw)
            if dialect not in turnstate.SUPPORTED_DIALECTS or name in clashes:
                status(turn_report_result="unsupported" if name not in clashes else "store_conflict")
                continue
            try:
                # Reserve other seats' enrolled inodes too: a shared file never reports for two peers.
                reserved = reservations(all_identities, store_root)
                sources, error = context_worker.verified_enrolled_sources(
                    os.path.join(dirs[name], "enrollment.json"), seat=context_worker.resolve_seat(ident),
                    dialect=dialect, allowed_roots=ctx.get("allowed_roots") or (),
                    box_enrolled_inodes=reserved)
                if error:
                    raise ValueError("enrollment_refused")
                reporters[name].tick(ident, sources, now=time.monotonic(), status=status)
            except Exception as exc:
                status(turn_report_result="enrollment_failed", turn_report_error=type(exc).__name__)
        sleep(CHECK_SECONDS)
