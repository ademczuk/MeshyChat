"""context_worker.py - per-box postbox transcript-context capture worker (ingest plan Steps 3 and 10).

Runs on ONE daemon thread per box inside postbox.py, completely isolated from mail listener, reconcile,
and delivery threads. Walks context-enabled local identities round-robin, enforces file-identity enrollment
before any transcript is touched, checks the macOS interpreter-match guard (#355), paces passes to the
Vienna daytime (2 writes/s) and night-backfill (8 writes/s, latching to 2 writes/s on two consecutive
slow/failed /v1/livez probes) budgets, invokes `tools.transcript_cards.context_upload` one pass at a time,
and projects sanitized `context_held` diagnostics (held >= 1 hour, zero transcript text or file paths)
into postbox `status.json`.
"""
from __future__ import annotations

import datetime
import inspect
import json
import math
import fnmatch
import os
import re
import stat
import subprocess
import sys
import time
import traceback
from typing import Any
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import postbox_os  # noqa: E402

DAY_RATE_PER_S = 2.0
NIGHT_RATE_PER_S = 8.0
# 1 batch holds <= 32 items + 1 manifest = 33 records = 66 calls (memory.write + signed memory.read),
# staying safely inside the 120-call per-peer burst bucket shared with mail (kxxx).
DAY_MAX_BATCHES = 1
NIGHT_MAX_BATCHES = 1
# Per-seat steady-state budgets (kxxx, memory-bxxxxx):
# 0.25 records/s day (0.50 calls/s = 50% of 1 call/s refill); 0.35 records/s Vienna 22:00..07:00 night (~70%).
SEAT_DAY_RATE_PER_S = 0.25
SEAT_NIGHT_RATE_PER_S = 0.35
RATE_LIMIT_BACKOFF_FACTOR = 1.25
DEFAULT_RATE_LIMIT_WAIT_S = 10.0
MAX_RATE_LIMIT_WAIT_S = 300.0
LIVEZ_INTERVAL_S = 60.0
LIVEZ_SLOW_S = 2.0
LIVEZ_LATCH_STREAK = 2
HELD_REPORT_AGE_S = 3600
DEFAULT_IDLE_SLEEP_S = 5.0
DEFAULT_DRAINED_BACKOFF_S = 60.0
DEFAULT_CHILD_TIMEOUT_S = 180.0
REPARSE_POINT_ATTR = 0x400
MAX_LEDGER_BYTES = 16 * 1048576
MAX_STATE_BYTES = 1048576
# The upload child gets every enrolled source, and the ledger only grows. Windows refuses to start a process whose
# command line passes 32,767 characters (WinError 206), which stopped HQB's codex-bxxxxx-cli silently at 431 files on
# 7 Nov 2026 (dxxxxx). Past this budget the list goes to a private file in the seat's store instead.
CMDLINE_BUDGET_CHARS = 30000
UPLOAD_SOURCES_FILE = "upload-sources.json"
# context.discover (goal 27 Oct, item 1): a seat's new session files are found and enrolled on their own, since every
# new Claude or Codex session writes a NEW transcript file and a fixed pins list stops at the next session.
DISCOVER_MAX_PER_ROUND = 8          # new enrollments per identity per discovery, oldest first: a backlog never stalls
DISCOVER_INTERVAL_S = 300.0         # an identity's roots are walked at most this often
DISCOVER_DEFAULT_MAX_AGE_H = 48.0   # only files written this recently are candidates
DISCOVER_MAX_ENTRIES = 200_000      # directory entries one root's walk may visit
# context.discover "seat_marker" (2 Nov, seat-handover item 1): in a root that holds more than one seat's sessions, a
# candidate qualifies only if its first SEAT_MARKER_PREFIX_BYTES hold a meshy channel_open naming one of this seat's
# identities. Claude transcripts only: Codex, Kimi and antigravity ones may carry no channel_open record. Where the
# first one lands varies by box: within 26 KB in every HQB channel session measured on 2 Nov, at byte 498,395 on HQD
# (kxxx's 218956a8), behind SessionStart hook output and attachments. 1 MiB covers both with room.
SEAT_MARKER_PREFIX_BYTES = 1024 * 1024
_MARKER_WALK_DEPTH = 6
# Optional Codex session ownership check: one complete JSONL header, including its newline.
SESSION_META_MAX_LINE_BYTES = 256 * 1024
# Optional Antigravity main-session ownership check: canonical <uuid>/.system_generated/logs/transcript*.jsonl layout,
# complete first USER_EXPLICIT/USER_INPUT JSONL header, and startup prefix and/or pinned conversation-id file.
ANTIGRAVITY_HEADER_MAX_LINE_BYTES = 256 * 1024
ANTIGRAVITY_SESSION_ID_MAX_BYTES = 4096
DISCOVER_SPEC_KEYS = frozenset({"root", "pattern", "recursive", "max_age_h", "exclude_globs", "seat_marker",
                                "session_meta", "antigravity_session"})
SESSION_META_FILTER_KEYS = frozenset({"originator", "cwd_prefix"})
ANTIGRAVITY_SESSION_FILTER_KEYS = frozenset({"session_id_file", "startup_prefixes"})
DEFAULT_ANTIGRAVITY_STARTUP_PREFIXES = (
    "Session resumed. Per GEMINI.md",
    "You are antigravity, an AI node on the AgentMeshy mesh.",
    "You are antigravity, an autonomous node on the AgentMeshy dev mesh.",
    "You are the antigravity node",
    "You are the ANTIGRAVITY node",
    'You are joining the AgentMeshy mesh as the node "antigravity".',
)
_ANTIGRAVITY_CONV_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# A seat whose enrolled files under a discover root have all had no new bytes for DISCOVER_STALL_H, while another file
# there has been written within it, has probably moved to a session nobody enrolled: one note per file per UTC day.
DISCOVER_STALL_H = 6.0

_VIENNA_ZONEINFO: Any = None  # None = unprobed, False = unavailable (stdlib EU CET/CEST fallback), else ZoneInfo
VALID_DIALECTS = frozenset({"claude", "codex", "antigravity", "kimi"})
HELD_KEYS = ("src", "gen", "batch", "first_ord", "last_ord", "code", "age_s")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# meshy_channel.py pushes `<channel source="plugin:meshy:meshy" kind="channel_open" identity="<name>" ...>` as the
# content of a Claude "user" record; attribute order is the client's, so attributes are read by name.
_CHANNEL_TAG_RE = re.compile(r"<channel\b([^>]*)>")
_TAG_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utc_dt() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _eu_central_offset(utc_dt: datetime.datetime) -> datetime.timezone:
    """Pure-stdlib EU Central Time (Europe/Vienna: CET UTC+1, CEST UTC+2) when OS/pip tzdata is absent."""
    y = utc_dt.year
    d_mar31 = datetime.date(y, 3, 31)
    last_sun_mar = 31 - ((d_mar31.weekday() + 1) % 7)
    d_oct31 = datetime.date(y, 10, 31)
    last_sun_oct = 31 - ((d_oct31.weekday() + 1) % 7)
    cest_start = datetime.datetime(y, 3, last_sun_mar, 1, 0, tzinfo=datetime.timezone.utc)
    cest_end = datetime.datetime(y, 10, last_sun_oct, 1, 0, tzinfo=datetime.timezone.utc)
    if cest_start <= utc_dt < cest_end:
        return datetime.timezone(datetime.timedelta(hours=2), "CEST")
    return datetime.timezone(datetime.timedelta(hours=1), "CET")


def _vienna_local(dt: datetime.datetime) -> datetime.datetime:
    """Convert `dt` to `Europe/Vienna` lazily without requiring `tzdata` at import or runtime."""
    global _VIENNA_ZONEINFO
    utc_dt = (
        dt.astimezone(datetime.timezone.utc)
        if dt.tzinfo is not None
        else dt.replace(tzinfo=datetime.timezone.utc)
    )
    if _VIENNA_ZONEINFO is None:
        try:
            from zoneinfo import ZoneInfo

            _VIENNA_ZONEINFO = ZoneInfo("Europe/Vienna")
        except Exception:
            _VIENNA_ZONEINFO = False
    if _VIENNA_ZONEINFO is not False:
        return utc_dt.astimezone(_VIENNA_ZONEINFO)
    return utc_dt.astimezone(_eu_central_offset(utc_dt))


class LedgerError(ValueError):
    """Raised when the per-seat enrollment ledger or its store path is corrupt or unsafe."""


def _is_reparse_or_link(st) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT_ATTR)


def read_ledger(ledger_path: str) -> dict:
    """Load the per-seat enrollment ledger, or return an empty v1 structure when absent."""
    try:
        st = os.lstat(ledger_path)
    except FileNotFoundError:
        return {"v": 1, "files": {}}
    except OSError as exc:
        raise LedgerError("unreadable enrollment ledger") from exc
    if _is_reparse_or_link(st) or not stat.S_ISREG(st.st_mode):
        raise LedgerError("unsafe enrollment ledger file")
    try:
        raw, _ = postbox_os.read_bounded_bytes(ledger_path, max_bytes=MAX_LEDGER_BYTES)
        data = json.loads(raw.decode("utf-8"))
    except postbox_os.OversizeFileError as exc:
        raise LedgerError("enrollment ledger exceeds %d-byte limit" % MAX_LEDGER_BYTES) from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if "not a regular file" in str(exc):
            raise LedgerError("unsafe enrollment ledger file") from exc
        raise LedgerError("corrupt enrollment ledger") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise LedgerError("corrupt enrollment ledger")
    for k, entry in data["files"].items():
        if (
            not isinstance(k, str)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("seat"), str)
            or not entry["seat"]
            or not isinstance(entry.get("dialect"), str)
            or not isinstance(entry.get("path"), str)
            or type(entry.get("dev")) is not int
            or type(entry.get("ino")) is not int
        ):
            raise LedgerError("corrupt enrollment ledger")
    superseded = data.get("superseded")
    if superseded is not None:
        if not isinstance(superseded, dict):
            raise LedgerError("corrupt enrollment ledger")
        for sk, sv in superseded.items():
            if not isinstance(sk, str) or not isinstance(sv, dict):
                raise LedgerError("corrupt enrollment ledger")
            if "dev" in sv or "ino" in sv:
                if (
                    type(sv.get("dev")) is not int
                    or type(sv.get("ino")) is not int
                    or not isinstance(sv.get("seat"), str)
                    or not sv["seat"]
                ):
                    raise LedgerError("corrupt enrollment ledger")
    return data


def write_ledger(ledger_path: str, data: dict) -> None:
    """Atomically write `ledger_path` with owner-only permissions (0600 on POSIX), refusing symlinks."""
    abs_ledger = os.path.abspath(ledger_path)
    parent = os.path.dirname(abs_ledger)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if _is_reparse_or_link(os.lstat(parent)):
        raise LedgerError("unsafe enrollment store_dir")
    try:
        existing_st = os.lstat(abs_ledger)
    except FileNotFoundError:
        existing_st = None
    if existing_st is not None and (_is_reparse_or_link(existing_st) or not stat.S_ISREG(existing_st.st_mode)):
        raise LedgerError("unsafe enrollment ledger file")
    tmp = "%s.%d.%d.tmp" % (abs_ledger, os.getpid(), time.monotonic_ns())
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, abs_ledger)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upgrade_antigravity_path(file_path: str, dialect: str, *, upgrade_check=None) -> str:
    """Prefer sibling transcript_full.jsonl over compact transcript.jsonl for antigravity when regular, safe, and valid."""
    if dialect != "antigravity":
        return file_path
    if os.path.basename(file_path).lower() != "transcript.jsonl":
        return file_path
    cand = os.path.join(os.path.dirname(file_path), "transcript_full.jsonl")
    try:
        st = os.lstat(cand)
    except OSError:
        return file_path
    if _is_reparse_or_link(st):
        return file_path
    if not stat.S_ISREG(st.st_mode):
        return file_path
    if upgrade_check is not None and not upgrade_check(cand):
        return file_path
    return cand


def _check_ancestor_chain(root_abs: str, target_abs: str) -> str | None:
    """Verify `target_abs` sits strictly inside `root_abs` and no component from root to parent is a link/reparse."""
    norm_root = os.path.normcase(os.path.normpath(root_abs))
    norm_target = os.path.normcase(os.path.normpath(target_abs))
    try:
        common = os.path.commonpath([norm_root, norm_target])
    except ValueError:
        return "outside_allowed_roots"
    if common != norm_root or norm_target == norm_root:
        return "outside_allowed_roots"
    try:
        root_st = os.lstat(root_abs)
    except OSError:
        return "outside_allowed_roots"
    if _is_reparse_or_link(root_st):
        return "symlink_or_reparse"
    rel = os.path.relpath(os.path.dirname(target_abs), root_abs)
    if rel != ".":
        cur = root_abs
        for part in rel.split(os.sep):
            cur = os.path.join(cur, part)
            try:
                st = os.lstat(cur)
            except OSError:
                return "missing_file"
            if _is_reparse_or_link(st):
                return "symlink_or_reparse"
    return None


def _within_allowed_roots(target_abs: str, allowed_roots) -> tuple[bool, str | None]:
    if not allowed_roots:
        return False, "outside_allowed_roots"
    saw_link = False
    for raw_root in allowed_roots:
        if not isinstance(raw_root, str) or not raw_root.strip():
            continue
        problem = _check_ancestor_chain(os.path.abspath(raw_root), target_abs)
        if problem is None:
            return True, None
        if problem == "symlink_or_reparse":
            saw_link = True
    return False, ("symlink_or_reparse" if saw_link else "outside_allowed_roots")


