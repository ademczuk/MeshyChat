"""Time every detector on long adversarial texts (the shapes that made five of them super-linear).
Usage: python detector_timing.py [chars] [--max-ms N] [--only NAME,...]
Prints each detector-by-shape time as it finishes; exits 1 when any one takes N ms or more."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcript_cards as tc  # noqa: E402

SHAPES = {
    "aB3_ repeated": "aB3_",
    "A1b2 repeated": "A1b2",
    "a repeated": "a",
    "key_ repeated": "key_",
    "token= repeated": "token=",
    "password_ repeated": "password_",
    "pw= repeated": "pw=",
    "a: repeated": "a:",
    "http:// repeated": "http://",
    "1 repeated": "1",
    "a- repeated": "a-",
    "A repeated": "A",
    # added with the uncapped rules (a reviewer 75e4b6): the shapes each new walk must stop on
    "hooks. repeated": "hooks.",
    "https:// then hooks. repeated": ("https://", "hooks."),
    "token=a: repeated": "token=a:",
    "_key= repeated": "_key=",
    "PAT_ repeated": "PAT_",
    "a_key repeated": "a_key",
}


def text_of(unit, n):
    prefix = ""
    if isinstance(unit, tuple):
        prefix, unit = unit
    return (prefix + unit * (n // len(unit) + 1))[:n]


def run(n, only=None, limit=None):
    worst = (0.0, "", "")
    for label, unit in SHAPES.items():
        text = text_of(unit, n)
        rules = list(tc.DETECTORS) + [(f"{seat}:{n}", r) for seat, rs in tc.SEAT_DETECTORS.items() for n, r in rs]
        for name, rx in rules:
            if only and name not in only:
                continue
            t = time.perf_counter()
            rx.search(text)
            ms = 1000 * (time.perf_counter() - t)
            if ms >= 20:
                print(f"{ms:9.1f} ms  {name:24s} {label}", flush=True)
            worst = max(worst, (ms, name, label))
    print(f"worst {worst[0]:.1f} ms ({worst[1]}, {worst[2]}) at {n} characters", flush=True)
    return worst[0]


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--") and not (sys.argv.index(a) > 0 and sys.argv[sys.argv.index(a) - 1] in ("--max-ms", "--only"))]
    n = int(args[0]) if args else 65536
    limit = float(sys.argv[sys.argv.index("--max-ms") + 1]) if "--max-ms" in sys.argv else None
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
    worst = run(n, only)
    sys.exit(1 if limit is not None and worst >= limit else 0)
