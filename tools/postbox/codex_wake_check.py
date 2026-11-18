"""Did a Codex seat take the mail the postbox delivered to it? A read-only check, beside the postbox (wake design v1,
oxxxxxx's a26d51, Sun 8 Nov 2026: the HZ admin seat was handed 94438f at (time withheld) and took no turn for 5 hours,
while every status said "ok").

WHY. The postbox's Codex delivery is `codex queue --thread <target> --message <notice>`, and rc 0 counts as delivered
(postbox.run_codex_delivery). Queueing stores the message for the thread and starts nothing. A turn follows only when
a live Codex process runs that very thread, is idle, and is not waiting on a person. Nothing checked that it did.

WHAT IS READ, per Codex identity of the postbox config (the ones with a `target`), and nothing else:
  * the queue `codex queue` writes, `<CODEX_HOME>/queue_1.sqlite` (table queued_items), opened read-only. A session
    that takes a message deletes its row, so a row for the target older than WAIT_S is a message nobody took, the
    most direct signal there is; rows of other threads are counted as debris (HQD held 112 of them on 8 Nov, the
    notices of five threads the seat had left). CODEX_HOME is the identity's `codex_home`, else the parent of its
    `codex_sessions_dir`, else ~/.codex: a wrapper that pins another home must be named in `codex_home`;
  * the dispatch log the delivery bank writes, `<log>.codex.offset.dispatch.jsonl`: one line per hand-off, each
    event with its key, role (to or cc) and requires_ack, only the lines whose consumer is this target. A postbox
    older than the bank writes none (the HZ builder's, a reviewer 64ccb5): then the spool's rows below
    `<log>.codex.offset` stand in, each handed over at its rx_at plus the pacing (settle and gap) at most;
  * the target thread's rollout under the identity's `codex_sessions_dir` (default ~/.codex/sessions), found as the
    postbox finds it, and the other threads' rollouts there, newest first, with the cwd their session_meta names;
  * the postbox's own status.json (beside its config.json, or --status). `run_codex_delivery` is "crashed" while the
    delivery loop crash-loops (postbox.guarded); `codex` is "retrying" while `codex queue` fails and "hold" while a
    helper holds delivery; and `last_poll_ok_at` is the last successful inbox poll, made every poll_s (5 min). A dead
    daemon leaves the file frozen, so the poll's AGE is the signal, never a value (a reviewer b08032, pxx);
  * the delivery bank's checkpoint, `<log>.codex.offset.bank.json`: its mode (POSIX), and the schema_version,
    consumer and identity the bank checks before it loads it.
Never a seat's state file, never the server, never a process. Nothing is written.

THREE STATES of a hand-off the seat MUST TAKE (every event addressed "to" it, and a cc with requires_ack true; a
cc without one may be held by design and never alarms):
  queued     the dispatch line exists (what the postbox records today);
  taken      a user message in the target's rollout, at or after (dispatch - GRACE_S), names one of its keys;
  answered   a task_complete, or an assistant message, follows that user message (a turn_aborted does not answer).
Correlated on the message, never on "the newest turn": an unrelated turn clears nothing (codex-oxxxxxx-cli's builder
evidence, 48249d). GRACE_S because the postbox logs the hand-off after `codex queue` returns, and the turn can start
first (the builder measured task_started 8 ms before codex_at; pxx's health check uses 30 s).

VERDICTS, worst first, for the oldest hand-off within LOOKBACK_S that is not answered:
  MODE_DRIFT          the delivery bank's checkpoint `<log>.codex.offset.bank.json` is a file the bank's own read
                      refuses (delivery_bank.open_regular_read): a link or reparse point, not a regular file, or on
                      POSIX another account's or a mode other than 0600. Every delivery pass then fails. Said before
                      any mail waits (oxxxxxx, 8 Nov: a retarget left it 0644; pxx, 4999b1).
  DELIVERY_DOWN       the delivery loop cannot run. Said before any mail waits, when one of these holds:
                      - the bank would refuse its checkpoint: a schema_version, consumer or identity that is not this
                        target's (delivery_bank.Bank). A retarget trips it until the checkpoint is rewritten;
                      - the last successful poll is older than POLL_MISSED polls: the daemon is dead, or it cannot
                        reach the server;
                      - status.json says run_codex_delivery crashed. This one carries `confirm_runs: 2`: a single
                        crash the loop recovered from reads the same for a minute, so the alert caller waits for a
                        second run in a row (pxx). MODE_DRIFT and a checkpoint refusal stay instant.
  UNKNOWN             0 or 2+ rollouts for the target, or no readable hand-off record and no queue verdict.
  DELIVERY_STALLED    the POSTBOX never handed it over: a must-take spool row older than WAIT_S plus the pacing,
                      with no dispatch line naming its key (or still past the codex offset), not skipped as already
                      read and not handled, while no turn holds it back. The delivery loop is down (F7: on 8 Nov the HZ
                      admin's crashed every 60 s for 12 h on a bank file a retarget left at mode 0644). QUICK_S stands
                      in for WAIT_S while status.json says `codex queue` keeps failing (retrying) or a helper holds
                      delivery (hold).
  STALE_TARGET        not taken for WAIT_S, and a session of ANOTHER thread, newer than the target's last record,
                      runs in the identity's `seat_dir`: the seat restarted into a new session (it is named; nothing
                      is repointed). Without a seat_dir the newer thread is only named on a STUCK, as
                      `newer_other_thread`: two seats can share one CODEX_HOME (HQB and HQC do).
  PARKED              not taken for WAIT_S, the target's last turn event is a task_started, the rollout has been
                      silent for WAIT_S, and its last record is a request for a person (PARKED_EVENTS, an assumption:
                      the names are not measured here, where the seat runs with approvals bypassed).
  RATE_LIMITED        not taken for WAIT_S, and the latest token_count says a limit is reached with its reset still
                      ahead, or the last turn ended on usage_limit_exceeded or rate_limit_exceeded (pxx's 1 Nov check).
  BUSY                not taken yet, and a turn is running with the rollout written within WAIT_S.
  STUCK               not taken for WAIT_S, and none of the above explains it.
  TAKEN_NOT_ANSWERED  taken, then neither a task_complete nor an assistant message for WAIT_S, with no turn running.
  OK                  everything expected was answered, or is still inside WAIT_S.

Exit status: 0 for OK, BUSY or RATE_LIMITED; 2 for PARKED, STUCK, TAKEN_NOT_ANSWERED, STALE_TARGET,
DELIVERY_STALLED, DELIVERY_DOWN or MODE_DRIFT; 3 for UNKNOWN.
One JSON line per identity on stdout: routing fields only (seat, verdict, times, keys, thread ids; never the status
file's error text). The alert itself is the caller's (an effect, separate from this observation).

Usage: python -I codex_wake_check.py --config <postbox config.json> [--status <its status.json>] [--identity NAME ...]
       [--now EPOCH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path

GRACE_S = 30
WAIT_S = 1800
LOOKBACK_S = 24 * 3600
#: The postbox's own Codex pacing defaults (postbox.CODEX_SETTLE_S, CODEX_MIN_GAP_S): the most a spooled row waits
#: before its hand-off, when there is no dispatch log to say when it went.
SETTLE_S, MIN_GAP_S = 30, 300


def _pacing(ident, field, default):
    v = ident.get(field, default)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 3600 else default
#: The most of a rollout read: a seat's thread can reach hundreds of MB, and only its recent part matters here.
MAX_ROLLOUT_BYTES = 64 * 1024 * 1024
MAX_LINE = 1024 * 1024
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
ROLLOUT_ID_RE = re.compile(r"^rollout-.+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
#: Event types that ask a person mid-turn. ASSUMED names (see the module docstring); a record carrying none of them
#: leaves a silent running turn STUCK, never OK.
PARKED_EVENTS = frozenset({"exec_approval_request", "apply_patch_approval_request", "request_user_input",
                           "elicitation_request"})
EXIT = {"OK": 0, "BUSY": 0, "RATE_LIMITED": 0, "PARKED": 2, "STUCK": 2, "TAKEN_NOT_ANSWERED": 2,
        "STALE_TARGET": 2, "DELIVERY_STALLED": 2, "DELIVERY_DOWN": 2, "MODE_DRIFT": 2, "UNKNOWN": 3}
#: The postbox polls the server inbox every poll_s (postbox.POLL_S) and stamps last_poll_ok_at on success; this many
#: missed polls is a postbox not polling. A dead daemon leaves the stamp frozen (postbox.run_poll's docstring).
POLL_S, POLL_MISSED = 300, 3
#: While status.json says delivery keeps failing ("retrying") or is held by a helper ("hold"), a must-take row is
#: stalled after QUICK_S plus the pacing instead of WAIT_S. The loop retries every 60 s, so a single failure clears
#: well inside it.
QUICK_S = 300
FAILING = ("retrying", "hold")
#: The most of a status file or bank checkpoint read.
MAX_STATE_BYTES = 32 * 1024 * 1024


def _epoch(text):
    try:
        return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def identities(config: dict, names=()):
    """The enabled Codex identities of a postbox config (a list or a {name: identity} map), with a usable target."""
    raw = config.get("identities")
    items = list(raw.values()) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    out = []
    for ident in items:
        if not isinstance(ident, dict) or not ident.get("enabled", True):
            continue
        if names and ident.get("name") not in names:
            continue
        if isinstance(ident.get("target"), str) and TARGET_RE.match(ident["target"]) and isinstance(ident.get("log"), str):
            out.append(ident)
    return out


def _day_dirs(root):
    """Every YYYY/MM/DD folder under the sessions root, newest first."""
    def walk(path, depth):
        try:
            names = sorted((n for n in os.listdir(path) if n.isdigit()), reverse=True)
        except OSError:
            return
        for n in names:
            sub = os.path.join(path, n)
            if depth == 2:
                yield sub
            elif os.path.isdir(sub):
                yield from walk(sub, depth + 1)
    yield from walk(root, 0)


#: Other threads' rollouts looked at for a stale target, newest first.
OTHERS_MAX = 50


def rollouts(ident):
    """(the target's rollouts, the other threads' rollouts as [(mtime, thread id, path)], newest first)."""
    root = os.path.expanduser(ident.get("codex_sessions_dir") or os.path.join("~", ".codex", "sessions"))
    suffix = "-" + ident["target"] + ".jsonl"
    mine, others = [], []
    for day in _day_dirs(root):
        try:
            names = os.listdir(day)
        except OSError:
            continue
        for n in names:
            if not n.startswith("rollout-") or not n.endswith(".jsonl"):
                continue
            path = os.path.join(day, n)
            if n.endswith(suffix):
                mine.append(path)
                continue
            m = ROLLOUT_ID_RE.match(n)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if m:
                others.append((mtime, m.group(1), path))
    others.sort(reverse=True)
    return sorted(mine), others[:OTHERS_MAX]


def session_cwd(path):
    """The cwd the rollout's session_meta (its first line) names, or None."""
    try:
        with open(path, "rb") as f:
            first = f.readline(MAX_LINE)
        d = json.loads(first)
        p = d.get("payload") if isinstance(d, dict) and d.get("type") == "session_meta" else None
        return p.get("cwd") if isinstance(p, dict) and isinstance(p.get("cwd"), str) else None
    except (OSError, ValueError):
        return None


