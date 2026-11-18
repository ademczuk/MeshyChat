"""Turn reporting progresses independently of uploads, with bounded children and no content payload."""
import json
import os
from types import SimpleNamespace

import pytest

from tools.postbox import turn_reporter as reporter, turnstate


def record(path, stamp, event="task_started", *, append=False):
    with path.open("ab" if append else "wb") as out:
        out.write(json.dumps({"type": "event_msg", "timestamp": stamp,
                              "payload": {"type": event, "text": "SECRET TRANSCRIPT"}}).encode() + b"\n")


def test_record_times_select_the_session_even_with_stale_or_reversed_mtimes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    record(a, "2026-11-04T11:00:00Z", "task_complete")
    record(b, "2026-11-04T11:01:00Z")
    os.utime(b, (1, 1))
    assert reporter.Reporter().observe([str(a), str(b)])["state"] == "open"
    record(b, "2026-11-04T11:02:00Z", "task_complete", append=True)
    os.utime(b, (1, 1))
    r = reporter.Reporter()
    assert r.observe([str(a), str(b)])["state"] == "closed"
    # On the same inode/mtime, size and the new record still reopen it.
    record(b, "2026-11-04T11:03:00Z", append=True)
    os.utime(b, (1, 1))
    assert r.observe([str(a), str(b)])["state"] == "open"


def test_marker_can_leave_the_tail_without_closing_a_long_turn(tmp_path):
    path = tmp_path / "turn"
    record(path, "2026-11-04T11:00:00Z")
    r = reporter.Reporter()
    assert r.observe([str(path)])["state"] == "open"
    body = json.dumps({"type": "response_item", "timestamp": "2026-11-04T11:01:00Z",
                       "payload": {"text": "x" * 8000}}).encode() + b"\n"
    with path.open("ab") as out:
        out.write(body * 40)
    assert path.stat().st_size > turnstate.TAIL_BYTES
    assert r.observe([str(path)])["state"] == "open"
    assert reporter.Reporter().observe([str(path)])["state"] == "open"
    record(path, "2026-11-04T11:02:00Z", "turn_aborted", append=True)
    assert r.observe([str(path)])["state"] == "closed"


def test_a_new_turn_start_can_leave_the_tail_between_closed_observations(tmp_path):
    path = tmp_path / "turn"
    record(path, "2026-11-04T11:00:00Z", "task_complete")
    r = reporter.Reporter()
    assert r.observe([str(path)])["state"] == "closed"
    record(path, "2026-11-04T11:01:00Z", append=True)
    output = json.dumps({"type": "response_item", "timestamp": "2026-11-04T11:02:00Z",
                         "payload": {"text": "x" * 8000}}).encode() + b"\n"
    with path.open("ab") as out:
        out.write(output * 40)
    expected = {"state": "open", "item_at": "2026-11-04T11:02:00+00:00"}
    assert r.observe([str(path)]) == expected
    assert reporter.Reporter().observe([str(path)]) == expected


@pytest.mark.parametrize("subagent", [{"thread_id": "parent"}, None])
def test_completed_codex_children_cannot_override_an_open_parent_even_beyond_the_tail(tmp_path, subagent):
    parent, child, other_main = tmp_path / "parent", tmp_path / "child", tmp_path / "other-main"
    record(parent, "2026-11-04T11:00:00Z")
    def header(source):
        return json.dumps({"type": "session_meta", "timestamp": "2026-11-04T11:01:00Z",
                           "payload": {"source": source}}).encode() + b"\n"
    output = json.dumps({"type": "response_item", "timestamp": "2026-11-04T11:02:00Z",
                         "payload": {"text": "x" * 8000}}).encode() + b"\n"
    child.write_bytes(header({"subagent": subagent}) + output * 40)
    record(child, "2026-11-04T11:03:00Z", "task_complete", append=True)
    r = reporter.Reporter()
    expected = {"state": "open", "item_at": "2026-11-04T11:00:00+00:00"}
    assert r.observe([str(parent), str(child)]) == expected
    assert r.observe([str(child)]) is None
    record(child, "2026-11-04T11:04:00Z", "task_complete", append=True)
    assert r.observe([str(parent), str(child)]) == expected
    # A second genuine main session is still eligible to supply the newer state.
    other_main.write_bytes(header("cli"))
    record(other_main, "2026-11-04T11:05:00Z", "task_complete", append=True)
    assert r.observe([str(parent), str(child), str(other_main)])["state"] == "closed"


@pytest.mark.parametrize("header", [b'{"type":"session_meta"', b'[]\n',
    b'{"type":"session_meta","padding":"' + b'x' * turnstate.TAIL_BYTES + b'"}\n'],
    ids=["partial-line", "not-an-object", "oversized-line"])
