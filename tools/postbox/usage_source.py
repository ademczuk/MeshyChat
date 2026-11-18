"""A seat's usage snapshot for the turn report (seat-state v2.1 moved into the turn report: kxxx,
memory-bxxxxx GO afd116, antigravity seams a24851; server side #525, migration 056).

Only the fields apps/peers/turn.validate_usage stores ever leave here: as_of, model, context_used_pct and up to four
limits of {window, used_pct, resets_at, source}. Nothing else from the saved input (cwd, paths, session ids, cost,
transcript names) is copied.

Sources, per identity, named in the postbox config (opt in; no setting means no usage):
    "usage": {"statusline_file": "<path>"}
The file is the status-line input the seat's own status-line script saves. It must be ONE seat's file, never one
shared by several seats on a box (memory-bxxxxx, point 1). as_of is the file's mtime: when the numbers were
OBSERVED, not when the report goes (afd116's FIX), so an idle seat's numbers go stale on time.

Codex (account/rateLimits/read) is not read here yet: such a seat sends no usage, never a guessed one.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat

MAX_FILE_BYTES = 64 * 1024
MAX_AGE_SECONDS = 23 * 3600  # the server refuses an as_of over a day old; stop a margin before it
WINDOWS = ("five_hour", "seven_day")
# apps/peers/turn.py _USAGE_MODEL: a model id the server refuses would refuse the whole report (pxx P2-1)
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}")
MOVE_POINTS = 5.0  # a limit or context move this large sends at once (29cf38, inside the 30 s tick: a24851 1)


def _pct(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if 0.0 <= value <= 100.0 else None


def _resets(value):
    """Claude Code gives resets_at as epoch seconds; an ISO string with an offset is kept as it is."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and 0 < value < 1e11:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
    if isinstance(value, str) and len(value) <= 40:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.isoformat() if parsed.tzinfo else None
    return None


def from_statusline(path: str, *, now: float | None = None) -> dict | None:
    """The snapshot in one saved status-line input, or None when there is none to send.

    None (no usage) for a missing file, a stale one or a source that is not a plain file. A file that is present but
    unreadable or malformed raises, so the caller can report the fault while the turn report still goes (a24851 3).
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("usage_source_not_a_file")
    if st.st_size > MAX_FILE_BYTES:
        raise ValueError("usage_source_too_big")
    now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
    if now - st.st_mtime > MAX_AGE_SECONDS:
        return None
    with open(path, "rb") as fh:
        opened = os.fstat(fh.fileno())
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            raise ValueError("usage_source_changed")
        raw = fh.read(MAX_FILE_BYTES + 1)
    doc = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError("usage_source_shape")
    model = doc.get("model")
    model_id = model.get("id") if isinstance(model, dict) else None
    if not (isinstance(model_id, str) and MODEL_ID.fullmatch(model_id)):
        model_id = None  # sent as null: the numbers still go
    ctx = doc.get("context_window")
    context = _pct(ctx.get("used_percentage")) if isinstance(ctx, dict) else None
    limits = []
    rl = doc.get("rate_limits")
    if isinstance(rl, dict):
        for window in WINDOWS:
            entry = rl.get(window)
            if isinstance(entry, dict):
                limits.append({"window": window, "used_pct": _pct(entry.get("used_percentage")),
                               "resets_at": _resets(entry.get("resets_at")), "source": "statusline"})
    as_of = dt.datetime.fromtimestamp(min(st.st_mtime, now), dt.timezone.utc).isoformat()
    return {"as_of": as_of, "model": model_id, "context_used_pct": context, "limits": limits}


def read(ident: dict, *, now: float | None = None) -> dict | None:
    """This identity's snapshot from its configured source, or None when it has no source configured."""
    cfg = ident.get("usage")
    if not isinstance(cfg, dict):
        return None
    path = cfg.get("statusline_file")
    if isinstance(path, str) and path:
        return from_statusline(path, now=now)
    return None


def moved(old: dict | None, new: dict | None) -> bool:
    """Whether `new` differs from the last snapshot sent enough to send now, not at the next 120 s refresh.

    A change of model, a limit window appearing or going, or a used_pct (limit or context) moving MOVE_POINTS or more.
    """
    if new is None:
        return False
    if old is None:
        return True
    if old.get("model") != new.get("model"):
        return True

    def far(a, b):
        if (a is None) != (b is None):
            return True
        return a is not None and abs(a - b) >= MOVE_POINTS

    if far(old.get("context_used_pct"), new.get("context_used_pct")):
        return True
    o = {lim["window"]: lim.get("used_pct") for lim in old.get("limits") or []}
    n = {lim["window"]: lim.get("used_pct") for lim in new.get("limits") or []}
    if set(o) != set(n):
        return True
    return any(far(o[w], n[w]) for w in n)
