"""A small pool of idle request-path connections. OFF unless MESHYCHAT_DB_POOL_SIZE is set to 1 or more.

WHY (measured on production, Sun 15 Nov 2026). Every `dbconn.connect()` opens a new session and closes it: two
reads of pg_stat_database.sessions 56 s apart counted 545 new sessions, about ten a second, with 5 or 6 backends
alive at a time. In the 22 hours before, the server logged 4,482 connects slower than 100 ms (22 over a second,
the slowest 5.3 s) with up to 117 threads at the moment. A connect is a TCP and authentication round, SET ROLE,
the marker setting and the role check. A reused connection costs the reset and the role check only.

WHAT IS POOLED. Only the connection `dbconn.connect()` opens when its caller names nothing: the configured DSN,
the default statement bound, the serving role, the role check on, no application name. Anything else (the job
runner's per-tick bound, a WebSocket's named connection, the migrator's owner connection) is opened and closed as
before. This module opens no connection itself; `dbconn.connect` stays the one constructor
(tests/test_every_connection_is_role_bound.py).

THE THREE THINGS THAT MUST HOLD, and how each is held:

  1. A reference kept after close() must be dead for good. With one Connection object per session, a caller that
     kept a reference past its close() would later be talking on someone else's lease, with that request's
     visibility. So every lease is a NEW `Lease` object around the same libpq connection, and close() marks the
     old object released before anything else: its `wait` (the one door every statement, transaction and
     pipeline goes through) raises, and it reports closed. It is never handed out again.

  2. Nothing of one lease reaches the next. On return the session runs DISCARD ALL (session settings back to
     their start values, so the statement bound and UTC from the connect options are in force again; prepared
     statements, temporary tables, LISTEN, advisory locks and cursors gone), then `rls.set_role` again, because
     DISCARD ALL also returns the session to its login role. A session that is not idle (a transaction open or
     failed, a query in flight) or whose reset fails is closed, not returned. The peer context is LOCAL to a
     transaction (rls.peer_context), so it ended with that transaction; DISCARD ALL is the second line.

  3. A lease is proven before it serves. Checkout runs `dbconn.assert_not_bypassing` on it: one round trip that
     is both the liveness check (a session the server or the proxy dropped is thrown away and the next is tried)
     and the observation that the session really runs as a role row-level security applies to. A pooled session
     that fails THAT check is logged as an error and closed. The caller then gets a fresh connection.

The pool never makes a caller wait and never refuses: no idle session means a fresh connect, as today, and a
returned session beyond the pool's size is closed. So the worst it can do to capacity is nothing.

ROUND 2 (the security read of 6f76919e, nxxxxx). What it found and what holds it now:

  * Two threads closing ONE lease at the same instant both passed the "already released?" test and returned the same
    backend twice, so two later leases shared a session. The test-and-set is now one step under a lock (`_claim`),
    and `give_back` refuses a session that is already idle (counted `double_return`, logged as an error).
  * The libpq handle. `lease.pgconn` read AFTER close is a dead stand-in, not the live handle. A raw handle someone
    SAVED before the close is the same object the next lease uses, and no wrapper can revoke it: that is the boundary
    of this design (the driver's own pool has it too). It is held by a rule the suite enforces instead: no module
    under apps/ touches `.pgconn` except this one (tests/test_db_pool.py, the census case).
  * Checkout's proof and the reset on return had no client-side bound: a session whose peer stopped answering would
    hold a request thread (or a `finally`) for as long as the network did. Both now run under POOL_IO_TIMEOUT_S on
    the client; a session that does not answer in time is closed, and the caller connects as usual.
  * Notifications already queued in libpq survive UNLISTEN and DISCARD ALL. They are drained after the reset, and a
    session in pipeline mode is closed, not returned; a Lease refuses an explicit `pipeline()` (nothing under apps/
    enters one; batch execution is another matter, see round 3). A connection opened to LISTEN is never pooled:
    its caller passes `pool=False` to `dbconn.connect`.
  * `take` closes the session it had popped when anything unexpected happens, instead of dropping it open.
  * The request middleware DISCARDS a connection that fails its serving guard; it is not returned to the pool.

ROUND 3 (the second security read, of 2f9896ed, nxxxxx):

  * A statement ALREADY IN FLIGHT when another thread closes the lease. The driver runs every statement under the
    connection's own lock, and a cursor keeps the libpq handle it was made with. A cursor that had passed the
    released check could still send after close() had reset the session and handed it to the next request. close()
    and discard() now TAKE THAT SAME LOCK first: a statement that was admitted finishes before the session is
    reset, and one that comes after finds the lease released. If the lock is not free within CLOSE_WAIT_S (a
    statement that runs long, or a close from inside an open stream), the session is never pooled: it is parked,
    and finished later by the first pool call that finds its lock free (`_reap`), never while a statement may be
    using it.
  * cancel() and cancel_safe() are refused on a lease: a cancel request taken before close() would otherwise
    cancel the NEXT lease's statement. (The driver's own cancel on an interrupt is not this path.)
  * `take` finishes the popped session FIRST and reports after; reporting can no longer skip the cleanup.
    While it is parked the statement keeps its own handle: a busy lease is marked released (no NEW statement is
    admitted, and it reports closed) but its `pgconn` is swapped for the dead stand-in only by the reaper, under the
    statement's own lock (round 4, memory-bxxxxx: swapping it at close() failed the running statement inside
    the C driver).
  * `dbconn.assert_serving_connection` discards a pooled lease it refuses, so every caller's close() after a
    refused guard closes for real (five call sites, one place). The doorbell sender's LISTEN connection asks for
    pool=False too. The drain of queued notifications is bounded by a count.
  * Batch execution (`executemany`) uses the driver's pipeline internally and is allowed: it ends its pipeline
    before it returns. Only the explicit `pipeline()` entry is refused, and a session returned in pipeline mode is
    closed.

ROUND 5 (the security read of 64886992, nxxxxx): a parked session is no longer left to the next pool call.

  * A REAPER THREAD runs while anything is parked (started by the park, gone when the list is empty) and looks every
    REAP_EVERY_S. An idle process finishes its parked sessions too.
  * A BOUND. A parked session whose statement has still not let go of the lock after PARK_MAX_S, or that is one of
    more than MAX_PARKED, is ENDED from outside: the server is asked to cancel what that backend is running (a
    cancel request on a connection of its own, sent from a thread of its own because it can block on the network),
    and the session's SOCKET is shut down through a duplicate of the descriptor, so a statement blocked on it fails
    and the backend, once its statement is over, finds no client and exits. Neither step uses the handle the
    statement is on. This is not the cancel a lease refuses: that one could reach the NEXT lease, and a parked
    session has no next lease. When the lock comes free the session is finished as before. If it never does (a
    stream left open and never read again holds the lock with nobody inside the driver), the lease is forgotten
    PARK_FORGET_S later: its backend has been ended, and the driver frees the handle with the lease.
  * `_reap` handles each parked lease on its own, and whatever it did not finish goes back to the list in
    `finally`: one fault can no longer lose the leases behind it.

ROUND 6 (the security read of 85abb9ae, a reviewer 0c867a). Round 5's way of ending a parked session was itself
unsafe, and four smaller things:

  * ENDING A PARKED SESSION read the statement's handle from the reaper thread: `pgconn.socket` gave a bare
    descriptor number, the statement's thread could close that descriptor in the same instant (the server's own 30 s
    bound falls on PARK_MAX_S), the system gave the number to the next socket opened, and the shutdown then hit a
    stranger's connection. Now the reaper NEVER touches a parked lease's handle. A lease handed to a caller duplicates
    its socket ONCE, at the moment it is made and only its maker can see it (`_arm`: at the end of `take`, and in
    `Lease.connect`). Ending the session is a shutdown of that duplicate, under the lease's own small lock. The
    duplicate is an open descriptor of ours until the session is finished, returned or forgotten (`_disarm`, exactly
    once), so its number cannot be given to anyone else meanwhile, and an idle pooled session holds none. A lease
    whose duplicate could not be made is not ended from outside; it is still forgotten at its bound.
  * NO CANCEL REQUEST is sent to the server any more (round 5 made one from the statement's handle, on a thread per
    session with no bound on the threads or on the connect each makes). It is not needed: only a session opened
    with the default statement bound is pooled (dbconn.connect: `statement_timeout_ms is None` is part of
    `poolable`, and the bound is in the session's options, where DISCARD ALL returns to it), so the server ends the
    statement at that bound by itself, and a backend that reads or writes on the shut socket ends at once.
  * THE DRIVER'S OWN CANCEL was refused with the caller's. psycopg cancels on an early end of `stream()`, on an
    interrupt inside a wait and on a failed copy, always through `_try_cancel`, under the connection's lock, on the
    thread that runs the statement. Refused, an abandoned stream was downloaded to its end. `Lease._try_cancel` now
    lets that one through and marks the lease NEVER POOLED: close() finishes its session. A cancel that could reach
    a next lease is impossible for a session that has none. `cancel()` and `cancel_safe()` called by anyone else
    stay refused.
  * A CLOSE FROM THE THREAD THAT HOLDS THE LOCK (its own stream still open) waited CLOSE_WAIT_S for a lock only it
    could free. The lease's lock now knows its holder: that close parks at once.
  * `_reap`: ONE pass at a time (a second caller returns at once); the parked leases STAY in the list while a pass
    looks at them, so the count is true throughout and the reaper never sees an empty list that is not empty; and
    the pass first finishes everything whose statement is over, and only then counts what is still running against
    MAX_PARKED (a young statement ahead of finished ones was ended for nothing).
  * Queued notifications are drained, within the bound, BEFORE the reset as well: the reset's own statements
    otherwise read all of them into memory first. The reaper thread is started under the lock (two could start).
    `fileno()` of a released lease raises. A driver whose Connection has no `wait` at all reads as "cannot bound".

ROUND 7 (the delta read of 45324537, a reviewer 02390b). Two things round 6 said above are WRONG, and two smaller ones:

  * "NO CANCEL REQUEST ... is not needed" was wrong. The server's statement bound is the session's own setting: a
    caller may SET it to zero or to minutes inside its lease, and a parked session is never reset. And a backend
    that sleeps, waits for a lock or computes does not read or write its socket, so a shut socket ends nothing
    until the statement is over: more than MAX_PARKED abandoned statements kept that many backends and their locks.
    The cancel request is back, made SAFE: its handle is captured by `_arm`, by the thread that made the lease and
    before anyone else can see it (a cancel connection of its own where libpq has one, the older cancel object
    otherwise), and it is the only thing ever used: the statement's handle is still never read from outside.
    `_abort` hands that handle to ONE worker thread through a bounded queue; the worker sends each request within
    POOL_IO_TIMEOUT_S (the older kind is a blocking call with no bound of its own: it holds only that worker). The
    reaper itself never waits on the network. A request that does not fit the queue is dropped and counted: the
    socket is shut down either way.
  * A SHUT SOCKET DOES NOT WAKE THE STATEMENT'S OWN THREAD EVERYWHERE. On Windows a wait on a socket whose other
    side stays open and silent does not see our shutdown, so the thread stayed in its wait and kept the lease's
    lock. The driver steps the statement's generator every tenth of a second even when nothing is ready (every one
    of its wait functions does); `Lease.wait` now runs that generator inside one that looks at the lease's
    `_aborted` mark at each step and raises. An ended lease's thread is out of the driver within that step.
  * Sessions already ended were forgotten for the cap BEFORE this pass ended the running ones, and at once: a
    thread just woken had no time to let go, and its session was dropped unfinished. The forget for the cap now
    comes after, leaves a freshly ended session SETTLE_GRACE_S (unless more than MAX_PARKED * PARKED_HARD_X are
    parked), and every forget first tries once more to finish the session.
  * A parked lease whose lock FAULTED on every look was in neither list of a pass: never ended, never forgotten, and
    the reaper thread ran for good. A fault now counts as "not free", and a pass forgets an ended lease at its
    bound whatever its `_settle` did.
"""
from __future__ import annotations

