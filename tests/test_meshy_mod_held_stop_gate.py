"""REFACTOR-QUEUE-2026-11 A4: the meshy mod's held_stop node suite (tools/postbox/mod/meshy-mod/tests/held_stop.test.cjs)
ran in no gate, so a change to hooks/held_stop.cjs or hooks/held_peek.py could land red. This runs it with node's own
runner and reads node's TAP footer. node is a gate-host prerequisite (tools/ci/local_gate.py's docstring): a missing
node FAILS here rather than skips, because an unlisted skip would hide the suite again. MESHY_TEST_PYTHON is this
interpreter, so the suite's held_peek.py cases run instead of skipping. Needs no database."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tools" / "postbox" / "mod" / "meshy-mod" / "tests" / "held_stop.test.cjs"


def test_the_meshy_mods_held_stop_suite_passes_with_nothing_skipped():
    node = shutil.which("node")
    assert node, "node is not on PATH: the gate host needs it to run the meshy mod's held_stop suite"
    assert SUITE.is_file(), SUITE
    proc = subprocess.run([node, "--test", "--test-reporter=tap", str(SUITE)], cwd=SUITE.parent,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                          env=dict(os.environ, MESHY_TEST_PYTHON=sys.executable))
    footer = {k: int(v) for k, v in re.findall(r"^# (tests|pass|fail|cancelled|skipped|todo) (\d+)\s*$",
                                               proc.stdout, re.M)}
    detail = proc.stdout[-4000:] + proc.stderr[-2000:]
    assert proc.returncode == 0, detail
    assert footer.get("tests", 0) > 0 and footer.get("pass") == footer["tests"], (footer, detail)
    assert (footer.get("fail"), footer.get("cancelled"), footer.get("skipped"), footer.get("todo")) == (0, 0, 0, 0), \
        (footer, detail)
