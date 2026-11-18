"""`send` says so when a recipient is the sender itself: the message is stored and never delivered.

The server never nudges a message's author (divergence 020): the push skips it
(apps/wsgw/mesh_notify.py), the addressed wait filters it (apps/mesh/wait.py) and the waiter registry
does not wake it (apps/mesh/wake.py). `send` printed the result and exited 0 with no hint, so a seat
that tested its own delivery by writing to itself saw silence and could not tell it from lost mail.
Measured on HQB, 2026-10-01: two messages from a seat to its own label were stored and reached no
spool of that seat in 60 seconds, while mail from other seats arrived in the same window.

The warning is one line on stderr. Nothing else about `send` changes: the message is still sent, stdout
and the exit code are as before.

No network and no database: node.send is replaced, as tests/test_meshy_node_docshare.py does.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_self_send.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node  # noqa: E402


@pytest.fixture()
def sent(monkeypatch, tmp_path):
    """cli.main's `send` with node.send replaced by a recorder. Returns (calls, state, outcome): the
    state dict is what load_state hands back, and outcome["ok"] is what node.succeeded answers."""
    calls, state, outcome = [], {"label": "uxxx"}, {"ok": True}
    monkeypatch.setattr(node, "send", lambda *a, **kw: calls.append(kw) or {"status": "completed"})
    monkeypatch.setattr(node, "succeeded", lambda r: outcome["ok"])
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: state, raising=False)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    return calls, state, outcome


def _send(tmp_path, *extra):
    return cli.main(["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "state.json"),
                     "--thread", "t", "--body", "hello", *extra])


def _warnings(capsys):
    """The warning lines on stderr, parsed. Anything else on stderr fails the test that reads it."""
    lines = [json.loads(l) for l in capsys.readouterr().err.splitlines() if l.strip()]
    assert all("warning" in l for l in lines), lines
    return lines


def test_a_message_to_the_senders_own_label_is_sent_and_says_it_reaches_nobody(sent, tmp_path, capsys):
    calls, _state, _outcome = sent
    assert _send(tmp_path, "--to", "uxxx") == 0
    assert len(calls) == 1, "the message is still sent: it is stored and readable by key"
    (warning,) = _warnings(capsys)
    assert warning["warning"] == "self_addressed" and warning["labels"] == ["uxxx"]
    assert "never pushed" in warning["note"] and "020" in warning["note"]


def test_a_message_to_another_seat_prints_nothing_on_stderr(sent, tmp_path, capsys):
    """Control: the warning is about the sender's own label, not about every send."""
    calls, _state, _outcome = sent
    assert _send(tmp_path, "--to", "nxxxxx", "--cc", "kxxx") == 0
    assert len(calls) == 1 and _warnings(capsys) == []


def test_the_senders_label_among_other_recipients_and_in_cc_is_named(sent, tmp_path, capsys):
    calls, _state, _outcome = sent
    assert _send(tmp_path, "--to", "nxxxxx,uxxx", "--cc", "UXXX") == 0
    (warning,) = _warnings(capsys)
    assert warning["labels"] == ["uxxx", "UXXX"], "compared without case, as the push resolves labels"
    assert len(calls) == 1


def test_from_decides_who_the_sender_is(sent, tmp_path, capsys):
    """`--from` overrides the label in the state file, so it is the one compared."""
    calls, _state, _outcome = sent
    assert _send(tmp_path, "--to", "uxxx", "--from", "uxxx-other") == 0
    assert _warnings(capsys) == []
    assert _send(tmp_path, "--to", "uxxx-other", "--from", "uxxx-other") == 0
    assert [w["labels"] for w in _warnings(capsys)] == [["uxxx-other"]]


def test_a_state_file_with_no_label_warns_about_nothing(sent, tmp_path, capsys):
    """A node that has never run `inbox` has no label recorded. The warning cannot know the label
    then and stays silent; it must not crash a send over it."""
    calls, state, _outcome = sent
    state.clear()
    assert _send(tmp_path, "--to", "uxxx") == 0
    assert len(calls) == 1 and _warnings(capsys) == []


def test_a_send_the_server_refused_does_not_also_warn(sent, tmp_path, capsys):
    """Nothing was stored, so there is nothing to say about its delivery. Exit code 1 as before."""
    _calls, _state, outcome = sent
    outcome["ok"] = False
    assert _send(tmp_path, "--to", "uxxx") == 1
    assert _warnings(capsys) == []
