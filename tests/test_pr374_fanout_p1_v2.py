"""P1-V2: the unmapped Codex comparison accepts a rollout that vanishes after listing."""
import os
import sys

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402


def test_unmapped_comparison_skips_a_path_replaced_by_a_directory(tmp_path, monkeypatch):
    home, out = tmp_path / "home", tmp_path / "out"
    home.mkdir()
    vanished = home / "rollout-2026-10-23T08-00-00-gone.jsonl"
    vanished.write_text("{}\n", encoding="utf-8")
    original = tc.select_codex_files
    def select(*args, **kwargs):
        vanished.unlink()
        vanished.mkdir()
        kwargs["unmapped_meta"].append({"path": str(vanished), "file": vanished.name, "cwd": "other"})
        return [], 1
    monkeypatch.setattr(tc, "select_codex_files", select)
    # Empty selection metadata is enough to exercise the getmtime/getsize race guard.
    tc.run(None, "codex-seat", str(out), codex_dir=str(home), codex_cwds=[], files=None)
    monkeypatch.setattr(tc, "select_codex_files", original)


def _rollout(home, day, sid, text, cwd):
    import json
    folder = home / "2026" / "10" / day
    folder.mkdir(parents=True, exist_ok=True)
    recs = [
        {"timestamp": "2026-10-%sT05:00:00.000Z" % day, "type": "session_meta",
         "payload": {"id": sid, "source": "cli", "originator": "codex-tui", "cli_version": "0.161.0", "cwd": cwd}},
        {"timestamp": "2026-10-%sT05:01:00.000Z" % day, "type": "response_item",
         "payload": {"type": "message", "role": "user", "id": "msg_" + sid,
                     "content": [{"type": "input_text", "text": text}]}},
        {"timestamp": "2026-10-%sT05:01:00.000Z" % day, "type": "event_msg",
         "payload": {"type": "user_message", "message": text}},
    ]
    path = folder / ("rollout-2026-10-%sT05-00-00-%s.jsonl" % (day, sid))
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8", newline="\n")
    return path


def test_a_selected_rollout_deleted_after_the_listing_does_not_crash_with_an_unmapped_session(tmp_path, monkeypatch):
    """The fan-out probe (vanished-files/probe3.py codex_race_unmapped_getmtime): at d3827c9b this raised
    FileNotFoundError from os.path.getmtime over the selected paths; now the gone file is skipped and named."""
    cwd = "C:/Projects/Seat-A"
    home, out = tmp_path / "h", tmp_path / "o"
    gone = _rollout(home, "10", "aaa1", "please keep the nightly report", cwd)
    _rollout(home, "11", "aaa2", "use the staging bucket for this", cwd)
    _rollout(home, "12", "bbb1", "another seat's session", "C:/Projects/Other")       # unmapped
    real = tc.select_codex_files

    def select_then_delete(*args, **kwargs):
        res = real(*args, **kwargs)
        if gone.exists():
            gone.unlink()                                  # housekeeping removes it right after the listing
        return res

    monkeypatch.setattr(tc, "select_codex_files", select_then_delete)
    report = tc.run(None, "seat", str(out), codex_dir=str(home), codex_cwds=[cwd])
    assert any(str(v).endswith(gone.name) for v in (report.get("files_vanished") or []))
