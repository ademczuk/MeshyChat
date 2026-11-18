"""`GET /v1/metrics`: Prometheus text exposition (E01) and the NOTIFY queue
gauge (F28). The scrape config and alert rules ship in `deploy/prometheus/`; wiring them to
a running Prometheus and an Alertmanager is production's.

  * `meshychat_http_requests_total{status}`, counter: every response by status code, counted by
    `MetricsMiddleware`, the OUTERMOST middleware, so it sees the final status of every
    route, including the error envelope.
    EXCEPT the probe routes, `/v1/health` and `/v1/livez` (PROBE_PATHS, docs/DIVERGENCES.md
    080): /v1/health answers 503 by design on a server with no Redis, and a probe polled
    every few seconds turned that design into the error-rate alert's 5xx share. A probe's
    answer is what the probe reports; the counter is about traffic.
  * `meshychat_ws_connections`, gauge: WebSocket connections open in THIS process. The
    connection manager owns this count, so it is read at scrape time without changing hub
    files and cannot drift from the registry it reports.
  * These metrics are absent because their sources are absent: there is no Redis bridge, no
    identity rate watch and no connection pool (each request opens its own connection;
    docs/DIVERGENCES.md 035).
  * `meshychat_pg_notification_queue_usage`, gauge (F28, docs/DIVERGENCES.md 020): Postgres's
    `pg_notification_queue_usage()`, the fraction of the shared 8 GB NOTIFY queue in use. A
    listener that stops reading while it keeps its connection open holds the queue's tail;
    when the queue is full the WRITER's COMMIT fails, outside sql/036's exception guard. It
    is ABSENT when this scrape could not read it, never 0: 0 is "empty", and "empty" is the
    answer that hides the failure this gauge exists for.
  * `meshychat_db_up`, gauge: 1 when this scrape's database read answered within its deadline.
  * `meshychat_process_start_time_seconds`, gauge: a restart shows as a step.
  * `meshychat_ws_send_queue_dropped_total`, counter (F04, operator decision 60, 2026-09-27):
    frames the per-socket send queue's drop-oldest policy discarded in THIS process, added
    where apps/wsgw/consumer.py's writer logs "WS send queue dropped", by the count that
    warning names. The drop itself stays silent on the wire (P-001 in docs/DIVERGENCES.md).
    A burst dropped after the writer's last pop, on a socket that then closes, is neither
    logged nor counted: the writer reports loss when it next takes a frame.
  * `meshychat_job_claimable_seconds{job_name}`, gauge (E01, operator decision 55, 2026-09-27):
    for every registered job, what `run_jobs --explain` reports, read through the same
    function (apps/jobs/explain.explain_all) over the same rows (`background_job_leases`):
    how long the job has been claimable and unclaimed (`due_for_secs`), 0 when it is held
    by a live lease or not yet due, and +Inf when it has no row, i.e. no process has ever
    ticked it. Past explain.STALE_DUE_SECS (300 s) explain says `nothing_is_ticking`, and
    the alert rule MeshyChatJobsNotTicking uses the same number.
  * `meshychat_call_sweep_age_seconds`, gauge (E01): seconds since the call deadline sweep last
    FINISHED, by any process, `leases.seconds_since_finished` on its row: the liveness half
    of `run_call_worker --health`, which is unhealthy past worker.SWEEP_STALE_ALERT_SECS
    (30 s) or when it never finished, which is +Inf here. The oldest overdue call and the
    worker's code hash, the other two things --health reads, are not exported.

  * `meshychat_mesh_unversioned_accepted_total`, counter (docs/DIVERGENCES.md 011, dated paragraph
    2026-09-27): shared/mesh-chat messages with no `schema_version` that memory.write WROTE in
    this process because the transitional MESHY_MESH_ACCEPT_UNVERSIONED is on, moved by
    apps/core/mesh_unversioned.note_accepted after the row is inserted; a refusal and a replay
    do not move it. Nonzero is the fleet still sending the legacy shape; the flag comes off
    when it stays flat.

Every series read from the database is ABSENT, never 0, when this scrape could not read it.

THE GATE is deliberately closed by default (V1-DESIGN-RULES: gates default CLOSED): without
MESHY_METRICS_TOKEN, the route answers
503 E_METRICS_DISABLED rather than exposing process state without credentials. With one, the
request needs `Authorization: Bearer <token>`, compared as BYTES in constant time (a `str`
compare_digest raises on a non-ASCII header: apps/core/websec.py), else 401. The token is
never echoed or logged. No label carries a secret or a caller's value: the one label is a
status code.

THE DATABASE READ is bounded like /v1/livez's probe (meshychat/urls.py): one read at a time
on its own thread, waited on for at most DB_DEADLINE_S. A read past its deadline is
abandoned, and scrapes that arrive while it is still running join it instead of starting
another, so a database that accepts and never answers costs one thread, however often it is
scraped. Its connection carries urls.LIVEZ_TCP's keepalives so an abandoned read returns.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hmac
import logging
import math
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout

from django.http import HttpResponse
from django.views.decorators.http import require_http_methods

from apps.core.jsonerrors import envelope

log = logging.getLogger("meshychat.metrics")

#: The text exposition format, version 0.0.4.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Every family's name starts with PREFIX.
PREFIX = "meshychat_"

#: How long a scrape waits for its database read: /v1/health's probe bound.
DB_DEADLINE_S = 2.0

_START = time.time()


class _StatusCounter:
    """One counter family with the single label `status`. Its label values are HTTP status
    codes, so the series it can hold are bounded by the codes Django will send."""

    def __init__(self, name: str, help_text: str):
        self.name, self.help = name, help_text
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def inc(self, status: int) -> None:
        key = str(int(status))
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def samples(self) -> list[tuple[str, float]]:
        with self._lock:
            return [(f'{{status="{k}"}}', float(v)) for k, v in sorted(self._counts.items())]


HTTP_REQUESTS = _StatusCounter("meshychat_http_requests_total",
                               "Total HTTP requests served, by response status code, "
                               "the health and liveness probes excepted")

#: Routes whose responses HTTP_REQUESTS does not count (divergence 080). Exact paths: a
#: trailing slash or any other spelling is not a probe, is a 404, and is counted.
PROBE_PATHS = frozenset({"/v1/health", "/v1/livez"})


class _Counter:
    """One counter with no labels. Its whole state is one int, so it is bounded."""

    def __init__(self, name: str, help_text: str):
        self.name, self.help = name, help_text
        self._lock = threading.Lock()
        self._value = 0

    def inc(self, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError(f"{self.name} is a counter: it cannot move by {amount}")
        with self._lock:
            self._value += int(amount)

    def value(self) -> int:
        with self._lock:
            return self._value


#: F04 (operator decision 60): moved by apps/wsgw/consumer.py's writer where it logs the drop.
WS_SEND_QUEUE_DROPPED = _Counter(
    "meshychat_ws_send_queue_dropped_total",
    "Frames the WebSocket send queues of this process discarded (drop-oldest), as the "
    "writer found them")

#: Divergence 011 (2026-09-27): moved by apps/core/mesh_unversioned.note_accepted, once per row
#: memory.write wrote with no schema_version while MESHY_MESH_ACCEPT_UNVERSIONED is on.
MESH_UNVERSIONED_ACCEPTED = _Counter(
    "meshychat_mesh_unversioned_accepted_total",
    "shared/mesh-chat messages with no schema_version that memory.write accepted in this "
    "process (the transitional MESHY_MESH_ACCEPT_UNVERSIONED)")


class MetricsMiddleware:
    """Counts every response by status. Listed FIRST in
    settings.MIDDLEWARE, so it sees what the client is sent, after every other middleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path_info not in PROBE_PATHS:
            HTTP_REQUESTS.inc(response.status_code)
        return response


