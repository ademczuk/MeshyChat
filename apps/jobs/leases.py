"""Claiming a background job, and giving it back.

THE CLAIM IS ONE STATEMENT. `background_job_leases` has job_name as its PRIMARY KEY, so
there is exactly one row per job and the claim is a single conditional UPDATE. Two
workers racing for the same job serialize on that row: the loser blocks, re-evaluates
its WHERE against the winner's committed row, and matches nothing. A read-then-write
would let both observe "free" and both proceed, which is the same defect the nonce
ledger's docstring already warns about one layer down (apps/calls/nonce_ledger.py).

THE DATABASE IS THE ONLY CLOCK. Every comparison and every timestamp written here is
`now()` evaluated server-side. A worker never sends its own wall clock. That is not
tidiness: a worker whose clock runs fast would otherwise consider a live lease expired
and steal a job that is still running, and nothing in the result would say so.

CRASH RECOVERY IS THE CLAIM CONDITION, NOT A SEPARATE SWEEPER. A holder that dies stops
heartbeating, its `leased_until` passes, and the next claim matches it like any other
free job. There is no separate re-enqueue pass to forget to run. What a separate sweeper
DOES give you is a record that a takeover happened, so `claim()` reports the holder it
displaced rather than absorbing it silently: a job that is quietly taken over every tick
is a job that is crashing every tick, and that must not look like healthy scheduling.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Claim:
    """The outcome of one claim attempt.

    Typed rather than a bare bool so "I acquired it" and "I acquired it BY TAKING IT
    FROM A DEAD HOLDER" are different answers. Collapsing them loses the only in-band
    signal that a job is dying mid-run.
    """

    acquired: bool
    #: The holder whose expired lease this claim displaced, when there was one.
    took_over_from: str | None = None


def register(conn, job_name: str, *, due_now: bool = True) -> None:
    """Ensure a row exists for `job_name`. Idempotent, and never resets a live schedule.

    ON CONFLICT DO NOTHING on purpose: re-registering at boot must not drag a job's
    next_run_at back to now, or every restart would re-run every job immediately and a
    restart loop would become a hot loop over the whole job set.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO background_job_leases (job_name, next_run_at) "
            "VALUES (%s, CASE WHEN %s THEN now() ELSE now() + interval '1 hour' END) "
            "ON CONFLICT (job_name) DO NOTHING",
            (job_name, due_now),
        )


def claim(conn, job_name: str, holder: str, lease_secs: int) -> Claim:
    """Take the job if it is due and unheld (or held by an expired lease).

    Returns Claim(acquired=False) when the job is not due, is held by a live lease, or
    is not registered. Those three are deliberately one answer here: none of them is a
    reason for THIS worker to do anything except move on.
    """
    if lease_secs <= 0:
        raise ValueError("lease_secs must be > 0; a non-positive lease is born expired")
    with conn.cursor() as cur:
        # The previous holder is captured in the same statement that overwrites it.
        # Reading it first would be a different transaction's answer by the time the
        # UPDATE lands.
        cur.execute(
            "UPDATE background_job_leases AS b "
            "   SET holder = %s, "
            "       leased_until = now() + make_interval(secs => %s::double precision), "
            "       last_started_at = now() "
            "  FROM (SELECT job_name, holder AS prev_holder, leased_until AS prev_until "
            "          FROM background_job_leases WHERE job_name = %s) AS old "
            " WHERE b.job_name = old.job_name "
            "   AND b.next_run_at <= now() "
            "   AND (b.leased_until IS NULL OR b.leased_until < now()) "
            "RETURNING old.prev_holder, old.prev_until",
            (holder, lease_secs, job_name),
        )
        row = cur.fetchone()
    if row is None:
        return Claim(acquired=False)
    prev_holder, prev_until = row
    # prev_until is non-NULL only when a lease existed and had already expired, because
    # a live lease could not have matched the WHERE.
    return Claim(acquired=True, took_over_from=prev_holder if prev_until is not None else None)


def heartbeat(conn, job_name: str, holder: str, lease_secs: int) -> bool:
    """Extend OUR lease. False means we no longer hold it and must stop work.

    Scoped to `holder` so a worker that was displaced while running cannot extend a
    lease someone else now owns. A heartbeat that returns False is the signal to abort,
    not to retry.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE background_job_leases "
            "   SET leased_until = now() + make_interval(secs => %s::double precision) "
            " WHERE job_name = %s AND holder = %s "
            "RETURNING job_name",
            (lease_secs, job_name, holder),
        )
        return cur.fetchone() is not None


def release(conn, job_name: str, holder: str, *, delay_secs: float,
            result: str, error: str | None = None) -> bool:
    """Give the job back and schedule its next run. False means we did not hold it.

    `delay_secs` comes from apps/jobs/schedule.py and is already jittered. It is passed
    as a delay rather than an absolute timestamp so the schedule is computed against the
    database clock here, consistent with every other time in this module.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE background_job_leases "
            "   SET holder = NULL, "
            "       leased_until = NULL, "
            "       last_finished_at = now(), "
            # Every placeholder that is not pinned by a column type is cast explicitly.
            # Postgres cannot infer the type of a bare parameter inside `%s IS NULL`, and
            # refuses the whole statement with IndeterminateDatatype rather than guessing.
            "       next_run_at = now() + make_interval(secs => %s::double precision), "
            "       last_result = %s, "
            "       last_error = %s::text, "
            "       consecutive_failures = CASE WHEN %s::text IS NULL "
            "                                   THEN 0 "
            "                                   ELSE consecutive_failures + 1 END "
            " WHERE job_name = %s AND holder = %s "
            "RETURNING job_name",
            (delay_secs, result, error, error, job_name, holder),
        )
        return cur.fetchone() is not None


