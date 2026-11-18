"""Apply the project's SQL migration files and record their checksums in the migration ledger.

The schema contains 32 row-level-security policies over 12 tables, 26 triggers enforcing
invariants the application cannot bypass, 58 partial indexes, 14 PL/pgSQL functions, a
generated column, and a `REVOKE UPDATE, DELETE` that makes one table append-only. Applying
the migration files directly keeps that schema definition in one place and makes each
applied change checkable.

LEDGER FORMAT: rows in `_sqlx_migrations` use the required ledger shape and SHA-384 over
each file's bytes. The stable format lets migration tooling verify what ran and detect
changed files without reconstructing the schema from application models.

CRLF: files are read as BYTES and never newline-translated. That is the whole reason
`.gitattributes` pins these paths to LF.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

# Running this file AS A SCRIPT (the invocation README.md documents) puts apps/core on
# sys.path, not the repository root, so `from apps.core import schema_sets` in main()
# raises ModuleNotFoundError. The test suite never saw it because it calls main() in
# process, where pytest has already put the root on the path: the tests exercised the
# FUNCTION while the documentation exercised the ENTRY POINT. Measured 2026-09-21 on a
# pristine checkout of 0ee46bd.
if __package__ in (None, ""):  # script execution only; a normal import is untouched
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

SQL_DIR = Path(__file__).resolve().parent.parent.parent / "sql"
NAME_RE = re.compile(r"^(\d+)_(.+)\.sql$")

# A migration file runs in one implicit transaction, which CREATE INDEX CONCURRENTLY cannot run
# inside. This runner detects the statement and applies that file outside a transaction, so no
# pair-splitting is needed.
CONCURRENTLY = re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY\b", re.I)

LEDGER = "_sqlx_migrations"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS {ledger} (
    version BIGINT PRIMARY KEY,
    description TEXT NOT NULL,
    installed_on TIMESTAMPTZ NOT NULL DEFAULT now(),
    success BOOLEAN NOT NULL,
    checksum BYTEA NOT NULL,
    execution_time BIGINT NOT NULL
)
"""


def discover(sql_dir: Path = SQL_DIR):
    """[(version, description, path, sha384_bytes)] ordered by version.

    Refuses two files that claim one version. Two branches each take "the next number"
    and both are right until they merge; 2026-09-21 produced four such collisions in one
    day on the divergence NUMBERS, and the migration numbers are one fetch away from the
    same fate. Without this check the pair would be discovered, sorted, and the second
    file's SQL would RUN before the ledger's PRIMARY KEY refused its row, so the refusal
    would arrive as a constraint error naming a table nobody edited. This is the seam every
    apply path crosses (migrate, schema_sets.pending, the test-database provisioning), so
    the refusal is early, named, and carries the next free number.
    """
    named = []
    for path in sorted(sql_dir.glob("*.sql")):
        m = NAME_RE.match(path.name)
        if not m:
            raise ValueError(f"migration filename does not parse: {path.name}")
        named.append((int(m.group(1)), m.group(2).replace("_", " "), path))
    if not named:
        raise ValueError(f"no migrations found in {sql_dir}")
    seen: dict[int, Path] = {}
    for version, _description, path in named:
        if version in seen:
            raise ValueError(
                f"two migrations claim version {version}: {seen[version].name} and "
                f"{path.name}. Renumber the one that landed second; the next free version "
                f"in {sql_dir.name} is {max(v for v, _, _ in named) + 1}."
            )
        seen[version] = path
    out = []
    for version, description, path in named:
        raw = path.read_bytes()                      # bytes: never newline-translated
        if b"\r\n" in raw:
            raise ValueError(
                f"{path.name} contains CRLF. Applying it would checksum differently from "
                "an LF-recorded database and crash-loop the server. Check .gitattributes."
            )
        out.append((version, description, path, hashlib.sha384(raw).digest()))
    return out


