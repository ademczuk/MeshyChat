"""PR2 operation identities keep a retry from becoming another message.

These tests use only the client journal and stubbed signed calls. They do not open a
socket or a database connection.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node  # noqa: E402
from tools.meshy_node import sendguard  # noqa: E402


def _state():
    return {"peer_id": "00000000-0000-0000-0000-000000000001", "label": "uxxx", "status": "minted",
            "kid": "test-kid", "signing_seed_b64url": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "memory_peer_id": "29d6a9f7-ed27-5153-a9f2-f6feb3175e77"}


def _ok(key):
    return {"key": key, "status": "completed", "result": {"ok": True, "key": key, "version": 1},
            "result_signature_verified": True}


def _send(path, state, op_id):
    return node.op_send(path, state, op_id=op_id, to=["nxxxxx"], thread_id="pr2/test", body="hello",
                        msg_type="note", author_family="test")


def _send_with(path, state, op_id, **changes):
    args = {"to": ["nxxxxx"], "cc": ["cc"], "thread_id": "pr2/test", "body": "hello", "msg_type": "note",
            "author_family": "test", "in_reply_to": "msg/1-a", "closes": ["msg/2-b"], "requires_ack": True,
            "done_when": "answer", "attachments": [{"file_id": "00000000-0000-0000-0000-000000000001", "name": "a.txt", "size_bytes": 1, "sha256": "a" * 64}]}
    args.update(changes)
    return node.op_send(path, state, op_id=op_id, **args)


def _stored(path, op_id):
    return json.loads((Path(f"{path}.ops") / f"{op_id}.json").read_text(encoding="utf-8"))


def _matching_read(entry):
    return {"key": entry["key"], "version": 1, "_value": entry["value"],
            "signature_verified": True, "status_verified": True}


def _absent():
    return node.ReadFailed({"error": node.E_READ_REFUSED, "status": "failed", "error_code": "E_UNKNOWN_KEY"})


def _admit_from_journal(path):
    """The CLI's op callback: a retry is charged from the signed, stored value."""
    return lambda value, bulk_ack: sendguard.admit(path, to=value.get("to"), cc=value.get("cc"),
                                                    body=value.get("body", ""), subject=value.get("subject"),
                                                    bulk_ack=bulk_ack, refused=node.Refused)


def _six_large_sends(path):
    for number in range(sendguard.MAX_PER_RECIPIENT):
        sendguard.record(path, to=["nxxxxx"], cc=None, key=f"msg/{number}",
                         body_bytes=4096, body="x" * 4096)


def _uncertain_large_op(path, state, op_id, monkeypatch):
    monkeypatch.setattr(node, "invoke", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("before storage")))
    with pytest.raises(TimeoutError):
        node.op_send(path, state, op_id=op_id, to=["nxxxxx"], thread_id="pr2/test", body="x" * 4096,
                     msg_type="note", author_family="test")


def test_swallowed_receipt_reuses_one_message_and_one_send_record(tmp_path, monkeypatch):
    """Acceptance 1: a second presentation of one op never mints another key or record."""
    state, calls, notes = _state(), [], []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or _ok(k["args"]["key"]))
    monkeypatch.setattr(node, "note_sent", lambda *a: notes.append(a[2]) or True)
    first = _send(tmp_path / "seat.json", state, "swallowed")
    second = _send(tmp_path / "seat.json", state, "swallowed")
    assert len(calls) == 1 and len(notes) == 1
    assert second["already_sent"] is True and second["key"] == first["key"]


@pytest.mark.parametrize("changed", [
    {"to": ["other"]}, {"cc": ["other"]}, {"thread_id": "other/thread"}, {"msg_type": "question"},
    {"body": "different"}, {"in_reply_to": "msg/3-c"}, {"closes": ["msg/4-d"]}, {"requires_ack": False},
    {"done_when": "later"}, {"attachments": [{"file_id": "00000000-0000-0000-0000-000000000001", "name": "a.txt", "size_bytes": 1, "sha256": "b" * 64}]},
])
def test_an_op_id_cannot_be_reused_for_different_message_arguments(tmp_path, monkeypatch, changed):
    state, calls = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or _ok(k["args"]["key"]))
    _send_with(tmp_path / "seat.json", state, "stable")
    with pytest.raises(node.Refused, match="different message arguments"):
        _send_with(tmp_path / "seat.json", state, "stable", **changed)
    assert len(calls) == 1


def test_an_op_id_accepts_the_same_message_arguments(tmp_path, monkeypatch):
    state, calls = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or _ok(k["args"]["key"]))
    _send_with(tmp_path / "seat.json", state, "stable")
    assert _send_with(tmp_path / "seat.json", state, "stable")["state"] == "succeeded"
    assert len(calls) == 1


@pytest.mark.parametrize("flag,first,changed", [("--subject", "first-subject", "changed-subject"),
                                                  ("--ref", "msg/1-a", "msg/2-b")])
def test_cli_refuses_an_op_id_when_only_subject_or_ref_changes(tmp_path, monkeypatch, flag, first, changed):
    state, calls = _state(), []
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or _ok(k["args"]["key"]))
    monkeypatch.setattr(node, "note_sent", lambda *a: True)
    base = ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "seat.json"),
            "--thread", "pr2/test", "--to", "nxxxxx", "--body", "hello", "--op-id", "fingerprint",
            "--no-recipient-check", "--no-card-check"]

    assert cli.main([*base, flag, first]) == 0
    assert cli.main([*base, flag, changed]) == 2
    assert len(calls) == 1


@pytest.mark.parametrize("flag,value", [("--subject", "same-subject"), ("--ref", "msg/1-a")])
def test_cli_accepts_an_op_id_when_subject_or_ref_matches(tmp_path, monkeypatch, flag, value):
    state, calls = _state(), []
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or _ok(k["args"]["key"]))
    monkeypatch.setattr(node, "note_sent", lambda *a: True)
    base = ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "seat.json"),
            "--thread", "pr2/test", "--to", "nxxxxx", "--body", "hello", "--op-id", "fingerprint",
            "--no-recipient-check", "--no-card-check", flag, value]

    assert cli.main(base) == 0
    assert cli.main(base) == 0
    assert len(calls) == 1


