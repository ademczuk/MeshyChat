"""Does the suite ACTUALLY run against the isolated database?

Imported at MODULE level on purpose. An earlier version of this check imported the target
inside the test function, so it ran after the fixture and passed while every real test
module was still using the shared database. A probe must share the ordering of the thing
it is checking.
"""
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_rls_isolation import DSN as RLS_MODULE_DSN   # noqa: E402  module-level


def test_test_modules_use_the_isolated_database():
    if os.environ.get("MESHY_NO_TEST_DB") == "1":
        return
    with psycopg.connect(RLS_MODULE_DSN) as conn, conn.cursor() as cur:
        cur.execute("select current_database()")
        used = cur.fetchone()[0]
    assert used.startswith("meshy_test_"), (
        f"a test module resolved its DSN to {used!r}, not the isolated database. The "
        "provisioning must happen at conftest IMPORT time; a session fixture runs after "
        "test modules are imported and is therefore too late.")


def test_the_isolated_database_has_the_full_schema():
    """POSITIVE CONTROL: an empty database would also 'not be the shared one'."""
    if os.environ.get("MESHY_NO_TEST_DB") == "1":
        return
    with psycopg.connect(RLS_MODULE_DSN) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from _sqlx_migrations where success")
        applied = cur.fetchone()[0]
        cur.execute("select count(*) from pg_policies where schemaname='public'")
        policies = cur.fetchone()[0]
    assert applied == 104, f"{applied} migrations applied, expected 104"
    assert policies >= 26, f"{policies} RLS policies, expected at least 26"
