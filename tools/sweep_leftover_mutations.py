"""Find source a reviewer mutated and did not restore.

WHY THIS IS NOT OPTIONAL: the adversarial reviewers mutate source in place, re-run the
suite, and restore it. One was caught mid-campaign still mutated —
`apps/billing/owner_auth.py:217` carried `if False: raise errors.auth_invalid(...)`, i.e.
the unknown-owner check disabled.

**A mutation that SURVIVES is by definition one no test catches.** So a green suite is not
evidence the tree is clean; only reading it is. This narrows the reading to the lines worth
reading.

Run: python tools/sweep_leftover_mutations.py            (exit 1 if anything is found)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN = ["apps", "meshychat", "tools"]

# Shapes a mutation campaign leaves behind. Each is paired with why it matters, because a
# hit list without reasons gets skimmed.
PATTERNS = [
    (re.compile(r"^\s*if False:"), "a branch disabled outright — the usual way a check is removed"),
    (re.compile(r"^\s*if True:"), "a branch forced on — the usual way a guard is bypassed"),
    (re.compile(r"^\s*(#\s*)?return True\s*#"), "a predicate short-circuited to allow"),
    (re.compile(r"^\s*pass\s*#\s*(was|removed|mutat)", re.I), "a body replaced by pass"),
    (re.compile(r"^\s*#\s*(raise|assert)\b"), "a raise or assert commented out"),
    # A COMMENT MARKER, not the word. The first version matched `\bMUTATION\b` case
    # INSENSITIVELY, so it fired on two docstrings that merely DISCUSS mutation testing -
    # and a sweep that reports a hit on every run is a sweep everyone learns to skip past.
    # That is the same failure this file is written to prevent, one level up.
    (re.compile(r"#\s*(MUTANT|MUTATION)\b"), "an explicit mutation marker"),
]

# Security-relevant files get flagged even on a weak match: these are where a surviving
# mutation actually costs something.
SENSITIVE = re.compile(r"(auth|rls|crypto|primitives|verify|sign|permission|grant|tenant|"
                       r"policy|guard|owner)", re.I)


#: SELF-VALIDATION. A scanner that has been narrowed is a scanner that may now catch
#: nothing, and "clean" and "blind" print identically. Each of these must be caught, or the
#: sweep refuses to report clean - so the narrowing above cannot silently become a mute.
PLANTED = [
    "    if False:  # disabled",
    "    if True:",
    "    return True  # always allow",
    "    pass  # was: raise PermissionError",
    "    # raise errors.auth_invalid('unknown owner')",
    "    x = 1  # MUTANT",
]
#: ...and these must NOT be caught, because they are the false positives that muted it.
PLANTED_CLEAN = [
    "    A mutation replacing the check with `return` left all 76 tests green.",
    "    mutation testing is the review method used here",
    "    if flag:",
]


def self_test() -> list[str]:
    """Returns the failures. Empty means the scanner still discriminates."""
    failures = []
    for line in PLANTED:
        if not any(rx.search(line) for rx, _ in PATTERNS):
            failures.append(f"MISSED a planted mutation: {line.strip()!r}")
    for line in PLANTED_CLEAN:
        if any(rx.search(line) for rx, _ in PATTERNS):
            failures.append(f"FALSE POSITIVE on: {line.strip()!r}")
    return failures


def main() -> int:
    broken = self_test()
    if broken:
        print("sweep: THE SCANNER ITSELF IS BROKEN - refusing to report on the tree.")
        for line in broken:
            print("  " + line)
        return 2

    hits = []
    for top in SCAN:
        for path in (REPO / top).rglob("*.py"):
            if "__pycache__" in str(path) or path.name.startswith("sweep_"):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
            except OSError:
                continue
            for n, line in enumerate(lines, 1):
                for rx, why in PATTERNS:
                    if rx.search(line):
                        hits.append((path.relative_to(REPO), n, line.strip()[:90], why,
                                     bool(SENSITIVE.search(str(path)))))
                        break

    if not hits:
        print("sweep: clean — no mutation shapes found in", ", ".join(SCAN))
        return 0

    hits.sort(key=lambda h: (not h[4], str(h[0])))
    print(f"sweep: {len(hits)} suspect line(s). Read every one; a surviving mutation is "
          "one no test catches.\n")
    for rel, n, text, why, sensitive in hits:
        mark = "SECURITY-RELEVANT " if sensitive else ""
        print(f"  {mark}{rel}:{n}\n      {text}\n      ^ {why}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
