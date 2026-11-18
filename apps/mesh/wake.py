"""One subscriber per process and an in-process waiter registry for `/v1/mesh/wait` (STATUS F12).

DESIGN. This project has no Redis (divergence 018: "Here that means Redis first"), so until
F12 every parked waiter here polled the database every POLL_SECS.

WHAT THIS DOES. A nudge starts as a Postgres NOTIFY: sql/036's trigger sends `mesh_msg` for
every committed insert of a `msg/` key into shared/mesh-chat,
which the live push already listens to (apps/wsgw/mesh_notify.py, divergence 020). So:

  * `Subscriber`: ONE daemon thread per process, holding ONE dedicated LISTEN connection
    (named APPLICATION_NAME in pg_stat_activity) on that channel, started by the first waiter
    that parks. It holds no query connection: a notification is a POINTER (NOTIFY is
    unauthenticated, divergence 020), and all it does is wake waiters, who re-read with the
    query they always run. The one row read for it is the notified row's ADDRESSING, and
    only while an addressed waiter is parked (WHO IS WOKEN, below), by a thread of its own
    (THE ADDRESSING READ, below); nothing read reaches a caller. So a forged notification
    costs a share of one read and the re-reads of the waiters it wakes.
  * `Registry`: the parked waiters, each with a `threading.Event`. A notification wakes every
    waiter except the row's author's (the wait never answers a peer its own row); a payload
    that does not parse wakes every waiter, the safe side.
  * WHO IS WOKEN (review finding 4 of F12). A plain waiter is addressing-agnostic: every
    readable row is its, so every notification wakes it. An ADDRESSED waiter
    (registered with its labels) reads only the rows the push would send it, so when one is
    parked the row's addressing is read ONCE (`load_nudges`, the push's own `load_nudge`
    read by key and author, below) and wakes an addressed waiter only when
    `role_for` gives it a role, the push's own decision. The payload's `to` alone could
    not decide it (it carries no `cc`). A row that cannot be read (gone, or the read
    failed) wakes every addressed waiter, the safe side. So one message costs one read and
    the re-reads of its recipients, not one re-read per parked waiter: before, it woke up
    to MAX_WAITERS - 1 waiters that each opened a connection and re-scanned at once.
  * THE ADDRESSING READ (STATUS E22; review MEDIUM-1 of integ/r14). The LISTEN loop does
    not read it: it wakes the plain waiters at once and QUEUES (key, author). A thread of
    its own (`meshy-mesh-wait-addressing`) drains the queue and reads a whole group's
    addressing in ONE `key = ANY(...)` read (`load_nudges`: `load_nudge` for every pair) on
    ONE persistent role-bound connection, then wakes the addressed waiters each row is for.
    Before, the loop read each row serially on a fresh connection (about 67 ms each), so
    150 unrelated rows held an addressed waiter's wake about 10 s, every plain waiter's
    wake waited behind the same reads, and a slow read held the probe into a false deaf
    verdict. The queue is bounded (ADDRESSING_BACKLOG_MAX): past it the backlog is dropped
    and every addressed waiter woken, the same safe side as an unread row.
  * LISTEN LOST OR (RE)ESTABLISHED: every waiter is woken. A row committed while nothing
    listened was never announced, so each re-reads once; and a waiter re-decides on every
    wake whether it may wait for a nudge alone (`Subscriber.is_listening`) or must poll.

WHICH WAITERS STOP POLLING (the caller decides, apps/mesh/wait.py): a waiter whose whole set
the nudge covers, that is the addressed wait (shared/mesh-chat only) and a plain wait whose
readable namespaces are shared/mesh-chat alone, waits for a nudge or its deadline while the
subscriber is listening. A plain waiter that can read another namespace keeps a 2 s re-query
beside the nudge, since no trigger announces a row there. Any waiter polls while the subscriber
is not listening.

A LISTEN THAT STALLS WITHOUT CLOSING (review finding 3 of F12). A TCP path that stops
moving bytes and closes nothing (a NAT timeout, a VM network hiccup) raises nothing: the
connection's `notifies()` just returns empty, and every waiter that waits for a nudge alone
would wait to its deadline. So while any waiter is parked the subscriber proves the path
end to end: every PROBE_EVERY_SECS it sends itself a NOTIFY on PROBE_CHANNEL, a channel of
its own, from a short connection of its own, and one not heard back on the LISTEN
connection within PROBE_TIMEOUT_SECS means LISTEN is lost: the connection is dropped, every
waiter is woken and polls, and the subscriber reconnects (each connect bounded by
CONNECT_TIMEOUT_SECS). A stalled LISTEN is therefore found within PROBE_EVERY_SECS +
PROBE_TIMEOUT_SECS (plus up to one NOTIFY_POLL_SECS) of the first park after it stalled.

THREADING. A waiter is a Django view parked in its request's thread (divergence 018), so the
registry is thread-safe and the wake is an Event, not a coroutine. The subscriber thread is a
daemon and reconnects forever with capped exponential backoff, as the live bridge does. No
request connection is ever touched (V1-DESIGN-RULES E7).
"""
from __future__ import annotations

