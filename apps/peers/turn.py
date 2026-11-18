"""Advisory token-bound turn reports, isolated from presence beats and notifications."""
from __future__ import annotations

import collections
import datetime as dt
import math
import re
import threading
import time

from psycopg.types.json import Jsonb

from . import errors

REPORT_TTL_SECONDS = 600
REPORTS_PER_MINUTE = 4
UNSURFACED_CLOCK_TOLERANCE_SECONDS = 60
TRACKED_MAX = 4096
#: Seat usage (seat-state v2.1 moved into the turn report, memory-bxxxxx): only these fields are stored.
USAGE_KEYS = frozenset({"as_of", "model", "context_used_pct", "limits"})
USAGE_LIMIT_KEYS = frozenset({"window", "used_pct", "resets_at", "source"})
USAGE_SOURCES = frozenset({"statusline", "codex-app-server", "unknown"})
USAGE_MAX_LIMITS = 4
#: An observation older than this is refused: the seat sends no usage rather than an old one.
USAGE_MAX_AGE_SECONDS = 86400
_USAGE_WINDOW = re.compile(r"(?:five_hour|seven_day|[1-9][0-9]{0,5}m)")
_USAGE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}")
_reports = collections.OrderedDict()
_lock = threading.Lock()
_clock = time.monotonic


def admit(peer_id) -> None:
    """Four reports in a rolling minute per authenticated peer, in this serving process.

    Full live tables refuse a new peer; evicting an active key would reset its quota.
    """
    now = _clock()
    key = str(peer_id)
    with _lock:
        if key not in _reports and len(_reports) >= TRACKED_MAX:
            for expired in [peer for peer, times in _reports.items() if not times or now - times[-1] >= 60]:
                _reports.pop(expired)
            if len(_reports) >= TRACKED_MAX:
                raise errors.ApiError(429, "E_RATE_LIMITED", "turn report peer table is full")
        times = [stamp for stamp in _reports.get(key, ()) if now - stamp < 60]
        if len(times) >= REPORTS_PER_MINUTE:
            raise errors.ApiError(429, "E_RATE_LIMITED", "turn reports are limited to four per minute per peer")
        times.append(now)
        _reports[key] = times
        _reports.move_to_end(key)


def validate(body: dict, *, now: dt.datetime | None = None) -> tuple[str, dt.datetime]:
    # usage rides on either shape (the two-field one of seats without a notice tracker included), never alone.
    if (set(body) - {"usage"} not in ({"state", "item_at"},
                                      {"state", "item_at", "unsurfaced", "unsurfaced_oldest_at", "unsurfaced_since"})
            or body.get("state") not in ("open", "closed")):
        raise errors.validation("turn needs state/item_at and optionally count, oldest, since and usage fields")
    raw = body.get("item_at")
    if not isinstance(raw, str) or len(raw) > 64:
        raise errors.validation("item_at must be an ISO-8601 timestamp with a timezone")
    try:
        stamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("timezone required")
        stamp = stamp.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        raise errors.validation("item_at must be an ISO-8601 timestamp with a timezone")
    now = now or dt.datetime.now(dt.timezone.utc)
    if stamp > now + dt.timedelta(seconds=60):
        raise errors.validation("item_at must not be more than 60 seconds in the future")
    return body["state"], stamp


