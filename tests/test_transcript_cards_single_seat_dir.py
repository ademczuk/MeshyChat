"""L2 (PR #354): in a folder declared single-seat (--single-seat-dir), a transcript with no seat marker (a resumed or
continued session) is the seat's, unless it holds ANOTHER seat's marker. An undeclared folder keeps the marker rule
exactly. Measured on HQD, 23 Oct 2026: 8 of kxxx's 10 newest transcripts hold no marker, and only 1 of those 8 links
to another file by uuid, so a uuid-chain rule alone would have missed 7."""
import json
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402


def _write(path, prompts, first_type="last-prompt"):
    sid = os.path.basename(path)[:-6]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"type": first_type, "sessionId": sid, "leafUuid": "00000000-0000-4000-8000-000000000000"}) + "\n")
        for i, text in enumerate(prompts):
            f.write(json.dumps({"type": "user", "uuid": "%s-%d" % (sid, i), "sessionId": sid,
                                "timestamp": "2026-10-22T08:00:0%d.000Z" % i,
                                "message": {"role": "user", "content": text}}) + "\n")


def _tree(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(str(proj / "marked.jsonl"), ["echo txxxx-shell-ok", "work"])
    _write(str(proj / "resumed.jsonl"), ["carry on from where we were"], first_type="ai-title")
    _write(str(proj / "foreign.jsonl"), ["echo uxxx-shell-ok", "uxxx's work"])
    return proj


def _picked(proj, **kw):
    return sorted(os.path.basename(p) for p in tc.select_files(str(proj), "txxxx", cache={}, **kw))


def test_undeclared_folder_keeps_the_marker_rule(tmp_path):
    assert _picked(_tree(tmp_path)) == ["marked.jsonl"]


def test_declared_single_seat_folder_takes_unmarked_but_never_foreign(tmp_path):
    assert _picked(_tree(tmp_path), single_seat=True) == ["marked.jsonl", "resumed.jsonl"]


def test_the_flag_reaches_select_files_through_main(tmp_path, capsys):
    proj = _tree(tmp_path)
    rc = tc.main(["--project-dir", str(proj), "--seat", "txxxx", "--out", str(tmp_path / "out"), "--single-seat-dir"])
    report = json.loads(capsys.readouterr().out)
    assert rc in (0, None)
    assert sorted(report["files_read"]) == ["marked.jsonl", "resumed.jsonl"]
