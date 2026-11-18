"""`python -m tools.meshy_node fleet --seats a,b,...`: every named seat's presence and mail backlog in one table.

The idea is flightdeck's live fleet view (flightdeckhq/flightdeck, Apache-2.0: one row per agent with its state and its
latest activity); no code is taken from it. The data is MeshyChat's own and needs no new privilege: it comes from
`GET /v1/mesh/recipients` (divergence 083), which any peer that may read shared/mesh-chat may call and which answers
COUNTS ONLY. Per label: the peers it reaches, each one's presence (`effective_state`, which is `unknown` past the ttl
the seat gave or when it never published), the seconds since its last presence beat, its unread count and oldest unread
age, and how many of YOUR sends to it it has not answered.

From #484 on, each peer also carries `online`, MEASURED by the server: its listener holds a websocket that a worker
beat within 30 s (apps/wsgw/hub_presence, the rule call routing uses). That column, not the published state, says who
is there. Against a server before #484 it reads "?" and is never counted.

`--work` opts into timestamps and each peer's open/active ask counts over 14 days, held up to 60 s. Oxxx confirmed
counts and up to two recipient labels to this audience in signed f0bdf1 (Kxxx 67804f). Indexed context can lag and
is never treated as a live turn. Last sent covers eligible message keys from 7 days, not unlimited history.

Not here, on purpose (Codex's review of the fleet design, msg/<key>):
  - another seat's owed message list or text: --work adds counts and sampled recipient labels only;
  - fleet-wide fetch backlog: `fleet-unread` needs the bootstrap authority or mesh:backlog:read;
  - "restore ok": a presence state is what a seat SAID, never a measured restore of its mail, Brain or Planboard.

A label whose call failed reads "unavailable" with the reason, never a zero. A peer-supplied note is shown as quoted,
cleaned, capped data. The route takes at most 10 labels per call and 30 calls per minute per peer, so the seats are
asked in chunks of 10 normally, 4 for --work's more expensive on-demand facts.
"""
from __future__ import annotations

import re
import datetime as dt
from typing import Callable

#: Labels per call. The route takes up to 10 (apps/mesh/views.RECIPIENTS_MAX), but on 10 Nov 2026 at (time withheld) one
#: 10-label call ran past the client's 60 s under morning load while 4- and 5-label calls answered, and each label
#: now also costs the measured-activity read. Five keeps one call well inside the timeout.
CHUNK = 5
WORK_CHUNK = 4
TURN_QUIET_SECONDS = 300
TURN_REPORT_TTL_SECONDS = 600
NOTE_MAX = 40
# A seat's hooks re-publish presence at every SessionStart and Stop with a 6 h ttl (SEAT-SETUP.md step 9). A state
# that is not `unknown` with a beat older than that was published WITHOUT a ttl (a legacy beat never lapses: Codex,
# 2030f0 P2). Measured on production 2 Nov 09:47Z: uxxx `active` 75 days after its last beat, memory-bxxxxx 87 days.
# Such a state says nothing, so it is shown starred and counted, never as live.
STALE_SECS = 21600
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
_SEAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def turn_status(peer: dict) -> tuple[str, float | None]:
    """Reports lapse independently of socket/presence and the mail snapshot's held time."""
    item, report = _timestamp(peer.get("turn_item_at")), _timestamp(peer.get("turn_reported_at"))
    state, now = peer.get("turn_state"), _now()
    if state not in ("open", "closed") or item is None or report is None:
        return "?", None
    report_age, item_age = (now - report).total_seconds(), (now - item).total_seconds()
    if report_age >= TURN_REPORT_TTL_SECONDS or report_age < -60 or item_age < -60:
        return "?", None
    item_age = max(0, item_age)
    return ("idle" if state == "closed" else "quiet" if item_age >= TURN_QUIET_SECONDS else "working"), item_age


def idle_over(rows: list[dict], minutes: int) -> tuple[list[dict], list[dict]]:
    alerts, unknown = [], []
    for row in rows:
        if row.get("state") == "?" or "status" in row:
            unknown.append(row)
            continue
        owed = row.get("owed")
        unread = row.get("unread")
        queued = (isinstance(unread, int) and not isinstance(unread, bool) and unread > 0
                  or isinstance(owed, dict) and owed.get("open", 0) > 0)
        old_enough = (row.get("state") in ("idle", "quiet")
                      and row.get("turn_age_secs") is not None and row["turn_age_secs"] >= minutes * 60)
        if old_enough and queued:
            alerts.append(row)
        elif old_enough and unread is None:
            # The server could not count this seat's unread mail (the count passed its bound). It may be idle with
            # mail waiting: listed apart as unknown, never dropped and never alerted on as if the count were known.
            unknown.append(row)
    return alerts, unknown


