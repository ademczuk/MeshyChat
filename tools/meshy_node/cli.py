"""Argument parsing and exit codes for `python -m tools.meshy_node`.

EXIT CODES: 0 success; 1 the server answered and it was not success (a refused write, a
failed call, a result whose signature did not verify, a 4xx/5xx read, a `read` the server
refused, did not finish while polled, or whose result did not verify; a plain `listen`
whose socket the server closed with 4401, the token expired or refused, or whose upgrade
it answered with an HTTP status instead of a socket; a `send --attach` whose upload failed,
which sends nothing; a `fetch` that did not fetch every attachment); 2 the client
refused before sending anything, or could not reach the server (or got back something
that was not a complete HTTP response). `listen --read` and `inbox --bodies` exit 0 even
when some or every read failed: each failure is a typed `read_error` on its own line or
hit, so a caller that needs every message read checks for `read_error`, not the exit code.
`offer` exits 0 only when its file was served completely once, and 1 when the server did not
accept the offer message, the window closed with nothing served or the file changed; `pickup`
exits 1 on any failure after the read, with a typed `error` (offer.py says each).

`--base-url` and `--state-file` are REQUIRED on every subcommand but `share` (which talks to no server) and have no default and no
environment fallback: a node client that can run without being told which server it is
talking to can be run against the production mesh by accident.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import board_claim, board_dispatch, board_queue, board_tasks, cards, converse, custody, docshare, errors, files, hints, node, offer, orient, recipientwarn, resume_brief, sendguard, transcript_graph, verdicts, wire


def _labels(raw: list[str] | str | None) -> list[str] | None:
    """`--to a,b --to c` -> [a, b, c]; order kept, duplicates left for the server to refuse."""
    if not raw:
        return None
    items = [raw] if isinstance(raw, str) else raw
    return [part.strip() for item in items for part in item.split(",") if part.strip()]


def _at_least_one(text: str) -> int:
    """`listen --count`: a whole number of frames, at least 1. 0 had no defined meaning (a
    plain listen opened the socket, waited for nothing and exited 0; the monitor stopped at
    its first line), so it is refused here, as argparse refuses any bad value: exit 2, the
    option named, nothing sent. The same style as monitor.py's `_seconds` for its durations."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a whole number, at least 1, got {text!r}")
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text!r}")
    return value


def _whole_within(low: int, high: int):
    """An argparse type: a whole number from `low` to `high`, refused otherwise as argparse refuses any bad value
    (exit 2, the option named, nothing sent). `card recent`'s counts; refused, not clamped, as the route refuses
    a limit it does not take."""
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"must be a whole number, {low} to {high}, got {text!r}")
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be {low} to {high}, got {text!r}")
        return value
    return parse


def _seconds_within(low: float, high: float):
    """An argparse type: seconds from `low` to `high` (a plain decimal number), refused otherwise, as
    _whole_within refuses a count."""
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"must be a number of seconds, {low:g} to {high:g}, got {text!r}")
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be {low:g} to {high:g} seconds, got {text!r}")
        return value
    return parse


def _redact_version(text: str):
    """`redact --version`: "all", or a whole number of at least 1, as memory.redact takes it (redact.py
    `_version_arg`). Anything else is refused by argparse: exit 2, nothing sent."""
    if text == "all":
        return "all"
    try:
        return _at_least_one(text)
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError(f'must be "all" or a whole number of at least 1, got {text!r}') from None


_RELATIVE_AGE_RE = re.compile(r"^(?P<count>[0-9]+)(?P<unit>[dh])$")


def _stderr_line(record: dict) -> None:
    """One JSON line on stderr AFTER a send the server accepted, which must not be able to fail the
    command. A closed or broken stderr raises OSError (or ValueError on a closed file), `main` maps
    OSError to exit 2 "unreachable", and a caller that retries an exit 2 would send the message a
    second time. A note that cannot be written is dropped.

    Catching the error is not enough. The text that failed to go out stays in the stream's buffer,
    the interpreter flushes it again at exit, that fails too, and exit 0 becomes 120 (kxxx's Codex
    review of #88, reproduced on Windows with a closed stderr pipe). So on failure `sys.stderr` is
    pointed at the null device and the exit-time flush has nothing left to fail on. The flush makes
    the failure surface here even when the stream is block-buffered; the default stderr is
    line-buffered, so there the newline already flushes inside `write`."""
    try:
        sys.stderr.write(json.dumps(record) + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        try:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass


#: The message types that ask for something. `--type` is free text on the wire, so these are the
#: two spellings the fleet uses for an ask (README section 8 and the postbox reply command).
ASK_TYPES = ("request", "question")
#: The message types that answer one.
ANSWER_TYPES = ("answer", "ack")


def _send_hints(msg_type, requires_ack: bool, in_reply_to, closes=None, declines=None) -> list[dict]:
    """Hints for a send that was ACCEPTED, one stderr line each. Never a refusal and never a
    change to what was sent: the message is already stored when these print.

    The graph tracks a reply obligation only from two signed fields. `requires_ack` makes the
    `owes_ack` edge that puts an ask in the recipient's `owed`; `in_reply_to` makes the
    `replies_to` edge that marks it answered, and `closes` (#89) makes the
    `closes` edge that ends it, so an answer carrying either is linked. Measured on production 2026-10-04: one seat
    received at least 128 direct messages in seven days and none carried `requires_ack`, so
    `owed` had nothing new to show (roadmap item 8.0)."""
    hints = []
    if msg_type in ASK_TYPES and not requires_ack:
        hints.append({
            "hint": "ask_not_tracked", "type": msg_type,
            "note": "sent without --expects-reply, so it creates no reply obligation: the recipient's "
                    "`owed` will not list it. When you need an answer, add --expects-reply"})
    if msg_type in ANSWER_TYPES and not in_reply_to and not closes and not declines:
        hints.append({
            "hint": "answer_not_linked", "type": msg_type,
            "note": "sent without --in-reply-to, so it creates no replies_to edge and cannot mark "
                    "the message it answers as answered in `owed`. Name the message you are "
                    "answering with --in-reply-to <its key>, or --closes <its key> when this ends it"})
    if msg_type in ANSWER_TYPES and requires_ack:
        # SD 255 item 15 (b), 7 Nov 2026: 34 of the ~100 asks still open on one seat were answers sent this way
        # (oxxxxxx). An answer that expects a reply is an ask, and stays in every recipient's `owed`.
        hints.append({
            "hint": "answer_expects_reply", "type": msg_type,
            "note": "sent with --expects-reply (--requires-ack), so this answer now sits in each recipient's "
                    "`owed` until they reply to it or close it. Leave the flag off an answer unless it asks "
                    "something back, and then send it as --type question or request"})
    return hints


#: The window an answer looks back over for other asks it leaves owed: the same 14 days the
#: postbox wake and meshy-mod count as owed.
LEFT_OPEN_SINCE = "14d"
#: Wall-clock bound on that lookup. It runs after the message is stored, so a slow server only
#: delays the hint, and past this there is none.
LEFT_OPEN_TIMEOUT_S = 2.0
#: The close decision is advisory, but its pre-send lookup must not delay an answer indefinitely.
OWED_CLOSE_TIMEOUT_S = 2.0
# An ask a linked reply already answered (an ack, or an answer sent with --keep-open) is still owed until
# a close is signed, so a later answer must be able to close it (pxx, GitHub Codex r4359878147).
OWED_CLOSE_STATES = ("open", "active", "answered")
#: At most this many asks named in one hint; `more` counts the rest.
LEFT_OPEN_MAX = 10
#: Rows asked of /v1/mesh/owed. Without a limit the route answers its default 100 across ALL threads, newest
#: first, and the thread filter here runs after it, so a seat with a backlog lost the older asks of this thread
#: (kxxx on #152: one seat had 129 owed asks on production on 2026-10-07). The route does not cap `limit`.
LEFT_OPEN_LIMIT = 500


def _asks_left_open(owed_answer, to: list[str], thread: str, linked: set) -> list[dict]:
    """The asks an accepted answer leaves owed, as {key, author}: from one of its `to` recipients, in
    its thread, with an explicit open or active state, and neither the ask it replies to nor one it
    closes. Each key once, in the order `owed` gave them.

    Measured 2026-10-07 on one seat: six asks from one peer stayed in its `owed` for up to 17
    hours, every one handled. Each reply linked one ask with --in-reply-to while answering
    several, so `answer_not_linked` never fired and nothing named the rest until the operator
    asked why the count read 6. Only the replying seat knows whether its message answered them,
    so this names them and never closes anything itself."""
    if not isinstance(owed_answer, dict) or not isinstance(owed_answer.get("owed"), list):
        return []
    out, seen = [], set()
    for r in owed_answer["owed"]:
        hit = r.get("hit") if isinstance(r, dict) else None
        key = hit.get("key") if isinstance(hit, dict) else None
        if (not isinstance(key, str) or not _is_mesh_key(key) or key in linked or key in seen
                or r.get("thread_id") != thread or hit.get("author") not in to
                or r.get("state") not in ("open", "active")):
            continue
        seen.add(key)
        out.append({"key": key, "author": hit["author"]})
    return out


def _left_open_hint(state_file, state, to: list[str], thread, in_reply_to, closes, declines=None) -> dict | None:
    """One hint line naming the asks this answer leaves owed, or None. Fails quiet: a lookup that
    errors, times out or is not echoed is no hint, never a changed exit code. The lookup runs after
    the message is stored and adds at most LEFT_OPEN_TIMEOUT_S before the command returns."""
    linked = {k for k in (in_reply_to, *(closes or ()), *(declines or ())) if k}
    try:
        answer = recipientwarn._within(
            lambda _: node.owed(state_file, state, limit=LEFT_OPEN_LIMIT, states="open,active",
                                since=_owed_since(LEFT_OPEN_SINCE)),
            None, LEFT_OPEN_TIMEOUT_S)
        asks = _asks_left_open(answer, to, thread, linked)
    except Exception:                                      # noqa: BLE001 - a hint is advisory
        return None
    truncated = isinstance(answer, dict) and answer.get("truncated") is True
    if not asks and truncated:
        # A cut list with no match here is not "nothing left owed": this thread's asks may be past the cut (kxxx on
        # #152, the ask at the recorded rule). Say so instead of going quiet.
        return {"hint": "asks_left_open", "thread": thread, "asks": [], "more": 0, "truncated": True,
                "note": f"the owed lookup returned only its first {LEFT_OPEN_LIMIT} rows and none were from this "
                        "answer's recipients in this thread, so it could not see the whole list. Run `owed --state "
                        "open,active --since 14d --limit 2000` before assuming this answer left nothing owed"}
    if not asks:
        return None
    shown = asks[:LEFT_OPEN_MAX]
    hint = {"hint": "asks_left_open", "thread": thread, "asks": shown, "more": len(asks) - len(shown),
            "note": f"{len(asks)} other ask(s) from this answer's recipients in this thread still appear owed by "
                    "you (a projection can lag a minute behind a recent reply). Read each one; for those this "
                    "message really answered, settle them with one short send to their author in this thread, "
                    "--type answer --closes <key> for each. Leave the rest: they are still owed"}
    if truncated:                                          # the server cut its list: there may be more than `more`
        hint["truncated"] = True
    return hint


def _owed_question_to_close(state_file, state, in_reply_to: str | None, thread: str) -> tuple[str | None, str | None]:
    """Return ``(key, reason)`` for a close this seat may sign without fetching the ask.

    The read ledger is the local record of completed, verified reads.  In particular, do not
    call ``memory.read`` here: a send refused by sendguard must not make the ask disappear from
    unread merely because the client considered closing it.
    """
    if not in_reply_to:
        return None, None
    lookup_error = []

    def current_key(_):
        try:
            return node.owed_key(state_file, state, key=in_reply_to)
        except Exception as exc:                            # noqa: BLE001 - advisory lookup only
            lookup_error.append(exc)
            return None

    owed = recipientwarn._within(current_key, None, OWED_CLOSE_TIMEOUT_S)
    if not isinstance(owed, dict):
        # A 429 is the advisory 30-a-minute bound (txxxx, #407): say so, or a busy seat's close is skipped unseen.
        if any(isinstance(exc, node.ServerSaid) and exc.status == 429 for exc in lookup_error):
            return None, "the owed lookup was rate-limited"
        return (None, None) if lookup_error else (None, "the owed lookup did not complete within 2 seconds")
    rows = owed.get("owed")
    rows = rows if isinstance(rows, list) else []
    row = next((item for item in rows if isinstance(item, dict) and item.get("state") in OWED_CLOSE_STATES
                and isinstance(item.get("hit"), dict) and item["hit"].get("key") == in_reply_to), None)
    if row is None:
        return None, None
    # The same one-key result explains cross-thread replies without another lookup.
    if row.get("thread_id") != thread:
        if row.get("msg_type") == "question":
            return None, f"it is owed in thread {row.get('thread_id')!r}, not {thread!r}; use --closes"
        return None, None
    if row.get("msg_type") == "request":
        return None, None
    if row.get("msg_type") != "question":
        return None, "the server does not say whether it is a question"
    version = row["hit"].get("version")
    reads = node.ledger_records(state_file, state)
    if (type(version) is not int or version < 1
            or not any(read.get("key") == in_reply_to and type(read.get("version")) is int
                       and read["version"] >= version for read in reads if isinstance(read, dict))):
        return None, "not read by this seat yet; read it, then answer"
    return in_reply_to, None


def _owed_close_hint(key: str, reason: str) -> None:
    print(f"owed close: not signed for {key}: {reason}; send --type answer --closes {key} on its thread to settle it",
          file=sys.stderr)


def _is_mesh_key(raw: str) -> bool:
    """The write seam's msg/ shape needed before `send --closes` signs anything.

    `validate_mesh_key_ts` deliberately accepts every non-msg key unchanged; this command
    instead requires a msg/ key, then repeats its in-band epoch-ms test locally so a typo
    receives exit 2 rather than a signed request and a remote 422.
    """
    if not isinstance(raw, str) or not raw.startswith("msg/"):
        return False
    segment = raw[4:].split("-", 1)[0]
    return segment.isdigit() and 1_000_000_000_000 <= int(segment) <= 4_102_444_800_000


#: What a bare `owed` asks for (2026-10-07). Unbounded, the list was mostly history: on production 3,518 of
#: the 3,859 unclosed owed asks predated the 27 Sep cutover (imported mail), so a seat's owed count said little
#: about today and a new ask sat behind dozens of stale ones. The same 14 days the postbox wake and meshy-mod
#: count. `--since all` keeps the old, unbounded answer.
OWED_DEFAULT_SINCE = "14d"
#: What a bare `owed` lists. The default includes answered asks, but presents them separately from the reply-owed
#: open and active bucket. The owed line (owedline.STATES) keeps counting only open and active. `--state all` keeps
#: the unfiltered answer.
OWED_DEFAULT_STATE = "open,active,answered"
OWED_REPLY_STATES = ("open", "active")
OWED_ANSWERED_STATE = "answered"
#: What a bare `owed` (no --state) lists since SD 255 (round 1 item 15, 7 Nov 2026): the asks still waiting for a reply,
#: asked of the server as state=open. owed_acks filters the state in SQL before LIMIT, so no answered row can push an
#: open ask past --limit: on 7 Nov pxx's own bare `owed` came back as 100 answered rows with truncated true (515e7a),
#: and the ~100 asks still open on oxxxxxx had all been answered directly (05d6c9). The other states come from bounded
#: add-on calls and print as summaries, never as owed rows. An explicit --state behaves as before:
#: `--state open,active,answered` is #373's view byte for byte, and --counts-only keeps that state and its fields (the
#: weekly measurement reads them).
OWED_BARE_STATE = "open"
#: "active": the seat posted in the ask's thread after it, but not as a reply to it. One status post in a busy thread
#: turns every unanswered ask there active, so these may not be answered at all (pxx A): every key is shown while
#: they fit in the page, and past that the OLDEST few, since the old ones are the forgotten ones.
OWED_ACTIVE_STATE = "active"
#: Rows asked for by each add-on: the same bound as the asks-left-open lookup, which the route does not cap.
OWED_ADDON_LIMIT = 500
#: Each add-on's own time; one that does not answer in time is shown as unavailable, never as 0.
OWED_ADDON_TIMEOUT_S = 2.0
#: Active keys shown when there are more than fit in the page: the oldest this many.
OWED_ACTIVE_OLDEST_SHOWN = 10
#: The page a bare `owed` fits the active keys into when no --limit is given: the route's own default.
OWED_ROUTE_DEFAULT_LIMIT = 100
#: `owed --reconcile`'s defaults (tools/meshy_node/reconcile.py, SD 265 item 3): restated here for the help text,
#: and pinned equal to the module's by tests/test_meshy_node_owed_reconcile.py.
OWED_RECONCILE_EXPIRE = "72h"
OWED_RECONCILE_MAX_READS = 60
#: The options that mean nothing without --reconcile.
OWED_RECONCILE_ONLY = ("closures", "expire", "max_reads", "json")


def _owed_since(raw: str, *, now=None) -> str:
    """An offset-bearing ISO instant unchanged, or `Nd`/`Nh` as a UTC cutoff.

    The client converts only relative ages: a literal timestamp stays byte-for-byte useful
    in the route's `filters` echo. `now` is injectable because a boundary test must not
    sleep and a workstation clock must never make a naive timestamp meaningful.
    """
    relative = _RELATIVE_AGE_RE.fullmatch(raw)
    if relative:
        clock = now or (lambda: datetime.now(timezone.utc))
        current = clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise node.Refused("the injected owed clock must be timezone-aware")
        amount = int(relative.group("count"))
        delta = timedelta(days=amount) if relative.group("unit") == "d" else timedelta(hours=amount)
        return (current - delta).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise node.Refused("--since must be an ISO-8601 timestamp with timezone offset or Z, or a relative age like 14d or 36h")
    return raw


def _brief_age(recorded_at: object, *, now=None) -> str:
    """A compact UTC age for the one-call brief; malformed server data stays visible as ?."""
    try:
        then = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
        if then.tzinfo is None:
            return "?"
        seconds = max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds()))
    except (TypeError, ValueError):
        return "?"
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def _owed_bucket(answer, states: tuple[str, ...]) -> dict:
    """Routing-only rows and their observed count for one owed-state bucket."""
    rows = answer.get("owed") if isinstance(answer, dict) else None
    items, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        hit = row.get("hit") if isinstance(row, dict) else None
        key = hit.get("key") if isinstance(hit, dict) else None
        if not isinstance(row, dict) or row.get("state") not in states or not isinstance(key, str) or key in seen:
            continue
        seen.add(key)
        items.append(row)
    return {"count": len(items), "items": items,
            "truncated": bool(answer.get("truncated")) if isinstance(answer, dict) else False}


def _owed_type_counts(bucket: dict) -> dict:
    """The routing types in one owed bucket; an older server omits ``msg_type``."""
    counts = {"question": 0, "request": 0, "unknown": 0}
    for row in bucket.get("items", []):
        kind = row.get("msg_type") if isinstance(row, dict) else None
        counts[kind if kind in ("question", "request") else "unknown"] += 1
    return counts


def _answered_still_open(path: Path, state: dict, since: str) -> dict:
    """The separately queried answered bucket. A brief remains useful if this add-on is unavailable."""
    try:
        answered = recipientwarn._within(
            lambda _: node.owed(path, state, limit=None, states=OWED_ANSWERED_STATE, since=since),
            None, LEFT_OPEN_TIMEOUT_S)
    except (node.FiltersNotApplied, node.ServerSaid, node.Refused, OSError):
        return {"count": None, "items": [], "truncated": False}
    if not isinstance(answered, dict):
        return {"count": None, "items": [], "truncated": False}
    return _owed_bucket(answered, (OWED_ANSWERED_STATE,))


def _with_answered_still_open(answer: dict, path: Path, state: dict, since: str) -> dict:
    """Add the client-side bucket without changing any server-signed response fields."""
    return {**answer, "answered_still_open": _answered_still_open(path, state, since)}


def _owed_addons(path: Path, state: dict, wanted: tuple[str, ...], since: str | None,
                 thread: str | None) -> dict[str, dict]:
    """The bare `owed` add-ons: one query per state, so answered volume never truncates the active rows and each has
    its own truncated flag (a reviewer e00d74, pxx), run one after another under ONE OWED_ADDON_TIMEOUT_S
    budget. Not at once: two concurrent reads could both refresh the token in the state file. A query the budget
    leaves no time for is unavailable, like one that fails.

    recipientwarn._within abandons a read past its budget on a daemon thread, and its docstring says such a thread
    "writes nothing". node.owed can refresh the token and write the state file, so that holds here only because the
    state=open call has just run on the main thread with the same token (a refresh would have happened there), and
    the state write is atomic (node.py, the state file's replace). Keep the primary call first (pxx INFO)."""
    end = time.monotonic() + OWED_ADDON_TIMEOUT_S
    out = {}
    for states in wanted:
        left = end - time.monotonic()
        out[states] = (_owed_addon(path, state, states, since, thread, left) if left > 0 else
                       {"count": None, "unavailable": f"not started: the {OWED_ADDON_TIMEOUT_S:g} s budget was spent"})
    return out


def _owed_addon(path: Path, state: dict, states: str, since: str | None, thread: str | None,
                budget: float = OWED_ADDON_TIMEOUT_S) -> dict:
    """One bounded add-on read for a bare `owed`: the `_owed_bucket` of `states`, or {"count": None, "unavailable":
    why} when the read errs, is not echoed or does not finish in `budget` seconds. A read that did not complete is
    never shown as 0."""
    failure: list[str] = []

    def read(_):
        try:
            return node.owed(path, state, limit=OWED_ADDON_LIMIT, states=states, since=since, thread=thread)
        except node.FiltersNotApplied:
            failure.append("the server did not echo its filters")
        except node.ServerSaid as exc:
            failure.append(f"HTTP {exc.status}")
        except Exception as exc:                           # noqa: BLE001 - an add-on is a summary, never a failure
            failure.append(type(exc).__name__)
        return None

    answer = recipientwarn._within(read, None, budget)
    if not isinstance(answer, dict):
        return {"count": None,
                "unavailable": failure[0] if failure else f"no answer within the {OWED_ADDON_TIMEOUT_S:g} s budget"}
    # One state per add-on, filtered by the route and echoed, so a row without a state field is in it.
    return _owed_bucket(answer, (states, None))


def _owed_list_cmd(states: str, since: str | None, thread: str | None) -> str:
    """The exact command that lists one add-on's rows in full, its values shell-quoted: _owed_since passes a
    space-separated ISO time through unchanged, and an unquoted one is two arguments (pxx P2-1)."""
    cmd = f"owed --state {states} --since {shlex.quote(since or 'all')} --limit 2000"
    return cmd + (f" --thread {shlex.quote(thread)}" if thread else "")


def _owed_active_summary(bucket: dict, page: int, since: str | None, thread: str | None) -> dict:
    """The active asks of a bare `owed`, by key: every one while they fit in `page`, else the OLDEST
    OWED_ACTIVE_OLDEST_SHOWN (the route answers newest first), with the command that lists the rest."""
    if bucket.get("count") is None:
        return bucket
    rows = []
    for row in bucket.get("items", []):
        hit = row.get("hit") if isinstance(row.get("hit"), dict) else {}
        rows.append({"key": hit.get("key"), "author": hit.get("author"), "thread_id": row.get("thread_id"),
                     "recorded_at": hit.get("recorded_at")})
    rows.reverse()                                         # oldest first
    complete = bucket["count"] <= page and not bucket.get("truncated")
    shown = rows if complete else rows[:OWED_ACTIVE_OLDEST_SHOWN]
    out = {"count": bucket["count"], "truncated": bool(bucket.get("truncated")), "oldest_first": shown,
           "note": "you posted in each ask's thread after it, but not as a reply to it: read each one, then reply "
                   "with --in-reply-to, or settle a done one with --type answer --closes <key>"}
    if not complete:
        out["more"] = bucket["count"] - len(shown)
        out["list"] = _owed_list_cmd(OWED_ACTIVE_STATE, since, thread)
    if bucket.get("truncated"):
        # The route orders newest first before LIMIT, so a cut answer holds the NEWEST rows (pxx): say so,
        # rather than calling these the oldest.
        out["cut"] = (f"the server's list stopped at {OWED_ADDON_LIMIT}: these are the oldest of the newest "
                      f"{OWED_ADDON_LIMIT}, not of all; `{out['list']}` lists more")
    return out


def _owed_count_text(bucket: dict) -> str:
    count = bucket.get("count")
    if count is None:
        return "?"
    return f"{count}+" if bucket.get("truncated") else str(count)


def _owed_bare(args, state, since: str | None) -> dict:
    """The bare `owed` view (SD 255): the open asks as `owed`, the active ones by key, the answered ones as a count,
    and one summary line that the postbox owed line and meshy-mod's "owes N" reconcile with (pxx (2))."""
    answer = node.owed(args.state_file, state, limit=args.limit, states=OWED_BARE_STATE, since=since,
                       thread=args.thread)
    # The route filtered on state=open and echoed it, so a row without a state field is one of those, not hidden.
    reply_owed = _owed_bucket(answer, (OWED_BARE_STATE, None))
    addons = _owed_addons(args.state_file, state, (OWED_ACTIVE_STATE, OWED_ANSWERED_STATE), since, args.thread)
    active = _owed_active_summary(addons[OWED_ACTIVE_STATE], args.limit or OWED_ROUTE_DEFAULT_LIMIT, since,
                                  args.thread)
    answered = addons[OWED_ANSWERED_STATE]
    answered_view = {k: v for k, v in answered.items() if k != "items"}
    if answered.get("count") is not None:
        answered_view["list"] = _owed_list_cmd(OWED_ANSWERED_STATE, since, args.thread)
    summary = (f"open {_owed_count_text(reply_owed)}, replied in thread not linked {_owed_count_text(active)}, "
               f"answered not closed {_owed_count_text(answered)}")
    return {**answer, "summary": summary,
            "buckets": {"reply_owed": reply_owed, "replied_in_thread_not_linked": active,
                        "answered_not_closed": answered_view},
            "view": "a bare `owed` lists only the asks still waiting for a reply (state open); "
                    "`--state open,active,answered` lists every unclosed ask as before"}


