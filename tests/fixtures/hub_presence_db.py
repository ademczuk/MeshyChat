"""`hub_connections` hygiene for the presence tests (divergence 041 S2 review, I4).

Design section 6: "Autouse fixtures clear MANAGER, the new CallRegistry and hub_connections
after each test". A module that writes presence rows imports `hub_connections_clean`; being
autouse, it then runs around every test in that module and nowhere else.

It checks at SETUP that the table is empty and clears it at TEARDOWN. The setup check is
what makes the teardown observable: a test module whose teardown did not clear leaves rows,
and the NEXT test that uses this fixture errors at setup naming them, instead of every
later presence read quietly sharing the table with an earlier test's rows.

The DSN is read when the fixture runs, not at import: conftest publishes MESHY_TEST_DSN
for the session's own database, which is the only database this clears.
"""
from __future__ import annotations

import os

import psycopg
import pytest

_DEFAULT_DSN = "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django"


def _dsn() -> str:
    return os.environ.get("MESHY_TEST_DSN", _DEFAULT_DSN)


@pytest.fixture(autouse=True)
def hub_connections_clean():
    """Refuse to start on presence rows an earlier test left; clear this test's after it.

    The bootstrap role does both: it bypasses RLS, so it sees and removes every row.
    """
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT connection_id, peer_id FROM hub_connections ORDER BY connection_id")
        left = cur.fetchall()
    assert left == [], (
        f"{len(left)} hub_connections row(s) left by an earlier test, which did not clear "
        f"the table after itself (design section 6): {left[:3]}")
    yield
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM hub_connections")
        conn.commit()
