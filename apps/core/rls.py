"""Row-level-security context.

The database does the authorisation, not the application. MEASURED on the live schema:
26 policies over 12 tables (an earlier draft said 32, which was a count of CREATE POLICY
statements in the migration files - an upper bound, not the surviving objects). They
compare against `current_setting('app.current_peer_id')` and run as role `meshychat_app`.
Two things must therefore be true of EVERY connection that serves a request:

  1. the session has done `SET ROLE meshychat_app`          (else policies do not apply)
  2. the transaction has set `app.current_peer_id`     (else policies see NULL)

WHY THE FAILURE MODE IS SILENT: under FORCE RLS a missing setting makes
`current_setting(..., true)` return NULL, the policy comparison yields NULL, and the
row simply does not match. No error is raised. The request returns an empty list and
looks like "no data" rather than "misconfigured". The opposite mistake - connecting as
the owner role, which BYPASSES RLS - returns every tenant's rows just as quietly.

So this module refuses to be optional:
  - `peer_context()` sets the variable LOCAL to a transaction, so it cannot leak onto
    a pooled connection and be inherited by the next, unrelated request;
  - `assert_rls_active()` is a positive control a caller can run on any connection.
"""
from __future__ import annotations

import contextlib

import psycopg

APP_ROLE = "meshychat_app"

#: Divergence 039 (sql_meshychat/014). `set_role` sets it on every session this build opens,
#: beside SET ROLE, and the agree-trigger refuses a shared_memory row without
#: `signed_bytes` only from a `meshychat_app` session that carries it. The role alone cannot say
#: whether the session carries the marker: under A1, an unmarked `meshy_app` session (now `meshychat_app`) could insert
#: rows without bytes (the dashboard's memory create, revise and message send, the identity
#: rate watch), which A1 had to admit (design 4.1). Since A2 (divergence 051,
#: sql_meshychat/025) every session is refused a row without bytes, so the marker only
#: decides which trigger refuses it (014's fires first for a marked session). It stays: 025's
#: down script gives back A1, which needs it. Session-level, like the role: both last as
#: long as the connection and are undone together by a RESET ALL.
SIGNED_BYTES_WRITER_SETTING = "meshy.signed_bytes_writer"


def _require_no_open_transaction(conn, what: str) -> None:
    """`SET LOCAL` binds to the INNERMOST-ENCLOSING transaction, not to a savepoint.

    Caught by `test_context_does_not_leak_to_the_next_transaction`: on a connection that
    is NOT in autocommit, psycopg has already opened an implicit transaction, so
    `conn.transaction()` becomes a SAVEPOINT and the setting binds to the OUTER
    transaction - surviving the block and leaking the caller's visibility onto whatever
    the connection does next. That is the silent cross-tenant failure this whole module
    exists to prevent, so it is refused rather than documented.
    """
    status = conn.info.transaction_status
    if status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            f"{what} needs a connection with NO open transaction (status={status.name}). "
            "Use an autocommit connection so the context is bound to a real transaction "
            "and released with it; otherwise the setting outlives the block."
        )


def set_role(conn, role: str = APP_ROLE) -> None:
    """Run as the RLS-bound role. Must happen on every new connection.

    Also marks the session as this build's (SIGNED_BYTES_WRITER_SETTING, divergence 039),
    so a regression that drops `signed_bytes` from this build's INSERT fails the write,
    while an unmarked session must not be identified as this build merely from its role."""
    with conn.cursor() as cur:
        cur.execute(f'SET ROLE "{role}"')
        cur.execute(f"SET {SIGNED_BYTES_WRITER_SETTING} = 'on'")


