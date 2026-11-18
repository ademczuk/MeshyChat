"""context_snapshot.py - deterministic per-seat Context card extraction from transcript windows (#297).

Scans the tail of a seat's transcript files (Claude, Codex, Kimi, Antigravity) and its latest
extracted cards to build at most one active `Context` card (`card.v1`) per seat run.

Design invariants (Issue #297, kxxx ruling msg/<key>):
  1. Dedicated `type: "Context"`, `owner: <seat>`, `source.first_hand: False`,
     `attribution: {"speaker": "agent", "quoted_author": None, "extraction": "<dialect>/context_digest",
                    "confirmation": "typed"}`.
  2. Append-only supersession (`supersedes: ["card/<seat>/<prev_id>"]`) when `content_sha256` changes;
     an idle run whose `text_sha256` and `refs` match the active head emits nothing.
  3. Bounded `refs` (`branch`, `prs`, `threads`, repo-relative POSIX `files`), with every string
     screened through the loader's secret detectors (`detect`) before inclusion and again on the
     assembled card.
"""

import datetime
import hashlib
import json
import os
import re

MAX_REF_BRANCH_CHARS = 120
MAX_REF_PRS = 8
MAX_REF_PR_NUMBER = 999_999
MAX_REF_THREADS = 8
MAX_REF_FILES = 16
MAX_REF_FILE_CHARS = 200
MAX_CONTEXT_TEXT_CHARS = 600
TAIL_BYTES = 1024 * 1024
MAX_TAIL_FILES = 5
DOING_FIRST_READ_BYTES = 2 * 1024 * 1024
DOING_MAX_CHARS = 120
MAX_CONTEXT_SESSIONS = 5           # sessions a Context card names; the merged Codex list is cut to this too
MAX_CARD_BYTES = 8_192             # the server refuses a card value over this (apps/cards/validation.py, MAX_VALUE_BYTES)

_DOING_IN_PROGRESS_STATUSES = frozenset({"in_progress", "in-progress", "active"})
_DOING_TASK_FIELDS = ("activeForm", "content", "step", "description", "subject", "title")
_DOING_CURSOR_CACHE = {}

MESH_THREAD_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_PR_REF_RE = re.compile(r"^#[1-9][0-9]{0,5}$")
_HEX_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

_IGNORED_BRANCHES = frozenset({
    "main", "master", "HEAD", "origin", "origin/main", "origin/master", "-", "FETCH_HEAD",
})

_GIT_PREFIX = r"\bgit\s+(?:-[Cc]\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)*"
_WORKTREE_OPTS = (
    r"(?:(?:-[fFq]|--force|--quiet|--lock|--checkout|--no-checkout|--guess-remote|--no-guess-remote)\s+)*"
)

_GIT_BRANCH_PATTERNS = (
    re.compile(_GIT_PREFIX + r"(?:checkout|switch)\s+(?:-[bBcC]\s+)([A-Za-z0-9._/-]{1,120})"),
    re.compile(_GIT_PREFIX + r"worktree\s+add\s+" + _WORKTREE_OPTS + r"-[bB]\s+([A-Za-z0-9._/-]{1,120})"),
    re.compile(
        _GIT_PREFIX
        + r"worktree\s+add\s+"
        + _WORKTREE_OPTS
        + r"(?!--detach\b|-[bB]\b|-)(?:\"[^\"]+\"|'[^']+'|[^-\s]\S*)\s+([A-Za-z0-9_][A-Za-z0-9._/-]{0,119})"
    ),
    re.compile(_GIT_PREFIX + r"push\s+(?:-[uU]\s+|--set-upstream\s+)?origin\s+(?:HEAD:)?([A-Za-z0-9._/-]{1,120})"),
    re.compile(r"\bgh\s+pr\s+create\b[^\n]*?--head(?:\s+|=)[\"']?([A-Za-z0-9._/-]{1,120})[\"']?"),
    re.compile(_GIT_PREFIX + r"(?:checkout|switch)\s+(?!--detach\b)([A-Za-z0-9._/-]{1,120})"),
)

_PR_PATTERNS = (
    re.compile(r"\bgh\s+pr\s+(?:view|diff|comment|review|checks|merge|edit|close|reopen)\s+#?([1-9][0-9]{0,5})\b"),
    re.compile(r"/pulls?/([1-9][0-9]{0,5})\b"),
    re.compile(r"(?<![A-Za-z0-9_/])#([1-9][0-9]{0,5})\b"),
)

_THREAD_PATTERNS = (
    re.compile(r"--thread(?:(?:\s+|=)[\"']?|[\"']\s*,\s*[\"'])([a-zA-Z0-9][a-zA-Z0-9._:-]{0,127})[\"']?"),
    re.compile(
        r"\bmeshy_node\s+thread\b[^\n]*?--id(?:(?:\s+|=)[\"']?|[\"']\s*,\s*[\"'])"
        r"([a-zA-Z0-9][a-zA-Z0-9._:-]{0,127})[\"']?"
    ),
)

_REPO_ANCHORS = (
    "apps/", "tools/", "tests/", "docs/", "sql_meshychat/", "meshychat/",
    ".github/", "src/", "crates/", "scripts/", "migrations/",
)

_REPO_ROOT_Files = frozenset({
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "README.md", "CHANGELOG.md",
    "requirements.txt", "manage.py", "Cargo.toml", "pyproject.toml", "conftest.py",
})

_DOT_HOME_SEGMENTS = (
    "/.meshychat/", "/.dxxxxx/", "/.claude/", "/.codex/", "/.gemini/", "/.kimi/",
)

_CMD_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_./~\\:-])((?:apps|tools|tests|docs|sql_meshychat|meshychat|\.github|src|crates)"
    r"/[A-Za-z0-9._/-]+\.[A-Za-z0-9]{1,8})(?![A-Za-z0-9_.*?-])"
)

