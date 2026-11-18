"""Mutation runner: each mutant is applied to a throwaway checkout of the commit under test, never to a committed file.

    python tools/mutants/run.py                                every list, at HEAD
    python tools/mutants/run.py --list mesh_audit              one list
    python tools/mutants/run.py --only "mutate6:M12"           the mutants whose name holds that text
    python tools/mutants/run.py --check                        only: does each original occur exactly once?
    python tools/mutants/run.py --rev 1a1ea9f --jobs 3 --timeout 300
    python tools/mutants/run.py --timeout 30 --control-timeout 600  a short bound per mutant, a long one per control

Run it with the project's venv: pytest runs under the interpreter that runs this file, with the repository's own
tests/conftest.py, so a run needs MESHY_TEMPLATE_DSN as every pytest run here does. No git command and no test run of
this tool sees a GIT_* variable (a hook that exports GIT_DIR would otherwise make `git -C <checkout>` act on the hook's
repository). A test run also loses what would steer pytest or Python unseen, as tools/ci/local_gate.py's suite_env:
every PYTEST_* variable (PYTEST_ADDOPTS="--basetemp=DIR" makes pytest empty DIR), every PYTHON* one but the encoding
ones, and DJANGO_SETTINGS_MODULE. Every place pytest writes is the run's own: --basetemp, --junitxml and --log-file
inside the work folder (an ini log_file is opened with mode 'w' at configuration time), the cache provider off, and
the repository's addopts cleared with `-o addopts=` (they could name --debug=FILE or a plugin's report file). The
three paths are given relative to the checkout: pytest expands $VARIABLES and ~ in --junitxml, and the work folder's
path, under TMPDIR, may hold a `$`.

Databases: a test run never sees MESHY_TEST_DSN, MESHY_TEST_DSN_EXPLICIT, MESHY_DSN, DATABASE_URL,
MESHY_CLONE_TEMPLATE or MESHY_LIVE_MODELS, so tests/conftest.py makes and drops its own database on the
MESHY_TEMPLATE_DSN server every time; with MESHY_NO_TEST_DB set (conftest would then make none, and the tests would
write into the database MESHY_TEST_DSN names) the run is refused before anything starts (--check excepted).
Each run sets MESHY_TEST_DB_RECORD to a file in its scratch folder, where tests/conftest.py appends the name of each
database it makes before making it; after the run (its whole process group ended) any of those that still exists,
which a pytest killed before pytest_sessionfinish leaves, is dropped through the MESHY_TEMPLATE_DSN server, and one
that cannot be is a LEFTOVER (exit 1).

A list is a module in tools/mutants/lists/ whose MUTANTS holds (name, path, original, replacement, [test files]):
`path` is relative to the repository, `original` must occur in that file exactly once at the commit under test, and
the test files (or node ids) are what must fail once `original` is replaced. A test entry is a test file (or a node
id in one) inside the repository, written as pytest is given it: one that starts with `-` or `@` (pytest expands `@FILE`
into the arguments FILE holds, after the runner's own), is absolute, climbs out with `..`, holds a backslash or is not
in normal form refuses the list; one that is not a file in the checkout (a folder is collected through its links)
stops the run.
For each mutant:

  1. BLIND when the original does not occur exactly once in the file at the commit (no checkout, no test run);
  2. otherwise `git worktree add --detach <work>/<id> <commit>`, the one replacement written into that checkout's copy
     of the file, `python -m pytest -x -q -p no:cacheprovider <test files>` run in it, and the checkout removed with
     `git worktree remove --force`. The file is written only when the commit's own tree holds it as a regular file
     (`git ls-tree`: a link, mode 120000, is an ERROR before any checkout, also on Windows, where git with
     core.symlinks=false checks a link out as a plain file) and its path in the checkout leads to a file inside the
     checkout (realpath, the second guard);
  3. judged from pytest's junit report, not its console: CAUGHT when a test failed (or its setup did), when the
     tests cannot import the mutated file (a collection error whose record names that file), or when they run past
     --timeout; SURVIVED when they pass; ERROR when the run proves nothing: pytest interrupted (exit 2 with no
     collection error), its internal error (exit 3), ended by a signal, a usage error or no test collected, a
     collection error that does not come from the mutated file, no checkout.

Before any mutant, each distinct set of test files runs once on an unmutated checkout (the control); if one fails,
or runs past --control-timeout, no mutant runs. --control-timeout defaults to --timeout but never less than 300 s:
a short --timeout meant to end a hanging mutant fast does not refuse the run because the unmutated tests ran slow
under load. A control that passed but took longer than --timeout says so on its line: a mutant of those tests that
times out proves nothing. A CAUGHT line says how: on an assertion (an AssertionError, a bare `assert`, or `DID NOT
RAISE`), on another error, or by the timeout. --assertions-only also counts the others as not caught.

A test run's process group is ended with it. pytest leads its own group; once it has exited (its tests ended, the
timeout, or a stop), whatever else is alive in the group (a child a test started) gets SIGTERM, then SIGKILL after
GROUP_GRACE_S, and only then is pytest reaped. pytest is waited for without being reaped (waitid with WNOWAIT), so
its pid, which is the group's id, cannot pass to another process while the group is signalled. On Linux the runner
is a child subreaper, so what the group leaves is adopted by it and reaped (by pid) before the group counts as gone,
even where PID 1 reaps nothing; on macOS launchd adopts and reaps it. A process that left the group (a new session)
is not followed. On Windows only pytest itself is waited for and ended (best effort).

SIGTERM or SIGINT stops the run: no further test run starts, each running one is interrupted (so tests/conftest.py
drops its database) and killed if it is still there after STOP_GRACE_S, every checkout is removed, then the work
folder; the exit code is 128 + the signal's number.

Every temp folder of a run (checkouts, pytest's tmp_path, an empty bytecode prefix per run) is made inside one work
folder that is removed at the end, and no worktree stays registered. Bytecode: PYTHONDONTWRITEBYTECODE=1 and a fresh,
empty PYTHONPYCACHEPREFIX for every run, so no .pyc of one mutant is ever read for the next. Git runs with
core.hooksPath set to an empty folder: a throwaway checkout runs no hook.

Exit 0 only when every control passed and every mutant was CAUGHT; 1 otherwise; 2 when the command is wrong.
Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import codecs
import concurrent.futures
import importlib.util
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
LISTS_DIR = os.path.join(HERE, "lists")
PYTEST_FLAGS = ["-x", "-q", "-p", "no:cacheprovider"]
DEFAULT_TIMEOUT_S = 300
#: A tree entry's mode for a symbolic link, and the modes of a regular file: only a regular file is ever written.
LINK_MODE = "120000"
FILE_MODES = ("100644", "100755")
#: After the timeout pytest gets an interrupt and this long to finish, so tests/conftest.py drops the run's database.
INTERRUPT_GRACE_S = 30
#: On SIGTERM or SIGINT to the runner: how long each running test run gets to end its session before it is killed.
STOP_GRACE_S = 10
#: After a test run's leader exits: how long the rest of its process group gets after SIGTERM before SIGKILL.
GROUP_GRACE_S = 5
#: The only PYTHON* variables a test run keeps (tools/ci/local_gate.py's _PYTHON_KEPT).
PYTHON_KEPT = {"PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO"}
#: Set in the caller's environment, these refuse the run: with MESHY_NO_TEST_DB tests/conftest.py makes no database
#: of the run's own, and the tests write into whichever one MESHY_TEST_DSN names.
REFUSED_VARS = ("MESHY_NO_TEST_DB",)
#: Never handed to a test run: each points the tests at a database other than the one tests/conftest.py makes and
#: drops for the run (MESHY_LIVE_MODELS: a live run writes rows and calls models; a mutant's run does neither).
DB_VARS = ("MESHY_TEST_DSN", "MESHY_TEST_DSN_EXPLICIT", "MESHY_DSN", "DATABASE_URL", "MESHY_CLONE_TEMPLATE",
           "MESHY_LIVE_MODELS")

ASSERTION = re.compile(r"^E\s+(?:AssertionError\b|assert\b|Failed: DID NOT RAISE)", re.M)
ERROR_NAME = re.compile(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b", re.M)
FIRST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
SUMMARY = re.compile(r"^=*\s*(\d+ (?:passed|failed|error|errors|skipped|deselected|xfailed|xpassed|warnings?)\b.*?)\s*=*$",
                     re.M)


class Refused(Exception):
    """The command cannot run as asked (exit 2)."""


class Stopped(Exception):
    """SIGTERM or SIGINT came: the run is being stopped (exit 128 + the signal's number)."""

    def __init__(self, signum: int):
        super().__init__("signal %d" % signum)
        self.signum = signum


#: The signal state: `armed` while a signal may interrupt the main thread (never during a cleanup).
STOP = {"armed": False, "signum": None}


def _on_signal(signum, frame) -> None:
    """The first SIGTERM or SIGINT interrupts the main thread, once; a later one is only noted."""
    if STOP["signum"] is None:
        STOP["signum"] = signum
    if STOP["armed"]:
        STOP["armed"] = False
        raise Stopped(signum)


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The caller's environment without any GIT_* variable, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(extra or {})
    return env


def run_env(extra: dict[str, str]) -> dict[str, str]:
    """A test run's environment: clean_env without any PYTEST_* variable, any PYTHON* one but PYTHON_KEPT, and
    DJANGO_SETTINGS_MODULE (the local gate's rule), without DB_VARS and REFUSED_VARS (each run makes its own
    database on the MESHY_TEMPLATE_DSN server), with user site-packages off, plus `extra`."""
    env = {k: v for k, v in clean_env().items()
           if not (k.upper().startswith(("PYTEST_", "PYTHON")) and k.upper() not in PYTHON_KEPT)
           and k.upper() != "DJANGO_SETTINGS_MODULE"
           and k.upper() not in DB_VARS + REFUSED_VARS}
    env["PYTHONNOUSERSITE"] = "1"
    env.update(extra)
    return env


class Repo:
    """The repository the checkouts are made from. Its git commands never see a GIT_* variable or run a hook."""

    def __init__(self, path: str, no_hooks: str):
        self.path = path
        self.no_hooks = no_hooks
        self.lock = threading.Lock()          # one worktree add or remove at a time

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", "-c", "core.hooksPath=" + self.no_hooks, "-C", self.path, *args]
        p = subprocess.run(cmd, capture_output=True, env=clean_env(), stdin=subprocess.DEVNULL)
        if check and p.returncode != 0:
            raise Refused("git %s failed: %s" % (" ".join(args), p.stderr.decode("utf-8", "replace").strip()))
        return p

    def add_checkout(self, sha: str, dest: str) -> None:
        with self.lock:
            self.git("worktree", "add", "--detach", "--quiet", dest, sha)

    def remove_checkout(self, dest: str) -> str | None:
        """Remove a checkout and its registration; the error text when either is left."""
        with self.lock:
            p = self.git("worktree", "remove", "--force", dest, check=False)
        shutil.rmtree(dest, ignore_errors=True)
        if p.returncode != 0:
            return p.stderr.decode("utf-8", "replace").strip() or "git worktree remove exit %d" % p.returncode
        return None

    def registered_under(self, folder: str) -> list[str]:
        """The worktrees registered under `folder`."""
        p = self.git("worktree", "list", "--porcelain", check=False)
        real = os.path.realpath(folder)
        out = []
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
                if os.path.realpath(path).startswith(real + os.sep) or os.path.realpath(path) == real:
                    out.append(path)
        return out


# ---- lists

def load_lists(lists_dir: str, names: list[str] | None) -> list[tuple[str, tuple]]:
    """[(list name, (name, path, original, replacement, tests))] for the named lists, or every list in the folder."""
    sys.dont_write_bytecode = True                         # no __pycache__ beside the lists
    if not os.path.isdir(lists_dir):
        raise Refused("no lists folder: %s" % lists_dir)
    found = sorted(f[:-3] for f in os.listdir(lists_dir) if f.endswith(".py") and not f.startswith("_"))
    for n in names or []:
        if n not in found:
            raise Refused("no list %r in %s; the lists are: %s" % (n, lists_dir, ", ".join(found)))
    out: list[tuple[str, tuple]] = []
    seen: dict[str, str] = {}
    for list_name in names or found:
        spec = importlib.util.spec_from_file_location("mutant_list_" + list_name, os.path.join(lists_dir, list_name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entries = getattr(module, "MUTANTS", None)
        if not isinstance(entries, list):
            raise Refused("list %s has no MUTANTS list" % list_name)
        for entry in entries:
            problem = _entry_problem(entry)
            if problem:
                raise Refused("list %s: %s: %r" % (list_name, problem, entry[:1] if isinstance(entry, tuple) else entry))
            if entry[0] in seen:
                raise Refused("the mutant name %r is in list %s and in list %s" % (entry[0], seen[entry[0]], list_name))
            seen[entry[0]] = list_name
            out.append((list_name, (entry[0], entry[1], entry[2], entry[3], list(entry[4]))))
    return out


def _entry_problem(entry: object) -> str | None:
    if not (isinstance(entry, tuple) and len(entry) == 5):
        return "an entry is not (name, path, original, replacement, [test files])"
    name, path, original, replacement, tests = entry
    if not (isinstance(name, str) and name.strip()):
        return "a mutant has no name"
    if not (isinstance(path, str) and path and not os.path.isabs(path) and ".." not in path.replace("\\", "/").split("/")):
        return "the path is not a file inside the repository"
    if not (isinstance(original, str) and original and isinstance(replacement, str) and original != replacement):
        return "the original is empty or the replacement is the same text"
    if not (isinstance(tests, (list, tuple)) and tests and all(isinstance(t, str) and t for t in tests)):
        return "no test files"
    for t in tests:
        problem = bad_test_entry(t)
        if problem:
            return problem
    return None


def bad_test_entry(entry: str) -> str | None:
    """Why a test entry is not a test file or node id inside the repository, or None. A leading `-` would be read
    as an option and a leading `@` as a file of arguments (pytest expands `@args.txt`, and its `--basetemp` would
    then win over the runner's); a path must be relative, with no `..`, no backslash (on POSIX a file may be named
    `tests\\test_x.py`) and no NUL, and in normal form, so the path pytest opens is the path checked here."""
    if entry.startswith(("-", "@")):
        return "the test entry %r starts with %r: pytest would read it as an option or a file of arguments" % (
            entry, entry[0])
    path = entry.split("::", 1)[0]
    if "\\" in path:
        return "the test entry %r has a backslash: write the path with `/`, as pytest is given it" % entry
    if (not path or "\0" in entry or path.startswith("/") or re.match(r"[A-Za-z]:", path)
            or ".." in path.split("/")):
        return "the test entry %r is not a path inside the repository" % entry
    if posixpath.normpath(path) != path:
        return "the test entry %r is not in normal form; write it %r" % (entry, posixpath.normpath(path))
    return None


# ---- diff filter and AST predicate coverage audit

DIFF_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_PREFIX_RE = re.compile(r"^(?:[abciw]/(?=.)|/dev/null$)")
_SHEBANG_PY_RE = re.compile(rb"^#![^\r\n]*python", re.IGNORECASE)
_NON_PY_EXT_RE = re.compile(r"^(?!.*\.(?:py|pyw)$)[^/]*\.[^/]+$")
_SKIP_TEST_OR_LIST_RE = re.compile(
    r"^(?:tests/|tools/mutants/lists/|(?:.*/)?tests/(?:test_[^/]*|conftest\.py)$)"
)


def _unquote_diff_path(raw: str) -> str:
    """Unescape C-quoted git diff path (`"b/caf\\303\\251.py"`) and strip leading prefix or trailing tab."""
    s = raw.rstrip("\t").strip()
    if s.startswith('"'):
        decoded_bytes, _ = codecs.escape_decode(s[1:-1].encode("utf-8"))
        s = decoded_bytes.decode("utf-8", "replace")
    return _GIT_PREFIX_RE.sub("", s).replace("\\", "/")


def _parse_diff_hunks(
    diff_text: str,
) -> tuple[dict[str, set[int]], dict[tuple[str, str], set[int]], list[tuple[str, str, set[int], set[int]]]]:
    """Parse unified diff (`git diff -U0`) into `(changed_new_lines, deleted_old_lines, replace_hunks)`."""
    changed: dict[str, set[int]] = {}
    deleted: dict[tuple[str, str], set[int]] = {}
    replaced: list[tuple[str, str, set[int], set[int]]] = []
    old_path = ""
    current_path = ""
    expect_plus_header = False
    rem_body = 0
    # Split on "\n" only: git ends every patch line with it. splitlines() also cuts at a form feed and its kin, so one
    # added line could print as a "--- " line and a "+++ " line and move the hunks after it to another file.
    for raw_line in diff_text.split("\n"):
        if rem_body > 0 and raw_line[:1] in ("-", "+", " "):
            rem_body -= 1
            continue
        rem_body = 0

        if raw_line.startswith("diff "):
            old_path = ""
            current_path = ""
            expect_plus_header = False
        elif raw_line.startswith("--- "):
            old_path = _unquote_diff_path(raw_line[4:])
            expect_plus_header = True
        elif expect_plus_header and raw_line.startswith("+++ "):
            expect_plus_header = False
            current_path = _unquote_diff_path(raw_line[4:])
            if current_path:
                changed.setdefault(current_path, set())
        else:
            expect_plus_header = False
            m = DIFF_HUNK_RE.match(raw_line)
            if m is not None and current_path:
                old_start = int(m.group(1))
                old_count = int(m.groups("1")[1])
                start = int(m.group(3))
                new_count = int(m.groups("1")[3])
                rem_body = old_count + new_count
                del_key = (old_path or current_path, current_path)
                if new_count > 0:
                    new_span = set(range(start, start + new_count))
                    changed[current_path].update(new_span)
                    if old_count > 0:
                        replaced.append((del_key[0], del_key[1], set(range(old_start, old_start + old_count)), new_span))
                else:
                    deleted.setdefault(del_key, set()).update(range(old_start, old_start + old_count))
    return changed, deleted, replaced


def parse_diff_changed_lines(diff_text: str) -> dict[str, set[int]]:
    """Parse unified diff (`git diff -U0`) into `{path: {1-based modified/added line numbers}}`."""
    changed, _, _ = _parse_diff_hunks(diff_text)
    return changed


def _diff_raw(repo: Repo, base_ref: str, sha: str) -> tuple[str, str]:
    if not base_ref.strip() or base_ref.startswith("-"):
        raise Refused("bad diff base ref %r" % base_ref)
    base_sha = repo.git("rev-parse", "--verify", "--quiet", base_ref + "^{commit}", check=False).stdout.decode(
        "utf-8", "replace").strip()
    if not base_sha:
        raise Refused("no commit %r in %s" % (base_ref, repo.path))
    p = repo.git(
        "-c", "diff.noprefix=false",
        "-c", "diff.mnemonicPrefix=false",
        "-c", "core.quotepath=false",
        # The patch's shape is fixed here, which beats any git config a seat holds: the a/ b/ prefixes the parser strips
        # (diff.srcPrefix and diff.dstPrefix named the file y/calc.py and dropped every mutant on it; nxxxxx's review of
        # #652), and no context lines fused into a hunk by diff.interHunkContext.
        "diff", "-M10%", "--text", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
        "--src-prefix=a/", "--dst-prefix=b/", "--inter-hunk-context=0", "%s...%s" % (base_sha, sha),
    )
    return base_sha, p.stdout.decode("utf-8", "replace")


def diff_changed_lines(repo: Repo, base_ref: str, sha: str) -> dict[str, set[int]]:
    """Resolve `base_ref` and return modified files and changed lines in `git diff -U0 <base_ref>...<sha>`."""
    _, diff_text = _diff_raw(repo, base_ref, sha)
    return parse_diff_changed_lines(diff_text)


def snippet_line_span(source: str, original: str) -> tuple[int, int] | None:
    """Return the inclusive 1-based `(start_line, end_line)` of `original` when it occurs once in `source`."""
    norm_src = source.replace("\r\n", "\n")
    norm_orig = original.replace("\r\n", "\n")
    if norm_src.count(norm_orig) != 1:
        return None
    idx = norm_src.find(norm_orig)
    start_line = norm_src.count("\n", 0, idx) + 1
    end_line = start_line + norm_orig.rstrip("\n").count("\n")
    return start_line, end_line


def filter_mutants_by_diff(repo: Repo, base_ref: str, sha: str,
                           mutants: list[tuple[str, tuple]]) -> list[tuple[str, tuple]]:
    """Filter `mutants` to those whose target file is in `git diff -U0 <base_ref>...<sha>` and whose
    `original` snippet either overlaps changed lines or is BLIND at `sha` (so BLIND mutants are never silently dropped)."""
    changed = diff_changed_lines(repo, base_ref, sha)
    selected: list[tuple[str, tuple]] = []
    for list_name, mutant in mutants:
        _, path, original, _, _ = mutant
        norm_path = path.replace("\\", "/")
        if norm_path not in changed:
            continue
        lines = changed[norm_path]
        source = repo.git("show", "%s:%s" % (sha, norm_path), check=False).stdout.decode("utf-8", "replace")
        span = snippet_line_span(source, original)
        if span is None or any(ln in lines for ln in range(span[0], span[1] + 1)):
            selected.append((list_name, mutant))
    return selected


def _node_char_span(node: ast.AST, line_starts: list[int], src_len: int) -> tuple[int, int]:
    padded_starts = [0] + line_starts + [src_len]
    start_idx = padded_starts[getattr(node, "lineno", 0)] + getattr(node, "col_offset", 0)
    end_idx = padded_starts[getattr(node, "end_lineno", 0)] + getattr(node, "end_col_offset", 0)
    return start_idx, end_idx


def _mutant_edit_span(norm_src: str, original: str, replacement: str) -> tuple[int, int] | None:
    """Return `(edit_start, edit_end)` char offsets in `norm_src` for the mutated portion of `original`."""
    if snippet_line_span(norm_src, original) is None:
        return None
    norm_orig = original.replace("\r\n", "\n")
    base_idx = norm_src.find(norm_orig)
    norm_repl = replacement.replace("\r\n", "\n")
    pfx = len(os.path.commonprefix([norm_orig, norm_repl]))
    rem_orig = norm_orig[pfx:]
    rem_repl = norm_repl[pfx:]
    sfx = len(os.path.commonprefix([rem_orig[::-1], rem_repl[::-1]]))
    edit_start = base_idx + pfx
    edit_end = max(edit_start + 1, base_idx + len(norm_orig) - sfx)
    return edit_start, edit_end


_STATEMENT_PREDICATES = {
    ast.If: "If",
    ast.While: "While",
    ast.IfExp: "IfExp",
    ast.Assert: "Assert",
}
_DISPATCH_EXPR_RE = re.compile(r"^(?:filter\s*\(|\{[\s\S]*\}\s*(?:\.get\s*\(|\[))")


def _node_predicate_checks(node: ast.AST, norm_src: str, line_starts: list[int]) -> list[tuple[str, ast.AST]]:
    stmt_kind = _STATEMENT_PREDICATES.get(type(node))
    if stmt_kind:
        return [(stmt_kind, node.test)]
    if isinstance(node, ast.Compare):
        return [("Compare", node)]
    if isinstance(node, ast.comprehension):
        return [("CompIf", g) for g in node.ifs]
    if isinstance(node, ast.match_case) and node.guard is not None:
        return [("MatchGuard", node.guard)]
    a, b = _node_char_span(node, line_starts, len(norm_src))
    if isinstance(node, (ast.Call, ast.Subscript)) and _DISPATCH_EXPR_RE.match(norm_src[a:b]):
        return [("Dispatch", node)]
    return []


def _spans_overlap(span_a: tuple[int, int], span_b: tuple[int, int]) -> bool:
    return span_a[0] < span_b[1] and span_a[1] > span_b[0]


def _collect_uncovered_in_source(
    path: str,
    source: str,
    target_lines: set[int],
    mutants_for_file: list[tuple[str, str]],
) -> list[dict[str, object]]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise Refused("%s has a Python SyntaxError on line %s: %s" % (path, exc.lineno, exc.msg)) from exc

    norm_src = source.replace("\r\n", "\n")
    line_starts = [0] + [m.end() for m in re.finditer(r"\n", norm_src)]

    edit_spans: list[tuple[int, int]] = []
    for orig, repl in mutants_for_file:
        span = _mutant_edit_span(norm_src, orig, repl)
        if span is not None:
            edit_spans.append(span)

    def _is_covered(target_node: ast.AST) -> bool:
        n_span = _node_char_span(target_node, line_starts, len(norm_src))
        return any(_spans_overlap(m_span, n_span) for m_span in edit_spans)

    src_lines = norm_src.splitlines()
    by_line: dict[int, dict[str, None]] = {}

    def _record(lineno: int, end_lineno: int, kind: str) -> None:
        if any(ln in target_lines for ln in range(lineno, end_lineno + 1)):
            by_line.setdefault(lineno, {})[kind] = None

    for node in ast.walk(tree):
        for kind, cond_node in _node_predicate_checks(node, norm_src, line_starts):
            if not _is_covered(cond_node):
                _record(cond_node.lineno, cond_node.end_lineno, kind)
        if isinstance(node, ast.BoolOp):
            op_spans = [_node_char_span(v, line_starts, len(norm_src)) for v in node.values]
            single_op_muts = [
                m_span for m_span in edit_spans
                if sum(1 for o_span in op_spans if _spans_overlap(m_span, o_span)) == 1
            ]
            uncovered_ops = [
                ast.unparse(v)
                for idx, v in enumerate(node.values)
                if not any(_spans_overlap(m_span, op_spans[idx]) for m_span in single_op_muts)
            ]
            if uncovered_ops:
                _record(node.lineno, node.end_lineno, "BoolOp[%s]" % ", ".join(uncovered_ops))

    return [
        {
            "path": path,
            "lineno": lineno,
            "kinds": list(by_line[lineno]),
            "kind": ", ".join(by_line[lineno]),
            "source": src_lines[lineno - 1].strip(),
        }
        for lineno in sorted(by_line)
    ]


def _is_skipped_test_or_list(path: str, mutants_by_path: dict[str, list[tuple[str, str]]]) -> bool:
    return bool(_SKIP_TEST_OR_LIST_RE.search(path)) and path not in mutants_by_path


def _read_python_source(repo: Repo, rev: str, path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if _NON_PY_EXT_RE.match(base):
        return ""
    p_show = repo.git("show", "%s:%s" % (rev, path.replace("\\", "/")), check=False)
    if p_show.returncode != 0:
        raise Refused("cannot read %s at %s" % (path, rev[:12]))
    first_bytes = (p_show.stdout.splitlines() + [b""])[0]
    if not path.endswith((".py", ".pyw")) and not _SHEBANG_PY_RE.match(first_bytes):
        return ""
    try:
        return p_show.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused("%s at %s is not valid UTF-8: %s" % (path, rev[:12], exc)) from exc


def audit_uncovered_predicates(repo: Repo, base_ref: str, sha: str,
                               mutants: list[tuple[str, tuple]]) -> list[dict[str, object]]:
    """Walk the Python AST of modified `.py`/`.pyw` (and `#!.*python` script) files in `<base_ref>...<sha>`
    and return any changed or deleted `If`, `While`, `IfExp`, `Assert`, `MatchGuard`, `CompIf`,
    `BoolOp`, `Compare`, or `Dispatch` predicate not covered by a registered mutant's `original`."""
    base_sha, diff_text = _diff_raw(repo, base_ref, sha)
    changed, deleted, replaced = _parse_diff_hunks(diff_text)
    mutants_by_path: dict[str, list[tuple[str, str]]] = {}
    for _, (_, path, original, replacement, _) in mutants:
        mutants_by_path.setdefault(path.replace("\\", "/"), []).append((original, replacement))

    head_sources: dict[str, str] = {}
    uncovered: list[dict[str, object]] = []
    for path in sorted(changed):
        if _is_skipped_test_or_list(path, mutants_by_path):
            continue
        source = _read_python_source(repo, sha, path)
        head_sources[path] = source
        uncovered.extend(_collect_uncovered_in_source(path, source, changed[path], mutants_by_path.get(path, [])))

    for old_path, new_path, old_lns, new_lns in replaced:
        if not _collect_uncovered_in_source(new_path, head_sources.get(new_path, ""), new_lns, []):
            deleted.setdefault((old_path, new_path), set()).update(old_lns)

    for (old_path, new_path), del_lines in sorted(deleted.items()):
        file_muts = list(dict.fromkeys(mutants_by_path.get(new_path, []) + mutants_by_path.get(old_path, [])))
        base_source = _read_python_source(repo, base_sha, old_path)
        uncovered.extend(_collect_uncovered_in_source(new_path, base_source, del_lines, file_muts))
    return uncovered


# ---- one run

class Children:
    """The test runs in flight. None starts once a stop is asked; stop() interrupts each, then kills what is left.

    A run is in `live` from its start until its whole process group has been ended and its leader reaped (run_pytest).
    Its leader, pytest, is reaped last: until then its pid, which is the group's id, cannot be given to another
    process, so signalling the group can only reach this run's processes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.live: set[subprocess.Popen] = set()
        self.stopping = False
        self.stragglers: list[str] = []

    def start(self, cmd: list[str], **kwargs) -> subprocess.Popen | None:
        with self.lock:
            if self.stopping:
                return None
            p = subprocess.Popen(cmd, **kwargs)
            self.live.add(p)
            return p

    def reaped(self, p: subprocess.Popen) -> None:
        with self.lock:
            self.live.discard(p)

    def stop(self, grace: float) -> None:
        """Interrupt every running test run's group, wait up to `grace` for the runs to be ended and reaped, then kill
        the groups of those that are not (their leaders are unreaped, so the group ids are still theirs)."""
        with self.lock:
            self.stopping = True
            running = list(self.live)
        for p in running:
            _interrupt(p)
        end = time.monotonic() + grace
        while time.monotonic() < end:
            with self.lock:
                if not self.live:
                    return
            time.sleep(0.1)
        with self.lock:
            for p in self.live:
                _kill(p)


def run_pytest(children: Children, checkout: str, tests: list[str], scratch: str, timeout: float,
               grace: float = INTERRUPT_GRACE_S) -> tuple[int | None, str, float, list | None, list[str]] | None:
    """(pytest's exit code or None on the timeout, its output, seconds, the failures its junit report records or None
    when there is none, the databases of the run dropped here) for the tests, run in the checkout; None when a stop
    came first. After the timeout pytest is interrupted and, `grace` seconds later, killed.

    pytest is given every place it writes: --basetemp, --junitxml and --log-file inside `scratch`, the cache provider
    off, and the repository's addopts cleared (`-o addopts=`), so neither the environment nor the repository's
    configuration can point an output (an ini log_file, an addopts --debug=FILE) outside the run. Those paths are
    given relative to the checkout, pytest's working folder: pytest expands $VARIABLES and ~ in --junitxml, and the
    work folder's own path (under TMPDIR) may hold a `$`. Its output goes to a file, never a pipe, so a child that
    keeps a pipe open cannot hold a wait. Whatever is left of its process group once it has exited is ended before
    this returns (end_group), and then any database tests/conftest.py made for the run and could not drop (a killed
    pytest never reaches pytest_sessionfinish) is dropped by the name conftest recorded (drop_run_databases)."""
    pycache = os.path.join(scratch, "pyc")
    tmp = os.path.join(scratch, "tmp")
    os.makedirs(pycache)
    os.makedirs(tmp)
    record = os.path.join(scratch, "databases.txt")
    env = run_env({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": pycache, "PYTHONIOENCODING": "utf-8",
                   "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp, "MESHY_TEST_DB_RECORD": record})
    junit = os.path.join(scratch, "junit.xml")

    def here(path: str) -> str:
        return os.path.relpath(path, checkout)        # no `$` or `~` of the work folder's path reaches pytest

    cmd = [sys.executable, "-B", "-m", "pytest", *PYTEST_FLAGS, "--basetemp=" + here(os.path.join(scratch, "basetemp")),
           "--junitxml=" + here(junit), "--log-file=" + here(os.path.join(scratch, "pytest-log.txt")),
           "-o", "addopts=", *tests]
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True})
    start = time.monotonic()
    log = os.path.join(scratch, "pytest.out")
    with open(log, "wb") as out_file:
        p = children.start(cmd, cwd=checkout, env=env, stdin=subprocess.DEVNULL, stdout=out_file,
                           stderr=subprocess.STDOUT, **group)
        if p is None:
            return None
        timed_out = False
        try:
            if not exited(p, timeout):
                timed_out = True
                _interrupt(p)
                if not exited(p, grace):
                    _kill(p)
                    exited(p, None)
        finally:
            problems = [end_group(p)]                      # the rest of its group, then the leader is reaped
            children.reaped(p)
            dropped, problem = drop_run_databases(record)  # what a killed pytest could not drop
            problems.append(problem)
            with children.lock:
                children.stragglers.extend(x for x in problems if x)
    with open(log, "rb") as f:
        out = f.read().decode("utf-8", "replace")
    return (None if timed_out else p.returncode), out, time.monotonic() - start, failures_in(junit), dropped


#: The shape of the name tests/conftest.py gives a run's database (`meshy_test_<pid>_<6 hex>`).
RUN_DATABASE = re.compile(r"meshy_test_[0-9]+_[0-9a-f]{6}")
#: Run with the runner's interpreter (the project's venv, which has psycopg): drop each named database that still
#: exists, on the server MESHY_TEMPLATE_DSN names, through its `postgres` database as tests/conftest.py's
#: _admin_dsn does. The DSN comes from the environment, never the command line.
DROP_SCRIPT = """
import os, sys
import psycopg
dsn = (os.environ.get("MESHY_TEMPLATE_DSN") or "").strip()
with psycopg.connect(dsn.rpartition("/")[0] + "/postgres", autocommit=True, connect_timeout=10) as conn:
    for name in sys.argv[1:]:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
            print("dropped " + name, flush=True)
"""


def drop_run_databases(record: str) -> tuple[list[str], str | None]:
    """Drop each database the run's tests/conftest.py recorded in `record` (MESHY_TEST_DB_RECORD, written before the
    database is made) that still exists. Only a name of conftest's shape is touched, and only one this run's own
    record holds. ([dropped], what may be left and why, or None)."""
    try:
        with open(record, encoding="utf-8") as f:
            recorded = f.read().split()
    except FileNotFoundError:
        return [], None
    names = sorted({n for n in recorded if RUN_DATABASE.fullmatch(n)})
    odd = [n for n in recorded if not RUN_DATABASE.fullmatch(n)]
    problem = "the run's database record holds names no test run gives: %r" % odd[:3] if odd else None
    if not names:
        return [], problem
    try:
        p = subprocess.run([sys.executable, "-I", "-c", DROP_SCRIPT, *names], capture_output=True, env=clean_env(),
                           stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], "the run's databases %s may be left: %s" % (", ".join(names), exc)
    dropped = [ln.split(" ", 1)[1] for ln in p.stdout.decode("utf-8", "replace").splitlines()
               if ln.startswith("dropped ")]
    if p.returncode != 0:
        why = (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["exit %d" % p.returncode])[-1][:200]
        problem = "the run's databases %s may be left; dropping them failed: %s" % (
            ", ".join(n for n in names if n not in dropped), why)
    return dropped, problem


def exited(p: subprocess.Popen, timeout: float | None) -> bool:
    """Wait up to `timeout` (None: no bound) for the leader to exit, WITHOUT reaping it (POSIX: waitid with
    WNOWAIT), so its pid, the group's id, stays this run's; True when it has exited."""
    if os.name == "nt":
        try:
            p.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
    end = None if timeout is None else time.monotonic() + timeout
    while True:
        if os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            return True
        if end is not None and time.monotonic() >= end:
            return False
        time.sleep(0.05)


def end_group(p: subprocess.Popen) -> str | None:
    """Once the leader has exited: SIGTERM whatever else is alive in its process group, SIGKILL what is still alive
    GROUP_GRACE_S later, then reap the leader (last, so the group id stays this run's while it is signalled). A
    description of the processes still alive after that, or None. Windows has no such group here: the leader alone
    is waited for, and a process it started that outlives it is not followed (best effort)."""
    left: list[int] | None = []
    if os.name != "nt":
        if not exited(p, 0):                           # only after an error on the way here: end it all now
            _kill(p)
            exited(p, None)
        if group_members(p.pid) != []:                 # [] is "none"; None is "ps could not say"
            _terminate(p)
            if not group_gone(p.pid, GROUP_GRACE_S):
                _kill(p)
                group_gone(p.pid, GROUP_GRACE_S)
            left = group_members(p.pid)
    p.wait()
    if left:
        return "processes %s of the test run's group %d outlived SIGKILL" % (left, p.pid)
    return None


def group_members(pgid: int) -> list[int] | None:
    """The processes of the group but its leader that are not gone yet, from `ps`; None when ps cannot say.

    A live one counts. So does a zombie whose parent is this runner, until it is reaped, and it is reaped here: on
    Linux the runner is a child subreaper (become_subreaper), so the orphans a test run leaves are adopted by it,
    also where PID 1 reaps nothing (a container). It is reaped by its own pid, never with waitpid(-1), which could
    take the exit status of another run's pytest (kept unreaped on purpose, see exited) or of a git call another
    thread waits for; a group's members are never such a child, as each of those leads or sits in another group. A
    zombie whose parent is another process is gone for this purpose: on macOS, which has no subreaper, orphans go to
    launchd, which reaps them."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat="], capture_output=True, env=clean_env(),
                                 stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    me, out = str(os.getpid()), []
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[2] != str(pgid) or fields[0] == str(pgid):
            continue
        if not fields[3].startswith("Z"):
            out.append(int(fields[0]))                 # alive
        elif fields[1] == me:                          # a zombie this runner adopted: not gone until reaped
            _reap(int(fields[0]))
            out.append(int(fields[0]))
    return out


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def become_subreaper() -> bool:
    """Linux: make this runner a child subreaper (prctl PR_SET_CHILD_SUBREAPER), so a process a test run leaves
    behind is adopted by the runner when its parent dies, and reaped by group_members, instead of going to PID 1,
    which in a container may never reap it. Best effort: False where it is not Linux or the call fails. macOS has no
    such call; there launchd adopts and reaps orphans."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0     # 36: PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def group_gone(pgid: int, wait: float) -> bool:
    """Wait up to `wait` for the group to have no live member but its leader; when ps cannot say, wait it out."""
    end = time.monotonic() + wait
    while True:
        members = group_members(pgid)
        if members == []:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.2 if members is not None else wait)


def failures_in(junit: str) -> list[tuple[str, str, str, str]] | None:
    """[(failure or error, its message, its text, classname::name)] in the order pytest's junit report records them;
    None when there is no readable report."""
    try:
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError):
        return None
    out = []
    for case in root.iter("testcase"):
        for child in case:
            if child.tag in ("failure", "error"):
                out.append((child.tag, child.get("message") or "", child.text or "",
                            "%s::%s" % (case.get("classname") or "", case.get("name") or "")))
    return out


def _interrupt(p: subprocess.Popen) -> None:
    """SIGINT to the run's group: pytest ends its session. On Windows CTRL_BREAK_EVENT, which Python does not turn
    into KeyboardInterrupt: it ends pytest at once (no session finish), so there drop_run_databases is what drops
    the run's database."""
    try:
        if os.name == "nt":
            p.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(p.pid, signal.SIGINT)
    except OSError:
        pass


def _terminate(p: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            p.terminate()
        else:
            os.killpg(p.pid, signal.SIGTERM)
    except OSError:
        pass


def _kill(p: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            p.kill()
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def classify(rc: int | None, out: str, timeout: float, failures: list | None = None, path: str | None = None,
             checkout: str = "") -> tuple[str, str, bool]:
    """(verdict, how, on an assertion) for one mutant's test run, from pytest's exit code and its junit report."""
    if rc is None:
        return "CAUGHT", "timed out after %ds" % timeout, False
    if rc == 0:
        return "SURVIVED", "the tests passed", False
    if rc < 0:
        return "ERROR", "pytest ended on signal %d" % -rc, False
    if rc == 3:
        return "ERROR", "pytest's internal error (exit 3): no test result to judge by", False
    if rc not in (1, 2):
        return "ERROR", "pytest exit %d: the tests did not run as named" % rc, False
    if failures is None:
        return "ERROR", "pytest exit %d and no junit report of what ran" % rc, False
    collection = [text for kind, message, text, _ in failures if kind == "error" and message == "collection failure"]
    if collection:
        if path and any(names_file(text, path, checkout) for text in collection):
            error = ERROR_NAME.search("\n".join(collection))
            return "CAUGHT", "the tests cannot import the mutated file (%s), not an assertion" % (
                error.group(1) if error else "an error"), False
        return "ERROR", "a collection error that does not come from the mutated file", False
    if rc == 2:
        return "ERROR", "pytest was interrupted (exit 2) before its tests ended", False
    shown = FIRST_FAILED.search(out)
    for kind, message, text, test in failures:
        where = " in " + (shown.group(1) if shown else test)
        if kind == "failure" and ASSERTION.search(text):
            return "CAUGHT", "on an assertion" + where, True
        error = ERROR_NAME.search(text)
        name = error.group(1) if error else "an error"
        if kind == "failure":
            return "CAUGHT", "on %s, not an assertion%s" % (name, where), False
        return "CAUGHT", "on %s in a test's setup or teardown, not an assertion%s" % (name, where), False
    return "ERROR", "pytest exit 1 and its report holds no failure", False


def names_file(text: str, path: str, checkout: str) -> bool:
    """Does a collection error's record name the mutated file: its frame `<path>:<line>: in` as pytest prints it
    relative to the checkout, or its path in the checkout (a SyntaxError's `File "..."`, an ImportError's `(...)`)?"""
    text = text.replace("\\", "/")
    rel = path.replace("\\", "/")
    full = os.path.join(checkout, path).replace("\\", "/")
    return bool(re.search(r"^%s:\d+: in " % re.escape(rel), text, re.M)) or (bool(checkout) and full in text)


def last_summary(out: str) -> str:
    found = SUMMARY.findall(out)
    return found[-1] if found else (out.strip().splitlines() or [""])[-1][:160]


# ---- the run

#: What a checkout's run gives when a stop came before its tests ran or ended.
STOPPED = "stopped before the tests ran or ended"


class Run:
    def __init__(self, repo: Repo, sha: str, work: str, timeout: float, grace: float = INTERRUPT_GRACE_S,
                 control_timeout: float | None = None):
        self.repo, self.sha, self.work, self.timeout, self.grace = repo, sha, work, timeout, grace
        #: The bound of a control's test run (--control-timeout); a mutant's run is bound by `timeout`.
        self.control_timeout = timeout if control_timeout is None else control_timeout
        self.children = Children()
        self.blobs: dict[str, str | None] = {}
        self.modes: dict[str, str | None] = {}
        self.leftovers: list[str] = []
        self.printing = threading.Lock()
        self.counter = 0

    def blob(self, path: str) -> str | None:
        """The file at the commit as text, or None when it is not there or not UTF-8."""
        if path not in self.blobs:
            p = self.repo.git("show", "%s:%s" % (self.sha, path.replace("\\", "/")), check=False)
            try:
                self.blobs[path] = p.stdout.decode("utf-8") if p.returncode == 0 else None
            except UnicodeDecodeError:
                self.blobs[path] = None
        return self.blobs[path]

    def mode(self, path: str) -> str | None:
        """The path's mode in the commit's own tree (`git ls-tree`): one of FILE_MODES for a file, LINK_MODE for a
        symbolic link, None when the tree has no entry of exactly that name. Read from the commit, not from a
        checkout: git on Windows with core.symlinks=false checks a committed link out as a plain file that holds the
        link's target, and no realpath check can tell that file from any other."""
        if path not in self.modes:
            rel = path.replace("\\", "/")
            p = self.repo.git("--literal-pathspecs", "ls-tree", "-z", self.sha, "--", rel)
            found = None
            for entry in p.stdout.decode("utf-8", "replace").split("\0"):
                meta, tab, name = entry.partition("\t")
                if tab and name == rel:
                    found = meta.split(" ", 1)[0]
            self.modes[path] = found
        return self.modes[path]

    def blind(self, path: str, original: str) -> str | None:
        """Why the mutant cannot be applied at the commit, or None when its original occurs exactly once."""
        text = self.blob(path)
        if text is None:
            return "%s is missing or not UTF-8 at %s" % (path, self.sha[:7])
        n = text.replace("\r\n", "\n").count(original.replace("\r\n", "\n"))
        return None if n == 1 else "the original occurs %d times in %s at %s" % (n, path, self.sha[:7])

    def fresh(self, kind: str) -> str:
        with self.printing:
            self.counter += 1
            return os.path.join(self.work, "%s%d" % (kind, self.counter))

    def in_checkout(self, tests: list[str], path: str | None, mutated: str | None) -> tuple | str:
        """Run the tests in a fresh checkout, with `path` holding `mutated` when given (a mutant's run, bound by
        --timeout; without `path` it is a control, bound by --control-timeout): (exit code or None on the timeout,
        output, seconds, the junit failures, the checkout) or an error text; STOPPED when a stop came first.

        `path` is written only when the commit's tree holds it as a regular file (mode()) and its path in the
        checkout leads to a file inside the checkout (realpath): a link in the commit is refused by the first, also
        on Windows, where git may check it out as a plain file; the second is kept for whatever the first misses."""
        if self.children.stopping:
            return STOPPED
        if path is not None:                               # the first guard: what the commit says the path is
            mode = self.mode(path)
            if mode not in FILE_MODES:
                return "%s is %s in the commit (mode %s at %s); not written" % (
                    path, "a symbolic link" if mode == LINK_MODE else "not a file", mode, self.sha[:7])
        base = self.fresh("m" if path else "c")
        checkout, scratch = base, base + "s"
        os.makedirs(scratch)
        try:
            try:
                self.repo.add_checkout(self.sha, checkout)
            except Refused as exc:
                return "no checkout: %s" % exc
            real = os.path.realpath(checkout) + os.sep
            for t in tests:                                # a file, not a folder: a folder is collected through its links
                inside = os.path.join(checkout, *t.split("::", 1)[0].split("/"))
                if not (os.path.realpath(inside).startswith(real) and os.path.isfile(inside)):
                    return "the test path %r is not a test file in the checkout" % t
            if path is not None:                           # the second guard: where the checkout's path leads
                target = os.path.join(checkout, *path.replace("\\", "/").split("/"))
                if not os.path.realpath(target).startswith(os.path.realpath(checkout) + os.sep):
                    return "%s leads out of the checkout (a link?); not written" % path
                with open(target, "wb") as f:      # the blob's own bytes, one replacement made: no line-ending change
                    f.write(mutated.encode("utf-8"))
            bound = self.timeout if path is not None else self.control_timeout
            done = run_pytest(self.children, checkout, tests, scratch, bound, self.grace)
            return STOPPED if done is None else (*done, checkout)
        finally:
            problem = self.repo.remove_checkout(checkout) if os.path.exists(checkout) else None
            if problem:
                with self.printing:
                    self.leftovers.append("%s (%s)" % (checkout, problem))
            shutil.rmtree(scratch, ignore_errors=True)

    def say(self, line: str) -> None:
        with self.printing:
            print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="append", dest="lists", metavar="NAME", help="run this list (repeatable)")
    ap.add_argument("--only", action="append", metavar="TEXT", help="only the mutants whose name holds this text")
    ap.add_argument("--rev", default="HEAD", help="the commit under test (default HEAD; uncommitted edits are not seen)")
    ap.add_argument("--jobs", type=int, default=1, help="mutants run at once, each in its own checkout (default 1)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="seconds per mutant's test run; a run past it is CAUGHT (default %(default)s)")
    ap.add_argument("--control-timeout", type=float, default=None, metavar="S",
                    help="seconds per control's test run, on the unmutated commit; a control past it refuses the "
                         "run (default: --timeout, but never under %d, so a short --timeout meant to end a hanging "
                         "mutant fast does not refuse the run when the unmutated tests are slow under load)"
                         % DEFAULT_TIMEOUT_S)
    ap.add_argument("--grace", type=float, default=INTERRUPT_GRACE_S,
                    help="seconds a test run gets after the timeout's interrupt before it is killed (default %(default)s)")
    ap.add_argument("--check", action="store_true", help="only report the BLIND mutants; no checkout, no test run")
    ap.add_argument("--assertions-only", action="store_true",
                    help="a mutant caught other than on an assertion counts as not caught")
    ap.add_argument("--diff", default=None, metavar="BASE_REF",
                    help="only run mutants whose file and original snippet overlap git diff -U0 <base_ref>...<rev>")
    ap.add_argument("--allow-empty", action="store_true",
                    help="with --diff, exit 0 instead of 2 when zero mutants match the diff")
    ap.add_argument("--audit-uncovered-predicates", nargs="?", const="", default=None, metavar="BASE_REF",
                    help="audit modified .py/.pyw and #!python files in git diff -U0 <base_ref>...<rev> for changed "
                         "or deleted If, While, IfExp, Assert, MatchGuard, CompIf, BoolOp, or Compare predicates "
                         "not covered by any registered mutant")
    ap.add_argument("--repo", default=HERE, help="the repository (default: the one this file is in)")
    ap.add_argument("--lists-dir", default=LISTS_DIR, help="where the lists are (default: lists/ beside this file)")
    a = ap.parse_args(argv)
    if a.jobs < 1 or a.timeout <= 0 or a.grace < 0 or (a.control_timeout is not None and a.control_timeout <= 0):
        print("--jobs must be 1 or more, --timeout and --control-timeout above 0 and --grace not below 0",
              file=sys.stderr)
        return 2
    STOP.update(armed=False, signum=None)
    become_subreaper()                                     # before any child: the orphans of test runs come here
    handlers = {}
    for sig in (getattr(signal, "SIGTERM", None), signal.SIGINT):
        try:
            handlers[sig] = signal.signal(sig, _on_signal)
        except (TypeError, ValueError):                    # no such signal here, or not the main thread
            pass
    work = os.path.realpath(tempfile.mkdtemp(prefix="mut-"))
    try:
        STOP["armed"] = True
        if STOP["signum"] is not None:                     # it came before the handler could interrupt anything
            STOP["armed"] = False
            raise Stopped(STOP["signum"])
        os.makedirs(os.path.join(work, "no-hooks"))
        code = _main(a, work)
    except Refused as exc:
        print("refused: %s" % exc, file=sys.stderr)
        code = 2
    except Stopped as exc:
        print("\nstopped by signal %d: the test runs were ended and the checkouts removed" % exc.signum, flush=True)
        code = 128 + exc.signum
    finally:
        STOP["armed"] = False                              # a signal now only marks the exit code
        shutil.rmtree(work, ignore_errors=True)
        for sig, old in handlers.items():
            signal.signal(sig, old)
    return code or (128 + STOP["signum"] if STOP["signum"] is not None else 0)


def _main(a: argparse.Namespace, work: str) -> int:
    refused = [v for v in REFUSED_VARS if v in os.environ]
    if refused and not a.check and a.audit_uncovered_predicates is None:
        raise Refused("%s is set: tests/conftest.py would then make no database of the run's own, and the tests "
                      "would write into whichever database MESHY_TEST_DSN names. Unset it; each test run makes and "
                      "drops its own database on the MESHY_TEMPLATE_DSN server" % ", ".join(refused))
    p = subprocess.run(["git", "-C", a.repo, "rev-parse", "--show-toplevel"], capture_output=True, env=clean_env(),
                       stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise Refused("%s is not in a git repository" % a.repo)
    repo = Repo(p.stdout.decode("utf-8", "replace").strip(), os.path.join(work, "no-hooks"))
    sha = repo.git("rev-parse", "--verify", "--quiet", a.rev + "^{commit}", check=False).stdout.decode().strip()
    if not sha:
        raise Refused("no commit %r in %s" % (a.rev, repo.path))
    mutants = [(ln, m) for ln, m in load_lists(a.lists_dir, a.lists)
               if not a.only or any(o in m[0] for o in a.only)]
    if a.audit_uncovered_predicates is not None:
        audit_base = a.audit_uncovered_predicates or a.diff
        if not audit_base:
            raise Refused("--audit-uncovered-predicates requires a base ref (or --diff <base_ref>)")
        uncovered = audit_uncovered_predicates(repo, audit_base, sha, mutants)
        for item in uncovered:
            print("UNCOVERED %s:%d (%s) | %s" % (item["path"], item["lineno"], item["kind"], item["source"]),
                  flush=True)
        print("audit-uncovered-predicates at %s (diff %s): %d uncovered predicate line(s)" % (
            sha[:12], audit_base, len(uncovered)), flush=True)
        if uncovered:
            return 1
        return 0
    if a.diff is not None:
        mutants = filter_mutants_by_diff(repo, a.diff, sha, mutants)
        if not mutants:
            msg = ("0 mutants overlap diff %s...%s (0 mutants matched diff: no added/modified lines in registered "
                   "mutant targets; not a coverage proof)" % (a.diff, sha[:12]))
            if a.allow_empty:
                print(msg, flush=True)
                return 0
            raise Refused(msg)
    if not mutants:
        raise Refused("no mutant is selected")
    control_timeout = a.control_timeout if a.control_timeout is not None else max(a.timeout, DEFAULT_TIMEOUT_S)
    run = Run(repo, sha, work, a.timeout, a.grace, control_timeout)
    print("%d mutants from %d list(s), at %s in %s; timeout %gs per mutant, %gs per control; work folder %s" % (
        len(mutants), len({ln for ln, _ in mutants}), sha[:12], repo.path, a.timeout, control_timeout, work),
        flush=True)

    results: dict[str, tuple[str, str, bool]] = {}
    for ln, (name, path, original, _, _) in mutants:
        why = run.blind(path, original)
        if why:
            results[name] = ("BLIND", why, False)
            print("BLIND    [%s] %s | %s" % (ln, name, why), flush=True)
    blind = len(results)
    if a.check:
        print("\ncheck at %s: %d mutants, %d apply (the original occurs once), %d BLIND" % (
            sha[:12], len(mutants), len(mutants) - blind, blind))
        return 1 if blind else 0

    todo = [(ln, m) for ln, m in mutants if m[0] not in results]
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs)
    try:
        if not _controls(run, pool, sorted({tuple(m[4]) for _, m in todo})):
            print("\nsummary at %s: refused, a control is not green on the unmutated commit, so none of the %d "
                  "mutants was run (every one would look caught); %d BLIND" % (sha[:12], len(mutants), blind))
            return 1

        def one(item):
            ln, (name, path, original, replacement, tests) = item
            try:
                src_blob = run.blob(path)
                crlf_orig = original.replace("\r\n", "\n").replace("\n", "\r\n")
                crlf_repl = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
                mut_blob = src_blob.replace(original, replacement, 1).replace(crlf_orig, crlf_repl, 1)
                done = run.in_checkout(tests, path, mut_blob)
            except Exception as exc:                       # noqa: BLE001 - one mutant's failure is that mutant's ERROR
                done = "%s: %s" % (type(exc).__name__, exc)
            if done is STOPPED or run.children.stopping:
                return                                     # a stopped run judges nothing
            if isinstance(done, str):
                verdict, line = ("ERROR", done, False), done
            else:
                rc, out, secs, failures, dropped, checkout = done
                verdict = classify(rc, out, a.timeout, failures, path, checkout)
                line = "%s | %s (%.0fs)%s" % (verdict[1], last_summary(out), secs, _dropped(dropped))
            results[name] = verdict
            run.say("%-8s [%s] %s | %s" % (verdict[0], ln, name, line))

        for future in [pool.submit(one, item) for item in todo]:
            future.result()
    finally:
        STOP["armed"] = False                              # the cleanup below is never interrupted
        run.children.stop(STOP_GRACE_S)                    # on a stop: no run starts, each running one ends
        pool.shutdown(wait=True, cancel_futures=True)
        for path in repo.registered_under(work):
            problem = repo.remove_checkout(path)
            if problem:
                run.leftovers.append("%s (%s)" % (path, problem))
        run.leftovers.extend(run.children.stragglers)
        if run.leftovers:
            print("LEFTOVER (a checkout: remove it with `git worktree remove --force <path>`): %s"
                  % "; ".join(run.leftovers), flush=True)
    return _summary(mutants, results, sha, a.assertions_only) or (1 if run.leftovers else 0)


def _controls(run: Run, pool: concurrent.futures.Executor, controls: list[tuple[str, ...]]) -> bool:
    """Run each set of test files once, unmutated; True when every one passed."""
    green = True
    for tests, done in zip(controls, pool.map(lambda t: run.in_checkout(list(t), None, None), controls)):
        if isinstance(done, str):
            run.say("control  %s: ERROR %s" % (" ".join(tests), done))
            green = False
            continue
        rc, out, secs = done[0], done[1], done[2]
        # A control slower than --timeout: a mutant's run of these tests may time out (CAUGHT) with no fault found.
        slow = ("; the control took %.0fs, longer than --timeout %gs: a mutant of these tests that times out proves "
                "nothing" % (secs, run.timeout)) if rc == 0 and secs > run.timeout else ""
        run.say("control  %s: exit %s | %s%s%s" % (
            " ".join(tests), "timeout after %gs (--control-timeout)" % run.control_timeout if rc is None else rc,
            last_summary(out), _dropped(done[4]), slow))
        if rc != 0:
            green = False
            run.say(out.rstrip()[-3000:])
    return green


def _dropped(names: list[str]) -> str:
    return "; dropped the database the run left: %s" % ", ".join(names) if names else ""


def _summary(mutants: list, results: dict, sha: str, assertions_only: bool) -> int:
    verdicts = [results[m[0]] for _, m in mutants]
    caught = [v for v in verdicts if v[0] == "CAUGHT"]
    on_assert = sum(1 for v in caught if v[2])
    timed = sum(1 for v in caught if v[1].startswith("timed out"))
    count = {k: sum(1 for v in verdicts if v[0] == k) for k in ("SURVIVED", "BLIND", "ERROR")}
    not_caught = [(ln, m[0], results[m[0]]) for ln, m in mutants if results[m[0]][0] != "CAUGHT"
                  or (assertions_only and not results[m[0]][2])]
    if not_caught:
        print("\nnot caught:")
        for ln, name, v in not_caught:
            print("  %-8s [%s] %s | %s" % (v[0] if v[0] != "CAUGHT" else "NOT-ASSERTION", ln, name, v[1]))
    print("\nsummary at %s: %d mutants, %d CAUGHT (%d on an assertion, %d on another error, %d timed out), "
          "%d SURVIVED, %d BLIND, %d ERROR" % (sha[:12], len(mutants), len(caught), on_assert,
                                               len(caught) - on_assert - timed, timed, count["SURVIVED"],
                                               count["BLIND"], count["ERROR"]))
    return 1 if not_caught else 0


if __name__ == "__main__":
    sys.exit(main())