def record_code_hash(conn, job_name: str, sha256: str) -> bool:
    """Write the hash of the running code to `job_name`'s row, with the database's now()
    (sql_meshychat/022; only `run_call_worker` calls this, on the deadline sweep's row).
    Independent of the lease: a tick that did not claim the job records it all the same.
    False when the job is not registered."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE background_job_leases "
            "   SET worker_code_sha256 = %s, worker_code_at = now() "
            " WHERE job_name = %s RETURNING job_name",
            (sha256, job_name),
        )
        return cur.fetchone() is not None


def read_code_hash(conn, job_name: str) -> tuple[str | None, float | None]:
    """(the hash last recorded on `job_name`'s row, seconds since on the database clock);
    (None, None) when none was recorded or the job is not registered."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT worker_code_sha256, "
            "       EXTRACT(EPOCH FROM now() - worker_code_at)::float8 "
            "  FROM background_job_leases WHERE job_name = %s",
            (job_name,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None, None
    return row[0], float(row[1])


def read_state(conn, job_name: str) -> dict | None:
    """The row as a dict, or None when the job is not registered. Observability only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_name, holder, leased_until, next_run_at, last_started_at, "
            "       last_finished_at, last_result, last_error, consecutive_failures "
            "  FROM background_job_leases WHERE job_name = %s",
            (job_name,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def seconds_until_claimable(conn, job_name: str) -> float | None:
    """Seconds until a claim of `job_name` can match, on the DATABASE clock (the module
    docstring): until it is due AND any live lease on it has expired, the two conditions
    `claim` tests; zero or less when a claim can match now; None when it is not registered.
    What a loop that runs one job sleeps for (apps/calls/worker.py, divergence 041 S5).
    Observability only: the claim still decides.

    THE LEASE COUNTS (041 S5 review F4). A job that is due but held by another process
    cannot be claimed, and its `next_run_at` does not move until that holder releases it; a
    loop that read only `next_run_at` found it due on every tick and ticked every 0.2 s for
    as long as the holder held it (a `run_jobs` that died holding it: its whole lease).
    GREATEST ignores the NULL `leased_until` of a free job."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM GREATEST(next_run_at, leased_until) - now())::float8 "
            "  FROM background_job_leases WHERE job_name = %s", (job_name,))
        row = cur.fetchone()
    return None if row is None else float(row[0])


def seconds_since_finished(conn, job_name: str) -> float | None:
    """Seconds since any process last finished `job_name` (its `last_finished_at`, written by
    `release` whatever the result), on the DATABASE clock; None when it never finished or is
    not registered. The liveness half of `run_call_worker`'s health (041 S5 review F5)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM now() - last_finished_at)::float8 "
            "  FROM background_job_leases WHERE job_name = %s", (job_name,))
        row = cur.fetchone()
    return None if row is None or row[0] is None else float(row[0])


def force_due(conn, job_name: str) -> bool:
    """Bring a job's schedule forward to now. False when it is not registered.

    THE SAFE OPERATOR VERB. It moves `next_run_at` and touches NOTHING else, so the
    lease still decides who runs it and mutual exclusion is untouched. A job that is
    currently held stays held; this only says "you may run as soon as you are free".

    Deliberately not a "run it now" that bypasses the claim. Borrowed in spirit from
    DBOS's operator affordances (pause, resume, restart from a step), minus the part
    that would let an operator start a second concurrent copy by hand.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE background_job_leases SET next_run_at = now() "
            " WHERE job_name = %s RETURNING job_name", (job_name,))
        return cur.fetchone() is not None


def force_release(conn, job_name: str, *, force: bool = False) -> tuple[bool, str]:
    """Clear a lease by hand. Returns (released, explanation).

    THE DANGEROUS ONE, so it fails closed. A LIVE lease is refused unless `force=True`,
    because clearing one while its holder is still working is how an operator creates the
    concurrent run the whole table exists to prevent. The refusal names the holder and
    the expiry so the operator can decide rather than guess.

    An EXPIRED lease needs no force: the next claim would take it over anyway, so
    clearing it changes nothing except making the row read honestly in the meantime.
    """
    state = read_state(conn, job_name)
    if state is None:
        return False, f"{job_name!r} is not registered"
    if state["holder"] is None:
        return False, f"{job_name!r} holds no lease, nothing to release"

    # Compared in SQL, not in Python: two datetimes in one zone compare as wall-clock
    # times, so in a repeated clock-change hour a live lease could read as expired.
    with conn.cursor() as cur:
        cur.execute("SELECT leased_until IS NOT NULL AND leased_until > now() "
                    "FROM background_job_leases WHERE job_name = %s", (job_name,))
        row = cur.fetchone()
    live = bool(row and row[0])

    if live and not force:
        return False, (
            f"REFUSED: {job_name!r} is held by {state['holder']!r} until "
            f"{state['leased_until'].isoformat()}, which has NOT expired. Clearing a live "
            "lease lets a second worker start a concurrent copy of a job that is probably "
            "still running. Re-run with --force only if you know that holder is dead."
        )

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE background_job_leases SET holder = NULL, leased_until = NULL "
            " WHERE job_name = %s RETURNING job_name", (job_name,))
        ok = cur.fetchone() is not None
    kind = "LIVE (forced)" if live else "expired"
    return ok, f"released a {kind} lease previously held by {state['holder']!r}"