def _same_dir(a, b):
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def stale_candidate(others, seat_dir, after):
    """(the thread id of a session newer than `after` that is probably the seat's own, verified): verified only when
    the identity names its `seat_dir` and the session's cwd is that folder. Two seats can share one CODEX_HOME (HQB
    and HQC do: a reviewer 64ccb5), so without a seat_dir the newest other thread is named but never proves a stale
    target."""
    for mtime, tid, path in others:
        if mtime <= after:
            break
        if not seat_dir:
            return tid, False
        if _same_dir(session_cwd(path) or "", seat_dir):
            return tid, True
    return None


def read_rollout(path):
    """The records that matter, oldest first: ("start"|"end"|"user"|"assistant"|"parked"|"limit", epoch, data). Reads
    at most the last MAX_ROLLOUT_BYTES. Raises OSError for a file that cannot be read."""
    out = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > MAX_ROLLOUT_BYTES:
            f.seek(size - MAX_ROLLOUT_BYTES)
            f.readline()                                    # a partial first line
        for raw in f:
            if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                continue                                    # oversize, or a line still being written
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            at = _epoch(d.get("timestamp"))
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            kind = p.get("type")
            if at is None or not isinstance(kind, str):
                continue
            if d.get("type") == "event_msg":
                if kind == "task_started":
                    out.append(("start", at, None))
                elif kind in ("task_complete", "turn_aborted"):
                    # How the turn ended: "done" or "aborted" (an aborted turn answers nothing: GitHub Codex on #554),
                    # with "-limit" when it ended on a usage or rate limit (pxx's 1 Nov check, codex_error_info): the
                    # seat is limited until a later turn shows otherwise, when token_count carries no rate_limits.
                    limited = any(w in raw for w in (b"usage_limit_exceeded", b"rate_limit_exceeded"))
                    how = "aborted" if kind == "turn_aborted" else "done"
                    out.append(("end", at, how + "-limit" if limited else how))
                elif kind in PARKED_EVENTS:
                    out.append(("parked", at, kind))
                elif kind == "token_count" and isinstance(p.get("rate_limits"), dict):
                    out.append(("limit", at, p["rate_limits"]))
                elif kind == "user_message":
                    out.append(("user", at, raw))
                elif kind == "agent_message":
                    out.append(("assistant", at, None))
            elif d.get("type") == "response_item" and kind == "message":
                role = p.get("role")
                if role == "user":
                    out.append(("user", at, raw))
                elif role == "assistant":
                    out.append(("assistant", at, None))
    return out


