"""The call worker: the deadline sweep every 5 s, in a loop of its own (divergence 041 S5), and
beside it the invocation doorbell sender (041 S9).

Design docs/design/skill-frames.md (fin/design-skill-frames, 2e229a6) 4.2 and 4.10, the
approved Q4, critique F5 and F12, rule B9. `manage.py run_call_worker` runs `run_loop`.

WHY A PROCESS OF ITS OWN. Since S5 a call to a target with no socket is `queued` (design 4.4,
Q1), and the deadline sweep (apps/calls/janitor.py) is what ends a call whose target never
comes. The shared `run_jobs --loop` runs every registered job in ONE sequence
(apps/jobs/runner.run_due) and sleeps its interval after the tick; divergence 032's link job
shares that loop and may hold one tick for 45 s plus a 20 s per-source budget
(apps/jobs/registry.py). So a sweep there can come about two minutes after the one before it.
This loop runs `run_due(connect,
jobs=JOBS)`, the sweep and nothing else. The shared loop is unchanged and keeps the sweep
registered; the job's lease decides which process runs a given tick (apps/jobs/leases.py), so
two sweeps never run at once. ONE new process, which also sends the webhook doorbells (Q4):
HQD's RAM is tight, and each process costs about 0.3 GB (F12, R14).

THE DOORBELL SENDER (041 S9, design 4.11). `run_process`, what `run_call_worker` runs, first
creates or loads this host's webhook key (`webhook_key.ensure_configured`: an exclusive create
at the first start, never the token key), then starts `webhooks.run_sender` in a thread of its
own beside the deadline loop, so a doorbell's 15 s send never holds a sweep and a sweep never
holds a doorbell. With MESHY_INVOKE_WEBHOOK_KEY_PATH unset, doorbells are off and only the
deadline loop runs. A key that cannot be used (unreadable, not Ed25519, the token key) refuses
to start the process: `run_call_worker` exits 2 rather than run without saying so.

THE LOOP WAKES WHEN THE JOB CAN RUN, at most WORKER_INTERVAL_SECS later. The job's interval is
jittered (5 s plus or minus 20 %, apps/jobs/schedule.py), so a loop that slept a flat 5 s
would find a job due at 5.9 s not yet due at 5 s and run it at 10 s. After each tick the loop
reads, on the database clock, when a claim can next match (`leases.seconds_until_claimable`:
the job due AND no live lease on it) and sleeps until then, bounded to [MIN_WAIT_SECS,
WORKER_INTERVAL_SECS]. A lease held by another process counts (041 S5 review F4): reading
`next_run_at` alone, the loop found the held job due on every tick and ticked every
MIN_WAIT_SECS, two role-checked connections each, until the holder let go.

HEALTH (rule B9: health exercises the work path; design 4.10 (c)). Each tick also reads the
oldest in-flight call past its deadline (`janitor.oldest_overdue_secs`), the sweep's own
predicate over the sweep's own index. Past `janitor.OVERDUE_ALERT_SECS` it is logged as a
WARNING on every tick, and `run_call_worker --health` exits 1: the sweep is not running, is
not keeping up, or keeps skipping a locked row. That reading is None when nothing is overdue,
whether or not anything sweeps, so it also reads how long ago the sweep last FINISHED (its
lease row's `last_finished_at`, whichever process ran it; 041 S5 review F5): past
SWEEP_STALE_ALERT_SECS, or never, is unhealthy too, so a dead worker shows before a call is
late.

B9'S CONTENT HASH OF THE RUNNING CODE (operator decision 2026-09-25; the S5 review's F5 other
half, which a one-shot `--health` could not answer because it would hash the checker, not the
loop). Every tick, whether or not it claimed the sweep, writes `code_fingerprint.running()`
(the served tree as THIS process first read it, fixed at django.setup()) to the sweep's lease
row (`worker_code_sha256`, `worker_code_at`, sql_meshychat/022). `run_call_worker --health`
compares it with its own `running()`, the code a process started now serves, and a
difference is a STALE WORKER: unhealthy, exit 1. `/v1/health` reports the same comparison
against the serving process's code (meshychat/urls.py). A hash never recorded (no worker has
ticked since 022) is reported as None and is not, by itself, unhealthy: the sweep's age
already says whether anything sweeps.
"""
from __future__ import annotations

