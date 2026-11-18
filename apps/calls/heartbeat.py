"""A heartbeat on `running` calls (STATUS F09; divergence 017's "Known and not fixed").

THE GAP. A call is `running` from the target's `skill_running` frame until its result, its
error, a cancel or the deadline sweep. If the target's process dies in between, nothing
distinguishes the call from a slow one until `deadline_at` passes, and a caller polling it
waits the whole deadline.

WHAT A HEARTBEAT IS HERE. The target's presence: a worker holding the target's socket beats
its presence row every 10 s (divergence 041 S2, `apps.wsgw.hub_presence`), and the row reads
online for `ONLINE_WINDOW_SECS` (30 s) after its last beat; a socket that closes deletes its
row, and a crashed worker's rows age out. So a running call whose target is online now is
alive as far as the server can tell, and one whose target has not been online for
`LOST_AFTER_SECS` is LOST. No frame is added to the wire and no client changes: the target's
socket already beats. The job `running_call_heartbeat` records it per call in
`skill_call_heartbeats` (sql_meshychat/023): `beat_at`, the last tick that found the target
online (or, before the first such tick, the call's `started_at`), and `lost_at`, the tick that
first found it absent past the window. A target that comes back clears `lost_at`.

WHAT A LOST CALL BECOMES (divergence 069; the operator's ruling D-2026-10-03-1, ASDLC OI-66).
A lost call is logged at WARNING once, when it is first found, and `lost_calls` lists it. If
its target is STILL absent `lost_grace_secs()` after the call was marked lost (default
`LOST_GRACE_SECS`, setting `MESHY_LOST_CALL_GRACE_SECS`), the call FAILS: status `failed`,
`error_code` `E_TARGET_UNREACHABLE`, a message naming when the loss was first seen, and a
`failed` event with the compatibility source value `meshy`, all in one statement, as
the deadline janitor writes a timeout. So a caller polling it learns within about
`LOST_AFTER_SECS + LOST_GRACE_SECS` of the target's last beat, not at the deadline. A target
that comes back within the grace period clears the mark, as before, and the call runs on.
What failing the call early costs, chosen by the ruling: a target that was only cut off (its process alive, its
socket gone for four minutes) and then sends a result
finds the call already failed, and its completion is refused like any completion of a
terminal call; the call is not re-queued, so a skill never runs twice because of this job.

WHICH CALLS. `running` only: a `queued` or `routed` call's target never started it, and its
delivery has its own paths (redelivery, the re-offer sweep, the doorbell). Not the memory
authority's: its calls run in-process inside `start_call` (apps/calls/route.py) and it holds
no socket, so presence says nothing about them.

BOUNDED (B5). A tick reads at most `max_batch` running calls, by (deadline_at, call_id) past a
cursor in `background_job_cursors`, through `skill_calls_deadline_sweep_idx` (status,
deadline_at), and reports more pending until a round has visited every running call; the
runner then brings the next tick forward (FOLLOW_UP_SECS). One presence read per distinct
target. `LOST_AFTER_SECS` must exceed a round: at the shared loop's 30 s cadence and 1000
calls a tick, a round of up to 4000 running calls fits inside it, and more than that can
read a target that blinked offline as lost (the log and `lost_calls` say so; nothing acts).

Every read and write of `skill_calls` and `skill_call_heartbeats` runs under
server_internal, which both require (sql/016, sql_meshychat/012, 023).
"""
from __future__ import annotations

import dataclasses
import logging

import psycopg
from psycopg.rows import dict_row

from apps.calls import chunks as call_chunks
from apps.core import env, rls
from apps.jobs import cursors
from apps.jobs.result import TickResult
from apps.wsgw.hub_presence import ONLINE_WINDOW_SECS, is_online, online_condition

log = logging.getLogger("meshychat.calls.heartbeat")

#: How long a running call's target may be absent before the call is LOST. Four online
#: windows: a reconnecting socket (15 s at most, the client's backoff cap) and a worker
#: restart both fit, and a round of the job at the shared loop's cadence does too.
LOST_AFTER_SECS = 4 * ONLINE_WINDOW_SECS
#: How long a call stays lost before it fails (divergence 069). Four online windows again, so
#: a target is given eight in all from its last beat: a worker restart, a reboot of a node
#: that resumes its socket, or a network blip all fit, and a caller still learns in minutes
#: rather than at a deadline that may be an hour away.
LOST_GRACE_SECS = 4 * ONLINE_WINDOW_SECS
#: Code for a target the server cannot reach.
E_TARGET_UNREACHABLE = "E_TARGET_UNREACHABLE"
#: Running calls read per tick (B5).
PER_TICK = 1000
CURSOR = "call_heartbeat.running"