def ws_connections() -> int:
    """WebSocket connections open in this process. The hub module is not imported for this:
    when nothing has imported it, no socket has been accepted, and 0 is the truth.

    The manager's methods are coroutines on the server's event loop; this runs on a request
    thread. `list(dict.values())` is one C-level copy under the GIL, and `len` of each list
    is atomic, so the count is a consistent-enough snapshot for a gauge and never raises
    "dictionary changed size during iteration"."""
    module = sys.modules.get("apps.wsgw.connection_manager")
    if module is None:
        return 0
    return sum(len(handles) for handles in list(module.MANAGER._by_peer.values()))


@dataclasses.dataclass(frozen=True)
class _Reading:
    """One scrape's database read, all of it or none of it."""

    queue_usage: float
    #: (job name, seconds claimable) per registered job, in registry order.
    job_claimable: tuple[tuple[str, float], ...]
    call_sweep_age: float


def _claimable_secs(report) -> float:
    """What `meshychat_job_claimable_seconds` shows for one `run_jobs --explain` report."""
    from apps.jobs import explain
    if report.reason == explain.NOT_REGISTERED:
        return math.inf
    return float(report.due_for_secs or 0.0) if report.runnable else 0.0


def _read_database() -> _Reading:
    from psycopg.conninfo import make_conninfo

    from apps.calls import worker
    from apps.core import dbconn
    from apps.jobs import explain, leases, registry
    from meshychat.urls import LIVEZ_TCP
    target = make_conninfo(dbconn.dsn(), **LIVEZ_TCP)
    with contextlib.closing(dbconn.connect(
            target, connect_timeout=DB_DEADLINE_S,
            statement_timeout_ms=int(DB_DEADLINE_S * 1000),
            application_name="meshychat-metrics")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_notification_queue_usage()")
            usage = float(cur.fetchone()[0])
        # The operator commands' own reads, not a second opinion of them (E01).
        jobs = tuple((r.job, _claimable_secs(r))
                     for r in explain.explain_all(conn, registry.JOBS))
        swept = leases.seconds_since_finished(conn, worker.JOBS[0].name)
    return _Reading(queue_usage=usage, job_claimable=jobs,
                    call_sweep_age=math.inf if swept is None else float(swept))


class _SingleRead:
    """At most one database read in flight; a scrape joins it rather than start another."""

    def __init__(self, read):
        self._read = read
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metrics-db")
        self._flight: Future | None = None

    def get(self, deadline_s: float) -> _Reading | None:
        with self._lock:
            if self._flight is None or self._flight.done():
                self._flight = self._pool.submit(self._read)
            flight = self._flight
        try:
            return flight.result(timeout=deadline_s)
        except FutureTimeout:
            log.warning("/v1/metrics: the database read exceeded %.0fs; db_up=0 and no "
                        "database series this scrape", deadline_s)
        except Exception as exc:
            # Type only: a connection error's message can carry the host and the user.
            log.warning("/v1/metrics: the database read raised %s; db_up=0 and no "
                        "database series this scrape", type(exc).__name__)
        return None


DB_READ = _SingleRead(_read_database)


def _family(out: list[str], name: str, kind: str, help_text: str,
            samples: list[tuple[str, float]]) -> None:
    assert name.startswith(PREFIX), name
    out.append(f"# HELP {name} {help_text}")
    out.append(f"# TYPE {name} {kind}")
    for labels, value in samples:
        out.append(f"{name}{labels} {_number(value)}")


def _number(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def render() -> str:
    """The whole exposition. Every family is listed, with its HELP and TYPE, even when it has
    no sample this scrape (the queue gauge when the read failed), so a dashboard sees the
    family and the missing sample, not a missing family."""
    reading = DB_READ.get(DB_DEADLINE_S)
    out: list[str] = []
    _family(out, HTTP_REQUESTS.name, "counter", HTTP_REQUESTS.help, HTTP_REQUESTS.samples())
    _family(out, "meshychat_ws_connections", "gauge",
            "Currently-active WebSocket connections in this process",
            [("", float(ws_connections()))])
    _family(out, "meshychat_db_up", "gauge",
            "1 when this scrape's database read answered within its deadline, else 0",
            [("", 0.0 if reading is None else 1.0)])
    _family(out, "meshychat_pg_notification_queue_usage", "gauge",
            "Fraction of Postgres's NOTIFY queue in use (pg_notification_queue_usage); "
            "absent when this scrape could not read it",
            [] if reading is None else [("", reading.queue_usage)])
    _family(out, "meshychat_process_start_time_seconds", "gauge",
            "Start time of this process, seconds since the Unix epoch", [("", _START)])
    _family(out, WS_SEND_QUEUE_DROPPED.name, "counter", WS_SEND_QUEUE_DROPPED.help,
            [("", float(WS_SEND_QUEUE_DROPPED.value()))])
    _family(out, "meshychat_job_claimable_seconds", "gauge",
            "Seconds each registered job has been claimable and unclaimed, as run_jobs "
            "--explain reads it: 0 when held or not due, +Inf when never ticked; absent "
            "when this scrape could not read it",
            [] if reading is None else
            [(f'{{job_name="{name}"}}', secs) for name, secs in reading.job_claimable])
    _family(out, "meshychat_call_sweep_age_seconds", "gauge",
            "Seconds since the call deadline sweep last finished, as run_call_worker "
            "--health reads it: +Inf when it never has; absent when this scrape could not "
            "read it",
            [] if reading is None else [("", reading.call_sweep_age)])
    _family(out, MESH_UNVERSIONED_ACCEPTED.name, "counter", MESH_UNVERSIONED_ACCEPTED.help,
            [("", float(MESH_UNVERSIONED_ACCEPTED.value()))])
    return "\n".join(out) + "\n"


def token() -> bytes | None:
    from django.conf import settings
    raw = getattr(settings, "MESHY_METRICS_TOKEN", None)
    return raw.encode("utf-8") if raw else None


def _refusal(status: int, code: str, message: str) -> HttpResponse:
    return envelope(code, message, status=status)


@require_http_methods(["GET", "HEAD"])
def metrics_view(request):
    expected = token()
    if expected is None:
        return _refusal(503, "E_METRICS_DISABLED",
                        "metrics are off: set MESHY_METRICS_TOKEN to serve them")
    header = request.META.get("HTTP_AUTHORIZATION")
    if header is None:
        return _refusal(401, "E_AUTH_MISSING", "missing auth")
    # Django hands the header over decoded as latin-1, so encoding it back is lossless, and
    # compare_digest on BYTES has no ASCII restriction: a byte past 0x7F is a wrong token.
    raw = header.encode("latin-1", errors="replace")
    if not (raw.startswith(b"Bearer ") and hmac.compare_digest(raw[7:], expected)):
        return _refusal(401, "E_AUTH_INVALID", "invalid metrics token")
    return HttpResponse(render(), content_type=CONTENT_TYPE)


__all__ = ["CONTENT_TYPE", "DB_DEADLINE_S", "DB_READ", "HTTP_REQUESTS", "MESH_UNVERSIONED_ACCEPTED", "PROBE_PATHS",
           "MetricsMiddleware", "WS_SEND_QUEUE_DROPPED", "metrics_view", "render", "ws_connections"]