def _owed_expire_s(raw: str) -> int:
    """--expire in seconds: Nh or Nd, at least one hour."""
    match = _RELATIVE_AGE_RE.fullmatch(raw)
    if not match or int(match.group("count")) < 1:
        raise node.Refused("--expire must be a relative age of at least 1h, like 72h or 3d")
    return int(match.group("count")) * (86400 if match.group("unit") == "d" else 3600)


def _owed_reconcile(args, state, since: str | None) -> int:
    """`owed --reconcile` (tools/meshy_node/reconcile.py, SD 265 item 3): the open, active and answered lists, one
    call each, then at most --max-reads signed reads that are never recorded as read. Exit 0 with the view; 1 when
    the server did not echo a filter, an answer lacks a field the view reads, or the counts do not balance."""
    from tools.meshy_node import reconcile
    if args.state is not None or args.counts_only:
        raise node.Refused("--reconcile reads the open, active and answered lists itself: drop --state and "
                           "--counts-only")
    expire_s = _owed_expire_s(args.expire or reconcile.DEFAULT_EXPIRE)
    max_reads = reconcile.DEFAULT_MAX_READS if args.max_reads is None else args.max_reads
    if max_reads < 0:
        raise node.Refused("--max-reads cannot be negative")
    closures = [k.strip() for k in (args.closures or "").split(",") if k.strip()]
    lists = {}
    try:
        for listed in reconcile.STATES:                    # one after another: two reads at once could both refresh
            lists[listed] = node.owed(args.state_file, state, limit=args.limit or reconcile.LIST_LIMIT,
                                      states=listed, since=since, thread=args.thread)
    except node.FiltersNotApplied as exc:
        _emit(exc.error)
        return 1
    # Every receipt's anchor (the server's head version and time) BEFORE the unread GET (pxx): a receipt
    # version committed after the unread list was taken is then never the version read. Fan-out candidates are safe
    # the same way: their versions come from the owed snapshot above.
    anchors: dict = {}
    for key in dict.fromkeys(closures):
        try:
            anchors[key] = node.neighbours(args.state_file, state, key=key, include_inferred=False, limit=1)
        except Exception as exc:                           # noqa: BLE001 - kept, and raised for that receipt alone
            anchors[key] = exc

    def anchor(key):
        got = anchors[key]
        if isinstance(got, Exception):
            raise got
        return got
    # The seat's unread list, before any read, so reconcile never makes the first read of a message (pxx P1):
    # over the owed window, but at most reconcile.UNREAD_GATE_S back. An 89-day window timed out on production
    # (8 Nov 2026, QueryCanceled), so nothing older than the gate is read.
    now = datetime.now(timezone.utc)
    gate = now - timedelta(seconds=reconcile.UNREAD_GATE_S)
    if since is not None:
        gate = max(gate, datetime.fromisoformat(since[:-1] + "+00:00" if since.endswith("Z") else since))
    unread = node.unread(args.state_file, state, since=gate.astimezone(timezone.utc).isoformat(timespec="seconds")
                         .replace("+00:00", "Z"), limit=reconcile.UNREAD_LIMIT)
    me_peer = state.get("peer_id")
    me_label = node.SEAT_DIRECTIVE_PEERS.get(me_peer) or state.get("label")
    # Receipts that may close another seat's ask: oxxxxxx's pinned peer (it owns them) and this seat's own (pxx B).
    signers = {peer: label for peer, label in node.SEAT_DIRECTIVE_PEERS.items() if label == reconcile.RECEIPT_SEAT}
    if me_peer:
        signers[me_peer] = me_label or "this seat"
    try:
        result = reconcile.reconcile(
            lists, lambda key, version: node.read(args.state_file, state, key=key, version=version,
                                                  include_value=True, record=False),
            unread=unread, gate_since=gate, anchor=anchor,
            recipients=lambda labels: node.recipients(args.state_file, state, labels),
            me_label=me_label, signers=signers, peer_labels=node.SEAT_DIRECTIVE_PEERS, closures=closures,
            expire_s=expire_s, max_reads=max_reads, now=now)
    except reconcile.Malformed as exc:
        _emit({"error": "owed_answer_malformed", "detail": str(exc)})
        return 1
    answered_list = _owed_list_cmd(OWED_ANSWERED_STATE, since, args.thread)
    if args.json:
        _emit({"since": since, "thread": args.thread, "answered_list": answered_list, **result})
    else:
        print(reconcile.render(result, since=since, thread=args.thread, answered_list=answered_list))
    return 0 if result["balanced"] else 1


def _brief(args, state) -> int:
    """The brief: GET /v1/mesh/brief, or owed and awaiting composed on a server without it."""
    since = _owed_since(args.since)
    try:
        answer = node.brief(args.state_file, state, since=since)
    except node.ServerSaid as exc:
        if exc.status != 404:
            _brief_say(args, f"brief failed: HTTP {exc.status}")
            return 1
        # roadmap 8.3: a server from before #91 has no route; compose the brief and say so (on stderr with
        # --json, so the one JSON object on stdout stays parseable)
        note = "brief: this server has no /v1/mesh/brief (older than #91), composed from owed and awaiting"
        print(note, file=sys.stderr if args.json else sys.stdout)
        return _brief_composed(args, state, since)
    except node.FiltersNotApplied:
        _brief_say(args, "brief incomplete: the brief response did not echo the requested window and bounds")
        return 1
    answer = _with_answered_still_open(answer, args.state_file, state, since)
    if args.json:
        _emit(answer)
    else:
        _print_brief(answer, since)
    return 0


def _brief_say(args, line: str) -> None:
    """A brief failure: the line as it always was, or as {"error": line} with --json."""
    if getattr(args, "json", False):
        _emit({"error": line})
    else:
        print(line)


def _brief_composed(args, state, since: str) -> int:
    """The brief on a server without /v1/mesh/brief: `owed` and `awaiting`, each echo checked."""
    expected_filters = {"state": ["open", "active"], "since": since}
    try:
        owed = node.owed(args.state_file, state, limit=None, states="open,active", since=since)
    except node.ServerSaid as exc:
        _brief_say(args, f"brief failed: owed HTTP {exc.status}")
        return 1
    except node.FiltersNotApplied:
        # node.owed refuses an answer that did not echo its filters (#88) before the check below can
        _brief_say(args, "brief incomplete: owed response did not echo requested filters")
        return 1
    try:
        awaiting = node.awaiting(args.state_file, state, states="open,active", since=since)
    except node.ServerSaid as exc:
        _brief_say(args, f"brief failed: awaiting HTTP {exc.status}")
        return 1
    for title, body in (("owed", owed), ("awaiting", awaiting)):
        if not isinstance(body, dict) or body.get("filters") != expected_filters:
            _brief_say(args, f"brief incomplete: {title} response did not echo requested filters")
            return 1
    answer = _with_answered_still_open(
        {"composed": True, "filters": expected_filters, "owed": owed, "awaiting": awaiting},
        args.state_file, state, since)
    if getattr(args, "json", False):
        _emit(answer)
        return 0
    # The same text view as the route's (pxx/5e4563 P2-3): the owed rows split into open and active, and the
    # answered bucket as one count line. These rows are the whole list (no page), so there is no "of TOTAL".
    owed_rows = owed.get("owed", []) if isinstance(owed, dict) else []
    _brief_owed_split({"items": owed_rows, "truncated": bool(owed.get("truncated"))}, since)
    _brief_answered_count(answer.get("answered_still_open"), since)
    rows = awaiting.get("awaiting", []) if isinstance(awaiting, dict) else []
    suffix = f" (truncated, more than {len(rows)})" if awaiting.get("truncated") else ""
    print(f"awaiting: {len(rows)}{suffix}")
    for row in rows[:10]:
        hit = row.get("hit", {}) if isinstance(row, dict) else {}
        print(f"  {hit.get('key', '?')}  {hit.get('owing_label') or row.get('owing_label', '?')}  "
              f"{_brief_age(hit.get('recorded_at'))}  {row.get('state', '?')}  "
              f"{row.get('done_when') or '-'}")
    if len(rows) > 10:
        print(f"  and {len(rows) - 10} more")
    return 0


def _expiry_cell(row, now=None) -> str:
    """"  expires <time>Z" or "  EXPIRED <time>Z" for an owed ask whose authority ends (a directive, or relayed words
    for a named window; the server's `expires_at`), else "". An expired one must not be acted on: ask again."""
    exp = row.get("expires_at") if isinstance(row, dict) else None
    if not isinstance(exp, str) or not exp.endswith("Z"):
        return ""
    from datetime import datetime as _d, timezone as _tz
    try:
        when = _d.fromisoformat(exp[:-1] + "+00:00")
    except ValueError:
        return ""
    return ("  EXPIRED %s" if (now or _d.now(_tz.utc)) >= when else "  expires %s") % exp


def _brief_rows(title: str, section, party: str) -> None:
    """One owed or awaiting section of the route's answer: "N of TOTAL" when the counts say more exist."""
    items = section.get("items", []) if isinstance(section, dict) else []
    counts = section.get("counts", {}) if isinstance(section, dict) else {}
    total = sum(v for v in counts.values() if isinstance(v, int)) if isinstance(counts, dict) else 0
    suffix = f" of {total}" if total > len(items) else ""
    suffix += " (truncated)" if isinstance(section, dict) and section.get("truncated") else ""
    print(f"{title}: {len(items)}{suffix}")
    for row in items[:10]:
        hit = row.get("hit", {}) if isinstance(row, dict) else {}
        print(f"  {hit.get('key', '?')}  {hit.get(party) or row.get(party, '?')}  "
              f"{_brief_age(hit.get('recorded_at'))}  {row.get('state', '?')}  "
              f"{row.get('done_when') or '-'}{_expiry_cell(row)}")
    _brief_more(items)


def _brief_owed_split(section, since: str | None) -> None:
    """The route's owed section (open and active, newest first) as two: "reply owed" for the open asks, and "replied
    in thread, not linked" for the active ones, oldest first (SD 255; pxx A: the old ones are the forgotten
    ones). Each says "N of TOTAL" from the route's per-state counts when the page holds fewer. The command it names
    for the rest carries the brief's own window (pxx P2-2: a bare `owed --state active` reads 14 days)."""
    section = section if isinstance(section, dict) else {}
    items = [r for r in section.get("items", []) if isinstance(r, dict)]
    counts = section.get("counts") if isinstance(section.get("counts"), dict) else {}
    for title, wanted, oldest_first in (("reply owed", OWED_BARE_STATE, False),
                                         ("replied in thread, not linked", OWED_ACTIVE_STATE, True)):
        # Only a row the route calls active leaves "reply owed": one with no state, or one this client does not know,
        # stays an ask to answer, never hidden.
        rows = [r for r in items if (r.get("state") == OWED_ACTIVE_STATE) == (wanted == OWED_ACTIVE_STATE)]
        rows = rows[::-1] if oldest_first else rows
        total = counts.get(wanted)
        suffix = f" of {total}" if isinstance(total, int) and total > len(rows) else ""
        suffix += " (truncated)" if section.get("truncated") else ""
        print(f"{title}: {len(rows)}{suffix}")
        for row in rows[:10]:
            hit = row.get("hit", {}) if isinstance(row.get("hit"), dict) else {}
            print(f"  {hit.get('key', '?')}  {hit.get('author') or row.get('author', '?')}  "
                  f"{_brief_age(hit.get('recorded_at'))}  {row.get('state', '?')}  {row.get('done_when') or '-'}"
                  f"{_expiry_cell(row)}")
        _brief_more(rows)
        if wanted == OWED_ACTIVE_STATE and (rows or (isinstance(total, int) and total)):
            print("  (you posted in the thread after each, not as a reply: read it, then reply or --closes it; "
                  f"`{_owed_list_cmd(OWED_ACTIVE_STATE, since, None)}` lists them all)")


def _brief_answered_count(bucket, since: str | None) -> None:
    """The answered bucket as one count line in the text view; its rows stay in --json and in the command it names."""
    count = bucket.get("count") if isinstance(bucket, dict) else None
    if not isinstance(count, int):
        print("answered, not closed: not available")
        return
    suffix = " (truncated)" if bucket.get("truncated") else ""
    print(f"answered, not closed: {count}{suffix}"
          + (f"  (`{_owed_list_cmd(OWED_ANSWERED_STATE, since, None)}` lists them)" if count else ""))


def _print_brief(answer: dict, since: str | None) -> None:
    """The route's answer, one section after another; routing only, as the route gives it."""
    _brief_owed_split(answer.get("owed"), since)
    _brief_answered_count(answer.get("answered_still_open"), since)
    if answer.get("awaiting_timed_out") is True:
        # the server's awaiting read passed its bound: unknown, which is not "nothing awaited"
        print("awaiting: unknown (the server's read timed out; ask again, or `awaiting` with a shorter --since)")
    else:
        _brief_rows("awaiting", answer.get("awaiting"), "owing_label")
    if isinstance(answer.get("to_close"), dict):
        to_close = answer["to_close"]
        tc_items = [r for r in to_close.get("items", []) if isinstance(r, dict)]
        print(f"to close: {len(tc_items)}" + (" (truncated)" if to_close.get("truncated") else ""))
        for row in tc_items[:10]:
            print(f"  {row.get('key', '?')}  {row.get('owing_label') or '?'}  "
                  f"reply {row.get('latest_reply_key') or '?'} ({_brief_age(row.get('latest_reply_at'))})  "
                  f"{row.get('thread_id') or '-'}")
        _brief_more(tc_items)
        if tc_items:
            print("  (non-paging; read latest reply before running close-thread)")
    threads = answer.get("threads") if isinstance(answer.get("threads"), dict) else {}
    items = threads.get("items", [])
    print(f"threads with news: {len(items)}" + (" (truncated)" if threads.get("truncated") else ""))
    for row in items[:10]:
        print(f"  {row.get('thread_id', '?')}  {row.get('new', '?')} new, last {row.get('last_author') or '?'}  "
              f"{_brief_age(row.get('last_recorded_at'))}")
    _brief_more(items)
    wanted = answer.get("help_wanted") if isinstance(answer.get("help_wanted"), dict) else {}
    items = wanted.get("items", [])
    print(f"help wanted: {len(items)}" + (" (truncated)" if wanted.get("truncated") else ""))
    for row in items[:10]:
        print(f"  {row.get('key', '?')}  {row.get('author') or '?'}  {_brief_age(row.get('recorded_at'))}  "
              f"{row.get('thread_id') or '-'}")
    _brief_more(items)