import collections
import contextlib
import logging
import threading
import time
import uuid

from apps.core import dbconn
from apps.wsgw.constants import EDIT_NOTIFY_CHANNEL, MESH_NOTIFY_CHANNEL, REDACTION_NOTIFY_CHANNEL
from apps.wsgw.mesh_notify import parse_mesh_payload

log = logging.getLogger("meshychat.mesh.wake")

#: What pg_stat_activity shows for the subscriber's connection: one per serving process.
APPLICATION_NAME = "meshy-mesh-wait-subscriber"
#: How long one `notifies()` call blocks before the loop looks at its stop flag. Bounds
#: shutdown only; delivery is immediate.
NOTIFY_POLL_SECS = 1.0
#: The first wait before the subscriber re-connects after losing LISTEN, doubling per failed
#: attempt up to RESTART_DELAY_MAX_SECS. Module attributes so a test can hold the gap open.
RESTART_DELAY_SECS = 0.5
RESTART_DELAY_MAX_SECS = 30.0
#: The channel the subscriber's own probe travels on: its own, so a probe never wakes a
#: waiter and a mesh nudge is never taken for a probe.
PROBE_CHANNEL = "meshy_mesh_wait_probe"
#: While any waiter is parked, a probe goes out this long after the last one came back.
PROBE_EVERY_SECS = 5.0
#: A probe not heard back on the LISTEN connection within this long: LISTEN is lost.
PROBE_TIMEOUT_SECS = 5.0
#: The bound on opening the LISTEN connection (and the probe's): without one, libpq waits
#: on a path that answers nothing for as long as the kernel lets it.
CONNECT_TIMEOUT_SECS = 10
#: At most this many rows wait for their addressing to be read (E22); past it the backlog is
#: dropped and every addressed waiter is woken instead, so a burst cannot grow memory.
ADDRESSING_BACKLOG_MAX = 4096
#: At most this many rows in one addressing read.
ADDRESSING_BATCH_MAX = 500


class ListenDeaf(Exception):
    """The subscriber's own probe did not come back: the LISTEN connection hears nothing."""


class Waiter:
    """One parked wait: whose it is, the event a nudge sets, and, for an addressed wait, the
    labels the push answers for it (None for a plain wait, which every row may be for)."""

    __slots__ = ("peer_id", "event", "labels")

    def __init__(self, peer_id, labels=None) -> None:
        self.peer_id = str(peer_id)
        self.event = threading.Event()
        self.labels = None if labels is None else frozenset(labels)


