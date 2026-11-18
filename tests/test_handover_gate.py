"""Make the pure handover fixtures and mutants visible to the tests/ gate.

The local repository gate collects tests/ only. These adapters execute each
stdlib fixture module serially and expose any nested failure, error or skip as
a gate failure. No live sender, route, scheduler or database is used here.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tools.postbox.handover.check_watch_mutants import CASES


class HandoverGateTests(unittest.TestCase):
    def check_module(self, name):
        suite = unittest.defaultTestLoader.loadTestsFromName(name)
        expected = suite.countTestCases()
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
        self.assertGreater(expected, 0, "handover fixture module collected no cases")
        self.assertEqual(result.testsRun, expected, output.getvalue())
        self.assertEqual(result.skipped, [], output.getvalue())
        self.assertTrue(result.wasSuccessful(), output.getvalue())

    def test_shared_handover_fixtures(self):
        self.check_module("tools.postbox.handover.test_handover")

    def test_watch_runner_fixtures(self):
        self.check_module("tools.postbox.handover.test_watch")

    def test_watch_mutation_controls(self):
        result = subprocess.run(
            [sys.executable, "-m", "tools.postbox.handover.check_watch_mutants"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, encoding="utf-8", timeout=180,
            creationflags=0x08000000 if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines() if line]
        self.assertEqual([row["mutant"] for row in rows], [case[0] for case in CASES])
        self.assertTrue(all(row["assertion_detected"] is True for row in rows), result.stdout)


if __name__ == "__main__":
    unittest.main()
