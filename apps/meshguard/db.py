"""Connection handling for the meshguard surface.

MEASURED against the live schema (information_schema / pg_class / pg_policies, not
assumed), 2026-09-21:

  * NONE of the seven `meshy_guard_*` tables has row-level security enabled:
    `pg_class.relrowsecurity` is FALSE for meshy_guard_approvals, _policies,
    _policy_attachments, _policy_decisions, _scan_results, _flow_decisions and
    _reasoning_traces, and `pg_policies` lists no policy on any of them.
  * `meshy_app` (now `meshychat_app`) held SELECT/INSERT/UPDATE/DELETE on all seven
    (`information_schema.role_table_grants`), so `SET ROLE meshy_app` did not lock this
    app out of its own tables.

THE CONSEQUENCE, stated plainly because it is the thing a reviewer must check: the
database does NOT enforce tenancy on this surface. `sql_meshychat/001_write_side_ownership`
closed the write side of RLS for `local_memory`, `skill_calls` and `meshy_memory` - it does
not reach these tables, because they have no policies to ALTER. So every read and every
write below is authorised by the ROUTE, and the rule that makes that sound is:

    the owner is ALWAYS derived from the authenticated peer (`owner_for_peer`),
    NEVER read from a request body or a query parameter.

Every INSERT in `repository.py` binds `owner_id`/`caller_peer_id` from the token-derived
identity, satisfying
house rule 5 at the application layer because the database offers no constraint to
satisfy it at. A future migration that turns RLS on for these tables would tighten this,
not change it.

Connections are autocommit and `SET ROLE meshychat_app` for the reasons in `apps/core/rls.py`:
the owner role (`meshychat_owner`) has `rolbypassrls`, and a non-autocommit connection turns
`SET LOCAL` into a savepoint-scoped setting that outlives its block.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

import psycopg

from apps.core import dbconn


@contextlib.contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """An autocommit `meshychat_app` connection, opened the way a request must be served.

    `dbconn.connect` is used so this app
    inherits the single DSN resolution (settings.MESHY_DSN -> MESHY_DSN -> DATABASE_URL
    -> dev default) and the `SET ROLE` that follows it.

    `dbconn.assert_serving_connection()` is deliberately NOT called here. It probes
    `shared_memory` for FORCE-RLS, which is the right control for a connection that will
    read an RLS-protected table; none of the tables this app touches is one, so the probe
    would assert a property this surface does not depend on and would spend a round trip
    per request doing it. The property this surface DOES depend on - autocommit, and a
    role that is not the RLS-bypassing bootstrap user - is what `dbconn.connect()`
    establishes, and `assert_meshyapp_autocommit()` below is the positive control the
    tests run against a real connection.
    """
    conn = dbconn.connect()
    try:
        yield conn
    finally:
        conn.close()


def assert_meshyapp_autocommit(conn) -> None:
    """Positive control: raise unless `conn` is the kind of connection above.

    Exists so a test can prove the guarantee rather than trust the constructor, and so
    the failure names its cause. Checks both halves, because either one alone reads as
    healthy: a bypassing role sees every tenant's rows silently, and a non-autocommit
    connection leaks transaction-local settings onto the next request.
    """
    if not conn.autocommit:
        raise dbconn.NonAutocommitError(
            "meshguard refuses a non-autocommit connection: SET LOCAL would bind to a "
            "savepoint inside psycopg's implicit transaction (apps/core/rls.py)."
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_user, "
            "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
        )
        role, bypasses = cur.fetchone()
    if bypasses:
        raise dbconn.RlsBypassError(
            f"meshguard connection runs as {role!r}, which BYPASSES row-level security. "
            "Call apps.core.rls.set_role() first."
        )