def unsurfaced_status(peer: dict) -> tuple[int | None, str]:
    """A delayed notice is local observation evidence, never proof of message loss."""
    if turn_status(peer)[0] == "?":
        return None, "?"
    since = _timestamp(peer.get("unsurfaced_since"))
    if since is None or (since - _now()).total_seconds() > 60:
        return None, "?"
    count = peer.get("unsurfaced")
    if type(count) is not int or not -1 <= count <= 2048:
        return None, "?"
    if count == -1:
        return -1, "? partial"
    oldest = _timestamp(peer.get("unsurfaced_oldest_at"))
    if count == 0 and peer.get("unsurfaced_oldest_at") is None:
        return 0, "0"
    if oldest is None or (_now() - oldest).total_seconds() < 300:
        return None, "?"
    return count, f"{count}/{age((_now() - oldest).total_seconds())}"


#: seat-state v2.1 STALE_S: a usage snapshot OBSERVED longer ago than this is starred, whatever reported it since.
USAGE_STALE_SECONDS = 900
_WINDOW_SHORT = {"five_hour": "5h", "seven_day": "7d"}


def _window_short(window: object) -> str | None:
    if window in _WINDOW_SHORT:
        return _WINDOW_SHORT[window]
    m = re.fullmatch(r"([1-9][0-9]{0,5})m", window) if isinstance(window, str) else None
    if m is None:
        return None
    minutes = int(m.group(1))
    return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def usage_label(peer: dict, as_of: object) -> str:
    """'c45 5h12 7d38' = context 45% used, 5-hour window 12%, 7-day window 38%, as the seat itself last showed them;
    '*' = observed over USAGE_STALE_SECONDS before the answer's as_of; '-' = no snapshot reported."""
    usage, at = peer.get("usage"), _timestamp(peer.get("usage_at"))
    if not isinstance(usage, dict) or at is None:
        return "-"
    parts = []
    ctx = usage.get("context_used_pct")
    if isinstance(ctx, (int, float)) and not isinstance(ctx, bool):
        parts.append("c%d" % round(ctx))
    for lim in usage.get("limits") or []:
        short = _window_short(lim.get("window")) if isinstance(lim, dict) else None
        if short is None:
            continue
        pct = lim.get("used_pct")
        parts.append(short + ("?" if not isinstance(pct, (int, float)) or isinstance(pct, bool) else "%d" % round(pct)))
    ref = _timestamp(as_of) or _now()
    stale = (ref - at).total_seconds() > USAGE_STALE_SECONDS
    return (" ".join(parts) or "?") + ("*" if stale else "")


def age(secs: object) -> str:
    if not isinstance(secs, (int, float)) or isinstance(secs, bool) or secs < 0:
        return "-"
    s = int(secs)
    if s >= 172800:
        return f"{s // 86400}d"
    if s >= 5400:
        return f"{s // 3600}h"
    if s >= 90:
        return f"{s // 60}m"
    return f"{s}s"


def clean_note(note: object) -> str:
    if note is None or note == "":
        return ""
    flat = " ".join(_CONTROL.sub(" ", str(note)).split())
    flat = flat if len(flat) <= NOTE_MAX else flat[: NOTE_MAX - 3] + "..."
    return '"' + flat.replace('"', "'") + '"'


def parse_seats(raw: str) -> list[str]:
    seats = list(dict.fromkeys(s.strip() for s in raw.split(",") if s.strip()))
    bad = [s for s in seats if not _SEAT.fullmatch(s)]
    if not seats or bad:
        raise ValueError("--seats needs comma-separated seat labels" + (f"; not a label: {bad[0]!r}" if bad else ""))
    return seats


def _timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def _work_age(value: object, as_of: object) -> str:
    stamp, now = _timestamp(value), _timestamp(as_of)
    return "?" if stamp is None or now is None else age(max(0, (now - stamp).total_seconds()))


def _counts(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(s), int) and not isinstance(value[s], bool) and value[s] >= 0
               for s in ("open", "active")):
        return None
    return {s: value[s] for s in ("open", "active")}


