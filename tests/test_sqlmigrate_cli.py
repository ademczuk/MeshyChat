"""The migrator must work the way README.md says to run it.

THE REAL FAILURE, measured 2026-09-21 on a pristine checkout of 0ee46bd. Commit 44ae8a3
added `from apps.core import schema_sets` inside sqlmigrate.main(). Run as a script,
which is the invocation README.md documents, Python puts apps/core on sys.path rather
than the repository root, so that import raised ModuleNotFoundError and the documented
setup command died before touching the database.

tests/test_schema_sets.py did not catch it and could not have: it calls
`sqlmigrate.main([...])` IN PROCESS, where pytest has already put the repository root on
sys.path. Those tests exercise the FUNCTION. README.md exercises the ENTRY POINT. They
are different things and only one of them was covered.

So this file runs the CLI as a SUBPROCESS with a clean working directory. That is the
only shape that reproduces the defect, and it is why the test is here rather than being
another case appended to test_schema_sets.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


def _run(*args: str) -> subprocess.CompletedProcess:
    """Invoke the migrator exactly as README.md does: as a script, by path.

    cwd is the repository root because that is where a reader runs it from, and PYTHONPATH
    is explicitly stripped so an ambient value cannot supply the path the script must
    establish for itself. Without that strip this test would pass in any shell that
    happened to export PYTHONPATH and fail in a clean one.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(Path("apps") / "core" / "sqlmigrate.py"), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def test_the_documented_command_runs_as_a_script():
    """The whole point. A non-zero exit here is the README being wrong."""
    proc = _run("--dsn", DSN, "--check")
    assert "ModuleNotFoundError" not in proc.stderr, (
        "the script cannot import its own package when run by path:\n" + proc.stderr
    )
    assert proc.returncode == 0, (
        f"documented command failed (rc={proc.returncode}).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_the_documented_check_reports_every_migration_set():
    """Positive control with teeth: proves the run did real work, not just exited 0.

    An exit code alone cannot tell a working migrator from one that parsed its arguments
    and returned early.
    """
    proc = _run("--dsn", DSN, "--check")
    assert proc.returncode == 0, proc.stderr
    for expected in ("base", "meshychat"):
        assert expected in proc.stdout, (
            f"--check did not report the {expected!r} set:\n{proc.stdout}"
        )


def test_bad_arguments_still_fail_loudly_as_a_script():
    """The path bootstrap must not swallow argument errors into a silent success."""
    proc = _run("--check")  # --dsn is required
    assert proc.returncode != 0
    assert "dsn" in (proc.stderr + proc.stdout).lower()


@pytest.mark.parametrize("flag", ["--help"])
def test_help_works_without_a_database(flag):
    """--help must not need a DSN or a live database; it is what a stuck reader tries."""
    proc = _run(flag)
    assert proc.returncode == 0, proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr
