"""`python -m tools.meshy_node resume-brief`: what a seat's fresh session needs first, read live and cut to a budget.

Design: msg/<key>, amended msg/<key>; GO msg/<key>.
Built 2026-11-02 because seats hand over to themselves by hand or not at all once a transcript grows past a few
hundred MB, and a fresh session started from a hand-written file alone.

Plain text, no model between a question and the records. Each line names the source it came from (a msg/ key, a PR,
a ctx/ key), so the reader can open it. Sections fill the budget in priority order: the handover file's NEXT ACTION
(first because it is at most two lines and the one thing a fresh session must not lose; at --budget 800 it was the first
section cut when it came last, memory-bxxxxx's review of #480), then what this seat owes, what it waits on, its pull
requests, its own recent work. A line is never cut. What does not fit is counted with the command that shows it, and a
whole section that does not fit is named at the end. A source that fails, or answers with nothing, says which: an empty
or failed section never reads as "nothing happened".

With --hook the whole run has one deadline under the documented 30 s hook timeout: no source call starts after
HOOK_SOFT_S, each gh call gets only the time left, and past HOOK_HARD_S the run prints one line and exits 0 whatever is
still waiting.

No message bodies. The owed and waiting lists carry keys, counterparties, threads, types and ages only, as meshy-mod's
notes do (divergence 067); the reader opens a key to see what was asked. The recent-work lines quote the seat's OWN
transcript, and only with both a seat and a since: a search without them stalled production on 2026-11-01.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

DEFAULT_REPO = "sampleco/MeshyChat"
OWED_SINCE = timedelta(days=14)  # the owed and brief routes' own default window
RECENT_SINCE = timedelta(hours=6)
RECENT_K = 8
RECENT_TEXT = 120  # characters of each recent-work hit shown
RECENT_QUERY = "what I am working on: the next step, the plan, a review, a PR, a fix, a test, what is done and what is left"
GATE_FRESH = timedelta(minutes=5)
HANDOVER_HEAD_LINES = 15  # handover_audit.py looks for NEXT ACTION in the first 15 lines
MIN_BUDGET = 450  # the header, the no-tool line under it (never cut) and the footer's room; it was 200 before that line
FOOTER_ROOM = 90  # kept free while sections fill, so the "cut for budget" line always fits
MORE_ROOM = 64  # kept free under a section's rows, so its "(+N more ...)" line always fits
GH_TIMEOUT_S = 20
# --hook: the documented SessionStart timeout is 30 s, and the gh calls alone are sequential at up to 20 s each (measured
# runs 14.0 s and 5.5 s). No source call starts after HOOK_SOFT_S; at HOOK_HARD_S the run gives up, leaving room for
# interpreter start and exit inside the 30 s.
HOOK_SOFT_S = 18.0
HOOK_HARD_S = 25.0
# Harness events in the seat's own transcript (a background task ended, a reminder): the upload keeps them, the brief
# skips them, since they say nothing about the work.
_NOISE = re.compile(r"^\[user_text\]\s*<(?:task-notification|system-reminder)\b")
# A transcript chunk is "[<kind>]" + newline + text items joined by blank lines (apps/meshsem/ctxjob.turn_chunks), cut
# from inside a turn, so a hit's text usually starts mid-item with no label. A server that sends `item_kinds` (one per
# item, in ordinal order) names that leading item; the labels inside the text name the rest.
# Every item kind the server stores (apps/context/validation.ITEM_KINDS; tests pin the two equal). A label this list
# missed would be read as part of the item before it: a "task" plan step after the seat's own text was quoted as its
# words (kxxx on #502, P2-502-1).
ITEM_KINDS = ("user_text", "assistant_text", "task", "artifact", "omitted", "tool_exchange", "thinking")
_ITEM_LABEL = re.compile(r"(?:^|\n\n)\[(" + "|".join(ITEM_KINDS) + r")\]\n")
_PR_IN_THREAD = re.compile(r"(?:^|[^a-z0-9])(?:pr|mg|#)[-_]?(\d{2,5})(?![0-9])", re.IGNORECASE)
_SEAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class Section:
    name: str  # short, for the "cut for budget" line
    title: str
    rows: list[str] = field(default_factory=list)
    status: str | None = None  # set when the source failed or answered nothing: shown instead of rows
    more_cmd: str = ""  # the command that shows every row


class Deadline:
    """One deadline for a whole run: no source call starts after `soft` seconds, and a call that does start is given
    at most what is left before `hard`. `clock` is injectable so tests need no sleeping."""

    def __init__(self, soft: float, hard: float, clock: Callable[[], float] = time.monotonic):
        if not 0 < soft <= hard:
            raise ValueError("a deadline needs 0 < soft <= hard")
        self.soft, self.hard, self.clock = soft, hard, clock
        start = clock()
        self.soft_end, self.hard_end = start + soft, start + hard

    def expired(self) -> bool:
        return self.clock() >= self.soft_end

    def cap(self, timeout: float) -> float:
        """A call's own timeout, cut to what is left before the hard end (never below 0.5 s)."""
        return max(0.5, min(timeout, self.hard_end - self.clock()))


