"""P1-P2: the producer refuses every unreadable exclusion-policy shape before writing."""
import json
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402


def test_policy_validator_rejects_false_empty_and_bad_resolved_shapes(tmp_path, capsys):
    project, out = tmp_path / "project", tmp_path / "out"
    project.mkdir(); out.mkdir()
    (project / "s.jsonl").write_text(json.dumps({"type": "user", "uuid": "u", "sessionId": "s",
        "timestamp": "2026-10-23T08:00:00.000Z", "message": {"content": "echo txxxx-shell-ok\nkeep this"}}) + "\n",
        encoding="utf-8")
    for bad in (False, None, [], "", {"x": {"resolved": True}}, {"x": {"resolved": True,
        "realpaths": [], "sessions": "s", "card_refs": []}}):
        (out / "cursor.json").write_text(json.dumps({"excluded_files": bad}), encoding="utf-8")
        assert tc.main(["--project-dir", str(project), "--seat", "txxxx", "--out", str(out)]) == 4
        assert "unreadable as an exclusion policy" in capsys.readouterr().err
        assert not (out / "cards.jsonl").exists()