def _file_stat_identity(target_abs: str) -> tuple[tuple[int, int, int] | None, str | None]:
    try:
        st = os.lstat(target_abs)
    except OSError:
        return None, "missing_file"
    if _is_reparse_or_link(st):
        return None, "symlink_or_reparse"
    if not stat.S_ISREG(st.st_mode):
        return None, "not_regular_file"
    if int(st.st_ino) == 0:
        return None, "unidentifiable_inode"
    return (int(st.st_dev), int(st.st_ino), int(st.st_size)), None


def enroll_file(
    ledger_path: str,
    *,
    seat: str,
    dialect: str,
    file_path: str,
    allowed_roots,
    box_enrolled_inodes: dict | None = None,
    now_fn=None,
    upgrade_check=None,
) -> tuple[bool, str, dict | None]:
    """Enroll one transcript file by `(dev, ino)` identity after root, symlink, and cross-seat checks."""
    if dialect not in VALID_DIALECTS:
        return False, "invalid_dialect", None
    if not isinstance(file_path, str) or not file_path.strip():
        return False, "missing_file", None
    upgraded = upgrade_antigravity_path(file_path, dialect, upgrade_check=upgrade_check)
    target_abs = os.path.abspath(upgraded)
    ok_root, root_err = _within_allowed_roots(target_abs, allowed_roots)
    if not ok_root:
        return False, root_err or "outside_allowed_roots", None
    ident_tuple, stat_err = _file_stat_identity(target_abs)
    if ident_tuple is None:
        return False, stat_err or "missing_file", None
    dev, ino, size = ident_tuple
    inode_key = (dev, ino)
    if box_enrolled_inodes is not None:
        _refresh_ag_pair_inodes(box_enrolled_inodes)
        holder = box_enrolled_inodes.get(inode_key)
        if holder is not None and holder != seat:
            return False, "duplicate_inode_across_seats", None
    ledger = read_ledger(ledger_path)
    files = ledger["files"]
    norm_key = os.path.normcase(target_abs)
    base_lower = os.path.basename(target_abs).lower()
    stamp = (now_fn or _utc_now)()
    retired_compact = False
    if dialect == "antigravity":
        parent_dir = os.path.dirname(target_abs)
        if base_lower == "transcript.jsonl":
            full_key = os.path.normcase(os.path.join(parent_dir, "transcript_full.jsonl"))
            existing_full = files.get(full_key)
            if existing_full is not None:
                if existing_full.get("seat") != seat:
                    return False, "seat_mismatch", None
                return True, "superseded_by_full", dict(existing_full)
        elif base_lower == "transcript_full.jsonl":
            compact_abs = os.path.join(parent_dir, "transcript.jsonl")
            compact_key = os.path.normcase(compact_abs)
            c_ident, _ = _file_stat_identity(compact_abs)
            if c_ident is not None and box_enrolled_inodes is not None:
                c_holder = box_enrolled_inodes.get((c_ident[0], c_ident[1]))
                if c_holder is not None and c_holder != seat:
                    return False, "duplicate_inode_across_seats", None
            existing_compact = files.get(compact_key)
            existing_cur = files.get(norm_key)
            cur_conflicts = existing_cur is not None and (
                existing_cur.get("seat") != seat
                or existing_cur.get("dev") != dev
                or existing_cur.get("ino") != ino
            )
            if existing_compact is not None:
                if existing_compact.get("seat") != seat:
                    return False, "seat_mismatch", None
                if not cur_conflicts:
                    old_c = files.pop(compact_key)
                    if box_enrolled_inodes is not None:
                        if type(old_c.get("dev")) is int and type(old_c.get("ino")) is int:
                            box_enrolled_inodes[(int(old_c["dev"]), int(old_c["ino"]))] = seat
                        pair_key = ("ag_full_path", norm_key)
                        pair_val = box_enrolled_inodes.get(pair_key)
                        if isinstance(pair_val, tuple) and pair_val[0] == seat:
                            box_enrolled_inodes.pop(pair_key, None)
                    superseded = ledger.get("superseded")
                    if not isinstance(superseded, dict):
                        superseded = {}
                        ledger["superseded"] = superseded
                    superseded[compact_key] = {
                        "by": target_abs,
                        "seat": seat,
                        "at": stamp,
                        "path": old_c.get("path", compact_abs),
                        "dev": old_c.get("dev"),
                        "ino": old_c.get("ino"),
                    }
                    retired_compact = True
            elif c_ident is not None and not cur_conflicts:
                superseded = ledger.get("superseded")
                prev_sup = superseded.get(compact_key) if isinstance(superseded, dict) else None
                if isinstance(prev_sup, dict) and prev_sup.get("seat") not in (None, seat):
                    return False, "seat_mismatch", None
                if not (
                    isinstance(prev_sup, dict)
                    and prev_sup.get("seat") == seat
                    and prev_sup.get("dev") == c_ident[0]
                    and prev_sup.get("ino") == c_ident[1]
                ):
                    if not isinstance(superseded, dict):
                        superseded = {}
                        ledger["superseded"] = superseded
                    superseded[compact_key] = {
                        "by": target_abs,
                        "seat": seat,
                        "at": stamp,
                        "path": compact_abs,
                        "dev": c_ident[0],
                        "ino": c_ident[1],
                    }
                    retired_compact = True
            if c_ident is not None and box_enrolled_inodes is not None and not cur_conflicts:
                box_enrolled_inodes[(c_ident[0], c_ident[1])] = seat
    existing = files.get(norm_key)
    if existing is not None:
        if existing.get("seat") != seat:
            return False, "seat_mismatch", None
        if existing.get("dev") != dev or existing.get("ino") != ino:
            return False, "file_identity_changed", None
        if retired_compact:
            write_ledger(ledger_path, ledger)
        if box_enrolled_inodes is not None:
            box_enrolled_inodes[inode_key] = seat
            if dialect == "antigravity" and base_lower == "transcript.jsonl":
                pk = ("ag_full_path", os.path.normcase(os.path.join(os.path.dirname(target_abs), "transcript_full.jsonl")))
                box_enrolled_inodes.setdefault(pk, (seat, target_abs))
            elif dialect == "antigravity" and base_lower == "transcript_full.jsonl":
                cpk = ("ag_compact_path", os.path.normcase(os.path.join(os.path.dirname(target_abs), "transcript.jsonl")))
                box_enrolled_inodes.setdefault(cpk, (seat, target_abs))
        return True, "already_enrolled", dict(existing)
    entry = {
        "seat": seat,
        "dialect": dialect,
        "path": target_abs,
        "dev": dev,
        "ino": ino,
        "first_seen_size": size,
        "enrolled_at": stamp,
    }
    files[norm_key] = entry
    write_ledger(ledger_path, ledger)
    if box_enrolled_inodes is not None:
        box_enrolled_inodes[inode_key] = seat
        if dialect == "antigravity" and base_lower == "transcript.jsonl":
            pk = ("ag_full_path", os.path.normcase(os.path.join(os.path.dirname(target_abs), "transcript_full.jsonl")))
            box_enrolled_inodes.setdefault(pk, (seat, target_abs))
        elif dialect == "antigravity" and base_lower == "transcript_full.jsonl":
            cpk = ("ag_compact_path", os.path.normcase(os.path.join(os.path.dirname(target_abs), "transcript.jsonl")))
            box_enrolled_inodes.setdefault(cpk, (seat, target_abs))
    return True, "enrolled", dict(entry)


def _bounded_walk(root_abs: str, recursive: bool, limit: int):
    """(dir, file name) pairs under root_abs, never descending through a symlink or junction, and stopping after
    `limit` directory entries so an unexpected deep tree cannot stall a worker round (a reviewer 08dcfa)."""
    stack, seen = [root_abs], 0
    while stack:
        folder = stack.pop()
        try:
            it = os.scandir(folder)
        except OSError:
            continue
        # Consumed one entry at a time and closed at the cap: a directory larger than `limit` is never read whole into
        # memory (GitHub Codex on #431, context_worker.py:432).
        with it:
            try:
                for entry in it:
                    seen += 1
                    if seen > limit:
                        return
                    try:
                        est = entry.stat(follow_symlinks=False)
                        if (entry.is_symlink()
                                or (hasattr(entry, "is_junction") and entry.is_junction())
                                or _is_reparse_or_link(est)):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if recursive:
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield folder, entry.name
                    except OSError:
                        continue
            except OSError:
                continue


def _strings(value, depth: int):
    """Every string in a JSON value, at most `depth` containers deep."""
    if isinstance(value, str):
        yield value
    elif depth > 0 and isinstance(value, dict):
        for v in value.values():
            yield from _strings(v, depth - 1)
    elif depth > 0 and isinstance(value, list):
        for v in value:
            yield from _strings(v, depth - 1)