_PS_HEREDOC_RE = re.compile(r"(?s)@(['\"])[ \t]*\r?\n.*?\r?\n\1@")
_SH_HEREDOC_DASH_RE = re.compile(
    r"(?s)<<-[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\b\1([^\r\n]*)\r?\n.*?\r?\n\t*\2(?=\r?\n|$)"
)
_SH_HEREDOC_PLAIN_RE = re.compile(
    r"(?s)<<(?!-)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\b\1([^\r\n]*)\r?\n.*?\r?\n\2(?=\r?\n|$)"
)

_TRAILING_PARTIAL_U_RE = re.compile(r"(?<!\\)(?:\\\\)*(\\u[0-9a-fA-F]{0,3})$")
_JSON_UNICODE_ESC_RE = re.compile(r"\\u[0-9a-fA-F]{4}")


def _has_surrogate(text):
    return isinstance(text, str) and any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _is_pure_raw_win_path(raw_inner):
    """True only when `raw_inner` is an unescaped Windows path (single backslashes, no `/`, `\\\\`, `\\\"`, or `\\uXXXX`)."""
    return (
        isinstance(raw_inner, str)
        and "\\" in raw_inner
        and "/" not in raw_inner
        and "\\\\" not in raw_inner
        and '\\"' not in raw_inner
        and not _JSON_UNICODE_ESC_RE.search(raw_inner)
    )


def _strip_heredocs(cmd):
    """Remove inline PowerShell and POSIX heredoc bodies so prose inside scripts does not pollute file refs."""
    if not isinstance(cmd, str) or "\n" not in cmd:
        return cmd or ""
    cleaned = _PS_HEREDOC_RE.sub(" ", cmd)
    cleaned = _SH_HEREDOC_DASH_RE.sub(r" \3", cleaned)
    return _SH_HEREDOC_PLAIN_RE.sub(r" \3", cleaned)


def _unwrap_ag_arg(val, *, is_path=False):
    """Decode an Antigravity tool_calls argument value, which `transcript.jsonl` stores as a JSON string literal."""
    if not isinstance(val, str):
        return ""
    s = val.strip()
    was_truncated = s.endswith("...[truncated]")
    if was_truncated:
        s = s[:-len("...[truncated]")]
    if s.startswith('"'):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, str):
                if _has_surrogate(parsed):
                    return ""
                if is_path and any(ord(ch) < 32 for ch in parsed):
                    raw_inner = s[1:-1] if (len(s) >= 2 and s.endswith('"')) else s[1:]
                    if _is_pure_raw_win_path(raw_inner) and not _has_surrogate(raw_inner):
                        return raw_inner
                    return ""
                return parsed
        except ValueError:
            if not was_truncated:
                return "" if _has_surrogate(s) else s
            body = s[1:]
            if body.endswith('"') and (len(body[:-1]) - len(body[:-1].rstrip("\\"))) % 2 == 0:
                body = body[:-1]
            m = _TRAILING_PARTIAL_U_RE.search(body)
            if m:
                body = body[:m.start(1)]
            trailing_bs = len(body) - len(body.rstrip("\\"))
            if trailing_bs % 2 == 1:
                body = body[:-1]
            try:
                parsed = json.loads('"' + body + '"', strict=False)
                if isinstance(parsed, str):
                    if _has_surrogate(parsed):
                        return ""
                    if is_path and any(ord(ch) < 32 for ch in parsed):
                        if _is_pure_raw_win_path(body) and not _has_surrogate(body):
                            return body
                        return ""
                    return parsed
            except ValueError:
                pass
            return "" if _has_surrogate(body) else body
    return "" if _has_surrogate(s) else s


def is_repo_relative_file(path):
    """True when `path` is a safe repo-relative POSIX path within validation bounds (#297)."""
    if not isinstance(path, str) or not (1 <= len(path) <= MAX_REF_FILE_CHARS):
        return False
    if path.startswith("/") or "\\" in path or ":" in path:
        return False
    if any(ch in path for ch in "*?[]{}|<>"):
        return False
    if any(ord(ch) <= 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF or ch.isspace() for ch in path):
        return False
    parts = path.split("/")
    return all(part and part not in (".", "..") for part in parts)


def normalize_repo_path(raw_path):
    """Convert a tool file path or relative path into a repo-relative POSIX path, or None if outside known repo layout."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    clean_raw = _unwrap_ag_arg(raw_path, is_path=True).strip().strip("\"'")
    p = clean_raw.replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    p = p.rstrip("/")
    if not p or ".." in p.split("/"):
        return None
    if any(ch in p for ch in "*?[]{}|<>"):
        return None
    p_lower = "/" + p.lower().lstrip("/") + "/"
    if any(seg in p_lower for seg in _DOT_HOME_SEGMENTS) and "/worktrees/" not in p_lower:
        return None

    # Check if already repo-relative anchored
    if is_repo_relative_file(p):
        if p in _REPO_ROOT_Files or any(p.startswith(anchor) for anchor in _REPO_ANCHORS):
            return p

    # Strip known project root prefixes (e.g., C:/Projects/<repo>/... or /.../Projects/<repo>/...)
    m = re.search(r"(?i)(?:^|/)(?:projects|repos|worktrees)/[^/]+/(.+)$", p)
    if m:
        candidate = m.group(1)
        if is_repo_relative_file(candidate) and (
            candidate in _REPO_ROOT_Files or any(candidate.startswith(a) for a in _REPO_ANCHORS)
        ):
            return candidate

    # Look for leftmost repo anchor segment
    for anchor in _REPO_ANCHORS:
        idx = p.find("/" + anchor)
        if idx >= 0:
            candidate = p[idx + 1:]
            if is_repo_relative_file(candidate):
                return candidate
    return None


def _valid_branch(candidate):
    if not isinstance(candidate, str):
        return None
    b = candidate.strip().strip("\"'")
    if not (1 <= len(b) <= MAX_REF_BRANCH_CHARS):
        return None
    if b.startswith(("-", ".", "/")) or ".." in b or b in _IGNORED_BRANCHES or _HEX_COMMIT_RE.fullmatch(b):
        return None
    if any(ord(ch) <= 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF or ch.isspace() for ch in b):
        return None
    if b.startswith("origin/"):
        b = b[len("origin/"):]
        if not b or b.startswith(("-", ".", "/")) or b in _IGNORED_BRANCHES:
            return None
    # A branch typically contains a slash (e.g. seat/topic or fix/issue) or hyphen
    if "/" not in b and "-" not in b:
        return None
    return b


def _kimi_ms_to_iso(ms):
    """Convert Kimi epoch milliseconds to `YYYY-MM-DDTHH:MM:SS.mmmZ`, matching `kimi_dialect._time`."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return ""
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(ms) % 1000)


