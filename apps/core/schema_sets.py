"""The migration SETS this database is built from, declared once, in code.

WHY THIS FILE EXISTS, and the incident is from this repository.

`sqlmigrate.py` already supports a second migration set: `--sql-dir` selects the
directory and `--ledger` selects its table, and `applied()` documents why a divergence
must not share `_sqlx_migrations`. The capability was complete. What did not exist was
anything that KNEW there are two sets.

Measured on 2026-09-21 against a fresh database built by following README.md exactly:

    python apps/core/sqlmigrate.py --dsn postgresql://.../meshy_django
    -> OK: 104 applied, 0 already there, 104 total
    pytest tests/ -q
    -> 4 failed, 169 passed

The four failures were `test_write_side_ownership.py` (all three) and one peer-rotation
test. Their cause was not any of the code they test: `sql_meshychat/001_write_side_ownership.sql`
had never been applied, because the README documents one migrate command and that command
applies `sql/` only. Applying the divergence set turned all four green with no code change
(173 passed).

So the documented setup path produced a database whose INSERT policies are still
`WITH CHECK (true)` on the tenant tables, which is precisely the hole `docs/DIVERGENCES.md`
was written to close. Nothing reported a problem. The migrator said OK and meant it, because
it had been asked about one directory and answered about one directory.

Grepped before writing this, with a positive control: no Python file in the tree referenced
`sql_meshychat` or `meshychat_migrations`, while `docs/DIVERGENCES.md` and `.gitattributes`
both did. The set list existed only as prose and as operator memory.

This is the repository's own thesis turned on itself. `docs/V1-DESIGN-RULES.md`: "the defects
were not language defects. They were places where a convention was trusted to hold something a
mechanism should have held." A4 says a skipped check is a failure. E6 says the deploy gate
asserts schema is a superset of code, not that the process started. A migration set nobody
enumerates is a skipped check that reports success.

THE FIX IS THE DECLARATION, not the checker. `SETS` below is the only place that knows how
many migration sets exist. Adding a third one is one tuple entry, and every entry point that
iterates `SETS` picks it up without being told.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from apps.core import sqlmigrate

#: Repository root: this file is apps/core/schema_sets.py.
ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class MigrationSet:
    """One directory of .sql files and the ledger table that records them.

    `ledger` is NOT derivable from `sql_dir`. The `sql` set writes to `_sqlx_migrations`;
    writing its versions and checksums elsewhere would make its completed migration history
    look absent to the cutover checks.
    """

    name: str
    sql_dir: Path
    ledger: str
    #: Stated so a reader knows which failures to expect when this set is missing,
    #: rather than having to bisect a test suite the way this file's author did.
    why: str


#: EVERY migration set, in application order. The divergence set changes policies on
#: tables created by the `sql` set, so it cannot be applied to an empty database.
SETS: tuple[MigrationSet, ...] = (
    MigrationSet(
        name="base",
        sql_dir=ROOT / "sql",
        ledger=sqlmigrate.LEDGER,
        why="the schema itself, applied verbatim and checksummed",
    ),
    MigrationSet(
        name="meshychat",
        sql_dir=ROOT / "sql_meshychat",
        ledger="meshychat_migrations",
        why=(
            "deliberate divergences from the base set. Without it the INSERT policies on the "
            "tenant tables are still WITH CHECK (true), so the write side of row-level "
            "security is open and tests/test_write_side_ownership.py fails."
        ),
    ),
)


_SET_NAME_ALIASES = {"upstream": "base"}
_UPSTREAM_SET_DEPRECATION = (
    'DEPRECATED: set name "upstream" is now "base"; the old name still works and will be '
    "removed in a later release"
)


def canonical_set_name(name: str) -> str:
    """The current name of a set, for stored data written under an older name.

    No message is printed: this is for reading what is already on disk (a backup manifest
    taken before the first set was renamed), not for something an operator typed.
    """
    return _SET_NAME_ALIASES.get(name, name)


def resolve_set_name(name: str) -> MigrationSet:
    """Return the declared migration set named by input, including supported aliases."""
    resolved = _SET_NAME_ALIASES.get(name, name)
    if resolved != name:
        print(_UPSTREAM_SET_DEPRECATION, file=sys.stderr)
    for migration_set in SETS:
        if migration_set.name == resolved:
            return migration_set
    raise ValueError(
        f"unknown migration set {name!r}; valid names: {[s.name for s in SETS]}"
    )


class SchemaIncomplete(RuntimeError):
    """The database is missing migrations that exist on disk, or a checksum drifted.

    Deliberately a distinct type. A caller that wants to degrade on a missing OPTIONAL
    set still must not confuse this with a connection error, and a deploy gate that
    catches broad exceptions would otherwise turn a schema hole into a startup warning.
    """


def pending(conn) -> dict[str, list[int]]:
    """Per set, the versions on disk that this database has not applied.

    Returns an entry for every set, including empty lists, so a caller can tell
    "checked, nothing pending" apart from "never looked at that set". An empty dict
    would be indistinguishable from a set list that failed to load, which is the
    B3 typed-absence rule applied to this module's own return value.

    Raises rather than returning on checksum drift, because a drifted checksum means
    the files on disk are not the files this database was built from, and there is no
    honest way to express that as a count of pending versions.
    """
    out: dict[str, list[int]] = {}
    for s in SETS:
        plan = sqlmigrate.discover(s.sql_dir)
        # Read-only: pending() is a question, and answering it must not require
        # CREATE on the schema. See sqlmigrate.applied's create_if_missing note.
        done = sqlmigrate.applied(conn, s.ledger, create_if_missing=False)
        drift = [v for v, _, _, c in plan if v in done and done[v] != c]
        if drift:
            raise SchemaIncomplete(
                f"checksum drift in set {s.name!r} on versions {drift}: the files in "
                f"{s.sql_dir} are not the files this database was built from."
            )
        out[s.name] = [v for v, _, _, _ in plan if v not in done]
    return out


def assert_schema_complete(conn) -> None:
    """Raise unless every declared migration set is fully applied. No return on success.

    This is `docs/V1-DESIGN-RULES.md` E6: the gate asserts schema is a superset of code.
    Call it from a deploy check or a readiness probe. Do NOT call it from the liveness
    route: `apps/core/middleware.py` records that `/v1/health` must answer when Postgres
    is down, and a schema probe cannot.
    """
    missing = {name: vs for name, vs in pending(conn).items() if vs}
    if not missing:
        return
    detail = "; ".join(
        f"{name}: {len(vs)} unapplied {vs[:5]}{'...' if len(vs) > 5 else ''} "
        f"({next(s.why for s in SETS if s.name == name)})"
        for name, vs in missing.items()
    )
    raise SchemaIncomplete(
        "refusing: the database is missing migrations that exist on disk. " + detail +
        ". Apply every set with: python apps/core/sqlmigrate.py --dsn DSN"
    )


def apply_all(conn, *, verbose: bool = True, rollback_backup: Path | None = None,
              gate_checked: bool = False) -> dict[str, dict]:
    """Apply every declared set in order. Returns each set's summary, keyed by name.

    Asks apps/core/rollback.py first, over EVERY set, before applying ANY: a pending
    migration with no way back on a database in service raises RollForwardRefused and
    nothing is applied (E03). A fresh database always passes. `gate_checked` is for
    sqlmigrate.main, which has just asked the same question and printed the answer.
    """
    if not gate_checked:
        from apps.core import rollback
        rollback.require(conn, rollback_backup=rollback_backup)
    summaries: dict[str, dict] = {}
    for s in SETS:
        if verbose:
            print(f"[{s.name}] {s.sql_dir.name} -> {s.ledger}", flush=True)
        summaries[s.name] = sqlmigrate.migrate(
            conn, sql_dir=s.sql_dir, ledger=s.ledger, verbose=verbose
        )
    return summaries