def validate_unsurfaced(body, *, now=None):
    """Count and oldest plus an explicit coverage baseline; -1 means incomplete."""
    raw_since = body.get("unsurfaced_since")
    count_value = body.get("unsurfaced")
    if count_value is None:
        if raw_since is not None:
            raise errors.validation("no baseline without an observation")
    else:
        try:
            if not isinstance(raw_since, str) or len(raw_since) > 64:
                raise ValueError("timestamp required")
            since = dt.datetime.fromisoformat(raw_since.replace("Z", "+00:00"))
            if since.utcoffset() is None or since > (now or dt.datetime.now(dt.timezone.utc)) + dt.timedelta(seconds=60):
                raise ValueError("invalid baseline")
        except (ValueError, OverflowError):
            raise errors.validation("unsurfaced_since needs a timestamp with timezone, not future")
    count, raw = body.get("unsurfaced"), body.get("unsurfaced_oldest_at")
    if count is not None and (type(count) is not int or not -1 <= count <= 2048):
        raise errors.validation("unsurfaced must be null, -1 (incomplete), or 0..2048")
    if count in (None, 0):
        if raw is not None:
            raise errors.validation("no oldest timestamp without a pending observation")
        return count, None
    if raw is None and count == -1:
        return count, None
    if not isinstance(raw, str) or len(raw) > 64:
        raise errors.validation("unsurfaced_oldest_at must be a timestamp with timezone")
    try:
        stamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("timezone required")
        stamp = stamp.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        raise errors.validation("unsurfaced_oldest_at must be a timestamp with timezone")
    now = now or dt.datetime.now(dt.timezone.utc)
    # The reporter applies 300 seconds on its clock. Permit the same 60-second
    # skew as item_at/since so a boundary crossing cannot reject the turn pair.
    if (now - stamp).total_seconds() < 300 - UNSURFACED_CLOCK_TOLERANCE_SECONDS:
        raise errors.validation("unsurfaced notices must be 300 seconds old within 60 seconds of clock skew")
    return count, stamp


def _aware(raw, name: str) -> dt.datetime:
    if not isinstance(raw, str) or len(raw) > 64:
        raise errors.validation(name + " must be an ISO-8601 timestamp with a timezone")
    try:
        stamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("timezone required")
        return stamp.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        raise errors.validation(name + " must be an ISO-8601 timestamp with a timezone")