def _limited(rate_limits, now):
    """A limit reached with its reset still ahead (a reset that has passed is no limit: pxx's third lesson)."""
    windows = [rate_limits.get(k) for k in ("primary", "secondary")]
    for w in windows:
        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)) and w["used_percent"] >= 100:
            resets = w.get("resets_at")
            if isinstance(resets, (int, float)) and resets > now:
                return True
    return bool(rate_limits.get("rate_limit_reached_type")) and any(
        isinstance(w, dict) and isinstance(w.get("resets_at"), (int, float)) and w["resets_at"] > now for w in windows)


def _must_be_taken(e):
    """Mail the seat must take: every event addressed TO it, ack or not, and a cc that asks for a reply. Only a cc
    without one may be held by design (postbox_os.held_by_default, hold_cc_without_ack) and never alarms. Requiring
    requires_ack on "to" as well left 33 of HQB's 35 dispatches in a day unchecked (a reviewer ca5875, pxx)."""
    if not (isinstance(e, dict) and e.get("key")):
        return False
    return e.get("role") == "to" or (e.get("role") == "cc" and e.get("requires_ack") in (True, "True", "true"))


def _version(d):
    """A line's version as delivery_bank.version reads it: a line without one predates versions and is version 1, and
    anything but a positive int is None, which no receipt covers."""
    v = d.get("version", 1)
    return v if type(v) is int and v > 0 else None