def _marker_texts(rec) -> list[str]:
    """The strings of a Claude transcript record that may carry a channel event. Never an assistant record (the seat's
    own words can quote anything). A user record: its content string or text blocks, never a tool_result (tool output
    can quote another seat's transcript). Any other record: every string in it, because the harness wraps a channel
    event in records whose shape varies by version: a top-level `content` in queue-operation records on HQB, and
    queue-operation, api-request-blob and attachment records on HQD (kxxx on #486)."""
    if not isinstance(rec, dict) or rec.get("type") == "assistant":
        return []
    if rec.get("type") == "user":
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            return [b["text"] for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return []
    return list(_strings(rec, _MARKER_WALK_DEPTH))


def seat_marker_in_prefix(path: str, names, *, limit: int = SEAT_MARKER_PREFIX_BYTES) -> bool:
    """True when the first `limit` bytes of the Claude transcript at `path` hold a record with a string that opens with
    a meshy `channel_open` tag naming one of `names` (identity names); _marker_texts says which strings count. At most `limit` bytes are read; a line the limit
    cuts, or one still being written, fails to parse and is skipped, so a young file simply qualifies a round later.
    An empty `names` matches nothing. This attributes a file in a shared root; enroll_file still enforces ownership."""
    if not names:
        return False
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return False
    for raw in data.split(b"\n"):
        if b"channel_open" not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        for text in _marker_texts(rec):
            m = _CHANNEL_TAG_RE.match(text.lstrip())
            if m is None:
                continue
            attrs = dict(_TAG_ATTR_RE.findall(m.group(1)))
            if (attrs.get("kind") == "channel_open" and "meshy" in attrs.get("source", "")
                    and attrs.get("identity") in names):
                return True
    return False


def _spec_marker_names(spec: dict, seat_names):
    """The identity names a spec's channel_open must name: None when the spec has no seat_marker, `seat_names` for
    `true`, the listed names for a list of non-empty strings. Anything else is invalid and returns False."""
    sm = spec.get("seat_marker")
    if sm is None or sm is False:
        return None
    if sm is True:
        return frozenset(seat_names or ())
    if isinstance(sm, list) and sm and all(isinstance(n, str) and n.strip() for n in sm):
        return frozenset(n.strip() for n in sm)
    return False


def discover_uses_seat_marker(specs) -> bool:
    return any(isinstance(s, dict) and s.get("seat_marker") not in (None, False) for s in specs or ())


def _discover_spec_problems(index: int, spec) -> list[dict]:
    """Config diagnostics without paths or transcript contents; a bad spec never discovers files."""
    if not isinstance(spec, dict):
        return [{"spec_index": index, "key": "discover", "code": "invalid_discover_spec"}]
    problems = [{"spec_index": index, "key": str(key), "code": "unknown_discover_key"}
                for key in spec if key not in DISCOVER_SPEC_KEYS]
    if "session_meta" in spec:
        meta = spec["session_meta"]
        if not isinstance(meta, dict):
            problems.append({"spec_index": index, "key": "session_meta", "code": "invalid_session_meta_filter"})
        else:
            for key, value in meta.items():
                if key not in SESSION_META_FILTER_KEYS:
                    problems.append({"spec_index": index, "key": "session_meta." + str(key),
                                     "code": "unknown_session_meta_key"})
                elif (not isinstance(value, str) or not value.strip() or "\0" in value
                      or (key == "cwd_prefix" and not os.path.isabs(value.replace("\\", os.sep).replace("/", os.sep)))):
                    problems.append({"spec_index": index, "key": "session_meta." + key,
                                     "code": "invalid_session_meta_filter"})
    if "antigravity_session" in spec and not isinstance(spec["antigravity_session"], bool):
        ag = spec["antigravity_session"]
        if not isinstance(ag, dict) or not ag:
            problems.append({"spec_index": index, "key": "antigravity_session",
                             "code": "invalid_antigravity_session_filter"})
        else:
            for key, value in ag.items():
                if key not in ANTIGRAVITY_SESSION_FILTER_KEYS:
                    problems.append({"spec_index": index, "key": "antigravity_session." + str(key),
                                     "code": "unknown_antigravity_session_key"})
                elif key == "session_id_file":
                    if (
                        not isinstance(value, str)
                        or not value.strip()
                        or "\0" in value
                        or not os.path.isabs(value.strip().replace("\\", os.sep).replace("/", os.sep))
                    ):
                        problems.append({"spec_index": index, "key": "antigravity_session.session_id_file",
                                         "code": "invalid_antigravity_session_filter"})
                elif key == "startup_prefixes":
                    if (
                        not isinstance(value, list)
                        or not value
                        or not all(isinstance(p, str) and p.strip() and "\0" not in p for p in value)
                    ):
                        problems.append({"spec_index": index, "key": "antigravity_session.startup_prefixes",
                                         "code": "invalid_antigravity_session_filter"})
    return problems


def _normalized_cwd(path: str) -> str:
    # Accept either spelling on every host, then use that host's abspath/normcase rules.
    return os.path.normcase(os.path.abspath(path.replace("\\", os.sep).replace("/", os.sep)))


def session_meta_matches(path: str, wanted: dict, *, limit: int = SESSION_META_MAX_LINE_BYTES) -> bool:
    """Match only a complete first session_meta record, reading at most `limit` bytes total."""
    try:
        with open(path, "rb") as stream:
            raw = stream.readline(limit)
        if not raw.endswith(b"\n"):
            return False  # oversized or still being written; never inspect later records
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            return False
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return False
        if "originator" in wanted and payload.get("originator") != wanted["originator"]:
            return False
        if "cwd_prefix" in wanted:
            cwd = payload.get("cwd")
            if not isinstance(cwd, str) or not cwd.strip() or "\0" in cwd:
                return False
            prefix, actual = _normalized_cwd(wanted["cwd_prefix"]), _normalized_cwd(cwd)
            if os.path.commonpath((prefix, actual)) != prefix:
                return False
        return True
    except (OSError, ValueError, TypeError, RecursionError, MemoryError):
        return False


def _spec_antigravity_session(spec: dict):
    """Normalized filter dict for `antigravity_session`: None when absent/false, a dict when valid, False when invalid."""
    if "antigravity_session" not in spec or spec["antigravity_session"] is False:
        return None
    raw = spec["antigravity_session"]
    if raw is True:
        return {"session_id_file": None, "startup_prefixes": DEFAULT_ANTIGRAVITY_STARTUP_PREFIXES}
    if not isinstance(raw, dict) or not raw or any(k not in ANTIGRAVITY_SESSION_FILTER_KEYS for k in raw):
        return False
    sid_file = raw.get("session_id_file")
    if "session_id_file" in raw:
        if (
            not isinstance(sid_file, str)
            or not sid_file.strip()
            or "\0" in sid_file
            or not os.path.isabs(sid_file.strip().replace("\\", os.sep).replace("/", os.sep))
        ):
            return False
        sid_file = sid_file.strip()
    prefixes = raw.get("startup_prefixes")
    if "startup_prefixes" in raw:
        if (
            not isinstance(prefixes, list)
            or not prefixes
            or not all(isinstance(p, str) and p.strip() and "\0" not in p for p in prefixes)
        ):
            return False
        prefixes = tuple(prefixes)
    return {"session_id_file": sid_file, "startup_prefixes": prefixes}


def discover_uses_antigravity_session(specs) -> bool:
    return any(isinstance(s, dict) and "antigravity_session" in s and s["antigravity_session"] is not False
               for s in specs or ())


def _antigravity_conversation_id_from_path(path: str) -> str | None:
    """Verify `<conv-uuid>/.system_generated/logs/transcript{,_full}.jsonl` layout with link-free tail components."""
    abs_p = os.path.abspath(path)
    if os.path.basename(abs_p).lower() not in ("transcript.jsonl", "transcript_full.jsonl"):
        return None
    logs_dir = os.path.dirname(abs_p)
    sys_dir = os.path.dirname(logs_dir)
    conv_dir = os.path.dirname(sys_dir)
    conv_id = os.path.basename(conv_dir)
    if (
        os.path.basename(logs_dir).lower() != "logs"
        or os.path.basename(sys_dir).lower() != ".system_generated"
        or _ANTIGRAVITY_CONV_ID_RE.fullmatch(conv_id) is None
    ):
        return None
    try:
        file_st = os.lstat(abs_p)
        if _is_reparse_or_link(file_st) or not stat.S_ISREG(file_st.st_mode):
            return None
        for d_path in (logs_dir, sys_dir, conv_dir):
            d_st = os.lstat(d_path)
            if _is_reparse_or_link(d_st) or not stat.S_ISDIR(d_st.st_mode):
                return None
    except OSError:
        return None
    return conv_id.lower()


def _read_antigravity_session_id_file(pin_file: str) -> str | None:
    """Read an active Antigravity conversation UUID from `pin_file`, failing closed on links, cutover sentinels, or junk."""
    pin_abs = os.path.abspath(pin_file.replace("\\", os.sep).replace("/", os.sep))
    try:
        st = os.lstat(pin_abs)
        if _is_reparse_or_link(st) or not stat.S_ISREG(st.st_mode):
            return None
        raw, _ = postbox_os.read_bounded_bytes(pin_abs, max_bytes=ANTIGRAVITY_SESSION_ID_MAX_BYTES)
        val = raw.decode("utf-8-sig").strip()
    except (OSError, UnicodeDecodeError, ValueError, postbox_os.OversizeFileError):
        return None
    if not val or val.upper() in ("NEW", "PENDING_NEW") or _ANTIGRAVITY_CONV_ID_RE.fullmatch(val) is None:
        return None
    return val.lower()


def _antigravity_record_matches(path: str, wanted: dict, *, limit: int = ANTIGRAVITY_HEADER_MAX_LINE_BYTES) -> bool:
    """Verify the bounded first JSONL record of `path` matches main-session `USER_EXPLICIT`/`USER_INPUT` rules."""
    try:
        with open(path, "rb") as stream:
            raw = stream.readline(limit)
        if not raw.endswith(b"\n"):
            return False
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict):
            return False
        if record.get("source") != "USER_EXPLICIT" or record.get("type") != "USER_INPUT":
            return False
        if "step_index" in record and (type(record["step_index"]) is not int or record["step_index"] != 0):
            return False
        content = record.get("content")
        if not isinstance(content, str) or not content.strip():
            return False
        prefixes = wanted.get("startup_prefixes")
        if prefixes is not None:
            text = content.lstrip()
            if text.startswith("<USER_REQUEST>"):
                text = text[len("<USER_REQUEST>"):].lstrip()
            if not any(text.startswith(p) for p in prefixes):
                return False
        return True
    except (OSError, UnicodeDecodeError, ValueError, TypeError, RecursionError, MemoryError):
        return False


def antigravity_session_matches(path: str, wanted: dict, *, limit: int = ANTIGRAVITY_HEADER_MAX_LINE_BYTES) -> bool:
    """Match a genuine main Antigravity session transcript, rejecting subagents, probes, and incomplete headers."""
    conv_id = _antigravity_conversation_id_from_path(path)
    if conv_id is None:
        return False
    sid_file = wanted.get("session_id_file")
    if sid_file is not None:
        pinned_id = _read_antigravity_session_id_file(sid_file)
        if pinned_id is None or conv_id != pinned_id:
            return False
    return _antigravity_record_matches(path, wanted, limit=limit)


def make_antigravity_upgrade_check(discover_specs, *, pins=None, require_session_id_match: bool = True):
    """Build a fail-closed validator `(full_path) -> bool` for compact->full upgrades under `antigravity_session`.

    Returns `None` when `discover_specs` does not use `antigravity_session`. Explicit `pins` always allow upgrading
    their own compact transcript, since configured pins take precedence over discovery filters.
    """
    if not discover_uses_antigravity_session(discover_specs):
        return None
    pin_norms: set[str] = set()
    for pin in pins or ():
        if isinstance(pin, str) and pin.strip():
            p_abs = os.path.abspath(pin)
            pin_norms.add(os.path.normcase(p_abs))
            if os.path.basename(p_abs).lower() == "transcript.jsonl":
                pin_norms.add(os.path.normcase(os.path.join(os.path.dirname(p_abs), "transcript_full.jsonl")))

    def _check(full_path: str) -> bool:
        full_abs = os.path.abspath(full_path)
        if os.path.normcase(full_abs) in pin_norms:
            return True
        for index, spec in enumerate(discover_specs or ()):
            if _discover_spec_problems(index, spec):
                continue
            root = spec.get("root")
            pattern = spec.get("pattern") or "*.jsonl"
            excludes = spec.get("exclude_globs") or ()
            if (
                not isinstance(root, str)
                or not root.strip()
                or not isinstance(pattern, str)
                or not isinstance(excludes, (list, tuple))
                or not all(isinstance(g, str) for g in excludes)
            ):
                continue
            if spec.get("seat_marker") not in (None, False):
                continue
            root_abs = os.path.abspath(os.path.expanduser(root))
            if _check_ancestor_chain(root_abs, full_abs) is not None:
                continue
            if spec.get("recursive") is not True and os.path.normcase(os.path.dirname(full_abs)) != os.path.normcase(root_abs):
                continue
            if not fnmatch.fnmatch(os.path.basename(full_abs), pattern):
                continue
            rel = os.path.relpath(full_abs, root_abs).replace(os.sep, "/")
            if any(fnmatch.fnmatch(rel, g) for g in excludes):
                continue
            if "session_meta" in spec and not session_meta_matches(full_abs, spec["session_meta"]):
                continue
            ag_filter = _spec_antigravity_session(spec)
            if ag_filter is False:
                continue
            if ag_filter is not None:
                if require_session_id_match:
                    if not antigravity_session_matches(full_abs, ag_filter):
                        continue
                else:
                    if _antigravity_conversation_id_from_path(full_abs) is None or not _antigravity_record_matches(full_abs, ag_filter):
                        continue
            return True
        return False

    return _check


def discover_candidates(specs, *, known: set, known_inodes: set, now_s: float, seat_names=None,
                        seen: dict | None = None, problems: list | None = None) -> list[str]:
    """Transcript files under `context.discover` roots that are not yet enrolled, oldest mtime first.

    Each spec is {"root": dir, "pattern": "*.jsonl", "recursive": bool, "max_age_h": number, "exclude_globs": [..],
    "seat_marker": true | [identity names], "session_meta": {"originator": string, "cwd_prefix": path}}.
    Both content filters are optional; session_meta matches every supplied field in the FIRST record only.
    cwd_prefix must be an absolute path; resolving a relative filter against the worker's cwd is not permitted.
    Unknown spec/filter keys refuse that spec and are reported through `problems` without stopping valid specs.
    A file qualifies when its name matches the pattern, its path relative to
    the root (POSIX form) matches no exclude glob, and it was modified within max_age_h. Paths in `known`
    (os.path.normcase) and files whose (dev, ino) is in `known_inodes` are left out before any cap, so a renamed file is
    never enrolled twice and a backlog converges oldest first. Links and reparse points are skipped here and refused
    again by enroll_file, which keeps every other check (allowed_roots, (dev, ino) identity, one seat per inode). Only a
    content-filter spec reads file contents, after every non-content check. seat_marker_in_prefix reads at most
    SEAT_MARKER_PREFIX_BYTES; a true seat_marker means the names in `seat_names`, and an invalid one skips the spec.
    session_meta reads at most SESSION_META_MAX_LINE_BYTES of the first line, requiring a complete newline. Given
    `seen`, the walk also records there, by spec index, every matching file in the age window that is not known by path
    (mtime, dev, ino, path), before the inode and marker checks, so discover_stall needs no second walk (kxxx on #486)."""
    found = []
    for index, spec in enumerate(specs or ()):
        spec_problems = _discover_spec_problems(index, spec)
        if spec_problems:
            if problems is not None:
                problems.extend(spec_problems)
            continue
        root, pattern = spec.get("root"), spec.get("pattern") or "*.jsonl"
        excludes = spec.get("exclude_globs") or ()
        if (not isinstance(root, str) or not root.strip() or not isinstance(pattern, str)
                or not isinstance(excludes, (list, tuple)) or not all(isinstance(g, str) for g in excludes)):
            continue
        marker_names = _spec_marker_names(spec, seat_names)
        if marker_names is False:
            continue
        ag_filter = _spec_antigravity_session(spec)
        if ag_filter is False:
            continue
        try:
            max_age_s = float(spec.get("max_age_h", DISCOVER_DEFAULT_MAX_AGE_H)) * 3600.0
        except (TypeError, ValueError):
            continue
        root_abs = os.path.abspath(os.path.expanduser(root))
        for folder, name in _bounded_walk(root_abs, spec.get("recursive") is True, DISCOVER_MAX_ENTRIES):
            if not fnmatch.fnmatch(name, pattern):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root_abs).replace(os.sep, "/")
            if any(fnmatch.fnmatch(rel, g) for g in excludes) or os.path.normcase(path) in known:
                continue
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if _is_reparse_or_link(st) or not stat.S_ISREG(st.st_mode) or now_s - st.st_mtime > max_age_s:
                continue
            if seen is not None:
                seen.setdefault(index, []).append((st.st_mtime, int(st.st_dev), int(st.st_ino), path))
            if (int(st.st_dev), int(st.st_ino)) in known_inodes:
                continue
            if marker_names is not None and not seat_marker_in_prefix(path, marker_names):
                continue
            if "session_meta" in spec and not session_meta_matches(path, spec["session_meta"]):
                continue
            if ag_filter is not None and not antigravity_session_matches(path, ag_filter):
                continue
            found.append((st.st_mtime, path))
    found.sort()
    return [path for _mtime, path in found]


