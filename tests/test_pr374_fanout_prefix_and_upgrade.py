"""#374 after 77bba3d3 (kxxx 8c5f64 / 9d8079, pxx 30be81, GitHub Codex r4360873682). Synthetic fixtures only.
1. A legacy unresolved entry typed as a session prefix keeps excluding through load_hook's reader: the hook matches
   without a resolution pass, so a prefix gate there would load a session that was just excluded.
2. Upgrading an entry an older head froze (no "exact-v1") only ADDS identities: when a new file takes the old
   file's name, the originally excluded transcript stays excluded, in the upgrading pass and after it."""
import json
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import load_hook as lh  # noqa: E402
import transcript_cards as tc  # noqa: E402

LATER = "rollout-2026-10-23T09-00-00-shared-prefix-later.jsonl"


def test_a_legacy_prefix_entry_still_excludes_its_session_through_the_hooks_reader(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "cursor.json").write_text(json.dumps({"excluded_files": {"shared-prefix": True}}), encoding="utf-8")
    excluded = lh._store_excluded_fn(str(store))
    assert excluded is not None
    assert excluded(str(tmp_path / "h" / "2026" / "10" / "23" / LATER)) is True
    assert excluded(str(tmp_path / "h" / "2026" / "10" / "23" / "rollout-2026-10-23T09-00-00-other.jsonl")) is False


def _claude(project, name, sid, text):
    prompts = ["echo txxxx-shell-ok", text]
    (project / name).write_text("".join(json.dumps(
        {"type": "user", "uuid": "%s-%d" % (sid, i), "sessionId": sid, "timestamp": "2026-10-23T08:00:0%d.000Z" % i,
         "message": {"role": "user", "content": t}}) + "\n" for i, t in enumerate(prompts)), encoding="utf-8")


def _texts(store):
    path = store / "cards.jsonl"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def test_upgrading_an_old_frozen_entry_never_readmits_the_transcript_it_excluded(tmp_path):
    project, store = tmp_path / "project", tmp_path / "store"
    project.mkdir()
    _claude(project, "alpha.jsonl", "session-alpha", "Alpha secret plan for the excluded review.")
    _claude(project, "beta.jsonl", "session-beta", "Beta ordinary review of the deploy notes.")
    tc.run(str(project), "txxxx", str(store), exclude_files=["./alpha.jsonl"])
    cursor = json.loads((store / "cursor.json").read_text(encoding="utf-8"))
    entry = cursor["excluded_files"]["./alpha.jsonl"]
    assert "session-alpha" in entry["sessions"]
    entry.pop("resolution", None)                          # as an older head froze it (pre exact-v1)
    (store / "cursor.json").write_text(json.dumps(cursor), encoding="utf-8")
    os.rename(project / "alpha.jsonl", project / "alpha-old.jsonl")            # the excluded transcript, renamed
    _claude(project, "alpha.jsonl", "session-gamma", "Gamma new work under the old file name.")

    report = tc.run(str(project), "txxxx", str(store))                          # the upgrading pass
    assert "alpha-old.jsonl" not in (report.get("files_read") or [])
    assert "Alpha secret plan" not in _texts(store)
    upgraded = json.loads((store / "cursor.json").read_text(encoding="utf-8"))["excluded_files"]["./alpha.jsonl"]
    assert upgraded.get("resolution") == "exact-v1" and "session-alpha" in upgraded["sessions"]

    report = tc.run(str(project), "txxxx", str(store))                          # and after it
    assert "alpha-old.jsonl" not in (report.get("files_read") or [])
    assert "Alpha secret plan" not in _texts(store)
