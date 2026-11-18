"""A deploy refuses to roll the schema forward when it could not roll it back (E03).

BACKGROUND (docs/ISSUE-RESOLUTION-MAP.md, "rollback leaves the DB migrated"; V1-DESIGN-RULES D):
a rollback restored the previous image and left the schema migrated, because there were no
down-migrations. The rule: the deploy must be able to roll the database back, or must refuse
to roll forward.

At 8caac46 `apps/core/sqlmigrate.py` applied forward only, and one down script existed
(sql_meshychat/down/014, run by hand or by tests/test_signed_bytes_down.py). Nothing refused a
pending migration that had no way back.

THE GATE (apps/core/rollback.py), on every apply path that iterates the declared sets
(`schema_sets.apply_all`, so `sqlmigrate.py` and the test provisioning) and on the single-set
CLI path. A pending migration may be applied when ANY of these holds:
  * the database is fresh (no ledger row in any set): the way back is to drop it;
  * every pending migration has a down script, `<set>/down/<same stem>_down.sql`;
  * the operator names a verified backup (`--rollback-backup MANIFEST`, tools/backup) taken
    of THIS database at its CURRENT schema, and not older than a day: the way back is the
    restore.
Otherwise it refuses, names the versions without a down script, and applies nothing.

`sqlmigrate.py --rollback-to SET:VERSION` runs the down scripts newest first, each in its own
transaction with its ledger row, and refuses the whole plan, before running any of it, when a
version in the range has no down script.

Every down script is also held to what it claims: applying the migration and then its down
script must give back the catalog the migration found (test_every_down_script_restores...),
except the objects its `-- KEEPS:` header names.

Run:  .venv/Scripts/python.exe -m pytest tests/test_rollback_gate.py -q
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import schema_sets, sqlmigrate  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
DIVERGENCE = "meshychat"

PROBE_998 = b"CREATE TABLE rollback_probe_998 (x int);\n"
PROBE_998_DOWN = b"DROP TABLE rollback_probe_998;\n"
PROBE_999 = b"CREATE TABLE rollback_probe_999 (x int);\n"
PROBE_999_DOWN = b"DROP TABLE rollback_probe_999;\n"


def _admin() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


def _dsn_for(name: str) -> str:
    return DSN.rsplit("/", 1)[0] + "/" + name


def _drop(name: str) -> None:
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="module")
def migrated_template():
    """A fully migrated database nothing stays connected to, cloned per test."""
    name = f"meshy_rbgate_tpl_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(_dsn_for(name)) as conn:
            schema_sets.apply_all(conn, verbose=False)
            schema_sets.assert_schema_complete(conn)
        yield name
    finally:
        _drop(name)


@pytest.fixture()
def in_service(migrated_template):
    """A database in service: every set applied, ledgers populated."""
    name = f"meshy_rbgate_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}" TEMPLATE "{migrated_template}"')
    try:
        yield _dsn_for(name)
    finally:
        _drop(name)


@pytest.fixture()
def fresh():
    name = f"meshy_rbgate_fresh_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _dsn_for(name)
    finally:
        _drop(name)


def _divergence_copy(tmp_path: Path, extra: dict[str, bytes]) -> Path:
    """sql_meshychat/ copied byte for byte (so its checksums match the ledger), plus probes."""
    src = next(s.sql_dir for s in schema_sets.SETS if s.name == DIVERGENCE)
    dst = tmp_path / "sql_meshychat"
    shutil.copytree(src, dst)
    for rel, body in extra.items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_bytes(body)
    return dst


def _use(monkeypatch, sql_dir: Path) -> None:
    sets = tuple(dataclasses.replace(s, sql_dir=sql_dir) if s.name == DIVERGENCE else s
                 for s in schema_sets.SETS)
    monkeypatch.setattr(schema_sets, "SETS", sets)


def _use_base(monkeypatch, sql_dir: Path) -> None:
    sets = tuple(dataclasses.replace(s, sql_dir=sql_dir) if s.name == "base" else s
                 for s in schema_sets.SETS)
    monkeypatch.setattr(schema_sets, "SETS", sets)


def _exists(dsn: str, table: str) -> bool:
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]


def _ledger(dsn: str) -> list[int]:
    with psycopg.connect(dsn) as c:
        return [r[0] for r in c.execute("SELECT version FROM meshychat_migrations "
                                        "ORDER BY version").fetchall()]


def _main(*args: str) -> int:
    try:
        return sqlmigrate.main(list(args))
    except Exception as exc:  # a refusal may arrive as an exception; either is "not 0"
        print(f"main raised {type(exc).__name__}: {exc}")
        return -1


# ------------------------------------------------------------------------------- the gate

def test_a_pending_migration_without_a_down_script_is_refused_on_a_database_in_service(
        in_service, tmp_path, monkeypatch, capsys):
    _use(monkeypatch, _divergence_copy(tmp_path, {"999_rollback_probe.sql": PROBE_999}))
    rc = _main("--dsn", in_service)
    out = capsys.readouterr()
    assert rc != 0, "a migration with no way back was applied to a database in service"
    assert not _exists(in_service, "rollback_probe_999"), "the refusal must apply nothing"
    assert 999 not in _ledger(in_service)
    assert "999" in out.out + out.err, "the refusal must name the version"

    # The same refusal on the in-process path the test provisioning and tools use.
    with psycopg.connect(in_service) as conn, pytest.raises(Exception) as refused:
        schema_sets.apply_all(conn, verbose=False)
    assert "999" in str(refused.value)

    # Positive control, one file apart: with its down script the same migration applies.
    (tmp_path / "sql_meshychat" / "down").mkdir(exist_ok=True)
    (tmp_path / "sql_meshychat" / "down" / "999_rollback_probe_down.sql").write_bytes(
        PROBE_999_DOWN)
    assert _main("--dsn", in_service) == 0
    assert _exists(in_service, "rollback_probe_999") and 999 in _ledger(in_service)


def test_the_single_set_path_is_gated_too(in_service, tmp_path, capsys):
    sql_dir = _divergence_copy(tmp_path, {"999_rollback_probe.sql": PROBE_999})
    rc = _main("--dsn", in_service, "--sql-dir", str(sql_dir), "--ledger", "meshychat_migrations")
    assert rc != 0 and not _exists(in_service, "rollback_probe_999")
    (sql_dir / "down").mkdir(exist_ok=True)
    (sql_dir / "down" / "999_rollback_probe_down.sql").write_bytes(PROBE_999_DOWN)
    assert _main("--dsn", in_service, "--sql-dir", str(sql_dir),
                 "--ledger", "meshychat_migrations") == 0                    # control
    assert _exists(in_service, "rollback_probe_999")


def test_a_fresh_database_needs_no_down_script(fresh, tmp_path, monkeypatch):
    """Control on the gate's scope: the fresh-install path (README step 1) must not need
    one, because the way back from a fresh install is to drop the database."""
    _use(monkeypatch, _divergence_copy(tmp_path, {"999_rollback_probe.sql": PROBE_999}))
    assert _main("--dsn", fresh) == 0
    assert _exists(fresh, "rollback_probe_999")


def test_check_says_whether_the_deploy_would_be_refused(in_service, tmp_path, monkeypatch,
                                                        capsys):
    _use(monkeypatch, _divergence_copy(tmp_path, {"999_rollback_probe.sql": PROBE_999}))
    assert _main("--dsn", in_service, "--check") == 1
    out = capsys.readouterr().out
    assert "refused" in out.lower() and "999" in out, out
    assert not _exists(in_service, "rollback_probe_999"), "--check applies nothing"


# ------------------------------------------------------------------------ the backup path

def _pg_bin() -> str | None:
    base = os.environ.get("MESHY_PG_BIN")
    if base:
        return base if any((Path(base) / n).exists() for n in ("pg_dump", "pg_dump.exe")) \
            else None
    found = shutil.which("pg_dump")
    return str(Path(found).parent) if found else None


def _backup(dsn: str, out: Path) -> Path:
    if _pg_bin() is None:
        msg = "pg_dump not found (MESHY_PG_BIN or PATH): the backup path cannot be exercised"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg)
        pytest.skip(msg)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run([sys.executable, "-m", "tools.backup", "backup", "--dsn", dsn,
                           "--out", str(out), "--pg-bin", _pg_bin()],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    return Path(json.loads(proc.stdout.strip().splitlines()[-1])["manifest"])


def test_a_verified_backup_at_the_current_schema_is_a_way_back(in_service, tmp_path,
                                                               monkeypatch):
    stale = _backup(in_service, tmp_path / "bk1")

    # The schema moves on (998 has a down script, so it may), after the first backup.
    _use(monkeypatch, _divergence_copy(tmp_path / "a", {
        "998_rollback_probe.sql": PROBE_998, "down/998_rollback_probe_down.sql": PROBE_998_DOWN}))
    assert _main("--dsn", in_service) == 0

    # 999 has no down script: refused without a backup, and with one taken BEFORE 998.
    _use(monkeypatch, _divergence_copy(tmp_path / "b", {
        "998_rollback_probe.sql": PROBE_998, "down/998_rollback_probe_down.sql": PROBE_998_DOWN,
        "999_rollback_probe.sql": PROBE_999}))
    assert _main("--dsn", in_service) != 0
    assert _main("--dsn", in_service, "--rollback-backup", str(stale)) != 0, (
        "a backup taken at another schema is not a way back to this one")
    assert not _exists(in_service, "rollback_probe_999")

    # A backup whose dump changed is not a way back either.
    current = _backup(in_service, tmp_path / "bk2")
    dump = current.parent / json.loads(current.read_text("utf-8"))["dump_file"]
    original = dump.read_bytes()
    dump.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    assert _main("--dsn", in_service, "--rollback-backup", str(current)) != 0
    dump.write_bytes(original)

    # Control: the same backup, intact, taken at this schema, opens the gate.
    assert _main("--dsn", in_service, "--rollback-backup", str(current)) == 0
    assert _exists(in_service, "rollback_probe_999")


# ---------------------------------------------------------------------- rolling back

def _newest_without_down_script() -> int:
    """The newest version of the real divergence set with no down script: `--rollback-to`
    can go no lower. It was 017 at this file's base (8caac46); fin/skill-frames' 018 made it
    018 (integ/r10), and 020 and 021 above it both have down scripts. Read from the files, so
    a later landing moves it here and not in each test."""
    from apps.core import rollback
    div = next(s for s in schema_sets.SETS if s.name == DIVERGENCE)
    return max(v for v, _d, p, _c in sqlmigrate.discover(div.sql_dir)
               if rollback.down_script(p) is None)


def test_rollback_to_runs_the_down_scripts_newest_first(in_service, tmp_path, monkeypatch):
    _use(monkeypatch, _divergence_copy(tmp_path, {
        "998_rollback_probe.sql": PROBE_998, "down/998_rollback_probe_down.sql": PROBE_998_DOWN,
        "999_rollback_probe.sql": PROBE_999, "down/999_rollback_probe_down.sql": PROBE_999_DOWN}))
    assert _main("--dsn", in_service) == 0
    assert {998, 999} <= set(_ledger(in_service))

    assert _main("--dsn", in_service, "--rollback-to", f"{DIVERGENCE}:998", "--check") == 0
    assert _exists(in_service, "rollback_probe_999"), "--check undoes nothing"

    floor = _newest_without_down_script()
    assert _main("--dsn", in_service, "--rollback-to", f"{DIVERGENCE}:{floor}") == 0
    assert not _exists(in_service, "rollback_probe_998")
    assert not _exists(in_service, "rollback_probe_999")
    assert max(_ledger(in_service)) == floor
    assert _main("--dsn", in_service) == 0, "and the migrations apply again"
    assert _exists(in_service, "rollback_probe_999")


def test_rollback_to_accepts_the_base_set_legacy_alias(in_service, capsys):
    assert _main("--dsn", in_service, "--rollback-to", "upstream:999", "--check") == 0
    output = capsys.readouterr()
    assert output.err == (
        'DEPRECATED: set name "upstream" is now "base"; the old name still works and will '
        "be removed in a later release\n"
    )
    assert "base: would undo nothing (newest first)" in output.out


def test_rollback_to_refuses_an_unknown_set_name_with_the_valid_names(in_service, capsys):
    with pytest.raises(SystemExit):
        sqlmigrate.main(["--dsn", in_service, "--rollback-to", "missing:999", "--check"])
    assert "valid names: ['base', 'meshychat']" in capsys.readouterr().err


def test_base_set_still_has_its_gate_and_down_script_rules(in_service, tmp_path, monkeypatch):
    """The base set has no floor rule, but its pending migrations still need a way back."""
    from apps.core import rollback

    base = schema_sets.resolve_set_name("base")
    assert base.name not in rollback.DOWN_REQUIRED_FROM
    assert rollback.missing_required_down_scripts(base) == []

    copied = tmp_path / "sql"
    shutil.copytree(base.sql_dir, copied)
    (copied / "999_base_rollback_probe.sql").write_bytes(PROBE_999)
    _use_base(monkeypatch, copied)
    with psycopg.connect(in_service) as conn:
        verdict = rollback.gate(conn)
    assert not verdict.allowed
    assert verdict.pending["base"] == [999]
    assert verdict.without_down["base"] == [999]


def test_rollback_refuses_the_whole_range_when_one_version_has_no_down_script(
        in_service, tmp_path, monkeypatch):
    """The real state at 8caac46: 014 has a down script and 017 does not, so rolling the
    divergence set back to 013 must refuse BEFORE running any down script. 998 (with a down
    script) sits above the gap, so a rollback that ran until it hit the gap would have
    undone 998 first: the refusal has to come from the check, not from the crash."""
    _use(monkeypatch, _divergence_copy(tmp_path, {
        "998_rollback_probe.sql": PROBE_998, "down/998_rollback_probe_down.sql": PROBE_998_DOWN}))
    assert _main("--dsn", in_service) == 0
    before = _ledger(in_service)
    assert {14, 17, 998} <= set(before), "control: all three are applied"
    rc = _main("--dsn", in_service, "--rollback-to", f"{DIVERGENCE}:13")
    assert rc != 0
    assert _ledger(in_service) == before, "nothing may be undone when the range has a gap"
    assert _exists(in_service, "rollback_probe_998")
    with psycopg.connect(in_service) as c:
        assert c.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = "
                         "'shared_memory' AND column_name = 'signed_bytes'").fetchone()[0] == 1


# ------------------------------------------------------------------ the down scripts

def _catalog(dsn: str, keep: set[str]) -> dict[str, list]:
    """The schema as the catalog states it, one list per kind, minus kept tables. An
    instrument independent of the scripts: nothing here reads a .sql file.

    It reads every kind a migration here can change (review 2026-09-25, finding 1: column
    ACLs, a trigger's enabled state, view definitions, types, rules, comments, default and
    schema privileges, owners and storage options were not read, so a down script that left
    one of them changed passed). ROUND_TRIP_BLIND_SPOTS below holds a probe for each."""
    q = {
        # First column of every kind: the TABLE the row belongs to (an index's table, not
        # the index), so a kept table takes its indexes and policies with it by identity
        # rather than by a name prefix Postgres may have truncated.
        "relations": "SELECT coalesce(t.relname, c.relname), c.relname, c.relkind, "
                     "c.relrowsecurity, c.relforcerowsecurity, coalesce(c.relacl::text,''), "
                     "c.relowner::regrole::text, c.relpersistence, "
                     "coalesce(c.reloptions::text,''), c.relreplident "
                     "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                     "LEFT JOIN pg_index i ON i.indexrelid=c.oid "
                     "LEFT JOIN pg_class t ON t.oid=i.indrelid WHERE n.nspname='public'",
        "columns": "SELECT table_name, column_name, data_type, is_nullable, "
                   "coalesce(column_default,''), coalesce(is_generated,'') "
                   "FROM information_schema.columns WHERE table_schema='public'",
        # pg_attribute.attacl: GRANT <privilege> (column) is invisible to relacl.
        "column_acls": "SELECT c.relname, a.attname, a.attacl::text FROM pg_attribute a "
                       "JOIN pg_class c ON c.oid=a.attrelid "
                       "WHERE c.relnamespace='public'::regnamespace AND a.attacl IS NOT NULL",
        "constraints": "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                       "FROM pg_constraint WHERE connamespace='public'::regnamespace",
        "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes "
                   "WHERE schemaname='public'",
        # tgenabled: pg_get_triggerdef leaves out whether the trigger fires (ALTER TABLE ...
        # DISABLE TRIGGER), which is the whole of what a guard trigger is for.
        "triggers": "SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid), tgenabled "
                    "FROM pg_trigger WHERE NOT tgisinternal",
        "policies": "SELECT tablename, policyname, permissive, roles::text, cmd, "
                    "coalesce(qual,''), coalesce(with_check,'') FROM pg_policies",
        "views": "SELECT c.relname, c.relkind, pg_get_viewdef(c.oid) FROM pg_class c "
                 "WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('v','m')",
        "rules": "SELECT tablename, rulename, definition FROM pg_rules "
                 "WHERE schemaname='public'",
        "functions": "SELECT p.proname, pg_get_function_identity_arguments(p.oid), "
                     "md5(pg_get_functiondef(p.oid)), coalesce(p.proacl::text,''), "
                     "p.proowner::regrole::text "
                     "FROM pg_proc p WHERE p.pronamespace='public'::regnamespace "
                     "AND p.prokind IN ('f','p')",
        # Enums, domains, ranges, base types. A table's row type and every array type come
        # and go with what they belong to; a standalone composite type is in "relations".
        "types": "SELECT t.typname, t.typtype, t.typowner::regrole::text, "
                 "coalesce(t.typacl::text,''), format_type(t.typbasetype, t.typtypmod), "
                 "t.typnotnull, coalesce(t.typdefault,''), "
                 "coalesce((SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) "
                 "FROM pg_enum e WHERE e.enumtypid=t.oid),'') FROM pg_type t "
                 "WHERE t.typnamespace='public'::regnamespace "
                 "AND t.typtype IN ('b','d','e','r','m') AND t.typcategory <> 'A'",
        # Comments on anything user-made: the table's name first where it is a relation.
        "comments": "SELECT CASE WHEN d.classoid='pg_class'::regclass "
                    "THEN d.objoid::regclass::text ELSE '' END, "
                    "pg_describe_object(d.classoid, d.objoid, d.objsubid), d.description "
                    "FROM pg_description d WHERE d.objoid >= 16384",
        "default_acls": "SELECT defaclrole::regrole::text, defaclnamespace::text, "
                        "defaclobjtype, defaclacl::text FROM pg_default_acl",
        "schemas": "SELECT nspname, nspowner::regrole::text, coalesce(nspacl::text,'') "
                   "FROM pg_namespace WHERE left(nspname, 3) <> 'pg_' "
                   "AND nspname <> 'information_schema'",
        "extensions": "SELECT extname, extversion, extnamespace::regnamespace::text "
                      "FROM pg_extension",
    }
    out: dict[str, list] = {}
    with psycopg.connect(dsn) as c:
        for kind, sql in q.items():
            rows = [tuple(str(x) for x in r) for r in c.execute(sql).fetchall()]
            rows = [r for r in rows if r[0].removeprefix("public.") not in keep]
            out[kind] = sorted(rows)
    return out


def _down_scripts() -> list[tuple[str, int, Path]]:
    """Paired by file name here, not by the gate's own lookup, so a lookup that finds
    nothing cannot make the round-trip test below vacuous."""
    found = []
    for s in schema_sets.SETS:
        for version, _d, path, _c in sqlmigrate.discover(s.sql_dir):
            down = s.sql_dir / "down" / f"{path.stem}_down.sql"
            if down.is_file():
                found.append((s.name, version, down))
    return found


def test_there_is_a_down_script_to_hold_to_its_claim():
    """Control for the parametrised test below: it must not pass by finding nothing."""
    assert (DIVERGENCE, 14) in [(s, v) for s, v, _ in _down_scripts()]


#: What a down script's header may no longer say since the rollback gate: sqlmigrate HAS a
#: down step (`--rollback-to`, apps/core/rollback.py), and it runs the script.
_STALE_DOWN_CLAIMS = ("has no down step", "nothing runs it but an operator or tests")


def _down_header(path: Path) -> str:
    """The leading comment block, as one line of text (the claims wrap across lines)."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("--"):
            break
        lines.append(line[2:].strip())
    return " ".join(" ".join(lines).split())


