"""The audit event a call's transition writes: one row of `skill_call_events`.

`insert_event` replaces the six copies of the one statement apps/calls spelled out: route.py's
four guarded transitions (`routed`/`queued`, `running`, an unexecuted `failed`, and the signed
terminal), repository.py's `created`, reads.py's `caller_disconnected` and webhooks.py's
`webhook_fired`. Each caller writes the row it wrote before: its event_type; `details` as jsonb
when it gives one and NULL when it does not (never `{}`, a different stored value); `source`
the server's own compatibility value `'meshy'`, unless the caller names another; and no
`source_peer_id` unless it names one. `source` is one of the three values the live CHECK
constraint permits ('caller', 'target', 'meshy').

It runs on the caller's cursor, inside the transaction that holds the transition it records,
and commits nothing itself: a transition and its event land together or not at all.
`skill_call_events` has no row-level security (reads.py, MEASURED), so the row needs no context
beyond the caller's.

Two writers keep their own shape, on purpose: the deadline sweep (apps/calls/janitor.py) writes
its `timeout` events from a CTE in the one statement that times the calls out, and the WebSocket
target path (apps/wsgw/target_frames.py `_event`) writes with source 'target'.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb


def insert_event(cur, call_id: str, event_type: str, *, details: dict | None = None,
                 source: str = "meshy", source_peer_id=None) -> None:
    """One `skill_call_events` row on `cur`, in the caller's transaction."""
    cur.execute(
        "INSERT INTO skill_call_events (call_id, event_type, details, source, "
        "  source_peer_id) VALUES (%s, %s, %s, %s, %s)",
        (call_id, event_type, None if details is None else Jsonb(details), source,
         source_peer_id))


__all__ = ["insert_event"]
