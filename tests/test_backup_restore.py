"""A backup made by the repository's own tool restores into an empty database, and the restore
PROVES it against the source (E02).

THE FAILURE THIS GUARDS AGAINST (seen in an earlier deployment): the backup script was never in version control, and the restore check was
calibrated to a schema five times smaller than production's, so a restore "passed" against a
list of tables nobody had updated. The fix here is that the verifier has no list of its own:
`tools/backup` writes, beside the dump, a manifest of EVERY table's exact row count and every
migration ledger, taken in the SAME snapshot pg_dump reads (pg_export_snapshot), and the
restore compares the restored database with that manifest in both directions.

What this file proves, each refusal beside its positive control (house rule C1):
  * backup, then restore into an EMPTY database: every table's count and every ledger match,
    and the counts are real (the seeded peers are there, not a table of zeros);
  * a dump whose bytes changed is refused before pg_restore runs, and the target stays empty;
  * a target that is not empty is refused, and is left as it was;
  * a manifest whose count disagrees with the restored rows fails the verification (the
    verifier has teeth: C2, a gate that never refused is unarmed);
  * `--copy-to` writes a second copy and re-hashes it there: a copy whose bytes differ is an
    error, not a copy, and `--copy-to` naming the `--out` directory is refused before any
    dump is taken (it is not a second copy);
  * the counts and the dump are ONE snapshot: a row committed after the counts are read and
    before pg_dump runs is in neither, so the restore still verifies;
  * `created_at` is the snapshot's time (the server's clock at the start of the transaction
    the snapshot belongs to), not the time the dump finished;
  * a manifest whose ledger disagrees with the restored ledger fails the verification;
  * the manifest records which database instance it is of (the database's oid and the
    cluster's system_identifier), which the rollback gate compares.

The offsite copy (HQB pulls from HQD, operator decision 2026-09-25) and the restore rehearsal
into a new cluster are tests/test_backup_offsite.py. NOT PROVEN HERE: a rehearsal against a
copy of production (README "Offsite copy": rehearse monthly on HQB's copy). This file
restores synthetic rows on the dev server.

pg_dump and pg_restore come from MESHY_PG_BIN (a directory) or PATH, and must be the server's
major version or newer. Without them this file SKIPS on a developer box and FAILS under the
local gate (tools/ci/local_gate.py sets `CI=true`), where a skipped restore test is the #406
failure all over again.

Run:  MESHY_PG_BIN=... .venv/Scripts/python.exe -m pytest tests/test_backup_restore.py -q
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import types
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import schema_sets  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


def _pg_tool(name: str) -> str | None:
    base = os.environ.get("MESHY_PG_BIN")
    if base:
        for cand in (Path(base) / name, Path(base) / f"{name}.exe"):
            if cand.exists():
                return str(cand)
        return None
    return shutil.which(name)


@pytest.fixture(scope="module", autouse=True)
def _pg_tools():
    missing = [n for n in ("pg_dump", "pg_restore") if _pg_tool(n) is None]
    if missing:
        msg = (f"{missing} not found (MESHY_PG_BIN={os.environ.get('MESHY_PG_BIN')!r}, or "
               "PATH): the backup and restore cannot be exercised")
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg)
        pytest.skip(msg)


def _admin() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


def _dsn_for(name: str) -> str:
    return DSN.rsplit("/", 1)[0] + "/" + name


def _create(tag: str) -> str:
    name = f"meshy_bk_{tag}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    return name


def _drop(name: str) -> None:
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')


SEEDED_PEERS = 3


@pytest.fixture(scope="module")
def source():
    """A migrated database with rows in it: owners, peers and their keys."""
    name = _create("src")
    try:
        dsn = _dsn_for(name)
        with psycopg.connect(dsn) as conn:
            schema_sets.apply_all(conn, verbose=False)
            for i in range(SEEDED_PEERS):
                owner, peer = uuid.uuid4(), uuid.uuid4()
                conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                             "VALUES (%s,%s,'dev',%s)",
                             (owner, f"bk-{i}-{owner.hex[:8]}@example.test", uuid.uuid4()))
                conn.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                             "VALUES (%s,%s,%s,%s)", (peer, owner, f"bk-{i}", f"bk {i}"))
                conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                             "VALUES (%s,%s,%s,TRUE)", (peer, f"kid-{peer.hex}", "AAAA"))
            conn.commit()
        yield dsn
    finally:
        _drop(name)


@pytest.fixture()
def empty_target():
    name = _create("tgt")
    try:
        yield _dsn_for(name)
    finally:
        _drop(name)


def _cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-m", "tools.backup", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)


def _backup(source_dsn: str, out: Path, *extra: str) -> tuple[Path, dict]:
    proc = _cli("backup", "--dsn", source_dsn, "--out", str(out), *extra)
    assert proc.returncode == 0, f"backup failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    manifest = Path(result["manifest"])
    return manifest, json.loads(manifest.read_text(encoding="utf-8"))


def _user_tables(dsn: str) -> int:
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = "
                         "c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT IN "
                         "('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_%'"
                         ).fetchone()[0]


def test_backup_then_restore_reproduces_every_table_and_ledger(source, empty_target, tmp_path):
    manifest_path, manifest = _backup(source, tmp_path / "bk")

    # The manifest is taken from the source, not from a list: every table, real counts.
    assert manifest["tables"]["public.peers"] == SEEDED_PEERS, "control: the counts are real"
    assert manifest["tables"]["public.peer_keys"] == SEEDED_PEERS
    assert len(manifest["tables"]) == _user_tables(source), "every table is in the manifest"
    assert set(manifest["ledgers"]) == {s.name for s in schema_sets.SETS}
    assert all(manifest["ledgers"][s.name] for s in schema_sets.SETS), "ledgers are read"
    dump = manifest_path.parent / manifest["dump_file"]
    assert dump.exists() and dump.stat().st_size == manifest["dump_bytes"]
    # The password the backup actually connected with (pxx 2b9376 P3-1; a fixed literal tested nothing), as libpq
    # reports it (PQpass): it covers the DSN, PGPASSWORD and a pgpass file alike (GitHub Codex P2 on 6600cd68).
    # Under trust authentication there is none, and only the connection-string check applies (pxx 1ec862).
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert source not in manifest_text, "the manifest must not carry the source connection string"
    with psycopg.connect(source) as c:
        password = c.info.password
    if password:                      # trust authentication has no password to leak (GitHub Codex P2 on fd46e7b4)
        assert password not in manifest_text, "the manifest must not carry the connection password"

    proc = _cli("restore", "--manifest", str(manifest_path), "--dsn", empty_target)
    assert proc.returncode == 0, f"restore failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    assert verdict["verified"] is True and verdict["mismatches"] == [], verdict

    # A different instrument than the tool's own verifier: read the target directly.
    with psycopg.connect(empty_target) as c:
        assert c.execute("SELECT count(*) FROM peers").fetchone()[0] == SEEDED_PEERS
        schema_sets.assert_schema_complete(c)
    assert _user_tables(empty_target) == len(manifest["tables"])


def test_a_dump_whose_bytes_changed_is_refused_before_restore(source, empty_target, tmp_path):
    manifest_path, manifest = _backup(source, tmp_path / "bk")
    ok = _cli("verify", "--manifest", str(manifest_path))
    assert ok.returncode == 0, f"control: the untouched backup verifies\n{ok.stdout}{ok.stderr}"

    dump = manifest_path.parent / manifest["dump_file"]
    raw = bytearray(dump.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    dump.write_bytes(bytes(raw))

    bad = _cli("verify", "--manifest", str(manifest_path))
    assert bad.returncode != 0 and "sha256" in (bad.stdout + bad.stderr).lower(), bad
    proc = _cli("restore", "--manifest", str(manifest_path), "--dsn", empty_target)
    assert proc.returncode != 0
    assert "sha256" in (proc.stdout + proc.stderr).lower(), proc
    assert _user_tables(empty_target) == 0, "nothing may be restored from a changed dump"


def test_a_target_that_is_not_empty_is_refused_and_left_alone(source, tmp_path):
    manifest_path, _ = _backup(source, tmp_path / "bk")
    with psycopg.connect(source) as c:
        before = c.execute("SELECT count(*) FROM peers").fetchone()[0]
    assert before == SEEDED_PEERS, "control: the source has its rows"
    proc = _cli("restore", "--manifest", str(manifest_path), "--dsn", source)
    assert proc.returncode != 0
    assert "not empty" in (proc.stdout + proc.stderr).lower(), proc
    with psycopg.connect(source) as c:
        assert c.execute("SELECT count(*) FROM peers").fetchone()[0] == before


def test_the_verification_fails_when_a_count_disagrees(source, empty_target, tmp_path):
    """C2: the verifier must be able to say no. The manifest claims one more peer than the
    dump holds; the restore runs and the verification reports the table."""
    manifest_path, manifest = _backup(source, tmp_path / "bk")
    manifest["tables"]["public.peers"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    proc = _cli("restore", "--manifest", str(manifest_path), "--dsn", empty_target)
    assert proc.returncode != 0, proc
    verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    assert verdict["verified"] is False
    assert any("public.peers" in m for m in verdict["mismatches"]), verdict
    assert len(verdict["mismatches"]) == 1, "control: every other table matched"


def test_copy_to_writes_a_second_copy_and_rehashes_it(source, tmp_path):
    offsite = tmp_path / "offsite"
    manifest_path, manifest = _backup(source, tmp_path / "bk", "--copy-to", str(offsite))
    copied = offsite / manifest_path.name
    assert copied.exists() and (offsite / manifest["dump_file"]).exists()
    proc = _cli("verify", "--manifest", str(copied))
    assert proc.returncode == 0, proc


# ------------------------------------------------------------ added after the review

def _pg_bin_dir() -> str:
    return str(Path(_pg_tool("pg_dump")).parent)


@pytest.fixture()
def source_clone(source):
    """A copy of `source` a test may write to without changing what the others see."""
    name = f"meshy_bk_clone_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}" TEMPLATE "{source.rsplit("/", 1)[1]}"')
    try:
        yield _dsn_for(name)
    finally:
        _drop(name)


def _backup_with_a_write_before_the_dump(dsn: str, out: Path, monkeypatch) -> tuple[Path, dict, dict]:
    """Run the backup in-process; when it starts pg_dump, first commit one new
    dashboard_users row from another connection and read the server's clock."""
    from tools.backup import pgbackup
    seen: dict = {}
    real_run = subprocess.run

    def run(args, *a, **kw):
        if any(str(x) == "--format=custom" for x in args):
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                          "VALUES (%s,%s,'dev',%s)",
                          (uuid.uuid4(), f"late-{uuid.uuid4().hex[:8]}@example.test",
                           uuid.uuid4()))
                seen["clock_at_dump_start"] = c.execute("SELECT clock_timestamp()").fetchone()[0]
        return real_run(args, *a, **kw)

    monkeypatch.setattr(pgbackup, "subprocess", types.SimpleNamespace(run=run))
    result = pgbackup.backup(dsn, out, [], _pg_bin_dir())
    monkeypatch.undo()
    path = Path(result["manifest"])
    return path, json.loads(path.read_text(encoding="utf-8")), seen


