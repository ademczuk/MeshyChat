"""The LOCAL gate: the whole suite on one named commit, run by the HQD integrator before every
fast-forward push to main (operator decision 2026-09-25, item 1; E04, incident #904).

This is THE gate. Hosted CI does not gate: the operator decided on 2026-09-25 to route around
GitHub Actions, and on 2026-09-29 allowed ONE small workflow (.github/workflows/fast-checks.yml:
a fresh install on Linux and four test files, informing only). tests/test_local_gate.py fails
if any other .github/workflows file appears, or if that one could write or gate.

    python tools/ci/local_gate.py --commit SHA --template-dsn DSN [--pg-bin DIR] [--out DIR]
                                  [--repo DIR] [--require-conformance-bin]

It REFUSES before running anything (exit 1) when it has no template DSN: `--template-dsn`,
else MESHY_TEMPLATE_DSN from its environment, and NO default (operator decision 41,
2026-09-26: every pytest run names the Postgres server its database is created and dropped
on, because 127.0.0.1:5434 is this project's dev database on HQD and another project's
Postgres on HQB; tests/conftest.py refuses a run without it too). It prints the value it uses
(password masked) on a line of its own before anything runs, and hands it to the suite as
MESHY_TEMPLATE_DSN, whatever the shell had. With --require-conformance-bin it REFUSES, before
running anything, unless MESHY_BISCUIT_CONFORMANCE_BIN names an existing file: the two T12 tests
in tests/test_capability_interop.py that run a biscuit conformance tool skip without it; they
verify that Python-minted tokens meet the documented biscuit contract and so are deliberately
NOT on ALLOWED_SKIPS (memory-bxxxxx, 2026-09-29). The certification host provides the tool and
passes the flag; whenever the variable names a
file, flag or not, the gate prints the tool's path and sha256 on a line of its own. The gate host
also needs `node` on PATH: tests/test_meshy_mod_held_stop_gate.py runs the meshy mod's own node
suite and FAILS, not skips, without it (REFACTOR-QUEUE-2026-11 A4).
It also REFUSES when the working tree is dirty (a tracked change,
or an untracked file .gitignore does not cover) or HEAD is not the commit it was asked to
gate: a green run of some other tree says nothing about SHA. The tree check also refuses
what `git status` does not report (review 2026-09-25, finding 2): an index entry flagged
skip-worktree or assume-unchanged (its file can differ from the commit and status stays
empty), and an IGNORED file Python or pytest would load (a conftest.py, any .py, .pth or
sourceless .pyc, a pytest/tox/setup.cfg/pyproject config) outside __pycache__ and the
interpreter's own directory. On a clean tree it then REFUSES when `tools/mutants/run.py --check`
finds a BLIND mutant, one whose original text no longer occurs exactly once at SHA, so a mutant
list cannot drift away from the code it claims to test (mutant_list_problems). Then, from the repository root, with CI=true (a test that needs
pg_dump FAILS instead of skipping), MESHY_TEMPLATE_DSN (as above), MESHY_PG_BIN (from
--pg-bin, else the environment),
without the shell's PYTEST_*, DJANGO_SETTINGS_MODULE and every PYTHON* variable but the
encoding ones (PYTHONPATH can load a sitecustomize.py that sets PYTEST_PLUGINS again), with
no user site-packages, and with bytecode read and written only under OUT/pycache, so a .pyc
left in the tree's ignored __pycache__ (trusted when its recorded source size and mtime
match) is never run in place of the committed source (suite_env), it runs

    python -m pytest tests/ tools/ --collect-only -q -p no:cacheprovider > OUT/collect.txt
    python -m pytest tests/ tools/ -q -p no:cacheprovider --junitxml=OUT/junit.xml > OUT/suite.txt
                                                   (pytest's exit code -> OUT/suite.exit)

and decides from those FILES, not from anything it remembers: pytest's exit code must be 0;
pytest's own summary line (the last "N passed ... in Xs" line of suite.txt) must name no
failure or error, and its passed count must equal the report's; and tools/ci/junit_gate.py
must pass the report (no failure, no error, no skip off ALLOWED_SKIPS, no fewer tests than
were collected). The tree and HEAD are checked again afterwards, because a run during which
either changed tested something else.

The last line of stdout is the ONE verdict line, also written to OUT/verdict.txt:

    LOCAL GATE PASS <sha>: <pytest's summary line>
    LOCAL GATE REFUSED <sha>: <reason>; <reason> ...

Exit 0 on PASS, 1 on REFUSED. Standard library only (plus the interpreter it runs pytest
with, which is the one running this file: run it with the project's venv).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import junit_gate  # noqa: E402

# A skip the suite is allowed to report, as (WHERE, MESSAGE): WHERE must match the whole
# "classname::name" the JUnit report gives the test that skipped, MESSAGE its skip message.
# Anything else skipped is a test that did not run and nobody decided it may not
# (tests/test_local_gate.py checks each names a real skip). Each reason is pinned to the tests
# that own it (review 2026-09-25, finding 5: "^exempt: " alone was an open prefix any test
# could skip under).
# NOT on this list, on purpose: the two tests/test_capability_interop.py tests that skip with
# the no-conformance-binary message (T12, B07 slice 6, divergence 040). They are the only
# proof of token interoperability,
# so a gate that let them skip would stay green while that went untested (memory-bxxxxx,
# 2026-09-29). The box that certifies main builds the tool and runs this gate with
# --require-conformance-bin. Its hash changes with every rebuild, so the gate prints the one it
# was given.
_OPENER = (r"tests\.test_every_connection_is_role_bound::"
           r"test_each_opener_binds_the_role_or_is_named_exempt")
ALLOWED_SKIPS = [
    (r"tests\.test_meshy_node_errors::test_windows_network_errnos\[WSA(ENETUNREACH|EHOSTUNREACH|ECONNRESET)\]",
     "^Windows socket errno is unavailable on this platform$"),
    # PR388: NTFS does not alias these NFD spellings. Pin each skip to the
    # native-alias case that owns it and to its complete reason.
    (r"tests\.test_transcript_cards_file_identity::test_native_case_or_unicode_alias\[unicode\]",
     "^filesystem does not alias these spellings$"),
    (r"tests\.test_transcript_cards_file_identity::test_alias_exclusion_end_to_end\[NFD-(antigravity|kimi|claude)\]",
     "^filesystem does not make the NFD spelling an alias$"),
    (r"tests\.test_pr372_op_attachments::test_an_op_refuses_a_fifo_at_once",
     "^no FIFOs on this platform$"),
    (r"tests\.test_postbox_delivery_bank::test_receipts_are_owner_only_and_shared_receipts_are_refused",
     "^POSIX receipt modes$"),
    (r"tests\.test_postbox_delivery_bank::test_symlink_receipt_refused",
     "^this account cannot create a symlink without elevation$"),
    (r"tests\.test_(meshaudit|meshsem|meshsem_related)_live::.+",
     "^live LAN models: set MESHY_LIVE_MODELS=1$"),
    (r"tests\.test_meshsum_live::.+", "^live HQB test: set MESHY_LIVE_MODELS=1$"),
    (_OPENER + r"\[apps\.core\.dbconn\]",
     "^the guarded constructor is the thing every other path should use$"),
    (_OPENER + r"\[apps\.core\.sqlmigrate\]", "^exempt: "),
    # fin/biscuit (divergence 040, slice 4 review F4), integ/r11: the root key file's owner-only
    # mode has one test per platform, and the POSIX one skips on Windows, where the gate runs;
    # the Windows ACL test beside it runs instead. Pinned to that one test and its whole reason.
    (r"tests\.test_capability_keys::test_on_posix_the_key_file_is_owner_only_from_its_first_byte",
     "^POSIX modes; the Windows ACL is the next test$"),
    # fin/w2-backup-serve (E02), integ/r14: creating a symlink on Windows needs elevation or
    # Developer Mode, and the gate runs neither. The reparse-point branch it covers still runs
    # in the junction and synthetic-reparse tests beside it. Pinned to that one test.
    (r"tests\.test_backup_serve::test_a_symlink_with_a_valid_name_is_neither_listed_nor_served",
     "^this account cannot create a symlink without elevation"),
    # fin/postbox-macos: /dev/null detection is postbox_os's POSIX branch and skips on Windows,
    # where the gate runs; its other POSIX paths run
    # here (command text, the listener shim). Pinned to that one test and its whole reason.
    (r"tests\.test_postbox_macos::test_posix_devnull_is_detected",
     "^/dev/null detection is the POSIX branch$"),
    # txxxx/postbox-kimi-delivery (#381): postbox_child.py, the POSIX shim, ends a kimi turn's detached grandchild
    # when its daemon dies. Windows ties a turn to its daemon with the kill-on-close job instead and starts no shim,
    # so this test skips where the gate runs; the Windows path is test_real_a_daemon_killed_mid_turn_leaves_nothing_
    # wedged, which runs here. Pinned to that one test and its whole reason.
    (r"tests\.test_postbox_kimi_delivery::test_real_postbox_child_ends_a_detached_grandchild_when_its_daemon_dies",
     "^POSIX only: Windows ends a turn and its descendants by the kill-on-close job$"),
    # kxxx/p2-2-helper-settlement (#513): the helper gate is a POSIX exec and Windows starts helpers with no gate, so this
    # whole module skips where the gate runs, and THIS GATE NEVER RUNS IT. It runs one file at a time in a Linux container
    # on HQB (antigravity: 62 passed at 7a6a7500, msg 08d2dc), and oxxxxxx re-derives it on the HZ at each re-pin. What is
    # left without the gate runs here in tests/test_postbox_helper_spawning_records.py. Pinned to that module and its reason.
    (r"tests\.test_postbox_helper_settlement::.+", "^the gate is POSIX only$"),
    # pxx/card-recent (#224): `card recent` refuses a state file or a receipt that is not a regular file, and never
    # waits on a FIFO or a terminal; a pipe named as /dev/fd/N, a FIFO and a pty are POSIX objects, so these skip
    # on Windows, where the gate runs (kxxx's gate fb045947). The not-a-regular-file branch still runs there, by a
    # directory as the receipt. Pinned to each test and its whole reason.
    (r"tests\.test_meshy_node_card_recent::test_a_state_file_that_is_a_pipe_is_refused_and_never_read",
     "^/dev/fd is POSIX$"),
    (r"tests\.test_meshy_node_card_recent::test_a_receipt_that_is_a_pipe_is_refused_and_never_read",
     "^/dev/fd is POSIX$"),
    (r"tests\.test_meshy_node_card_recent::test_a_receipt_fifo_with_no_writer_is_not_waited_for",
     "^a FIFO is POSIX$"),
    (r"tests\.test_meshy_node_card_recent::test_a_terminal_that_is_gone_is_still_exit_0_and_says_nothing\[(True|False)\]",
     "^a pty is POSIX$"),
    # pxx/docs-client-update-meshy-mod (#203): the client-update page's block runs in bash with
    # git, tr, cut, find (or cat) and sha256sum, and these tests skip on Windows, where the gate
    # runs, because System32's bash.exe is WSL's and sees other paths. memory-bxxxxx ran the file
    # in Git Bash on HQB with the skip lifted (review of #203): the block itself passes there.
    # Pinned to that one module and its two whole reasons, written without regex metacharacters.
    (r"tests\.test_client_update_covers_the_mod::.+",
     "^needs bash, git, tr, cut, find and sha256sum, and not Windows, where System32's bash is WSL's$"),
    (r"tests\.test_client_update_covers_the_mod::.+",
     "^needs bash, git, tr, cut, cat and find, and not Windows, where System32's bash is WSL's$"),
    # pxx/mutants-runner (#216): tools/mutants/run.py ends a test run's whole POSIX process group, stops on SIGTERM
    # or SIGINT, and on Linux is a child subreaper; on Windows it waits for and ends pytest alone (best effort, as
    # its docstring says), so these seven checks skip where the gate runs (kxxx's gate, 10 Oct). The link refusal
    # and the control's timeout run on Windows too. Each is pinned to its one test and its whole reason.
    (r"tests\.test_mutants_runner::test_a_signal_to_the_runner_ends_its_test_runs_and_removes_everything\[.+\]",
     "^POSIX signals$"),
    (r"tests\.test_mutants_runner::test_what_a_test_run_leaves_running_is_ended_on_a_timeout_and_on_a_normal_exit",
     "^POSIX process groups$"),
    (r"tests\.test_mutants_runner::test_a_zombie_the_runner_adopted_counts_until_it_is_reaped",
     "^POSIX process groups$"),
    # On Windows a backslash separates folders, so no file can be named `tests\test_ok.py`; the backslash entry is
    # still refused there by the list check, which tests/test_mutants_runner.py's parametrized refusal test runs.
    (r"tests\.test_mutants_runner::test_a_backslash_entry_never_reaches_a_link_of_that_name",
     "^a backslash is a file name's character on POSIX only$"),
    (r"tests\.test_mutants_runner::test_a_folder_entry_is_refused_and_never_collected_through_its_links",
     "^POSIX links$"),
    (r"tests\.test_mutants_runner::test_the_runner_is_a_child_subreaper_on_linux",
     "^PR_SET_CHILD_SUBREAPER is Linux's$"),
    # pxx/card-recent-session-start (#236 part 1, #263): the SessionStart briefing's child runs in its own POSIX
    # session and the hook kills its whole process group, so a descendant that holds stdout dies with it. On Windows,
    # where the gate runs (kxxx's gate 60af8de7), the hook kills the child alone, and `card recent` starts no process
    # of its own. The timeout, cap and cleanup tests run there too. Pinned to that one test and its whole reason.
    (r"tests\.test_transcript_cards_load_hook::test_session_start_timeout_kills_descendants_even_if_leader_exited\[(True|False)\]",
     "^POSIX process-group descendants$"),
    # pxx/card-recent-freshness (#236 part 2, #266): the freshness cache is refused, never read or written through,
    # when it is a FIFO or a link, and a store under a linked parent folder still works. On Windows, where the gate
    # runs, a FIFO does not exist and the account cannot make a symlink without elevation, so these three cases skip;
    # the corrupt, array, oversized and directory cases, the atomic replace and the Windows-API test run there. Pinned
    # to each test case and its whole reason.
    (r"tests\.test_meshy_node_card_recent::test_seat_freshness_unsafe_cache_is_ignored_without_following_links\[fifo\]",
     "^FIFO files are unavailable on this platform$"),
    (r"tests\.test_meshy_node_card_recent::test_seat_freshness_unsafe_cache_is_ignored_without_following_links\[link\]",
     "^file symlinks are unavailable on this platform$"),
    (r"tests\.test_meshy_node_card_recent::test_seat_freshness_parent_directory_link_is_allowed",
     "^directory symlinks require POSIX$"),
    # pxx/windows-attachment-names (#293): the POSIX receive branch reaches the presign for every Windows-unsafe name,
    # but some Windows builds report a device name (CON, aux, NUL.bin ...) as present in any folder, so on such a build
    # that row skips (HQD, gate ed7d6861: 9 rows; HQB, Windows 11 26200: none). Which rows depends on the build, so the
    # id is open and the whole reason is pinned (the id names the row). The Windows branch's refusal of every
    # one of these names runs on Windows too (test_windows_unsafe_attachment_is_refused_before_presign_or_download).
    (r"tests\.test_meshy_node_attachments::test_posix_receiver_keeps_access_to_older_names_before_presign\[.+\]",
     "^this Windows build reports a device name as present in an empty folder$"),
    # The same PR: a POSIX receiver still downloads these three legacy names an older sender could publish; Windows
    # cannot hold them as files, so the three rows skip there. Pinned to each id and the whole reason.
    (r"tests\.test_meshy_node_attachments::test_posix_receiver_downloads_older_windows_unsafe_names\[('report:v2\.txt'|'CON'|'draft\.')\]",
     "^these legacy names require a POSIX filesystem$"),
    # #562 (bank retarget at 0600): the 8 Nov state was a 0644 checkpoint on a POSIX box; Windows has no such mode
    # bits, so the repair-the-mode test skips there. The retarget's lock, its atomic rewrite and its refusal while a
    # postbox runs are tested beside it on every platform. Pinned to that one test and its whole reason (mg562s).
    (r"tests\.test_postbox_bank_fault::test_a_0644_checkpoint_with_the_right_consumer_is_rewritten_0600",
     "^file modes are POSIX$"),
    # The tests under tools/ (TEST_PATHS below) were collected by no run until 17 Nov 2026: memory-bxxxxx ran them on
    # main 5dac10f3 on HQB, "226 passed, 19 skipped, 991 subtests passed in 99.72s", and named every skip. A unittest
    # class's tests carry the class in their classname, so these name it.
    (r"tools\.transcript_cards\.test_context_wire_compatibility\.ContextWireCompatibility::test_\w+",
     "^explicit pinned validator snapshot required$"),
    (r"tools\.transcript_cards\.test_native_window\.\w+::"
     r"test_778_mib_source_drains_with_bounded_reads_and_small_source_interleaving",
     "^opt-in 778 MiB synthetic read-bound acceptance$"),
    (r"tools\.transcript_cards\.test_capture_queue\.\w+::test_native_unicode_alias_rejected_by_inventory",
     "^source filesystem has distinct Unicode normalization names$"),
    (r"tools\.transcript_cards\.test_capture_queue\.\w+::test_native_case_alias_rejected_by_inventory",
     "^source filesystem has distinct case-sensitive names$"),
]

#: What the gate runs. tests/ alone left the 245 tests under tools/ in no run at all (they passed on 5dac10f3,
#: memory-bxxxxx); they run in the same pytest invocation, so one summary line and one junit file cover both.
TEST_PATHS = ("tests/", "tools/")

#: pytest 9 also counts unittest subTests: "226 passed, 19 skipped, 991 subtests passed in 99.72s" (#662, GitHub
#: Codex P1). A label may be "subtests <outcome>".
_SUMMARY = re.compile(r"^=*\s*((?:\d+ (?:subtests? )?[a-z]+(?:, )?)+) in [\d.]+s\b.*?=*\s*$")


def _git(repo: Path, *args: str) -> str:
    # No cached answer about the tree (fsmonitor, untracked cache) and no replace refs: the
    # gate asks git about the files as they are now and the commit as it is.
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1")
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                           *args], cwd=repo, capture_output=True, text=True, check=True,
                          env=env).stdout.strip()


# An ignored file by these names or suffixes is code or configuration Python or pytest would
# load from the tree; __pycache__ is not (bytecode is read from OUT/pycache, see suite_env).
_LOADABLE_NAMES = {"conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg",
                   "pyproject.toml", "sitecustomize.py", "usercustomize.py"}
_LOADABLE_SUFFIXES = {".py", ".pyc", ".pyo", ".pyd", ".pth", ".so", ".pyw"}


def _loadable(rel: str, interpreter_dirs: list[str]) -> bool:
    parts = rel.rstrip("/").split("/")
    if "__pycache__" in parts or any(rel == d or rel.startswith(d + "/")
                                     for d in interpreter_dirs):
        return False
    name = parts[-1]
    return name in _LOADABLE_NAMES or Path(name).suffix.lower() in _LOADABLE_SUFFIXES


def _interpreter_dirs(repo: Path) -> list[str]:
    """The running interpreter's own prefix, relative to the repository, when it lives inside
    it (a .venv in the clone): the gate trusts the interpreter it runs with."""
    out = []
    for p in {sys.prefix, sys.base_prefix}:
        try:
            out.append(Path(p).resolve().relative_to(repo.resolve()).as_posix())
        except ValueError:
            pass
    return [d for d in out if d not in ("", ".")]


def tree_problems(repo: Path, sha: str) -> list[str]:
    problems = []
    head = _git(repo, "rev-parse", "HEAD")
    if head != sha:
        problems.append(f"HEAD is {head}, not the commit asked for")
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    if dirty:
        problems.append(f"dirty tree ({len(dirty)} paths, e.g. "
                        f"{[d.strip() for d in dirty[:5]]})")
    # `git ls-files -v`: "H" is a plain cached entry; "S" is skip-worktree and a lower-case
    # tag is assume-unchanged, either of which lets the file differ with status empty.
    flagged = [ln for ln in _git(repo, "ls-files", "-v").splitlines()
               if ln and not ln.startswith("H ")]
    if flagged:
        problems.append(f"index flags hide changes from git status ({len(flagged)} entries, "
                        f"e.g. {flagged[:5]}; git update-index --no-skip-worktree / "
                        "--no-assume-unchanged)")
    ignored = [ln[3:] for ln in _git(repo, "status", "--porcelain=v1", "--ignored",
                                     "--untracked-files=all").splitlines()
               if ln.startswith("!! ")]
    dirs = _interpreter_dirs(repo)
    loadable = [p.strip('"') for p in ignored if _loadable(p.strip('"'), dirs)]
    if loadable:
        problems.append(f"ignored files Python or pytest would load ({len(loadable)}, e.g. "
                        f"{loadable[:5]})")
    return problems


def summary_counts(summary: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for n, sub, word in re.findall(r"(\d+) (subtests? )?([a-z]+)", summary):
        key = {"errors": "error", "warnings": "warning"}.get(word, word)
        if sub and key not in ("failed", "error"):
            key = "subtests " + key                 # a failed or erroring subtest refuses like a failed test
        counts[key] = counts.get(key, 0) + int(n)
    return counts


def decide(out: Path) -> tuple[list[str], str | None]:
    """(problems, pytest's summary line) from the files a run left in `out`."""
    problems: list[str] = []
    exit_file = out / "suite.exit"
    if not exit_file.is_file():
        problems.append(f"no {exit_file.name}: pytest's exit code was not recorded")
    else:
        code = exit_file.read_text(encoding="utf-8").strip()
        if code != "0":
            problems.append(f"pytest exit code {code}")
    summary = None
    suite = out / "suite.txt"
    if suite.is_file():
        for line in suite.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _SUMMARY.match(line.strip())
            if m:
                summary = line.strip().strip("=").strip()
    if summary is None:
        problems.append(f"no pytest summary line ('N passed ... in Xs') in {suite.name}")
        counts = {}
    else:
        counts = summary_counts(summary)
        for bad in ("failed", "error"):
            if counts.get(bad):
                problems.append(f"summary: {counts[bad]} {bad}")
    collect = out / "collect.txt"
    try:
        collected = junit_gate.collected_from(collect)
    except SystemExit as exc:
        problems.append(str(exc))
        collected = None
    report = junit_gate.evaluate(out / "junit.xml", collected, ALLOWED_SKIPS)
    problems += [f"junit: {p}" for p in report["problems"]]
    if summary is not None and "counts" in report:
        if counts.get("passed", 0) != report["counts"]["passed"]:
            problems.append(f"summary says {counts.get('passed', 0)} passed, the report "
                            f"{report['counts']['passed']}")
    return problems, summary


def suite_env(parent, pg_bin: str | None, pycache: Path | None = None,
              template_dsn: str | None = None) -> dict[str, str]:
    """The environment the suite runs in: the shell's, minus what would change WHICH tests
    run or how they are configured without showing in the files. PYTEST_ADDOPTS="-k x"
    narrows the collect and the run alike, so the collected-count check cannot see it;
    PYTEST_PLUGINS loads code; DJANGO_SETTINGS_MODULE freezes settings before
    tests/conftest.py runs (its _correct_a_setting_frozen_before_us).

    Every PYTHON* variable goes too, except the encoding ones (review finding 2):
    PYTHONPATH can put a sitecustomize.py first that sets PYTEST_PLUGINS after this function
    removed it, PYTHONHOME and PYTHONUSERBASE move where modules come from, PYTHONSTARTUP and
    PYTHONINSPECT run code. User site-packages are off, and bytecode is read and written only
    under `pycache` (OUT/pycache), never from the tree's __pycache__.

    MESHY_TEMPLATE_DSN is the gate's value (`template_dsn`), never the shell's: the one the
    gate printed is the one the suite gets (decision 41)."""
    env = {k: v for k, v in parent.items()
           if not k.upper().startswith(("PYTEST_", "PYTHON"))
           and k.upper() not in ("DJANGO_SETTINGS_MODULE", "MESHY_TEMPLATE_DSN")}
    for k, v in parent.items():
        if k.upper() in _PYTHON_KEPT:
            env[k] = v
    env["CI"] = "true"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    if pycache:
        env["PYTHONPYCACHEPREFIX"] = str(pycache)
    if pg_bin:
        env["MESHY_PG_BIN"] = pg_bin
    if template_dsn:
        env["MESHY_TEMPLATE_DSN"] = template_dsn
    return env


def shown_dsn(dsn: str) -> str:
    """The DSN with its password masked, for the line the gate prints: which server and
    which user are the point, the password is not. URL form and key=value form both."""
    if "://" in dsn:
        parts = urllib.parse.urlsplit(dsn)
        if parts.password is not None:
            netloc = parts.netloc.rsplit("@", 1)
            user = netloc[0].split(":", 1)[0]
            parts = parts._replace(netloc=f"{user}:***@{netloc[1]}")
        return urllib.parse.urlunsplit(parts)
    return re.sub(r"(password\s*=\s*)('[^']*'|\S+)", r"\1***", dsn)


# The PYTHON* variables the suite keeps: they choose an encoding, not code.
_PYTHON_KEPT = {"PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO"}


def _run(cmd: list[str], cwd: Path, env: dict, log: Path) -> int:
    with open(log, "wb") as f:                 # a file, never a pipe: nothing can wedge on it
        return subprocess.run(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL).returncode


CONFORMANCE_ENV = "MESHY_BISCUIT_CONFORMANCE_BIN"


def conformance_problem(value: str | None) -> str | None:
    """Why the T12 interop tests could not run their tool from this environment, or None."""
    v = (value or "").strip()
    if not v:
        return (f"{CONFORMANCE_ENV} is not set; --require-conformance-bin needs it to name a "
                "built biscuit conformance tool")
    if not Path(v).is_file():
        return f"{CONFORMANCE_ENV} names no file: {v}"
    return None


def conformance_line(value: str | None) -> str | None:
    """The line the gate prints when the variable names a file: which tool the suite will run."""
    v = (value or "").strip()
    if not v or not Path(v).is_file():
        return None
    import hashlib
    digest = hashlib.sha256(Path(v).read_bytes()).hexdigest()
    return f"LOCAL GATE conformance tool ({CONFORMANCE_ENV}): {v} sha256 {digest}"


# ---- FAST mode (kxxx, 9 Nov 2026, on memory-bxxxxx's design note in <thread>) ----
#
# A client-only PR can be gated in about 15 minutes instead of about 70: part A runs every file on
# tests/no_db.txt with NO database (MESHY_NO_TEST_DB=1 and both DSNs on a closed port, so a file
# that reaches for one fails instead of quietly using a shared one), and part B runs the PR's own
# test files and every test file that names a changed file, with the throwaway Postgres as the
# full gate does. Everything else is skipped and listed in OUT/skipped-files.txt.
#
# Eligibility is an ALLOW-LIST of client paths and fails closed: a changed path it does not
# recognise means a full gate. The gate's own code (tools/ci/), schema (sql/, sql_meshychat/),
# settings, manage.py, conftest, shared fixtures and every dependency or lock file always need
# the full gate.
#
# Backstop rules (process, enforced by the merge seat, not by this file): the full gate runs on
# main after every third fast merge and at least once a day; a red backstop is bisected across
# the fast merges since the last green full run, and until it is green again no further fast
# merge lands and no client install from a fast merge goes out; a production SERVER deploy needs
# a green FULL gate at that exact main sha, whatever ran before.

NO_DB_LIST = "tests/no_db.txt"
FAST_DEAD_DSN = "postgresql://nobody@127.0.0.1:1/meshy_no_database"
FAST_CLIENT_PREFIXES = ("tools/meshy_node/", "tools/postbox/", "tools/transcript_cards/", "docs/")
_FAST_DEPENDENCY_NAMES = {"package.json", "package-lock.json", "npm-shrinkwrap.json", "pyproject.toml",
                          "setup.py", "setup.cfg", "pipfile", "pipfile.lock", "poetry.lock", "uv.lock"}
_TEST_FILE = re.compile(r"tests/test_[^/]+\.py")


def fast_blockers(paths: list[str]) -> list[str]:
    """The changed paths that make a PR need the FULL gate. Empty means fast-eligible."""
    blockers = []
    for p in paths:
        name = p.rsplit("/", 1)[-1].lower()
        if name in _FAST_DEPENDENCY_NAMES or name.startswith(("requirements", "constraints")):
            blockers.append(p)                 # a dependency or lock file, wherever it is
        elif p.startswith("tools/ci/"):
            blockers.append(p)                 # a PR that changes the gate is never judged by it
        elif p.startswith(FAST_CLIENT_PREFIXES) or _TEST_FILE.fullmatch(p):
            continue
        elif "/" not in p and name.endswith(".md"):
            continue                           # README, CHANGELOG and the like
        else:
            blockers.append(p)                 # anything not known to be client-side
    return blockers


def mutant_list_problems(repo: Path, sha: str, timeout: float = 300.0) -> list[str]:
    """REFACTOR-QUEUE A5: every mutant list's original text must occur exactly once at SHA, which is what
    `tools/mutants/run.py --check` reports (no checkout, no test run). A BLIND entry is a mutant no run can
    apply, so a full mutant run exits 1 and the list claims more than it tests; ten had drifted that way by
    c64db7aa without anyone seeing it. Checked before the suite, like the tree. A repository with neither the
    runner nor its lists (the gate's own tiny test repositories) has nothing to check; lists without the runner
    is a refusal, so a commit that moves or renames run.py cannot switch the check off. The child gets the
    suite's own cleaned environment (suite_env), so a PYTHONPATH or PYTEST_* in the gate's shell reaches
    neither, and a fresh, empty bytecode prefix of its own (the Codex review of #645): `-B` and
    PYTHONDONTWRITEBYTECODE stop cache WRITES, not reads, and run.py imports each list from the tree, so a
    stale .pyc left in an ignored tools/mutants/lists/__pycache__ (trusted when its recorded source size and
    mtime match, and the tree check allows __pycache__) would be checked instead of the committed list."""
    runner = repo / "tools" / "mutants" / "run.py"
    lists = repo / "tools" / "mutants" / "lists"
    if not runner.is_file():
        if lists.is_dir() and any(lists.glob("*.py")):
            return ["tools/mutants/lists holds mutant lists but tools/mutants/run.py is missing, so the lists "
                    "cannot be checked"]
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="mutant-pycache-", ignore_cleanup_errors=True) as cache:
            p = subprocess.run([sys.executable, "-B", str(runner), "--check", "--rev", sha, "--repo", str(repo)],
                               cwd=repo, capture_output=True, text=True, timeout=timeout,
                               env=dict(suite_env(os.environ, None, Path(cache)), PYTHONDONTWRITEBYTECODE="1"))
    except subprocess.TimeoutExpired:
        return [f"tools/mutants/run.py --check did not finish in {timeout:.0f} s"]
    if p.returncode == 0:
        return []
    blind = [ln.split("|")[0].split("]", 1)[-1].strip() for ln in p.stdout.splitlines() if ln.startswith("BLIND")]
    if blind:
        return [f"tools/mutants/run.py --check: {len(blind)} BLIND mutant(s), their original text no longer "
                f"occurs exactly once: {'; '.join(blind[:10])}"]
    tail = (p.stdout.strip().splitlines() or p.stderr.strip().splitlines() or ["no output"])[-1]
    return [f"tools/mutants/run.py --check failed (exit {p.returncode}): {tail}"]


