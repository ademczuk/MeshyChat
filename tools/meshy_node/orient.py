"""Pre-idle orientation for `python -m tools.meshy_node orient`.

In addition to the five original sources below, explicitly configured --repo paths
get open GitHub PR review states, and a configured seat/state file gets read-only
board tasks and potential FREE-owner rows from TKT-998's ranked pull queue. --offline skips
both additions; absent configuration or failed/capped reads remain explicit gaps.
Board reads use per-call transport timeouts; no whole-run deadline or total read
budget is enforced. Queue documents are limited to 1 MiB of UTF-8 text before parsing.

Inspects 5 sources before a seat goes idle so the seat never finishes a single turn
passively without surfacing concrete next-step options (`candidate_options`):

1. Postbox Spool & Banked `cc` Check (`spool_dir`):
   Reads `<identity>.log.<consumer>.offset.bank.json` (`held` array) and the tail of
   `<identity>.log` (strictly skipping `*.lock` files to avoid Windows mandatory byte-range
   locks), surfacing banked `cc` messages (#199) and uncollected spool bytes.
2. MeshyChat Message Graph (`owed`, `awaiting`, `brief`):
   Queries `owed`, `awaiting`, and `brief` when `--base-url` and `--state-file` are
   provided (and `--offline` is not set), extracting `reply_owed`, `answered_still_open`,
   `awaiting`, and `threads_with_news`.
3. Local Worktrees & Branches (`--worktree-root` / `--repo`):
   Scans git repositories and worktrees for uncommitted changes (`dirty_count`), commits
   ahead of trunk (`origin/main`), and unpushed branch state.
4. Handovers & Peer Seat Transcripts / Gate Logs (`--handover-dir`, `--scratchpad-dir`):
   Checks `HANDOVER-*.md` staleness (age in hours) and scans `gate-runner.log` /
   `verdict.txt` files for `LOCAL GATE PASS` or `LOCAL GATE REFUSED` outcomes.
5. Brain (`staff` & `elevated`) & Environment Hygiene:
   Detects `REFUSED_ENVIRONMENT` variables (`OPENSSL_CONF`, `NODE_OPTIONS`,
   `NODE_TLS_REJECT_UNAUTHORIZED`, `NODE_USE_ENV_PROXY`, `NODE_DEBUG`,
   `NODE_DEBUG_NATIVE`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_USE_SYSTEM_CA`,
   `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NODE_EXTRA_CA_CERTS`) in `os.environ`
   that cause `brain-os.mjs` / `codex-brain-bridge.mjs` to exit with `rc=5` on Windows HQB,
   and optionally runs `cf-access-headers.sh --identity` with refused variables stripped
   for `staff` and `elevated` to report whether Brain sign-in is active or lapsed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import board_queue, board_tasks, cli, node, preflight, recipientwarn

REFUSED_ENV_VARS: tuple[str, ...] = (
    "OPENSSL_CONF",
    "NODE_OPTIONS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "NODE_USE_ENV_PROXY",
    "NODE_DEBUG",
    "NODE_DEBUG_NATIVE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_USE_SYSTEM_CA",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NODE_EXTRA_CA_CERTS",
)

REFUSED_ENV_RE = re.compile(
    r"^(?:NODE_OPTIONS|NODE_TLS_REJECT_UNAUTHORIZED|NODE_USE_ENV_PROXY|NODE_DEBUG|"
    r"NODE_DEBUG_NATIVE|OPENSSL_CONF|HTTPS?_PROXY|ALL_PROXY|NODE_USE_SYSTEM_CA|"
    r"SSL_CERT_FILE|SSL_CERT_DIR|NODE_EXTRA_CA_CERTS)$",
    re.IGNORECASE,
)

DEFAULT_STALE_HANDOVER_HOURS = 24.0
DEFAULT_SPOOL_TAIL_LINES = 25
SUBPROCESS_TIMEOUT_S = 10.0
MAX_QUEUE_DOCUMENT_BYTES = 1024 * 1024

_GATE_PASS_RE = re.compile(r"\bLOCAL\s+GATE\s+PASS\b", re.IGNORECASE)
_GATE_REFUSED_RE = re.compile(r"\bLOCAL\s+GATE\s+(?:REFUSED|FAIL(?:ED)?)\b", re.IGNORECASE)
_HANDOVER_SEAT_RE = re.compile(
    r"^HANDOVER-(?P<seat>[A-Za-z0-9_-]+?)(?:-session)?(?:-\d{4}-\d{2}-\d{2}[A-Za-z0-9_-]*|-\d{8})?\.md$",
    re.IGNORECASE,
)


def _no_window_flags() -> int:
    return 0x08000000 if os.name == "nt" else 0


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _tail_jsonl_lines(path: Path, max_lines: int = DEFAULT_SPOOL_TAIL_LINES, max_bytes: int = 65536) -> list[dict]:
    """Read up to `max_lines` parsed JSON objects from the tail of `path` without touching any `.lock` file."""
    if path.name.endswith(".lock") or not path.is_file():
        return []
    try:
        size = path.stat().st_size
        read_len = min(size, max(4096, max_bytes))
        with path.open("rb") as fh:
            if size > read_len:
                fh.seek(size - read_len)
            raw = fh.read(read_len)
    except OSError:
        return []
    lines = raw.splitlines()
    if size > read_len and lines:
        lines = lines[1:]  # drop partial leading line
    parsed: list[dict] = []
    for raw_line in lines[-max_lines:]:
        try:
            obj = json.loads(raw_line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
    return parsed


_TERMINAL_DISPOSITIONS: tuple[str, ...] = ("done", "no_action", "superseded")


def _msg_version(d: dict[str, Any], token_str: Any = None) -> int | None:
    """Extract positive integer message version matching `delivery_bank.version`."""
    if "version" in d:
        val = d.get("version")
        return val if type(val) is int and val > 0 else None
    if isinstance(token_str, str) and token_str.startswith("["):
        try:
            parsed = json.loads(token_str)
            if isinstance(parsed, list) and len(parsed) >= 3 and type(parsed[2]) is int and parsed[2] > 0:
                return parsed[2]
        except ValueError:
            pass
    return 1


def _is_valid_handled_entry(item: Any) -> bool:
    """Match `delivery_bank.entry_faults`: terminal disposition + verified signature & status + exact key/version."""
    if not isinstance(item, dict):
        return False
    if not isinstance(item.get("key"), str) or not item["key"]:
        return False
    ver = item.get("version")
    if "version" not in item or type(ver) is not int or ver <= 0:
        return False
    if item.get("disposition") not in _TERMINAL_DISPOSITIONS:
        return False
    if item.get("signature_verified") is not True or item.get("status_verified") is not True:
        return False
    return True


def _parse_handled_receipt_file(path: Path, expected_identity: str) -> set[tuple[str, int]]:
    """Load authenticated terminal `(key, version)` pairs from a postbox handled receipt file.

    Follows `tools/postbox/delivery_bank.py::handled(..., skip_bad=True)`:
    - Rejects symlinks or non-regular files.
    - For standard JSON receipts (`schema_version == 1`), verifies `identity == expected_identity`
      and `isinstance(handled, list)`, skipping individual malformed entries while keeping valid ones.
    - Also supports line-delimited `.jsonl` handled ledgers when every entry passes `_is_valid_handled_entry`.
    """
    try:
        if path.name.endswith(".lock") or path.is_symlink() or not path.is_file():
            return set()
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()

    completed: set[tuple[str, int]] = set()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None

    if isinstance(data, dict):
        if (
            data.get("schema_version") != 1
            or data.get("identity") != expected_identity
            or not isinstance(data.get("handled"), list)
        ):
            return set()
        for item in data["handled"]:
            if _is_valid_handled_entry(item):
                completed.add((item["key"], item["version"]))
        return completed

    if path.suffix.lower() == ".jsonl":
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("identity") not in (None, expected_identity):
                continue
            if _is_valid_handled_entry(item):
                completed.add((item["key"], item["version"]))
    return completed


def _discover_handled_versions(
    spool_dir: Path,
    ident_name: str,
    *,
    postbox_config: Path | None = None,
    handled_files: dict[str, Path] | None = None,
) -> set[tuple[str, int]]:
    """Locate and load terminal `(key, version)` pairs for `ident_name` across configured and standard paths."""
    candidate_paths: list[Path] = []
    if handled_files and ident_name in handled_files and handled_files[ident_name]:
        candidate_paths.append(Path(handled_files[ident_name]))

    cfg_candidates = [postbox_config] if postbox_config else [
        spool_dir / "config.json",
        spool_dir.parent / "config.json",
    ]
    for cfg_path in cfg_candidates:
        if cfg_path is None or not cfg_path.is_file() or cfg_path.is_symlink():
            continue
        try:
            cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        identities_list = cfg_data.get("identities") if isinstance(cfg_data, dict) else None
        if not isinstance(identities_list, list):
            continue
        for entry in identities_list:
            if not isinstance(entry, dict) or entry.get("name") != ident_name:
                continue
            if os.name == "nt" and entry.get("handled_private_acl_verified") is False:
                continue
            hfile = entry.get("handled_file")
            if isinstance(hfile, str) and hfile.strip():
                candidate_paths.append(Path(hfile.strip()))

    candidate_paths.extend([
        spool_dir.parent / "handled" / f"{ident_name}.json",
        spool_dir.parent.parent / "receipt-ledgers" / ident_name / "handled.json",
        spool_dir / "handled" / f"{ident_name}.json",
        spool_dir / f"{ident_name}.handled.json",
        spool_dir / f"{ident_name}.log.handled.json",
        spool_dir / f"{ident_name}.handled.jsonl",
        spool_dir / f"{ident_name}.log.handled.jsonl",
    ])

    completed: set[tuple[str, int]] = set()
    seen_paths: set[str] = set()
    for cpath in candidate_paths:
        norm = os.path.normcase(str(cpath))
        if norm in seen_paths:
            continue
        seen_paths.add(norm)
        completed.update(_parse_handled_receipt_file(cpath, ident_name))
    return completed


def inspect_spool(
    spool_dir: Path | None,
    *,
    identities: list[str] | None = None,
    tail_lines: int = DEFAULT_SPOOL_TAIL_LINES,
    postbox_config: Path | None = None,
    handled_files: dict[str, Path] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Source 1: Inspect postbox spool `.bank.json` and `.log` tails (never touching `*.lock`)."""
    now_ts = time.time() if now is None else now
    if spool_dir is None or not spool_dir.is_dir():
        return {
            "available": False,
            "spool_dir": str(spool_dir) if spool_dir else None,
            "bank_files_checked": 0,
            "held_cc_count": 0,
            "held_cc": [],
            "uncollected": [],
            "recent_tail_messages": [],
        }

    ident_filter = {i.strip() for i in (identities or []) if i and i.strip()}
    held_cc: list[dict[str, Any]] = []
    uncollected: list[dict[str, Any]] = []
    recent_tail_messages: list[dict[str, Any]] = []
    bank_files_checked = 0

    try:
        entries = sorted(spool_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        entries = []

    bank_files: list[Path] = []
    log_files: dict[str, Path] = {}
    for entry in entries:
        name = entry.name
        if name.endswith(".lock"):
            continue
        if name.endswith(".offset.bank.json") and entry.is_file():
            bank_files.append(entry)
        elif name.endswith(".log") and entry.is_file():
            ident_name = name[:-4]
            if not ident_filter or ident_name in ident_filter:
                log_files[ident_name] = entry

    seen_held_keys: set[tuple[str, str, int | None]] = set()
    handled_cache: dict[str, set[tuple[str, int]]] = {}
    for bpath in bank_files:
        prefix = bpath.name.split(".log.", 1)[0]
        if ident_filter and prefix not in ident_filter:
            continue
        bank_files_checked += 1
        try:
            data = json.loads(bpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        ident_name = str(data.get("identity") or prefix)
        if ident_filter and ident_name not in ident_filter and prefix not in ident_filter:
            continue
        if ident_name not in handled_cache:
            handled_cache[ident_name] = _discover_handled_versions(
                spool_dir,
                ident_name,
                postbox_config=postbox_config,
                handled_files=handled_files,
            )
        completed_versions = handled_cache[ident_name]
        consumer = str(data.get("consumer") or "")
        offset = data.get("offset")
        delivered_raw = data.get("delivered")
        delivered = set(delivered_raw) if isinstance(delivered_raw, list) else set()
        raw_held = data.get("held")
        if isinstance(raw_held, list):
            for item in raw_held:
                if not isinstance(item, dict):
                    continue
                tok = item.get("token")
                if isinstance(tok, str) and tok in delivered:
                    continue
                routing = item.get("routing") if isinstance(item.get("routing"), dict) else {}
                key = str(routing.get("key") or item.get("key") or "")
                if not key:
                    continue
                msg_ver = _msg_version(routing if routing else item, tok)
                if msg_ver is not None and (key, msg_ver) in completed_versions:
                    continue
                dedup = (ident_name, key, msg_ver)
                if dedup in seen_held_keys:
                    continue
                seen_held_keys.add(dedup)
                held_at = item.get("held_at")
                ep = _epoch(held_at)
                age_s = max(0.0, round(now_ts - ep, 1)) if ep is not None else None
                held_cc.append({
                    "identity": ident_name,
                    "consumer": consumer,
                    "key": key,
                    "version": msg_ver,
                    "from": routing.get("from") or item.get("from"),
                    "role": routing.get("role") or "cc",
                    "requires_ack": routing.get("requires_ack", False),
                    "created_at": routing.get("created_at") or item.get("created_at"),
                    "held_at": held_at,
                    "age_s": age_s,
                    "preview_unverified": routing.get("preview_unverified"),
                    "bank_file": bpath.name,
                })

        log_path = spool_dir / f"{prefix}.log"
        if type(offset) is int and offset >= 0 and log_path.is_file():
            try:
                log_size = log_path.stat().st_size
            except OSError:
                log_size = 0
            if log_size > offset:
                uncollected.append({
                    "identity": ident_name,
                    "consumer": consumer,
                    "offset": offset,
                    "log_size": log_size,
                    "uncollected_bytes": log_size - offset,
                })

    for ident_name, lpath in sorted(log_files.items()):
        tail = _tail_jsonl_lines(lpath, max_lines=tail_lines)
        for obj in tail:
            if obj.get("event") in ("message", "edit") and isinstance(obj.get("key"), str):
                recent_tail_messages.append({
                    "identity": ident_name,
                    "event": obj.get("event"),
                    "key": obj.get("key"),
                    "from": obj.get("from"),
                    "to": obj.get("to"),
                    "cc": obj.get("cc"),
                    "role": obj.get("role"),
                    "requires_ack": obj.get("requires_ack"),
                    "created_at": obj.get("created_at") or obj.get("edited_at"),
                    "rx_at": obj.get("rx_at"),
                })

    return {
        "available": True,
        "spool_dir": str(spool_dir),
        "bank_files_checked": bank_files_checked,
        "held_cc_count": len(held_cc),
        "held_cc": held_cc,
        "uncollected": uncollected,
        "recent_tail_messages": recent_tail_messages[-tail_lines:],
    }


def inspect_mesh_graph(
    state_file: Path | None,
    base_url: str | None,
    *,
    since: str = "14d",
    offline: bool = False,
    now=None,
) -> dict[str, Any]:
    """Source 2: Query MeshyChat message graph (`owed`, `awaiting`, `brief`)."""
    if offline or not base_url or state_file is None:
        return {
            "available": False,
            "mode": "offline",
            "reply_owed_count": 0,
            "reply_owed": [],
            "answered_still_open_count": 0,
            "answered_still_open": [],
            "awaiting_count": 0,
            "awaiting": [],
            "threads_with_news_count": 0,
            "threads_with_news": [],
            "help_wanted_count": 0,
            "help_wanted": [],
            "error": None,
        }

    try:
        norm_url = node.normalize_base_url(base_url)
        state = node.load_state(state_file, norm_url)
        cutoff = cli._owed_since(since, now=now)
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "mode": "error",
            "reply_owed_count": 0,
            "reply_owed": [],
            "answered_still_open_count": 0,
            "answered_still_open": [],
            "awaiting_count": 0,
            "awaiting": [],
            "threads_with_news_count": 0,
            "threads_with_news": [],
            "help_wanted_count": 0,
            "help_wanted": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    errors_seen: list[str] = []
    reply_owed_items: list[dict[str, Any]] = []
    answered_items: list[dict[str, Any]] = []
    awaiting_items: list[dict[str, Any]] = []
    threads_news: list[dict[str, Any]] = []
    help_wanted: list[dict[str, Any]] = []

    # 1. `brief` call (gives owed, awaiting, threads, help_wanted on modern server)
    brief_ans: dict[str, Any] | None = None
    try:
        brief_ans = node.brief(state_file, state, since=cutoff)
    except Exception as exc:  # noqa: BLE001
        errors_seen.append(f"brief: {type(exc).__name__}: {exc}")

    # 2. `owed` call for explicit open,active,answered buckets
    owed_ans: dict[str, Any] | None = None
    try:
        owed_ans = node.owed(state_file, state, limit=None, states=cli.OWED_DEFAULT_STATE, since=cutoff)
    except Exception as exc:  # noqa: BLE001
        errors_seen.append(f"owed: {type(exc).__name__}: {exc}")

    # 3. `awaiting` call if brief did not provide awaiting
    awaiting_ans: dict[str, Any] | None = None
    if not isinstance(brief_ans, dict) or not isinstance(brief_ans.get("awaiting"), dict):
        try:
            awaiting_ans = node.awaiting(state_file, state, states="open,active", since=cutoff)
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(f"awaiting: {type(exc).__name__}: {exc}")

    if isinstance(owed_ans, dict):
        ro_bucket = cli._owed_bucket(owed_ans, cli.OWED_REPLY_STATES)
        ans_bucket = cli._owed_bucket(owed_ans, (cli.OWED_ANSWERED_STATE,))
        for row in ro_bucket.get("items", []):
            hit = row.get("hit") if isinstance(row.get("hit"), dict) else {}
            reply_owed_items.append({
                "key": hit.get("key"),
                "author": hit.get("author"),
                "thread_id": row.get("thread_id") or hit.get("thread_id"),
                "state": row.get("state"),
                "msg_type": row.get("msg_type"),
                "recorded_at": hit.get("recorded_at"),
                "done_when": row.get("done_when"),
            })
        for row in ans_bucket.get("items", []):
            hit = row.get("hit") if isinstance(row.get("hit"), dict) else {}
            answered_items.append({
                "key": hit.get("key"),
                "author": hit.get("author"),
                "thread_id": row.get("thread_id") or hit.get("thread_id"),
                "state": row.get("state"),
                "msg_type": row.get("msg_type"),
                "recorded_at": hit.get("recorded_at"),
                "done_when": row.get("done_when"),
            })
    elif isinstance(brief_ans, dict) and isinstance(brief_ans.get("owed"), dict):
        for row in brief_ans["owed"].get("items", []):
            if not isinstance(row, dict):
                continue
            hit = row.get("hit") if isinstance(row.get("hit"), dict) else {}
            reply_owed_items.append({
                "key": hit.get("key"),
                "author": hit.get("author"),
                "thread_id": row.get("thread_id") or hit.get("thread_id"),
                "state": row.get("state"),
                "msg_type": row.get("msg_type"),
                "recorded_at": hit.get("recorded_at"),
                "done_when": row.get("done_when"),
            })
        try:
            ans_fallback = cli._answered_still_open(state_file, state, cutoff)
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(f"answered_still_open: {type(exc).__name__}: {exc}")
            ans_fallback = {}
        else:
            if isinstance(ans_fallback, dict) and "count" in ans_fallback and ans_fallback.get("count") is None:
                errors_seen.append("answered_still_open: unavailable")
        for row in ans_fallback.get("items", []) if isinstance(ans_fallback, dict) else []:
            hit = row.get("hit") if isinstance(row.get("hit"), dict) else {}
            answered_items.append({
                "key": hit.get("key"),
                "author": hit.get("author"),
                "thread_id": row.get("thread_id") or hit.get("thread_id"),
                "state": row.get("state"),
                "msg_type": row.get("msg_type"),
                "recorded_at": hit.get("recorded_at"),
                "done_when": row.get("done_when"),
            })

    if isinstance(brief_ans, dict) and isinstance(brief_ans.get("awaiting"), dict):
        raw_aw = brief_ans["awaiting"].get("items", [])
    elif isinstance(awaiting_ans, dict):
        raw_aw = awaiting_ans.get("awaiting", [])
    else:
        raw_aw = []
    for row in raw_aw if isinstance(raw_aw, list) else []:
        if not isinstance(row, dict):
            continue
        hit = row.get("hit") if isinstance(row.get("hit"), dict) else {}
        awaiting_items.append({
            "key": hit.get("key"),
            "owing_label": row.get("owing_label") or hit.get("owing_label"),
            "thread_id": row.get("thread_id") or hit.get("thread_id"),
            "state": row.get("state"),
            "recorded_at": hit.get("recorded_at"),
            "done_when": row.get("done_when"),
        })

    if isinstance(brief_ans, dict):
        threads_sec = brief_ans.get("threads")
        if isinstance(threads_sec, dict) and isinstance(threads_sec.get("items"), list):
            for row in threads_sec["items"]:
                if isinstance(row, dict):
                    threads_news.append({
                        "thread_id": row.get("thread_id"),
                        "new": row.get("new"),
                        "last_author": row.get("last_author"),
                        "last_recorded_at": row.get("last_recorded_at"),
                    })
        hw_sec = brief_ans.get("help_wanted")
        if isinstance(hw_sec, dict) and isinstance(hw_sec.get("items"), list):
            for row in hw_sec["items"]:
                if isinstance(row, dict):
                    help_wanted.append({
                        "key": row.get("key"),
                        "author": row.get("author"),
                        "thread_id": row.get("thread_id"),
                        "recorded_at": row.get("recorded_at"),
                    })

    available = bool(brief_ans is not None or owed_ans is not None or awaiting_ans is not None)
    return {
        "available": available,
        "mode": ("partial" if errors_seen else "live") if available else "error",
        "seat": state.get("label"),
        "reply_owed_count": len(reply_owed_items),
        "reply_owed": reply_owed_items,
        "answered_still_open_count": len(answered_items),
        "answered_still_open": answered_items,
        "awaiting_count": len(awaiting_items),
        "awaiting": awaiting_items,
        "threads_with_news_count": len(threads_news),
        "threads_with_news": threads_news,
        "help_wanted_count": len(help_wanted),
        "help_wanted": help_wanted,
        "error": "; ".join(errors_seen) if errors_seen else None,
    }


def _git_run(cwd: Path, args: list[str], timeout: float = SUBPROCESS_TIMEOUT_S) -> subprocess.CompletedProcess | None:
    git_env = dict(os.environ)
    git_env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=git_env,
            timeout=timeout,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _inspect_single_git_dir(git_dir: Path, trunk: str = "origin/main") -> dict[str, Any] | None:
    if not git_dir.is_dir() or not (git_dir / ".git").exists():
        return None
    st = _git_run(git_dir, ["status", "--porcelain", "-b"])
    if st is None or st.returncode != 0:
        return None
    lines = [line for line in st.stdout.splitlines() if line.strip()]
    branch = "HEAD"
    dirty_files: list[str] = []
    for line in lines:
        if line.startswith("## "):
            header = line[3:].strip()
            if header.startswith("HEAD (no branch)") or header.startswith("No commits yet"):
                branch = "detached"
            else:
                branch = header.split("...", 1)[0].split(" ", 1)[0].strip()
        else:
            dirty_files.append(line[3:].strip() if len(line) > 3 else line.strip())

    head_res = _git_run(git_dir, ["rev-parse", "--short=12", "HEAD"])
    head_sha = head_res.stdout.strip() if head_res and head_res.returncode == 0 else None

    ahead_trunk = 0
    behind_trunk = 0
    lr = _git_run(git_dir, ["rev-list", "--left-right", "--count", f"{trunk}...HEAD"])
    if lr and lr.returncode == 0:
        parts = lr.stdout.strip().split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            behind_trunk = int(parts[0])
            ahead_trunk = int(parts[1])

    unpushed = 0
    up = _git_run(git_dir, ["rev-list", "--count", "@{u}..HEAD"])
    if up and up.returncode == 0 and up.stdout.strip().isdigit():
        unpushed = int(up.stdout.strip())
    else:
        # No upstream (a branch made from a local base, never pushed): `@{u}` does not resolve, and reading that as 0
        # called a never-pushed commit safe (uxxx on #482, 8e470f). Count the commits no remote branch holds instead.
        nr = _git_run(git_dir, ["rev-list", "--count", "HEAD", "--not", "--remotes"])
        if nr and nr.returncode == 0 and nr.stdout.strip().isdigit():
            unpushed = int(nr.stdout.strip())

    return {
        "path": git_dir.as_posix(),
        "name": git_dir.name,
        "branch": branch,
        "head_sha": head_sha,
        "dirty_count": len(dirty_files),
        "dirty_files": dirty_files[:20],
        "ahead_of_trunk": ahead_trunk,
        "behind_trunk": behind_trunk,
        "unpushed_commits": unpushed,
        "actionable": bool(len(dirty_files) > 0 or ahead_trunk > 0 or unpushed > 0),
    }


def inspect_worktrees(
    *,
    repos: list[Path] | None = None,
    worktree_roots: list[Path] | None = None,
    trunk: str = "origin/main",
) -> dict[str, Any]:
    """Source 3: Scan local git checkouts and worktrees for dirty files or commits ahead of trunk."""
    candidates: list[Path] = []
    seen_paths: set[str] = set()

    def _add_candidate(p: Path) -> None:
        try:
            norm = os.path.normcase(str(p.resolve()))
        except OSError:
            norm = os.path.normcase(str(p))
        if norm not in seen_paths:
            seen_paths.add(norm)
            candidates.append(p)

    for r in repos or []:
        if r and r.is_dir():
            _add_candidate(r)

    for root in worktree_roots or []:
        if not root or not root.is_dir():
            continue
        if (root / ".git").exists():
            _add_candidate(root)
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and (child / ".git").exists():
                _add_candidate(child)

    items: list[dict[str, Any]] = []
    for cand in candidates:
        info = _inspect_single_git_dir(cand, trunk=trunk)
        if info is not None:
            items.append(info)

    actionable = [w for w in items if w.get("actionable")]
    return {
        "scanned_count": len(items),
        "actionable_count": len(actionable),
        "worktrees": items,
        "actionable": actionable,
    }


def inspect_handovers_and_gates(
    *,
    handover_dir: Path | None = None,
    scratchpad_dirs: list[Path] | None = None,
    seat: str | None = None,
    stale_hours: float = DEFAULT_STALE_HANDOVER_HOURS,
    now: float | None = None,
) -> dict[str, Any]:
    """Source 4: Check `~/.dxxxxx/HANDOVER-*.md` staleness and scan `gate-runner.log` / `verdict.txt`."""
    now_ts = time.time() if now is None else now
    handovers: list[dict[str, Any]] = []
    latest_by_seat: dict[str, dict[str, Any]] = {}

    if handover_dir and handover_dir.is_dir():
        try:
            files = sorted(handover_dir.glob("HANDOVER-*.md"), key=lambda p: p.name)
        except OSError:
            files = []
        for fpath in files:
            if not fpath.is_file():
                continue
            try:
                mtime = fpath.stat().st_mtime
            except OSError:
                continue
            age_h = max(0.0, round((now_ts - mtime) / 3600.0, 2))
            m = _HANDOVER_SEAT_RE.match(fpath.name)
            inferred_seat = m.group("seat").lower() if m else fpath.stem.replace("HANDOVER-", "").lower()
            entry = {
                "file": fpath.name,
                "path": fpath.as_posix(),
                "seat": inferred_seat,
                "mtime_iso": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "age_hours": age_h,
                "stale": age_h >= stale_hours,
            }
            handovers.append(entry)
            prev = latest_by_seat.get(inferred_seat)
            if prev is None or mtime > _epoch(prev["mtime_iso"] or "") or age_h < prev["age_hours"]:
                latest_by_seat[inferred_seat] = entry

    seat_lower = seat.strip().lower() if isinstance(seat, str) and seat.strip() else None
    own_handover: dict[str, Any] | None = None
    if seat_lower:
        for key_candidate in (seat_lower, seat_lower.replace("-session", ""), seat_lower.replace("memory-", "")):
            if key_candidate in latest_by_seat:
                own_handover = latest_by_seat[key_candidate]
                break

    stale_latest = [
        v for v in sorted(latest_by_seat.values(), key=lambda x: x["seat"])
        if v["stale"] and (seat_lower is None or v["seat"] in (seat_lower, seat_lower.replace("-session", ""), seat_lower.replace("memory-", "")))
    ]

    gate_reports: list[dict[str, Any]] = []
    seen_gate_files: set[str] = set()

    def _scan_gate_file(gpath: Path) -> None:
        try:
            norm = os.path.normcase(str(gpath.resolve()))
        except OSError:
            norm = os.path.normcase(str(gpath))
        if norm in seen_gate_files or not gpath.is_file():
            return
        seen_gate_files.add(norm)
        try:
            text = gpath.read_text(encoding="utf-8", errors="replace")
            mtime = gpath.stat().st_mtime
        except OSError:
            return
        verdict = None
        last_line = ""
        for raw_line in reversed(text.splitlines()):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if not last_line:
                last_line = stripped[:200]
            if _GATE_REFUSED_RE.search(stripped):
                verdict = "LOCAL GATE REFUSED"
                last_line = stripped[:200]
                break
            if _GATE_PASS_RE.search(stripped):
                verdict = "LOCAL GATE PASS"
                last_line = stripped[:200]
                break
        if verdict:
            gate_reports.append({
                "file": gpath.name,
                "path": gpath.as_posix(),
                "verdict": verdict,
                "line": last_line,
                "age_hours": max(0.0, round((now_ts - mtime) / 3600.0, 2)),
            })

    for sdir in scratchpad_dirs or []:
        if not sdir or not sdir.is_dir():
            continue
        for target_name in ("gate-runner.log", "verdict.txt"):
            direct = sdir / target_name
            if direct.is_file():
                _scan_gate_file(direct)
            try:
                for sub in sdir.rglob(target_name):
                    if sub.is_file():
                        _scan_gate_file(sub)
            except OSError:
                continue

    return {
        "handover_dir": handover_dir.as_posix() if handover_dir else None,
        "handovers_found": len(handovers),
        "own_handover": own_handover,
        "stale_handovers": stale_latest,
        "gate_reports": gate_reports,
    }


def _find_git_bash() -> str | None:
    if os.name != "nt":
        return "bash"
    candidates = [
        os.environ.get("BRAIN_TEST_BASH"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return None


def _discover_brain_helper_scripts(home_dir: Path | None = None) -> dict[str, Path]:
    home = home_dir or Path.home()
    out: dict[str, Path] = {}
    installed_json = home / ".claude" / "plugins" / "installed_plugins.json"
    plugin_map = {
        "staff": "example-brain@example",
        "elevated": "example-brain-elevated@example",
    }
    if installed_json.is_file():
        try:
            data = json.loads(installed_json.read_text(encoding="utf-8-sig"))
            plugins = data.get("plugins") if isinstance(data, dict) else {}
        except (OSError, ValueError):
            plugins = {}
        if isinstance(plugins, dict):
            for tier, key in plugin_map.items():
                rows = plugins.get(key)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    install_path = row.get("installPath")
                    if isinstance(install_path, str) and install_path:
                        candidate = Path(install_path) / "scripts" / "cf-access-headers.sh"
                        if candidate.is_file():
                            out[tier] = candidate
                            break

    cache_root = home / ".claude" / "plugins" / "cache" / "the company"
    for tier, folder in (("staff", "example-brain"), ("elevated", "example-brain-elevated")):
        if tier in out:
            continue
        tier_dir = cache_root / folder
        if not tier_dir.is_dir():
            continue
        try:
            subdirs = sorted(
                [d for d in tier_dir.iterdir() if d.is_dir()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            subdirs = []
        for sd in subdirs:
            cand = sd / "scripts" / "cf-access-headers.sh"
            if cand.is_file():
                out[tier] = cand
                break
    return out


_EMAIL_TOKEN_RE = re.compile(r"\S+@\S+")


def _redact_brain_identity(raw_ident: str) -> str:
    """Redact any email token in `cf-access-headers.sh --identity` output so transcripts
    and decision cards never capture operator or client email addresses."""
    if not isinstance(raw_ident, str) or not raw_ident:
        return "none"
    if raw_ident.startswith("cloudflared ") and "@" in raw_ident:
        return "cloudflared (signed in)"
    return _EMAIL_TOKEN_RE.sub("<redacted-email>", raw_ident)


def clean_brain_env(env: dict[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Return `(cleaned_env, refused_keys_present)` with all `REFUSED_ENVIRONMENT` variables stripped."""
    source = dict(os.environ if env is None else env)
    refused_present = sorted(
        k for k, v in source.items()
        if REFUSED_ENV_RE.match(k) and v is not None and v != ""
    )
    cleaned = {k: v for k, v in source.items() if not REFUSED_ENV_RE.match(k)}
    return cleaned, refused_present


def inspect_brain_and_env(
    *,
    env: dict[str, str] | None = None,
    probe_identity: bool = True,
    staff_script: Path | None = None,
    elevated_script: Path | None = None,
    home_dir: Path | None = None,
    bash_bin: str | None = None,
) -> dict[str, Any]:
    """Source 5: Inspect `REFUSED_ENVIRONMENT` variables (`OPENSSL_CONF`, etc.) and Brain sign-in."""
    cleaned_env, refused_present = clean_brain_env(env)
    unset_hint = None
    if refused_present:
        unset_vars_sh = " ".join(f"-u {name}" for name in refused_present)
        unset_vars_ps = "; ".join(f"Remove-Item Env:{name} -ErrorAction SilentlyContinue" for name in refused_present)
        unset_hint = (
            f"Brain CLI tools (scripts/brain-os.mjs / scripts/codex-brain-bridge.mjs; subcommands `overview`, `ops actions --limit 5`) "
            f"refuse to start (rc=5) when {', '.join(refused_present)} is set. Strip with `env {unset_vars_sh} node ...` in bash "
            f"or `{unset_vars_ps}` in PowerShell."
        )

    discovered = _discover_brain_helper_scripts(home_dir)
    scripts = {
        "staff": staff_script or discovered.get("staff"),
        "elevated": elevated_script or discovered.get("elevated"),
    }

    tiers: dict[str, dict[str, Any]] = {}
    bash_exe = bash_bin or _find_git_bash()
    for tier in ("staff", "elevated"):
        script_path = scripts.get(tier)
        if script_path is None or not Path(script_path).is_file():
            tiers[tier] = {
                "installed": False,
                "script": str(script_path) if script_path else None,
                "brain_os_script": None,
                "subcommands": ["overview", "ops actions --limit 5"],
                "identity": None,
                "active": False,
            }
            continue
        s_path = Path(script_path)
        brain_os_cand = s_path.parent / "brain-os.mjs"
        brain_os_str = brain_os_cand.as_posix() if brain_os_cand.is_file() else None
        if not probe_identity or not bash_exe:
            tiers[tier] = {
                "installed": True,
                "script": s_path.as_posix(),
                "brain_os_script": brain_os_str,
                "subcommands": ["overview", "ops actions --limit 5"],
                "identity": "not_probed",
                "active": None,
            }
            continue
        try:
            proc = subprocess.run(
                [bash_exe, s_path.as_posix(), "--identity"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=cleaned_env,
                timeout=SUBPROCESS_TIMEOUT_S,
                creationflags=_no_window_flags(),
            )
            ident_out = _redact_brain_identity((proc.stdout or "").strip() or "none")
        except (OSError, subprocess.SubprocessError) as exc:
            ident_out = f"error: {type(exc).__name__}"
        active = bool(ident_out and ident_out != "none" and not ident_out.startswith("error:"))
        tiers[tier] = {
            "installed": True,
            "script": s_path.as_posix(),
            "brain_os_script": brain_os_str,
            "subcommands": ["overview", "ops actions --limit 5"],
            "identity": ident_out,
            "active": active,
        }

    return {
        "refused_env_present": refused_present,
        "env_clean": len(refused_present) == 0,
        "remediation": unset_hint,
        "staff": tiers["staff"],
        "elevated": tiers["elevated"],
    }


# Positive policy list, not an inference from a nonempty label or a dynamically discovered fleet roster.
# New readers need an explicit reviewed entry; Codex runtime markers still override a listed label.
CLAUDE_PR_READER_SEATS = frozenset({
    "dxxxxx", "exxxx-session", "kxxx", "nxxxxx", "memory-bxxxxx", "oxxxxxx", "pxx", "txxxx", "uxxx", "vxxxxxxxxxx",
})


def inspect_pr_reviews(repos: list[Path] | None, *, offline: bool = False, seat: str | None = None,
                       env: dict[str, str] | None = None) -> dict[str, Any]:
    """Read open PRs for the authenticated GitHub account in explicitly configured repositories.

    Resolve canonical metadata before PR content (an alias is not an authorization boundary).
    No fetch, checkout, review, comment, or authentication prompt is performed.
    """
    result: dict[str, Any] = {"available": False, "mode": "offline" if offline else "unconfigured",
                              "prs": [], "errors": [], "scope": "authenticated GitHub account; not seat ownership"}
    if offline or not repos:
        return result
    seen = set()
    source_env = os.environ if env is None else env
    gh_env = dict(source_env, GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0")
    # This cooperative seat policy is separate from board-key access: an approved Codex board reader still
    # has no standing authorization for this org. A claimed Claude label cannot override a Codex runtime.
    restricted_org = (not seat or seat.lower() not in CLAUDE_PR_READER_SEATS
                      or any(k in source_env for k in board_tasks._CODEX_ENV))

    def query(cwd: Path, args: list[str]):
        completed = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", env=gh_env,
                                   timeout=SUBPROCESS_TIMEOUT_S, creationflags=_no_window_flags())
        if completed.returncode:
            raise ValueError("GitHub read failed (exit %d)" % completed.returncode)
        return json.loads(completed.stdout)

    for repo in repos[:10]:
        source = str(repo.resolve())
        reason = "repository metadata unavailable"
        try:
            metadata = query(repo, ["repo", "view", "--json", "nameWithOwner"])
            canonical = metadata.get("nameWithOwner") if isinstance(metadata, dict) else None
            if not isinstance(canonical, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", canonical):
                raise ValueError("canonical repository identity missing")
            source = canonical
            # Apply the Codex/unknown-seat restriction after alias resolution, without imposing it on Claude.
            if restricted_org and canonical.split("/", 1)[0].lower() == "acmeone":
                reason = "repository authorization required"
                raise ValueError("repository requires separate explicit authorization")
            if canonical.lower() in seen:
                continue
            seen.add(canonical.lower())
            reason = "PR list unavailable or malformed"
            rows = query(repo, ["pr", "list", "--repo", canonical, "--author", "@me", "--state", "open",
                                "--limit", "100", "--json", "number,url,headRefOid,reviewDecision,isDraft"])
            if not isinstance(rows, list):
                raise ValueError("PR list is not an array")
            for row in rows:
                if (not isinstance(row, dict) or type(row.get("number")) is not int
                        or row["number"] <= 0 or not isinstance(row.get("url"), str)
                        or not isinstance(row.get("headRefOid"), str)
                        or row.get("reviewDecision") not in (None, "", "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED")):
                    raise ValueError("PR review row is malformed")
                result["prs"].append({"repo": canonical, "number": row["number"], "url": row["url"],
                                      "head": row["headRefOid"],
                                      "review_state": (row.get("reviewDecision") or "no decision") + ", threads unknown",
                                      "review_decision": row.get("reviewDecision") or None,
                                      "open_threads": None, "latest_review_commit": None,
                                      "latest_review_matches_head": None,
                                      "draft": row.get("isDraft") is True})
            result["available"] = True
            reason = "PR review threads unavailable or incomplete"
            if rows:
                owner, name = canonical.split("/")
                # One bounded batch per repository, not one subprocess per PR. Numbers and canonical
                # identity are validated above. GraphQL variables keep names out of query syntax.
                fields = " ".join("pr%d:pullRequest(number:%d){reviewThreads(first:100){nodes{isResolved isOutdated} "
                                  "pageInfo{hasNextPage}} reviews(last:1){nodes{state commit{oid}}}}" %
                                  (r["number"], r["number"]) for r in rows)
                graph = query(repo, ["api", "graphql", "-f", "query=query($owner:String!,$name:String!){"
                                    "repository(owner:$owner,name:$name){" + fields + "}}",
                                    "-f", "owner=" + owner, "-f", "name=" + name])
                if not isinstance(graph, dict) or graph.get("errors"):
                    raise ValueError("review query failed")
                repository = graph.get("data", {}).get("repository")
                if not isinstance(repository, dict):
                    raise ValueError("review repository missing")
                for item in [p for p in result["prs"] if p["repo"] == canonical]:
                    try:
                        details = repository["pr%d" % item["number"]]
                        threads, reviews = details["reviewThreads"], details["reviews"]["nodes"]
                        nodes, more = threads["nodes"], threads["pageInfo"]["hasNextPage"]
                        if (not isinstance(nodes, list) or type(more) is not bool or more
                                or not isinstance(reviews, list) or len(reviews) > 1
                                or any(not isinstance(t, dict) or type(t.get("isResolved")) is not bool
                                       or type(t.get("isOutdated")) is not bool for t in nodes)):
                            raise ValueError("incomplete review threads")
                        latest = reviews[0]["commit"]["oid"] if reviews else None
                        if latest is not None and (not isinstance(latest, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", latest)):
                            raise ValueError("malformed review commit")
                        count = sum(not t["isResolved"] and not t["isOutdated"] for t in nodes)
                        item.update(open_threads=count, latest_review_commit=latest,
                                    latest_review_matches_head=(latest == item["head"]) if latest else None,
                                    review_state=(item["review_decision"] or "no decision") + ", %d open threads" % count)
                    except (KeyError, TypeError, AttributeError, ValueError):
                        result["errors"].append("%s#%d: %s" % (canonical, item["number"], reason))
            if len(rows) >= 100:
                result["errors"].append("%s: PR list reached its 100-row cap" % canonical)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # Do not copy stderr, credentials, or repository exception text into the report.
            result["errors"].append("%s: %s (%s)" % (source, reason, type(exc).__name__))
    if len(repos) > 10:
        result["errors"].append("repository scan capped at 10 configured paths")
    result["mode"] = "partial" if result["errors"] else "live"
    result["prs"].sort(key=lambda r: (r["repo"].lower(), r["number"]))
    return result


class BoardTaskShapeError(ValueError):
    """A task read cannot be classified safely (never includes returned board text)."""


class QueueDocumentTooLarge(ValueError):
    """The ranked queue exceeds the orientation parser's UTF-8 byte limit."""


class _OrientBoardReader:
    """Only task read tools and the one ranked-queue GET may reach the shared transport."""

    def __init__(self, board):
        self._board = board

    @staticmethod
    def _validate_tasks(tool: str, value):
        if tool not in board_tasks.READ_TOOLS:
            return value
        if tool == "planboardListIssues":
            if not isinstance(value, list):
                raise BoardTaskShapeError("task listing must be an array")
            rows = value
        else:
            rows = [value]
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not row["id"].strip()):
                raise BoardTaskShapeError("task rows require a nonempty string id")
        return value

    def call(self, tool: str, args: dict):
        if tool not in board_tasks.READ_TOOLS and not (
                tool == "planboardApiRequest" and args == {
                    "method": "GET", "path": "/issues/TKT-998/documents/pull-queue-g12"}):
            raise board_tasks.BoardUnavailable("orientation permits only task reads and the ranked-queue GET")
        return self._validate_tasks(tool, self._board.call(tool, args))

    def call_paged(self, tool: str, args: dict):
        if tool not in board_tasks.READ_TOOLS:
            raise board_tasks.BoardUnavailable("orientation paging permits only task reads")
        value, note = self._board.call_paged(tool, args)
        return self._validate_tasks(tool, value), note

    def close(self):
        # run_tasks closes its reader; inspect_board_work owns the shared transport's final close.
        pass


# Plugin transports and malformed JSON pages must not abort the other orientation sources.
_BOARD_READ_FAILURES = (board_tasks.BoardUnavailable, OSError, ValueError, LookupError,
                        TypeError, AttributeError, RuntimeError, subprocess.SubprocessError,
                        AssertionError, StopIteration)


def inspect_board_work(seat: str | None, state_file: Path | None, *, offline: bool = False) -> dict[str, Any]:
    """Read tasks and the ranked pull queue, without cursors, expiry writes, or claims."""
    result: dict[str, Any] = {"available": False, "mode": "offline" if offline else "unconfigured",
                              "tasks": {}, "queue_rows": [], "queue_revision": None, "errors": [],
                              "timing_scope": "per-call transport timeouts; no whole-run deadline or total read budget",
                              "queue_max_bytes": MAX_QUEUE_DOCUMENT_BYTES,
                              "queue_scope": "FREE-owner rows only; read the queue document for named reservations"}
    if offline or not seat or state_file is None:
        return result
    why = board_tasks.codex_refusal(seat, state_file=state_file)
    if why:
        result.update(mode="error", errors=["board access refused for this identity"])
        return result
    board = None
    try:
        board = board_tasks.McpBoard(board_tasks.plugin_root(), tools=board_tasks.READ_TOOLS | board_tasks.CLAIM_TOOLS)
        reader = _OrientBoardReader(board)
        try:
            tasks = board_tasks.run_tasks(seat, None, state_file=state_file, board_factory=lambda: reader,
                                          roster=board_tasks.fleet_seats_roster(), cursor_path=None, update_cursor=False)
            result["tasks"] = tasks
            result["available"] = True
            if not board_tasks.scan_complete(tasks):
                result["errors"].append("board tasks read is capped or truncated")
        except _BOARD_READ_FAILURES as exc:
            result["errors"].append("board tasks unavailable (%s)" % type(exc).__name__)
        try:
            doc = reader.call("planboardApiRequest", {"method": "GET",
                             "path": "/issues/TKT-998/documents/pull-queue-g12"})
            if (not isinstance(doc, dict) or not isinstance(doc.get("body"), str)
                    or not doc.get("latestRevisionId") or "error" in doc):
                raise ValueError("ranked pull queue unavailable")
            # Bound parser work, including multibyte text. The transport's separate line limit
            # still applies before this point; this is not a response-download memory bound.
            if (len(doc["body"]) > MAX_QUEUE_DOCUMENT_BYTES
                    or len(doc["body"].encode("utf-8")) > MAX_QUEUE_DOCUMENT_BYTES):
                raise QueueDocumentTooLarge()
            parsed = board_queue.queue_rows(doc["body"])
            result["errors"].extend("ranked pull queue: " + reason for reason in board_queue.partial_reasons(parsed))
            # The shared parser judges whole owner/state cells and enclosing hold headings. These
            # are potential FREE rows, not seat eligibility or permission to bypass a current claim.
            result["queue_rows"] = ["%s | %s | %s | %s" % (r["row"], r["owner"], r["state"], r["slice"])
                                    for r in parsed["rows"] if r["owner_open"] and r["state_open"] and not r["waits"]]
            result["queue_revision"] = doc["latestRevisionId"]
            result["available"] = True
        except _BOARD_READ_FAILURES as exc:
            result["errors"].append("ranked pull queue unavailable (%s)" % type(exc).__name__)
    except _BOARD_READ_FAILURES as exc:
        result["errors"].append("board plugin unavailable (%s)" % type(exc).__name__)
    finally:
        if board is not None:
            try:
                board.close()
            except _BOARD_READ_FAILURES as exc:
                result["errors"].append("board transport close failed (%s)" % type(exc).__name__)
    result["mode"] = "partial" if result["errors"] else "live"
    return result


def synthesize_candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a deterministic, priority-ranked list of concrete next-step options (`candidate_options`)
    suitable for feeding directly into `AskUserQuestion` / `ask_question`.
    """
    options: list[dict[str, Any]] = []

    # Priority 1: Incomplete mesh graph or reply-owed messages on the MeshyChat graph
    graph = report.get("mesh_graph") if isinstance(report.get("mesh_graph"), dict) else {}
    if graph and (graph.get("available") is not True or graph.get("error") or graph.get("mode") == "partial"):
        options.append({
            "priority": 1,
            "source": "mesh_graph.incomplete",
            "label": "Reconcile work against an incomplete mesh graph",
            "description": "Remote obligations are unknown or only partly read. Preserve known open work, "
                           "consult local continuity and authenticated handled records, and do independent work. "
                           "Do not infer an empty queue, repeat sends, or start a second listener. "
                           + str(graph.get("error") or graph.get("mode") or "unavailable"),
        })
    reply_owed = graph.get("reply_owed") if isinstance(graph.get("reply_owed"), list) else []
    if reply_owed:
        top_asks = reply_owed[:3]
        desc_parts = [
            f"{item.get('key')} from {item.get('author') or '?'} ({item.get('thread_id') or '?'})"
            for item in top_asks
        ]
        options.append({
            "priority": 1,
            "source": "mesh_graph.reply_owed",
            "label": f"Answer {len(reply_owed)} reply-owed mesh ask(s)",
            "description": f"Read and answer open/active asks: {', '.join(desc_parts)}",
            "keys": [item.get("key") for item in reply_owed if item.get("key")],
        })

    # Priority 2: Banked `cc` messages held in postbox spool (#199) or uncollected spool bytes
    spool = report.get("spool") if isinstance(report.get("spool"), dict) else {}
    held_cc = spool.get("held_cc") if isinstance(spool.get("held_cc"), list) else []
    if held_cc:
        top_cc = held_cc[:3]
        cc_parts = [f"{item.get('key')} from {item.get('from') or '?'}" for item in top_cc]
        options.append({
            "priority": 2,
            "source": "spool.held_cc",
            "label": f"Inspect {len(held_cc)} banked cc message(s) in postbox spool",
            "description": f"Read held cc updates (#199): {', '.join(cc_parts)}",
            "keys": [item.get("key") for item in held_cc if item.get("key")],
        })
    uncollected = spool.get("uncollected") if isinstance(spool.get("uncollected"), list) else []
    if uncollected:
        ident_list = ", ".join(
            f"{u.get('identity')}:{u.get('uncollected_bytes')}B" for u in uncollected[:3]
        )
        options.append({
            "priority": 2,
            "source": "spool.uncollected",
            "label": f"Drain uncollected postbox spool bytes ({len(uncollected)} consumer(s))",
            "description": f"Uncollected spool tail awaiting collection: {ident_list}",
        })

    # Priority 3: Unannounced gate verdicts (`LOCAL GATE REFUSED` / `LOCAL GATE PASS`)
    hg = report.get("handovers_and_gates") if isinstance(report.get("handovers_and_gates"), dict) else {}
    gate_reports = hg.get("gate_reports") if isinstance(hg.get("gate_reports"), list) else []
    for gr in gate_reports:
        verdict = gr.get("verdict")
        options.append({
            "priority": 3,
            "source": "handovers_and_gates.gate_reports",
            "label": f"Handle {verdict} in {gr.get('file')}",
            "description": f"{gr.get('path')}: {gr.get('line')}",
        })

    # Priority 4: Dirty or ahead git worktrees
    wt_sec = report.get("worktrees") if isinstance(report.get("worktrees"), dict) else {}
    actionable_wts = wt_sec.get("actionable") if isinstance(wt_sec.get("actionable"), list) else []
    if actionable_wts:
        wt_summaries = [
            f"{w.get('name')} ({w.get('branch')}: {w.get('dirty_count')} dirty, +{w.get('ahead_of_trunk')} ahead)"
            for w in actionable_wts[:3]
        ]
        options.append({
            "priority": 4,
            "source": "worktrees.actionable",
            "label": f"Finish or clean {len(actionable_wts)} active git worktree(s)",
            "description": "; ".join(wt_summaries),
        })

    # Priority 5: Threads with news or answered-still-open asks
    threads_news = graph.get("threads_with_news") if isinstance(graph.get("threads_with_news"), list) else []
    if threads_news:
        t_parts = [
            f"{t.get('thread_id')} ({t.get('new')} new, last {t.get('last_author') or '?'})"
            for t in threads_news[:3]
        ]
        options.append({
            "priority": 5,
            "source": "mesh_graph.threads_with_news",
            "label": f"Catch up on {len(threads_news)} mesh thread(s) with news",
            "description": "; ".join(t_parts),
        })

    answered_open = graph.get("answered_still_open") if isinstance(graph.get("answered_still_open"), list) else []
    if answered_open:
        a_parts = [f"{a.get('key')} ({a.get('thread_id') or '?'})" for a in answered_open[:3]]
        options.append({
            "priority": 5,
            "source": "mesh_graph.answered_still_open",
            "label": f"Settle {len(answered_open)} answered-still-open ask(s) with --closes",
            "description": f"Verify completion and close: {', '.join(a_parts)}",
            "keys": [a.get("key") for a in answered_open if a.get("key")],
        })

    help_wanted = graph.get("help_wanted") if isinstance(graph.get("help_wanted"), list) else []
    if help_wanted:
        hw_parts = [
            f"{h.get('key')} from {h.get('author') or '?'} ({h.get('thread_id') or '?'})"
            for h in help_wanted[:3]
        ]
        options.append({
            "priority": 5,
            "source": "mesh_graph.help_wanted",
            "label": f"Answer {len(help_wanted)} broadcast help-wanted ask(s)",
            "description": f"Unanswered broadcast asks needing help: {', '.join(hw_parts)}",
            "keys": [h.get("key") for h in help_wanted if h.get("key")],
        })

    awaiting = graph.get("awaiting") if isinstance(graph.get("awaiting"), list) else []
    if awaiting:
        dependencies = ", ".join(
            f"{item.get('key') or '?'} awaiting {item.get('owing_label') or '?'}"
            for item in awaiting[:3]
            if isinstance(item, dict)
        )
        options.append({
            "priority": 5,
            "source": "mesh_graph.awaiting",
            "label": f"Preserve {len(awaiting)} outstanding peer dependency(s)",
            "description": f"{dependencies}. Check the recorded owner, deadline and completion evidence in local continuity. "
                           "Do not resend unchanged requests or treat delivery as acceptance. "
                           "Continue independent authorized work while waiting.",
            "keys": [item.get("key") for item in awaiting if isinstance(item, dict) and item.get("key")],
        })

    # Priority 6: Brain environment hygiene or lapsed sign-in
    brain = report.get("brain") if isinstance(report.get("brain"), dict) else {}
    refused_env = brain.get("refused_env_present") if isinstance(brain.get("refused_env_present"), list) else []
    if refused_env:
        options.append({
            "priority": 6,
            "source": "brain.refused_env",
            "label": f"Strip refused Brain env var(s): {', '.join(refused_env)}",
            "description": brain.get("remediation") or "Unset refused environment variables before invoking Brain CLI.",
        })
    for tier in ("staff", "elevated"):
        t_info = brain.get(tier) if isinstance(brain.get(tier), dict) else {}
        if t_info.get("installed") and t_info.get("active") is False:
            options.append({
                "priority": 6,
                "source": f"brain.{tier}",
                "label": f"Reconnect lapsed {tier} Brain sign-in",
                "description": f"{tier} cf-access-headers.sh --identity returned {t_info.get('identity')!r}",
            })

    # Priority 7: Stale handover file refresh
    stale_handovers = hg.get("stale_handovers") if isinstance(hg.get("stale_handovers"), list) else []
    if stale_handovers:
        h_parts = [f"{h.get('file')} ({h.get('age_hours')}h old)" for h in stale_handovers[:3]]
        options.append({
            "priority": 7,
            "source": "handovers_and_gates.stale_handovers",
            "label": f"Refresh {len(stale_handovers)} stale HANDOVER file(s)",
            "description": "; ".join(h_parts),
        })

    for key, label in (("github_prs", "GitHub PR review states"), ("board_work", "board tasks and pull queue")):
        source = report.get(key)
        if not isinstance(source, dict):
            continue
        if source.get("mode") in ("partial", "error"):
            options.append({"priority": 3, "source": key + ".incomplete", "label": "Check unavailable " + label,
                            "description": "This source is unknown or incomplete, not empty: " +
                                           str(source.get("errors") or source.get("mode"))})
        if key == "github_prs" and source.get("prs"):
            options.append({"priority": 3, "source": key, "label": "Check open PRs and their review states",
                            "description": "; ".join("%s#%s %s%s" % (p["repo"], p["number"], p["review_state"],
                                                " (draft)" if p["draft"] else "") for p in source["prs"]) +
                                           ". Account-owned PRs; confirm this seat's responsibility. No merge is authorized."})
        if key == "board_work":
            tasks = source.get("tasks") or {}
            owned = tasks.get("owned") or []
            requested = tasks.get("requested") or []
            if owned or requested:
                options.append({"priority": 3, "source": key + ".tasks", "label": "Check owned/requested board work",
                                "description": "%d owned, %d requested; read blockers and existing claims before work."
                                               % (len(owned), len(requested))})
            if source.get("queue_rows"):
                options.append({"priority": 4, "source": key + ".queue", "label": "Read ranked pull-queue candidates",
                                "description": "Queue revision %s: %d potential FREE rows. Read named reservations in the "
                                               "queue document. Check lane, dependencies and "
                                               "current claim; take a claim before starting. A row is not authorization."
                                               % (source.get("queue_revision"), len(source["queue_rows"]))})
    options.sort(key=lambda item: item["priority"])
    return options


def _inspect_actionable_review_preflight(state_file: Path, base_url: str, actionable_wts: list[dict[str, Any]], max_paths: int = 20) -> dict[str, Any] | None:
    changed, diff_errors = [], []
    add = lambda p: changed.append(p) if p and p not in changed and len(changed) < max_paths else None
    for w in (x for x in actionable_wts if isinstance(x, dict)):
        if (w.get("ahead_of_trunk") or 0) > 0 and isinstance(wt_path := w.get("path"), str) and wt_path:
            try:
                for ln in preflight._collect_git_diff_paths(Path(wt_path), "origin/main", "HEAD"):
                    add(ln)
            except (OSError, subprocess.SubprocessError) as exc:
                diff_errors.append(f"{type(exc).__name__}: {exc}")
        for raw_df in w.get("dirty_files") or []:
            if isinstance(raw_df, str):
                add(raw_df.split(" -> ")[-1].strip().strip('"'))
        if len(changed) >= max_paths:
            break
    if diff_errors:
        return {"available": False, "ok": False, "paths": changed, "decision_cards": [], "fix_first_messages": [], "match_count": 0, "error": "; ".join(diff_errors)}
    if not changed:
        return None
    try:
        return preflight.check_review_preflight(state_file, node.load_state(state_file, node.normalize_base_url(base_url)), paths=changed)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "paths": changed, "decision_cards": [], "fix_first_messages": [], "match_count": 0, "error": f"{type(exc).__name__}: {exc}"}


def run_orient(
    *,
    state_file: Path | None = None,
    base_url: str | None = None,
    identities: list[str] | None = None,
    seat: str | None = None,
    spool_dir: Path | None = None,
    repos: list[Path] | None = None,
    worktree_roots: list[Path] | None = None,
    handover_dir: Path | None = None,
    scratchpad_dirs: list[Path] | None = None,
    since: str = "14d",
    stale_handover_hours: float = DEFAULT_STALE_HANDOVER_HOURS,
    offline: bool = False,
    probe_brain_identity: bool = True,
    include_review_preflight: bool = False,
    env: dict[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Run all 5 orientation checks deterministically and synthesize `candidate_options`."""
    home = Path.home()
    resolved_spool = spool_dir if spool_dir is not None else (home / ".meshychat" / "postbox" / "spool")
    resolved_handover = handover_dir if handover_dir is not None else (home / ".dxxxxx")

    spool_report = inspect_spool(
        resolved_spool,
        identities=identities,
        now=now,
    )
    dt_now = (
        (lambda: datetime.fromtimestamp(now, tz=timezone.utc))
        if now is not None
        else None
    )
    graph_report = inspect_mesh_graph(
        state_file,
        base_url,
        since=since,
        offline=offline,
        now=dt_now,
    )
    inferred_seat = seat or graph_report.get("seat")
    if not seat and isinstance(inferred_seat, str):
        inferred_seat = inferred_seat.removesuffix("-prod")
    if not inferred_seat and identities:
        inferred_seat = identities[0].removesuffix("-prod")

    worktree_report = inspect_worktrees(
        repos=repos,
        worktree_roots=worktree_roots,
    )
    handover_report = inspect_handovers_and_gates(
        handover_dir=resolved_handover,
        scratchpad_dirs=scratchpad_dirs,
        seat=inferred_seat,
        stale_hours=stale_handover_hours,
        now=now,
    )
    brain_report = inspect_brain_and_env(
        env=env,
        probe_identity=probe_brain_identity,
    )

    report: dict[str, Any] = {
        "seat": inferred_seat,
        "identities": identities or [],
        "spool": spool_report,
        "mesh_graph": graph_report,
        "worktrees": worktree_report,
        "handovers_and_gates": handover_report,
        "brain": brain_report,
        "github_prs": inspect_pr_reviews(repos, offline=offline, seat=inferred_seat, env=env),
        "board_work": inspect_board_work(inferred_seat, state_file, offline=offline),
    }
    if include_review_preflight and not offline and state_file is not None and base_url:
        if (rev_pf := _inspect_actionable_review_preflight(state_file, base_url, worktree_report.get("actionable") or [])) is not None:
            report["review_preflight"] = rev_pf
    report["candidate_options"] = synthesize_candidates(report)
    return report


def format_orient_text(report: dict[str, Any]) -> str:
    """Render a concise human-readable 5-source orientation summary and ranked candidate options."""
    lines: list[str] = []
    seat = report.get("seat") or "unknown"
    idents = ", ".join(report.get("identities") or []) or "all"
    lines.append(f"=== MeshyChat Pre-Idle Orientation (seat: {seat}, identities: {idents}) ===")

    # 1. Spool
    spool = report.get("spool") or {}
    lines.append(
        f"[1/5] Postbox Spool: held_cc={spool.get('held_cc_count', 0)}, "
        f"uncollected_consumers={len(spool.get('uncollected') or [])}, "
        f"recent_tail_events={len(spool.get('recent_tail_messages') or [])}"
    )
    for item in (spool.get("held_cc") or [])[:5]:
        lines.append(
            f"  - HELD CC: {item.get('key')} from {item.get('from') or '?'} "
            f"(ident={item.get('identity')}, age={item.get('age_s')}s)"
        )
    for u in (spool.get("uncollected") or [])[:5]:
        lines.append(
            f"  - UNCOLLECTED: {u.get('identity')} ({u.get('consumer')}): {u.get('uncollected_bytes')} bytes"
        )

    # 2. Mesh Graph
    graph = report.get("mesh_graph") or {}
    if graph.get("available"):
        ro_list = graph.get("reply_owed") if isinstance(graph.get("reply_owed"), list) else []
        ans_list = graph.get("answered_still_open") if isinstance(graph.get("answered_still_open"), list) else []
        aw_list = graph.get("awaiting") if isinstance(graph.get("awaiting"), list) else []
        th_list = graph.get("threads_with_news") if isinstance(graph.get("threads_with_news"), list) else []
        hw_list = graph.get("help_wanted") if isinstance(graph.get("help_wanted"), list) else []
        lines.append(
            f"[2/5] MeshyChat Graph: reply_owed={graph.get('reply_owed_count', len(ro_list))}, "
            f"answered_still_open={graph.get('answered_still_open_count', len(ans_list))}, "
            f"awaiting={graph.get('awaiting_count', len(aw_list))}, "
            f"threads_with_news={graph.get('threads_with_news_count', len(th_list))}, "
            f"help_wanted={graph.get('help_wanted_count', len(hw_list))}"
        )
        for ask in ro_list[:5]:
            lines.append(
                f"  - OWED: {ask.get('key')} from {ask.get('author') or '?'} "
                f"on {ask.get('thread_id') or '?'} ({ask.get('state')})"
            )
        for aw in aw_list[:5]:
            if isinstance(aw, dict):
                lines.append(
                    f"  - AWAITING: {aw.get('key')} awaiting {aw.get('owing_label') or '?'} "
                    f"on {aw.get('thread_id') or '?'} ({aw.get('state') or '?'})"
                )
        for th in th_list[:5]:
            lines.append(
                f"  - THREAD NEWS: {th.get('thread_id')} ({th.get('new')} new, last {th.get('last_author') or '?'})"
            )
        for hw in hw_list[:5]:
            lines.append(
                f"  - HELP WANTED: {hw.get('key')} from {hw.get('author') or '?'} "
                f"on {hw.get('thread_id') or '?'}"
            )
    else:
        err_suffix = f" ({graph.get('error')})" if graph.get("error") else ""
        lines.append(f"[2/5] MeshyChat Graph: {graph.get('mode', 'offline')}{err_suffix}")
    if graph.get("available") and graph.get("error"):
        lines.append(f"  - PARTIAL GRAPH: {graph['error']}; missing obligations remain unknown")

    # 3. Worktrees
    wt = report.get("worktrees") or {}
    lines.append(
        f"[3/5] Worktrees & Branches: scanned={wt.get('scanned_count', 0)}, "
        f"actionable={wt.get('actionable_count', 0)}"
    )
    for w in (wt.get("actionable") or [])[:8]:
        lines.append(
            f"  - WORKTREE: {w.get('name')} [{w.get('branch')} @ {w.get('head_sha') or '?'}] "
            f"dirty={w.get('dirty_count')}, ahead={w.get('ahead_of_trunk')}, unpushed={w.get('unpushed_commits')}"
        )
    if "review_preflight" in report:
        rpf = report.get("review_preflight") or {}
        if rpf.get("available") is False:
            lines.append(f"  - REVIEW PREFLIGHT: unavailable ({rpf.get('error') or 'unknown'})")
        else:
            dcards, ffnotes = rpf.get("decision_cards") or [], rpf.get("fix_first_messages") or []
            lines.append(f"  - REVIEW PREFLIGHT: {len(dcards)} card(s), {len(ffnotes)} FIX FIRST note(s)")
            lines.extend(f"    * CARD [{c.get('type')}] {c.get('key')}: {c.get('snippet')}" for c in dcards[:5])
            lines.extend(f"    * FIX FIRST {n.get('key')} from {n.get('author') or n.get('from') or '?'}: {n.get('snippet')}" for n in ffnotes[:5])

    # 4. Handovers & Gates
    hg = report.get("handovers_and_gates") or {}
    own_h = hg.get("own_handover")
    own_str = (
        f"{own_h.get('file')} ({own_h.get('age_hours')}h, {'STALE' if own_h.get('stale') else 'fresh'})"
        if isinstance(own_h, dict)
        else "none"
    )
    lines.append(
        f"[4/5] Handovers & Gate Logs: own_handover={own_str}, "
        f"stale_count={len(hg.get('stale_handovers') or [])}, "
        f"gate_verdicts={len(hg.get('gate_reports') or [])}"
    )
    for gr in (hg.get("gate_reports") or [])[:5]:
        lines.append(f"  - GATE: {gr.get('verdict')} in {gr.get('path')} ({gr.get('line')})")

    # 5. Brain & Environment Hygiene
    brain = report.get("brain") or {}
    refused = brain.get("refused_env_present") or []
    staff_info = brain.get("staff") or {}
    elev_info = brain.get("elevated") or {}
    staff_ident = _redact_brain_identity(staff_info["identity"]) if staff_info.get("identity") else "not_installed"
    elev_ident = _redact_brain_identity(elev_info["identity"]) if elev_info.get("identity") else "not_installed"
    lines.append(
        f"[5/5] Brain & Env Hygiene: env_clean={brain.get('env_clean')}, "
        f"refused_env={refused}, "
        f"staff={staff_ident}, "
        f"elevated={elev_ident}"
    )
    if brain.get("remediation"):
        lines.append(f"  - ENV WARNING: {brain.get('remediation')}")

    for key, label in (("github_prs", "GitHub PR review states"), ("board_work", "Board tasks and ranked pull queue")):
        source = report.get(key)
        if not isinstance(source, dict):
            continue
        lines.append("[%s] %s: %s" % (key, label, source.get("mode")))
        for error in source.get("errors") or []:
            lines.append("  - GAP: " + board_tasks._q(error))
        if key == "github_prs":
            for pr in source.get("prs") or []:
                lines.append("  - %s#%s %s head=%s%s" % (pr["repo"], pr["number"], pr["review_state"], pr["head"],
                                                       " (draft)" if pr["draft"] else ""))
                if pr.get("latest_review_commit"):
                    lines.append("    latest review commit=%s (%s)" % (pr["latest_review_commit"],
                                 "matches head" if pr.get("latest_review_matches_head") else "different from head"))
        else:
            if source.get("timing_scope"):
                lines.append("  - " + source["timing_scope"])
            if source.get("queue_max_bytes"):
                lines.append("  - queue document limit: %d UTF-8 bytes" % source["queue_max_bytes"])
            if source.get("tasks"):
                lines.append(board_tasks.format_text(source["tasks"]).rstrip())
            lines.append("  - queue revision: " + str(source.get("queue_revision") or "unknown"))
            if source.get("queue_scope"):
                lines.append("  - " + source["queue_scope"])
            for row in source.get("queue_rows") or []:
                lines.append("  - " + board_tasks._q(row))

    # Candidate Options
    candidates = report.get("candidate_options") or []
    lines.append(f"=== Ranked Candidate Options ({len(candidates)}) ===")
    if not candidates:
        lines.append("  (No candidates surfaced by the checks performed; this does not establish that all work is complete.)")
    else:
        for idx, opt in enumerate(candidates, 1):
            lines.append(
                f"  {idx}. [P{opt.get('priority')} | {opt.get('source')}] {opt.get('label')}: "
                f"{opt.get('description')}"
            )
    return "\n".join(lines) + "\n"