def _extract_from_record(o, dialect):
    """Return (commands, file_paths, timestamp, session, user_text) from one parsed JSONL record."""
    cmds, files, ts, session, user_text = [], [], "", "", ""
    if not isinstance(o, dict):
        return cmds, files, ts, session, user_text

    if dialect == "antigravity":
        ts = o.get("created_at") or ""
        rtype = o.get("type")
        if o.get("source") == "USER_EXPLICIT" and rtype == "USER_INPUT":
            content = o.get("content")
            if isinstance(content, str):
                m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
                user_text = (m.group(1) if m else content).strip()
        elif rtype == "PLANNER_RESPONSE":
            for tc in o.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                cmd = _unwrap_ag_arg(args.get("CommandLine"))
                if cmd and cmd.strip():
                    cmds.append(cmd)
                for k in ("TargetFile", "AbsolutePath"):
                    fp = _unwrap_ag_arg(args.get(k), is_path=True)
                    if fp and fp.strip():
                        files.append(fp)
        return cmds, files, ts, session, user_text

    if dialect == "kimi":
        # Real Kimi wire.jsonl records carry top-level `time` in epoch milliseconds (kimi_dialect._time)
        # and loop events under `{"type": "context.append_loop_event", "time": <ms>, "event": {"type": "tool.call", ...}}`.
        ts = _kimi_ms_to_iso(o.get("time"))
        rtype = o.get("type")
        if rtype == "goal.create":
            obj = o.get("objective")
            if isinstance(obj, str) and obj.strip():
                user_text = obj.strip()
        elif rtype == "context.append_message":
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            origin = msg.get("origin") if isinstance(msg.get("origin"), dict) else {}
            if msg.get("role") == "user" and origin.get("kind") in ("user", None):
                content = msg.get("content")
                parts = content if isinstance(content, list) else [content]
                texts = []
                for c in parts:
                    if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                        if not c["text"].lstrip().startswith("<system-reminder"):
                            texts.append(c["text"])
                    elif isinstance(c, str):
                        texts.append(c)
                joined = "\n".join(t for t in texts if t.strip()).strip()
                if joined:
                    user_text = joined
        ev = o.get("event") if isinstance(o.get("event"), dict) else {}
        if ev.get("type") == "tool.call":
            raw_args = ev.get("args")
            args = {}
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        args = parsed
                except ValueError:
                    args = {}
            for k in ("command", "cmd", "CommandLine"):
                val = args.get(k)
                if isinstance(val, str) and val.strip():
                    cmds.append(val)
            for k in ("path", "file_path", "target_file", "absolutePath", "AbsolutePath", "TargetFile", "file"):
                val = args.get(k)
                if isinstance(val, str) and val.strip():
                    files.append(val)
        return cmds, files, ts, session, user_text

    if dialect == "codex":
        ts = o.get("timestamp") or ""
        payload = o.get("payload") if isinstance(o.get("payload"), dict) else {}
        if o.get("type") == "response_item" and payload.get("type") == "function_call":
            raw_args = payload.get("arguments")
            args = {}
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except ValueError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            if isinstance(args, dict):
                for k in ("command", "cmd"):
                    val = args.get(k)
                    if isinstance(val, str):
                        cmds.append(val)
                    elif isinstance(val, list):
                        cmds.append(" ".join(str(x) for x in val))
                for k in ("file_path", "path", "workdir"):
                    if isinstance(args.get(k), str):
                        files.append(args[k])
        return cmds, files, ts, session, user_text

    # Default: claude dialect
    ts = o.get("timestamp") or ""
    session = o.get("sessionId") or o.get("session_id") or ""
    msg = o.get("message") if isinstance(o.get("message"), dict) else {}
    content = msg.get("content")
    if o.get("type") == "assistant" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            for k in ("command", "cmd"):
                if isinstance(inp.get(k), str):
                    cmds.append(inp[k])
            for k in ("file_path", "path", "pattern"):
                if isinstance(inp.get(k), str):
                    files.append(inp[k])
    return cmds, files, ts, session, user_text