def no_db_list(repo: Path, required: bool = True) -> tuple[list[str], str, list[str]]:
    """(files, sha256 of the list file, problems). A listed file that is missing, renamed or not a
    test file is a problem, so the list can't rot silently (it also refuses the FULL gate). An
    absent list is a problem only when `required` (fast mode); the full gate runs without one."""
    import hashlib
    path = repo / NO_DB_LIST
    if not path.is_file():
        return [], "", [f"{NO_DB_LIST} is missing"] if required else []
    raw = path.read_bytes()
    files, problems, seen = [], [], set()
    for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if not _TEST_FILE.fullmatch(entry):
            problems.append(f"{NO_DB_LIST}:{n}: {entry!r} is not a tests/test_*.py path")
        elif entry in seen:
            problems.append(f"{NO_DB_LIST}:{n}: {entry} is listed twice")
        elif not (repo / entry).is_file():
            problems.append(f"{NO_DB_LIST}:{n}: {entry} does not exist (removed or renamed?)")
        else:
            files.append(entry)
        seen.add(entry)
    return files, hashlib.sha256(raw).hexdigest(), problems


def _needles(path: str) -> list[str]:
    """How a test file can name a changed file without importing it: the path itself (a
    spec_from_file_location or a script path) and, for Python, the dotted module (import, runpy,
    `python -m`)."""
    out = [path]
    if path.endswith(".py"):
        mod = path[:-3].replace("/", ".")
        out.append(mod[:-len(".__init__")] if mod.endswith(".__init__") else mod)
    return out