#: The archive slots a bounded log rotates into (postbox_os.append_regular_private, delivery_bank.DISPATCH_MAX_ARCHIVES).
ARCHIVES = 16


def with_archives(path, since):
    """The bounded log at `path`, then its rotated archives (`.1` .. `.16`) last written at or after `since`. A rotation
    moves the newest lines out of the active file, and slots fill lowest-free-first, so a slot's number says nothing
    of its age: its write time does (the dispatch and skips logs rotate at 4 MiB; README "Status and liveness")."""
    out = [path]
    for i in range(1, ARCHIVES + 1):
        try:
            if os.path.getmtime(f"{path}.{i}") >= since:
                out.append(f"{path}.{i}")
        except OSError:
            continue
    return out


def expecting(dispatch_log, since, target):
    """[(taken from, alarm from, [keys])], oldest first, of the hand-offs to `target` since `since` that carry an event
    the seat must take: both times are the dispatch line's. A line of another consumer (an earlier target) is not this
    thread's. Reads the archives a rotation wrote within the window too. Raises OSError (FileNotFoundError when the
    active dispatch log does not exist)."""
    out = []
    for path in with_archives(dispatch_log, since):
        try:
            f = open(path, encoding="utf-8")
        except FileNotFoundError:
            if path == dispatch_log:
                raise
            continue                                        # an archive pruned meanwhile
        with f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                at = _epoch(d.get("at")) if isinstance(d, dict) else None
                if at is None or at < since:
                    continue
                if "consumer" in d and d["consumer"] != "codex:" + target:
                    continue
                keys = [str(e["key"]) for e in (d.get("events") or []) if _must_be_taken(e)]
                if keys:
                    out.append((at, at, keys))
    return sorted(out, key=lambda h: h[1])


def expecting_from_spool(log, since, allowance, passed=None):
    """The fallback for a postbox older than the delivery bank (no dispatch log; the HZ builder's, antigravity
    64ccb5): the spool's must-take rows below <log>.codex.offset were handed over somewhere between their rx_at
    and rx_at plus the pacing `allowance` (settle and gap). [(taken from, alarm from, [key])]: a take counts from
    rx_at, and only the alarm waits for the allowance (GitHub Codex on #554: a notice taken quickly was never
    counted). Reads at most MAX_SPOOL_BYTES below the offset (pxx). A row `passed` names by key and version
    (passed_over: read already at that version or later, or handled at it) was passed over, not handed over: the
    bank moves the offset past it and writes a skip receipt, and no notice (GitHub Codex on #554, second round).
    Raises OSError."""
    with open(log + ".codex.offset", encoding="utf-8") as f:
        offset = int(f.read().strip() or 0)
    out = []
    with open(log, "rb") as f:
        read = max(0, offset - MAX_SPOOL_BYTES)
        f.seek(read)
        if read:
            read += len(f.readline())                       # a partial first line
        for raw in f:
            read += len(raw)
            if read > offset:
                break                                       # not yet handed over
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not (isinstance(d, dict) and d.get("event") == "message" and _must_be_taken(d)):
                continue
            if passed is not None and passed(str(d["key"]), _version(d)):
                continue                                    # passed over by the bank: the seat owes it no take
            rx = _epoch(d.get("rx_at"))
            if rx is not None and rx >= since:
                out.append((rx, rx + allowance, [str(d["key"])]))
    return out


def codex_home(ident):
    """The CODEX_HOME the postbox queues into for this identity: its `codex_home`, else the parent of its
    `codex_sessions_dir`, else ~/.codex. (A wrapper that pins another home must be named in `codex_home`.)"""
    if isinstance(ident.get("codex_home"), str) and ident["codex_home"]:
        return os.path.expanduser(ident["codex_home"])
    if isinstance(ident.get("codex_sessions_dir"), str) and ident["codex_sessions_dir"]:
        return os.path.dirname(os.path.expanduser(ident["codex_sessions_dir"]).rstrip("/\\"))
    return os.path.expanduser(os.path.join("~", ".codex"))


