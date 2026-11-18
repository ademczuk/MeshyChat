"""owedline: one line in a MeshyChat wake naming the asks this seat owes a reply to (roadmap 8.2).

The server keeps, for each label, the asks sent to it that expect a reply and have none yet (GET
/v1/mesh/owed, #73, with the state and since filters of #79). A seat that never asks for that list
learns what it owes only by rereading its mail. So the three renderers put it in front of the seat
when mail arrives, e.g.

    owed (routing only, read each ask for its content): 1 ask(s) of the last 14 days wait on your reply (+1 replied in thread, not linked: `tools.meshy_node owed --state active` lists them): msg/A (from uxxx, 3h).

  The open asks are what waits on a reply; an active one (posted in its thread, not as a reply to it) is counted
  beside them, so the line and a bare `owed` give the same open count (SD 255, pxx E). Before that, every
  listed ask carried its state: msg/A (from uxxx, 3h, open), msg/B (from kxxx, 2d, active).

ROUTING ONLY. Keys, sender labels, ages and the server's state, never message text: an ask's
`done_when` is its sender's words, so the line does not carry it and the cache does not keep it. A
sender label is cut to the characters a label uses (letters, digits and . _ : / @ # + -), as meshy-mod
cuts one, so a label cannot carry a sentence into the seat's context.

A HELP, NEVER A GATE. Nothing here raises, and a lookup that fails or times out gives no line: the
notice goes out exactly as it did without this module, and that identity makes no further calls for
FAIL_BACKOFF_S in this process. So does an answer that does not echo the filters it was sent: a server
from before #79 ignores them and answers the whole list, which must never be taken for the filtered
one. An answer that echoes them but has a row this module cannot read whole (a key that is not a
server key, a state outside open and active, which the filter excluded) is never shown in part: the
line says the list could not be read here, and names the command that lists it (codex's review,
2026-10-04: a silent drop and an empty list read the same).

ONE SNAPSHOT PER IDENTITY PER TTL_S. The answer is kept in <log>.owed.json, shared by the renderers on
one identity, for TTL_S, tied to the state file (its path, size and mtime: a state file replaced by
another seat's does not inherit this one's list) and the server. A file read back passes the same
checks as an answer from the server. A failed lookup is not kept, and a slower lookup never replaces a
newer snapshot another renderer wrote, even when the two save at the same moment: the save reads, checks
and replaces under a lock beside the cache.

WHEN IT IS SHOWN. `wake_line` decides and returns a token; `delivered(token)` records it once the
notice actually went out, so a notice that failed to queue does not count as seen, and a retry asks
again rather than replaying an old list. The channel's open event always carries it (a new session
knows nothing yet). A channel or Codex wake carries it when the list differs from the last one that
consumer was shown, or REPEAT_S after that, since a long session loses old context; when a list it was
shown empties, it says so once. A waiter is one process per wake and is shown a non-empty list every
time (`every=True`): its wake is what the seat acts on next, and it has no other place to see it.
"""
import datetime
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import postbox_os  # noqa: E402  (NO_WINDOW, the creation flag every postbox subprocess uses)

PREFIX = "owed (routing only, read each ask for its content): "
WINDOW_S = 14 * 86_400
STATES = ("open", "active")
LIMIT = 20
SHOW = 6
CALL_TIMEOUT_S = 8
FAIL_BACKOFF_S = 60
TTL_S = 120
#: How long a save waits for the lock beside the cache. Not taken by then means no save, which costs the
#: next renderer one lookup and nothing else.
SAVE_LOCK_WAIT_S = 1.0
REPEAT_S = 4 * 3600
MAX_BYTES = 1600
STDOUT_MAX = 1_000_000
MAX_OWED_CACHE_BYTES = 262_144
LABEL_MAX = 120
#: a key as the server mints it (apps/mesh/normalize.py, _MSG_REF)
MSG_KEY = re.compile(r"msg/[0-9]{13}-[A-Za-z0-9][A-Za-z0-9_.:-]{0,200}")
LIST_CMD = "tools.meshy_node owed"

