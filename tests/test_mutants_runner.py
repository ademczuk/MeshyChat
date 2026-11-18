"""Checks for tools/mutants/run.py, the mutation runner: each mutant goes to a throwaway checkout, never in place.
Needs no database.

    pytest tests/test_mutants_runner.py -q -p no:cacheprovider --noconftest

Each check builds a small git repository in a temp folder (a module, its tests, a branch whose tests fail, and a
post-checkout hook that would leave a mark) with a folder of lists beside it, and runs the runner on them as a command,
as an operator would. The runner finds its own file beside these checks, so a mutant of run.py made in a throwaway
checkout is the one these checks run. tools/mutants/lists/mutants_runner.py holds the mutants of run.py they catch:

    python tools/mutants/run.py --list mutants_runner
"""
from __future__ import annotations

import concurrent.futures
import os
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tools", "mutants", "run.py")

CALC = '''READY = True


def add(a, b):
    return a + b


def double(x):
    return x * 2


def wait():
    return "done"
'''
TEST_CALC = '''import os

import pytest

import calc

# Read when the tests are collected: a mutant of numbers.txt breaks the collection without naming calc.py.
FIVE = int(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "numbers.txt")).read())


@pytest.fixture
def ready():
    if not calc.READY:
        raise RuntimeError("calc is not ready")


def test_add(ready):
    assert calc.add(2, 3) == 5


def test_double_of_zero():
    assert calc.double(0) == 0


def test_wait():
    assert calc.wait() == "done"
'''
# Run with every mutant: the runner's environment as the fixture's tests see it.
TEST_ENV = '''import os


def test_bytecode_guard_and_no_git_variable():
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"
    prefix = os.environ.get("PYTHONPYCACHEPREFIX", "")
    assert prefix and os.path.isdir(prefix) and not os.listdir(prefix)
    assert not [k for k in os.environ if k.upper().startswith("GIT_")]


def test_no_database_of_the_caller():
    named = ("MESHY_TEST_DSN", "MESHY_TEST_DSN_EXPLICIT", "MESHY_DSN", "DATABASE_URL", "MESHY_CLONE_TEMPLATE",
             "MESHY_LIVE_MODELS", "MESHY_NO_TEST_DB")
    assert [k for k in named if k in os.environ] == []


def test_a_temp_file(tmp_path):
    # pytest's basetemp is the one the runner names, beside the run's bytecode prefix, not pytest's default.
    run_folder = os.path.dirname(os.path.realpath(os.environ["PYTHONPYCACHEPREFIX"]))
    assert os.path.realpath(str(tmp_path.parent)) == os.path.join(run_folder, "basetemp")
    (tmp_path / "scratch.txt").write_text("made under pytest's basetemp")
'''
# A repository hook that does nothing, so a mutant of it can make pytest fail internally (exit 3).
CONFTEST = '''def pytest_runtest_logreport(report):
    return None
'''
TESTS = ["test_calc.py", "test_env.py"]
LIST = '''TESTS = %r
MUTANTS = [
    ("kills: add subtracts", "calc.py", "return a + b", "return a - b", TESTS),
    ("misses: double triples", "calc.py", "return x * 2", "return x * 3", TESTS),
    ("blind: not in the file", "calc.py", "return a * b", "return a / b", TESTS),
    ("blind: three times in the file", "calc.py", "return", "yield", TESTS),
    ("crash: a name that is not there", "calc.py", "return a + b", "return a + nothing_here", TESTS),
]
''' % TESTS
# Mutants whose runs prove different things: none of the ERROR ones may count as caught.
ODD = '''TESTS = %r
MUTANTS = [
    ("interrupt: a test raises KeyboardInterrupt", "calc.py", '    return "done"', "    raise KeyboardInterrupt", TESTS),
    ("internal: a hook of the repository raises", "conftest.py", "    return None",
     '    raise RuntimeError("a hook fails")', TESTS),
    ("syntax: the mutated file does not parse", "calc.py", "def double(x):", "def double(x:", TESTS),
    ("collection: a data file the tests read when collected", "numbers.txt", "5", "five", TESTS),
    ("setup: a fixture fails", "calc.py", "READY = True", "READY = False", TESTS),
]
''' % TESTS
SLOW = '''MUTANTS = [
    ("hang: wait never returns", "calc.py", '    return "done"', '    import time; time.sleep(600); return "done"',
     %r),
]
''' % TESTS
LINE = re.compile(r"^(BLIND|CAUGHT|SURVIVED|ERROR)\s+\[(\w+)\] (.+?) \| (.*)$", re.M)