def test_the_counts_and_the_dump_are_one_snapshot(source_clone, empty_target, tmp_path,
                                                  monkeypatch):
    path, manifest, seen = _backup_with_a_write_before_the_dump(source_clone, tmp_path / "bk",
                                                                monkeypatch)
    assert "clock_at_dump_start" in seen, "control: the write ran, just before pg_dump"
    with psycopg.connect(source_clone) as c:
        now = c.execute("SELECT count(*) FROM dashboard_users").fetchone()[0]
    assert now == manifest["tables"]["public.dashboard_users"] + 1, (
        "control: the row committed after the counts is in the source")
    proc = _cli("restore", "--manifest", str(path), "--dsn", empty_target)
    verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    assert proc.returncode == 0 and verdict["mismatches"] == [], (
        "the dump holds a row the counts do not: pg_dump did not read the counts' snapshot",
        verdict)


def test_created_at_is_the_snapshot_time_not_the_end_of_the_dump(source_clone, tmp_path,
                                                                 monkeypatch):
    _path, manifest, seen = _backup_with_a_write_before_the_dump(
        source_clone, tmp_path / "bk", monkeypatch)
    created = dt.datetime.fromisoformat(manifest["created_at"])
    assert created.tzinfo is not None, manifest["created_at"]
    assert created <= seen["clock_at_dump_start"], (
        f"created_at {created} is after pg_dump started ({seen['clock_at_dump_start']}): "
        "the age the gate measures would understate the rows a restore loses")


