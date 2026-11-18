"""No hosted CI on this repository, by operator decision, and the LOCAL gate used instead
(operator decision 2026-09-25, item 1; E04).

THE DECISION, in the operator's words: "I want to not have to worry about the github actions
CI workflow so route around it". So there is no .github/workflows file, and the HQD
integrator runs tools/ci/local_gate.py on the exact commit before every fast-forward push.

The gate addresses seven of seven audits that reported green while the step read "skipped",
and an attestation that wrote green over 107 tests that never ran.
pytest's exit code alone says "no test failed", not how many ran.

What this file proves, each refusal beside its passing control:
  * no .github/workflows file exists, on disk or in the index, so a later branch cannot bring
    hosted CI back silently; the check finds a workflow when there is one;
  * the JUnit gate (tools/ci/junit_gate.py) refuses a failure, an error, a skip not on the
    allow-list, fewer tests than were collected, and a missing report;
  * the local gate, run end to end on a throwaway repository, passes a green run and names the
    commit in its one verdict line, quoting pytest's own summary line from the file it wrote;
    and refuses a failing run, a dirty tree (tracked change, untracked file), a HEAD that is
    not the commit it was asked to gate, a skip not on its allow-list, and an output
    directory inside the repository;
  * what `git status` does not show cannot turn a failing commit green (review 2026-09-25,
    finding 2): a skip-worktree or assume-unchanged entry and an ignored conftest.py are
    refused, and PYTHONPATH/PYTHONSTARTUP/PYTHONUSERBASE and a stale .pyc of a passing
    version in the ignored __pycache__ do not reach the run (it reports the failure);
  * the verdict is read from the FILES (exit code and summary line): an exit code of 1 beside a
    clean-looking summary is refused, and so is a failure in the summary beside exit code 0;
  * the gate hands the suite CI=true (a test that needs pg_dump fails instead of skipping),
    MESHY_PG_BIN and MESHY_TEMPLATE_DSN;
  * the gate REFUSES to start without a template DSN (`--template-dsn`, else the environment;
    operator decision 41, no default) and runs nothing; it prints the value it uses with the
    password masked, and the flag wins over the environment;
  * every skip on the gate's allow-list names a skip the suite has.

Run:  .venv/Scripts/python.exe -m pytest tests/test_local_gate.py -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JUNIT_GATE = ROOT / "tools" / "ci" / "junit_gate.py"
LOCAL_GATE = ROOT / "tools" / "ci" / "local_gate.py"


def _local_gate_module():
    spec = importlib.util.spec_from_file_location("local_gate", LOCAL_GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ no hosted CI

def hosted_ci_files(root: Path) -> list[str]:
    """Every file under .github/workflows on disk, whatever its extension."""
    wf = root / ".github" / "workflows"
    if not wf.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in wf.rglob("*") if p.is_file())


#: The ONE hosted workflow the operator allowed on 2026-09-29 (ASDLC D-2026-09-29-U18, -U20),
#: replacing the decision of 2026-09-25 that there is none. It informs and gates nothing.
#: Anything else under .github/workflows is still refused.
ALLOWED_HOSTED_CI = [".github/workflows/fast-checks.yml"]


def test_the_only_hosted_ci_workflow_is_the_one_allowed():
    found = hosted_ci_files(ROOT)
    assert [f for f in found if f not in ALLOWED_HOSTED_CI] == [], (
        "the operator allowed ONE hosted workflow (2026-09-29); the gate is still "
        "tools/ci/local_gate.py (README 'CI')")
    tracked = subprocess.run(["git", "ls-files", "--", ".github/workflows"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    assert [f for f in tracked if f not in ALLOWED_HOSTED_CI] == [], (
        f"tracked hosted CI files beyond the one allowed: {tracked}")


def test_the_allowed_workflow_cannot_gate_or_write():
    """What makes the exception safe to keep: it reads the repository and nothing more, and it
    runs only on a push or by hand, never on a pull request from a fork or on a schedule."""
    path = ROOT / ALLOWED_HOSTED_CI[0]
    if not path.is_file():
        return                      # the exception permits the file, it does not require it
    text = path.read_text(encoding="utf-8")
    lines = [ln.rstrip() for ln in text.splitlines()]
    at = lines.index("permissions:")
    assert lines[at + 1] == "  contents: read" and not lines[at + 2].startswith("  ")
    for forbidden in ("pull_request", "schedule:", "secrets.", "contents: write",
                      "id-token", "packages: write"):
        assert forbidden not in text, forbidden


def test_the_hosted_ci_check_finds_a_workflow_when_there_is_one(tmp_path):
    """Positive control: the check is not blind to the shape it forbids."""
    assert hosted_ci_files(tmp_path) == []
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    assert hosted_ci_files(tmp_path) == []                 # an empty directory is no workflow
    (wf / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (wf / "nested").mkdir()
    (wf / "nested" / "other.yaml").write_text("name: x\n", encoding="utf-8")
    assert hosted_ci_files(tmp_path) == [".github/workflows/ci.yml",
                                         ".github/workflows/nested/other.yaml"]


# ------------------------------------------------------------------ the JUnit report gate

def _junit(tmp_path: Path, cases: list[tuple[str, str | None, str | None]]) -> Path:
    """cases: (name, outcome in {None, 'failure', 'error', 'skipped'}, message)."""
    body = []
    for name, outcome, msg in cases:
        inner = f'<{outcome} message="{msg or ""}"/>' if outcome else ""
        body.append(f'<testcase classname="tests.x" name="{name}" time="0.1">{inner}</testcase>')
    counts = {k: sum(1 for _, o, _ in cases if o == k) for k in ("failure", "error", "skipped")}
    xml = ('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
           f'errors="{counts["error"]}" failures="{counts["failure"]}" '
           f'skipped="{counts["skipped"]}" tests="{len(cases)}">' + "".join(body) +
           "</testsuite></testsuites>")
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def _junit_gate(junit: Path, collected: int, *allow: str) -> subprocess.CompletedProcess:
    log = junit.parent / "collect.txt"
    log.write_text(f"tests/x.py::a\n\n{collected} tests collected in 1.00s\n", encoding="utf-8")
    args = [sys.executable, str(JUNIT_GATE), "--junit", str(junit), "--collect-log", str(log)]
    for a in allow:
        args += ["--allow-skip", a]
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def test_the_junit_gate_passes_a_clean_report(tmp_path):
    proc = _junit_gate(_junit(tmp_path, [("a", None, None),
                                         ("b", "skipped", "live LAN models: x")]),
                       2, "live LAN models")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("case,collected,expect", [
    ([("a", None, None), ("b", "failure", "boom")], 2, "failure"),
    ([("a", None, None), ("b", "error", "boom")], 2, "error"),
    ([("a", None, None), ("b", "skipped", "pg_dump not found")], 2, "skip"),
    ([("a", None, None), ("b", None, None)], 3, "collected"),
    ([], 0, "no tests"),
])
def test_the_junit_gate_refuses(tmp_path, case, collected, expect):
    proc = _junit_gate(_junit(tmp_path, case), collected, "live LAN models")
    assert proc.returncode != 0, f"the gate passed a report with a {expect}: {proc.stdout}"
    assert expect in (proc.stdout + proc.stderr).lower(), proc.stdout + proc.stderr


def test_the_junit_gate_refuses_a_missing_report(tmp_path):
    proc = _junit_gate(tmp_path / "junit.xml", 1)
    assert proc.returncode != 0 and "no junit report" in (proc.stdout + proc.stderr).lower()


# ------------------------------------------------------------------ the local gate, end to end

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=gate-test", "-c", "user.email=gate@test",
                           "-c", "core.autocrlf=false", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _tiny_repo(tmp_path: Path, test_body: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "tests" / "test_tiny.py").write_text(test_body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tiny")
    return repo, _git(repo, "rev-parse", "HEAD")


GREEN = ("import os\n\n"
         "def test_env():\n"
         "    assert os.environ['CI'] == 'true'\n"
         "    assert os.environ['MESHY_PG_BIN'] == os.environ['TINY_EXPECT_PG_BIN']\n"
         "    assert os.environ['MESHY_TEMPLATE_DSN'] == "
         "os.environ['TINY_EXPECT_TEMPLATE_DSN']\n\n"
         "def test_two():\n"
         "    assert 1 + 1 == 2\n")


#: What the tiny runs are given as their template server. Nothing connects to it (the tiny
#: repositories have no conftest); the tests read it back from the child's environment.
GATE_TEMPLATE_DSN = "postgresql://gate:gate-secret@127.0.0.1:1/postgres"


def _run_gate(repo: Path, commit: str, out: Path, pg_bin: Path | None = None,
              template_dsn: str | None = GATE_TEMPLATE_DSN,
              extra_args: tuple[str, ...] = (),
              **extra_env: str) -> subprocess.CompletedProcess:
    """The gate as the integrator's shell would run it: this suite's own process state
    (DJANGO_SETTINGS_MODULE set by the modules imported so far, PYTEST_CURRENT_TEST) is
    inherited on purpose, because the gate must not depend on a clean parent. The shell's
    MESHY_TEMPLATE_DSN (this run's own) is dropped: `template_dsn` goes in as the flag, and
    a test that wants the environment path sets MESHY_TEMPLATE_DSN in `extra_env`."""
    args = [sys.executable, str(LOCAL_GATE), "--repo", str(repo), "--commit", commit,
            "--out", str(out), *extra_args]
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "a.module.the.tiny.repository.does.not.have"
    env.pop("MESHY_TEMPLATE_DSN", None)
    # The box that certifies main has this set (the gate passes it through to the suite), so a
    # tiny run must not inherit it: a test that wants it names it in extra_env.
    env.pop("MESHY_BISCUIT_CONFORMANCE_BIN", None)
    env.update(extra_env)
    env.pop("MESHY_PG_BIN", None)
    if pg_bin is not None:
        args += ["--pg-bin", str(pg_bin)]
        env["TINY_EXPECT_PG_BIN"] = str(pg_bin)
    if template_dsn is not None:
        args += ["--template-dsn", template_dsn]
    expected = template_dsn if template_dsn is not None else env.get("MESHY_TEMPLATE_DSN")
    if expected is not None:
        env["TINY_EXPECT_TEMPLATE_DSN"] = expected
    return subprocess.run(args, capture_output=True, text=True, timeout=300, env=env)


def _verdict(proc: subprocess.CompletedProcess) -> str:
    lines = proc.stdout.strip().splitlines()
    assert lines, proc.stderr
    return lines[-1]


def test_the_local_gate_passes_a_green_run_and_names_the_commit(tmp_path):
    repo, sha = _tiny_repo(tmp_path, GREEN)
    out = tmp_path / "out"
    proc = _run_gate(repo, sha, out, pg_bin=tmp_path)
    line = _verdict(proc)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert line.startswith(f"LOCAL GATE PASS {sha}"), line
    for name in ("collect.txt", "suite.txt", "suite.exit", "junit.xml"):
        assert (out / name).is_file(), f"the gate keeps {name}"
    assert (out / "suite.exit").read_text(encoding="utf-8").strip() == "0"
    summary = [ln.strip("= ").strip() for ln in
               (out / "suite.txt").read_text(encoding="utf-8").splitlines()
               if re.search(r"\d+ passed", ln)][-1]
    assert summary in line, "the verdict quotes pytest's own summary line"
    assert "2 passed" in line


def test_the_local_gate_refuses_to_start_without_a_template_dsn(tmp_path):
    """Decision 41: no --template-dsn and no MESHY_TEMPLATE_DSN in the gate's environment is
    REFUSED before anything runs, naming the variable. Mutant D41-M2 (the refusal removed)
    runs the suite and passes it."""
    repo, sha = _tiny_repo(tmp_path, GREEN)
    out = tmp_path / "out"
    proc = _run_gate(repo, sha, out, pg_bin=tmp_path, template_dsn=None)
    line = _verdict(proc)
    assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
    assert "MESHY_TEMPLATE_DSN is not set" in line and "--template-dsn" in line, line
    assert not (out / "collect.txt").exists() and not (out / "suite.txt").exists(), \
        "a refused gate runs nothing"
    assert "template DSN (from" not in proc.stdout, "nothing to print when nothing is used"


def test_the_local_gate_takes_the_template_dsn_from_its_environment_and_prints_it(tmp_path):
    """Control for the refusal above: the environment is the other accepted source. The value
    used is printed with the password masked, before the verdict; and the flag wins over the
    environment (the tiny test asserts the value the suite received)."""
    repo, sha = _tiny_repo(tmp_path, GREEN)
    from_env = "postgresql://gate:env-secret@127.0.0.1:2/postgres"
    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path, template_dsn=None,
                     MESHY_TEMPLATE_DSN=from_env)
    line = _verdict(proc)
    assert proc.returncode == 0 and "2 passed" in line, proc.stdout + proc.stderr
    assert ("LOCAL GATE template DSN (from MESHY_TEMPLATE_DSN): "
            "postgresql://gate:***@127.0.0.1:2/postgres") in proc.stdout, proc.stdout
    assert "env-secret" not in proc.stdout + proc.stderr, "the password was printed"

    proc = _run_gate(repo, sha, tmp_path / "out2", pg_bin=tmp_path,
                     MESHY_TEMPLATE_DSN=from_env)             # flag GATE_TEMPLATE_DSN too
    line = _verdict(proc)
    assert proc.returncode == 0 and "2 passed" in line, proc.stdout + proc.stderr
    assert ("LOCAL GATE template DSN (from --template-dsn): "
            "postgresql://gate:***@127.0.0.1:1/postgres") in proc.stdout, proc.stdout


def test_the_local_gate_refuses_when_the_conformance_tool_is_required_and_not_named(tmp_path):
    """memory-bxxxxx's ruling of 2026-09-29: the two T12 interop tests are NOT on ALLOWED_SKIPS
    (they are the only proof that a Python-minted token verifies under the conformance tool), so
    the box that certifies main passes --require-conformance-bin, and a gate that could only
    skip them refuses BEFORE running anything. Mutant CB-M1 (the refusal removed) runs the
    suite and passes it."""
    repo, sha = _tiny_repo(tmp_path, GREEN)
    out = tmp_path / "out"
    proc = _run_gate(repo, sha, out, pg_bin=tmp_path, extra_args=("--require-conformance-bin",))
    line = _verdict(proc)
    assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
    assert "MESHY_BISCUIT_CONFORMANCE_BIN is not set" in line, line
    assert not (out / "collect.txt").exists() and not (out / "suite.txt").exists(), \
        "a refused gate runs nothing"


def test_the_local_gate_refuses_a_conformance_tool_that_is_not_a_file(tmp_path):
    """Naming the variable is not enough: it must name a file. Mutant CB-M2 (the is_file check
    removed) lets a typo in the path through to a suite that then skips."""
    repo, sha = _tiny_repo(tmp_path, GREEN)
    out = tmp_path / "out"
    proc = _run_gate(repo, sha, out, pg_bin=tmp_path, extra_args=("--require-conformance-bin",),
                     MESHY_BISCUIT_CONFORMANCE_BIN=str(tmp_path / "no-such-tool.exe"))
    line = _verdict(proc)
    assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
    assert "names no file" in line, line
    assert not (out / "suite.txt").exists(), "a refused gate runs nothing"


def test_the_local_gate_runs_with_the_conformance_tool_required_and_prints_which_one(tmp_path):
    """The control for the two refusals above (one variable apart: the file exists), and the
    line that makes a PASS name the tool it ran with: path and sha256, before anything runs.
    Mutant CB-M3 (the line not printed) leaves a PASS that cannot say which tool certified it."""
    import hashlib
    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"not a tool: the tiny suite never runs it")
    repo, sha = _tiny_repo(tmp_path, GREEN)
    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path,
                     extra_args=("--require-conformance-bin",),
                     MESHY_BISCUIT_CONFORMANCE_BIN=str(tool))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _verdict(proc).startswith(f"LOCAL GATE PASS {sha}"), _verdict(proc)
    digest = hashlib.sha256(tool.read_bytes()).hexdigest()
    assert (f"LOCAL GATE conformance tool (MESHY_BISCUIT_CONFORMANCE_BIN): {tool} sha256 {digest}"
            in proc.stdout.splitlines()), proc.stdout


def test_the_conformance_tool_is_required_only_when_the_flag_says_so(tmp_path):
    """Without the flag an unset variable is not a refusal (a box that does not certify main
    can still run the gate), and nothing is printed about a tool that was not named."""
    repo, sha = _tiny_repo(tmp_path, GREEN)
    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "conformance tool" not in proc.stdout, proc.stdout


def test_the_printed_dsn_masks_the_password_in_both_forms():
    shown = _local_gate_module().shown_dsn
    assert shown("postgresql://meshychat_owner:pw@127.0.0.1:5434/postgres") == \
        "postgresql://meshychat_owner:***@127.0.0.1:5434/postgres"
    assert shown("postgresql://meshychat_owner@127.0.0.1:5434/postgres") == \
        "postgresql://meshychat_owner@127.0.0.1:5434/postgres"     # control: nothing to mask
    assert shown("host=127.0.0.1 port=5434 user=meshychat_owner password=pw dbname=postgres") == \
        "host=127.0.0.1 port=5434 user=meshychat_owner password=*** dbname=postgres"


def test_the_local_gate_runs_the_whole_suite_whatever_the_shell_exported(tmp_path):
    """PYTEST_ADDOPTS="-k ..." narrows BOTH the collect and the run, so the collected-count
    check cannot see it: the gate must not inherit it."""
    repo, sha = _tiny_repo(tmp_path, GREEN)
    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path,
                     PYTEST_ADDOPTS="-k test_two", PYTEST_PLUGINS="no_such_plugin")
    line = _verdict(proc)
    assert proc.returncode == 0 and "2 passed" in line, line


def test_the_local_gate_refuses_a_failing_run(tmp_path):
    repo, sha = _tiny_repo(tmp_path, "def test_a():\n    pass\n\ndef test_b():\n"
                                     "    assert False\n")
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
    assert "1 failed" in line, line
    assert (tmp_path / "out" / "suite.exit").read_text(encoding="utf-8").strip() == "1"


def test_the_local_gate_refuses_an_unlisted_skip(tmp_path):
    repo, sha = _tiny_repo(tmp_path, "import pytest\n\ndef test_a():\n"
                                     "    pytest.skip('pg_dump not found')\n")
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 1 and "REFUSED" in line and "skip" in line.lower(), line


@pytest.mark.parametrize("how", ["tracked", "untracked"])
def test_the_local_gate_refuses_a_dirty_tree_and_runs_nothing(tmp_path, how):
    repo, sha = _tiny_repo(tmp_path, GREEN)
    if how == "tracked":
        with open(repo / "tests" / "test_tiny.py", "a", encoding="utf-8") as f:
            f.write("\n# edited\n")
    else:
        (repo / "stray.txt").write_text("x", encoding="utf-8")
    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path)
    line = _verdict(proc)
    assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
    assert "dirty" in line.lower(), line
    assert not (tmp_path / "out" / "suite.txt").exists(), "a refused tree runs nothing"


def test_the_local_gate_refuses_a_head_that_is_not_the_commit(tmp_path):
    repo, first = _tiny_repo(tmp_path, GREEN)
    (repo / "tests" / "test_more.py").write_text("def test_c():\n    pass\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    proc = _run_gate(repo, first, tmp_path / "out", pg_bin=tmp_path)
    line = _verdict(proc)
    assert proc.returncode == 1 and "REFUSED" in line and "HEAD" in line, line
    assert not (tmp_path / "out" / "suite.txt").exists()


# Review 2026-09-25, finding 2: state outside the commit that `git status` does not report
# turned a failing run into a PASS. Each probe starts from a commit whose test_a FAILS, so a
# PASS is the gate being fooled; each has its control beside it.

RED_COMMIT = "def test_a():\n    assert 1 == 2\n\ndef test_b():\n    pass\n"
FLIPPER = ("import pytest\n\n@pytest.hookimpl(hookwrapper=True)\n"
           "def pytest_runtest_makereport(item, call):\n"
           "    out = yield\n    r = out.get_result()\n"
           "    if r.failed:\n        r.outcome = 'passed'\n")


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_the_local_gate_refuses_an_index_flag_that_hides_a_change(tmp_path, flag):
    repo, sha = _tiny_repo(tmp_path, RED_COMMIT)
    _git(repo, "update-index", flag, "tests/test_tiny.py")
    (repo / "tests" / "test_tiny.py").write_text(RED_COMMIT.replace("1 == 2", "2 == 2"),
                                                 encoding="utf-8")
    assert _git(repo, "status", "--porcelain") == "", "control: status does not show it"
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
    assert "tests/test_tiny.py" in line and not (tmp_path / "out" / "suite.txt").exists(), line


def test_the_local_gate_refuses_an_ignored_conftest(tmp_path):
    repo, sha = _tiny_repo(tmp_path, RED_COMMIT)
    (repo / "tests" / "conftest.py").write_text(FLIPPER, encoding="utf-8")
    with open(repo / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
        f.write("tests/conftest.py\n")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == "", "control"
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 1 and "REFUSED" in line and "tests/conftest.py" in line, line
    assert not (tmp_path / "out" / "suite.txt").exists(), "a refused tree runs nothing"


def test_the_local_gate_is_not_steered_by_the_shells_python_variables(tmp_path):
    """PYTHONPATH pointing at a sitecustomize.py that sets PYTEST_PLUGINS after the gate
    stripped it: the plugin flips the failure. The gate must run the suite without it."""
    repo, sha = _tiny_repo(tmp_path, RED_COMMIT)
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "sitecustomize.py").write_text(
        "import os\nos.environ['PYTEST_PLUGINS'] = 'flipper'\n", encoding="utf-8")
    (evil / "flipper.py").write_text(FLIPPER, encoding="utf-8")
    proc = _run_gate(repo, sha, tmp_path / "out", PYTHONPATH=str(evil),
                     PYTHONSTARTUP=str(evil / "sitecustomize.py"), PYTHONUSERBASE=str(evil))
    line = _verdict(proc)
    assert proc.returncode == 1 and "1 failed" in line, line


def test_the_local_gate_does_not_load_bytecode_left_in_the_tree(tmp_path):
    """A pyc of a PASSING test_tiny.py (same size, same mtime as the committed failing one)
    in the ignored tests/__pycache__: Python and pytest's assertion rewriter trust a pyc
    whose recorded source size and mtime are equal, so the committed source never runs."""
    repo, sha = _tiny_repo(tmp_path, RED_COMMIT)
    src = repo / "tests" / "test_tiny.py"
    stamp = src.stat().st_mtime
    src.write_text(RED_COMMIT.replace("1 == 2", "2 == 2"), encoding="utf-8")
    os.utime(src, (stamp, stamp))
    # The warm-up runs as a plain shell would, without this suite's DJANGO_SETTINGS_MODULE
    # (set by modules imported earlier in a full run; pytest-django then looks for a Django
    # project in the tiny repository) or bytecode settings that would prevent
    # this control from creating its deliberately stale cache. Postbox imports
    # set PYTHONDONTWRITEBYTECODE, so this must also work after the postbox tests.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTEST_")
           and k.upper() not in ("DJANGO_SETTINGS_MODULE", "PYTHONPYCACHEPREFIX",
                                 "PYTHONDONTWRITEBYTECODE")}
    warm = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p",
                           "no:cacheprovider"], cwd=repo, env=env, capture_output=True,
                          text=True, timeout=120)
    assert warm.returncode == 0, warm.stdout                    # control: the pyc passes
    src.write_text(RED_COMMIT, encoding="utf-8")
    os.utime(src, (stamp, stamp))
    assert list((repo / "tests" / "__pycache__").glob("test_tiny*.pyc")), "control: a pyc"
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == "", "control"
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 1 and "1 failed" in line, line


def test_the_local_gate_refuses_an_out_directory_inside_the_repository(tmp_path):
    repo, sha = _tiny_repo(tmp_path, GREEN)
    proc = _run_gate(repo, sha, repo / "gate-out", pg_bin=tmp_path)
    assert proc.returncode == 1 and "REFUSED" in _verdict(proc)
    assert not (repo / "gate-out").exists()


#: A stand-in for tools/mutants/run.py in a tiny repository: it answers only `--check --rev SHA --repo R`
#: and records its argv OUTSIDE the tree (a file inside would dirty it), exiting with TINY_MUTANTS_RC.
MUTANTS_STUB = ("import os, sys\n"
                "open(os.environ['TINY_MUTANTS_ARGV'], 'w', encoding='utf-8').write('\\n'.join(sys.argv[1:]))\n"
                "if os.environ.get('TINY_MUTANTS_ENV'):\n"
                "    open(os.environ['TINY_MUTANTS_ENV'], 'w', encoding='utf-8').write('\\n'.join(sorted(\n"
                "        k for k in os.environ if k.upper().startswith(('PYTEST_', 'PYTHONPATH', 'PYTHONSTARTUP')))))\n"
                "rc = int(os.environ['TINY_MUTANTS_RC'])\n"
                "if rc:\n"
                "    print('BLIND    [lst] mut:X1 a moved line | the original occurs 0 times in a.py at abc')\n"
                "print('check at abc: 2 mutants, %d apply (the original occurs once), %d BLIND' % (2 - rc, rc))\n"
                "sys.exit(rc)\n")


@pytest.mark.parametrize("rc", [1, 0])
def test_the_local_gate_runs_the_mutant_check_before_the_suite(tmp_path, rc):
    """REFACTOR-QUEUE A5: a BLIND mutant (its original text gone from the code) refuses the gate before the
    suite runs, with the entry's name in the verdict; a clean check lets the run go on."""
    repo, _ = _tiny_repo(tmp_path, GREEN)
    (repo / "tools" / "mutants").mkdir(parents=True)
    (repo / "tools" / "mutants" / "run.py").write_text(MUTANTS_STUB, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a mutant runner")
    sha = _git(repo, "rev-parse", "HEAD")
    argv = tmp_path / "mutants-argv.txt"
    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path,
                     TINY_MUTANTS_RC=str(rc), TINY_MUTANTS_ARGV=str(argv))
    line = _verdict(proc)
    assert argv.read_text(encoding="utf-8").split("\n") == ["--check", "--rev", sha, "--repo", str(repo.resolve())]
    if rc:
        assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
        assert "1 BLIND mutant(s)" in line and "mut:X1 a moved line" in line, line
        assert not (tmp_path / "out" / "suite.txt").exists(), "a refused list runs nothing"
    else:
        assert proc.returncode == 0 and line.startswith(f"LOCAL GATE PASS {sha}"), proc.stdout + proc.stderr


def test_mutant_lists_without_their_runner_refuse_the_gate(tmp_path):
    """kxxx's review of #645 (P3-1): a commit that moves or renames run.py must not switch the check off while
    the lists are still there. Control: a repository with neither runs as before (every other test here)."""
    repo, _ = _tiny_repo(tmp_path, GREEN)
    (repo / "tools" / "mutants" / "lists").mkdir(parents=True)
    (repo / "tools" / "mutants" / "lists" / "a_list.py").write_text("MUTANTS = []\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "lists, no runner")
    sha = _git(repo, "rev-parse", "HEAD")
    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path)
    line = _verdict(proc)
    assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
    assert "tools/mutants/run.py is missing" in line, line
    assert not (tmp_path / "out" / "suite.txt").exists()


def test_the_mutant_check_runs_in_the_suites_cleaned_environment(tmp_path):
    """kxxx's review of #645 (P3-2): a PYTHONPATH or PYTEST_* in the gate runner's shell reaches neither the suite
    nor the mutant check."""
    repo, _ = _tiny_repo(tmp_path, GREEN)
    (repo / "tools" / "mutants").mkdir(parents=True)
    (repo / "tools" / "mutants" / "run.py").write_text(MUTANTS_STUB, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a mutant runner")
    sha = _git(repo, "rev-parse", "HEAD")
    seen = tmp_path / "mutants-env.txt"
    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path, TINY_MUTANTS_RC="0",
                     TINY_MUTANTS_ARGV=str(tmp_path / "argv.txt"), TINY_MUTANTS_ENV=str(seen),
                     PYTHONPATH=str(tmp_path / "nowhere"), PYTEST_ADDOPTS="-k nothing_at_all")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert seen.read_text(encoding="utf-8") == "", seen.read_text(encoding="utf-8")


def test_the_mutant_check_reads_the_committed_list_not_a_stale_bytecode_cache(tmp_path):
    """The Codex review of #645 (P2): `-B` and PYTHONDONTWRITEBYTECODE stop cache writes, not reads, and the
    tree check allows an ignored __pycache__, so a stale .pyc whose recorded size and mtime match the committed
    list would be checked in its place. Here the cached list's original occurs once (it applies) and the
    committed one's occurs nowhere (BLIND), at the same length and mtime. The REAL tools/mutants/run.py runs, so
    the list is imported the way load_lists imports it.

    Control: the same runner in a cleaned environment with no cache prefix of its own reads the stale bytecode
    (exit 0, nothing BLIND), so the refusal below comes from the gate's fresh prefix, not from a cache that was
    never going to load."""
    import py_compile
    repo, _ = _tiny_repo(tmp_path, GREEN)
    (repo / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    lists = repo / "tools" / "mutants" / "lists"
    lists.mkdir(parents=True)
    runner = repo / "tools" / "mutants" / "run.py"
    runner.write_bytes((ROOT / "tools" / "mutants" / "run.py").read_bytes())
    src = lists / "stale.py"
    entry = 'MUTANTS = [("stale:1 the value", "a.py", "VALUE = %s", "VALUE = 2", ["tests/test_tiny.py"])]\n'
    src.write_bytes((entry % "1").encode("utf-8"))
    # Built by hand: inside the gate the suite runs with PYTHONPYCACHEPREFIX set, and py_compile's default
    # cfile would then go under that prefix, leaving the tree with no stale cache to find.
    cfile = lists / "__pycache__" / f"stale.{sys.implementation.cache_tag}.pyc"
    py_compile.compile(str(src), cfile=str(cfile), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    st = src.stat()
    src.write_bytes((entry % "9").encode("utf-8"))        # same length; "VALUE = 9" occurs nowhere in a.py
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert src.stat().st_size == st.st_size and int(src.stat().st_mtime) == int(st.st_mtime)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a mutant list, and a stale cache of an older version of it")
    sha = _git(repo, "rev-parse", "HEAD")
    assert "!! tools/mutants/lists/__pycache__/" in _git(repo, "status", "--porcelain", "--ignored").splitlines()

    bare = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTEST_", "PYTHON"))}
    control = subprocess.run([sys.executable, "-B", str(runner), "--check", "--rev", sha, "--repo", str(repo)],
                             cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env=dict(bare, PYTHONDONTWRITEBYTECODE="1"), timeout=300)
    assert control.returncode == 0 and "0 BLIND" in control.stdout, \
        "control: without a prefix of its own the runner must read the stale cache, or this test proves nothing\n" \
        + control.stdout + control.stderr

    proc = _run_gate(repo, sha, tmp_path / "out", pg_bin=tmp_path)
    line = _verdict(proc)
    assert proc.returncode == 1 and line.startswith(f"LOCAL GATE REFUSED {sha}"), line
    assert "1 BLIND mutant(s)" in line and "stale:1 the value" in line, line
    assert not (tmp_path / "out" / "suite.txt").exists(), "a refused list runs nothing"


# ------------------------------------------------------------------ the verdict, from the files

def _files(out: Path, exit_code: str, summary: str, junit_cases, collected: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "suite.exit").write_text(exit_code + "\n", encoding="utf-8")
    (out / "suite.txt").write_text(f"....\n{summary}\n", encoding="utf-8")
    (out / "collect.txt").write_text(f"tests/x.py::a\n\n{collected} tests collected in 0.1s\n",
                                     encoding="utf-8")
    _junit(out, junit_cases)


def test_the_verdict_is_read_from_the_files(tmp_path):
    gate = _local_gate_module()
    three = [("a", None, None), ("b", None, None), ("c", None, None)]
    _files(tmp_path / "ok", "0", "3 passed in 0.10s", three, 3)
    assert gate.decide(tmp_path / "ok") == ([], "3 passed in 0.10s")            # control
    _files(tmp_path / "exit1", "1", "3 passed in 0.10s", three, 3)
    problems, _ = gate.decide(tmp_path / "exit1")
    assert any("exit code 1" in p for p in problems), problems
    _files(tmp_path / "sumfail", "0", "1 failed, 2 passed in 0.10s", three, 3)
    problems, _ = gate.decide(tmp_path / "sumfail")
    assert any("failed" in p for p in problems), problems
    _files(tmp_path / "count", "0", "2 passed in 0.10s", three, 3)
    problems, _ = gate.decide(tmp_path / "count")
    assert any("passed" in p and "3" in p for p in problems), problems
    _files(tmp_path / "nosum", "0", "no tests ran in 0.01s", three, 3)
    problems, _ = gate.decide(tmp_path / "nosum")
    assert any("summary" in p for p in problems), problems
    (tmp_path / "ok" / "suite.exit").unlink()
    problems, _ = gate.decide(tmp_path / "ok")
    assert any("suite.exit" in p for p in problems), problems


def test_the_exempt_skip_is_allowed_only_to_the_test_that_owns_it(tmp_path):
    """Review 2026-09-25, finding 5: `^exempt: ` was an open prefix, so any test that called
    pytest.skip("exempt: anything") passed the gate. It is allowed to the one test that
    skips with it, for the one module it exempts."""
    skip = "import pytest\n\ndef test_a():\n    pytest.skip('exempt: anything')\n"
    repo, sha = _tiny_repo(tmp_path, skip)
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 1 and "REFUSED" in line and "skip" in line.lower(), line


def test_the_exempt_skip_passes_where_it_belongs(tmp_path):
    """Control for the test above: the same skip, from the test and parameter the allow-list
    names, passes."""
    owner = ("import pytest\n\n@pytest.mark.parametrize('module', ['apps.core.sqlmigrate'])\n"
             "def test_each_opener_binds_the_role_or_is_named_exempt(module):\n"
             "    pytest.skip('exempt: the migrator CLI')\n\ndef test_b():\n    pass\n")
    repo, sha = _tiny_repo(tmp_path, owner)
    (repo / "tests" / "test_tiny.py").rename(repo / "tests" / "test_every_connection_is_role_bound.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename")
    sha = _git(repo, "rev-parse", "HEAD")
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 0 and "1 passed, 1 skipped" in line, line


def test_every_allowed_skip_names_a_skip_the_suite_has():
    """An allow-list entry nothing needs is a hole waiting for a test to fall into."""
    allowed = _local_gate_module().ALLOWED_SKIPS
    assert allowed, "control: the gate states its allowed skips"
    for where, pattern in allowed:
        module_re, func_re = where.split("::", 1)
        if module_re.startswith(r"tools\."):
            # tools/ tests run too (TEST_PATHS). Their entries name a literal module path, then a class: the file
            # is the longest dotted prefix that exists as a .py under the repository.
            parts = module_re.replace("\\.", ".").split(".")
            mods = [ROOT.joinpath(*parts[:i]).with_suffix(".py") for i in range(len(parts), 1, -1)
                    if ROOT.joinpath(*parts[:i]).with_suffix(".py").is_file()][:1]
            assert mods, f"no test module under tools/ matches {where!r}"
        else:
            assert module_re.startswith(r"tests\."), f"{where!r} names no module under tests/ or tools/"
            mods = [m for m in (ROOT / "tests").glob("*.py")
                    if re.fullmatch(module_re.removeprefix(r"tests\."), m.stem)]
        assert mods, f"no test module matches {where!r}"
        literal = pattern.strip("^$")
        assert literal and all(literal in m.read_text(encoding="utf-8") for m in mods), (
            f"a module {where!r} names does not skip for {pattern!r}")
        func = func_re.split(r"\[")[0]
        if func != ".+":
            # The name may be a pattern (test_\w+ for a whole unittest class): match it against real definitions.
            assert all(re.search(r"\bdef " + func + r"\(", m.read_text(encoding="utf-8")) for m in mods), func


def test_the_gate_runs_the_tests_under_tools_too(tmp_path):
    """Until 17 Nov 2026 the gate ran `pytest tests/` only, so the 245 tests under tools/ were in no run: a red
    test there passed the gate. A failing test under tools/ now refuses it, and a green one is counted."""
    repo, _ = _tiny_repo(tmp_path, "def test_tiny():\n    pass\n")
    (repo / "tools" / "thing").mkdir(parents=True)
    (repo / "tools" / "thing" / "test_thing.py").write_text(RED_COMMIT, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a red test under tools/")
    sha = _git(repo, "rev-parse", "HEAD")
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 1 and "1 failed, 2 passed" in line, line
    (repo / "tools" / "thing" / "test_thing.py").write_text(RED_COMMIT.replace("1 == 2", "2 == 2"), encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "green")
    sha = _git(repo, "rev-parse", "HEAD")
    proc = _run_gate(repo, sha, tmp_path / "out2")
    line = _verdict(proc)
    assert proc.returncode == 0 and "3 passed" in line, line


def test_a_tree_without_tools_is_gated_on_tests_alone(tmp_path):
    """The gated tree may have no tools/ (the tiny repositories here): the gate names only the paths it has, since
    pytest refuses a run that names a missing directory."""
    repo, sha = _tiny_repo(tmp_path, "def test_tiny():\n    pass\n")
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 0 and "1 passed" in line, line


@pytest.mark.parametrize("summary, want", [
    ("226 passed, 19 skipped, 991 subtests passed in 99.72s",
     {"passed": 226, "skipped": 19, "subtests passed": 991}),
    ("10 passed, 2 subtests failed in 3.10s", {"passed": 10, "failed": 2}),
    ("12956 passed, 119 skipped, 4 warnings in 6970.44s (1:56:10)",
     {"passed": 12956, "skipped": 119, "warning": 4}),
])
def test_a_summary_with_subtests_is_read_and_a_failed_subtest_counts_as_failed(summary, want):
    """pytest 9 counts unittest subTests in the summary (#662, GitHub Codex P1): the line must still match, and a
    failed subtest must refuse the gate like a failed test."""
    gate = _local_gate_module()
    assert gate._SUMMARY.match(summary), summary
    assert gate.summary_counts(summary) == want


def test_a_real_subtest_failure_refuses_the_gate(tmp_path):
    """End to end: a unittest subTest that fails gives a summary with 'subtests failed', and the gate refuses."""
    body = ("import unittest\n\nclass T(unittest.TestCase):\n    def test_many(self):\n"
            "        for i in range(3):\n            with self.subTest(i=i):\n                self.assertNotEqual(i, 1)\n")
    repo, sha = _tiny_repo(tmp_path, body)
    proc = _run_gate(repo, sha, tmp_path / "out")
    line = _verdict(proc)
    assert proc.returncode == 1 and "failed" in line, line
