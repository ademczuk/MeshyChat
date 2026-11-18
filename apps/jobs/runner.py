"""One tick: claim what is due, run it on its own connection, give it back.

E7, THE RULE THIS FILE EXISTS TO OBEY. "Background work never shares the request pool."
An hourly full-table audit on the request-serving pool with no yield and no timeout can
stall the fleet for 2.5 minutes every hour. So `run_due` takes a
CONNECTION FACTORY, never a connection. There is no parameter through which a caller can
hand it a live request's connection, which makes the rule structural instead of a line
in a docstring that a future caller has to read.

THE TIMEOUT IS THE DATABASE'S, NOT PYTHON'S. Each job body runs on a connection opened
with `statement_timeout` already set, so an overrunning query is cancelled by the server.
A Python-side wall-clock check cannot interrupt a blocking driver call; it can only
notice afterwards, which is exactly when the damage is done.

TWO CONNECTIONS PER CLAIMED JOB, ON PURPOSE. The control connection claims, heartbeats
and releases; the work connection runs the body under the tight timeout. Sharing one
would put release() under the job's timeout, so a job that times out would also fail to
record that it timed out, and the lease would lapse instead of being returned with its
error. The failure would then read as a crash rather than as a timeout.
"""
from __future__ import annotations

import dataclasses
import logging
import socket
import os
import time
import uuid

from apps.jobs import leases, registry, schedule
from apps.jobs.result import TickResult

log = logging.getLogger(__name__)


def default_holder() -> str:
    """Identifies WHICH worker holds a lease, stably within a process.

    host and pid so a stuck lease can be traced to a machine and a process; a short
    random suffix because a pid is reused and two workers on one host can otherwise
    collide on a name and each believe it holds the other's lease.
    """
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


@dataclasses.dataclass(frozen=True)
class Outcome:
    job: str
    claimed: bool
    result: str | None = None          #: "ok" | "error" | None when not claimed
    rows: int | None = None
    error: str | None = None
    took_over_from: str | None = None  #: set when this claim displaced a dead holder
    more_work_pending: bool = False    #: the body hit its bound or said it stopped early
    #: Seconds the body ran (monotonic), None when it was not claimed. The loop's log shows it for a slow body, so a
    #: pass that holds every other job can be named (10 Nov 2026: mesh_edges_catch_up, registered at 60 s, ran every
    #: 2 to 5 minutes on production and the log could not say which job held the pass).
    elapsed_s: float | None = None


#: How soon a tick that reported more work pending is due again (jittered, E7). One
#: second, so "due again" means the next pass of the loop that ticks it (`run_jobs --loop`
#: every 30 s, `run_call_worker` every 5 s) rather than a tight loop of its own.
FOLLOW_UP_SECS = 1.0


def _read_body_result(value, job) -> tuple[int | None, bool]:
    """(rows, more_work_pending) from what a body returned (STATUS D18).

    A `TickResult` is the body's own answer and is read as given: only the body knows it
    stopped on a bound that is not rows read. A plain int keeps the original rule, rows
    at the declared bound means the log may hold more.
    """
    if isinstance(value, TickResult):
        return value.rows, bool(value.more_work_pending)
    return value, (value is not None and value >= job.max_batch)


def run_due(connect, *, jobs=None, holder: str | None = None) -> list[Outcome]:
    """Run every registered job that is due. Returns one Outcome per job, always.

    An Outcome for a job that was NOT claimed is included rather than omitted, so a
    caller can tell "checked, not due" apart from "never looked at it". An empty list
    would be indistinguishable from a registry that failed to load.
    """
    jobs = registry.JOBS if jobs is None else jobs
    holder = holder or default_holder()
    outcomes: list[Outcome] = []

    control = connect()
    try:
        for job in jobs:
            leases.register(control, job.name)
            claim = leases.claim(control, job.name, holder, job.lease_secs)
            if not claim.acquired:
                outcomes.append(Outcome(job=job.name, claimed=False))
                continue
            if claim.took_over_from is not None:
                # Not an error, and not silence either. A job taken over on every tick
                # is a job dying on every tick, and that must not read as healthy.
                log.warning(
                    "job %s: took over an EXPIRED lease from %s; the previous holder "
                    "did not release it", job.name, claim.took_over_from,
                )

            state = leases.read_state(control, job.name) or {}
            prior_failures = int(state.get("consecutive_failures") or 0)

            rows, more, result, error = None, False, "ok", None
            work = connect(statement_timeout_ms=int(job.timeout_secs * 1000))
            started = time.monotonic()
            try:
                rows, more = _read_body_result(job.run(work, job), job)
            except Exception as exc:
                result = "error"
                # Type plus message: a bare message loses whether this was a timeout,
                # a programming error, or the database refusing.
                error = f"{type(exc).__name__}: {exc}"[:2000]
                log.exception("job %s failed", job.name)
            finally:
                elapsed = round(time.monotonic() - started, 2)
                work.close()

            if result == "ok" and more:
                # STATUS D18: a tick that reports more pending is due again at once (the
                # next pass of whatever loop ticks it), not a full interval later. Still
                # jittered (E7), and still one bounded batch per tick (B5): what changes is
                # how soon the next batch starts, never how big it is.
                delay = schedule.next_delay_secs(min(job.interval_secs, FOLLOW_UP_SECS),
                                                 job.jitter_frac)
            elif result == "ok":
                delay = schedule.next_delay_secs(job.interval_secs, job.jitter_frac)
            else:
                delay = schedule.backoff_delay_secs(
                    job.interval_secs, prior_failures + 1, job.jitter_frac)

            if not leases.release(control, job.name, holder,
                                  delay_secs=delay, result=result, error=error):
                # We ran the body but no longer held the lease when we tried to give it
                # back, which means somebody else has already taken the job. Worth a
                # loud line: it is the observable symptom of a lease that is too short
                # for the work.
                log.error("job %s: released a lease we no longer held (holder=%s)",
                          job.name, holder)

            outcomes.append(Outcome(
                job=job.name, claimed=True, result=result, rows=rows, error=error,
                took_over_from=claim.took_over_from,
                more_work_pending=(result == "ok" and more),
                elapsed_s=elapsed,
            ))
    finally:
        control.close()
    return outcomes