def test_every_down_script_header_says_the_rollback_gate_runs_it():
    """Operator decision 2026-09-25, item 5: 014's, 020's and 021's headers were written before
    the rollback gate and said sqlmigrate has no down step and nothing but an operator runs
    them; `sqlmigrate.py --rollback-to` runs them now. Every down script's header names
    `--rollback-to` and makes neither stale claim. CONTROL: the stale claims are found in the
    text they came from (a header that joins its wrapped lines)."""
    downs = sorted((ROOT / "sql_meshychat" / "down").glob("*_down.sql"))
    assert len(downs) >= 4, downs
    stale = {p.name: [c for c in _STALE_DOWN_CLAIMS if c in _down_header(p)] for p in downs}
    assert {k: v for k, v in stale.items() if v} == {}, stale
    assert [p.name for p in downs if "--rollback-to" not in _down_header(p)] == []
    control = ("-- NOT A MIGRATION. It lives outside (apps/core/sqlmigrate.py\n"
               "-- globs *.sql in that directory only and has no down step), and nothing runs "
               "it but an\n-- operator or tests.\n")
    joined = " ".join(" ".join(l[2:].strip() for l in control.splitlines()).split())
    assert all(c in joined for c in _STALE_DOWN_CLAIMS), joined


def test_a_header_that_names_the_rollback_gate_beside_an_operator_is_not_stale(tmp_path):
    """Review of fin/post-r10, finding 2: main's 015 and 019 down scripts (integ/r11) say
    "nothing runs it but an operator, `sqlmigrate.py --rollback-to ...` or
    tests/test_rollback_gate.py's round trip", which is true, and the stale-claim scan
    flagged it on the merged tree because its phrase stopped at "an operator". The stale
    claim is the operator-or-tests one that leaves the gate out. The header below is 015's,
    verbatim (origin/main 6d70836). CONTROL: 014's pre-gate header, verbatim (6663950),
    is still flagged."""
    true_015 = (
        "-- DOWN SCRIPT for sql_meshychat/015_namespace_permission_predicate.sql (divergence"
        " 040, slice\n"
        "-- 3; written at its integration, integ/r11).\n"
        "--\n"
        "-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set"
        " (apps/core/sqlmigrate.py\n"
        "-- globs *.sql in that directory only), and nothing runs it but an operator,"
        " `sqlmigrate.py\n"
        "-- --rollback-to meshychat:14` (apps/core/rollback.py) or"
        " tests/test_rollback_gate.py's round\n"
        "-- trip.\n"
        "\n"
        "SET LOCAL lock_timeout = '5s';\n")
    stale_014 = (
        "-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set"
        " (apps/core/sqlmigrate.py\n"
        "-- globs *.sql in that directory only and has no down step), and nothing runs it"
        " but an\n"
        "-- operator or tests/test_signed_bytes_down.py. Design section 4.3.\n")
    t = tmp_path / "015_x_down.sql"
    t.write_text(true_015, encoding="utf-8")
    assert [c for c in _STALE_DOWN_CLAIMS if c in _down_header(t)] == []
    assert "--rollback-to" in _down_header(t)
    t.write_text(stale_014, encoding="utf-8")
    assert [c for c in _STALE_DOWN_CLAIMS if c in _down_header(t)] == list(_STALE_DOWN_CLAIMS)


