"""P2-E2: identities are case-sensitive while real paths keep their platform rule."""
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402


def test_sessions_and_card_refs_do_not_case_fold_or_prefix_match():
    assert not tc._matches_file_spec("CaseID", ["caseid"])
    assert not tc._matches_file_spec("planboard:issue:CaseID", ["planboard:issue:caseid"])
    assert not tc._matches_file_spec("CaseIDx", ["CaseID"])
