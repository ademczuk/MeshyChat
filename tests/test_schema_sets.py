"""The database is built from more than one migration set, and nothing used to know that.

THE REAL FAILURE THESE TESTS ARE PROVED AGAINST, measured 2026-09-21 on a fresh database
built by following README.md exactly: `sqlmigrate.py --dsn ...` applied `sql/`, printed
"OK: 104 applied", and left `sql_meshychat/001_write_side_ownership.sql` unapplied. The
suite then failed 4 tests whose cause was nowhere near the code they exercise. Applying the
divergence set turned all four green with no code change.

So the negative case below is not invented. It is the state a correctly-followed README
produced, reproduced here by removing the divergence ledger rows inside a transaction that
is rolled back, so the shared dev database is never left damaged.

House rules this file is written to (docs/V1-DESIGN-RULES.md):
  C1  every negative assertion has a positive control in the SAME test
  C2  a gate that has never refused is unarmed, not passing
  C3  prove the test can fail by breaking the fixture, not by reading the code
  B3  absence is typed: `pending()` reports every set, including the empty ones

Run:  pytest tests/test_schema_sets.py -v
Needs MESHY_TEST_DSN (defaults to the dev stack database built by sqlmigrate.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.core import schema_sets, sqlmigrate  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

DIVERGENCE_SET = "meshychat"
BASE_SET = "base"


@pytest.fixture()
def conn():
    """A NON-autocommit connection, on purpose.

    Every negative below mutates a ledger and relies on the rollback to undo it. An
    autocommit connection would make each of those permanent, and the damage would be
    invisible until someone else's suite failed.
    """
    with psycopg.connect(DSN) as c:
        c.autocommit = False
        yield c
        c.rollback()


def _ledger_of(name: str) -> str:
    return next(s.ledger for s in schema_sets.SETS if s.name == name)


# --------------------------------------------------------------------------- declaration

def test_the_divergence_set_is_still_declared():
    """Guards the DECLARATION, which is the part that was missing rather than the checker.

    Deleting this entry would silently restore the 2026-09-21 behavior: every checker in
    this file would keep passing while checking one fewer set. That is the vacuous-scope
    failure, so it gets an explicit test rather than being assumed.
    """
    names = [s.name for s in schema_sets.SETS]
    assert BASE_SET in names, "positive control: the base set must be declared"
    assert DIVERGENCE_SET in names, (
        "the divergence set is no longer declared. Without it the write side of RLS is "
        "unenforced on a fresh database and nothing reports a problem."
    )
    div = next(s for s in schema_sets.SETS if s.name == DIVERGENCE_SET)
    assert div.sql_dir.name == "sql_meshychat"
    assert div.ledger != sqlmigrate.LEDGER, (
        "the divergence set must not share the base ledger: a divergence numbered 001 "
        "would collide with base 001."
    )


def test_every_declared_set_points_at_a_real_directory_with_migrations():
    for s in schema_sets.SETS:
        assert s.sql_dir.is_dir(), f"{s.name}: {s.sql_dir} is not a directory"
        # discover() raises on an empty directory, which is the loud failure we want.
        found = sqlmigrate.discover(s.sql_dir)
        assert found, f"{s.name}: no migrations discovered"


# ----------------------------------------------------------------- the healthy positive

def test_a_fully_migrated_database_passes(conn):
    """Positive control for every negative in this file.

    If this fails, the dev database is simply not migrated and the refusals below would
    pass for the wrong reason.
    """
    assert schema_sets.assert_schema_complete(conn) is None


def test_pending_reports_every_set_including_the_empty_ones(conn):
    """B3: absence is typed. A set with nothing pending appears with an empty list.

    An implementation that returned only the non-empty sets would be indistinguishable
    from one whose set list failed to load.
    """
    pend = schema_sets.pending(conn)
    assert set(pend) == {s.name for s in schema_sets.SETS}
    assert all(v == [] for v in pend.values()), f"dev database is not fully migrated: {pend}"


# ------------------------------------------------------------------------- the refusals

def test_it_refuses_when_the_divergence_set_is_unapplied(conn):
    """THE REAL FAILURE. Reproduces the state a correctly-followed README produced.

    Rolled back by the fixture, so the shared database keeps its rows.
    """
    ledger = _ledger_of(DIVERGENCE_SET)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {ledger}")
        before = cur.fetchone()[0]
    assert before > 0, "positive control: the divergence set must be applied to start with"

    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {ledger}")

    with pytest.raises(schema_sets.SchemaIncomplete) as exc:
        schema_sets.assert_schema_complete(conn)

    message = str(exc.value)
    assert DIVERGENCE_SET in message, "the refusal must name the set that is missing"
    assert "row-level security" in message, (
        "the refusal must state the CONSEQUENCE. A reader who only learns that a file is "
        "unapplied has to go and find out whether it mattered."
    )
    # And the base set is untouched, so the refusal is specific rather than blanket.
    assert schema_sets.pending(conn)[BASE_SET] == []


def test_set_name_alias_resolves_to_base_and_warns(capsys):
    base = schema_sets.resolve_set_name(BASE_SET)
    assert schema_sets.resolve_set_name("upstream") is base
    assert capsys.readouterr().err == (
        'DEPRECATED: set name "upstream" is now "base"; the old name still works and will '
        "be removed in a later release\n"
    )


def test_unknown_set_name_names_the_valid_sets():
    with pytest.raises(ValueError, match="unknown migration set 'missing'") as exc:
        schema_sets.resolve_set_name("missing")
    assert "['base', 'meshychat']" in str(exc.value)


def test_it_refuses_on_checksum_drift(conn):
    """A drifted checksum means the files on disk are not the files this database used.

    Distinct from "unapplied" and must not be reported as a count of pending versions.
    """
    ledger = _ledger_of(DIVERGENCE_SET)
    assert schema_sets.pending(conn)[DIVERGENCE_SET] == []  # positive control

    with conn.cursor() as cur:
        cur.execute(f"UPDATE {ledger} SET checksum = %s", (b"\x00" * 48,))

    with pytest.raises(schema_sets.SchemaIncomplete) as exc:
        schema_sets.pending(conn)
    assert "drift" in str(exc.value).lower()


def test_refusal_is_a_distinct_type_not_a_bare_runtime_error(conn):
    """E1: a caller must be able to tell a schema hole from a connection failure."""
    assert issubclass(schema_sets.SchemaIncomplete, RuntimeError)
    assert schema_sets.SchemaIncomplete is not RuntimeError


# ----------------------------------------------------------------------------- the CLI

def test_default_check_covers_every_set(capsys):
    """The DOCUMENTED command, with no --sql-dir and no --ledger, must see every set.

    This is the regression that matters: the old default saw `sql/` only and returned 0
    while the divergence set was unapplied.
    """
    rc = sqlmigrate.main(["--dsn", DSN, "--check"])
    out = capsys.readouterr().out
    assert rc == 0, f"dev database is not fully migrated:\n{out}"
    for s in schema_sets.SETS:
        assert s.name in out, f"--check did not report the {s.name!r} set:\n{out}"


def test_explicit_sql_dir_still_selects_a_single_set(capsys):
    """Backward compatibility: naming a directory keeps the old single-set behavior."""
    rc = sqlmigrate.main(["--dsn", DSN, "--check", "--sql-dir", str(schema_sets.ROOT / "sql")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "on disk" in out, f"single-set mode should use the original report shape:\n{out}"
    assert DIVERGENCE_SET not in out


def test_a_backup_manifest_taken_under_the_old_set_name_loads_under_the_new_one(tmp_path):
    """A manifest written before the first set was renamed keys its ledgers by "upstream".

    The rollback gate compares `manifest["ledgers"]` with the live ledgers and a restore
    compares them again, both keyed by set name. Without the alias in `backup_manifest.load`
    the production backup named by deploy/hqd/prod/step6_migrate.ps1 would be refused as
    "not taken at this database's current schema", and a restore of it would report
    unverified. The old key must come out as "base", and the new key must pass through.
    """
    import hashlib
    import json

    from apps.core import backup_manifest as bm

    dump = tmp_path / "d.dump"
    dump.write_bytes(b"dump bytes")
    for key, expected in (("upstream", "base"), ("base", "base"), ("meshychat", "meshychat")):
        manifest = {
            "format": bm.FORMAT, "dump_file": dump.name, "dump_bytes": dump.stat().st_size,
            "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(), "tables": {},
            "ledgers": {key: {"1": "ab"}}, "database": "meshy_django",
            "created_at": "2026-10-23T00:00:00+00:00",
        }
        path = tmp_path / f"{key}.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        assert list(bm.load(path)["ledgers"]) == [expected], key


def test_a_backup_manifest_that_lists_one_set_under_both_names_is_refused(tmp_path):
    """"upstream" and "base" are one set. Keeping whichever key comes last would silently
    drop the other ledger from what the rollback gate and a restore compare."""
    import hashlib
    import json

    from apps.core import backup_manifest as bm

    dump = tmp_path / "d.dump"
    dump.write_bytes(b"dump bytes")
    manifest = {
        "format": bm.FORMAT, "dump_file": dump.name, "dump_bytes": dump.stat().st_size,
        "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(), "tables": {},
        "ledgers": {"upstream": {"1": "aa"}, "base": {"1": "bb"}}, "database": "meshy_django",
        "created_at": "2026-10-23T00:00:00+00:00",
    }
    path = tmp_path / "both.manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(bm.BackupInvalid, match="two ledgers name the set 'base'"):
        bm.load(path)