#: Per log path: the time before which this process makes no calls for that identity. In memory only.
_backoff_until = {}
#: Per (log path, consumer): (the signature last delivered, when). In memory only.
_shown = {}


def _cache_path(ident):
    return ident["log"] + ".owed.json"


def _since(t):
    return datetime.datetime.fromtimestamp(t - WINDOW_S, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label(value):
    """A sender label cut to the characters a label uses, or None."""
    if not isinstance(value, str):
        return None
    return re.sub(r"[^A-Za-z0-9._:/@#+-]", "", value[:4 * LABEL_MAX])[:LABEL_MAX] or None


def _stamp(ident):
    """The state file as it is now (size and mtime), or None when it cannot be read."""
    try:
        st = os.stat(ident["state"])
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


def _item(x):
    """One kept or rendered ask, checked: the same test for an answer from the server and for the cache."""
    return (isinstance(x, dict) and isinstance(x.get("key"), str) and MSG_KEY.fullmatch(x["key"]) is not None
            and x.get("state") in STATES and (x.get("from") is None or _label(x.get("from")) == x.get("from"))
            and (x.get("at") is None or (isinstance(x.get("at"), str) and len(x["at"]) <= 40)))


def _rows(body, since):
    """The snapshot of one owed answer: {"outcome": "ok", "items", "truncated"}, {"outcome": "invalid"} when it
    echoes the filters but has a row this module cannot read, or None when it is not the filtered list."""
    if not isinstance(body, dict):
        return None
    echo = body.get("filters")
    if not isinstance(echo, dict) or echo.get("state") != list(STATES) or echo.get("since") != since:
        return None
    owed = body.get("owed")
    if not isinstance(owed, list):
        return None
    items, seen = [], set()
    for row in owed[:LIMIT]:
        hit = row.get("hit") if isinstance(row, dict) else None
        if not isinstance(hit, dict):
            return {"outcome": "invalid"}
        at = hit.get("recorded_at")
        item = {"key": hit.get("key"), "from": _label(hit.get("author")), "state": row.get("state"),
                "at": at if isinstance(at, str) and len(at) <= 40 else None}
        if not _item(item):
            return {"outcome": "invalid"}       # never a part of the list shown as the whole
        if item["key"] in seen:
            continue                            # an ask owed under two labels of one seat is one ask
        seen.add(item["key"])
        items.append(item)
    return {"outcome": "ok", "items": items, "truncated": body.get("truncated") is True or len(owed) > LIMIT}


def _call(ident, since, run):
    """One owed call: a snapshot, or None when it failed."""
    argv = [ident["python"], "-m", "tools.meshy_node", "owed", "--base-url", ident["base_url"],
            "--state-file", ident["state"], "--state", ",".join(STATES), "--since", since, "--limit", str(LIMIT)]
    hdir = postbox_os.helper_dir_for_log(ident["log"])
    try:
        settled = postbox_os.settle_helper_dir(hdir, only_kind="owed")
        if not settled.get("settled"):            # admin 361a7e P2-2: no new lookup over an unsettled one
            return None
        r = postbox_os.run_bounded(
            argv, cwd=ident["client"], max_stdout=STDOUT_MAX, max_stderr=65536, text=True,
            encoding="utf-8", errors="replace", timeout=CALL_TIMEOUT_S, run_fn=run or subprocess.run,
            stdin=subprocess.DEVNULL, creationflags=postbox_os.NO_WINDOW,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            helper_dir=hdir, helper_kind="owed")
        if (r.returncode != 0 or getattr(r, "stdout_truncated", False) or not isinstance(r.stdout, str)
                or len(r.stdout) > STDOUT_MAX or len(r.stdout.encode("utf-8")) > STDOUT_MAX):
            return None
        return _rows(json.loads(r.stdout), since)
    except Exception:
        return None


def _read(ident):
    try:
        data = postbox_os.read_bounded_json(_cache_path(ident), max_bytes=MAX_OWED_CACHE_BYTES)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load(ident, t, stamp):
    """The snapshot kept for this state file (as it is now) and server, younger than TTL_S, else None."""
    data = _read(ident)
    if not data or data.get("schema_version") != 1:
        return None
    if data.get("state") != ident["state"] or data.get("base_url") != ident["base_url"] or data.get("stamp") != stamp:
        return None
    at = data.get("at")
    # finite (json reads NaN), and young in either direction: a clock that stepped back must not make it immortal
    if type(at) not in (int, float) or not math.isfinite(at) or abs(t - at) >= TTL_S:
        return None
    if data.get("outcome") == "invalid":
        return {"outcome": "invalid"}
    items = data.get("items")
    if (data.get("outcome") != "ok" or not isinstance(items, list) or len(items) > LIMIT or not all(_item(x) for x in items)
            or not isinstance(data.get("truncated"), bool)):
        return None
    return {"outcome": "ok", "items": items, "truncated": data["truncated"]}


def _save_lock(path):
    """An fd holding the lock beside the cache at `path`, or None when it was not taken within SAVE_LOCK_WAIT_S.
    The fd is closed here unless it is returned holding the lock, whatever interrupts the wait."""
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT)
    taken = False
    try:
        deadline = time.monotonic() + SAVE_LOCK_WAIT_S
        while not postbox_os.try_lock(fd):
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)
        taken = True
        return fd
    finally:
        if not taken:
            os.close(fd)


