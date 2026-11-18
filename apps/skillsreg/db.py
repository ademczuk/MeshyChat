"""Database access policy for the skills registry.

MEASURED ON THE LIVE SCHEMA (meshy_django, 2026-09-21), not assumed:

    SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='skills'
      -> (False, False)
    SELECT policyname FROM pg_policies WHERE tablename='skills'
      -> 0 rows

`skills` HAS NO ROW-LEVEL SECURITY. Neither does `skill_grants`. That is a fact about
this schema, and it decides how every write in this app must be written:

  * `sql_meshychat/001_write_side_ownership.sql` closes the write side for
    `local_memory`, `skill_calls` and `meshy_memory` by requiring the inserted row to
    be attributed to `app.current_peer_id`. `skills` is NOT in that file, because there
    is no INSERT policy on `skills` to tighten.
  * So there is no database-level backstop here. The attribution is the ROUTE's:
    `peer_id` comes from the verified JWT and is NEVER read from the request body, and
    every UPDATE/DELETE carries `WHERE peer_id = <the authenticated peer>`. That is the
    whole of the authorization for a write, which is why it is stated here rather than
    left implicit in `repository.py`.
  * `tests/test_skills_registry.py::test_skills_has_no_row_level_security_so_the_route_is_the_only_guard`
    asserts that measurement in the suite. If someone later enables RLS on `skills`,
    that test FAILS and says so - at which point these writes must start binding
    `apps.core.rls.peer_context` or they will silently write zero rows.

Two rules are inherited from `apps/core/rls.py` rather than restated by hand:

  1. every connection does `SET ROLE meshychat_app`, because the owner role (`meshychat_owner`)
     BYPASSES row-level security and the failure mode is silent. `meshychat_app` holds
     SELECT/INSERT/UPDATE/DELETE on `skills` (checked via
     `information_schema.role_table_grants`), so `SET ROLE` does not lock this app out.
  2. every connection is AUTOCOMMIT, because `SET LOCAL` binds to the innermost
     ENCLOSING transaction: on a non-autocommit connection psycopg has already opened
     one, so `conn.transaction()` degrades to a SAVEPOINT.

Rule 2 matters to this app for a second reason. `release_version` MUST run its
flip-previous-current + insert inside ONE real transaction: a
separately-committed flip leaves the skill with no `is_current = TRUE` row, it vanishes
from list/resolve/search and every later release returns NotFound - a wedge needing
manual DB repair. With autocommit, the `conn.transaction` context opens a genuine
transaction and the rollback is real; on a non-autocommit one it would be a savepoint
inside a transaction nobody commits.
"""
from __future__ import annotations

import contextlib

from apps.core import dbconn

DEFAULT_DSN = "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django"


def dsn() -> str:
    """Delegates to `apps.core.dbconn.dsn()` - ONE resolution for the whole project.

    WHAT THIS MODULE USED TO DO, and why it was wrong:
        os.environ.get("MESHY_DSN") or os.environ.get("MESHY_TEST_DSN") or DEFAULT_DSN

    Three divergences from every other app, in a function short enough that none of them
    looked like a decision:
      * it ignored `DATABASE_URL`, which both `meshychat/settings.py` and
        `apps/core/dbconn.dsn()` honour. A deployment configured only that way had THIS
        app - alone - pointed at a hardcoded localhost dev database, silently.
      * it ignored `settings.MESHY_DSN`, so `override_settings` could not move it.
      * it read `MESHY_TEST_DSN`, which the application layer deliberately does NOT know
        about. That variable belongs to `tests/conftest.py`; test modules promote it to
        `MESHY_DSN` at import time, and `_refuse_a_hijacked_application_dsn` now refuses a
        run where the two disagree. An app reading it directly made this module pass in a
        configuration where the others would correctly have failed - a fallback that hides
        a misconfiguration is worse than the misconfiguration.

    `DEFAULT_DSN` is kept for the identical value dbconn falls back to, so the constant is
    not silently orphaned; the resolution itself is no longer duplicated here.
    """
    return dbconn.dsn()


@contextlib.contextmanager
def connection():
    """An autocommit `meshychat_app` connection, the way a request must be served."""
    # This module was the ONE of seven that skipped the guarded constructor entirely,
    # while the session user is `meshy` (rolsuper, rolbypassrls). Deleting its set_role
    # survived the whole mutation campaign. dbconn.connect() verifies the role.
    # dsn() is dbconn.dsn(), so DATABASE_URL reaches this module too (STATUS C59,
    # tests/test_connection_role_is_verified.py::test_database_url_reaches_every_app).
    conn = dbconn.connect(dsn())
    try:
        yield conn
    finally:
        conn.close()
