"""The manifest half of the backup (apps/core/backup_manifest.py) held to what it says (E02).

These need the dev Postgres but not pg_dump or pg_restore, so they run on every box, unlike
tests/test_backup_restore.py.

  * `compare` reports a difference in BOTH directions (a table only in the backup, a table
    only in the database) and a ledger that differs (a version only on one side, a checksum
    that differs), each beside a control of two equal descriptions;
  * `table_counts` reads with row security OFF, so a role that row-level security would
    filter gets an ERROR, never a smaller number. The suite runs as a superuser, which RLS
    never filters, so this is shown with a role made for the test that RLS does filter;
    beside it, the control that this role really is filtered with row security on, and that
    the owner's counts are the true ones.

Run:  .venv/Scripts/python.exe -m pytest tests/test_backup_manifest.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import backup_manifest as bm, schema_sets  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


def _d(tables: dict, ledgers: dict) -> dict:
    return {"tables": tables, "ledgers": ledgers}


BASE = _d({"public.a": 1, "public.b": 2}, {"s": {"1": "aa", "2": "bb"}, "t": {}})


def test_equal_descriptions_compare_equal():
    assert bm.compare(BASE, _d(dict(BASE["tables"]), {k: dict(v) for k, v in
                                                      BASE["ledgers"].items()})) == []


def test_a_table_only_in_the_database_is_reported():
    observed = _d({**BASE["tables"], "public.c": 0}, BASE["ledgers"])
    assert bm.compare(BASE, observed) == ["public.c: in the database, not in the backup"]


def test_a_table_only_in_the_backup_is_reported():
    observed = _d({"public.a": 1}, BASE["ledgers"])
    assert bm.compare(BASE, observed) == ["public.b: in the backup, missing from the database"]


def test_a_count_that_differs_is_reported():
    observed = _d({"public.a": 1, "public.b": 3}, BASE["ledgers"])
    assert bm.compare(BASE, observed) == ["public.b: backup has 2 rows, database has 3"]


@pytest.mark.parametrize("ledgers,expected", [
    ({"s": {"1": "aa"}, "t": {}}, "ledger s: backup-only ['2'], database-only [], "
                                  "checksum differs []"),
    ({"s": {"1": "aa", "2": "bb", "3": "cc"}, "t": {}},
     "ledger s: backup-only [], database-only ['3'], checksum differs []"),
    ({"s": {"1": "aa", "2": "XX"}, "t": {}},
     "ledger s: backup-only [], database-only [], checksum differs ['2']"),
    ({"s": {"1": "aa", "2": "bb"}}, "ledger t: backup-only [], database-only [], "
                                    "checksum differs []"),
], ids=["version-only-in-backup", "version-only-in-database", "checksum", "set-absent"])
def test_a_ledger_that_differs_is_reported(ledgers, expected):
    assert bm.compare(BASE, _d(BASE["tables"], ledgers)) == [expected]


# ---------------------------------------------------------------- row security

def _admin() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


def _dsn_for(name: str) -> str:
    return DSN.rsplit("/", 1)[0] + "/" + name


@pytest.fixture()
def migrated_with_a_peer():
    name = f"meshy_bkm_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    role = f"meshy_bkm_role_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
        c.execute(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS')
    try:
        dsn = _dsn_for(name)
        with psycopg.connect(dsn) as conn:
            schema_sets.apply_all(conn, verbose=False)
            owner, peer = uuid.uuid4(), uuid.uuid4()
            conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                         "VALUES (%s,%s,'dev',%s)",
                         (owner, f"bkm-{owner.hex[:8]}@example.test", uuid.uuid4()))
            conn.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                         "VALUES (%s,%s,%s,%s)", (peer, owner, "bkm", "bkm"))
            conn.execute("INSERT INTO dashboard_log_settings (owner_id) VALUES (%s)", (owner,))
            conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
            conn.commit()
        yield dsn, role
    finally:
        with psycopg.connect(_admin(), autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')
            c.execute(f'DROP ROLE IF EXISTS "{role}"')


T = "dashboard_log_settings"     # row-level security, visible only to server-internal reads


def test_counts_refuse_rather_than_undercount_under_row_security(migrated_with_a_peer):
    dsn, role = migrated_with_a_peer
    with psycopg.connect(dsn) as conn:
        assert conn.execute(f"SELECT relrowsecurity FROM pg_class WHERE oid = '{T}'::regclass"
                            ).fetchone()[0], f"control: {T} is under row-level security"
        truth = bm.table_counts(conn)
        assert truth[f"public.{T}"] == 1, "control: the owner's count is the true one"
        conn.rollback()

        conn.execute(f'SET ROLE "{role}"')
        conn.execute("SET LOCAL row_security = on")
        seen = conn.execute(f"SELECT count(*) FROM {T}").fetchone()[0]
        assert seen < truth[f"public.{T}"], (
            "control: with row security on, this role is filtered (so a count read as it "
            "would silently be smaller)")
        conn.rollback()

        conn.execute(f'SET ROLE "{role}"')
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
            counts = bm.table_counts(conn)
            pytest.fail(f"a role that row security filters counted {counts.get('public.' + T)} "
                        f"rows of {T} of {truth['public.' + T]} instead of refusing")
        assert "row-level security" in str(refused.value)
