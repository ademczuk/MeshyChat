"""L1 (PR #354): a --seat-file that has vanished from --project-dir is skipped with a warning; it never refuses
the run, so the seat's other transcripts still load. Before the fix, main() called ap.error for it (exit 2), and one
transcript deleted by Claude Code housekeeping stopped every card of the seat (the HZ, 22 Oct 2026, 12 h).
A name that is wrong in shape (a path, not .jsonl, a key-like name), or missing and never recorded as a pin of this
store (a typo, a case variant), is still refused."""
import json
import os
import sys

import pytest

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402


def _transcript(path, text):
    rec = {"type": "user", "timestamp": "2026-10-22T08:00:00.000Z", "sessionId": os.path.basename(path)[:-6],
           "message": {"role": "user", "content": text}}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec) + "\n")


def test_a_vanished_seat_file_is_skipped_and_the_rest_still_load(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    _transcript(str(proj / "kept.jsonl"), "continuation without a marker")
    _transcript(str(proj / "gone.jsonl"), "this one will vanish")
    args = ["--project-dir", str(proj), "--seat", "txxxx", "--out", str(tmp_path / "out"),
            "--seat-file", "kept.jsonl", "--seat-file", "gone.jsonl"]
    assert tc.main(args) in (0, None)            # both pins are recorded in the store's cursor
    capsys.readouterr()
    os.remove(proj / "gone.jsonl")             # then housekeeping deletes one
    rc = tc.main(["--project-dir", str(proj), "--seat", "txxxx", "--out", str(tmp_path / "out"),
                  "--seat-file", "kept.jsonl", "--seat-file", "gone.jsonl"])
    captured = capsys.readouterr()
    assert rc in (0, None), captured.err
    report = json.loads(captured.out)
    assert report["seat_files_missing"] == ["gone.jsonl"]
    assert "kept.jsonl" in report["files_read"]
    assert "gone.jsonl is not in --project-dir any more" in captured.err


@pytest.mark.parametrize("bad", ["sub/x.jsonl", "notes.txt", "key.jsonl"])
def test_a_wrong_shaped_seat_file_is_still_refused(tmp_path, bad):
    proj = tmp_path / "proj"
    proj.mkdir()
    with pytest.raises(SystemExit) as exc:
        tc.main(["--project-dir", str(proj), "--seat", "txxxx", "--out", str(tmp_path / "out"), "--seat-file", bad])
    assert exc.value.code == 2
