"""Unit and CLI integration tests for --diff and --audit-uncovered-predicates in tools/mutants/run.py."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tools", "mutants", "run.py")


def load_runner():
    spec = importlib.util.spec_from_file_location("mutants_run_diff_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env_without_git(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.pop("MESHY_NO_TEST_DB", None)
    env.update(extra)
    return env


def git(cwd: str, *args: str) -> str:
    p = subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.org",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        env=env_without_git(),
        capture_output=True,
        text=True,
        check=True,
    )
    return p.stdout


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def run_cli(repo: str, lists: str, *args: str, extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, RUNNER, "--repo", repo, "--lists-dir", lists, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env_without_git(**(extra_env or {})),
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    return p.returncode, p.stdout + p.stderr


def test_parse_diff_changed_lines_and_snippet_line_span():
    runner = load_runner()
    sample_diff = "\n".join([
        "diff --git a/calc.py b/calc.py",
        "index 1111111..2222222 100644",
        "--- a/calc.py",
        "+++ b/calc.py",
        "@@ -2 +2 @@ def add(a, b):",
        "-    return a + b",
        "+    return a + b + 0",
        "@@ -10,2 +10,3 @@ def sub(a, b):",
        "+    if a < b:",
        "+        return 0",
        "+    return a - b",
        "@@ -20,2 +21,0 @@ def old():",
        "-    pass",
        "diff --git a/gone.py b/gone.py",
        "--- a/gone.py",
        "+++ /dev/null",
        "@@ -1,3 +0,0 @@",
    ])
    changed = runner.parse_diff_changed_lines(sample_diff)
    assert changed == {"calc.py": {2, 10, 11, 12}}

    src = "line1\nline2\nline3\nline4\n"
    assert runner.snippet_line_span(src, "line2") == (2, 2)
    assert runner.snippet_line_span(src, "line2\nline3\n") == (2, 3)
    assert runner.snippet_line_span(src, "missing") is None
    assert runner.snippet_line_span("dup\ndup\n", "dup") is None


def test_diff_filters_mutants_to_modified_lines_only(tmp_path):
    repo = str(tmp_path / "repo")
    lists = str(tmp_path / "lists")
    os.makedirs(repo)
    os.makedirs(lists)
    git(repo, "init", "-q")

    base_calc = (
        "def add(a, b):\n"
        "    if a == 0:\n"
        "        return b\n"
        "    return a + b\n"
        "\n"
        "def clamp(x, limit):\n"
        "    return x\n"
    )
    test_calc = (
        "import calc\n\n"
        "def test_add():\n"
        "    assert calc.add(2, 3) == 5\n"
        "    assert calc.add(0, 4) == 4\n\n"
        "def test_clamp():\n"
        "    assert calc.clamp(10, 5) == 5\n"
        "    assert calc.clamp(3, 5) == 3\n"
    )
    write(os.path.join(repo, "calc.py"), base_calc)
    write(os.path.join(repo, "test_calc.py"), test_calc)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feature")

    # Feature commit modifies ONLY clamp(), leaving add() untouched
    head_calc = (
        "def add(a, b):\n"
        "    if a == 0:\n"
        "        return b\n"
        "    return a + b\n"
        "\n"
        "def clamp(x, limit):\n"
        "    if x > limit and limit >= 0:\n"
        "        return limit\n"
        "    return x\n"
    )
    write(os.path.join(repo, "calc.py"), head_calc)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "update clamp")

    # Register two mutants:
    # 1. "mut:add" on untouched add() (which would be run without --diff, but skipped with --diff main)
    # 2. "mut:clamp" on modified clamp()
    list_text = (
        "MUTANTS = [\n"
        '    ("mut:add", "calc.py", "return a + b", "return a - b", ["test_calc.py"]),\n'
        '    ("mut:clamp", "calc.py", "return limit", "return x", ["test_calc.py"]),\n'
        "]\n"
    )
    write(os.path.join(lists, "calc_list.py"), list_text)

    # With --diff main --check, only mut:clamp (1 mutant) is selected
    code, out = run_cli(repo, lists, "--diff", "main", "--check")
    assert code == 0, out
    assert "1 mutants from 1 list(s)" in out, out

    # Full run with --diff main executes only mut:clamp and catches it
    code_run, out_run = run_cli(repo, lists, "--diff", "main")
    assert code_run == 0, out_run
    assert "mut:clamp" in out_run
    assert "mut:add" not in out_run

    # Empty diff against HEAD selects 0 mutants and exits 2 unless --allow-empty is given
    code_empty_refused, out_empty_refused = run_cli(repo, lists, "--diff", "HEAD")
    assert code_empty_refused == 2, out_empty_refused
    assert "0 mutants overlap diff" in out_empty_refused
    assert "0 mutants matched diff: no added/modified lines in registered mutant targets; not a coverage proof" in out_empty_refused

    code_empty, out_empty = run_cli(repo, lists, "--diff", "HEAD", "--allow-empty")
    assert code_empty == 0, out_empty
    assert "0 mutants overlap diff" in out_empty
    assert "0 mutants matched diff: no added/modified lines in registered mutant targets; not a coverage proof" in out_empty

    # Deletion-only commit (hunk with count == 0) where remaining mutants still apply at HEAD
    # selects 0 added/modified lines -> exits 2 without --allow-empty, or 0 with --allow-empty
    git(repo, "checkout", "-q", "-b", "deletion-only", "feature")
    deleted_calc = (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def clamp(x, limit):\n"
        "    if x > limit and limit >= 0:\n"
        "        return limit\n"
        "    return x\n"
    )
    write(os.path.join(repo, "calc.py"), deleted_calc)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "delete zero-branch in add")
    code_del_refused, out_del_refused = run_cli(repo, lists, "--diff", "feature")
    assert code_del_refused == 2, out_del_refused
    assert "0 mutants matched diff: no added/modified lines in registered mutant targets; not a coverage proof" in out_del_refused
    code_del, out_del = run_cli(repo, lists, "--diff", "feature", "--allow-empty")
    assert code_del == 0, out_del
    assert "0 mutants matched diff: no added/modified lines in registered mutant targets; not a coverage proof" in out_del

    # Invalid, empty/whitespace, or flag-shaped base_ref exits 2 with explicit refusal message (R9)
    code_bad, out_bad = run_cli(repo, lists, "--diff", "no-such-branch")
    assert code_bad == 2 and "no commit" in out_bad
    code_flag, out_flag = run_cli(repo, lists, "--diff=--output=foo")
    assert code_flag == 2 and "bad diff base ref" in out_flag
    code_ws, out_ws = run_cli(repo, lists, "--diff", "   ")
    assert code_ws == 2 and "bad diff base ref" in out_ws

    # MESHY_NO_TEST_DB is refused on normal runs (R46) but allowed during --check (R47) and --audit-uncovered-predicates (R48)
    rc_ref_run, out_ref_run = run_cli(repo, lists, "--diff", "main", extra_env={"MESHY_NO_TEST_DB": "1"})
    assert rc_ref_run == 2 and "MESHY_NO_TEST_DB is set" in out_ref_run
    rc_ref_chk, out_ref_chk = run_cli(repo, lists, "--diff", "main", "--check", extra_env={"MESHY_NO_TEST_DB": "1"})
    assert rc_ref_chk == 0, out_ref_chk
    rc_ref_aud, out_ref_aud = run_cli(
        repo, lists, "--audit-uncovered-predicates", "main", extra_env={"MESHY_NO_TEST_DB": "1"}
    )
    assert rc_ref_aud in (0, 1) and "MESHY_NO_TEST_DB is set" not in out_ref_aud


def test_audit_uncovered_predicates_reports_and_clears_when_covered(tmp_path):
    repo = str(tmp_path / "repo")
    lists = str(tmp_path / "lists")
    os.makedirs(repo)
    os.makedirs(lists)
    git(repo, "init", "-q")

    write(
        os.path.join(repo, "calc.py"),
        "def check(x, limit):\n    return x\n",
    )
    write(
        os.path.join(repo, "test_calc.py"),
        "import calc\ndef test_check():\n    assert calc.check(5, 3) == 3\n",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feature")

    # Modify calc.py to add an If + BoolOp + Compare on line 2
    write(
        os.path.join(repo, "calc.py"),
        "def check(x, limit):\n"
        "    if x > limit and limit > 0:\n"
        "        return limit\n"
        "    return x\n",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add predicate")

    # Initially, mutant only covers line 3 ("return limit"), leaving line 2's predicate uncovered
    write(
        os.path.join(lists, "calc_list.py"),
        'MUTANTS = [("mut:ret", "calc.py", "return limit", "return x", ["test_calc.py"])]\n',
    )
    code1, out1 = run_cli(repo, lists, "--audit-uncovered-predicates", "main")
    assert code1 == 1, out1
    assert "UNCOVERED calc.py:2 (" in out1, out1
    assert "if x > limit and limit > 0:" in out1, out1
    assert "1 uncovered predicate line(s)" in out1, out1

    # A single mutant that wraps `not (...)` around both operands or flips both operands at once (N13)
    # does NOT count as per-operand BoolOp coverage
    write(
        os.path.join(lists, "calc_list.py"),
        "MUTANTS = [\n"
        '    ("mut:ret", "calc.py", "return limit", "return x", ["test_calc.py"]),\n'
        '    ("mut:both", "calc.py", "if x > limit and limit > 0:", "if x >= limit or limit <= 0:", ["test_calc.py"]),\n'
        "]\n",
    )
    code_both, out_both = run_cli(repo, lists, "--diff", "main", "--audit-uncovered-predicates")
    assert code_both == 1, out_both
    assert "BoolOp[x > limit, limit > 0]" in out_both, out_both

    # Now add dedicated single-operand mutants covering each operand of line 2's BoolOp
    # Also modify tests/test_calc.py with an uncovered predicate and verify it is skipped UNLESS tests/test_calc.py is in mutants_by_path (R39)
    write(
        os.path.join(repo, "tests", "test_calc.py"),
        "import calc\ndef test_check():\n    if True and True:\n        assert calc.check(5, 3) == 3\n",
    )
    git(repo, "add", "tests/test_calc.py")
    git(repo, "commit", "-q", "-m", "add predicate in tests/test_calc.py")
    write(
        os.path.join(lists, "calc_list.py"),
        "MUTANTS = [\n"
        '    ("mut:ret", "calc.py", "return limit", "return x", ["test_calc.py"]),\n'
        '    ("mut:pred1", "calc.py", "if x > limit and limit > 0:", "if x >= limit and limit > 0:", ["test_calc.py"]),\n'
        '    ("mut:pred2", "calc.py", "if x > limit and limit > 0:", "if x > limit and limit <= 0:", ["test_calc.py"]),\n'
        "]\n",
    )
    code2, out2 = run_cli(repo, lists, "--diff", "main", "--audit-uncovered-predicates")
    assert code2 == 0, out2
    assert "0 uncovered predicate line(s)" in out2, out2

    # When tests/test_calc.py is an explicit mutant target, its uncovered predicates ARE audited (R39)
    write(
        os.path.join(lists, "calc_list.py"),
        "MUTANTS = [\n"
        '    ("mut:ret", "calc.py", "return limit", "return x", ["test_calc.py"]),\n'
        '    ("mut:pred1", "calc.py", "if x > limit and limit > 0:", "if x >= limit and limit > 0:", ["test_calc.py"]),\n'
        '    ("mut:pred2", "calc.py", "if x > limit and limit > 0:", "if x > limit and limit <= 0:", ["test_calc.py"]),\n'
        '    ("mut:test", "tests/test_calc.py", "assert calc.check(5, 3) == 3", "assert calc.check(5, 3) == 0", ["test_calc.py"]),\n'
        "]\n",
    )
    code3, out3 = run_cli(repo, lists, "--diff", "main", "--audit-uncovered-predicates")
    assert code3 == 1, out3
    assert "UNCOVERED tests/test_calc.py:3" in out3, out3


def test_parse_diff_hunk_body_plus_lines_quoted_paths_spaces_and_noprefix(tmp_path):
    runner = load_runner()
    # 1. Hunk body containing a removed line starting with "-- a/fake.py" (-> "--- a/fake.py" in diff)
    # and an added line starting with "++ b/fake.py" (-> "+++ b/fake.py" in diff) inside the hunk body (R2),
    # and a multi-hunk file where hunk 1's body is interrupted by a new header (R3).
    diff_with_body_plus = "\n".join([
        "diff --git a/src/a.py b/src/a.py",
        "--- a/src/a.py",
        "+++ b/src/a.py",
        "@@ -10,1 +11,2 @@",
        "--- a/fake.py",
        "+++ b/fake.py",
        "+@@ -99 +99 @@",
        "@@ -30 +32 @@",
        "-old",
        "+new",
    ])
    assert runner.parse_diff_changed_lines(diff_with_body_plus) == {"src/a.py": {11, 12, 32}}
    # Truncated hunk body followed immediately by next file's diff header (R3) must reset rem_body and parse next file
    truncated_body_diff = "\n".join([
        "diff --git a/first.py b/first.py",
        "--- a/first.py",
        "+++ b/first.py",
        "@@ -1,5 +1,5 @@",
        "diff --git a/second.py b/second.py",
        "--- a/second.py",
        "+++ b/second.py",
        "@@ -1 +1 @@",
        "-old",
        "+new",
    ])
    assert runner.parse_diff_changed_lines(truncated_body_diff) == {"first.py": {1, 2, 3, 4, 5}, "second.py": {1}}

    # 2. C-quoted non-ASCII path, trailing tab on spaced path, mnemonic prefix (w/), and noprefix
    assert runner.parse_diff_changed_lines('--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n-a\n+b\n') == {
        "caf\u00e9.py": {1}
    }
    assert runner.parse_diff_changed_lines("--- a/my file.py\t\n+++ b/my file.py\t\n@@ -1 +1 @@\n-a\n+b\n") == {
        "my file.py": {1}
    }
    assert runner.parse_diff_changed_lines("--- a/a.py\n+++ w/a.py\n@@ -1 +1 @@\n-a\n+b\n") == {"a.py": {1}}
    assert runner.parse_diff_changed_lines("--- a.py\n+++ a.py\n@@ -1 +1 @@\n-a\n+b\n") == {"a.py": {1}}

    # 3. `diff --git` header resets state after an incomplete/orphan `--- a/orphan.py` header (R4)
    orphan_diff = "\n".join([
        "--- a/orphan.py",
        "diff --git a/real.py b/real.py",
        "+++ b/real.py",
        "@@ -1 +1 @@",
        "-x",
        "+y",
    ])
    assert runner.parse_diff_changed_lines(orphan_diff) == {}
    _, del_orphan, _ = runner._parse_diff_hunks(
        "--- a/orphan.py\ndiff --git a/real.py b/real.py\n--- /dev/null\n+++ b/real.py\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    assert del_orphan == {("real.py", "real.py"): {1, 2}}
    # Also verify `diff --git` resets `current_path` so an orphan hunk after `diff --git` without headers is ignored (R4)
    assert runner.parse_diff_changed_lines(
        "--- a/prev.py\n+++ b/prev.py\n@@ -1 +1 @@\n-a\n+b\ndiff --git a/next.py b/next.py\n@@ -10 +10 @@\n-c\n+d\n"
    ) == {"prev.py": {1}}


def test_diff_handles_git_noprefix_spaced_paths_and_blind_mutants(tmp_path):
    repo = str(tmp_path / "repo")
    lists = str(tmp_path / "lists")
    os.makedirs(repo)
    os.makedirs(lists)
    git(repo, "init", "-q")
    git(repo, "config", "diff.noprefix", "true")
    git(repo, "config", "diff.mnemonicPrefix", "true")

    write(os.path.join(repo, "my calc.py"), "def f(a, b):\n    if a < 0:\n        return 0\n    return a + b\n")
    write(os.path.join(repo, "test_calc.py"), "def test_ok():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    # Modify "my calc.py" and delete the `if a < 0:` guard so `m_guard` becomes BLIND
    write(os.path.join(repo, "my calc.py"), "def f(a, b):\n    return a + b + 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "drop guard and edit return")

    write(
        os.path.join(lists, "m.py"),
        "MUTANTS = [\n"
        '    ("m_guard", "my calc.py", "if a < 0:", "if a <= 0:", ["test_calc.py"]),\n'
        '    ("m_ret", "my calc.py", "return a + b + 0", "return a - b", ["test_calc.py"]),\n'
        "]\n",
    )
    # --diff HEAD~1 --check must find both m_ret (changed line) and m_guard (BLIND in changed file), exiting 1
    code, out = run_cli(repo, lists, "--diff", "HEAD~1", "--check")
    assert code == 1, out
    assert "BLIND" in out and "m_guard" in out, out
    assert "2 mutants, 1 apply (the original occurs once), 1 BLIND" in out, out


def test_diff_ignores_a_textconv_driver_from_git_config(tmp_path):
    # REFACTOR-QUEUE A2, follow-up: git diff runs a configured textconv driver unless it is told not to, and the patch then
    # describes converted text. With a driver that keeps only each file's first line, a change on line 2 was missing from
    # the diff, so a mutant on that line was never selected. --no-textconv on the command line beats the config.
    runner = load_runner()
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    git(repo, "init", "-q")
    git(repo, "config", "diff.seatconv.textconv", 'f() { head -n 1 "$1"; }; f')
    os.makedirs(os.path.join(repo, ".git", "info"), exist_ok=True)
    write(os.path.join(repo, ".git", "info", "attributes"), "*.py diff=seatconv\n")
    write(os.path.join(repo, "calc.py"), "def f(a, b):\n    return a + b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    write(os.path.join(repo, "calc.py"), "def f(a, b):\n    return a - b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "line two")
    # Control: the driver is live here, so a plain git diff does not show the change on line 2.
    assert "+    return a - b" not in git(repo, "diff", "-U0", "HEAD~1", "HEAD")
    assert "+    return a - b" in git(repo, "diff", "--no-textconv", "-U0", "HEAD~1", "HEAD")
    no_hooks = str(tmp_path / "no-hooks")
    os.makedirs(no_hooks)
    assert runner.diff_changed_lines(runner.Repo(repo, no_hooks), "HEAD~1", "HEAD") == {"calc.py": {2}}


def _repo_with_src_and_dst_prefixes(tmp_path):
    # A repo whose own git config sets diff.srcPrefix and diff.dstPrefix, one commit that changes line 2 of calc.py, and a
    # list with one mutant on that line. Returns (repo, lists).
    repo = str(tmp_path / "repo")
    lists = str(tmp_path / "lists")
    os.makedirs(repo)
    os.makedirs(lists)
    git(repo, "init", "-q")
    write(os.path.join(repo, "calc.py"), "def add(a, b):\n    return a + b\n")
    write(os.path.join(repo, "test_calc.py"), "import calc\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    write(os.path.join(repo, "calc.py"), "def add(a, b):\n    return a + b + 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "line two")
    git(repo, "config", "diff.srcPrefix", "x/")
    git(repo, "config", "diff.dstPrefix", "y/")
    # Control: the setting is live here, so a plain git diff names the file x/calc.py and y/calc.py. git older than 2.45
    # does not know the two settings; there the control is not asked for and the rest must hold all the same.
    raw = git(repo, "diff", "-U0", "HEAD~1", "HEAD")
    git_version = tuple(int(part) for part in git(repo, "--version").split()[2].split(".")[:2])
    assert ("--- x/calc.py" in raw and "+++ y/calc.py" in raw) or git_version < (2, 45), raw
    write(
        os.path.join(lists, "m.py"),
        'MUTANTS = [("m:ret", "calc.py", "return a + b + 0", "return a - b", ["test_calc.py"])]\n',
    )
    return repo, lists


def test_diff_ignores_src_and_dst_prefixes_from_git_config(tmp_path):
    # nxxxxx's review of #652: the runner pinned diff.noprefix and diff.mnemonicPrefix but not diff.srcPrefix and
    # diff.dstPrefix. With x/ and y/ the changed file was named y/calc.py, which is no file a mutant is registered for.
    # --src-prefix and --dst-prefix on the command line beat the config.
    runner = load_runner()
    repo, _lists = _repo_with_src_and_dst_prefixes(tmp_path)
    no_hooks = str(tmp_path / "no-hooks")
    os.makedirs(no_hooks)
    assert runner.diff_changed_lines(runner.Repo(repo, no_hooks), "HEAD~1", "HEAD") == {"calc.py": {2}}


def test_cli_selects_the_mutant_on_a_changed_line_under_src_and_dst_prefixes(tmp_path):
    # The same repo through the command line. Before the fix --diff refused with zero mutants, and with --allow-empty it
    # passed with zero. The mutant on the changed line is selected either way: neither run may report zero.
    repo, lists = _repo_with_src_and_dst_prefixes(tmp_path)
    for extra in ((), ("--allow-empty",)):
        code, out = run_cli(repo, lists, "--diff", "HEAD~1", "--check", *extra)
        assert code == 0, out
        assert "1 mutants from 1 list(s)" in out, out
        assert "0 mutants matched diff" not in out and "0 mutants overlap diff" not in out, out


def test_diff_ignores_inter_hunk_context_from_git_config(tmp_path):
    # With diff.interHunkContext git fuses hunks that stand close together and prints the unchanged lines between them
    # inside the hunk, so the runner counted lines 2 to 4 as changed where lines 2 and 4 were. With
    # diff.suppressBlankEmpty the blank line between them prints as an empty line. --inter-hunk-context=0 beats both.
    runner = load_runner()
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    git(repo, "init", "-q")
    write(os.path.join(repo, "calc.py"), "A = 1\nB = 2\n\nC = 3\nD = 4\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    write(os.path.join(repo, "calc.py"), "A = 1\nB = 20\n\nC = 30\nD = 4\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "two lines, a blank one between")
    git(repo, "config", "diff.interHunkContext", "3")
    git(repo, "config", "diff.suppressBlankEmpty", "true")
    # Control: the setting is live here. A plain git diff prints one hunk of three lines.
    assert "@@ -2,3 +2,3 @@" in git(repo, "diff", "-U0", "HEAD~1", "HEAD")
    no_hooks = str(tmp_path / "no-hooks")
    os.makedirs(no_hooks)
    assert runner.diff_changed_lines(runner.Repo(repo, no_hooks), "HEAD~1", "HEAD") == {"calc.py": {2, 4}}


def test_diff_does_not_cut_a_body_line_at_a_form_feed(tmp_path):
    # splitlines() cuts a line at a form feed as well as at a newline. One added line that carries two of them then read
    # as an added line, a "--- " header and a "+++ " header, and every hunk after it in the file was booked to fake.py.
    runner = load_runner()
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    git(repo, "init", "-q")
    write(os.path.join(repo, "calc.py"), "A = 1\nB = 2\nC = 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    write(os.path.join(repo, "calc.py"), "# \x0c--- a/fake.py\x0c+++ b/fake.py\nA = 1\nB = 2\nC = 30\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a form feed line, then a change")
    # Control: the patch carries both header shapes inside one added line.
    assert "+# \x0c--- a/fake.py\x0c+++ b/fake.py\n" in git(repo, "diff", "--text", "-U0", "HEAD~1", "HEAD")
    no_hooks = str(tmp_path / "no-hooks")
    os.makedirs(no_hooks)
    assert runner.diff_changed_lines(runner.Repo(repo, no_hooks), "HEAD~1", "HEAD") == {"calc.py": {1, 4}}


def test_audit_uncovered_predicates_fails_closed_on_undecodable_or_syntax_error(tmp_path):
    repo = str(tmp_path / "repo")
    lists = str(tmp_path / "lists")
    os.makedirs(repo)
    os.makedirs(lists)
    git(repo, "init", "-q")

    write(os.path.join(repo, "mod.py"), "def f(a, b):\n    return a\n")
    write(os.path.join(repo, "other.py"), "x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    write(os.path.join(lists, "m.py"), 'MUTANTS = [("m0", "other.py", "x = 1", "x = 2", ["other.py"])]\n')

    # 1. Invalid UTF-8 bytes in modified .py file -> exits 2 (refused)
    with open(os.path.join(repo, "mod.py"), "wb") as f:
        f.write(b"# caf\xe9\ndef f(a, b):\n    if a and b:\n        return a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "latin1")
    rc_utf8, out_utf8 = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc_utf8 == 2, out_utf8
    assert "is not valid UTF-8" in out_utf8, out_utf8

    # 2. UTF-16 BOM binary .py file -> exits 2 (refused) via git diff --text
    with open(os.path.join(repo, "mod.py"), "wb") as f:
        f.write("def f(a, b):\n    if a and b:\n        return a\n".encode("utf-16"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "utf16")
    rc_u16, out_u16 = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc_u16 == 2, out_u16
    assert "is not valid UTF-8" in out_u16, out_u16

    # 3. SyntaxError in modified .py file -> exits 2 (refused)
    write(os.path.join(repo, "mod.py"), "def f(a, b):\n    if a and b\n        return a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "syntax error")
    rc_syn, out_syn = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc_syn == 2, out_syn
    assert "SyntaxError" in out_syn, out_syn


def test_audit_uncovered_predicates_node_kinds_subconditions_pyw_shebang_and_deleted_guard(tmp_path):
    repo = str(tmp_path / "repo")
    lists = str(tmp_path / "lists")
    os.makedirs(repo)
    os.makedirs(lists)
    git(repo, "init", "-q")

    write(os.path.join(repo, "kinds.py"), "def k(x, y, z):\n    return 0\n")
    write(os.path.join(repo, "gui.pyw"), "def g(x):\n    return x\n")
    write(os.path.join(repo, "tool_script"), "#!/usr/bin/env python3\ndef s(x):\n    return x\n")
    write(os.path.join(repo, "del_guard.py"), "def d(x):\n    if x < 0:\n        return -1\n    return x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    # Modify kinds.py with While, Assert, IfExp, CompIf, MatchGuard, and a 3-operand BoolOp;
    # modify gui.pyw and extensionless tool_script; delete the guard in del_guard.py.
    write(
        os.path.join(repo, "kinds.py"),
        "def k(x, y, z):\n"
        "    while x > 0:\n"
        "        x -= 1\n"
        "    assert y != 0\n"
        "    v = 1 if z > 0 else 0\n"
        "    items = [i for i in range(5) if i > 2]\n"
        "    match v:\n"
        "        case n if n > 0:\n"
        "            pass\n"
        "    if x == 0 and y > 0 and z > 0:\n"
        "        return v + len(items)\n"
        "    return 0\n",
    )
    write(os.path.join(repo, "gui.pyw"), "def g(x):\n    if x > 10:\n        return 10\n    return x\n")
    write(os.path.join(repo, "tool_script"), "#!/usr/bin/env python3\ndef s(x):\n    if x < 0:\n        return 0\n    return x\n")
    write(os.path.join(repo, "del_guard.py"), "def d(x):\n    return x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "predicates and deletions")

    # Register only a partial mutant covering operand `x == 0` of `if x == 0 and y > 0 and z > 0:`
    write(
        os.path.join(lists, "m.py"),
        'MUTANTS = [("m_partial", "kinds.py", "x == 0", "x != 0", ["kinds.py"])]\n',
    )
    rc, out = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc == 1, out
    for expected in ("While", "Assert", "IfExp", "CompIf", "MatchGuard", "BoolOp", "gui.pyw", "tool_script", "del_guard.py"):
        assert expected in out, f"expected {expected!r} in:\n{out}"
    # Uncovered BoolOp operands must be named explicitly (F4 / t16 P1)
    assert "BoolOp[y > 0, z > 0]" in out, out


def test_audit_uncovered_predicates_edit_span_rename_deletion_and_binary_skip(tmp_path):
    repo = str(tmp_path / "repo")
    lists = str(tmp_path / "lists")
    os.makedirs(repo)
    os.makedirs(lists)
    git(repo, "init", "-q")

    write(os.path.join(repo, "mod.py"), "def f(a, b, c):\n    return 0\n")
    write(os.path.join(repo, "old_name.py"), "def g(a, b):\n    if a and b:\n        return 1\n    return 0\n")
    with open(os.path.join(repo, "latin1_del.py"), "wb") as f:
        f.write(b"# caf\xe9\ndef h(a, b):\n    if a and b:\n        return 1\n    return 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    # 1. t16 P1 & P2: full-line original that only mutates `a` or padded original that only mutates `return 0`
    #    must still report uncovered BoolOp operands and If predicate.
    write(os.path.join(repo, "mod.py"), "def f(a, b, c):\n    if a and b and c:\n        return 0\n    return 1\n")
    # Also add an extensionless binary file in the diff (t16 P3): must be skipped, NOT refuse the audit!
    with open(os.path.join(repo, "blob"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\xfd")
    # Also rename old_name.py -> new_name.py while deleting its `if a and b:` guard (t16 P5)
    git(repo, "mv", "old_name.py", "new_name.py")
    write(os.path.join(repo, "new_name.py"), "def g(a, b):\n    return 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "predicates, binary blob, and rename+delete")

    # P1: full-line original `if a and b and c:` -> `if False and b and c:` only mutates `a`
    write(
        os.path.join(lists, "m.py"),
        "MUTANTS = [\n"
        '    ("m_p1", "mod.py", "    if a and b and c:", "    if False and b and c:", ["mod.py"]),\n'
        "]\n",
    )
    rc_p1, out_p1 = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc_p1 == 1, out_p1
    assert "UNCOVERED mod.py:2 (BoolOp[b, c])" in out_p1, out_p1
    assert "UNCOVERED new_name.py:2" in out_p1, out_p1

    # P2: padded multi-line original that only mutates `return 0` -> `return 2` leaves line 2's If & BoolOp uncovered
    write(
        os.path.join(lists, "m.py"),
        "MUTANTS = [\n"
        '    ("m_p2", "mod.py", "    if a and b and c:\\n        return 0", "    if a and b and c:\\n        return 2", ["mod.py"]),\n'
        "]\n",
    )
    rc_p2, out_p2 = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc_p2 == 1, out_p2
    assert "UNCOVERED mod.py:2 (If, BoolOp[a, b, c])" in out_p2, out_p2

    # 2. t16 P6: deleting a guard from a non-UTF-8 base file must fail closed (exit 2)
    with open(os.path.join(repo, "latin1_del.py"), "wb") as f:
        f.write(b"# caf\xe9\ndef h(a, b):\n    return 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "delete guard in latin1 base file")
    rc_p6, out_p6 = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc_p6 == 2, out_p6
    assert "latin1_del.py" in out_p6 and "is not valid UTF-8" in out_p6, out_p6

    # 3. t29 P7 (N8): guard removed inside a replace hunk (`if a and b:\n    return a` -> `log(a)`) is reported uncovered
    write(os.path.join(repo, "replace_guard.py"), "def r(a, b):\n    if a and b:\n        return a\n    return b\n")
    git(repo, "add", "replace_guard.py")
    git(repo, "commit", "-q", "-m", "add replace_guard")
    write(os.path.join(repo, "replace_guard.py"), "def r(a, b):\n    log(a)\n    return b\n")
    git(repo, "add", "replace_guard.py")
    git(repo, "commit", "-q", "-m", "replace guard with log(a)")
    rc_p7, out_p7 = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc_p7 == 1, out_p7
    assert "UNCOVERED replace_guard.py:2" in out_p7 and "if a and b:" in out_p7, out_p7

    # 4. t28 (N12): dispatch-style branches (`{...}.get(...)`, `{True: ...}[...]`, `filter(None, ...)`) are reported uncovered
    write(
        os.path.join(repo, "dispatch_mod.py"),
        "def disp(x):\n"
        "    for _ in filter(None, (x,)):\n"
        "        return {True: 1, False: 0}[bool(x)]\n"
        "    return {int: lambda v: v}.get(type(x), lambda _: 0)(x)\n",
    )
    git(repo, "add", "dispatch_mod.py")
    git(repo, "commit", "-q", "-m", "add dispatch branches")
    rc_p28, out_p28 = run_cli(repo, lists, "--audit-uncovered-predicates", "HEAD~1")
    assert rc_p28 == 1, out_p28
    assert "UNCOVERED dispatch_mod.py:2 (Dispatch)" in out_p28, out_p28
    assert "UNCOVERED dispatch_mod.py:3 (Dispatch)" in out_p28, out_p28
    assert "UNCOVERED dispatch_mod.py:4 (Dispatch)" in out_p28, out_p28


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