def part_b_files(repo: Path, changed: list[str], all_tests: list[str]) -> list[str]:
    """The PR's own changed test files, plus every test file whose TEXT names a changed file by its
    path or dotted module. A false hit costs seconds; a miss lets a broken client file through."""
    selected = {p for p in changed if _TEST_FILE.fullmatch(p) and (repo / p).is_file()}
    sources = [p for p in changed if not _TEST_FILE.fullmatch(p)]
    needles = [n for p in sources for n in _needles(p)]
    # A path assembled from components can name only the filename. Skip generic
    # or short names: they occur across the suite and would select nearly all tests.
    needles += [name for p in changed if (name := p.rsplit("/", 1)[-1])
                not in {"__init__.py", "conftest.py"} and len(name) >= 6]
    # A Windows path, as written in a file or inside a Python string literal (doubled).
    needles += [n.replace("/", sep) for n in needles if "/" in n for sep in ("\\", "\\\\")]
    # `from tools.postbox import bank_fault` names the module in neither form above.
    froms = [re.compile(r"from\s+" + re.escape(p[:-3].rsplit("/", 1)[0].replace("/", ".")) + r"\s+import\s+[^\n]*\b"
                        + re.escape(p[:-3].rsplit("/", 1)[1]) + r"\b")
             for p in sources if p.endswith(".py") and "/" in p and not p.endswith("/__init__.py")]
    for test in all_tests:
        if test in selected:
            continue
        text = (repo / test).read_text(encoding="utf-8", errors="replace")
        if any(n in text for n in needles) or any(f.search(text) for f in froms):
            selected.add(test)
    return sorted(selected)


