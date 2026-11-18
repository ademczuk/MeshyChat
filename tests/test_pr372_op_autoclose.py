"""PR #372 keeps an operation's automatic close decision stable across retries."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from tools.meshy_node import cli, node  # noqa: E402


QUESTION = "msg/1790000000000-heron-question"


def _state():
    return {"peer_id": "00000000-0000-0000-0000-000000000001", "label": "uxxx", "status": "minted",
            "kid": "test-kid", "signing_seed_b64url": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "memory_peer_id": "29d6a9f7-ed27-5153-a9f2-f6feb3175e77"}


def _ok(key):
    return {"key": key, "status": "completed", "result": {"ok": True, "key": key, "version": 1},
            "result_signature_verified": True}


def _entry(path, op_id):
    return json.loads((Path(f"{path}.ops") / f"{op_id}.json").read_text(encoding="utf-8"))


def _matching_read(entry):
    return {"key": entry["key"], "version": 1, "_value": entry["value"],
            "signature_verified": True, "status_verified": True}


def _send(path, op_id):
    return ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(path), "--to", "heron",
            "--thread", "ops/pr372", "--type", "answer", "--in-reply-to", QUESTION, "--body", "settled",
            "--op-id", op_id, "--no-recipient-check", "--no-card-check"]


def _owed(open_question):
    return {"owed": ([{"hit": {"key": QUESTION, "version": 1}, "thread_id": "ops/pr372",
                       "state": "open", "msg_type": "question"}] if open_question["value"] else [])}


def _owed_key(open_question):
    """node.owed_key's single-key answer (#407): the auto-close lookup no longer reads the owed list."""
    def answer(*_a, key=None, **_k):
        return {"key": key, "owed": _owed(open_question)["owed"] if key == QUESTION else []}
    return answer


def _drop_reply_after_storage(path, monkeypatch):
    """Start an op whose write happened but whose caller lost the answer."""
    state, calls, open_question = _state(), [], {"value": True}
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "ledger_records", lambda *a, **k: [{"key": QUESTION, "version": 1}])
    monkeypatch.setattr(node, "owed", lambda *a, **k: _owed(open_question))
    monkeypatch.setattr(node, "owed_key", _owed_key(open_question))
    monkeypatch.setattr(node, "note_sent", lambda *a, **k: True)

    def lost_reply(*_args, **kwargs):
        calls.append(kwargs["args"]["value"])
        open_question["value"] = False
        raise TimeoutError("stored, but the reply was dropped")

    monkeypatch.setattr(node, "invoke", lost_reply)
    assert cli.main(_send(path, "dropped")) == 2
    entry = _entry(path, "dropped")
    assert entry["value"]["closes"] == [QUESTION]

    monkeypatch.setattr(node, "read", lambda *_a, **_k: _matching_read(_entry(path, "dropped")))
    return calls, entry


def test_retry_after_a_dropped_reply_reuses_one_journalled_auto_close(tmp_path, monkeypatch):
    path = tmp_path / "seat.json"
    calls, _entry_before = _drop_reply_after_storage(path, monkeypatch)

    assert cli.main(_send(path, "dropped")) == 0
    entry = _entry(path, "dropped")
    assert len(calls) == 1
    assert entry["state"] == "succeeded"
    assert entry["value"]["closes"] == [QUESTION]


def test_fresh_op_auto_closes_exactly_like_plain_send(tmp_path, monkeypatch):
    state, sent = _state(), []
    open_question = {"value": True}
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "ledger_records", lambda *a, **k: [{"key": QUESTION, "version": 1}])
    monkeypatch.setattr(node, "owed", lambda *a, **k: _owed(open_question))
    monkeypatch.setattr(node, "owed_key", _owed_key(open_question))
    monkeypatch.setattr(node, "note_sent", lambda *a, **k: True)

    def accept(*_args, **kwargs):
        sent.append(kwargs["args"]["value"])
        return _ok(kwargs["args"]["key"])

    monkeypatch.setattr(node, "invoke", accept)
    plain = _send(tmp_path / "plain.json", "unused")
    plain.remove("--op-id")
    plain.remove("unused")
    assert cli.main(plain) == 0
    assert cli.main(_send(tmp_path / "op.json", "fresh")) == 0
    assert [value.get("closes") for value in sent] == [[QUESTION], [QUESTION]]