def test_every_down_script_pairs_with_a_migration():
    from apps.core import rollback
    for s in schema_sets.SETS:
        down_dir = s.sql_dir / "down"
        ups = {p.stem for _v, _d, p, _c in sqlmigrate.discover(s.sql_dir)}
        for f in sorted(down_dir.glob("*.sql")) if down_dir.is_dir() else []:
            assert f.name.endswith("_down.sql"), f"{f}: a down script is <stem>_down.sql"
            assert f.name[:-len("_down.sql")] in ups, f"{f}: no migration named {f.stem}"
            assert rollback.down_script(s.sql_dir / (f.name[:-len("_down.sql")] + ".sql")) == f


@pytest.mark.parametrize("set_name,version,down", _down_scripts(),
                         ids=lambda v: str(v) if not isinstance(v, Path) else v.name)
def test_every_down_script_restores_the_schema_its_migration_found(set_name, version, down,
                                                                  fresh):
    _assert_round_trip(set_name, version, down, fresh)


def _assert_round_trip(set_name: str, version: int, down: Path, fresh: str) -> None:
    """Apply everything below `version`, then `version`, then its down script: the catalog
    must be what the migration found (minus what KEEPS names), and the migration must apply
    again. Reads schema_sets.SETS at call time, so a test can point a set at a copy."""
    from apps.core import rollback
    keep = rollback.kept_tables(down)
    with psycopg.connect(fresh) as conn:
        for s in schema_sets.SETS:
            if s.name == set_name:
                sqlmigrate.migrate(conn, sql_dir=s.sql_dir, ledger=s.ledger, verbose=False,
                                   upto=version - 1)
                target = s
                break
            sqlmigrate.migrate(conn, sql_dir=s.sql_dir, ledger=s.ledger, verbose=False)
    before = _catalog(fresh, keep)
    # KEEPS exempts only what the migration CREATED: a table the migration found is part of
    # the schema the down script must give back, columns, indexes, policies and all.
    found = {r[0] for r in _catalog(fresh, set())["relations"]}
    already = sorted(keep & found)
    assert not already, (
        f"{down.name}: `-- KEEPS:` names {already}, which existed before the migration ran; "
        "KEEPS may name only a table the migration created, or it hides that table's every "
        "column, index, trigger and policy from this test")
    with psycopg.connect(fresh) as conn:
        summary = sqlmigrate.migrate(conn, sql_dir=target.sql_dir, ledger=target.ledger,
                                     verbose=False, upto=version)
    assert summary["versions"] == [version]
    applied = _catalog(fresh, keep)
    assert applied != before, "control: the migration changed the catalog"
    with psycopg.connect(fresh) as conn:
        undone = rollback.rollback(conn, target, version - 1)
    assert undone == [version]
    after = _catalog(fresh, keep)
    for kind in before:
        assert after[kind] == before[kind], (
            f"{down.name} does not restore {kind}: "
            f"extra {sorted(set(after[kind]) - set(before[kind]))[:5]}, "
            f"missing {sorted(set(before[kind]) - set(after[kind]))[:5]}")
    with psycopg.connect(fresh) as conn:            # and the round trip closes
        again = sqlmigrate.migrate(conn, sql_dir=target.sql_dir, ledger=target.ledger,
                                   verbose=False, upto=version)
    assert again["versions"] == [version]


