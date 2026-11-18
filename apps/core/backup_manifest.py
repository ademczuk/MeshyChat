"""What a backup of this database must prove about itself, and how a restore checks it (E02).

The failure this answers: the backup script was never in version control, and the restore
check was calibrated to a schema five times smaller than production's. A check that carries
its own list of tables passes against whatever that list still names.

So this module has NO list. A manifest is read from the source database in the snapshot the
dump is taken in: EVERY table (from pg_class), its exact row count, and every migration ledger
the declaration in apps/core/schema_sets.py names, with each version's checksum. A restore is
verified by reading the same three things from the restored database and comparing them in
both directions: a table the manifest has and the target lacks, a table the target has and
the manifest lacks, a count that differs, a ledger that differs.

It lives in apps/core, not tools/backup, because the deploy's rollback gate
(apps/core/rollback.py) accepts a verified backup as the way back and must not import a tool.

A manifest also records WHICH database it is of: the database's oid and the cluster's
system_identifier (`identity`). The name alone is shared by a database dropped and
re-created and by another server's database of the same name; the rollback gate compares all
three before it accepts a backup as the way back (apps/core/rollback.py). A restore is a new
database and has a new oid, so a backup of the source is not a way back for the restore.

Counts are read with `row_security = off`: a role that row-level security would filter gets
an ERROR, never a smaller number. That is the difference between a backup that refuses and a
backup that silently holds one tenant's rows.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FORMAT = "meshychat-backup/1"

_TABLES = """
SELECT n.nspname, c.relname
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r', 'p')
   AND n.nspname NOT IN ('pg_catalog', 'information_schema')
   AND n.nspname NOT LIKE 'pg\\_%'
 ORDER BY 1, 2
"""


class BackupInvalid(RuntimeError):
    """The files on disk are not the backup the manifest describes, or the manifest is not
    one this code wrote. Distinct from a verification mismatch, which is about a DATABASE."""


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def table_counts(conn) -> dict[str, int]:
    """{schema.table: exact rows} for every table, in the caller's transaction."""
    conn.execute("SET LOCAL row_security = off")
    out: dict[str, int] = {}
    for schema, table in conn.execute(_TABLES).fetchall():
        n = conn.execute(f"SELECT count(*) FROM {_quote(schema)}.{_quote(table)}").fetchone()[0]
        out[f"{schema}.{table}"] = int(n)
    return out


def ledgers(conn) -> dict[str, dict[str, str]]:
    """{set name: {version: sha384 hex}} for every declared set; an absent ledger is {}."""
    from apps.core import schema_sets, sqlmigrate

    return {
        s.name: {str(v): c.hex() for v, c in sorted(
            sqlmigrate.applied(conn, s.ledger, create_if_missing=False).items())}
        for s in schema_sets.SETS
    }


def identity(conn) -> dict:
    """Which database INSTANCE this is: its oid (new for every CREATE DATABASE, a restore
    included) and its cluster's system_identifier (new for every initdb). pg_control_system()
    is executable by PUBLIC by default; where it has been revoked this raises, and the backup
    fails rather than write a manifest the gate would refuse."""
    oid, sysid = conn.execute(
        "SELECT d.oid::bigint, s.system_identifier::text FROM pg_database d, "
        "pg_control_system() s WHERE d.datname = current_database()").fetchone()
    return {"database_oid": int(oid), "system_identifier": str(sysid)}


def describe(conn) -> dict:
    """The database-side half of a manifest, read in the caller's transaction."""
    return {
        "database": conn.execute("SELECT current_database()").fetchone()[0],
        **identity(conn),
        "server_version": conn.execute("SHOW server_version").fetchone()[0],
        "tables": table_counts(conn),
        "ledgers": ledgers(conn),
    }


def compare(manifest: dict, observed: dict) -> list[str]:
    """Every difference between a manifest and a database description, as sentences."""
    out: list[str] = []
    want, have = manifest["tables"], observed["tables"]
    for t in sorted(set(want) - set(have)):
        out.append(f"{t}: in the backup, missing from the database")
    for t in sorted(set(have) - set(want)):
        out.append(f"{t}: in the database, not in the backup")
    for t in sorted(set(want) & set(have)):
        if want[t] != have[t]:
            out.append(f"{t}: backup has {want[t]} rows, database has {have[t]}")
    for name in sorted(set(manifest["ledgers"]) | set(observed["ledgers"])):
        a, b = manifest["ledgers"].get(name), observed["ledgers"].get(name)
        if a != b:
            only_a = sorted(set(a or {}) - set(b or {}), key=int)
            only_b = sorted(set(b or {}) - set(a or {}), key=int)
            drift = sorted((v for v in set(a or {}) & set(b or {}) if a[v] != b[v]), key=int)
            out.append(f"ledger {name}: backup-only {only_a}, database-only {only_b}, "
                       f"checksum differs {drift}")
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(manifest_path: Path) -> dict:
    """Read a manifest and check its dump's size and sha256. Raises BackupInvalid."""
    manifest_path = Path(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupInvalid(f"cannot read manifest {manifest_path}: {exc}") from exc
    if manifest.get("format") != FORMAT:
        raise BackupInvalid(f"{manifest_path.name}: format {manifest.get('format')!r}, "
                            f"expected {FORMAT!r}")
    for field in ("dump_file", "dump_sha256", "dump_bytes", "tables", "ledgers", "database",
                  "created_at"):
        if field not in manifest:
            raise BackupInvalid(f"{manifest_path.name}: no {field!r}")
    dump = manifest_path.parent / manifest["dump_file"]
    if Path(manifest["dump_file"]).name != manifest["dump_file"]:
        raise BackupInvalid(f"dump_file must be a bare name beside the manifest: "
                            f"{manifest['dump_file']!r}")
    if not dump.is_file():
        raise BackupInvalid(f"the dump {dump} is missing")
    size = dump.stat().st_size
    if size != manifest["dump_bytes"]:
        raise BackupInvalid(f"{dump.name}: {size} bytes, the manifest says "
                            f"{manifest['dump_bytes']}")
    digest = sha256_file(dump)
    if digest != manifest["dump_sha256"]:
        raise BackupInvalid(f"{dump.name}: sha256 {digest} does not match the manifest's "
                            f"{manifest['dump_sha256']}; the dump is not the one backed up")
    from apps.core import schema_sets

    # A manifest taken before the first set was renamed keys its ledgers by the old name.
    ledgers = {}
    for name, ledger in manifest["ledgers"].items():
        current = schema_sets.canonical_set_name(name)
        if current in ledgers:
            raise BackupInvalid(f"{manifest_path.name}: two ledgers name the set {current!r}; "
                                f"a manifest lists each set once")
        ledgers[current] = ledger
    manifest["ledgers"] = ledgers
    return manifest