def applied(conn, ledger: str = LEDGER, *, create_if_missing: bool = True) -> dict[int, bytes]:
    """Rows already applied, from the named ledger.

    Divergences get their OWN ledger. Sharing `_sqlx_migrations` means a divergence numbered
    001 collides with the `sql` set's 001, caught by the checksum guard. A separate table
    keeps `_sqlx_migrations` byte-for-byte unchanged.

    `create_if_missing=False` makes this READ ONLY, which is what a caller that is only
    ASKING needs. Creating the ledger is a migrator action requiring CREATE on the schema,
    and the application role deliberately does not have it, so a read-only caller running
    as that role got `permission denied for schema public` out of a function whose entire
    job was to answer a question. Measured 2026-09-21 by running `manage.py run_jobs`,
    which checks schema completeness before its first tick. A missing ledger then reports
    as "nothing applied", which is the truthful answer rather than an error.
    """
    if not ledger.replace("_", "").isalnum():
        raise ValueError(f"unsafe ledger name: {ledger!r}")
    with conn.cursor() as cur:
        if create_if_missing:
            cur.execute(CREATE_TABLE.format(ledger=ledger))
        else:
            cur.execute("SELECT to_regclass(%s)", (ledger,))
            if cur.fetchone()[0] is None:
                return {}
        cur.execute(f"SELECT version, checksum FROM {ledger} WHERE success")
        return {int(v): bytes(c) for v, c in cur.fetchall()}


def migrate(conn, *, sql_dir: Path = SQL_DIR, verbose: bool = True, ledger: str = LEDGER,
            upto: int | None = None) -> dict:
    """Apply what is missing. Returns a summary. Raises on checksum drift.

    This is the UNGATED primitive: it applies whatever is pending without asking whether
    there is a way back. Deploys go through `schema_sets.apply_all` or `main()`, which ask
    apps/core/rollback.py first. `upto` stops after that version (the down-script round
    trip in tests/test_rollback_gate.py builds a database to the version before one).
    """
    import time

    done = applied(conn, ledger)
    plan = discover(sql_dir)
    drift = [v for v, _, _, c in plan if v in done and done[v] != c]
    if drift:
        raise RuntimeError(
            f"checksum drift on already-applied migrations {drift}: the files on disk are "
            "not the files this database was built from. Refusing to continue."
        )
    ran = []
    for version, description, path, checksum in plan:
        if version in done or (upto is not None and version > upto):
            continue
        body = path.read_bytes().decode("utf-8")
        started = time.monotonic()
        concurrent = bool(CONCURRENTLY.search(body))
        old_autocommit = conn.autocommit
        try:
            if concurrent:
                # CREATE INDEX CONCURRENTLY cannot run inside a transaction, and psycopg will not switch a connection to
                # autocommit while one is open. Reading the ledger (`applied`, above) opened one on any non-autocommit
                # connection, which is what `main()` and `schema_sets.apply_all` hand in, so the first no-transaction migration
                # in a set with a down script (sql_meshychat/028) could not be applied: found by the rollback gate's round
                # trip on 2026-09-30. End that read-only transaction first.
                conn.commit()
                conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(body)
                cur.execute(
                    f"INSERT INTO {ledger} (version, description, success, checksum, execution_time) "
                    "VALUES (%s, %s, true, %s, %s)",
                    (version, description, checksum, int((time.monotonic() - started) * 1e9)),
                )
            if not concurrent:
                conn.commit()
        except Exception:
            if not concurrent:
                conn.rollback()
            raise
        finally:
            conn.autocommit = old_autocommit
        ran.append(version)
        if verbose:
            print(f"  applied {version:>3} {description}{' [no-txn]' if concurrent else ''}", flush=True)
    return {"already_applied": len(done), "applied_now": len(ran), "total": len(plan), "versions": ran}


