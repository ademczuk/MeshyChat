"""`GET /v1/metrics`: the Prometheus exposition (E01), the NOTIFY queue gauge
(F28), and the scrape config and alert rules that ship beside them.

What is under test, each negative beside its positive control:

  * WIRING through the real URLconf and the real MIDDLEWARE list, never a pinned one: a route
    test that supplies its own URLconf proves the view and not the mount.
  * THE GATE. With no MESHY_METRICS_TOKEN the route is OFF (503 E_METRICS_DISABLED, no series
    in the body): gates default closed (V1-DESIGN-RULES). An unset token must not open it.
    With a token: no header 401, wrong token 401, a non-ASCII header 401 and
    never a 500 (apps/core/websec.py's lesson), the right one 200.
  * THE FORMAT: text exposition 0.0.4, every sample under its own HELP and TYPE.
  * `meshychat_http_requests_total{status}` counts what the MIDDLEWARE saw, by exact delta.
  * F28: `meshychat_pg_notification_queue_usage` is `pg_notification_queue_usage()` read at scrape
    time. Proved against a queue this test really fills (a listener held inside a
    transaction does not read, so the notifications it has not read stay queued), not
    against the dev database's 0.0, which a hard-wired zero would also match.
  * FAIL CLOSED: an unreachable database gives `meshychat_db_up 0` and NO queue series. A zero
    would read as "queue empty", which is the answer that hides the defect F28 is about.
  * BOUNDED: a database that accepts and never answers costs one probe thread however many
    scrapes arrive, and each scrape answers within its deadline.
  * The shipped scrape config and alert rules name only series this server emits, and the
    rule checker is shown to catch a planted rule that names one it does not.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ["MESHY_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
os.environ.setdefault("MESHY_DSN", DSN)

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

TOKEN = "scrape-token-for-tests-only"
PATH = "/v1/metrics"
SCRAPE = ROOT / "deploy" / "prometheus" / "scrape.yml"
ALERTS = ROOT / "deploy" / "prometheus" / "alerts.yml"
SERIES_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def _scrape(client=None, token=TOKEN, **headers):
    if token is not None:
        headers.setdefault("HTTP_AUTHORIZATION", f"Bearer {token}")
    with override_settings(MESHY_METRICS_TOKEN=TOKEN):
        return (client or Client()).get(PATH, **headers)


def parse(body: str) -> dict:
    """{family: {"help", "type", "samples": {labels-text: value}}}, refusing anything that is
    not the text exposition format: a sample before its family's HELP and TYPE, a bad name,
    a value that is not a float."""
    families: dict = {}
    for line in body.splitlines():
        if not line:
            continue
        if line.startswith("# HELP ") or line.startswith("# TYPE "):
            kind, name, rest = line[2:6], *line[7:].split(" ", 1)
            assert SERIES_NAME.match(name), line
            families.setdefault(name, {"samples": {}})[kind.lower()] = rest
            continue
        assert not line.startswith("#"), line
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})? (\S+)$", line)
        assert m, f"not a sample line: {line!r}"
        name, labels, value = m.group(1), m.group(2) or "", float(m.group(3))
        fam = families.get(name)
        assert fam and "help" in fam and "type" in fam, f"{name}: sample before HELP/TYPE"
        fam["samples"][labels] = value
    return families


def _value(families, name, labels=""):
    return families.get(name, {}).get("samples", {}).get(labels)


# ------------------------------------------------------------------- the gate

def test_the_route_is_off_until_a_token_is_configured():
    with override_settings(MESHY_METRICS_TOKEN=None):
        off = Client().get(PATH, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert off.status_code == 503, off.content[:200]
    assert off.json()["error"] == "E_METRICS_DISABLED"
    assert b"meshychat_" not in off.content
    on = _scrape()                                   # positive control: the same request
    assert on.status_code == 200, on.content[:200]
    assert b"meshychat_http_requests_total" in on.content


@pytest.mark.parametrize("header,code", [
    (None, "E_AUTH_MISSING"),
    ("Bearer wrong-token", "E_AUTH_INVALID"),
    (f"Bearer {TOKEN}x", "E_AUTH_INVALID"),
    (f"bearer {TOKEN}", "E_AUTH_INVALID"),
    (TOKEN, "E_AUTH_INVALID"),
    ("Bearer café", "E_AUTH_INVALID"),
])
def test_a_scrape_without_the_token_is_refused_and_never_a_500(header, code):
    headers = {} if header is None else {"HTTP_AUTHORIZATION": header}
    with override_settings(MESHY_METRICS_TOKEN=TOKEN):
        resp = Client().get(PATH, **headers)
    assert resp.status_code == 401, (header, resp.status_code, resp.content[:200])
    assert resp.json()["error"] == code
    assert TOKEN.encode() not in resp.content and b"meshychat_" not in resp.content


def test_only_get_and_head_are_served():
    with override_settings(MESHY_METRICS_TOKEN=TOKEN):
        post = Client().post(PATH, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert post.status_code == 405
    assert _scrape().status_code == 200


# ----------------------------------------------------------------- the format

def test_the_exposition_is_text_format_with_every_family_described():
    resp = _scrape()
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    fams = parse(resp.content.decode("utf-8"))
    for name, kind in [("meshychat_http_requests_total", "counter"), ("meshychat_ws_connections", "gauge"),
                       ("meshychat_db_up", "gauge"), ("meshychat_pg_notification_queue_usage", "gauge"),
                       ("meshychat_process_start_time_seconds", "gauge"),
                       ("meshychat_ws_send_queue_dropped_total", "counter"),
                       ("meshychat_job_claimable_seconds", "gauge"),
                       ("meshychat_call_sweep_age_seconds", "gauge")]:
        assert name in fams, f"{name} missing: {sorted(fams)}"
        assert fams[name]["type"] == kind, (name, fams[name]["type"])
    assert _value(fams, "meshychat_db_up") == 1.0
    assert 0 < _value(fams, "meshychat_process_start_time_seconds") <= time.time()
    assert _value(fams, "meshychat_ws_connections") >= 0


def test_every_family_is_emitted_once_under_the_prefix():
    """Nine families, each named `meshychat_...`, and nothing else in the exposition.
    MUTANT: emit a family a second time under another name; the name list fails."""
    from apps.core import metrics

    fams = parse(_scrape().content.decode("utf-8"))
    assert len(fams) == 9, sorted(fams)
    assert all(name.startswith(metrics.PREFIX) for name in fams), sorted(fams)
    assert sorted(parse(metrics.render())) == sorted(fams)


def test_http_requests_are_counted_by_status_by_exact_delta():
    client = Client()
    before = parse(_scrape(client).content.decode())
    assert client.get("/v1/no-such-route-for-metrics").status_code == 404
    after = parse(_scrape(client).content.decode())

    def delta(status):
        key = f'{{status="{status}"}}'
        return (_value(after, "meshychat_http_requests_total", key) or 0) - \
            (_value(before, "meshychat_http_requests_total", key) or 0)

    assert delta(404) == 1, "the 404 was not counted once"
    assert delta(200) == 1, "the first scrape's own 200 was not counted once"
    assert delta(503) == 0, "a status nobody answered moved"


def test_a_refusal_by_an_inner_middleware_is_counted_because_the_counter_is_outermost():
    """settings.MIDDLEWARE lists MetricsMiddleware FIRST so it counts what the client is sent,
    the security refusals included. CapabilityHeaderMiddleware refuses any `Meshychat-Capability`
    header with 403 before a view runs; that 403 must move the count by exactly one. Placed
    inside that middleware, the counter never sees the refusal and it goes uncounted (the
    review's mutant MA, 2026-09-26). Control: a request that reaches the URL resolver is
    counted as its own status wherever the counter sits."""
    from django.conf import settings

    client = Client()
    before = parse(_scrape(client).content.decode())
    refused = client.get("/v1/no-such-route-for-metrics", HTTP_MESHYCHAT_CAPABILITY="x")   # not a probe (080)
    assert refused.status_code == 403, refused.content[:200]
    assert refused.json()["error"] == "E_CAPABILITY_UNAVAILABLE"
    assert client.get("/v1/no-such-route-for-metrics").status_code == 404     # control
    after = parse(_scrape(client).content.decode())

    def delta(status):
        key = f'{{status="{status}"}}'
        return (_value(after, "meshychat_http_requests_total", key) or 0) - \
            (_value(before, "meshychat_http_requests_total", key) or 0)

    assert delta(404) == 1, "control: a request that reached the resolver was not counted"
    assert delta(403) == 1, "the capability refusal was not counted: the counter is not outermost"
    assert settings.MIDDLEWARE[0] == "apps.core.metrics.MetricsMiddleware", settings.MIDDLEWARE


def test_the_probe_routes_are_not_counted_and_any_other_route_is():
    """Divergence 080: /v1/health answers 503 by design on a server with no Redis, and a probe
    polled every few seconds made that design the error-rate alert's 5xx share. The two probe
    routes move no series, whatever they answer; an ordinary 5xx, and a probe path spelled any
    other way, still count. Through the real middleware with a stubbed inner response, so the
    statuses are forced rather than whatever this machine's Redis happens to give."""
    from django.http import HttpResponse
    from django.test import RequestFactory

    from apps.core import metrics

    rf = RequestFactory()

    def counts():
        return dict(metrics.HTTP_REQUESTS.samples())

    def through(path, status):
        before = counts()
        metrics.MetricsMiddleware(lambda request: HttpResponse(status=status))(rf.get(path))
        after = counts()
        return {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}

    assert metrics.PROBE_PATHS == {"/v1/health", "/v1/livez"}
    assert through("/v1/health", 503) == {}, "the health probe's 503 was counted"
    assert through("/v1/livez", 503) == {}, "the liveness probe's 503 was counted"
    assert through("/v1/health", 200) == {}, "the health probe's 200 was counted"
    assert through("/v1/memory/write", 503) == {'{status="503"}': 1.0}, "control: an ordinary 503 was not counted"
    assert through("/v1/health/", 404) == {'{status="404"}': 1.0}, "another spelling of a probe path was not counted"
    assert through("/v1/healthz", 500) == {'{status="500"}': 1.0}, "a path that only starts like a probe was not counted"


def test_a_real_health_request_moves_no_series_through_the_real_stack():
    """The same through the real URLconf and MIDDLEWARE: whatever /v1/health and /v1/livez answer
    here, no status series moves, while a 404 beside them moves its own by one (the control)."""
    client = Client()
    before = parse(_scrape(client).content.decode())
    statuses = {client.get("/v1/health").status_code, client.get("/v1/livez").status_code}
    assert client.get("/v1/no-such-route-for-metrics").status_code == 404
    after = parse(_scrape(client).content.decode())

    def delta(status):
        key = f'{{status="{status}"}}'
        return (_value(after, "meshychat_http_requests_total", key) or 0) -             (_value(before, "meshychat_http_requests_total", key) or 0)

    assert delta(404) == 1, "control: the 404 was not counted once"
    for status in statuses - {200, 404}:
        assert delta(status) == 0, f"a probe's {status} was counted"
    assert delta(200) == 1, "only the first scrape's own 200 may move the 200 series"


def test_the_ws_gauge_counts_every_socket_the_connection_manager_holds():
    """`meshychat_ws_connections` is read from apps.wsgw.connection_manager.MANAGER at scrape time.
    Three sockets on two peers must move it by exactly three, and it must fall back when they
    leave (the control): a gauge hard-wired to 0, or reading a manager or attribute that was
    renamed, fails here (the review's mutant MK, 2026-09-26)."""
    import uuid

    from apps.wsgw import connection_manager

    held = connection_manager.MANAGER._by_peer
    base = _value(parse(_scrape().content.decode()), "meshychat_ws_connections")
    a, b = uuid.uuid4(), uuid.uuid4()
    held[a], held[b] = [object(), object()], [object()]
    try:
        during = _value(parse(_scrape().content.decode()), "meshychat_ws_connections")
    finally:
        held.pop(a, None)
        held.pop(b, None)
    after = _value(parse(_scrape().content.decode()), "meshychat_ws_connections")
    assert base is not None and during - base == 3, (base, during)
    assert after == base, (base, after)


# --------------------------------------------------------------------- F28

def _usage(conn) -> float:
    return float(conn.execute("SELECT pg_notification_queue_usage()").fetchone()[0])


def test_the_queue_gauge_reads_the_real_queue_while_a_listener_holds_its_tail():
    channel = f"metrics_probe_{os.getpid()}"
    with psycopg.connect(DSN, autocommit=True) as held, \
            psycopg.connect(DSN, autocommit=True) as writer:
        held.execute(f"LISTEN {channel}")
        held.execute("BEGIN")               # inside a transaction it reads nothing
        held.execute("SELECT 1")
        payload = "x" * 7000
        for _ in range(40):                 # ~35 queue pages: a fraction near 3.5e-5
            writer.execute("SELECT pg_notify(%s, %s)", (channel, payload))
        low = _usage(writer)
        fams = parse(_scrape().content.decode())
        high = _usage(writer)
        got = _value(fams, "meshychat_pg_notification_queue_usage")
        held.execute("ROLLBACK")
        held.execute(f"UNLISTEN {channel}")
    assert low > 0.0, "control: the held listener did not keep the queue non-empty"
    assert got is not None, "no queue series"
    assert min(low, high) <= got <= max(low, high), (low, got, high)


class BlackHole:
    """Accepts connections and never answers: a database that is up at TCP and wedged. With
    `hangup`, it closes each at once instead: a database that is down, answered in
    milliseconds (a closed port costs about two seconds per attempt on Windows)."""

    def __init__(self, hangup: bool = False):
        self.hangup = hangup
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted, self.held = 0, []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.accepted += 1
            if self.hangup:
                conn.close()
            else:
                self.held.append(conn)

    def close(self):
        self._stop.set()
        self._t.join(2)
        for c in self.held:
            with contextlib.suppress(OSError):
                c.close()
        self.sock.close()


def test_an_unreachable_database_is_db_up_0_and_no_queue_series():
    down = BlackHole(hangup=True)
    try:
        with override_settings(MESHY_DSN=f"postgresql://meshychat_owner:x@127.0.0.1:{down.port}/nope"):
            fams = parse(_scrape().content.decode())
    finally:
        down.close()
    assert down.accepted >= 1, "control: the scrape did not try the database it was given"
    assert _value(fams, "meshychat_db_up") == 0.0
    assert fams["meshychat_pg_notification_queue_usage"]["samples"] == {}, \
        "an unmeasured queue must be absent, never 0"
    fams = parse(_scrape().content.decode())       # positive control: database back
    assert _value(fams, "meshychat_db_up") == 1.0
    assert _value(fams, "meshychat_pg_notification_queue_usage") is not None


def test_a_wedged_database_costs_one_probe_and_every_scrape_keeps_its_deadline():
    """Five scrapes at once against a database that accepts and never answers: ONE
    connection reaches it (they share the read in flight), and every scrape answers
    db_up 0 within the deadline. Control: the same five against the real database answer 1."""
    from apps.core import metrics

    def burst(n=5):
        results, took = [None] * n, [None] * n
        go = threading.Barrier(n)

        def one(i):
            go.wait()
            t0 = time.monotonic()
            results[i] = parse(_scrape().content.decode())
            took[i] = time.monotonic() - t0

        threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return [_value(f, "meshychat_db_up") for f in results], took

    hole = BlackHole()
    try:
        with override_settings(MESHY_DSN=f"postgresql://meshychat_owner:x@127.0.0.1:{hole.port}/nope"):
            ups, took = burst()
        assert ups == [0.0] * 5, ups
        assert hole.accepted == 1, f"{hole.accepted} probes against one wedged database"
        assert max(took) < metrics.DB_DEADLINE_S + 1.5, took
    finally:
        hole.close()
    # The abandoned read returns (libpq's connect_timeout, then the hang-up above), and the
    # next scrapes measure again: the positive control.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ups, _ = burst()
        if ups == [1.0] * 5:
            break
        time.sleep(0.25)
    assert ups == [1.0] * 5, ups


# ------------------------------------------------ the scrape config and alert rules

def rule_problems(text: str, emitted: set[str]) -> list[str]:
    """Every alert rule in `text` must have one single-line expr naming only emitted
    `meshychat_` series (or Prometheus's own `up`), a `for`, a severity and a summary."""
    problems, rules = [], re.split(r"(?m)^\s*- alert:\s*", text)[1:]
    if not rules:
        return ["no alert rules"]
    for rule in rules:
        name = rule.splitlines()[0].strip()
        exprs = re.findall(r"(?m)^\s*expr:\s*(.+)$", rule)
        if len(exprs) != 1 or exprs[0].strip() in ("|", ">", "|-", ">-"):
            problems.append(f"{name}: needs exactly one single-line expr")
            continue
        expr = re.sub(r"\{[^}]*\}", "", exprs[0])
        for series in set(re.findall(r"\bmeshychat_[a-z0-9_]+\b", expr)):
            if series not in emitted:
                problems.append(f"{name}: names {series}, which this server does not emit")
        for field in (r"for:\s*\d+[smh]", r"severity:\s*(critical|warning)", r"summary:\s*\S"):
            if not re.search(r"(?m)^\s*" + field, rule):
                problems.append(f"{name}: missing {field}")
    return problems


def _emitted() -> set[str]:
    return set(parse(_scrape().content.decode()))


def test_the_rule_checker_catches_a_rule_naming_a_series_nobody_emits():
    planted = ("groups:\n- name: g\n  rules:\n  - alert: Ghost\n    expr: meshychat_nope_total > 0\n"
               "    for: 5m\n    labels:\n      severity: critical\n    annotations:\n"
               "      summary: never\n")
    assert rule_problems(planted, _emitted()) == [
        "Ghost: names meshychat_nope_total, which this server does not emit"]
    ok = planted.replace("meshychat_nope_total", "meshychat_db_up")   # control: the same rule, real
    assert rule_problems(ok, _emitted()) == []


def test_the_shipped_alert_rules_name_only_emitted_series():
    text = ALERTS.read_text(encoding="utf-8")
    assert rule_problems(text, _emitted()) == []
    for series in ("meshychat_pg_notification_queue_usage", "meshychat_db_up", "up{",
                   "meshychat_http_requests_total"):
        assert series in text, f"no rule watches {series}"
    exprs = _rule_exprs(text)
    unmeasured = exprs.get("MeshyChatNotifyQueueUnmeasured", "")
    # An unmeasured queue must alert too (the gauge is absent, not 0, when unread), and PER
    # TARGET: `absent(<series>)` fires only when EVERY target has lost the reading, so with
    # two instances one blind instance never paged (review of e983a9a, LOW 6).
    assert re.search(r"\bup\{[^}]*\}\s*==\s*1\s+unless\s+on\s*\(\s*instance\s*,\s*job\s*\)\s*"
                     r"meshychat_pg_notification_queue_usage\{", unmeasured), unmeasured
    assert "absent(" not in unmeasured, unmeasured
    # ...and a job with no target at all, which no per-target rule can see, pages on its own.
    assert any(re.fullmatch(r'absent\(up\{job="meshychat"\}\)', e.strip())
               for e in exprs.values()), exprs


def _rule_exprs(text: str) -> dict[str, str]:
    """{alert name: its one-line expr}."""
    out = {}
    for rule in re.split(r"(?m)^\s*- alert:\s*", text)[1:]:
        expr = re.search(r"(?m)^\s*expr:\s*(.+)$", rule)
        out[rule.splitlines()[0].strip()] = expr.group(1) if expr else ""
    return out


def test_the_shipped_scrape_config_matches_the_route_and_keeps_the_token_out_of_the_file():
    text = SCRAPE.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*metrics_path:\s*/v1/metrics\s*$", text)
    assert re.search(r"(?m)^\s*credentials_file:\s*\S", text), "the token comes from a file"
    assert not re.search(r"(?m)^\s*(credentials|bearer_token):\s", text), "inline token"
    # E01, operator decision 55 (2026-09-27): HQB's Prometheus scrapes over plain http on the
    # LAN; after cutover there is no TLS. The change carries its date in the file.
    assert re.search(r"(?m)^\s*scheme:\s*http\s*$", text), "scheme is not http"
    assert re.search(r"(?m)^\s*#.*2026-09-27.*$", text), "the scheme change is not dated"
    job = re.search(r"(?m)^\s*-\s*job_name:\s*(\S+)\s*$", text).group(1)
    assert f'job="{job}"' in ALERTS.read_text(encoding="utf-8")


# ------------------------------------------- F04: the send queue's drops are counted
#
# Operator decision 60, 2026-09-27 ("count it, fix terminals"): the drop-oldest policy stays
# silent on the wire, so a full send queue does not alter the frame protocol seen by clients, and
# every frame it drops is COUNTED, where
# the writer's "WS send queue dropped" WARNING fires, as meshychat_ws_send_queue_dropped_total.

def _drive_writer(frames: int, capacity: int) -> tuple[list, list]:
    """Push `frames` frames into a consumer's queue of `capacity` BEFORE its writer starts
    (so the queue overflows deterministically, no timing), then run the real
    `_writer_loop` until it has written what survived. Returns (written, warnings)."""
    import asyncio
    import json
    import logging
    import uuid

    from apps.wsgw.connection_manager import ConnectionManager
    from apps.wsgw.consumer import HubConfig, MeshyWebSocketConsumer
    from apps.wsgw.presence import RecordingPresenceBackend
    from apps.wsgw.send_queue import SendQueue

    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    async def run():
        config = HubConfig(jwt_public_key_pem=None, jwt_issuer=None, dpop_enforce=False,
                           check_peer_active=False, zero_rtt=False)
        consumer = MeshyWebSocketConsumer(config=config, manager=ConnectionManager(),
                                         presence=RecordingPresenceBackend())
        consumer.peer_id = uuid.uuid4()
        consumer.queue = SendQueue(capacity=capacity)
        for i in range(frames):
            consumer.queue.push_nowait({"type": "pong", "i": i})
        survivors = min(frames, capacity)
        written: list = []
        done = asyncio.Event()

        async def record(text_data=None, bytes_data=None, close=False):
            written.append(json.loads(text_data))
            if len(written) == survivors:
                done.set()

        consumer.send = record
        writer = asyncio.create_task(consumer._writer_loop())
        try:
            await asyncio.wait_for(done.wait(), 10)
        finally:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        return written

    from apps.wsgw import consumer as consumer_module
    logger = consumer_module.log              # the logger the writer warns on
    grab = Grab(level=logging.WARNING)
    logger.addHandler(grab)
    try:
        written = asyncio.run(run())
    finally:
        logger.removeHandler(grab)
    warnings = [r.getMessage() for r in records
                if r.levelno == logging.WARNING and "WS send queue dropped" in r.getMessage()]
    return written, warnings


def _dropped_total() -> float | None:
    return _value(parse(_scrape().content.decode()), "meshychat_ws_send_queue_dropped_total")


def test_a_forced_send_queue_drop_is_counted_where_the_warning_fires():
    """Five frames into a queue of two: the drop-oldest policy discards frames 0..2, the
    writer writes 3 and 4, logs ONE warning naming three frames, and the counter moves by
    exactly three. CONTROL: two frames into the same capacity drop nothing, warn nothing,
    and leave the counter where it was. Mutant M-F04 (the increment removed) fails here."""
    before = _dropped_total()
    assert before is not None, "meshychat_ws_send_queue_dropped_total is not exposed"
    written, warnings = _drive_writer(frames=5, capacity=2)
    assert [f["i"] for f in written] == [3, 4], written
    assert len(warnings) == 1 and "dropped 3 frame(s)" in warnings[0], warnings
    after = _dropped_total()
    assert after - before == 3, (before, after)

    written, warnings = _drive_writer(frames=2, capacity=2)          # positive control
    assert [f["i"] for f in written] == [0, 1] and warnings == [], (written, warnings)
    assert _dropped_total() == after, "the counter moved with nothing dropped"


def test_the_drop_counter_is_a_counter_and_never_decreases():
    from apps.core import metrics

    fams = parse(_scrape().content.decode())
    assert fams["meshychat_ws_send_queue_dropped_total"]["type"] == "counter"
    with pytest.raises(ValueError):
        metrics.WS_SEND_QUEUE_DROPPED.inc(-1)
    assert _value(parse(_scrape().content.decode()),
                  "meshychat_ws_send_queue_dropped_total") == \
        _value(fams, "meshychat_ws_send_queue_dropped_total")            # control: unchanged


# ------------------------------------ E01: server-side liveness, from the lease rows
#
# Operator decision 55, 2026-09-27: full alerting before cutover. `run_jobs --explain` reads
# `background_job_leases` through apps/jobs/explain.py; `run_call_worker --health` reads the
# deadline sweep's row there too (leases.seconds_since_finished). The gauges read the SAME
# functions, so they cannot describe a scheduler the operator commands would not. Every
# fixture timestamp is the DATABASE's (now() in SQL); no test clock is compared with it.

JOB = "pairing_code_sweep"          # a registered job whose body nothing here runs
SWEEP_JOB = "call_deadline_sweep"   # the call worker's job (apps/calls/worker.JOBS[0])


@pytest.fixture()
def owner():
    with psycopg.connect(os.environ.get("MESHY_TEST_DSN", DSN), autocommit=True) as c:
        yield c


@pytest.fixture()
def lease_row(owner):
    """`put(job, col=sql_expr, ...)` puts a registered job's lease row into an exact state
    (every timestamp computed by the database); `put.drop(job)` deletes it. Every row it
    touched is put back as it was found."""
    saved: dict = {}

    def remember(job):
        if job not in saved:
            saved[job] = owner.execute(
                "SELECT row_to_json(l)::text FROM background_job_leases l "
                "WHERE job_name = %s", (job,)).fetchone()

    def put(job, **cols):
        remember(job)
        owner.execute("INSERT INTO background_job_leases (job_name, next_run_at) "
                      "VALUES (%s, now()) ON CONFLICT (job_name) DO NOTHING", (job,))
        sets = ", ".join(f"{col} = {expr}" for col, expr in cols.items())
        owner.execute(f"UPDATE background_job_leases SET {sets} WHERE job_name = %s", (job,))

    def drop(job):
        remember(job)
        owner.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job,))

    put.drop = drop
    yield put
    for job, row in saved.items():
        owner.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job,))
        if row is not None:
            owner.execute("INSERT INTO background_job_leases "
                          "SELECT * FROM json_populate_record(NULL::background_job_leases, "
                          "%s::json)", (row[0],))