def test_unverifiable_source_headers_withhold_the_identity_report(tmp_path, header):
    parent, bad = tmp_path / "parent", tmp_path / "bad"
    record(parent, "2026-11-04T11:00:00Z", "task_complete")
    bad.write_bytes(header)
    sent, statuses = [], []
    reporter.Reporter().tick({}, [str(parent), str(bad)], now=0,
        send=lambda ident, payload: sent.append(payload) or True,
        status=lambda **kw: statuses.append(kw))
    assert sent == []
    assert statuses[-1]["turn_report_result"] == "failed"


def test_on_change_refresh_failure_retry_and_payload_are_distinct(tmp_path):
    path = tmp_path / "PRIVATE-PATH"
    record(path, "2026-11-04T11:00:00Z")
    r, sent, statuses = reporter.Reporter(), [], []
    def send(ident, payload):
        sent.append(payload)
        return True
    def tick(now, send_fn=send):
        r.tick({}, [str(path)], now=now, send=send_fn, status=lambda **kw: statuses.append(kw))
    tick(0); tick(30); tick(119)
    assert len(sent) == 1
    tick(120)
    assert len(sent) == 2
    record(path, "2026-11-04T11:01:00Z", "task_complete", append=True)
    tick(150)
    assert sent[-1]["state"] == "closed"
    assert all(set(payload) == {"state", "item_at"} for payload in sent)
    assert "PRIVATE-PATH" not in str(sent) and "SECRET" not in str(sent)
    tick(270, lambda *a: False)
    assert r.sent_at == 150 and r.next_try == 300
    tick(299)
    assert r.sent_at == 150
    tick(300, lambda *a: False)
    assert r.next_try == 360
    tick(360)
    assert r.sent_at == 360 and r.failures == 0
    assert any(row.get("turn_report_result") == "failed" for row in statuses)


@pytest.mark.parametrize("failure", ["unsurfaced_log_limit", "unsurfaced_directory_limit",
                                  "unsurfaced_checkpoint_limit", "missing-log-directory"])
def test_tracker_failure_sends_plain_turn_and_recovers_without_transport_backoff(tmp_path, failure):
    path = tmp_path / "PRIVATE-PATH"
    record(path, "2026-11-04T11:00:00Z")
    class FailingTracker:
        def update(self, *args, **kwargs):
            raise ValueError(failure + " PRIVATE-PATH")
    tracker = (reporter.unsurfaced.Tracker(str(tmp_path / "observer.json"))
               if failure == "missing-log-directory" else FailingTracker())
    r, sent, statuses = reporter.Reporter(tracker=tracker), [], []
    ident = {"name": "seat", "log": str(tmp_path / "absent" / "spool")}
    def tick(now):
        r.tick(ident, [str(path)], now=now,
               send=lambda ident, payload: sent.append(payload.copy()) or True,
               status=lambda **kw: statuses.append(kw))
    tick(0)
    assert sent == [{"state": "open", "item_at": "2026-11-04T11:00:00+00:00"}]
    assert statuses[-1]["turn_report_result"] == "ok"
    error = "FileNotFoundError" if failure == "missing-log-directory" else "ValueError"
    assert any(row.get("unsurfaced_error") == error for row in statuses)
    assert "PRIVATE-PATH" not in str(statuses) and "PRIVATE-PATH" not in str(sent)
    assert r.failures == 0 and r.next_try == reporter.CHECK_SECONDS
    r.tracker = SimpleNamespace(update=lambda *a, **kw: {
        "unsurfaced": 0, "unsurfaced_oldest_at": None,
        "unsurfaced_since": "2026-11-04T11:00:00+00:00"})
    tick(30)
    assert len(sent) == 2 and sent[-1]["unsurfaced"] == 0
    assert any("unsurfaced_error" in row and row["unsurfaced_error"] is None for row in statuses)
    assert r.failures == 0 and statuses[-1]["turn_report_result"] == "ok"


def test_child_is_windowless_bounded_and_only_accepts_success_json(monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"applied":true}')
    monkeypatch.setattr(reporter.postbox_os, "run_bounded", run)
    ident = {"python": "python", "base_url": "http://fixture", "state": "local-state", "client": "local-client"}
    payload = {"state": "open", "item_at": "2026-11-04T11:00:00Z"}
    assert reporter.run_child(ident, payload)
    argv, kw = calls[0]
    assert argv[-4:] == ["--state", "open", "--item-at", payload["item_at"]]
    assert kw["creationflags"] == reporter.postbox_os.NO_WINDOW
    assert kw["timeout"] == 15 and kw["max_stdout"] == 8192 and kw["max_stderr"] == 4096
    monkeypatch.setattr(reporter.postbox_os, "run_bounded", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{}'))
    assert not reporter.run_child(ident, payload)
    monkeypatch.setattr(reporter.postbox_os, "run_bounded", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout='{"ok":true,"applied":false}'))
    assert not reporter.run_child(ident, payload)