def test_crash_after_accept_status_reconciles_to_succeeded(tmp_path, monkeypatch):
    """Acceptance 2: an interrupted command is recovered by its version-one value."""
    state = _state()
    monkeypatch.setattr(node, "invoke", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("after accept")))
    with pytest.raises(TimeoutError):
        _send(tmp_path / "seat.json", state, "crash")
    entry = _stored(tmp_path / "seat.json", "crash")
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(entry))
    got = node.op_status(tmp_path / "seat.json", state, "crash")
    assert got["state"] == "succeeded" and got["already_sent"] is True


def test_status_refuses_an_unstored_large_op_at_the_guard_without_presenting_it(tmp_path, monkeypatch):
    state, path = _state(), tmp_path / "seat.json"
    _uncertain_large_op(path, state, "status-guard", monkeypatch)
    _six_large_sends(path)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    writes = []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k))

    got = node.op_status(path, state, "status-guard", admit=_admit_from_journal(path))

    assert got["state"] == "refused-by-guard" and writes == []
    assert _stored(path, "status-guard")["state"] == "refused-by-guard"


def test_drain_refuses_an_unstored_large_op_at_the_guard_without_presenting_it(tmp_path, monkeypatch):
    state, path = _state(), tmp_path / "seat.json"
    _uncertain_large_op(path, state, "drain-guard", monkeypatch)
    _six_large_sends(path)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    writes = []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k))

    got = node.op_drain(path, state, admit=_admit_from_journal(path))

    assert got["refused"] == 1 and got["refused_ids"] == ["drain-guard"] and writes == []
    assert _stored(path, "drain-guard")["state"] == "refused-by-guard"


def test_cli_drain_records_each_success_before_admitting_the_next_large_op(tmp_path, monkeypatch):
    """One drain pass must consume guard allowance as each proven op settles."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    for number in range(sendguard.MAX_PER_RECIPIENT + 1):
        _uncertain_large_op(path, state, f"drain-{number}", monkeypatch)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    args = ["send", "drain", "--base-url", "http://127.0.0.1:1", "--state-file", str(path)]

    assert cli.main(args) == 0
    assert len(writes) == sendguard.MAX_PER_RECIPIENT
    assert len(json.loads(sendguard.ledger_path(path).read_text(encoding="utf-8"))) == sendguard.MAX_PER_RECIPIENT
    assert cli.main(args) == 0
    assert len(writes) == sendguard.MAX_PER_RECIPIENT


def test_drain_counts_every_uncounted_success_before_admitting_a_pending_op(tmp_path, monkeypatch):
    """GitHub Codex on ad4f6030 (pxx 70f7c9): a success whose CLI exited before its ledger entry ("z") must be
    counted before an earlier-sorting pending large op ("a") is admitted, or the drain sends one over the limit."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    for number in range(sendguard.MAX_PER_RECIPIENT - 1):
        sendguard.record(path, to=["nxxxxx"], cc=None, key=f"msg/{number}", body_bytes=4096, body="x" * 4096)
    _uncertain_large_op(path, state, "z", monkeypatch)
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(_stored(path, "z")))
    assert node.op_status(path, state, "z")["state"] == "succeeded"
    assert node.op_uncounted_successes(path) == ["z"]
    _uncertain_large_op(path, state, "a", monkeypatch)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))

    assert cli.main(["send", "drain", "--base-url", "http://127.0.0.1:1", "--state-file", str(path)]) == 0

    assert writes == []
    assert len(json.loads(sendguard.ledger_path(path).read_text(encoding="utf-8"))) == sendguard.MAX_PER_RECIPIENT
    assert node.op_uncounted_successes(path) == []
    assert _stored(path, "a")["state"] == "refused-by-guard"


def test_a_crash_between_the_ledger_write_and_guard_counted_charges_the_send_once(tmp_path, monkeypatch):
    """GitHub Codex r4366608260 (pxx 8b5f89): the ledger is written before the journal's guard_counted flag. If the
    process dies between the two, the next status records the same send again; the ledger must not charge it twice."""
    state, path = _state(), tmp_path / "seat.json"
    _uncertain_large_op(path, state, "crash", monkeypatch)
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(_stored(path, "crash")))
    assert node.op_status(path, state, "crash")["state"] == "succeeded"
    key = _stored(path, "crash")["key"]
    real_write = node._op_write

    def crash_on_counted(where, entry):
        if entry.get("guard_counted"):
            raise KeyboardInterrupt("killed after the ledger write")
        return real_write(where, entry)

    monkeypatch.setattr(node, "_op_write", crash_on_counted)
    with pytest.raises(KeyboardInterrupt):
        cli._record_op_sendguard(path, "crash")
    monkeypatch.setattr(node, "_op_write", real_write)
    assert node.op_uncounted_successes(path) == ["crash"]

    assert cli._record_op_sendguard(path, "crash") == (True, None)

    ledger = json.loads(sendguard.ledger_path(path).read_text(encoding="utf-8"))
    assert [entry["to"] for entry in ledger if entry["key"] == key] == ["nxxxxx"]
    tpath = sendguard.token_ledger_path(path)
    tokens = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else []
    assert len([entry for entry in tokens if entry.get("to") == "nxxxxx"]) <= 1
    assert node.op_uncounted_successes(path) == []


