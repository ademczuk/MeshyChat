"""`read` of a redacted message says it was redacted (tools/meshy_node/node.py read, redaction_of).

kxxx, verifying memory.redact on production (msg/<key>): a read of the redacted message
showed body null and a signature error, and nothing said why. The tombstone memory.redact writes
(apps/memoryskills/redact.py tombstone) carries `redacted: true`, `reason_code` and `redacted_at`; `read` now
prints those as `redacted` and says in `signature_error` that the server wrote the version.

What is held here:
  * a tombstone whose writer signature does not verify reads as redacted, with its reason and time, body null;
  * a value a writer SIGNED that says `redacted: true` is that writer's claim and is NOT shown as a redaction;
  * an ordinary message whose signature failed keeps its own signature error and has no `redacted`;
  * a redacted read IS recorded in the read ledger (kxxx's ruling, msg/<key>): there is
    nothing left to read. A writer-signed value that says `redacted: true` is NOT, unless it passes the
    ordinary rule (both checks verified).

No server: `invoke` and the two checks are faked, as tests/test_meshy_node_read_ledger.py does.
Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_read_redacted.py -q -p no:cacheprovider
Mutants (node.py, each seen to fail a test here): redaction_of ignores `verified`; redaction_of always None;
read() does not attach the redaction; note_read ignores the tombstone; note_read records any `redacted`
field whatever the writer's signature says; redaction_of skips the provenance `payload_redacted` check (2 fail);
redaction_of skips the tombstone shape check (1 fails).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import node, wire  # noqa: E402

PEER = "0447bf92-25fb-5230-b531-5a0d9c00d8c7"
WRITER = "11111111-1111-1111-1111-111111111111"
STATE = {"base_url": "http://fixture", "peer_id": PEER, "status": "bound"}
TOMBSTONE = {"redacted": True, "reason_code": "sent_in_error", "redacted_at": "2026-10-10T11:39:00+00:00",
             "schema_version": 1, "from": "kxxx", "to": ["kxxx"], "type": "note",
             "thread_id": "edit-redact-verify-20261010"}
#: What memory.redact leaves in provenance_jsonb (apps/memoryskills/redact.py _redact_one): its own top-level
#: `payload_redacted` and the call id alone. A writer's envelope sits under `payload`, so it cannot set this key.
SERVER_PROVENANCE = {"payload_redacted": True, "payload": {"call_id": "c0"}}
#: An ordinary write's provenance (apps/memoryskills/handlers.py _provenance): no `payload_redacted`.
WRITER_PROVENANCE = {"payload": {"call_id": "c0", "value": {"redacted": True}}, "signature": "s", "signing_kid": "k"}


@pytest.fixture
def fake(monkeypatch):
    """`fake.value` is the stored value; `fake.signed` is whether the writer's signature verifies."""
    class Fake:
        value = dict(TOMBSTONE)
        signed = False
        status_ok = True
        provenance = dict(SERVER_PROVENANCE)

    monkeypatch.setattr(node, "require_peer", lambda *a, **k: None)

    def invoke(path, state, *, skill, args, **kw):
        memo = {"namespace": wire.MESH_NAMESPACE, "key": args["key"], "version": 2, "is_head": True,
                "created_at": "2026-10-10 11:39:00+00:00", "author_label": "kxxx", "value": Fake.value,
                "created_by_peer_id": WRITER, "provenance": Fake.provenance}
        return {"status": "completed", "result_signature_verified": True, "call_id": "c1", "result": {"memo": memo}}

    monkeypatch.setattr(node, "invoke", invoke)
    monkeypatch.setattr(wire, "author_signature_ok",
                        lambda m, keys: (True, None) if Fake.signed else (False, "E_SIGNATURE_MISMATCH: fixture"))
    monkeypatch.setattr(wire, "status_check", lambda m, verified, why: (
        {"status_verified": True, "author_signed": True} if verified and Fake.status_ok
        else {"status_verified": False, "status_error": why}))
    return Fake


