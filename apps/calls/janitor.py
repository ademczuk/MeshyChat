"""The deadline janitor: no call stays in flight past its deadline. docs/DIVERGENCES.md 017.

Without it a call whose target never answers
stays `pending` / `running` forever and a polling caller waits until its OWN timeout,
which is the state every call in this server was left in before step 9 existed.

WHAT IT DOES, per row: an in-flight call in the six swept statuses, including the two
interactive-pause states (TASK-28 AC#1), whose `deadline_at` has passed becomes
`timeout` with `completed_at = NOW()`, and gets a `timeout` event in the SAME transaction.

THREE DESIGN RULES:

   1. BOUNDED. Without a LIMIT, the first sweep after an outage updates the whole backlog
      in one statement. The job declares `max_batch` (rule B5, enforced
     by apps/jobs/registry.py) and the sweep takes that many rows, oldest deadline first,
     through `skill_calls_deadline_sweep_idx` - the partial index whose predicate is
     exactly this status list, so the bound is an index range scan, not a sort.
   2. THE EVENT IS ATOMIC WITH THE TRANSITION. One statement writes both, so a crash cannot
      leave a timed-out call with no audit row.
   3. THE TIMEOUT IS TYPED. `error_code` is `E_DEADLINE_EXCEEDED`, so timeout handling does
      not fall back to an unknown error, and the
     message names the status the call was stuck in, read from the row being updated.

`FOR UPDATE SKIP LOCKED` is kept: two workers sweeping at once each take different rows,
and a row locked by an in-flight guarded transition (a completion, a cancel) is skipped
rather than waited on; the next tick sees it if it is still expired.

THE TRIM AT TERMINAL (divergence 041 S7, design 5.2, and the S7 review's H1). Each call the
sweep times out keeps its newest 200 chunks (`apps/calls/chunks.trim`) in this transaction. ONE
STATEMENT PER CALL, EACH UNDER A SAVEPOINT: a trim that fails (its statement timeout, a lock) is rolled back
to its savepoint and logged, and the batch's timeouts and the other calls' trims commit. S7 first
trimmed the whole batch in one statement, and the review measured the result: one call whose trim
could not finish inside the job's 10 s rolled back every timeout of the batch, and since the batch
is taken oldest deadline first, the same call headed every tick and no call timed out again. A
call whose trim failed keeps its chunks (at most `chunks.MAX_CHUNKS_PER_CALL`); nothing retries
it, and the log line names it.
"""
from __future__ import annotations

import logging

import psycopg

from apps.calls import chunks as call_chunks
from apps.core import rls

log = logging.getLogger("meshychat.calls.janitor")

#: Typed timeout error code.
E_DEADLINE_EXCEEDED = "E_DEADLINE_EXCEEDED"

#: The health signal's threshold (divergence 041 S5, design 4.10 (c), rule B9): an in-flight
#: call more than this far past its deadline means the sweep, run every 5 s by
#: `run_call_worker` (apps/calls/worker.py), is not running or not keeping up. Six ticks.
OVERDUE_ALERT_SECS = 30

#: The predicate of `skill_calls_deadline_sweep_idx`.
SWEPT_STATUSES = ("pending", "routed", "queued", "running", "input_required",
                  "auth_required")


def sweep_expired_deadlines(conn, *, limit: int) -> list[str]:
    """Time out up to `limit` expired in-flight calls. Returns their call_ids.

    A caller that gets back exactly `limit` ids should assume more remain; the job
    runner reads it that way (`Outcome.more_work_pending`). `limit <= 0` is refused: a
    zero bound is a sweep that silently does nothing.
    """
    if limit <= 0:
        raise ValueError("limit must be > 0; a zero bound would be a silent no-op")
    statuses = list(SWEPT_STATUSES)
    # server_internal: skill_calls is FORCE-RLS and its SELECT policy is peer-scoped, so
    # under meshychat_app with no context the CTE would match ZERO rows and report a clean,
    # empty sweep.
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "WITH expired AS ("
                "  SELECT id FROM skill_calls "
                "   WHERE status = ANY(%s::text[]) AND deadline_at < NOW() "
                "   ORDER BY deadline_at "
                "   LIMIT %s "
                "   FOR UPDATE SKIP LOCKED"
                "), swept AS ("
                "  UPDATE skill_calls s "
                "     SET status = 'timeout', completed_at = NOW(), error_code = %s, "
                # In an UPDATE's SET list a column reference is the row's PRIOR value, so
                # this records the status the call was stuck in, not 'timeout'.
                "         error_message = 'deadline ' || "
                "           to_char(s.deadline_at AT TIME ZONE 'UTC', "
                "                   'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') || "
                "           ' passed while the call was ' || s.status "
                "    FROM expired e WHERE s.id = e.id "
                "  RETURNING s.call_id"
                ") "
                "INSERT INTO skill_call_events (call_id, event_type, details, source, "
                "  source_peer_id) "
                "SELECT call_id, 'timeout', NULL, 'meshy', NULL FROM swept "
                "RETURNING call_id",
                (statuses, limit, E_DEADLINE_EXCEEDED))
            swept = [r[0] for r in cur.fetchall()]
        # The trim at terminal, one statement per call, each under a savepoint (module
        # docstring): a trim that fails undoes neither the timeouts nor another call's trim.
        for call_id in swept:
            try:
                with conn.transaction():
                    with conn.cursor() as cur:
                        call_chunks.trim(cur, call_id)
            except psycopg.Error as exc:
                log.warning("deadline sweep: the chunk trim of call %r failed and was rolled "
                            "back; the call is timed out and keeps its chunks: %s: %s",
                            call_id, type(exc).__name__, exc)
        return swept


def oldest_overdue_secs(conn) -> float | None:
    """How far past its deadline the oldest in-flight call is, in seconds on the database
    clock; None when no in-flight call is past its deadline. The health signal of divergence
    041 S5 (design 4.10 (c), rule B9). It reads the sweep's OWN predicate (the six statuses,
    `deadline_at < now()`), so it measures the work the sweep has left, through the same
    partial index (`min` over `skill_calls_deadline_sweep_idx`, not a scan).

    server_internal for the reason `sweep_expired_deadlines` gives: under meshychat_app with no
    context the table reads empty, and "nothing overdue" would be a lie."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM now() - min(deadline_at))::float8 "
                "  FROM skill_calls WHERE status = ANY(%s::text[]) AND deadline_at < now()",
                (list(SWEPT_STATUSES),))
            value = cur.fetchone()[0]
    return None if value is None else float(value)


def run_deadline_sweep_job(conn, job) -> int:
    """The registered job body (apps/jobs/registry.py). Returns rows swept."""
    return len(sweep_expired_deadlines(conn, limit=job.max_batch))


__all__ = ["E_DEADLINE_EXCEEDED", "OVERDUE_ALERT_SECS", "SWEPT_STATUSES",
           "oldest_overdue_secs", "run_deadline_sweep_job", "sweep_expired_deadlines"]
