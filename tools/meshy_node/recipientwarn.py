"""Warn, never refuse, when a recipient is unavailable or behind on its mail (issue #140, divergence 083).

Before `send`, one GET /v1/mesh/recipients for the labels in `to` (cc is not checked: a copy asks nothing of
its reader). Two warnings, kept distinct because they fail differently:
  UNAVAILABLE  the recipient published `asleep` (compacting, stopped) and it has not expired, or it published
               a state that HAS expired: its own word ran out. A peer that never published is NOT warned
               about: absence of a hook is not absence of the seat, and until seats publish (#139) every
               peer would warn.
  BEHIND       it has BEHIND_MIN_UNREAD or more messages addressed to it that it has not fetched.
  UNRESOLVED   the label reaches NO peer (issue #239): the server answered an empty peer list, so the row is
               stored and nobody is told of it. A roster entry added after the server started is the measured
               case (11 Oct: a PING to a new seat's roster name went to no one). all/both reach everyone by
               design, so they are never unresolved.
(`unanswered`, a recipient that has not answered YOU, is the send guard's own concern, not this one.)

FAIL OPEN. Any error, timeout (TIMEOUT_S) or malformed answer is no warning, and the message goes out exactly
as before: this module never changes what is sent. Answers are cached for CACHE_TTL_S beside the state file,
so a burst of sends costs one call.

MEASURE BEFORE TIGHTENING. Every checked send, warned or not, appends one line to `<state>.sendwarn.jsonl`:
time, key, and per recipient label its effective_state, seconds since its beat and unread count. Counts only,
no body. BEHIND_MIN_UNREAD is a starting value; the threshold is to be set from two weeks of this log.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

CACHE_SUFFIX = ".recipients.json"
LOG_SUFFIX = ".sendwarn.jsonl"
CACHE_TTL_S = 60.0
TIMEOUT_S = 2.0
BEHIND_MIN_UNREAD = 5
#: Labels that address everyone (apps/memoryskills/roster.py and apps/wsgw/mesh_notify.py BROADCAST_LABELS; a test
#: holds the three equal). The server answers them with no peers, which is not "unresolved".
BROADCAST_LABELS = frozenset({"all", "both"})


def cache_path(state_file: Path) -> Path:
    return state_file.with_name(state_file.name + CACHE_SUFFIX)


def log_path(state_file: Path) -> Path:
    return state_file.with_name(state_file.name + LOG_SUFFIX)


def _well_formed(answer) -> bool:
    """recipients: a list of dicts, each peers: a list of dicts. Anything else is no answer."""
    if not isinstance(answer, dict) or not isinstance(answer.get("recipients"), list):
        return False
    for rec in answer["recipients"]:
        if not isinstance(rec, dict) or not isinstance(rec.get("peers"), list):
            return False
        if not all(isinstance(p, dict) for p in rec["peers"]):
            return False
    return True


def _within(fetcher, labels, deadline_s: float):
    """`fetcher(labels)` on a daemon thread, waited on for at most `deadline_s` of WALL time. A slow
    server, a response trickled byte by byte or a stalled socket is abandoned, not waited out: the
    thread is a daemon and writes nothing, so leaving it is safe (Codex on #145)."""
    box: dict = {}

    def run():
        try:
            box["answer"] = fetcher(labels)
        except BaseException as exc:                      # noqa: BLE001 - carried back, judged below
            box["error"] = exc

    t = threading.Thread(target=run, name="recipient-check", daemon=True)
    t.start()
    t.join(deadline_s)
    if t.is_alive() or "error" in box:
        return None
    return box.get("answer")


def fetch(state_file: Path, labels: list[str], fetcher, *, now: float | None = None,
          deadline_s: float = TIMEOUT_S) -> dict | None:
    """The recipients answer for `labels`, from the cache when fresh, else from `fetcher(labels)` under a
    wall-clock deadline. None on anything that goes wrong: a warning that cannot be computed is no warning."""
    want = sorted(set(labels))
    if not want:
        return None
    now = time.time() if now is None else now
    path = cache_path(state_file)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(cached, dict) and cached.get("to") == want and isinstance(cached.get("t"), (int, float))
                and 0 <= now - cached["t"] < CACHE_TTL_S and _well_formed(cached.get("answer"))):
            return cached["answer"]
    except (OSError, ValueError):
        pass
    answer = _within(fetcher, want, deadline_s)
    if not _well_formed(answer):
        return None
    try:
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"t": now, "to": want, "answer": answer}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return answer