# ------------------------- every NEW migration ships a down script (operator, 2026-09-25)
#
# OPERATOR DECISION 2026-09-25: from sql_meshychat/020 on, every migration ships a down script,
# and the round trip above holds it to the catalog. The rule is keyed on the FILE, not the
# number (review finding 6): a migration needs a down script unless its stem is one of the
# frozen rollback.NO_DOWN_SCRIPT_ALLOWED (main's 001-014 and 017, and 015/016/018/019, in flight
# on branches when the operator decided). A migration without one fails here; one with a down
# script is parametrised into the round trip by construction (_down_scripts pairs every
# migration that has one), and the test checks that it was.

def test_every_migration_from_the_floor_has_a_down_script_the_round_trip_holds():
    from apps.core import rollback
    assert rollback.DOWN_REQUIRED_FROM == {DIVERGENCE: 20}, "the operator's floor is 020"
    held = {(s, v) for s, v, _ in _down_scripts()}
    for s in schema_sets.SETS:
        missing = rollback.missing_required_down_scripts(s)
        assert not missing, (
            f"{s.name}: {missing} need a down script {s.sql_dir.name}/down/<stem>_down.sql "
            f"(every migration numbered {rollback.DOWN_REQUIRED_FROM.get(s.name)} or higher "
            "ships one; README 'A deploy refuses to roll forward without a way back')")
        allowed = rollback.NO_DOWN_SCRIPT_ALLOWED.get(s.name, frozenset())
        in_rule = s.name in rollback.DOWN_REQUIRED_FROM
        for v, _d, p, _c in sqlmigrate.discover(s.sql_dir) if in_rule else []:
            if p.stem not in allowed:
                assert (s.name, v) in held, f"{s.name}:{v} is not in the round trip"