@pytest.mark.parametrize("command", ["send-status", "send-drain"])
def test_status_and_drain_reconcile_the_journalled_auto_close(tmp_path, monkeypatch, command):
    path = tmp_path / "seat.json"
    calls, entry_before = _drop_reply_after_storage(path, monkeypatch)

    if command == "send-status":
        args = [command, "--base-url", "http://127.0.0.1:1", "--state-file", str(path), "dropped"]
    else:
        args = [command, "--base-url", "http://127.0.0.1:1", "--state-file", str(path)]
    assert cli.main(args) == 0
    entry_after = _entry(path, "dropped")
    assert len(calls) == 1
    assert entry_before["value"]["closes"] == entry_after["value"]["closes"] == [QUESTION]


def test_a_retry_with_a_new_explicit_close_is_refused_not_dropped(tmp_path, monkeypatch):
    """A --closes the journalled op never carried must not be silently ignored on a retry."""
    path = tmp_path / "seat.json"
    calls, _entry_before = _drop_reply_after_storage(path, monkeypatch)

    retry = [*_send(path, "dropped"), "--closes", "msg/1790000000001-heron-other"]
    assert cli.main(retry) == 2
    assert len(calls) == 1
    assert _entry(path, "dropped")["value"]["closes"] == [QUESTION]


def test_a_retry_that_drops_an_explicit_close_is_refused(tmp_path, monkeypatch):
    """The journal must not put back a --closes the caller typed and then left out of the retry (pxx 66c582)."""
    state, calls = _state(), []
    other = "msg/1790000000002-heron-other"
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "ledger_records", lambda *a, **k: [{"key": QUESTION, "version": 1}])
    monkeypatch.setattr(node, "owed", lambda *a, **k: _owed({"value": True}))
    monkeypatch.setattr(node, "owed_key", _owed_key({"value": True}))
    monkeypatch.setattr(node, "note_sent", lambda *a, **k: True)

    def lost_reply(*_args, **kwargs):
        calls.append(kwargs["args"]["value"])
        raise TimeoutError("stored, but the reply was dropped")

    monkeypatch.setattr(node, "invoke", lost_reply)
    path = tmp_path / "seat.json"
    assert cli.main([*_send(path, "explicit"), "--closes", other]) == 2
    entry = _entry(path, "explicit")
    assert entry["value"]["closes"] == [other, QUESTION]
    assert entry["auto_closes"] == [QUESTION]
    monkeypatch.setattr(node, "read", lambda *_a, **_k: _matching_read(_entry(path, "explicit")))

    assert cli.main(_send(path, "explicit")) == 2
    assert len(calls) == 1


def test_a_retry_with_keep_open_of_an_auto_closed_op_is_refused(tmp_path, monkeypatch, capsys):
    """GitHub Codex r4366654087 (pxx b3ff8e): the journalled auto-close must not come back on a --keep-open retry."""
    path = tmp_path / "seat.json"
    calls, _entry_before = _drop_reply_after_storage(path, monkeypatch)
    capsys.readouterr()

    assert cli.main([*_send(path, "dropped"), "--keep-open"]) == 2
    assert "--keep-open" in json.loads(capsys.readouterr().err)["refused"]
    assert len(calls) == 1
    assert _entry(path, "dropped")["value"]["closes"] == [QUESTION]


def test_an_op_created_with_keep_open_gains_no_close_on_a_plain_retry(tmp_path, monkeypatch):
    """The other direction: keep-open at creation journals no auto-close, so a retry without it closes nothing."""
    state, calls = _state(), []
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "ledger_records", lambda *a, **k: [{"key": QUESTION, "version": 1}])
    monkeypatch.setattr(node, "owed", lambda *a, **k: _owed({"value": True}))
    monkeypatch.setattr(node, "owed_key", _owed_key({"value": True}))
    monkeypatch.setattr(node, "note_sent", lambda *a, **k: True)

    def lost_reply(*_args, **kwargs):
        calls.append(kwargs["args"]["value"])
        raise TimeoutError("stored, but the reply was dropped")

    monkeypatch.setattr(node, "invoke", lost_reply)
    path = tmp_path / "seat.json"
    assert cli.main([*_send(path, "kept"), "--keep-open"]) == 2
    entry = _entry(path, "kept")
    assert entry["auto_closes"] == [] and not entry["value"].get("closes")
    monkeypatch.setattr(node, "read", lambda *_a, **_k: _matching_read(_entry(path, "kept")))

    assert cli.main(_send(path, "kept")) == 0
    assert len(calls) == 1
    assert not _entry(path, "kept")["value"].get("closes")
