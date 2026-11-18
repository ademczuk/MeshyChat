"""A send whose outcome is unknown names the key it tried (kxxx's double send of 12 Nov, 9a5cc4).

The client mints the msg/ key before memory.write. When the server could not be reached or answered 5xx, it may
still have written it, and the caller, not knowing the key, resent under a new one: a duplicate. Now the exception
carries `attempted_key` and cli.main prints it, with how to settle it, beside "outcome": "unknown" only.
CONTROLS: a refused connect (nothing written: retryable, no outcome) and a send that succeeds print no attempted_key.
MUTANT: drop `exc.attempted_key = key` in node.send; the first test fails. Database-free.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from tools.meshy_node import cli, node

STATE = {"base_url": "http://x", "label": "kxxx-prod", "peer_id": "p" * 32}


def _send(monkeypatch, tmp_path, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(node, "require_peer", lambda *a, **k: None)
    monkeypatch.setattr(node, "invoke", boom)
    with pytest.raises(type(exc)) as got:
        node.send(tmp_path / "state.json", dict(STATE), to=["pxx"], thread_id="t1", body="hello",
                  msg_type="answer", author_family="claude")
    return got.value


def test_an_unreachable_send_carries_the_key_it_tried(monkeypatch, tmp_path):
    exc = _send(monkeypatch, tmp_path, urllib.error.URLError(TimeoutError("timed out")))
    assert isinstance(exc.attempted_key, str) and exc.attempted_key.startswith("msg/")
    rec = cli._with_attempted_key(exc, {"unreachable": "x", "code": "E_UNREACHABLE", "retryable": False,
                                        "outcome": "unknown"})
    assert rec["attempted_key"] == exc.attempted_key
    assert "read --key %s" % exc.attempted_key in rec["settle"]


def test_a_refused_connect_says_nothing_new(monkeypatch, tmp_path):
    """A refused connect wrote nothing: errors.io_metadata gives it no outcome, so no key is offered."""
    exc = _send(monkeypatch, tmp_path, urllib.error.URLError(ConnectionRefusedError(111, "refused")))
    rec = cli._with_attempted_key(exc, {"code": "E_UNREACHABLE", "retryable": True})
    assert "attempted_key" not in rec and "settle" not in rec


def test_main_prints_the_key_on_an_unknown_outcome(monkeypatch, tmp_path, capsys):
    exc = urllib.error.URLError(TimeoutError("timed out"))
    exc.attempted_key = "msg/1-kxxx-abcdef"
    monkeypatch.setattr(cli, "run", lambda args: (_ for _ in ()).throw(exc))
    code = cli.main(["send", "--base-url", "http://x", "--state-file", str(tmp_path / "s.json"), "--to", "pxx",
                     "--thread", "t1", "--type", "answer", "--body", "hello"])
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert code == 2
    assert err.get("outcome") == "unknown", err
    assert err["attempted_key"] == "msg/1-kxxx-abcdef"


def test_a_completed_send_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(node, "require_peer", lambda *a, **k: None)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {"status": "completed", "result_signature_verified": True})
    monkeypatch.setattr(node, "note_sent", lambda *a, **k: None)
    sent = node.send(tmp_path / "state.json", dict(STATE), to=["pxx"], thread_id="t1", body="hello",
                     msg_type="answer", author_family="claude")
    assert sent["status"] == "completed" and "attempted_key" not in sent
