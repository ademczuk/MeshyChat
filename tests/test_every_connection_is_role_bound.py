"""Every database connection under apps/ comes from ONE constructor, apps.core.dbconn.

STATE AS OF 2026-09-23 (divergence 022's change set, risk R3 of the migration synthesis).
The six openers listed below are now TWO: the guarded constructor itself and the migrator,
which is exempt by role. authapi, peers and wsgw had already moved onto
`dbconn.connect()`; `apps/memoryskills/db.py` was the last app module that opened its own,
and it did so with a resolver no other module used: `MESHY_TEST_DSN`, a TEST variable, with
a hard-coded dev-database fallback, ignoring MESHY_DSN, DATABASE_URL and settings. Its
role binding was right (`rls.set_role` plus `assert_rls_active`), which is why the old
per-module check passed it; its DATABASE was not, so any in-process dispatcher that took a
connection from it would have served the memory skills from the dev database while every
other surface used the configured one. It now delegates to `dbconn.connect(dbconn.dsn())`
and crosses `dbconn.assert_serving_connection`, and
`test_no_app_module_opens_its_own_connection` below makes "one constructor" a property the
suite enforces instead of a sentence in docs/V1-DESIGN-RULES.md B1.

The history below is kept because it is why the file is shaped this way.

WHAT THIS CORRECTED, measured 2026-09-21.

`docs/V1-DESIGN-RULES.md` B1 states the rule and then marks it settled:

    "There is one pool constructor, it takes a principal, and it does SET ROLE + the
     local setting. There is no way to obtain an unbound connection.
     **Already true here** - apps/core/rls.py"

The PROPERTY is true today. The MECHANISM is not, and the difference is the whole point
of that document. Six call sites open a connection, not one:

    apps/core/dbconn.py:90        the guarded constructor, the one B1 describes
    apps/core/sqlmigrate.py:156   the migrator CLI, exempt by role (see EXEMPT below)
    apps/authapi/db.py:33         app-serving, opens its own
    apps/peers/db.py:39           app-serving, opens its own
    apps/memoryskills/db.py:49    app-serving, opens its own
    apps/wsgw/db.py:48            app-serving, opens its own

All four app modules DO the right thing: `autocommit=True` plus `rls.set_role(conn)`, so
every live connection really is running as the NOBYPASSRLS `meshychat_app` role. Nothing is
leaking today and this test is not reporting a hole.

What is missing is the part that would survive a fifth module. `dbconn.assert_serving_connection`
is the check that PROVES binding rather than assuming it, by probing that RLS is actually
in force on a FORCE-RLS table; a bare `set_role` call is an instruction whose effect nobody
verifies. That probe is reachable only through `dbconn`, and no app-serving path uses
`dbconn`. So the convention is held by four independent authors remembering it, which is
precisely what B1 exists to replace, and what the same document calls a convention trusted
to hold something a mechanism should have held.

This test does not refactor anyone's connection code. It makes the convention mechanical:
a new module that opens a connection and forgets to bind it fails here, and a new module
that opens one at all has to be named, so the count cannot grow silently back toward the
thing B1 already claims is impossible.

INSTRUMENT NOTE, because it bit the author. This scans the AST, not the text. A text search
for `psycopg.connect` with an unescaped dot matches the PROSE "psycopg connection" in a
docstring, which is how a first pass at this file produced two false openers
(`apps/calls/pipeline.py:71`, `apps/core/middleware.py:142`, both docstrings). An AST walk
cannot make that mistake.

Run:  pytest tests/test_every_connection_is_role_bound.py -v   (no database needed)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APPS_DIR = ROOT / "apps"

#: The ONE constructor B1 describes. Its connections cross `assert_serving_connection`.
GUARDED = "apps.core.dbconn"

#: Openers that legitimately do NOT bind the app role, each with the reason. An entry here
#: is a decision, not a default: keep it short, and never add one to silence a failure.
EXEMPT = {
    "apps.core.sqlmigrate": (
        "the migrator CLI. It applies DDL as the OWNER before the app role's grants and "
        "policies exist, so binding to meshy_app would make migration 010, which CREATEs "
        "that very role, unrunnable."
    ),
}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _calls(tree: ast.AST) -> set[str]:
    """Dotted names of every call in the module, e.g. {'psycopg.connect', 'rls.set_role'}."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts: list[str] = []
        f = node.func
        while isinstance(f, ast.Attribute):
            parts.append(f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            parts.append(f.id)
            out.add(".".join(reversed(parts)))
    return out


def scan() -> dict[str, set[str]]:
    """{module: calls} for every module under apps/ that opens a connection."""
    found: dict[str, set[str]] = {}
    for path in sorted(APPS_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file is a different test's problem
            continue
        calls = _calls(tree)
        if "psycopg.connect" in calls:
            found[_module_name(path)] = calls
    return found


# --------------------------------------------------------------------------- controls

def test_the_scan_finds_connection_openers_at_all():
    """Positive control. An empty scan would make every assertion below vacuous."""
    found = scan()
    assert found, "the AST scan found no connection openers; the instrument is broken"
    assert GUARDED in found, f"sanity: {GUARDED} is known to open a connection"


def test_the_scan_does_not_match_prose():
    """Sensitivity control for the exact defect that produced this file's first draft.

    Both modules below contain the WORDS 'psycopg connection' in a docstring and open no
    connection. A text search with an unescaped dot reports them; the AST must not.
    """
    found = scan()
    for prose_only in ("apps.calls.pipeline", "apps.core.middleware"):
        assert prose_only not in found, (
            f"{prose_only} was reported as a connection opener. It only mentions one in "
            "prose, so the scan has regressed to text matching."
        )


# ------------------------------------------------------------------------ the assertion

@pytest.mark.parametrize("module", sorted(scan()))
def test_each_opener_binds_the_role_or_is_named_exempt(module):
    """One case per module, so a failure names the module rather than a set."""
    calls = scan()[module]
    if module == GUARDED:
        pytest.skip("the guarded constructor is the thing every other path should use")
    if module in EXEMPT:
        pytest.skip(f"exempt: {EXEMPT[module]}")
    assert "rls.set_role" in calls, (
        f"{module} opens a psycopg connection and never calls rls.set_role on it. "
        f"Without it the connection runs as the bootstrap role, which has rolbypassrls, "
        f"so every tenant's rows are visible and NOTHING RAISES. Either route it through "
        f"{GUARDED}, or bind the role here, or add it to EXEMPT with the reason."
    )


def test_the_exempt_list_has_not_gone_stale():
    """An exemption for a module that no longer opens a connection is rot.

    Without this, EXEMPT accumulates entries nobody can justify and the next reader cannot
    tell a live decision from a leftover.
    """
    openers = set(scan())
    stale = set(EXEMPT) - openers
    assert not stale, f"EXEMPT names modules that open no connection any more: {sorted(stale)}"


def test_no_app_module_opens_its_own_connection():
    """B1 as a property: the ONLY openers under apps/ are the guarded constructor and the
    named exemptions. Replaces the test that recorded the gap while four app modules still
    opened their own; that test said to update it and B1 together when the gap closed.

    `dbconn.connect()` sets the role AND observes it (`assert_not_bypassing`), and resolves
    the database through `dbconn.dsn()`, so a module that routes through it cannot reach a
    different database or run as a bypassing role. A module that opens its own can do
    either, silently: `apps/memoryskills/db.py` did the first (MESHY_TEST_DSN, dev default)
    until 2026-09-23.
    """
    openers = set(scan())
    assert GUARDED in openers, "control: the guarded constructor is found by the scan"
    own = openers - {GUARDED} - set(EXEMPT)
    assert not own, (
        f"{sorted(own)} open(s) a psycopg connection directly. Route it through "
        f"{GUARDED}.connect() (or serving_connection()), which binds the role, verifies "
        "it, and resolves the ONE configured database. docs/V1-DESIGN-RULES.md B1."
    )


def test_the_memory_skills_module_delegates_to_the_guarded_constructor():
    """The specific opener that was last to move, pinned by name so a revert is loud.
    The in-process memory dispatcher (apps/calls/route.py) runs on the request's own
    `dbconn.serving_connection`; `apps.memoryskills.db` is the connection its tests, the
    package docstring and any shell caller take, and it must be the same kind."""
    tree = ast.parse((APPS_DIR / "memoryskills" / "db.py").read_text(encoding="utf-8"))
    calls = _calls(tree)
    assert "dbconn.connect" in calls, "control: the module is read and delegates"
    assert "psycopg.connect" not in calls, "apps/memoryskills/db.py opens its own again"
    # And it crosses the same RLS probe a request's connection does, not only set_role.
    assert "dbconn.assert_serving_connection" in calls
