"""Database access for the auth routes.

None of the five tables these routes touch (auth_challenges, peers, peer_keys,
refresh_tokens, dashboard_users) carries a row-level-security policy — measured, not
assumed:

    SELECT relname FROM pg_class WHERE relrowsecurity  -- 12 tables, none of the above

So there is no peer/owner context to bind here, and binding one would be misleading.
What DOES still hold is `apps/core/rls.py`'s other rule: the connection runs as
`meshychat_app` and is AUTOCOMMIT, so any `SET LOCAL` a later caller adds binds to a real
transaction rather than to a savepoint inside an implicit one. Sign-in and refresh are
therefore written with EXPLICIT transactions where they need atomicity, and everything
else runs autocommit.

Connection-per-request is deliberate: it cannot leak session state between requests. A pool
belongs here once the whole surface exists,
and it must re-run `SET ROLE` in its after-connect hook.
"""
from __future__ import annotations

import contextlib

from apps.authapi.config import get_config
from apps.core import dbconn


@contextlib.contextmanager
def connection(dsn: str | None = None):
    """An autocommit connection running as `meshychat_app`."""
    # dbconn.connect() rather than psycopg.connect(): it does the SET ROLE and then
    # VERIFIES it (assert_not_bypassing). A bare connect as the bootstrap role served
    # every request and passed the whole suite on two other surfaces - the role check is
    # the only thing that notices, because set_role is an instruction nobody observed.
    conn = dbconn.connect(dsn or get_config().dsn)
    try:
        yield conn
    finally:
        conn.close()
