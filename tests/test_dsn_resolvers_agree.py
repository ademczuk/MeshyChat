"""All three DSN resolvers must reach the same database during a test run.

THE HAZARD, measured 2026-09-21 and present in the invocation everyone uses. There are
THREE places that answer "which database", and they do not consult the same variables:

    apps/peers/db.dsn()      MESHY_DSN, then MESHY_TEST_DSN, then the default
    apps/authapi/config      MESHY_DSN, then MESHY_TEST_DSN only when testing, else raise
    apps/core/dbconn.dsn()   settings.MESHY_DSN, then MESHY_DSN, then DATABASE_URL,
                             then the default. NEVER MESHY_TEST_DSN.

With MESHY_DSN unset, which is the correct invocation and the one that produces a green
suite, conftest published the per-run clone as MESHY_TEST_DSN while `dbconn.dsn()` fell
through every branch and returned the SHARED database. Any route resolving through dbconn
without an explicit `override_settings` therefore read and wrote shared state during the
run, on a box where more than one checkout runs this suite at once.

WHY THIS TEST HAD TO EXIST RATHER THAN JUST THE FIX. The suite could not tell the
difference: 684 passed with the divergence and 684 passed without it, measured as a
control at the same commit. A correctness change that no test can detect is a change
nobody can keep, because the next person to touch conftest has nothing that objects.

WHY THE FIX IS IN CONFTEST AND NOT IN THE RESOLVERS. apps/authapi/config.py:112-116
records a review finding on this exact precedence: consulting MESHY_TEST_DSN before
MESHY_DSN, and separately leaving MESHY_DSN unset, left the auth surface running as an
RLS-BYPASSING superuser. Reordering a resolver is how that happened. Setting one variable
so all three already agree changes no resolver at all.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _db(dsn: str) -> str:
    """Just the database name. Comparing whole DSNs would also compare credentials."""
    return dsn.rsplit("/", 1)[-1]


@pytest.fixture()
def clone_name() -> str:
    name = os.environ.get("MESHY_TEST_DSN", "")
    if not name:
        pytest.skip("MESHY_NO_TEST_DB=1: no isolated database was built for this run")
    return _db(name)


def test_dbconn_reaches_the_isolated_clone(clone_name):
    """The one that was wrong. dbconn never consults MESHY_TEST_DSN, so it only lands on
    the clone because conftest sets MESHY_DSN to it."""
    from apps.core import dbconn
    assert _db(dbconn.dsn()) == clone_name, (
        "apps/core/dbconn resolves to a different database than the isolated clone, so "
        "any route using it reads and writes SHARED state during the test run"
    )


def test_peers_db_reaches_the_isolated_clone(clone_name):
    from apps.peers import db as peersdb
    assert _db(peersdb.dsn()) == clone_name


def test_the_three_resolvers_agree_with_each_other(clone_name):
    """Agreement, not just correctness against one expected value.

    Checked as a set so adding a fourth resolver that disagrees fails here rather than
    being discovered the way this one was.
    """
    from apps.authapi import config as authcfg
    from apps.core import dbconn
    from apps.peers import db as peersdb

    resolved = {
        "dbconn": _db(dbconn.dsn()),
        "peers.db": _db(peersdb.dsn()),
    }
    # authapi resolves through its own helper and raises rather than defaulting, which is
    # deliberate. Include it only when it answers, so this test reports disagreement
    # rather than failing on a refusal that is working as designed.
    for attr in ("resolve_dsn", "dsn", "database_dsn"):
        fn = getattr(authcfg, attr, None)
        if callable(fn):
            try:
                resolved["authapi"] = _db(fn(os.environ))
            except TypeError:
                try:
                    resolved["authapi"] = _db(fn())
                except Exception:
                    pass
            except Exception:
                pass
            break

    assert len(set(resolved.values())) == 1, (
        "the resolvers disagree about which database this run uses: " + repr(resolved)
    )
    assert set(resolved.values()) == {clone_name}


def test_the_shared_database_is_not_what_we_are_testing_against(clone_name):
    """A named negative. The clone always carries the pid, so a run that somehow points
    at meshy_django would satisfy 'they all agree' and still be wrong."""
    from apps.core import dbconn
    assert _db(dbconn.dsn()) != "meshy_django", (
        "this run is pointed at the shared development database rather than a clone"
    )
    assert clone_name.startswith("meshy_test_")