def _tail_lines(path, max_bytes=TAIL_BYTES):
    """Yield complete JSONL lines from the last `max_bytes` of `path`."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard partial first line
            for raw in f:
                if raw.endswith(b"\n") and raw.strip():
                    yield raw
    except OSError:
        return


def _in_progress_todo(item):
    if not isinstance(item, dict):
        return False
    status = item.get("status")
    return isinstance(status, str) and status in _DOING_IN_PROGRESS_STATUSES


def _todo_task_text(item):
    if not isinstance(item, dict):
        return None
    for k in _DOING_TASK_FIELDS:
        val = item.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


_DOING_TOOL_NAMES = frozenset({
    "TodoWrite",
    "TodoRead",
    "update_plan",
    "write_todos",
    "todo_write",
    "SetTodoList",
})
_DOING_ARG_KEYS = frozenset({"input", "args", "arguments", "todos", "plan"})
_DOING_SKIP_RECORD_TYPES = frozenset({
    "tool_result",
    "function_call_output",
    "custom_tool_call_output",
    "USER_INPUT",
    "user",
})
_DOING_SKIP_DICT_KEYS = frozenset({
    "output",
    "result",
    "tool_result",
    "stdout",
    "stderr",
    "error",
    "thinking",
    "reasoning",
})


class DoingSeen:
    """Incremental state of a session's title and active in-progress todo/plan item."""

    def __init__(self, title=None, task=None, has_task_list=False):
        self.title = title
        self.task = task
        self.has_task_list = bool(has_task_list)

    def clone(self):
        return DoingSeen(title=self.title, task=self.task, has_task_list=self.has_task_list)

    def __eq__(self, other):
        if not isinstance(other, DoingSeen):
            return NotImplemented
        return (self.title, self.task, self.has_task_list) == (other.title, other.task, other.has_task_list)

    def __repr__(self):
        return "DoingSeen(title=%r, task=%r, has_task_list=%r)" % (self.title, self.task, self.has_task_list)

    def visit(self, v, _in_tool_args=False):
        if isinstance(v, dict):
            if v.get("isSidechain"):
                return
            vtype = v.get("type")
            vrole = v.get("role")
            if vtype == "ai-title" and isinstance(v.get("aiTitle"), str) and v["aiTitle"].strip():
                self.title = v["aiTitle"].strip()
            elif isinstance(v.get("thread_name"), str) and v["thread_name"].strip():
                self.title = v["thread_name"].strip()
            if (isinstance(vtype, str) and vtype in _DOING_SKIP_RECORD_TYPES) or (
                isinstance(vrole, str) and vrole in ("user", "tool")
            ):
                return
            tool_name = v.get("name")
            if isinstance(tool_name, str) and tool_name not in _DOING_TOOL_NAMES:
                if (
                    any(k in v for k in ("input", "args", "arguments"))
                    or (isinstance(vtype, str) and vtype in ("tool_use", "function_call", "custom_tool_call"))
                ):
                    return
            is_recognized_tool = isinstance(tool_name, str) and tool_name in _DOING_TOOL_NAMES
            is_synthetic_arg_wrapper = (
                tool_name is None
                and set(v.keys()) <= {"input", "args", "arguments", "todos", "plan", "type"}
            )
            allow_task_args = _in_tool_args or is_recognized_tool or is_synthetic_arg_wrapper
            for k, val in v.items():
                if k in _DOING_SKIP_DICT_KEYS:
                    continue
                if (
                    allow_task_args
                    and k in ("todos", "plan")
                    and isinstance(val, list)
                    and (not val or any(isinstance(i, dict) and "status" in i for i in val))
                ):
                    self.has_task_list = True
                    self.task = next((_todo_task_text(i) for i in val if _in_progress_todo(i)), None)
                else:
                    child_in_args = allow_task_args and (k in _DOING_ARG_KEYS)
                    self.visit(val, _in_tool_args=child_in_args)
        elif isinstance(v, list):
            for item in v:
                self.visit(item, _in_tool_args=_in_tool_args)
        elif isinstance(v, str) and _in_tool_args:
            s = v.strip()
            if s.startswith("{") and any(tok in s for tok in ('"status"', '"todos"', '"plan"')):
                try:
                    parsed = json.loads(s)
                except ValueError:
                    return
                self.visit(parsed, _in_tool_args=True)
            elif s.startswith('"') and any(tok in s for tok in ("status", "todos", "plan")):
                unwrapped = _unwrap_ag_arg(s).strip()
                if unwrapped.startswith("{") and any(tok in unwrapped for tok in ('"status"', '"todos"', '"plan"')):
                    try:
                        parsed = json.loads(unwrapped)
                    except ValueError:
                        return
                    self.visit(parsed, _in_tool_args=True)


def format_doing(title, task):
    """Format `<title> | <in_progress_task>`, either alone, or None, capped at 120 chars."""
    t = " ".join(title.split()).strip() if isinstance(title, str) else ""
    k = " ".join(task.split()).strip() if isinstance(task, str) else ""
    if t and k:
        line = "%s | %s" % (t, k) if t.lower() != k.lower() else t
    elif t:
        line = t
    elif k:
        line = k
    else:
        return None
    if _has_surrogate(line):
        line = line.encode("utf-8", "replace").decode("utf-8")
    if len(line) <= DOING_MAX_CHARS:
        return line
    return line[: DOING_MAX_CHARS - 3].rstrip() + "..."