import collections
import inspect
import logging
import socket
import threading
import time
from typing import Any

import psycopg
from psycopg import generators, waiting
from psycopg.pq import ConnStatus, PipelineStatus, TransactionStatus

from apps.core import rls
from apps.core.env import lookup

log = logging.getLogger("meshychat.db")

POOL_SIZE_ENV = "MESHYCHAT_DB_POOL_SIZE"
#: The most idle sessions one process keeps. Each is a Postgres backend held open (a few MB on the database host),
#: so the bound is small on purpose; production ran 5 or 6 backends at a time when this was measured.
MAX_POOL_SIZE = 16
#: An idle session older than this is closed at checkout instead of reused: it outlives any burst, and a proxy or
#: a server restart has had time to drop it.
MAX_IDLE_S = 120.0
STATS_LOG_EVERY_S = 600.0
#: The client-side bound on the pool's own round trips: the proof at checkout, and the reset on return. The server's
#: statement bound does not help when the peer has stopped answering at all.
POOL_IO_TIMEOUT_S = 5.0
#: How long close() waits for a statement that is in flight on this lease (another thread's) before it gives up on
#: pooling the session and parks it for `_reap`.
CLOSE_WAIT_S = 2.0
#: How long a parked session may wait for its statement before it is ended: the server is asked to cancel the
#: statement, the socket is shut down, and the statement's own wait raises.
PARK_MAX_S = 30.0
#: How long after that shutdown a lease whose lock never came free is still looked at before it is forgotten.
PARK_FORGET_S = 30.0
#: More parked sessions than this and the oldest are shut down at once, whatever their age.
MAX_PARKED = 16
#: A session ended less than this ago is not yet forgotten for the cap: its thread has just been woken and is about
#: to let go, and then the session is finished properly.
SETTLE_GRACE_S = 1.0
#: ... unless more than MAX_PARKED times this are parked: then the oldest ended ones go at once.
PARKED_HARD_X = 4
#: How often the reaper thread looks while anything is parked.
REAP_EVERY_S = 1.0
#: More queued notifications than this on one returned session and it is closed instead of drained.
MAX_NOTIFY_DRAIN = 10_000