def _db_now(owner):
    return owner.execute("SELECT now()").fetchone()[0]


def _seconds_between(earlier, later) -> float:
    """Subtract in UTC. psycopg returns both in the server's TimeZone, and Python subtracts
    two datetimes sharing a zone as wall-clock times, so an interval that straddles a
    clock change reads an hour off."""
    utc = _dt.timezone.utc
    return (later.astimezone(utc) - earlier.astimezone(utc)).total_seconds()


def _job_gauge(job: str) -> float | None:
    return _value(parse(_scrape().content.decode()), "meshychat_job_claimable_seconds",
                  f'{{job_name="{job}"}}')


def test_the_jobs_gauge_is_run_jobs_explain_over_the_lease_row(owner, lease_row):
    """`meshychat_job_claimable_seconds{job_name}` is explain's `due_for_secs` for a job that is
    claimable, 0 for one that is held or not due, and +Inf for one no process has ever
    registered. Each state is written into the row and read back through the route.

    The held case is the one a second predicate gets wrong: due by the clock, but a live
    lease means something IS running it. Mutant M-E01j (the gauge reads next_run_at alone,
    ignoring the lease) reports it overdue and fails here."""
    from apps.jobs import explain

    # Claimable for 400 s: past run_jobs' nothing_is_ticking threshold (300 s).
    lease_row(JOB, next_run_at="now() - interval '400 seconds'", holder="NULL",
              leased_until="NULL")
    set_at = owner.execute("SELECT next_run_at FROM background_job_leases "
                           "WHERE job_name = %s", (JOB,)).fetchone()[0]
    got = _job_gauge(JOB)
    bound = _seconds_between(set_at, _db_now(owner))
    assert got is not None, "no meshychat_job_claimable_seconds series for the job"
    assert 400 <= got <= bound, (got, bound)
    report = explain.explain(owner, JOB)
    assert report.nothing_is_ticking and got > explain.STALE_DUE_SECS, report

    # Held by a live lease although due: not claimable, so 0.
    lease_row(JOB, holder="'metrics-test'", leased_until="now() + interval '1 hour'")
    assert explain.explain(owner, JOB).reason == explain.HELD_BY_ANOTHER
    assert _job_gauge(JOB) == 0.0

    # Not due for an hour: 0 (the held case is not the only way to 0).
    lease_row(JOB, holder="NULL", leased_until="NULL",
              next_run_at="now() + interval '1 hour'")
    assert _job_gauge(JOB) == 0.0

    # Claimable a few seconds: positive, under the threshold (a gauge stuck at 0 fails).
    lease_row(JOB, next_run_at="now() - interval '5 seconds'")
    small = _job_gauge(JOB)
    assert small is not None and 5 <= small < explain.STALE_DUE_SECS, small

    # Never registered: nothing has ever ticked it.
    lease_row.drop(JOB)
    assert explain.explain(owner, JOB).reason == explain.NOT_REGISTERED
    assert _job_gauge(JOB) == float("inf")