class Registry:
    """The parked waiters of this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: set[Waiter] = set()

    def register(self, peer_id, labels=None) -> Waiter:
        waiter = Waiter(peer_id, labels)
        with self._lock:
            self._waiters.add(waiter)
        return waiter

    def unregister(self, waiter: Waiter) -> None:
        with self._lock:
            self._waiters.discard(waiter)

    def count(self) -> int:
        with self._lock:
            return len(self._waiters)

    def wake(self, author, is_for=None, *, plain=True, addressed=True) -> int:
        """Set the event of every waiter but `author`'s that the row may be for; the number
        woken. `is_for(labels)`: whether the row is for an addressed waiter with those
        labels; None (the row's addressing unknown) wakes every one. A plain waiter is
        always woken. `plain` or `addressed` False leaves that kind alone (E22: the plain
        ones are woken at the notification, the addressed ones once its addressing is
        read). `author` None excludes nobody."""
        author = None if author is None else str(author)
        with self._lock:
            woken = [w for w in self._waiters if w.peer_id != author
                     and (plain if w.labels is None
                          else addressed and (is_for is None or is_for(w.labels)))]
        for w in woken:
            w.event.set()
        return len(woken)

    def addressed_besides(self, author) -> bool:
        """Whether an addressed waiter other than `author`'s is parked: only then is the
        row's addressing worth a read."""
        author = str(author)
        with self._lock:
            return any(w.labels is not None and w.peer_id != author for w in self._waiters)

    def wake_all(self) -> int:
        with self._lock:
            woken = list(self._waiters)
        for w in woken:
            w.event.set()
        return len(woken)


class Subscriber:
    """The one LISTEN loop, feeding a Registry."""

    def __init__(self, registry: Registry, *, dsn=None) -> None:
        self.registry = registry
        self._dsn = dsn
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._listening = threading.Event()
        self._stats: collections.Counter = collections.Counter()
        self._stats_lock = threading.Lock()
        #: Failures since LISTEN last held; the restart delay doubles with it.
        self._failures = 0
        #: The probe in flight (its token and when it went out), when the next may go, and
        #: the thread sending the last one (a sender stuck on a dead path is not doubled).
        self._probe_token: str | None = None
        self._probe_sent = 0.0
        self._probe_next = 0.0
        self._probe_sender: threading.Thread | None = None
        #: (key, author) of each notification whose addressing is still to be read (E22),
        #: at most ADDRESSING_BACKLOG_MAX; the addressing thread drains it.
        self._pending: list = []
        self._pending_ready = threading.Condition()

    # --- lifecycle ------------------------------------------------------------------

    def ensure_started(self, *, ready_timeout: float = 0.0) -> bool:
        """Start the thread if it is not running; True once LISTEN is live, waiting at most
        `ready_timeout` for it. Never raises for a database problem: a waiter without a
        listening subscriber polls."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._listening.clear()
                self._thread = threading.Thread(
                    target=self._run, args=(self._stop,), name="meshy-mesh-wait-subscriber",
                    daemon=True)
                self._thread.start()
        if ready_timeout > 0:
            self._listening.wait(ready_timeout)
        return self._listening.is_set()

    def is_listening(self) -> bool:
        return self._listening.is_set()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None:
            thread.join(timeout)

    def stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)

    def _count(self, name: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[name] += n

    # --- the loop -------------------------------------------------------------------

    def _target(self) -> str:
        return self._dsn or dbconn.dsn()

    def _run(self, stop: threading.Event) -> None:
        """Reconnect forever: RESTART_DELAY_SECS after a failure, doubling per failure in a
        row up to RESTART_DELAY_MAX_SECS, back to the first delay once LISTEN has held.
        The module attributes are read at each failure, not when the thread started."""
        self._failures = 0
        threading.Thread(target=self._address_loop, args=(stop,),
                         name="meshy-mesh-wait-addressing", daemon=True).start()
        while not stop.is_set():
            try:
                self._run_once(stop)
            except Exception as exc:
                delay = min(RESTART_DELAY_SECS * (2 ** min(self._failures, 16)),
                            RESTART_DELAY_MAX_SECS)
                self._failures += 1
                self._count("listen_errors")
                log.warning("mesh wait subscriber: %s: %s; waiters poll, retrying in %.1fs",
                            type(exc).__name__, exc, delay)
                stop.wait(delay)
        self._lost()

    def _lost(self) -> None:
        """LISTEN is gone: every waiter re-reads now and polls until it is back."""
        if self._listening.is_set():
            self._listening.clear()
            self.registry.wake_all()

    def _run_once(self, stop: threading.Event) -> None:
        listen = dbconn.connect(self._target(), application_name=APPLICATION_NAME,
                                connect_timeout=CONNECT_TIMEOUT_SECS)
        try:
            with listen.cursor() as cur:
                cur.execute(f"LISTEN {MESH_NOTIFY_CHANNEL}")
                cur.execute(f"LISTEN {REDACTION_NOTIFY_CHANNEL}")
                cur.execute(f"LISTEN {EDIT_NOTIFY_CHANNEL}")
                cur.execute(f"LISTEN {PROBE_CHANNEL}")
            # Anything committed while nothing listened was never announced: every parked
            # waiter re-reads once, and from here may wait for a nudge alone.
            self._probe_token = None
            self._probe_next = time.monotonic()
            self._listening.set()
            self._failures = 0
            self._count("listens")
            self.registry.wake_all()
            while not stop.is_set():
                for note in listen.notifies(timeout=NOTIFY_POLL_SECS):
                    if note.channel == PROBE_CHANNEL:
                        self._on_probe(note.payload)
                    elif note.channel == REDACTION_NOTIFY_CHANNEL:
                        self._on_redaction()
                    elif note.channel == EDIT_NOTIFY_CHANNEL:
                        self._on_edit()
                    else:
                        self._on_notify(note.payload)
                    if stop.is_set():
                        break
                self._probe_tick()
        finally:
            self._lost()
            try:
                listen.close()
            except Exception:
                pass

    # --- the probe (review finding 3) -------------------------------------------------

    def _probe_tick(self) -> None:
        """Between two `notifies()` calls: raise ListenDeaf when the probe in flight is
        overdue; otherwise send the next one when it is due and a waiter is parked (with no
        waiter, a deaf LISTEN costs nothing, and the first park's probe finds it)."""
        now = time.monotonic()
        if self._probe_token is not None:
            if now - self._probe_sent > PROBE_TIMEOUT_SECS:
                self._count("deaf")
                raise ListenDeaf(f"probe not heard back within {PROBE_TIMEOUT_SECS:.1f}s")
            return
        if now < self._probe_next or self.registry.count() == 0:
            return
        token = uuid.uuid4().hex
        self._probe_token, self._probe_sent = token, now
        sender = self._probe_sender
        if sender is not None and sender.is_alive():
            return                        # the last one is stuck on the path: this is overdue
        self._probe_sender = threading.Thread(target=self._send_probe, args=(token,),
                                              name="meshy-mesh-wait-probe", daemon=True)
        self._probe_sender.start()

    def _send_probe(self, token: str) -> None:
        """NOTIFY PROBE_CHANNEL with `token`, from a connection of its own. Never raises: a
        probe that cannot be sent is a probe not heard back."""
        try:
            with contextlib.closing(dbconn.connect(
                    self._target(), application_name=APPLICATION_NAME + "-probe",
                    connect_timeout=max(1, int(PROBE_TIMEOUT_SECS)))) as conn:
                conn.execute("SELECT pg_notify(%s, %s)", (PROBE_CHANNEL, token))
            self._count("probes")
        except Exception as exc:
            self._count("probe_errors")
            log.warning("mesh wait subscriber: probe not sent: %s: %s", type(exc).__name__, exc)

    def _on_probe(self, payload: str) -> None:
        """Our probe, heard back: LISTEN hears. Another process's probe is ignored."""
        if self._probe_token is not None and payload == self._probe_token:
            self._probe_token = None
            self._probe_next = time.monotonic() + PROBE_EVERY_SECS
            self._count("probes_heard")

    def _on_redaction(self) -> None:
        """A mail row was redacted (divergence 053). Its event is a row of the commit order that
        only an addressed wait on the commit order reads, so every parked ADDRESSED waiter
        re-reads and decides for itself (`scan_addressed`); a plain waiter walks `(created_at,
        id)` and never sees one. Redactions are rare, so no addressing read is spent on this:
        the waiter's own scan is that read."""
        self._count("redaction_notifications")
        self.registry.wake(None, plain=False)

    def _on_edit(self) -> None:
        """A mail row was edited (divergence 059). Its event is a row of the commit order that only an addressed
        wait on the commit order reads, exactly as a redaction's is, so every parked ADDRESSED waiter re-reads
        and decides for itself (`scan_addressed`, `edit_event`). Edits are rare, and the waiter's own scan is
        the addressing read."""
        self._count("edit_notifications")
        self.registry.wake(None, plain=False)

    def _on_notify(self, payload: str) -> None:
        """One mesh NOTIFY, on the LISTEN loop, which never reads for it (E22). With no
        addressed waiter parked besides the author's, every waiter but the author's is
        woken now; otherwise the plain ones are woken now and the row is queued for the
        addressing thread, which wakes the addressed ones it is for."""
        self._count("notifications")
        parsed = parse_mesh_payload(payload)
        if parsed is None:
            self._count("undecodable")
            self.registry.wake_all()
            return
        key, author = parsed
        if not self.registry.addressed_besides(author):
            self.registry.wake(author)
            return
        self.registry.wake(author, addressed=False)
        overflow = False
        with self._pending_ready:
            if len(self._pending) >= ADDRESSING_BACKLOG_MAX:
                self._pending.clear()
                overflow = True
            else:
                self._pending.append((key, author))
                self._pending_ready.notify()
        if overflow:
            # Every row dropped here, and this one, wakes every addressed waiter instead.
            self._count("addressing_overflow")
            self.registry.wake(None, plain=False)

    # --- the addressing read (E22) ----------------------------------------------------

    def _address_loop(self, stop: threading.Event) -> None:
        """The addressing thread: drain the queue, read a group's addressing in one read,
        wake the addressed waiters each row is for. Runs until `stop`; a failed read wakes
        every addressed waiter, and nothing here ends the thread."""
        conn = None
        try:
            while not stop.is_set():
                with self._pending_ready:
                    if not self._pending:
                        self._pending_ready.wait(NOTIFY_POLL_SECS)
                    batch = self._pending[:ADDRESSING_BATCH_MAX]
                    del self._pending[:len(batch)]
                if not batch:
                    continue
                nudges, conn = self._addressing(conn, batch)
                try:
                    for key, author in batch:
                        nudge = nudges.get((key, author))
                        is_for = None if nudge is None else _is_for(nudge)
                        self.registry.wake(author, is_for, plain=False)
                except Exception as exc:
                    self._count("addressing_errors")
                    log.warning("mesh wait subscriber: waking by addressing failed, waking "
                                "every addressed waiter: %s: %s", type(exc).__name__, exc)
                    self.registry.wake(None, plain=False)
        finally:
            _close(conn)

    def _addressing(self, conn, batch):
        """({(key, author): nudge} for the rows of `batch` that could be read, the
        connection to keep). One read on the persistent connection, (re)opened guarded as a
        request's is, under server_internal as the push reads. Never raises: a failed read
        answers {} and drops the connection. A row missing from the answer wakes every
        addressed waiter."""
        try:
            if conn is None or conn.closed:
                conn = None
                conn = dbconn.connect(self._target(),
                                      application_name=APPLICATION_NAME + "-addressing",
                                      connect_timeout=CONNECT_TIMEOUT_SECS)
                dbconn.assert_serving_connection(conn)
            with dbconn.request_server_internal(conn) as c:
                found = load_nudges(c, batch)
        except Exception as exc:
            self._count("addressing_errors")
            log.warning("mesh wait subscriber: addressing of %d row(s) unread, waking every "
                        "addressed waiter: %s: %s", len(batch), type(exc).__name__, exc)
            _close(conn)
            return {}, None
        self._count("addressing_reads")
        unknown = len({p for p in batch if p not in found})
        if unknown:
            self._count("addressing_unknown", unknown)
        return found, conn


def _is_for(nudge):
    """`Registry.wake`'s `is_for` for one read row: the push's own decision, `role_for`."""
    return lambda labels: nudge.role(labels) is not None


def _close(conn) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def load_nudges(conn, pairs) -> dict:
    """{(key, author): Nudge} for each (key, author) in `pairs` whose row exists: for each,
    exactly `mesh_notify.load_nudge(conn, key, author)` (the newest version of `key` by
    `author`), in ONE read (`key = ANY(...)`, on `shared_memory_nkv_unique (namespace, key,
    version)`). A pair with no row is absent. Must run inside `rls.server_internal`, as
    load_nudge must."""
    from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
    from apps.wsgw import mesh_notify
    wanted = {(str(key), uuid.UUID(str(author))) for key, author in pairs}
    if not wanted:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (key, created_by_peer_id) key, created_by_peer_id, "
            + mesh_notify.nudge_columns() + " "
            "FROM shared_memory "
            "WHERE namespace = %s AND key = ANY(%s) AND created_by_peer_id = ANY(%s) "
            "ORDER BY key, created_by_peer_id, version DESC",
            (MESH_CHAT_NAMESPACE, sorted({k for k, _ in wanted}),
             sorted({a for _, a in wanted})))
        rows = cur.fetchall()
    found = {}
    for key, author, *cols in rows:
        if (key, author) in wanted:
            found[(key, author)] = mesh_notify.nudge_from_columns(key, author, cols)
    return found


#: The process's registry and subscriber. The subscriber starts with the first park.
REGISTRY = Registry()
SUBSCRIBER = Subscriber(REGISTRY)
