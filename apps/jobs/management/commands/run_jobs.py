"""`manage.py run_jobs`. The tick, and the explain surface.

WHY A MANAGEMENT COMMAND AND NOT AN ASGI LIFESPAN HOOK. E7: "background work never
shares the request pool." Starting the scheduler from the ASGI lifespan ties job
execution to the serving processes, so every web worker also runs jobs and the coupling
E7 forbids is reintroduced at the process level rather than the connection level. A
command starts nothing by itself: an operator points cron, a systemd timer, a Windows
scheduled task or a supervisor at it, and that choice stays theirs. This file
deliberately does not decide how often it runs.

EXIT CODES MEAN SOMETHING, because a scheduler wrapped in cron is read by its exit code
and nothing else:
    0  every claimed job succeeded (including the case where nothing was due)
    1  at least one claimed job failed
    2  the tick itself could not run (no database, schema incomplete)

LANES. `--loop` ticks the jobs in one serial pass, except a job registered `own_lane`,
which ticks on a thread of its own at the same interval: its tick can outlast its
statement_timeout (the files janitor's object deletes are blob calls, not SQL), and in the
shared pass a slow store would hold every other job for as long as it took. A single tick
(no `--loop`) still runs every job in one pass.
"""
from __future__ import annotations

import os

import json
import sys
import threading
import time

from django.core.management.base import BaseCommand, CommandError

from apps.core import dbconn, schema_sets
from apps.jobs import explain as explain_mod
from apps.jobs import leases
from apps.jobs import registry, runner

#: A body that ran this long or longer gets its duration on its log line (runner.Outcome.elapsed_s); a faster one keeps
#: today's line exactly, so the log does not double in size.
SLOW_BODY_SECS = 1.0


def _slow(o) -> str:
    e = getattr(o, "elapsed_s", None)
    return f" in {e:.1f}s" if isinstance(e, (int, float)) and e >= SLOW_BODY_SECS else ""


#: How long an interrupted `--loop` waits for its lanes to finish the tick in hand.
LANE_JOIN_SECS = 5.0
_OUTPUT_LOCK = threading.Lock()


def excluded_jobs(flag_values) -> frozenset:
    """The job names to leave out: --exclude when given, else MESHY_JOBS_EXCLUDE (comma-separated), else none.
    Every name must be registered, or the command refuses: a typo would otherwise run the job it meant to cut."""
    if flag_values:
        names = [n.strip() for v in flag_values for n in v.split(",")]
    else:
        names = [n.strip() for n in os.environ.get("MESHY_JOBS_EXCLUDE", "").split(",")]
    names = [n for n in names if n]
    known = {j.name for j in registry.JOBS}
    unknown = sorted(set(names) - known)
    if unknown:
        raise CommandError("unknown job name(s) to exclude: %s; registered: %s" % (unknown, sorted(known)))
    return frozenset(names)


