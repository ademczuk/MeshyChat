"""URL map, plus `GET /v1/health` - the deep liveness probe.

Health endpoint contract:

  * path            `/v1/health`, GET only, UNAUTHENTICATED, outside both the
                    HMAC-enforced peer group and the owner-auth group.
  * body            `{"status","version","git_sha","git_dirty","db","redis","blob"}` in
                    that order. Field order is part of the wire, not a detail.
                    PLUS, AFTER `blob` (rule B9, operator decision 2026-09-25, divergence
                    041's dated paragraph): `code_sha256`, the hash of
                    the code this process serves (`code_fingerprint.running()`, fixed at
                    start); `worker_code_sha256`, the hash `run_call_worker` last recorded
                    on the deadline sweep's lease row (sql_meshychat/022), null when none
                    or when the read failed or missed the deadline; `worker_code_stale`,
                    true when the two differ, null when either is unknown. A stale worker
                    does NOT change `status` or the status code: this process serves
                    requests whatever the worker runs, and a load balancer must not evict
                    it for that. `run_call_worker --health` is where a stale worker fails.
                    AND LAST (divergence 040, slice 6, Q11): while capability tokens are
                    on, `"capabilities":["object-capability-v1"]` follows B9's three; off,
                    it is absent. The conditional field is last so that the always-present
                    ten are one fixed prefix either way.
  * status          200 when db && redis && blob, else 503.
  * blob            for the fs backend, blob_dir is a directory AND (divergence 047,
                    ASDLC OI-48), a file under it for each of the newest
                    BLOB_READY_ROWS_CAP ready `file_artifacts` rows.
                    Still a JSON bool: the wire's shape is unchanged, and more cases answer
                    false. How many files are missing goes to the log, counts only.
  * status field    "ok" when all three pass, otherwise "degraded".
   * timeout         each dependency is probed concurrently under a 2s bound, so the route
                    returns within ~2s even with a wedged backend.
   * no secrets      nothing here reads a credential
                    into the response. An UNCONFIGURED dependency is reported as
                    `false` + "degraded" + 503, never as healthy: the whole reason this
                    handler exists is that the old one "always returned 200 (even with
                    db:false), so a status-200 health check could never evict a dead
                    instance behind a load balancer". Reporting an absent Redis as
                    healthy would re-create exactly that defect.

`GET /v1/livez` - the load balancer's route (docs/DIVERGENCES.md 035 is /v1/livez).
It exists because the rule above, correct for the deep probe, makes a node without Redis or
blob storage answer 503 "degraded" on every check, and a load balancer pointed at /v1/health
evicts a node that is serving requests (status item E06). The operator ruled: /v1/health keeps
its seven base fields. B9 added three after `blob` (2026-09-25); while tokens are on,
divergence 040's `capabilities` is last; divergence 047 (2026-09-27) widens what an fs store's
`blob` checks without adding a field. This route
answers the one question a load balancer needs answered - does this process reach its
database? It is a READINESS answer: a check that restarts the process on a 503 would
restart every node in a loop for the length of a database outage.

  * path            `/v1/livez`, GET and HEAD only, UNAUTHENTICATED, no trailing slash.
  * body            `{"status":"ok"}` with 200 when `SELECT 1` answers, otherwise
                    `{"status":"unavailable"}` with 503. Nothing else: no version, no
                    dependency flags, nothing read from the DSN.
  * probe           `dbconn.ping`, as /v1/health's db probe, answered within ONE probe
                    timeout, on the route's own threads (never /v1/health's pool).
  * single flight   concurrent checks share the probe in flight, and a finished answer is
                    served again for LIVEZ_REUSE_S, so the database's load does not follow
                    the request rate of an unauthenticated caller.
  * abandonment     a probe past its deadline is abandoned, not waited on: the next check
                    starts a new one. At most LIVEZ_MAX_PROBES probe threads exist; at the
                    bound a check answers 503 without probing.
"""
from __future__ import annotations

import time

import json
import logging
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import unquote, urlsplit

import psycopg
from django.http import HttpResponse
from psycopg.conninfo import make_conninfo
from django.urls import include, path
from django.views.decorators.http import require_http_methods

from apps.core import dbconn
from apps.core.env import lookup
from apps.core.metrics import metrics_view

log = logging.getLogger("meshychat.health")