@pytest.mark.parametrize("ack", ["", "   "])
def test_op_send_with_an_empty_bulk_ack_is_a_json_refusal_not_a_traceback(tmp_path, monkeypatch, capsys, ack):
    """GitHub Codex on ad4f6030 (pxx 70f7c9): --op-id with an empty --bulk-ack refuses like the plain send path."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    base = ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(path), "--thread", "pr2/test",
            "--to", "nxxxxx", "--body", "hello", "--bulk-ack", ack, "--no-recipient-check", "--no-card-check"]

    plain = cli.main(base)
    plain_err = capsys.readouterr().err
    code = cli.main(base + ["--op-id", "empty-ack"])
    captured = capsys.readouterr()

    assert code == plain == 2 and captured.out == ""
    assert "non-empty reason" in json.loads(captured.err)["refused"]
    assert json.loads(captured.err) == json.loads(plain_err)
    assert writes == [] and not Path(f"{path}.ops").joinpath("empty-ack.json").exists()


def test_op_bulk_ack_is_journalled_and_reused_for_drain_admission(tmp_path, monkeypatch):
    """The override is routing metadata, retained for every presentation but outside the message fingerprint."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    _six_large_sends(path)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    reason = "the peer asked for this batch by name"
    send = ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(path), "--thread", "pr2/test",
            "--to", "nxxxxx", "--body", "x" * 4096, "--op-id", "acknowledged", "--bulk-ack", reason,
            "--no-recipient-check", "--no-card-check"]

    assert cli.main(send) == 0
    entry = _stored(path, "acknowledged")
    assert entry["bulk_ack"] == reason
    entry["state"] = "uncertain"
    node._op_write(path, entry)
    assert cli.main(["send", "drain", "--base-url", "http://127.0.0.1:1", "--state-file", str(path)]) == 0
    assert len(writes) == 2

    bare_path = tmp_path / "bare.json"
    _six_large_sends(bare_path)
    bare = node.op_send(bare_path, state, op_id="unacknowledged", to=["nxxxxx"], thread_id="pr2/test",
                        body="x" * 4096, msg_type="note", author_family="test",
                        admit=lambda value, bulk_ack: cli._admit_op_sendguard(bare_path, value, bulk_ack))
    assert bare["state"] == "refused-by-guard" and len(writes) == 2


def test_cli_new_guard_refusal_exits_like_a_plain_guard_refusal(tmp_path, monkeypatch, capsys):
    state, path, writes = _state(), tmp_path / "seat.json", []
    _six_large_sends(path)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))

    code = cli.main(["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(path),
                     "--thread", "pr2/test", "--to", "nxxxxx", "--body", "x" * 4096,
                     "--op-id", "newly-refused", "--no-recipient-check", "--no-card-check"])

    captured = capsys.readouterr()
    assert code == 2 and captured.out == "" and "--bulk-ack" in json.loads(captured.err)["refused"]
    assert writes == [] and _stored(path, "newly-refused")["state"] == node.OP_HELD


def test_read_back_of_an_already_stored_op_needs_no_guard_admission(tmp_path, monkeypatch):
    state, path = _state(), tmp_path / "seat.json"
    _uncertain_large_op(path, state, "read-back-no-guard", monkeypatch)
    entry = _stored(path, "read-back-no-guard")
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(entry))

    got = node.op_status(path, state, "read-back-no-guard",
                         admit=lambda _value, _bulk_ack: pytest.fail("read-back must settle before guard admission"))

    assert got["state"] == "succeeded" and got["already_sent"] is True


def test_failed_call_with_written_value_is_not_resigned(tmp_path, monkeypatch):
    """Acceptance 3: a failed call is reconciled before any retry is signed."""
    state, calls = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or {"status": "failed"})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    _send(tmp_path / "seat.json", state, "failed-written")
    entry = _stored(tmp_path / "seat.json", "failed-written")
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(entry))
    got = node.op_status(tmp_path / "seat.json", state, "failed-written")
    assert got["already_sent"] is True and len(calls) == 1
    assert _stored(tmp_path / "seat.json", "failed-written")["call_id"] == entry["call_id"]


def test_reconcile_requires_the_exact_stored_value_bytes(tmp_path, monkeypatch):
    """The same key is not enough: a different version-one value is not this operation."""
    state, calls = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or {"status": "failed"})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    _send(tmp_path / "seat.json", state, "different-value")
    entry = _stored(tmp_path / "seat.json", "different-value")
    other = {**entry["value"], "body": "not the signed operation"}
    monkeypatch.setattr(node, "read", lambda *a, **k: {**_matching_read(entry), "_value": other})
    got = node.op_status(tmp_path / "seat.json", state, "different-value")
    assert got["already_sent"] is False and got["state"] == "uncertain" and len(calls) == 2


def test_missing_value_after_window_resigns_same_key_once(tmp_path, monkeypatch):
    """Acceptance 4: only the call identity changes after the envelope window expires."""
    state, calls = _state(), []
    clock = [node.datetime.fromtimestamp(0, node.timezone.utc)]
    monkeypatch.setattr(node, "_op_now", lambda: clock[0])
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or {"status": "failed"})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    first = _send(tmp_path / "seat.json", state, "missing")
    old = _stored(tmp_path / "seat.json", "missing")
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(node.ReadFailed({"error": "E_MISSING"})))
    clock[0] = node.datetime.fromtimestamp(130, node.timezone.utc)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or _ok(k["args"]["key"]))
    got = node.op_status(tmp_path / "seat.json", state, "missing")
    new = _stored(tmp_path / "seat.json", "missing")
    again = node.op_status(tmp_path / "seat.json", state, "missing")
    assert got["key"] == first["key"] == new["key"] and new["call_id"] != old["call_id"]
    assert again["already_sent"] is True and len(calls) == 2


def test_two_drainers_and_a_retry_share_one_locked_operation(tmp_path, monkeypatch):
    """Acceptance 5: concurrent callers serialize on the operation lock."""
    state, calls, notes = _state(), [], []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or _ok(k["args"]["key"]))
    monkeypatch.setattr(node, "note_sent", lambda *a: notes.append(a[2]) or True)
    _send(tmp_path / "seat.json", state, "race")
    assert node.op_drain(tmp_path / "seat.json", state)["succeeded"] == 1
    assert _send(tmp_path / "seat.json", state, "race")["already_sent"] is True
    assert len(calls) == 1 and len(notes) == 1