def env_without_git(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(extra)
    return env


def caller_env(root: str, **extra: str) -> dict[str, str]:
    """The environment the runner is started from: no GIT_* variable, no bytecode guard and a PYTHONPYCACHEPREFIX that
    holds a stale file, so the guard the fixture's tests see can only be the runner's own; and every variable that
    would point the tests at a database of the caller's (test_env.py checks that none arrives)."""
    stale = os.path.join(root, "stale-pyc")
    os.makedirs(stale, exist_ok=True)
    write(os.path.join(stale, "stale.pyc"), "not bytecode")
    env = env_without_git(**{
        # Each names a database other than the one a run makes for itself; none may reach a test run.
        "MESHY_TEST_DSN": "postgresql://nobody@127.0.0.1:1/a_shared_database",
        "MESHY_TEST_DSN_EXPLICIT": "postgresql://nobody@127.0.0.1:1/a_shared_database",
        "MESHY_DSN": "postgresql://nobody@127.0.0.1:1/a_shared_database",
        "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/a_shared_database",
        "MESHY_CLONE_TEMPLATE": "a_shared_database", "MESHY_LIVE_MODELS": "1", **extra})
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPYCACHEPREFIX"] = stale
    return env


def git(cwd: str, *args: str) -> str:
    """git in `cwd`, with no GIT_* variable of whatever runs these checks (a hook's GIT_DIR would redirect it)."""
    p = subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.org",
                        "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                       cwd=cwd, env=env_without_git(), capture_output=True, text=True, check=True)
    return p.stdout


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def snapshot(root: str) -> dict[str, bytes]:
    """Every file under `root`, the .git folder included, by relative path."""
    out = {}
    for folder, _, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def make_repo(root: str, mark: str, sentinel: str) -> str:
    """A repository: main holds calc.py and its passing tests; branch `broken` has a failing test. Its pytest.ini
    points pytest's outputs into `sentinel` (log_file, and addopts naming a basetemp, a log file and a debug file),
    which the runner must override. The post-checkout hook, installed last, writes `mark` whenever a checkout runs
    it."""
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q")
    write(os.path.join(repo, "calc.py"), CALC)
    write(os.path.join(repo, "test_calc.py"), TEST_CALC)
    write(os.path.join(repo, "test_env.py"), TEST_ENV)
    write(os.path.join(repo, "conftest.py"), CONFTEST)
    write(os.path.join(repo, "numbers.txt"), "5\n")
    out = sentinel.replace("\\", "/")
    write(os.path.join(repo, "pytest.ini"), "[pytest]\nlog_file = %s/keep.txt\n"
          "addopts = --basetemp=%s --log-file=%s/keep.txt --debug=%s/debug.log\n" % (out, out, out, out))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "calc")
    git(repo, "checkout", "-q", "-b", "broken")
    write(os.path.join(repo, "test_calc.py"), TEST_CALC.replace("== 5", "== 6"))
    git(repo, "commit", "-q", "-am", "a red test")
    git(repo, "checkout", "-q", "main")
    hook = os.path.join(repo, ".git", "hooks", "post-checkout")
    write(hook, "#!/bin/sh\necho ran > '%s'\n" % mark.replace("\\", "/"))
    os.chmod(hook, 0o755)
    return repo


def sentinel(path: str) -> str:
    """A folder of someone else's, with a file in it, that no run may empty or write into."""
    os.makedirs(path)
    write(os.path.join(path, "keep.txt"), "not pytest's\n")
    return path


