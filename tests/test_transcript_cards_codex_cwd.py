"""Checks for --codex-cwd: a Codex home is not a seat, so a load for one seat names the folders its sessions ran in.
Needs no database. Every fixture is synthetic.

Why (kxxx, HQD, 12 Oct 2026): one Codex home serves codex-kxxx-cli AND the Codex runs kxxx's seat starts, so
"every interactive session under this home is the seat's" attributes another seat's decisions to it. A session is
mapped by its own recorded working folder (session_meta.cwd); an unmapped one is skipped and listed with its cwd,
never guessed. Without --codex-cwd the old behavior holds (accept_seat.py runs an old and a new loader with the
same arguments, so the flag must stay optional).

Run:  python tests/test_transcript_cards_codex_cwd.py     (exit 0 = all pass; prints one line per check)
      pytest tests/test_transcript_cards_codex_cwd.py -q
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

LOADER_DIR = os.environ.get("TRANSCRIPT_CARDS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)) if detail and not cond else ""))


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def session(home, day, sid, cwd, text, source="cli"):
    """One interactive rollout file in a dated subfolder, as Codex writes them."""
    folder = os.path.join(home, "2026", "10", day)
    os.makedirs(folder, exist_ok=True)
    meta = {"timestamp": "2026-10-%sT05:00:00.000Z" % day, "type": "session_meta",
            "payload": {"id": sid, "source": source, "originator": "codex-tui", "cli_version": "0.161.0"}}
    if cwd is not None:
        meta["payload"]["cwd"] = cwd
    item = {"timestamp": "2026-10-%sT05:01:00.000Z" % day, "type": "response_item",
            "payload": {"type": "message", "role": "user", "id": "msg_" + sid,
                        "content": [{"type": "input_text", "text": text}]}}
    event = {"timestamp": "2026-10-%sT05:01:00.000Z" % day, "type": "event_msg",
             "payload": {"type": "user_message", "message": text}}
    path = os.path.join(folder, "rollout-2026-10-%sT05-00-00-%s.jsonl" % (day, sid))
    write_jsonl(path, [meta, item, event])
    return os.path.basename(path)


def cli(args):
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return tc.main(args)
    except SystemExit as stop:
        return "exit %s" % stop.code


def main():
    root = tempfile.mkdtemp(prefix="tc-codex-cwd-")
    try:
        home = os.path.join(root, "sessions")
        mine = session(home, "10", "aaa1", "C:\\Projects\\Seat-A\\work", "please keep the nightly report")
        mine_root = session(home, "10", "aaa2", "c:/projects/seat-a", "use the staging bucket for this")
        other = session(home, "11", "bbb1", "C:/Projects/Seat-B", "drop the old cache table now")
        sibling = session(home, "11", "bbb2", "C:/Projects/Seat-A-old", "a session in a folder that only shares a prefix")
        no_cwd = session(home, "11", "ccc1", None, "a session that recorded no folder")
        session(home, "12", "eee1", "C:/Projects/Seat-A", "an exec run", source="exec")

        picked, seen = tc.select_codex_files(home)
        check("C1 without cwds every interactive session is picked, as before", seen == 6 and len(picked) == 5,
              (seen, len(picked)))

        unmapped = []
        picked, seen = tc.select_codex_files(home, cwds=["C:/Projects/Seat-A"], unmapped=unmapped)
        names = sorted(os.path.basename(p) for p in picked)
        check("C2 a session is the seat's when its recorded cwd is the folder or under it, case-blind and whatever "
              "the separator", names == sorted([mine, mine_root]), names)
        check("C3 a folder that only shares a name prefix is not under it",
              sibling in {u["file"] for u in unmapped}, unmapped)
        check("C4 every other interactive session is listed as unmapped with its cwd; one with no cwd is unmapped "
              "with cwd None; an exec run is not listed at all",
              sorted(u["file"] for u in unmapped) == sorted([other, sibling, no_cwd])
              and {u["file"]: u["cwd"] for u in unmapped}[no_cwd] is None, unmapped)

        out = os.path.join(root, "out-a")
        report = tc.run(None, "codex-seat-a", out, codex_dir=home, codex_cwds=["C:/Projects/Seat-A"])
        texts = [c.get("text", "") for c in tc._load_jsonl(os.path.join(out, "cards.jsonl"))]
        check("C5 the run reads only the mapped sessions, records the folders, and lists the rest",
              report["files"] == 2 and report["codex_cwds"] == ["C:/Projects/Seat-A"]
              and len(report["codex_unmapped"]) == 3
              and any("nightly report" in t for t in texts) and any("staging bucket" in t for t in texts)
              and not any("old cache table" in t for t in texts), (report["files"], report["codex_unmapped"], texts))
        check("C6 without codex_cwds the report says so (None) and lists nothing unmapped",
              tc.run(None, "codex-seat-a", os.path.join(root, "out-all"), codex_dir=home)["codex_cwds"] is None)

        check("C7 the command takes --codex-cwd with --codex-sessions",
              cli(["--codex-sessions", home, "--codex-cwd", "C:/Projects/Seat-A", "--seat", "codex-seat-a",
                   "--out", os.path.join(root, "out-cli")]) == 0)
        check("C8 --codex-cwd without --codex-sessions is refused",
              cli(["--project-dir", root, "--codex-cwd", "C:/x", "--seat", "s", "--out", os.path.join(root, "o")])
              == "exit 2")
        check("C9 an empty --codex-cwd is refused",
              cli(["--codex-sessions", home, "--codex-cwd", " ", "--seat", "s", "--out", os.path.join(root, "o2")])
              == "exit 2")
        check("C11 case is folded for a Windows path only; on POSIX two spellings stay two folders",
              tc.cwd_maps("C:\\Projects\\Seat-A\\x", ["c:/projects/seat-a"])
              and not tc.cwd_maps("/home/a/Proj/x", ["/home/a/proj"])
              and tc.cwd_maps("/home/a/proj/x", ["/home/a/proj/"]))
        check("C12 a relative --codex-cwd is refused (a recorded cwd is always absolute, so it could never match)",
              cli(["--codex-sessions", home, "--codex-cwd", "proj", "--seat", "s", "--out", os.path.join(root, "o4")])
              == "exit 2"
              and cli(["--codex-sessions", home, "--codex-cwd", ".", "--seat", "s", "--out", os.path.join(root, "o5")])
              == "exit 2"
              and cli(["--codex-sessions", home, "--codex-cwd", "/home/a/proj", "--seat", "s",
                       "--out", os.path.join(root, "o6")]) == 3)
        check("C10 when no interactive session ran in a named folder the command exits 3, not 0",
              cli(["--codex-sessions", home, "--codex-cwd", "D:/nowhere", "--seat", "s",
                   "--out", os.path.join(root, "o3")]) == 3)

        narrow_out = os.path.join(root, "out-narrow")
        r_ab = tc.run(None, "codex-seat-a", narrow_out, codex_dir=home,
                      codex_cwds=["C:/Projects/Seat-A", "C:/Projects/Seat-B"])
        r_a = tc.run(None, "codex-seat-a", narrow_out, codex_dir=home,
                     codex_cwds=["C:/Projects/Seat-A"])
        cards_a = [c.get("text", "") for c in tc._load_jsonl(os.path.join(narrow_out, "cards.jsonl"))]
        r_a_repeat = tc.run(None, "codex-seat-a", narrow_out, codex_dir=home,
                            codex_cwds=["c:\\projects\\seat-a\\"])
        r_widen = tc.run(None, "codex-seat-a", narrow_out, codex_dir=home,
                         codex_cwds=["C:/Projects/Seat-A", "C:/Projects/Seat-B"])
        check("C13 narrowing --codex-cwd rebuilds the store so cards from removed folders do not linger; "
              "repeating or widening stays incremental",
              r_ab["cards_total"] == 3 and r_a["cards_total"] == 2 and r_a["new_cards"] == 2
              and not any("old cache table" in t for t in cards_a)
              and r_a_repeat["new_cards"] == 0 and r_a_repeat["lines_read"] == 0
              and r_widen["cards_total"] == 3 and r_widen["new_cards"] == 1
              and r_widen["lines_read"] == 3,
              (r_ab, r_a, cards_a, r_a_repeat, r_widen))

        check("C14 a UNC path folds case like a drive-letter path",
              tc._norm_dir("\\\\Host\\Share\\Sub\\") == "//host/share/sub"
              and tc.cwd_maps("\\\\h\\s\\work", ["\\\\h\\S"])
              and not tc.cwd_maps("\\\\h\\s-other", ["\\\\h\\S"]))

        check("C15 POSIX root '/' normalises to '/' (not empty) and matches POSIX sessions only",
              tc._norm_dir("/") == "/" and tc._norm_dir("///") == "/"
              and tc.cwd_maps("/home/a/proj", ["/"]) and tc.cwd_maps("/", ["/"])
              and not tc.cwd_maps("C:/Projects/Seat-A", ["/"])
              and not tc.cwd_maps("\\\\h\\s", ["/"]))

        # C16 (#337 review): an explicit `files=` run with a narrower `codex_cwds` skips the store rebuild, so it
        # must not record the narrower filter in cursor["codex_cwds_norm"]; the next normal run with that narrow
        # filter then sees the narrowing and rebuilds to drop the excluded folder's cards.
        out_c16 = os.path.join(root, "out-c16")
        r16_wide = tc.run(None, "codex-seat-a", out_c16, codex_dir=home,
                          codex_cwds=["C:/Projects/Seat-A", "C:/Projects/Seat-B"])
        r16_explicit = tc.run(None, "codex-seat-a", out_c16, codex_dir=home,
                              files=[picked[0]],
                              codex_cwds=["C:/Projects/Seat-A"])
        r16_narrow = tc.run(None, "codex-seat-a", out_c16, codex_dir=home,
                            codex_cwds=["C:/Projects/Seat-A"])
        cards_c16 = [c.get("text", "") for c in tc._load_jsonl(os.path.join(out_c16, "cards.jsonl"))]
        check("C16 an explicit --files run with a narrow --codex-cwd preserves codex_cwds_norm so the next normal narrow run rebuilds",
              r16_wide["cards_total"] == 3
              and r16_explicit["cards_total"] == 3
              and r16_narrow["cards_total"] == 2
              and not any("old cache table" in t for t in cards_c16),
              (r16_wide["cards_total"], r16_explicit["cards_total"], r16_narrow["cards_total"], cards_c16))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print("%d of %d checks pass" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


def test_codex_cwd():
    RESULTS.clear()
    assert main() == 0, [r for r in RESULTS if not r[1]]


if __name__ == "__main__":
    sys.exit(main())
