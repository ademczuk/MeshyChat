"""FAST mode of the local gate (tools/ci/local_gate.py --fast; kxxx, 9 Nov 2026, on memory-bxxxxx's
design note in <thread>). No database: this file is on tests/no_db.txt itself.

What it pins, each refusal beside its passing control:
  * eligibility is an allow-list of client paths and fails closed: the gate's own code, schema,
    settings, manage.py, conftest, shared fixtures, any dependency or lock file and any path the
    list doesn't know make a PR need the full gate;
  * part B picks the PR's own test files and every test file that NAMES a changed file, by path
    (spec_from_file_location, a script path, a Windows path) or dotted module (import, runpy,
    `python -m`), not only by import; an unrelated test file is not picked;
  * tests/no_db.txt: the real list is clean, every entry exists, and the list is not empty; a
    missing, renamed, duplicated or non-test entry is a problem, and the FULL gate refuses it;
  * an absent list refuses fast mode only, so the full gate still runs a repository without one.

Run:  .venv/Scripts/python.exe -m pytest tests/test_local_gate_fast.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOCAL_GATE = ROOT / "tools" / "ci" / "local_gate.py"


@pytest.fixture(scope="module")
def lg():
    spec = importlib.util.spec_from_file_location("local_gate_fast_under_test", LOCAL_GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("path", [
    "tools/meshy_node/board_tasks.py",
    "tools/postbox/postbox.py",
    "tools/postbox/mod/meshy-mod/hooks/meshy.js",
    "tools/transcript_cards/load_hook.py",
    "docs/client-update.md",
    "tests/test_meshy_node_board_tasks.py",
    "README.md",
])
def test_client_paths_are_fast_eligible(lg, path):
    assert lg.fast_blockers([path]) == []


@pytest.mark.parametrize("path", [
    "tools/ci/local_gate.py",                          # the gate never judges a change to itself
    "tools/ci/junit_gate.py",
    "apps/mesh/views.py",
    "sql/001_base.sql",
    "sql_meshychat/057_cards_head.sql",
    "meshychat/settings.py",
    "manage.py",
    "tests/conftest.py",
    "tests/fixtures/hub_presence_db.py",
    "tests/no_db.txt",                                 # changing the list itself needs the full gate
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "tools/postbox/mod/meshy-mod/package.json",        # a lock or dependency file under the mod
    "tools/postbox/mod/meshy-mod/package-lock.json",
    "tools/backup/run.py",                             # not on the client allow-list: fail closed
    "Makefile",
    "deploy/compose.yml",
])
def test_everything_else_needs_the_full_gate(lg, path):
    assert lg.fast_blockers([path]) == [path]


def test_one_server_path_makes_the_whole_pr_full(lg):
    paths = ["tools/meshy_node/cli.py", "tests/test_meshy_node_cli.py", "apps/files/views.py"]
    assert lg.fast_blockers(paths) == ["apps/files/views.py"]


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_part_b_picks_tests_that_name_a_changed_file_without_importing_it(lg, tmp_path):
    repo = _tree(tmp_path, {
        "tools/postbox/bank_fault.py": "",
        "tests/test_imports.py": "from tools.postbox import bank_fault\n",
        "tests/test_spec_loader.py": 'spec_from_file_location("b", ROOT / "tools/postbox/bank_fault.py")\n',
        "tests/test_windows_path.py": 'p = "tools\\\\postbox\\\\bank_fault.py"\n',
        "tests/test_runpy.py": 'runpy.run_module("tools.postbox.bank_fault")\n',
        "tests/test_dash_m.py": 'subprocess.run([sys.executable, "-m", "tools.postbox.bank_fault"])\n',
        "tests/test_unrelated.py": "import tools.meshy_node.cli\n",
        "tests/test_own.py": "",
    })
    tests = sorted(p.relative_to(repo).as_posix() for p in (repo / "tests").glob("test_*.py"))
    picked = lg.part_b_files(repo, ["tools/postbox/bank_fault.py", "tests/test_own.py"], tests)
    assert picked == ["tests/test_dash_m.py", "tests/test_imports.py", "tests/test_own.py",
                      "tests/test_runpy.py", "tests/test_spec_loader.py", "tests/test_windows_path.py"]


def test_a_package_init_is_named_by_its_package(lg, tmp_path):
    repo = _tree(tmp_path, {"tools/meshy_node/__init__.py": "",
                            "tests/test_pkg.py": "import tools.meshy_node\n"})
    assert lg.part_b_files(repo, ["tools/meshy_node/__init__.py"], ["tests/test_pkg.py"]) == ["tests/test_pkg.py"]


def test_part_b_picks_real_sender_alias_for_spelled_filename(lg):
    tests = sorted(p for p in subprocess.check_output(
        ["git", "ls-files", "tests/"], cwd=ROOT, text=True).splitlines()
        if lg._TEST_FILE.fullmatch(p))
    picked = lg.part_b_files(ROOT, ["tools/postbox/meshy_channel.py"], tests)
    assert "tests/test_sender_alias.py" in picked


def test_part_b_basename_does_not_pick_other_file_or_generic_names(lg, tmp_path):
    repo = _tree(tmp_path, {
        "tools/postbox/meshy_channel.py": "",
        "tools/postbox/other_channel.py": "",
        "tools/postbox/__init__.py": "",
        "tools/postbox/conftest.py": "",
        "tools/postbox/a.py": "",
        "tests/test_target.py": 'p = ROOT / "tools" / "postbox" / "meshy_channel.py"\n',
        "tests/test_other.py": 'p = ROOT / "tools" / "postbox" / "other_channel.py"\n',
        "tests/test_generic.py": 'names = ["__init__.py", "conftest.py", "a.py"]\n',
    })
    tests = sorted(p.relative_to(repo).as_posix() for p in (repo / "tests").glob("test_*.py"))
    assert lg.part_b_files(repo, ["tools/postbox/meshy_channel.py"], tests) == ["tests/test_target.py"]
    assert lg.part_b_files(repo, ["tools/postbox/__init__.py"], tests) == []
    assert lg.part_b_files(repo, ["tools/postbox/conftest.py"], tests) == []
    assert lg.part_b_files(repo, ["tools/postbox/a.py"], tests) == []


def test_the_real_no_db_list_is_clean(lg):
    files, digest, problems = lg.no_db_list(ROOT)
    assert problems == []
    assert len(files) >= 100 and len(digest) == 64
    assert "tests/test_local_gate_fast.py" in files
    assert not {"tests/conftest.py"} & set(files)


@pytest.mark.parametrize("entry,problem", [
    ("tests/test_gone.py", "does not exist"),
    ("tools/meshy_node/cli.py", "is not a tests/test_*.py path"),
    ("tests/conftest.py", "is not a tests/test_*.py path"),
])
def test_a_rotten_entry_is_a_problem(lg, tmp_path, entry, problem):
    repo = _tree(tmp_path, {"tests/no_db.txt": "# header\n\n" + entry + "\n"})
    files, _, problems = lg.no_db_list(repo)
    assert files == [] and len(problems) == 1 and problem in problems[0]


def test_a_duplicate_entry_is_a_problem(lg, tmp_path):
    repo = _tree(tmp_path, {"tests/test_a.py": "",
                            "tests/no_db.txt": "tests/test_a.py\ntests/test_a.py  # again\n"})
    files, _, problems = lg.no_db_list(repo)
    assert files == ["tests/test_a.py"] and len(problems) == 1 and "listed twice" in problems[0]


def test_an_absent_list_refuses_fast_mode_only(lg, tmp_path):
    assert lg.no_db_list(tmp_path)[2] == ["tests/no_db.txt is missing"]
    assert lg.no_db_list(tmp_path, required=False)[2] == []


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _commit_repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = _tree(tmp_path / "r", files)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_fast_require_conformance_refuses_before_dispatch(lg, tmp_path, monkeypatch, capsys):
    repo, sha = _commit_repo(tmp_path, {"tests/test_a.py": "def test_a(): pass\n"})
    monkeypatch.delenv(lg.CONFORMANCE_ENV, raising=False)
    called = []
    monkeypatch.setattr(lg, "fast_gate", lambda *args: called.append(args) or (0, "LOCAL GATE PASS"))
    code = lg.main(["--repo", str(repo), "--commit", sha, "--fast", "--base", sha,
                    "--template-dsn", "postgresql://u@127.0.0.1:1/x", "--require-conformance-bin"])
    assert code == 1 and not called
    assert f"LOCAL GATE REFUSED {sha}: {lg.CONFORMANCE_ENV} is not set" in capsys.readouterr().out


def test_fast_require_conformance_accepts_existing_file(lg, tmp_path, monkeypatch, capsys):
    repo, sha = _commit_repo(tmp_path, {"tests/test_a.py": "def test_a(): pass\n"})
    tool = tmp_path / "conformance-tool"
    tool.write_bytes(b"synthetic tool")
    monkeypatch.setenv(lg.CONFORMANCE_ENV, str(tool))
    called = []
    monkeypatch.setattr(lg, "fast_gate", lambda *args: called.append(args) or (0, "LOCAL GATE PASS"))
    code = lg.main(["--repo", str(repo), "--commit", sha, "--fast", "--base", sha,
                    "--template-dsn", "postgresql://u@127.0.0.1:1/x", "--require-conformance-bin"])
    assert code == 0 and len(called) == 1
    assert str(tool) in capsys.readouterr().out


def test_fast_without_conformance_flag_remains_allowed(lg, tmp_path, monkeypatch):
    repo, sha = _commit_repo(tmp_path, {"tests/test_a.py": "def test_a(): pass\n"})
    monkeypatch.delenv(lg.CONFORMANCE_ENV, raising=False)
    called = []
    monkeypatch.setattr(lg, "fast_gate", lambda *args: called.append(args) or (0, "LOCAL GATE PASS"))
    code = lg.main(["--repo", str(repo), "--commit", sha, "--fast", "--base", sha,
                    "--template-dsn", "postgresql://u@127.0.0.1:1/x"])
    assert code == 0 and len(called) == 1


def test_the_full_gate_refuses_a_list_naming_a_missing_file(lg, tmp_path):
    repo, sha = _commit_repo(tmp_path, {"tests/test_a.py": "def test_a():\n    pass\n",
                                         "tests/no_db.txt": "tests/test_a.py\ntests/test_renamed.py\n"})
    code, line = lg.gate(repo, sha, tmp_path / "out", None, "postgresql://u@127.0.0.1:1/x")
    assert code == 1 and line.startswith("LOCAL GATE REFUSED " + sha)
    assert "tests/test_renamed.py does not exist" in line


def test_fast_mode_refuses_a_server_change_before_running_anything(lg, tmp_path):
    repo, base = _commit_repo(tmp_path, {"tests/test_a.py": "", "tests/no_db.txt": "tests/test_a.py\n"})
    (repo / "apps").mkdir()
    (repo / "apps" / "views.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "server")
    sha = _git(repo, "rev-parse", "HEAD")
    code, line = lg.fast_gate(repo, sha, base, tmp_path / "out", None, "postgresql://u@127.0.0.1:1/x")
    assert code == 1 and "not fast-eligible, run the FULL gate" in line and "apps/views.py" in line
    assert not (tmp_path / "out").exists()                    # nothing ran


@pytest.mark.parametrize("renames", ["true", "false"])
def test_a_move_out_of_server_code_needs_the_full_gate(lg, tmp_path, renames):
    # kxxx, on 2e3a9e3f: with rename detection --name-only lists only the NEW path of a move, so
    # apps/x.py moved to tools/postbox/x.py looked client-only. Both sides must be listed, whatever
    # the box's diff.renames says.
    repo, base = _commit_repo(tmp_path, {"tests/test_a.py": "", "tests/no_db.txt": "tests/test_a.py\n",
                                         "apps/x.py": "x = 1\n"})
    _git(repo, "config", "diff.renames", renames)
    (repo / "tools" / "postbox").mkdir(parents=True)
    _git(repo, "mv", "apps/x.py", "tools/postbox/x.py")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "move")
    sha = _git(repo, "rev-parse", "HEAD")
    code, line = lg.fast_gate(repo, sha, base, tmp_path / "out", None, "postgresql://u@127.0.0.1:1/x")
    assert code == 1 and "not fast-eligible, run the FULL gate" in line and "apps/x.py" in line


def test_fast_part_a_masks_inherited_test_dsn_before_collection(lg, tmp_path, monkeypatch):
    """No-DB collection must never see a caller's live-looking test DSN."""
    repo, base = _commit_repo(tmp_path, {
        "tests/no_db.txt": "tests/test_safe.py\n",
        "tests/test_safe.py": "def test_safe(): pass\n",
        "tests/test_client.py": "# tools.meshy_node.cli\n",
        "tools/meshy_node/cli.py": "version = 1\n",
    })
    (repo / "tools/meshy_node/cli.py").write_text("version = 2\n", encoding="utf-8")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "client")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("MESHY_TEST_DSN", "postgresql://shared@127.0.0.1:5432/sentinel")
    captured = []

    def fake_part(_repo, files, env, _out):
        captured.append((files, dict(env)))
        return [], "1 passed"

    monkeypatch.setattr(lg, "_fast_part", fake_part)
    monkeypatch.setattr(lg, "tree_problems", lambda *_: [])
    template = "postgresql://throwaway@127.0.0.1:5432/template"
    code, _ = lg.fast_gate(repo, head, base, tmp_path / "out", None, template)
    assert code == 0
    part_a = captured[0][1]
    assert part_a["MESHY_NO_TEST_DB"] == "1"
    assert part_a["MESHY_DSN"] == lg.FAST_DEAD_DSN
    assert part_a["MESHY_TEST_DSN"] == lg.FAST_DEAD_DSN
    assert part_a["MESHY_TEMPLATE_DSN"] == lg.FAST_DEAD_DSN
    assert len(captured) == 2
    part_b = captured[1][1]
    assert part_b["MESHY_TEMPLATE_DSN"] == template
    assert part_b.get("MESHY_TEST_DSN") != lg.FAST_DEAD_DSN