def current_role(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        return cur.fetchone()[0]


@contextlib.contextmanager
def server_internal(conn):
    """The THIRD mechanism, and the one most easily missed.

    Measured from the live schema: the policies do not all key on the peer. Three
    distinct rules are in use --
      peer-scoped    `skill_calls`, `local_memory`   -> app.current_peer_id
      tenant-scoped  `meshy_memory`                   -> app.current_owner_id
      server-internal `shared_memory`, `shared_memory_grant`, `shared_memory_namespace`,
                      `work_pool`, 5 `dashboard_*`   -> app.server_internal = '1'

    So a request path that sets only the peer id sees NOTHING in shared_memory, and a
    path that sets server_internal sees EVERYTHING in it. This is a privileged context:
    enter it for a specific server-side operation, never for the length of a request.
    """
    _require_no_open_transaction(conn, "server_internal()")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.server_internal', '1', true)")
        yield conn


#: The GUC only background jobs set (sql_meshychat/008, divergence 022). Named once, here:
#: tests/test_projection_writer.py refuses the string anywhere else in the code.
PROJECTION_WRITER_SETTING = "app.projection_writer"


@contextlib.contextmanager
def projection_writer(conn):
    """`server_internal` PLUS the right to write a projection table. Jobs and their CLIs only.

    Divergence 022. Every table derived from shared_memory (mesh_edges, the vectors, the
    inferred edges, and each one's state row) READS under `app.server_internal = '1'` and
    WRITES only when `app.projection_writer = '1'` is ALSO set (sql_meshychat/008). A
    request holds `server_internal` for its reads, so without the second flag "no request
    path writes a projection" would be a convention the database agreed to break.

    Only the job bodies enter this: `apps.mesh.projection.catch_up`/`rebuild`,
    `apps.meshsem.embedjob.catch_up`, `apps.meshsem.graphwalk.catch_up_namespace` (divergence
    060), `apps.meshsem.related.catch_up` (and its
    `_record_status`, divergence 032), `apps.memoryskills.legacy_verdict.catch_up`
    (divergence 039), and `apps.meshaudit.derive` on a `--write` run. TWO request paths are
    allowed in besides: `apps.memoryskills.redact.handle_redact` (divergence 053) and
    `apps.memoryskills.edit.handle_edit` (divergence 059), which delete the derived rows of the
    version they blank or replace in the same transaction; without the flag those deletes match
    nothing.
    tests/test_projection_writer.py enforces that list by AST and refuses the setting's
    name anywhere but this module. Both values are LOCAL to the transaction, like
    `server_internal`, so a connection that ran a job carries neither afterwards.
    """
    _require_no_open_transaction(conn, "projection_writer()")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.server_internal', '1', true), "
                        f"set_config('{PROJECTION_WRITER_SETTING}', '1', true)")
        yield conn


@contextlib.contextmanager
def peer_context(conn, peer_id: str | None = None, owner_id: str | None = None):
    """Bind the transaction to a peer/owner, then clear by ending the transaction.

    `set_config(..., is_local => true)` ties the value to the CURRENT TRANSACTION.
    A session-level setting would survive on a pooled connection and silently grant
    the next request the previous caller's visibility.
    """
    _require_no_open_transaction(conn, "peer_context()")
    with conn.transaction():
        with conn.cursor() as cur:
            if peer_id is not None:
                cur.execute("SELECT set_config('app.current_peer_id', %s, true)", (str(peer_id),))
            if owner_id is not None:
                cur.execute("SELECT set_config('app.current_owner_id', %s, true)", (str(owner_id),))
        yield conn


def context_values(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.current_peer_id', true), "
                    "       current_setting('app.current_owner_id', true), current_user")
        peer, owner, role = cur.fetchone()
    return {"peer_id": peer, "owner_id": owner, "role": role}


def assert_rls_active(conn, table: str = "shared_memory") -> None:
    """Raise unless this connection is actually subject to RLS on `table`.

    Checks the two independent things that must BOTH hold, because either one alone
    reads as healthy: the role is not a bypasser, and the table forces RLS.
    """
    with conn.cursor() as cur:
        # relrowsecurity = RLS enabled; relforcerowsecurity = enforced even for the table
        # OWNER. Checking only the first passes on a table whose owner is exempt, which is
        # the case this control exists to catch. Both are read.
        cur.execute("SELECT current_user, "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user), "
                    "(SELECT relrowsecurity FROM pg_class c JOIN pg_namespace n "
                    "   ON n.oid = c.relnamespace WHERE n.nspname='public' AND c.relname=%s), "
                    "(SELECT relforcerowsecurity FROM pg_class c JOIN pg_namespace n "
                    "   ON n.oid = c.relnamespace WHERE n.nspname='public' AND c.relname=%s)",
                    (table, table))
        role, bypasses, enabled, forced = cur.fetchone()
    if bypasses:
        raise RuntimeError(
            f"connection runs as {role!r}, which BYPASSES row-level security. Every tenant's "
            "rows would be visible and no error would be raised. Call set_role() first."
        )
    if not enabled:
        raise RuntimeError(f"row-level security is not enabled on {table!r}")
    if not forced:
        raise RuntimeError(
            f"row-level security on {table!r} is ENABLED but not FORCED, so the table owner "
            "is exempt. A connection that happens to own the table reads everything and "
            "raises nothing.")