def test_the_files_allowed_no_down_script_are_frozen_below_the_floor():
    """The exemption is a closed list of files that existed when the operator decided: every
    one numbered below 020, each named once, and none of them a file main does not know
    except the four then in flight. A new entry fails here (ship a down script instead)."""
    from apps.core import rollback
    allowed = rollback.NO_DOWN_SCRIPT_ALLOWED
    assert set(allowed) == {DIVERGENCE}
    stems = allowed[DIVERGENCE]
    assert len(stems) == 19, "frozen 2026-09-25: 001-019, one file each; add nothing"
    assert sorted(int(s[:3]) for s in stems) == list(range(1, 20))
    on_disk = {p.stem for _v, _d, p, _c in sqlmigrate.discover(
        next(s.sql_dir for s in schema_sets.SETS if s.name == DIVERGENCE))}
    in_flight = {"015_namespace_permission_predicate", "016_hub_connections_presence",
                 "018_skill_call_notify_triggers", "019_capability_revocation"}
    assert stems - on_disk <= in_flight, sorted(stems - on_disk - in_flight)


def test_the_rule_names_a_migration_without_a_down_script_whatever_its_number(tmp_path):
    """Positive control for the test above, on a set of probes (no database): a frozen file
    needs no down script, a NEW file below the floor (019_b) or above it (021_d) is named,
    020 with one passes, and a set outside the rule is never asked."""
    from apps.core import rollback
    d = tmp_path / "sql_meshychat"
    (d / "down").mkdir(parents=True)
    for name in ("001_write_side_ownership.sql", "018_skill_call_notify_triggers.sql",
                 "019_b.sql", "020_c.sql", "021_d.sql"):
        (d / name).write_bytes(b"SELECT 1;\n")
    (d / "down" / "020_c_down.sql").write_bytes(b"SELECT 1;\n")
    div = next(s for s in schema_sets.SETS if s.name == DIVERGENCE)
    assert rollback.missing_required_down_scripts(dataclasses.replace(div, sql_dir=d)) \
        == [19, 21]
    (d / "down" / "021_d_down.sql").write_bytes(b"SELECT 1;\n")
    (d / "down" / "019_b_down.sql").write_bytes(b"SELECT 1;\n")
    assert rollback.missing_required_down_scripts(dataclasses.replace(div, sql_dir=d)) == []
    (d / "down" / "020_c_down.sql").unlink()
    assert rollback.missing_required_down_scripts(dataclasses.replace(div, sql_dir=d)) == [20]
    other = next(s for s in schema_sets.SETS if s.name != DIVERGENCE)
    assert rollback.missing_required_down_scripts(dataclasses.replace(other, sql_dir=d)) == []


