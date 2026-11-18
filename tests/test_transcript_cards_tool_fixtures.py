"""The transcript-cards tool's own unittest modules run in the gate (REFACTOR-QUEUE-2026-11 A3).

WHY. `tools/transcript_cards/test_*.py` (six modules, about 170 tests) ran in no gate: the gate collects `tests/`,
and `FAST_CLIENT_PREFIXES` makes a change to `capture_queue`, `frozen_outbox`, `incremental_store`, `native_window`
or `authenticated_readback` fast-eligible while none of their tests ran. The tool ships in v0.1.0, so its tests have
to run before a release.

HOW. Each module runs as `python -m unittest -v <module>` in a CHILD process with `cwd=tools/transcript_cards`, the
way the tool's README runs them. In-process loading is not safe: one module calls `settings.configure()`, one patches
`sys.modules` at class scope, two put the repository root on `sys.path`, and the bare `frozen_outbox` import would
shadow `tools.transcript_cards.frozen_outbox`. Each run must exit 0, report "Ran N tests" with N above 0, and skip only
for a reason listed below: the skips depend on the host (symlinks, case and Unicode behavior of the filesystem,
Windows ACLs) and on two opt-in switches, so a list of exact skipped tests would be wrong on some host, and a reason
not listed here is a new skip that has to be read first.

Needs no database.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "transcript_cards"
MODULES = sorted(p.stem for p in TOOL.glob("test_*.py"))

#: Every skip reason a module may report, and why it may skip.
ALLOWED_SKIPS = {
    "source filesystem has distinct case-sensitive names",          # case-sensitive host filesystem
    "source filesystem has distinct Unicode normalization names",   # no normalization on this filesystem
    "explicit pinned validator snapshot required",                  # opt-in: a pinned validator snapshot
    "Windows ACL proof",                                            # Windows only
    "host does not permit synthetic symlink creation",              # no symlink privilege
    "host does not permit creating synthetic symlinks",             # no symlink privilege
    "opt-in 778 MiB synthetic read-bound acceptance",               # opt-in: MESHY_SYNTHETIC_LARGE_WINDOW=1
}


def test_the_tool_still_has_its_six_test_modules():
    """Control for the parametrization: an empty glob would make every case below vanish, not fail."""
    assert len(MODULES) >= 6, MODULES


@pytest.mark.parametrize("module", MODULES)
def test_a_transcript_cards_module_passes_with_only_known_skips(module):
    proc = subprocess.run([sys.executable, "-m", "unittest", "-v", module], cwd=TOOL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=600)
    out = proc.stdout + proc.stderr
    tail = out[-4000:]
    assert proc.returncode == 0, tail
    ran = re.search(r"^Ran (\d+) tests? in ", out, re.M)
    assert ran and int(ran.group(1)) > 0, tail
    # unittest prints a reason with repr(), so one holding an apostrophe comes out in double quotes (pxx's review of
    # #646): match both quote forms, and require as many reasons read as unittest's own skip count, so a skip line in
    # a shape this pattern does not know refuses instead of passing.
    records = [m.group(2) for m in re.finditer(r"\.\.\. skipped (['\"])(.*)\1$", out, re.M)]
    counted = re.search(r"^OK(?: \((.*)\))?\s*$", out, re.M)
    skipped = int(m.group(1)) if counted and counted.group(1) and (m := re.search(r"skipped=(\d+)", counted.group(1))) else 0
    assert counted, tail
    assert len(records) == skipped, f"{module}: unittest counted {skipped} skips, {len(records)} skip reasons were read"
    unknown = sorted(set(records) - set(ALLOWED_SKIPS))
    assert not unknown, f"{module} skipped for a reason not on the list: {unknown}"