def _pct(value, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise errors.validation(name + " must be null or a number from 0 to 100")
    return round(float(value), 2)


def validate_usage(body, *, now=None) -> tuple[dict | None, dt.datetime | None]:
    """The seat's usage snapshot and when the seat OBSERVED it (usage.as_of), or (None, None) without one.

    as_of is the observation time, not the time of this report (memory-bxxxxx): a seat that keeps sending one
    old snapshot must go stale on time. Only the listed fields are accepted; any other field refuses the report,
    so nothing else (cost, paths, session ids) can be stored by mistake.
    """
    usage = body.get("usage")
    if usage is None:
        return None, None
    if not isinstance(usage, dict) or not set(usage) <= USAGE_KEYS or "as_of" not in usage:
        raise errors.validation("usage is an object of as_of and optionally model, context_used_pct and limits")
    now = now or dt.datetime.now(dt.timezone.utc)
    as_of = _aware(usage["as_of"], "usage.as_of")
    if as_of > now + dt.timedelta(seconds=60):
        raise errors.validation("usage.as_of must not be more than 60 seconds in the future")
    if (now - as_of).total_seconds() > USAGE_MAX_AGE_SECONDS:
        raise errors.validation("usage.as_of is over a day old: send no usage rather than an old one")
    model = usage.get("model")
    if model is not None and (not isinstance(model, str) or not _USAGE_MODEL.fullmatch(model)):
        raise errors.validation("usage.model must be null or a model id")
    raw_limits = usage.get("limits")
    raw_limits = [] if raw_limits is None else raw_limits
    if not isinstance(raw_limits, list) or len(raw_limits) > USAGE_MAX_LIMITS:
        raise errors.validation("usage.limits is a list of at most %d windows" % USAGE_MAX_LIMITS)
    limits, seen = [], set()
    for lim in raw_limits:
        if not isinstance(lim, dict) or not set(lim) <= USAGE_LIMIT_KEYS or not {"window", "source"} <= set(lim):
            raise errors.validation("each usage limit is window and source, optionally used_pct and resets_at")
        window, source = lim["window"], lim["source"]
        if not isinstance(window, str) or not _USAGE_WINDOW.fullmatch(window) or window in seen:
            raise errors.validation("usage limit windows are five_hour, seven_day or <minutes>m, each once")
        if not isinstance(source, str) or source not in USAGE_SOURCES:      # a list or object is unhashable: 400, not 500
            raise errors.validation("usage limit source is statusline, codex-app-server or unknown")
        resets = lim.get("resets_at")
        seen.add(window)
        limits.append({"window": window, "used_pct": _pct(lim.get("used_pct"), "usage limit used_pct"),
                       "resets_at": None if resets is None else _aware(resets, "usage limit resets_at").isoformat(),
                       "source": source})
    return {"model": model, "context_used_pct": _pct(usage.get("context_used_pct"), "usage.context_used_pct"),
            "limits": limits}, as_of


def report(conn, peer_id, body: dict) -> dict:
    state, item_at = validate(body)
    count, oldest_at = validate_unsurfaced(body)
    usage, usage_at = validate_usage(body)
    since = dt.datetime.fromisoformat(body["unsurfaced_since"].replace("Z", "+00:00")) if count is not None else None
    admit(peer_id)
    # A report without usage leaves the stored snapshot as it is (memory-bxxxxx: one failed read must not blank
    # a row; usage_at already says how old it is), and an older snapshot never replaces a newer one.
    keep_usage = "EXCLUDED.usage IS NULL OR (mesh_presence.usage_at IS NOT NULL AND EXCLUDED.usage_at < mesh_presence.usage_at)"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mesh_presence (peer_id, turn_state, turn_item_at, turn_reported_at, unsurfaced, unsurfaced_oldest_at, unsurfaced_since, "
            "usage, usage_at) "
            "VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s) ON CONFLICT (peer_id) DO UPDATE SET "
            "turn_state = EXCLUDED.turn_state, turn_item_at = EXCLUDED.turn_item_at, turn_reported_at = NOW() "
            ", unsurfaced = EXCLUDED.unsurfaced, unsurfaced_oldest_at = EXCLUDED.unsurfaced_oldest_at, unsurfaced_since = EXCLUDED.unsurfaced_since "
            ", usage = CASE WHEN " + keep_usage + " THEN mesh_presence.usage ELSE EXCLUDED.usage END "
            ", usage_at = CASE WHEN " + keep_usage + " THEN mesh_presence.usage_at ELSE EXCLUDED.usage_at END "
            "WHERE mesh_presence.turn_item_at IS NULL OR EXCLUDED.turn_item_at >= mesh_presence.turn_item_at "
            "RETURNING peer_id",
            (str(peer_id), state, item_at, count, oldest_at, since, None if usage is None else Jsonb(usage), usage_at))
        applied = cur.fetchone() is not None
    return {"ok": True, "applied": applied}


def read(conn, peer_id) -> dict:
    """A fresh primary-key lookup even when expensive work facts are held for 60 seconds.

    Otherwise a stopped reporter could remain live for eleven minutes. This lookup never
    changes states_of or the default recipients answer.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT turn_state, turn_item_at, turn_reported_at, unsurfaced, unsurfaced_oldest_at, unsurfaced_since, "
                    "usage, usage_at FROM mesh_presence "
                    "WHERE peer_id = %s AND turn_reported_at > NOW() - INTERVAL '600 seconds'", (str(peer_id),))
        row = cur.fetchone()
    if row is None:
        return {"turn_state": None, "turn_item_at": None, "turn_reported_at": None,
                "unsurfaced": None, "unsurfaced_oldest_at": None, "unsurfaced_since": None,
                "usage": None, "usage_at": None}
    state, item_at, reported_at, count, oldest_at, since, usage, usage_at = row
    return {"turn_state": state, "turn_item_at": item_at.isoformat(), "turn_reported_at": reported_at.isoformat(),
            "unsurfaced": count, "unsurfaced_oldest_at": oldest_at.isoformat() if oldest_at else None,
            "unsurfaced_since": since.isoformat() if since else None,
            "usage": usage if isinstance(usage, dict) else None, "usage_at": usage_at.isoformat() if usage_at else None}