def _brief_more(items) -> None:
    """Every section prints ten rows and says how many it did not (codex on #99: the route sends up to 20)."""
    if len(items) > 10:
        print(f"  and {len(items) - 10} more")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", required=True,
                        help="the server, e.g. http://127.0.0.1:8765. REQUIRED, no default")
    common.add_argument("--state-file", required=True, type=Path,
                        help="this node's state file (holds its PRIVATE key)")

    ap = argparse.ArgumentParser(prog="python -m tools.meshy_node",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    mint = sub.add_parser("mint", parents=[common], help="redeem a pairing code")
    mint.add_argument("--code", required=True,
                      help="a pairing code, from the owner (`pair`) or from any node (`invite`)")
    mint.add_argument("--device-label", default="meshy-node")
    mint.add_argument("--memory-peer", default=node.DEFAULT_MEMORY_PEER_ID,
                      help="the memory authority's peer id (default: the fleet's)")

    sub.add_parser("signin", parents=[common], help="re-mint the JWT with the node's key")

    # ---- the operator's key: the fresh-install runbook in README.md
    keygen = sub.add_parser("keygen", parents=[common],
                            help="OPERATOR: generate the key bootstrap_owner registers")
    keygen.add_argument("--public-key-out", required=True, type=Path,
                        help="where to write the public key, for manage.py bootstrap_owner "
                             "--public-key-file")
    keygen.add_argument("--memory-peer", default=node.DEFAULT_MEMORY_PEER_ID,
                        help="the memory authority's peer id (default: the fleet's)")

    bind = sub.add_parser("bind", parents=[common],
                          help="OPERATOR: bind the key to the signer peer bootstrap_owner "
                               "printed, proven by a sign-in")
    bind.add_argument("--peer-id", required=True,
                      help="`peer_id` from manage.py bootstrap_owner's output")
    bind.add_argument("--kid", default=None,
                      help="`peer_kid` from the same output (default: derived from the key "
                           "by the same rule)")

    adopt = sub.add_parser("adopt", parents=[common],
                           help="a node that already has an identity (a migrated AgentMeshy "
                                "node): write this client's state file from the old one, "
                                "proven by a sign-in, without minting a second peer")
    adopt.add_argument("--from-state", required=True, type=Path,
                       help="the old client's state file; read only, never changed")
    adopt.add_argument("--memory-peer", default=node.DEFAULT_MEMORY_PEER_ID,
                       help="the memory authority's peer id (default: the fleet's)")

    pair = sub.add_parser("pair", parents=[common],
                          help="OPERATOR: owner sign-in, then single-use pairing codes")
    pair.add_argument("--count", type=int, default=1)

    invite = sub.add_parser("invite", parents=[common],
                            help="mint single-use pairing codes AS THIS NODE, for its own owner "
                                 "(no owner sign-in; any active peer may)")
    invite.add_argument("--count", type=int, default=1)

    claim = sub.add_parser("claim", parents=[common],
                           help="OPERATOR: namespace.claim, as the bootstrap authority")
    claim.add_argument("--namespace", required=True)
    claim.add_argument("--retention-policy", choices=("keep_all", "high_churn", "ephemeral"),
                       default=None, help="fixed at claim (server default: keep_all)")

    grant = sub.add_parser("grant", parents=[common],
                           help="OPERATOR: memory.grant a peer read/write/admin, or revoke")
    grant.add_argument("--namespace", required=True)
    grant.add_argument("--grantee", required=True, help="the grantee's peer id")
    how = grant.add_mutually_exclusive_group(required=True)
    how.add_argument("--permission", choices=("read", "write", "admin"))
    how.add_argument("--revoke", action="store_true")
    grant.add_argument("--expires-at", default=None,
                       help="ISO-8601; required by the server for a cross-tenant grant (I1)")

    send = sub.add_parser("send", parents=[common], help="send a mesh message v1")
    send.add_argument("--no-recipient-check", action="store_true",
                      help="skip the warning (never a refusal) about recipients that are asleep or behind on "
                           "their mail (divergence 083)")
    send.add_argument("--no-card-check", action="store_true",
                      help="skip the line (never a refusal) that names another seat's open Commitment or "
                           "unanswered Question in this thread (divergence 091, tools/meshy_node/cards.py)")
    send.add_argument("--to", action="append", required=True,
                      help="recipient label(s), comma-separated or repeated; a message is never "
                           "delivered to its own author, so your own label here reaches nobody")
    send.add_argument("--cc", action="append")
    send.add_argument("--thread", required=True, help="thread_id")
    send.add_argument("--type", default="note", dest="msg_type")
    body = send.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file", type=Path)
    send.add_argument("--in-reply-to", help="the msg/ key this message answers")
    send.add_argument("--keep-open", action="store_true",
                      help="with --in-reply-to, leave an owed question answered instead of closing it")
    send.add_argument("--closes", action="append", metavar="MSG_KEY",
                      help="explicitly settle this msg/ ask (repeatable)")
    send.add_argument("--declines", action="append", metavar="MSG_KEY",
                      help="explicitly decline this msg/ ask after reading it (repeatable)")
    send.add_argument("--force-blind-close", action="store_true",
                      help="allow --declines without a prior verified read in the local read ledger")
    send.add_argument("--from", dest="sender",
                      help="sender label (default: the label `inbox` reported, else peer-<id8>)")
    send.add_argument("--author-family", default="unknown")
    send.add_argument("--requires-ack", "--expects-reply", dest="requires_ack", action="store_true",
                      help="mark this message as requiring an answer (--expects-reply is an alias)")
    send.add_argument("--done-when", metavar="TEXT",
                      help="completion condition for a required answer (requires --requires-ack or --expects-reply)")
    send.add_argument("--subject",
                      help="a slug the server accepts, never prose: 1 to 256 characters, a letter or digit, then only "
                           "letters, digits and _ . : / - (e.g. pr/933); refused before anything is sent otherwise")
    send.add_argument("--ref",
                      help="as --subject, at most 128 characters (e.g. a commit sha or pr/933)")
    send.add_argument("--session-label")
    send.add_argument("--allow-local-path", action="store_true",
                      help="send even though the body names a FILE on this machine (no reader can open it; see `share`). "
                           "A folder or a path that is no file here is sent without it, with a warning")
    send.add_argument("--bulk-ack", metavar="REASON",
                      help=f"send although this identity has already sent {sendguard.MAX_PER_RECIPIENT} messages of "
                           f"{sendguard.COUNTED_MIN_BYTES} bytes or more naming a recipient in the last "
                           f"{sendguard.WINDOW_S // 60} minutes, or the body is over "
                           f"{sendguard.MAX_BODY_BYTES} bytes. The reason is written into the message the recipient reads. "
                           "A bundle of files belongs in a commit (`share`), a log or report in --attach, not inline.")
    send.add_argument("--operator-words-file", type=Path, metavar="FILE",
                      help="an operator voice only (memory-bxxxxx, oxxxxxx, kxxx, ixxxxxx): the operator's own words, verbatim, one "
                           "instruction per non-empty line, sent in the message's operator_words field. The server "
                           "refuses it from any other seat. Recipients on the to line read it as his instruction "
                           "(threat_model.md, amendment of 28 Oct 2026)")
    send.add_argument("--directive-file", type=Path, metavar="FILE",
                      help="a roster seat only (the directive rule): your instruction to ONE seat, one line per instruction, "
                           "sent in the message's directive field. The target reads it as the operator typing in its "
                           "own window (threat_model.md, amendment of 2 Nov 2026). Needs --directive-to; the server "
                           "refuses it from a seat off the roster, for a target off the to line, or when the switch "
                           "is off. Hard limits (credentials, deletes, spend, new accounts, the Pxx) need Bxxxxx's own "
                           "words, not a directive")
    send.add_argument("--directive-to", metavar="LABEL",
                      help="the ONE seat the directive is for; must also be on --to. Fan-out is one message per seat")
    send.add_argument("--directive-hours", type=float, default=None, metavar="H",
                      help="hours until the directive expires (default 4, or window_end + 30 min with --window-end; "
                           "at most 24)")
    send.add_argument("--window-end", metavar="ISO_Z",
                      help="with --directive-file or --operator-words-file: the end of the window the words are for, "
                           "an ISO 8601 UTC instant ending in Z (for example 2026-11-11T11:45:00Z). A directive then "
                           "expires 30 min after it at the latest (and by default), and a reader treats relayed words "
                           "as expired 30 min after it. Without it, relayed words NEVER expire (Bxxxxx's 28 Oct "
                           "rule): name a window whenever the words are for one")
    send.add_argument("--urgent", action="store_true",
                      help="deliver first, ahead of ordinary mail, to the ONE seat on --to (the urgent lane; delivery "
                           "order only, never authority). At most 4 per hour per seat: past that, the server refuses it "
                           "and this send resends it as ordinary, marked urgent_declined, unless --no-urgent-fallback "
                           "or --op-id is given (then the refusal is returned and the caller decides)")
    send.add_argument("--no-urgent-fallback", action="store_true",
                      help="with --urgent: return the server's refusal instead of resending as ordinary")
    send.add_argument("--urgent-declined", action="store_true",
                      help="send as ordinary, marked as an urgent message the server turned down for this seat's "
                           "hourly allowance (your own label: it is delivered as ordinary)")
    send.add_argument("--attach", action="append", type=Path, metavar="FILE",
                      help=f"upload FILE to the server's file service and cite it in the message (repeatable, at most "
                           f"{wire.MAX_ATTACHMENTS}). Every file is uploaded and verified BEFORE the message is sent, and "
                           "any failure sends nothing. Any peer of your owner can download it, recipient or not: never "
                           "attach firmware, calibration or keys (divergence 079)")
    send.add_argument("--op-id", default=None,
                      help="caller-held send identity; retry this exact operation with the same value")
    send.add_argument("--hints-check", action="store_true",
                      help="query POST /v1/mesh/hints before starting a new thread (no --in-reply-to) and print "
                           "at most 3 calibrated collision hints to stderr; advisory only, fails open")
    send.add_argument("--no-hints-check", action="store_true",
                      help="skip the advisory pre-send hints check even when MESHY_SEND_HINTS_CHECK=1 is set")

    send_status = sub.add_parser("send-status", parents=[common], help=argparse.SUPPRESS)
    send_status.add_argument("op_id", help="operation identity from `send --op-id`")
    send_status.add_argument("--peek", action="store_true",
                             help="report the journal's state only: no lock, no server call, nothing written "
                                  "(without it, an unsettled op may be presented again)")
    send_drain = sub.add_parser("send-drain", parents=[common], help=argparse.SUPPRESS)

    docshare.add_parser(sub)
    offer.add_parsers(sub, common)     # `offer` and `pickup`: private bytes over the LAN, never through the server

    fetch = sub.add_parser("fetch", parents=[common],
                           help="download every attachment of one mesh message, each verified against the size and "
                                "sha256 the message names (divergence 079); exit 1 on any failure")
    fetch.add_argument("--key", required=True, help="the msg/ key of the message")
    fetch.add_argument("--out", type=Path, default=None,
                       help="the directory to write into (default: the current directory). It must exist, and a "
                            "file already there is never replaced")

    edit = sub.add_parser("edit", parents=[common],
                          help="edit the body of a message this node's owner sent (memory.edit): a new signed "
                               "version; to, cc, thread and `from` cannot change")
    edit.add_argument("--key", required=True, help="the msg/ key of the message to edit")
    edit_body = edit.add_mutually_exclusive_group(required=True)
    edit_body.add_argument("--body", help="the new body")
    edit_body.add_argument("--body-file", type=Path)
    edit.add_argument("--expected-head-version", type=int, default=None,
                      help="the head version you are editing (default: the head just read); a head that "
                           "moved is refused, never overwritten")

    redact = sub.add_parser("redact", parents=[common],
                            help="blank the text of a message or card this node's owner wrote (memory.redact). It "
                                 "cannot be undone; the key, to, cc and thread stay. Exit 0 done, 1 refused, 3 done "
                                 "but text remains in a version another owner wrote")
    redact.add_argument("--key", required=True, help="the key of the row: a msg/ key, or a card/ key with "
                                                     "--namespace shared/decisions")
    redact.add_argument("--namespace", default=wire.MESH_NAMESPACE,
                        help=f"the row's namespace (default {wire.MESH_NAMESPACE})")
    redact.add_argument("--version", required=True, type=_redact_version,
                        help='the version to redact, or "all". Required: the server\'s default is every version, '
                             "and a redaction cannot be undone")
    redact.add_argument("--reason", required=True, choices=node.REDACT_REASON_CODES,
                        help="why, from the server's closed list; there is no free text")
    redact.add_argument("--blank-thread-id", action="store_true",
                        help="also blank the thread id, which is free text a sender chose")

    inbox = sub.add_parser("inbox", parents=[common], help="GET /v1/mesh/inbox")
    inbox.add_argument("--limit", type=int, default=50)
    inbox.add_argument("--no-cc", action="store_true")
    inbox.add_argument("--bodies", action="store_true",
                       help="read each hit through `read` (one signed memory.read per hit) "
                            "and attach it as `read`, or a typed `read_error`")

    owed = sub.add_parser("owed", parents=[common],
                          help="GET /v1/mesh/owed: acks this node owes")
    owed.add_argument("--limit", type=int)
    owed.add_argument("--state", default=None,
                      help="comma-separated states to include: open,active,answered,declined,closed; `all` asks for every "
                           "state. Without it, `owed` lists the open asks (still waiting for a reply) and summarises "
                           f"the active and answered ones, and --counts-only reads {OWED_DEFAULT_STATE}. Exits 1 "
                           "without printing a list when the server does not echo the filter (a server before "
                           "divergence 070 ignores it)")
    owed.add_argument("--counts-only", action="store_true",
                      help="print only reply-owed and answered-still-open counts, never routing rows")
    owed.add_argument("--since", default=OWED_DEFAULT_SINCE,
                      help="ISO-8601 with offset/Z, or relative age such as 14d or 36h (default "
                           f"{OWED_DEFAULT_SINCE}); `all` asks for every ask ever owed, imported history "
                           "included. Refused the same way when the server does not echo it")
    owed.add_argument("--thread", help="only the asks in this thread_id (#155), with --state and --since as "
                                       "given. Refused the same way when the server does not echo it (a server "
                                       "before #155 ignores it)")
    owed.add_argument("--reconcile", action="store_true",
                      help="sort every ask in the open, active and answered lists (one call each, --since and "
                           "--thread as given) into ONE class with its evidence: live_work, edge_only, "
                           "expired_unreviewed, intentional_fanout, retransmission, answered, closed, unknown; the "
                           "counts balance. Read only: it sends and acknowledges nothing, and its signed reads mark "
                           "nothing read (tools/meshy_node/reconcile.py). Exits 1 when they do not balance")
    owed.add_argument("--closures", help="with --reconcile: comma-separated msg/ keys of closure receipts. One closes "
                                         "an ask when its signature verifies, oxxxxxx or this seat signed it, and "
                                         "its `closes` list names the ask's key exactly")
    owed.add_argument("--expire", help=f"with --reconcile: an open ask older than this is expired_unreviewed "
                                       f"(Nh or Nd; default {OWED_RECONCILE_EXPIRE})")
    owed.add_argument("--max-reads", type=int,
                      help=f"with --reconcile: signed reads made at most, the receipts first, then the fan-out "
                           f"candidates oldest first (default {OWED_RECONCILE_MAX_READS}; 0 reads nothing)")
    owed.add_argument("--json", action="store_true", help="with --reconcile: the whole view as one JSON object")

    unread = sub.add_parser("unread", parents=[common],
                            help="GET /v1/mesh/unread: mail addressed to this node whose current "
                                 "version it has not fetched with a completed read (server-side)")
    window = unread.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="look back this many days (server default 7)")
    window.add_argument("--since", help="ISO-8601 with offset/Z, or relative age such as 36h; the "
                                        "server caps it below its call retention and says so")
    unread.add_argument("--limit", type=int, help="items to list, 1..500 (server default 100)")

    fleet = sub.add_parser("fleet-unread", parents=[common],
                           help="GET /v1/mesh/fleet/unread: every seat's fetch backlog as counts "
                                "(bootstrap authority or the mesh:backlog:read scope); with --peer, "
                                "one seat's waiting messages (bootstrap authority only)")
    fleet.add_argument("--peer", help="a peer id: list that peer's waiting messages instead of counts")
    fwindow = fleet.add_mutually_exclusive_group()
    fwindow.add_argument("--days", type=int, help="look back this many days (server default 7)")
    fwindow.add_argument("--since", help="ISO-8601 with offset/Z, or relative age such as 36h; the "
                                         "server caps it below its call retention and says so")
    fleet.add_argument("--limit", type=int, help="with --peer only: items to list, 1..500 "
                                                 "(server default 100)")

    fleetv = sub.add_parser("fleet", parents=[common],
                            help="every named seat's presence and mail backlog in one table, from "
                                 "GET /v1/mesh/recipients (counts only, open to any mesh reader)")
    fleetv.add_argument("--seats", required=True, help="comma-separated seat labels (asked 10 at a time)")
    fleetv.add_argument("--json", action="store_true", help="the rows as JSON instead of the table")
    fleetv.add_argument("--work", action="store_true",
                        help="on-demand mail facts and the seat's recent reported turn state")
    fleetv.add_argument("--idle-over", type=int, metavar="MINUTES",
                        help="with --work, list idle/quiet seats with unread or open owed work; exit 3 when any qualify")
    fleetv.add_argument("--steerable", action="store_true",
                        help="a steer column: each seat's newest signed conformance result by another seat "
                             "(thread conformance-<seat>; see the conformance command), one thread read per seat")
    fleetv.add_argument("--steer-by", metavar="LABELS",
                        help="with --steerable: count only results written by these comma-separated seats (default: "
                             "the seat's coordinator from conformance.COORDINATORS; any other seat when it has none)")

    conf = sub.add_parser("conformance", parents=[common],
                          help="steering conformance (plan item 5): a plain request to change the seat's canary must "
                               "be refused and a seat directive to change it done; posts the result on "
                               "conformance-<seat>")
    conf.add_argument("--seat", required=True, help="the seat to check")
    conf.add_argument("--coordinator", required=True, help="your own seat label, as the probes sign it")
    conf.add_argument("--sender", action="append", required=True, metavar="ARG",
                      help="your OWN gated sender as argv, one --sender per element (python, then the script); it "
                           "must take meshy_node send's options, as kxxx_send does, including --directive-file")
    conf.add_argument("--canary-path", help="the seat's canary file, when it is on THIS box: it is read as well")
    conf.add_argument("--timeout", type=int, default=900, help="seconds to wait for each reply (default 900)")
    conf.add_argument("--no-record", action="store_true", help="do not post the CONFORMANCE line")

    turn_cmd = sub.add_parser("turn", parents=[common], help="publish this seat's turn state and newest record time")
    turn_cmd.add_argument("--state", required=True, choices=["open", "closed"])
    turn_cmd.add_argument("--item-at", required=True, help="newest transcript record timestamp with timezone")
    turn_cmd.add_argument("--unsurfaced", type=int, help="aged pending count (held while open), or -1 for incomplete evidence")
    turn_cmd.add_argument("--unsurfaced-since", help="coverage baseline timestamp; required with count")
    turn_cmd.add_argument("--unsurfaced-oldest-at", help="oldest qualifying dispatch timestamp with timezone")
    turn_cmd.add_argument("--usage-file", help="a JSON file with this seat's usage snapshot: as_of (when it was "
                          "observed) and optionally model, context_used_pct and limits")

    pres = sub.add_parser("presence", parents=[common],
                          help="POST /v1/peers/me/presence: publish this seat's state (for its hooks: "
                               "asleep + note compacting before a compaction, active at session start)")
    pres.add_argument("--state", required=True, choices=["active", "idle", "asleep"])
    pres.add_argument("--note", help="a short reason, e.g. compacting")
    pres.add_argument("--ttl", type=int, help="seconds the state stays true unless re-published (1..86400); "
                                             "past it readers see effective_state unknown")
    thread = sub.add_parser("thread", parents=[common], help="GET /v1/mesh/threads/<thread_id>")
    thread.add_argument("--id", required=True, help="the thread id, including any / or :")
    thread.add_argument("--limit", type=int)
    thread.add_argument("--after", default=None,
                        help="only the messages after this msg/ key (an answer's next_after pages on)")
    thread.add_argument("--order", choices=("stream", "causal"), default=None,
                        help="stream (chronological default) or causal (reply-tree order with unresolved_refs)")
    thread.add_argument("--envelope", action="store_true",
                        help="each message you may read comes with its content (a DPoP-proved GET); not a read: "
                             "nothing is marked read")
    thread.add_argument("--provenance", action="store_true",
                        help="with --envelope: verify Ed25519 author signatures, bind memo key/version/fields to "
                             "each hit, set key_source, and strip relay/directive fields")

    rnd = sub.add_parser("round", parents=[common],
                         help="which roster seats have posted a round's fixed first line on one thread (SD 255): the "
                              "whole thread, each roster post read signed, counted by the signing peer")
    rnd.add_argument("--thread", required=True, help="the round's thread id")
    rnd.add_argument("--seats", required=True, help="the roster, comma-separated labels")
    rnd.add_argument("--item", required=True,
                     help="the ITEM value the round's first line carries, matched exactly; a seat's later post with "
                          "another ITEM (a collation, a correction of something else) does not replace its reply")
    rnd.add_argument("--prefix", default="VERDICT",
                     help="count only first lines whose first key is this (default VERDICT, the round's reply; "
                          "RESULT for a stand-in round); ANY counts any first key")
    rnd.add_argument("--max-reads", type=int, default=None,
                     help="signed reads at most, newest post first (default 60); what it leaves unread is named")
    rnd.add_argument("--json", action="store_true", help="the whole answer as one JSON object")

    hpf = sub.add_parser("handoff-preflight", parents=[common],
                         help="before recording a review verdict on the board: can the board record it, and if not, "
                              "what first (SD 265 item 1; tools/meshy_node/handoff.py). It reads the facts file and "
                              "oxxxxxx's cap registry cards, calls no board and writes nothing. The facts are gathered "
                              "by this seat's own board reads, so it prevents mistakes, not misreporting. Exits 0 "
                              "recordable, 1 not recordable, 2 when it cannot judge")
    hpf.add_argument("--facts", required=True, type=Path,
                     help="the facts file, schema handoff-facts/1: issue, verdict, execution_run_id, bound_handoff, "
                          "rounds_recorded, max_review_rounds, result_cap, reads (each with its time and the sha256 "
                          "of the response); no other field")
    hpf.add_argument("--registry-thread", default=HANDOFF_REGISTRY_THREAD,
                     help=f"the thread of oxxxxxx's cap registry cards (default {HANDOFF_REGISTRY_THREAD})")
    hpf.add_argument("--json", action="store_true", help="the whole answer as one JSON object")

    mentions = sub.add_parser("mentions", parents=[common], help="GET /v1/mesh/mentions")
    mentions.add_argument("--ref", required=True)
    mentions.add_argument("--limit", type=int)
    mentions.add_argument("--by-peer", action="store_true",
                          help="who cites it: the hits grouped by the peer that signed each (divergence 090)")
    awaiting = sub.add_parser("awaiting", parents=[common], help="GET /v1/mesh/awaiting")
    awaiting.add_argument("--state", help="comma-separated states to include: open,active,answered,declined,closed")
    awaiting.add_argument("--since")
    close_cmd = sub.add_parser("close-thread", parents=[common],
                               help="settle answered asks you authored in GET /v1/mesh/brief to_close after reading "
                                    "each ask's latest reply")
    close_target = close_cmd.add_mutually_exclusive_group(required=True)
    close_target.add_argument("--thread", help="settle every settled answered ask you authored in this thread")
    close_target.add_argument("--key", action="append", metavar="MSG_KEY",
                              help="settle specific settled answered msg/ ask(s) you authored (repeatable)")
    close_cmd.add_argument("--to", action="append",
                           help="override recipient label(s), comma-separated or repeated (default: your own label)")
    close_cmd.add_argument("--notify", action="store_true",
                           help="address the settlement note to the owing seat(s) instead of your own label")
    close_cmd.add_argument("--body", default="Settling answered ask(s) after reading latest reply.",
                           help="note body for the settlement message")
    close_cmd.add_argument("--since", default="14d", help="relative age or ISO timestamp window (default 14d)")
    close_cmd.add_argument("--dry-run", action="store_true",
                           help="verify read-ledger custody and print the settlement plan without sending")
    close_cmd.add_argument("--force-blind-close", action="store_true",
                           help="allow closing even if latest_reply_key is absent from the local read ledger")
    close_cmd.add_argument("--author-family", default="unknown")
    close_cmd.add_argument("--from", dest="sender", help="sender label")
    close_cmd.add_argument("--bulk-ack", metavar="REASON", help="sendguard override reason when needed")
    brief = sub.add_parser("brief", parents=[common],
                           help="GET /v1/mesh/brief: owed, awaiting, threads with news, help wanted "
                                "(composed from owed and awaiting on a server without the route)")
    brief.add_argument("--since", default="14d", help="relative age or ISO timestamp (default 14d)")
    brief.add_argument("--json", action="store_true",
                       help="print the answer as one JSON object: the route's, or {\"composed\": true, \"owed\", "
                            "\"awaiting\"} on an older server (its one-line notice then goes to stderr)")

    search = sub.add_parser("search", parents=[common],
                            help="GET /v1/mesh/search: the MAIL. The cards are `card search`")
    search.add_argument("--q", required=True)
    search.add_argument("--k", type=int, default=10)
    # On unless --no-rerank, as on the server (divergence 026, as amended: on a live
    # paraphrase test the reranker put the target first in 10/12 queries against 5/12).
    # --rerank is still accepted and means the default.
    search.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=True,
                        help="rerank the fused top 40 (the default; --no-rerank sends "
                             "rerank=0)")

    tx_search = sub.add_parser(
        "transcript-search", parents=[common],
        help="GET /v1/mesh/search over shared/transcript-context: session turns across seats (untrusted, may hold secrets)")
    tx_search.add_argument("--q", required=True)
    tx_search.add_argument("--seat", default=None)
    tx_search.add_argument("--src", default=None)
    tx_search.add_argument("--since", default=None)
    tx_search.add_argument("--until", default=None)
    tx_search.add_argument("--collapse", default="chunk", help="chunk|item|session (default chunk)")
    tx_search.add_argument("--k", type=int, default=10)
    tx_search.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=True,
                           help="rerank the fused candidate pool (default; --no-rerank sends rerank=0)")
    tx_search.add_argument("--namespace", default=None,
                           help=f"transcript namespace (MESHY_CONTEXT_NAMESPACE, else {transcript_graph.DEFAULT_TRANSCRIPT_NAMESPACE})")
    tx_search.add_argument("--json", dest="as_json", action="store_true",
                           help="print JSON (carries _untrusted_transcript_evidence: true)")

    tx_usage = sub.add_parser(
        "transcript-usage", parents=[common],
        help="GET /v1/mesh/transcript-usage: how often each seat searched shared/transcript-context, per UTC day and seat")
    tx_usage.add_argument("--days", type=int, help="1..90, default 7")

    rb = sub.add_parser(
        "resume-brief", parents=[common],
        help="what this seat owes, waits on, its PRs and its own recent work, as plain text cut to --budget "
             "characters: for a fresh session's start (SessionStart hook) and after a compaction")
    rb.add_argument("--seat", required=True, help="this seat's name, as its transcript uploads are labelled")
    rb.add_argument("--budget", type=int, default=6000,
                    help=f"characters, at least {resume_brief.MIN_BUDGET} (800 at session start, 6000 after a compaction)")
    rb.add_argument("--repo", default=resume_brief.DEFAULT_REPO,
                    help="the GitHub repository whose PRs are listed; a PR number read from an owed ask's thread id is "
                         "looked up here too, so a thread about another repository's PR of that number reads wrong")
    rb.add_argument("--gate-file", type=Path, default=None,
                    help="the landing gate's status JSON (hqb-gate.json); older than 5 min reads unavailable")
    rb.add_argument("--handover-file", type=Path, default=None,
                    help="this seat's handover file: its NEXT ACTION line is shown")
    rb.add_argument("--hook", action="store_true",
                    help="for a SessionStart hook: any error prints one line and exits 0, so the session is never failed")

    tx_read = sub.add_parser(
        "transcript-read", parents=[common],
        help="read exact ctx/ or ctxm/ transcript items (or <= 32 contiguous ordinals) with author signature checks")
    tx_read.add_argument("--key", default=None, help="exact ctx/... or ctxm/... key")
    tx_read.add_argument("--seat", default=None)
    tx_read.add_argument("--src", default=None)
    tx_read.add_argument("--gen", type=int, default=None)
    tx_read.add_argument("--batch", type=int, default=None)
    tx_read.add_argument("--first-ord", type=int, default=None)
    tx_read.add_argument("--last-ord", type=int, default=None)
    tx_read.add_argument("--namespace", default=None,
                         help=f"transcript namespace (MESHY_CONTEXT_NAMESPACE, else {transcript_graph.DEFAULT_TRANSCRIPT_NAMESPACE})")
    tx_read.add_argument("--require-signature", action="store_true", default=False,
                         help="exit 1 (E_AUTHOR_UNVERIFIED) when any requested row is missing or fails Ed25519 author signature verification")
    tx_read.add_argument("--json", dest="as_json", action="store_true",
                         help="print JSON (carries _untrusted_transcript_evidence: true)")

    tx_cons = sub.add_parser(
        "transcript-consensus", parents=[common],
        help="build the shared fleet transcript & diagnostic consensus across seats from MeshyChat graph stores")
    tx_cons.add_argument("--q", default=None, help="optional topic query for session hits across seats")
    tx_cons.add_argument("--seat", default=None, help="filter consensus to one lowercase seat label")
    tx_cons.add_argument("--card-namespace", default=None,
                         help=f"decision card namespace (MESHY_CARDS_NAMESPACE, else {cards.DEFAULT_NAMESPACE})")
    tx_cons.add_argument("--transcript-namespace", default=None,
                         help=f"transcript namespace (MESHY_CONTEXT_NAMESPACE, else {transcript_graph.DEFAULT_TRANSCRIPT_NAMESPACE})")
    tx_cons.add_argument("--stale-hours", type=float, default=6.0,
                         help="hours after which a seat's Context card is marked stale (default 6)")
    tx_cons.add_argument("--stale-ingest-hours", type=float, default=transcript_graph.DEFAULT_STALE_INGEST_HOURS,
                         help="hours without any emitted decision card before flagging stale_card_ingestion (default 36)")
    tx_cons.add_argument("--stale-transcript-hours", type=float, default=transcript_graph.DEFAULT_STALE_TRANSCRIPT_HOURS,
                         help="hours of transcript-context silence or lag behind latest card before flagging transcript_lag (default 6)")
    tx_cons.add_argument("--upload-mode", action="append", default=[], metavar="SEAT=MODE",
                         help="per-seat upload mode (<seat>=continuous or <seat>=one_off); one_off lag stays informational")
    tx_cons.add_argument("--json", dest="as_json", action="store_true",
                         help="print full structured JSON report instead of < 80 line summary")

    context = sub.add_parser("context", parents=[common], help="GET /v1/mesh/context")
    context.add_argument("--key", required=True)
    context.add_argument("--k", type=int, default=10)
    context.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=True,
                         help="rerank the related messages (the default; --no-rerank sends "
                              "rerank=0)")
    context.add_argument("--with-context", action="store_true",
                         help="attach conversation_context (cross-thread related_edges, active decision cards, "
                              "and condensed transcript-context pointers without raw transcript text)")

    neighbours = sub.add_parser("neighbours", parents=[common],
                                help="GET /v1/mesh/neighbours: one hop from a message")
    neighbours.add_argument("--key", required=True)
    neighbours.add_argument("--include-inferred", action="store_true",
                            help="also the persisted related_to edges the reranker wrote "
                                 "(divergence 032), each with its scores")
    neighbours.add_argument("--limit", type=int,
                            help="at most this many (the server allows 1..100)")

    sub.add_parser("lookup-help", parents=[common],
                   help="print which lookup answers which question, with this seat's own command line (issue #269): "
                        "the text the Claude channel keeps in a session's instructions, for a seat without the "
                        "channel (a doorbell seat, a Codex AGENTS.md, a hook). Calls no server")

    rollcall = sub.add_parser("rollcall", parents=[common],
                              help="ask each seat to answer and report who did: one request per "
                                   "seat with a token, then the inbox is polled for signed replies "
                                   "that carry it. Exit 1 when a seat stayed silent. It costs each "
                                   "seat a turn, so it is a check you run, not a monitor")
    rollcall.add_argument("--to", action="append", required=True,
                          help="seat label(s), comma-separated or repeated")
    rollcall.add_argument("--thread", help="thread_id (default: rollcall-<token>)")
    rollcall.add_argument("--timeout", type=float, default=300.0,
                          help="seconds to wait for replies (default 300: the inbox lags the log "
                               "and an agent needs a whole turn to answer)")
    rollcall.add_argument("--interval", type=float, default=10.0,
                          help="seconds between inbox polls (default 10)")
    rollcall.add_argument("--from", dest="sender",
                          help="sender label (default: as `send`)")
    rollcall.add_argument("--author-family", default="unknown")
    rollcall.add_argument("--expect", action="append", required=True, metavar="LABEL=PEER_ID",
                          help="the peer id of each seat in --to (repeatable, one per seat). A reply "
                               "counts for LABEL only when it was signed by PEER_ID; a label is "
                               "chosen by the writer and proves nothing by itself")

    summ = sub.add_parser("summarize", parents=[common],
                          help="GET /v1/mesh/summaries/thread or /backlog: a GENERATED, "
                               "UNVERIFIED summary (divergence 037)")
    which = summ.add_mutually_exclusive_group(required=True)
    which.add_argument("--thread", metavar="THREAD_ID", help="summarise this thread")
    which.add_argument("--backlog", action="store_true",
                       help="summarise what is addressed to this node after a cursor")
    summ.add_argument("--before", help="thread: a window.before cursor, for older messages")
    since = summ.add_mutually_exclusive_group()
    since.add_argument("--since", help="backlog: a /v1/mesh/wait cursor, or `start`")
    since.add_argument("--since-cursor-file", type=Path,
                       help="backlog: READ the cursor from the monitor's sidecar "
                            "(never written)")
    summ.add_argument("--max-messages", type=int, default=None,
                      help="at most this many messages (the server allows 1..40)")
    summ.add_argument("--deterministic", action="store_true",
                      help="request mode=deterministic (in-process extractive digest + declared facts, no LLM call)")
    summ.add_argument("--text", action="store_true",
                      help="print labelled lines instead of the JSON")

    listen = sub.add_parser("listen", parents=[common],
                            help="print mesh_message frames from /v1/ws")
    from . import monitor           # `listen --monitor` and its flags: monitor.py
    monitor.add_arguments(listen)
    listen.add_argument("--count", type=_at_least_one, default=None,
                        help="exit after this many mesh_message frames (at least 1)")
    listen.add_argument("--duration", type=float, default=None,
                        help="exit after this many seconds")
    listen.add_argument("--read", action="store_true",
                        help="print each mesh_message frame with the message it points at, "
                             "read through `read`, on one line (a failed read is a typed "
                             "`read_error` on that line, and listening continues)")

    card = sub.add_parser("card", help="decision cards (divergence 091): write, list, search, commit, done, recent")
    card_sub = card.add_subparsers(dest="card_cmd", required=True)
    card_write = card_sub.add_parser(
        "write", parents=[common],
        help="write cards from a file (one JSON object, an array, or one object a line). A key that already "
             "exists is left as it is; exit 3 when a redaction detector stopped a card, 1 when the server "
             "refused one")
    card_write.add_argument("--file", required=True, type=Path)
    card_write.add_argument("--update", action="store_true",
                            help="write a NEW VERSION of a card that exists (the server keeps its time, type, seat "
                                 "and source); without it an existing key is reported and not touched")
    card_write.add_argument("--dry-run", action="store_true",
                            help="check and screen the cards and say what would be sent; no call is made")
    card_write.add_argument("--namespace", default=None,
                            help=f"the card namespace (MESHY_CARDS_NAMESPACE, else {cards.DEFAULT_NAMESPACE})")
    card_list = card_sub.add_parser("list", parents=[common], help="GET /v1/cards: the cards, filtered, newest first")
    card_list.add_argument("--type", action="append", dest="types", choices=cards.TYPES,
                           help="repeatable; the answer also holds a card that REPLACES one of these")
    card_list.add_argument("--thread")
    card_list.add_argument("--seat")
    card_list.add_argument("--owner")
    card_list.add_argument("--status", choices=("active", "superseded", "any"), help="default: active")
    card_list.add_argument("--since", help="cards whose own time is at or after this instant; a bare date or an instant without milliseconds work too")
    card_list.add_argument("--until", help="cards whose own time is before this instant; a bare date means that whole day")
    card_list.add_argument("--recorded-since",
                           help="cards the SERVER recorded at or after this instant: what is new since a last read")
    card_list.add_argument("--limit", type=int, help="1..200, default 50")
    card_list.add_argument("--after", help="the `next` cursor of the page before")
    card_usage = card_sub.add_parser(
        "usage", parents=[common],
        help="GET /v1/cards/usage: how often each seat read the cards (list and search), per UTC day and outcome; "
             "diagnostics, a read is a fetch and not a use")
    card_usage.add_argument("--days", type=int, help="1..90, default 14")
    card_search = card_sub.add_parser(
        "search", parents=[common],
        help="GET /v1/mesh/search over the cards: by meaning, with the filters of `card list`. Active cards "
             "unless --status says otherwise. (`search` alone is the mail.)")
    card_search.add_argument("--q", required=True)
    card_search.add_argument("--k", type=int, default=10)
    card_search.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=True,
                             help="rerank the fused top 40 (the default; --no-rerank sends rerank=0)")
    card_search.add_argument("--type", action="append", dest="types", choices=cards.TYPES,
                             help="repeatable; the answer also holds a card that REPLACES one of these")
    card_search.add_argument("--thread")
    card_search.add_argument("--seat")
    card_search.add_argument("--owner")
    card_search.add_argument("--status", choices=("active", "superseded", "any"), help="default: active")
    card_search.add_argument("--since", help="cards whose own time is at or after this instant (as `card list`)")
    card_search.add_argument("--until", help="cards whose own time is before this instant; a bare date means that whole day (as `card list`)")
    card_search.add_argument("--namespace", default=None,
                             help=f"the card namespace (MESHY_CARDS_NAMESPACE, else {cards.DEFAULT_NAMESPACE})")
    card_commit = card_sub.add_parser(
        "commit", parents=[common],
        help="say what this seat is working on in a thread: writes a Commitment card other seats are shown "
             "before they send in that thread. Names the other seats' open Commitments of the thread first")
    card_commit.add_argument("--thread", required=True)
    card_commit.add_argument("--done-when", required=True, metavar="TEXT")
    card_commit.add_argument("--namespace", default=None)
    card_commit.add_argument("what")
    card_hook = card_sub.add_parser(
        "hook", parents=[common],
        help="meshy-mod's entry for the AskUserQuestion dialog (divergence 094): the call as JSON on stdin. "
             "Phase ask writes a Question card a question and names other seats' cards on the same question; "
             "phase answer writes a Ruling card a question the operator answered")
    card_hook.add_argument("--namespace", default=None)
    card_done = card_sub.add_parser(
        "done", parents=[common],
        help="close one of this seat's own Commitments: writes a Decision (owner: this seat) that supersedes it")
    card_done.add_argument("--namespace", default=None)
    card_done.add_argument("key", help="the Commitment's key, card/<seat>/<id>, as `card commit` printed it")
    card_done.add_argument("result")
    card_recent = card_sub.add_parser(
        "recent", parents=[common],
        help="a briefing for a session start, as plain text: this seat's newest active Rulings, how fresh its "
             "cards are, and the Rulings other seats recorded lately, quoted and said to be no instructions. Four "
             "core reads, optional cached seat freshness, ended within about 2 s; it always exits 0, with a "
             "'not available' line when it could not read")
    card_recent.add_argument("--seat", help="default: the label the server stamps for this peer")
    card_recent.add_argument("--n", type=_whole_within(1, 200), default=10,
                             help="this seat's Rulings shown, 1..200 (the route's own limit), default 10")
    card_recent.add_argument("--others", type=_whole_within(1, cards.RECENT_OTHERS_READ), default=8,
                             help=f"other seats' Rulings shown, 1..{cards.RECENT_OTHERS_READ} (one page of that "
                                  "many is read), default 8")
    card_recent.add_argument("--days", type=_whole_within(1, 30), default=3,
                             help="other seats' Rulings the server recorded in this many days, 1..30, default 3")
    card_recent.add_argument("--load-result", type=Path, default=None,
                             help="a card load's receipt, JSON {time, status, detail, seat}: shown as one line, "
                                  "with a NOTE when its status is not ok or skipped")
    card_recent.add_argument("--deadline", type=_seconds_within(0.5, cards.RECENT_DEADLINE_MAX_S), default=None,
                             help=f"seconds the whole run may take, 0.5 to {cards.RECENT_DEADLINE_MAX_S:g}; default "
                                  f"{cards.RECENT_DEADLINE_S:g} (the SessionStart hook passes more: a slower seat "
                                  "still gets its briefing, with the same reads)")
    card_recent.add_argument("--freshness-cache", type=Path, default=None,
                             help="remember seats seen so far and show those a day behind cards elsewhere; "
                                  "default: freshness.json beside --load-result, otherwise disabled")
    card_context = card_sub.add_parser(
        "context", parents=[common],
        help="show each seat's active Context snapshot and open Commitments, and flag overlapping branches, PRs, "
             "repo-relative files, or threads across seats (#297)")
    card_context.add_argument("--seat", help="filter to a single seat label")
    card_context.add_argument("--overlap", help="filter to seats referencing a PR (#302), branch, file, or thread")
    card_context.add_argument("--json", action="store_true", dest="as_json",
                              help="print structured JSON instead of the plain-text status board")

    read = sub.add_parser("read", parents=[common],
                          help="one mesh message through the signed memory.read path")
    read.add_argument("--key", required=True,
                      help="the key: a msg/ key in shared/mesh-chat, or e.g. wiki/<slug> with "
                           "--namespace shared/wiki")
    read.add_argument("--namespace", default=None,
                      help="the namespace to read (default shared/mesh-chat). Another namespace "
                           "prints the row's whole value, status and tags")
    read.add_argument("--version", type=int, default=None,
                      help="a specific version (default: the head)")
    read.add_argument("--with-context", action="store_true",
                      help="attach conversation_context (cross-thread related_edges, active decision cards, "
                           "and condensed transcript-context pointers without raw transcript text)")
    dcheck = sub.add_parser("directive-check", parents=[common],
                            help="the directive rule: re-read one directive right before acting on it, every check again "
                                 "(signature, roster, switch, target, expiry), including one this identity was shown "
                                 "before. Act only if it prints seat_directive")
    dcheck.add_argument("--key", required=True, help="the directive's msg/ key")
    ddone = sub.add_parser("directive-done", parents=[common],
                           help="the directive rule: record, once, the outcome of a directive this identity claimed")
    ddone.add_argument("--key", required=True, help="the directive's msg/ key")
    ddone.add_argument("--outcome", required=True, choices=("done", "refused", "unknown"))
    ddone.add_argument("--note", default="", help="one line on what happened (kept locally)")
    dcheck.add_argument("--resume", action="store_true",
                        help="show a directive this identity already claimed, only to finish that same work")

    vbundle = sub.add_parser(
        "verify-bundle",
        help="verify archive custody (.tar.gz/.tgz/.tar/.zip), streaming member safety, inner SHA256SUMS / "
             "MANIFEST.json completeness, and BEFORE/AFTER test-input provenance without extracting to disk",
    )
    vbundle.add_argument("path", type=Path, help="path to the .tar.gz / .tgz / .tar / .zip bundle")
    vbundle.add_argument("--expected-sha256", help="expected 64-hex outer archive SHA-256")
    vbundle.add_argument("--provenance-map", type=Path, default=None,
                         help="JSON file mapping test-input source paths to recorded SHA-256 digests")
    vbundle.add_argument("--base-url", default=None, help=argparse.SUPPRESS)
    vbundle.add_argument("--state-file", type=Path, default=None, help=argparse.SUPPRESS)

    gate = sub.add_parser(
        "gate-check",
        help="evaluate SHA-pinned review quorum (VERDICT: CODE GO / FIX FIRST), verifier standing, "
             "merge-tree conflicts, and path-scoped verdict freshness",
    )
    gate.add_argument("--base-url", default=None,
                      help="server URL when reading a live --thread (not needed with --messages-file)")
    gate.add_argument("--state-file", type=Path, default=None,
                      help="state file when reading a live --thread (not needed with --messages-file)")
    gate.add_argument("--thread", default=None, help="mesh thread_id to fetch signed review messages from")
    gate.add_argument(
        "--messages-file",
        type=Path,
        default=None,
        help="local JSON file of messages for offline predicate evaluation (identity, signature, and timestamp "
             "fields in the file are caller-asserted; not evidence of who approved unless --expected-peer maps "
             "required seats; in mixed-timestamp files, untimestamped unbound blocks sort before timestamped "
             "messages and do not latch across a later timestamped CODE GO for target_sha)",
    )
    gate.add_argument("--target-sha", required=True,
                      help="target commit SHA (>= 12 hex) or 64-hex archive SHA-256 that CODE GO verdicts must cite")
    gate.add_argument("--builder", default=None, help="builder seat label (enforces SELF_VERIFY and standing)")
    gate.add_argument("--builder-family", default=None, help="explicit builder family (claude/codex/gemini/kimi)")
    gate.add_argument("--required-seats", action="append", default=None,
                      help="comma-separated or repeated seat labels required for quorum")
    gate.add_argument("--required-families", action="append", default=None,
                      help="comma-separated or repeated model families required for quorum")
    gate.add_argument("--min-families", type=int, default=1,
                      help="minimum distinct model families required (default: 1)")
    gate.add_argument("--require-cross-family", action="store_true",
                      help="refuse same-family reviews with O2_FAMILY_MATCH")
    gate.add_argument("--require-operator-ruling", action="store_true",
                      help="require an active operator Ruling card in the thread")
    gate.add_argument("--expected-peer", action="append", default=None, metavar="LABEL=PEER_ID",
                      help="optional trusted seat-to-peer-id binding (repeatable)")
    gate.add_argument("--repo", type=Path, default=None,
                      help="optional git repository path to run read-only merge-tree conflict check")
    gate.add_argument("--trunk", default=None,
                      help="trunk ref/commit for --repo merge-tree conflict check (default: origin/main)")

    tasks_p = sub.add_parser(
        "tasks",
        help="this seat's open items on the Planboard board (Owner: lines, [seat:X] titles), read-only, through "
             "the installed planboard plugin's own MCP server; no MeshyChat server or state file",
    )
    tasks_p.add_argument("--seat", required=True, help="the seat label to list items for (e.g. kxxx)")
    tasks_p.add_argument("--alias", action="append", default=None,
                         help="another name the board may use for this seat (e.g. its postbox identity); repeatable")
    tasks_p.add_argument("--project", action="append", default=None,
                         help="a Planboard project id to scan (default: the Live AI roadmap); repeatable")
    tasks_p.add_argument("--issues-file", type=Path, default=None,
                         help="read issue records from a JSON file instead of the board (offline, tests)")
    tasks_p.add_argument("--owner-map", type=Path, default=None,
                         help='JSON {"owner text": "seat label"} for owner lines that name a seat another way')
    tasks_p.add_argument("--cursor-file", type=Path, default=None,
                         help="the resume cursor (default ~/.meshychat/tasks/<seat>.cursor.json)")
    tasks_p.add_argument("--update-cursor", action="store_true",
                         help="record this read as seen (local file only), so the next read marks only changes")
    tasks_p.add_argument("--no-cursor", action="store_true", help="no change marks: ignore the cursor")
    tasks_p.add_argument("--reader", default=None,
                         help="the seat whose board access reads (default: --seat); a Codex reader needs its own "
                              "operator decision, so a Claude seat lists a Codex seat's items with --reader <itself>")
    tasks_p.add_argument("--export", type=Path, default=None,
                         help="also write the records behind this list to a local file (0600) that a seat with no "
                              "board access reads with --issues-file")
    tasks_p.add_argument("--roster", action="append", default=None,
                         help="the MeshyChat seat labels an Owner line may name (comma-separated or repeated; default: "
                              "meshy-mod.json fleet_seats); any other owner value is a board agent")
    tasks_p.add_argument("--state-file", type=Path, default=None,
                         help="this seat's state file; for a Codex reader its label is the identity the (cooperative) "
                              "board guard checks; only the label is read")
    tasks_p.add_argument("--json", action="store_true", help="emit the structured report")

    claim_p = sub.add_parser(
        "board-claim",
        help="every change to a board item's claim (claim.v2 draft 5: one compare-and-swap write of the issue's "
             "claim document, server times, a generation that only goes up, the PR head pinned at register), "
             "show it, or `check` a PR as the merger does",
    )
    claim_p.add_argument("verb", choices=("take", "renew", "register", "re-register", "adopt", "release", "reopen",
                                          "hand-back", "done", "show", "check", "reserve", "decline", "pause", "resume"))
    claim_p.add_argument("--issue", default=None,
                         help="the board issue, e.g. TKT-3526 (every verb but pause and resume, which act on the "
                              "claim-pause document of the queue issue: new takes and reservations stop while paused; "
                              "queue owners only)")
    claim_p.add_argument("--project-id", default=None,
                         help="pause/resume: scope the pause to one board project id in paused_projects without "
                              "stopping other projects")
    claim_p.add_argument("--seat", required=True, help="this seat's label (the claim's holder)")
    claim_p.add_argument("--work-ref", default=None,
                         help="register: the GitHub PR URL the work is in (its head SHA is read from GitHub); "
                              "check: the PR to test (default: the claim's work_ref)")
    claim_p.add_argument("--work-doc", default=None,
                         help="done or hand-back: the key of the result document on the same issue, for work that "
                              "ends as a board document and not a PR (it must be yours and written under this claim)")
    claim_p.add_argument("--for", dest="for_seat", default=None,
                         help="reserve: the one seat the item is held for (a dispatcher only; switched off while "
                              "no dispatcher is named)")
    claim_p.add_argument("--code", default=None, choices=("busy", "not-my-lane", "blocked", "allowance"),
                         help="decline: why this seat hands a reservation back")
    claim_p.add_argument("--presence-base-url", default=None,
                         help="with --state-file: after take publish this seat's presence active, after done, "
                              "release or decline idle (the server at this URL). Off unless given")
    claim_p.add_argument("--note", default=None, help="a short note stored on the claim")
    claim_p.add_argument("--reason", default=None, help="release: why; reopen: what was reviewed")
    claim_p.add_argument("--handover-file", type=Path, default=None,
                         help="adopt: the handover note that names the item (read only to check it names it)")
    claim_p.add_argument("--session-key", default=None,
                         help="the session this claim belongs to (default: the Claude or Codex session id from the "
                              "environment; refused when there is none)")
    claim_p.add_argument("--state-file", type=Path, default=None,
                         help="this seat's state file; only its label is read, for the Codex board guard")
    claim_p.add_argument("--json", action="store_true", help="emit the structured report")

    queue_p = sub.add_parser(
        "board-queue",
        help="the queue view of the claims (claim.v2 draft 5): read every claim document, mark lapsed v2 leases "
             "expired (lazy expiry, one compare-and-swap write each), and write one `queue-view` document; or "
             "`show` the current view",
    )
    queue_p.add_argument("verb", choices=("refresh", "show"))
    queue_p.add_argument("--seat", required=True, help="the seat refreshing the view (named on its first line)")
    queue_p.add_argument("--target-issue", default=board_queue.DEFAULT_TARGET,
                         help="the issue that holds the queue-view document (default %s)" % board_queue.DEFAULT_TARGET)
    queue_p.add_argument("--days", type=int, default=board_queue.DEFAULT_DAYS,
                         help="closed issues are probed when active in this many days (default %d)"
                              % board_queue.DEFAULT_DAYS)
    queue_p.add_argument("--item", action="append", default=None,
                         help="probe this issue too, e.g. TKT-4770; repeatable")
    queue_p.add_argument("--queue-doc", default=board_queue.QUEUE_DOC_KEY,
                         help="the queue document on the target issue whose FREE rows the view lists (default %s; "
                              "read only)" % board_queue.QUEUE_DOC_KEY)
    queue_p.add_argument("--full", action="store_true", help="probe every candidate, not only what changed")
    queue_p.add_argument("--dry-run", action="store_true", help="render the view and write NOTHING: no view and no lazy expiry mark (a lapsed lease is shown as "
                              "expired, unmarked)")
    queue_p.add_argument("--state-file", type=Path, default=None,
                         help="this seat's state file; only its label is read, for the Codex board guard")
    queue_p.add_argument("--json", action="store_true", help="emit the structured report")

    dispatch_p = sub.add_parser(
        "board-dispatch",
        help="the dispatcher's plan as a DRY RUN: which free queue row it would reserve for which seat, from the "
             "queue view and a saved `fleet --json` answer. It reserves, writes and sends nothing",
    )
    dispatch_p.add_argument("verb", choices=("plan",))
    dispatch_p.add_argument("--seat", required=True, help="the seat running the plan (for the Codex board guard)")
    dispatch_p.add_argument("--fleet-json", type=Path, required=True,
                            help="the saved output of `fleet --seats ... --json`: its rows, and its as_of, "
                                 "which must be at most five minutes old")
    dispatch_p.add_argument("--view-file", type=Path, default=None,
                            help="a saved queue view to plan from; default: read queue-view from the board")
    dispatch_p.add_argument("--families-file", type=Path, default=None,
                            help="JSON object, seat label to model family (claude, codex, gemini, grok, kimi); a "
                                 "seat without one gets no row at all (codex-... labels are codex)")
    dispatch_p.add_argument("--target-issue", default=board_queue.DEFAULT_TARGET,
                            help="the issue that holds the queue-view document (default %s)" % board_queue.DEFAULT_TARGET)
    dispatch_p.add_argument("--state-file", type=Path, default=None,
                            help="this seat's state file; only its label is read, for the Codex board guard")
    dispatch_p.add_argument("--json", action="store_true", help="emit the plan as JSON")

    receipt_p = sub.add_parser(
        "review-receipt",
        help="is there a clean AI review of EXACTLY this head: GitHub's Codex review, else a fallback receipt (a PR "
             "comment, the reviewing session's signed message and the report). Read only. It fetches the PR and its "
             "comments from GitHub itself and reads each message through this client's verified read; it is the "
             "AI-review leg of the merge rule and nothing else",
    )
    receipt_p.add_argument("--head", required=True, help="the PR's head, as a full 40-hex sha")
    receipt_p.add_argument("--repo", default=None, help="OWNER/REPO, read with `gh api` (GET only)")
    receipt_p.add_argument("--pr", type=int, default=None, help="the PR number")
    receipt_p.add_argument("--message-key", action="append", default=None,
                           help="for a fallback receipt: the msg/ key of the reviewing session's signed message; "
                                "repeatable. Read through this client's own verified read, so it needs --base-url "
                                "and --state-file")
    receipt_p.add_argument("--base-url", default=None, help="the MeshyChat server, for --message-key")
    receipt_p.add_argument("--state-file", type=Path, default=None, help="this seat's state file, for --message-key")
    receipt_p.add_argument("--report", type=Path, default=None,
                           help="the review report file; a fallback receipt is void without it")
    receipt_p.add_argument("--comments-file", type=Path, default=None,
                           help="NOT A MERGE CHECK: judge saved comments instead of fetching them (for tests and "
                                "for looking at a case). The run says so and never exits 0")
    receipt_p.add_argument("--messages-file", type=Path, default=None,
                           help="with --comments-file only: saved messages, believed as written")
    receipt_p.add_argument("--author-seat", default=None, help="with --comments-file only: the author's seat")
    receipt_p.add_argument("--json", action="store_true", help="emit the verdict as JSON")

    orient_p = sub.add_parser(
        "orient",
        help="5-source pre-idle orientation (postbox spool/banked cc, mesh graph, local worktrees, "
             "handovers/gate logs, and Brain/environment hygiene) with ranked AskUserQuestion options",
    )
    orient_p.add_argument("--base-url", default=None,
                          help="optional server URL for live owed/awaiting/brief queries")
    orient_p.add_argument("--state-file", type=Path, default=None,
                          help="optional state file for live owed/awaiting/brief queries")
    orient_p.add_argument("--identity", action="append", default=None,
                          help="comma-separated or repeated postbox identity names (e.g. antigravity-prod)")
    orient_p.add_argument("--seat", default=None,
                          help="seat label (default: inferred from state file or --identity)")
    orient_p.add_argument("--spool-dir", type=Path, default=None,
                          help="postbox spool directory (default: ~/.meshychat/postbox/spool)")
    orient_p.add_argument("--repo", action="append", type=Path, default=None,
                          help="git repository checkout to inspect (repeatable)")
    orient_p.add_argument("--worktree-root", action="append", type=Path, default=None,
                          help="directory of git worktrees/checkouts to scan (repeatable)")
    orient_p.add_argument("--handover-dir", type=Path, default=None,
                          help="directory holding HANDOVER-*.md files (default: ~/.dxxxxx)")
    orient_p.add_argument("--scratchpad-dir", action="append", type=Path, default=None,
                          help="scratchpad directory to scan for gate-runner.log / verdict.txt (repeatable)")
    orient_p.add_argument("--since", default="14d",
                          help="relative age or ISO cutoff for live mesh graph queries (default: 14d)")
    orient_p.add_argument("--stale-handover-hours", type=float, default=orient.DEFAULT_STALE_HANDOVER_HOURS,
                          help="hours after which a seat's latest HANDOVER file is flagged stale (default: 24)")
    orient_p.add_argument("--offline", action="store_true",
                          help="skip live MeshyChat server calls even when --base-url and --state-file are given")
    orient_p.add_argument("--no-brain-probe", action="store_true",
                          help="check REFUSED_ENVIRONMENT variables only without invoking cf-access-headers.sh --identity")
    orient_p.add_argument("--include-review-preflight", action="store_true",
                          help="surface matching Antibody/Constraint/Ruling/OpenItem cards and FIX FIRST notes for actionable worktree diffs")
    orient_p.add_argument("--board-tasks", action="store_true",
                          help="add this seat's open Planboard board items (the `tasks` read, read-only) as a source; "
                               "needs --seat")
    orient_p.add_argument("--json", action="store_true",
                          help="emit structured JSON instead of human-readable summary")

    hints_p = sub.add_parser(
        "hints",
        parents=[common],
        help="query advisory pre-send graph collision hints (POST /v1/mesh/hints); fails open",
    )
    hints_p.add_argument("--to", action="append", default=None,
                         help="recipient label(s), comma-separated or repeated")
    hints_p.add_argument("--cc", action="append", default=None)
    hints_p.add_argument("--thread", default=None, help="candidate thread_id")
    hints_p.add_argument("--in-reply-to", default=None, help="msg/ key this draft answers (suppresses new-thread notice)")
    hints_p.add_argument("--closes", action="append", default=None, metavar="MSG_KEY")
    hints_p.add_argument("--from", dest="sender", default=None)
    hints_p.add_argument("--type", default="note", dest="msg_type")
    hints_p.add_argument("--ref", default=None)
    hints_body = hints_p.add_mutually_exclusive_group(required=True)
    hints_body.add_argument("--body")
    hints_body.add_argument("--body-file", type=Path)
    hints_p.add_argument("--k", type=_whole_within(1, hints.MAX_K), default=hints.DEFAULT_K,
                         help=f"maximum hints to return (1..{hints.MAX_K}, default {hints.DEFAULT_K})")
    hints_p.add_argument("--timeout", type=float, default=hints.DEFAULT_TIMEOUT_S,
                         help=f"wall-clock budget in seconds (default {hints.DEFAULT_TIMEOUT_S})")
    hints_p.add_argument("--text", action="store_true",
                         help="print the <= 3-line calibrated notice instead of JSON")
    return ap


