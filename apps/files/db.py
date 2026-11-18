"""The connection a file request is served on.

Thin over `apps.core.dbconn`, which owns the policy: autocommit (so `SET LOCAL` binds to
a real transaction rather than a savepoint) and `SET ROLE meshychat_app` (so row-level
security applies - the owner role (`meshychat_owner`) has `rolbypassrls` and would read every
tenant's rows while raising nothing).

`serving_connection` runs `assert_serving_connection` before handing the connection over,
so the refusal happens once, on the request path, rather than being re-derived in every
view. Indirected through a module-level name so a test can record the SQL a request
actually emits without production code carrying a tracing hook.
"""
from __future__ import annotations

import contextlib

from apps.core import dbconn


@contextlib.contextmanager
def connection():
    with dbconn.serving_connection() as conn:
        yield conn