def _save(ident, t, stamp, snap):
    """Replace the file in one step, unless it holds a newer snapshot already; a failure is ignored.

    The read, the check and the replace hold the lock beside the cache, as postbox's fetch trim holds its
    sidecar's. Without it a renderer whose lookup started earlier could read the file, another could write a
    newer snapshot, and the first could then replace that, so an ask answered in between showed again until
    TTL_S (codex on #100). A lock not taken within SAVE_LOCK_WAIT_S means no save."""
    lock = None
    try:
        lock = _save_lock(_cache_path(ident))
        if lock is None:
            return
        kept = _read(ident)
        if kept and type(kept.get("at")) in (int, float) and math.isfinite(kept["at"]) and kept["at"] > t \
                and kept.get("state") == ident["state"] and kept.get("base_url") == ident["base_url"]:
            return                              # another renderer's lookup started later and finished first
        payload = dict(snap, schema_version=1, state=ident["state"], base_url=ident["base_url"], stamp=stamp, at=t)
        postbox_os.write_private_json_atomic(_cache_path(ident), payload, prefix=".owed-",
                                             max_bytes=MAX_OWED_CACHE_BYTES)
    except Exception:
        pass
    finally:
        if lock is not None:
            try:
                postbox_os.unlock(lock)
            except OSError:
                pass
            try:
                os.close(lock)
            except OSError:
                pass


def snapshot(ident, *, run=None, now=time.time):
    """What this identity owes now, from the cache or one call: {"outcome": "ok", "items", "truncated"},
    {"outcome": "invalid"}, or None when it is not available. Never raises."""
    try:
        if not ident.get("owed_line", True):
            return None
        if not all(isinstance(ident.get(f), str) and ident.get(f) for f in ("python", "client", "state", "base_url", "log")):
            return None
        if not os.path.isdir(os.path.join(ident["client"], "tools", "meshy_node")):
            return None
        t, stamp = now(), _stamp(ident)
        if stamp is None:
            return None
        kept = _load(ident, t, stamp)
        if kept is not None:
            return kept
        path = _cache_path(ident)
        if t < _backoff_until.get(path, 0):
            return None
        snap = _call(ident, _since(t), run)
        if snap is None:
            _backoff_until[path] = t + FAIL_BACKOFF_S
            return None
        _save(ident, t, stamp, snap)
        return snap
    except Exception:
        return None


