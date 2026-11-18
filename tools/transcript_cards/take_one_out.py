"""Red-prove attack_redaction.py: take each detector out of a scratch copy of the loader and run the attack
against the copy. The attack must end with its own verdict (exit 1) every time.

    python -B tools/transcript_cards/take_one_out.py tools/transcript_cards         tools/transcript_cards/attack_redaction.py <an empty work folder outside the repository>

Prints every removal the attack does not notice (exit 0) and every run that ends in a traceback (shown as exit
99: a crash is not a verdict, and python exits 1 for both). First a control with the loader unchanged, which
must be green. Then edits of other kinds: a detector widened until clean prose trips it, a seat rule moved into
the standing list, a detector added with no case of its own, a dead detector under a live one's name, and three
ways the loader's own detect() can stop applying its table. Exit 0 only when everything was noticed.
"""
import os
import shutil
import subprocess
import sys

loader_dir, attack, work = sys.argv[1:4]
src = open(os.path.join(loader_dir, "transcript_cards.py"), encoding="utf-8").read()
os.makedirs(work, exist_ok=True)
shutil.copy(attack, os.path.join(work, "attack_redaction.py"))
# The loader can import a module beside it (a dialect reader). The copy runs from its own folder, so every other
# .py of the loader's folder goes beside it unchanged; only transcript_cards.py and the attack are written here.
for name in sorted(os.listdir(loader_dir)):
    if name.endswith(".py") and name not in ("transcript_cards.py", "attack_redaction.py", "take_one_out.py"):
        shutil.copy(os.path.join(loader_dir, name), os.path.join(work, name))


def run(tail):
    open(os.path.join(work, "transcript_cards.py"), "w", encoding="utf-8", newline="\n").write(src + "\n" + tail + "\n")
    p = subprocess.run([sys.executable, "-B", os.path.join(work, "attack_redaction.py"), work], capture_output=True,
                       text=True, encoding="utf-8", stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = p.stdout + p.stderr
    return (99 if 'Traceback (most recent call last)' in out else p.returncode), out


sys.path.insert(0, loader_dir)
sys.dont_write_bytecode = True
import transcript_cards as tc  # noqa: E402  (names only)

standing = [n for n, _ in tc.DETECTORS]
seat = [(s, n) for s, rules in sorted(tc.SEAT_DETECTORS.items()) for n, _ in rules]

rc, out = run("# no-op control")
print("control, loader unchanged: exit", rc)
if rc != 0:
    print(out[-600:])
    raise SystemExit("the control is not green; nothing below means anything")

noticed = 0
for name in standing:
    rc, out = run(f"DETECTORS[:] = [d for d in DETECTORS if d[0] != {name!r}]\nassert len(DETECTORS) == {len(standing) - 1}")
    noticed += rc == 1
    if rc != 1:
        print("  NOT NOTICED" if rc == 0 else f"  exit {rc} (crash, not a verdict)", "standing", name, out[-200:] if rc not in (0, 1) else "")
for s, name in seat:
    rc, out = run(f"SEAT_DETECTORS[{s!r}] = [d for d in SEAT_DETECTORS[{s!r}] if d[0] != {name!r}]")
    noticed += rc == 1
    if rc != 1:
        print("  NOT NOTICED" if rc == 0 else f"  exit {rc} (crash, not a verdict)", "seat", s, name, out[-200:] if rc not in (0, 1) else "")
print(f"take one out: noticed {noticed} of {len(standing) + len(seat)} ({len(standing)} standing, {len(seat)} seat)")

others = [
    ("a detector widened until clean prose trips it",
     "DETECTORS[:] = [(n, re.compile(r'\\bPIN\\b') if n == 'otp_or_pin' else rx) for n, rx in DETECTORS]"),
    ("a seat rule moved into the standing list",
     "DETECTORS.extend(SEAT_DETECTORS['pxx'])"),
    ("a detector added with no case of its own",
     "DETECTORS.append(('added_without_a_case', re.compile(r'zzz-never-in-any-case-zzz')))"),
    ("a dead detector appended under a live detector's name",
     "DETECTORS.append((DETECTORS[0][0], re.compile(r'(?!)')))"),
    ("the loader's detect() searches only the seat rules",
     "def detect(text, disabled=(), seat=None):" + chr(10)
     + "    return [n for n, rx in SEAT_DETECTORS.get(seat, []) if n not in disabled and rx.search(text)]"),
    ("the loader's detect() takes no seat at all",
     "def detect(text, disabled=()):" + chr(10)
     + "    return [n for n, rx in DETECTORS if n not in disabled and rx.search(text)]"),
    ("the loader's detect() no longer applies seat rules",
     "def detect(text, disabled=(), seat=None):" + chr(10)
     + "    return [n for n, rx in DETECTORS if n not in disabled and rx.search(text)]"),
]
other_noticed = 0
for label, tail in others:
    rc, out = run(tail)
    other_noticed += rc == 1
    print(f"{label}: exit {rc}", "(noticed)" if rc == 1 else "(NOT NOTICED)" if rc == 0 else "(crash) " + out[-200:])
sys.exit(0 if noticed == len(standing) + len(seat) and other_noticed == len(others) else 1)
