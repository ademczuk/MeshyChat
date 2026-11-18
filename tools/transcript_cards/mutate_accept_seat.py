"""Mutation check for tests/test_transcript_cards_accept_seat.py: each mutant of accept_seat.py must turn it red.

    python -B tools/transcript_cards/mutate_accept_seat.py     (exit 0 = every mutant was caught)

Each mutant is applied to a copy of this folder in a temp folder, and the checks run against the copy with pytest
(ACCEPT_SEAT_PY names it; no database, no conftest). A control with accept_seat.py unchanged runs first and must be
green. The committed files are never changed. The docstrings of the checks name these mutants.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_FILE = os.path.join(HERE, os.pardir, os.pardir, "tests", "test_transcript_cards_accept_seat.py")

MUTANTS = [
    ("print a card's text",
     '        if key in old:\n            row["old"] += 1\n',
     '        if key in old:\n            row["old"] += 1\n            print(old[key].get("text"))\n', 1),
    ("second_scan's own output passed through",
     '        if name == "second_scan.py":\n',
     '        if name == "second_scan.py":\n            sys.stdout.write(text)\n', 1),
    ("a failed loader's stderr passed through",
     '    return p.returncode, _error_name(p.returncode, p.stderr)\n',
     '    sys.stderr.write(p.stderr.decode("utf-8", "replace"))\n'
     '    return p.returncode, _error_name(p.returncode, p.stderr)\n', 1),
    ("a failed loader makes accept_seat exit non-zero",
     '        return 0\n    finally:', '        return 0 if result["compared"] else 1\n    finally:', 1),
    ("both loaders write into one store",
     'store = os.path.join(work, "store-" + side)', 'store = os.path.join(work, "store")', 1),
    ("the second_scan count is read from its cards-scanned line",
     'SECOND_COUNT = re.compile(r"distinct high-entropy mixed tokens not flagged: (\\d+)")',
     'SECOND_COUNT = re.compile(r"cards scanned: (\\d+)")', 1),
    ("added and removed swapped",
     '            row["removed"] += 1\n        elif key not in old:\n            row["added"] += 1\n',
     '            row["added"] += 1\n        elif key not in old:\n            row["removed"] += 1\n', 1),
    ("a card is keyed by its record and block even when it has an id",
     '    if isinstance(cid, str) and cid:\n', '    if False:\n', 1),
    ("a card without an id is keyed without its block index",
     'return kind, "source:" + str(src.get("record_uuid")), src.get("block_index")',
     'return kind, "source:" + str(src.get("record_uuid")), None', 1),
    ("git is run with the caller's GIT_* variables",
     ' if not k.startswith("GIT_")}', '}', 1),
    ("changed is decided by text_sha256",
     '    return src.get("source_sha256")', '    return card.get("text_sha256")', 1),
    ("loader_version counted as another field",
     '    return {k: v for k, v in card.items() if k != "loader_version"}', '    return dict(card)', 1),
    ("every delta is old minus new",
     '"delta": None if a is None or b is None else b - a', '"delta": None if a is None or b is None else a - b', 1),
    ("each loader reads the live folder",
     'code, error = run_loader(os.path.join(work, "loader-" + side), input_dir, codex, store, a)',
     'code, error = run_loader(os.path.join(work, "loader-" + side), os.path.abspath(source), codex, store, a)', 1),
    ("the copy is taken again for the new loader",
     '            store = os.path.join(work, "store-" + side)\n',
     '            store = os.path.join(work, "store-" + side)\n            if side == "new":\n'
     '                shutil.rmtree(input_dir)\n                frozen = freeze(source, input_dir, codex)\n', 1),
    ("--all-files not passed", '    if a.all_files:\n        args.append', '    if False:\n        args.append', 1),
    ("--marker not passed", '(("--marker", a.marker), ', '(("--marker", None), ', 1),
    ("only transcript_cards.py staged from a revision",
     'names = [n for n in listing.stdout.decode("utf-8", "replace").splitlines() if n.endswith(".py")]',
     'names = [n for n in listing.stdout.decode("utf-8", "replace").splitlines() if n.endswith("/" + LOADER)]', 1),
    ("a path's other modules not copied beside it",
     'if name.endswith(".py") and os.path.isfile(path) and os.path.abspath(path) != os.path.abspath(src):',
     'if False:', 1),
    ("a missing input exits 0", '        return 2\n', '        return 0\n', None),
    ("the scratch folder is kept without --keep",
     '        if not a.keep:\n            shutil.rmtree', '        if False:\n            shutil.rmtree', 1),
    ("the exception's name taken from the last stderr line shaped like one",
     '    starts = [i for i, line in enumerate(lines) if line.startswith("Traceback (most recent call last):")]\n',
     '    for line in reversed(lines):\n'
     '        m = re.match(r"^([A-Za-z_][\\w.]*(?:Error|Exception))\\b", line.strip())\n'
     '        if m:\n            return m.group(1)\n    starts = []\n', 1),
    ("a Codex sessions folder is copied top level only",
     '    walk = os.walk(source) if codex else [(source, [], os.listdir(source))]',
     '    walk = [(source, [], os.listdir(source))]', 1),
    # Added 10 Oct 2026, from Codex's review of f3f102f.
    ("a name not on the list of built-in exceptions is shown",
     'return m.group(1) if m and m.group(1) in KNOWN_EXCEPTIONS else UNNAMED_EXCEPTION',
     'return m.group(1) if m else UNNAMED_EXCEPTION', 1),
    ("a scan's output read as bytes and decoded, its CR kept",
     'env=_env(), text=True, encoding="utf-8", errors="replace")\n        text = p.stdout\n',
     'env=_env())\n        text = p.stdout.decode("utf-8", "replace")\n', 1),
    ("a report problem does not stop the comparison",
     'new_exit == 0 and not result["report_problems"]', 'new_exit == 0', 1),
    ("an absent counter passes",
     'for k in REPORT_COUNTS if _count(report.get(k)) is None]',
     'for k in REPORT_COUNTS if k in report and _count(report[k]) is None]', 1),
    ("a bool is taken for a count",
     'isinstance(v, int) and not isinstance(v, bool) and v >= 0', 'isinstance(v, int) and v >= 0', 1),
    ("a negative value is taken for a count",
     'isinstance(v, int) and not isinstance(v, bool) and v >= 0', 'isinstance(v, int) and not isinstance(v, bool)', 1),
    ("a counter map's values are not checked",
     '        elif any(_count(v) is None for v in table.values()):', '        elif False:', 1),
    ("a value that is not a count is copied into the output",
     'return {"old": a, "new": b, "delta"', 'return {"old": old, "new": new, "delta"', 1),
]


def run(old, new, count):
    """(exit code of the checks, their summary line, the failing test names) for accept_seat.py with old -> new."""
    work = tempfile.mkdtemp(prefix="accept-seat-mut-")
    try:
        folder = os.path.join(work, "transcript_cards")
        shutil.copytree(HERE, folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        path = os.path.join(folder, "accept_seat.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if old is not None:
            n = src.count(old)
            if n == 0 or (count is not None and n != count):
                return None, "the target text occurs %d times, mutant not applied" % n, []
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(src.replace(old, new))
        # Every temp folder of the run (pytest's, and the scratch folder a mutant may fail to delete) is made inside
        # this one, so nothing outlives the mutant.
        env = dict(os.environ, ACCEPT_SEAT_PY=path, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   TMPDIR=work, TEMP=work, TMP=work)
        cmd = [sys.executable, "-B", "-m", "pytest", os.path.abspath(TEST_FILE), "-q", "-p", "no:cacheprovider",
               "--noconftest"]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=work,
                           stdin=subprocess.DEVNULL, timeout=900)
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        failed = [ln.split("::")[1].split(" ")[0] for ln in lines if ln.startswith("FAILED ") and "::" in ln]
        return p.returncode, (lines[-1] if lines else ""), failed
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    rc, last, _ = run(None, None, None)
    print("control, accept_seat.py unchanged: exit %s | %s" % (rc, last))
    if rc != 0:
        print("the control is not green; nothing below means anything")
        return 1
    survived = []
    for label, old, new, count in MUTANTS:
        rc, last, failed = run(old, new, count)
        if rc is None:
            print("BLIND    %s: %s" % (label, last))
            survived.append(label)
        elif rc != 0:
            print("CAUGHT   %s: %s (%s)" % (label, last, ", ".join(failed)))
        else:
            print("SURVIVED %s: %s" % (label, last))
            survived.append(label)
    print("\n%d mutants, %d survived" % (len(MUTANTS), len(survived)))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