def test_a_new_migration_below_the_floor_needs_a_down_script_too(tmp_path):
    """Review 2026-09-25, finding 6: the floor was a NUMBER, and a new migration could take a
    free number below it (sqlmigrate applies pending migrations out of order) and ship
    without a down script. The rule is keyed on the FILE: a migration needs a down script
    unless it is one of the files named when the operator decided.

    Since integ/r11 no number below the floor is free (fin/biscuit's 015 and 019 landed), so
    the new file below it takes 015's number in a copy that leaves 015's file out, as a branch
    renumbering onto a gap would: the number is one the list names, the file is not."""
    from apps.core import rollback
    div = next(s for s in schema_sets.SETS if s.name == DIVERGENCE)
    # 020 itself is taken since integ/r9 (020_grants_list_indexes, which has a down script),
    # so the file above the floor takes the next free number instead (022 on integ/r10).
    above = max(v for v, _d, _p, _c in sqlmigrate.discover(div.sql_dir)) + 1
    assert above > rollback.DOWN_REQUIRED_FROM[DIVERGENCE]
    d = _divergence_copy(tmp_path, {"015_new_feature.sql": b"SELECT 1;\n",
                                    f"{above:03d}_new_feature.sql": b"SELECT 1;\n"})
    for gone in ("015_namespace_permission_predicate.sql",
                 "down/015_namespace_permission_predicate_down.sql"):
        (d / gone).unlink(missing_ok=True)
    assert "015_namespace_permission_predicate" in rollback.NO_DOWN_SCRIPT_ALLOWED[DIVERGENCE]
    assert rollback.missing_required_down_scripts(dataclasses.replace(div, sql_dir=d)) \
        == [15, above]
    (d / "down" / "015_new_feature_down.sql").write_bytes(b"SELECT 1;\n")      # control
    assert rollback.missing_required_down_scripts(dataclasses.replace(div, sql_dir=d)) \
        == [above]


PROBE_900 = (b"CREATE TABLE rollback_probe_900 (x int);\n"
             b"CREATE INDEX rollback_probe_900_x ON rollback_probe_900 (x);\n")


def test_the_round_trip_refuses_a_down_script_that_does_not_restore_the_schema(
        fresh, tmp_path, monkeypatch):
    """Positive control for the round trip: a probe migration whose down script drops the
    index and leaves the table fails it, naming the relation; the same probe with a down
    script that drops the table passes (the control one file apart)."""
    bad = b"DROP INDEX rollback_probe_900_x;\n"
    sql_dir = _divergence_copy(tmp_path, {"900_rollback_probe.sql": PROBE_900,
                                          "down/900_rollback_probe_down.sql": bad})
    _use(monkeypatch, sql_dir)
    down = sql_dir / "down" / "900_rollback_probe_down.sql"
    with pytest.raises(AssertionError) as refused:
        _assert_round_trip(DIVERGENCE, 900, down, fresh)
    assert "does not restore relations" in str(refused.value), refused.value
    assert "rollback_probe_900" in str(refused.value)


def test_the_round_trip_accepts_a_down_script_that_restores_the_schema(
        fresh, tmp_path, monkeypatch):
    sql_dir = _divergence_copy(tmp_path, {
        "900_rollback_probe.sql": PROBE_900,
        "down/900_rollback_probe_down.sql": b"DROP TABLE rollback_probe_900;\n"})
    _use(monkeypatch, sql_dir)
    _assert_round_trip(DIVERGENCE, 900, sql_dir / "down" / "900_rollback_probe_down.sql", fresh)


