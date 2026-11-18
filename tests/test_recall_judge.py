"""The recall judge, tools/transcript_cards/recall/judge.py, and its mutants. Needs no database.

Run:  pytest tests/test_recall_judge.py -q
      python tools/transcript_cards/recall/judge.py --selftest      (one line per check)
      python tools/transcript_cards/recall/mutate_judge.py          (one line per mutant)

The judge scores a retrieval run against labelled queries. Its selftest shows every check passing on a good run and
failing on the one defect it exists for. mutate_judge.py breaks the judge one rule at a time and requires the
selftest to go red on each. A judge that cannot fail would report a broken search as a passing one, so the suite
runs both. Every label here is synthetic; real labels stay outside the repository (recall/README.md).
"""
import os
import subprocess
import sys
from pathlib import Path

RECALL = Path(__file__).resolve().parent.parent / "tools" / "transcript_cards" / "recall"
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
#: mutate_judge.py held 59 mutants when this was written. Every one of them must report a verdict (its own count,
#: printed last), and the floor catches a harness that lost most of its table.
MIN_MUTANTS = 59


def _run(script, *args):
    return subprocess.run([sys.executable, str(RECALL / script), *args], capture_output=True, text=True,
                          encoding="utf-8", env=ENV, timeout=600)


def test_the_judge_selftest_passes():
    r = _run("judge.py", "--selftest")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert r.stdout.rstrip().endswith("SELFTEST passed"), r.stdout[-3000:]
    assert not [line for line in r.stdout.splitlines() if line.startswith("FAIL")], r.stdout[-3000:]


def test_every_mutant_of_the_judge_fails_its_selftest():
    r = _run("mutate_judge.py")
    lines = r.stdout.splitlines()
    declared = [int(line.split()[1]) for line in lines if line.startswith("mutants ")]
    verdicts = [line for line in lines if not line.startswith(("original selftest", "mutants "))]
    killed = [line for line in verdicts if ": KILLED |" in line]
    assert declared and len(verdicts) == declared[0] >= MIN_MUTANTS, (
        f"{len(verdicts)} verdicts for {declared} mutants:\n{r.stdout[-3000:]}{r.stderr[-2000:]}")
    assert killed == verdicts, "\n".join(line for line in verdicts if line not in killed)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