def discover_stall(specs, seen: dict, *, enrolled_paths, known: set, known_inodes: set, now_s: float,
                   stall_h: float = DISCOVER_STALL_H) -> list[dict]:
    """Discover roots where this seat's upload has probably stopped following it (seat-handover item 1).

    For each spec root holding at least one of `enrolled_paths`: when every one of those has had no new bytes for
    `stall_h` hours, and a file that the same round's discover_candidates walk recorded in `seen` for that spec, and
    that nobody enrolled (not in `known` by path, not in `known_inodes`, which callers fill with every seat's enrolled
    inodes), was written within `stall_h` and after this seat's newest file there, one {"root", "enrolled", "idle_h",
    "newer", "newer_age_min"} names both. A hint, not an attribution: in a shared root the newer file may be another
    seat's. No walk of its own and no file contents: only the lstat of this seat's enrolled files. A spec whose
    max_age_h is under stall_h sees fewer newer files, since the walk records only its age window."""
    stall_s = float(stall_h) * 3600.0
    out = []
    for index, spec in enumerate(specs or ()):
        if not isinstance(spec, dict) or not isinstance(spec.get("root"), str) or not spec["root"].strip():
            continue
        root_abs = os.path.abspath(os.path.expanduser(spec["root"]))
        prefix = os.path.normcase(root_abs.rstrip("\\/") + os.sep)
        mine = None
        for p in enrolled_paths or ():
            if not isinstance(p, str) or not os.path.normcase(os.path.abspath(p)).startswith(prefix):
                continue
            try:
                m = os.lstat(p).st_mtime
            except OSError:
                continue
            if mine is None or m > mine[0]:
                mine = (m, p)
        if mine is None or now_s - mine[0] < stall_s:
            continue
        newer = None
        for mtime, dev, ino, path in seen.get(index, ()):
            if os.path.normcase(path) in known or (dev, ino) in known_inodes:
                continue
            if mtime > mine[0] and now_s - mtime < stall_s and (newer is None or mtime > newer[0]):
                newer = (mtime, path)
        if newer is not None:
            out.append({"root": root_abs, "enrolled": mine[1], "idle_h": round((now_s - mine[0]) / 3600.0, 1),
                        "newer": newer[1], "newer_age_min": round((now_s - newer[0]) / 60.0, 1)})
    return out


def _gone_under_a_live_root(path: str, allowed_roots) -> bool:
    """True only when `path` no longer exists and the allowed root that holds it still does.

    Claude Code deletes old session transcripts at startup, so an enrolled file can vanish for an ordinary reason.
    Its bytes were read while it lived and it can never change again, so it is no conflict; before this, one such
    file stopped every upload for its seat. A root that has gone is a broken box or config, and stays a refusal. So
    does any stat failure other than "no such file": that is no answer, not an absence.

    It walks down from the root, because os.lstat follows the directories above the file: a directory swapped for a
    junction to somewhere without the file would also say "no such file" (a reviewer on #515). Every component above
    the first missing one must be a real directory, or this is not an absence and the caller's refusal stands.
    """
    target = os.path.abspath(path)
    norm_target = os.path.normcase(os.path.normpath(target))
    for raw_root in allowed_roots or ():
        if not isinstance(raw_root, str) or not raw_root.strip():
            continue
        root_abs = os.path.abspath(raw_root)
        norm_root = os.path.normcase(os.path.normpath(root_abs))
        try:
            inside = norm_target != norm_root and os.path.commonpath([norm_root, norm_target]) == norm_root
        except ValueError:
            continue
        if not inside:
            continue
        try:
            root_st = os.lstat(root_abs)
        except OSError:
            continue
        if not stat.S_ISDIR(root_st.st_mode) or _is_reparse_or_link(root_st):
            continue
        cur = root_abs
        for part in os.path.relpath(target, root_abs).split(os.sep):
            cur = os.path.join(cur, part)
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                return True
            except OSError:
                break
            if not stat.S_ISDIR(st.st_mode) or _is_reparse_or_link(st):
                break
    return False


def _enrolled_pin_is_gone(ledger_path: str, pin_path, seat: str, dialect: str, allowed_roots) -> bool:
    """A pin this seat enrolled once whose file has since gone: the same case as a discovered file. A pin that was
    never enrolled and does not exist is a mistake in the config, and stays a config error.

    An antigravity pin on transcript.jsonl is stored under its transcript_full.jsonl sibling, and with the directory
    gone upgrade_antigravity_path can no longer say so, so the sibling's key is looked up too (a reviewer on #515)."""
    if not isinstance(pin_path, str) or not pin_path.strip():
        return False
    requested = os.path.abspath(pin_path)
    keys = {os.path.normcase(requested), os.path.normcase(os.path.abspath(upgrade_antigravity_path(pin_path, dialect)))}
    if dialect == "antigravity" and os.path.basename(requested).lower() == "transcript.jsonl":
        keys.add(os.path.normcase(os.path.join(os.path.dirname(requested), "transcript_full.jsonl")))
    files = read_ledger(ledger_path)["files"]
    for key in keys:
        entry = files.get(key)
        if (entry is not None and entry.get("seat") == seat and entry.get("dialect") == dialect
                and _gone_under_a_live_root(entry["path"], allowed_roots)):
            return True
    return False


def verified_enrolled_sources(
    ledger_path: str,
    *,
    seat: str,
    dialect: str,
    allowed_roots,
    box_enrolled_inodes: dict | None = None,
) -> tuple[list[str], str | None]:
    """Return enrolled file paths for `(seat, dialect)` whose `(dev, ino)` and root safety still match.

    A file that no longer exists under a live root is left out, not refused (`_gone_under_a_live_root`). A file
    that exists with another identity is still `file_identity_changed`."""
    _refresh_ag_pair_inodes(box_enrolled_inodes)
    ledger = read_ledger(ledger_path)
    files = ledger["files"]
    out = []
    for norm_key in sorted(files):
        entry = files[norm_key]
        if entry.get("seat") != seat:
            return [], "seat_mismatch"
        if entry.get("dialect") != dialect:
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            return [], "file_identity_changed"
        if dialect == "antigravity" and os.path.basename(path).lower() == "transcript.jsonl":
            full_sibling_key = os.path.normcase(os.path.join(os.path.dirname(path), "transcript_full.jsonl"))
            sibling_entry = files.get(full_sibling_key)
            if (
                isinstance(sibling_entry, dict)
                and sibling_entry.get("seat") == seat
                and sibling_entry.get("dialect") == "antigravity"
            ):
                continue
        if _gone_under_a_live_root(path, allowed_roots):
            continue
        ok_root, _ = _within_allowed_roots(path, allowed_roots)
        if not ok_root:
            return [], "outside_allowed_roots"
        ident_tuple, _ = _file_stat_identity(path)
        if ident_tuple is None:
            return [], "file_identity_changed"
        dev, ino, _ = ident_tuple
        if dev != entry.get("dev") or ino != entry.get("ino"):
            return [], "file_identity_changed"
        if box_enrolled_inodes is not None:
            holder = box_enrolled_inodes.get((dev, ino))
            if holder is not None and holder != seat:
                return [], "duplicate_inode_across_seats"
            box_enrolled_inodes[(dev, ino)] = seat
        out.append(path)
    return out, None


def check_interpreter_match(ident: dict, postbox_python: str | None = None,
                            platform: str | None = None) -> tuple[bool, str | None]:
    """On macOS (`darwin`), require `realpath(ident['python']) == realpath(postbox_python)` unless overridden."""
    plat = sys.platform if platform is None else platform
    if plat != "darwin":
        return True, None
    if ident.get("allow_distinct_context_python") is True:
        return True, None
    child_py = ident.get("python")
    if not isinstance(child_py, str) or not child_py.strip():
        return False, "missing_python"
    host_py = sys.executable if postbox_python is None else postbox_python
    if os.path.realpath(child_py) != os.path.realpath(host_py):
        return False, "darwin_distinct_python"
    return True, None


def probe_livez(base_url: str, *, timeout_s: float = LIVEZ_SLOW_S,
                opener=urllib.request.urlopen, mono_fn=time.monotonic) -> bool:
    """Return True when GET `<base_url>/v1/livez` answers 2xx within `timeout_s` seconds."""
    url = base_url.rstrip("/") + "/v1/livez"
    t0 = mono_fn()
    try:
        with opener(url, timeout=timeout_s) as resp:
            code = getattr(resp, "status", None)
            if code is None:
                code = resp.getcode()
            resp.read(256)
    except Exception:
        return False
    elapsed = mono_fn() - t0
    if elapsed > timeout_s:
        return False
    return 200 <= int(code) < 300


def _vienna_seat_night_id(local_dt: datetime.datetime) -> str | None:
    """Identifier for the continuous 22:00..(time withheld) overnight window, or None during 07:00..22:00 day.

    22:00..23:59 on date D and 00:00..06:59 on date D+1 share morning date `(D+1).isoformat()`, so a night
    latch triggered at 22:30 stays latched past midnight until (time withheld) and resets the next night.
    """
    if local_dt.hour >= 22:
        return (local_dt.date() + datetime.timedelta(days=1)).isoformat()
    if local_dt.hour < 7:
        return local_dt.date().isoformat()
    return None


def _positive_rate(val, default: float) -> float:
    if type(val) is not bool and isinstance(val, (int, float)) and math.isfinite(val) and val > 0.0:
        return float(val)
    return default


def rate_limit_backoff_s(payload: dict | None, default_wait_s: float = DEFAULT_RATE_LIMIT_WAIT_S) -> float:
    """Compute `(named_seconds * 1.25)` backoff on `rate_limited` exits (kxxx, memory-bxxxxx)."""
    named = None
    if isinstance(payload, dict):
        for key in ("retry_after_s", "retry_after"):
            cand = payload.get(key)
            if type(cand) is not bool and isinstance(cand, (int, float)) and math.isfinite(cand) and cand > 0.0:
                named = float(cand)
                break
    base = named if named is not None else _positive_rate(default_wait_s, DEFAULT_RATE_LIMIT_WAIT_S)
    clamped = min(max(base, 1.0), MAX_RATE_LIMIT_WAIT_S)
    return clamped * RATE_LIMIT_BACKOFF_FACTOR


class RateController:
    """Box-wide and per-seat Vienna rate budgets.

    - Box ceiling (Step 10): 2 writes/s daytime (07:00..24:00), 8 writes/s night (00:00..07:00) with a
      per-base-url 60s `/v1/livez` health check that latches to 2 writes/s for the rest of that Vienna
      night if two consecutive probes fail or exceed 2s.
    - Per-seat call-bucket ceiling (5a1714 / 11d720): 0.25 records/s daytime (07:00..22:00), 0.35 records/s
      night (22:00..(time withheld)), dropping and latching that seat to the day rate for the rest of that
      Vienna night if `rate_limited` (HTTP 429) or `mail_rate_limited` is observed.
    """

    def __init__(self, *, probe_fn=probe_livez, now_fn=_utc_dt, mono_fn=time.monotonic):
        self.probe_fn = probe_fn
        self.now_fn = now_fn
        self.mono_fn = mono_fn
        self._by_url: dict[str, dict] = {}
        self._last_slot: dict | None = None
        self._seat_latched_night: dict[str, str] = {}

    @property
    def consecutive_slow(self) -> int:
        return int(self._last_slot["consecutive_slow"]) if self._last_slot is not None else 0

    @property
    def latched_night(self) -> str | None:
        return self._last_slot["latched_night"] if self._last_slot is not None else None

    def seat_latched_night(self, seat_name: str) -> str | None:
        return self._seat_latched_night.get(seat_name)

    def latch_seat_for_night(self, seat_name: str) -> str | None:
        local = _vienna_local(self.now_fn())
        night_id = _vienna_seat_night_id(local)
        if night_id is not None:
            self._seat_latched_night[seat_name] = night_id
        return night_id

    def seat_rate(self, seat_name: str, *, ctx: dict | None = None, ident: dict | None = None) -> float:
        cfg_ctx = ctx if isinstance(ctx, dict) else {}
        cfg_ident = ident if isinstance(ident, dict) else {}
        day_rate = _positive_rate(cfg_ctx.get("day_seat_rate_per_s"), SEAT_DAY_RATE_PER_S)
        night_rate = _positive_rate(cfg_ctx.get("night_seat_rate_per_s"), SEAT_NIGHT_RATE_PER_S)
        local = _vienna_local(self.now_fn())
        night_id = _vienna_seat_night_id(local)
        if cfg_ident.get("mail_rate_limited") is True or cfg_ctx.get("force_day_rate") is True:
            if night_id is not None:
                self._seat_latched_night[seat_name] = night_id
            return day_rate
        if night_id is None:
            return day_rate
        if self._seat_latched_night.get(seat_name) == night_id:
            return day_rate
        return night_rate

    def _slot(self, base_url: str) -> dict:
        key = (base_url or "").rstrip("/")
        slot = self._by_url.get(key)
        if slot is None:
            slot = {
                "latched_night": None,
                "active_night": None,
                "consecutive_slow": 0,
                "last_probe_mono": None,
            }
            self._by_url[key] = slot
        self._last_slot = slot
        return slot

    def budget(self, base_url: str) -> tuple[float, int]:
        slot = self._slot(base_url)
        local = _vienna_local(self.now_fn())
        if local.hour >= 7:
            slot["consecutive_slow"] = 0
            slot["last_probe_mono"] = None
            return DAY_RATE_PER_S, DAY_MAX_BATCHES
        night_id = local.date().isoformat()
        if slot["active_night"] != night_id:
            slot["active_night"] = night_id
            slot["consecutive_slow"] = 0
            slot["last_probe_mono"] = None
        if slot["latched_night"] == night_id:
            return DAY_RATE_PER_S, DAY_MAX_BATCHES
        mono = self.mono_fn()
        should_probe = slot["last_probe_mono"] is None or (mono - slot["last_probe_mono"]) >= LIVEZ_INTERVAL_S
        if should_probe:
            slot["last_probe_mono"] = mono
            if self.probe_fn(base_url):
                slot["consecutive_slow"] = 0
            else:
                slot["consecutive_slow"] += 1
                if slot["consecutive_slow"] >= LIVEZ_LATCH_STREAK:
                    slot["latched_night"] = night_id
                    return DAY_RATE_PER_S, DAY_MAX_BATCHES
        return NIGHT_RATE_PER_S, NIGHT_MAX_BATCHES


def writes_in_payload(payload: dict | None) -> int:
    """Count server write attempts reported by `context_upload --json`."""
    if not isinstance(payload, dict):
        return 0
    batches = payload.get("batches_committed")
    items = payload.get("items_committed")
    total = (batches if type(batches) is int and batches > 0 else 0)
    total += (items if type(items) is int and items > 0 else 0)
    acc = payload.get("accounting")
    failed = acc.get("failed") if isinstance(acc, dict) else None
    if isinstance(failed, dict):
        for val in failed.values():
            if type(val) is int and val > 0:
                total += val
    if total == 0 and payload.get("status") == "progressed":
        return 1
    return total