def test_drain_bounds_each_op_and_reaches_a_healthy_later_op(tmp_path, monkeypatch):
    state, seen = _state(), []
    for op_id in ("first", "second"):
        folder = Path(f"{tmp_path / 'seat.json'}.ops")
        folder.mkdir(exist_ok=True)
        (folder / f"{op_id}.json").write_text(json.dumps({"schema": 1, "op_id": op_id, "state": "uncertain"}), encoding="utf-8")
    def settle(_path, _state, op_id, *, poll_timeout):
        seen.append((op_id, poll_timeout))
        return {"state": "uncertain" if op_id == "first" else "succeeded"}
    monkeypatch.setattr(node, "op_status", settle)
    got = node.op_drain(tmp_path / "seat.json", state, poll_timeout=40)
    assert got["succeeded"] == 1 and seen == [("first", node.DRAIN_OP_TIMEOUT_S), ("second", node.DRAIN_OP_TIMEOUT_S)]


def test_drain_rotates_after_its_bounded_share(tmp_path, monkeypatch):
    state, seen = _state(), []
    folder = Path(f"{tmp_path / 'seat.json'}.ops")
    folder.mkdir()
    for number in range(node.DRAIN_MAX_OPS + 1):
        (folder / f"op{number}.json").write_text(json.dumps({"schema": 1, "op_id": f"op{number}", "state": "uncertain"}), encoding="utf-8")
    monkeypatch.setattr(node, "op_status", lambda _p, _s, op_id, **kw: seen.append(op_id) or {"state": "uncertain"})
    node.op_drain(tmp_path / "seat.json", state)
    node.op_drain(tmp_path / "seat.json", state)
    assert seen[:node.DRAIN_MAX_OPS] == [f"op{n}" for n in range(node.DRAIN_MAX_OPS)]
    assert seen[node.DRAIN_MAX_OPS] == f"op{node.DRAIN_MAX_OPS}"


def _pending_entry(path, state, op_id):
    entry = node._op_entry(path, state, op_id=op_id, to=["nxxxxx"], thread_id="pr2/test", body="hello",
                           msg_type="note", author_family="test")
    node._op_write(path, entry)
    return entry


def test_drain_deadline_bounds_the_post_and_continues_to_the_next_op(tmp_path, monkeypatch):
    state, path, clock, calls = _state(), tmp_path / "seat.json", [100.0], []
    first = _pending_entry(path, state, "first")
    second = _pending_entry(path, state, "second")
    monkeypatch.setattr(node.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))

    def invoke(_path, _state, **kw):
        calls.append((kw["args"]["key"], node._FINISH_BY.get()))
        if kw["args"]["key"] == first["key"]:
            clock[0] += node.DRAIN_OP_TIMEOUT_S + 0.1
            raise node.DeadlinePassed("POST ran past this operation's share")
        return _ok(second["key"])
    monkeypatch.setattr(node, "invoke", invoke)

    got = node.op_drain(path, state, poll_timeout=40)

    assert got["uncertain_ids"] == ["first"] and got["succeeded"] == 1
    assert [key for key, _deadline in calls] == [first["key"], second["key"]]
    assert all(deadline is not None for _key, deadline in calls)


def test_drain_saves_the_cursor_before_an_interrupted_op(tmp_path, monkeypatch):
    state, path, seen = _state(), tmp_path / "seat.json", []
    _pending_entry(path, state, "first")
    _pending_entry(path, state, "second")
    monkeypatch.setattr(node, "op_status", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("child killed")))

    with pytest.raises(KeyboardInterrupt, match="child killed"):
        node.op_drain(path, state)

    cursor = Path(f"{path}.ops") / ".drain-cursor.json"
    assert json.loads(cursor.read_text(encoding="utf-8"))["op_id"] == "first"
    monkeypatch.setattr(node, "op_status", lambda _p, _s, op_id, **kw: seen.append(op_id) or {"state": "uncertain"})
    node.op_drain(path, state)
    assert seen[0] == "second"


def test_key_already_written_is_never_refused_even_when_the_read_back_fails(tmp_path, monkeypatch):
    """A re-presentation of a message that was written gets E_USE_MEMORY_EDIT (the key holds version 1). When the
    read-back then fails too, the op must stay uncertain and be read again, never marked refused."""
    state, calls = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or
                        {"status": "failed", "error_code": "E_USE_MEMORY_EDIT", "result_signature_verified": True})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(node.ReadFailed({"error": node.E_UNREACHABLE})))
    got = _send(tmp_path / "seat.json", state, "written")
    assert got["state"] == "uncertain"
    assert node.op_status(tmp_path / "seat.json", state, "written")["state"] != "refused"


def test_refused_operation_stays_terminal_and_drain_does_not_retry(tmp_path, monkeypatch):
    """Acceptance 6: a typed refusal is visible and never retried by the drain."""
    state, calls = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or
                        {"status": "failed", "error_code": "E_GUARD", "result_signature_verified": True})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    got = _send(tmp_path / "seat.json", state, "refused")
    assert got["state"] == "refused"
    assert _send(tmp_path / "seat.json", state, "refused")["state"] == "refused"
    assert node.op_status(tmp_path / "seat.json", state, "refused")["state"] == "refused"
    assert node.op_drain(tmp_path / "seat.json", state)["refused"] == 1 and len(calls) == 1


