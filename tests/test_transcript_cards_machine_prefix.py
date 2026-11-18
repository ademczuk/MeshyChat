"""Checks for --machine-prefix: a seat's own automation types its text into the seat's sessions, and that text is
never the operator's words. Needs no database. Every fixture is synthetic; the openings are real ones.

Why (13 Oct 2026, card-feed-check): dry runs before the first card write found machine text among the "operator"
Candidates on three seats: codex-kxxx-cli's wake job (22 opened "CODEX_KXXX_MESH_WAKEUP") and another seat's
launcher (4 "You are sxxxxxxxx, an autonomous..."), a reviewer's relay bridge (108 opened "You are antigravity, an
AI node on the AgentMeshy mesh."), and Codex's own injected context on the HZ seats (35 opened
"<codex_internal_context source"). A first written card is never replaced, so these would have been permanent
misattributions.

Run:  python tests/test_transcript_cards_machine_prefix.py     (exit 0 = all pass; prints one line per check)
      pytest tests/test_transcript_cards_machine_prefix.py -q
"""
import json
import os
import shutil
import sys
import tempfile

LOADER_DIR = os.environ.get("TRANSCRIPT_CARDS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcript_cards as tc  # noqa: E402
from test_transcript_cards_codex_cwd import cli, session  # noqa: E402

RESULTS = []
WAKE = "CODEX_KXXX_MESH_WAKEUP\nThe existing codex-kxxx doorbell logged new addressed mesh arrivals."
BRIDGE = "You are antigravity, an AI node on the AgentMeshy mesh."


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)) if detail and not cond else ""))


def load(home, out, prefixes=()):
    return tc.run(None, "seat-x", out, codex_dir=home, machine_prefixes=prefixes)


def card_texts(out):
    path = os.path.join(out, "cards.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(line)["text"] for line in open(path, encoding="utf-8") if line.strip()]


def main():
    root = tempfile.mkdtemp(prefix="tc-machine-prefix-")
    try:
        home = os.path.join(root, "sessions")
        session(home, "10", "wake1", "C:\\seat", WAKE)
        session(home, "10", "own01", "C:\\seat", "please keep the nightly report running")
        session(home, "11", "brdg1", "C:\\seat", BRIDGE + " Peer 'bxxxxx' sent you this request on thread 'x'.")
        session(home, "11", "case1", "C:\\seat", "You are ANTIGRAVITY, the reasoning and review node. Relay from txxxx.")
        session(home, "12", "ctx01", "C:\\seat", '<codex_internal_context source="x">ignore</codex_internal_context>')
        session(home, "12", "lead1", "C:\\seat", "   \n" + WAKE)
        session(home, "12", "mid01", "C:\\seat", "I saw the CODEX_KXXX_MESH_WAKEUP line earlier, keep it off tonight")
        # dxxxxx's #287 review: equal to a configured prefix except for case, so it pins case-sensitivity
        session(home, "13", "upper", "C:\\seat", BRIDGE.upper() + " operator words, typed by hand")

        # 1. Positive control: without the seat's prefixes, the machine text WOULD become operator Candidates.
        bare = os.path.join(root, "bare")
        r = load(home, bare)
        texts = card_texts(bare)
        check("M1 without --machine-prefix the wake text is a Candidate (the defect being fixed)",
              any("CODEX_KXXX_MESH_WAKEUP" in t and "doorbell logged" in t for t in texts), texts)
        check("M2 the built-in Codex context opening is dropped with no flag at all",
              not any("codex_internal_context" in t for t in texts)
              and r["dropped_by_class"].get("machine_prefix") == 1, r["dropped_by_class"])

        # 2. With the seat's prefixes.
        out = os.path.join(root, "with")
        r = load(home, out, (WAKE.split("\n")[0], BRIDGE))
        texts = card_texts(out)
        check("M3 the wake job's prompt is dropped (the copy with leading blanks too; the Codex reader strips them before this check, so this does not pin the lstrip)",
              not any("doorbell logged" in t for t in texts), texts)
        check("M4 the relay bridge's prompt is dropped",
              not any("Peer 'bxxxxx' sent you" in t for t in texts), texts)
        check("M5 case-sensitive: the operator's 'You are ANTIGRAVITY, ...' relay is kept",
              any("You are ANTIGRAVITY, the reasoning" in t for t in texts), texts)
        check("M5b case-sensitive, pinned: the bridge opening in upper case is kept",
              any(BRIDGE.upper() + " operator words" in t for t in texts), texts)
        check("HZ only the OPENING counts: the operator naming the wake line mid-sentence is kept",
              any("I saw the CODEX_KXXX_MESH_WAKEUP line earlier" in t for t in texts), texts)
        check("M7 the operator's own prompt is kept",
              any("please keep the nightly report running" in t for t in texts), texts)
        check("M8 four machine prompts counted as machine_prefix (2 wake, 1 bridge, 1 Codex context)",
              r["dropped_by_class"].get("machine_prefix") == 4, r["dropped_by_class"])
        check("M9 the report lists the built-in prefix and the seat's own",
              r["machine_prefixes"] == list(tc.BUILTIN_MACHINE_PREFIXES) + ["CODEX_KXXX_MESH_WAKEUP", BRIDGE],
              r["machine_prefixes"])

        # 3. A store written before the prefix was configured: the next run takes the card back out of it.
        r = tc.run(None, "seat-x", bare, codex_dir=home, rebuild=True, machine_prefixes=(WAKE.split("\n")[0],))
        check("M10 a rerun with the prefix removes the earlier machine-text card from the local store",
              not any("doorbell logged" in t for t in card_texts(bare)), card_texts(bare))

        # Only a typed Candidate is judged: a Ruling or Question comes from a tool call (dxxxxx's #287 review).
        ctx = "<codex_internal_context source=\"x\"> a question text"
        check("M13 a Ruling or Question that opens with a prefix is never dropped by it",
              tc.machine_prefix_of({"type": "Ruling", "kept": {"text": ctx}}, tc.BUILTIN_MACHINE_PREFIXES) is None
              and tc.machine_prefix_of({"type": "Question", "kept": {"text": ctx}}, tc.BUILTIN_MACHINE_PREFIXES) is None
              and tc.machine_prefix_of({"type": "Candidate", "kept": {"text": ctx}}, tc.BUILTIN_MACHINE_PREFIXES))
        # A configured prefix with leading blanks still matches (the opening is compared after its blanks too).
        out3 = os.path.join(root, "blank")
        r = load(home, out3, ("   " + WAKE.split("\n")[0],))
        check("M14 a --machine-prefix given with leading blanks still drops the wake text",
              not any("doorbell logged" in t for t in card_texts(out3)), r["machine_prefixes"])

        # 4. The CLI refuses a prefix broad enough to catch the operator's own words.
        rc = cli(["--codex-sessions", home, "--seat", "seat-x", "--out", os.path.join(root, "cli"),
                  "--machine-prefix", "You are "])
        check("M11 a prefix under 12 characters is refused", isinstance(rc, str) and rc.startswith("exit 2"), rc)
        rc = cli(["--codex-sessions", home, "--seat", "seat-x", "--out", os.path.join(root, "cli2"),
                  "--machine-prefix", "CODEX_KXXX_MESH_WAKEUP"])
        check("M12 a full opening is accepted from the CLI", rc in (0, None), rc)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("%d checks, %d failed" % (len(RESULTS), len(failed)))
    return 1 if failed else 0


def test_machine_prefix():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