def _age(t, iso):
    try:
        then = datetime.datetime.fromisoformat(iso[:-1] + "+00:00" if iso.endswith("Z") else iso).timestamp()
    except Exception:
        return "?"
    s = max(0, round(t - then))
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % round(s / 60)
    if s < 172_800:
        return "%dh" % round(s / 3600)
    return "%dd" % round(s / 86_400)


#: The active asks (this seat posted in the ask's thread after it, but not as a reply to it) are counted beside the
#: open ones, never as what waits on a reply: the line, meshy-mod's "owes N" and a bare `owed` (#544) give the same
#: open count (SD 255, pxx E). This command lists them.
ACTIVE_CMD = LIST_CMD + " --state active"


def render(snap, t):
    """The line for a snapshot, or None when nothing is owed: the OPEN asks are what waits on a reply, at most SHOW
    of them named and at most MAX_BYTES in UTF-8 (the Codex batch reserves that much for it), the rest counted and the
    command that lists them named; the ACTIVE ones are counted beside them, with the command that lists those."""
    if snap.get("outcome") == "invalid":
        return PREFIX + "the server's owed list has a row this seat's postbox cannot read, so it is not named here; `%s` lists it." % LIST_CMD
    items = snap.get("items") or []
    if not items:
        return None
    opened = [x for x in items if x["state"] == "open"]
    active = len(items) - len(opened)
    least = "at least " if snap.get("truncated") else ""
    beside = " (+%s%d replied in thread, not linked: `%s` lists them)" % (least, active, ACTIVE_CMD) if active else ""
    head = "%s%s%d ask(s) of the last 14 days wait on your reply%s" % (PREFIX, least, len(opened), beside)
    if not opened:
        return head + "."
    head += ": "
    rest = lambda n: ", and %d more (`%s` lists every one)." % (n, LIST_CMD) if n else "."  # noqa: E731
    parts = []
    for x in opened[:SHOW]:
        part = "%s (from %s, %s)" % (x["key"], x.get("from") or "unknown", _age(t, x.get("at") or ""))
        if len((head + ", ".join(parts + [part]) + rest(len(opened) - len(parts) - 1)).encode("utf-8")) > MAX_BYTES:
            break
        parts.append(part)
    if not parts:
        return head.rstrip(": ") + " (`%s` lists them)." % LIST_CMD
    return head + ", ".join(parts) + rest(len(opened) - len(parts))


def line_for(ident, *, run=None, now=time.time):
    """The owed line for this identity now, or None. Never raises."""
    try:
        snap = snapshot(ident, run=run, now=now)
        return render(snap, now()) if snap else None
    except Exception:
        return None


def _sig(snap):
    if snap.get("outcome") == "invalid":
        return ("invalid",)
    items = snap.get("items") or []
    if not items:
        return ("empty",)
    return ("ok", tuple(sorted((x["key"], x["state"]) for x in items)), bool(snap.get("truncated")))


def wake_line(ident, consumer, *, every=False, run=None, now=time.time):
    """(line, token) for one wake of `consumer`; (None, None) when there is nothing to show. With `every`, any
    owed list; otherwise one that differs (by key, state and the cut, so aging is not a change) from the last one
    delivered to that consumer, or is REPEAT_S older than it, and once a line saying a list it was shown has
    emptied. Pass the token to `delivered` once the notice went out. Never raises."""
    try:
        snap = snapshot(ident, run=run, now=now)
        if not snap:
            return None, None
        t, sig = now(), _sig(snap)
        slot = (ident["log"], consumer)
        last = _shown.get(slot)
        if sig == ("empty",):
            if every or not last or last[0] == sig:
                return None, None
            return PREFIX + "nothing waits on your reply now.", (slot, sig, t)
        if not every and last and last[0] == sig and abs(t - last[1]) < REPEAT_S:
            return None, None
        return render(snap, t), (slot, sig, t)
    except Exception:
        return None, None


def delivered(token):
    """Record that the line `wake_line` gave with this token reached its consumer. Never raises."""
    try:
        if token:
            slot, sig, t = token
            _shown[slot] = (sig, t)
    except Exception:
        pass