#: handoff-preflight's registry thread (tools/meshy_node/handoff.REGISTRY_THREAD), restated for the help text and
#: pinned equal by tests/test_meshy_node_handoff_preflight.py.
HANDOFF_REGISTRY_THREAD = "cap-registry"
#: GET /v1/cards pages read for the registry at most; the cards themselves are bounded by handoff.REGISTRY_MAX_CARDS.
HANDOFF_REGISTRY_PAGES = 5


def _handoff_registry_keys(args, state, thread: str) -> list[str]:
    """The keys of oxxxxxx's cards on the registry thread, every status, paged by `next`. More than
    REGISTRY_MAX_CARDS, or a page bound reached with more to read, refuses rather than judge on a prefix."""
    from tools.meshy_node import handoff
    keys, after = [], None
    for _ in range(HANDOFF_REGISTRY_PAGES):
        body = node.cards(args.state_file, state, cards.list_query(
            thread=thread, seat=handoff.REGISTRY_SEAT, status="any", limit=handoff.REGISTRY_MAX_CARDS, after=after))
        rows = body.get("cards") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise handoff.Unjudgeable("GET /v1/cards answered without a list of cards")
        for row in rows:
            key = row.get("key") if isinstance(row, dict) else None
            if not isinstance(key, str) or not cards.KEY_RE.fullmatch(key):
                raise handoff.Unjudgeable("GET /v1/cards answered a card without a card key")
            keys.append(key)
        if len(keys) > handoff.REGISTRY_MAX_CARDS:
            raise handoff.Unjudgeable(f"the registry thread holds more than {handoff.REGISTRY_MAX_CARDS} cards")
        # Only an exact null ends the registry; a cursor is a non-empty string. Absent, false, 0, [] or {} would
        # otherwise end it early and hide a newer revision on the next page (pxx P2 F3).
        if "next" not in body:
            raise handoff.Unjudgeable("GET /v1/cards answered without its `next` cursor")
        after = body["next"]
        if after is None:
            return list(dict.fromkeys(keys))
        if not isinstance(after, str) or not after:
            raise handoff.Unjudgeable("GET /v1/cards answered a malformed `next` cursor")
    raise handoff.Unjudgeable(f"the registry did not end within {HANDOFF_REGISTRY_PAGES} pages")


def _handoff_now() -> datetime:
    """The clock handoff-preflight reads, once before the registry and once after it (pxx P2 F1)."""
    return datetime.now(timezone.utc)