def lost_grace_secs() -> int:
    """`MESHY_LOST_CALL_GRACE_SECS`, read at call time through `env.setting`: a setting of that
    name wins, else the environment variable, else `LOST_GRACE_SECS`. (`settings.py` defines no
    such setting, so the environment variable is what an operator exports.) Anything but a
    positive whole number is refused, loudly: a zero or negative grace would fail a call on the
    tick that first finds it lost, and a value nobody can parse must not become a default
    silently."""
    raw = env.setting("MESHY_LOST_CALL_GRACE_SECS", "MESHY_LOST_CALL_GRACE_SECS")
    if raw is None or raw == "":
        return LOST_GRACE_SECS
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"MESHY_LOST_CALL_GRACE_SECS must be a whole number of seconds, "
                         f"got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"MESHY_LOST_CALL_GRACE_SECS must be > 0, got {value}")
    return value


@dataclasses.dataclass(frozen=True)
class Report:
    checked: int
    beating: int
    newly_lost: tuple[str, ...]
    returned: tuple[str, ...]
    #: Lost past the grace period with the target still absent: now `failed` (divergence 069).
    failed: tuple[str, ...]
    #: The round is not finished: running calls past the cursor remain.
    more: bool


def _read_cursor(conn) -> tuple | None:
    """The (deadline, call_id) the last tick stopped at, from the cursor's `deadline|call_id`
    key; None when a round starts (no cursor yet, or a tick that finished the last round)."""
    row = cursors.read(conn, CURSOR)
    if not row or not row["position_key"]:
        return None
    deadline, _, call_id = row["position_key"].partition("|")
    return deadline, call_id


