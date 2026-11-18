"""Database access for the peers app.

Two rules from `apps/core/rls.py` are carried here rather than restated by hand:

  1. every connection does `SET ROLE meshychat_app`, because the owner role BYPASSES
     row-level security and the failure mode is silent;
  2. every connection is AUTOCOMMIT, because `SET LOCAL` binds to the innermost
     ENCLOSING transaction. On a non-autocommit connection psycopg has already opened
     one, so `conn.transaction()` degrades to a SAVEPOINT and the setting outlives its
     block.

MEASURED, not assumed: none of the four tables this app writes (`mesh_presence`,
`peer_keys`, `agent_cards`, `key_publish_nonces`) has row-level security enabled -
`pg_class.relrowsecurity` is false for all four and `pg_policies` lists none. Their
authorisation is the ROUTE's (peer id comes from the token, never the body), which is
why every write below keys on an id the caller did not supply. `meshychat_app` holds
SELECT/INSERT/UPDATE/DELETE on all four (checked via
`information_schema.role_table_grants`), so `SET ROLE` does not lock this app out.
"""
from __future__ import annotations

import contextlib

from apps.core import dbconn


def dsn() -> str:
    """Delegates to `apps.core.dbconn.dsn()` - ONE resolution for the whole project.

    Each app used to carry its own two-line resolver, and they had QUIETLY DIVERGED. A
    resolver short enough to read at a glance is one nobody reads: `apps/skillsreg` and
    `apps/peers` ignored `DATABASE_URL` and `settings.MESHY_DSN`, and `apps/wsgw` did not
    read `MESHY_DSN` AT ALL - a deployment configured the documented way would have had the
    WebSocket gateway on a hardcoded localhost dev database while every route used the real
    one, with nothing reporting a problem.

    They also all read `MESHY_TEST_DSN`, which the application layer deliberately does not
    know about: that variable belongs to `tests/conftest.py`, and test modules promote it to
    `MESHY_DSN` at import time. An app reading it directly made these surfaces pass in a
    configuration where the others would correctly have failed.

    `tests/test_connection_role_is_verified.py` now asserts every app resolves to the same
    string, and that moving `DATABASE_URL` moves all of them together.
    """
    return dbconn.dsn()


@contextlib.contextmanager
def connection():
    """An autocommit `meshychat_app` connection, the way a request must be served."""
    # See apps/core/dbconn.assert_not_bypassing: SET ROLE is an instruction, and this
    # constructor is the one that checks it took effect.
    conn = dbconn.connect(dsn())
    try:
        yield conn
    finally:
        conn.close()
