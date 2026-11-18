"""Divergence 041 S9, the review of a6bb87af, findings F4 and F5: migration 021's rollback and
its lock wait.

F5. 021 shipped no down script and no deploy-order note, and step 9 needs it even with
doorbells off (`apps/calls/route._route_remote` calls `webhooks.insert_due` on every queued
edge). The runner had no down step then (apps/core/sqlmigrate.py; since the rollback gate
`--rollback-to` runs these scripts), so, as A1 did for 014
(tests/test_signed_bytes_down.py), 021 ships a named script OUTSIDE the migrate set,
sql_meshychat/down/021_invocation_webhook_doorbell_down.sql, and this file applies it to a
populated database: after it the three tables, both trigger functions and the ledger row are
gone, the declaration reports 021 pending, and 021 applies again.

F4. 021's foreign keys take SHARE ROW EXCLUSIVE on `skill_calls` and `peers`, and the file set
no lock_timeout: queued behind one open writer of `skill_calls`, the migration held every later
writer of the table behind it for as long as that writer ran (the reviewer measured it). 021
and its down script now give up after 5 s, as 014 does (SET LOCAL, so the timeout ends with
the file's own transaction). This track's two earlier migrations, 016 and 018, had the same
unbounded wait on `peers` and on `skill_calls`/`skill_call_chunks`, and get the same line.

It builds and drops a database of its own: the down script changes the schema, and the run's
shared database must keep 021 for every other test.

Run:  .venv/Scripts/python.exe -m pytest tests/test_invoke_webhook_down.py -q
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import schema_sets  # noqa: E402
from tests.fixtures.call_rows import seed_call  # noqa: E402
from tests.test_invoke_webhook import make_world  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
MIGRATIONS = ROOT / "sql_meshychat"
DOWN = MIGRATIONS / "down" / "021_invocation_webhook_doorbell_down.sql"
TABLES = ("peer_invocation_webhooks", "peer_webhook_attempts", "peer_webhook_verifications")
FUNCTIONS = ("meshy_webhook_verify_notify", "meshy_webhook_attempt_notify")
#: Every migration this track (divergence 041) adds.
TRACK = ("016_hub_connections_presence.sql", "018_skill_call_notify_triggers.sql",
         "021_invocation_webhook_doorbell.sql")


def _admin(dsn: str) -> str:
    return dsn.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def down_dsn():
    name = f"meshy_down_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(_admin(DSN), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(dsn) as conn:
            schema_sets.apply_all(conn, verbose=False)
            schema_sets.assert_schema_complete(conn)
        yield dsn
    finally:
        with psycopg.connect(_admin(DSN), autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _apply_down(owner) -> None:
    with owner.transaction():
        owner.execute(DOWN.read_text("utf-8"))


def _reapply(owner) -> None:
    """Whatever an earlier test in this module left, end at the full schema."""
    schema_sets.apply_all(owner, verbose=False)
    schema_sets.assert_schema_complete(owner)
    owner.commit()


def _present(owner) -> dict:
    tables = {t: owner.execute("SELECT to_regclass(%s) IS NOT NULL", (t,)).fetchone()[0]
              for t in TABLES}
    functions = owner.execute("SELECT count(*) FROM pg_proc WHERE proname = ANY(%s)",
                              (list(FUNCTIONS),)).fetchone()[0]
    ledger = owner.execute("SELECT count(*) FROM meshychat_migrations WHERE version = 21"
                           ).fetchone()[0]
    owner.commit()
    return {"tables": tables, "functions": functions, "ledger": ledger}


def test_the_021_down_script_removes_the_doorbell_schema_and_021_applies_again(down_dsn):
    """F5: on a database holding a registration, a doorbell attempt and a verification
    record, the down script (one transaction, the owner role) leaves none of 021's tables,
    functions or ledger row; the declaration says 021 is pending; 021 applies again, with
    RLS forced on every table. CONTROL: before it, all of them are there."""
    owner = psycopg.connect(down_dsn, autocommit=True)
    try:
        world = make_world(owner)
        peer = world["target"]["peer_id"]
        call_id = seed_call(owner, world["caller"], world["target"], world["skill_id"],
                            status="queued", call_id=f"s9-down-{uuid.uuid4()}")
        owner.execute("INSERT INTO peer_invocation_webhooks (peer_id, url, challenge) "
                      "VALUES (%s, 'https://hook.example.test/x', %s)", (peer, "D" * 43))
        owner.execute("INSERT INTO peer_webhook_attempts (peer_id, call_id) VALUES (%s,%s)",
                      (peer, call_id))
        owner.execute("INSERT INTO peer_webhook_verifications (peer_id, url, claimed_by, "
                      "claimed_at) VALUES (%s, 'https://hook.example.test/x', 'down', now())",
                      (peer,))
    finally:
        owner.close()

    owner = psycopg.connect(down_dsn)
    try:
        assert _present(owner) == {"tables": dict.fromkeys(TABLES, True), "functions": 2,
                                   "ledger": 1}, "control"
        _apply_down(owner)
        assert _present(owner) == {"tables": dict.fromkeys(TABLES, False), "functions": 0,
                                   "ledger": 0}
        pending = schema_sets.pending(owner)
        owner.commit()
        assert (pending["base"], pending["meshychat"]) == ([], [21]), pending
        assert owner.execute("SELECT count(*) FROM skill_calls WHERE call_id = %s",
                             (call_id,)).fetchone()[0] == 1, "the calls are untouched"
        owner.commit()

        _reapply(owner)
        assert _present(owner) == {"tables": dict.fromkeys(TABLES, True), "functions": 2,
                                   "ledger": 1}
        for table in TABLES:
            rls = owner.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = %s AND relnamespace = 'public'::regnamespace",
                (table,)).fetchone()
            count = owner.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            owner.commit()
            assert (rls, count) == ((True, True), 0), table
    finally:
        owner.close()


def test_021_and_its_down_script_give_up_on_a_lock_instead_of_queueing(down_dsn):
    """F4: behind one open writer of `skill_calls` (ROW EXCLUSIVE, what any INSERT or UPDATE
    holds until its commit), 021's foreign key and the down script's DROP wait for a lock,
    and every later writer of the table queues behind that wait. Both now give up with
    LockNotAvailable well inside 15 s, land nothing, apply once the writer finishes, and
    leave the session's lock_timeout as it was."""
    owner = psycopg.connect(down_dsn)
    writer = psycopg.connect(down_dsn)
    try:
        _reapply(owner)
        owner.execute("SET statement_timeout = '25s'")   # bounds the RED run
        owner.commit()
        default = writer.execute("SHOW lock_timeout").fetchone()[0]
        writer.commit()

        for step in ("down", "021"):
            writer.execute("LOCK TABLE skill_calls IN ROW EXCLUSIVE MODE")
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                if step == "down":
                    _apply_down(owner)
                else:
                    schema_sets.apply_all(owner, verbose=False)
            waited = time.monotonic() - started
            assert waited < 15, (step, waited)
            owner.rollback()
            state = _present(owner)
            assert state["tables"] == dict.fromkeys(TABLES, step == "down"), (step, state)
            writer.rollback()                     # the writer commits and goes
            if step == "down":
                _apply_down(owner)
            else:
                _reapply(owner)
            assert owner.execute("SHOW lock_timeout").fetchone()[0] == default, (
                "the timeout outlived the migration's transaction")
            owner.commit()
    finally:
        writer.close()
        owner.close()
        with psycopg.connect(down_dsn) as c:
            _reapply(c)


