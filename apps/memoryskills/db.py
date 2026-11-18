"""The connection shape the memory skills require, and nothing else.

The Django/ASGI process runs as `meshychat_app`, and all three memory tables force RLS with a single server-internal
policy each. Measured off pg_policies:

    shared_memory            ALL  current_setting('app.server_internal', true) = '1'
    shared_memory_grant      ALL  current_setting('app.server_internal', true) = '1'
    shared_memory_namespace  ALL  current_setting('app.server_internal', true) = '1'

and relrowsecurity = relforcerowsecurity = true on all three. So a connection
WITHOUT that setting sees zero rows and writes nothing - and raises no error. Every
handler therefore runs its whole body inside `rls.server_internal(conn)`.

`peers`, `dashboard_users`, `peer_keys` and `tenancy_denials` have NO RLS at all
(relrowsecurity false, measured), which is why the audit insert can run outside the
block after a rollback.

AUTOCOMMIT IS NOT OPTIONAL. `rls.server_internal` sets the flag with `SET LOCAL`
semantics inside `conn.transaction()`; on a non-autocommit connection psycopg has
already opened an implicit transaction, so that becomes a SAVEPOINT and the setting
binds to the OUTER transaction, outliving the block. apps/core/rls.py refuses that
connection outright rather than documenting it.

ONE CONSTRUCTOR, ONE DATABASE (2026-09-23, R3 of the migration synthesis). This module
used to open its own connection, resolved from `MESHY_TEST_DSN` with a hard-coded dev
fallback: a TEST variable, read by nothing else in the serving code, ignoring
settings.MESHY_DSN, MESHY_DSN and DATABASE_URL. The role was bound correctly, so every
role check passed it; the DATABASE was not, so an in-process dispatcher, a shell, or the
usage this package's docstring documents would have run the memory skills against the dev
database while every route used the configured one. It now delegates to
`apps.core.dbconn`: `dbconn.dsn()` is the one resolution, `dbconn.connect()` sets AND
verifies the role, and `assert_serving_connection` probes that RLS is really in force, the
same checks a request's connection crosses. The request path itself (apps/calls/views.py
-> apps/calls/route.py) never used this module: it dispatches on the request's own
`dbconn.serving_connection`. tests/test_every_connection_is_role_bound.py refuses a module
under apps/ that opens its own connection again; docs/V1-DESIGN-RULES.md B1.
"""
from __future__ import annotations

import psycopg

from apps.core import dbconn


def dsn() -> str:
    """The project's one resolution (settings.MESHY_DSN, MESHY_DSN, DATABASE_URL, then the
    dev default), never MESHY_TEST_DSN: the test harness points MESHY_DSN at its clone."""
    return dbconn.dsn()


def connect(url: str | None = None) -> psycopg.Connection:
    """An autocommit connection running as `meshychat_app`, ready for the handlers.

    Callers own closing it. `dbconn.connect` sets the role and observes that it took;
    `assert_serving_connection` is then run as the POSITIVE CONTROL a request's
    connection gets: connecting as a bypassing role is the silent cross-tenant leak, and
    it looks identical to a healthy connection from the application's side.
    """
    conn = dbconn.connect(url or dsn())
    try:
        dbconn.assert_serving_connection(conn)
    except Exception:
        conn.close()
        raise
    return conn