def queue_rows(home, target):
    """(the created times of the target's queued rows, {other thread: rows}) from <home>/queue_1.sqlite, opened
    read-only; None when that home has no queue database. A session that takes a queued message deletes its row, so a
    row that stays is a message nobody took. Raises sqlite3.Error or OSError for a database that cannot be read."""
    path = os.path.join(home, "queue_1.sqlite")
    if not os.path.isfile(path):
        return None
    con = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        mine = [ms / 1000 for (ms,) in con.execute("SELECT created_at_ms FROM queued_items WHERE thread_id = ?",
                                                   (target,)) if isinstance(ms, (int, float))]
        debris = dict(con.execute("SELECT thread_id, count(*) FROM queued_items WHERE thread_id != ? "
                                  "GROUP BY thread_id", (target,)))
    finally:
        con.close()
    return mine, debris


#: The most of the spool read for DELIVERY_STALLED, from its end.
MAX_SPOOL_BYTES = 32 * 1024 * 1024
#: How long the postbox itself holds mail behind a running turn (postbox.CODEX_TURN_STALE_S): within it, mail not yet
#: handed over is the postbox waiting, not stalled.
HOLD_MAX_S = 7200


def spool_rows(log):
    """[(end byte, rx epoch, key, version)] of the spool's must-take message rows, from at most its last
    MAX_SPOOL_BYTES. Raises OSError."""
    out = []
    size = os.path.getsize(log)
    with open(log, "rb") as f:
        pos = max(0, size - MAX_SPOOL_BYTES)
        f.seek(pos)
        if pos:
            pos += len(f.readline())                        # a partial first line
        for raw in f:
            pos += len(raw)
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("event") == "message" and _must_be_taken(d):
                rx = _epoch(d.get("rx_at"))
                if rx is not None:
                    out.append((pos, rx, str(d["key"]), _version(d)))
    return out