import dataclasses
import logging
import threading

from apps.calls import janitor
from apps.core import code_fingerprint
from apps.jobs import leases, registry, runner

log = logging.getLogger("meshychat.calls.worker")

#: The loop's cadence, and the sweep's registered interval (apps/jobs/registry.py).
WORKER_INTERVAL_SECS = 5

#: The shortest sleep between two ticks, so a job that is due now cannot spin the loop.
MIN_WAIT_SECS = 0.2

#: The jobs this process's loop runs: the sweep alone. The doorbell sender is not a job: it
#: runs beside the loop in a thread of its own (041 S9, `run_process`).
JOBS: tuple[registry.Job, ...] = (registry.by_name("call_deadline_sweep"),)

#: How long the sweep may go unfinished before health says so (041 S5 review F5): six of this
#: loop's ticks. The shared `run_jobs` loop alone (this process dead) runs it every 30 s at
#: best and about every two minutes behind the link job, so it reads unhealthy then too.
SWEEP_STALE_ALERT_SECS = 30


@dataclasses.dataclass(frozen=True)
class Tick:
    """One tick: the runner's outcomes, the health readings, and when the job can run next."""

    outcomes: list
    #: Seconds past its deadline of the oldest in-flight call, None when there is none.
    oldest_overdue_secs: float | None
    #: Seconds until a claim of the first job can match (due, and no live lease), on the
    #: database clock; None if unknown.
    next_due_secs: float | None
    #: Seconds since the first job last finished, by any process; None when it never did.
    sweep_age_secs: float | None = None

    @property
    def healthy(self) -> bool:
        return is_healthy(self.oldest_overdue_secs, self.sweep_age_secs)


def is_healthy(oldest_overdue_secs: float | None, sweep_age_secs: float | None) -> bool:
    """No in-flight call is past its deadline by more than the alert threshold, AND the sweep
    finished within SWEEP_STALE_ALERT_SECS (a sweep that never finished is not healthy)."""
    overdue_ok = oldest_overdue_secs is None or oldest_overdue_secs <= janitor.OVERDUE_ALERT_SECS
    swept_ok = sweep_age_secs is not None and sweep_age_secs <= SWEEP_STALE_ALERT_SECS
    return overdue_ok and swept_ok


def is_stale(worker_code: str | None, serving_code: str | None) -> bool | None:
    """True when the worker recorded a hash and it is not the serving code's; None when
    either is unknown (nothing to compare)."""
    if worker_code is None or serving_code is None:
        return None
    return worker_code != serving_code


def health(oldest_overdue_secs: float | None, sweep_age_secs: float | None, *,
           worker_code: str | None = None, worker_code_age_secs: float | None = None,
           serving_code: str | None = None) -> dict:
    """A health reading as `run_call_worker` reports it. A stale worker (B9) is unhealthy."""
    stale = is_stale(worker_code, serving_code)
    return {"oldest_overdue_secs": oldest_overdue_secs,
            "alert_after_secs": janitor.OVERDUE_ALERT_SECS,
            "sweep_age_secs": sweep_age_secs,
            "sweep_alert_after_secs": SWEEP_STALE_ALERT_SECS,
            "code_sha256": serving_code,
            "worker_code_sha256": worker_code,
            "worker_code_age_secs": worker_code_age_secs,
            "worker_code_stale": stale,
            "healthy": is_healthy(oldest_overdue_secs, sweep_age_secs) and stale is not True}


def read_health(conn) -> dict:
    """The health reading, read now, as `run_call_worker --health` reports it: the worker's
    recorded code compared with the code this process serves."""
    worker_code, worker_code_age = leases.read_code_hash(conn, JOBS[0].name)
    return health(janitor.oldest_overdue_secs(conn),
                  leases.seconds_since_finished(conn, JOBS[0].name),
                  worker_code=worker_code, worker_code_age_secs=worker_code_age,
                  serving_code=code_fingerprint.running())