def test_peek_reads_the_journal_only_and_never_calls_reads_or_writes(tmp_path, monkeypatch, capsys):
    """#549 (codex-oxxxxxx-cli 29cd67): op_status may present an unsettled op again, so `send-status --peek` reads the
    journal and nothing else: no server call, no read, no write, and no state file. A settled op answers as op_status
    does. MUTANT: peek reconciles."""
    state, calls = _state(), []
    path = tmp_path / "seat.json"
    entry = _pending_entry(path, state, "pending")
    journal = Path(f"{path}.ops") / "pending.json"
    before = journal.read_bytes()
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append("invoke") or _ok(k["args"]["key"]))
    monkeypatch.setattr(node, "read", lambda *a, **k: calls.append("read") or _matching_read(entry))
    got = node.op_peek(path, "pending")
    assert got == {"op_id": "pending", "key": entry["key"], "state": entry["state"], "already_sent": None,
                   "peek": True}
    assert calls == [] and journal.read_bytes() == before
    assert cli.main(["send-status", "--peek", "--base-url", "http://127.0.0.1:1", "--state-file", str(path),
                     "pending"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == entry["state"]
    assert calls == [] and journal.read_bytes() == before and not path.exists(), "no call, no write, no state file"
    _send(path, state, "done")                                    # settled through op_send
    assert node.op_peek(path, "done")["state"] == "succeeded" and node.op_peek(path, "done")["already_sent"] is True
    with pytest.raises(node.Refused):
        node.op_peek(path, "never")


def test_an_unverified_failed_result_stays_uncertain_and_status_reads_again(tmp_path, monkeypatch):
    state, reads = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {"status": "failed", "error_code": "E_GUARD",
                                                           "result_signature_verified": False})
    monkeypatch.setattr(node, "read", lambda *a, **k: reads.append(k) or (_ for _ in ()).throw(_absent()))
    assert _send(tmp_path / "seat.json", state, "unverified-no")["state"] == "uncertain"
    assert node.op_status(tmp_path / "seat.json", state, "unverified-no")["state"] == "uncertain"
    assert len(reads) >= 2


_TIMEOUT = {"status": "timeout", "error_code": "E_DEADLINE_EXCEEDED", "result_signature_verified": True}


def test_deadline_swept_call_with_written_value_is_already_sent(tmp_path, monkeypatch):
    """The deadline sweep's real shape (apps/calls/janitor.py: status timeout, E_DEADLINE_EXCEEDED) with the
    message row present: the first presentation reads the key back and reports it sent, never refused."""
    state, calls = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k) or dict(_TIMEOUT))
    monkeypatch.setattr(node, "note_sent", lambda *a: True)
    holder = {}
    real_present = node._op_present

    def present(path, state, entry, **kw):
        holder["entry"] = dict(entry)
        return real_present(path, state, entry, **kw)
    monkeypatch.setattr(node, "_op_present", present)
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(holder["entry"]))
    got = _send(tmp_path / "seat.json", state, "swept-written")
    again = _send(tmp_path / "seat.json", state, "swept-written")
    assert got["state"] == "succeeded" and node.succeeded(got) and node.op_storage_proven(got) and len(calls) == 1
    assert again["state"] == "succeeded" and node.succeeded(again) and again["status"] == "completed"
    saved = _stored(tmp_path / "seat.json", "swept-written")
    assert saved["state"] == "succeeded" and saved["storage_receipt"]["proven_by"] == "read_back"


def test_status_reconciliation_uses_a_receipt_not_a_missing_result(tmp_path, monkeypatch):
    state = _state()
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {"status": "failed"})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    _send(tmp_path / "seat.json", state, "later")
    entry = _stored(tmp_path / "seat.json", "later")
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(entry))
    assert node.op_status(tmp_path / "seat.json", state, "later")["state"] == "succeeded"
    retry = _send(tmp_path / "seat.json", state, "later")
    assert retry["state"] == "succeeded" and node.succeeded(retry) and retry["status"] == "completed"


def test_deadline_swept_call_without_row_stays_retryable(tmp_path, monkeypatch):
    """The same sweep with no row: the operation is uncertain (retried by status and the drain), not refused."""
    state = _state()
    monkeypatch.setattr(node, "invoke", lambda *a, **k: dict(_TIMEOUT))
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    got = _send(tmp_path / "seat.json", state, "swept-absent")
    assert got["state"] == "uncertain"
    assert _stored(tmp_path / "seat.json", "swept-absent")["state"] == "uncertain"


@pytest.mark.parametrize("wrong", [{"key": "msg/other"}, {"version": 2}])
def test_direct_success_with_wrong_receipt_stays_uncertain(tmp_path, monkeypatch, wrong):
    state = _state()
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {**_ok(k["args"]["key"]),
                        "result": {"ok": True, "key": wrong.get("key", k["args"]["key"]), "version": wrong.get("version", 1)}})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    assert _send(tmp_path / "seat.json", state, "wrong-direct")["state"] == "uncertain"


def test_resign_window_counts_from_the_last_signing(tmp_path, monkeypatch):
    """A re-signed envelope gets a fresh 120 s window: a reconcile right after a re-sign presents the same
    call id again instead of signing a third envelope."""
    state, calls = _state(), []
    monkeypatch.setattr(node, "invoke", lambda *a, **k: calls.append(k["envelope"]["payload"]["call_id"])
                        or {"status": "failed"})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    _send(tmp_path / "seat.json", state, "window")
    file = Path(f"{tmp_path / 'seat.json'}.ops") / "window.json"
    entry = json.loads(file.read_text(encoding="utf-8"))
    entry["created_at"] = entry["signed_at"] = "2020-01-01T00:00:00+00:00"
    file.write_text(json.dumps(entry), encoding="utf-8")
    node.op_status(tmp_path / "seat.json", state, "window")
    node.op_status(tmp_path / "seat.json", state, "window")
    assert len(calls) == 3 and calls[1] != calls[0] and calls[2] == calls[1]


def test_resign_keeps_the_original_capability(tmp_path, monkeypatch):
    """A re-signed envelope carries the capability the first one carried."""
    state, seen = _state(), []
    monkeypatch.setattr(node.wire, "call_envelope",
                        lambda **kw: seen.append(kw.get("capability")) or {"payload": {"call_id": f"c{len(seen)}"}})
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {"status": "failed"})
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    node.op_send(tmp_path / "seat.json", state, op_id="cap", to=["nxxxxx"], thread_id="pr2/test", body="hi",
                 msg_type="note", author_family="test", capability="cap-token")
    file = Path(f"{tmp_path / 'seat.json'}.ops") / "cap.json"
    entry = json.loads(file.read_text(encoding="utf-8"))
    entry["signed_at"] = "2020-01-01T00:00:00+00:00"
    file.write_text(json.dumps(entry), encoding="utf-8")
    node.op_status(tmp_path / "seat.json", state, "cap")
    assert seen == ["cap-token", "cap-token"]