def vienna(dt: datetime) -> str:
    """HH:MM in Vienna by the EU rule (summer time from the last Sunday of March to the last Sunday of October, both at
    01:00 UTC), so no tz database is needed: Git Bash and some Windows Pythons have none, and fall back to UTC silently."""
    dt = dt.astimezone(timezone.utc)

    def last_sunday(month: int) -> datetime:
        d = datetime(dt.year, month, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    summer = last_sunday(3) <= dt < last_sunday(10)
    return (dt + timedelta(hours=2 if summer else 1)).strftime("%H:%M")


def age(then: object, now: datetime) -> str:
    try:
        t = datetime.fromisoformat(str(then).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "?"
    if t.tzinfo is None:
        return "?"
    s = max(0, int((now - t).total_seconds()))
    if s >= 172800:
        return f"{s // 86400}d"
    if s >= 5400:
        return f"{s // 3600}h"
    if s >= 90:
        return f"{s // 60}m"
    return f"{s}s"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def one_line(text: object, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def prs_in_thread(thread: object) -> list[int]:
    return sorted({int(m) for m in _PR_IN_THREAD.findall(str(thread or ""))})


def _hit(row: dict) -> dict:
    hit = row.get("hit") if isinstance(row, dict) else None
    return hit if isinstance(hit, dict) else {}


# ---- sources -------------------------------------------------------------------------------------------------------

def owed_section(owed_fn: Callable[[], dict], pr_states: dict[int, str], now: datetime, repo: str = DEFAULT_REPO) -> Section:
    sec = Section("owed", "owed (reply expected from you, oldest first)", more_cmd="meshy_node owed")
    try:
        answer = owed_fn()
    except Exception as exc:  # noqa: BLE001 - any failure is reported on the line, never as an empty list
        sec.status = f"unavailable ({type(exc).__name__}: {one_line(exc, 100)})"
        return sec
    # Open only since SD 255 (round 1 item 15): an active ask (posted in its thread after it, not as a reply) has its own
    # section below, so the count here is the asks still waiting for a reply. A row with no state stays here, never hidden.
    rows = [r for r in (answer.get("owed") or []) if isinstance(r, dict) and r.get("state") in ("open", None)]
    if not rows:
        sec.status = "none open in the last 14 days" + (" (the server's list was cut)" if answer.get("truncated") else "")
        return sec
    rows.sort(key=lambda r: str(_hit(r).get("recorded_at") or ""))
    sec.rows.extend(_owed_line(r, pr_states, now, repo) for r in rows)
    sec.title += f" {len(rows)}" + ("+" if answer.get("truncated") else "")
    return sec


def _owed_line(r: dict, pr_states: dict[int, str], now: datetime, repo: str) -> str:
    """One owed or replied-in-thread ask: key, author, age, state, type and thread, its done-when, and "likely done" when
    every PR its thread names has MERGED."""
    h = _hit(r)
    line = (f"  {h.get('key', '?')} from {h.get('author', '?')}, {age(h.get('recorded_at'), now)}, {r.get('state') or '?'}"
            f", {r.get('msg_type') or '?'}, thread {r.get('thread_id') or '-'}")
    if r.get("done_when"):
        line += ", has a done-when"
    prs = prs_in_thread(r.get("thread_id"))
    if prs and all(pr_states.get(n) == "MERGED" for n in prs):
        # The number comes from the thread id and was looked up in --repo; a thread about another repository's PR of
        # the same number reads wrong here, so the line names the repository it checked (#480 P3).
        line += (f"; likely done ({repo.rsplit('/', 1)[-1]} PR {', '.join('#%d' % n for n in prs)} MERGED), close with "
                 f"--closes {h.get('key', '?')}?")
    return line


def replied_section(owed_fn: Callable[[], dict], pr_states: dict[int, str], now: datetime,
                    repo: str = DEFAULT_REPO) -> Section:
    """The active asks: the seat posted in the ask's thread after it, but not as a reply to it (SD 255). One status post
    in a busy thread turns every unanswered ask there active, so they are listed by key, oldest first (pxx A)."""
    sec = Section("replied in thread", "replied in thread, not linked (read each; reply or --closes it, oldest first)",
                  more_cmd="meshy_node owed --state active")
    try:
        answer = owed_fn()
    except Exception as exc:  # noqa: BLE001 - the owed section reports the same failure; say it here too
        sec.status = f"unavailable ({type(exc).__name__}: {one_line(exc, 100)})"
        return sec
    rows = [r for r in (answer.get("owed") or []) if isinstance(r, dict) and r.get("state") == "active"]
    cut = bool(answer.get("truncated"))
    if not rows:
        sec.status = "none in the last 14 days" + (" (the server's list was cut)" if cut else "")
        return sec
    rows.sort(key=lambda r: str(_hit(r).get("recorded_at") or ""))
    sec.rows.extend(_owed_line(r, pr_states, now, repo) for r in rows)
    sec.title += f" {len(rows)}" + ("+" if cut else "")
    return sec


def waiting_section(awaiting_fn: Callable[[], dict], now: datetime) -> Section:
    sec = Section("waiting on", "waiting on (your asks others owe)", more_cmd="meshy_node awaiting --state open,active")
    try:
        answer = awaiting_fn()
    except Exception as exc:  # noqa: BLE001
        sec.status = f"unavailable ({type(exc).__name__}: {one_line(exc, 100)})"
        return sec
    rows = [r for r in (answer.get("awaiting") or []) if isinstance(r, dict) and r.get("state") in ("open", "active")]
    cut = bool(answer.get("truncated"))
    if not rows:
        # A cut page with none open says nothing about the rows past the cut (Codex on #480, resume_brief.py:181).
        sec.status = "none open or active in the last 14 days" + (" (the server's list was cut)" if cut else "")
        return sec
    rows.sort(key=lambda r: str(_hit(r).get("recorded_at") or ""))
    for r in rows:
        h = _hit(r)
        # No thread here: the server's awaiting rows carry none (apps/mesh/graph.AwaitingAck has no thread_id, unlike
        # OwedAck), so a thread column would read "-" on every row (Codex on #480, memory-bxxxxx).
        sec.rows.append(f"  {h.get('key', '?')} on {r.get('owing_label') or h.get('dst') or '?'}, {age(h.get('recorded_at'), now)}, "
                        f"{r.get('state')}")
    sec.title += f" {len(rows)}" + ("+" if cut else "")
    return sec


def pr_section(seat: str, repo: str, owed_rows: list[dict], awaiting_rows: list[dict], gh: Callable[[list[str]], object],
               gate_file: Path | None, now: datetime) -> tuple[Section, dict[int, str]]:
    """The seat's PRs: those whose branch carries its lane prefix (`<seat>/`), and those named in the thread ids of its
    owed asks (role "thread T, msg/..."). Each PR's state comes from gh at run time. Waiting asks name no PR here:
    the server's awaiting rows carry no thread id (apps/mesh/graph.AwaitingAck), so `awaiting_rows` is read only if a
    row does carry one (a later server may add it)."""
    sec = Section("pull requests", f"pull requests in {repo}", more_cmd=f"gh pr list --repo {repo}")
    roles: dict[int, str] = {}
    states: dict[int, str] = {}
    heads: dict[int, str] = {}
    errors: list[str] = []
    try:
        listed = gh(["pr", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json", "number,headRefName,headRefOid"])
        for p in listed if isinstance(listed, list) else []:
            if str(p.get("headRefName", "")).startswith(seat + "/"):
                n = int(p["number"])
                roles[n] = f"yours, branch {p['headRefName']}"
                states[n] = "OPEN"
                heads[n] = str(p.get("headRefOid") or "")[:8]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"gh pr list failed ({type(exc).__name__}: {one_line(exc, 80)})")
    for rows, what in ((owed_rows, "owed"), (awaiting_rows, "waiting")):
        for r in rows:
            for n in prs_in_thread(r.get("thread_id")):
                roles.setdefault(n, f"{what} in thread {r.get('thread_id')}, {_hit(r).get('key', '?')}")
    for n in sorted(roles):
        if n in states:
            continue
        try:
            v = gh(["pr", "view", str(n), "--repo", repo, "--json", "number,state,headRefOid"])
            states[n] = str(v.get("state") or "?") if isinstance(v, dict) else "?"
            heads[n] = str(v.get("headRefOid") or "")[:8] if isinstance(v, dict) else ""
        except Exception as exc:  # noqa: BLE001
            states[n] = f"? (gh failed: {type(exc).__name__})"
    for n in sorted(roles):
        sec.rows.append(f"  PR #{n} {states.get(n, '?')}" + (f" head {heads[n]}" if heads.get(n) else "") + f" ({roles[n]})")
    sec.rows.append("  gate: " + gate_line(gate_file, now))
    if errors:
        sec.rows.insert(0, "  " + "; ".join(errors))
    if not roles:
        sec.title += ": none found by branch prefix or thread id"
    else:
        sec.title += f" {len(roles)}"
    return sec, states


def gate_line(gate_file: Path | None, now: datetime) -> str:
    if gate_file is None:
        return "unavailable (no --gate-file given)"
    try:
        doc = json.loads(gate_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"unavailable ({gate_file.name} missing)"
    except (OSError, ValueError) as exc:
        return f"unavailable ({gate_file.name} unreadable: {type(exc).__name__})"
    updated = doc.get("updated_at") if isinstance(doc, dict) else None
    try:
        t = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        stale = t.tzinfo is None or now - t > GATE_FRESH
    except (TypeError, ValueError):
        stale = True
    if stale:
        return f"unavailable ({gate_file.name} stale, updated_at {updated})"
    parts = []
    legacy = {k: doc[k] for k in ("verdict", "status", "head", "pr") if k in doc}
    if legacy:
        parts.append(json.dumps(legacy, sort_keys=True))
    # HQB's runner projection (hqb_gate_json.py) carries running, queue and recent; before #480's P3 only `running`
    # showed, so the line read {"running": null} with the queue and the last results left out.
    if "running" in doc:
        parts.append(f"running {_gate_entry(doc['running'])}" if doc["running"] else "nothing running")
    queue = doc.get("queue")
    if isinstance(queue, list):
        parts.append(f"queue {len(queue)}" + (": " + ", ".join(_gate_entry(e) for e in queue[:3]) +
                                              (", ..." if len(queue) > 3 else "") if queue else ""))
    recent = [e for e in doc.get("recent") or [] if isinstance(e, dict)] if isinstance(doc.get("recent"), list) else []
    if recent:
        last = max(recent, key=lambda e: str(e.get("finished_at") or e.get("started_at") or ""))
        parts.append(f"last {_gate_entry(last)} {last.get('verdict') or '?'}" +
                     (f", finished {age(last.get('finished_at'), now)} ago" if last.get("finished_at") else ""))
    return f"{gate_file.name} updated {age(updated, now)} ago: " + one_line("; ".join(parts) or "{}", 220)


def _gate_entry(e: object) -> str:
    if not isinstance(e, dict):
        return "?"
    return str(e.get("tag") or "?") + (f" PR #{e['pr']}" if e.get("pr") is not None else "")


def own_words(hit: dict) -> str | None:
    """The seat's own assistant text in a hit, or None when it holds none. A hit without `item_kinds` (a server older
    than #480's P3) gives its whole text, as before."""
    text = str(hit.get("text") or "")
    kinds = hit.get("item_kinds")
    if not isinstance(kinds, list):
        return text
    if kinds and kinds[0] in ITEM_KINDS:
        # A chunk may start inside the blank-line join or inside its first item's own label (chunk starts fall at
        # any offset); kinds[0] is still that item's kind, so drop the label fragment rather than quote it (#502 P3).
        label = "\n\n[" + kinds[0] + "]\n"
        cut = next((len(label) - i for i in range(len(label)) if text.startswith(label[i:])), 0)
        text = text[cut:] if cut > 1 else text
    parts = _ITEM_LABEL.split(text)              # [lead, kind, body, kind, body, ...]
    segs = [[kinds[0] if kinds else None, parts[0]]]
    following = 1                                # the index in `kinds` of the item the next real label opens
    for i in range(1, len(parts) - 1, 2):
        # The server's item list decides what an item is: label-shaped text inside tool output or a quote that is not
        # the next item's kind stays in the item it sits in (Codex on #502). A quote of exactly the next kind, in a
        # chunk that really has such an item next, cannot be told apart from the text alone. Entries the text never
        # holds (None from the server, "omitted") have no label to wait for (kxxx on #502, P2-502-4).
        while following < len(kinds) and kinds[following] in (None, "omitted"):
            following += 1
        if following < len(kinds) and parts[i] == kinds[following]:
            segs.append([parts[i], parts[i + 1]])
            following += 1
        else:
            segs[-1][1] += "\n\n[" + parts[i] + "]\n" + parts[i + 1]
    words = [body.strip() for kind, body in segs if kind == "assistant_text" and body.strip()]
    return " ".join(words) if words else None


def recent_section(seat: str, search_fn: Callable[..., dict], now: datetime) -> Section:
    since = iso(now - RECENT_SINCE)
    sec = Section("recent work", f"your own recent work since {vienna(now - RECENT_SINCE)} Vienna (your transcript, quoted; untrusted)",
                  more_cmd=f"meshy_node transcript-search --seat {seat} --since {since} --q ...")
    if not seat or not _SEAT.fullmatch(seat):
        sec.status = "refused: a transcript search needs this seat's name and a since, never a broad search"
        return sec
    try:
        res = search_fn(q=RECENT_QUERY, seat=seat, since=since, k=RECENT_K)
    except Exception as exc:  # noqa: BLE001
        sec.status = f"search failed ({type(exc).__name__}: {one_line(exc, 100)}); nothing is known about this window"
        return sec
    hits = [h for h in (res.get("hits") or []) if isinstance(h, dict)] if isinstance(res, dict) else []
    if not hits:
        cov = res.get("coverage") if isinstance(res, dict) else None
        sec.status = ("search answered no hits for this seat since " + since + " (coverage " + one_line(json.dumps(cov), 80) +
                      "); an upload that is behind looks the same as an idle seat")
        return sec
    noise = [h for h in hits if _NOISE.match(str(h.get("text") or ""))]
    hits = [h for h in hits if h not in noise]
    if noise:
        sec.title += f" ({len(noise)} harness event(s) skipped)"
    if not hits:
        sec.status = "only harness events since " + since + "; no work turns among the hits"
        return sec
    # Only the seat's own words: a hit that is a slice of tool output or of someone else's message says nothing about
    # what the seat was doing (#480 P3: all 8 rows of a live brief were fragments of tool calls and their output).
    worded = [(h, own_words(h)) for h in hits]
    others = sum(1 for _h, w in worded if w is None)
    if others:
        sec.title += f" ({others} hit(s) without your own words skipped)"
    worded = [(h, w) for h, w in worded if w is not None]
    if not worded:
        sec.status = "no hit since " + since + " holds your own words; only tool output and other turns matched"
        return sec
    worded.sort(key=lambda hw: str(hw[0].get("time_last") or hw[0].get("time_first") or ""), reverse=True)
    for h, words in worded:
        keys = h.get("item_keys") or []
        sec.rows.append(f"  {age(h.get('time_last'), now)} ago {keys[0] if keys else '?'}: {one_line(words, RECENT_TEXT)}")
    return sec


def handover_section(path: Path | None, now: datetime) -> Section:
    sec = Section("handover file", "handover file (its NEXT ACTION)")
    if path is None:
        sec.status = "none given (--handover-file)"
        return sec
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            head = [next(f, "") for _ in range(HANDOVER_HEAD_LINES)]
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError as exc:
        sec.status = f"unreadable ({type(exc).__name__}: {path})"
        return sec
    nxt = next((ln for ln in head if "NEXT ACTION" in ln), None)
    # the file's mtime is its last edit, not when the handover was written (kxxx): say so
    sec.rows.append(f"  {path}, last modified {age(iso(mtime), now)} ago")
    sec.rows.append("  " + (one_line(nxt, 300) if nxt else f"no NEXT ACTION line in its first {HANDOVER_HEAD_LINES} lines"))
    return sec


# ---- the budget ----------------------------------------------------------------------------------------------------

#: Said at every session start and after every compaction, where the mistake is made: a compaction's list of "tools
#: used before the summary" names mcp__plugin_meshy_meshy__channel, and it is not a tool (pxx, 15 Nov 2026).
#: The same sentence as tools/postbox/meshy_channel.py NO_TOOLS; tests/test_resume_brief.py holds them together.
NO_TOOLS_LINE = ("  the meshy plugin exposes no tools: never call mcp__plugin_meshy_meshy__channel (a list of tools used "
                 "before a summary may name it: deliveries, not a tool); every read, fetch and send is tools.meshy_node.")


def render(header: str, sections: list[Section], budget: int) -> str:
    """Lines in order while they fit `budget` characters (newlines counted). Rows that do not fit become one
    "(+N more: run X)" line; a section whose title does not fit is named in a last "cut for budget" line."""
    if budget < MIN_BUDGET:
        raise ValueError(f"--budget must be at least {MIN_BUDGET}")
    out = [header]
    used = len(header) + 1
    room = budget - FOOTER_ROOM
    cut: list[str] = []
    for sec in sections:
        title = f"{sec.title}: {sec.status}" if sec.status else f"{sec.title}:"
        if used + len(title) + 1 + (0 if sec.status or not sec.rows else MORE_ROOM) > room:
            cut.append(sec.name)
            continue
        out.append(title)
        used += len(title) + 1
        if sec.status:
            continue
        for i, row in enumerate(sec.rows):
            if used + len(row) + 1 + (MORE_ROOM if i < len(sec.rows) - 1 else 0) > room:
                more = f"  (+{len(sec.rows) - i} more" + (f": run {sec.more_cmd})" if sec.more_cmd else ")")
                more = more if len(more) <= MORE_ROOM else f"  (+{len(sec.rows) - i} more)"
                out.append(more)
                used += len(more) + 1
                break
            out.append(row)
            used += len(row) + 1
    if cut:
        foot = "cut for budget: " + ", ".join(cut) + " (run with a larger --budget)"
        if len(foot) > FOOTER_ROOM - 1:
            foot = f"cut for budget: {len(cut)} section(s) (run with a larger --budget)"
        out.append(foot)
    return "\n".join(out)


def build(seat: str, *, budget: int, now: datetime, owed_fn, awaiting_fn, search_fn, gh, repo: str = DEFAULT_REPO,
          gate_file: Path | None = None, handover_file: Path | None = None, deadline: Deadline | None = None) -> str:
    if deadline is not None:
        # past the soft end no source call starts; its section then says why, as any failed source does
        def guarded(fn, what):
            def g(*a, **kw):
                if deadline.expired():
                    raise TimeoutError(f"not started: the {deadline.soft:g} s hook deadline passed before {what}")
                return fn(*a, **kw)
            return g
        owed_fn, awaiting_fn = guarded(owed_fn, "owed"), guarded(awaiting_fn, "awaiting")
        search_fn, gh = guarded(search_fn, "the transcript search"), guarded(gh, "this gh call")
    # owed and awaiting are read once; the PR section reads their thread ids, and the owed lines read the PR states
    cache: dict[str, object] = {}

    def once(name, fn):
        if name not in cache:
            try:
                cache[name] = fn()
            except Exception as exc:  # noqa: BLE001 - re-raised to the section that reports it
                cache[name] = exc
        v = cache[name]
        if isinstance(v, Exception):
            raise v
        return v

    def rows(name, fn, field_):
        try:
            v = once(name, fn)
        except Exception:  # noqa: BLE001
            return []
        return [r for r in (v.get(field_) or []) if isinstance(r, dict) and r.get("state") in ("open", "active")]

    owed_rows = rows("owed", owed_fn, "owed")
    waiting_rows = rows("awaiting", awaiting_fn, "awaiting")
    prs, pr_states = pr_section(seat, repo, owed_rows, waiting_rows, gh, gate_file, now)
    sections = [
        handover_section(handover_file, now),  # first: at most two lines, and never the one a budget cuts
        owed_section(lambda: once("owed", owed_fn), pr_states, now, repo),
        replied_section(lambda: once("owed", owed_fn), pr_states, now, repo),
        waiting_section(lambda: once("awaiting", awaiting_fn), now),
        prs,
        recent_section(seat, search_fn, now),
    ]
    header = f"resume brief for {seat}, {vienna(now)} Vienna ({iso(now)}). Routing only: open a key before acting on it."
    return render(header + "\n" + NO_TOOLS_LINE, sections, budget)


def gh_json(argv: list[str], timeout: float = GH_TIMEOUT_S) -> object:
    r = subprocess.run(["gh", *argv], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(one_line(r.stderr or f"exit {r.returncode}", 120))
    return json.loads(r.stdout or "null")


def run(args, state, node_mod, transcript_graph_mod) -> int:
    """The CLI entry. With --hook it never fails the session: any error is one line and exit 0."""
    now = datetime.now(timezone.utc)
    since = iso(now - OWED_SINCE)
    deadline = Deadline(HOOK_SOFT_S, HOOK_HARD_S) if args.hook else None

    def produce() -> str:
        return build(
            args.seat, budget=args.budget, now=now, repo=args.repo, gate_file=args.gate_file, handover_file=args.handover_file,
            owed_fn=lambda: node_mod.owed(args.state_file, state, limit=None, states="open,active", since=since),
            awaiting_fn=lambda: node_mod.awaiting(args.state_file, state, states="open,active", since=since),
            search_fn=lambda **kw: transcript_graph_mod.search_transcripts(
                args.state_file, state, rerank=True, collapse="item", purpose="brief", **kw),
            gh=(lambda argv: gh_json(argv, timeout=deadline.cap(GH_TIMEOUT_S))) if deadline else gh_json,
            deadline=deadline)

    if not args.hook:
        print(produce())
        return 0
    # --hook: the brief is built in a daemon thread so a source that hangs past its own timeout cannot hold the
    # session's start beyond HOOK_HARD_S; whatever is still waiting then is abandoned with the process.
    result: dict[str, object] = {}

    def work() -> None:
        try:
            result["text"] = produce()
        except BaseException as exc:  # noqa: BLE001 - reported as the one line below
            result["exc"] = exc

    worker = threading.Thread(target=work, name="resume-brief", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline.hard_end - deadline.clock()))
    if worker.is_alive():
        print(f"resume-brief: unavailable (over the {HOOK_HARD_S:g} s hook deadline; run it without --hook to wait for it)")
        return 0
    if "exc" in result:
        exc = result["exc"]
        print(f"resume-brief: unavailable ({type(exc).__name__}: {one_line(exc, 160)})")
        return 0
    print(result["text"])
    return 0