def _handoff_preflight(args, state) -> int:
    """`handoff-preflight` (tools/meshy_node/handoff.py): the facts file checked, oxxxxxx's registry cards listed and
    each read signed, then the answer. Exit 0 recordable, 1 not recordable, 2 when it cannot judge."""
    from tools.meshy_node import handoff
    try:
        raw = json.loads(Path(args.facts).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise node.Refused(f"--facts could not be read as JSON ({type(exc).__name__})") from None
    signer = next(peer for peer, label in node.SEAT_DIRECTIVE_PEERS.items() if label == handoff.REGISTRY_SEAT)
    try:
        f = handoff.facts(raw, _handoff_now())
        try:
            keys = _handoff_registry_keys(args, state, args.registry_thread)
        except (node.ServerSaid, node.Refused, OSError) as exc:
            # A listing that failed cannot say a cap is current: can't judge, exit 2, never "not recordable" (pxx P2 F4).
            status = getattr(exc, "status", None)
            raise handoff.Unjudgeable("the registry could not be listed (%s%s)" % (
                type(exc).__name__, ", HTTP %s" % status if status else "")) from None
        reg = handoff.registry(keys, lambda key: node.read(args.state_file, state, key=key,
                                                            namespace=cards.namespace()), signer,
                               thread=args.registry_thread)
        # The facts' age by the clock AFTER the registry reads: up to 50 reads must not let stale facts pass.
        result = handoff.judge(f, reg, _handoff_now(), thread=args.registry_thread)
    except handoff.Unjudgeable as exc:
        _emit({"error": "handoff_unjudgeable", "detail": str(exc), "note": handoff.NOTE})
        return 2
    if args.json:
        _emit(result)
    else:
        print(handoff.render(result))
    return 0 if result["recordable"] else 1


def _round(args, state) -> int:
    """`round`: tools/meshy_node/rounds.py over the whole thread, the roster's peers, and one signed read per post.
    Exit 0 with the answer; 1 when the thread came back truncated (a prefix is never read as the thread)."""
    from tools.meshy_node import conformance, rounds
    seats = list(dict.fromkeys(s.strip() for s in args.seats.split(",") if s.strip()))   # a repeat counts once
    if not seats:
        raise node.Refused("--seats names no seat")
    max_reads = rounds.DEFAULT_MAX_READS if args.max_reads is None else args.max_reads
    if max_reads < 1:
        raise node.Refused("--max-reads must be at least 1")
    try:
        got = conformance.whole_thread(lambda **kw: node.thread(args.state_file, state, **kw), args.thread)
    except conformance.ThreadTruncated as exc:
        _emit({"error": "thread_truncated", "detail": str(exc)})
        return 1
    hits = [h for h in got.get("hits") or [] if isinstance(h, dict)]
    recipients = lambda labels: node.recipients(args.state_file, state, labels)  # noqa: E731
    authors = sorted({str(h.get("author")) for h in hits if h.get("author")})
    seat_peers = rounds.roster_peers(seats, recipients)
    author_peers = rounds.roster_peers(authors, recipients)
    prefix = None if args.prefix.strip().upper() in ("", "ANY") else args.prefix.strip()
    result = rounds.collate(hits, seats, seat_peers, author_peers,
                            lambda key, version=None: node.read(args.state_file, state, key=key, version=version,
                                                                record=False),
                            item=args.item, prefix=prefix, max_reads=max_reads)
    if args.json:
        _emit({"thread": args.thread, **result})
    else:
        print(rounds.render(result, args.thread))
    return 0


def _self_addressed(state: dict, sender: str | None, to: list[str] | None,
                    cc: list[str] | None) -> list[str]:
    """The recipients that are the sender's own label, compared without case as the push
    resolves them (apps/wsgw/mesh_notify.py peer_labels). Such a message is stored and can be
    read by key, but the server never nudges a message's author (divergence 020: the push, the
    addressed wait and the waiter registry all skip it), so it reaches no spool of the sender's.
    Only the label this node sends as is known here; mail to another label of the same peer is
    not delivered either and is not caught."""
    me = (sender or state.get("label") or "").casefold()
    return [label for label in (to or []) + (cc or []) if me and label.casefold() == me]


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=False) + "\n")
    sys.stdout.flush()


def _record_op_sendguard(state_file: Path, op_id: str) -> tuple[bool, str | None]:
    """Count a proven operation from its original journalled value, exactly once, at the time it was sent."""
    def record(value, key, sent_at=None):
        now = time.time()
        if sent_at is not None:
            # A success proven late (a crash between the journal's "succeeded" and this write) is charged when it was
            # sent; one already outside the window has nothing left to charge (GitHub Codex r4366980594, pxx).
            now = min(sent_at, now)
            if time.time() - now >= sendguard.WINDOW_S:
                return None
        return sendguard.record(state_file, to=value.get("to"), cc=value.get("cc"), key=key, now=now,
                                body_bytes=len(value.get("body", "").encode("utf-8")), body=value.get("body", ""),
                                idempotent=True)
    return node.op_record_sendguard(state_file, op_id, record)


def _record_drain_sendguard(state_file: Path, op_id: str) -> None:
    """Account for a settled drain item before the next item can ask the guard."""
    try:
        counted, missed = _record_op_sendguard(state_file, op_id)
    except Exception as exc:                               # noqa: BLE001 - drain swallows it: say so (pxx)
        _stderr_line({"warning": "send_not_recorded", "op_id": op_id, "reason": f"{type(exc).__name__}: {exc}"})
        return
    if missed:
        _stderr_line({"warning": "send_not_recorded", "op_id": op_id, "reason": missed})


def _admit_op_sendguard(state_file: Path, value: dict, bulk_ack: str | None = None) -> None:
    """Check a new operation presentation against the signed value in its journal."""
    sendguard.admit(state_file, to=value.get("to"), cc=value.get("cc"), body=value.get("body", ""),
                    subject=value.get("subject"), bulk_ack=bulk_ack, refused=node.Refused)


def _raise(exc: Exception):
    """An op_send admit hook that refuses with a guard refusal already made before the upload."""
    def admit(value: dict, bulk_ack: str | None = None) -> None:
        raise exc
    return admit


def _ref_fingerprints(refs: list[dict]) -> list[dict]:
    """The same name-and-sha256 shape as _attachment_fingerprints, for refs already uploaded."""
    return [{"name": ref.get("name"), "sha256": ref.get("sha256")} for ref in refs]


def _attachment_fingerprints(paths: list[Path] | None) -> list[dict]:
    """The attachment input to an op fingerprint, computed locally without an upload."""
    out = []
    for path in paths or []:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        # The name is what `_attach` signs and recipients fetch it as, so it is bound too (GitHub Codex r4364598288).
        out.append({"name": path.name, "sha256": digest.hexdigest()})
    return out


def _attach_local_refusals(paths) -> None:
    """Every local refusal of `send --attach`, before any file is opened: too many files, a path that is not a regular
    file (a FIFO or a device would block or never end when read), one over the size a reference can carry (by stat, so
    nothing is read), a name the schema refuses, one name twice. The op path runs it before it hashes the files for
    preflight or takes the op lock, so a bad path is refused at once there too (GitHub Codex r4366931417)."""
    paths = list(paths)
    if len(paths) > wire.MAX_ATTACHMENTS:
        raise node.Refused(f"--attach names {len(paths)} files; a message cites at most {wire.MAX_ATTACHMENTS}")
    for path in paths:
        problem = files.local_problem(path)
        if problem:
            raise node.Refused(problem)
        try:
            size = Path(path).stat().st_size
        except OSError:
            continue                                       # upload reports an unreadable file in its own words
        if size > wire.MAX_ATTACHMENT_SIZE_BYTES:
            raise node.Refused(f"--attach {path}: {size} bytes is over what a reference can carry")
    names = [Path(path).name for path in paths]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise node.Refused(f"--attach names {', '.join(twice)} more than once; `fetch` writes each attachment under "
                           "its own name, so rename one")