def _cli_send(path, op_id, *extra):
    return cli.main(["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(path),
                     "--thread", "pr2/test", "--to", "nxxxxx", "--body", "x" * 4096,
                     "--op-id", op_id, "--no-recipient-check", "--no-card-check", *extra])


def test_a_new_op_refused_on_its_first_send_is_never_sent_by_drain_or_the_postbox(tmp_path, monkeypatch, capsys):
    """pxx 8af206 #1: the user was told it was refused, so nothing sends it later on its own."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    _six_large_sends(path)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    assert _cli_send(path, "told-refused") == 2
    sendguard.ledger_path(path).unlink()                  # the window moves: the guard would admit it now
    drain = ["send", "drain", "--base-url", "http://127.0.0.1:1", "--state-file", str(path)]
    assert cli.main(drain) == 0 and cli.main(drain) == 0
    assert writes == [] and _stored(path, "told-refused")["state"] == node.OP_HELD
    status = node.op_status(path, state, "told-refused", admit=_admit_from_journal(path))
    assert status["state"] == node.OP_HELD and writes == []
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "postbox_held_under_test", Path(__file__).resolve().parent.parent / "tools" / "postbox" / "postbox.py")
    postbox = importlib.util.module_from_spec(spec)
    if "msvcrt" not in sys.modules:
        try:
            import msvcrt  # noqa: F401
        except ImportError:
            import types
            monkeypatch.setitem(sys.modules, "msvcrt", types.ModuleType("msvcrt"))
    spec.loader.exec_module(postbox)
    assert postbox._pending_send_ops({"state": str(path)}) is False
    capsys.readouterr()


def test_bulk_ack_on_a_retry_sends_a_held_op_once(tmp_path, monkeypatch, capsys):
    """pxx 8af206 #2: the refusal says to pass --bulk-ack; a retry carrying it must be admitted and sent once."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    _six_large_sends(path)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    assert _cli_send(path, "ack-later") == 2 and writes == []
    assert _cli_send(path, "ack-later") == 2 and writes == []        # retried without it: still refused
    assert _cli_send(path, "ack-later", "--bulk-ack", "one review bundle") == 0
    assert len(writes) == 1 and _stored(path, "ack-later")["state"] == "succeeded"
    assert _stored(path, "ack-later")["bulk_ack"] == "one review bundle"
    assert _cli_send(path, "ack-later", "--bulk-ack", "one review bundle") == 0 and len(writes) == 1  # same op: stored
    # Without the override it is a different message under a sent op id: refused, nothing sent, nothing removed.
    assert _cli_send(path, "ack-later") == 2 and len(writes) == 1
    assert _stored(path, "ack-later")["bulk_ack"] == "one review bundle"
    capsys.readouterr()


def test_a_drain_ledger_write_failure_is_reported_not_swallowed(tmp_path, monkeypatch, capsys):
    """pxx 8af206 minor: a failed ledger write during drain prints send_not_recorded."""
    state, path = _state(), tmp_path / "seat.json"
    monkeypatch.setattr(cli, "_record_op_sendguard", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    cli._record_drain_sendguard(path, "some-op")
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["warning"] == "send_not_recorded" and line["op_id"] == "some-op" and "disk full" in line["reason"]


@pytest.mark.parametrize("extra", [(), ("--bulk-ack", "a reviewed bundle")])
def test_a_plain_send_without_op_id_reaches_the_real_node_send(tmp_path, monkeypatch, capsys, extra):
    """GitHub Codex r4360487372 (P1): bulk_ack in the shared kwargs made every plain send raise TypeError before
    sending. Only invoke is stubbed, so the real node.send signature is exercised."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    code = cli.main(["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(path), "--thread", "pr2/test",
                     "--to", "nxxxxx", "--body", "plain", "--no-recipient-check", "--no-card-check", *extra])
    assert code == 0 and len(writes) == 1
    capsys.readouterr()


def test_send_status_through_the_cli_uses_the_real_signatures(tmp_path, monkeypatch, capsys):
    """kxxx bca0b0: every send path has a test through cli.main with the real node functions, stubbing only the
    network (invoke and read). Status: a held op is reported, not presented; an uncertain op the read-back finds is
    settled with no new write."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    _six_large_sends(path)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    assert _cli_send(path, "held-status") == 2
    status = ["send", "status", "held-status", "--base-url", "http://127.0.0.1:1", "--state-file", str(path)]
    assert cli.main(status) == 1 and writes == []
    assert json.loads(capsys.readouterr().out)["state"] == node.OP_HELD

    sendguard.ledger_path(path).unlink()
    _uncertain_large_op(path, state, "landed", monkeypatch)
    entry = _stored(path, "landed")
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(entry))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    status = ["send", "status", "landed", "--base-url", "http://127.0.0.1:1", "--state-file", str(path)]
    assert cli.main(status) == 0 and writes == []
    assert _stored(path, "landed")["state"] == "succeeded"
    capsys.readouterr()


def test_a_capability_op_presents_its_token_through_the_real_invoke(tmp_path, monkeypatch):
    """GitHub Codex r4360868437 (pxx e11bd9): the envelope carries capability_sha256, so the POST must carry the
    token in the capability header or the op fails on every attempt. Real op_send and invoke; only the HTTP layer
    (and the sign-in token) is stubbed."""
    from tools.meshy_node import wire
    state, path, seen = {**_state(), "base_url": "http://127.0.0.1:1"}, tmp_path / "seat.json", []
    monkeypatch.setattr(node, "require_capability_support", lambda base_url: None)
    monkeypatch.setattr(node, "fresh_jwt", lambda *a, **k: "header.payload.sig")

    def http_json(method, base_url, route, headers=None, body=None, **kwargs):
        seen.append((method, route, dict(headers or {})))
        raise OSError("stop after the POST")

    monkeypatch.setattr(node, "http_json", http_json)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    with pytest.raises(OSError):
        node.op_send(path, state, op_id="cap-op", to=["nxxxxx"], thread_id="pr2/test", body="hi",
                     msg_type="note", author_family="test", capability="cap-token-123")
    posts = [h for m, r, h in seen if m == "POST" and r.endswith("/invoke")]
    assert posts and posts[0].get(wire.CAPABILITY_HEADER) == "cap-token-123"