def add_steerable(rows: list[dict], steer_fn: Callable[[str], dict]) -> list[dict]:
    """--steerable (steering plan item 5): each seat's newest conformance result, asked ONCE per seat however many
    peers answer its label (tools/meshy_node/conformance.steerable_for). A failed ask reads unknown, never no."""
    got: dict[str, dict] = {}
    for row in rows:
        seat = row["seat"]
        if seat not in got:
            try:
                got[seat] = steer_fn(seat)
            except Exception:  # noqa: BLE001
                got[seat] = {"steer": "unknown", "steer_age_s": None, "steer_by": None}
        row.update(got[seat])
    return rows


def _steer_cell(row: dict) -> str:
    """yes/no with the result's age and its writer (pxx on #539, P3 1), or unknown (no result, a stale one, or no
    answer)."""
    steer = row.get("steer") or "unknown"
    secs = row.get("steer_age_s")
    if steer == "unknown" or not isinstance(secs, (int, float)) or isinstance(secs, bool):
        return "unknown"
    by = row.get("steer_by")
    by = by[:12] if isinstance(by, str) and _SEAT.fullmatch(by) else "?"
    return f"{steer} {age(int(secs))} {by}"


def collect(seats: list[str], recipients_fn: Callable[[list[str]], dict], *,
            work: bool = False) -> tuple[list[dict], list[str]]:
    """One row per (label, peer); a label with no peer, or whose chunk failed, gets one row that says so."""
    rows: list[dict] = []
    as_of: list[str] = []
    chunk_size = WORK_CHUNK if work else CHUNK
    for i in range(0, len(seats), chunk_size):
        chunk = seats[i:i + chunk_size]
        try:
            answer = recipients_fn(chunk)
            got = {r.get("label"): r for r in answer.get("recipients") or [] if isinstance(r, dict)}
            if answer.get("as_of"):
                as_of.append(str(answer["as_of"]))
        except Exception as exc:  # noqa: BLE001 - said on each row, never shown as an empty fleet
            why = f"unavailable ({type(exc).__name__}: {' '.join(str(exc).split())[:80]})"
            rows.extend({"seat": s, "status": why} for s in chunk)
            continue
        for s in chunk:
            rec = got.get(s)
            if rec is None:
                rows.append({"seat": s, "status": "unavailable (the server's answer did not name this label)"})
                continue
            peers = [p for p in rec.get("peers") or [] if isinstance(p, dict)]
            if not peers:
                rows.append({"seat": s, "status": "no peer answers this label"})
                continue
            for p in peers:
                state = str(p.get("effective_state") or "unknown")
                beat = p.get("beat_age_secs")
                # A state published WITH a ttl carries state_expires_at; the server already turns an expired one
                # into another state (apps/peers/presence.states_of), so an unexpired ttl is the publisher's own
                # bound (up to 86400 s) and is never starred. Only a state with no ttl is judged by beat age
                # (Codex on #483, fleet.py:96).
                has_ttl = p.get("state_expires_at") not in (None, "")
                stale = state != "unknown" and not has_ttl and (
                    not isinstance(beat, (int, float)) or isinstance(beat, bool) or beat > STALE_SECS)
                online = p.get("online")
                row = {"seat": s, "peer": str(p.get("peer_id") or "?")[:8],
                             "state": state, "stale": stale,
                             # the state was published with a TTL that has not run out (the server turns an
                             # expired one into another state): the dispatcher's plan asks for this
                             "ttl": has_ttl,
                             # MEASURED (#484): the listener holds a websocket beaten within 30 s. None = a server
                             # before #484, which never says; shown "?" and never counted either way.
                             "online": online if isinstance(online, bool) else None,
                             "beat": age(p.get("beat_age_secs")),
                             "unread": p.get("unread_count"), "oldest": age(p.get("unread_oldest_age_secs")),
                       "yours": p.get("unanswered_from_you"), "note": clean_note(p.get("note")),
                       # MEASURED (f38d28 item 5): it SENT within 30 min, or published `active` with a live TTL
                       # (the dashboard's #575 rule). None = a server before it, shown "?" and never counted.
                       "active": p.get("active") if isinstance(p.get("active"), bool) else None,
                       "active_sent": _work_age(p.get("active_sent_at"), answer.get("as_of"))
                       if _timestamp(p.get("active_sent_at")) else None}
                if work:
                    reported_state, turn_age = turn_status(p)
                    unseen, lost_label = unsurfaced_status(p)
                    is_held = p.get("turn_state") == "open" and reported_state != "?"
                    row.update(published_state=row["state"], state=reported_state, turn_age_secs=turn_age,
                               turn_state=p.get("turn_state"), turn_item_at=p.get("turn_item_at"),
                               turn_reported_at=p.get("turn_reported_at"), unsurfaced=None if is_held else unseen,
                               held=unseen if is_held else None,
                               unsurfaced_oldest_at=p.get("unsurfaced_oldest_at"), unsurfaced_since=p.get("unsurfaced_since"),
                               since=_work_age(p.get("unsurfaced_since"), answer.get("as_of")),
                               lost="deferred" if is_held else lost_label,
                               held_label=lost_label if is_held else "?" if reported_state == "?" else "-")
                    meta = answer.get("work")
                    sent_days = meta.get("sent_days") if isinstance(meta, dict) else None
                    known_window = isinstance(sent_days, int) and not isinstance(sent_days, bool) and 0 < sent_days <= 365
                    # An explicit successful snapshot plus a known window distinguishes no recent send from no data.
                    no_send = (known_window and p.get("last_sent_at") is None
                               and _timestamp(p.get("work_as_of")) is not None)
                    awaiting = _counts(p.get("awaiting"))
                    targets = p.get("awaiting", {}).get("recipients", []) if awaiting is not None else []
                    targets = targets if isinstance(targets, list) else []
                    row.update(last_sent_at=p.get("last_sent_at") if _timestamp(p.get("last_sent_at")) else None,
                               indexed_turn_at=p.get("indexed_turn_at") if _timestamp(p.get("indexed_turn_at")) else None,
                               work_as_of=p.get("work_as_of") if _timestamp(p.get("work_as_of")) else None,
                               sent=f"none/{sent_days}d" if no_send else _work_age(p.get("last_sent_at"), answer.get("as_of")),
                               sent_days=sent_days if known_window else None,
                               indexed=_work_age(p.get("indexed_turn_at"), answer.get("as_of")),
                               owed=_counts(p.get("owed")), awaiting=awaiting,
                               awaiting_recipients=list(dict.fromkeys(
                                   t for t in targets if isinstance(t, str) and _SEAT.fullmatch(t)))[:2],
                               usage=p.get("usage") if isinstance(p.get("usage"), dict) else None,
                               usage_at=p.get("usage_at") if _timestamp(p.get("usage_at")) else None,
                               usage_label=usage_label(p, answer.get("as_of")))
                rows.append(row)
    return rows, as_of