def test_fast_part_a_real_conftest_collection_uses_closed_port(lg, tmp_path, monkeypatch):
    """The actual conftest must import before any no-DB tests run; a fake _fast_part misses it."""
    repo, base = _commit_repo(tmp_path, {
        "tests/no_db.txt": "tests/test_safe.py\n",
        "tests/test_safe.py": "def test_safe(): pass\n",
        "tools/meshy_node/cli.py": "version = 1\n",
    })
    (repo / "tools/meshy_node/cli.py").write_text("version = 2\n", encoding="utf-8")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "client")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(lg, "tree_problems", lambda *_: [])

    def real_a(_repo, files, env, _out):
        if env.get("MESHY_NO_TEST_DB") != "1":
            return [], "part B not needed for this seam"
        proc = subprocess.run([sys.executable, "-m", "pytest", "tests/test_local_gate_fast.py",
                               "--collect-only", "-q", "-p", "no:cacheprovider",
                               f"--basetemp={tmp_path / 'nested-pytest'}"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "collected" in proc.stdout or "tests collected" in proc.stdout
        return [], "real conftest collected"

    monkeypatch.setattr(lg, "_fast_part", real_a)
    monkeypatch.setenv("MESHY_TEMPLATE_DSN", "postgresql://shared@127.0.0.1:5432/postgres")
    code, line = lg.fast_gate(repo, head, base, tmp_path / "out", None,
                              "postgresql://throwaway@127.0.0.1:5432/postgres")
    assert code == 0, line