def test_the_manifest_records_which_database_it_is_of(source, tmp_path):
    _, manifest = _backup(source, tmp_path / "bk")
    with psycopg.connect(source) as c:
        oid, sysid = c.execute(
            "SELECT d.oid::bigint, s.system_identifier::text FROM pg_database d, "
            "pg_control_system() s WHERE d.datname = current_database()").fetchone()
    assert manifest.get("database_oid") == oid, manifest.get("database_oid")
    assert manifest.get("system_identifier") == sysid, manifest.get("system_identifier")


def test_a_ledger_that_disagrees_fails_the_verification(source, empty_target, tmp_path):
    manifest_path, manifest = _backup(source, tmp_path / "bk")
    ledger = manifest["ledgers"]["meshychat"]
    newest = max(ledger, key=int)
    del ledger[newest]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    proc = _cli("restore", "--manifest", str(manifest_path), "--dsn", empty_target)
    assert proc.returncode == 5, proc
    verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    assert verdict["mismatches"] == [
        f"ledger meshychat: backup-only [], database-only ['{newest}'], checksum differs []"
    ], verdict


def test_a_copy_whose_bytes_differ_is_an_error_not_a_copy(source, tmp_path, monkeypatch, capsys):
    from tools.backup import pgbackup
    real_copy = shutil.copyfile

    def corrupting(src, dst, *a, **kw):
        out = real_copy(src, dst, *a, **kw)
        if str(dst).endswith(".dump"):
            raw = bytearray(Path(dst).read_bytes())
            raw[len(raw) // 2] ^= 0x01
            Path(dst).write_bytes(bytes(raw))
        return out

    ok = pgbackup.main(["backup", "--dsn", source, "--out", str(tmp_path / "a"),
                        "--copy-to", str(tmp_path / "a-copy"), "--pg-bin", _pg_bin_dir()])
    assert ok == 0, "control: an intact copy is a copy"
    capsys.readouterr()

    monkeypatch.setattr(pgbackup, "shutil", types.SimpleNamespace(
        copyfile=corrupting, which=shutil.which))
    rc = pgbackup.main(["backup", "--dsn", source, "--out", str(tmp_path / "b"),
                        "--copy-to", str(tmp_path / "b-copy"), "--pg-bin", _pg_bin_dir()])
    out = capsys.readouterr()
    assert rc != 0, "a copy whose dump does not hash to the manifest was reported as made"
    assert "sha256" in (out.out + out.err).lower()


def test_copy_to_the_out_directory_is_refused_before_the_dump(source, tmp_path):
    out = tmp_path / "bk"
    for same in (str(out), str(out) + "/."):
        proc = _cli("backup", "--dsn", source, "--out", str(out), "--copy-to", same)
        assert proc.returncode == 3, proc
        assert "--copy-to" in proc.stderr, proc.stderr
    assert not (out.exists() and list(out.glob("*.dump*"))), "nothing may be dumped"
    # Control: another directory is a copy.
    proc = _cli("backup", "--dsn", source, "--out", str(out), "--copy-to", str(tmp_path / "c"))
    assert proc.returncode == 0, proc