def _named_in_jsonl(path):
    """The records a jsonl file names a key in, as dicts: each line with a key of its own (a skip receipt) and each
    event of a line that has events (a dispatch line). Nothing when the file is missing."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                if isinstance(d.get("key"), str):
                    out.append(d)
                events = d.get("events")
                for e in events if isinstance(events, list) else []:
                    if isinstance(e, dict) and isinstance(e.get("key"), str):
                        out.append(e)
    except FileNotFoundError:
        pass
    return out


def handed_tokens(dispatch_log, since):
    """The (event, key, version) of every event a dispatch line handed over, in the log and the archives a rotation
    wrote since `since`: the bank's own token (delivery_bank.token), so a hand-off of version 1 is not one of
    version 2."""
    return {(e.get("event"), e["key"], _version(e)) for p in with_archives(dispatch_log, since)
            for e in _named_in_jsonl(p)}


def passed_over(ident, since):
    """A test, (key, version) -> bool, for the spool rows the bank passes over and never hands to the seat, decided as
    the bank decides it (delivery_bank Bank.collect): the seat read that key at the row's version or later
    (fetched_skip; each skip receipt names the version it covered, in the skips log and the archives a rotation wrote
    since `since`), or its handled receipts hold that exact key and version. By key alone, a receipt for version 1 hid
    an undelivered version 2 (GitHub Codex on #554, third round)."""
    read = {}
    for p in with_archives(ident["log"] + ".codex.offset" + ".skips.jsonl", since):
        for d in _named_in_jsonl(p):
            v = _version(d)
            if v is not None and v > read.get(d["key"], 0):
                read[d["key"]] = v
    handled = _handled_pairs(ident)
    return lambda key, v: v is not None and (read.get(key, 0) >= v or (key, v) in handled)


def _delivery_bank():
    """delivery_bank from this file's own folder, loaded on first use. `python -I` leaves that folder off sys.path, and
    a postbox older than the bank (the HZ builder's) has no delivery_bank.py at all: imported at module level, it
    stopped the checker, and the alert caller with it, before either started (pxx on #555)."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import delivery_bank
    return delivery_bank


def _handled_pairs(ident):
    """The exact (key, version) pairs the bank itself trusts, read by the bank's own reader as Bank.collect reads them
    (delivery_bank.handled with skip_bad): an entry that is not authenticated terminal work suppresses no delivery,
    and so no alarm here either (GitHub Codex on #554, round 4; deferred to this follow-up). A file the bank refuses
    suppresses nothing: the bank's own loop then fails on it, which the status file shows. Nor does anything where no
    bank sits beside the checker: a postbox without one has no receipts the bank trusts."""
    if not ident.get("handled_file"):
        return set()
    try:
        bank = _delivery_bank()
    except ImportError:
        return set()
    try:
        return set(bank.handled(ident, skip_bad=True, report=lambda line: None))
    except (TypeError, OSError, ValueError, AttributeError, KeyError):
        return set()


def never_handed(ident, now, allowance, wait=WAIT_S):
    """The must-take spool rows that arrived more than `wait` plus the pacing allowance ago and were never handed
    over: with a dispatch log, no dispatch line names their key at their version (under any consumer: a row handed
    to an earlier target was handed); without one, they lie past the codex offset. Rows the bank passes over
    (passed_over: read already at that version or later, or handled at it) are left out: it never hands those over.
    The dispatch and skips logs are read with the archives a rotation wrote within the lookback: after a rotation the
    active files no longer hold the lines that show a row was handed over or skipped. [(rx, key)], oldest first.
    Raises OSError for a spool that cannot be read; None when there is no spool at all."""
    log = ident["log"]
    if not os.path.exists(log):
        return None
    rows = [r for r in spool_rows(log) if now - LOOKBACK_S <= r[1] <= now - wait - allowance]
    dispatch = log + ".codex.offset.dispatch.jsonl"
    if os.path.exists(dispatch):
        handed = handed_tokens(dispatch, now - LOOKBACK_S)
        waiting = [r for r in rows if ("message", r[2], r[3]) not in handed]
    else:
        try:
            with open(log + ".codex.offset", encoding="utf-8") as f:
                offset = int(f.read().strip() or 0)
        except (OSError, ValueError):
            return None
        waiting = [r for r in rows if r[0] > offset]
    passed = passed_over(ident, now - LOOKBACK_S)
    return sorted((rx, key) for _, rx, key, v in waiting if not passed(key, v))


def _bank_mode(path):
    """The permission bits of the delivery bank file, or None where they mean nothing (Windows) or it is missing."""
    if os.name == "nt":
        return None
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _owner_uid():
    """This account's uid, which must own the checkpoint; None where there is none to compare (Windows)."""
    return os.getuid() if hasattr(os, "getuid") else None


def bank_file_problem(path):
    """(why the bank's own read would refuse the checkpoint FILE, its mode) or (None, None), mirroring
    delivery_bank.open_regular_read(require_private=True): a link or reparse point, or not a regular file; on POSIX
    another account's file, or a mode other than 0600. lstat, never stat: a link to a good checkpoint is still refused
    (pxx). A missing checkpoint is a fresh bank."""
    try:
        info = os.lstat(path)
    except OSError:
        return None, None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
        return "is a link", None
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file", None
    uid = _owner_uid()
    if uid is not None and info.st_uid != uid:
        return "is owned by another account", None
    mode = _bank_mode(path)
    if mode is not None and mode != 0o600:
        return f"has mode {oct(mode)}", mode
    return None, None


def read_json(path):
    """(a JSON object, None) from a file the postbox replaces whole, else (None, why): "absent", "unreadable: ..." when
    this check could not read it, "invalid: ..." when what it read is no JSON object (the postbox could not load it
    either)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, f"unreadable: {type(exc).__name__}"
    if len(raw) > MAX_STATE_BYTES:
        return None, "unreadable: too large"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "invalid: not JSON"
    return (data, None) if isinstance(data, dict) else (None, "invalid: not an object")


def bank_refusal(ident):
    """(why the delivery bank would refuse its checkpoint, a read problem): the check Bank.__init__ makes before it
    loads one (delivery_bank.py, 'delivery checkpoint identity/consumer mismatch'): schema_version 1 or 2, consumer
    "codex:" + target, identity the identity's name. The postbox derives the consumer from the target (postbox.py
    run_codex_delivery), so a retarget trips it until the checkpoint is rewritten; on 8 Nov the hand edit that did
    that left the file at 0644 (MODE_DRIFT). A missing checkpoint is a fresh bank: no refusal."""
    data, problem = read_json(ident["log"] + ".codex.offset.bank.json")
    if problem is not None and problem.startswith("invalid"):
        return f"bank checkpoint {problem}", None           # the bank's own read raises on it, every pass
    if data is None:
        return None, (None if problem == "absent" else problem)
    if data.get("schema_version") not in (1, 2):
        return "bank checkpoint schema_version is not 1 or 2", None
    if data.get("consumer") != "codex:" + ident["target"]:
        return "bank checkpoint consumer is not this target's", None
    if data.get("identity") != ident.get("name"):
        return "bank checkpoint identity is not this identity", None
    return None, None


def loop_down(status, ident, now):
    """(why the postbox's own status entry says delivery cannot run, how many runs in a row must see it), or
    (None, 0). No successful inbox poll for POLL_MISSED polls is down at once. run_codex_delivery "crashed" needs two
    runs: postbox.guarded sets it on every crash and "restarted" 60 s later, so a crash loop reads crashed nearly all
    the time, but so does one crash the loop already recovered from, read inside its minute (pxx)."""
    polled = _epoch(status["last_poll_ok_at"]) if isinstance(status.get("last_poll_ok_at"), str) else None
    poll_s = max(_pacing(ident, "poll_s", POLL_S), 60)
    if polled is not None and now - polled > POLL_MISSED * poll_s:
        return f"status.json: no successful poll for {round((now - polled) / 60)} min", 1
    if status.get("run_codex_delivery") == "crashed":
        return "status.json: run_codex_delivery crashed", 2
    return None, 0


def judge(ident, now, status=None):
    """{seat, verdict, ...} for one identity at `now` (epoch seconds). `status` is the identity's entry of the postbox's
    status.json, or a string saying why there is none ("absent", "no entry", "unreadable: ...")."""
    seat = {"seat": ident.get("name"), "target": ident["target"]}
    # The delivery bank refuses a checkpoint file that is not a private regular file, and its loop then fails on every
    # pass (oxxxxxx's 1355a7: a manual retarget left it 0644, 1,610 errors over 13 h, 15 messages). Said at once.
    why, mode = bank_file_problem(ident["log"] + ".codex.offset.bank.json")
    if why:
        drift = {**seat, "verdict": "MODE_DRIFT", "why": "bank checkpoint " + why}
        if mode is not None:
            drift["bank_mode"] = oct(mode)
        return drift
    # The loop cannot run at all: also said at once (a reviewer b08032). The status file is the postbox's own record,
    # which is what went unread all night (pxx); its poll stamp's age still shows a dead daemon.
    refusal, bank_problem = bank_refusal(ident)
    if bank_problem:
        seat["bank"] = bank_problem
    if refusal:
        return {**seat, "verdict": "DELIVERY_DOWN", "why": refusal}
    failing = False
    if isinstance(status, dict):
        down, runs = loop_down(status, ident, now)
        if down:
            out = {**seat, "verdict": "DELIVERY_DOWN", "why": down}
            if runs > 1:
                out["confirm_runs"] = runs                  # the alert caller waits for this many runs in a row
            return out
        failing = status.get("codex") in FAILING
        if failing:
            seat["postbox_codex"] = status["codex"]
    elif status is not None:
        seat["status"] = str(status)
    try:
        queue = queue_rows(codex_home(ident), ident["target"])
    except (sqlite3.Error, OSError) as exc:
        queue, seat["queue"] = None, f"unreadable: {type(exc).__name__}"
    if queue is not None:
        seat["queue_rows"] = len(queue[0])
        if queue[1]:
            seat["queue_debris"] = sum(queue[1].values())  # rows of other threads: messages never taken, long ago
    mine, others = rollouts(ident)
    if len(mine) != 1:
        return {**seat, "verdict": "UNKNOWN", "why": f"{len(mine)} rollouts for the target"}
    try:
        records = read_rollout(mine[0])
        written = os.path.getmtime(mine[0])
    except OSError as exc:
        return {**seat, "verdict": "UNKNOWN", "why": f"rollout unreadable: {type(exc).__name__}"}
    # The hand-offs: the dispatch log, else the spool below the codex offset. Neither to be had is UNKNOWN, but only
    # after the queue has had its say: a row nobody took is a verdict whatever the logs are.
    handoffs, handoff_problem = [], None
    allowance = _pacing(ident, "codex_settle_s", SETTLE_S) + _pacing(ident, "codex_min_gap_s", MIN_GAP_S)
    try:
        handoffs = expecting(ident["log"] + ".codex.offset.dispatch.jsonl", now - LOOKBACK_S, ident["target"])
    except FileNotFoundError:
        try:
            handoffs = expecting_from_spool(ident["log"], now - LOOKBACK_S, allowance,
                                            passed_over(ident, now - LOOKBACK_S))
            seat["source"] = "spool_offset"
        except (OSError, ValueError) as exc:
            handoff_problem = f"no dispatch log, and the spool: {type(exc).__name__}"
    except (OSError, ValueError) as exc:
        handoff_problem = f"dispatch log unreadable: {type(exc).__name__}"
    turn_events = [r for r in records if r[0] in ("start", "end")]
    running = bool(turn_events) and turn_events[-1][0] == "start"
    ended_on_limit = bool(turn_events) and turn_events[-1][0] == "end" and turn_events[-1][2].endswith("-limit")
    # A request for a person inside the running turn, with nothing of the conversation after it (a token_count may
    # follow it, and does not count): what the turn is waiting on.
    waiting_on = None
    if running:
        for kind, at, data in reversed(records):
            if kind in ("user", "assistant", "end", "start"):
                break
            if kind == "parked":
                waiting_on = data
                break
    limits = [r[2] for r in records if r[0] == "limit"]

    def untaken(at, base):
        """The verdict for a message not taken since `at`, now WAIT_S or more ago: which class it is."""
        last_mine = records[-1][1] if records else written
        candidate = stale_candidate(others, ident.get("seat_dir"), max(last_mine, at))
        if candidate is not None and candidate[1]:
            return {**base, "verdict": "STALE_TARGET", "candidate_thread": candidate[0]}
        if waiting_on is not None and now - written >= WAIT_S:
            return {**base, "verdict": "PARKED", "waiting_on": waiting_on}
        if (limits and _limited(limits[-1], now)) or ended_on_limit:
            return {**base, "verdict": "RATE_LIMITED"}
        if running and now - written < WAIT_S:
            return {**base, "verdict": "BUSY"}
        stuck = {**base, "verdict": "STUCK", "waited_s": round(now - at)}
        if candidate is not None:
            stuck["newer_other_thread"] = candidate[0]      # unverified: no seat_dir, and the home may be shared
        return stuck

    # The queue itself first: a row for the target older than the wait is a message nobody took, whatever the
    # dispatch log says (it also catches a split home, F5, where no rollout ever shows the message).
    waiting_rows = queue[0] if queue is not None else []    # a row from after `now` (a replay) is never old enough
    if waiting_rows and now - min(waiting_rows) >= WAIT_S:
        return untaken(min(waiting_rows), {**seat, "queued_at": min(waiting_rows)})
    # Mail the postbox never handed over (F7: its delivery loop down; on 8 Nov the HZ admin's crashed every 60 s on a
    # bank file left 0644 by a retarget, 1,606 errors, 15 messages). Not while the postbox is holding it behind a
    # running turn, which it does for HOLD_MAX_S at the most.
    try:
        stalled = never_handed(ident, now, allowance, wait=QUICK_S if failing else WAIT_S)
    except OSError as exc:
        stalled, seat["spool"] = None, f"unreadable: {type(exc).__name__}"
    held_behind_turn = bool(stalled) and running and now - written < HOLD_MAX_S
    if stalled and not held_behind_turn:
        return {**seat, "verdict": "DELIVERY_STALLED", "arrived_at": stalled[0][0], "waiting": len(stalled),
                "keys": [k for _, k in stalled[:5]]}
    if handoff_problem is not None:
        return {**seat, "verdict": "UNKNOWN", "why": handoff_problem}
    for since, at, keys in handoffs:                        # the oldest first: the longest wait is the one to name
        token = [k.encode() for k in keys]
        took = next((i for i, r in enumerate(records) if r[0] == "user" and r[1] >= since - GRACE_S
                     and any(k in r[2] for k in token)), None)
        base = {**seat, "dispatch_at": at, "keys": keys[:5]}
        if took is None:
            if now - at < WAIT_S:
                if running and now - written < WAIT_S:
                    return {**base, "verdict": "BUSY"}
                continue                                    # still inside the wait: nothing to say yet
            return untaken(at, base)
        taken = records[took][1]
        # Only the taking turn answers: an assistant message, or that turn completing (an aborted one answers nothing),
        # between the take and the next task_started. An answer in a later, unrelated turn closed a take whose own turn
        # had aborted (GitHub Codex on #554, round 4; deferred to this follow-up).
        turn_end = next((j for j in range(took + 1, len(records)) if records[j][0] == "start"), len(records))
        answered = any(r[0] == "assistant" or (r[0] == "end" and not r[2].startswith("aborted"))
                       for r in records[took + 1:turn_end])
        # The taking turn, still running, answers in its own time, but only while its rollout grows: the bound BUSY
        # uses. A Codex that took the notice and died leaves task_started last for good, and read OK for good (GitHub
        # Codex on #554, third round). A later turn running says nothing of this take. If the silent turn is waiting on
        # a person, that is what it waits on.
        own_running = running and turn_end == len(records)
        if not answered and now - taken >= WAIT_S and not (own_running and now - written < WAIT_S):
            if waiting_on is not None:
                return {**base, "verdict": "PARKED", "waiting_on": waiting_on, "taken_at": taken}
            if (limits and _limited(limits[-1], now)) or ended_on_limit:
                return {**base, "verdict": "RATE_LIMITED", "taken_at": taken}
            return {**base, "verdict": "TAKEN_NOT_ANSWERED", "taken_at": taken}
    if held_behind_turn:
        return {**seat, "verdict": "BUSY", "held": len(stalled)}   # the postbox holds them while the turn runs
    return {**seat, "verdict": "OK", "checked": len(handoffs)}


def load_statuses(config_path, status_path=None):
    """(the postbox's status.json, None) or (None, why). postbox.py keeps config.json and status.json side by side
    (CONFIG, STATUS), so the default is beside the config."""
    return read_json(status_path or os.path.join(os.path.dirname(os.path.abspath(config_path)), "status.json"))


def status_entry(statuses, name):
    """One identity's status entry for judge(): its dict, or a string saying why there is none."""
    data, problem = statuses
    if data is None:
        return problem
    entry = data.get(name)
    return "no entry" if entry is None else entry if isinstance(entry, dict) else "invalid: not an object"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--status", default=None, help="the postbox's status.json (default: beside --config)")
    ap.add_argument("--identity", action="append", default=[])
    ap.add_argument("--now", type=float, default=None)
    a = ap.parse_args(argv)
    now = time.time() if a.now is None else a.now
    try:
        with open(a.config, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as exc:
        print(json.dumps({"verdict": "UNKNOWN", "why": f"config: {type(exc).__name__}"}))
        return 3
    statuses = load_statuses(a.config, a.status)
    worst = 0
    for ident in identities(config, a.identity):
        result = judge(ident, now, status_entry(statuses, ident.get("name")))
        print(json.dumps(result, sort_keys=True))
        worst = max(worst, EXIT[result["verdict"]])
    return worst


if __name__ == "__main__":
    sys.exit(main())