def _fast_part(repo: Path, files: list[str], env: dict, out: Path) -> tuple[list[str], str | None]:
    out.mkdir(parents=True, exist_ok=True)
    py = [sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"]
    _run(py + ["--collect-only"], repo, env, out / "collect.txt")
    code = _run(py + [f"--junitxml={out / 'junit.xml'}"], repo, env, out / "suite.txt")
    (out / "suite.exit").write_text(f"{code}\n", encoding="utf-8")
    return decide(out)


def fast_gate(repo: Path, commit: str, base: str, out: Path | None, pg_bin: str | None,
              template_dsn: str | None = None) -> tuple[int, str]:
    repo = repo.resolve()
    try:
        sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
        base_sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        fork = _git(repo, "merge-base", base_sha, sha)
    except subprocess.CalledProcessError:
        return 1, f"LOCAL GATE REFUSED (fast) {commit}: {commit} or base {base} is not a commit in {repo}"
    head = f"LOCAL GATE REFUSED (fast) {sha}: "
    if not (template_dsn or "").strip():
        return 1, head + "MESHY_TEMPLATE_DSN is not set (part B runs with the throwaway Postgres)"
    changed = [p for p in _git(repo, "diff", "--name-only", "--no-renames", f"{fork}..{sha}").splitlines() if p]
    if not changed:
        return 1, head + f"no change against base {base_sha[:12]}; nothing to gate"
    blockers = fast_blockers(changed)
    if blockers:
        return 1, head + (f"not fast-eligible, run the FULL gate ({len(blockers)} path(s) outside the "
                          f"client allow-list, e.g. {blockers[:5]})")
    if out is None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = Path(tempfile.gettempdir()) / "meshy-local-gate" / f"{sha[:12]}-fast-{stamp}"
    out = out.resolve()
    if out == repo or repo in out.parents:
        return 1, head + f"--out {out} is inside the repository"
    problems = tree_problems(repo, sha)
    no_db, list_sha, list_problems = no_db_list(repo)
    problems += list_problems
    if problems:
        return 1, head + "; ".join(problems[:20])
    all_tests = sorted(p for p in _git(repo, "ls-files", "tests/").splitlines() if _TEST_FILE.fullmatch(p))
    b_files = [p for p in part_b_files(repo, changed, all_tests) if p not in no_db]
    out.mkdir(parents=True, exist_ok=True)
    skipped = [p for p in all_tests if p not in no_db and p not in b_files]
    (out / "skipped-files.txt").write_text("".join(p + "\n" for p in skipped), encoding="utf-8")
    (out / "part-b-files.txt").write_text("".join(p + "\n" for p in b_files), encoding="utf-8")
    env_a = suite_env(os.environ, pg_bin, out / "pycache", FAST_DEAD_DSN)
    env_a.update(MESHY_NO_TEST_DB="1", MESHY_DSN=FAST_DEAD_DSN,
                 MESHY_TEST_DSN=FAST_DEAD_DSN, MESHY_TEMPLATE_DSN=FAST_DEAD_DSN)
    problems_a, summary_a = _fast_part(repo, no_db, env_a, out / "A")
    summary_b, problems_b = "no files", []
    if b_files:
        env_b = suite_env(os.environ, pg_bin, out / "pycache", template_dsn.strip())
        problems_b, summary_b = _fast_part(repo, b_files, env_b, out / "B")
    problems = ([f"A: {p}" for p in problems_a] + [f"B: {p}" for p in problems_b]
                + [f"after the run: {p}" for p in tree_problems(repo, sha)])
    detail = (f"A ({len(no_db)} files, no database): {summary_a}; B ({len(b_files)} files, Postgres): "
              f"{summary_b}; {len(skipped)} test files skipped; {NO_DB_LIST} sha256 {list_sha[:16]}; "
              f"base {base_sha[:12]}")
    if problems:
        return 1, head + detail + "; " + "; ".join(problems[:20]) + f" (files: {out})"
    return 0, f"LOCAL GATE PASS (fast) {sha}: {detail} (files: {out})"


def gate(repo: Path, commit: str, out: Path | None, pg_bin: str | None,
         template_dsn: str | None = None) -> tuple[int, str]:
    repo = repo.resolve()
    try:
        sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        return 1, f"LOCAL GATE REFUSED {commit}: not a commit in {repo}"
    if not (template_dsn or "").strip():
        return 1, (f"LOCAL GATE REFUSED {sha}: MESHY_TEMPLATE_DSN is not set; pass "
                   "--template-dsn DSN or set MESHY_TEMPLATE_DSN (the Postgres server the "
                   "suite creates and drops its database on; no default, operator decision 41)")
    if out is None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = Path(tempfile.gettempdir()) / "meshy-local-gate" / f"{sha[:12]}-{stamp}"
    out = out.resolve()
    if out == repo or repo in out.parents:
        return 1, (f"LOCAL GATE REFUSED {sha}: --out {out} is inside the repository; its "
                   "files would dirty the tree the gate is checking")
    problems = tree_problems(repo, sha) + no_db_list(repo, required=False)[2]
    if not problems:
        problems = mutant_list_problems(repo, sha)          # on a clean tree only: the lists are read from it
    if problems:
        return 1, f"LOCAL GATE REFUSED {sha}: " + "; ".join(problems)
    out.mkdir(parents=True, exist_ok=True)
    env = suite_env(os.environ, pg_bin, out / "pycache", template_dsn.strip())
    # Only the paths the gated tree has: pytest refuses a run naming a missing directory (exit 4).
    paths = [p for p in TEST_PATHS if (repo / p).is_dir()]
    py = [sys.executable, "-m", "pytest", *paths, "-q", "-p", "no:cacheprovider"]
    _run(py + ["--collect-only"], repo, env, out / "collect.txt")
    code = _run(py + [f"--junitxml={out / 'junit.xml'}"], repo, env, out / "suite.txt")
    (out / "suite.exit").write_text(f"{code}\n", encoding="utf-8")
    problems, summary = decide(out)
    problems += [f"after the run: {p}" for p in tree_problems(repo, sha)]
    if problems:
        head = f"LOCAL GATE REFUSED {sha}: "
        if summary:
            head += f"{summary}; "
        return 1, head + "; ".join(problems[:20]) + f" (files: {out})"
    return 0, f"LOCAL GATE PASS {sha}: {summary} (files: {out})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", required=True, help="the commit to gate; HEAD must be it")
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--out", type=Path, default=None,
                    help="where the run's files go (outside the repository); default: "
                         "a new directory under the system temp directory")
    ap.add_argument("--template-dsn", default=None,
                    help="the Postgres server the suite creates and drops its database on, "
                         "handed to it as MESHY_TEMPLATE_DSN (default: MESHY_TEMPLATE_DSN "
                         "from the environment; neither: refused, there is no default)")
    ap.add_argument("--pg-bin", default=None,
                    help="directory with pg_dump/pg_restore of the server's major "
                         "(default: MESHY_PG_BIN from the environment)")
    ap.add_argument("--fast", action="store_true",
                    help="FAST mode for a client-only PR: part A runs tests/no_db.txt with no database, "
                         "part B the PR's own and naming test files with Postgres (needs --base)")
    ap.add_argument("--base", default=None,
                    help="with --fast: the main commit the PR is measured against")
    ap.add_argument("--require-conformance-bin", action="store_true",
                    help="REFUSE unless MESHY_BISCUIT_CONFORMANCE_BIN names an existing file: "
                         "the certification host runs the biscuit conformance tool")
    a = ap.parse_args(argv)
    if (a.template_dsn or "").strip():
        template_dsn, source = a.template_dsn.strip(), "--template-dsn"
    else:
        template_dsn = (os.environ.get("MESHY_TEMPLATE_DSN") or "").strip() or None
        source = "MESHY_TEMPLATE_DSN"
    if template_dsn:
        print(f"LOCAL GATE template DSN (from {source}): {shown_dsn(template_dsn)}",
              flush=True)
    tool = conformance_line(os.environ.get(CONFORMANCE_ENV))
    if tool:
        print(tool, flush=True)
    if a.require_conformance_bin:
        why = conformance_problem(os.environ.get(CONFORMANCE_ENV))
        if why:
            try:
                sha = _git(a.repo.resolve(), "rev-parse", "--verify", "--quiet", f"{a.commit}^{{commit}}")
            except subprocess.CalledProcessError:
                sha = a.commit
            print(f"LOCAL GATE REFUSED {sha}: {why}")
            return 1
    if a.fast:
        if not a.base:
            print(f"LOCAL GATE REFUSED (fast) {a.commit}: --fast needs --base")
            return 1
        code, line = fast_gate(a.repo, a.commit, a.base, a.out,
                               a.pg_bin or os.environ.get("MESHY_PG_BIN"), template_dsn)
    else:
        code, line = gate(a.repo, a.commit, a.out, a.pg_bin or os.environ.get("MESHY_PG_BIN"),
                          template_dsn)
    out_dir = re.search(r"\(files: (.+)\)$", line)
    if out_dir:
        try:
            Path(out_dir.group(1), "verdict.txt").write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