def test_every_registered_job_has_exactly_one_series():
    from apps.jobs import registry

    samples = parse(_scrape().content.decode())["meshychat_job_claimable_seconds"]["samples"]
    assert sorted(samples) == sorted(f'{{job_name="{j.name}"}}' for j in registry.JOBS)


def test_the_call_worker_gauge_is_run_call_worker_health_sweep_age(owner, lease_row):
    """`meshychat_call_sweep_age_seconds` is `leases.seconds_since_finished` on the sweep's row,
    the liveness half of `run_call_worker --health`: seconds since the sweep last FINISHED,
    +Inf when it never has (which --health also reads as unhealthy).

    The fixture starts a tick just now and finished the last one 90 s ago: a worker stuck
    mid-tick. Mutant M-E01c (reading last_started_at) reports it fresh and fails here."""
    from apps.calls import worker
    from apps.jobs import leases

    assert worker.JOBS[0].name == SWEEP_JOB
    lease_row(SWEEP_JOB, last_started_at="now()",
              last_finished_at="now() - interval '90 seconds'")
    finished = owner.execute("SELECT last_finished_at FROM background_job_leases "
                             "WHERE job_name = %s", (SWEEP_JOB,)).fetchone()[0]
    got = _value(parse(_scrape().content.decode()), "meshychat_call_sweep_age_seconds")
    bound = _seconds_between(finished, _db_now(owner))
    assert got is not None, "no meshychat_call_sweep_age_seconds series"
    assert 90 <= got <= bound, (got, bound)
    assert not worker.is_healthy(None, got), "--health would call this age healthy"

    lease_row(SWEEP_JOB, last_finished_at="now()")                    # positive control
    fresh = _value(parse(_scrape().content.decode()), "meshychat_call_sweep_age_seconds")
    assert fresh is not None and 0 <= fresh < worker.SWEEP_STALE_ALERT_SECS, fresh
    assert worker.is_healthy(None, fresh)

    lease_row(SWEEP_JOB, last_finished_at="NULL")
    assert leases.seconds_since_finished(owner, SWEEP_JOB) is None
    assert _value(parse(_scrape().content.decode()),
                  "meshychat_call_sweep_age_seconds") == float("inf")


