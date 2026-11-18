"""`manage.py run_call_worker`: the deadline sweep every 5 s, in a loop of its own, and the
invocation doorbell sender beside it.

Divergence 041 S5, design 4.10 and Q4 (apps/calls/worker.py says why this is a process of its
own). Run it beside `run_jobs --loop`, with the SAME environment as the server, under a
supervisor. `run_jobs` still runs every job, the sweep included; the lease decides which
process runs a given tick of it.

DOORBELLS (041 S9, apps/calls/webhooks.py). With MESHY_INVOKE_WEBHOOK_KEY_PATH set, the first
start creates this host's webhook key there (never overwritten, never the token key), and the
process also verifies registered doorbell URLs and rings them. Unset, doorbells are off. The
loop runs the sender in a thread beside the deadline loop; `--once` runs one deadline tick and
then one doorbell pass.

EXIT CODES, as `run_jobs`'s:
    0  --once: the tick's claimed jobs succeeded (or nothing was due); --health: healthy
    1  --once: a claimed job failed; --health: an in-flight call is past its deadline by more
       than the alert threshold (janitor.OVERDUE_ALERT_SECS), or the sweep has not finished
       within worker.SWEEP_STALE_ALERT_SECS, or never has (041 S5 review F5), or the worker
       is STALE: the code hash its last tick recorded (sql_meshychat/022) is not the hash of
       the code this command runs (rule B9, operator decision 2026-09-25)
    2  it could not run at all (no database, schema incomplete, or doorbells configured that
       cannot be signed: a webhook key it cannot use, or no issuer)
The loop itself runs until it is stopped, and exits 0 on an interrupt.
"""
from __future__ import annotations

import json
import sys
import threading

from django.core.management.base import BaseCommand

from apps.calls import webhook_key, webhooks, worker
from apps.core import code_fingerprint, dbconn, schema_sets


class Command(BaseCommand):
    help = ("Run the call worker: the deadline sweep every 5 s in its own loop, and the "
            "invocation doorbells when MESHY_INVOKE_WEBHOOK_KEY_PATH is set.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="run one tick and exit with its status")
        parser.add_argument("--health", action="store_true",
                            help="report the oldest in-flight call past its deadline, how "
                                 "long ago the sweep last finished and whether the worker "
                                 "runs the serving code, run nothing, and exit 1 when either "
                                 "is past its alert threshold or the worker is stale")
        parser.add_argument("--json", action="store_true", help="machine-readable output")

    def handle(self, *args, **opts):
        try:
            with dbconn.serving_connection() as conn:
                schema_sets.assert_schema_complete(conn)
                health = worker.read_health(conn) if opts["health"] else None
        except schema_sets.SchemaIncomplete as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            sys.exit(2)
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"cannot reach the database: {exc}"))
            sys.exit(2)

        if health is not None:
            self._report(health, as_json=opts["json"])
            sys.exit(0 if health["healthy"] else 1)

        if opts["once"]:
            report = worker.tick(dbconn.connect)
            running = code_fingerprint.running()
            reading = worker.health(report.oldest_overdue_secs, report.sweep_age_secs,
                                    worker_code=running, worker_code_age_secs=0.0,
                                    serving_code=running)
            try:
                doorbells = self._doorbell_pass()
            except (webhook_key.WebhookKeyError, webhooks.DoorbellConfigError) as exc:
                self.stderr.write(self.style.ERROR(f"doorbells: {exc}"))
                sys.exit(2)
            if opts["json"]:
                self.stdout.write(json.dumps(
                    {"outcomes": [o.__dict__ for o in report.outcomes], "health": reading,
                     "doorbells": doorbells}, default=str))
            else:
                for o in report.outcomes:
                    state = "skip" if not o.claimed else o.result
                    self.stdout.write(f"  {state:5} {o.job} rows={o.rows}")
                self._report(reading, as_json=False)
                if doorbells is None:
                    self.stdout.write(f"  doorbells off: {webhook_key.SETTING} is not set")
                else:
                    self.stdout.write(f"  doorbells: {doorbells}")
            failed = any(o.result == "error" for o in report.outcomes)
            sys.exit(1 if failed else 0)

        stop = threading.Event()
        try:
            worker.run_process(dbconn.connect, stop=stop)
        except (webhook_key.WebhookKeyError, webhooks.DoorbellConfigError) as exc:
            self.stderr.write(self.style.ERROR(f"doorbells: {exc}"))
            sys.exit(2)
        except KeyboardInterrupt:
            stop.set()
            sys.exit(0)

    def _doorbell_pass(self) -> dict | None:
        """One doorbell pass, creating the key at the first start; None when doorbells are
        off. Counts per outcome."""
        if webhook_key.ensure_configured() is None:
            return None
        report = webhooks.run_pass(dbconn.connect)
        counts: dict = {"verifications": {}, "attempts": {}}
        for _peer, outcome in report.verifications:
            counts["verifications"][outcome] = counts["verifications"].get(outcome, 0) + 1
        for ring in report.attempts:
            counts["attempts"][ring.state] = counts["attempts"].get(ring.state, 0) + 1
        return counts

    def _report(self, health: dict, *, as_json: bool) -> None:
        if as_json:
            self.stdout.write(json.dumps(health))
            return
        oldest = health["oldest_overdue_secs"]
        line = ("no in-flight call is past its deadline" if oldest is None else
                f"the oldest in-flight call is {oldest:.1f}s past its deadline")
        age = health["sweep_age_secs"]
        swept = ("the deadline sweep has never finished" if age is None else
                 f"the deadline sweep last finished {age:.1f}s ago")
        stale = health.get("worker_code_stale")
        code = ("no worker code hash recorded" if stale is None else
                f"the worker runs STALE code {health['worker_code_sha256'][:12]} (serving "
                f"{health['code_sha256'][:12]})" if stale else
                f"the worker runs the serving code {health['code_sha256'][:12]}")
        style = self.style.SUCCESS if health["healthy"] else self.style.ERROR
        self.stdout.write(style(f"  {'ok' if health['healthy'] else 'ALERT'}  {line} "
                                f"(alert after {health['alert_after_secs']}s); {swept} "
                                f"(alert after {health['sweep_alert_after_secs']}s); {code}"))