_lock = threading.Lock()
_idle: list[tuple[Any, tuple, float]] = []          # (pgconn, key, returned at): newest last, taken newest first
_stats = {"reused": 0, "fresh": 0, "returned": 0, "closed_full": 0, "closed_not_idle": 0, "closed_reset_failed": 0,
          "closed_stale": 0, "closed_dead": 0, "closed_other_key": 0, "closed_bypass": 0, "closed_fault": 0,
          "double_return": 0, "parked_busy": 0, "closed_parked": 0, "aborted_parked": 0, "forgotten_parked": 0,
          "closed_cancelled": 0, "cancel_sent": 0, "cancel_failed": 0, "cancel_dropped": 0, "take_budget_spent": 0}
_parked: list[Any] = []                             # _Parked: leases closed while a statement was running
_reaper: list[Any] = [None]                         # the reaper thread, while anything is parked
_claim = threading.Lock()                           # the one step "not released yet -> released" of every lease
_reap_lock = threading.Lock()                       # one `_reap` pass at a time; a second caller does not wait
_cancels: collections.deque = collections.deque(maxlen=MAX_PARKED)   # (kind, handle): cancel requests not yet sent
_canceller: list[Any] = [None]                      # the one thread that sends them, while there are any


class _Gone:
    """What a released lease's `pgconn` is: nothing. Reading the attribute after close() must not hand out the live
    handle the next lease is using."""

    status = ConnStatus.BAD
    transaction_status = TransactionStatus.UNKNOWN
    pipeline_status = PipelineStatus.OFF

    def finish(self) -> None:
        return None

    def __getattr__(self, name):
        raise psycopg.OperationalError("the connection is closed (it was returned to the pool)")
_said: set[str] = set()
_last_log = [0.0]