STEER_LEGEND = ("steer: the newest signed conformance result by another seat (yes = a plain request to change the canary"
                " was refused and a seat directive was done), with its age; unknown = none, older than 7 d, or unreadable")


def _active_cell(r: dict) -> str:
    if r.get("active") is None:
        return "?"
    if not r["active"]:
        return "no"
    return "yes " + r["active_sent"] if r.get("active_sent") else "yes beat"


def render(rows: list[dict], as_of: list[str], *, work: bool = False, steer: bool = False) -> str:
    if work:
        return _render_work(rows, as_of, steer=steer)
    head = (f"{'seat':<18} {'peer':<8} {'sock':<4} " + (f"{'steer':<19} " if steer else "")
            + f"{'active':<10} {'state':<8} {'beat':>5} {'unread':>6} {'oldest':>6} {'yours':>5}  note")
    lines = ["fleet (active and sock are MEASURED; state is what each seat said; counts only)"
             + (f", as of {min(as_of)}" if as_of else ""), head]
    for r in rows:
        if "status" in r:
            lines.append(f"{r['seat'][:18]:<18} {r['status']}")
            continue
        unread = "?" if r["unread"] is None else str(r["unread"])
        yours = "?" if r["yours"] is None else str(r["yours"])
        state = (r["state"][:7] + "*") if r.get("stale") else r["state"][:8]
        sock = {True: "yes", False: "no"}.get(r.get("online"), "?")
        cell = f"{_steer_cell(r):<19} " if steer else ""
        act = _active_cell(r)
        lines.append(f"{r['seat'][:18]:<18} {r['peer']:<8} {sock:<4} {cell}{act:<10} {state:<8} {r['beat']:>5} {unread:>6} "
                     f"{r['oldest']:>6} {yours:>5}  {r['note']}".rstrip())
    unknown = sum(1 for r in rows if r.get("state") == "unknown")
    stale = sum(1 for r in rows if r.get("stale"))
    missing = sum(1 for r in rows if "status" in r)
    measured = [r for r in rows if isinstance(r.get("online"), bool)]
    sock_line = (f"{sum(1 for r in measured if r['online'])} of {len(measured)} with a live socket; " if measured
                 else "sockets not reported by this server (before #484); ")
    acted = [r for r in rows if isinstance(r.get("active"), bool)]
    if acted:
        sock_line = f"{sum(1 for r in acted if r['active'])} of {len(acted)} active; " + sock_line
    lines.append(f"{len(rows)} row(s); {sock_line}{unknown} unknown (no live presence); {stale} stale (*: published"
                 f" without a ttl, last beat over {STALE_SECS // 3600} h ago, says nothing); {missing} without data")
    lines.append("active: MEASURED, sent a message in the last 30 min (age of that send) or published active with a"
                 " live ttl ('beat'); sock: MEASURED, the listener's websocket seen within 30 s; state: as published,"
                 " unknown = lapsed"
                 " or never set; beat = since last publish; yours = your sends it has not answered")
    if steer:
        lines.append(STEER_LEGEND)
    return "\n".join(lines)


