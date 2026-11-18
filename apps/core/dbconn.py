"""Connection plumbing: every connection that serves a request must be RLS-bound.

`apps/core/rls.py` supplies the *mechanism* (SET ROLE, the LOCAL settings, and the
`assert_rls_active` control). This module supplies the *policy*: a request is never
served on a connection that could bypass row-level security, and the refusal is an
exception rather than a log line.

Two independent things make a connection unsafe, and each one alone reads as healthy:

  1. the session runs as a role with `rolbypassrls` (the owner login does;
     `SELECT rolname, rolbypassrls FROM pg_roles` shows meshychat_owner with it and meshychat_app without).
     Every tenant's rows are then visible and no error is raised.
  2. the connection is NOT in autocommit, so psycopg has already opened an implicit
     transaction and `conn.transaction()` degrades to a SAVEPOINT. `SET LOCAL` then
     binds to the OUTER transaction and the caller's peer context survives the block,
     leaking onto whatever that pooled connection does next. rls.py refuses this at
     context-entry; we refuse it earlier, when the connection is handed a request, so
     the failure is attributed to the connection rather than to the first view that
     happens to open a context.

The request role is selected unconditionally because an unset configuration variable must
not silently disable row-level security and disclose data.
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from typing import Iterator

import psycopg

from apps.core import dbpool, rls
from apps.core.env import lookup

#: The dev/test database the SQL migrations are applied to. Overridden by MESHY_DSN, then by
#: DATABASE_URL (a compatibility fallback).
DEFAULT_DSN = "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django"

#: Table used as the subject of the RLS control. Any FORCE-RLS table would do;
#: shared_memory is the one rls.assert_rls_active defaults to.
RLS_CONTROL_TABLE = "shared_memory"

#: E15. Every connection receives a statement timeout so a runaway query cannot pin a backend.
#: `connect` applies the bound whenever its caller names none. A caller that names one (the
#: job runner's per-tick bound, the health probe) keeps its own.
STATEMENT_TIMEOUT_ENV = "MESHYCHAT_DB_STATEMENT_TIMEOUT_MS"
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
#: The largest `statement_timeout` Postgres accepts: the GUC is a C int of milliseconds.
MAX_STATEMENT_TIMEOUT_MS = 2_147_483_647
#: The bound on ESTABLISHING a connection when a caller names none (12 Nov 2026: two production wedges with request
#: threads piled on connects while HQD paged; memory-bxxxxx and 5652f6). libpq has no default bound at all.
CONNECT_TIMEOUT_ENV = "MESHYCHAT_DB_CONNECT_TIMEOUT_S"
DEFAULT_CONNECT_TIMEOUT_S = 10
MAX_CONNECT_TIMEOUT_S = 3600


class RlsBypassError(RuntimeError):
    """The connection offered to serve a request is not subject to RLS."""


class NonAutocommitError(RuntimeError):
    """The connection would bind `SET LOCAL` to a savepoint, leaking the context."""


def dsn() -> str:
    """The DSN, resolved at CALL time so tests and operators can move the database.

    Django settings win when configured (settings.MESHY_DSN), then the environment,
    then the dev default. Read late on purpose: reading at import time would freeze
    whichever value happened to exist when the first module imported this one.
    """
    try:  # pragma: no cover - trivial branch, exercised both ways by the test suite
        from django.conf import settings as _s

        if _s.configured and getattr(_s, "MESHY_DSN", None):
            return _s.MESHY_DSN
    except Exception:
        pass
    return os.environ.get("MESHY_DSN") or os.environ.get("DATABASE_URL") or DEFAULT_DSN


def default_statement_timeout_ms() -> int:
    """The bound `connect()` applies when its caller names none, read at CALL time.

    `MESHYCHAT_DB_STATEMENT_TIMEOUT_MS` is
    a non-negative integer of milliseconds; `0` disables the timeout, and unset or a value
    that does not parse (`-1`, `1.5`, `thirty`) is the 30 s default. The fallback rather than
    a refusal is deliberate: this is read for every connection, and a typo must not turn
    every request into a 500. It is logged instead, so the typo is visible.

    A value above `MAX_STATEMENT_TIMEOUT_MS` is a value that does not parse, too. Postgres
    refuses it at session start ("Value exceeds integer range"), so passing it on would fail
    every connection, the outcome this fallback exists to prevent.
    """
    raw = lookup(STATEMENT_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_STATEMENT_TIMEOUT_MS
    text = raw.strip()
    if text.isdigit() and text.isascii() and int(text) <= MAX_STATEMENT_TIMEOUT_MS:
        return int(text)
    logging.getLogger("meshychat.db").warning(
        "%s=%r is not a non-negative integer of milliseconds up to %d; using the default %d ms",
        STATEMENT_TIMEOUT_ENV, raw, MAX_STATEMENT_TIMEOUT_MS, DEFAULT_STATEMENT_TIMEOUT_MS)
    return DEFAULT_STATEMENT_TIMEOUT_MS


def default_connect_timeout_s() -> int:
    """The connect bound `connect()` applies when its caller names none, read at CALL time, like the statement bound.

    `MESHYCHAT_DB_CONNECT_TIMEOUT_S` is a non-negative integer of seconds; `0` disables it (libpq's own default: wait
    as long as the OS does), and unset or a value that does not parse (`-1`, `1.5`, `ten`, above
    MAX_CONNECT_TIMEOUT_S) is the 10 s default, logged, never a refusal: it is read for every connection. It bounds
    connection SETUP only; the SET ROLE and the role check after it are under the statement bound.
    """
    raw = lookup(CONNECT_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_CONNECT_TIMEOUT_S
    text = raw.strip()
    if text.isdigit() and text.isascii() and int(text) <= MAX_CONNECT_TIMEOUT_S:
        return int(text)
    logging.getLogger("meshychat.db").warning(
        "%s=%r is not a non-negative integer of seconds up to %d; using the default %d s",
        CONNECT_TIMEOUT_ENV, raw, MAX_CONNECT_TIMEOUT_S, DEFAULT_CONNECT_TIMEOUT_S)
    return DEFAULT_CONNECT_TIMEOUT_S


def _target_sets_connect_timeout(target: str) -> bool:
    """Whether the DSN or libpq's environment already names a connect bound (then the default stays out of it)."""
    from psycopg.conninfo import conninfo_to_dict

    if os.environ.get("PGCONNECT_TIMEOUT", "").strip():
        return True
    try:
        return "connect_timeout" in conninfo_to_dict(target)
    except Exception:  # a DSN psycopg cannot parse fails at connect with its own error, not here
        return False


