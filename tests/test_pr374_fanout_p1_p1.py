"""P1-P1: direct hook mode checks its store policy before invoking a writer."""
import json
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import load_hook as lh  # noqa: E402


def test_direct_policy_reader_fails_closed_on_false_and_bad_base(tmp_path):
    store = tmp_path / "store"; store.mkdir()
    for cursor in ({"excluded_files": False}, {"excluded_files": {}, "exclude_base": 7}):
        (store / "cursor.json").write_text(json.dumps(cursor), encoding="utf-8")
        try:
            lh._store_excluded_fn(str(store))
        except lh.PolicyUnreadable:
            pass
        else:
            raise AssertionError("unreadable policy was accepted")


def test_direct_write_mode_writes_nothing_when_the_store_policy_is_unreadable(tmp_path, monkeypatch):
    """The fan-out probe (policy-unreadable/probe_hook_e2e.py): at d3827c9b one_pass in direct mode sent the
    excluded transcript's cards to the writer when cursor.json was unreadable as a policy; now it fails closed."""
    import argparse
    import json
    import transcript_cards as tc
    monkeypatch.delenv("WRITE", raising=False)
    monkeypatch.delenv("MESHY_CARDS_WRITE", raising=False)
    monkeypatch.setattr(lh, "tie_to_worker", lambda p: None)
    project, store = tmp_path / "project", tmp_path / "store"
    project.mkdir()
    for name, sid in (("alpha.jsonl", "session-alpha"), ("beta.jsonl", "session-beta")):
        prompts = ["echo txxxx-shell-ok", "Please review the synthetic example %s." % sid]
        (project / name).write_text("".join(json.dumps(
            {"type": "user", "uuid": "%s-%d" % (sid, i), "sessionId": sid, "timestamp": "2026-10-23T08:00:0%d.000Z" % i,
             "message": {"role": "user", "content": t}}) + "\n" for i, t in enumerate(prompts)), encoding="utf-8")
    tc.run(str(project), "txxxx", str(store), exclude_files=["./alpha.jsonl"])
    cursor = json.loads((store / "cursor.json").read_text(encoding="utf-8"))
    cursor["excluded_files"] = False                       # damaged: the loader reads this as unreadable
    (store / "cursor.json").write_text(json.dumps(cursor), encoding="utf-8")
    captured = tmp_path / "captured.jsonl"
    writer = tmp_path / "stub_writer.py"
    writer.write_text(
        "import sys, json, shutil\n"
        "f = sys.argv[sys.argv.index('--file') + 1]\n"
        "shutil.copyfile(f, %r)\n"
        "print(json.dumps({'outcomes': {'written': 1}}))\n" % str(captured), encoding="utf-8")
    cfg = argparse.Namespace(project_dir=str(project), seat="txxxx", store=str(store), marker=(), own_name=(),
                             extra=(), stage_timeout=120, lock_fd=None, seat_files=None, seat_file_mode=None,
                             emit_context=False, root_store=None, writer=str(writer), base_url="http://127.0.0.1:9",
                             state_file=str(tmp_path / "s.json"), loader=None, write_mode="direct", identity=None)
    status, detail = lh.one_pass(cfg)
    assert status == "failed" and not captured.exists(), (status, detail)
