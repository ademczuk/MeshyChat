"""Mesh presence: advisory peer liveness.

THE TWO WINDOWS:
  DEFAULT_STALE_AFTER_SECS  = 20   (~2x the 7s poll plus slack) -> `live`
  DEFAULT_ACTIVE_AFTER_SECS = 900                               -> `working`
Both are overridable per request via `?stale_after_secs=` / `?active_after_secs=`,
each clamped to at least 1.

THE ABANDONED-BLOCKER RULE:
The code tests the liveness of the row's own peer, not of the peer `blocked_on` names.
That reads like a bug; it is kept because clients already depend on this output, so do not
change it without a wire-compat decision.

TRUST: presence is advisory and peer-authenticated only. It is
never a trust input; a real operation still gates at the receiver.

THE FAN-OUT, over Postgres instead of Redis (divergence 020). The beat NOTIFYs
`meshy_presence_row` with the peer id only, and apps/wsgw/mesh_notify.py reads the row
back and sends the frame. Nodes subscribe to `presence:<its peer>` and process
`peer_presence` frames.

Why the id and not the frame: the note is caller-supplied and unbounded, and NOTIFY
refuses a payload of 8000 bytes or more. A frame-sized payload would make a long note
the one beat that is stored but never pushed. The id is 50 bytes whatever the note is.

A STATE CAN GO STALE (divergence 082, issue #139). A seat publishes its state from its own
hooks: `asleep` with note `compacting` before its session compacts, `active` when a session
starts. A hook can miss (a resumed session has been seen to lose one), so a beat may carry
`ttl_secs` (1..MAX_TTL_SECS) and the row keeps `state_expires_at`. The list answers
`effective_state`, which is `unknown` when the state has expired AND when the peer never beat
at all (a row `apps/calls/reads.touch_last_activity` created holds the column default
`active`, which no seat said). `state` itself is unchanged on the wire; a
beat without `ttl_secs` never expires, as before.
"""
from __future__ import annotations

import json
import logging
import uuid

import psycopg

from apps.wsgw.constants import PRESENCE_ROW_NOTIFY_CHANNEL

from . import errors
from .timefmt import format_rfc3339, now_utc

log = logging.getLogger("meshychat.peers.presence")

# Also enforced by `mesh_presence_state_check` in the schema.
STATES = ("active", "idle", "asleep")
DEFAULT_STALE_AFTER_SECS = 20
DEFAULT_ACTIVE_AFTER_SECS = 900
#: The longest a published state may claim to stay true (divergence 082): one day. A seat that is
#: still in that state re-publishes it; a state that outlives its seat's hooks reads `unknown`.
MAX_TTL_SECS = 86400
UNKNOWN = "unknown"
# Bounded like the other "list everything" exports.
LIST_LIMIT = 5000


def beat(conn, peer_id, body: dict) -> dict:
    """Handle `POST /v1/peers/me/presence`.

    `peer_id` comes from the TOKEN, never the body, so a peer can only write its own
    row. That is the whole authorisation story for this table (it carries no RLS
    policy), so the caller must pass an authenticated id.
    """
    state = body.get("state", "active")           # Missing state defaults to active.
    if state is None:
        state = "active"
    if state not in STATES:
        raise errors.validation(f"invalid state: {state}")

    blocked_on = body.get("blocked_on")
    if blocked_on is not None:
        try:
            blocked_on = str(uuid.UUID(str(blocked_on)))
        except (ValueError, AttributeError, TypeError):
            raise errors.validation(f"invalid blocked_on: {blocked_on}")
        # A peer cannot be blocked on itself.
        if blocked_on == str(peer_id):
            raise errors.validation("cannot be blocked on self")

    note = body.get("note")

    ttl = body.get("ttl_secs")
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int)
                            or not 1 <= ttl <= MAX_TTL_SECS):
        raise errors.validation(f"invalid ttl_secs: must be an integer within 1..{MAX_TTL_SECS}")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mesh_presence (peer_id, state, blocked_on, note, last_beat, updated_at, "
            "  state_expires_at) "
            "VALUES (%s,%s,%s,%s,NOW(),NOW(), "
            "  CASE WHEN %s::int IS NULL THEN NULL ELSE NOW() + make_interval(secs => %s::int) END) "
            "ON CONFLICT (peer_id) DO UPDATE SET "
            "  state = EXCLUDED.state, blocked_on = EXCLUDED.blocked_on, "
            "  note = EXCLUDED.note, last_beat = NOW(), "
            "  state_expires_at = EXCLUDED.state_expires_at",
            (str(peer_id), state, blocked_on, note, ttl, ttl))
    _nudge_presence_row(conn, peer_id)
    return {"ok": True}


