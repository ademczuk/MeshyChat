"""PR #372 fingerprints every signed attribution field of an operation send."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from tools.meshy_node import cli, node  # noqa: E402


def _state():
    return {"peer_id": "00000000-0000-0000-0000-000000000001", "label": "uxxx", "status": "minted",
            "kid": "test-kid", "signing_seed_b64url": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "memory_peer_id": "29d6a9f7-ed27-5153-a9f2-f6feb3175e77"}


def _ok(key):
    return {"key": key, "status": "completed", "result": {"ok": True, "key": key, "version": 1},
            "result_signature_verified": True}


def _base(path):
    return ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(path), "--to", "heron",
            "--thread", "ops/pr372", "--body", "attribution", "--op-id", "attribution-op",
            "--no-recipient-check", "--no-card-check"]


@pytest.mark.parametrize(("flag", "first", "changed"), [
    ("--from", "uxxx-one", "uxxx-two"),
    ("--author-family", "family-one", "family-two"),
    ("--session-label", "session-one", "session-two"),
])
def test_a_changed_signed_attribution_field_refuses_without_a_second_write(tmp_path, monkeypatch, flag, first, changed):
    state, writes = _state(), []
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "note_sent", lambda *a, **k: True)
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    path = tmp_path / "seat.json"

    assert cli.main([*_base(path), flag, first]) == 0
    assert cli.main([*_base(path), flag, changed]) == 2
    assert len(writes) == 1


def test_unchanged_signed_attribution_resolves_the_stored_message(tmp_path, monkeypatch):
    state, writes = _state(), []
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "note_sent", lambda *a, **k: True)
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    path = tmp_path / "seat.json"
    args = [*_base(path), "--from", "uxxx-one", "--author-family", "family-one", "--session-label", "session-one"]

    assert cli.main(args) == 0
    assert cli.main(args) == 0
    assert len(writes) == 1
