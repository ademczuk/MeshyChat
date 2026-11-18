"""P1-C1: queued human prompts participate in the one shared marker definition."""
import json
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import load_hook as lh  # noqa: E402
import transcript_cards as tc  # noqa: E402


def _record(text, n, kind="user"):
    if kind == "queued":
        return {"type": "attachment", "uuid": "q-%d" % n, "timestamp": "2026-10-23T08:00:00.000Z",
                "attachment": {"type": "queued_command", "prompt": text, "commandMode": "prompt",
                               "origin": {"kind": "human"}}}
    return {"type": "user", "uuid": "u-%d" % n, "sessionId": "s", "timestamp": "2026-10-23T08:00:00.000Z",
            "message": {"role": "user", "content": text}}


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def test_queued_foreign_marker_is_refused_but_an_unmarked_control_is_admitted(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "uxxx.jsonl", [_record("hello", 1), _record("echo uxxx-shell-ok", 2, "queued"),
                                      _record("uxxx private ruling", 3)])
    _write(project / "plain.jsonl", [_record("ordinary continuation", 4)])
    own, marker = {"txxxx"}, [b"echo txxxx-shell-ok"]
    assert tc._has_foreign_seat_marker(str(project / "uxxx.jsonl"), own, marker)
    assert lh._transcript_has_foreign_marker(str(project), "uxxx.jsonl", "txxxx")
    picked = [os.path.basename(p) for p in tc.select_files(str(project), "txxxx", cache={}, single_seat=True)]
    assert picked == ["plain.jsonl"]


def test_queued_own_marker_selects_an_undeclared_transcript(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "own.jsonl", [_record("echo txxxx-shell-ok", 1, "queued"), _record("work", 2)])
    picked = [os.path.basename(p) for p in tc.select_files(str(project), "txxxx", cache={})]
    assert picked == ["own.jsonl"]
