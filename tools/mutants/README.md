# mutants: one runner, and the mutant lists behind pxx's reviews

`run.py` checks that the tests catch a deliberate fault. Each mutant replaces one exact piece of text in one
repository file. The runner applies it to a throwaway checkout of the commit under test, runs the tests that are
supposed to fail, and reports whether they did. A committed file is never edited: the old session scripts this
replaces (`mutate*.py`, kept only in a session scratchpad) rewrote the checkout they ran in and restored it afterwards.

`lists/` holds those scripts' mutants, ported unchanged, one module per area of the code, plus the runner's own list.

## Running it

Run it with the project's venv. The tests run with the repository's own `tests/conftest.py`, so a run needs
`MESHY_TEMPLATE_DSN` as every pytest run here does:

    export MESHY_TEMPLATE_DSN=...                                   # as for any pytest run
    python tools/mutants/run.py --check                             # fast: does every original occur once?
    python tools/mutants/run.py --list mesh_audit                   # one list, at HEAD
    python tools/mutants/run.py --only "mutate6:M12"                # every mutant whose name holds that text
    python tools/mutants/run.py --rev 1a1ea9f --jobs 3              # every list, at a commit, three at a time

| Option | |
|---|---|
| `--list NAME` | one list (repeatable); default every list in `lists/` |
| `--only TEXT` | the mutants whose name holds `TEXT` (repeatable) |
| `--rev REV` | the commit under test, default `HEAD`; edits not yet committed are not seen |
| `--jobs N` | mutants run at once, each in its own checkout (default 1) |
| `--timeout S` | seconds per mutant's test run; a run past it is CAUGHT (default 300) |
| `--control-timeout S` | seconds per control's test run; a control past it refuses the run (default `--timeout`, but never under 300) |
| `--grace S` | seconds a test run gets after the timeout's interrupt before it is killed (default 30) |
| `--check` | only report the BLIND mutants; no checkout and no test run (under a second for every list) |
| `--assertions-only` | a mutant caught other than on an assertion counts as not caught |

For each mutant the runner reads the file at the commit (`git show`). If the original text does not occur in it
exactly once the mutant is **BLIND**: it is not applied and no test runs. Otherwise it makes
`git worktree add --detach <work>/<id> <commit>`, writes the file's committed text with that one replacement into the
checkout (only a regular file: see the link guarantee below), runs `python -m pytest -x -q -p no:cacheprovider <test files>` there, and removes the checkout with
`git worktree remove --force`. The verdict comes from pytest's exit code and its junit report (written into the
run's scratch folder), not from its console:

- **CAUGHT**: a test failed (exit 1, the report records the failure), or a test's setup or teardown did; the tests
  cannot import the mutated file (exit 2, and the collection error's record names that file: a SyntaxError, or an
  error raised while it is imported); or the run went past `--timeout`.
- **SURVIVED**: the tests passed.
- **ERROR**: the run proves nothing about the mutant, and the run exits 1: pytest was interrupted (exit 2 with no
  collection error), failed internally (exit 3), was ended by a signal, found no test or was misused (exit 4, 5), or
  hit a collection error that does not come from the mutated file; or there was no checkout.

Before any mutant, each distinct set of test files runs once on an unmutated checkout. If one of these controls
fails, or runs past `--control-timeout`, no mutant runs: every mutant would look caught. A control has a bound of its
own because a short `--timeout` is how a hanging mutant is ended fast, and the unmutated tests can take longer than
that on a loaded machine (kxxx's gate on HQD, 10 Oct: `tests/test_mesh_normalize.py` passed `--timeout 5` to the
mutant it meant to time out, and its control took longer under the suite's load, so the run refused). By default
the control's bound is `--timeout`, but never under 300 s. A control that passed but took longer than `--timeout`
says so on its line: a mutant of those tests that times out proves nothing about the mutant.

A CAUGHT line says how the tests failed: `on an assertion` (an `AssertionError`, a bare `E   assert ...`, or
`Failed: DID NOT RAISE`), `on NameError, not an assertion`, or `timed out`. Kxxx's rule is that only an assertion
is a clean kill; `--assertions-only` holds a run to it.

Exit 0 only when every control passed and every mutant was CAUGHT; 1 otherwise; 2 when the command is wrong.

What the runner guarantees, and `tests/test_mutants_runner.py` checks on a fixture repository:

- nothing is applied in place: the repository, its `.git` folder included, is byte for byte the same afterwards;
- no mutant is written through a link. The commit's own tree is read first (`git ls-tree`): a path that is not a
  regular file there (a link is mode 120000) is an ERROR before any checkout is made. This is the guard that holds
  on Windows, where git with `core.symlinks=false` checks a committed link out as a plain file holding its target's
  path, so the checkout's realpath stays inside it (kxxx's gate, 10 Oct: the mutant through a link SURVIVED there).
  The realpath check is kept as the second guard: a path whose realpath in the checkout leads outside it is not
  written either;