def invalidate(state_file: Path) -> None:
    """Drop the cached answer after a send the server accepted: the send itself changed the numbers
    (`unanswered_from_you` grew by one, divergence 084), so the next send must not reuse the old ones
    (Codex on #146). Never raises."""
    try:
        cache_path(state_file).unlink(missing_ok=True)
    except OSError:
        pass


def warnings(answer: dict | None, body_bytes: int | None = None) -> list[dict]:
    """One warning per (label, peer, kind). Never raises: anything malformed is no warning. With
    `body_bytes`, the send guard's advisory tier too (divergence 084): a large body to a recipient that has
    not answered an earlier send."""
    try:
        return _warnings(answer, body_bytes)
    except Exception:                                     # noqa: BLE001 - a warning never stops a send
        return []


def _warnings(answer, body_bytes=None) -> list[dict]:
    from . import sendguard
    out: list[dict] = []
    if not isinstance(answer, dict):
        return out
    for rec in answer.get("recipients") or []:
        if not isinstance(rec, dict):
            continue
        label = rec.get("label")
        peers = rec.get("peers")
        if (isinstance(label, str) and isinstance(peers, list) and not peers
                and label.strip().lower() not in BROADCAST_LABELS):
            out.append({"warning": "recipient_unresolved", "label": label,
                        "note_for_sender": "the message is sent and stored, but no peer answers to this label, so "
                                           "nobody is told of it. Check the spelling, or address the seat by a label "
                                           "it answers to (its roster name, its peers.name or peer-<id8>); a roster "
                                           "entry added since the server last started is not loaded yet"})
        for peer in peers or []:
            if not isinstance(peer, dict):
                continue
            state, age = peer.get("effective_state"), peer.get("beat_age_secs")
            if state == "asleep" or (state == "unknown" and isinstance(age, int)):
                out.append({"warning": "recipient_unavailable", "label": label, "peer_id": peer.get("peer_id"),
                            "effective_state": state, "note": peer.get("note"), "beat_age_secs": age,
                            "note_for_sender": "the message is sent; this recipient said it is not working right "
                                               "now (or its last word has expired), so expect a delayed answer"})
            unread = peer.get("unread_count")
            if isinstance(unread, int) and not isinstance(unread, bool) and unread >= BEHIND_MIN_UNREAD:
                out.append({"warning": "recipient_behind", "label": label, "peer_id": peer.get("peer_id"),
                            "unread_count": unread, "unread_oldest_age_secs": peer.get("unread_oldest_age_secs"),
                            "note_for_sender": f"the message is sent; this recipient has {unread} messages it has "
                                              f"not fetched yet, so a follow-up will only add to the pile"})
            unans = peer.get("unanswered_from_you")
            if body_bytes is not None and sendguard.advisory(body_bytes, unans):
                # SHADOW (divergence 084): logged, not printed, for two weeks. Scored on 2026-10-05 it would fire on
                # 44 of 300 ordinary sends (14.7%), and the second labeller puts its precision at 0.14: a warning
                # that often, that unsure, would be learned and ignored. The log decides whether it is shown.
                out.append({"warning": "recipient_unanswered", "shadow": True, "label": label,
                            "peer_id": peer.get("peer_id"), "unanswered_from_you": unans, "body_bytes": body_bytes,
                            "note_for_sender": f"the message is sent; this is a large body and {unans} of your earlier "
                                              f"messages to this recipient are unanswered. A second large send before "
                                              f"an answer is the commonest churn: consider waiting, or a pointer"})
    return out


def log(state_file: Path, *, key: str, answer: dict | None, warned: list[dict], now: float | None = None,
        body_bytes: int | None = None) -> None:
    """Append one line for a checked send. Counts only. Never raises."""
    if not isinstance(answer, dict):
        return
    try:
        rows = []
        for rec in answer.get("recipients") or []:
            for peer in (rec.get("peers") or []) if isinstance(rec, dict) else []:
                if isinstance(peer, dict):
                    rows.append({"label": rec.get("label"), "effective_state": peer.get("effective_state"),
                                 "beat_age_secs": peer.get("beat_age_secs"), "unread_count": peer.get("unread_count"),
                                 "unanswered_from_you": peer.get("unanswered_from_you")})
        line = {"t": time.time() if now is None else now, "key": key, "body_bytes": body_bytes, "recipients": rows,
                "warned": sorted({w["warning"] for w in warned})}
        with log_path(state_file).open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:                                     # noqa: BLE001 - measuring never fails a send
        pass
