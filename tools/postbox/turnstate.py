"""Pure Claude/Codex/Antigravity/Kimi turn markers from a bounded JSONL tail. No filesystem or network IO."""
from __future__ import annotations

import datetime as dt
import json
import math

TAIL_BYTES = 256 * 1024
SUPPORTED_DIALECTS = ("claude", "codex", "antigravity", "kimi")
EPOCH_MIN_SECONDS = 946684800.0   # 2000-01-01T00:00:00Z
EPOCH_MAX_SECONDS = 4102444800.0  # 2100-01-01T00:00:00Z
LOCAL_COMMAND_PREFIXES = ("<command-name>", "<command-message>", "<local-command-stdout>",
                          "<local-command-stderr>", "<local-command-caveat>")
KIMI_PY_ACTIVITY_TYPES = frozenset({
    "StepBegin", "StepInterrupted", "CompactionEnd", "StatusUpdate", "ContentPart",
    "ToolCall", "ToolCallPart", "ToolResult", "ApprovalRequest", "ApprovalResponse",
    "SubagentEvent", "MCPLoadingBegin", "MCPLoadingEnd", "Notification",
})
KIMI_CODE_OPEN_TYPES = frozenset({
    "turn.prompt", "turn.steer", "prompt.steered", "agent.turn.started", "full_compaction.begin",
})
KIMI_CODE_CLOSED_TYPES = frozenset({
    "turn.ended", "prompt.completed", "agent.turn.ended",
})
KIMI_CODE_ACTIVITY_TYPES = frozenset({
    "llm.request", "context.append_loop_event", "context.append_message",
    "context.apply_compaction", "full_compaction.complete", "token_counting.measured",
    "token_counting.rebased", "token_counting.turn_recorded", "usage.record",
    "turn.step.retrying", "turn.step.interrupted", "tools.update_store",
    "file_history.tracked", "file_history.checkpoint",
})


def timestamp(value) -> dt.datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(dt.timezone.utc) if result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def epoch_timestamp(value, *, ms: bool = False) -> dt.datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    seconds = float(value) / 1000.0 if ms else float(value)
    if not (EPOCH_MIN_SECONDS <= seconds <= EPOCH_MAX_SECONDS):
        return None
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)


def record_timestamp(dialect: str, record: dict) -> dt.datetime | None:
    if dialect in ("claude", "codex"):
        return timestamp(record.get("timestamp"))
    if dialect == "antigravity":
        return timestamp(record.get("created_at"))
    if dialect == "kimi":
        if record.get("type") is None and isinstance(record.get("message"), dict):
            return epoch_timestamp(record.get("timestamp"), ms=False)
        return epoch_timestamp(record.get("time"), ms=True)
    return None


def kind(record: dict) -> tuple[str | None, str | None]:
    """Return (supported dialect, marker). Unknown formats supply no state."""
    step_index = record.get("step_index")
    if isinstance(step_index, int) and not isinstance(step_index, bool) and isinstance(record.get("created_at"), str):
        typ = record.get("type")
        if typ in ("USER_INPUT", "SYSTEM_MESSAGE"):
            return "antigravity", "open"
        if typ == "PLANNER_RESPONSE":
            status = record.get("status")
            tool_calls = record.get("tool_calls")
            if (isinstance(tool_calls, list) and len(tool_calls) > 0) or status == "RUNNING":
                return "antigravity", "open"
            if (tool_calls is None or tool_calls == []) and status in ("DONE", "ERROR", "CANCELLED", "CANCELED"):
                return "antigravity", "closed"
            return "antigravity", None
        if typ in ("GENERIC", "CHECKPOINT", "ERROR_MESSAGE"):
            return "antigravity", None
        return None, None

    typ = record.get("type")
    if typ is None and isinstance(record.get("message"), dict):
        msg = record["message"]
        mtype = msg.get("type")
        if isinstance(msg.get("payload"), dict):
            if mtype in ("TurnBegin", "CompactionBegin"):
                return "kimi", "open"
            if mtype == "TurnEnd":
                return "kimi", "closed"
            if mtype in KIMI_PY_ACTIVITY_TYPES:
                return "kimi", None
        return None, None

    if isinstance(typ, str) and "time" in record:
        if "agentId" in record and record.get("agentId") != "main":
            return None, None
        if typ in KIMI_CODE_OPEN_TYPES:
            return "kimi", "open"
        if typ in KIMI_CODE_CLOSED_TYPES:
            return "kimi", "closed"
        if typ in KIMI_CODE_ACTIVITY_TYPES:
            return "kimi", None

    payload = record.get("payload")
    if typ == "event_msg" and isinstance(payload, dict):
        event = payload.get("type")
        return "codex", ("open" if event == "task_started" else
                         "closed" if event in ("task_complete", "turn_aborted") else None)
    if typ in ("response_item", "turn_context", "token_usage_record", "compacted", "world_state", "session_meta"):
        return "codex", None
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    if typ == "system":
        return "claude", "closed" if record.get("subtype") == "turn_duration" else None
    if typ == "assistant":
        stop = message.get("stop_reason")
        return "claude", ("open" if stop in ("tool_use", "pause_turn") else
                          "closed" if stop in ("end_turn", "stop_sequence", "max_tokens", "refusal") else None)
    if typ == "user":
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"
        ] if isinstance(content, list) else []
        # Local commands are user rows without a model turn. isMeta also marks real
        # hook/channel/scheduled prompts, so only the content tags exclude a row.
        # Exclude local timestamps too, avoiding the growing-model-turn fallback.
        if texts and all(
                isinstance(text, str) and text.lstrip().startswith(LOCAL_COMMAND_PREFIXES) for text in texts):
            return None, None
        interrupted = any(isinstance(text, str) and text.startswith("[Request interrupted") for text in texts)
        return "claude", "closed" if interrupted else "open"
    return None, None


def classify(tail_bytes: bytes) -> tuple[str | None, dt.datetime | None]:
    """Newest supported non-sidechain record time and last timestamped turn marker.

    A tail may start mid-line or finish with an incomplete write. Both fragments are
    ignored. A marker-free tail returns None, leaving growth continuity to the reporter.
    """
    state = None
    newest = marker_at = None
    for raw in tail_bytes[-TAIL_BYTES:].splitlines():
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        dialect, marker = kind(record)
        if dialect is None:
            continue
        stamp = record_timestamp(dialect, record)
        if stamp is None:
            continue
        if newest is None or stamp > newest:
            newest = stamp
        if marker is not None and (marker_at is None or stamp >= marker_at):
            state, marker_at = marker, stamp
    return state, newest
