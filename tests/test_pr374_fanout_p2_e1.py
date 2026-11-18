"""P2-E1: one bare filename across two Kimi sessions is refused as ambiguous."""
import json
import os
import sys

import pytest

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402


def _wire(home, sid):
    path = home / "wd" / ("session_" + sid) / "agents" / "main" / "wire.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "metadata"}) + "\n", encoding="utf-8")


def test_bare_wire_name_with_two_matches_is_refused(tmp_path):
    home = tmp_path / "home"
    _wire(home, "11111111-1111-1111-1111-111111111111")
    _wire(home, "0447bf92-25fb-5230-b531-5a0d9c00d8c7")
    with pytest.raises(ValueError, match="ambiguous"):
        tc.run(None, "nxxxxx", str(tmp_path / "out"), kimi_dir=str(home), exclude_files=["wire.jsonl"])
