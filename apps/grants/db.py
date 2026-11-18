"""Connections for the grants surface.

Every connection is AUTOCOMMIT and has done `SET ROLE meshychat_app`, via
`apps.core.dbconn.connect` - the two properties `apps/core/rls.py` refuses to make
optional (owner roles BYPASS row-level security; `SET LOCAL` on a non-autocommit
connection binds to the enclosing transaction and leaks the context onto whatever the
pooled connection does next).

MEASURED ON THE LIVE SCHEMA, NOT ASSUMED - and it changes what "zero rows" means:

    SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class …
      skill_grants            f  f
      skill_access_requests   f  f
      peers                   f  f
      dashboard_users         f  f

NEITHER TABLE ON THIS SURFACE HAS ROW-LEVEL SECURITY. So their authorisation is
entirely the ROUTE's - which is why every query in repository.py is keyed on an owner
id that came from the authenticated credential and never from the body - and, second,
why a zero-row read here genuinely means "no data" rather than "no context bound".
That distinction is the one `apps/core/rls.py` warns is silent, so it is not taken on
trust: `assert_route_scoped_tables_are_not_rls_bound` below is a control any caller
(and `tests/test_grants.py`) can run, and it FAILS if a later migration enables RLS on
either table - at which point the `Empty` outcome in repository.py becomes ambiguous
and the reads need a bound context.

`meshychat_app` holds SELECT/INSERT/UPDATE/DELETE on both tables plus `peers`,
`dashboard_users`, `owner_api_keys`, `owner_sessions`, `owner_human_keys` and `skills`
(information_schema.role_table_grants), so SET ROLE does not lock this app out.
"""
from __future__ import annotations

import contextlib

from apps.core import dbconn

#: The tables this app reads/writes whose protection is the route's, not the database's.
ROUTE_SCOPED_TABLES = ("skill_grants", "skill_access_requests")


class RlsAssumptionBroken(RuntimeError):
    """A table this app treats as route-scoped has become RLS-bound."""


@contextlib.contextmanager
def connection():
    """An autocommit `meshychat_app` connection that has PASSED the serving guard, closed
    when the block ends.

    `dbconn.serving_connection` runs `assert_serving_connection` first, as files, a2a
    and calls do, so a connection that bypasses row-level security (the bootstrap
    role) is refused here rather than served. The two tables this app owns carry no
    RLS today, but the guard is about the connection, not the table.
    """
    with dbconn.serving_connection() as conn:
        yield conn


def assert_route_scoped_tables_are_not_rls_bound(conn, tables=ROUTE_SCOPED_TABLES) -> None:
    """Raise unless every named table is still RLS-free.

    Reads `relrowsecurity` AND the policy count, because a table can carry policies
    while RLS is disabled (they are inert) and enabling it later is a one-line
    migration that would silently start filtering these reads.
    """
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(
                "SELECT c.relrowsecurity, "
                "       (SELECT count(*) FROM pg_policies p "
                "         WHERE p.schemaname='public' AND p.tablename = c.relname) "
                "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE n.nspname='public' AND c.relname = %s", (table,))
            row = cur.fetchone()
            if row is None:
                raise RlsAssumptionBroken(f"table {table!r} does not exist")
            enabled, policies = row
            if enabled:
                raise RlsAssumptionBroken(
                    f"row-level security is now ENABLED on {table!r} ({policies} policies). "
                    "Reads through this app are no longer route-scoped: a zero-row result "
                    "may mean 'no context bound' rather than 'no data', so repository.Empty "
                    "is ambiguous until a peer/owner context is bound on the read path.")