def test_an_unreadable_database_leaves_the_liveness_gauges_absent_never_0():
    """0 is the healthiest value these gauges can take, so an unread one must be ABSENT
    (MeshyChatDatabaseUnreadable pages on meshychat_db_up 0 instead). Control: back on the real
    database, both families have samples."""
    down = BlackHole(hangup=True)
    try:
        with override_settings(MESHY_DSN=f"postgresql://meshychat_owner:x@127.0.0.1:{down.port}/nope"):
            fams = parse(_scrape().content.decode())
    finally:
        down.close()
    assert _value(fams, "meshychat_db_up") == 0.0
    for name in ("meshychat_job_claimable_seconds", "meshychat_call_sweep_age_seconds"):
        assert name in fams and fams[name]["samples"] == {}, (name, fams.get(name))
    fams = parse(_scrape().content.decode())                          # positive control
    assert fams["meshychat_job_claimable_seconds"]["samples"]
    assert _value(fams, "meshychat_call_sweep_age_seconds") is not None


# ------------------------------------ E01: the rules for those series, and the scrape

def job_selector_problems(text: str, job: str) -> list[str]:
    """Every `meshychat_` series and every `up` in every rule's expr must select `job="<job>"`,
    the scrape config's job: a rule without it reads every job Prometheus scrapes, and one
    naming another job never fires for this one."""
    problems = []
    for name, expr in _rule_exprs(text).items():
        for series, labels in re.findall(r"\b(meshychat_[a-z0-9_]+|up)\b(\{[^}]*\})?", expr):
            if f'job="{job}"' not in (labels or ""):
                problems.append(f"{name}: {series} does not select job=\"{job}\"")
    return problems