def pace_sleep_s(writes_done: int, elapsed_s: float, rate_per_s: float) -> float:
    """Seconds to sleep after a pass so effective write rate stays at or below `rate_per_s`."""
    if writes_done <= 0 or rate_per_s <= 0.0:
        return 0.0
    clamped_elapsed = elapsed_s if elapsed_s > 0.0 else 0.0
    needed = (float(writes_done) / float(rate_per_s)) - clamped_elapsed
    return needed if needed > 0.0 else 0.0


def _clean_int_or_none(val) -> int | None:
    return val if type(val) is int else None


_F3_DEFERRED_SRC = re.compile(r"^[0-9a-f]{32}$")


def sanitize_f3_deferred(raw) -> dict | None:
    """The sources the last pass kept text-only under --f3 (enrolled before F3), so a seat that reports F3 on can see
    which of its sources are not F3 yet (txxxx on #471). Each is named by its WIRE src (the id in its ctx/ and ctxm/
    keys, 32 hex, or null before its first record) and generation (pxx on #471). Only a count and up to 8 entries:
    never a path or transcript text. None when there are none or the uploader sent no list."""
    if not isinstance(raw, list):
        return None
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        src, gen = e.get("src"), e.get("gen")
        if (src is None or (isinstance(src, str) and _F3_DEFERRED_SRC.match(src))) and type(gen) is int and gen >= 1:
            out.append({"src": src, "gen": gen})
    if not out:
        return None
    uniq = sorted({(e["src"] or "", e["gen"]) for e in out})
    return {"count": len(out), "sources": [{"src": s or None, "gen": g} for s, g in uniq][:8]}


def sanitize_context_held(raw_held) -> list[dict] | None:
    """Keep only batches held >= 1 hour, stripping all non-whitelisted keys and validating scalar shapes
    so transcript content or file paths can NEVER reach status.json."""
    if not isinstance(raw_held, list):
        return None
    cleaned = []
    for entry in raw_held:
        if not isinstance(entry, dict):
            continue
        age_s = entry.get("age_s")
        if type(age_s) is bool or not isinstance(age_s, (int, float)):
            continue
        if age_s < HELD_REPORT_AGE_S:
            continue
        raw_src = entry.get("src")
        src = raw_src if isinstance(raw_src, str) and _HEX32_RE.fullmatch(raw_src) else None
        raw_code = entry.get("code")
        code = raw_code if isinstance(raw_code, str) and _CODE_RE.fullmatch(raw_code) else "UNKNOWN_CODE"
        cleaned.append({
            "src": src,
            "gen": _clean_int_or_none(entry.get("gen")),
            "batch": _clean_int_or_none(entry.get("batch")),
            "first_ord": _clean_int_or_none(entry.get("first_ord")),
            "last_ord": _clean_int_or_none(entry.get("last_ord")),
            "code": code,
            "age_s": round(float(age_s), 3) if isinstance(age_s, float) else int(age_s),
        })
    return cleaned if cleaned else None


_CONFLICT_E_CODE = re.compile(r"^(E_[A-Z0-9_]{2,63})\b")
_CONFLICT_PHRASE = re.compile(r"[a-z][a-z0-9 :_,()-]{0,99}")   # fullmatch: no "/" or ".", no trailing newline
_UNREACHABLE_REASON = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_ :.-]{0,63}")


def sanitize_unreachable_reason(raw) -> str | None:
    """Keep a short path-free unreachable reason code from context_upload (never a path or transcript text)."""
    if not isinstance(raw, str) or not raw:
        return None
    return raw if _UNREACHABLE_REASON.fullmatch(raw) and ".." not in raw else "other"


def sanitize_conflict_codes(raw) -> list[str] | None:
    """Why an exit 4 happened, for status.json and the log (pxx: every seat that hit exit 4 on 1 Nov had to
    rerun the uploader by hand to see its code). The uploader's conflict codes are its own fixed strings, but an
    admission refusal carries the validator's message after its E_ code, so only the leading E_ token is kept from
    those; another code is kept only when it is a short lowercase fixed phrase ("store: enrollment mismatch ...",
    "source missing", "readback_differs"), else it is reported as "other". Up to 5 distinct codes; never a path,
    a src or transcript text. None when the uploader reported none."""
    if not isinstance(raw, list):
        return None
    out = []
    for c in raw:
        code = c.get("code") if isinstance(c, dict) else None
        if not isinstance(code, str) or not code:
            continue
        m = _CONFLICT_E_CODE.match(code)
        kept = m.group(1) if m else (code if _CONFLICT_PHRASE.fullmatch(code) else "other")
        if kept not in out:
            out.append(kept)
    return out[:5] or None


def _clearing_stale_conflict_codes(set_status):
    """status.json merges fields, and quarantined_seats starts empty again when guarded_context_worker restarts the
    worker loop after a fault while postbox's _status survives (pxx on #474, P2-1). So every write that reports a
    worker state other than conflict also clears context_conflict_codes, unless the call names it."""
    def wrapped(name, **kw):
        if "context_worker" in kw and kw["context_worker"] != "conflict":
            kw.setdefault("context_conflict_codes", None)
        return set_status(name, **kw)
    return wrapped


def build_upload_argv(
    ident: dict,
    *,
    store_dir: str,
    seat: str,
    dialect: str,
    sources: list[str],
    max_batches: int | None,
    namespace: str | None = None,
    sources_file: str | None = None,
) -> list[str]:
    """Construct the exact `tools.transcript_cards.context_upload` CLI command.

    With `sources_file`, the sources travel in that file (`--sources-file`) instead of one `--source` each."""
    argv = [
        ident["python"],
        "-m",
        "tools.transcript_cards.context_upload",
        "--state-file",
        ident["state"],
        "--base-url",
        ident["base_url"],
        "--store-dir",
        store_dir,
        "--seat",
        seat,
        "--dialect",
        dialect,
    ]
    if sources_file is not None:
        argv.extend(["--sources-file", sources_file])
    else:
        for src in sources:
            argv.extend(["--source", src])
    if namespace:
        argv.extend(["--namespace", namespace])
    if max_batches is not None:
        argv.extend(["--max-batches", str(max_batches)])
    ctx = ident.get("context") if isinstance(ident.get("context"), dict) else {}
    if ctx.get("f3") is True:
        argv.append("--f3")
    argv.append("--json")
    return argv


def upload_argv(ident: dict, *, store_dir: str, seat: str, dialect: str, sources: list[str],
                max_batches: int | None, namespace: str | None = None) -> list[str]:
    """`build_upload_argv` inline while its command line fits CMDLINE_BUDGET_CHARS; past it, the sources are written
    to UPLOAD_SOURCES_FILE in `store_dir` (owner-only, atomic) and passed with `--sources-file`. Inline stays the
    default so an app_root child that predates `--sources-file` keeps working for every seat under the budget."""
    argv = build_upload_argv(ident, store_dir=store_dir, seat=seat, dialect=dialect, sources=sources,
                             max_batches=max_batches, namespace=namespace)
    if len(subprocess.list2cmdline(argv)) <= CMDLINE_BUDGET_CHARS:
        return argv
    path = os.path.join(os.path.abspath(store_dir), UPLOAD_SOURCES_FILE)
    write_ledger(path, {"sources": list(sources)})
    return build_upload_argv(ident, store_dir=store_dir, seat=seat, dialect=dialect, sources=sources,
                             max_batches=max_batches, namespace=namespace, sources_file=path)


def resolve_app_root(ident: dict) -> tuple[str | None, str | None]:
    """Validate `context.app_root` (or `ident['app_root']`) contains regular `apps/context/validation.py`."""
    ctx = ident.get("context") if isinstance(ident.get("context"), dict) else {}
    raw = ctx.get("app_root") or ident.get("app_root")
    if not isinstance(raw, str) or not raw.strip():
        return None, "app_root_missing"
    app_abs = os.path.abspath(raw.strip())
    for dir_path in (
        app_abs,
        os.path.join(app_abs, "apps"),
        os.path.join(app_abs, "apps", "context"),
    ):
        try:
            d_st = os.lstat(dir_path)
        except OSError:
            return None, "app_root_missing"
        if _is_reparse_or_link(d_st) or not stat.S_ISDIR(d_st.st_mode):
            return None, "app_root_missing"
    val_py = os.path.join(app_abs, "apps", "context", "validation.py")
    try:
        v_st = os.lstat(val_py)
    except OSError:
        return None, "app_root_missing"
    if _is_reparse_or_link(v_st) or not stat.S_ISREG(v_st.st_mode):
        return None, "app_root_missing"
    return app_abs, None


def resolve_launch_dir(ident: dict, app_root: str) -> tuple[str | None, str | None]:
    """The uploader child's cwd, which `python -m` puts FIRST on sys.path (ahead of PYTHONPATH=app_root).

    When app_root carries the uploader itself (a full checkout), the child starts there, so every module loads from
    the deployed tree: an older full checkout in `client` (its own apps/ and tools/transcript_cards/) would otherwise
    shadow it, and HQD ran new context_upload over old sibling modules and an old apps (29 Oct, exit 4 then rc=1).
    A client-only install (app_root without the uploader) still starts in `client`, whose tools/ is the uploader; a
    client that also holds apps/ there would shadow app_root's apps, so it is refused, never run."""
    if os.path.isfile(os.path.join(app_root, "tools", "transcript_cards", "context_upload.py")):
        return app_root, None
    client = ident.get("client")
    if isinstance(client, str) and client and os.path.isdir(os.path.join(client, "apps")):
        return None, "client_shadows_app_root"
    return client if isinstance(client, str) and client else None, None


def _sources_stat_signature(sources: list[str]) -> tuple[tuple[str, int, int], ...] | None:
    """Snapshot `(norm_path, st_size, st_mtime_ns)` across enrolled sources via one `os.stat` each."""
    sig: list[tuple[str, int, int]] = []
    for src in sources:
        try:
            st = os.stat(src)
        except OSError:
            return None
        mtime_ns = getattr(st, "st_mtime_ns", None)
        if type(mtime_ns) is not int:
            mtime_ns = int(float(st.st_mtime) * 1_000_000_000)
        sig.append((os.path.normcase(os.path.abspath(src)), int(st.st_size), mtime_ns))
    return tuple(sig)


