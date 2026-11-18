"""Bounded local rollout telemetry; never emits transcript text or changes mail state.

Only installed-version-qualified accounting may supply a compact limit. A model
window is deliberately not an input to the ratio policy.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .large_record import RELEVANT_TYPES, feed, header


def positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass
class Metrics:
    thread: str
    epoch: str
    identity: str = ""
    offset: int = 0
    anchor: str = ""
    partial: str = ""  # bounded base64, local-only; never in public status
    discard: bool = False
    large: dict = field(default_factory=dict)  # bounded validator/header; private
    degraded: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    task: str | None = None
    compactions: list[float] = field(default_factory=list)
    last_stamp: float | None = None
    floor_pending: bool = False
    floors: list[dict] = field(default_factory=list)
    current_input: int | None = None
    total_compactions: int = 0
    file_bytes: int = 0
    last_event: float | None = None
    generation: int = 0
    warning_sent: bool = False
    request_sent: bool = False
    source_verified: bool = False
    runtime_model: str | None = None

    def public(self):
        data = asdict(self)
        for key in ("partial", "large", "seen", "anchor", "identity"):
            data.pop(key)
        data["provenance"] = "local-rollout-telemetry; not operator authority"
        return data


class Scanner:
    def __init__(self, state: Metrics, *, scan_bytes=262144, record_bytes=262144):
        if not 1 <= record_bytes <= scan_bytes <= 1048576:
            raise ValueError("invalid scan bounds")
        self.state, self.scan_bytes, self.record_bytes = state, scan_bytes, record_bytes

    def unknown(self, reason):
        if reason not in self.state.degraded:
            self.state.degraded = (self.state.degraded + [reason])[-16:]
        # A gap cannot leave two old floors looking like consecutive samples.
        self.state.floors.clear()
        self.state.compactions.clear()
        self.state.floor_pending = False

    def scan(self, path: Path, epoch: str):
        s = self.state
        if epoch != s.epoch:
            s.epoch = epoch
            s.floors.clear()
            s.compactions.clear()
            s.floor_pending = False
            s.current_input = None
        try:
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = f"{stat.st_dev}:{stat.st_ino}"
                s.file_bytes = stat.st_size
                changed = s.identity and (s.identity != identity or stat.st_size < s.offset)
                if s.offset and not changed:
                    stream.seek(max(0, s.offset - 64))
                    changed = hashlib.sha256(stream.read(min(64, s.offset))).hexdigest() != s.anchor
                if changed:
                    # Skip the historical replacement. Only later appended events
                    # may trigger a new action. Mail offsets are never touched.
                    self.unknown("rollout-replaced-or-truncated")
                    s.offset, s.partial, s.discard = stat.st_size, "", False
                    s.large = {}
                    s.source_verified = False
                    s.identity = identity
                    stream.seek(max(0, s.offset - 64))
                    s.anchor = hashlib.sha256(stream.read(min(64, s.offset))).hexdigest()
                    return s.public()
                s.identity = identity
                stream.seek(s.offset)
                chunk = stream.read(self.scan_bytes)
                s.offset += len(chunk)
                stream.seek(max(0, s.offset - 64))
                s.anchor = hashlib.sha256(stream.read(min(64, s.offset))).hexdigest()
        except OSError:
            self.unknown("rollout-unreadable")
            return s.public()
        try:
            pending = base64.b64decode(s.partial, validate=True)
            if len(pending) > self.record_bytes:
                raise ValueError()
        except (ValueError, TypeError):
            pending = b""
            self.unknown("invalid-partial-state")
        for part in chunk.splitlines(keepends=True):
            ended = part.endswith(b"\n")
            if s.discard:
                if ended:
                    s.discard = False
                continue
            if s.large:
                self.large_part(part, ended)
                continue
            pending += part
            if len(pending) > self.record_bytes:
                try:
                    event = header(pending)
                    s.large = {"event": event, "validator": {
                        "guard_payload_type": event["type"] == "event_msg"}}
                    self.large_part(pending, ended)
                except (ValueError, TypeError, IndexError, RecursionError, MemoryError):
                    self.unknown("oversized-record")
                    s.large = {}
                    s.discard = not ended
                pending = b""
                continue
            if ended:
                self.record(pending)
                pending = b""
        s.partial = base64.b64encode(pending).decode("ascii")
        return s.public()

    def large_part(self, raw, ended):
        s = self.state
        try:
            feed(s.large["validator"], raw, final=ended)
            if ended:
                event = s.large["event"]
                s.large = {}
                # Only metadata reaches the ordinary accounting path.
                self.record(json.dumps(event).encode())
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, MemoryError):
            self.unknown("oversized-record")
            s.large = {}
            s.discard = not ended

    def record(self, raw: bytes):
        s = self.state
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError()
            kind = event.get("type")
            if kind == "event_msg":
                kind = payload.get("type")
            if kind not in RELEVANT_TYPES:
                return
            ts = stamp(event.get("timestamp"))
            if ts is None:
                raise ValueError()
            # Equivalent compaction forms share timestamp and task identity.
            category = "compacted" if kind in {"compacted", "context_compacted"} else kind
            signature = hashlib.sha256(json.dumps([category, ts, s.task if category == "compacted" else None,
                None if category == "compacted" else payload], sort_keys=True).encode()).hexdigest()
            if signature in s.seen:
                return
            s.seen = (s.seen + [signature])[-256:]
            if s.last_stamp is not None and ts < s.last_stamp:
                self.unknown("clock-rollback")
            s.last_stamp = s.last_event = ts
            if kind == "session_meta":
                if payload.get("id") != s.thread:
                    self.unknown("ambiguous-thread")
                    s.source_verified = False
                else:
                    s.source_verified = True
            elif kind == "turn_context":
                model = payload.get("model")
                if not isinstance(model, str) or not model:
                    self.unknown("unknown-runtime-model")
                elif s.runtime_model and s.runtime_model != model:
                    self.unknown("runtime-model-changed-requalification-required")
                s.runtime_model = model
            elif kind == "task_started":
                s.task = payload.get("turn_id")
                s.compactions.clear()
            elif kind in {"task_complete", "turn_aborted"}:
                s.task = None
                s.compactions.clear()
            elif category == "compacted":
                s.total_compactions += 1
                s.compactions = [t for t in s.compactions if ts - 600 <= t <= ts][-31:] + [ts]
                if s.floor_pending:
                    # An unmeasured compaction breaks the consecutive-floor
                    # predicate; retain the independent rate evidence.
                    s.floors.clear()
                s.floor_pending = True
            elif kind == "token_count":
                info = payload.get("info") or {}
                usage = info.get("last_token_usage") or {}
                count = usage.get("input_tokens")
                if not positive(count):
                    return  # no positive post-compaction sample yet => UNKNOWN
                s.current_input = int(count)
                if s.floor_pending:
                    body = usage.get("accounted_body_tokens")
                    s.floors = (s.floors + [{"input": int(count), "body": body if positive(body) else None,
                                            "timestamp": ts, "provenance": "UNKNOWN"}])[-2:]
                    s.floor_pending = False
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError,
                RecursionError, MemoryError):
            # Byte bounds do not bound decoder nesting or allocation. Keep the
            # cursor recoverable and preserve the durable notification history.
            self.unknown("malformed-record")


@dataclass(frozen=True)
class Accounting:
    limit: int | None = None
    scope: str = "UNKNOWN"
    qualified_epoch: str | None = None


def decision(s: Metrics, accounting: Accounting, *, now: float, cooldown=False, stale_s=300):
    """Pure policy. Caller durably records warning/request before exposing it."""
    if not math.isfinite(now):
        return "HOLD", "invalid-clock"
    if s.offset < s.file_bytes:
        return "UNKNOWN", "scan-backlog"
    if not s.source_verified:
        return "UNKNOWN", "unverified-rollout-source"
    if s.partial or s.discard or s.large:
        return "UNKNOWN", "incomplete-record"
    if s.last_event is None or not 0 <= now - s.last_event <= stale_s:
        return "UNKNOWN", "stale-or-missing-telemetry"
    # Any parse gap poisons the epoch; qualification must explicitly reset it.
    if s.degraded:
        return "UNKNOWN", s.degraded[-1]
    candidates = [("OK", "no-trigger")]
    if positive(accounting.limit) and accounting.qualified_epoch == s.epoch:
        reserve = max(16000, .2 * accounting.limit)
        if reserve >= accounting.limit:
            return "HOLD", "unusable-budget"
        key = {"total": "input", "body_after_prefix": "body"}.get(accounting.scope)
        samples = [f[key] for f in s.floors] if key else []
        if len(samples) == 2 and all(positive(n) for n in samples):
            if min(samples) >= accounting.limit - reserve:
                candidates.append(("REQUEST", "floor-reserve"))
            elif min(samples) >= accounting.limit - reserve - .05 * accounting.limit:
                candidates.append(("WARN", "floor-warning"))
    recent = [t for t in s.compactions if now - 600 <= t <= now]
    if len(recent) >= 3:
        candidates.append(("REQUEST", "same-task-compaction-rate") if s.task else ("WARN", "unknown-task-rate"))
    if s.file_bytes >= 150000000:
        candidates.append(("REQUEST", "rollout-size-maintenance"))
    # Independent triggers can escalate, never downgrade. Equal severity keeps
    # the first reason in floor, compaction-rate, rollout-size order.
    severity = {"OK": 0, "WARN": 1, "REQUEST": 2}
    action, reason = max(candidates, key=lambda candidate: severity[candidate[0]])
    if action in {"REQUEST", "WARN"} and cooldown:
        return "HOLD", "trigger-during-cooldown"
    if (action == "REQUEST" and s.request_sent) or (action == "WARN" and s.warning_sent):
        return "OK", "already-recorded-this-generation"
    return action, reason
