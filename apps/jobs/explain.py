"""Why is this job not running? A named reason, and every clause that was checked.

ONE AUTHORITY, NO RACING HEURISTICS. The single authority is the WHERE clause in
`leases.claim`. This module must never become a second opinion about it.

A NAMED FALLBACK REASON, AS A CONSTANT IN CODE. The reason constants below make "we do
not know" a queryable value rather than an implicit default.

EXPLAIN KEEPS THE LOSERS. `evaluated` holds every rule the engine checked whether it
matched or not. Explain is the engine's ordinary output with non-matching rules kept
instead of discarded, so an operator can ask "why did clause X NOT fire" without
re-running anything. A first-blocker-only explanation cannot answer that: it reports the
schedule blocked the job and says nothing about whether the lease was also a problem, so
the next question needs another round trip.

WHAT THIS MODULE DECIDES: NOTHING. It reads the same row `claim` filters on and reports
which clause would reject. A second implementation of the claim predicate that drifts
from the first is worse than no explanation, because it describes a decision the
scheduler did not make. `tests/test_jobs_explain.py` drives both over the same states and
requires them to agree, so drift fails rather than misleads.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt

from apps.jobs import leases

#: Named reasons. Strings rather than an enum so they survive JSON, a log line, and a
#: paste into a chat window without needing this module to interpret them.
NOT_REGISTERED = "not_registered"
HELD_BY_ANOTHER = "held_by_another"
NOT_DUE = "not_due"
DUE_NOW = "due_now"

#: How long a job may sit claimable before that is itself the finding. A job that has
#: been due for many minutes is not a job problem: nothing is ticking, or the tick is
#: starved. Without this an operator reads "due_now" as healthy.
STALE_DUE_SECS = 300


@dataclasses.dataclass(frozen=True)
class Clause:
    """One condition the claim predicate evaluates, and how it came out.

    Kept even when it does not block, which is the point. `blocks=False` on the lease
    clause is the answer to "is something holding it", and that answer has to exist
    without re-running anything.
    """

    name: str
    blocks: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class Explanation:
    """What the scheduler would do with this job right now, and why.

    `runnable` is the verdict, `reason` is the named cause, `detail` is human text that
    may change freely, and `evaluated` is every clause including the ones that passed.
    Callers branch on `reason`, never on `detail`, so improving wording cannot break one.
    """

    job: str
    runnable: bool
    reason: str
    detail: str
    evaluated: tuple[Clause, ...] = ()
    holder: str | None = None
    next_run_at: _dt.datetime | None = None
    consecutive_failures: int = 0
    last_result: str | None = None
    last_error: str | None = None
    #: Not due AND carrying a failure streak, so the delay is backoff rather than the
    #: ordinary interval. A MODIFIER, not a fifth reason: the claim predicate cannot tell
    #: those apart, and inventing a reason the scheduler cannot act on is how an
    #: explainer starts lying.
    in_backoff: bool = False
    #: Seconds this job has been claimable without being claimed. Only meaningful when
    #: runnable is True.
    due_for_secs: float | None = None
    #: due_for_secs has exceeded STALE_DUE_SECS, so nothing is ticking or the tick is
    #: starved. The job is fine; the worker is the finding.
    nothing_is_ticking: bool = False
    #: Registered but never started even once. Distinct from "ran and is now waiting",
    #: which the timestamps alone do not separate for a reader.
    never_run: bool = False


def _utc(value):
    """An aware database time in UTC (None and naive values unchanged). Python compares and subtracts two datetimes of
    one zone on their WALL clock, so in a session zone with summer time a lease an hour past `now` across the autumn
    change (02:30 CEST, then 02:30 CET) read as already expired (codex-pxx-cli TASK AL, mg391 on 25 Oct)."""
    return value.astimezone(_dt.timezone.utc) if value is not None and value.tzinfo is not None else value


def explain(conn, job_name: str, *, now=None) -> Explanation:
    """Read the lease row and name the clause that decides the outcome.

    `now` is accepted for tests only. It defaults to the DATABASE clock, because every
    comparison in leases.py is server-side and an explanation computed against a Python
    clock could disagree with the scheduler it claims to describe.
    """
    state = leases.read_state(conn, job_name)
    if state is None:
        return Explanation(
            job=job_name, runnable=False, reason=NOT_REGISTERED,
            detail=("no row in background_job_leases. The scheduler creates one on its "
                    "first tick, so this also means the job has never been ticked."),
            evaluated=(Clause("registered", True, "no row for this job name"),),
        )

    if now is None:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            now = cur.fetchone()[0]

    now = _utc(now)
    holder = state["holder"]
    leased_until = _utc(state["leased_until"])
    next_run_at = _utc(state["next_run_at"])
    failures = int(state["consecutive_failures"] or 0)

    # Every clause is evaluated and KEPT, matched or not, mirroring the order of the
    # WHERE clause in leases.claim. The first blocker becomes the reason; the rest stay
    # visible so the next question does not need another round trip.
    held = leased_until is not None and leased_until > now
    stale_lease = holder is not None and leased_until is not None and leased_until <= now
    not_due = next_run_at is not None and next_run_at > now

    clauses = (
        Clause("registered", False, "row exists"),
        Clause(
            "lease_free", held,
            f"held by {holder!r} until {leased_until.isoformat()}" if held
            else (f"lease from {holder!r} EXPIRED at {leased_until.isoformat()}, "
                  "so the next claim takes it over" if stale_lease else "not held"),
        ),
        Clause(
            "due", not_due,
            f"next_run_at {next_run_at.isoformat()} is in the future" if not_due
            else f"next_run_at {next_run_at.isoformat()} has passed",
        ),
    )

    common = dict(job=job_name, evaluated=clauses, holder=holder,
                  next_run_at=next_run_at, consecutive_failures=failures,
                  last_result=state["last_result"], last_error=state["last_error"],
                  never_run=state["last_started_at"] is None)

    if held:
        return Explanation(
            runnable=False, reason=HELD_BY_ANOTHER,
            detail=(f"held by {holder!r} until {leased_until.isoformat()}. A lease that "
                    "is still live blocks the claim even when the job is otherwise due."),
            **common,
        )

    if not_due:
        wait = (next_run_at - now).total_seconds()
        backoff = failures > 0
        return Explanation(
            runnable=False, reason=NOT_DUE,
            detail=(f"not due for {wait:.0f}s, at {next_run_at.isoformat()}"
                    + (f". {failures} consecutive failures, so this delay is backoff "
                       "rather than the ordinary interval." if backoff else ".")),
            in_backoff=backoff, **common,
        )

    overdue = (now - next_run_at).total_seconds() if next_run_at is not None else 0.0
    starved = overdue > STALE_DUE_SECS
    return Explanation(
        runnable=True, reason=DUE_NOW,
        detail=(
            f"due now, claimable for {overdue:.0f}s"
            + (f", and the next claim will TAKE OVER an expired lease from {holder!r}, "
               "which means that holder did not release it" if stale_lease else "")
            + (f". It has been claimable for longer than {STALE_DUE_SECS}s, so nothing "
               "is ticking run_due or the tick is starved. That is a WORKER finding, "
               "not a job finding." if starved else ".")
        ),
        due_for_secs=overdue, nothing_is_ticking=starved, **common,
    )


def explain_all(conn, jobs) -> list[Explanation]:
    """One Explanation per registered job, in registry order, never a filtered subset.

    Every job appears including the runnable ones, so "checked and it is fine" is
    distinguishable from "this job was not examined".
    """
    return [explain(conn, job.name) for job in jobs]