# Review 2026-09-25, finding 1: the `applied != before` control needs only ONE visible change,
# so a migration that makes a visible change AND one the catalog did not read, with a down
# script that undoes only the visible one, passed. Each probe below is exactly that shape:
# CREATE TABLE (visible, undone by the down script) plus one change of a kind `_catalog` must
# read (never undone). `setup` is an earlier probe migration, applied below 900, that makes the
# object the invisible change alters. A change that CREATES something is written the idempotent
# way (IF NOT EXISTS, OR REPLACE, DROP IF EXISTS first), as a careful migration would be, so the
# final re-apply cannot catch it by accident. The round trip must refuse every one, naming the
# kind of catalog row the down script failed to give back.
ROUND_TRIP_BLIND_SPOTS = {
    "trigger_disabled": (None, "triggers",
                         "ALTER TABLE shared_memory DISABLE TRIGGER "
                         "shared_memory_mesh_single_writer_trg;"),
    "column_grant": (None, "column_acls", "GRANT UPDATE (display_name) ON peers TO PUBLIC;"),
    "view_definition": ("CREATE VIEW rollback_probe_v AS SELECT 1 AS x;", "views",
                        "CREATE OR REPLACE VIEW rollback_probe_v AS SELECT 2 AS x;"),
    "enum_type": (None, "types", "DROP TYPE IF EXISTS rollback_probe_e; "
                                 "CREATE TYPE rollback_probe_e AS ENUM ('a', 'b');"),
    "domain": (None, "types", "DROP DOMAIN IF EXISTS rollback_probe_d; "
                              "CREATE DOMAIN rollback_probe_d AS int NOT NULL;"),
    "owner": ("CREATE TYPE rollback_probe_c AS (a int);", "relations",
              "ALTER TYPE rollback_probe_c OWNER TO meshy_app;"),
    "default_acl": (None, "default_acls",
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO PUBLIC;"),
    "comment": (None, "comments", "COMMENT ON TABLE peers IS 'rollback probe';"),
    "schema_acl": (None, "schemas", "GRANT CREATE ON SCHEMA public TO PUBLIC;"),
    "new_schema": (None, "schemas", "CREATE SCHEMA IF NOT EXISTS rollback_probe_s;"),
    "rule": (None, "rules", "CREATE OR REPLACE RULE rollback_probe_r AS ON INSERT TO peers "
                            "WHERE false DO INSTEAD NOTHING;"),
    "storage_option": (None, "relations", "ALTER TABLE peers SET (fillfactor = 70);"),
}


@pytest.mark.parametrize("probe", sorted(ROUND_TRIP_BLIND_SPOTS))
def test_the_round_trip_refuses_a_down_script_that_leaves_a_change_it_cannot_see_by_name(
        probe, fresh, tmp_path, monkeypatch):
    setup, kind, invisible = ROUND_TRIP_BLIND_SPOTS[probe]
    extra = {"900_rollback_probe.sql": b"CREATE TABLE rollback_probe_900 (x int);\n"
                                       + invisible.encode() + b"\n",
             "down/900_rollback_probe_down.sql": b"DROP TABLE rollback_probe_900;\n"}
    if setup:
        extra["899_rollback_probe_setup.sql"] = setup.encode() + b"\n"
    sql_dir = _divergence_copy(tmp_path, extra)
    _use(monkeypatch, sql_dir)
    with pytest.raises(AssertionError) as refused:
        _assert_round_trip(DIVERGENCE, 900, sql_dir / "down" / "900_rollback_probe_down.sql",
                           fresh)
    assert f"does not restore {kind}" in str(refused.value), refused.value


# ------------------------------------------- the backup path: THIS database, and its age

_ABSENT = object()


def _identity(dsn: str) -> tuple:
    """(name, database oid, cluster system_identifier, the server's clock), read here with
    SQL of this test's own, not through the module under test.

    The clock comes back in UTC: in the server's TimeZone, "now minus 23 h" is wall-clock
    arithmetic and spans 24 real hours across a clock change."""
    with psycopg.connect(dsn) as c:
        name, oid, sysid, now = c.execute(
            "SELECT current_database(), d.oid::bigint, s.system_identifier::text, "
            "clock_timestamp() FROM pg_database d, pg_control_system() s "
            "WHERE d.datname = current_database()").fetchone()
    return name, oid, sysid, now.astimezone(dt.timezone.utc)


def _hand_manifest(dsn: str, out: Path, **override) -> Path:
    """A manifest of THIS database written by hand, with a stand-in dump the gate hashes.
    Each test changes one field of it; everything else is this database's truth."""
    from apps.core import backup_manifest as bm
    out.mkdir(parents=True, exist_ok=True)
    dump = out / "hand.dump"
    dump.write_bytes(b"a stand-in; the gate checks its size and sha256 only\n")
    name, oid, sysid, now = _identity(dsn)
    with psycopg.connect(dsn) as c:
        ledgers = bm.ledgers(c)
    m = {"format": bm.FORMAT, "database": name, "database_oid": int(oid),
         "system_identifier": sysid,
         "created_at": (now - dt.timedelta(minutes=1)).isoformat(),
         "dump_file": dump.name, "dump_bytes": dump.stat().st_size,
         "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
         "tables": {}, "ledgers": ledgers}
    for k, v in override.items():
        if v is _ABSENT:
            m.pop(k)
        else:
            m[k] = v
    path = out / "hand.manifest.json"
    path.write_text(json.dumps(m), encoding="utf-8")
    return path


def _gate_line(dsn: str, manifest: Path | None) -> str:
    """The verdict line; an exception is reported as such, never as a refusal."""
    from apps.core import rollback
    try:
        with psycopg.connect(dsn) as conn:
            return rollback.gate(conn, rollback_backup=manifest).line()
    except Exception as exc:
        return f"RAISED {type(exc).__name__}: {exc}"


def test_a_hand_manifest_of_this_database_opens_the_gate(in_service, tmp_path, monkeypatch):
    """Control for the refusals below: the manifest they each change one field of is
    accepted as written, so each refusal is that field's."""
    _use(monkeypatch, _divergence_copy(tmp_path, {"999_rollback_probe.sql": PROBE_999}))
    without = _gate_line(in_service, None)
    assert "REFUSED" in without and "999" in without, without     # control: 999 needs a way back
    line = _gate_line(in_service, _hand_manifest(in_service, tmp_path / "m"))
    assert "ALLOWED" in line, line
    _n, _o, _s, now = _identity(in_service)
    near = _gate_line(in_service, _hand_manifest(
        in_service, tmp_path / "near",
        created_at=(now - dt.timedelta(hours=23)).isoformat()))
    assert "ALLOWED" in near, f"23 h old is inside the 24 h bound: {near}"


def _refusal_cases(now: dt.datetime, name: str, oid: int, sysid: str) -> dict[str, dict]:
    """case: (the one field changed, a phrase the refusal must give as its reason)"""
    return {
        "another-name": ({"database": name + "_elsewhere"}, "not of"),
        "another-oid": ({"database_oid": oid + 1}, "another database"),
        "another-cluster": ({"system_identifier": str(int(sysid) ^ 1)}, "another cluster"),
        "no-oid": ({"database_oid": _ABSENT}, "records no database_oid"),
        "no-cluster": ({"system_identifier": _ABSENT}, "records no system_identifier"),
        "over-24h": ({"created_at": (now - dt.timedelta(hours=25)).isoformat()}, "bound"),
        "in-the-future": ({"created_at": (now + dt.timedelta(days=1)).isoformat()},
                          "in the future"),
        "no-timezone": ({"created_at": (now - dt.timedelta(minutes=1))
                         .replace(tzinfo=None).isoformat()}, "no timezone"),
        "unparseable": ({"created_at": "yesterday"}, "does not parse"),
    }


@pytest.mark.parametrize("case", list(_refusal_cases(
    dt.datetime.now(dt.timezone.utc), "x", 1, "1")))
def test_the_backup_path_refuses_a_manifest_not_of_this_database_or_not_recent(
        case, in_service, tmp_path, monkeypatch):
    """The name alone does not say which database a backup is of (a database dropped and
    re-created, a restore, another server's meshy_django at the same release), and a
    created_at in the future, without a timezone, or over the bound is not a backup of the
    last 24 hours. Each is a REFUSED verdict, never a traceback."""
    _use(monkeypatch, _divergence_copy(tmp_path, {"999_rollback_probe.sql": PROBE_999}))
    name, oid, sysid, now = _identity(in_service)
    override, reason = _refusal_cases(now, name, int(oid), sysid)[case]
    line = _gate_line(in_service, _hand_manifest(in_service, tmp_path / "m", **override))
    assert line.startswith("roll-forward gate: REFUSED"), f"{case}: {line}"
    assert reason in line, f"{case}: refused, but not for the reason under test: {line}"


def test_a_backup_of_a_database_since_dropped_and_recreated_is_not_a_way_back(
        in_service, migrated_template, tmp_path, monkeypatch):
    """End to end with the real tool: back up X with a row in it, drop X, re-create it at
    the same schema (so the name and every ledger match), and offer the old backup as the
    way back for a migration with no down script."""
    def users() -> int:
        with psycopg.connect(in_service) as c:
            return c.execute("SELECT count(*) FROM dashboard_users").fetchone()[0]

    n0 = users()
    with psycopg.connect(in_service) as c:
        c.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                  "VALUES (%s, %s, 'dev', %s)",
                  (uuid.uuid4(), f"rb-{uuid.uuid4().hex[:8]}@example.test", uuid.uuid4()))
        c.commit()
    old = _backup(in_service, tmp_path / "old")

    name = in_service.rsplit("/", 1)[1]
    _drop(name)
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}" TEMPLATE "{migrated_template}"')
    assert users() == n0, "control: the re-created database does not hold the backed-up row"

    _use(monkeypatch, _divergence_copy(tmp_path, {"999_rollback_probe.sql": PROBE_999}))
    assert _main("--dsn", in_service, "--rollback-backup", str(old)) != 0, (
        "a backup of the database that was dropped is not a way back for this one")
    assert not _exists(in_service, "rollback_probe_999")

    # Control: a backup of THIS database opens the gate.
    new = _backup(in_service, tmp_path / "new")
    assert _main("--dsn", in_service, "--rollback-backup", str(new)) == 0
    assert _exists(in_service, "rollback_probe_999")