def test_local_only_tail_does_not_invent_open_on_startup_or_after_growth(tmp_path):
    path = tmp_path / "local-command"
    def local(stamp):
        return json.dumps({"type": "user", "timestamp": stamp,
                           "message": {"content": "<command-name>/status</command-name>"}}).encode() + b"\n"
    path.write_bytes(local("2026-11-04T11:00:00Z"))
    r = reporter.Reporter()
    assert r.observe([str(path)]) is None
    with path.open("ab") as out:
        out.write(local("2026-11-04T11:01:00Z"))
    assert r.observe([str(path)]) is None
    # A real completed turn followed by a local command stays closed.
    path.write_bytes(json.dumps({"type": "assistant", "timestamp": "2026-11-04T11:02:00Z",
                    "message": {"stop_reason": "end_turn"}}).encode() + b"\n" + local("2026-11-04T11:03:00Z"))
    assert r.observe([str(path)])["state"] == "closed"


def test_meta_prompt_reopens_a_completed_session_before_assistant_output(tmp_path):
    path = tmp_path / "meta-prompt"
    path.write_bytes(json.dumps({"type": "assistant", "timestamp": "2026-11-04T11:00:00Z",
                                 "message": {"stop_reason": "end_turn"}}).encode() + b"\n")
    r = reporter.Reporter()
    assert r.observe([str(path)])["state"] == "closed"
    with path.open("ab") as out:
        out.write(json.dumps({"type": "user", "timestamp": "2026-11-04T11:01:00Z", "isMeta": True,
                              "message": {"content": "scheduled work"}}).encode() + b"\n")
    expected = {"state": "open", "item_at": "2026-11-04T11:01:00+00:00"}
    assert r.observe([str(path)]) == expected
    assert reporter.Reporter().observe([str(path)]) == expected


def test_unsupported_dialect_runs_no_report_and_no_enrollment(monkeypatch, tmp_path):
    ident = {"name": "unsupported", "cli": "gemini", "context": {"enabled": True}}
    states, sleeps = [], []
    monkeypatch.setattr(reporter, "reservations", lambda *a: (_ for _ in ()).throw(AssertionError("no reads")))
    reporter.run([ident], store_root=str(tmp_path), all_identities=[ident],
                 set_status=lambda name, **kw: states.append(kw), stopped=lambda: bool(sleeps),
                 sleep=lambda secs: sleeps.append(secs))
    assert states == [{"turn_report_result": "unsupported"}] and sleeps == [30]


def test_thread_targets_are_separate_from_upload_and_each_other(tmp_path):
    from tools.postbox import postbox
    identities = [{"name": name, "cli": dialect, "context": {"enabled": True}}
                  for name, dialect in [("a", "codex"), ("b", "claude"), ("c", "antigravity"),
                                        ("d", "kimi"), ("e", "gemini")]]
    threads = postbox.build_turn_reporter_threads({"identities": identities,
                "context_store_root": str(tmp_path)}, identities)
    assert len(threads) == 4
    assert all(thread.daemon and thread._target.__name__ == "run" for thread in threads)
    assert [thread._args[0][0]["name"] for thread in threads] == ["a", "b", "c", "d"]
    assert all(not thread.is_alive() for thread in threads)  # constructing a reporter never starts one