def _render_work(rows: list[dict], as_of: list[str], *, steer: bool = False) -> str:
    def pair(counts):
        return "?" if counts is None else f"{counts['open']}/{counts['active']}"

    lines = ["fleet work (reported turns and mail facts; published state is not a measured turn state)"
             + (f", as of {min(as_of)}" if as_of else ""),
             f"{'seat':<18} {'peer':<8} {'sock':<4} " + (f"{'steer':<19} " if steer else "")
             + f"{'state':<7} {'published':<9} {'sent':>8} {'turn<=':>6} "
             f"{'unread':>6} {'oldest':>6} {'lost':>13} {'held':>13} {'since':>7} {'owed':>9} {'awaits':>9} "
             f"{'usage':<14} recipients"]
    for row in rows:
        if "status" in row:
            lines.append(f"{row['seat'][:18]:<18} {row['status']}")
            continue
        published = row.get("published_state", "unknown")[:8] + ("*" if row.get("stale") else "")
        sock = {True: "yes", False: "no"}.get(row.get("online"), "?")
        unread = "?" if row["unread"] is None else str(row["unread"])
        cell = f"{_steer_cell(row):<19} " if steer else ""
        lines.append(f"{row['seat'][:18]:<18} {row['peer']:<8} {sock:<4} {cell}{row['state']:<7} {published:<9} "
                     f"{row.get('sent', '?'):>8} {row.get('indexed', '?'):>6} {unread:>6} {row['oldest']:>6} "
                     f"{row.get('lost', '?'):>13} "
                     f"{row.get('held_label', '?'):>13} {row.get('since', '?'):>7} "
                     f"{pair(row.get('owed')):>9} {pair(row.get('awaiting')):>9} "
                     f"{row.get('usage_label', '-'):<14} "
                     + ",".join(row.get("awaiting_recipients", [])))
    windows = sorted({row['sent_days'] for row in rows if row.get('sent_days') is not None})
    if windows:
        lines.append("sent window (days): " + ",".join(map(str, windows))
                     + "; none/Nd = no eligible send in that window, not an unknown timestamp.")
    lines.append("owed/awaits = open/active asks over 14 days, not proof of a blocked seat; ? = unavailable. "
                 "Work facts may be held for 60 s; turn<= is indexed age, a lagging upper bound on turn age. "
                 "A socket is the listener, not the turn.")
    lines.append("state: working = open with a record under 5m; quiet = open without a record for 5m; "
                 "idle = closed; ? = no report within 10m. Quiet does not identify a wait, prompt or hang.")
    lines.append("lost = count/oldest age of dispatched notices without surfaced or lookup evidence after 5m, "
                 "while closed: delays as well as possible losses. Held shows the same aged pending count "
                 "while open; it is not zero, proof of a hang, or a lost notice. "
                 "since = age of coverage baseline; zero means none since that baseline. After 30 quiet minutes a gap rebases "
                 "and retains suspected missing notices: an arrival inside the gap may count until read. "
                 "? = no observation; ? partial = incomplete coverage, reset/history gap or overflow, never zero.")
    lines.append("usage = what the seat's own status line or app server last showed: c = context used %, "
                 "5h/7d = plan window used %; * = observed over 15m ago, whatever reported since; - = none reported.")
    if steer:
        lines.append(STEER_LEGEND)
    return "\n".join(lines)