def test_drain_does_not_prune_a_journal_whose_op_lock_is_held(tmp_path, monkeypatch, capsys):
    """GitHub Codex r4360868454 (pxx e11bd9, kxxx c19eee): pruning takes the op lock; a busy op is skipped and
    reported, and its journal survives."""
    import os
    import time
    state, path = _state(), tmp_path / "seat.json"
    _six_large_sends(path)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: _ok(k["args"]["key"]))
    assert _cli_send(path, "busy-held") == 2                               # a held op
    journal = node._op_file(path, "busy-held")
    old = time.time() - node.OPS_RETENTION_S - 60
    os.utime(journal, (old, old))                                          # past retention
    lock = node._op_lock(path, "busy-held")                                # a retry holds it right now
    try:
        counts = node.op_drain(path, state)
    finally:
        node._sidecar_unlock(lock)
    assert journal.exists() and counts["busy_ids"] == ["busy-held"]
    counts = node.op_drain(path, state)                                    # lock free: pruned as before
    assert not journal.exists() and counts["busy_ids"] == []
    capsys.readouterr()


def _uncertain_op_with_ack(path, state, op_id, bulk_ack, monkeypatch):
    monkeypatch.setattr(node, "invoke", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("before storage")))
    with pytest.raises(TimeoutError):
        node.op_send(path, state, op_id=op_id, to=["nxxxxx"], thread_id="pr2/test", body="x" * 4096,
                     msg_type="note", author_family="test", bulk_ack=bulk_ack)


@pytest.mark.parametrize("first, retry", [(None, "the peer asked for this batch"),
                                          ("the peer asked for this batch", None),
                                          ("the peer asked for this batch", "a different reason")])
def test_a_retry_that_changes_the_bulk_ack_override_is_refused(tmp_path, monkeypatch, first, retry):
    """codex-pxx-cli's retry table (pxx 6ed9c9): the requested override is part of the operation."""
    state, path = _state(), tmp_path / "seat.json"
    _uncertain_op_with_ack(path, state, "ack-change", first, monkeypatch)
    with pytest.raises(node.Refused, match="different message arguments"):
        node.op_send(path, state, op_id="ack-change", to=["nxxxxx"], thread_id="pr2/test", body="x" * 4096,
                     msg_type="note", author_family="test", bulk_ack=retry)


def test_the_same_bulk_ack_override_retries_even_with_other_whitespace(tmp_path, monkeypatch):
    state, path, writes = _state(), tmp_path / "seat.json", []
    _uncertain_op_with_ack(path, state, "ack-same", "the peer asked  for this batch", monkeypatch)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    node.op_send(path, state, op_id="ack-same", to=["nxxxxx"], thread_id="pr2/test", body="x" * 4096,
                 msg_type="note", author_family="test", bulk_ack=" the peer asked for this batch ")
    assert len(writes) == 1