class Command(BaseCommand):
    help = "Run background jobs that are due, or explain why they are not."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true",
                            help="keep ticking instead of exiting after one tick")
        parser.add_argument("--interval", type=float, default=30.0,
                            help="seconds between ticks when --loop is given")
        parser.add_argument("--explain", action="store_true",
                            help="report why each job is or is not runnable, and run nothing")
        parser.add_argument("--job", help="limit to one registered job by name")
        parser.add_argument("--exclude", action="append", default=None, metavar="NAME",
                            help="leave this registered job out (repeatable). Without the flag, "
                                 "MESHY_JOBS_EXCLUDE (comma-separated names) is read. An unknown name "
                                 "refuses to start. HQD: mesh_related_edges builds the search indexes in this "
                                 "process (about 1 GB); run it on its own instead")
        parser.add_argument("--json", action="store_true", help="machine-readable output")
        parser.add_argument("--run-now", action="store_true",
                            help="bring --job forward so the next tick picks it up. Does "
                                 "NOT bypass the lease")
        parser.add_argument("--release-lease", action="store_true",
                            help="clear --job's lease. Refuses a LIVE lease without --force")
        parser.add_argument("--force", action="store_true",
                            help="with --release-lease, clear a lease that has not expired")

    def handle(self, *args, **opts):
        jobs = registry.JOBS
        if opts["job"]:
            try:
                jobs = (registry.by_name(opts["job"]),)
            except KeyError as exc:
                raise CommandError(str(exc)) from exc
        excluded = excluded_jobs(opts.get("exclude"))
        if excluded:
            if opts["job"]:
                raise CommandError("--job and --exclude (or MESHY_JOBS_EXCLUDE) never go together")
            jobs = tuple(j for j in jobs if j.name not in excluded)
            if not opts["json"]:
                self._write(self.stdout, "excluded: %s" % ", ".join(sorted(excluded)))

        if opts["run_now"] or opts["release_lease"]:
            if not opts["job"]:
                raise CommandError("--run-now and --release-lease need --job <name>")
            return self._operator_verb(opts)

        if opts["explain"]:
            return self._explain(jobs, as_json=opts["json"])

        # The schema check runs ONCE, before any tick, and refuses rather than warning.
        # A scheduler whose lease table is missing would otherwise fail every job
        # individually and look like a job problem instead of a deploy problem.
        try:
            with dbconn.serving_connection() as conn:
                schema_sets.assert_schema_complete(conn)
        except schema_sets.SchemaIncomplete as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            sys.exit(2)
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"cannot reach the database: {exc}"))
            sys.exit(2)

        if not opts["loop"]:
            sys.exit(self._tick(jobs, as_json=opts["json"]))

        # A loop is for a supervisor that restarts it. It does NOT swallow a failing
        # tick into silence: the failure is reported every time and the exit code of the
        # last tick (the shared pass's, or any lane's) is what the process eventually
        # returns if it is stopped.
        shared = tuple(j for j in jobs if not j.own_lane)
        stop = threading.Event()
        lane_last: dict[str, int] = {}
        lanes = [threading.Thread(target=self._lane, args=(job, opts, stop, lane_last),
                                  name=f"run_jobs-{job.name}", daemon=True)
                 for job in jobs if job.own_lane]
        for lane in lanes:
            lane.start()
        last = 0
        try:
            while True:
                if shared:
                    last = self._tick(shared, as_json=opts["json"])
                time.sleep(opts["interval"])
        except KeyboardInterrupt:
            stop.set()
            for lane in lanes:
                # A lane stuck in a blob call cannot be interrupted; it is a daemon thread,
                # so it does not keep the process alive past this bounded wait.
                lane.join(LANE_JOIN_SECS)
            sys.exit(max([last, *lane_last.values()]))

    def _lane(self, job, opts, stop: threading.Event, lane_last: dict) -> None:
        """One `own_lane` job's loop until `stop`. A tick that raises (the database gone)
        is reported and the lane goes on, as the shared loop's supervisor would restart it:
        a dead thread would be a job that silently never runs again."""
        while not stop.is_set():
            try:
                lane_last[job.name] = self._tick((job,), as_json=opts["json"])
            except Exception as exc:
                lane_last[job.name] = 1
                self._write(self.stdout, self.style.ERROR(
                    f"  FAIL  {job.name} lane tick: {type(exc).__name__}: {exc}"))
            stop.wait(opts["interval"])

    def _operator_verb(self, opts) -> None:
        """The two manual affordances, kept deliberately narrow.

        Borrowed in spirit from DBOS's pause/resume/restart-from-step, minus anything that
        would let an operator start a second concurrent copy by hand. --run-now moves the
        schedule and nothing else, so the lease still decides who runs it.
        """
        name = opts["job"]
        with dbconn.serving_connection() as conn:
            if opts["run_now"]:
                if not leases.force_due(conn, name):
                    raise CommandError(f"{name!r} is not registered; it has never been ticked")
                self.stdout.write(self.style.SUCCESS(
                    f"  {name} is now due. The NEXT tick will claim it if nothing holds it; "
                    "this did not bypass the lease."))
                return
            released, why = leases.force_release(conn, name, force=opts["force"])
            if released:
                self.stdout.write(self.style.WARNING(f"  {name}: {why}"))
            else:
                self.stderr.write(self.style.ERROR(f"  {name}: {why}"))
                sys.exit(1)

    def _write(self, stream, line: str) -> None:
        # Lanes tick on threads of their own; one lock keeps each line whole.
        with _OUTPUT_LOCK:
            stream.write(line)

    def _tick(self, jobs, *, as_json: bool) -> int:
        outcomes = runner.run_due(dbconn.connect, jobs=jobs)
        failed = [o for o in outcomes if o.result == "error"]

        if as_json:
            self._write(self.stdout, json.dumps([o.__dict__ for o in outcomes], default=str))
        else:
            for o in outcomes:
                if not o.claimed:
                    self._write(self.stdout, f"  skip  {o.job} (not due, or held elsewhere)")
                elif o.result == "ok":
                    more = " MORE PENDING" if o.more_work_pending else ""
                    took = f" (took over from {o.took_over_from})" if o.took_over_from else ""
                    self._write(self.stdout, self.style.SUCCESS(
                        f"  ok    {o.job} rows={o.rows}{more}{took}{_slow(o)}"))
                else:
                    self._write(self.stdout, self.style.ERROR(f"  FAIL  {o.job} {o.error}{_slow(o)}"))
        return 1 if failed else 0

    def _explain(self, jobs, *, as_json: bool) -> None:
        with dbconn.serving_connection() as conn:
            reports = explain_mod.explain_all(conn, jobs)
        if as_json:
            self.stdout.write(json.dumps([r.__dict__ for r in reports], default=str))
            return
        for r in reports:
            head = "RUNNABLE" if r.runnable else "blocked "
            style = self.style.SUCCESS if r.runnable else self.style.WARNING
            self.stdout.write(style(f"  {head}  {r.job}  [{r.reason}]"))
            self.stdout.write(f"            {r.detail}")
            if r.last_error:
                self.stdout.write(f"            last error: {r.last_error}")
