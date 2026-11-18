"""P1-V1: a gone file and an excluded file are independent sets."""
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402


def test_a_vanished_file_keeps_cards_when_a_different_file_is_excluded(tmp_path):
    base = tmp_path / "sessions"
    gone, excluded = base / "one" / "wire.jsonl", base / "two" / "wire.jsonl"
    for path in (gone, excluded):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    gone.unlink()
    vanished = []
    changed = tc._missing_still_on_disk({str(gone), str(excluded)}, [], str(base), vanished,
                                        excluded=lambda p: p == str(excluded))
    assert changed is False
    assert vanished == [os.path.join("one", "wire.jsonl")]