def read_worker_code(conn) -> tuple[str | None, float | None]:
    """What the call worker last recorded (sql_meshychat/022): (hash, seconds since)."""
    return leases.read_code_hash(conn, JOBS[0].name)


def tick(connect, *, jobs: tuple = JOBS, holder: str | None = None) -> Tick:
    """Run what is due (the lease decides), then read the health and the next due time.
    `connect` is a connection FACTORY (rule E7, apps/jobs/runner.py): nothing here can be
    handed a request's connection."""
    outcomes = runner.run_due(connect, jobs=jobs, holder=holder)
    conn = connect()
    try:
        # B9: the code this process runs, every tick, claimed or not (sql_meshychat/022).
        leases.record_code_hash(conn, jobs[0].name, code_fingerprint.running())
        oldest = janitor.oldest_overdue_secs(conn)
        due = leases.seconds_until_claimable(conn, jobs[0].name)
        swept = leases.seconds_since_finished(conn, jobs[0].name)
    finally:
        conn.close()
    report = Tick(outcomes=outcomes, oldest_overdue_secs=oldest, next_due_secs=due,
                  sweep_age_secs=swept)
    if not is_healthy(oldest, 0.0):
        log.warning("call worker: an in-flight call is %.1fs past its deadline (alert after "
                    "%ds): the deadline sweep is not keeping up", oldest,
                    janitor.OVERDUE_ALERT_SECS)
    if not is_healthy(None, swept):
        log.warning("call worker: %s last finished %s ago (alert after %ds): nothing is "
                    "running it on time", jobs[0].name,
                    "never" if swept is None else f"{swept:.1f}s", SWEEP_STALE_ALERT_SECS)
    for outcome in outcomes:
        if outcome.result == "error":
            log.error("call worker: %s failed: %s", outcome.job, outcome.error)
    return report


def run_loop(connect, *, stop: threading.Event, interval: float = WORKER_INTERVAL_SECS,
             jobs: tuple = JOBS, holder: str | None = None) -> None:
    """Tick until `stop` is set. A tick that raises (the database is down) is logged and
    retried after `interval`; the loop itself never ends on an error."""
    holder = holder or runner.default_holder()
    while not stop.is_set():
        try:
            report = tick(connect, jobs=jobs, holder=holder)
        except Exception:
            log.exception("call worker: the tick failed; retrying in %.1fs", interval)
            wait = interval
        else:
            due = report.next_due_secs
            wait = interval if due is None else min(interval, max(MIN_WAIT_SECS, due))
        stop.wait(wait)


#: How long `run_process` waits for the doorbell thread once stopped: a send's 15 s budget
#: and the record after it.
SENDER_JOIN_SECS = 30


def run_process(connect, *, stop: threading.Event, interval: float = WORKER_INTERVAL_SECS,
                holder: str | None = None) -> None:
    """What `run_call_worker` runs until `stop`: the deadline loop in this thread and, when
    this host has a doorbell key path, the doorbell sender in a thread beside it (041 S9).
    Raises `webhook_key.WebhookKeyError` or `webhooks.DoorbellConfigError` before anything
    runs when doorbells are configured and cannot be signed (the key unusable, no issuer).
    Setting `stop` ends both."""
    from apps.calls import webhook_key, webhooks

    key = webhook_key.ensure_configured()
    sender = None
    if key is None:
        log.warning("call worker: %s is not set; doorbells are off", webhook_key.SETTING)
    else:
        webhooks.require_issuer()
        log.info("call worker: doorbells on, webhook key kid %s", key.kid)
        sender = threading.Thread(target=webhooks.run_sender, args=(connect,),
                                  kwargs={"stop": stop}, name="meshy-doorbell", daemon=True)
        sender.start()
    try:
        run_loop(connect, stop=stop, interval=interval, holder=holder)
    finally:
        stop.set()
        if sender is not None:
            sender.join(SENDER_JOIN_SECS)


__all__ = ["JOBS", "MIN_WAIT_SECS", "SENDER_JOIN_SECS", "SWEEP_STALE_ALERT_SECS", "Tick",
           "WORKER_INTERVAL_SECS", "health", "is_healthy", "is_stale", "read_health",
           "read_worker_code", "run_loop", "run_process", "tick"]