# ------------------------------------------------------------ rolling back: drift

def test_rollback_refuses_versions_applied_from_different_files(in_service, tmp_path,
                                                                monkeypatch, capsys):
    """A version whose file on disk is not the file that was applied has a down script
    written for something else; nothing may be undone."""
    d = _divergence_copy(tmp_path, {
        "998_rollback_probe.sql": PROBE_998, "down/998_rollback_probe_down.sql": PROBE_998_DOWN})
    _use(monkeypatch, d)
    assert _main("--dsn", in_service) == 0
    before = _ledger(in_service)
    assert 998 in before, "control: 998 is applied"

    (d / "998_rollback_probe.sql").write_bytes(PROBE_998 + b"-- edited after it was applied\n")
    capsys.readouterr()
    floor = _newest_without_down_script()
    assert _main("--dsn", in_service, "--rollback-to", f"{DIVERGENCE}:{floor}") != 0
    assert "998" in capsys.readouterr().err
    assert _ledger(in_service) == before and _exists(in_service, "rollback_probe_998"), (
        "nothing may be undone when a version was applied from another file")

    # Control: the file as it was applied rolls back.
    (d / "998_rollback_probe.sql").write_bytes(PROBE_998)
    assert _main("--dsn", in_service, "--rollback-to", f"{DIVERGENCE}:{floor}") == 0
    assert not _exists(in_service, "rollback_probe_998")


def test_060_declines_rollback_reapply_and_backfill_on_isolated_db(in_service):
    """Populated round-trip for sql_meshychat/060 on an isolated cloned database (never shared MESHY_TEST_DSN)."""
    from apps.mesh import graph, projection
    from tests.test_mesh_graphread import _grant, _mesh_claimant, _peer, _row

    with psycopg.connect(in_service) as conn:
        claimant = _mesh_claimant(conn)
        asker = _peer(conn, "rb60-asker-" + uuid.uuid4().hex[:6])
        recip = _peer(conn, "rb60-recip-" + uuid.uuid4().hex[:6])
        for p in (asker, recip):
            _grant(conn, "shared/mesh-chat", p, claimant, permission="write")
        t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
        ask = _row(conn, {**asker, "label": asker["name"]},
                   {"to": [recip["name"]], "thread_id": "rb60", "requires_ack": True,
                    "type": "question", "body": "take?"}, created_at=t0)
        _row(conn, {**recip, "label": recip["name"]},
             {"to": [asker["name"]], "thread_id": "rb60", "type": "note",
              "declines": [ask["key"]], "body": "no"}, created_at=t0 + dt.timedelta(minutes=1))
        conn.commit()
        projection.rebuild(conn)
        conn.commit()
        assert len(graph.owed_acks(conn, [recip["name"]], keys=[ask["key"]], states=["declined"])) == 1

    try:
        assert _main("--dsn", in_service, "--rollback-to", "meshychat:59") == 0
        with psycopg.connect(in_service) as conn:
            assert conn.execute("SELECT count(*) FROM mesh_edges WHERE edge_type = 'declines'").fetchone()[0] == 0
    finally:
        assert _main("--dsn", in_service) == 0

    with psycopg.connect(in_service) as conn:
        rep = projection.backfill(conn, types=("declines",))
        conn.commit()
        assert rep.edges_written >= 1
        owed_after = graph.owed_acks(conn, [recip["name"]], keys=[ask["key"]], states=["declined"])
        assert len(owed_after) == 1 and owed_after[0].state == "declined"