def _options_with(target: str, setting: str) -> str:
    """libpq `options` carrying `setting` AFTER whatever the DSN (or PGOPTIONS) already set.

    Passing `options=` to psycopg.connect REPLACES the DSN's own `options` and PGOPTIONS, so
    a DSN that carries `-c search_path=...` would silently lose it once every connection is
    bounded. Appended rather than replaced; a later `-c` for the same name wins in Postgres,
    so an explicit bound still decides the timeout.
    """
    from psycopg.conninfo import conninfo_to_dict

    existing = (conninfo_to_dict(target).get("options")
                or os.environ.get("PGOPTIONS") or "").strip()
    return f"{existing} {setting}" if existing else setting


#: A connect at least this slow is logged with the process's thread count (pxx).
SLOW_CONNECT_LOG_S = 0.1


def connect(target: str | None = None, *, connect_timeout: float | None = None,
            statement_timeout_ms: int | None = None,
            application_name: str | None = None,
            role: str = rls.APP_ROLE,
            verify_role: bool = True, pool: bool = True) -> psycopg.Connection:
    """Open an autocommit connection already switched to the RLS-bound role, and CHECK it.

    autocommit is not a tuning choice: see the module docstring and rls.py's
    `_require_no_open_transaction`.

    `verify_role` costs one round trip and turns `SET ROLE` from an instruction into an
    observation - see `assert_not_bypassing`. Pass False only where a bypassing role is the
    POINT (the migrator runs DDL as the owner). Passing it False to make a test go green
    would remove the only runtime check that the role took effect.

    `statement_timeout_ms` None means the default bound, `default_statement_timeout_ms`
    (E15): every connection this opens, the request's included, carries one.
    """
    kwargs: dict = {"autocommit": True}
    # The pool (apps/core/dbpool.py, OFF unless MESHYCHAT_DB_POOL_SIZE is set) serves only the connection a caller
    # asks for with no argument of its own: the configured DSN, the default bounds, the serving role, checked.
    # `pool=False`: a caller that will LISTEN on the connection, or wants a session of its own for any reason.
    poolable = (pool is True and dbpool.size() > 0 and target in (None, dsn()) and connect_timeout is None
                and statement_timeout_ms is None and application_name is None and role == rls.APP_ROLE
                and verify_role is True)
    target = target or dsn()
    if statement_timeout_ms is None:
        statement_timeout_ms = default_statement_timeout_ms()
    if connect_timeout is None and not _target_sets_connect_timeout(target):
        # The default bound (0 = off). An explicit caller's value, a DSN's own connect_timeout and libpq's
        # PGCONNECT_TIMEOUT all win: a keyword would silently override the latter two.
        connect_timeout = default_connect_timeout_s() or None
    if connect_timeout is not None:
        # libpq's connect_timeout is integer SECONDS and a value < 2 is silently
        # treated as 2; a float would be rendered into the conninfo string and
        # rejected, so it is coerced here rather than at every call site.
        kwargs["connect_timeout"] = max(1, int(connect_timeout))
    # -c is applied by the server at session start, so it is in force before the first
    # statement rather than after an extra round trip, and a RESET returns to it.
    # TimeZone=UTC: psycopg returns timestamptz in the session's zone, and Python compares
    # or subtracts two datetimes that share a zone as wall-clock times, so on a server whose
    # zone has a clock change an interval across it reads an hour off. Pinned here, every
    # connection reads UTC whatever the server's default (production's is already Etc/UTC).
    kwargs["options"] = _options_with(
        target, f"-c statement_timeout={int(statement_timeout_ms)} -c TimeZone=UTC")
    if application_name is not None:
        # What pg_stat_activity shows for the session: a long-lived connection says whose it
        # is (a WebSocket's own connection names its socket, apps/wsgw/target_frames.py).
        kwargs["application_name"] = application_name
    pool_key = (target, kwargs["options"], role) if poolable else None
    if pool_key is not None:
        # A reused session has passed assert_not_bypassing just now (that check is also its liveness check), and
        # carries the role and the marker from its reset. None means no idle session: connect as always.
        lease = dbpool.take(pool_key, assert_not_bypassing)
        if lease is not None:
            return lease
        dbpool.fresh()
    started = time.monotonic()
    if pool_key is not None:
        conn = dbpool.Lease.connect(target, **kwargs)
        conn._pool_key = pool_key
    else:
        conn = psycopg.connect(target, **kwargs)
    took = time.monotonic() - started
    if took >= SLOW_CONNECT_LOG_S:
        # Measurement for the pool decision (pxx): "connects pile up" as a number from production, with how
        # many threads the process had at that moment, not a count of threads in a stack dump.
        logging.getLogger("meshychat.db").warning(
            "slow database connect: %.3fs (threads %d)", took, threading.active_count())
    try:
        if role:
            rls.set_role(conn, role)
            if verify_role:
                assert_not_bypassing(conn)
    except Exception:
        # a session that failed its role binding is closed for real, never returned to the pool
        getattr(conn, "discard", conn.close)()
        raise
    return conn