def test_antigravity_and_kimi_subagent_files_cannot_override_main_sessions(tmp_path):
    # Antigravity: main starts with USER_INPUT; subagent starts with SYSTEM_MESSAGE.
    ag_main = tmp_path / "brain" / "main-id" / ".system_generated" / "logs" / "transcript_full.jsonl"
    ag_sub = tmp_path / "brain" / "sub-id" / ".system_generated" / "logs" / "transcript_full.jsonl"
    ag_main.parent.mkdir(parents=True)
    ag_sub.parent.mkdir(parents=True)
    ag_main.write_bytes(
        json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                    "status": "DONE", "created_at": "2026-11-05T08:00:00Z"}).encode() + b"\n"
    )
    ag_sub.write_bytes(
        json.dumps({"step_index": 0, "source": "SYSTEM_SDK", "type": "SYSTEM_MESSAGE",
                    "status": "DONE", "created_at": "2026-11-05T08:01:00Z"}).encode() + b"\n"
        + json.dumps({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
                      "status": "DONE", "created_at": "2026-11-05T08:02:00Z"}).encode() + b"\n"
    )
    assert reporter.Reporter().observe([str(ag_main), str(ag_sub)]) == {
        "state": "open", "item_at": "2026-11-05T08:00:00+00:00",
    }
    assert reporter.Reporter().observe([str(ag_sub)]) is None

    # Kimi-code (/agents/agent-7/wire.jsonl + line 1 agentId != main) and Kimi Python (/subagents/<id>/wire.jsonl)
    kc_main = tmp_path / "session_1" / "agents" / "main" / "wire.jsonl"
    kc_sub_dir = tmp_path / "session_1" / "agents" / "agent-7" / "wire.jsonl"
    kc_sub_moved = tmp_path / "moved_sub_wire.jsonl"
    kp_main = tmp_path / "py_sess" / "wire.jsonl"
    kp_sub = tmp_path / "py_sess" / "subagents" / "sub1" / "wire.jsonl"
    for p in (kc_main, kc_sub_dir, kp_main, kp_sub):
        p.parent.mkdir(parents=True, exist_ok=True)

    kc_meta = b'{"type":"metadata","protocol_version":"1.5","created_at":1793612764804}\n'
    kc_main.write_bytes(
        kc_meta
        + b'{"type":"runtime.set_binding","agentId":"main","time":1793612764843}\n'
        + b'{"type":"turn.prompt","agentId":"main","time":1793612765000}\n'
    )
    sub_body = (
        kc_meta
        + b'{"type":"runtime.set_binding","agentId":"agent-7","time":1793612770000}\n'
        + b'{"type":"agent.turn.ended","outcome":"done","time":1793612790000,"kind":"event"}\n'
    )
    kc_sub_dir.write_bytes(sub_body)
    kc_sub_moved.write_bytes(sub_body)
    # An unrelated ancestor directory named "subagents" must not exclude a genuine main session.
    kc_under_subagents_anc = tmp_path / "subagents" / ".kimi-code" / "session_2" / "agents" / "main" / "wire.jsonl"
    kc_under_subagents_anc.parent.mkdir(parents=True, exist_ok=True)
    kc_under_subagents_anc.write_bytes(kc_main.read_bytes())
    assert reporter.Reporter().observe([str(kc_under_subagents_anc)]) == {
        "state": "open", "item_at": "2026-11-02T09:46:05+00:00",
    }
    assert reporter.Reporter().observe([str(kc_main), str(kc_sub_dir), str(kc_sub_moved)]) == {
        "state": "open", "item_at": "2026-11-02T09:46:05+00:00",
    }
    assert reporter.Reporter().observe([str(kc_sub_dir), str(kc_sub_moved)]) is None

    # Malformed line-1 record on a kimi-code wire fails closed like line 0.
    kc_bad_second = tmp_path / "bad_second_wire.jsonl"
    kc_bad_second.write_bytes(kc_meta + b'{"type":"runtime.set_binding"')
    with pytest.raises(ValueError, match="source_header"):
        reporter.Reporter().observe([str(kc_bad_second)])

    # Python Kimi main allows >TAIL_BYTES line 1 prompt without failing source_header, while /subagents/ is excluded.
    kp_meta = b'{"type":"metadata","protocol_version":"1.10"}\n'
    big_prompt = "x" * (turnstate.TAIL_BYTES + 4096)
    kp_main.write_bytes(
        kp_meta
        + json.dumps({"timestamp": 1793256970.0,
                      "message": {"type": "TurnBegin", "payload": {"user_input": big_prompt}}}).encode() + b"\n"
        + b'{"timestamp":1793256980.0,"message":{"type":"StepBegin","payload":{"n":1}}}\n'
    )
    kp_sub.write_bytes(
        kp_meta
        + b'{"timestamp":1793256995.0,"message":{"type":"TurnEnd","payload":{}}}\n'
    )
    assert reporter.Reporter().observe([str(kp_main), str(kp_sub)]) == {
        "state": "open", "item_at": "2026-10-29T06:56:20+00:00",
    }
    assert reporter.Reporter().observe([str(kp_sub)]) is None


def test_inode_reservations_are_read_only_and_conflicts_are_not_ignored(monkeypatch, tmp_path):
    reads = []
    def read(path):
        reads.append(path)
        seat = "a" if str(path).endswith("a/enrollment.json") or str(path).endswith("a\\enrollment.json") else "b"
        return {"files": {"source": {"seat": seat}}, "superseded": []}
    monkeypatch.setattr(reporter.context_worker, "read_ledger", read)
    monkeypatch.setattr(reporter.context_worker, "_entry_reserved_inodes", lambda entry: [(1, 9)])
    monkeypatch.setattr(reporter.context_worker, "_superseded_reserved_inodes", lambda entries: [])
    identities = [{"name": "a"}, {"name": "b"}]
    assert reporter.reservations(identities, str(tmp_path)) == {(1, 9): "__CONFLICT__"}
    assert len(reads) == 2