def _first_statement(sql: str) -> str:
    """The first SQL statement of a file, comments dropped."""
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return " ".join(code.split(";", 1)[0].split())


BOUNDED = re.compile(r"SET LOCAL lock_timeout = '5s'")


def test_every_migration_this_track_adds_bounds_its_lock_wait():
    """F4, and the same shape the review named in 018 (and 016 has, on `peers`): the first
    statement of each of this track's migrations and of 021's down script is `SET LOCAL
    lock_timeout = '5s'`, before any DDL takes a lock. CONTROLS: 014 and its down script
    (the precedent) pass the same check; a file whose first statement is DDL fails it."""
    for path in [MIGRATIONS / name for name in TRACK] + [DOWN]:
        first = _first_statement(path.read_text("utf-8"))
        assert BOUNDED.fullmatch(first), (path.name, first)
    for path in (MIGRATIONS / "014_shared_memory_signed_bytes.sql",
                 MIGRATIONS / "down" / "014_shared_memory_signed_bytes_down.sql"):
        assert BOUNDED.fullmatch(_first_statement(path.read_text("utf-8"))), path.name
    assert not BOUNDED.fullmatch(_first_statement(
        "-- SET LOCAL lock_timeout = '5s';\nCREATE TABLE t (a int);\nSET LOCAL lock_timeout "
        "= '5s';"))