def scan_transcript_doing(path, cursor_cache=None):
    """Incrementally scan `path` using `(mtime_ns, offset, whole)` cursor state to return `DoingSeen`."""
    p = os.path.abspath(os.fspath(path))
    try:
        st = os.stat(p)
    except OSError:
        return DoingSeen()
    mtime_ns, size, dev, ino = st.st_mtime_ns, st.st_size, st.st_dev, st.st_ino
    cache = _DOING_CURSOR_CACHE if cursor_cache is None else cursor_cache
    entry = cache.get(p)
    try:
        with open(p, "rb") as f:
            head_probe = f.read(min(256, size))
            if (
                isinstance(entry, dict)
                and entry.get("mtime_ns") == mtime_ns
                and entry.get("offset") == size
                and entry.get("dev") == dev
                and entry.get("ino") == ino
                and entry.get("head_probe") == head_probe
                and isinstance(entry.get("seen"), DoingSeen)
            ):
                return entry["seen"].clone()
            prev_probe = entry.get("head_probe") if isinstance(entry, dict) else None
            probe_mismatch = (
                prev_probe is not None
                and not head_probe.startswith(prev_probe)
            )
            cached_offset = entry.get("offset", 0) if isinstance(entry, dict) else 0
            prev_tail_probe = entry.get("tail_probe") if isinstance(entry, dict) else None
            tail_probe_mismatch = False
            if (
                isinstance(entry, dict)
                and cached_offset > 0
                and size >= cached_offset
                and prev_tail_probe is not None
            ):
                tail_len = min(256, len(prev_tail_probe), cached_offset)
                f.seek(cached_offset - tail_len)
                tail_probe_mismatch = f.read(tail_len) != prev_tail_probe[-tail_len:]
            mtime_changed_without_growth = (
                isinstance(entry, dict)
                and entry.get("mtime_ns") is not None
                and entry.get("mtime_ns") != mtime_ns
                and size <= cached_offset
            )
            identity_changed = (
                not isinstance(entry, dict)
                or entry.get("dev") not in (None, dev)
                or entry.get("ino") not in (None, ino)
                or probe_mismatch
                or tail_probe_mismatch
            )
            if (
                identity_changed
                or entry.get("whole")
                or size < cached_offset
                or mtime_changed_without_growth
            ):
                was_whole = (
                    bool(entry.get("whole"))
                    if (isinstance(entry, dict) and not identity_changed)
                    else False
                )
                entry = {
                    "mtime_ns": None,
                    "offset": 0,
                    "whole": was_whole,
                    "dev": dev,
                    "ino": ino,
                    "head_probe": head_probe,
                    "tail_probe": None,
                    "seen": DoingSeen(),
                }
                cache[p] = entry

            is_json_doc = p.lower().endswith(".json")
            prev_offset = entry["offset"]
            if is_json_doc or entry["whole"]:
                start = 0
            else:
                start = max(0, size - DOING_FIRST_READ_BYTES) if prev_offset == 0 else prev_offset
            prev_byte_is_nl = False
            if start > 0 and prev_offset == 0:
                f.seek(start - 1)
                prev_byte_is_nl = f.read(1) == b"\n"
            else:
                f.seek(start)
            buf = f.read(size - start)
    except OSError:
        return entry["seen"].clone() if isinstance(entry, dict) and isinstance(entry.get("seen"), DoingSeen) else DoingSeen()

    entry["mtime_ns"] = mtime_ns
    entry["dev"] = dev
    entry["ino"] = ino
    entry["head_probe"] = head_probe
    if entry["whole"] or (start == 0 and (is_json_doc or not buf.endswith(b"\n"))):
        try:
            doc = json.loads(buf.decode("utf-8", "replace"))
        except ValueError:
            doc = None
        if isinstance(doc, (dict, list)) and (is_json_doc or b"\n" not in buf.strip()):
            entry["whole"] = True
            entry["offset"] = size
            entry["seen"] = DoingSeen()
            entry["seen"].visit(doc)
            return entry["seen"].clone()
        entry["whole"] = False

    last_nl = buf.rfind(b"\n")
    end = (last_nl + 1) if last_nl >= 0 else 0
    if start > 0 and prev_offset == 0 and not prev_byte_is_nl:
        first_nl = buf.find(b"\n", 0, end)
        skip = (first_nl + 1) if first_nl >= 0 else end
    else:
        skip = 0
    parsed_records = []
    needed_ag_full = set()
    is_ag_compact = os.path.basename(p) == "transcript.jsonl"
    for raw_line in buf[skip:end].split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            v = json.loads(raw_line.decode("utf-8", "replace"))
        except ValueError:
            continue
        parsed_records.append(v)
        if (
            is_ag_compact
            and isinstance(v, dict)
            and v.get("type") == "PLANNER_RESPONSE"
            and isinstance(v.get("truncated_fields"), list)
            and "tool_calls" in v.get("truncated_fields")
            and v.get("step_index") is not None
        ):
            needed_ag_full.add((v.get("step_index"), v.get("created_at") or ""))
    ag_full_by_step = {}
    if needed_ag_full:
        full_p = os.path.join(os.path.dirname(p), "transcript_full.jsonl")
        if os.path.isfile(full_p):
            try:
                with open(full_p, "rb") as ff:
                    for raw_full in ff:
                        if b"PLANNER_RESPONSE" not in raw_full:
                            continue
                        try:
                            fo = json.loads(raw_full.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        if isinstance(fo, dict):
                            key = (fo.get("step_index"), fo.get("created_at") or "")
                            if key in needed_ag_full:
                                ag_full_by_step[key] = fo
                                if len(ag_full_by_step) == len(needed_ag_full):
                                    break
            except OSError:
                pass
    for v in parsed_records:
        if ag_full_by_step and isinstance(v, dict) and v.get("truncated_fields"):
            key = (v.get("step_index"), v.get("created_at") or "")
            if key in ag_full_by_step:
                v = ag_full_by_step[key]
        entry["seen"].visit(v)
    new_offset = start + end
    entry["offset"] = new_offset
    if end > 0:
        new_tail_bytes = buf[max(0, end - 256):end]
        if prev_tail_probe and start == prev_offset and len(new_tail_bytes) < 256:
            entry["tail_probe"] = (prev_tail_probe + new_tail_bytes)[-256:]
        else:
            entry["tail_probe"] = new_tail_bytes
    return entry["seen"].clone()


def _bump_time_after(candidate_time, floor_time, utc_instant_fn):
    """Ensure `candidate_time` is strictly greater than `floor_time` (`YYYY-MM-DDTHH:MM:SS.mmmZ`)."""
    cand = utc_instant_fn(candidate_time) if candidate_time else ""
    floor = utc_instant_fn(floor_time) if floor_time else ""
    if floor:
        try:
            dt_floor = datetime.datetime.strptime(floor, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            floor = ""
            dt_floor = None
    else:
        dt_floor = None
    if not cand and not floor:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if not floor:
        return cand
    if cand and cand > floor:
        return cand
    bumped = dt_floor + datetime.timedelta(milliseconds=1)
    return bumped.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (bumped.microsecond // 1000)


def _latest_task_summary(ordered_cards, fallback_prompt, detect_fn, disabled_detectors, seat):
    """Extract a concise, detector-clean summary of the seat's latest Ruling a recorded decision Candidate."""
    for card in reversed(ordered_cards or ()):
        ctype = card.get("type")
        if ctype not in ("Ruling", "Candidate"):
            continue
        if ctype == "Candidate" and not (card.get("source") or {}).get("first_hand"):
            continue
        raw_text = card.get("text") or ""
        body = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text
        cleaned = " ".join(body.split()).strip()
        if not cleaned:
            continue
        if ctype == "Ruling":
            q = " ".join(str(card.get("question") or "").split()).strip()
            ans = " ".join(str(card.get("answer") or "").split()).strip()
            if not (q and ans) and "\nA: " in body:
                q_part, a_part = body.split("\nA: ", 1)
                q_line = q_part.split("\nOffered: ", 1)[0]
                if q_line.startswith("Q: "):
                    q_line = q_line[3:]
                q = q or " ".join(q_line.split()).strip()
                ans = ans or " ".join(a_part.split()).strip()
            if q and ans and len(cleaned) > 180:
                q_clip = (q[:62].rstrip() + "...") if len(q) > 65 else q
                a_clip = (ans[:112].rstrip() + "...") if len(ans) > 115 else ans
                cleaned = "Q: %s | A: %s" % (q_clip, a_clip)
        if _has_surrogate(cleaned):
            cleaned = cleaned.encode("utf-8", "replace").decode("utf-8")
        if len(cleaned) > 220:
            cleaned = cleaned[:217].rstrip() + "..."
        if not detect_fn(cleaned, disabled_detectors, seat):
            return cleaned, card.get("time") or ""
    if fallback_prompt:
        cleaned = " ".join(fallback_prompt.split()).strip()
        if _has_surrogate(cleaned):
            cleaned = cleaned.encode("utf-8", "replace").decode("utf-8")
        if len(cleaned) > 220:
            cleaned = cleaned[:217].rstrip() + "..."
        if cleaned and not detect_fn(cleaned, disabled_detectors, seat):
            return cleaned, ""
    return "Active session context snapshot", ""


def _serialize_doing_cursor_cache(cache):
    out = {}
    for p, entry in (cache or {}).items():
        if not isinstance(p, str) or not isinstance(entry, dict):
            continue
        seen = entry.get("seen")
        if not isinstance(seen, DoingSeen):
            continue
        hp = entry.get("head_probe")
        tp = entry.get("tail_probe")
        out[p] = {
            "mtime_ns": entry.get("mtime_ns"),
            "offset": entry.get("offset", 0),
            "whole": bool(entry.get("whole")),
            "dev": entry.get("dev"),
            "ino": entry.get("ino"),
            "head_probe_hex": hp.hex() if isinstance(hp, (bytes, bytearray)) else None,
            "tail_probe_hex": tp.hex() if isinstance(tp, (bytes, bytearray)) else None,
            "title": seen.title,
            "task": seen.task,
            "has_task_list": bool(seen.has_task_list),
        }
    return out


def _deserialize_doing_cursor_cache(raw):
    out = {}
    if not isinstance(raw, dict):
        return out
    for p, entry in raw.items():
        if not isinstance(p, str) or not isinstance(entry, dict):
            continue
        hp_hex = entry.get("head_probe_hex")
        tp_hex = entry.get("tail_probe_hex")
        try:
            hp = bytes.fromhex(hp_hex) if isinstance(hp_hex, str) else None
            tp = bytes.fromhex(tp_hex) if isinstance(tp_hex, str) else None
        except ValueError:
            continue
        out[p] = {
            "mtime_ns": entry.get("mtime_ns"),
            "offset": int(entry.get("offset") or 0),
            "whole": bool(entry.get("whole")),
            "dev": entry.get("dev"),
            "ino": entry.get("ino"),
            "head_probe": hp,
            "tail_probe": tp,
            "seen": DoingSeen(
                title=entry.get("title") if isinstance(entry.get("title"), str) else None,
                task=entry.get("task") if isinstance(entry.get("task"), str) else None,
                has_task_list=bool(entry.get("has_task_list")),
            ),
        }
    return out


def extract_seat_context(paths, dialect, seat, ordered_cards, *, base_dir=None, detect_fn=None,
                         disabled_detectors=(), utc_instant_fn=None, doing_cursor_cache=None):
    """Scan the most recent transcript files in `paths` and `ordered_cards` to produce a raw context snapshot dict."""
    if detect_fn is None:
        detect_fn = lambda s, d, st: set()  # noqa: E731
    if utc_instant_fn is None:
        utc_instant_fn = lambda s: s or ""  # noqa: E731

    # Sort paths by modification time (newest last), inspect up to MAX_TAIL_FILES
    dated_paths = []
    for p in paths or ():
        try:
            dated_paths.append((os.path.getmtime(p), p))
        except OSError:
            continue
    dated_paths.sort(key=lambda item: (item[0], item[1]))
    recent_paths = [p for _, p in dated_paths[-MAX_TAIL_FILES:]]

    branch = None
    prs_rev = []
    seen_pr_nums = set()
    threads_rev = []
    seen_threads = set()
    files_rev = []
    seen_files = set()
    sessions = []
    source_files = []
    latest_ts = ""
    latest_prompt = ""
    doing_title = None
    doing_task = None
    doing_session_id = None

    all_cmds = []
    all_raw_files = []

    for p in recent_paths:
        rel_name = os.path.relpath(p, base_dir) if base_dir else os.path.basename(p)
        if rel_name not in source_files:
            source_files.append(rel_name)
        norm_parts = os.path.normpath(p).split(os.sep)
        path_sess = None
        if dialect == "kimi":
            for comp in reversed(norm_parts):
                if comp.startswith("session_"):
                    path_sess = comp[len("session_"):]
                    if path_sess not in sessions:
                        sessions.append(path_sess)
                    break
        elif dialect == "antigravity":
            for idx, comp in enumerate(norm_parts):
                if comp == ".system_generated" and idx > 0:
                    path_sess = norm_parts[idx - 1]
                    if path_sess not in sessions:
                        sessions.append(path_sess)
                    break
        seen_doing = scan_transcript_doing(p, cursor_cache=doing_cursor_cache)
        tail_records = []
        needed_full_steps = set()
        for raw_line in _tail_lines(p):
            try:
                o = json.loads(raw_line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            tail_records.append(o)
            if (
                dialect == "antigravity"
                and o.get("type") == "PLANNER_RESPONSE"
                and "tool_calls" in (o.get("truncated_fields") or ())
                and o.get("step_index") is not None
            ):
                needed_full_steps.add((o.get("step_index"), o.get("created_at") or ""))
        full_by_step = {}
        if needed_full_steps and os.path.basename(p) == "transcript.jsonl":
            full_p = os.path.join(os.path.dirname(p), "transcript_full.jsonl")
            if os.path.isfile(full_p):
                for raw_full in _tail_lines(full_p, max_bytes=TAIL_BYTES * 4):
                    if b"PLANNER_RESPONSE" not in raw_full:
                        continue
                    try:
                        fo = json.loads(raw_full.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if isinstance(fo, dict):
                        key = (fo.get("step_index"), fo.get("created_at") or "")
                        if key in needed_full_steps:
                            full_by_step[key] = fo
        file_sess = path_sess
        for o in tail_records:
            if dialect == "antigravity" and o.get("truncated_fields"):
                key = (o.get("step_index"), o.get("created_at") or "")
                if key in full_by_step:
                    o = full_by_step[key]
            cmds, fpaths, ts, sess, utext = _extract_from_record(o, dialect)
            if not file_sess and sess:
                file_sess = sess
            if not file_sess and isinstance(o.get("id"), str) and ("thread_name" in o or o.get("type") == "session_meta"):
                file_sess = o["id"]
            inst = utc_instant_fn(ts)
            if inst and inst > latest_ts:
                latest_ts = inst
            if sess and sess not in sessions:
                sessions.append(sess)
            if utext:
                latest_prompt = utext
            all_cmds.extend(cmds)
            all_raw_files.extend(fpaths)
            for cmd in cmds:
                for m in _CMD_FILE_RE.finditer(_strip_heredocs(cmd)):
                    all_raw_files.append(m.group(1))
        file_scope = file_sess or os.path.abspath(p)
        if file_scope != doing_session_id:
            doing_title = None
            doing_task = None
            doing_session_id = file_scope
        if seen_doing.title and not detect_fn(seen_doing.title, disabled_detectors, seat):
            doing_title = seen_doing.title
        if seen_doing.has_task_list:
            if seen_doing.task and not detect_fn(seen_doing.task, disabled_detectors, seat):
                doing_task = seen_doing.task
            else:
                doing_task = None

    # Walk commands in reverse so most recent branch, PRs, and threads win (dropping oldest first)
    for cmd in reversed(all_cmds):
        if branch is None:
            branch_matches = []
            for pat_idx, pat in enumerate(_GIT_BRANCH_PATTERNS):
                for m in pat.finditer(cmd):
                    branch_matches.append((m.start(), -pat_idx, m.group(1)))
            branch_matches.sort(reverse=True)
            for _pos, _pidx, raw_cand in branch_matches:
                cand = _valid_branch(raw_cand)
                if cand and not detect_fn(cand, disabled_detectors, seat):
                    branch = cand
                    break
        pr_matches = []
        for pat_idx, pat in enumerate(_PR_PATTERNS):
            for m in pat.finditer(cmd):
                pr_matches.append((m.start(), -pat_idx, int(m.group(1))))
        pr_matches.sort(reverse=True)
        for _pos, _pidx, num in pr_matches:
            if 1 <= num <= MAX_REF_PR_NUMBER and num not in seen_pr_nums and len(prs_rev) < MAX_REF_PRS:
                pr_str = f"#{num}"
                if not detect_fn(pr_str, disabled_detectors, seat):
                    seen_pr_nums.add(num)
                    prs_rev.append(pr_str)
        thread_matches = []
        for pat_idx, pat in enumerate(_THREAD_PATTERNS):
            for m in pat.finditer(cmd):
                thread_matches.append((m.start(), -pat_idx, m.group(1)))
        thread_matches.sort(reverse=True)
        for _pos, _pidx, tid in thread_matches:
            if MESH_THREAD_RE.fullmatch(tid) and tid not in seen_threads and len(threads_rev) < MAX_REF_THREADS:
                if not detect_fn(tid, disabled_detectors, seat):
                    seen_threads.add(tid)
                    threads_rev.append(tid)

    for raw_fp in reversed(all_raw_files):
        norm = normalize_repo_path(raw_fp)
        if norm and norm not in seen_files and len(files_rev) < MAX_REF_FILES:
            if not detect_fn(norm, disabled_detectors, seat):
                seen_files.add(norm)
                files_rev.append(norm)

    doing_line = format_doing(doing_title, doing_task)
    if doing_line and detect_fn(doing_line, disabled_detectors, seat):
        doing_line = None

    task_summary, card_ts = _latest_task_summary(ordered_cards, latest_prompt, detect_fn, disabled_detectors, seat)
    if card_ts and card_ts > latest_ts:
        latest_ts = card_ts
    if doing_line and task_summary == "Active session context snapshot":
        task_summary = doing_line

    refs = {
        "branch": branch,
        "prs": prs_rev,
        "threads": threads_rev,
        "files": files_rev,
    }
    out = {
        "refs": refs,
        "task": task_summary,
        "latest_time": latest_ts,
        "sessions": sessions[-MAX_CONTEXT_SESSIONS:],
        "source_files": source_files[-5:],
    }
    if doing_line:
        out["doing"] = doing_line
    return out


def build_context_card(seat, dialect, snapshot, *, loader_version, detect_fn, disabled_detectors=(),
                       utc_instant_fn=None, prev_context_id=None, prev_context_time=None):
    """Build and screen a `card.v1` Context card from `snapshot`.

    Returns `(card, content_sha256, None)` on success, or `(None, content_sha256, reason)` if empty or redacted.
    """
    if utc_instant_fn is None:
        utc_instant_fn = lambda s: s or ""  # noqa: E731

    refs = snapshot["refs"]
    task = snapshot["task"]
    doing = snapshot.get("doing")
    if doing and (not task or task == "Active session context snapshot"):
        task = doing
    has_signal = bool(refs["branch"] or refs["prs"] or refs["threads"] or refs["files"]
                      or doing or (task and task != "Active session context snapshot"))
    if not has_signal:
        return None, "", "no_context_signal"

    card_time = _bump_time_after(snapshot.get("latest_time") or "", prev_context_time or "", utc_instant_fn)
    head = "Context | owner: %s | seat: %s | %s" % (seat, seat, card_time[:16].replace("T", " "))
    branch_str = refs["branch"] or "none"
    prs_str = ", ".join(refs["prs"][:8]) if refs["prs"] else "none"
    threads_str = ", ".join(refs["threads"][:4]) if refs["threads"] else "none"
    files_str = ", ".join(refs["files"][:6]) if refs["files"] else "none"
    lines = [
        head,
        "Branch: %s | PRs: %s | Threads: %s" % (branch_str, prs_str, threads_str),
        "Files: %s" % files_str,
        "Task: %s" % task,
    ]
    if doing and doing != task:
        lines.append("Doing: %s" % doing)
    full_text = "\n".join(lines)
    truncated = len(full_text) > MAX_CONTEXT_TEXT_CHARS
    text = full_text[:MAX_CONTEXT_TEXT_CHARS] if truncated else full_text
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # Content hash covers the semantic payload (refs + task + optional doing), independent of timestamp bumps
    semantic_obj = {"refs": refs, "task": task, "seat": seat}
    if doing and doing != task:
        semantic_obj["doing"] = doing
    semantic_payload = json.dumps(semantic_obj, sort_keys=True, ensure_ascii=False)
    content_sha = hashlib.sha256(semantic_payload.encode("utf-8")).hexdigest()

    # Screen full text + all ref strings through secret detectors (fail-closed)
    all_strings = "\n".join([
        text,
        doing or "",
        refs["branch"] or "",
        "\n".join(str(x) for x in refs["prs"]),
        "\n".join(refs["threads"]),
        "\n".join(refs["files"]),
    ])
    fired = detect_fn(all_strings, disabled_detectors, seat)
    if fired:
        return None, content_sha, "redaction:" + ",".join(sorted(fired))

    if prev_context_id:
        record_uuid = "context:%s:%s:%s" % (seat, content_sha[:16], prev_context_id[:8])
    else:
        record_uuid = "context:%s:%s" % (seat, content_sha[:16])
    cid = hashlib.sha256(("v1|%s|0" % record_uuid).encode("utf-8")).hexdigest()[:32]

    clean_dialect = re.sub(r"[^a-z_]+", "_", str(dialect).lower()).strip("_")[:31] or "unknown"
    card = {
        "schema": "card.v1",
        "id": cid,
        "type": "Context",
        "owner": seat,
        "seat": seat,
        "date": card_time[:10],
        "time": card_time,
        "status": "active",
        "text": text,
        "text_sha256": text_sha,
        "truncated": truncated,
        "loader_version": loader_version,
        "attribution": {
            "speaker": "agent",
            "quoted_author": None,
            "extraction": "%s/context_digest" % clean_dialect,
            "confirmation": "typed",
        },
        "source": {
            "record_uuid": record_uuid,
            "block_index": 0,
            "source_sha256": content_sha,
            "kind": "context_digest",
            "first_hand": False,
            "sessions": list(snapshot.get("sessions") or ()),
            "files": list(snapshot.get("source_files") or ()),
        },
        "refs": refs,
    }
    if prev_context_id and prev_context_id != cid:
        card["supersedes"] = ["card/%s/%s" % (seat, prev_context_id)]
    # The server refuses a value over MAX_CARD_BYTES, so a card that would be refused is never emitted: the session
    # list is cut to its bound, then shortened from the end until the card fits (#374, Codex P2: 221 mapped Codex
    # sessions made a 9,894-byte card).
    del card["source"]["sessions"][MAX_CONTEXT_SESSIONS:]
    if not fit_card_sessions(card):
        return None, content_sha, "oversize:%d" % _card_bytes(card)
    return card, content_sha, None


def _card_bytes(card):
    """The card's size as the server measures it (apps/cards/validation.py: compact JSON, UTF-8)."""
    return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def fit_card_sessions(card, protect=()):
    """Shorten card["source"]["sessions"] from the end, never removing one in `protect`, until the card fits
    MAX_CARD_BYTES. True when it fits. Every card the loader writes passes through this (#374, Codex P2: a Context
    card and a parent's Candidate card each went over the server's limit once many Codex sessions were added)."""
    src = card.get("source") if isinstance(card.get("source"), dict) else {}
    sessions = src.get("sessions") if isinstance(src.get("sessions"), list) else []
    while _card_bytes(card) > MAX_CARD_BYTES:
        for i in range(len(sessions) - 1, -1, -1):
            if sessions[i] not in protect:
                del sessions[i]
                break
        else:
            return False
    return True