def recorded(state_path):
    p = Path(f"{state_path}{node.READ_LEDGER_SUFFIX}")
    if not p.exists():
        return []
    return [(r["key"], r["version"]) for r in json.loads(p.read_text(encoding="utf-8"))["reads"]]


def test_a_tombstone_reads_as_redacted_with_its_reason_and_time(fake, tmp_path):
    state = tmp_path / "seat-prod-state.json"
    message = node.read(state, STATE, key="msg/1-kxxx-1c632a")
    assert message["redacted"] == {"reason_code": "sent_in_error", "redacted_at": "2026-10-10T11:39:00+00:00"}
    assert message["body"] is None and message["signature_verified"] is False
    assert message["signature_error"].startswith("REDACTED: ") and "sent_in_error" in message["signature_error"]
    assert recorded(state) == [("msg/1-kxxx-1c632a", 2)], "a server tombstone counts as read"


def test_a_value_a_writer_signed_is_not_shown_as_a_redaction(fake, tmp_path):
    fake.signed = True
    fake.value = {**TOMBSTONE, "body": "I say this was redacted"}
    message = node.read(tmp_path / "seat-prod-state.json", STATE, key="msg/1-x")
    assert message["signature_verified"] is True
    assert "redacted" not in message and "signature_error" not in message


def test_a_writer_signed_redacted_claim_is_not_marked_read_when_its_status_fails(fake, tmp_path):
    """kxxx's negative: a value a writer signed that says redacted: true is not auto-marked read. Its status
    check fails here, so the ordinary rule records nothing."""
    fake.signed, fake.status_ok = True, False
    state = tmp_path / "seat-prod-state.json"
    message = node.read(state, STATE, key="msg/1-z")
    assert message["signature_verified"] is True and message["status_verified"] is False
    assert "redacted" not in message and recorded(state) == []


def test_an_unverified_message_carrying_a_redacted_field_is_not_marked_read(tmp_path):
    """note_read on its own: a `redacted` field with a VERIFIED writer signature is not a tombstone (read()
    never sets it that way), so the ordinary rule applies and a failed status records nothing."""
    state = tmp_path / "seat-prod-state.json"
    message = {"key": "msg/1-w", "version": 1, "redacted": {"reason_code": "x", "redacted_at": "t"},
               "signature_verified": True, "status_verified": False}
    assert node.note_read(state, STATE, message) is False and recorded(state) == []


def test_a_writer_value_saying_redacted_whose_signature_fails_is_not_a_tombstone(fake, tmp_path):
    """Codex on #222 (P2, discussion_r4238438577): a writer can put `redacted: true` in its own value, and its
    signature can fail later (a rotated key, a legacy row). Without the server's provenance marker that is not a
    redaction: it keeps its own signature error and is NOT marked read."""
    fake.provenance = dict(WRITER_PROVENANCE)
    state = tmp_path / "seat-prod-state.json"
    message = node.read(state, STATE, key="msg/1-v")
    assert "redacted" not in message
    assert message["signature_error"] == "E_SIGNATURE_MISMATCH: fixture"
    assert recorded(state) == []


def test_a_marker_without_the_tombstone_shape_is_not_a_redaction(fake, tmp_path):
    """The marker alone is not enough either: the value must be the tombstone memory.redact writes, with a
    string reason_code and redacted_at."""
    fake.value = {"redacted": True, "reason_code": None, "redacted_at": "2026-10-10T11:39:00+00:00"}
    state = tmp_path / "seat-prod-state.json"
    message = node.read(state, STATE, key="msg/1-u")
    assert "redacted" not in message and recorded(state) == []


def test_no_provenance_at_all_is_not_a_redaction(fake, tmp_path):
    fake.provenance = None
    message = node.read(tmp_path / "seat-prod-state.json", STATE, key="msg/1-t")
    assert "redacted" not in message


def test_an_ordinary_failed_signature_keeps_its_own_error(fake, tmp_path):
    fake.value = {"from": "x", "to": ["y"], "body": "ordinary text"}
    message = node.read(tmp_path / "seat-prod-state.json", STATE, key="msg/1-y")
    assert "redacted" not in message
    assert message["signature_error"] == "E_SIGNATURE_MISMATCH: fixture"
