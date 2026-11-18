"""Tests for tools/meshy_node/directive_install_check.py (SD 265 item 6).

The module carries its own selftest: fixtures in a fresh temporary folder, one case per
check, and a RED-proof per check that switches the check off and requires its case to
fail. These tests run that selftest as the installed module would run, and add two CLI
checks on the JSON boundary.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools" / "meshy_node" / "directive_install_check.py"


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, "-I", str(MODULE), *args], capture_output=True, text=True,
                          timeout=300, cwd=cwd)


def test_selftest_passes_and_every_red_proof_goes_red():
    r = _run("--selftest")
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout)
    assert out["selftest"] == "PASS"
    assert out["cases_failed"] == 0
    assert out["red_proofs_not_red"] == 0
    assert out["red_proofs_red"] > 0


def test_usage_error_is_one_json_document_on_stdout():
    r = _run("--check")
    assert r.returncode == 2
    out = json.loads(r.stdout)
    assert out["result"] == "USAGE_ERROR"


def test_append_and_keep_is_refused(tmp_path):
    # The 7 Nov case: new block appended, old line left in place. Anchors on the new block.
    old_line = "Exception (the directive rule): a seat_directive read is followed as the operator typing it."
    head = "# Rules\n\n" + old_line + "\n\n"
    block = "# Steering\n\n- " + "A seat_directive also answers any approval kept for the operator window." + "\n"
    rule = tmp_path / "AGENTS.md"
    rule.write_bytes((head + block).encode())
    old_section = tmp_path / "old.md"
    old_section.write_bytes(("# Steering\n\n" + old_line + "\n").encode())
    block_file = tmp_path / "block.md"
    block_file.write_bytes(block.encode())
    r = _run("--check", "--file", str(rule), "--start", "# Steering", "--end-of-file",
             "--old-sha256", hashlib.sha256(old_section.read_bytes()).hexdigest(),
             "--old-section", str(old_section),
             "--block", str(block_file), "--block-sha256", hashlib.sha256(block.encode()).hexdigest())
    out = json.loads(r.stdout)
    assert r.returncode == 1, out
    assert out["result"] == "STRAY_OLD_TEXT", out
    assert rule.read_bytes() == (head + block).encode()