#: Each dependency probe has a two-second bound.
PROBE_TIMEOUT = 2.0

#: Four workers so the three probes and B9's worker-code read really are concurrent.
#: Module-level rather than per-request so a burst of
#: probes cannot spawn unbounded threads. Every probe is additionally bounded by its own
#: socket and statement timeouts, so a worker always returns and the pool cannot silt up.
_PROBES = ThreadPoolExecutor(max_workers=4, thread_name_prefix="health-probe")

#: /v1/livez runs ONE probe, so its whole-request bound is one probe timeout, the same as
#: /v1/health's (HEALTH_DEADLINE: its three probes and B9's worker-code read run concurrently).
LIVEZ_DEADLINE = PROBE_TIMEOUT

#: /v1/health's whole-set bound. The probes (and B9's worker-code read, which shares it) run
#: concurrently, each under PROBE_TIMEOUT, so the set answers within one of them
#: even if one backend is wedged. It was `PROBE_TIMEOUT * len(PROBES) if 'PROBES'
#: in globals() else PROBE_TIMEOUT * 3`: no `PROBES` exists (the executor is `_PROBES`), so the
#: first arm was dead and the bound was always 6 s (status item F17).
HEALTH_DEADLINE = PROBE_TIMEOUT

#: How long a finished /v1/livez answer is served again instead of probing again. Every
#: probe is a new database login (connect, password, SET ROLE, role check, SELECT 1: 34 ms
#: median in review), and the route is unauthenticated: without this, review measured
#: ~59 requests/s queueing a load balancer's check past its deadline. With it, the database
#: sees about one probe a second from this route whatever the request rate.
LIVEZ_REUSE_S = 1.0

#: Probe threads alive at once: the one checks share, plus any abandoned at their deadline
#: that have not returned. Each can hold a database connection opened for an anonymous
#: caller, so this is the bound on those. /v1/livez never uses `_PROBES`: review measured a
#: slow-drip Redis holding one of those workers for 21 s. Its old fixed pool of two was a
#: bound too, and two probes stalled mid-reply held it for as long as their connections
#: lived, answering 503 with the database healthy again (review F1).
LIVEZ_MAX_PROBES = 4

#: libpq settings for the probe's connection ONLY. They never decide an answer - the
#: deadline does. They decide how soon an ABANDONED probe returns and frees its slot when
#: the network lost the database's reply: a dead peer is detected after about 1 s idle plus
#: 3 unanswered 1 s probes (Windows ignores the count and sends 10), and on Linux
#: tcp_user_timeout also bounds data left unacknowledged. A peer that ACKs and never
#: answers is not detected by these; LIVEZ_MAX_PROBES is the bound for that case.
LIVEZ_TCP = {"keepalives": 1, "keepalives_idle": 1, "keepalives_interval": 1,
             "keepalives_count": 3, "tcp_user_timeout": 4000}

#: How many ready `file_artifacts` rows /v1/health's blob probe looks for under blob_dir,
#: newest first (divergence 047, ASDLC OI-48): the bound on its cost, one stat per row read.
#: Read at call time, so a test can lower it.
BLOB_READY_ROWS_CAP = 1000

#: The warning when ready rows have no file. Counts only, never a key (a key names its
#: owner). Its arguments are (missing, rows read, cap), so a test reads them by position,
#: not from the text.
BLOB_FILES_MISSING_LOG = ("/v1/health: %d of the newest %d ready file row(s) (at most %d "
                          "are read) have no file under blob_dir; reporting blob=false")


def _redis_url() -> str:
    """Return the configured Redis URL (`MESHY_REDIS_URL`)."""
    return (lookup("MESHY_REDIS_URL") or "").strip()