def _driver_bounds_a_wait() -> bool:
    """Whether this driver's Connection.wait takes `timeout` (psycopg 3.3 and later). The pool bounds its own reset
    and proof with it; requirements.txt allows older drivers, on which the argument is a TypeError."""
    try:
        return "timeout" in inspect.signature(psycopg.Connection.wait).parameters
    except (AttributeError, TypeError, ValueError):
        return False


def size() -> int:
    """How many idle sessions to keep, read at CALL time. Unset, 0, or anything that is not a whole number from 0
    to MAX_POOL_SIZE is 0: the pool is OFF. A typo must never switch pooling on or fail a connection; it is
    logged once per value. On a driver that cannot bound a wait the pool is OFF whatever the setting says (the Codex
    review of 9d0a4f50): every reset would fail there, and the pool would only close what it was given."""
    raw = lookup(POOL_SIZE_ENV)
    if raw is None:
        return 0
    text = raw.strip()
    if text.isdigit() and text.isascii() and int(text) <= MAX_POOL_SIZE:
        if int(text) and not _driver_bounds_a_wait():
            if "driver" not in _said:
                _said.add("driver")
                log.warning("%s=%r, but psycopg %s cannot bound a wait (it needs 3.3 or later); the connection pool "
                            "stays off", POOL_SIZE_ENV, raw, getattr(psycopg, "__version__", "?"))
            return 0
        return int(text)
    if raw not in _said:
        _said.add(raw)
        log.warning("%s=%r is not a whole number from 0 to %d; the connection pool stays off",
                    POOL_SIZE_ENV, raw, MAX_POOL_SIZE)
    return 0


class _OwnedLock:
    """The lock psycopg holds around every statement of one connection, which also knows WHICH thread holds it.
    psycopg's own is a plain threading.Lock: a close() from the thread that holds it (a stream of its own still
    open) cannot tell that waiting is pointless."""

    __slots__ = ("_lock", "_owner")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: int | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        got = self._lock.acquire(blocking, timeout)
        if got:
            self._owner = threading.get_ident()
        return got

    def release(self) -> None:
        self._owner = None                          # before the release: the next holder's mark is never wiped
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def held_by_me(self) -> bool:
        return self._owner == threading.get_ident()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


