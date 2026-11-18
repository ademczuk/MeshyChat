"""Every pytest run names its template server: MESHY_TEMPLATE_DSN, no default. Decision 41.

THE DECISION (operator, 2026-09-26, decision 41): EVERY pytest run must set
MESHY_TEMPLATE_DSN explicitly. tests/conftest.py makes the run's database (CREATE DATABASE)
and drops it at the end, on the server MESHY_TEMPLATE_DSN names. It used to default that to
127.0.0.1:5434, which is this project's dev Postgres on HQD and ANOTHER project's Postgres
on HQB, so a plain `pytest` on HQB created and dropped databases on a server nobody chose.
w2-metrics already refused that for LIVE runs (tests/live_env.py); this extends it to every
run, MESHY_NO_TEST_DB=1 included.

WHAT THIS FILE PROVES, each refusal beside its control, in a CHILD pytest (the refusal is at
conftest import, so it can only be observed from outside the run):
  * RED -> GREEN: with MESHY_TEMPLATE_DSN unset (or blank, or with MESHY_NO_TEST_DB=1) the
    run is REFUSED, names the variable, runs no test, and made NO database connection. The
    last is observed, not inferred from the message: a plugin loaded with `-p` (before
    conftest) replaces psycopg.connect with a probe that records every attempt.
  * the probe's own control: with the variable set, conftest's first connection IS recorded,
    so an empty record above means "nothing connected", not "the probe saw nothing";
  * positive control: with the variable set to the real template server, a suite file runs
    and passes.
Mutant D41-M1 (the refusal replaced by the old 127.0.0.1:5434 default) turns the refusal
tests red: the child proceeds and the probe records its connection.

Run:  MESHY_TEMPLATE_DSN=postgresql://USER:PW@127.0.0.1:5434/postgres \\
      .venv/Scripts/python.exe -m pytest tests/test_template_dsn_required.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
#: A small suite file with no fixture of its own that needs anything but the run's database.
TARGET = ["tests/test_core_jsonerrors.py", "-k", "compact"]
#: Everything that could name a database for the child, and pytest's own steering: stripped,
#: then set per scenario, so the parent's values never leak in as if the child had named them.
STRIPPED = ("MESHY_TEMPLATE_DSN", "MESHY_TEST_DSN", "MESHY_TEST_DSN_EXPLICIT", "MESHY_DSN",
            "DATABASE_URL", "MESHY_NO_TEST_DB", "MESHY_LIVE_MODELS", "MESHY_CLONE_TEMPLATE")

PROBE = '''import os
import psycopg

def _probe(*args, **kwargs):
    with open(os.environ["MESHY_CONNECT_PROBE_LOG"], "a", encoding="utf-8") as f:
        f.write("connect\\n")
    raise psycopg.OperationalError("connect probe: every connection is refused on purpose")

psycopg.connect = _probe
'''


def _child(tmp_path: Path, *, probe: bool, **env_set: str):
    env = {k: v for k, v in os.environ.items()
           if k not in STRIPPED and not k.upper().startswith("PYTEST_")}
    env.update(env_set)
    args = [sys.executable, "-m", "pytest", *TARGET, "-q", "-p", "no:cacheprovider"]
    log = tmp_path / "connects.log"
    if probe:
        (tmp_path / "meshy_connect_probe.py").write_text(PROBE, encoding="utf-8")
        env["PYTHONPATH"] = str(tmp_path)
        env["MESHY_CONNECT_PROBE_LOG"] = str(log)
        args += ["-p", "meshy_connect_probe"]
    proc = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)
    connects = log.read_text(encoding="utf-8").count("connect") if log.exists() else 0
    return proc, connects


def _said(proc) -> str:
    return (f"exit {proc.returncode}\n--- stdout\n{proc.stdout[-2500:]}"
            f"\n--- stderr\n{proc.stderr[-2500:]}")


@pytest.mark.parametrize("extra", [{}, {"MESHY_TEMPLATE_DSN": "   "},
                                   {"MESHY_NO_TEST_DB": "1"}],
                         ids=["unset", "blank", "no-test-db"])
def test_a_run_without_the_template_dsn_is_refused_before_any_connection(tmp_path, extra):
    proc, connects = _child(tmp_path, probe=True, **extra)
    said = proc.stdout + proc.stderr
    assert connects == 0, f"connected {connects}x before refusing\n{_said(proc)}"
    assert proc.returncode != 0, _said(proc)
    assert "REFUSED" in said and "MESHY_TEMPLATE_DSN" in said, _said(proc)
    assert "passed" not in said, f"tests ran\n{_said(proc)}"


def test_the_probe_sees_conftests_connection_when_the_variable_is_set(tmp_path):
    """Control for the test above: named, conftest connects, and the probe records it."""
    proc, connects = _child(tmp_path, probe=True,
                            MESHY_TEMPLATE_DSN="postgresql://meshychat_owner:pw@127.0.0.1:1/postgres")
    assert connects >= 1, f"the probe recorded nothing\n{_said(proc)}"
    assert "MESHY_TEMPLATE_DSN is not set" not in proc.stdout + proc.stderr, _said(proc)


def test_with_the_template_dsn_named_the_suite_runs(tmp_path):
    """Positive control: the same child, named with this run's own template server."""
    named = os.environ.get("MESHY_TEMPLATE_DSN", "").strip()
    assert named, "this run itself was started without MESHY_TEMPLATE_DSN; conftest refuses that"
    proc, _ = _child(tmp_path, probe=False, MESHY_TEMPLATE_DSN=named)
    assert proc.returncode == 0, _said(proc)
    assert "1 passed" in proc.stdout, _said(proc)