def assert_not_bypassing(conn) -> None:
    """Raise unless `conn`'s CURRENT role is one that row-level security applies to.

    THE DIFFERENCE FROM `assert_serving_connection`, and why both exist: that one probes a
    named FORCE-RLS table, so it answers "is RLS in force for this connection on THAT
    table". This one asks the connection what it IS. It depends on no table, so it is the
    check every surface can run - including the ones whose own tables carry no RLS, where
    `apps/meshguard/db.py` correctly declined the table probe as asserting a property it
    does not depend on.

    WHY IT IS WORTH A ROUND TRIP. `rls.set_role()` is an INSTRUCTION. Nothing downstream
    observed whether it took effect, and adversarial review found exactly that gap live on
    two surfaces: replacing the guarded constructor with a bare `psycopg.connect` as the
    bootstrap `meshy` role (`rolbypassrls = true`) served every request and passed 28/28 and
    34/34. A bypassing role does not fail - it silently returns every tenant's rows, which
    is the failure direction that costs the most and shows the least.

    An AST-level check that the `set_role` CALL is present (see
    `tests/test_every_connection_is_role_bound.py`) cannot see this: the mutation does not
    remove the call, it opens a different connection entirely.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_user, "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)")
        role, bypasses = cur.fetchone()
    if bypasses:
        raise RlsBypassError(
            f"refusing to serve a request on a connection running as {role!r}, which "
            "BYPASSES row-level security. Open it with apps.core.dbconn.connect(), which "
            "does SET ROLE and then verifies it."
        )


def assert_serving_connection(conn, *, table: str = RLS_CONTROL_TABLE) -> None:
    """Raise unless `conn` may be used to serve a request. No return value on success.

    Order matters: autocommit is checked first because it is a property of the client
    and needs no round trip, and because a non-autocommit connection would also make
    the RLS probe below run inside the caller's open transaction.
    """
    try:
        _serving_checks(conn, table)
    except Exception:
        # A pooled lease that fails the guard is closed for real, HERE, so that no caller's close() after the refusal
        # can return it to the pool (five call sites close() on this failure; a plain connection has no discard and
        # is closed by its caller as before).
        discard = getattr(conn, "discard", None)
        if callable(discard):
            discard()
        raise


def _serving_checks(conn, table: str) -> None:
    if not conn.autocommit:
        raise NonAutocommitError(
            "refusing to serve a request on a connection that is not in autocommit: "
            "SET LOCAL would bind to a savepoint inside psycopg's implicit transaction "
            "and the peer context would outlive its block (see apps/core/rls.py)."
        )
    try:
        rls.assert_rls_active(conn, table=table)
    except RuntimeError as exc:  # rls.py raises a bare RuntimeError for both causes
        raise RlsBypassError(str(exc)) from exc


@contextlib.contextmanager
def serving_connection(target: str | None = None, **kwargs) -> Iterator[psycopg.Connection]:
    """A connection that has PASSED the guard, closed when the block ends."""
    conn = connect(target, **kwargs)
    try:
        assert_serving_connection(conn)
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def request_peer_context(conn, peer_id=None, owner_id=None) -> Iterator[psycopg.Connection]:
    """Bind a request's identity to the transaction, guard first.

    Thin over `rls.peer_context`; the point of the wrapper is that the guard runs on
    the path a request actually takes, so no view can reach the database through a
    bypassing connection by calling rls.py directly and skipping the check.
    """
    assert_serving_connection(conn)
    with rls.peer_context(conn, peer_id=peer_id, owner_id=owner_id) as c:
        yield c


@contextlib.contextmanager
def request_server_internal(conn) -> Iterator[psycopg.Connection]:
    """The privileged server-side context (`app.server_internal='1'`), guard first.

    rls.py: "enter it for a specific server-side operation, never for the length of a
    request" - so this is deliberately NOT what the middleware binds by default.
    """
    assert_serving_connection(conn)
    with rls.server_internal(conn) as c:
        yield c


def ping(target: str | None = None, *, timeout: float = 2.0) -> bool:
    """`SELECT 1` against the database, bounded, never raising.

    Bounded twice - connect_timeout for a
    black-holed host, statement_timeout for a server that accepts and then stalls -
    because the caller runs this on a worker thread and a thread that never returns
    would outlive the request.
    """
    try:
        with contextlib.closing(connect(target, connect_timeout=timeout,
                                        statement_timeout_ms=int(timeout * 1000))) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ping")
                return cur.fetchone()[0] == 1
    except Exception:
        return False
