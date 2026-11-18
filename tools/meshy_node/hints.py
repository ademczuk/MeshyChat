"""Pre-send graph collision hints client (`POST /v1/mesh/hints`, Pillar 4).

Advisory-only pre-send lookup for new-thread drafts (`in_reply_to is None`):
- Authenticates with a fresh peer JWT + single-use Ed25519 DPoP proof (`_proved_headers`),
  matching production `MESHY_DPOP_ENFORCE=True`.
- Bounded by a strict wall-clock budget (`timeout_s`, default 0.8s) and response byte cap.
- Fails OPEN (`{"ran": False, "reason": ..., "hints": []}`) on HTTP 404/405 (pre-deploy
  server), 429, 401/403, 5xx, malformed payloads, connection errors, or timeouts so
  sending MeshyChat mail is never delayed or blocked by hints availability.
- Strips all `RELAY_FIELDS` and raw body/text fields from unsigned hint payloads, and
  escapes control/format/newline characters before rendering a compact `<= 3`-line notice.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import node as _node

HINTS_PATH = "/v1/mesh/hints"
DEFAULT_TIMEOUT_S = 0.8
DEFAULT_K = 3
MAX_K = 10
MIN_DRAFT_CHARS = 40
MAX_DRAFT_CHARS = 4000
MAX_STATE_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536
MAX_LINE_CHARS = 180
MAX_TITLE_CHARS = 110

DEFAULT_THRESHOLDS: dict[str, float] = {
    "decisions": 2.0, "mesh_chat": 1.3, "transcript_context": 1.5, "min_direction": 0.65,
}
ALLOWED_SOURCES = frozenset({"decisions", "mesh_chat", "transcript_context"})
OPEN_ASK_KINDS = frozenset({"question", "request", "ask", "proposal"})
FORBIDDEN_HINT_FIELDS = frozenset({"body", "text", "raw_body", "content", "transcript", "snippet"})

_NON_ACTIONABLE_RE = re.compile(
    r"^\s*(?:ack(?:nowledged)?|got\s+it|received|thanks|thank\s+you|ty|noted|ok(?:ay)?|"
    r"lgtm|done|will\s+do|on\s+it|copy(?:\s+that)?|sounds\s+good|roger|standing\s+by)[\s.!?,;:]*$",
    re.IGNORECASE,
)


def is_non_actionable_receipt(body: str) -> bool:
    trimmed = body.strip()
    return not trimmed or (len(trimmed) <= 80 and bool(_NON_ACTIONABLE_RE.match(trimmed)))


def _clean_single_line(raw: Any, *, limit: int = MAX_TITLE_CHARS) -> str:
    if raw is None:
        return ""
    collapsed = " ".join(str(raw).replace("\r", " ").replace("\n", " ").split())
    collapsed = " ".join(collapsed.replace("\u2014", " - ").replace("\u2013", " - ").replace("`", "").split())
    if collapsed.startswith("#"):
        collapsed = collapsed.lstrip("#").strip()
    safe = _node._one_line(collapsed)
    return safe if len(safe) <= limit else (safe[:limit] if limit <= 3 else safe[: limit - 3].rstrip() + "...")


def _clean_token(raw: Any, *, limit: int = 128) -> str:
    if not isinstance(raw, str) or "\x00" in raw:
        return ""
    return _clean_single_line(raw.strip(), limit=limit)


def _normalize_labels(raw: Iterable[str] | str | None, *, max_items: int = 64, max_len: int = 128) -> list[str]:
    if raw is None or isinstance(raw, (bytes, bytearray, dict)) or not isinstance(raw, (str, Iterable)):
        return []
    parts = [p.strip() for p in raw.split(",")] if isinstance(raw, str) else [
        p.strip() for item in raw if isinstance(item, str) for p in item.split(",")
    ]
    out: list[str] = []
    for p in parts:
        if p and "\x00" not in p:
            out.append(p[:max_len])
            if len(out) >= max_items:
                break
    return out


def _normalize_closes(raw: Iterable[str] | str | None) -> list[str]:
    if raw is None or isinstance(raw, (bytes, bytearray, dict)) or not isinstance(raw, (str, Iterable)):
        return []
    seq = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in seq:
        if isinstance(item, str) and item.strip() and "\x00" not in item:
            out.append(item.strip()[:256])
            if len(out) >= 32:
                break
    return out


def _fail_open(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ran": False, "reason": reason, "hints": [], **extra}


def _resolve_node(target: Any, *, state_file: Path | str | None = None) -> tuple[Path | None, dict[str, Any] | None, Any]:
    """Unpack `(state_file, state)`, `state` dict, state file path, or duck-typed client."""
    custom_post = getattr(target, "post_hints", None)
    if callable(custom_post):
        return (Path(state_file) if state_file is not None else None, None, custom_post)
    if isinstance(target, (tuple, list)) and len(target) == 2 and isinstance(target[1], dict):
        p_raw, st = target
        return (Path(p_raw) if p_raw is not None else (Path(state_file) if state_file is not None else None), st, None)
    if isinstance(target, dict):
        sf = state_file or target.get("_state_file")
        return (Path(sf) if sf is not None else None, target, None)
    if isinstance(target, (str, os.PathLike)):
        p = Path(target)
        with open(p, "rb") as fh:
            raw = fh.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise _node.Refused("state file exceeds maximum size")
        parsed = json.loads(raw.decode("utf-8"))
        base_url = parsed.get("base_url") if isinstance(parsed, dict) else None
        if not isinstance(parsed, dict) or parsed.get("schema") != _node.STATE_SCHEMA or not isinstance(base_url, str) or not base_url.strip():
            raise _node.Refused("invalid state schema or base_url")
        parsed["base_url"] = _node.normalize_base_url(base_url)
        return (p, parsed, None)
    obj_state = getattr(target, "state", None)
    if isinstance(obj_state, dict):
        sf = state_file if state_file is not None else getattr(target, "state_file", None)
        return (Path(sf) if sf is not None else None, obj_state, None)
    raise _node.Refused("unsupported node handle")


def _classify_server_status(status: int) -> str:
    if status in (404, 405, 501):
        return "unsupported_route"
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return f"auth_{status}"
    if 500 <= status <= 599:
        return f"server_{status}"
    return f"http_{status}"


def _sanitize_thresholds(raw: Any) -> dict[str, float]:
    merged = dict(DEFAULT_THRESHOLDS)
    if isinstance(raw, dict):
        for key in ("decisions", "mesh_chat", "transcript_context", "min_direction"):
            val = raw.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val)):
                merged[key] = float(val)
    return merged


def _sanitize_hint(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    source = _clean_token(item.get("source"), limit=32)
    key = _clean_token(item.get("key"), limit=256)
    if source not in ALLOWED_SOURCES or not key:
        return None
    score_raw = item.get("rerank_score")
    score = (
        round(float(score_raw), 6)
        if not isinstance(score_raw, bool) and isinstance(score_raw, (int, float)) and math.isfinite(float(score_raw))
        else None
    )
    tid = item.get("thread_id")
    cleaned: dict[str, Any] = {
        "source": source, "namespace": _clean_token(item.get("namespace"), limit=128), "key": key,
        "kind": _clean_token(item.get("kind"), limit=64), "author": _clean_token(item.get("author"), limit=64),
        "thread_id": _clean_token(tid, limit=256) if isinstance(tid, str) and tid.strip() else None,
        "title": _clean_single_line(item.get("title") or "", limit=MAX_TITLE_CHARS), "rerank_score": score,
    }
    for forbidden in FORBIDDEN_HINT_FIELDS | _node.RELAY_FIELDS:
        cleaned.pop(forbidden, None)
    return cleaned


def query_send_hints(
    node: Any, *, to: Iterable[str] | str | None = (), cc: Iterable[str] | str | None = None,
    thread_id: str | None = None, in_reply_to: str | None = None, body: str = "", k: int = DEFAULT_K,
    timeout_s: float = DEFAULT_TIMEOUT_S, closes: Iterable[str] | str | None = None,
    sender_label: str | None = None, msg_type: str | None = None, ref: str | None = None,
    state_file: Path | str | None = None, allow_signin: bool = True,
) -> dict[str, Any]:
    """Query `POST /v1/mesh/hints` with DPoP auth; fail OPEN cleanly on any error or timeout."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(float(timeout_s)) or timeout_s <= 0:
        return _fail_open("invalid_timeout")
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        return _fail_open("zero_limit")
    limit_k = min(k, MAX_K)
    if not isinstance(body, str) or "\x00" in body:
        return _fail_open("invalid_body")
    clean_msg_type = (msg_type or "").strip()[:32] if isinstance(msg_type, str) and "\x00" not in msg_type else ""
    if clean_msg_type.lower() == "ack":
        return _fail_open("ack_or_short_draft")
    trimmed = body.strip()
    if len(trimmed) < MIN_DRAFT_CHARS or is_non_actionable_receipt(trimmed):
        return _fail_open("ack_or_short_draft")

    clean_reply = in_reply_to.strip()[:256] if isinstance(in_reply_to, str) and in_reply_to.strip() and "\x00" not in in_reply_to else None
    clean_thread = thread_id.strip()[:256] if isinstance(thread_id, str) and thread_id.strip() and "\x00" not in thread_id else None
    clean_ref = ref.strip()[:256] if isinstance(ref, str) and ref.strip() and "\x00" not in ref else None

    deadline = time.monotonic() + float(timeout_s)
    try:
        path, state, custom_post = _resolve_node(node, state_file=state_file)
    except Exception:  # noqa: BLE001
        return _fail_open("invalid_state")

    resolved_sender = sender_label
    if resolved_sender is None and isinstance(state, dict) and isinstance(state.get("label"), str) and state["label"].strip():
        resolved_sender = state["label"].strip()

    payload: dict[str, Any] = {
        "body": trimmed[:MAX_DRAFT_CHARS],
        "to": _normalize_labels(to),
        "cc": _normalize_labels(cc),
    }
    if clean_thread is not None:
        payload["thread_id"] = clean_thread
    if clean_reply is not None:
        payload["in_reply_to"] = clean_reply
    closes_list = _normalize_closes(closes)
    if closes_list:
        payload["closes"] = closes_list
    if isinstance(resolved_sender, str) and resolved_sender.strip() and "\x00" not in resolved_sender:
        payload["sender_label"] = resolved_sender.strip()[:64]
    if clean_msg_type:
        payload["msg_type"] = clean_msg_type
    if clean_ref is not None:
        payload["ref"] = clean_ref

    if custom_post is not None:
        box_c: dict[str, Any] = {}

        def _run_custom() -> None:
            try:
                box_c["resp"] = custom_post(payload, timeout=max(0.01, deadline - time.monotonic()))
            except BaseException as exc:  # noqa: BLE001
                box_c["err"] = exc

        worker = threading.Thread(target=_run_custom, name="meshy-send-hints", daemon=True)
        worker.start()
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            return _fail_open("timeout")
        if "err" in box_c:
            return _classify_exception(box_c["err"])
        ans = box_c.get("resp")
        if isinstance(ans, tuple) and len(ans) == 2 and isinstance(ans[0], int):
            if ans[0] != 200:
                return _fail_open(_classify_server_status(ans[0]), http_status=ans[0])
            ans = ans[1]
        return _normalize_server_response(ans, limit_k=limit_k, in_reply_to=clean_reply, thread_id=clean_thread)

    assert state is not None
    try:
        _node.require_peer(path or Path("<memory>"), state)
        raw_base = state.get("base_url")
        if not isinstance(raw_base, str) or not raw_base.strip():
            return _fail_open("invalid_state")
        base_url = _node.normalize_base_url(raw_base)
        peer_id = str(state["peer_id"])
        seed = _node._seed(state)
        if len(seed) != 32:
            raise ValueError("signing seed must be 32 bytes")
        state_snap = {**state, "base_url": base_url}
        cached_jwt: str | None = None
        if not _node._expires_soon(state):
            cached_jwt = str(state["jwt"])
        elif isinstance(state.get("jwt"), str) and state["jwt"].strip() and not state.get("jwt_expires_at"):
            cached_jwt = state["jwt"].strip()
        elif not allow_signin:
            return _fail_open("missing_jwt")
    except (_node.Refused, KeyError, ValueError, TypeError):
        return _fail_open("invalid_state")
    except Exception:  # noqa: BLE001
        return _fail_open("connection_error")

    if time.monotonic() >= deadline:
        return _fail_open("timeout")

    box: dict[str, Any] = {}

    def _call_http() -> None:
        try:
            if time.monotonic() >= deadline:
                box["err"] = _node.DeadlinePassed("deadline passed before POST /v1/mesh/hints")
                return
            with _node.finishing_by(deadline):
                jwt = cached_jwt
                if jwt is None:
                    jwt, _ = _node._peer_signin(base_url, peer_id, seed, max_bytes=MAX_RESPONSE_BYTES)
                headers = _node._proved_headers(state_snap, jwt, "POST", HINTS_PATH)
                rem = max(0.01, deadline - time.monotonic())
                box["status"], box["resp"] = _node.http_json(
                    "POST", base_url, HINTS_PATH, body=payload, headers=headers,
                    timeout=rem, max_bytes=MAX_RESPONSE_BYTES,
                )
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc

    worker = threading.Thread(target=_call_http, name="meshy-send-hints", daemon=True)
    worker.start()
    worker.join(timeout=max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        return _fail_open("timeout")
    if "err" in box:
        return _classify_exception(box["err"])
    http_status = box.get("status")
    if not isinstance(http_status, int):
        return _fail_open("connection_error")
    if http_status != 200:
        return _fail_open(_classify_server_status(http_status), http_status=http_status)
    return _normalize_server_response(box.get("resp"), limit_k=limit_k, in_reply_to=clean_reply, thread_id=clean_thread)


def _classify_exception(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, (_node.DeadlinePassed, TimeoutError, socket.timeout)):
        return _fail_open("timeout")
    if isinstance(exc, _node.ServerSaid):
        return _fail_open(_classify_server_status(exc.status), http_status=exc.status)
    if isinstance(exc, _node.AnswerTooLarge):
        return _fail_open("response_too_large")
    if isinstance(exc, _node.Refused):
        return _fail_open("invalid_state")
    return _fail_open("connection_error")


def _normalize_server_response(raw_resp: Any, *, limit_k: int, in_reply_to: str | None, thread_id: str | None) -> dict[str, Any]:
    if not isinstance(raw_resp, dict):
        return _fail_open("malformed_response")
    stripped = _node.strip_relay_fields(raw_resp)
    if not isinstance(stripped, dict):
        return _fail_open("malformed_response")
    enabled = stripped.get("enabled")
    abstained = stripped.get("abstained_reason")
    if enabled is False:
        reason = _clean_token(abstained, limit=64) if isinstance(abstained, str) and abstained.strip() else "disabled"
        return _fail_open(reason, enabled=False)
    if isinstance(abstained, str) and abstained.strip():
        return _fail_open(_clean_token(abstained, limit=64) or "abstained", enabled=bool(enabled), reranked=bool(stripped.get("reranked")))
    raw_hints = stripped.get("hints")
    if not isinstance(raw_hints, list):
        return _fail_open("malformed_response")

    safe_hints: list[dict[str, Any]] = []
    for item in raw_hints:
        clean_item = _sanitize_hint(item)
        if clean_item is not None:
            safe_hints.append(clean_item)
            if len(safe_hints) >= limit_k:
                break
    return {
        "ran": True, "enabled": True, "reranked": bool(stripped.get("reranked")),
        "space": _clean_token(stripped.get("space"), limit=64) if isinstance(stripped.get("space"), str) else None,
        "hints": safe_hints, "truncated": bool(stripped.get("truncated")),
        "thresholds": _sanitize_thresholds(stripped.get("thresholds")),
        "in_reply_to": in_reply_to, "thread_id": thread_id,
    }


def _format_one_hint_line(hint: dict[str, Any]) -> str:
    source = str(hint.get("source") or "")
    key = _clean_token(hint.get("key"), limit=96)
    kind = _clean_token(hint.get("kind"), limit=32)
    author = _clean_token(hint.get("author"), limit=40)
    thread_id = _clean_token(hint.get("thread_id"), limit=64)
    title = _clean_single_line(hint.get("title") or "", limit=96)
    tail = f": {title}" if title else ""
    if source == "decisions":
        where = f" (thread {thread_id})" if thread_id else ""
        line = f"[hint: recent decision] {kind or 'Card'} {key}{where}{tail}"
    elif source == "mesh_chat" and kind.lower() in OPEN_ASK_KINDS:
        who = f" from {author}" if author else ""
        where = f" (thread {thread_id})" if thread_id else ""
        line = f"[hint: open ask] {key}{who}{where}{tail}"
    elif source == "transcript_context":
        who = f" (from {author})" if author else ""
        line = f"[hint: active thread] session context {key}{who}{tail}"
    else:
        target = thread_id or key
        meta_parts = ([key] if thread_id and key else []) + ([f"from {author}"] if author else [])
        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
        line = f"[hint: active thread] {target}{meta}{tail}"
    return _clean_single_line(line, limit=MAX_LINE_CHARS)


def format_send_hints_notice(resp: dict[str, Any] | None, *, in_reply_to: str | None = None, max_lines: int = 3) -> str:
    """Format calibrated `reranked` hints into a compact `<= 3`-line notice for new threads."""
    if not isinstance(resp, dict) or isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines <= 0:
        return ""
    effective_reply = in_reply_to if in_reply_to is not None else resp.get("in_reply_to")
    if (isinstance(effective_reply, str) and effective_reply.strip()) or (effective_reply is not None and not isinstance(effective_reply, str)):
        return ""
    if resp.get("ran") is False or resp.get("enabled") is False or resp.get("abstained_reason") or resp.get("reranked") is not True:
        return ""
    raw_hints = resp.get("hints")
    if not isinstance(raw_hints, list) or not raw_hints:
        return ""

    line_cap = min(max_lines, 3)
    thresholds = _sanitize_thresholds(resp.get("thresholds"))
    lines: list[str] = []
    seen_keys: set[tuple[str, str]] = set()
    for raw_item in raw_hints:
        clean_item = _sanitize_hint(raw_item)
        if clean_item is None or clean_item["rerank_score"] is None:
            continue
        source = clean_item["source"]
        if clean_item["rerank_score"] < thresholds.get(source, DEFAULT_THRESHOLDS.get(source, 2.0)):
            continue
        dedup_key = (source, clean_item["key"])
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        rendered = _format_one_hint_line(clean_item)
        if rendered:
            lines.append(rendered)
            if len(lines) >= line_cap:
                break
    return "\n".join(lines[:3])