def _scrape_job() -> str:
    return re.search(r"(?m)^\s*-\s*job_name:\s*(\S+)\s*$",
                     SCRAPE.read_text(encoding="utf-8")).group(1)


def test_the_job_selector_check_catches_a_rule_without_one():
    planted = ("groups:\n- name: g\n  rules:\n  - alert: Loose\n    expr: meshychat_db_up == 0\n"
               "    for: 5m\n    labels:\n      severity: critical\n    annotations:\n"
               "      summary: s\n")
    assert job_selector_problems(planted, "meshychat") == [
        'Loose: meshychat_db_up does not select job="meshychat"']
    ok = planted.replace("meshychat_db_up ==", 'meshychat_db_up{job="meshychat"} ==')   # control
    assert job_selector_problems(ok, "meshychat") == []


def test_the_liveness_and_drop_rules_ship_parse_and_select_the_scrape_job():
    """The three rules this batch adds: each parses under rule_problems (one-line expr, only
    emitted series, a for, a severity, a summary), every series in every rule selects the
    scrape config's job, and the thresholds are the operator commands' own."""
    from apps.calls import worker
    from apps.jobs import explain

    text = ALERTS.read_text(encoding="utf-8")
    assert rule_problems(text, _emitted()) == []
    assert job_selector_problems(text, _scrape_job()) == []
    exprs = _rule_exprs(text)
    jobs = exprs.get("MeshyChatJobsNotTicking", "")
    assert re.search(r"meshychat_job_claimable_seconds\{[^}]*\}\)?\s*>\s*"
                     + str(explain.STALE_DUE_SECS) + r"\b", jobs), jobs
    stale = exprs.get("MeshyChatCallWorkerStale", "")
    assert re.search(r"meshychat_call_sweep_age_seconds\{[^}]*\}\s*>\s*"
                     + str(worker.SWEEP_STALE_ALERT_SECS) + r"\b", stale), stale
    drops = exprs.get("MeshyChatWsSendQueueDropping", "")
    assert re.search(r"increase\(meshychat_ws_send_queue_dropped_total\{[^}]*\}\[10m\]\)\s*>\s*0",
                     drops), drops