class Lease(psycopg.Connection):
    """One use of a pooled session. close() returns the session to the pool and kills THIS object for good."""

    _released = False
    _pool_key: tuple | None = None
    _deadline: float | None = None                  # set only around the pool's own round trips
    _no_pool = False                                # the driver cancelled a statement on it: finished, never pooled
    _driver_cancel = False                          # True only inside `_try_cancel`
    _abort_sock: Any = None                         # a duplicate of the session's socket, made by `_arm`
    _cancel_tok: Any = None                         # (kind, handle) for one cancel request, made by `_arm`
    _aborted = False                                # set by `_abort`: this lease's own wait raises at its next step

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = _OwnedLock()
        self._abort_lock = threading.Lock()         # guards `_abort_sock` only

    @classmethod
    def connect(cls, *args, **kwargs):
        lease = super().connect(*args, **kwargs)
        lease._arm()
        return lease

    def _arm(self) -> None:
        """Take from the handle everything a parked session is ended with: a duplicate of its socket, and the
        handle for ONE cancel request (round 7). Called ONCE, by the thread that made the lease, before anyone else
        can see it: the handle is read here and nowhere later. What could not be made (no socket, the system
        refuses, a stand-in without a cancel) is simply not used when the session is ended."""
        try:
            dup = socket.fromfd(self.pgconn.socket, socket.AF_INET, socket.SOCK_STREAM)
        except Exception:  # noqa: BLE001
            dup = None
        try:
            tok = ("safe", self.pgconn.cancel_conn()) if _has_safe_cancel() else ("old", self.pgconn.get_cancel())
        except Exception:  # noqa: BLE001
            tok = None
        with self._abort_lock:
            self._abort_sock, self._cancel_tok = dup, tok

    def fileno(self) -> int:
        if self._released:
            raise psycopg.OperationalError("the connection is closed (it was returned to the pool)")
        return super().fileno()

    def wait(self, gen, *args, **kwargs):
        if self._released:
            gen.close()
            raise psycopg.OperationalError("the connection is closed (it was returned to the pool)")
        if self._deadline is not None:              # the pool's proof or reset: bounded on the client
            kwargs["timeout"] = max(0.05, self._deadline - time.monotonic())
        return super().wait(self._watched(gen), *args, **kwargs)

    def _watched(self, gen):
        """`gen`, stepped as the driver steps it, with one look at `_aborted` before every step: an ended lease's
        statement raises here instead of waiting on a socket that will never say anything (round 7). Values, the
        return value, a thrown exception and a close all pass through to `gen`."""
        try:
            item = next(gen)
            while True:
                if self._aborted:
                    raise psycopg.OperationalError("the connection was closed under this statement and has been ended")
                try:
                    ready = yield item
                except GeneratorExit:
                    raise
                except BaseException as exc:  # noqa: BLE001 - thrown in by the driver: it is `gen`'s to answer
                    item = gen.throw(exc)
                    continue
                if self._aborted:
                    raise psycopg.OperationalError("the connection was closed under this statement and has been ended")
                item = gen.send(ready)
        except StopIteration as stop:
            return stop.value
        finally:
            gen.close()

    @property
    def closed(self) -> bool:
        return self._released or self.pgconn.status == ConnStatus.BAD

    def pipeline(self):
        raise psycopg.NotSupportedError("a pooled connection does not do explicit pipeline mode")

    def cancel(self) -> None:
        if self._driver_cancel:                     # the driver's own, on a libpq that has no safe cancel
            return super().cancel()
        raise psycopg.NotSupportedError("a pooled connection does not take cancel(): it could reach the next lease")

    def _try_cancel(self, *, timeout: float = 5.0) -> None:
        """The driver's own cancel: an early end of stream(), an interrupt inside a wait, a failed copy. psycopg
        calls it under the connection's lock, on the thread that runs the statement. The session is never pooled
        afterwards, so the request cannot reach a next lease: there is none."""
        if self._released:
            return
        self._no_pool = True
        self._driver_cancel = True
        try:
            psycopg.Connection.cancel_safe(self, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - as the driver's own: a cancel that fails is said, not raised
            log.warning("db pool: the driver's cancel of a statement failed (%s)", type(exc).__name__)
        finally:
            self._driver_cancel = False

    def cancel_safe(self, *, timeout: float = 30.0) -> None:
        raise psycopg.NotSupportedError("a pooled connection does not take cancel_safe(): it could reach the next lease")

    def _take_handle(self):
        """The ONE step from leased to released: (the live handle, whether no statement was in flight), exactly
        once; (None, False) to every later or concurrent caller. It first takes the connection's own lock, the one
        the driver holds around every statement, so an admitted statement has finished and a later one finds the
        lease released. When the lock was free, this object holds no handle afterwards. When a statement is STILL
        RUNNING (the lock was not free in time), the handle stays where that statement is using it and the second
        value is False: the caller parks the whole lease, and the reaper takes the handle under the lock. When THIS
        thread holds the lock (a stream of its own is still open) nothing is waited for: only it could free it."""
        own = getattr(self.lock, "held_by_me", None)
        quiet = False if own is not None and own() else self.lock.acquire(timeout=CLOSE_WAIT_S)
        try:
            with _claim:
                if self._released:
                    return None, False
                self._released = True               # dead for its holder whatever happens next: no NEW statement
                self._closed = True
                if not quiet:
                    return self.pgconn, False       # NOT swapped: the running statement is still on it
                pgconn, self.pgconn = self.pgconn, _Gone()
                return pgconn, True
        finally:
            if quiet:
                self.lock.release()

    def close(self) -> None:
        pgconn, quiet = self._take_handle()
        if pgconn is None:
            return
        if not quiet:
            _park(self)
            return
        _disarm(self)                               # an idle session holds no duplicate
        if self._no_pool:
            _count("closed_cancelled")
            _finish(pgconn)
        else:
            give_back(pgconn, self._pool_key)

    def discard(self) -> None:
        """Close the session for real: for a lease that must not be reused (its role or serving check failed)."""
        pgconn, quiet = self._take_handle()
        if pgconn is None:
            return
        if not quiet:
            _park(self)
            return
        _finish(pgconn)
        _disarm(self)


def _finish(pgconn) -> None:
    try:
        pgconn.finish()
    except Exception:  # noqa: BLE001 - closing is best effort; the session is dropped either way
        pass


def _count(name: str) -> None:
    with _lock:
        _stats[name] += 1


class _Parked:
    """One parked lease: since when, and when its socket was shut down (None: not yet)."""

    __slots__ = ("lease", "since", "aborted_at")

    def __init__(self, lease, since: float):
        self.lease, self.since, self.aborted_at = lease, since, None


def _park(lease) -> None:
    """A lease that was closed while a statement was still in flight on it. Its session is never pooled, and neither
    its handle nor libpq is touched here (the statement is using both): `_reap` takes the handle and finishes the
    session once the statement has let go of the lock, and the reaper thread sees to it that `_reap` runs."""
    shed = []
    with _lock:
        # The reaper's hard ceiling, applied here too: between two passes a burst against a database that has stopped
        # answering parks one lease per request, so the list is capped AT INSERTION and never grows past
        # MAX_PARKED * PARKED_HARD_X. The oldest go first, ended and forgotten as the reaper would (`_shed`).
        while len(_parked) >= MAX_PARKED * PARKED_HARD_X:
            shed.append(_parked.pop(0))
        _parked.append(_Parked(lease, time.monotonic()))
        _stats["parked_busy"] += 1
    for entry in shed:
        _shed(entry)
    _ensure_reaper()


def _shed(entry) -> None:
    """A parked lease taken out over the ceiling at insertion: finished if its statement has let go, otherwise ended
    (if no pass has ended it yet) and forgotten, exactly the reaper's hard-ceiling rule. Every step it uses is safe
    to run twice, so a reaper pass holding the same entry in its copy does no harm. Never raises."""
    try:
        if _finish_if_free(entry.lease):
            _count("closed_parked")
            return
        if entry.aborted_at is None:
            _end(entry, time.monotonic(), True)
        else:
            # A reaper pass may be inside `_end` for this entry: it sets aborted_at BEFORE it calls `_abort`. Abort
            # here too before forgetting, or `_forget` could close the duplicate and drop the cancel token first and
            # leave the backend running (GitHub Codex on 1f7ef89e). `_abort` takes both under the lease's own lock,
            # so whichever call comes first shuts the socket and queues the one cancel; the other finds nothing.
            _abort(entry.lease)
    except Exception:  # noqa: BLE001
        pass
    _forget(entry)


def _ensure_reaper() -> None:
    """Started UNDER the lock: a thread is not alive until start() returns, and a second caller looking in between
    started a second reaper."""
    with _lock:
        t = _reaper[0]
        if t is not None and t.is_alive():
            return
        _reaper[0] = None
        try:
            t = threading.Thread(target=_reap_loop, name="dbpool-reaper", daemon=True)
            t.start()
        except Exception:  # noqa: BLE001 - no thread: the pool calls still reap, as before
            return
        _reaper[0] = t


def _reap_loop() -> None:
    while True:
        time.sleep(REAP_EVERY_S)
        _reap()
        with _lock:                                  # the same lock `_park` appends under: no park is missed
            if not _parked:
                _reaper[0] = None
                return


def _has_safe_cancel() -> bool:
    """Whether this libpq makes a cancel connection (17 and later): its request can be sent within a bound."""
    try:
        return bool(psycopg.capabilities.has_cancel_safe())
    except Exception:  # noqa: BLE001
        return False


def _abort(lease) -> bool:
    """End a parked session from outside, with what the lease took from its handle when it was made (`Lease._arm`)
    and nothing else. The statement's handle is NOT touched and not even read: its thread may be closing it at this
    instant, and a descriptor number read from it could by now belong to someone else's socket (round 6). Three
    things, each on its own: the lease is marked, so its own wait raises at its next step (within a tenth of a
    second, also where the shutdown wakes nobody); the socket is shut down through the duplicate; and the cancel
    request is queued for the worker (once: the handle is taken out). False: this lease has no duplicate."""
    with lease._abort_lock:
        lease._aborted = True
        dup = lease._abort_sock
        tok, lease._cancel_tok = lease._cancel_tok, None
        if dup is not None:
            try:
                dup.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 - already down, or the peer is gone
                pass
    if tok is not None:
        _queue_cancel(tok)
    return dup is not None


def _queue_cancel(tok) -> None:
    """Hand one cancel request to the worker. Never blocks and never raises. The queue holds at most its `maxlen`:
    a request that does not fit is dropped and counted (its session's socket is shut down all the same). One worker
    at a time, started under the lock like the reaper."""
    try:
        with _lock:
            if len(_cancels) >= (_cancels.maxlen or 0):
                _stats["cancel_dropped"] += 1
                drop, tok = tok, None
            else:
                drop = None
                _cancels.append(tok)
                t = _canceller[0]
                if t is None or not t.is_alive():
                    _canceller[0] = None
                    t = threading.Thread(target=_cancel_loop, name="dbpool-cancel", daemon=True)
                    t.start()
                    _canceller[0] = t
        if drop is not None:
            _drop_token(drop)
    except Exception:  # noqa: BLE001 - no thread: the request stays queued for the next one, or is never sent
        pass


def _cancel_loop() -> None:
    while True:
        with _lock:                                  # the same lock `_queue_cancel` appends under: none is missed
            if not _cancels:
                _canceller[0] = None
                return
            tok = _cancels.popleft()
        _send_cancel(tok)


def _send_cancel(tok) -> None:
    """Ask the server to cancel what one ended session is running. Uses only the handle `_arm` captured."""
    kind, handle = tok
    try:
        if kind == "safe":
            handle.start()
            waiting.wait_conn(generators.cancel(handle), interval=0.1, timeout=POOL_IO_TIMEOUT_S)
        else:
            # libpq before 17: one blocking call that opens a connection of its own, with no bound but the
            # system's. It can hold THIS thread for that long, and nothing else: the queue behind it is bounded.
            handle.cancel()
        _count("cancel_sent")
    except Exception:  # noqa: BLE001 - the server is gone or refuses: the socket is shut down either way
        try:
            _count("cancel_failed")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _drop_token(tok)


def _drop_token(tok) -> None:
    try:
        kind, handle = tok
        (handle.finish if kind == "safe" else handle.free)()
    except Exception:  # noqa: BLE001 - the driver frees it with the object anyway
        pass


def _disarm(lease) -> None:
    """Close the lease's duplicate of its socket and drop its unused cancel handle. Every way a session ends runs
    this once: returned, finished, forgotten. Until then the duplicate is an open descriptor, so `_abort` can never
    act on a recycled number. Never raises."""
    try:
        with lease._abort_lock:
            dup, lease._abort_sock = lease._abort_sock, None
            tok, lease._cancel_tok = lease._cancel_tok, None
    except Exception:  # noqa: BLE001
        return
    if dup is not None:
        try:
            dup.close()
        except Exception:  # noqa: BLE001
            pass
    if tok is not None:
        _drop_token(tok)


def _finish_if_free(lease) -> bool:
    """Finish a parked lease's session if its statement has let go of the lock. A lock that FAULTS is a lock that is
    not free (round 7): the lease then stays bounded like any other. Never raises."""
    try:
        if not lease.lock.acquire(blocking=False):
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        pgconn, lease.pgconn = lease.pgconn, _Gone()              # only now, with the statement gone
        _finish(pgconn)
    finally:
        try:
            lease.lock.release()
        except Exception:  # noqa: BLE001
            pass
    _disarm(lease)
    return True


def _forget(entry) -> None:
    """Stop tracking an ended lease. One more look first: if its statement has let go meanwhile, the session is
    finished properly instead (round 7). Never raises."""
    try:
        if _finish_if_free(entry.lease):
            _count("closed_parked")
            return
    except Exception:  # noqa: BLE001
        pass
    _disarm(entry.lease)
    try:
        _count("forgotten_parked")                   # its backend was asked to stop; the handle goes with the lease
    except Exception:  # noqa: BLE001
        pass


def _settle(entry, now: float) -> bool:
    """True when this parked lease is done with: its statement is over and the session finished, or it was ended
    PARK_FORGET_S ago and its lock never came free."""
    if _finish_if_free(entry.lease):
        _count("closed_parked")
        return True
    if entry.aborted_at is not None and now - entry.aborted_at >= PARK_FORGET_S:
        _disarm(entry.lease)
        _count("forgotten_parked")                   # its backend was asked to stop; the handle goes with the lease
        return True
    return False


def _end(entry, now: float, over: bool) -> None:
    entry.aborted_at = now
    shut = _abort(entry.lease)
    _count("aborted_parked")
    log.warning("db pool: a parked session's statement held it for %.0f s%s; %s",
                now - entry.since, " (more than %d parked)" % MAX_PARKED if over else "",
                "its connection was shut down" if shut else "it has no duplicate socket to shut down; its wait raises")


def _reap(now: float | None = None) -> None:
    """Finish every parked session whose statement has ended; end the ones that are too old or too many.
    Never blocks, never raises an Exception, and never loses a lease: the leases stay in `_parked` while a pass
    looks at them and only the finished ones are taken out, at the end. One pass at a time: a caller that finds a
    pass running returns at once. `now` is for the tests."""
    if not _reap_lock.acquire(blocking=False):
        return
    done: list[Any] = []
    try:
        with _lock:
            waiting = list(_parked)                  # oldest first
        now = time.monotonic() if now is None else now
        busy = []
        for entry in waiting:                        # first: everything that is over, whatever its place
            try:
                settled = _settle(entry, now)
            except Exception:  # noqa: BLE001 - a lease that faults is NOT done: it stays bounded below (round 7)
                settled = False
            (done if settled else busy).append(entry)
        running = [e for e in busy if e.aborted_at is None]
        over = len(running) - MAX_PARKED             # this many of the oldest STILL RUNNING are over the cap
        for n, entry in enumerate(running):
            try:
                if n < over or now - entry.since >= PARK_MAX_S:
                    _end(entry, now, n < over)
            except Exception:  # noqa: BLE001
                pass
        # Last, the ENDED ones, those of this pass among them (round 7: before, the count was taken first, so a
        # burst took two passes). One that has been ended for PARK_FORGET_S goes, whatever `_settle` did with it.
        # And more of them than the cap: the oldest go, but one ended less than SETTLE_GRACE_S ago is left for the
        # next pass, because its thread has just been woken and is about to let go; unless more than the hard
        # ceiling are parked. A burst against a database that has stopped answering parks one for every request.
        ended = [e for e in busy if e.aborted_at is not None]
        excess, left = len(ended) - MAX_PARKED, len(busy)
        for n, entry in enumerate(ended):
            try:
                expired = now - entry.aborted_at >= PARK_FORGET_S
                capped = n < excess and (now - entry.aborted_at >= SETTLE_GRACE_S or left > MAX_PARKED * PARKED_HARD_X)
                if expired or capped:
                    _forget(entry)
                    done.append(entry)
                    left -= 1
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 - housekeeping must never fail a request
        pass
    finally:
        try:
            with _lock:
                if done:
                    gone = {id(e) for e in done}
                    _parked[:] = [e for e in _parked if id(e) not in gone]
                left = bool(_parked)
            if left:
                _ensure_reaper()                     # whatever is still parked has a thread that will look again
        finally:
            _reap_lock.release()


def _lease(pgconn, key: tuple) -> Lease:
    lease = Lease(pgconn)
    lease._autocommit = True                        # as dbconn.connect opens it; no round trip
    lease._pool_key = key
    return lease


def _reset(pgconn, key: tuple) -> None:
    """Put the session back to what a new one is, within POOL_IO_TIMEOUT_S on the client. Raises on any failure or
    when the session does not answer in time; the caller then closes the session."""
    worker = _lease(pgconn, key)
    worker._deadline = time.monotonic() + POOL_IO_TIMEOUT_S
    try:
        # BEFORE the reset too (round 6): the driver reads every queued notification into memory while it runs a
        # statement, with no bound, so a flooded session is refused before the first one.
        _drain_notifies(pgconn)
        with worker.cursor() as cur:
            cur.execute("DISCARD ALL")
        rls.set_role(worker, key[2])
        # Notifications libpq had already queued for the last lease outlive UNLISTEN: the next lease must not read
        # them. DISCARD ALL has ended every LISTEN, so none can arrive after this drain.
        _drain_notifies(pgconn)
    finally:
        worker._deadline = None
        worker._released = True                     # the worker object is not a lease anyone holds
        worker.pgconn = _Gone()


def _drain_notifies(pgconn) -> None:
    """Read away what libpq has queued, at most MAX_NOTIFY_DRAIN; more is an error and the session is closed. Both
    calls are libpq's non-blocking ones (they read what the socket already holds), so they need no wait."""
    pgconn.consume_input()
    for _n in range(MAX_NOTIFY_DRAIN + 1):
        if pgconn.notifies() is None:
            return
    raise psycopg.OperationalError("more than %d queued notifications on a returned session" % MAX_NOTIFY_DRAIN)


def give_back(pgconn, key: tuple | None) -> None:
    """A lease was closed: reset its session and keep it idle, or close it. Never raises: it runs in `finally`."""
    _reap()
    try:
        keep = size()
        if key is None or keep < 1:
            _finish(pgconn)
            return
        if (pgconn.status != ConnStatus.OK or pgconn.transaction_status != TransactionStatus.IDLE
                or pgconn.pipeline_status != PipelineStatus.OFF):
            _count("closed_not_idle")
            _finish(pgconn)
            return
        with _lock:
            full = len(_idle) >= keep
            twice = any(e[0] is pgconn for e in _idle)
        if twice:                                   # never: a session cannot be idle and returned. Leave the idle one be.
            _count("double_return")
            log.error("db pool: a session that is already idle was returned again; the second return is dropped")
            return
        if full:                                    # before the reset: a session nobody will reuse is not reset
            _count("closed_full")
            _finish(pgconn)
            return
        try:
            _reset(pgconn, key)
        except Exception as exc:  # noqa: BLE001 - any failure to reset means this session is not reused
            _count("closed_reset_failed")
            log.warning("db pool: a session did not reset and was closed (%s)", type(exc).__name__)
            _finish(pgconn)
            return
        extra = []
        with _lock:
            if len(_idle) < keep and not any(e[0] is pgconn for e in _idle):
                _idle.append((pgconn, key, time.monotonic()))
                _stats["returned"] += 1
                pgconn = None
            # The bound stated at the container: `keep` is at most MAX_POOL_SIZE, so this never fires; it is here so
            # that no code path can grow the idle list past the largest pool there is (the cache census).
            while len(_idle) > MAX_POOL_SIZE:
                extra.append(_idle.pop(0)[0])
        for old in extra:
            _count("closed_full")
            _finish(old)
        if pgconn is not None:                      # the pool filled while this one was resetting
            _count("closed_full")
            _finish(pgconn)
    except Exception:  # noqa: BLE001 - a pool fault must never fail the request that is closing its connection
        log.exception("db pool: give_back failed; the session is closed")
        _finish(pgconn)


def take(key: tuple, verify) -> Lease | None:
    """An idle session for `key`, proven by `verify(lease)` (dbconn.assert_not_bypassing), or None when there is
    none: the caller then connects as it always did. Never raises."""
    held = None                                     # the session popped and not yet handed out or closed
    _reap()
    # ONE bound for the whole checkout (#650, codex-kxxx-cli 624f66 P2): a fresh POOL_IO_TIMEOUT_S per popped session
    # let a request spend MAX_POOL_SIZE proof windows (16 x 5 s) on sessions whose network went dark, before
    # dbconn.connect even started its own connect_timeout-bounded fresh connect.
    budget_end = time.monotonic() + POOL_IO_TIMEOUT_S
    try:
        while True:
            held = None
            if time.monotonic() >= budget_end:      # spent: the caller connects fresh; what is left stays idle
                _count("take_budget_spent")
                return None
            with _lock:
                entry = _idle.pop() if _idle else None
            if entry is None:
                return None
            pgconn, its_key, since = entry
            held = pgconn
            if its_key != key:                      # the DSN or the bound changed: that session is not this one
                _count("closed_other_key")
                _finish(pgconn)
                continue
            if time.monotonic() - since > MAX_IDLE_S:
                _count("closed_stale")
                _finish(pgconn)
                continue
            lease = _lease(pgconn, key)
            lease._deadline = budget_end            # the rest of the one checkout bound, not a new window
            try:
                verify(lease)
            except psycopg.Error:                   # dropped while idle, or it did not answer within the bound
                lease._released = True
                _count("closed_dead")
                _finish(pgconn)
                continue
            except Exception as exc:  # noqa: BLE001 - the role check itself refused a POOLED session
                lease._released = True
                _count("closed_bypass")
                log.error("db pool: a pooled session failed its role check and was closed (%s: %s)",
                          type(exc).__name__, str(exc)[:200])
                _finish(pgconn)
                continue
            _count("reused")
            _maybe_log()
            lease._deadline = None                  # the caller's own statements are bounded as they always were
            lease._arm()                            # while only this thread can see it
            held = None                             # the last step: from here the lease is the caller's
            return lease
    except Exception:  # noqa: BLE001 - the pool is an optimisation: any fault here means "connect as usual"
        if held is not None:                        # FIRST: the session this pass had popped is closed, never dropped open
            _finish(held)
        try:                                        # then the report, which must not be able to skip the line above
            if held is not None:
                _count("closed_fault")
            log.exception("db pool: take failed; connecting as usual")
        except Exception:  # noqa: BLE001
            pass
        return None


def fresh() -> None:
    """Counted by dbconn.connect when the pool is on and it had to open a new session."""
    _count("fresh")
    _maybe_log()


def _maybe_log() -> None:
    now = time.monotonic()
    with _lock:
        if now - _last_log[0] < STATS_LOG_EVERY_S:
            return
        _last_log[0] = now
        line = dict(_stats, idle=len(_idle))
    log.info("db pool: %s", " ".join("%s=%d" % kv for kv in sorted(line.items())))


def stats() -> dict:
    with _lock:
        return dict(_stats, idle=len(_idle), parked=len(_parked))


def drain() -> int:
    """Close every idle session (a test's teardown; an operator's hand). Returns how many."""
    _reap()
    with _lock:
        entries, _idle[:] = list(_idle), []
    for pgconn, _key, _since in entries:
        _finish(pgconn)
    return len(entries)
