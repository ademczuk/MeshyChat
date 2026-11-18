"""The deploy's way back: a gate that refuses to roll forward without one, and the rollback (E03).

THE RULE (docs/V1-DESIGN-RULES.md D, docs/ISSUE-RESOLUTION-MAP.md "rollback leaves the DB
migrated"): the deploy must be able to roll the database back, or must refuse to roll
forward. Before this module the runner applied forward only, and the one down script
(sql_meshychat/down/014) ran by hand, so a rollback could leave an earlier release facing a
newer schema.

A pending migration may be applied when ANY of these holds, and otherwise nothing is applied:

  1. the database is FRESH: no set's ledger has a row. The way back is to drop it;
  2. EVERY pending migration has a down script, `<set dir>/down/<same stem>_down.sql`;
  3. the operator names a backup made by tools/backup (`--rollback-backup MANIFEST`) whose
     dump verifies (sha256, size), which was taken of THIS database at its CURRENT schema,
     and which is not older than `max_backup_age` (24 h by default). The way back is the
     restore, and the rows written between the backup and the rollback are what it costs;
     the age bound is there so that cost is stated rather than unbounded.

     THIS database is the same name, the same database oid and the same cluster
     (pg_control_system().system_identifier): the name alone is shared by a database dropped
     and re-created, by a restore, and by another server's meshy_django at the same release,
     and a backup of any of those is not a way back for this one. A manifest that records
     no oid or cluster is refused. CURRENT schema is every set's ledger equal, every version
     and checksum. The age is measured on the server's clock at both ends (the manifest's
     `created_at` is the start of the snapshot's transaction); a `created_at` in the future
     or without a timezone is refused, not trusted. The manifest is not signed: these checks
     catch a wrong backup, not a forged one.

A migration that changes only data (an UPDATE backfill) can never have a down script that
tests/test_rollback_gate.py accepts, because that test requires the migration to change the
catalog and the down script to restore it; the catalog cannot see rows. Such a migration
reaches a database in service only through path 3.

The `sql/` set is applied from its checked-in files without transformations and has no down
scripts, so a schema change in that set reaches a database in service only through path 3. The
cutover does too: production has that set applied and this repository's divergence set pending,
and 001-013 and 016-018 have no down scripts. At the cutover (operator, 2026-09-27) the backup
is taken on HQD before the window's migrations run. Restoring it recovers the HQD database from
that backup; host selection and application deployment remain unchanged.

`rollback()` runs down scripts newest first, each in its own transaction together with the
deletion of its ledger row, after refusing the WHOLE range when any version in it has no down
script: a partial rollback that stops at a gap leaves a schema no release was built for.

A down script may state what it deliberately leaves behind with a header line
`-- KEEPS: table_name` (014's keeps the retired verdicts). tests/test_rollback_gate.py holds
every down script to "the catalog the migration found, except what it KEEPS".
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from apps.core import sqlmigrate

DOWN_DIR = "down"
DEFAULT_MAX_BACKUP_AGE = dt.timedelta(hours=24)
_KEEPS = re.compile(r"^--\s*KEEPS:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", re.M)
_NO_TXN = re.compile(r"\bINDEX\s+CONCURRENTLY\b", re.I)


class RollForwardRefused(RuntimeError):
    """A pending migration has no way back. Distinct from SchemaIncomplete: the database is
    not broken, the deploy is refused."""


class RollbackRefused(RuntimeError):
    """The requested rollback cannot run as a whole; nothing was undone."""


def down_script(up_path: Path) -> Path | None:
    """The down script for one migration file, or None."""
    cand = up_path.parent / DOWN_DIR / f"{up_path.stem}_down.sql"
    return cand if cand.is_file() else None


# OPERATOR DECISION 2026-09-25: every NEW migration ships a down script, "020 onward". Sets not
# named here (`sql/`) are not asked. tests/test_rollback_gate.py fails on a migration
# that needs a down script and has none, and holds every down script to the round trip (the
# catalog its migration found).
DOWN_REQUIRED_FROM = {"meshychat": 20}

# The number alone is not the rule (review 2026-09-25, finding 6): 015, 016, 018 and 019 were
# free on main, a new file could take one, and sqlmigrate applies a pending migration below the
# ledger's newest. So the migrations that may have NO down script are named by FILE STEM, frozen:
# those on main when the operator decided (001-014, 017), and the four numbered below the floor
# that were then in flight on branches (fin/biscuit: 015, 019; fin/skill-frames: 016, 018).
# Every other file in the set needs a down script, whatever its number. Do not add to this set:
# a new migration ships a down script instead. Being named here permits no down script; it does
# not forbid one: 015 and 019 ship theirs (integ/r11), because on a database in service the
# gate below refuses ANY pending migration without one, and the test server applies 015 and 019
# after 020 and 021.
NO_DOWN_SCRIPT_ALLOWED = {"meshychat": frozenset({
    "001_write_side_ownership",
    "002_background_job_leases",
    "003_board_declared_act_index",
    "004_mesh_edges_projection",
    "005_mesh_route_usage",
    "006_mesh_embeddings",
    "007_mesh_inferred_edges",
    "008_projection_writer_only",
    "009_embed_skip_no_schema_version",
    "010_mesh_lexical",
    "011_mesh_message_single_writer",
    "012_write_policies_bound_and_frame_retention",
    "013_mesh_related_edges",
    "014_shared_memory_signed_bytes",
    "015_namespace_permission_predicate",          # fin/biscuit, landed integ/r11
    "016_hub_connections_presence",                # fin/skill-frames, landed integ/r10
    "017_mesh_summary_requests",
    "018_skill_call_notify_triggers",              # fin/skill-frames, landed integ/r10
    "019_capability_revocation",                   # fin/biscuit, landed integ/r11
})}


def missing_required_down_scripts(migration_set) -> list[int]:
    """Versions in a set under the rule whose file is not one NO_DOWN_SCRIPT_ALLOWED names
    and that have no down script, ascending."""
    if migration_set.name not in DOWN_REQUIRED_FROM:
        return []
    allowed = NO_DOWN_SCRIPT_ALLOWED.get(migration_set.name, frozenset())
    return [v for v, _d, path, _c in sqlmigrate.discover(migration_set.sql_dir)
            if path.stem not in allowed and down_script(path) is None]


def kept_tables(down_path: Path) -> set[str]:
    return set(_KEEPS.findall(down_path.read_text(encoding="utf-8")))


@dataclass
class Verdict:
    allowed: bool
    reason: str
    pending: dict[str, list[int]] = field(default_factory=dict)
    without_down: dict[str, list[int]] = field(default_factory=dict)

    def line(self) -> str:
        return f"roll-forward gate: {'ALLOWED' if self.allowed else 'REFUSED'}: {self.reason}"


def _declared():
    from apps.core import schema_sets
    return schema_sets.SETS


def gate(conn, sets=None, *, rollback_backup: Path | None = None,
         max_backup_age: dt.timedelta = DEFAULT_MAX_BACKUP_AGE,
         now: dt.datetime | None = None) -> Verdict:
    """Decide whether applying what is pending in `sets` (default: every declared set) has
    a way back. Reads only; never raises for a refusal (see `require`)."""
    sets = tuple(sets if sets is not None else _declared())
    pending: dict[str, list[int]] = {}
    without: dict[str, list[int]] = {}
    for s in sets:
        done = sqlmigrate.applied(conn, s.ledger, create_if_missing=False)
        plan = sqlmigrate.discover(s.sql_dir)
        todo = [(v, p) for v, _d, p, _c in plan if v not in done]
        pending[s.name] = [v for v, _ in todo]
        missing = [v for v, p in todo if down_script(p) is None]
        if missing:
            without[s.name] = missing
    if not any(pending.values()):
        return Verdict(True, "nothing is pending", pending)
    ledgers = {s.ledger for s in sets} | {s.ledger for s in _declared()}
    in_service = any(sqlmigrate.applied(conn, name, create_if_missing=False)
                     for name in sorted(ledgers))
    if not in_service:
        return Verdict(True, "fresh database (no ledger row in any set): the way back is to "
                             "drop it", pending, without)
    if not without:
        return Verdict(True, "every pending migration has a down script", pending)
    need = "; ".join(f"{name}: {vs}" for name, vs in without.items())
    if rollback_backup is None:
        return Verdict(False, (
            f"pending migrations with no down script on a database in service ({need}). "
            "Write <set>/down/<stem>_down.sql for each, or take a backup now with "
            "`python -m tools.backup backup --dsn DSN --out DIR` and pass "
            "--rollback-backup DIR/NAME.manifest.json"), pending, without)
    try:
        problem = _backup_problem(conn, Path(rollback_backup), max_backup_age, now)
    except Exception as exc:  # a refusal, never a traceback, and nothing applied
        problem = f"it could not be checked ({type(exc).__name__}: {exc})"
    if problem:
        return Verdict(False, f"no way back for {need}: the backup {rollback_backup} is not "
                              f"one: {problem}", pending, without)
    return Verdict(True, f"no down script for {need}; the way back is restoring "
                         f"{rollback_backup}", pending, without)


def _backup_problem(conn, manifest_path: Path, max_age: dt.timedelta,
                    now: dt.datetime | None) -> str | None:
    from apps.core import backup_manifest as bm
    try:
        manifest = bm.load(manifest_path)
    except bm.BackupInvalid as exc:
        return str(exc)
    database = conn.execute("SELECT current_database()").fetchone()[0]
    if manifest["database"] != database:
        return f"it is a backup of {manifest['database']!r}, not of {database!r}"
    here = bm.identity(conn)
    for key, what in (("system_identifier", "cluster"), ("database_oid", "database")):
        if manifest.get(key) is None:
            return (f"it records no {key}, so it cannot be shown to be of this {what}; take a "
                    "new backup with this checkout's tools/backup")
        if manifest[key] != here[key]:
            return (f"it is of another {what} named {database!r} ({key} {manifest[key]}, "
                    f"this one's is {here[key]}): a database dropped and re-created, a "
                    "restore, or another server")
    current = bm.ledgers(conn)
    if manifest["ledgers"] != current:
        diff = bm.compare({"tables": {}, "ledgers": manifest["ledgers"]},
                          {"tables": {}, "ledgers": current})
        return "it was not taken at this database's current schema (" + "; ".join(diff) + ")"
    try:
        created = dt.datetime.fromisoformat(str(manifest["created_at"]))
    except ValueError:
        return f"created_at {manifest['created_at']!r} does not parse"
    if created.tzinfo is None:
        return f"created_at {manifest['created_at']!r} has no timezone"
    if now is None:          # the server's clock, the clock created_at was read from
        now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    if created > now:
        return f"created_at {manifest['created_at']} is in the future (the server says {now})"
    age = now - created
    if age > max_age:
        return f"it is {age} old, over the {max_age} bound"
    return None


def require(conn, sets=None, **kw) -> Verdict:
    verdict = gate(conn, sets, **kw)
    if not verdict.allowed:
        raise RollForwardRefused(verdict.line())
    return verdict


def rollback(conn, migration_set, to_version: int, *, dry_run: bool = False) -> list[int]:
    """Undo every applied version above `to_version` in one set, newest first. Returns the
    versions undone (or, with dry_run, that would be). Refuses the whole range first."""
    ledger = migration_set.ledger
    done = sqlmigrate.applied(conn, ledger, create_if_missing=False)
    by_version = {v: (p, c) for v, _d, p, c in sqlmigrate.discover(migration_set.sql_dir)}
    todo = sorted((v for v in done if v > to_version), reverse=True)
    unknown = [v for v in todo if v not in by_version]
    if unknown:
        raise RollbackRefused(f"{migration_set.name}: applied versions {unknown} have no file "
                              "on disk; this checkout cannot undo them")
    drift = [v for v in todo if by_version[v][1] != done[v]]
    if drift:
        raise RollbackRefused(f"{migration_set.name}: versions {drift} were applied from "
                              "different files than these; their down scripts do not match")
    missing = [v for v in todo if down_script(by_version[v][0]) is None]
    if missing:
        raise RollbackRefused(
            f"{migration_set.name}: no down script for {missing}, so nothing above "
            f"{to_version} is undone. The way back is restoring the backup taken before they "
            "were applied (python -m tools.backup restore)")
    if dry_run:
        conn.rollback()
        return todo
    for v in todo:
        body = down_script(by_version[v][0]).read_text(encoding="utf-8")
        no_txn = bool(_NO_TXN.search(body))
        old = conn.autocommit
        try:
            if no_txn:
                conn.autocommit = True
            elif conn.info.transaction_status != 0:   # close the reads above first
                conn.commit()
            with conn.cursor() as cur:
                cur.execute(body)
                cur.execute(f"DELETE FROM {ledger} WHERE version = %s", (v,))
            if not no_txn:
                conn.commit()
        except Exception:
            if not no_txn:
                conn.rollback()
            raise
        finally:
            conn.autocommit = old
    return todo