def _directive(args, resolve=None):
    """The directive object from --directive-file, --directive-to and --directive-hours, or None (the directive rule)."""
    path = getattr(args, "directive_file", None)
    target = getattr(args, "directive_to", None)
    if path is None and target is None:
        return None
    if path is None or not target:
        raise node.Refused("--directive-file and --directive-to go together")
    # An explicit 0 is refused below, never read as the default (codex-oxxxxxx-admin-cli ef2965).
    raw = getattr(args, "directive_hours", None)
    hours = None if raw is None else float(raw)
    if hours is not None and not 0 < hours <= 24:
        raise node.Refused("--directive-hours is more than 0 and at most 24")
    words = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not words:
        raise node.Refused(f"--directive-file {path} has no non-empty line")
    from datetime import datetime as _dt_, timedelta as _td_, timezone as _tz_
    now = _dt_.now(_tz_.utc).replace(microsecond=0)
    window_end = _window_end(args)
    if window_end is None:
        expires = now + _td_(hours=4.0 if hours is None else hours)
    else:
        # kxxx: a directive does not outlive its window by more than the margin; with no hours given it lasts
        # exactly that long. The server refuses the same bound (validation.validate_window_end).
        last = _dt_.fromisoformat(window_end[:-1] + "+00:00") + _td_(seconds=node.WINDOW_MARGIN_S)
        # Never past the 24 h directive limit either: a window 24 h ahead plus the margin would be refused by the
        # server's own lifetime bound (GitHub Codex on #586, cli.py:1835).
        last = min(last, now + _td_(hours=24))
        expires = last if hours is None else now + _td_(hours=hours)
        # These two are a convenience before signing, for a NEW send only. An --op-id send skips them, so an identical
        # late retry still reaches the journal and gets its stored result; the server decides a new one (GitHub Codex
        # on #586, cli.py:1840).
        if not getattr(args, "op_id", None):
            if expires > last:
                raise node.Refused("--directive-hours puts the expiry after --window-end + %d min; drop "
                                   "--directive-hours or shorten it" % (node.WINDOW_MARGIN_S // 60))
            if expires <= now:
                raise node.Refused("--window-end + %d min has already passed" % (node.WINDOW_MARGIN_S // 60))
    if resolve is None:
        raise node.Refused("--directive-to needs the target's peer id, and this command has no server to ask")
    return {"v": 1, "words": words, "target": target, "target_peer": resolve(target),
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _urgent_kwargs(args) -> dict:
    """send's urgent-lane arguments (kxxx): --urgent and --urgent-declined never together."""
    urgent, declined = bool(getattr(args, "urgent", False)), bool(getattr(args, "urgent_declined", False))
    if urgent and declined:
        raise node.Refused("--urgent and --urgent-declined never go together")
    return {"urgent": urgent, "urgent_declined": "rate_limited" if declined else None}


def _urgent_was_rate_limited(args, result) -> bool:
    """True when this --urgent send (no --op-id, no --no-urgent-fallback) came back as the server's signed
    E_RATE_LIMITED refusal: the one case the fallback resends."""
    return (bool(getattr(args, "urgent", False)) and not getattr(args, "op_id", None)
            and not getattr(args, "no_urgent_fallback", False) and isinstance(result, dict)
            and result.get("status") == "failed" and result.get("result_signature_verified") is True
            and result.get("error_code") == "E_RATE_LIMITED")
def _window_end(args):
    """--window-end, checked, or None (kxxx)."""
    raw = getattr(args, "window_end", None)
    if raw is None:
        return None
    problem = wire.window_end_problem(raw)
    if problem:
        raise node.Refused("--window-end: " + problem)
    return raw


def _directive_hours(args):
    """The hours the operation journals: the given value, or the old default 4.0 when no window is named, so an
    --op-id retry across this change still matches; None when the window sets the expiry."""
    if getattr(args, "directive_file", None) is None:
        return None
    raw = getattr(args, "directive_hours", None)
    if raw is not None:
        return raw
    return None if getattr(args, "window_end", None) is not None else 4.0


def _operator_words(args):
    """The non-empty lines of --operator-words-file, or None when it was not given."""
    path = getattr(args, "operator_words_file", None)
    if path is None:
        return None
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _attach(args, state: dict) -> list[dict]:
    """`send --attach`: every file uploaded and verified, in order, BEFORE anything is sent (divergence 079); the
    references `node.send` puts in the message. Every local refusal comes first (too many files, a path that is not a
    regular file, a name the schema refuses, one name twice: `fetch` writes each file under its own name, so the
    second could never be fetched beside the first), so a bad last path costs no upload. When an upload fails, in
    ANY way (a typed failure, a refusal, or an exception no step types), what this command uploaded before it is
    deleted again, best effort, and nothing cites them, because the message is not sent. The original failure is the
    one raised, and it says which went and which the server would not delete: an AttachFailed in its record, a
    node.Refused in its message when one stayed, anything else in a stderr line before it propagates.
    Raises files.AttachFailed or node.Refused, or what the upload raised."""
    paths = list(args.attach)
    _attach_local_refusals(paths)
    done: list[dict] = []
    for path in paths:
        try:
            done.append(files.upload(args.state_file, state, path))
        except BaseException as exc:                   # every way out but success: nothing will cite `done`
            if done:
                gone = [ref["file_id"] for ref in done if files.discard(args.state_file, state, ref["file_id"])]
                left = [ref["file_id"] for ref in done if ref["file_id"] not in gone]
                if isinstance(exc, files.AttachFailed):
                    exc.error.update({"earlier_uploads_discarded": gone, "earlier_uploads_left": left})
                elif isinstance(exc, node.Refused):
                    if left:
                        raise node.Refused(f"{exc}; uploaded before it and NOT deleted: {', '.join(left)}") from exc
                else:
                    _stderr_line({"warning": "attach_failed", "failure": f"{type(exc).__name__}: {exc}",
                                  "sent": False, "earlier_uploads_discarded": gone, "earlier_uploads_left": left})
            raise
    return done


def _card_write(args, state: dict) -> int:
    """`card write`: every card of the file through cards.write, each checked for shape BEFORE anything is sent,
    so a bad last line costs no write. One card prints its report; more print the counts, the keys written and
    every report that is neither "written" nor "exists"."""
    batch = cards.load_cards(args.file)
    if not batch:
        raise node.Refused(f"{args.file} holds no card")
    for n, card in enumerate(batch, 1):
        why = cards.shape_problem(card)
        if why:
            raise node.Refused(f"card {n} of {args.file}: {why}; nothing was written")
    if args.dry_run:
        reports = []
        for card in batch:
            fired = [] if cards.LOCAL_ONLY_FIELD in card else cards.screen(card)
            outcome = cards.SKIPPED if cards.LOCAL_ONLY_FIELD in card else cards.SCREENED if fired else "would_send"
            reports.append({"key": cards.card_key(card), "dry_run": True, "detectors": fired, "outcome": outcome})
    else:
        ns = cards.named_namespace(args.namespace)
        started, waits = time.monotonic(), {"n": 0, "s": 0.0}

        def on_wait(secs: float) -> None:                  # the first wait is said; the rest are counted
            if not waits["n"]:
                _stderr_line({"card_write": "paced", "retry_after_s": secs,
                              "note": "the server allows this peer about one call a second; the write waits as "
                                      "long as it is told to and goes on from the same card, nothing is lost"})
            waits["n"] += 1
            waits["s"] += secs

        seat, rows, reports = None, None, []
        # A file of cards asks the server ONCE which seat this peer is (cards.confirmed_seat, one inbox). The keys
        # it confirmed within a day under that seat (cards.load_confirmed) are answered with no call, and the
        # seat's listing is read only when two or more of its cards are left that the keys do not hold. One card,
        # and an update, still read their head and print what it says.
        bulk = len(batch) > 1 and not args.update
        identity, confirmed = None, {}
        asked_at = cards._wall()
        # Every call below waits out the rate limit BY ITSELF. Not a card's read and write together: retried as
        # one, the read would take each token the bucket refills and the write never get one (GitHub's review).
        with cards.pacing(on_wait):
            todo = batch
            try:
                if bulk:
                    # Only for the seat the server has just confirmed is this peer's: the listing is by LABEL and
                    # says nothing of who wrote a card, so under any other label it would pass another seat's
                    # cards as seen. The keys are filed under that seat too, so the state file's word, which a
                    # changed roster leaves stale, never decides which of them count (pxx's F3 on #553).
                    seat = cards.confirmed_seat(args.state_file, state)
                    if seat is not None:
                        identity = cards.confirmed_identity(args.base_url, state, ns, seat)
                        confirmed = cards.load_confirmed(args.state_file, identity)
                    unconfirmed = sum(1 for card in batch if card["seat"] == seat
                                      and cards.LOCAL_ONLY_FIELD not in card and cards.card_key(card) not in confirmed)
                    if unconfirmed > 1:                    # one listing instead of one signed read a card
                        rows = cards.own_rows(args.state_file, state, seat, ns)
            except node.ServerSaid as exc:
                if exc.status != 429:
                    raise
                # The limit did not lift while the seat or its listing was read. The run ends with its summary
                # like any other, and says that no card was tried (Codex on this change).
                todo = []
                reports.append({"key": None, "outcome": cards.LIMITED, "written": False, "not_tried": len(batch),
                                "note": f"the server answered rate limited for {cards.RATE_PATIENCE_S:.0f} s "
                                        "before the first card (while the seat, or its listing, was read); no "
                                        "card was tried. Run the same command again"})
            for n, card in enumerate(todo, 1):
                listed = rows if card["seat"] == seat else None
                known = confirmed if confirmed and card["seat"] == seat else None
                try:
                    reports.append(cards.write(args.state_file, state, card, ns=ns, update=args.update,
                                               listed=listed, confirmed=known))
                    if reports[-1].get("settling_read") not in (None, node.E_READ_REFUSED):
                        # The write went, and the read after it timed out or could not be trusted: the server is
                        # not giving reads to settle by. The file stops here, as it does for a server that is
                        # down, and does not go on changing cards it could not then account for.
                        reports[-1]["not_tried"] = len(batch) - n
                        break
                except node.ReadFailed as exc:             # one card's read failing does not lose the others
                    reports.append({"key": cards.card_key(card), "outcome": cards.UNREAD, "written": False,
                                    "read_error": exc.error})
                except (node.ServerSaid, OSError) as exc:
                    limited = isinstance(exc, node.ServerSaid) and exc.status == 429
                    if len(batch) == 1 and not limited:
                        raise                              # one card: the server's answer is the whole report
                    # The file ends here WITH its summary. Either waiting did not help for a quarter of an hour,
                    # or the server answered an error or not at all (a restart in the middle of a load). Printed
                    # as the bare HTTP answer, the cards already stored were named by nothing (memory-bxxxxx's
                    # review of #183, and uxxx and vxxxxxxxxxx of the plain client). Whether THIS card was
                    # written is not known here: the failure may be the write's own or the wait for its answer.
                    again = ("With --update, read its version first (`read --namespace`): a second run writes "
                             "another version of a card that is there" if args.update else
                             "Run the same command again: a card that is there is skipped")
                    said = (f"rate limited for {cards.RATE_PATIENCE_S:.0f} s" if limited else
                            f"HTTP {exc.status}" if isinstance(exc, node.ServerSaid) else f"nothing ({exc})")
                    reports.append({"key": cards.card_key(card), "written": None, "not_tried": len(batch) - n,
                                    "outcome": cards.LIMITED if limited else cards.UNANSWERED,
                                    **({} if limited or not isinstance(exc, node.ServerSaid)
                                       else {"http_status": exc.status}),
                                    "note": f"the server answered {said} on this card. Whether it was written is "
                                            f"not known; the cards after it were not tried. {again}"})
                    break
                if n % 100 == 0 and n < len(batch):
                    _stderr_line({"card_write": "progress", "done": n, "remaining": len(batch) - n,
                                  "waited_s": round(waits["s"])})
        if identity is not None:
            # Only what the SERVER said in this run: a write it took, or a key it showed (listed, or read). Never
            # a key the file already held, so an entry ages out a day after the server last said so, whatever
            # runs between; never another label's key; never a refusal, a foreign card or one in doubt.
            fresh = {r["key"]: {"at": asked_at, **({"redacted": True} if r.get("redacted") is True else {})}
                     for r in reports
                     if r["outcome"] in (cards.WRITTEN, cards.EXISTS) and not r.get("cached")
                     and isinstance(r.get("key"), str) and r["key"].startswith(f"card/{seat}/")}
            cards.save_confirmed(args.state_file, identity, fresh, now=asked_at)
    if len(batch) == 1:
        _emit(reports[0])
    else:
        counts: dict = {}
        for r in reports:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        summary = {"cards": len(batch), "outcomes": counts,
                   "written": [r["key"] for r in reports if r["outcome"] == cards.WRITTEN],
                   # Named, and not a problem: a redacted key is closed for good, so a push that finds one is done
                   # with it.
                   "redacted": [r["key"] for r in reports if r.get("redacted")],
                   "problems": [r for r in reports
                                if r["outcome"] not in (cards.WRITTEN, cards.EXISTS, cards.SKIPPED, "would_send")]}
        if not args.dry_run:
            summary.update({"seconds": round(time.monotonic() - started, 1), "waited_s": round(waits["s"]),
                            "waits": waits["n"],
                            # What the run asked of the server: how many cards its confirmed keys answered,
                            # and whether the seat's listing was read.
                            "cached": sum(1 for r in reports if r.get("cached")), "listed": rows is not None})
        _emit(summary)
    return cards.exit_code(reports)


def _card(args, state: dict) -> int:
    """`card write | list | search | usage | commit | done | hook` (tools/meshy_node/cards.py; divergence 091, the client
    half). `card recent` is `_card_recent`, before any state is read."""
    if args.card_cmd == "write":
        return _card_write(args, state)
    if args.card_cmd == "search":
        _emit(cards.search(args.state_file, state, args.q, ns=args.namespace, k=args.k, rerank=args.rerank,
                           types=args.types or (), thread=args.thread, seat=args.seat, owner=args.owner,
                           status=args.status, since=args.since, until=args.until))
        return 0
    if args.card_cmd == "hook":
        return _card_hook(args, state)
    if args.card_cmd == "usage":
        _emit(node.card_usage(args.state_file, state, args.days))
        return 0
    if args.card_cmd == "list":
        _emit(node.cards(args.state_file, state, cards.list_query(
            types=args.types or (), thread=args.thread, seat=args.seat, owner=args.owner, status=args.status,
            since=args.since, until=args.until, recorded_since=args.recorded_since, limit=args.limit,
            after=args.after)))
        return 0
    if args.card_cmd == "context":
        data = cards.fleet_context(args.state_file, state, seat=args.seat, overlap=args.overlap)
        if args.as_json:
            _emit(data)
        else:
            _write_text("\n".join(cards.format_context_board(data)) + "\n")
        return 0
    node.require_peer(args.state_file, state)              # a card is written as this seat: it has to be one
    # Every refusal this client makes on its own comes first: words it cannot make a card of cost no call.
    if args.card_cmd == "commit":
        cards.check_commit(args.what, args.done_when)
    else:
        cards.check_done(args.key, args.result)
    cards.named_namespace(args.namespace)                  # ... and so does a namespace that is named and empty
    with cards.pacing():                                   # a commitment made while a file is loading is still made
        return _card_as_seat(args, state)


def _quiet_stdout() -> None:
    """After stdout failed: the text that did not go out stays in the stream's buffer, the interpreter flushes it
    again at exit, that fails too, and exit 0 becomes 120 with a flush error on stderr (`_stderr_line` met the
    same on stderr). So the stream's own descriptor is pointed at the null device, and that flush goes through
    into nothing; a stream with no descriptor is replaced instead."""
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, sys.stdout.fileno())
        finally:
            os.close(null)
    except (OSError, ValueError, AttributeError):
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass


def _write_text(text: str) -> None:
    """`text` through the stream itself: a terminal, or a stdout with no descriptor (a test's capture). A stream
    whose encoding cannot hold a card's characters gets them as '?' (the text encodes whole before anything is
    written); one that is closed or broken drops it (`_quiet_stdout`)."""
    try:
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            sys.stdout.write(text.encode(encoding, "replace").decode(encoding))
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        _quiet_stdout()


def _print_text(lines: list[str], by: float) -> None:
    """Plain lines on stdout, never a failure, and done by `by` (a time.monotonic() instant; at least
    RECENT_OUTPUT_MIN_S from now). A pipe that is open and not read blocks a write without raising, and before
    this a reader that never read held the session start for as long as it liked (Codex on this change). So the
    text is encoded whole, as UTF-8, and written to stdout's descriptor with os.write on a thread of its own,
    which is waited for until `by` and no longer. UTF-8 whatever the stream's encoding: the reader is a hook, and
    Claude Code reads a hook's output as UTF-8, while on Windows a pipe's stream encodes in the ANSI code page,
    which wrote 'é' as one byte that is no UTF-8 and every emoji as '?' (kxxx's gate fb045947, HQD). Still writing
    then, it is left behind: a daemon, which the interpreter's exit does not wait for, and stdout's own buffer
    holds nothing (nothing went through it), so nothing at exit can block or fail on it, and the process ends,
    exit 0, with the thread in it. A write that fails (the reader is gone) is dropped, and for the same reason
    nothing at exit tries it again. A terminal, or a stdout with no descriptor, is written through the stream as
    before (`_write_text`): a person at a terminal is not a hook, and on Windows only the stream writes a
    console's characters right."""
    text = "\n".join(lines) + "\n"
    try:
        fd = sys.stdout.fileno()
        through_stream = os.isatty(fd)
    except (OSError, ValueError, AttributeError):
        fd, through_stream = None, True
    if through_stream:
        _write_text(text)
        return
    data = memoryview(text.encode("utf-8", "replace"))

    def write():
        try:
            left = data
            while left:
                left = left[os.write(fd, left):]
        except OSError:
            pass                                           # the reader is gone: nobody is left to tell

    writer = threading.Thread(target=write, name="card-recent-output", daemon=True)
    writer.start()
    writer.join(max(cards.RECENT_OUTPUT_MIN_S, by - time.monotonic()))


def _card_recent(args) -> int:
    """`card recent` (cards.recent): the briefing a session-start hook prints into the session's context. Plain
    text, not JSON, and exit 0 WHATEVER happens, the state file, the base URL and stdout included: any failure is
    one "decision cards: not available (...)" line, so a hook running it can never stop or hold a session. The
    deadline is counted from here; the state file is read inside it, and the text is handed to stdout by it."""
    seconds = cards.RECENT_DEADLINE_S if args.deadline is None else args.deadline
    deadline = time.monotonic() + seconds
    try:
        lines = cards.recent(args.state_file, args.base_url, seat=args.seat, n=args.n, others=args.others,
                             days=args.days, load_result=args.load_result, deadline=deadline,
                             freshness_cache=args.freshness_cache, deadline_s=seconds)
    except Exception as exc:                               # noqa: BLE001 - a session start is never stopped
        lines = [cards.recent_unavailable(exc)]
    _print_text(lines, deadline)
    return 0


def _card_hook(args, state: dict) -> int:
    """`card hook`: the dialog's call on stdin, the reports on stdout, exit as `card write`. The Question cards are
    written BEFORE the earlier cards are looked for, so the card of this question exists even when the search is
    slow or fails."""
    deadline = cards._mono() + cards.HOOK_ASK_BUDGET_S       # the mod gives the whole run 20 s, start included
    ns = cards.named_namespace(args.namespace)             # stripped, and refused when named and empty
    try:
        event = json.loads(sys.stdin.read())
    except ValueError as exc:
        raise node.Refused(f"card hook reads one JSON object on stdin: {exc}") from exc
    node.require_peer(args.state_file, state)
    # Paced as `commit` and `done` are: a dialog of four questions opened while a file is loading would end on
    # the first 429 with no report and no search (memory-bxxxxx on #189, measured).
    with cards.pacing():
        reports = cards.hook_write(args.state_file, state, event, ns=ns)
        out = {"phase": event.get("phase"), "cards": reports}
        if event.get("phase") == "ask":
            seat = cards.learn_seat(args.state_file, state)
            # The search sends a question's words to the server, so only a question whose card the screen PASSED is
            # searched: an allowlist of the outcomes cards.write reaches only after screen() let the card through
            # (written, exists, foreign, refused, unanswered). Everything else is not searched: one the
            # detectors refused (screened, or not_built "redaction:..."), one refused before they ran (not_built: no
            # call id, a time in another spelling), SKIPPED (returned before the screen runs), and any outcome added
            # later (GitHub's Codex review of #190, P1; kxxx's review of #201: fail closed).
            after_screen = (cards.WRITTEN, cards.EXISTS, cards.FOREIGN, cards.REFUSED, cards.UNANSWERED)
            passed = {r["index"] for r in reports if r.get("outcome") in after_screen}
            out["earlier"] = cards.earlier_cards(args.state_file, state,
                                                 [q for q in cards.hook_questions(event) if q["index"] in passed],
                                                 seat, ns=ns, deadline=deadline)
    _emit(out)
    return cards.exit_code([r for r in reports if r.get("outcome") != "not_built"])


def _card_as_seat(args, state: dict) -> int:
    """`card commit | done`, once their words are checked: every call from here is made as the seat."""
    # The ONE label the server stamps for this peer, ASKED AFRESH: what follows uses it before any write could be
    # refused for it. From a state file whose roster entry changed, `commit` told the other seats' cards with
    # the old seat and `done` refused a key under the peer's own, new one (GitHub's review of #180).
    seat = cards.confirmed_seat(args.state_file, state)
    if seat is None:
        raise node.Refused("the server did not say which label it stamps for this peer; a card is written as a "
                           "seat, so nothing was written. `inbox` shows the label and where it is from")
    ns = cards.named_namespace(args.namespace)
    if args.card_cmd == "commit":
        at = cards.now_time()
        cards.commitment(seat, args.thread, args.what, args.done_when, at=at)     # refused here, before any read
        # Who else is on this thread, BEFORE this seat's own card is written, and shown before or not: this is
        # the moment a duplicate starts. Advisory like the line at a send: a read that fails names nobody.
        lines, named = [], {}
        try:
            answer = cards.Beside(lambda: node.cards(args.state_file, state, cards.thread_query(args.thread),
                                                     timeout=cards.TIMEOUT_S, purpose="write")).answer()
            lines, named = cards.commitment_lines(answer, own=seat, thread=args.thread)
        except Exception:                                  # noqa: BLE001 - advisory
            lines, named = [], {}
        for line in lines:
            _stderr_line(line)
        cards.remember(args.state_file, named)
        report, card = cards.write_as_seat(     # asks the server again if its word on the seat turns out stale
            args.state_file, state, lambda seat: cards.commitment(seat, args.thread, args.what, args.done_when, at=at),
            ns=ns)
        if report.get("outcome") == cards.EXISTS:          # the same words as before: is that Commitment still open
            said = cards.commitment_open(args.state_file, state, ns, card)
            if said["open"] is False:
                why = "is closed" if said.get("listed", True) else "is no longer a listed card (redacted)"
                report = {**report, "outcome": cards.CLOSED, **{k: v for k, v in said.items() if k != "open"},
                          "note": f"these words were committed in this thread before and that commitment {why}, "
                                  "so nothing is open and no seat will be shown it. Say what is different this "
                                  "time (a scope, a date) and commit that"}
            elif said["open"] is None:
                report = {**report, "open": "unknown",
                          "note": "the commitment exists; the server could not be asked whether it is still open. "
                                  "`card list --status any --type Commitment --thread <thread>` says"}
        _emit({**report, "seat": card["seat"], "thread": args.thread, "others_named": len(lines)})
        return cards.exit_code([report])
    parts = cards.key_parts(args.key)                      # card done; its shape was checked above
    if parts[0] != seat:
        raise node.Refused(f"{args.key} is not a card of this seat ({seat}): a seat closes only its own commitment")
    held = cards.head(args.state_file, state, ns, args.key)
    if held is None:
        raise node.Refused(f"{args.key} has no row in {ns}; nothing was written")
    report = cards.write(args.state_file, state, cards.decision(parts[0], args.key, held["value"], args.result),
                         ns=ns)
    _emit({**report, "closes": args.key})
    return cards.exit_code([report])


def _lookup_help(args: argparse.Namespace) -> int:
    """Issue #269: print postbox_os.lookup_text for this checkout, this python, and the --base-url and
    --state-file given; the same text the Claude channel puts in a session's instructions. postbox_os is loaded
    from this checkout's tools/postbox by path, as the postbox itself loads it (that folder is not a package)."""
    import importlib.util
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("meshy_postbox_os", repo / "tools" / "postbox" / "postbox_os.py")
    pos = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pos)
    ident = {"name": "this seat", "client": repo.as_posix(), "python": Path(sys.executable).as_posix(),
             "base_url": node.normalize_base_url(args.base_url), "state": Path(args.state_file).as_posix()}
    text = pos.lookup_text([ident])
    if not text:
        print("lookup-help: no text: this checkout registers none of the lookup routes, or a value holds a "
              "control character", file=sys.stderr)
        return 1
    print(text)
    return 0


def _verify_bundle(args: argparse.Namespace) -> int:
    """`verify-bundle`: verify outer archive custody, streaming member safety, inner manifest/SHA256SUMS,
    and optional test-input provenance without extracting to disk."""
    report = custody.verify_bundle(
        args.path,
        expected_sha256=args.expected_sha256,
        provenance_map=args.provenance_map,
    )
    _emit(report)
    return 0 if report["ok"] else 1


def _gate_check(args: argparse.Namespace) -> int:
    """`gate-check`: evaluate SHA-pinned review quorum from a local JSON file or a live mesh thread."""
    if args.messages_file is None and not args.thread:
        raise node.Refused("gate-check requires either --messages-file <path> or --thread <thread_id>")

    ruling_cards: list[dict] = []
    read_failures: list[dict] = []
    projection_incomplete: dict | None = None
    coverage: dict | None = None
    if args.messages_file is not None:
        try:
            raw = json.loads(args.messages_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise node.Refused(f"--messages-file {args.messages_file} is unreadable: {exc}") from exc
        if isinstance(raw, dict):
            messages = list(raw.get("messages") or raw.get("items") or [])
            ruling_cards = list(raw.get("ruling_cards") or raw.get("cards") or [])
        elif isinstance(raw, list):
            messages = raw
        else:
            raise node.Refused("--messages-file must hold a JSON array of messages or {messages: [...]}")
    else:
        if not args.base_url or args.state_file is None:
            raise node.Refused("gate-check --thread requires --base-url and --state-file")
        base_url = node.normalize_base_url(args.base_url)
        state = node.load_state(args.state_file, base_url)
        thread_ans = node.thread(args.state_file, state, thread_id=args.thread, limit=None)
        if isinstance(thread_ans, dict):
            raw_cov = thread_ans.get("coverage")
            if isinstance(raw_cov, dict):
                coverage = raw_cov
                proj = raw_cov.get("projected")
                tot = raw_cov.get("total")
                if (
                    (
                        isinstance(proj, int)
                        and not isinstance(proj, bool)
                        and isinstance(tot, int)
                        and not isinstance(tot, bool)
                        and proj < tot
                    )
                    or raw_cov.get("complete") is False
                ):
                    projection_incomplete = {"projected": proj, "total": tot}
            if thread_ans.get("truncated") is True and projection_incomplete is None:
                projection_incomplete = {
                    "projected": coverage.get("projected") if isinstance(coverage, dict) else None,
                    "total": coverage.get("total") if isinstance(coverage, dict) else None,
                    "truncated": True,
                }
        hits = thread_ans.get("messages") or thread_ans.get("hits") or [] if isinstance(thread_ans, dict) else []
        messages = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            key = h.get("key")
            if isinstance(key, str) and key.startswith("msg/"):
                try:
                    msg_doc = node.read(args.state_file, state, key=key)
                    if isinstance(msg_doc, dict):
                        payload = msg_doc.get("value") if isinstance(msg_doc.get("value"), dict) else msg_doc
                        body_val = payload.get("body") if isinstance(payload, dict) else None
                        server_author = msg_doc.get("author")
                        if not isinstance(server_author, str) or not server_author.strip():
                            read_failures.append({
                                "key": key,
                                "code": "E_MISSING_SERVER_AUTHOR",
                                "message": "server-stamped author is missing or null",
                            })
                        elif not isinstance(body_val, str):
                            read_failures.append({
                                "key": key,
                                "code": "E_INVALID_MESSAGE_BODY",
                                "message": "message body is missing or not a string",
                            })
                        else:
                            clean_author = server_author.strip()
                            messages.append({
                                "key": msg_doc.get("key") or key,
                                "version": msg_doc.get("version"),
                                "recorded_at": msg_doc.get("recorded_at"),
                                "author": clean_author,
                                "from": clean_author,
                                "author_family": None,
                                "body": body_val,
                                "signature_verified": msg_doc.get("signature_verified"),
                                "status_verified": msg_doc.get("status_verified"),
                                "signed_by": msg_doc.get("signed_by"),
                            })
                except node.ReadFailed as exc:
                    err_obj = getattr(exc, "error", None)
                    if isinstance(err_obj, dict):
                        code = str(err_obj.get("error") or err_obj.get("code") or "E_READ_FAILED")
                        msg_str = str(err_obj.get("message") or err_obj.get("error_message") or exc)
                    else:
                        code = str(getattr(exc, "code", None) or err_obj or "E_READ_FAILED")
                        msg_str = str(exc)
                    read_failures.append({"key": key, "code": code, "message": msg_str})
        if args.require_operator_ruling:
            try:
                ans = node.cards(
                    args.state_file,
                    state,
                    cards.list_query(
                        types=("Ruling",),
                        thread=args.thread,
                        seat=None,
                        owner="operator",
                        status="active",
                        since=None,
                        until=None,
                        recorded_since=None,
                        limit=50,
                        after=None,
                    ),
                )
                ruling_cards = ans.get("cards", []) if isinstance(ans, dict) else []
            except Exception:  # noqa: BLE001
                ruling_cards = []

    expected_peers: dict[str, str] | None = None
    if getattr(args, "expected_peer", None):
        expected_peers = {}
        for pair in args.expected_peer:
            label, sep, peer = pair.partition("=")
            if not sep or not label.strip() or not peer.strip():
                raise node.Refused(f"--expected-peer takes LABEL=PEER_ID, got {pair!r}")
            expected_peers[label.strip()] = peer.strip()

    result = verdicts.evaluate_quorum(
        messages,
        target_sha=args.target_sha,
        builder=args.builder,
        builder_family=args.builder_family,
        required_seats=_labels(args.required_seats),
        required_families=_labels(args.required_families),
        min_distinct_families=args.min_families,
        require_cross_family=args.require_cross_family,
        ruling_cards=ruling_cards,
        require_operator_ruling=args.require_operator_ruling,
        expected_peers=expected_peers,
    )

    if projection_incomplete is not None:
        result["quorum_met"] = False
        if coverage is not None:
            result["projection_coverage"] = coverage
        result.setdefault("blockers", []).append({
            "reason": "INCOMPLETE_THREAD_PROJECTION",
            "projected": projection_incomplete.get("projected"),
            "total": projection_incomplete.get("total"),
        })

    if read_failures:
        result["quorum_met"] = False
        result["read_failures"] = read_failures
        result.setdefault("blockers", []).append({
            "reason": "INCOMPLETE_THREAD_READ",
            "failed_keys": [f["key"] for f in read_failures],
        })

    if args.repo is not None:
        trunk = args.trunk or "origin/main"
        try:
            merge_check = verdicts.check_merge_conflicts(args.repo, trunk, args.target_sha)
        except ValueError as exc:
            merge_check = {"clean": False, "conflicting_files": [], "error": str(exc)}
        result["merge_check"] = merge_check
        if not merge_check["clean"]:
            result["quorum_met"] = False

    _emit(result)
    return 0 if result["quorum_met"] else 1


def _resume_brief(args: argparse.Namespace, base_url: str) -> int:
    """`resume-brief`: the state file is loaded here, so that with --hook a missing or bad one is one line too."""
    try:
        state = node.load_state(args.state_file, base_url)
    except Exception as exc:  # noqa: BLE001
        if not args.hook:
            raise
        print(f"resume-brief: unavailable ({type(exc).__name__}: {resume_brief.one_line(exc, 160)})")
        return 0
    return resume_brief.run(args, state, node, transcript_graph)


def _orient(args: argparse.Namespace) -> int:
    """`orient`: run 5-source pre-idle orientation and print JSON or human-readable summary."""
    report = orient.run_orient(
        state_file=args.state_file,
        base_url=args.base_url,
        identities=_labels(args.identity),
        seat=args.seat,
        spool_dir=args.spool_dir,
        repos=args.repo,
        worktree_roots=args.worktree_root,
        handover_dir=args.handover_dir,
        scratchpad_dirs=args.scratchpad_dir,
        since=args.since,
        stale_handover_hours=args.stale_handover_hours,
        offline=args.offline,
        probe_brain_identity=not args.no_brain_probe,
        include_review_preflight=args.include_review_preflight,
    )
    board = None
    if getattr(args, "board_tasks", False):
        # Kept here, not in orient.py, so it stacks on SD 265 item 5's orient.py without a conflict (a reviewer 02ae6a).
        if not args.seat:
            board = {"error": "board_tasks needs --seat"}
        else:
            try:
                board = board_tasks.run_tasks(args.seat, _labels(args.identity),
                                              cursor_path=board_tasks.default_cursor_path(args.seat),
                                              roster=board_tasks.fleet_seats_roster(),
                                              state_file=args.state_file)
            except (board_tasks.BoardUnavailable, ValueError, OSError) as exc:
                board = {"error": "board_unavailable", "detail": str(exc)[:240]}
        report["board_tasks"] = board
    if args.json:
        _emit(report)
    else:
        sys.stdout.write(orient.format_orient_text(report))
        if board is not None:
            sys.stdout.write(board_tasks.format_text(board) if "error" not in board
                             else "board tasks: unavailable (%s)%s" % (board.get("detail") or board["error"], chr(10)))
        sys.stdout.flush()
    return 0


def _tasks(args: argparse.Namespace) -> int:
    """`tasks`: this seat's open items on the Planboard board (board_tasks.py), read-only."""
    cursor = None if args.no_cursor else (args.cursor_file or board_tasks.default_cursor_path(args.seat))
    try:
        report = board_tasks.run_tasks(
            args.seat, _labels(args.alias), projects=args.project or None, issues_file=args.issues_file,
            owner_map=board_tasks.load_owner_map(args.owner_map), cursor_path=cursor,
            update_cursor=args.update_cursor, reader=args.reader, export_path=args.export,
            roster=(_labels(args.roster) or None) if args.issues_file is not None
            else (_labels(args.roster) or board_tasks.fleet_seats_roster()),
            state_file=args.state_file,
            now=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except board_tasks.BoardUnavailable as exc:
        _emit({"error": "board_unavailable", "detail": str(exc)})
        return 3
    if args.json:
        _emit(report)
    else:
        sys.stdout.write(board_tasks.format_text(report))
        sys.stdout.flush()
    return 0


def _claim(args: argparse.Namespace) -> int:
    """`board-claim`: every change to a board item's claim (board_claim.py, claim.v2 draft 4)."""
    refusal = board_tasks.codex_refusal(args.seat, state_file=args.state_file)
    if refusal:
        _emit({"refused": refusal})
        return board_claim.EXIT_REFUSED
    key = board_claim.session_key(override=args.session_key)
    if key is None and args.verb not in ("show", "check"):
        _emit({"refused": "no session id in the environment (CLAUDE_CODE_SESSION_ID or a Codex thread id): a claim "
                          "must belong to a session. Pass --session-key to name one"})
        return board_claim.EXIT_USAGE
    handover_text = None
    if args.handover_file is not None:
        try:
            raw = args.handover_file.read_bytes()[:1_000_000]
        except OSError as exc:
            _emit({"refused": "the handover file did not read: %s" % exc})
            return board_claim.EXIT_USAGE
        handover_text = raw.decode("utf-8", "replace")
    try:
        nonce = board_claim.session_nonce(args.seat, key) if key else ""
    except OSError as exc:
        _emit({"error": "session_file", "detail": str(exc)})
        return board_claim.EXIT_BOARD
    me = board_claim.Me(args.seat, nonce, board_claim.box_user(args.seat))
    if (args.issue is None) != (args.verb in ("pause", "resume")):
        _emit({"refused": "--issue is required for %s" % args.verb if args.issue is None
               else "%s acts on the queue's claim-pause document; it takes no --issue" % args.verb})
        return board_claim.EXIT_USAGE
    if args.project_id is not None and args.verb not in ("pause", "resume"):
        _emit({"refused": "--project-id goes with pause or resume only"})
        return board_claim.EXIT_USAGE
    try:
        mcp = board_tasks.McpBoard(board_tasks.plugin_root(), tools=board_tasks.CLAIM_TOOLS)
    except (board_tasks.BoardUnavailable, OSError) as exc:
        _emit({"error": "board_unavailable", "detail": str(exc)})
        return board_claim.EXIT_BOARD
    try:
        if args.verb in ("pause", "resume"):
            report = board_claim.run_pause(args.verb, board_claim.ClaimBoard(mcp.call), me,
                                           reason=args.reason, project_id=args.project_id)
        else:
            report = board_claim.run(args.verb, args.issue, board_claim.ClaimBoard(mcp.call), me,
                                     work_ref=args.work_ref, note=args.note, reason=args.reason,
                                     handover_text=handover_text, work_doc=args.work_doc,
                                     for_seat=args.for_seat, code=args.code)
        board_claim.remember_box_user(args.seat, report.get("board_user"))
    except board_claim.Refused as exc:
        _emit({"refused": str(exc), "issue": args.issue})
        return board_claim.EXIT_REFUSED
    except board_claim.Lost as exc:
        _emit({"lost": str(exc), "issue": args.issue, "next": "re-read with `board-claim show` and decide again"})
        return board_claim.EXIT_LOST
    except (board_tasks.BoardUnavailable, LookupError) as exc:
        _emit({"error": "board", "detail": str(exc), "issue": args.issue})
        return board_claim.EXIT_BOARD
    finally:
        mcp.close()
    code = board_claim.EXIT_OK
    wanted = board_claim.presence_for(args.verb, args.issue, report) if args.presence_base_url else None
    if wanted is not None:
        report["presence"], code = _claim_presence(args, *wanted)
    _emit(report)
    if report.get("merge_allowed") is False:
        return board_claim.EXIT_REFUSED
    return code


def _claim_presence(args: argparse.Namespace, state_value: str, note: str) -> tuple[dict, int]:
    """Publish the seat's presence after a claim write. The claim already stands: a failure here is reported with
    the command that repeats it, never by undoing the claim."""
    retry = ("python -m tools.meshy_node presence --base-url %s --state-file %s --state %s --note \"%s\" --ttl %d"
             % (args.presence_base_url, args.state_file, state_value, note, board_claim.PRESENCE_TTL_SECONDS))
    if args.state_file is None:
        return {"published": False, "why": "--presence-base-url needs --state-file", "retry": retry}, \
            board_claim.EXIT_PRESENCE
    try:
        # the same spelling rule as every other route: a trailing slash must not miss the state file's stored URL
        # (Codex P2 on cfaa872c, cli.py:2691)
        state = node.load_state(args.state_file, node.normalize_base_url(args.presence_base_url))
        node.presence(args.state_file, state, state_value=state_value, note=note,
                      ttl_secs=board_claim.PRESENCE_TTL_SECONDS)
    except Exception as exc:                          # noqa: BLE001 - the claim stands; say what failed
        return {"published": False, "state": state_value, "why": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "retry": retry}, board_claim.EXIT_PRESENCE
    return {"published": True, "state": state_value, "note": note}, board_claim.EXIT_OK


def _dispatch(args: argparse.Namespace) -> int:
    """`board-dispatch plan`: a dry run (board_dispatch.py). One board GET at most; no write of any kind."""
    refusal = board_tasks.codex_refusal(args.seat, state_file=args.state_file)
    if refusal:
        _emit({"refused": refusal})
        return board_queue.EXIT_USAGE
    if not board_queue.is_id(args.target_issue):
        _emit({"refused": "--target-issue must be an issue id like TKT-998"})
        return board_queue.EXIT_USAGE
    try:
        fleet = json.loads(Path(args.fleet_json).read_text(encoding="utf-8-sig"))
        families = (json.loads(Path(args.families_file).read_text(encoding="utf-8-sig"))
                    if args.families_file else None)
        body = Path(args.view_file).read_text(encoding="utf-8") if args.view_file else None
    except (OSError, ValueError) as exc:
        _emit({"refused": "an input file did not read (%s)" % type(exc).__name__})
        return board_queue.EXIT_USAGE
    rows = fleet.get("rows") if isinstance(fleet, dict) else fleet
    if not isinstance(rows, list) or (families is not None and not isinstance(families, dict)):
        _emit({"refused": "--fleet-json must hold the fleet rows and --families-file a JSON object"})
        return board_queue.EXIT_USAGE
    if body is None:
        try:
            mcp = board_tasks.McpBoard(board_tasks.plugin_root(), tools=board_tasks.READ_TOOLS | board_tasks.CLAIM_TOOLS)
        except (board_tasks.BoardUnavailable, OSError) as exc:
            _emit({"error": "board_unavailable", "detail": str(exc)})
            return board_queue.EXIT_BOARD
        try:
            doc = board_queue.ViewBoard(mcp.call).read(args.target_issue)
        except (board_tasks.BoardUnavailable, LookupError, ValueError) as exc:
            _emit({"error": "board", "detail": board_queue.safe_text(exc)})
            return board_queue.EXIT_BOARD
        finally:
            mcp.close()
        body = (doc or {}).get("body")
    plan = board_dispatch.plan(body, rows, now=board_claim._now(), families=families, dispatcher=args.seat,
                               fleet_as_of=fleet.get("as_of") if isinstance(fleet, dict) else None)
    if args.json:
        _emit(plan)
    else:
        sys.stdout.write(board_dispatch.render(plan) + chr(10))
        sys.stdout.flush()
    return board_queue.EXIT_OK


def _review_receipt(args: argparse.Namespace) -> int:
    """`review-receipt`: read only. Exit 0 a clean AI review of the PR's current head, 1 not clean, 2 an input that
    did not read, 3 the file mode (never a merge check, whatever it found)."""
    import hashlib

    from . import review_receipt
    try:
        report = hashlib.sha256(Path(args.report).read_bytes()).hexdigest() if args.report else None
    except OSError as exc:
        _emit({"refused": "--report did not read (%s)" % type(exc).__name__})
        return 2
    if args.comments_file is not None:
        try:
            comments = json.loads(Path(args.comments_file).read_text(encoding="utf-8-sig"))
            messages = (json.loads(Path(args.messages_file).read_text(encoding="utf-8-sig"))
                        if args.messages_file else [])
        except (OSError, ValueError) as exc:
            _emit({"refused": "an input file did not read (%s)" % type(exc).__name__})
            return 2
        v = review_receipt.verdict(comments, [messages] if isinstance(messages, dict) else messages, args.head,
                                   author_seat=args.author_seat or None, report_sha256=report)
        v["live"] = False
        if args.json:
            _emit(v)
        else:
            sys.stdout.write(review_receipt.render(v) + chr(10))
            sys.stdout.flush()
        return 3
    if args.messages_file is not None or args.author_seat is not None:
        _emit({"refused": "--messages-file and --author-seat go with --comments-file only; a merge check reads the "
                          "messages itself (--message-key) and takes the author from the PR"})
        return 2
    if not args.repo or args.pr is None:
        _emit({"refused": "a merge check needs --repo OWNER/REPO and --pr N"})
        return 2
    read_message = None
    if args.message_key:
        if not args.base_url or args.state_file is None:
            _emit({"refused": "--message-key needs --base-url and --state-file (the message is read and verified "
                              "by this client)"})
            return 2
        try:
            state = node.load_state(args.state_file, node.normalize_base_url(args.base_url))
        except (node.Refused, OSError, ValueError) as exc:
            _emit({"refused": "the state file did not load (%s)" % type(exc).__name__})
            return 2

        def read_message(key):
            return node.read(args.state_file, state, key=key, record=False)
    v = review_receipt.check(args.repo, args.pr, args.head, message_keys=args.message_key or [],
                             read_message=read_message, report_sha256=report)
    if args.json:
        _emit(v)
    else:
        sys.stdout.write(review_receipt.render(v) + chr(10))
        sys.stdout.flush()
    return 0 if v.get("clean") is True else 1


def _queue(args: argparse.Namespace) -> int:
    """`board-queue`: the queue view (board_queue.py). `refresh` can write expiry marks and the view document."""
    refusal = board_tasks.codex_refusal(args.seat, state_file=args.state_file)
    if refusal:
        _emit({"refused": refusal})
        return board_queue.EXIT_USAGE
    if args.days < 1 or args.days > 365:
        _emit({"refused": "--days must be 1 to 365"})
        return board_queue.EXIT_USAGE
    if not board_queue.is_queue_key(args.queue_doc):
        _emit({"refused": "--queue-doc must be a queue document key like pull-queue-g12"})
        return board_queue.EXIT_USAGE
    bad = [x for x in [args.target_issue, *(args.item or [])] if not board_queue.is_id(x)]
    if bad or len(args.item or []) > board_queue.MAX_EXTRA:
        _emit({"refused": "--target-issue and --item must be issue ids like TKT-998 (at most %d --item)"
                          % board_queue.MAX_EXTRA})
        return board_queue.EXIT_USAGE
    try:
        mcp = board_tasks.McpBoard(board_tasks.plugin_root(),
                                   tools=board_tasks.READ_TOOLS | board_tasks.CLAIM_TOOLS)
    except (board_tasks.BoardUnavailable, OSError) as exc:
        _emit({"error": "board_unavailable", "detail": str(exc)})
        return board_queue.EXIT_BOARD
    try:
        if args.verb == "show":
            doc = board_queue.ViewBoard(mcp.call).read(args.target_issue)
            report = {"target": args.target_issue, "present": doc is not None,
                      "body": (doc or {}).get("body"), "revision": (doc or {}).get("latestRevisionId")}
        else:
            report = board_queue.refresh(mcp.call, seat=args.seat, target=args.target_issue, days=args.days,
                                         extra=args.item or [], full=args.full, dry_run=args.dry_run,
                                         call_paged=mcp.call_paged, queue_key=args.queue_doc,
                                         anchor=board_queue.Anchor(
                                             Path(args.state_file).parent / ("board-queue-anchor-%s.json" % args.target_issue)
                                             if args.state_file else None))
    except board_claim.Lost as exc:
        _emit({"lost": str(exc), "next": "read the newer view with `board-queue show`"})
        return board_queue.EXIT_LOST
    except (board_tasks.BoardUnavailable, board_queue.ReadBudgetSpent, LookupError, ValueError) as exc:
        _emit({"error": "board", "detail": board_queue.safe_text(exc)})
        return board_queue.EXIT_BOARD
    finally:
        mcp.close()
    if not args.json and report.get("body") and (args.verb == "show" or args.dry_run):
        sys.stdout.write(board_queue.frame(report["body"]) + chr(10))
        sys.stdout.flush()
        return board_queue.EXIT_OK
    _emit(report)
    return board_queue.EXIT_OK


def run(args: argparse.Namespace) -> int:
    """Dispatch one command. The op lock a send with attachments takes (args.op_hold) is released however the
    command ends, a raise included (pxx INFO a)."""
    with contextlib.ExitStack() as op_hold:
        args.op_hold = op_hold
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    if getattr(args, "monitor", False) or getattr(args, "monitor_only", None):  # monitor.py
        from . import monitor
        return monitor.run(args)
    if args.cmd == "share":                                # no node identity: it commits a file and prints the citation
        return docshare.run(args)
    if args.cmd == "verify-bundle":
        return _verify_bundle(args)
    if args.cmd == "gate-check":
        return _gate_check(args)
    if args.cmd == "orient":
        return _orient(args)
    if args.cmd == "board-claim":                          # the board only, like tasks
        return _claim(args)
    if args.cmd == "board-queue":                          # the board only, like tasks
        return _queue(args)
    if args.cmd == "board-dispatch":                       # a dry run: one board read at most
        return _dispatch(args)
    if args.cmd == "review-receipt":                       # reads only: GitHub GETs, verified message reads
        return _review_receipt(args)
    if args.cmd == "tasks":                                # no node identity and no MeshyChat server: the board only
        return _tasks(args)
    if args.cmd == "lookup-help":                          # no node identity and no server: it prints a text
        return _lookup_help(args)
    if args.cmd == "card" and args.card_cmd == "recent":   # exit 0 whatever happens, a bad state file included
        return _card_recent(args)
    if args.cmd == "send-status" and args.peek:            # the journal only: no state load, no server (#549)
        result = node.op_peek(args.state_file, args.op_id)
        _emit(result)
        return 0 if result.get("state") == "succeeded" else 1
    base_url = node.normalize_base_url(args.base_url)
    if args.cmd == "mint":
        _emit(node.mint(args.state_file, base_url, args.code,
                        device_label=args.device_label, memory_peer_id=args.memory_peer))
        return 0
    if args.cmd == "keygen":
        _emit(node.keygen(args.state_file, base_url, public_key_out=args.public_key_out,
                          memory_peer_id=args.memory_peer))
        return 0
    if args.cmd == "adopt":
        _emit(node.adopt(args.state_file, base_url, from_state=args.from_state,
                         memory_peer_id=args.memory_peer))
        return 0

    if args.cmd == "resume-brief":
        return _resume_brief(args, base_url)
    state = node.load_state(args.state_file, base_url)
    if args.cmd == "bind":
        _emit(node.bind(args.state_file, state, peer_id=args.peer_id, kid=args.kid))
        return 0
    if args.cmd == "pair":
        _emit(node.pair(args.state_file, state, count=args.count))
        return 0
    if args.cmd == "invite":
        _emit(node.invite(args.state_file, state, count=args.count))
        return 0
    if args.cmd == "claim":
        result = node.claim(args.state_file, state, namespace=args.namespace,
                            retention_policy=args.retention_policy)
        _emit(result)
        return 0 if node.succeeded(result) else 1
    if args.cmd == "grant":
        result = node.grant(args.state_file, state, namespace=args.namespace,
                            grantee_peer_id=args.grantee, permission=args.permission,
                            expires_at=args.expires_at, revoke=args.revoke)
        _emit(result)
        ok = node.succeeded(result) and (not args.revoke or node.revoke_took_effect(result))
        return 0 if ok else 1
    if args.cmd == "signin":
        _emit(node.signin(args.state_file, state))
        return 0
    if args.cmd == "send-status":
        result = node.op_status(args.state_file, state, args.op_id,
                                admit=lambda value, bulk_ack: _admit_op_sendguard(args.state_file, value, bulk_ack))
        if result.get("state") == "succeeded":
            counted, missed = _record_op_sendguard(args.state_file, args.op_id)
            if missed:
                _stderr_line({"warning": "send_not_recorded", "reason": missed})
        _emit(result)
        return 0 if result.get("state") == "succeeded" else 1
    if args.cmd == "send-drain":
        result = node.op_drain(args.state_file, state,
                               admit=lambda value, bulk_ack: _admit_op_sendguard(args.state_file, value, bulk_ack),
                               record=lambda op_id: _record_drain_sendguard(args.state_file, op_id))
        sys.stdout.write(json.dumps({"event": "send_drain", "succeeded": result["succeeded"],
                                    "refused": result["refused"], "uncertain": result["uncertain"],
                                    "refused_ids": result["refused_ids"], "uncertain_ids": result["uncertain_ids"],
                                     "busy_ids": result.get("busy_ids", [])},
                                   separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    if args.cmd == "send":
        if args.done_when is not None and not args.requires_ack:
            raise node.Refused("--done-when requires --requires-ack or --expects-reply")
        for close in args.closes or ():
            if not _is_mesh_key(close):
                raise node.Refused("--closes values must be in-band epoch-ms msg/ keys")
        declines = list(args.declines or ())
        for decline in declines:
            if not _is_mesh_key(decline):
                raise node.Refused("--declines values must be in-band epoch-ms msg/ keys")
        if len(declines) > 16:
            raise node.Refused("--declines accepts at most 16 msg/ keys")
        if len(set(declines)) != len(declines):
            raise node.Refused("--declines must not contain duplicate msg/ keys")
        if args.closes and (set(args.closes) & set(declines)):
            raise node.Refused("--closes and --declines must not contain the same msg/ key")
        if declines and not args.force_blind_close:
            read_keys = {r["key"] for r in node.ledger_records(args.state_file, state)}
            unread_declines = [d for d in declines if d not in read_keys]
            if unread_declines:
                raise node.Refused(
                    f"cannot decline unread ask(s) {', '.join(unread_declines)}; "
                    f"run `read --key {unread_declines[0]}` first or pass --force-blind-close"
                )
        # Backlog item 15: the server's subject and ref shapes, checked before any upload, read or send. An empty
        # value is not sent (wire.mesh_message omits it), so it is not asked.
        for flag, given, problem in (("--subject", args.subject, wire.subject_problem),
                                     ("--ref", args.ref, wire.ref_problem)):
            why = problem(given) if given else None
            if why:
                raise node.Refused(f"{flag} {given!r} refused: {why}")
        text = args.body if args.body is not None else args.body_file.read_text(encoding="utf-8")
        sendguard.refuse_untrusted_transcript_paste(text, refused=node.Refused)
        cited = [] if args.allow_local_path else docshare.refuse_local_files(text, node.Refused)
        to, cc = _labels(args.to), _labels(args.cc)
        sender = (args.sender or node.default_sender(state)) if args.op_id else args.sender
        journalled_auto = node.op_journalled_auto_closes(args.state_file, args.op_id) if args.op_id else None
        closes = list(args.closes or ())
        auto_closes: list[str] = []
        if journalled_auto and args.keep_open:
            # The op was created with an automatic close, so a retry asking --keep-open is a changed retry: refused
            # like an added or a dropped --closes, never sent with the close put back (GitHub Codex r4366654087).
            raise node.Refused(f"--keep-open on a retry of op {args.op_id!r}, which was created with the automatic "
                               f"close {', '.join(journalled_auto)}; a changed retry is refused: use a new --op-id")
        if journalled_auto is not None:
            # Only the auto-close journalled when the op was created comes back (pxx). The explicit --closes are
            # the caller's, so an added or a dropped one changes the fingerprint and the retry is refused.
            auto_closes = [key for key in journalled_auto if key not in closes]
            closes += auto_closes
        close_reason = None
        if journalled_auto is None and not args.keep_open and args.msg_type == "answer":
            auto_close, close_reason = _owed_question_to_close(args.state_file, state, args.in_reply_to, args.thread)
            if auto_close is not None and auto_close not in closes and auto_close not in declines:
                closes.append(auto_close)
                auto_closes.append(auto_close)
        if args.op_id:
            try:
                reason = sendguard.clean_reason(args.bulk_ack)
            except ValueError as exc:                      # the JSON refusal admit() gives, not a traceback (pxx)
                raise node.Refused(str(exc))
            text = f"[bulk-ack] {reason}\n{text}" if reason is not None else text
            subject = args.subject if args.subject else ("bulk-ack" if reason is not None else None)
            if args.attach:
                _attach_local_refusals(args.attach)        # before hashing or locking anything
                # Held from this preflight through the upload and op_send, so no other send of the same id can bind it
                # in between with other files (pxx #1) or upload a second copy (GitHub Codex r4366812520).
                args.op_hold.enter_context(node.op_hold(args.state_file, args.op_id))
            preflight = node.op_preflight(args.state_file, op_id=args.op_id, lock_held=bool(args.attach),
                                          to=to, cc=cc, thread_id=args.thread,
                                          body=text, msg_type=args.msg_type, author_family=args.author_family,
                                          sender=sender, in_reply_to=args.in_reply_to, requires_ack=args.requires_ack,
                                          subject=subject, ref=args.ref, session_label=args.session_label,
                                          done_when=args.done_when, closes=closes, declines=declines or None,
                                          auto_closes=auto_closes,
                                           attachments=_attachment_fingerprints(args.attach), bulk_ack=args.bulk_ack,
                                          operator_words=_operator_words(args), directive=_directive(args, lambda label: node.directive_target_peer(args.state_file, state, label)),
                                          directive_hours=_directive_hours(args), window_end=_window_end(args),
                                          **_urgent_kwargs(args))
            if preflight is not None:
                args.op_hold.close()                       # recording takes the op lock itself
                counted, missed = _record_op_sendguard(args.state_file, args.op_id)
                if missed:
                    _stderr_line({"warning": "send_not_recorded", "reason": missed})
                _emit(preflight)
                return 0
        else:
            text, subject = sendguard.admit(args.state_file, to=to, cc=cc, body=text, subject=args.subject,
                                            bulk_ack=args.bulk_ack, refused=node.Refused)
        attachments = None
        # A presented op that isn't held already has its files uploaded and cited in its journalled envelope; preflight
        # has checked the caller's files against it. Uploading again would spend quota on files nothing cites
        # (GitHub Codex r4364598285), so only a new or a held op uploads.
        journalled_refs = node.op_journalled_attachments(args.state_file, args.op_id) if args.op_id else None
        # Looked up with or without --attach: a held op retried without files still abandons its earlier upload, and
        # that upload is named below (pxx #1).
        held_refs = (node.op_held_attachments(args.state_file, args.op_id)
                     if args.op_id and journalled_refs is None else None)
        guard_refusal = None
        if args.op_id and args.attach and journalled_refs is None:
            # A new or a held op is admitted by op_send only after this point, so check the same guard here first:
            # a refused op must not upload files that then spend quota (GitHub Codex r4366756038). op_send still
            # admits right before presenting; a refusal here is handed to it, so it holds the op the same way.
            try:
                _admit_op_sendguard(args.state_file, {"to": to, "cc": cc, "body": text, "subject": subject},
                                    args.bulk_ack)
            except node.Refused as exc:
                guard_refusal = exc
        if args.attach and journalled_refs is not None:
            attachments = journalled_refs
        elif args.attach and held_refs and _ref_fingerprints(held_refs) == _attachment_fingerprints(args.attach):
            attachments = held_refs                        # a held op's own upload, the same files: never uploaded twice
        elif args.attach and guard_refusal is None:        # after every local refusal, before the message
            try:
                attachments = _attach(args, state)
            except files.AttachFailed as exc:
                args.op_hold.close()
                _emit({**exc.error, "sent": False})
                return 2 if exc.error.get("error") == files.E_ATTACH_UNREACHABLE else 1
        if held_refs and attachments is not held_refs:
            # The held op is rebound with other files (or none), so its earlier upload is cited by nothing. Named, as
            # divergence 079 names an unconfirmed send's files, so it is not lost silently (pxx #2).
            _stderr_line({"warning": "held_op_attachments_uncited", "op_id": args.op_id,
                          "file_ids": [ref.get("file_id") for ref in held_refs],
                          "note": "uploaded for an earlier, never-sent try of this op and now cited by nothing; "
                                  "delete one (DELETE /v1/files/<id>) if no other message cites it"})
        # Divergence 083 (#140): warn, never refuse, when a `to` recipient is unavailable or behind.
        # After every local refusal, so a refused send makes no call; fails open, so it never delays or
        # changes the send beyond its own short timeout.
        checked, warned = None, []
        if not args.no_recipient_check:
            try:
                checked = recipientwarn.fetch(
                    args.state_file, to,
                    lambda labels: node.recipient_status(args.state_file, state, labels,
                                                         timeout=recipientwarn.TIMEOUT_S))
                warned = recipientwarn.warnings(checked, body_bytes=len(text.encode("utf-8")))
            except Exception:                              # noqa: BLE001 - advisory: never stops the send
                checked, warned = None, []
        for w in warned:
            if not w.get("shadow"):                        # shadow warnings are logged, never printed (084)
                _stderr_line(w)
        want_hints = (
            (args.hints_check or os.environ.get("MESHY_SEND_HINTS_CHECK", "").strip().lower() in ("1", "true", "yes"))
            and not args.no_hints_check
        )
        if want_hints and not args.in_reply_to and guard_refusal is None:
            try:
                hints_ans = hints.query_send_hints(
                    (args.state_file, state),
                    to=to,
                    cc=cc,
                    thread_id=args.thread,
                    in_reply_to=args.in_reply_to,
                    body=text,
                    closes=closes,
                    sender_label=(args.sender or node.default_sender(state)),
                    msg_type=args.msg_type,
                    ref=args.ref,
                )
                hints_notice = hints.format_send_hints_notice(hints_ans, in_reply_to=args.in_reply_to)
                if hints_notice:
                    for hline in hints_notice.splitlines():
                        _stderr_line({"hint": "send_collision_hint", "thread": args.thread, "note": hline})
            except Exception:                              # noqa: BLE001 - advisory: never stops the send
                pass
        # Divergence 093: what OTHER seats hold in this thread. The read runs beside the send, begun by the send
        # itself once its write is accepted (never before: both are charged to one bucket) and collected after
        # it, so a healthy send waits for nothing; it fails quiet, like the check above.
        card_read = None
        # A seat with neither a stamped label nor a peer to ask about cannot tell another seat's card from its own.
        if not args.no_card_check and (cards.stamped_seat(state) is not None or state.get("peer_id")):
            card_read = cards.Beside(lambda: cards.thread_read(args.state_file, state, args.thread))
        try:
            with node.after_accept(card_read.start if card_read is not None else None):
                send_kwargs = dict(to=to, cc=cc, thread_id=args.thread, body=text, msg_type=args.msg_type,
                                   author_family=args.author_family, sender=sender,
                                   in_reply_to=args.in_reply_to, requires_ack=args.requires_ack,
                                   subject=subject, ref=args.ref, session_label=args.session_label,
                                    done_when=args.done_when, closes=closes, declines=declines or None,
                                    attachments=attachments,
                                    operator_words=_operator_words(args), directive=_directive(args, lambda label: node.directive_target_peer(args.state_file, state, label)),
                                    window_end=_window_end(args), **_urgent_kwargs(args))
                # bulk_ack is journalled by op_send only; node.send takes no such argument (GitHub Codex r4360487372).
                result = (node.op_send(args.state_file, state, op_id=args.op_id,
                                        admit=(_raise(guard_refusal) if guard_refusal is not None else
                                               lambda value, bulk_ack: _admit_op_sendguard(args.state_file, value,
                                                                                           bulk_ack)),
                                        bulk_ack=args.bulk_ack, auto_closes=auto_closes,
                                        directive_hours=_directive_hours(args),
                                        lock_held=bool(args.attach), **send_kwargs)
                          if args.op_id else node.send(args.state_file, state, **send_kwargs))
        except (node.ServerSaid, node.Refused, wire.NotV1, OSError):
            if attachments:
                # NOT deleted: an invoke that failed after it was accepted may still have stored the message, and a
                # file it cites must not vanish under it. Named, so they are not lost either (divergence 079).
                _stderr_line({"warning": "attachments_uploaded_send_unconfirmed",
                              "file_ids": [ref["file_id"] for ref in attachments],
                              "note": "uploaded for this send, which did not complete; kept, because the message "
                                      "may still have been stored. Delete one (DELETE /v1/files/<id>) only once "
                                      "you know no message cites it"})
            raise
        finally:
            args.op_hold.close()                           # the op lock, when this send took one for its upload
        if _urgent_was_rate_limited(args, result):
            # The urgent lane's fallback (kxxx, 3e): the SAME message as ordinary, marked urgent_declined, so a
            # question is never dropped and its reader sees the sender spent its hourly allowance.
            _stderr_line({"warning": "urgent_declined", "reason": result.get("error_code"),
                          "note": "this seat's urgent allowance is spent; the message is resent as ordinary, marked "
                                  "urgent_declined"})
            result = node.send(args.state_file, state, **dict(send_kwargs, urgent=False,
                                                              urgent_declined="rate_limited"))
        if args.op_id and result.get("state") == "refused-by-guard":
            raise node.Refused(result["guard_reason"])
        _emit(result)
        ok = node.succeeded(result)
        # An op-id send is recorded from the original journal entry, whichever command first proves storage.
        first = not args.op_id or result.get("first_verified_success") is True
        if ok and close_reason and args.in_reply_to:
            _owed_close_hint(args.in_reply_to, close_reason)
        if ok:                                             # a send the server did not accept is not counted
            missed = None
            if args.op_id:
                first, missed = _record_op_sendguard(args.state_file, args.op_id)
            if first:
                if not args.op_id:
                    missed = sendguard.record(args.state_file, to=to, cc=cc,
                                              key=result.get("key", "") if isinstance(result, dict) else "",
                                              body_bytes=len(text.encode("utf-8")), body=text)
                if missed:                                 # never fails the accepted send; the rules undercount it by one
                    _stderr_line({"warning": "send_not_recorded", "reason": missed})
                recipientwarn.log(args.state_file, key=str(result.get("key") or "") if isinstance(result, dict) else "",
                                  answer=checked, warned=warned, body_bytes=len(text.encode("utf-8")))
                recipientwarn.invalidate(args.state_file)  # this send changed the counts the cache holds
        own = _self_addressed(state, args.sender, _labels(args.to), _labels(args.cc)) if ok else []
        if ok:                                             # cards first: one in this very thread is the stronger signal
            for line in cards.after_send(args.state_file, card_read, state=state, thread=args.thread,
                                         key=str(result.get("key") or "") if isinstance(result, dict) else ""):
                _stderr_line(line)
        if ok and not args.attach and sendguard.says_attached(text):    # audit finding 5: "attached", nothing attached
            _stderr_line({"warning": "says_attached_without_attach",
                          "note": "sent, but the body says a file is attached and the message carries none. Send the "
                                  "file with --attach to the readers who need it; a file already sent to someone else "
                                  "can be granted to another reader by its owner (POST /v1/files/<id>/grants)"})
        if ok and cited:                                   # #120: a cited folder or another machine's path is sent, and said
            _stderr_line({"warning": "local_path_cited", "paths": cited[:docshare._MAX_SHOWN],
                          "note": "sent: each names no file on this machine (a folder, another machine's path, or one "
                                  "that does not exist here), so it reads as a citation. No reader can open it; put what "
                                  "they need inline, or `share` it"})
        for hint in _send_hints(args.msg_type, args.requires_ack, args.in_reply_to, closes, declines) if ok else []:
            _stderr_line(hint)
        if ok and args.msg_type in ANSWER_TYPES and (args.in_reply_to or closes or declines):
            left = _left_open_hint(args.state_file, state, to, args.thread, args.in_reply_to, closes, declines)
            if left:
                _stderr_line(left)
        if own:
            _stderr_line({
                "warning": "self_addressed", "labels": own,
                "note": "stored and readable by key, but a message is never pushed or spooled to "
                        "its own author (divergence 020); a send to yourself is not a delivery "
                        "test, ask another seat to answer instead"})
        return 0 if ok else 1
    if args.cmd == "fetch":
        out_dir = args.out if args.out is not None else Path.cwd()
        if not out_dir.is_dir():
            raise node.Refused(f"--out {out_dir} is not a directory")
        try:
            message = node.read(args.state_file, state, key=args.key)
        except node.ReadFailed as exc:
            _emit(exc.error)
            return 1
        # `is not False` rejects explicit signature failure while allowing offline/mock dicts that omit the flag
        if isinstance(message, dict) and message.get("signature_verified") is not False:
            msg_sender = str(message.get("author") or message.get("from") or "").strip()
            own_label = str(state.get("label") or "").strip()
            if msg_sender and msg_sender.casefold() != own_label.casefold():
                sendguard.record_inbound(args.state_file, sender=msg_sender, key=str(message.get("key") or args.key))
        summary = files.fetch(args.state_file, state, message, out_dir)
        _emit(summary)
        return 0 if summary["ok"] else 1
    if args.cmd == "offer":
        return offer.run_offer(args, state)
    if args.cmd == "pickup":
        return offer.run_pickup(args, state)
    if args.cmd == "edit":
        text = args.body if args.body is not None else args.body_file.read_text(encoding="utf-8")
        sendguard.refuse_untrusted_transcript_paste(text, refused=node.Refused)
        try:
            result = node.edit(args.state_file, state, key=args.key, body=text,
                               expected_head_version=args.expected_head_version)
        except node.ReadFailed as exc:
            _emit(exc.error)
            return 1
        _emit(result)
        return 0 if node.succeeded(result) else 1
    if args.cmd == "redact":
        result = node.redact(args.state_file, state, key=args.key, namespace=args.namespace, version=args.version,
                             reason_code=args.reason, blank_thread_id=args.blank_thread_id)
        _emit(result)
        if not node.succeeded(result):
            return 1
        return 3 if result["text_remains_in_versions"] else 0
    if args.cmd == "inbox":
        _emit(node.inbox(args.state_file, state, limit=args.limit, include_cc=not args.no_cc,
                         bodies=args.bodies))
        return 0
    if args.cmd == "owed":
        owed_since = None if args.since in (None, "all") else _owed_since(args.since)
        if args.reconcile:
            return _owed_reconcile(args, state, owed_since)
        stray = [name for name in OWED_RECONCILE_ONLY if getattr(args, name) not in (None, False)]
        if stray:
            raise node.Refused(f"--{stray[0].replace('_', '-')} goes with --reconcile")
        if args.state is None and not args.counts_only:
            try:
                _emit(_owed_bare(args, state, owed_since))
            except node.FiltersNotApplied as exc:
                _emit(exc.error)
                return 1
            return 0
        if args.state is None:                             # --counts-only keeps #373's state and fields
            args.state = OWED_DEFAULT_STATE
        try:
            answer = node.owed(args.state_file, state, limit=args.limit,
                               states=None if args.state == "all" else args.state,
                               since=owed_since, thread=args.thread)
        except node.FiltersNotApplied as exc:
            _emit(exc.error)
            return 1
        reply_owed = _owed_bucket(answer, OWED_REPLY_STATES)
        answered_still_open = _owed_bucket(answer, (OWED_ANSWERED_STATE,))
        if args.counts_only:
            requested_states = {"open", "active", "answered", "declined", "closed"} if args.state == "all" else set(args.state.split(","))
            reply_in_scope = bool(requested_states & set(OWED_REPLY_STATES))
            answered_in_scope = OWED_ANSWERED_STATE in requested_states
            # SD 255 adds the split of reply_owed, so a count reconciles with a bare `owed`; the fields before it stay.
            # A row with no state counts as open, the bare view's rule (never hidden; pxx INFO).
            open_count = _owed_bucket(answer, (OWED_BARE_STATE, None))["count"]
            active_count = _owed_bucket(answer, (OWED_ACTIVE_STATE,))["count"]
            _emit({"reply_owed_by_type": _owed_type_counts(reply_owed) if reply_in_scope else None,
                   "answered_still_open_by_type": _owed_type_counts(answered_still_open) if answered_in_scope else None,
                   "reply_owed": reply_owed["count"] if reply_in_scope else None,
                   "answered_still_open": answered_still_open["count"] if answered_in_scope else None,
                   "open": open_count if OWED_BARE_STATE in requested_states else None,
                   "replied_in_thread_not_linked": active_count if OWED_ACTIVE_STATE in requested_states else None,
                   "truncated": bool(reply_owed["truncated"] or answered_still_open["truncated"]),
                   "filters": answer.get("filters") if isinstance(answer, dict) else None})
        elif args.state == OWED_DEFAULT_STATE:
            _emit({**answer, "buckets": {"reply_owed": reply_owed,
                                           "answered_still_open": answered_still_open}})
        else:
            _emit(answer)
        return 0
    if args.cmd == "unread":
        _emit(node.unread(args.state_file, state, days=args.days, limit=args.limit,
                          since=None if args.since is None else _owed_since(args.since)))
        return 0
    if args.cmd == "fleet-unread":
        _emit(node.fleet_unread(args.state_file, state, peer=args.peer, days=args.days,
                                limit=args.limit,
                                since=None if args.since is None else _owed_since(args.since)))
        return 0
    if args.cmd == "fleet":
        from tools.meshy_node import fleet as fleet_mod
        try:
            seats = fleet_mod.parse_seats(args.seats)
        except ValueError as exc:
            raise node.Refused(str(exc)) from None
        fetch = ((lambda labels: node.recipients(args.state_file, state, labels, work=True)) if args.work else
                 (lambda labels: node.recipients(args.state_file, state, labels)))
        if args.idle_over is not None and (not args.work or args.idle_over < 1):
            raise node.Refused("--idle-over needs --work and a positive number of minutes")
        rows, as_of = fleet_mod.collect(seats, fetch, work=args.work)
        if args.steer_by and not args.steerable:
            raise node.Refused("--steer-by needs --steerable")
        if args.steerable:
            from tools.meshy_node import conformance
            by = set(fleet_mod.parse_seats(args.steer_by)) if args.steer_by else None
            when = datetime.now(timezone.utc)
            rows = fleet_mod.add_steerable(rows, lambda seat: conformance.steerable_for(
                seat, lambda t: conformance.whole_thread(lambda **kw: node.thread(args.state_file, state, **kw), t),
                lambda k: node.read(args.state_file, state, key=k), now=when, by=by))
        if args.idle_over is not None:
            alerts, unknown = fleet_mod.idle_over(rows, args.idle_over)
            delayed = [r for r in rows if type(r.get("unsurfaced")) is int and r["unsurfaced"] > 0]
            held = [r for r in rows if type(r.get("held")) is int and r["held"] > 0]
            incomplete = [r for r in rows if r.get("unsurfaced") == -1 or r.get("held") == -1]
            alerts = [r for r in alerts if r not in delayed and r not in held]
            if args.json:
                _emit({"rows": alerts, "unknown": unknown, "unsurfaced": delayed,
                       "held": held, "incomplete": incomplete, "as_of": as_of})
            else:
                print(fleet_mod.render(alerts, as_of, work=True))
                print("Unknown turn reports (listed apart; never treated as idle):")
                print(fleet_mod.render(unknown, as_of, work=True))
                print("Delayed or missing notices (listed apart from idle seats):")
                print(fleet_mod.render(delayed, as_of, work=True))
                print("Held notices on open turns (not called lost or idle):")
                print(fleet_mod.render(held, as_of, work=True))
                print("Incomplete notice observations (never counted as zero):")
                print(fleet_mod.render(incomplete, as_of, work=True))
            return 3 if alerts or delayed or held else 0
        if args.json:
            _emit({"rows": rows, "as_of": as_of})
        else:
            print(fleet_mod.render(rows, as_of, work=args.work, steer=args.steerable))
        return 0
    if args.cmd == "conformance":
        from tools.meshy_node import conformance
        if args.timeout < 60:
            raise node.Refused("--timeout must be at least 60 seconds: a seat answers a probe in minutes")
        argv = list(args.sender)

        def send(**kw):
            return conformance.send_via(argv, **kw)

        try:
            out = conformance.run(args.seat, coordinator=args.coordinator, send=send,
                                  read_thread=lambda t: conformance.whole_thread(
                                      lambda **kw: node.thread(args.state_file, state, **kw), t),
                                  read_msg=lambda k: node.read(args.state_file, state, key=k),
                                  canary_path=args.canary_path, timeout_s=args.timeout, record=not args.no_record)
        except (ValueError, conformance.SendFailed, conformance.ThreadTruncated) as exc:
            raise node.Refused(str(exc)) from None
        _emit(out)
        return 0 if out["result"] == "yes" else 3
    if args.cmd == "presence":
        _emit(node.presence(args.state_file, state, state_value=args.state, note=args.note,
                            ttl_secs=args.ttl))
        return 0
    if args.cmd == "turn":
        from . import turn
        if (args.unsurfaced_oldest_at or args.unsurfaced_since) and args.unsurfaced is None:
            raise node.Refused("--unsurfaced-oldest-at and --unsurfaced-since need --unsurfaced")
        observation = ({"unsurfaced": args.unsurfaced, "unsurfaced_oldest_at": args.unsurfaced_oldest_at,
                        "unsurfaced_since": args.unsurfaced_since}
                       if args.unsurfaced is not None else None)
        usage = None
        if args.usage_file:
            try:
                with open(args.usage_file, encoding="utf-8") as fh:
                    usage = json.load(fh)
            except (OSError, ValueError) as exc:
                raise node.Refused("--usage-file: not a readable JSON file (%s)" % type(exc).__name__) from None
        _emit(turn.publish(args.state_file, state, state_value=args.state, item_at=args.item_at,
                           observation=observation, usage=usage))
        return 0
    if args.cmd == "thread":
        kw = {k: v for k, v in (("order", args.order), ("provenance", args.provenance)) if v}
        _emit(node.thread(args.state_file, state, thread_id=args.id, limit=args.limit, after=args.after,
                          envelope=args.envelope, **kw))
        return 0
    if args.cmd == "round":
        return _round(args, state)
    if args.cmd == "handoff-preflight":
        return _handoff_preflight(args, state)
    if args.cmd == "mentions":
        _emit(node.mentions(args.state_file, state, ref=args.ref, limit=args.limit,
                            by="peer" if args.by_peer else None))
        return 0
    if args.cmd == "awaiting":
        _emit(node.awaiting(args.state_file, state, states=args.state,
                            since=None if args.since is None else _owed_since(args.since)))
        return 0
    if args.cmd == "close-thread":
        return _close_thread(args, state)
    if args.cmd == "brief":
        try:
            return _brief(args, state)
        except OSError as exc:
            if not args.json:
                raise          # the text form reports it as every command does, through main's handler
            # --json promises one JSON object on stdout, so an unreachable server is one too (codex on #99)
            _brief_say(args, f"brief failed: unreachable ({type(exc).__name__}: {exc})")
            return 1
    if args.cmd == "card":
        try:
            return _card(args, state)
        except node.ReadFailed as exc:
            _emit(exc.error)
            return 1
    if args.cmd == "directive-done":
        _emit(node.record_directive_outcome(args.state_file, args.key, args.outcome, args.note))
        return 0
    if args.cmd == "directive-check":
        try:
            msg_doc = node.read(args.state_file, state, key=args.key, directive_claim=True,
                                directive_resume=bool(args.resume))
        except node.ReadFailed as exc:
            _emit(exc.error)
            return 1
        _emit({k: msg_doc.get(k) for k in ("key", "from", "signature_verified", "signed_by", "seat_directive",
                                           "seat_directive_refused") if k in msg_doc})
        return 0 if "seat_directive" in msg_doc else 1
    if args.cmd == "read":
        try:
            msg_doc = node.read(args.state_file, state, key=args.key, version=args.version,
                                **({"namespace": args.namespace} if args.namespace else {}))
        except node.ReadFailed as exc:
            _emit(exc.error)
            return 1
        # `is not False` rejects explicit signature failure while allowing offline/mock dicts that omit the flag
        if isinstance(msg_doc, dict) and msg_doc.get("signature_verified") is not False:
            msg_sender = str(msg_doc.get("author") or msg_doc.get("from") or "").strip()
            own_label = str(state.get("label") or "").strip()
            if msg_sender and msg_sender.casefold() != own_label.casefold():
                sendguard.record_inbound(args.state_file, sender=msg_sender, key=str(msg_doc.get("key") or args.key))
        if getattr(args, "with_context", False) and isinstance(msg_doc, dict):
            msg_doc = dict(msg_doc)
            msg_doc["conversation_context"] = converse.build_conversation_context(
                args.state_file,
                state,
                key=args.key,
                primary=msg_doc,
                mode="read",
                namespace=args.namespace,
            )
        _emit(msg_doc)
        return 0
    if args.cmd == "search":
        _emit(node.search(args.state_file, state, q=args.q, k=args.k, rerank=args.rerank))
        return 0
    if args.cmd == "transcript-search":
        tx_res = transcript_graph.search_transcripts(
            args.state_file, state, q=args.q, k=args.k, rerank=args.rerank,
            ns=args.namespace, seat=args.seat, src=args.src, since=args.since,
            until=args.until, collapse=args.collapse)
        if args.as_json:
            _emit(tx_res)
        else:
            _write_text(transcript_graph.format_transcript_search(tx_res) + "\n")
        return 0
    if args.cmd == "transcript-usage":
        _emit(node.transcript_usage(args.state_file, state, args.days))
        return 0
    if args.cmd == "transcript-read":
        try:
            tx_items = transcript_graph.read_transcript_items(
                args.state_file, state, key=args.key, seat=args.seat, src=args.src,
                gen=args.gen, batch=args.batch, first_ord=args.first_ord,
                last_ord=args.last_ord, ns=args.namespace,
                require_signature=args.require_signature)
        except node.ReadFailed as exc:
            _emit(exc.error)
            return 1
        if args.as_json:
            _emit(tx_items)
        else:
            _write_text(transcript_graph.format_transcript_read(tx_items) + "\n")
        return 0
    if args.cmd == "transcript-consensus":
        upload_modes: dict[str, str] = {}
        for item in args.upload_mode or ():
            if "=" not in item:
                raise node.Refused("--upload-mode must be <seat>=continuous or <seat>=one_off")
            k_seat, v_mode = item.split("=", 1)
            upload_modes[k_seat] = v_mode
        cons = transcript_graph.fleet_transcript_consensus(
            args.state_file, state, card_ns=args.card_namespace,
            transcript_ns=args.transcript_namespace, query=args.q,
            seat_filter=args.seat, stale_context_hours=args.stale_hours,
            stale_ingest_hours=args.stale_ingest_hours,
            stale_transcript_hours=args.stale_transcript_hours,
            upload_modes=upload_modes or None)
        if args.as_json:
            _emit(cons)
        else:
            _write_text(transcript_graph.format_consensus_report(cons) + "\n")
        return 0
    if args.cmd == "context":
        ctx_doc = node.context(args.state_file, state, key=args.key, k=args.k,
                               rerank=args.rerank)
        if getattr(args, "with_context", False) and isinstance(ctx_doc, dict):
            ctx_doc = dict(ctx_doc)
            ctx_doc["conversation_context"] = converse.build_conversation_context(
                args.state_file,
                state,
                key=args.key,
                primary=ctx_doc,
                mode="context",
            )
        _emit(ctx_doc)
        return 0
    if args.cmd == "summarize":
        if args.backlog and args.before is not None:
            raise node.Refused("--before pages a thread; a backlog pages with --since")
        if args.thread and (args.since is not None or args.since_cursor_file is not None):
            raise node.Refused("--since and --since-cursor-file are for --backlog")
        body = node.summarize(args.state_file, state, thread=args.thread,
                              before=args.before, backlog=args.backlog, since=args.since,
                              since_cursor_file=args.since_cursor_file,
                              max_messages=args.max_messages,
                              deterministic=args.deterministic)
        if args.text:
            sys.stdout.write(node.summary_text(body))
            sys.stdout.flush()
        else:
            _emit(body)
        return 0
    if args.cmd == "neighbours":
        _emit(node.neighbours(args.state_file, state, key=args.key,
                              include_inferred=args.include_inferred, limit=args.limit))
        return 0
    if args.cmd == "rollcall":
        if args.timeout <= 0 or args.interval <= 0:
            raise node.Refused("--timeout and --interval must be positive")
        expect = {}
        for pair in args.expect or []:
            label, sep, peer = pair.partition("=")
            if not sep or not label.strip() or not peer.strip():
                raise node.Refused(f"--expect takes LABEL=PEER_ID, got {pair!r}")
            expect[label.strip()] = peer.strip()
        unknown = sorted(set(k.casefold() for k in expect) - set(l.casefold() for l in _labels(args.to)))
        if unknown:
            raise node.Refused(f"--expect names a label that is not in --to: {unknown}")
        result = node.rollcall(args.state_file, state, to=_labels(args.to), thread_id=args.thread,
                               timeout=args.timeout, interval=args.interval,
                               author_family=args.author_family, sender=args.sender,
                               expect=expect)
        _emit(result)
        return 1 if result["silent"] or result["error"] else 0
    if args.cmd == "hints":
        if args.timeout <= 0 or not (args.timeout == args.timeout and args.timeout != float("inf")):
            raise node.Refused("--timeout must be a finite positive number")
        try:
            text = args.body if args.body is not None else args.body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise node.Refused(f"--body-file {args.body_file}: {exc}") from exc
        ans = hints.query_send_hints(
            (args.state_file, state),
            to=_labels(args.to) or (),
            cc=_labels(args.cc),
            thread_id=args.thread,
            in_reply_to=args.in_reply_to,
            body=text,
            k=args.k,
            timeout_s=args.timeout,
            closes=args.closes,
            sender_label=(args.sender or node.default_sender(state)),
            msg_type=args.msg_type,
            ref=args.ref,
        )
        if args.text:
            notice = hints.format_send_hints_notice(ans, in_reply_to=args.in_reply_to)
            if notice:
                sys.stdout.write(notice + "\n")
                sys.stdout.flush()
        else:
            _emit(ans)
        return 0
    if args.cmd == "listen":
        try:
            seen = node.listen(args.state_file, state, count=args.count,
                               duration=args.duration, out=sys.stdout, err=sys.stderr,
                               read=args.read)
        except node.ListenRefused as exc:
            sys.stderr.write(json.dumps({
                "listen": "refused", "code": exc.code,
                **({} if exc.http_status is None else {"http_status": exc.http_status}),
                "mesh_message_frames": exc.seen, "detail": str(exc)}) + "\n")
            return 1
        sys.stderr.write(json.dumps({"listen": "done", "mesh_message_frames": seen}) + "\n")
        return 0
    raise node.Refused(f"unknown command {args.cmd!r}")


def _value_options(parser: argparse.ArgumentParser) -> set[str]:
    """Every option string, across the subcommands, that takes exactly one value."""
    out: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                out |= _value_options(sub)
        elif action.option_strings and action.nargs is None and isinstance(
                action, (argparse._StoreAction, argparse._AppendAction)):
            out.update(action.option_strings)
    return out


def _bind_values(parser: argparse.ArgumentParser, argv: list[str]) -> list[str]:
    """Fold `--opt VALUE` into `--opt=VALUE` for every single-value option, so a value that starts
    with '-' is still that option's value. argparse otherwise reads a space-free token such as the
    pairing code "-rOgmD0WHMb4u8JvgMBAvd0o6XRHdlvH" as an option and reports the code missing; a
    base64url code starts with '-' one time in 64. An option with no following token is left alone,
    so argparse still reports it."""
    opts = _value_options(parser)
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok in opts and i + 1 < len(argv):
            out.append(f"{tok}={argv[i + 1]}")
            i += 2
        else:
            out.append(tok)
            i += 1
    return out


_CLOSE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _close_thread(args: argparse.Namespace, state: dict) -> int:
    """Settle answered asks authored by this seat after verifying read-ledger custody of each latest reply."""
    if args.to and args.notify:
        raise node.Refused("--to and --notify cannot be combined")
    explicit_to: list[str] | None = None
    if args.to is not None:
        explicit_to = _labels(args.to) or []
        if not explicit_to:
            raise node.Refused("--to must name at least one recipient label")
    for k in args.key or ():
        if not _is_mesh_key(k):
            raise node.Refused("--key values must be in-band epoch-ms msg/ keys")
    sendguard.refuse_untrusted_transcript_paste(args.body, refused=node.Refused)
    brief_ans = node.brief(args.state_file, state, since=_owed_since(args.since), limit=100)
    tc_section = brief_ans.get("to_close") if isinstance(brief_ans, dict) else None
    if not isinstance(tc_section, dict) or not isinstance(tc_section.get("items"), list):
        raise node.Refused("server brief did not include a to_close section")
    items = [r for r in tc_section["items"] if isinstance(r, dict)]
    truncated = tc_section.get("truncated") is not False
    if truncated and not args.force_blind_close:
        scope = (f" ({sum(1 for r in items if r.get('thread_id') == args.thread)} in thread {args.thread!r})"
                 if args.thread else "")
        raise node.Refused(
            f"to_close is truncated after {len(items)} row(s){scope}; unsettled asks or co-recipient replies "
            "remain beyond brief's 100-row cap, so complete reply custody cannot be verified. "
            "Narrow --since, settle individual asks with `send --closes` after reading all replies, "
            "or pass --force-blind-close"
        )
    if truncated and args.notify and explicit_to is None:
        raise node.Refused("to_close is truncated, so --notify cannot resolve every owing recipient; "
                           "pass --to explicitly or narrow --since")
    if args.thread:
        matched = [r for r in items if r.get("thread_id") == args.thread]
        if not matched:
            if truncated:
                raise node.Refused(
                    f"to_close is truncated after {len(items)} row(s) with 0 row(s) for thread {args.thread!r} "
                    "on page 1; narrow --since"
                )
            _emit({"sent": False, "closed": [], "thread_id": args.thread,
                   "note": "no settled answered asks to close in this thread"})
            return 0
    else:
        wanted = list(dict.fromkeys(args.key or ()))
        by_key: dict[str, list[dict]] = {}
        for r in items:
            rk = r.get("key")
            if isinstance(rk, str):
                by_key.setdefault(rk, []).append(r)
        missing = [k for k in wanted if k not in by_key]
        if missing:
            raise node.Refused(
                f"ask(s) {', '.join(missing)} are not in settled to_close "
                "(unanswered, within settle window, already closed/declined, or outside --since)"
            )
        matched = [r for k in wanted for r in by_key[k]]

    if not args.force_blind_close:
        ledger_by_key: dict[str, set[int]] = {}
        for rec in node.ledger_records(args.state_file, state):
            ledger_by_key.setdefault(rec["key"], set()).add(rec["version"])
        for row in matched:
            rk = row.get("latest_reply_key")
            rv = row.get("latest_reply_version")
            if not isinstance(rk, str) or type(rv) is not int or rv < 1 or rv not in ledger_by_key.get(rk, ()):
                raise node.Refused(
                    f"cannot close {row.get('key')} before reading latest reply {rk} (v{rv}); "
                    f"run `read --key {rk}` first or pass --force-blind-close"
                )

    if args.sender:
        self_label = args.sender
    else:
        candidates = [brief_ans.get("label"), state.get("label"), node.default_sender(state)]
        self_label = next(
            (c for c in candidates if isinstance(c, str) and _CLOSE_LABEL_RE.fullmatch(c)),
            f"peer-{str(state['peer_id'])[:8].lower()}",
        )

    groups: dict[str, list[dict]] = {}
    for row in matched:
        tid = row.get("thread_id") or args.thread
        if not isinstance(tid, str) or not tid.strip():
            raise node.Refused(f"ask {row.get('key')} has no thread_id; pass --thread explicitly")
        groups.setdefault(tid, []).append(row)

    dry_run = bool(args.dry_run)
    plan_batches: list[dict] = []
    for tid, group_rows in groups.items():
        if explicit_to is not None:
            recs = explicit_to
        elif args.notify:
            recs = list(dict.fromkeys(r["owing_label"] for r in group_rows
                                      if isinstance(r.get("owing_label"), str) and r["owing_label"]))
            if not recs:
                raise node.Refused(f"no owing recipient label resolved for thread {tid!r}; pass --to explicitly")
        else:
            recs = [self_label]
        distinct_keys = list(dict.fromkeys(r["key"] for r in group_rows if isinstance(r.get("key"), str)))
        for idx in range(0, len(distinct_keys), 16):
            batch = distinct_keys[idx:idx + 16]
            verified = sorted({r["latest_reply_key"] for r in group_rows
                               if r.get("key") in batch and isinstance(r.get("latest_reply_key"), str)})
            plan_batches.append({"thread_id": tid, "to": recs, "closes": batch, "verified_replies": verified})

    extra_meta = {"to_close_truncated": True} if truncated else {}
    if dry_run:
        _emit({"dry_run": True, "sent": False, "batches": plan_batches, **extra_meta})
        return 0

    closed_keys: list[str] = []
    messages: list[dict] = []
    all_ok = True
    for batch_info in plan_batches:
        recs = batch_info["to"]
        tid = batch_info["thread_id"]
        batch = batch_info["closes"]
        note_body, subject = sendguard.admit(args.state_file, to=recs, cc=[], body=args.body, subject=None,
                                             bulk_ack=args.bulk_ack, refused=node.Refused)
        res = node.send(args.state_file, state, to=recs, cc=[], thread_id=tid, body=note_body, msg_type="note",
                        author_family=args.author_family, sender=args.sender or self_label,
                        subject=subject, closes=batch)
        ok = node.succeeded(res)
        if ok:
            missed = sendguard.record(args.state_file, to=recs, cc=[],
                                      key=str(res.get("key") or "") if isinstance(res, dict) else "",
                                      body=note_body, body_bytes=len(note_body.encode("utf-8")))
            if missed:
                _stderr_line({"warning": "send_not_recorded", "reason": missed})
            recipientwarn.invalidate(args.state_file)
            closed_keys.extend(batch)
        else:
            all_ok = False
        messages.append(res)
    _emit({"sent": all_ok, "closed": closed_keys, "messages": messages, **extra_meta})
    return 0 if all_ok else 1


def _mutating_command(args: argparse.Namespace) -> bool:
    if args.cmd == "card":
        return args.card_cmd not in {"list", "usage", "search", "recent", "context"}
    if args.cmd == "send-status" and getattr(args, "peek", False):
        return False                                       # --peek reads the local journal and nothing else
    if args.cmd == "close-thread" and getattr(args, "dry_run", False):
        return False                                       # --dry-run reads brief and the local ledger only
    # Unknown/new commands default to the conservative write contract.
    # send-status can reconcile by presenting an unresolved memory.write again,
    # so it and send-drain retain the write contract despite the status name.
    return args.cmd not in {
        "inbox", "owed", "unread", "fleet-unread", "fleet", "thread", "mentions", "awaiting",
        "brief", "read", "directive-check", "search", "context", "summarize", "neighbours", "lookup-help",
        "verify-bundle", "gate-check", "orient", "transcript-search",
        "transcript-read", "transcript-consensus", "transcript-usage", "resume-brief", "hints",
    }


def _with_attempted_key(exc, record: dict) -> dict:
    """An unknown-outcome send names the key it tried, and how to settle it (kxxx): read it back; present
    means it landed, absent (E_NOT_FOUND) means it did not and a resend is safe. Only on "outcome": "unknown", so
    a send the server refused (it wrote nothing) says nothing new."""
    key = getattr(exc, "attempted_key", None)
    if record.get("outcome") != "unknown" or not isinstance(key, str):
        return record
    return {**record, "attempted_key": key,
            "settle": "read --key %s before any resend: present = it landed, do not resend" % key}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    # Keep the established `send` spelling while giving its status form no send-only required flags.
    if len(raw) >= 2 and raw[0] == "send" and raw[1] == "status":
        raw = ["send-status", *raw[2:]]
    elif len(raw) >= 2 and raw[0] == "send" and raw[1] == "drain":
        raw = ["send-drain", *raw[2:]]
    args = parser.parse_args(_bind_values(parser, raw))
    mutating = _mutating_command(args)
    with errors.track_accepted_calls() as tracked:
        try:
            return run(args)
        except (node.Refused, wire.NotV1) as exc:
            sys.stderr.write(json.dumps({"refused": str(exc), "code": "E_CLIENT_REFUSED",
                                         "retryable": False}) + "\n")
            return 2
        except node.ServerSaid as exc:
            _emit(_with_attempted_key(exc, {"http_status": exc.status, "body": exc.body,
                                            **errors.http_metadata(exc.status, mutating=mutating, body=exc.body,
                                                                   accepted_call=tracked["accepted"])}))
            return 1
        except OSError as exc:
            sys.stderr.write(json.dumps(_with_attempted_key(exc, {
                "unreachable": f"{type(exc).__name__}: {exc}",
                **errors.io_metadata(exc, mutating=mutating, accepted_call=tracked["accepted"])})) + "\n")
            return 2