def _nudge_presence_row(conn, peer_id) -> None:
    """Notify subscribers after a presence upsert (divergence 020).

    A SEPARATE statement from the upsert, and a failure here is logged, never raised:
    The row is already written, so the beat still answers `{"ok": true}` on a publish
    failure. The row is the durable fact; the push is advisory.
    The connection is autocommit, so the NOTIFY commits, and is delivered, at once.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_notify(%s, %s)",
                        (PRESENCE_ROW_NOTIFY_CHANNEL, json.dumps({"peer_id": str(peer_id)})))
    except psycopg.Error as exc:
        log.warning("presence beat nudge failed for %s: %s", peer_id, exc)


def states_of(conn, peer_ids) -> dict[str, dict]:
    """Presence for a set of peers, for the sender's recipient check (divergence 083): per peer id,
    `effective_state` (the list's rule: `unknown` past the expiry and for a peer that never beat),
    `note`, `beat_age_secs` (None when it never beat) and `state_expires_at`. A peer with no row
    is absent from the answer; the caller reads that as never beat."""
    ids = [str(p) for p in peer_ids]
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT peer_id, "
            "       CASE WHEN last_beat IS NULL THEN 'unknown' "
            "            WHEN state_expires_at IS NOT NULL AND state_expires_at <= NOW() "
            "            THEN 'unknown' ELSE state END, "
            "       note, "
            "       CASE WHEN last_beat IS NULL THEN NULL "
            "            ELSE EXTRACT(EPOCH FROM (NOW() - last_beat))::bigint END, "
            "       state_expires_at "
            "FROM mesh_presence WHERE peer_id = ANY(%s::uuid[])", (ids,))
        rows = cur.fetchall()
    return {str(pid): {"effective_state": eff, "note": note, "beat_age_secs": age,
                       "state_expires_at": None if exp is None else format_rfc3339(exp)}
            for pid, eff, note, age, exp in rows}


def _window(raw, default: int) -> int:
    """Return a request window, defaulting when omitted and clamping to at least 1.

    A non-integer query value is invalid and rejects the request, so it is a 400 rather
    than a silent fall back to the
    default. Falling back would make a typo look like a working request.
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise errors.validation(f"invalid window value: {raw!r}")
    return max(value, 1)


def list_presence(conn, stale_after_secs=None, active_after_secs=None) -> dict:
    """Handle `GET /v1/peers/presence`."""
    stale_after = _window(stale_after_secs, DEFAULT_STALE_AFTER_SECS)
    active_after = _window(active_after_secs, DEFAULT_ACTIVE_AFTER_SECS)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT peer_id, state, blocked_on, note, last_beat, last_activity, "
            "       CASE WHEN last_beat IS NULL THEN 'unknown' "
            "            WHEN state_expires_at IS NOT NULL AND state_expires_at <= NOW() "
            "            THEN 'unknown' ELSE state END AS effective_state, "
            "       state_expires_at, "
            "       CASE WHEN last_beat IS NULL THEN NULL "
            "            ELSE EXTRACT(EPOCH FROM (NOW() - last_beat))::bigint END AS age_secs, "
            "       CASE WHEN last_activity IS NULL THEN NULL "
            "            ELSE EXTRACT(EPOCH FROM (NOW() - last_activity))::bigint "
            "       END AS activity_age_secs "
            "FROM mesh_presence ORDER BY last_beat DESC NULLS LAST LIMIT %s", (LIST_LIMIT,))
        rows = cur.fetchall()

    # First pass: determine which peers are live.
    live_set = set()
    for pid, state, bo, note, lb, la, eff, exp, age, activity_age in rows:
        if age is not None and age <= stale_after:
            live_set.add(pid)

    # Second pass: apply the abandoned-blocker rule.
    out = []
    for pid, state, bo, note, lb, la, eff, exp, age, activity_age in rows:
        live = pid in live_set
        working = activity_age is not None and activity_age <= active_after
        out.append({
            "peer_id": str(pid),
            "state": state,
            # Divergence 082: what a reader should believe. `unknown` past the expiry the seat
            # gave, and for a peer that never beat.
            "effective_state": eff,
            "state_expires_at": None if exp is None else format_rfc3339(exp),
            "blocked_on": (str(bo) if bo is not None else None) if live else None,
            "note": note,
            "last_beat": None if lb is None else format_rfc3339(lb),
            "last_activity": None if la is None else format_rfc3339(la),
            "live": live,
            "age_secs": age,
            "working": working,
            "activity_age_secs": activity_age,
        })

    # The verdict is itself a time-sensitive fact, so it
    # carries its own provenance. A reader that caches the list without `computed_at`
    # cannot tell a fresh verdict from an hour-old one.
    return {
        "presence": out,
        "stale_after_secs": stale_after,
        "active_after_secs": active_after,
        "computed_at": format_rfc3339(now_utc()),
        "verified_via": "server-staleness-rule",
        "probed_by": "meshy-server",
    }