def beat(conn, *, limit: int = PER_TICK, memory_peer_id=None) -> Report:
    """One tick. `conn`: autocommit, no open transaction (rls.server_internal)."""
    if limit <= 0:
        raise ValueError("limit must be > 0; a zero bound would be a silent no-op")
    if memory_peer_id is None:
        from apps.calls.route import memory_peer_id_from_settings
        memory_peer_id = memory_peer_id_from_settings()
    grace = lost_grace_secs()

    with rls.server_internal(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            after = _read_cursor(conn)
            cur.execute(
                "SELECT c.call_id, c.target_peer_id, c.deadline_at, "
                "       h.lost_at, "
                "       (h.lost_at < now() - make_interval(secs => %s)) AS grace_over, "
                "       (COALESCE(h.beat_at, c.started_at, c.created_at) "
                "          < now() - make_interval(secs => %s)) AS stale "
                "  FROM skill_calls c "
                "  LEFT JOIN skill_call_heartbeats h ON h.call_id = c.call_id "
                " WHERE c.status = 'running' AND c.target_peer_id <> %s "
                "   AND (%s::timestamptz IS NULL "
                "        OR (c.deadline_at, c.call_id) > (%s::timestamptz, %s)) "
                " ORDER BY c.deadline_at, c.call_id "
                " LIMIT %s",
                (grace, LOST_AFTER_SECS, memory_peer_id, after and after[0], after and after[0],
                 after and after[1], int(limit)))
            rows = cur.fetchall()

    online = {t: is_online(conn, t) for t in {r["target_peer_id"] for r in rows}}

    alive = [r["call_id"] for r in rows if online[r["target_peer_id"]]]
    returned = [r["call_id"] for r in rows
                if online[r["target_peer_id"]] and r["lost_at"] is not None]
    lost = [r["call_id"] for r in rows
            if not online[r["target_peer_id"]] and r["stale"] and r["lost_at"] is None]
    expired = [r["call_id"] for r in rows
               if not online[r["target_peer_id"]] and r["lost_at"] is not None
               and r["grace_over"]]
    more = len(rows) >= limit
    with rls.server_internal(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            # Only a call still `running` is written: one that ended since the read is left
            # alone (its row, if any, goes with the call).
            if alive:
                cur.execute(
                    "INSERT INTO skill_call_heartbeats (call_id, beat_at, lost_at) "
                    "SELECT c.call_id, now(), NULL FROM skill_calls c "
                    " WHERE c.call_id = ANY(%s) AND c.status = 'running' "
                    "ON CONFLICT (call_id) DO UPDATE SET beat_at = now(), lost_at = NULL",
                    (alive,))
            if lost:
                cur.execute(
                    "INSERT INTO skill_call_heartbeats (call_id, beat_at, lost_at) "
                    "SELECT c.call_id, COALESCE(c.started_at, c.created_at), now() "
                    "  FROM skill_calls c WHERE c.call_id = ANY(%s) AND c.status = 'running' "
                    "ON CONFLICT (call_id) DO UPDATE SET lost_at = now() "
                    "WHERE skill_call_heartbeats.lost_at IS NULL",
                    (lost,))
            failed = _fail_expired(cur, expired, grace) if expired else []
            last = rows[-1] if more else None
            key = f"{last['deadline_at'].isoformat()}|{last['call_id']}" if last else None
            cursors.save(conn, CURSOR, key=key)
    # The trim at terminal, as the deadline janitor does for each call it ends (janitor.py
    # docstring): one statement per call under a savepoint, so a failed trim undoes neither
    # the failures nor another call's trim.
    for call_id in failed:
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    call_chunks.trim(cur, call_id)
        except psycopg.Error as exc:
            log.warning("call heartbeat: the chunk trim of failed call %r was rolled back; the "
                        "call is failed and keeps its chunks: %s: %s",
                        call_id, type(exc).__name__, exc)
    for call_id in lost:
        log.warning("running call %s: its target has held no live socket for more than %d s; "
                    "it fails in %d s unless the target returns (divergence 069)",
                    call_id, LOST_AFTER_SECS, grace)
    for call_id in failed:
        log.warning("running call %s failed: its target stayed unreachable for %d s after the "
                    "call was marked lost (%s, divergence 069)", call_id, grace,
                    E_TARGET_UNREACHABLE)
    for call_id in returned:
        log.info("running call %s: its target is online again", call_id)
    return Report(checked=len(rows), beating=len(alive), newly_lost=tuple(lost),
                  returned=tuple(returned), failed=tuple(failed), more=more)


def _fail_expired(cur, call_ids: list[str], grace: int) -> list[str]:
    """End the lost calls whose grace is over: `failed`, `E_TARGET_UNREACHABLE`, and a `failed`
    event, in one statement (the deadline janitor's shape, janitor.py departure 2). `cur` is a
    dict_row cursor inside rls.server_internal. The guard
    is re-read under the row lock, not trusted from the tick's read: still `running` (a
    completion or a cancel since the read wins), still lost past the grace (a target that
    returned and was beaten since clears `lost_at`), AND the target holds no live socket NOW
    (`online_condition`, the rule `is_online` runs: a target that reconnected after the tick's
    presence read is not failed while online). SKIP LOCKED: a call locked by an in-flight
    transition is left for the next tick."""
    cur.execute(
        "WITH due AS ("
        "  SELECT c.id, h.lost_at FROM skill_calls c "
        "    JOIN skill_call_heartbeats h ON h.call_id = c.call_id "
        "   WHERE c.call_id = ANY(%s) AND c.status = 'running' "
        "     AND h.lost_at < now() - make_interval(secs => %s) "
        "     AND NOT " + online_condition("c.target_peer_id") + " "
        "   FOR UPDATE OF c SKIP LOCKED"
        "), ended AS ("
        "  UPDATE skill_calls s "
        "     SET status = 'failed', completed_at = now(), error_code = %s, "
        "         error_message = 'target unreachable: no live socket since before ' || "
        "           to_char(d.lost_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') || "
        "           ', and none within ' || %s::text || ' s after the call was marked lost' "
        "    FROM due d WHERE s.id = d.id "
        "  RETURNING s.call_id, s.error_code, s.error_message"
        ") "
        # The event's details as every other server-written failure gives them
        # (apps/calls/route.py _fail_unexecuted): {code, message}.
        "INSERT INTO skill_call_events (call_id, event_type, details, source, source_peer_id) "
        "SELECT call_id, 'failed', jsonb_build_object('code', error_code, 'message', "
        "       error_message), 'meshy', NULL FROM ended "
        "RETURNING call_id",
        (call_ids, grace, ONLINE_WINDOW_SECS, E_TARGET_UNREACHABLE, grace))
    return [r["call_id"] for r in cur.fetchall()]


def lost_calls(conn) -> list[dict]:
    """Every `running` call the heartbeat has marked lost, oldest loss first. The health
    read an operator (or a later decision) acts on."""
    with rls.server_internal(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT c.call_id, c.target_peer_id, c.started_at, c.deadline_at, h.beat_at, "
                "       h.lost_at "
                "  FROM skill_call_heartbeats h JOIN skill_calls c ON c.call_id = h.call_id "
                " WHERE h.lost_at IS NOT NULL AND c.status = 'running' "
                " ORDER BY h.lost_at, c.call_id LIMIT %s", (PER_TICK,))
            return list(cur.fetchall())


def run_job(conn, job) -> TickResult:
    """The registered body of `running_call_heartbeat` (apps/jobs/registry.py)."""
    report = beat(conn, limit=job.max_batch)
    return TickResult(rows=report.checked, more_work_pending=report.more)


__all__ = ["E_TARGET_UNREACHABLE", "LOST_AFTER_SECS", "LOST_GRACE_SECS", "PER_TICK", "Report",
           "beat", "lost_calls", "lost_grace_secs", "run_job"]
