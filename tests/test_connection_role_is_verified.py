"""`SET ROLE` is an instruction; this proves something observes whether it took effect.

THE GAP THIS CLOSES, found by adversarial review on TWO surfaces independently. Replacing
the guarded constructor with a bare `psycopg.connect` as the bootstrap `meshychat_owner` role
(`rolbypassrls = true`) served every request and passed the full suite - 28 of 28 on the
calls surface, 34 of 34 on a2a. Nothing failed, because a bypassing role does not error:
it silently returns every tenant's rows.

WHY THE EXISTING STRUCTURAL TEST CANNOT COVER THIS.
`tests/test_every_connection_is_role_bound.py` scans the AST and requires every module that
opens a connection to also call `rls.set_role`. That is a real control and it catches a
DELETED call. It cannot catch this, because the mutation does not delete the call - it opens
a different connection entirely, one the scan has no reason to look at. A test that reads
source and a test that runs a query are answering different questions, and only the second
one can tell you what the server thinks your role is.

So these tests exercise the RUNTIME property, and every one of them asks the database.

Run: .venv/Scripts/python.exe -m pytest tests/test_connection_role_is_verified.py -q
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from apps.core import dbconn, rls  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


def _role_facts(conn) -> tuple[str, bool]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_user, "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)")
        return cur.fetchone()


def test_the_bootstrap_role_really_does_bypass_rls():
    """THE INSTRUMENT CHECK, first, because everything below is meaningless without it.

    If the DSN's own role did NOT bypass RLS, every refusal in this file would pass for the
    wrong reason and the suite would report a guard that had never been exercised.
    """
    with psycopg.connect(DSN, autocommit=True) as conn:
        role, bypasses = _role_facts(conn)
    assert bypasses, (
        f"the test DSN connects as {role!r}, which does NOT bypass RLS. These tests cannot "
        "prove anything about a bypassing connection; fix the fixture, do not skip.")


def test_a_bypassing_connection_is_refused():
    """The measured mutation, as a test: serve on the bootstrap role and be refused."""
    with psycopg.connect(DSN, autocommit=True) as conn:          # no SET ROLE
        with pytest.raises(dbconn.RlsBypassError) as exc:
            dbconn.assert_not_bypassing(conn)
        assert "BYPASSES" in str(exc.value)

        # POSITIVE CONTROL ON THE SAME CONNECTION: after SET ROLE the identical call
        # passes. Without this, the refusal above could be a broken probe that refuses
        # everything, and the test would read the same.
        rls.set_role(conn)
        dbconn.assert_not_bypassing(conn)
        role, bypasses = _role_facts(conn)
        assert not bypasses and role == rls.APP_ROLE


def test_the_constructor_verifies_the_role_it_just_set():
    """`connect()` must REFUSE to hand back a bypassing connection, not merely ask for a
    role. This is the difference between an instruction and a guarantee.

    Asking for the bootstrap role explicitly is the cleanest way to prove the check runs:
    `SET ROLE` succeeds, so only a verification step can catch it.
    """
    with pytest.raises(dbconn.RlsBypassError):
        dbconn.connect(DSN, role="meshychat_owner")

    # CONTROL: the same call with the app role returns a usable connection.
    conn = dbconn.connect(DSN)
    try:
        role, bypasses = _role_facts(conn)
        assert role == rls.APP_ROLE and not bypasses
        assert conn.autocommit
    finally:
        conn.close()


def test_a_refused_connection_is_closed_and_not_leaked():
    """A constructor that raises must not leave the socket open. Otherwise the guard
    trades a silent data leak for a silent connection leak.

    The refusals' EXCEPTIONS ARE HELD until after the count. An exception's traceback keeps
    the frame of `dbconn.connect`, and with it the connection object, alive; so a connect that
    raised WITHOUT closing still has an open backend when the count is read. Until 2026-10-01
    the exceptions were dropped at once, CPython then collected the connection, which closes
    it, and this test passed with the `conn.close()` removed (mutant run, on main): it could
    only ever fail falsely."""
    held = []
    with psycopg.connect(DSN, autocommit=True) as probe, probe.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        before = cur.fetchone()[0]
        for _ in range(5):
            try:
                dbconn.connect(DSN, role="meshychat_owner")
            except dbconn.RlsBypassError as exc:
                held.append(exc)
        assert len(held) == 5, "control: every one of the five connects was refused"
        # A closed connection's BACKEND exits a moment after the client's close() returns, and until then
        # pg_stat_activity still lists it. Measured on HQD, 2026-10-01: the count read straight after the
        # fifth refusal saw one in 27 of 200 rounds, gone within 8 ms. Read at once, that is a "leak" in about
        # one run in eight (two whole gates were refused on it). A real leak never goes away while the
        # exceptions are held, so wait for the count to come back, up to 5 s, and fail only if it does not.
        deadline = time.monotonic() + 5
        while True:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
            after = cur.fetchone()[0]
            if after <= before or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    assert after <= before, f"backends grew {before} -> {after} and stayed for 5 s: refused connections leaked"
    del held


def test_the_opt_out_exists_and_is_explicit():
    """`verify_role=False` is how the migrator runs DDL as the owner. It must WORK - a
    guard with no documented way past it gets removed rather than parameterised - but it
    must also be the only way past, so it is spelled out at the call site."""
    conn = dbconn.connect(DSN, role="meshychat_owner", verify_role=False)
    try:
        role, bypasses = _role_facts(conn)
        assert role == "meshychat_owner" and bypasses, "the opt-out did not actually opt out"
    finally:
        conn.close()


@pytest.mark.parametrize("module_path,attr", [
    ("apps.authapi.db", "connection"),
    ("apps.peers.db", "connection"),
    ("apps.skillsreg.db", "connection"),
    ("apps.wsgw.db", "connect"),
    ("apps.files.db", "connection"),
    ("apps.grants.db", "connection"),
    ("apps.meshguard.db", "connection"),
    ("apps.memoryskills.db", "connect"),
])
def test_every_app_helper_hands_back_a_non_bypassing_connection(module_path, attr):
    """THE ONE THAT WOULD HAVE CAUGHT THE ORIGINAL MUTATIONS, per app, at runtime.

    Each helper is called for real and the connection it returns is interrogated. This does
    not read source, so it does not care HOW the helper opens the connection - only what
    comes back, which is the property the surviving mutations violated.
    """
    import contextlib
    import importlib

    mod = importlib.import_module(module_path)
    helper = getattr(mod, attr)
    opened = helper()
    ctx = opened if hasattr(opened, "__enter__") else contextlib.nullcontext(opened)
    with ctx as conn:
        role, bypasses = _role_facts(conn)
        assert not bypasses, f"{module_path}.{attr} returned a connection as {role!r}, " \
                             "which bypasses row-level security"
        assert conn.autocommit, f"{module_path}.{attr} returned a non-autocommit connection"
    if not hasattr(opened, "__enter__"):
        opened.close()


def test_every_app_resolves_its_dsn_through_the_one_function():
    """ONE resolution, so a deployment cannot configure six apps and miss the seventh.

    `apps/skillsreg/db.py` used to resolve its own: it ignored `DATABASE_URL` (which
    `settings.py` and `dbconn` both honour) and ignored `settings.MESHY_DSN`, so a
    deployment configured only by `DATABASE_URL` had that ONE app pointed at a hardcoded
    localhost dev database while every other app used the real one.

    Asserted by VALUE rather than by reading source, because what matters is where each
    app would actually connect - and `DATABASE_URL` is the case that was broken, so that is
    the one the control moves.
    """
    import importlib

    from apps.core import dbconn

    modules = ["apps.authapi.db", "apps.peers.db", "apps.skillsreg.db", "apps.wsgw.db",
               "apps.memoryskills.db"]
    resolvers = []
    for name in modules:
        mod = importlib.import_module(name)
        fn = getattr(mod, "dsn", None)
        if callable(fn):
            resolvers.append((name, fn))
    assert resolvers, "control: no module exposed a dsn() to compare"

    expected = dbconn.dsn()
    for name, fn in resolvers:
        assert fn() == expected, f"{name}.dsn() resolved {fn()!r}, dbconn said {expected!r}"


def test_database_url_reaches_every_app(monkeypatch):
    """The specific variable that was ignored. Moving it must move every app together.

    THIS TEST SKIPPED ITSELF ON THE FIRST ATTEMPT and I nearly shipped it that way. The
    resolution order is settings.MESHY_DSN -> MESHY_DSN -> DATABASE_URL, and the suite sets
    MESHY_DSN, so `settings.MESHY_DSN` is always configured and the environment arm was
    never reachable. It reported SKIPPED, which in a 688-test run is invisible - a test that
    never executes and a test that passes look identical in the summary line.

    So the two higher-priority sources are cleared here rather than skipped around, with
    `override_settings` for the Django layer and monkeypatch for the environment.
    """
    import importlib

    from django.test import override_settings

    from apps.core import dbconn

    monkeypatch.delenv("MESHY_DSN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.invalid:5432/moved")

    with override_settings(MESHY_DSN=None):
        # CONTROL FIRST: the shared resolver honours it. If this fails the rest is noise.
        assert "db.invalid" in dbconn.dsn(), "dbconn itself did not honour DATABASE_URL"

        checked = []
        for name in ["apps.skillsreg.db", "apps.peers.db", "apps.wsgw.db", "apps.authapi.db",
                     "apps.memoryskills.db"]:
            mod = importlib.import_module(name)
            fn = getattr(mod, "dsn", None)
            if not callable(fn):
                continue
            checked.append(name)
            assert "db.invalid" in fn(), f"{name}.dsn() ignored DATABASE_URL"
        assert len(checked) >= 3, f"control: only {checked} exposed a dsn() to check"

    # AND THE NEGATIVE HALF: with DATABASE_URL gone, nothing may still be pointing at it -
    # otherwise the assertions above could be passing on a cached module-level value.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with override_settings(MESHY_DSN=None):
        assert "db.invalid" not in dbconn.dsn()


def test_the_memory_skill_connection_is_the_configured_database_not_a_test_one(monkeypatch):
    """R3 (migration synthesis, 2026-09-23). `apps/memoryskills/db.py` resolved its
    database from `MESHY_TEST_DSN` with a hard-coded dev fallback, so anything that took a
    connection from it (the package docstring's documented usage, an in-process
    dispatcher, a shell) reached whatever that TEST variable named, while every other
    surface reached the configured one. Here the test variable names a database that does
    not exist: the module must neither resolve to it nor try to open it.

    Control, in the same test: dbconn itself resolves the configured clone, and the
    connection that comes back crosses the SAME probe a request's does
    (`assert_serving_connection`), not only `set_role`.
    """
    from apps.memoryskills import db as msdb

    monkeypatch.setenv("MESHY_TEST_DSN",
                       "postgresql://nobody:nothing@127.0.0.1:1/no_such_database")
    expected = dbconn.dsn()
    assert "no_such_database" not in expected, "control: dbconn ignores the test variable"
    assert msdb.dsn() == expected, (
        f"apps.memoryskills.db resolved {msdb.dsn()!r}; the project resolves {expected!r}")
    conn = msdb.connect()
    try:
        dbconn.assert_serving_connection(conn)
        role, bypasses = _role_facts(conn)
        assert role == rls.APP_ROLE and not bypasses
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            assert cur.fetchone()[0] == expected.rsplit("/", 1)[-1].split("?")[0]
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