def test_a_journal_fingerprinted_before_the_override_joined_still_retries(tmp_path, monkeypatch):
    """An op journalled by the previous client (fingerprint without bulk_ack) retries when the override is unchanged,
    and is refused when it changed: refusing an unchanged one would push the caller to a new op id and a duplicate."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    _uncertain_op_with_ack(path, state, "legacy", "asked by name", monkeypatch)
    entry = _stored(path, "legacy")
    entry["fingerprint"] = node._op_fingerprint_for_args(to=["nxxxxx"], thread_id="pr2/test", msg_type="note",
                                                         body="x" * 4096, sender=entry["value"]["from"],
                                                         author_family="test")
    node._op_write(path, entry)
    with pytest.raises(node.Refused):
        node.op_send(path, state, op_id="legacy", to=["nxxxxx"], thread_id="pr2/test", body="x" * 4096,
                     msg_type="note", author_family="test", bulk_ack="changed")
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    node.op_send(path, state, op_id="legacy", to=["nxxxxx"], thread_id="pr2/test", body="x" * 4096,
                 msg_type="note", author_family="test", bulk_ack="asked by name")
    assert len(writes) == 1


@pytest.mark.parametrize("legacy", [True, False])
def test_a_retry_that_drops_the_override_is_refused_on_old_and_new_journals(tmp_path, monkeypatch, legacy):
    """GitHub Codex r4366738272 (pxx f17bc0): a retry without --bulk-ack must not keep the journalled override."""
    state, path, writes = _state(), tmp_path / "seat.json", []
    _uncertain_op_with_ack(path, state, "dropped", "asked by name", monkeypatch)
    if legacy:
        entry = _stored(path, "dropped")
        entry["fingerprint"] = node._op_fingerprint_for_args(to=["nxxxxx"], thread_id="pr2/test", msg_type="note",
                                                             body="x" * 4096, sender=entry["value"]["from"],
                                                             author_family="test")
        node._op_write(path, entry)
    monkeypatch.setattr(node, "read", lambda *a, **k: (_ for _ in ()).throw(_absent()))
    monkeypatch.setattr(node, "invoke", lambda *a, **k: writes.append(k) or _ok(k["args"]["key"]))
    with pytest.raises(node.Refused, match="different message arguments"):
        node.op_send(path, state, op_id="dropped", to=["nxxxxx"], thread_id="pr2/test", body="x" * 4096,
                     msg_type="note", author_family="test", bulk_ack=None)
    assert writes == []



def _proven_large_op_signed_ago(path, state, op_id, minutes, monkeypatch):
    """A large op whose storage is proven now, but whose envelope was signed `minutes` ago."""
    _uncertain_large_op(path, state, op_id, monkeypatch)
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(_stored(path, op_id)))
    assert node.op_status(path, state, op_id)["state"] == "succeeded"
    entry = _stored(path, op_id)
    then = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    entry["signed_at"] = entry["created_at"] = then
    (Path(f"{path}.ops") / f"{op_id}.json").write_text(json.dumps(entry), encoding="utf-8")
    return entry["key"]


def test_a_success_counted_after_the_window_is_not_charged_now(tmp_path, monkeypatch):
    """GitHub Codex r4366980594 (pxx eb89c1): a success journalled 61 minutes ago and counted now leaves no entry in
    the window, and the next large send to the same recipient is not refused because of it."""
    state, path = _state(), tmp_path / "seat.json"
    for number in range(sendguard.MAX_PER_RECIPIENT - 1):
        sendguard.record(path, to=["nxxxxx"], cc=None, key=f"msg/{number}", body_bytes=4096, body="x" * 4096)
    key = _proven_large_op_signed_ago(path, state, "late", 61, monkeypatch)

    assert cli._record_op_sendguard(path, "late") == (True, None)

    ledger = json.loads(sendguard.ledger_path(path).read_text(encoding="utf-8"))
    assert [entry for entry in ledger if entry["key"] == key] == []
    assert node.op_uncounted_successes(path) == []
    assert sendguard.refusals(path, to=["nxxxxx"], cc=None, body="y" * 4096) == []


def test_a_success_counted_late_inside_the_window_is_stamped_with_its_send_time(tmp_path, monkeypatch):
    state, path = _state(), tmp_path / "seat.json"
    key = _proven_large_op_signed_ago(path, state, "recent", 10, monkeypatch)
    sent = datetime.fromisoformat(_stored(path, "recent")["signed_at"]).timestamp()

    assert cli._record_op_sendguard(path, "recent") == (True, None)

    stamped = [entry["t"] for entry in json.loads(sendguard.ledger_path(path).read_text(encoding="utf-8"))
               if entry["key"] == key]
    assert stamped == [pytest.approx(sent, abs=1)]



def test_a_new_ops_first_write_fsyncs_the_file_then_its_directory_before_presenting(tmp_path, monkeypatch):
    """GitHub Codex r4367067494 (pxx 7ad7ff): on POSIX the journal's name is made durable before the first
    presentation, and so is the ops directory the first op creates. Windows skips directory fsync; the platform
    check is forced on here so the order is checked on every runner."""
    state, path = _state(), tmp_path / "seat.json"
    events = []
    real_open, real_fsync, real_close = os.open, os.fsync, os.close
    dir_fds = {}

    def open_(name, flags, *args, **kwargs):
        if os.path.isdir(name):
            fd = 10_000_000 + len(dir_fds)
            dir_fds[fd] = Path(name)
            return fd
        return real_open(name, flags, *args, **kwargs)

    def fsync_(fd):
        if fd in dir_fds:
            events.append(("dir", dir_fds[fd]))
        else:
            events.append(("file", None))
            real_fsync(fd)

    def close_(fd):
        if fd not in dir_fds:
            real_close(fd)

    monkeypatch.setattr(node, "_dir_fsync_supported", lambda: True, raising=False)
    monkeypatch.setattr(node.os, "open", open_)
    monkeypatch.setattr(node.os, "fsync", fsync_)
    monkeypatch.setattr(node.os, "close", close_)

    def invoke(*args, **kwargs):
        events.append(("invoke", None))
        raise TimeoutError("before storage")

    monkeypatch.setattr(node, "invoke", invoke)
    with pytest.raises(TimeoutError):
        node.op_send(path, state, op_id="durable", to=["nxxxxx"], thread_id="pr2/test", body="hello",
                     msg_type="note", author_family="test")
    ops_dir = Path(f"{path}.ops")
    first_invoke = events.index(("invoke", None))
    before = events[:first_invoke]
    assert ("dir", tmp_path) in before                    # the ops directory's own entry
    file_at = before.index(("file", None))
    assert ("dir", ops_dir) in before[file_at + 1:]       # the journal's name, after its bytes



def _load_postbox(monkeypatch, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent.parent / "tools" / "postbox" / "postbox.py")
    postbox = importlib.util.module_from_spec(spec)
    if "msvcrt" not in sys.modules:
        try:
            import msvcrt  # noqa: F401
        except ImportError:
            import types
            monkeypatch.setitem(sys.modules, "msvcrt", types.ModuleType("msvcrt"))
    spec.loader.exec_module(postbox)
    return postbox


def test_an_uncounted_success_keeps_the_postbox_drain_running_until_counted(tmp_path, monkeypatch):
    """#392 (GitHub Codex r4367106926 on #372): a send that exited between the journal's "succeeded" and the guard
    write leaves guard_counted unset. With no other op pending, the postbox must still run `send drain`, whose
    uncounted-success pass counts it once; after that the journal is settled and the drain stops."""
    state, path = _state(), tmp_path / "seat.json"
    _uncertain_large_op(path, state, "late", monkeypatch)
    monkeypatch.setattr(node, "read", lambda *a, **k: _matching_read(_stored(path, "late")))
    assert node.op_status(path, state, "late")["state"] == "succeeded"
    assert node.op_uncounted_successes(path) == ["late"]  # the crash window: succeeded, guard not counted yet

    postbox = _load_postbox(monkeypatch, "postbox_uncounted_under_test")
    ident = {"name": "seat", "state": str(path), "python": sys.executable, "base_url": "http://127.0.0.1:1",
             "client": str(tmp_path)}
    assert postbox._pending_send_ops(ident) is True
    ran = []

    def fake_run(cmd, **kwargs):
        ran.append(cmd)
        assert cli.main(cmd[cmd.index("send"):]) == 0     # the real client's drain, in-process
        return subprocess.CompletedProcess(cmd, 0, b'{"refused": 0, "uncertain": 0}\n', b"")

    monkeypatch.setattr(postbox.subprocess, "run", fake_run)
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    postbox.drain_send_operations(ident)
    assert len(ran) == 1 and ran[0][ran[0].index("send") + 1] == "drain"
    assert node.op_uncounted_successes(path) == []
    ledger = json.loads(sendguard.ledger_path(path).read_text(encoding="utf-8"))
    assert [entry["to"] for entry in ledger if entry["key"] == _stored(path, "late")["key"]] == ["nxxxxx"]
    assert postbox._pending_send_ops(ident) is False      # counted: settled, the drain stops
    postbox.drain_send_operations(ident)
    assert len(ran) == 1