def _read_line(sock) -> bytes:
    buf = bytearray()
    while not buf.endswith(b"\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("redis closed the connection mid-reply")
        buf += chunk
        if len(buf) > 4096:
            raise ConnectionError("redis reply line too long")
    return bytes(buf)


def _resp(*args: str) -> bytes:
    out = [b"*%d\r\n" % len(args)]
    for a in args:
        raw = a.encode()
        out.append(b"$%d\r\n%s\r\n" % (len(raw), raw))
    return b"".join(out)


def probe_redis(timeout: float = PROBE_TIMEOUT) -> bool:
    """Liveness of the Redis the WS fan-out publishes on.

    A real command round trip rather than a connection-exists check, so this sends DBSIZE
    rather than PING.

    Spoken over a raw socket because this venv has no Redis client library; the wire
    protocol is the contract, and inventing a dependency to speak it would be a larger
    guess than the twelve lines below.

    An unset URL returns False. An unconfigured dependency is not healthy, even though the
    route still answers.
    """
    url = _redis_url()
    if not url:
        log.warning("/v1/health: MESHY_REDIS_URL is unset; reporting redis=false "
                    "(an unconfigured dependency is not a healthy one)")
        return False
    parts = urlsplit(url)
    if parts.scheme not in ("redis", "rediss"):
        log.warning("/v1/health: unsupported redis scheme %r; reporting redis=false",
                    parts.scheme)
        return False
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 6379
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        if parts.scheme == "rediss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        if parts.password:
            pw = unquote(parts.password)
            cmd = (_resp("AUTH", unquote(parts.username), pw) if parts.username
                   else _resp("AUTH", pw))
            sock.sendall(cmd)
            if not _read_line(sock).startswith(b"+"):
                log.warning("/v1/health: redis AUTH rejected; reporting redis=false")
                return False
        sock.sendall(_resp("DBSIZE"))
        # RESP integer reply, e.g. b":0\r\n". Anything else (b"-ERR ...") is a failure.
        return _read_line(sock).startswith(b":")
    except Exception as exc:
        log.warning("/v1/health: redis probe failed: %s: %s", type(exc).__name__, exc)
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def probe_blob(timeout: float = PROBE_TIMEOUT) -> bool:
    """Liveness of the blob backend: the `health_check` of the store built AT STARTUP
    (apps/files/apps.py -> blob.activate).

    fs  -> the startup blob_dir is a directory, THEN (divergence 047, ASDLC OI-48), a
           file under it for each of the newest
           BLOB_READY_ROWS_CAP ready rows (`ready_files_missing`). Production answered
           `blob: true` for six hours while both of its stored files were missing: the
           directory existed, and the files had never been placed in it. False when a
           ready row's file is missing, and false when the rows cannot be read (E1: a
           check that could not look is not a pass).
    r2  -> the constructed, credential-bearing client is the liveness signal. A store that
           could not be constructed
           stopped the process at startup (B11), so a running r2 server has one. No row
           is compared with it: that would be a HEAD, a network round trip, per row.
    none -> False: MESHYCHAT_BLOB_BACKEND was unset at startup. This documented configuration
           exception starts without blob storage (docs/DIVERGENCES.md 035).

    It does not read the environment (review of c007c45, B11): it used to, so it could
    report `blob: false` while every file route served from the startup store, or `true`
    on a server that has no store at all.
    """
    from apps.files import blob as blobmod
    try:
        _cfg, store = blobmod.active()
    except blobmod.BlobError:
        log.warning("/v1/health: no blob store was built at startup "
                    "(MESHYCHAT_BLOB_BACKEND unset); reporting blob=false")
        return False
    try:
        store.health_check()
    except (blobmod.BlobError, OSError) as exc:
        log.warning("/v1/health: blob health check failed: %s", exc)
        return False
    if not isinstance(store, blobmod.FsBlobStore):
        return True
    try:
        read, missing = ready_files_missing(store, timeout)
    except (psycopg.Error, OSError) as exc:
        log.warning("/v1/health: could not compare the ready file rows with blob_dir "
                    "(%s: %s); reporting blob=false", type(exc).__name__, exc)
        return False
    if missing:
        log.warning(BLOB_FILES_MISSING_LOG, missing, read, BLOB_READY_ROWS_CAP)
        return False
    return True


def ready_files_missing(store, timeout: float = PROBE_TIMEOUT) -> tuple[int, int]:
    """(rows read, rows whose file is missing): the newest BLOB_READY_ROWS_CAP ready
    `file_artifacts` rows against the fs `store`'s blob_dir (divergence 047, ASDLC OI-48).
    Counts only; no key leaves this function.

    COST, BOUNDED however large the table or the store grows:
      * one connection, opened as `probe_worker_code`'s is: connect and statement timeouts
        of one probe timeout, the application role;
      * one read of at most BLOB_READY_ROWS_CAP keys (`repository.newest_ready_keys`), a
        sequential scan of `file_artifacts` while that table is small (divergence 047 says
        when to add the index that bounds it);
      * one stat per key, no file opened (`FsBlobStore.missing_keys`);
      * only when a file is missing, one probe of the unique r2_key index per missing key
        (`repository.still_ready`).
    What the bound costs: rows past the cap, the OLDEST, are not looked for. An empty or
    partial directory (a copy that never happened, a wrong MESHYCHAT_BLOB_DIR, a lost volume)
    shows in the newest rows as much as in the oldest; a single old file lost on a store
    of more than a thousand is not seen.

    WHY THE RE-READ. Every delete removes the row BEFORE the file
    (`FileService.soft_delete`; the grace sweep), so a delete landing between the read of
    the rows and the stat of the files would look like a lost file. A missing key is
    counted only if its row is still ready afterwards: a key read ready, deleted, then not
    found is a delete in flight, and a check that fires on a race gets muted. Uploads
    cannot race this way: a row turns ready only after its file is written (finalize reads
    it with `head`)."""
    import contextlib

    from apps.files import repository as repo
    with contextlib.closing(dbconn.connect(connect_timeout=timeout,
                                           statement_timeout_ms=int(timeout * 1000))) as conn:
        keys = repo.newest_ready_keys(conn, BLOB_READY_ROWS_CAP)
        if isinstance(keys, repo.Absent):
            return 0, 0
        gone = store.missing_keys(keys)
        if not gone:
            return len(keys), 0
        still = repo.still_ready(conn, gone)
        return len(keys), (0 if isinstance(still, repo.Absent) else len(still))


def probe_db(timeout: float = PROBE_TIMEOUT) -> bool:
    """`SELECT 1` on a connection opened the way request connections are opened."""
    return dbconn.ping(timeout=timeout)


#: What /v1/health advertises while object-capability tokens are on (the project's design log (not part of this release) 040,
#: slice 6; design B07 Q11): a client reads it before it sends `Meshychat-Capability`, and refuses
#: to send one to a server that does not list it (tools/meshy_node). This prevents a token
#: from being silently ignored.
CAPABILITIES = ("object-capability-v1",)


def advertised_capabilities() -> list[str] | None:
    """The `capabilities` field, or None to leave it OUT. Present only when tokens are on AND
    a root key store is loaded: the test the token routes make (apps/memoryskills/views.py,
    apps/calls/object_capability.py), so the field never promises what they would refuse.
    ABSENT when off, not an empty list: the body then ends at rule B9's three fields, after
    the fixed fields, byte for byte. With tokens off nothing of the capability code is
    imported."""
    from apps.core import capability_header
    if not capability_header.tokens_on():
        return None
    from apps.memoryskills import capability_keys
    return list(CAPABILITIES) if capability_keys.current() is not None else None


def probe_worker_code(timeout: float = PROBE_TIMEOUT) -> str | None:
    """The code hash `run_call_worker` last recorded (rule B9, sql_meshychat/022), on a
    connection bounded as `probe_db`'s is; None when none was recorded."""
    import contextlib

    from apps.calls import worker
    with contextlib.closing(dbconn.connect(connect_timeout=timeout,
                                           statement_timeout_ms=int(timeout * 1000))) as conn:
        return worker.read_worker_code(conn)[0]


@require_http_methods(["GET", "HEAD"])
def health(request):
    deadline = time.monotonic() + HEALTH_DEADLINE
    from django.conf import settings

    # Submitted together, collected together: concurrent.
    futures = [_PROBES.submit(probe_db), _PROBES.submit(probe_redis), _PROBES.submit(probe_blob)]
    worker_code_future = _PROBES.submit(probe_worker_code)
    results = []
    for fut, name in zip(futures, ("db", "redis", "blob")):
        try:
            # DEADLINE, not a per-future timeout. Measured in review: a slow-drip Redis
            # kept probe_redis running 21 s against a 2 s "timeout" (settimeout is
            # per-recv, and the reader loops), and three serial waits stack. fut.result()
            # does not cancel the worker, so the only honest bound is a wall-clock budget
            # for the whole set, after which the remaining probes are reported UNKNOWN
            # rather than waited on.
            remaining = max(0.0, deadline - time.monotonic())
            results.append(bool(fut.result(timeout=remaining)))
        except FutureTimeout:
            log.warning("/v1/health: %s probe exceeded %.0fs", name, PROBE_TIMEOUT)
            results.append(False)
        except Exception as exc:
            log.warning("/v1/health: %s probe raised %s: %s", name, type(exc).__name__, exc)
            results.append(False)
    db, redis_ok, blob = results
    # B9: read under the same deadline; unknown (null) when it fails or misses it.
    try:
        worker_code = worker_code_future.result(
            timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeout:
        log.warning("/v1/health: the worker code read exceeded the deadline")
        worker_code = None
    except Exception as exc:
        log.warning("/v1/health: the worker code read raised %s: %s", type(exc).__name__, exc)
        worker_code = None
    from apps.calls.worker import is_stale
    from apps.core import code_fingerprint
    serving_code = code_fingerprint.running()

    all_ok = db and redis_ok and blob
    body = {
        "status": "ok" if all_ok else "degraded",
        "version": getattr(settings, "MESHY_VERSION", "0.1.0"),
        # `git_sha` is 12 hex chars, or "unknown" when build provenance was unavailable.
        "git_sha": lookup("MESHYCHAT_GIT_SHA") or "unknown",
        # The build marks dirty source with the value "1".
        "git_dirty": lookup("MESHYCHAT_GIT_DIRTY") == "1",
        "db": db,
        "redis": redis_ok,
        "blob": blob,
        # Rule B9, after the seven base fields described in the module docstring.
        "code_sha256": serving_code,
        "worker_code_sha256": worker_code,
        "worker_code_stale": is_stale(worker_code, serving_code),
    }
    # Divergence 040, slice 6 (Q11): last, after B9's three, and only while tokens are on.
    capabilities = advertised_capabilities()
    if capabilities is not None:
        body["capabilities"] = capabilities
    return HttpResponse(
        # Compact JSON preserves the specified response bytes; json.dumps defaults to ", " / ": "
        # separators, which would change every byte after the first key.
        json.dumps(body, separators=(",", ":")),
        content_type="application/json",
        status=200 if all_ok else 503,
    )


def _livez_probe() -> bool:
    """`dbconn.ping` - the probe /v1/health's db check runs - with LIVEZ_TCP added."""
    return dbconn.ping(make_conninfo(dbconn.dsn(), **LIVEZ_TCP), timeout=PROBE_TIMEOUT)


class _LivezProbe:
    __slots__ = ("started", "done", "ok")

    def __init__(self, started: float):
        self.started = started
        self.done = threading.Event()
        self.ok = False


class _Livez:
    """Single-flight probing for /v1/livez, with abandonment and a bound on probe threads.

    dbconn.ping bounds connect time and statement time, but not a server that stops
    answering mid-reply: its thread then waits in recv until the connection ends, and a
    thread cannot be cancelled from outside. (Shutting the socket down from another thread
    was tried: on Windows it does not wake the waiting select.) So a probe is never waited
    on past its deadline. The check answers 503 at the deadline and ABANDONS the probe, and
    the next check starts a new one. Probe threads are daemons, so an abandoned one cannot
    hold up process exit.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._shared: _LivezProbe | None = None       # the probe checks now wait on
        self._alive = 0                               # probe threads not yet returned
        self._last: tuple[bool, float] | None = None  # (answer, when it was reached)

    def alive(self) -> int:
        with self._lock:
            return self._alive

    def check(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._last is not None and now - self._last[1] < LIVEZ_REUSE_S:
                return self._last[0]
            probe = self._shared
            if probe is None:
                if self._alive >= LIVEZ_MAX_PROBES:
                    self._last = (False, now)
                    log.warning("/v1/livez: %d db probes still running past their deadline; "
                                "reporting 503 without probing", self._alive)
                    return False
                probe = _LivezProbe(now)
                try:
                    threading.Thread(target=self._run, args=(probe,), name="livez-probe",
                                     daemon=True).start()
                except RuntimeError as exc:           # the interpreter cannot start a thread
                    self._last = (False, now)
                    log.warning("/v1/livez: cannot start a db probe (%s); reporting 503",
                                type(exc).__name__)
                    return False
                self._shared = probe
                self._alive += 1
        # Wall-clock bound from when the PROBE started, so a check that joins a probe in
        # flight is answered no later than the check that started it.
        if probe.done.wait(max(0.0, probe.started + LIVEZ_DEADLINE - time.monotonic())):
            return probe.ok
        with self._lock:
            if probe.done.is_set():
                return probe.ok
            if self._shared is probe:                  # the first check past the deadline
                self._shared = None
                self._last = (False, time.monotonic())
                log.warning("/v1/livez: db probe exceeded %.0fs; abandoned it and "
                            "reporting 503", LIVEZ_DEADLINE)
        return False

    def _run(self, probe: _LivezProbe) -> None:
        try:
            ok = bool(_livez_probe())
            if not ok:
                log.warning("/v1/livez: the database did not answer SELECT 1; reporting 503")
        except Exception as exc:
            # Type only: the message of a connection error can carry the host and user.
            log.warning("/v1/livez: db probe raised %s; reporting 503", type(exc).__name__)
            ok = False
        with self._lock:
            self._alive -= 1
            probe.ok = ok
            if self._shared is probe:                  # answered within its deadline
                self._shared = None
                self._last = (ok, time.monotonic())
            probe.done.set()


_LIVEZ = _Livez()


@require_http_methods(["GET", "HEAD"])
def livez(request):
    """Is the database answering? 200 or 503, within one probe timeout. See the header."""
    ok = _LIVEZ.check()
    return HttpResponse(
        json.dumps({"status": "ok" if ok else "unavailable"}, separators=(",", ":")),
        content_type="application/json",
        status=200 if ok else 503,
    )


urlpatterns = [
    # No trailing slash, and settings.APPEND_SLASH is False, so `/v1/health/` 404s
    # instead of 301-ing.
    path("v1/health", health, name="health"),
    # /v1/livez, the load balancer's route: database only (the project's design log (not part of this release) 035).
    path("v1/livez", livez, name="livez"),
    # /v1/metrics, the Prometheus exposition, OFF until MESHY_METRICS_TOKEN is set and then
    # bearer-gated (apps/core/metrics.py; E01, F28). /v1/health is unchanged by it.
    path("v1/metrics", metrics_view, name="metrics"),
    # MOUNTED 2026-09-21. These modules existed and were NOT included here, so every one
    # of their route tests passed while the application served 404 - the tests pinned
    # ROOT_URLCONF to each app's own urls module. A route test that supplies its own
    # URLconf proves the view, never the wiring. tests/test_project_wiring.py now asserts
    # reachability through THIS file.
    path("", include("apps.authapi.urls")),
    # Node-local model capabilities. No database handle and no auth: see
    # apps/modelhost/views.py on why discovery must not depend on the thing it enables.
    path("", include("apps.modelhost.urls")),
    path("", include("apps.peers.urls")),
    path("", include("apps.calls.urls")),
    # grants, skillsreg, files, dashboard, meshguard, board and meshsum are OPTIONAL surfaces,
    # mounted at the end of this file and only when MESHY_SURFACES names them
    # (apps/core/surfaces.py, the project's design log (not part of this release) 054).
    path("", include("apps.a2a.urls")),
    # The mesh graph: read-only, peer-authenticated edge projection over mesh-chat.
    # Written only by its background job; no request path reaches the projection tables
    # with a write (the project's design log (not part of this release) 014).
    path("", include("apps.mesh.urls")),
    # Semantic mesh reads (search, context): peer-authenticated, READ permission on
    # shared/mesh-chat, read only. Vectors are written by the mesh_embed_catch_up job
    # only (the project's design log (not part of this release) 019).
    path("", include("apps.meshsem.urls")),
    # Decision cards: the filtered list over the card namespace, read only, READ permission on
    # that namespace (the project's design log (not part of this release) 091). Cards are written by memory.write.
    path("", include("apps.cards.urls")),
    # Object-capability tokens, mint and revoke: peer-authenticated POSTs, 403
    # E_CAPABILITY_UNAVAILABLE while MESHY_CAPABILITIES is off (the project's design log (not part of this release) 040).
    path("", include("apps.memoryskills.urls")),
]


def optional_urlpatterns(enabled) -> list:
    """The OPTIONAL surfaces `enabled` names, as patterns (apps/core/surfaces.py). A function,
    so a test can build either state without reloading this module."""
    from apps.core import surfaces
    return [path("", include(module)) for module in surfaces.modules(enabled)]


from django.conf import settings as _settings  # noqa: E402

urlpatterns += optional_urlpatterns(_settings.MESHY_SURFACES)