def _run_subprocess(
    argv: list[str],
    cwd: str | None,
    timeout_s: float,
    env_extra: dict[str, str] | None = None,
) -> tuple[int | None, str, str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    if env_extra:
        env.pop("PYTHONSAFEPATH", None)
        env.update(env_extra)
    try:
        r = postbox_os.run_bounded(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, creationflags=postbox_os.NO_WINDOW,
            max_stdout=4 * 1048576, max_stderr=262144, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        out_s = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err_s = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, out_s, err_s
    except OSError as exc:
        return -1, "", "%s: %s" % (type(exc).__name__, exc)
    return r.returncode, r.stdout, r.stderr


def _invoke_runner(runner, argv: list[str], cwd: str | None, timeout_s: float, env_extra: dict[str, str]):
    try:
        params = list(inspect.signature(runner).parameters.values())
        accepts_env = any(
            p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for p in params
        ) or sum(
            1
            for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ) >= 4
    except (TypeError, ValueError):
        accepts_env = False
    if accepts_env:
        return runner(argv, cwd, timeout_s, env_extra)
    return runner(argv, cwd, timeout_s)


def _parse_json_stdout(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _is_driver_missing(rc: int | None, stdout: str, stderr: str) -> bool:
    if rc in (None, 0):
        return False
    combined = stderr + "\n" + stdout
    return "No module named" in combined and "context_upload" in combined


def resolve_seat(ident: dict) -> str:
    """Resolve the server-stamped caller seat label for `ident`.

    Production postbox `config.json` entries frequently name identities `<seat>-prod` (e.g. `antigravity-prod`,
    `dxxxxx-prod`, `bxxxxx-prod`) while the server stamps the minted peer's `label` from `ident['state']`
    (or top-level `ident['seat']`). Prefer explicit `context.seat`, then `ident['seat']`, then `state['label']`
    from `ident['state']`, then `ident['address']`, falling back to `ident['name']`.
    """
    ctx = ident.get("context") if isinstance(ident.get("context"), dict) else {}
    for candidate in (ctx.get("seat"), ident.get("seat")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    st_path = ident.get("state")
    if isinstance(st_path, str) and st_path.strip():
        try:
            st_doc = postbox_os.read_bounded_json(st_path, max_bytes=MAX_STATE_BYTES)
            lbl = st_doc.get("label") if isinstance(st_doc, dict) else None
            if isinstance(lbl, str) and lbl.strip():
                return lbl.strip()
        except (OSError, UnicodeDecodeError, ValueError):
            pass
    addr = ident.get("address")
    if isinstance(addr, str) and addr.strip():
        return addr.strip()
    return str(ident["name"])


def context_enabled_identities(identities: list[dict]) -> list[dict]:
    """Filter active postbox identities to those opting into `context.enabled == True`."""
    out = []
    for ident in identities:
        if not ident.get("enabled", True):
            continue
        ctx = ident.get("context")
        if isinstance(ctx, dict) and ctx.get("enabled") is True:
            out.append(ident)
    return out


def seat_identity_names(identities) -> dict[str, frozenset]:
    """seat -> the names of the configured identities whose context names that seat explicitly (context.seat, then a
    top-level seat). The names a `seat_marker: true` spec accepts in a channel_open; reads no state file."""
    out: dict[str, set] = {}
    for ident in identities or ():
        if not isinstance(ident, dict) or not isinstance(ident.get("name"), str):
            continue
        ctx = ident.get("context") if isinstance(ident.get("context"), dict) else {}
        for cand in (ctx.get("seat"), ident.get("seat")):
            if isinstance(cand, str) and cand.strip():
                out.setdefault(cand.strip(), set()).add(ident["name"])
                break
    return {seat: frozenset(names) for seat, names in out.items()}


def _report_discover_problems(name, problems, *, now_s, set_status, note, seat_notes) -> None:
    """Keep refused specs visible beside context_stall; log each problem at most once per UTC day."""
    set_status(name, context_discover_problems=problems or None)
    day = time.strftime("%Y-%m-%d", time.gmtime(now_s))
    for problem in problems:
        prob_key = ("discover_spec", name, problem["spec_index"], problem["key"], problem["code"])
        if seat_notes is not None and seat_notes.get(prob_key) == day:
            continue
        if seat_notes is not None:
            seat_notes[prob_key] = day
        note("%s: context discover spec %d refused: %s (%s)"
             % (name, problem["spec_index"], problem["key"], problem["code"]))


def _report_stall(name, specs, walked, ledger_path, *, known, known_inodes, box_enrolled_inodes, now_s, set_status, note,
                  seat_notes) -> None:
    """After a discovery round: context_stall in the status (None when clear), and one note per enrolled file per UTC
    day while discover_stall holds. The newer file is excluded when ANY seat on the box enrolled it."""
    files = read_ledger(ledger_path)["files"]
    enrolled = [e.get("path") for e in files.values() if isinstance(e, dict)]
    stalls = discover_stall(
        specs, walked, enrolled_paths=enrolled,
        known=set(known) | set(files),
        known_inodes=set(known_inodes) | set(box_enrolled_inodes or {})
        | {(e.get("dev"), e.get("ino")) for e in files.values() if isinstance(e, dict)},
        now_s=now_s)
    set_status(name, context_stall=stalls[0] if stalls else None)
    day = time.strftime("%Y-%m-%d", time.gmtime(now_s))
    for s in stalls:
        key = ("stall", name, os.path.normcase(s["enrolled"]))
        if seat_notes is not None and seat_notes.get(key) == day:
            continue
        if seat_notes is not None:
            seat_notes[key] = day
        note("%s: context upload may have stopped following this seat: %s has had no new bytes for %.1f h, while %s "
             "under the same discover root was written %.0f min ago and nobody enrolled it. If that is this seat's "
             "new session, enroll it (a pin, or seat_marker on the discover spec)."
             % (name, s["enrolled"], s["idle_h"], s["newer"], s["newer_age_min"]))


def sanitize_visited(raw, sources: list[str]) -> list[dict] | None:
    """context_upload's `visited` ([{"source_index": i, "batches": n}], in visit order) mapped back to `sources`, the
    --source list this worker passed: [{"source": path, "batches": n}]. An index outside the list is dropped."""
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw[:256]:
        if not isinstance(item, dict):
            continue
        idx, batches = item.get("source_index"), item.get("batches")
        if type(idx) is int and 0 <= idx < len(sources) and type(batches) is int and batches >= 0:
            out.append({"source": sources[idx], "batches": batches})
    return out


def run_seat_pass(
    ident: dict,
    *,
    store_dir: str,
    rate_controller: RateController,
    box_enrolled_inodes: dict,
    quarantined_seats: set,
    missing_driver_logged: set,
    set_status,
    note,
    seat_next_mono: dict[str, float] | None = None,
    drained_state: dict[str, tuple[float, tuple]] | None = None,
    now_fn=_utc_now,
    postbox_python: str | None = None,
    platform: str | None = None,
    runner=_run_subprocess,
    mono_fn=time.monotonic,
    child_timeout_s: float = DEFAULT_CHILD_TIMEOUT_S,
    discover_state: dict[str, float] | None = None,
    wall_fn=time.time,
    box_pins: list[str] | None = None,
    seat_names=None,
    seat_notes: dict | None = None,
) -> dict:
    """Execute one bounded capture/upload pass for `ident`. `box_pins` is every enabled identity's explicit pins on this
    box (_box_pin_paths): discovery never claims one, so a later identity's pin under an earlier identity's discovery
    root is enrolled by its own seat, never by the first (GitHub Codex on #431, context_worker.py:1049). `seat_names` are
    the identity names a `seat_marker: true` spec accepts (default: this identity's own name; run_context_worker passes
    every configured identity of the same seat). `seat_notes` keeps what was already noted, so the stall hint and the
    sources-visited line are written once rather than every pass."""
    name = ident["name"]
    set_status = _clearing_stale_conflict_codes(set_status)
    if name in quarantined_seats:
        return {"outcome": "quarantined", "sleep_s": 0.0, "more_work": False}

    if seat_next_mono is not None:
        now_m = mono_fn()
        due_m = seat_next_mono.get(name, 0.0)
        if now_m < due_m:
            return {"outcome": "paced", "sleep_s": 0.0, "more_work": True, "due_in_s": due_m - now_m}

    ok_py, py_err = check_interpreter_match(ident, postbox_python=postbox_python, platform=platform)
    if not ok_py:
        set_status(name, context_worker="config_error", context_config_error=True,
                   context_error=py_err, context_at=now_fn())
        return {"outcome": "config_error", "sleep_s": 0.0, "more_work": False}

    ctx = ident["context"]
    seat = resolve_seat(ident)
    dialect = ctx.get("dialect") or ident.get("cli") or ""
    if dialect not in VALID_DIALECTS:
        set_status(name, context_worker="config_error", context_config_error=True,
                   context_error="invalid_dialect", context_at=now_fn())
        return {"outcome": "config_error", "sleep_s": 0.0, "more_work": False}
    if dialect != "claude" and discover_uses_seat_marker(ctx.get("discover")):
        set_status(name, context_worker="config_error", context_config_error=True,
                   context_error="seat_marker_needs_claude", context_at=now_fn())
        return {"outcome": "config_error", "sleep_s": 0.0, "more_work": False}
    if dialect != "antigravity" and discover_uses_antigravity_session(ctx.get("discover")):
        set_status(name, context_worker="config_error", context_config_error=True,
                   context_error="antigravity_session_needs_antigravity", context_at=now_fn())
        return {"outcome": "config_error", "sleep_s": 0.0, "more_work": False}

    app_root, app_err = resolve_app_root(ident)
    launch_dir, launch_err = (None, None) if app_root is None else resolve_launch_dir(ident, app_root)
    if app_root is None or launch_err:
        set_status(name, context_worker="config_error", context_config_error=True,
                   context_error=app_err or launch_err or "app_root_missing", context_at=now_fn())
        return {"outcome": "config_error", "sleep_s": 0.0, "more_work": False}

    allowed_roots = ctx.get("allowed_roots") or ()
    ledger_path = os.path.join(store_dir, "enrollment.json")
    try:
        for pin_path in ctx.get("pins") or ():
            ok_en, en_reason, _ = enroll_file(
                ledger_path,
                seat=seat,
                dialect=dialect,
                file_path=pin_path,
                allowed_roots=allowed_roots,
                box_enrolled_inodes=box_enrolled_inodes,
                now_fn=now_fn,
            )
            if not ok_en:
                if _enrolled_pin_is_gone(ledger_path, pin_path, seat, dialect, allowed_roots):
                    continue
                if en_reason in ("file_identity_changed", "duplicate_inode_across_seats", "seat_mismatch"):
                    quarantined_seats.add(name)
                    set_status(name, context_worker="conflict", context_conflict=True,
                               context_error=en_reason, context_at=now_fn())
                    note("%s: context enrollment conflict (%s); pausing retries" % (name, en_reason))
                    return {"outcome": "conflict", "sleep_s": 0.0, "more_work": False}
                set_status(name, context_worker="config_error", context_config_error=True,
                           context_error=en_reason, context_at=now_fn())
                return {"outcome": "config_error", "sleep_s": 0.0, "more_work": False}

        if ctx.get("discover") and discover_state is not None:
            due = discover_state.get(name)
            if due is None or mono_fn() - due >= DISCOVER_INTERVAL_S:
                discover_state[name] = mono_fn()
                ledger_doc = read_ledger(ledger_path)
                files = ledger_doc["files"]
                known = set(files) | {os.path.normcase(str(e.get("path", ""))) for e in files.values()}
                known_inodes = {(e.get("dev"), e.get("ino")) for e in files.values()}
                for sup_k, sup_v in (ledger_doc.get("superseded") or {}).items():
                    known.add(sup_k)
                    if isinstance(sup_v, dict):
                        if isinstance(sup_v.get("path"), str) and sup_v["path"]:
                            known.add(os.path.normcase(sup_v["path"]))
                        if type(sup_v.get("dev")) is int and type(sup_v.get("ino")) is int:
                            known_inodes.add((int(sup_v["dev"]), int(sup_v["ino"])))
                for pin in box_pins or ():
                    known.add(os.path.normcase(pin))
                    try:
                        st = os.lstat(pin)
                        if st.st_ino:                     # an inode of 0 identifies nothing (a reviewer f414df)
                            known_inodes.add((int(st.st_dev), int(st.st_ino)))
                    except OSError:
                        pass
                added = 0
                own_names = frozenset(seat_names) if seat_names is not None else frozenset({name})
                walked: dict = {}
                problems: list = []
                candidates = discover_candidates(ctx.get("discover"), known=known, known_inodes=known_inodes,
                                                 now_s=wall_fn(), seat_names=own_names, seen=walked,
                                                 problems=problems)
                _report_discover_problems(name, problems, now_s=wall_fn(), set_status=set_status,
                                          note=note, seat_notes=seat_notes)
                ag_disc_upgrade_check = make_antigravity_upgrade_check(
                    ctx.get("discover"), pins=ctx.get("pins"), require_session_id_match=True
                )
                for cand in candidates:
                    if added >= DISCOVER_MAX_PER_ROUND:
                        break
                    ok_en, en_reason, _ = enroll_file(
                        ledger_path,
                        seat=seat,
                        dialect=dialect,
                        file_path=cand,
                        allowed_roots=allowed_roots,
                        box_enrolled_inodes=box_enrolled_inodes,
                        now_fn=now_fn,
                        upgrade_check=ag_disc_upgrade_check,
                    )
                    # A refused candidate is skipped, never a quarantine: a shared root can hold another seat's
                    # file (duplicate_inode_across_seats) or a link, and neither is this seat's conflict.
                    if ok_en and en_reason == "enrolled":
                        added += 1
                set_status(name, context_discovered=added, context_at=now_fn())
                _report_stall(name, ctx.get("discover"), walked, ledger_path, known=known, known_inodes=known_inodes,
                              box_enrolled_inodes=box_enrolled_inodes, now_s=wall_fn(), set_status=set_status,
                              note=note, seat_notes=seat_notes)

        ag_persisted_upgrade_check = make_antigravity_upgrade_check(
            ctx.get("discover"), pins=ctx.get("pins"), require_session_id_match=False
        )
        up_err = _upgrade_persisted_antigravity_entries(
            ledger_path,
            seat=seat,
            dialect=dialect,
            allowed_roots=allowed_roots,
            box_enrolled_inodes=box_enrolled_inodes,
            now_fn=now_fn,
            upgrade_check=ag_persisted_upgrade_check,
        )
        if up_err is not None:
            if up_err in ("file_identity_changed", "duplicate_inode_across_seats", "seat_mismatch"):
                quarantined_seats.add(name)
                set_status(name, context_worker="conflict", context_conflict=True,
                           context_error=up_err, context_at=now_fn())
                note("%s: context enrollment conflict (%s); pausing retries" % (name, up_err))
                return {"outcome": "conflict", "sleep_s": 0.0, "more_work": False}
            set_status(name, context_worker="config_error", context_config_error=True,
                       context_error=up_err, context_at=now_fn())
            return {"outcome": "config_error", "sleep_s": 0.0, "more_work": False}

        sources, verify_err = verified_enrolled_sources(
            ledger_path,
            seat=seat,
            dialect=dialect,
            allowed_roots=allowed_roots,
            box_enrolled_inodes=box_enrolled_inodes,
        )
    except LedgerError as exc:
        quarantined_seats.add(name)
        set_status(name, context_worker="conflict", context_conflict=True,
                   context_error="corrupt_enrollment_ledger", context_at=now_fn())
        note("%s: enrollment ledger error (%s); pausing retries" % (name, exc))
        return {"outcome": "conflict", "sleep_s": 0.0, "more_work": False}

    if verify_err is not None:
        quarantined_seats.add(name)
        set_status(name, context_worker="conflict", context_conflict=True,
                   context_error=verify_err, context_at=now_fn())
        note("%s: enrolled source verification failed (%s); pausing retries" % (name, verify_err))
        return {"outcome": "conflict", "sleep_s": 0.0, "more_work": False}

    if not sources:
        if drained_state is not None:
            drained_state.pop(name, None)
        set_status(name, context_worker="idle", context_at=now_fn())
        return {"outcome": "idle", "sleep_s": 0.0, "more_work": False}

    pre_sig = _sources_stat_signature(sources)
    if drained_state is not None:
        prev_drained = drained_state.get(name)
        if prev_drained is not None:
            due_drained_m, prev_sig = prev_drained
            now_d = mono_fn()
            if now_d < due_drained_m and pre_sig is not None and pre_sig == prev_sig:
                return {
                    "outcome": "drained_backoff",
                    "sleep_s": 0.0,
                    "more_work": False,
                    "due_in_s": due_drained_m - now_d,
                }
            drained_state.pop(name, None)

    rate_per_s, default_cap = rate_controller.budget(ident["base_url"])
    cfg_cap = ctx.get("max_batches")
    max_batches = cfg_cap if (type(cfg_cap) is int and cfg_cap >= 1) else default_cap
    try:
        argv = upload_argv(
            ident,
            store_dir=store_dir,
            seat=seat,
            dialect=dialect,
            sources=sources,
            max_batches=max_batches,
            namespace=ctx.get("namespace"),
        )
    except (OSError, LedgerError) as exc:
        err = "sources_file_unwritable"
        set_status(name, context_worker="error", context_error=err, context_at=now_fn())
        _note_error_once(name, err, "%s: %s" % (type(exc).__name__, exc), note, seat_notes)
        return {"outcome": "error", "sleep_s": 0.0, "more_work": False}
    t0 = mono_fn()
    rc, stdout, stderr = _invoke_runner(
        runner, argv, launch_dir, child_timeout_s, {"PYTHONPATH": app_root}
    )
    t1 = mono_fn()
    elapsed_s = t1 - t0

    if rc is None:
        if drained_state is not None:
            drained_state.pop(name, None)
        set_status(name, context_worker="timeout", context_error="child_timeout", context_at=now_fn())
        note("%s: context_upload timed out after %.1fs" % (name, child_timeout_s))
        return {"outcome": "timeout", "sleep_s": 0.0, "more_work": False}

    if _is_driver_missing(rc, stdout, stderr):
        if drained_state is not None:
            drained_state.pop(name, None)
        set_status(name, context_worker="driver_missing", context_at=now_fn())
        if name not in missing_driver_logged:
            missing_driver_logged.add(name)
            note("%s: context_upload module is not installed yet; skipping without crash loop" % name)
        return {"outcome": "driver_missing", "sleep_s": 0.0, "more_work": False}

    payload = _parse_json_stdout(stdout)
    held_out = sanitize_context_held(payload.get("context_held") if isinstance(payload, dict) else None)
    writes = writes_in_payload(payload)
    sleep_s = pace_sleep_s(writes, elapsed_s, rate_per_s)
    s_rate = rate_controller.seat_rate(name, ctx=ctx, ident=ident)
    if seat_next_mono is not None and writes > 0 and s_rate > 0.0:
        clamped_t0 = t0 if elapsed_s >= 0.0 else t1
        seat_next_mono[name] = max(seat_next_mono.get(name, 0.0), clamped_t0 + (float(writes) / float(s_rate)))

    reason = payload.get("reason") if isinstance(payload, dict) else None
    if reason == "rate_limited":
        if drained_state is not None:
            drained_state.pop(name, None)
        cfg_wait = ctx.get("rate_limit_wait_s", DEFAULT_RATE_LIMIT_WAIT_S)
        backoff_s = rate_limit_backoff_s(payload, default_wait_s=cfg_wait)
        rate_controller.latch_seat_for_night(name)
        if seat_next_mono is not None:
            seat_next_mono[name] = max(seat_next_mono.get(name, 0.0), t1 + backoff_s)
        if rc == 5:
            set_status(
                name,
                context_worker="rate_limited",
                context_error="rate_limited",
                context_backoff_s=round(backoff_s, 3),
                context_held=held_out,
                context_at=now_fn(),
            )
            return {"outcome": "rate_limited", "sleep_s": sleep_s, "more_work": True, "due_in_s": backoff_s}

    if rc == 0:
        if seat_notes is not None:
            seat_notes.pop(("unreachable", name), None)
            seat_notes.pop(("error", name), None)
        last_m = payload.get("last_manifest_key") if isinstance(payload, dict) else None
        set_status(
            name,
            context_worker="ok",
            context_conflict=False,
            context_config_error=False,
            context_error=None,
            context_held=held_out,
            context_last_manifest=last_m if isinstance(last_m, str) else None,
            context_f3_deferred=sanitize_f3_deferred(payload.get("f3_deferred") if isinstance(payload, dict) else None),
            context_at=now_fn(),
        )
        visited = sanitize_visited(payload.get("visited") if isinstance(payload, dict) else None, sources)
        if visited:
            # one line naming the sources the pass visited, in order, with each one's batches; repeated only when
            # that changes, so a long backfill is one line, and a new session overtaking it is the next
            sig = tuple((v["source"], v["batches"] > 0) for v in visited)
            if seat_notes is None or seat_notes.get(("visited", name)) != sig:
                if seat_notes is not None:
                    seat_notes[("visited", name)] = sig
                note("%s: context pass visited %s" % (
                    name, ", ".join("%s (%d batches)" % (os.path.basename(v["source"]), v["batches"])
                                    for v in visited)))
        items_c = payload.get("items_committed") if isinstance(payload, dict) else 0
        more_work = bool(
            isinstance(payload, dict)
            and (payload.get("status") == "progressed" or (type(items_c) is int and items_c > 0))
        )
        if drained_state is not None:
            if not more_work and pre_sig is not None:
                drained_backoff_s = _positive_rate(ctx.get("drained_backoff_s"), DEFAULT_DRAINED_BACKOFF_S)
                drained_state[name] = (t1 + drained_backoff_s, pre_sig)
            else:
                drained_state.pop(name, None)
        return {"outcome": "ok", "sleep_s": sleep_s, "more_work": more_work}

    if drained_state is not None:
        drained_state.pop(name, None)

    if rc == 3:
        if seat_notes is not None:
            seat_notes.pop(("unreachable", name), None)
        set_status(
            name,
            context_worker="held",
            context_conflict=False,
            context_config_error=False,
            context_error=None,
            context_held=held_out,
            context_at=now_fn(),
        )
        return {"outcome": "held", "sleep_s": sleep_s, "more_work": False}

    if rc == 4:
        quarantined_seats.add(name)
        codes = sanitize_conflict_codes(payload.get("conflicts") if isinstance(payload, dict) else None)
        set_status(
            name,
            context_worker="conflict",
            context_conflict=True,
            context_conflict_codes=codes,
            context_held=held_out,
            context_at=now_fn(),
        )
        note("%s: context_upload exited 4 (conflict: %s); pausing retries until reviewed"
             % (name, ", ".join(codes) if codes else "no code reported"))
        return {"outcome": "conflict", "sleep_s": 0.0, "more_work": False}

    if rc == 2:
        set_status(
            name,
            context_worker="config_error",
            context_config_error=True,
            context_at=now_fn(),
        )
        return {"outcome": "config_error", "sleep_s": 0.0, "more_work": False}

    if rc == 5:
        u_reason = sanitize_unreachable_reason(reason)
        set_status(name, context_worker="unreachable", context_error=u_reason, context_at=now_fn())
        if u_reason and (seat_notes is None or seat_notes.get(("unreachable", name)) != u_reason):
            if seat_notes is not None:
                seat_notes[("unreachable", name)] = u_reason
            note("%s: context_upload exited 5 (unreachable: %s)" % (name, u_reason))
        return {"outcome": "unreachable", "sleep_s": 0.0, "more_work": False}

    set_status(name, context_worker="error", context_error="rc=%d" % rc, context_at=now_fn())
    _note_error_once(name, "rc=%d" % rc, stderr, note, seat_notes)
    return {"outcome": "error", "sleep_s": 0.0, "more_work": False}


def _note_error_once(name: str, err: str, detail, note, seat_notes) -> None:
    """One log line for an upload pass that failed without a known exit code (a child that could not start is rc -1),
    with the last line of its stderr; repeated only when the error changes, so a failing seat is not silent and a
    failure on every tick is not a flood."""
    lines = [ln.strip() for ln in str(detail or "").splitlines() if ln.strip()]
    last = lines[-1][:300] if lines else "no stderr"
    if seat_notes is not None:
        if seat_notes.get(("error", name)) == (err, last):
            return
        seat_notes[("error", name)] = (err, last)
    note("%s: context_upload failed (%s): %s" % (name, err, last))


def _seat_store_dirs(identities: list[dict], store_root: str) -> tuple[dict[str, str], set[str]]:
    """Map identity name -> per-seat store dir, returning `(dirs, shared_dir_conflicts)`."""
    by_dir: dict[str, list[str]] = {}
    dirs: dict[str, str] = {}
    for ident in identities:
        name = ident["name"]
        ctx = ident["context"]
        raw_dir = ctx.get("store_dir") or os.path.join(store_root, name)
        abs_dir = os.path.abspath(raw_dir)
        dirs[name] = abs_dir
        norm_dir = os.path.normcase(abs_dir)
        by_dir.setdefault(norm_dir, []).append(name)
    clashes: set[str] = set()
    for names in by_dir.values():
        if len(names) > 1:
            clashes.update(names)
    return dirs, clashes


def _is_pending_antigravity_upgrade(entry: dict, *, upgrade_check=None) -> bool:
    if not isinstance(entry, dict) or entry.get("dialect") != "antigravity":
        return False
    path = entry.get("path")
    if not isinstance(path, str) or os.path.basename(path).lower() != "transcript.jsonl":
        return False
    return os.path.normcase(
        os.path.abspath(upgrade_antigravity_path(path, "antigravity", upgrade_check=upgrade_check))
    ) != os.path.normcase(
        os.path.abspath(path)
    )


def _ag_compact_pair(entry: dict, owner_seat: str) -> tuple[tuple[str, str], str] | None:
    if not isinstance(entry, dict) or entry.get("dialect") != "antigravity":
        return None
    path = entry.get("path")
    if not isinstance(path, str):
        return None
    base_lower = os.path.basename(path).lower()
    p_abs = os.path.abspath(path)
    if base_lower == "transcript.jsonl":
        full_norm = os.path.normcase(os.path.join(os.path.dirname(p_abs), "transcript_full.jsonl"))
        return ("ag_full_path", full_norm), p_abs
    if base_lower == "transcript_full.jsonl":
        compact_norm = os.path.normcase(os.path.join(os.path.dirname(p_abs), "transcript.jsonl"))
        return ("ag_compact_path", compact_norm), p_abs
    return None


def _refresh_ag_pair_inodes(box_enrolled_inodes: dict | None) -> None:
    """Reserve newly created Antigravity compact/full sibling files for the seat owning the enrolled half."""
    if box_enrolled_inodes is None:
        return
    for k, val in list(box_enrolled_inodes.items()):
        if not (isinstance(k, tuple) and len(k) == 2 and isinstance(val, tuple) and len(val) == 2):
            continue
        if k[0] == "ag_full_path":
            owner_seat, c_abs = val
            up_abs = os.path.abspath(upgrade_antigravity_path(c_abs, "antigravity"))
            if os.path.normcase(up_abs) != os.path.normcase(c_abs):
                up_ident, _ = _file_stat_identity(up_abs)
                if up_ident is not None:
                    up_ik = (up_ident[0], up_ident[1])
                    cur_holder = box_enrolled_inodes.get(up_ik)
                    if cur_holder is None:
                        box_enrolled_inodes[up_ik] = owner_seat
                    elif cur_holder != owner_seat:
                        box_enrolled_inodes[up_ik] = "__CONFLICT__"
        elif k[0] == "ag_compact_path":
            owner_seat, f_abs = val
            c_abs = os.path.join(os.path.dirname(os.path.abspath(f_abs)), "transcript.jsonl")
            c_ident, _ = _file_stat_identity(c_abs)
            if c_ident is not None:
                c_ik = (c_ident[0], c_ident[1])
                cur_holder = box_enrolled_inodes.get(c_ik)
                if cur_holder is None:
                    box_enrolled_inodes[c_ik] = owner_seat
                elif cur_holder != owner_seat:
                    box_enrolled_inodes[c_ik] = "__CONFLICT__"


def _superseded_reserved_inodes(superseded) -> list[tuple[tuple[int, int], str | None]]:
    """Return persisted `(dev, ino)` pairs recorded for retired compact Antigravity transcripts."""
    if not isinstance(superseded, dict):
        return []
    out: list[tuple[tuple[int, int], str | None]] = []
    for sv in superseded.values():
        if not isinstance(sv, dict):
            continue
        if type(sv.get("dev")) is int and type(sv.get("ino")) is int:
            sv_seat = sv.get("seat") if isinstance(sv.get("seat"), str) and sv["seat"] else None
            out.append(((int(sv["dev"]), int(sv["ino"])), sv_seat))
    return out


def _entry_reserved_inodes(entry: dict) -> list[tuple[int, int]]:
    """Return persisted `(dev, ino)` plus any eligible Antigravity compact/full sibling inode."""
    out: list[tuple[int, int]] = []
    if type(entry.get("dev")) is int and type(entry.get("ino")) is int:
        out.append((int(entry["dev"]), int(entry["ino"])))
    if _is_pending_antigravity_upgrade(entry):
        c_path = entry["path"]
        c_ident, _ = _file_stat_identity(c_path)
        if not os.path.lexists(c_path) or (
            c_ident is not None and (c_ident[0], c_ident[1]) == (entry.get("dev"), entry.get("ino"))
        ):
            up_path = os.path.abspath(upgrade_antigravity_path(c_path, "antigravity"))
            up_ident, _ = _file_stat_identity(up_path)
            if up_ident is not None:
                up_ik = (up_ident[0], up_ident[1])
                if up_ik not in out:
                    out.append(up_ik)
    elif (
        isinstance(entry, dict)
        and entry.get("dialect") == "antigravity"
        and isinstance(entry.get("path"), str)
        and os.path.basename(entry["path"]).lower() == "transcript_full.jsonl"
    ):
        c_sib = os.path.join(os.path.dirname(os.path.abspath(entry["path"])), "transcript.jsonl")
        c_sib_ident, _ = _file_stat_identity(c_sib)
        if c_sib_ident is not None:
            c_sib_ik = (c_sib_ident[0], c_sib_ident[1])
            if c_sib_ik not in out:
                out.append(c_sib_ik)
    return out


def _upgrade_persisted_antigravity_entries(
    ledger_path: str,
    *,
    seat: str,
    dialect: str,
    allowed_roots,
    box_enrolled_inodes: dict | None,
    now_fn,
    upgrade_check=None,
) -> str | None:
    """Upgrade any persisted compact `transcript.jsonl` entries whose sibling `transcript_full.jsonl` now exists."""
    if dialect != "antigravity":
        return None
    ledger = read_ledger(ledger_path)
    compact_paths = [
        entry["path"]
        for entry in list(ledger["files"].values())
        if _is_pending_antigravity_upgrade(entry, upgrade_check=upgrade_check) and entry.get("seat") == seat
    ]
    for c_path in compact_paths:
        ok_up, up_reason, _ = enroll_file(
            ledger_path,
            seat=seat,
            dialect="antigravity",
            file_path=c_path,
            allowed_roots=allowed_roots,
            box_enrolled_inodes=box_enrolled_inodes,
            now_fn=now_fn,
            upgrade_check=upgrade_check,
        )
        if not ok_up:
            return up_reason
    return None


def _box_pin_paths(enabled: list[dict]) -> list[str]:
    """Every enabled identity's explicit pins, as absolute paths (enroll_file's form). Pins are configured, so they win
    over any seat's discovery: an identity's pin is its own even before its pass enrolls it."""
    out = []
    for ident in enabled:
        ctx = ident.get("context") or {}
        dialect = ctx.get("dialect") or ident.get("cli") or ""
        for pin in ctx.get("pins") or ():
            if isinstance(pin, str) and pin.strip():
                out.append(os.path.abspath(pin))
                if dialect == "antigravity":
                    # Both siblings, whether or not they exist yet: enroll_file takes transcript_full.jsonl for a
                    # compact pin when it exists, and this list is built once, at startup, so one that appears later
                    # must already be reserved (a reviewer f414df).
                    folder = os.path.dirname(os.path.abspath(pin))
                    out += [os.path.join(folder, "transcript.jsonl"), os.path.join(folder, "transcript_full.jsonl")]
    return out


def _preload_box_inodes(
    enabled: list[dict],
    all_identities: list[dict],
    dirs: dict[str, str],
    clashes: set[str],
    store_root: str,
    *,
    box_enrolled_inodes: dict,
    quarantined_seats: set,
    set_status,
    note,
    now_fn,
) -> None:
    """Hydrate `box_enrolled_inodes` from all persisted seat ledgers before processing any pins or uploads."""
    owners_by_inode: dict[tuple[int, int], set[str]] = {}
    enabled_names_by_inode: dict[tuple[int, int], set[str]] = {}
    ag_pairs: dict[tuple[str, str], tuple[set[str], str]] = {}

    # 1. Read every enabled seat's persisted ledger first to reserve active inodes.
    for ident in enabled:
        name = ident["name"]
        if name in clashes:
            continue
        seat = resolve_seat(ident)
        ledger_path = os.path.join(dirs[name], "enrollment.json")
        try:
            ledger = read_ledger(ledger_path)
        except LedgerError as exc:
            quarantined_seats.add(name)
            set_status(name, context_worker="conflict", context_conflict=True,
                       context_error="corrupt_enrollment_ledger", context_at=now_fn())
            note("%s: enrollment ledger error (%s); pausing retries" % (name, exc))
            continue

        for entry in ledger["files"].values():
            if not isinstance(entry, dict):
                continue
            entry_seat = entry.get("seat") if isinstance(entry.get("seat"), str) and entry.get("seat") else seat
            for ik in _entry_reserved_inodes(entry):
                owners_by_inode.setdefault(ik, set()).add(entry_seat)
                enabled_names_by_inode.setdefault(ik, set()).add(name)
            pair_info = _ag_compact_pair(entry, entry_seat)
            if pair_info is not None:
                pk, c_abs = pair_info
                ag_pairs.setdefault(pk, (set(), c_abs))[0].add(entry_seat)
        for sup_ik, sup_seat in _superseded_reserved_inodes(ledger.get("superseded")):
            eff_sup_seat = sup_seat or seat
            owners_by_inode.setdefault(sup_ik, set()).add(eff_sup_seat)
            enabled_names_by_inode.setdefault(sup_ik, set()).add(name)

    # 2. Also reserve inodes from disabled/context-disabled identities or sibling ledgers under store_root.
    extra_ledgers: set[str] = set()
    enabled_names = {ident["name"] for ident in enabled}
    for ident in all_identities:
        if not isinstance(ident, dict):
            continue
        nm = ident.get("name")
        if not isinstance(nm, str) or not nm or nm in enabled_names:
            continue
        ctx = ident.get("context") if isinstance(ident.get("context"), dict) else {}
        s_dir = os.path.abspath(ctx.get("store_dir") or os.path.join(store_root, nm))
        extra_ledgers.add(os.path.join(s_dir, "enrollment.json"))
    try:
        if os.path.isdir(store_root):
            for child in os.listdir(store_root):
                extra_ledgers.add(os.path.abspath(os.path.join(store_root, child, "enrollment.json")))
    except OSError:
        pass
    seen_enabled_ledgers = {os.path.normcase(os.path.abspath(os.path.join(dirs[i["name"]], "enrollment.json"))) for i in enabled}
    for lp in sorted(extra_ledgers):
        if os.path.normcase(os.path.abspath(lp)) in seen_enabled_ledgers:
            continue
        try:
            extra_data = read_ledger(lp)
        except LedgerError:
            continue
        for entry in extra_data["files"].values():
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("seat"), str)
                and entry["seat"]
            ):
                for ik in _entry_reserved_inodes(entry):
                    owners_by_inode.setdefault(ik, set()).add(entry["seat"])
                pair_info = _ag_compact_pair(entry, entry["seat"])
                if pair_info is not None:
                    pk, c_abs = pair_info
                    ag_pairs.setdefault(pk, (set(), c_abs))[0].add(entry["seat"])
        for sup_ik, sup_seat in _superseded_reserved_inodes(extra_data.get("superseded")):
            if sup_seat:
                owners_by_inode.setdefault(sup_ik, set()).add(sup_seat)

    # 3. Populate box_enrolled_inodes; if two persisted ledgers already claim the same inode for different seats,
    # poison the inode and quarantine every enabled seat involved so neither can upload it.
    for pk, (pair_seats, c_abs) in ag_pairs.items():
        box_enrolled_inodes[pk] = (next(iter(pair_seats)) if len(pair_seats) == 1 else "__CONFLICT__", c_abs)
    for ik, owner_seats in owners_by_inode.items():
        if len(owner_seats) == 1:
            box_enrolled_inodes[ik] = next(iter(owner_seats))
        else:
            box_enrolled_inodes[ik] = "__CONFLICT__"
            for bad_name in sorted(enabled_names_by_inode.get(ik, ())):
                quarantined_seats.add(bad_name)
                set_status(bad_name, context_worker="conflict", context_conflict=True,
                           context_error="duplicate_inode_across_seats", context_at=now_fn())
                note("%s: persisted duplicate inode across seats; pausing retries" % bad_name)

    # 4. Upgrade any persisted Antigravity compact entries whose full sibling appeared while stopped, then verify.
    for ident in enabled:
        name = ident["name"]
        if name in clashes or name in quarantined_seats:
            continue
        seat = resolve_seat(ident)
        ctx = ident["context"]
        dialect = ctx.get("dialect") or ident.get("cli") or ""
        allowed_roots = ctx.get("allowed_roots")
        ledger_path = os.path.join(dirs[name], "enrollment.json")
        if dialect not in VALID_DIALECTS or not isinstance(allowed_roots, list) or not allowed_roots:
            continue
        try:
            ag_preload_upgrade_check = make_antigravity_upgrade_check(
                ctx.get("discover"), pins=ctx.get("pins"), require_session_id_match=False
            )
            up_err = _upgrade_persisted_antigravity_entries(
                ledger_path,
                seat=seat,
                dialect=dialect,
                allowed_roots=allowed_roots,
                box_enrolled_inodes=box_enrolled_inodes,
                now_fn=now_fn,
                upgrade_check=ag_preload_upgrade_check,
            )
            if up_err is not None:
                if up_err in ("file_identity_changed", "duplicate_inode_across_seats", "seat_mismatch"):
                    quarantined_seats.add(name)
                    set_status(name, context_worker="conflict", context_conflict=True,
                               context_error=up_err, context_at=now_fn())
                    note("%s: context enrollment conflict (%s); pausing retries" % (name, up_err))
                else:
                    set_status(name, context_worker="config_error", context_config_error=True,
                               context_error=up_err, context_at=now_fn())
                continue
            if not read_ledger(ledger_path)["files"]:
                continue
            _, verify_err = verified_enrolled_sources(
                ledger_path,
                seat=seat,
                dialect=dialect,
                allowed_roots=allowed_roots,
                box_enrolled_inodes=box_enrolled_inodes,
            )
        except LedgerError as exc:
            quarantined_seats.add(name)
            set_status(name, context_worker="conflict", context_conflict=True,
                       context_error="corrupt_enrollment_ledger", context_at=now_fn())
            note("%s: enrollment ledger error (%s); pausing retries" % (name, exc))
            continue
        if verify_err is not None:
            quarantined_seats.add(name)
            set_status(name, context_worker="conflict", context_conflict=True,
                       context_error=verify_err, context_at=now_fn())
            note("%s: enrolled source verification failed (%s); pausing retries" % (name, verify_err))


def run_context_worker(
    identities: list[dict],
    *,
    all_identities: list[dict] | None = None,
    store_root: str,
    set_status,
    note,
    now_fn=_utc_now,
    postbox_python: str | None = None,
    platform: str | None = None,
    rate_controller: RateController | None = None,
    runner=_run_subprocess,
    mono_fn=time.monotonic,
    sleep_fn=time.sleep,
    idle_sleep_s: float = DEFAULT_IDLE_SLEEP_S,
    child_timeout_s: float = DEFAULT_CHILD_TIMEOUT_S,
    max_rounds: int | None = None,
) -> None:
    """Single-thread round-robin context capture loop across all context-enabled seats on the box."""
    enabled = context_enabled_identities(identities)
    if not enabled:
        return
    ownership_identities = list(all_identities) + list(identities) if all_identities is not None else list(identities)
    controller = rate_controller if rate_controller is not None else RateController(mono_fn=mono_fn)
    dirs, clashes = _seat_store_dirs(enabled, store_root)
    box_enrolled_inodes: dict = {}
    quarantined_seats: set = set()
    missing_driver_logged: set = set()
    seat_next_mono: dict[str, float] = {}
    drained_state: dict[str, tuple[float, tuple]] = {}
    discover_state: dict[str, float] = {}

    for bad_name in sorted(clashes):
        quarantined_seats.add(bad_name)
        set_status(bad_name, context_worker="config_error", context_config_error=True,
                   context_error="shared_store_dir", context_at=now_fn())
        note("%s: refuses shared context store_dir; each identity requires its own store" % bad_name)

    _preload_box_inodes(
        enabled,
        ownership_identities,
        dirs,
        clashes,
        store_root,
        box_enrolled_inodes=box_enrolled_inodes,
        quarantined_seats=quarantined_seats,
        set_status=set_status,
        note=note,
        now_fn=now_fn,
    )

    box_pins = _box_pin_paths(enabled)
    names_by_seat = seat_identity_names(ownership_identities)
    seat_notes: dict = {}
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        _refresh_ag_pair_inodes(box_enrolled_inodes)
        more_work_seats: list[str] = []
        for ident in enabled:
            name = ident["name"]
            if name in clashes:
                continue
            try:
                res = run_seat_pass(
                    ident,
                    store_dir=dirs[name],
                    rate_controller=controller,
                    box_enrolled_inodes=box_enrolled_inodes,
                    quarantined_seats=quarantined_seats,
                    missing_driver_logged=missing_driver_logged,
                    set_status=set_status,
                    note=note,
                    seat_next_mono=seat_next_mono,
                    drained_state=drained_state,
                    now_fn=now_fn,
                    postbox_python=postbox_python,
                    platform=platform,
                    runner=runner,
                    mono_fn=mono_fn,
                    child_timeout_s=child_timeout_s,
                    discover_state=discover_state,
                    box_pins=box_pins,
                    seat_names=names_by_seat.get(resolve_seat(ident), frozenset()) | {name},
                    seat_notes=seat_notes,
                )
            except Exception as exc:
                note("%s: context worker seat pass crashed: %s\n%s"
                     % (name, type(exc).__name__, traceback.format_exc()))
                set_status(name, context_worker="crashed",
                           context_error="%s: %s" % (type(exc).__name__, exc), context_at=now_fn())
                continue
            if res["sleep_s"] > 0.0:
                sleep_fn(res["sleep_s"])
            if res["more_work"]:
                more_work_seats.append(name)
        if max_rounds is None or rounds < max_rounds:
            if not more_work_seats:
                sleep_fn(idle_sleep_s)
            else:
                now_end = mono_fn()
                rems = [seat_next_mono.get(n, 0.0) - now_end for n in more_work_seats]
                if all(r > 0.0 for r in rems):
                    sleep_fn(min(idle_sleep_s, min(rems)))


def guarded_context_worker(identities: list[dict], *, pause_s: float = 60.0, sleep_fn=time.sleep, **kwargs) -> None:
    """Supervisor wrapper isolating the context worker thread from ever dying on an unexpected outer error."""
    note = kwargs.get("note")
    while True:
        try:
            run_context_worker(identities, sleep_fn=sleep_fn, **kwargs)
            return
        except Exception:
            trace = traceback.format_exc()
            if callable(note):
                try:
                    note("context_worker crashed, restarting in %d s\n%s" % (pause_s, trace))
                except Exception:
                    pass
            sleep_fn(pause_s)
