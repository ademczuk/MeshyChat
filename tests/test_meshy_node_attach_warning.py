"""Audit finding 5 (a reviewer 55836e; oxxxxxx 10b8bd), client half: `send` warns when the body says a file is
attached and the message carries none. Six reviewer round trips started with a "see attached" that attached nothing.
Database-free: node.send is replaced by a recorder, as in test_meshy_node_docshare.py.

Run:  python -m pytest tests/test_meshy_node_attach_warning.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node, sendguard  # noqa: E402


@pytest.fixture()
def sent(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(node, "send", lambda *a, **kw: calls.append(kw) or {"status": "completed"})
    monkeypatch.setattr(node, "succeeded", lambda r: True)
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {}, raising=False)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    return calls


def _send(tmp_path, body):
    return cli.main(["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "state.json"),
                     "--to", "bob", "--thread", "t", "--body", body])


def _warnings(capsys):
    return [json.loads(ln) for ln in capsys.readouterr().err.splitlines() if ln.startswith("{")]


@pytest.mark.parametrize("body", ["the report is attached", "see the Attachment for the numbers",
                                  "attaching the log now", "two attachments: the diff and the run", "enclosed: the plan"])
def test_a_body_that_says_attached_with_nothing_attached_is_sent_with_a_warning(sent, tmp_path, capsys, body):
    """RED before the fix: no warning, and the reader looks for a file that isn't there."""
    assert _send(tmp_path, body) == 0 and len(sent) == 1
    (w,) = [w for w in _warnings(capsys) if w.get("warning") == "says_attached_without_attach"]
    assert "--attach" in w["note"] and "/v1/files/<id>/grants" in w["note"]


@pytest.mark.parametrize("body", ["no file here, just a status", "the attacker model is in the threat doc",
                                  "unattached branch", "detached HEAD at abc123"])
def test_a_body_that_does_not_say_attached_gets_no_warning(sent, tmp_path, capsys, body):
    """CONTROL: the warning is about the word, not every send; near words do not trip it."""
    assert _send(tmp_path, body) == 0 and len(sent) == 1
    assert not [w for w in _warnings(capsys) if w.get("warning") == "says_attached_without_attach"]


def test_says_attached_is_word_bounded():
    assert sendguard.says_attached("Attached.") and sendguard.says_attached("(attachments)")
    assert not sendguard.says_attached("reattached") and not sendguard.says_attached("") \
        and not sendguard.says_attached("attach")