- no worktree stays registered, and every temp folder (checkouts, pytest's `tmp_path`, the bytecode prefix) is inside
  one work folder that is removed at the end;
- no git command and no test run sees a `GIT_*` variable of the caller (a hook that exports `GIT_DIR` made
  `git -C <tmp>` write into the hook's repository on 10 Oct), and git runs with `core.hooksPath` set to an empty
  folder, so a checkout runs no hook;
- every test run has `PYTHONDONTWRITEBYTECODE=1` and a fresh, empty `PYTHONPYCACHEPREFIX`, so no `.pyc` of one
  mutant is read for the next (8 Oct: two mutants of one length written in the same second shared one);
- a run past the timeout gets an interrupt first, so pytest ends its session and `tests/conftest.py` drops the run's
  database, and is killed only if it is still there 30 s later (`--grace`). On Windows the interrupt is
  CTRL_BREAK_EVENT, which Python does not turn into KeyboardInterrupt: its default handler ends pytest at once, so
  there a timeout ends the run without a summary or a session finish, conftest's own drop does not run, and the
  runner's record-based drop (below) covers the run's database;
- a test run sees none of the caller's `PYTEST_*` variables, no `PYTHON*` one but the encoding ones, and no
  `DJANGO_SETTINGS_MODULE` (the rule of `tools/ci/local_gate.py`). Every place pytest writes is the run's own:
  `--basetemp`, `--junitxml` and `--log-file` inside the work folder, the cache provider off, and the repository's
  addopts cleared (`-o addopts=`). `PYTEST_ADDOPTS="--basetemp=DIR"` would have had pytest empty `DIR`, and an ini
  `log_file` is opened with mode `w` as pytest starts. The three paths are given relative to the checkout, because
  pytest expands `$VARIABLES` and `~` in `--junitxml` and the work folder's own path may hold a `$`;
- a test entry is a test file or a node id in one, written as pytest is given it: one starting with `-` or `@`
  (pytest expands `@FILE` into the arguments in FILE, after the runner's own), an absolute one, one with `..` or a
  backslash (on POSIX `tests\test_x.py` is one file name), or one not in normal form refuses the list; one that is
  not a file in the checkout stops the run (a folder is collected through every link in it);
- no test run sees a database of the caller's (`MESHY_TEST_DSN`, `MESHY_TEST_DSN_EXPLICIT`, `MESHY_DSN`,
  `DATABASE_URL`, `MESHY_CLONE_TEMPLATE`, `MESHY_LIVE_MODELS`): `tests/conftest.py` makes and drops its own every
  time. `MESHY_NO_TEST_DB` refuses the run (exit 2) before anything starts, because conftest would then make none;
- a test run's database never outlives it. Each run sets `MESHY_TEST_DB_RECORD` to a file in its scratch folder;
  `tests/conftest.py` appends the name of each database it makes there before making it (a pytest a test starts
  inherits the variable and appends its own). After the run, any recorded database that still exists (a pytest
  killed before `pytest_sessionfinish` never drops its own) is dropped through the `MESHY_TEMPLATE_DSN` server, and
  the run's line says so; one that cannot be dropped is a LEFTOVER (exit 1). Only names of conftest's shape
  (`meshy_test_<pid>_<6 hex>`) from the run's own record are touched. Without the variable conftest does exactly
  what it did before;
- what a test run leaves running is ended with it: once pytest (which leads its own process group) has exited,
  anything else alive in its group gets SIGTERM, then SIGKILL 5 s later, and only then is pytest reaped, so the
  group's id cannot have passed to another process. On Linux the runner is a child subreaper, so what a run leaves
  is adopted by the runner and reaped before the group counts as gone, also in a container whose PID 1 reaps
  nothing; on macOS launchd adopts and reaps it. A process that started a session of its own is not followed; on
  Windows only pytest itself is (best effort);
- SIGTERM or SIGINT stops a run cleanly: no further test run starts, each running one is interrupted and killed if it
  is still there 10 s later, every checkout is removed, then the work folder, and the exit code is 128 plus the
  signal's number. A second signal during that cleanup does not cut it short.

The runner's own mutants are a list too: `python tools/mutants/run.py --list mutants_runner` (about a minute each).

## The lists

| List | Code | Tests | Mutants | From |
|---|---|---|---|---|
| `mesh_normalize` | `apps/mesh/normalize.py` | `test_mesh_normalize.py` (+ `test_mesh_custody.py`, `test_mesh_audit.py`) | 81 | mutate, mutate2, mutate4, mutate5, mutate6, mutate8 (S1-S6), mutate9 |
| `mesh_audit` | `apps/mesh/audit.py` | `test_mesh_audit.py` (+ one database test) | 22 | mutate7, mutate8 (S7), mutate10 |
| `mesh_custody` | `apps/mesh/graph.py`, `tools/meshy_node/node.py`, `cli.py` | `test_mesh_custody.py`, one `test_meshy_node_owed.py` test | 8 | mutate3 |
| `meshy_node_cards` | `tools/meshy_node/cards.py`, `cli.py`, `node.py` | `test_meshy_node_cards.py` | 236 | mutate11, mutate11-search, -followups, -shape |
| `meshy_node_card_hook` | `tools/meshy_node/cards.py`, `cli.py` | `test_meshy_node_card_hook.py` | 19 | mutate-seat |
| `meshy_node_card_recent` | `tools/meshy_node/cards.py` | `test_meshy_node_card_recent.py` | 12 | crf (`card recent`'s follow-up to #224) |
| `mutants_runner` | `tools/mutants/run.py` | `test_mutants_runner.py` | 47 | written with the runner |

A mutant's name is the script's own name for it after the script's name and a colon (`mutate6:M12 KEY_CONTEXT = 20`),
so it can be traced back. The four `mutate11*` scripts were successive versions of one list; a mutant is listed once,
under the first script that held it. The texts are the scripts' own literals, unchanged; their helper texts are kept
under the script's name (`_HZ_RE` is mutate6's `RE`).

Of the 379 distinct mutants in the 15 scripts, 18 were BLIND at 1a1ea9f, because the code they were written against
had changed since. 5 are given the same mutation on today's code, each with a comment naming the commit its old text
came from and the commit that changed it; 13 are dropped, because pxx's later scripts already hold today's form of
the same mutation (the dropped ones are listed, with reasons, at the end of their list). One test was added to a
mutant: `mutate10:R5` said it "needs the database test", and that test is now named with it.

## Adding a list

A list is a module in `lists/` (its file name is its name) with one variable:

```python
MUTANTS = [
    # (name, repository path, original text, replacement, test files or node ids)
    ("area:short name of the fault", "apps/x/y.py", "    if allowed:\n", "    if True or allowed:\n",
     ["tests/test_y.py"]),
]
```

- The original must occur in the file exactly once at the commit; take enough of the line to make it so.
- Name the tests that should fail, and as few as do it: each mutant runs them all (`-x` stops at the first failure).
- Names must be unique across every list.
- Run `--check` first, then the list. A survivor is a test gap: say which input would tell the mutant apart.

When the code changes under a mutant it turns BLIND and the run exits 1. Give it the same mutation on the new code,
with a comment naming the commit of its old text, or drop it with a reason; do not change a mutant so that it applies
without asking whether it still makes the same fault.

## Open survivors at 1a1ea9f

The five ported lists, run at 1a1ea9f with `--jobs 3` under the laptop's Postgres (about 16 minutes): 366 mutants,
365 CAUGHT (363 on an assertion; `mutate7:A16` on a KeyError and `mutate3:M1` on a ValueError), 1 SURVIVED,
0 BLIND, 0 ERROR. The tests are not changed here; the survivor is a gap to close in its own change.

| List | Ported | BLIND | CAUGHT | SURVIVED |
|---|---|---|---|---|
| `mesh_normalize` | 81 | 0 | 80 | 1 |
| `mesh_audit` | 22 | 0 | 22 | 0 |
| `mesh_custody` | 8 | 0 | 8 | 0 |
| `meshy_node_cards` | 236 | 0 | 236 | 0 |
| `meshy_node_card_hook` | 19 | 0 | 19 | 0 |

- **`mutate:M9 url paths`**, `apps/mesh/normalize.py:210`: `_excluded_spans` without `_URL`. It survives because
  `_LOCAL_RUN` (`normalize.py:122`) already spans every URL's `//host/path` up to the first space, backtick or
  quote, so the URL exclusion only matters where a URL holds a quote or backtick, and no test has one. The tests
  that name this mutant (`tests/test_mesh_normalize.py:311` and `:329`, "drop the URL ... exclusions", "take a
  path out of a URL") use plain URLs only. Two bodies that tell it apart: ``see https://x.test/q?`docs/a.md` now``
  gives `path:docs/a.md` under the mutant and nothing today, and `see https://x.test/a'deadbeef1234 now` gives
  `hash:deadbeef1234` under the mutant and nothing today.