def test_run_reports_all_four_dialects_and_scopes_unsurfaced_tracker_to_claude_codex(monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    sources_root = tmp_path / "sources"
    sources_root.mkdir()
    store_root = tmp_path / "store"

    files = {
        "codex-seat": sources_root / "codex.jsonl",
        "claude-seat": sources_root / "claude.jsonl",
        "ag-seat": sources_root / "transcript_full.jsonl",
        "kimi-seat": sources_root / "wire.jsonl",
    }
    record(files["codex-seat"], "2026-11-05T11:00:00Z", "task_complete")
    files["claude-seat"].write_bytes(
        json.dumps({"type": "assistant", "timestamp": "2026-11-05T11:01:00Z",
                    "message": {"stop_reason": "end_turn"}}).encode() + b"\n"
    )
    files["ag-seat"].write_bytes(
        json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                    "status": "DONE", "created_at": "2026-11-05T11:02:00Z"}).encode() + b"\n"
    )
    files["kimi-seat"].write_bytes(
        b'{"type":"metadata","protocol_version":"1.5","created_at":1793876580000}\n'
        b'{"type":"runtime.set_binding","agentId":"main","time":1793876580001}\n'
        b'{"type":"agent.turn.ended","outcome":"done","time":1793876581000,"kind":"event"}\n'
    )

    identities = []
    for name, dialect in [("codex-seat", "codex"), ("claude-seat", "claude"),
                          ("ag-seat", "antigravity"), ("kimi-seat", "kimi")]:
        (store_root / name).mkdir(parents=True, exist_ok=True)
        log_path = spool / f"{name}.log"
        log_path.write_bytes(b"")
        identities.append({
            "name": name,
            "cli": dialect,
            "python": "python",
            "base_url": "http://fixture",
            "state": str(tmp_path / f"{name}-state.json"),
            "client": str(tmp_path),
            "log": str(log_path),
            "context": {"enabled": True, "dialect": dialect, "allowed_roots": [str(sources_root)]},
        })

    monkeypatch.setattr(reporter, "reservations", lambda *a: {})
    monkeypatch.setattr(
        reporter.context_worker, "verified_enrolled_sources",
        lambda ledger_path, *, seat, dialect, allowed_roots, box_enrolled_inodes: ([str(files[seat])], None),
    )
    calls = {}
    statuses = {}
    sleeps = []

    def fake_run_bounded(argv, **kw):
        state_file = argv[argv.index("--state-file") + 1]
        seat = os.path.basename(state_file).removesuffix("-state.json")
        calls[seat] = list(argv)
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"applied":true}')

    monkeypatch.setattr(reporter.postbox_os, "run_bounded", fake_run_bounded)

    reporter.run(
        identities,
        store_root=str(store_root),
        all_identities=identities,
        set_status=lambda name, **kw: statuses.setdefault(name, {}).update(kw),
        stopped=lambda: bool(sleeps),
        sleep=lambda secs: sleeps.append(secs),
    )

    assert sleeps == [reporter.CHECK_SECONDS]
    for name in ("codex-seat", "claude-seat", "ag-seat", "kimi-seat"):
        assert statuses[name]["turn_report_result"] == "ok"

    # Claude and Codex attach Step C unsurfaced tracking (5-field CLI args + checkpoint).
    for name in ("codex-seat", "claude-seat"):
        argv = calls[name]
        assert argv[argv.index("--state") + 1] == "closed"
        assert argv[argv.index("--unsurfaced") + 1] == "0"
        assert "--unsurfaced-since" in argv
        assert (store_root / name / "unsurfaced.json").is_file()

    # Antigravity and Kimi report Step B turn state (2-field CLI args, no false Step C loss/gap).
    assert calls["ag-seat"][-4:] == ["--state", "open", "--item-at", "2026-11-05T11:02:00+00:00"]
    assert "--unsurfaced" not in calls["ag-seat"]
    assert calls["kimi-seat"][-4:] == ["--state", "closed", "--item-at", "2026-11-05T11:03:01+00:00"]
    assert "--unsurfaced" not in calls["kimi-seat"]
    for name in ("ag-seat", "kimi-seat"):
        assert not (store_root / name / "unsurfaced.json").exists()
