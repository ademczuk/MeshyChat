"""P1-B1/P2-E3: board identities resolve exactly and old widened policies are re-resolved."""
import json
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402


def _card(identifier):
    return {"id": "card-" + identifier, "schema": "card.v1", "type": "Claim",
            "time": "2026-10-23T08:00:00.000Z", "source": {
        "record_uuid": "planboard:issue:" + identifier, "block_index": 0,
        "files": ["planboard:issue:" + identifier], "sessions": ["uuid-" + identifier]}}


def _store(tmp_path):
    out = tmp_path / "store"
    out.mkdir()
    (out / "cursor.json").write_text(json.dumps({"id_rule": tc.ID_RULE, "files": {}, "seat_files": {}}),
                                     encoding="utf-8")
    with open(out / "cards.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for ident in ("TKT-3", "TKT-32", "TKT-38"):
            f.write(json.dumps(_card(ident)) + "\n")
    return out


def _ids(out):
    return [json.loads(line)["source"]["files"][0] for line in (out / "cards.jsonl").read_text(encoding="utf-8").splitlines()]


def test_identifier_and_full_planboard_ref_match_only_the_exact_issue(tmp_path):
    out = _store(tmp_path)
    tc.run(None, "oxxxxxx", str(out), files=[], exclude_files=["TKT-3"])
    assert _ids(out) == ["planboard:issue:TKT-32", "planboard:issue:TKT-38"]
    policy = json.loads((out / "cursor.json").read_text(encoding="utf-8"))["excluded_files"]["TKT-3"]
    assert policy["card_refs"] == ["planboard:issue:TKT-3"]
    assert tc._matches_file_spec("planboard:issue:TKT-32", ["planboard:issue:TKT-3"]) is False


def test_a_frozen_old_prefix_result_is_reresolved(tmp_path):
    out = _store(tmp_path)
    (out / "cursor.json").write_text(json.dumps({"id_rule": tc.ID_RULE, "files": {}, "seat_files": {}, "excluded_files": {"TKT-3": {
        "resolved": True, "realpaths": [], "sessions": [],
        "card_refs": ["planboard:issue:TKT-3", "planboard:issue:TKT-32"]}}}), encoding="utf-8")
    tc.run(None, "oxxxxxx", str(out), files=[])
    assert _ids(out) == ["planboard:issue:TKT-32", "planboard:issue:TKT-38"]
    policy = json.loads((out / "cursor.json").read_text(encoding="utf-8"))["excluded_files"]["TKT-3"]
    assert policy["card_refs"] == ["planboard:issue:TKT-3"]