def main(argv=None) -> int:
    import argparse
    import psycopg

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    # Default None, not SQL_DIR/LEDGER: the default has to be distinguishable from an
    # operator naming one set explicitly, because with neither flag this command now
    # applies EVERY declared set. Before 2026-09-21 it applied `sql/` and reported OK,
    # leaving the divergence set unapplied and the write side of RLS open. See
    # apps/core/schema_sets.py for the measurement.
    ap.add_argument("--sql-dir", type=Path, default=None)
    ap.add_argument("--check", action="store_true", help="report only; apply nothing")
    ap.add_argument("--ledger", default=None, help="ledger table (divergences use their own)")
    # E03: the deploy refuses to roll forward without a way back (apps/core/rollback.py).
    ap.add_argument("--rollback-backup", type=Path, default=None, metavar="MANIFEST",
                    help="a tools/backup manifest of THIS database at its current schema, "
                         "taken within 24 h: the way back for pending migrations that have "
                         "no down script")
    ap.add_argument("--rollback-to", default=None, metavar="SET:VERSION",
                    help="run the down scripts of every applied version above VERSION in "
                         "SET, newest first; with --check, print the plan only")
    a = ap.parse_args(argv)

    # Importing here rather than at module scope keeps sqlmigrate.py importable by the
    # bootstrap path without dragging in the set declaration, which imports this module.
    from apps.core import rollback, schema_sets

    single = a.sql_dir is not None or a.ledger is not None
    sql_dir = a.sql_dir or SQL_DIR
    ledger = a.ledger or LEDGER
    if single:
        named = [s for s in schema_sets.SETS if s.ledger == ledger]
        only = schema_sets.MigrationSet(name=named[0].name if named else sql_dir.name,
                                        sql_dir=sql_dir, ledger=ledger, why="named on the CLI")

    with psycopg.connect(a.dsn) as conn:
        if a.rollback_to:
            set_name, _, version = a.rollback_to.partition(":")
            try:
                target = schema_sets.resolve_set_name(set_name)
            except ValueError as exc:
                ap.error(f"--rollback-to {a.rollback_to!r}: {exc}")
            if not version.isdigit():
                ap.error(f"--rollback-to {a.rollback_to!r}: expected SET:VERSION")
            try:
                undone = rollback.rollback(conn, target, int(version), dry_run=a.check)
            except rollback.RollbackRefused as exc:
                print(f"REFUSED: {exc}", file=sys.stderr)
                return 1
            verb = "would undo" if a.check else "undone"
            print(f"{target.name}: {verb} {undone or 'nothing'} (newest first)")
            return 0
        if a.check:
            if single:
                done, plan = applied(conn, ledger), discover(sql_dir)
                missing = [v for v, _, _, _ in plan if v not in done]
                print(f"on disk {len(plan)}, applied {len(done)}, missing {len(missing)}: {missing[:10]}")
                print(rollback.gate(conn, [only], rollback_backup=a.rollback_backup).line())
                return 0 if not missing else 1
            pend = schema_sets.pending(conn)
            for name, versions in pend.items():
                print(f"{name}: {len(versions)} missing {versions[:10]}")
            print(rollback.gate(conn, rollback_backup=a.rollback_backup).line())
            return 0 if not any(pend.values()) else 1
        try:
            verdict = rollback.require(conn, [only] if single else None,
                                       rollback_backup=a.rollback_backup)
        except rollback.RollForwardRefused as exc:
            print(f"REFUSED, nothing applied. {exc}", file=sys.stderr)
            return 1
        print(verdict.line())
        if single:
            summary = migrate(conn, sql_dir=sql_dir, ledger=ledger)
            print(f"OK: {summary['applied_now']} applied, {summary['already_applied']} already there, "
                  f"{summary['total']} total")
            return 0
        summaries = schema_sets.apply_all(conn, rollback_backup=a.rollback_backup,
                                          gate_checked=True)
        applied_now = sum(s["applied_now"] for s in summaries.values())
        total = sum(s["total"] for s in summaries.values())
        print(f"OK: {applied_now} applied, {total - applied_now} already there, {total} total "
              f"across {len(summaries)} sets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