def run(root: str, repo: str, lists: str, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    p = subprocess.run([sys.executable, RUNNER, "--repo", repo, "--lists-dir", lists, *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env or caller_env(root),
                       stdin=subprocess.DEVNULL, timeout=600)
    return p.returncode, p.stdout + p.stderr


def verdicts(out: str) -> dict[str, tuple[str, str]]:
    return {m.group(3): (m.group(1), m.group(4)) for m in LINE.finditer(out)}


def verdict_of(out: str, name: str) -> tuple[str, str]:
    """(verdict, the rest of its line) of one mutant; a mutant with no line fails the check's assert, not a lookup."""
    return verdicts(out).get(name, ("NOT REPORTED", ""))


def work_folder(out: str) -> str:
    found = re.search(r"; work folder (.+)$", out, re.M)
    assert found, out
    return found.group(1)


def registered(repo: str) -> list[str]:
    return [ln for ln in git(repo, "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """The fixture repository, its lists, a snapshot of it before any run, and one run of the main list."""
    root = str(tmp_path_factory.mktemp("mutants-runner"))
    mark = os.path.join(root, "hook-ran")
    ini_sentinel = sentinel(os.path.join(root, "sentinel-ini"))
    repo = make_repo(root, mark, ini_sentinel)
    folders = {}
    for name, text in (("lists", LIST), ("slow", SLOW), ("odd", ODD)):
        folders[name] = os.path.join(root, name)
        os.makedirs(folders[name])
        write(os.path.join(folders[name], name + ".py"), text)
    before, ini_before = snapshot(repo), snapshot(ini_sentinel)
    code, out = run(root, repo, folders["lists"], "--jobs", "2")
    return {"root": root, "repo": repo, **folders, "mark": mark, "before": before, "code": code, "out": out,
            "ini_sentinel": ini_sentinel, "ini_before": ini_before}


def test_a_mutant_the_tests_fail_on_is_caught_on_an_assertion(world):
    verdict, how = verdict_of(world["out"], "kills: add subtracts")
    assert (verdict, how.split(" | ")[0]) == ("CAUGHT", "on an assertion in test_calc.py::test_add"), world["out"]


def test_a_mutant_the_tests_pass_on_survived(world):
    assert verdict_of(world["out"], "misses: double triples")[0] == "SURVIVED", world["out"]


def test_an_original_that_is_not_in_the_file_exactly_once_is_blind(world):
    assert verdict_of(world["out"], "blind: not in the file") == (
        "BLIND", "the original occurs 0 times in calc.py at %s" % git(world["repo"], "rev-parse", "HEAD").strip()[:7])
    three = verdict_of(world["out"], "blind: three times in the file")
    assert three[0] == "BLIND" and "occurs 3 times" in three[1], world["out"]


def test_a_crash_is_caught_and_said_not_to_be_an_assertion(world):
    verdict, how = verdict_of(world["out"], "crash: a name that is not there")
    assert verdict == "CAUGHT" and how.startswith("on NameError, not an assertion in test_calc.py::test_add"), how


def test_the_summary_counts_each_verdict_and_the_run_exits_1(world):
    assert world["code"] == 1, world["out"]
    assert re.search(r"^summary at [0-9a-f]{12}: 5 mutants, 2 CAUGHT \(1 on an assertion, 1 on another error, "
                     r"0 timed out\), 1 SURVIVED, 2 BLIND, 0 ERROR$", world["out"], re.M), world["out"]
    assert world["out"].count("control  test_calc.py test_env.py: exit 0") == 1


def test_neither_the_environment_nor_the_repositorys_addopts_move_pytests_files(world):
    """PYTEST_ADDOPTS naming another folder as basetemp (which pytest empties) and as the place of its junit report
    and log, and PYTEST_PLUGINS naming a plugin that is not there: neither reaches a test run, and the folder is byte
    for byte what it was."""
    env_sentinel = sentinel(os.path.join(world["root"], "sentinel-env"))
    before = snapshot(env_sentinel)
    env = caller_env(world["root"], PYTEST_ADDOPTS="--basetemp=%s --junitxml=%s --log-file=%s" % (
        env_sentinel, os.path.join(env_sentinel, "junit.xml"), os.path.join(env_sentinel, "pytest.log")),
        PYTEST_PLUGINS="no_such_plugin_for_this_check")
    code, out = run(world["root"], world["repo"], world["lists"], "--only", "kills", "--only", "misses", env=env)
    assert {k: v[0] for k, v in verdicts(out).items()} == {"kills: add subtracts": "CAUGHT",
                                                          "misses: double triples": "SURVIVED"}, out
    assert snapshot(env_sentinel) == before


def test_the_repositorys_ini_points_no_output_outside_the_run(world):
    """The fixture's pytest.ini sets log_file to an outside file (pytest opens it with mode 'w' when it is configured,
    the control included) and its addopts name an outside basetemp, log file and debug file: after every run so far
    the folder is byte for byte what it was."""
    assert snapshot(world["ini_sentinel"]) == world["ini_before"]


def test_a_run_that_proves_nothing_is_an_error_and_never_caught(world):
    """An interrupted run (exit 2), pytest's internal error (exit 3) and a collection error that does not come from
    the mutated file are ERROR; a mutated file the tests cannot import, and a failing setup, are caught but not on
    an assertion."""
    code, out = run(world["root"], world["repo"], world["odd"], "--jobs", "2")
    got = {name: verdict_of(out, name) for name in (
        "interrupt: a test raises KeyboardInterrupt", "internal: a hook of the repository raises",
        "syntax: the mutated file does not parse", "collection: a data file the tests read when collected",
        "setup: a fixture fails")}
    assert got["interrupt: a test raises KeyboardInterrupt"][0] == "ERROR", out
    assert "interrupted (exit 2)" in got["interrupt: a test raises KeyboardInterrupt"][1], out
    assert got["internal: a hook of the repository raises"][0] == "ERROR", out
    assert "internal error (exit 3)" in got["internal: a hook of the repository raises"][1], out
    assert got["collection: a data file the tests read when collected"][0] == "ERROR", out
    assert "does not come from the mutated file" in got["collection: a data file the tests read when collected"][1]
    assert got["syntax: the mutated file does not parse"][0] == "CAUGHT", out
    assert got["syntax: the mutated file does not parse"][1].startswith(
        "the tests cannot import the mutated file (SyntaxError), not an assertion"), out
    assert got["setup: a fixture fails"][0] == "CAUGHT", out
    assert got["setup: a fixture fails"][1].startswith(
        "on RuntimeError in a test's setup or teardown, not an assertion in test_calc.py::test_add"), out
    assert code == 1
    assert re.search(r"^summary at [0-9a-f]{12}: 5 mutants, 2 CAUGHT \(0 on an assertion, 2 on another error, "
                     r"0 timed out\), 0 SURVIVED, 0 BLIND, 3 ERROR$", out, re.M), out


def test_nothing_is_applied_in_place(world):
    """The repository, its .git folder included, is byte for byte what it was before the run."""
    assert snapshot(world["repo"]) == world["before"]


def test_no_checkout_stays_registered_and_the_work_folder_is_gone(world):
    assert len(registered(world["repo"])) == 1, registered(world["repo"])
    assert not os.path.exists(work_folder(world["out"]))


def test_a_throwaway_checkout_runs_no_hook(world):
    assert not os.path.exists(world["mark"])


def test_a_red_control_runs_no_mutant(world):
    code, out = run(world["root"], world["repo"], world["lists"], "--rev", "broken")
    assert code == 1, out
    assert re.search(r"^control  test_calc.py test_env.py: exit 1 \| 1 failed in ", out, re.M), out
    assert re.search(r"^summary at [0-9a-f]{12}: refused, a control is not green on the unmutated commit, so none of "
                     r"the 5 mutants was run", out, re.M), out
    assert [v for v, _ in verdicts(out).values()] == ["BLIND", "BLIND"], out
    assert not os.path.exists(work_folder(out)) and len(registered(world["repo"])) == 1


def test_git_variables_of_the_caller_redirect_nothing(world, tmp_path):
    """GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE name a decoy repository; the runner and its test runs never see them."""
    decoy = str(tmp_path / "decoy")
    os.makedirs(decoy)
    git(decoy, "init", "-q")
    write(os.path.join(decoy, "calc.py"), CALC.replace("a + b", "b + a"))
    git(decoy, "add", "-A")
    git(decoy, "commit", "-q", "-m", "decoy")
    decoy_before, repo_before = snapshot(decoy), snapshot(world["repo"])
    env = caller_env(world["root"], GIT_DIR=os.path.join(decoy, ".git"), GIT_WORK_TREE=decoy,
                     GIT_INDEX_FILE=os.path.join(decoy, ".git", "index"))
    code, out = run(world["root"], world["repo"], world["lists"], "--only", "kills", "--only", "misses", env=env)
    assert {k: v[0] for k, v in verdicts(out).items()} == {"kills: add subtracts": "CAUGHT",
                                                          "misses: double triples": "SURVIVED"}, out
    assert code == 1
    assert snapshot(decoy) == decoy_before
    assert snapshot(world["repo"]) == repo_before
    assert len(registered(world["repo"])) == 1 and len(registered(decoy)) == 1


def test_a_run_past_the_timeout_is_caught_and_stopped(world):
    start = time.monotonic()
    code, out = run(world["root"], world["repo"], world["slow"], "--timeout", "8")
    verdict, how = verdict_of(out, "hang: wait never returns")
    assert verdict == "CAUGHT" and how.startswith("timed out after 8s"), out
    # Interrupted, not killed: pytest ended its session (and with it tests/conftest.py drops the run's database). Not
    # on Windows: the interrupt there is CTRL_BREAK_EVENT, which Python does not turn into KeyboardInterrupt, and its
    # default handler ends pytest at once, with no summary and no session finish (memory-bxxxxx on #216). There a
    # timeout ends the run, and the runner's record-based drop covers the run's database.
    if os.name != "nt":
        assert "| 2 passed in " in how, out
    assert code == 0 and time.monotonic() - start < 120, out
    assert not os.path.exists(work_folder(out)) and len(registered(world["repo"])) == 1


@pytest.mark.parametrize(("args", "code"), [
    (("--only", "kills"), 0),                          # every selected mutant caught
    (("--only", "crash"), 0),
    (("--only", "crash", "--assertions-only"), 1),     # caught, but not on an assertion
    (("--only", "misses"), 1),
    (("--check", "--only", "kills"), 0),
    (("--check",), 1),                                 # two BLIND
    (("--list", "nope"), 2),
    (("--only", "no such mutant"), 2),
    (("--rev", "no-such-rev"), 2),
    (("--jobs", "0"), 2),
    (("--control-timeout", "0"), 2),
    (("--lists-dir", "odd", "--only", "interrupt"), 1),   # an interrupted run never passes for caught
    (("--lists-dir", "odd", "--only", "internal"), 1),    # nor does pytest's internal error
])
def test_exit_codes(world, args, code):
    if args[0] == "--lists-dir":
        got, out = run(world["root"], world["repo"], world[args[1]], *args[2:])
    else:
        got, out = run(world["root"], world["repo"], world["lists"], *args)
    assert got == code, out


def test_check_reads_the_commit_and_makes_no_checkout(world):
    code, out = run(world["root"], world["repo"], world["lists"], "--check")
    assert re.search(r"^check at [0-9a-f]{12}: 5 mutants, 3 apply \(the original occurs once\), 2 BLIND$", out, re.M), out
    assert not re.search(r"^control  ", out, re.M) and not os.path.exists(world["mark"])   # no control ran


@pytest.mark.parametrize("symlinks", [None, "false"], ids=["git's default", "core.symlinks=false"])
def test_a_file_that_is_a_link_in_the_commit_is_never_written(tmp_path, symlinks):
    """A link committed through git itself (mode 120000, its blob the outside file's path), with no os.symlink, so
    this runs wherever git does. With git's default the checkout holds a real link on macOS and Linux. With
    core.symlinks=false, as git on Windows has it, the checkout holds a plain file with the target's path in it,
    which no realpath check can tell from a file (kxxx's gate, 10 Oct: the mutant SURVIVED there); that case runs on
    every platform. The mutant, whose original is in the link's blob, is an ERROR that names the link in the commit;
    the outside file is unchanged and only the repository's own worktree is registered."""
    outside = tmp_path / "outside.py"
    write(str(outside), "keep = 1\n")
    repo = str(tmp_path / "linked")
    os.makedirs(repo)
    git(repo, "init", "-q")
    if symlinks is not None:
        git(repo, "config", "core.symlinks", symlinks)      # the runner's checkouts share the repository's config
    write(os.path.join(repo, "test_ok.py"), "def test_ok():\n    pass\n")
    git(repo, "add", "-A")
    blob_text = str(tmp_path / "link-blob.txt")              # outside the repository: `git add -A` never sees it
    with open(blob_text, "w", encoding="utf-8", newline="") as f:
        f.write(str(outside).replace("\\", "/"))             # a link's blob is its target's path, no newline
    blob = git(repo, "hash-object", "-w", "--no-filters", blob_text).strip()
    git(repo, "update-index", "--add", "--cacheinfo", "120000,%s,link.py" % blob)
    git(repo, "commit", "-q", "-m", "a link")
    assert git(repo, "ls-tree", "HEAD", "--", "link.py").split()[:1] == ["120000"]     # control: a link in the commit
    lists = str(tmp_path / "lists")
    os.makedirs(lists)
    write(os.path.join(lists, "ln.py"), 'MUTANTS = [("through the link", "link.py", "outside.py", "elsewhere.py", '
                                         '["test_ok.py"])]\n')
    code, out = run(str(tmp_path), repo, lists)
    verdict, how = verdict_of(out, "through the link")
    assert (code, verdict) == (1, "ERROR"), out
    assert how.startswith("link.py is a symbolic link in the commit (mode 120000 at %s); not written" % (
        git(repo, "rev-parse", "HEAD").strip()[:7])), out
    assert outside.read_text() == "keep = 1\n" and len(registered(repo)) == 1


def load_runner():
    """tools/mutants/run.py as a module, to check one of its parts alone."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mutants_run_under_test", RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def small_repo(root: str) -> tuple[str, str]:
    """A repository holding sub/calc.py and a passing test_ok.py at its top: (its path, its commit)."""
    repo = os.path.join(root, "small")
    os.makedirs(os.path.join(repo, "sub"))
    git(repo, "init", "-q")
    write(os.path.join(repo, "sub", "calc.py"), CALC)
    write(os.path.join(repo, "test_ok.py"), "def test_ok():\n    pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "calc in a folder")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def a_run(runner, root: str, repo: str, sha: str, *args):
    """A runner.Run on `repo` at `sha` with its own work folder; `args` follow the work folder (timeout, ...)."""
    work = os.path.realpath(os.path.join(root, "work"))
    os.makedirs(os.path.join(work, "no-hooks"))
    return runner.Run(runner.Repo(repo, os.path.join(work, "no-hooks")), sha, work, *args)


def link_folder(target: str, link: str) -> None:
    """`link` made to lead to the folder `target`: a junction on Windows (no elevation needed), a symbolic link
    elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link, target_is_directory=True)


def test_a_path_that_leads_out_of_the_checkout_is_never_written(tmp_path, monkeypatch):
    """The second guard alone. sub/calc.py is a regular file in the commit's tree, so the first guard lets it pass;
    once the checkout is made, its `sub` is turned into a link (a junction on Windows) to a folder outside it. The
    mutant is refused as leading out of the checkout, the outside file is unchanged, no test run starts, and the
    checkout is removed."""
    runner = load_runner()
    repo, sha = small_repo(str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    write(str(outside / "calc.py"), CALC)
    run_ = a_run(runner, str(tmp_path), repo, sha, 5)
    add, remove = run_.repo.add_checkout, run_.repo.remove_checkout

    def add_then_link(at: str, dest: str) -> None:
        add(at, dest)
        shutil.rmtree(os.path.join(dest, "sub"))
        link_folder(str(outside), os.path.join(dest, "sub"))

    def unlink_then_remove(dest: str):
        link = os.path.join(dest, "sub")
        if os.path.lexists(link):                          # the link alone goes, never what it leads to
            (os.rmdir if os.name == "nt" else os.unlink)(link)
        return remove(dest)

    started = []
    monkeypatch.setattr(run_.repo, "add_checkout", add_then_link)
    monkeypatch.setattr(run_.repo, "remove_checkout", unlink_then_remove)
    monkeypatch.setattr(runner, "run_pytest", lambda *args: started.append(args) or (0, "1 passed", 0.1, [], []))
    assert run_.mode("sub/calc.py") in runner.FILE_MODES                    # control: the first guard lets it pass
    done = run_.in_checkout(["test_ok.py"], "sub/calc.py", CALC.replace("a + b", "a - b"))
    assert done == "sub/calc.py leads out of the checkout (a link?); not written", done
    assert (outside / "calc.py").read_text(encoding="utf-8") == CALC and started == []
    assert len(registered(repo)) == 1 and not os.path.exists(os.path.join(run_.work, "m1"))


def test_the_control_is_bound_by_its_own_timeout_and_a_mutant_by_timeout(tmp_path, monkeypatch, capsys):
    """A control's test run gets --control-timeout and a mutant's gets --timeout (kxxx's gate, 10 Oct: under load the
    control hit the 5 s meant for a hanging mutant and refused the run). A control that passed but took longer than
    --timeout says so: a mutant of those tests that times out proves nothing."""
    runner = load_runner()
    repo, sha = small_repo(str(tmp_path))
    run_ = a_run(runner, str(tmp_path), repo, sha, 5, 2, 300)
    bounds = []

    def fake(children, checkout, tests, scratch, timeout, grace):
        bounds.append((tests, timeout, grace))
        return 0, "== 1 passed in 9.00s ==", 9.0, [], []

    monkeypatch.setattr(runner, "run_pytest", fake)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        assert runner._controls(run_, pool, [("test_ok.py",)])
    assert run_.in_checkout(["test_ok.py"], "sub/calc.py", CALC.replace("a + b", "a - b"))[0] == 0
    assert bounds == [(["test_ok.py"], 300, 2), (["test_ok.py"], 5, 2)]
    out = capsys.readouterr().out
    assert ("control  test_ok.py: exit 0 | 1 passed in 9.00s; the control took 9s, longer than --timeout 5s: a mutant "
            "of these tests that times out proves nothing") in out, out


@pytest.mark.parametrize(("args", "bounds"), [
    ((), "timeout 300s per mutant, 300s per control"),
    (("--timeout", "8"), "timeout 8s per mutant, 300s per control"),       # a short --timeout: the control keeps 300
    (("--timeout", "900"), "timeout 900s per mutant, 900s per control"),
    (("--timeout", "8", "--control-timeout", "40"), "timeout 8s per mutant, 40s per control"),
])
def test_the_controls_bound_is_timeout_but_never_under_300s_unless_named(world, args, bounds):
    code, out = run(world["root"], world["repo"], world["lists"], "--check", *args)
    assert "; %s; work folder " % bounds in out, out


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_the_runner_ends_its_test_runs_and_removes_everything(world, tmp_path, signum):
    """The runner is stopped while a mutant's pytest sleeps, after it started a child that ignores SIGINT and
    SIGTERM: that pytest, the child and its process group are gone, no checkout stays registered, the work folder is
    removed, and the exit code says which signal it was."""
    pid_file = str(tmp_path / "pytest.pid")
    lists = str(tmp_path / "stop")
    os.makedirs(lists)
    child_file = str(tmp_path / "child.pid")
    sleeps = ('    %s; open(%r, "w").write(str(__import__("os").getpid())); __import__("time").sleep(600); return "done"'
              % (stubborn(child_file), pid_file))
    write(os.path.join(lists, "stop.py"), "MUTANTS = [%r]\n" % (
        ("sleeps: wait says who it is and sleeps", "calc.py", '    return "done"', sleeps, TESTS),))
    log = str(tmp_path / "runner.out")
    with open(log, "wb") as out_file:
        runner = subprocess.Popen([sys.executable, RUNNER, "--repo", world["repo"], "--lists-dir", lists],
                                  stdout=out_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  env=caller_env(world["root"]))
    pid = child = None
    try:
        pid = wait_for_pid(pid_file, runner)
        child = wait_for_pid(child_file, runner)
        assert pid and child, open(log).read()
        runner.send_signal(signum)
        try:
            code = runner.wait(timeout=60)
        except subprocess.TimeoutExpired:
            code = None                                    # still running a minute after the signal
        out = open(log, encoding="utf-8", errors="replace").read()
        left = True
        try:
            os.killpg(pid, 0)                              # the mutant's pytest led its own process group
        except ProcessLookupError:
            left = False
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGKILL)             # whatever this check finds, it leaves nothing running
            except (ProcessLookupError, PermissionError):
                pass
        child_gone = child is None or gone(child)
        kill_quietly(child)
    assert code == 128 + signum, out
    assert "stopped by signal %d" % signum in out, out
    assert not left, "the mutant's pytest outlived the runner"
    assert child_gone, "a child of the mutant's pytest that ignores SIGINT and SIGTERM outlived the runner"
    assert len(registered(world["repo"])) == 1, registered(world["repo"])
    assert not os.path.exists(work_folder(out)), out


def stubborn(pid_file: str) -> str:
    """One statement that starts a child of the test run which ignores SIGINT and SIGTERM, writes its pid and
    sleeps, and then waits (at most 30 s) until the child has written its pid: the child writes it only after it
    ignores both signals, so the test run cannot end, and signal the child, before the child is ready (a handshake;
    without it a pytest that ends at once could end the child before it wrote its pid)."""
    child = ("import os, signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); open(%r, 'w').write(str(os.getpid())); time.sleep(600)"
             % pid_file)
    ready = ('any(__import__("os").path.exists(%r) and open(%r).read() or __import__("time").sleep(0.05) '
             'for _ in range(600))' % (pid_file, pid_file))
    return ('__import__("subprocess").Popen([__import__("sys").executable, "-c", %r], stdout=-3, stderr=-3); %s'
            % (child, ready))


def wait_for_pid(path: str, runner: subprocess.Popen | None = None, wait: float = 120) -> int | None:
    """The pid written into `path`, read at once and then for up to `wait` while `runner` (if any) is running."""
    deadline = time.monotonic() + wait
    while True:
        if os.path.exists(path) and open(path).read():
            return int(open(path).read())
        if time.monotonic() >= deadline or (runner is not None and runner.poll() is not None):
            return None
        time.sleep(0.1)


def gone(pid: int, wait: float = 10) -> bool:
    """Is the process gone (allowing `wait` for its new parent to reap it once killed)?"""
    deadline = time.monotonic() + wait
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:                            # a zombie on macOS: not yet reaped by its new parent
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def kill_quietly(*pids: int | None) -> None:
    for pid in pids:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.parametrize("entry", ["@args.txt", "@{outside}/args.txt", "../repo/test_calc.py", "{outside}/test_x.py",
                                   "C:/x/test_x.py", "-p", "test_calc.py\0", "tests\\test_calc.py",
                                   "./test_calc.py", "tests//test_calc.py", "test_calc.py/"])
def test_an_argument_file_or_a_path_outside_the_repository_is_refused_before_any_run(world, tmp_path, entry):
    """pytest expands `@FILE` into the arguments FILE holds, after the runner's own, so its --basetemp would win and
    pytest would empty that folder. Such an entry, and a test path that is absolute or climbs out with `..`, refuse
    the run before anything starts; the folder the argument file names is byte for byte what it was."""
    outside = sentinel(str(tmp_path / "outside"))
    write(os.path.join(outside, "args.txt"), "--basetemp=%s\n--junitxml=%s/report.xml\ntest_calc.py\n" % (
        outside, outside))
    before = snapshot(outside)
    lists = str(tmp_path / "lists")
    os.makedirs(lists)
    write(os.path.join(lists, "at.py"), "MUTANTS = [%r]\n" % (
        ("an odd test entry", "calc.py", "return a + b", "return a - b", [entry.format(outside=outside)]),))
    code, out = run(world["root"], world["repo"], lists)
    assert code == 2 and "refused: list at:" in out, out
    assert "control" not in out
    assert snapshot(outside) == before


def test_a_test_path_that_is_not_in_the_checkout_stops_the_control(world, tmp_path):
    lists = str(tmp_path / "lists")
    os.makedirs(lists)
    write(os.path.join(lists, "missing.py"), "MUTANTS = [%r]\n" % (
        ("a test file the commit does not have", "calc.py", "return a + b", "return a - b", ["test_nothing_here.py"]),))
    code, out = run(world["root"], world["repo"], lists)
    assert code == 1, out
    assert ("control  test_nothing_here.py: ERROR the test path 'test_nothing_here.py' is not a test file in the "
            "checkout") in out
    assert len(registered(world["repo"])) == 1 and not os.path.exists(work_folder(out))


def test_a_database_opt_out_refuses_the_run_before_anything_starts(world):
    """MESHY_NO_TEST_DB=1 has tests/conftest.py make no database of the run's own, so the tests would write into the
    database MESHY_TEST_DSN names: the runner refuses before any control and makes no checkout. --check, which runs
    no test, is still answered."""
    env = caller_env(world["root"], MESHY_NO_TEST_DB="1",
                     MESHY_TEMPLATE_DSN="postgresql://nobody@127.0.0.1:1/postgres")
    code, out = run(world["root"], world["repo"], world["lists"], env=env)
    assert code == 2 and "refused: MESHY_NO_TEST_DB is set" in out, out
    assert "control" not in out and not LINE.search(out)
    assert len(registered(world["repo"])) == 1 and not os.path.exists(world["mark"])
    code, out = run(world["root"], world["repo"], world["lists"], "--check", env=env)
    assert code == 1 and "2 BLIND" in out, out


def test_a_database_named_by_the_caller_never_reaches_a_test_run(world):
    """MESHY_TEST_DSN alone, naming a shared database: the fixture's test_env.py (in the control and in every
    mutant's run) asserts that no such variable arrives, so a green control and a SURVIVED mutant prove it."""
    env = env_without_git(MESHY_TEST_DSN="postgresql://nobody@127.0.0.1:1/a_shared_database")
    code, out = run(world["root"], world["repo"], world["lists"], "--only", "misses", env=env)
    assert re.search(r"^control  test_calc.py test_env.py: exit 0 ", out, re.M), out
    assert verdict_of(out, "misses: double triples")[0] == "SURVIVED", out


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_what_a_test_run_leaves_running_is_ended_on_a_timeout_and_on_a_normal_exit(world, tmp_path):
    """Two mutants each start a child that ignores SIGINT and SIGTERM: one then sleeps past the timeout, the other
    returns and its tests pass. Once each run's pytest has exited, the rest of its process group is ended
    (SIGTERM, then SIGKILL): neither child is alive after the run."""
    late, done = str(tmp_path / "late.pid"), str(tmp_path / "done.pid")
    lists = str(tmp_path / "lists")
    os.makedirs(lists)
    write(os.path.join(lists, "left.py"), "MUTANTS = %r\n" % ([
        ("left: a child, then past the timeout", "calc.py", '    return "done"',
         "    %s; __import__('time').sleep(600); return 'done'" % stubborn(late), TESTS),
        ("left: a child, then the tests pass", "calc.py", '    return "done"',
         "    %s; return 'done'" % stubborn(done), TESTS)],))
    late_pid = done_pid = None
    try:
        code, out = run(world["root"], world["repo"], lists, "--jobs", "2", "--timeout", "8")
        late_pid, done_pid = wait_for_pid(late, wait=0), wait_for_pid(done, wait=0)
        assert verdict_of(out, "left: a child, then past the timeout")[1].startswith("timed out after 8s"), out
        assert verdict_of(out, "left: a child, then the tests pass")[0] == "SURVIVED", out
        assert late_pid and done_pid, out
        assert gone(late_pid) and gone(done_pid), "a child of a test run outlived it"
        assert "LEFTOVER" not in out and len(registered(world["repo"])) == 1
    finally:
        kill_quietly(late_pid, done_pid)


def outside_test(folder: str, mark: str) -> str:
    """A test file outside every checkout that leaves `mark` if pytest ever runs it."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "test_outside.py")
    write(path, "def test_outside():\n    open(%r, 'w').write('ran')\n" % mark)
    return path


def linked_repo(root: str, links: dict[str, str]) -> str:
    """A repository holding calc.py, test_calc.py and the given links (name in the repository -> target)."""
    repo = os.path.join(root, "linked")
    os.makedirs(repo)
    git(repo, "init", "-q")
    write(os.path.join(repo, "calc.py"), CALC)
    write(os.path.join(repo, "test_calc.py"), "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
    for name, target in links.items():
        os.makedirs(os.path.dirname(os.path.join(repo, name)), exist_ok=True)
        os.symlink(target, os.path.join(repo, name))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "links")
    return repo


@pytest.mark.skipif(os.name == "nt", reason="a backslash is a file name's character on POSIX only")
def test_a_backslash_entry_never_reaches_a_link_of_that_name(tmp_path):
    """On POSIX `tests\\test_ok.py` is one file name. The repository commits a link of exactly that name to a test
    outside it: the entry is refused before any run, and the outside test never runs."""
    mark = str(tmp_path / "outside-ran")
    target = outside_test(str(tmp_path / "outside"), mark)
    repo = linked_repo(str(tmp_path), {"tests\\test_ok.py": target})
    lists = str(tmp_path / "lists")
    os.makedirs(lists)
    write(os.path.join(lists, "bs.py"), "MUTANTS = [%r]\n" % (
        ("through a backslash", "calc.py", "return a + b", "return a - b", ["tests\\test_ok.py"]),))
    code, out = run(str(tmp_path), repo, lists)
    assert code == 2 and "has a backslash" in out, out
    assert not os.path.exists(mark)


@pytest.mark.skipif(os.name == "nt", reason="POSIX links")
def test_a_folder_entry_is_refused_and_never_collected_through_its_links(tmp_path):
    """A folder named as the tests is collected through every link in it, `tests/external -> /outside` included. Only
    a test file is taken: the control stops on the folder, and the outside test never runs."""
    mark = str(tmp_path / "outside-ran")
    outside_test(str(tmp_path / "outside"), mark)
    repo = linked_repo(str(tmp_path), {"tests/external": str(tmp_path / "outside")})
    write(os.path.join(repo, "tests", "test_inside.py"), "def test_inside():\n    pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "an inside test")
    lists = str(tmp_path / "lists")
    os.makedirs(lists)
    write(os.path.join(lists, "dir.py"), "MUTANTS = [%r]\n" % (
        ("a folder of tests", "calc.py", "return a + b", "return a - b", ["tests"]),))
    code, out = run(str(tmp_path), repo, lists)
    assert code == 1, out
    assert "control  tests: ERROR the test path 'tests' is not a test file in the checkout" in out, out
    assert not os.path.exists(mark)


def test_a_dollar_in_the_temp_folder_moves_no_report(world, tmp_path):
    """pytest expands $VARIABLES in --junitxml. With TMPDIR a folder literally named `$OUT` and OUT naming another
    one, an absolute report path would be written into that other folder (and not be found): the verdicts still
    come from the report, and nothing appears where the expansion leads."""
    literal = tmp_path / "$OUT"
    literal.mkdir()
    env = caller_env(world["root"], TMPDIR=str(literal), OUT="elsewhere")
    code, out = run(world["root"], world["repo"], world["lists"], "--only", "kills", env=env)
    assert verdict_of(out, "kills: add subtracts")[1].startswith("on an assertion in test_calc.py::test_add"), out
    assert code == 0, out
    assert not (tmp_path / "elsewhere").exists()
    assert os.listdir(str(literal)) == []                  # the work folder made inside it is gone too


def admin_dsn() -> str:
    return os.environ["MESHY_TEMPLATE_DSN"].strip().rpartition("/")[0] + "/postgres"


def run_databases(conn) -> set[str]:
    return {row[0] for row in conn.execute(r"SELECT datname FROM pg_database WHERE datname LIKE 'meshy\_test\_%'")}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                                # someone else's, or a zombie: not ours to judge
        return True
    return True


@pytest.mark.skipif(not (os.environ.get("MESHY_TEMPLATE_DSN") or "").strip(), reason="needs a Postgres server")
def test_a_killed_run_leaves_no_database_of_its_own(tmp_path):
    """The repository's own tests/conftest.py makes a database for the run before any test module is imported. A
    mutant of apps/mesh/normalize.py ignores SIGINT and sleeps when imported, so the run outlives its timeout and its
    grace and is killed, and conftest never drops that database. The runner drops it, by the name conftest recorded,
    and says so; the database is gone.

    So that a runner that fails here leaves nothing either, this check drops what such a runner would leave: a
    database made while it ran, whose pytest is no longer alive and that nothing is connected to (the server this
    suite uses listens on this machine, so the pid in the name is this machine's)."""
    import psycopg
    lists = str(tmp_path / "lists")
    os.makedirs(lists)
    deaf = ("MAX_DST_LEN = 512; __import__('signal').signal(__import__('signal').SIGINT, __import__('signal').SIG_IGN);"
            " __import__('time').sleep(600)")
    write(os.path.join(lists, "deaf.py"), "MUTANTS = [%r]\n" % (
        ("deaf: normalize ignores the interrupt", "apps/mesh/normalize.py", "MAX_DST_LEN = 512", deaf,
         ["tests/test_mesh_normalize.py"]),))
    env = env_without_git(MESHY_TEMPLATE_DSN=os.environ["MESHY_TEMPLATE_DSN"])
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        before = run_databases(conn)
    left_behind = []
    try:
        # The control (the unmutated tests/test_mesh_normalize.py) gets a bound of its own: under the gate's load it
        # took longer than the 5 s meant for the deaf mutant (kxxx, 10 Oct), and a control past its bound refuses.
        code, out = run(str(tmp_path), ROOT, lists, "--rev", "HEAD", "--timeout", "10", "--grace", "2",
                        "--control-timeout", "300", env=env)
    finally:
        with psycopg.connect(admin_dsn(), autocommit=True) as conn:
            for name in sorted(run_databases(conn) - before):
                in_use = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s", (name,)).fetchone()[0]
                if not pid_alive(int(name.split("_")[2])) and not in_use:
                    conn.execute('DROP DATABASE IF EXISTS "%s"' % name)
                    left_behind.append(name)
    found = re.search(r"; dropped the database the run left: (meshy_test_\d+_[0-9a-f]{6})", out)
    how = verdict_of(out, "deaf: normalize ignores the interrupt")[1]
    assert found and how.startswith("timed out after 10s"), out
    assert int(re.search(r"\((\d+)s\)", how).group(1)) < 30, out    # killed after --grace 2, not the default 30
    # --timeout 10, not 5: the mutant must start pytest and let conftest create its database before the timeout;
    # on HQB that takes about 4 s idle (dxxxxx), and the gate box is loaded (kxxx's refusal of b750c1f9).
    assert code == 0 and "LEFTOVER" not in out, out
    assert left_behind == [], "the runner left the database(s) %s of the run it killed" % left_behind
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        assert conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (found.group(1),)).fetchone() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_zombie_the_runner_adopted_counts_until_it_is_reaped():
    """group_members counts a zombie in the group whose parent is the runner (on Linux, an orphan the subreaper
    adopted) as not gone, and reaps it by its pid; the next look finds the group empty. Here the checking process is
    that parent: a leader in a group of its own, and a member that exits at once and stays a zombie of ours."""
    runner = load_runner()
    leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], process_group=0)
    try:
        member = subprocess.Popen([sys.executable, "-c", "pass"], process_group=leader.pid)
        deadline = time.monotonic() + 30
        while os.waitid(os.P_PID, member.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            assert time.monotonic() < deadline, "the member did not exit"
            time.sleep(0.05)
        assert runner.group_members(leader.pid) == [member.pid]     # a zombie of ours: not gone yet
        assert runner.group_members(leader.pid) == []               # reaped by the first look
        with pytest.raises(ChildProcessError):
            os.waitpid(member.pid, os.WNOHANG)
    finally:
        leader.kill()
        leader.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_CHILD_SUBREAPER is Linux's")
def test_the_runner_is_a_child_subreaper_on_linux():
    probe = ("import ctypes, importlib.util, sys\n"
             "spec = importlib.util.spec_from_file_location('r', sys.argv[1]); r = importlib.util.module_from_spec(spec)\n"
             "spec.loader.exec_module(r)\n"
             "print(r.become_subreaper())\n"
             "v = ctypes.c_int(0); ctypes.CDLL(None).prctl(37, ctypes.byref(v), 0, 0, 0); print(v.value)\n")
    p = subprocess.run([sys.executable, "-c", probe, RUNNER], capture_output=True, text=True, env=env_without_git())
    assert p.stdout.split() == ["True", "1"], p.stdout + p.stderr
