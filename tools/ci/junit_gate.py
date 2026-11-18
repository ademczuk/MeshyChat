"""Read pytest's JUnit report and refuse anything that is not a full, green run (E04, #904).

    python tools/ci/junit_gate.py --junit junit.xml --collect-log collect.txt \
        --allow-skip "live LAN models" [--allow-skip REGEX ...]

INCIDENT #904: audits reported green while the step read "skipped", and an attestation wrote
green over 107 tests that never ran. pytest's exit code alone says "no test failed"; it does
not say how many ran. This gate says it, from the report, and refuses (exit 1) when:

  * there is no report, or it does not parse;
  * the report holds no tests;
  * any test failed or errored;
  * a test was skipped for a reason no `--allow-skip` pattern matches (re.search on the skip
    message). A skip that is not on the list is a test that did not run and nobody decided
    it may not;
  * the report holds fewer tests than `pytest --collect-only -q` collected (its last line,
    "N tests collected"): a run that stopped early, or a module that vanished between the two.

It prints one JSON object on its last line either way. Standard library only, so it runs
before or without the project's dependencies.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _allowed(allow, where: str, msg: str) -> bool:
    """An allow entry is a REGEX on the skip message (any test), or a pair (WHERE, REGEX):
    WHERE must match the whole "classname::name" of the test that skipped (re.fullmatch)
    and REGEX its message, so a reason can be allowed to the one test that owns it."""
    for entry in allow:
        if isinstance(entry, str):
            if re.search(entry, msg):
                return True
        else:
            at, pattern = entry
            if re.fullmatch(at, where) and re.search(pattern, msg):
                return True
    return False


def evaluate(junit: Path, collected: int | None, allow: list) -> dict:
    problems: list[str] = []
    if not junit.is_file():
        return {"ok": False, "problems": [f"no JUnit report at {junit}"]}
    try:
        root = ET.parse(junit).getroot()
    except ET.ParseError as exc:
        return {"ok": False, "problems": [f"the JUnit report does not parse: {exc}"]}
    cases = list(root.iter("testcase"))
    counts = {"tests": len(cases), "passed": 0, "failure": 0, "error": 0, "skipped": 0}
    unexpected_skips: list[str] = []
    for case in cases:
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        kinds = [child.tag for child in case if child.tag in ("failure", "error", "skipped")]
        if "failure" in kinds:
            counts["failure"] += 1
            problems.append(f"failure: {name}")
        if "error" in kinds:
            counts["error"] += 1
            problems.append(f"error: {name}")
        if "skipped" in kinds:
            counts["skipped"] += 1
            msg = case.find("skipped").get("message", "") or ""
            if not _allowed(allow, name, msg):
                unexpected_skips.append(f"{name}: {msg!r}")
        if not kinds:
            counts["passed"] += 1
    for s in unexpected_skips:
        problems.append(f"skip not on the allow-list: {s}")
    if not cases:
        problems.append("no tests in the report")
    if collected is not None and len(cases) < collected:
        problems.append(f"the report holds {len(cases)} tests, {collected} were collected")
    return {"ok": not problems, "counts": counts, "collected": collected,
            "problems": problems}


def collected_from(log: Path) -> int:
    text = log.read_text(encoding="utf-8", errors="replace")
    found = re.findall(r"^(\d+)(?:/\d+)? tests? collected", text, re.M)
    if not found:
        raise SystemExit(f"junit_gate: no 'N tests collected' line in {log}")
    return int(found[-1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--junit", required=True, type=Path)
    ap.add_argument("--collect-log", type=Path, default=None,
                    help="output of `pytest --collect-only -q`")
    ap.add_argument("--allow-skip", action="append", default=[], metavar="REGEX")
    a = ap.parse_args(argv)
    collected = collected_from(a.collect_log) if a.collect_log else None
    result = evaluate(a.junit, collected, a.allow_skip)
    for p in result["problems"][:50]:
        print(f"REFUSED: {p}", file=sys.stderr)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
