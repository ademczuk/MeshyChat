"""Put a priority notice at the FRONT of a Codex seat's own queue (Bxxxxx, Wed 11 Nov 2026: "Prove native priority").

Why: the postbox hands mail to a Codex seat with `codex queue --thread <t> --message <m>`, which only APPENDS. Codex's
queue store is FIFO per thread, one turn per item, so a directive arriving behind three notices runs fourth (live test
<thread>, verdict 5ef51e). codex-bxxxxx-cli's research on HQB (msg 9de38f, REPORT.md; codex-cli 0.167.0,
source pin rust-v0.167.0 086ccc83) found the supported route: the app-server's experimental queue API. A SECOND,
short-lived `codex app-server --stdio` sharing the seat's Codex home can add to and reorder the queue of the runtime
the seat's TUI hosts. The running runtime re-reads the first durable row at dispatch, so the reordered item is taken
on its next turn (probe: D, O1, O2, O3).

What this does, in one helper session: initialize (experimentalApi), thread/queue/add (with a client id), list every
page, thread/queue/reorder [ours, the rest in their order], then list again to verify. It never resumes, starts or
steers the thread (that would make a competing runtime: REPORT.md section 3).

LIMITS, stated, not hidden:
- add + list + reorder are separate calls. If the seat's current turn ends between them, an ordinary item may already
  have been selected; a reorder cannot undo that. The result says so ("front": False), and nothing is retried in a way
  that could add the notice twice.
- If the reorder fails after the add, the notice stays queued at the back: delivered, not prioritised.
- The API is EXPERIMENTAL in 0.167.0. If the server refuses it, the caller falls back to `codex queue` (append).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

PROTOCOL_ERRORS = ("method not found", "unknown method", "experimental")


class FrontError(Exception):
    """The helper could not run or the API is unavailable; nothing was added (safe to fall back to `codex queue`)."""


PRIORITY_PREFIX = "postbox-priority-"     # the clientUserMessageId every priority notice of the postbox carries


def _items(result):
    """[(id, clientUserMessageId or "")], nextCursor."""
    if not isinstance(result, dict):
        raise ValueError("queue list: no result object")
    data = result.get("data")
    if not isinstance(data, list):
        raise ValueError("queue list: no data list")
    items = []
    for it in data:
        if not isinstance(it, dict) or not isinstance(it.get("id"), str) or not it["id"]:
            raise ValueError("queue list: an item without an id")
        cuid = it.get("clientUserMessageId")
        items.append((it["id"], cuid if isinstance(cuid, str) else ""))
    return items, result.get("nextCursor")


def front_order(items, ours):
    """The full id list with OURS placed after any priority notices already leading the queue (an earlier directive
    keeps its place: FIFO among priority notices, pxx P3) and before every ordinary item. Returns
    (order, target_index)."""
    rest = [(i, c) for i, c in items if i != ours]
    lead = 0
    while lead < len(rest) and rest[lead][1].startswith(PRIORITY_PREFIX):
        lead += 1
    order = [i for i, _c in rest[:lead]] + [ours] + [i for i, _c in rest[lead:]]
    return order, lead


def _in_front(items, ours):
    """True when only priority notices of the postbox stand before OURS, False when an ordinary item does, None
    when ours is no longer listed (taken or not: unknown, never counted as success)."""
    ids = [i for i, _c in items]
    if ours not in ids:
        return None
    return all(c.startswith(PRIORITY_PREFIX) for _i, c in items[:ids.index(ours)])


class _Session:
    def __init__(self, cmd, env, deadline):
        self.deadline = deadline
        self.q = queue.Queue()
        self.seq = 0
        self.stuck = False
        try:
            self.p = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise FrontError("app-server did not start: %s" % exc)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.p.stdout:
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            self.q.put(None)

    def send(self, msg):
        """Write one request, bounded by the session deadline (pxx: a helper that stops reading its stdin would
        block a write larger than the pipe buffer forever, and with it this seat's delivery loop). The write runs in
        a short thread (Windows pipes have no select); on timeout the helper is killed and TimeoutError raised."""
        data = json.dumps(msg) + "\n"
        err = []

        def write():
            try:
                self.p.stdin.write(data)
                self.p.stdin.flush()
            except BaseException as exc:          # noqa: BLE001 - handed back to the caller below
                err.append(exc)
        t = threading.Thread(target=write, daemon=True)
        t.start()
        t.join(max(0.0, self.deadline - time.monotonic()))
        if t.is_alive():
            self.stuck = True                     # close() must not flush into the same full pipe
            self.p.kill()
            raise TimeoutError("write of %s" % msg.get("method"))
        if err:
            raise err[0]

    def call(self, method, params):
        self.seq += 1
        ident = self.seq
        try:
            self.send({"id": ident, "method": method, "params": params})
        except OSError as exc:       # the server closed its stdin: this request was NOT sent (pxx)
            raise FrontError("could not send %s (%s)" % (method, exc))
        while True:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            try:
                item = self.q.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(method)
            if item is None:
                raise FrontError("app-server exited during %s" % method)
            if item.get("id") == ident and ("result" in item or "error" in item):
                return item

    def close(self):
        if self.stuck:                            # a write timed out: never flush or close into that pipe again
            self.p.kill()
        else:
            try:
                self.p.stdin.close()
            except OSError:
                pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _list_all(s, thread):
    items, cursor, pages = [], None, 0
    while True:
        params = {"threadId": thread} if cursor is None else {"threadId": thread, "cursor": cursor}
        r = s.call("thread/queue/list", params)
        if "error" in r:
            raise ValueError("queue list refused: %s" % r["error"].get("message"))
        page, cursor = _items(r.get("result"))
        items += page
        pages += 1
        if not cursor:
            return items
        if pages >= 200:
            raise ValueError("queue list: more than 200 pages")   # never reorder from a partial list


def queue_at_front(cmd, thread, text, client_id, *, env=None, timeout=60.0):
    """Add `text` to `thread`'s Codex queue and move it to the front. Returns a dict:
    {"added": bool, "id": str|None, "front": bool, "ahead_before": int|None, "why": str}.
    Raises FrontError ONLY when nothing was added (the caller may then fall back to `codex queue`)."""
    deadline = time.monotonic() + timeout
    s = _Session(cmd, env if env is not None else dict(os.environ), deadline)
    added_id = None
    sent_add = False                  # once the add is SENT, its outcome may be unknown: never fall back then
    try:
        try:
            init = s.call("initialize", {"clientInfo": {"name": "meshychat_postbox_front", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
        except TimeoutError:
            raise FrontError("app-server did not answer initialize")
        if "error" in init:
            raise FrontError("initialize refused: %s" % init["error"].get("message"))
        try:
            s.send({"method": "initialized", "params": {}})
        except OSError as exc:
            raise FrontError("could not send initialized (%s)" % exc)
        sent_add = True
        add = s.call("thread/queue/add", {"threadId": thread, "clientUserMessageId": client_id,
                                          "input": [{"type": "text", "text": text}]})
        if "error" in add:
            msg = str(add["error"].get("message", ""))
            raise FrontError("queue add refused: %s" % msg)
        sub = (add.get("result") or {}).get("queuedSubmission") or {}
        added_id = sub.get("id") if isinstance(sub.get("id"), str) else None
        if not added_id:
            return {"added": True, "id": None, "front": False, "ahead_before": None,
                    "why": "added, but the server returned no id: left at the back"}
        for attempt in (1, 2):                              # a stale full list is re-read once, never forced
            items = _list_all(s, thread)
            ids = [i for i, _c in items]
            if added_id not in ids:
                # NOT evidence that it went next: on the real 0.167.0 runtime this was seen while dispatch ran
                # O1, O2, O3, D (codex-bxxxxx-cli 445bac). Its position is unknown; only the live turn order shows it
                return {"added": True, "id": added_id, "front": None, "ahead_before": None,
                        "why": "not in the queue list when read: priority outcome UNKNOWN"}
            ahead = ids.index(added_id)
            order, target = front_order(items, added_id)
            if ahead == target:
                return {"added": True, "id": added_id, "front": True, "ahead_before": 0,
                        "why": "already in place (behind %d earlier priority notice(s))" % target}
            # the server refuses a list that does not name every queued item exactly once (REAL 0.167.0, RAN by
            # codex-bxxxxx-cli: REPORT.md section 2, "-32600 queue reorder must include every queued submission
            # exactly once"), so a racing arrival makes this fail and re-read, never drops an item
            r = s.call("thread/queue/reorder", {"threadId": thread, "queuedSubmissionIds": order})
            if "error" not in r:
                ok = _in_front(_list_all(s, thread), added_id)
                why = {True: "moved ahead of %d ordinary item(s)" % (ahead - target),
                       False: "reordered, but the list disagrees",
                       None: "reordered, then gone from the list before the check: position UNKNOWN"}[ok]
                return {"added": True, "id": added_id, "front": ok, "ahead_before": ahead - target, "why": why}
            if attempt == 2:
                return {"added": True, "id": added_id, "front": False, "ahead_before": ahead,
                        "why": "reorder refused twice: %s; left at the back" % r["error"].get("message")}
    except (TimeoutError, ValueError, FrontError, OSError) as exc:
        if added_id is not None:
            return {"added": True, "id": added_id, "front": False, "ahead_before": None,
                    "why": "after the add: %s: %s; left where it is" % (type(exc).__name__, exc)}
        # Only an add the server REFUSED is certainly not added. A failed write of the add counts as uncertain too
        # (part of the line may have reached the server): never append after an uncertain send (codex-bxxxxx-cli
        # 445bac); the postbox retries on its next poll instead.
        not_added = isinstance(exc, FrontError) and str(exc).startswith("queue add refused")
        if sent_add and not not_added:
            # the add was sent and not answered: it may have landed. Report UNKNOWN; the caller must not add again
            return {"added": None, "id": None, "front": False, "ahead_before": None,
                    "why": "add outcome unknown (%s: %s); not retried, not added again" % (type(exc).__name__, exc)}
        if isinstance(exc, FrontError):
            raise
        raise FrontError("%s before the add: %s" % (type(exc).__name__, exc))
    finally:
        s.close()
