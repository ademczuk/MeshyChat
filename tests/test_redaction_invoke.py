"""memory.redact over the wire (divergence 053): write, read and redact through
`POST /v1/agents/<memory authority>/skills/<skill>/invoke`, the route every seat uses.

tests/test_redaction.py dispatches in-process, where no call is stored. Here the calls are
real rows of `skill_calls`, made by the route itself, so the census runs over what production
would hold: the row, its envelope, the stored write call, and the stored read that returned it.

Run:  .venv/Scripts/python.exe -m pytest tests/test_redaction_invoke.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_call_routing import (  # noqa: E402,F401
    call_row, client, invoke, normalized, owner_conn, poll, server_config, signed, world,
)
from tests.test_redaction import census  # noqa: E402


def _do(client, peer, memory_peer, skill, args) -> dict:
    envelope = signed(peer, memory_peer, skill, args)
    accepted = invoke(client, peer, memory_peer, skill, envelope)
    assert accepted.status_code == 202, accepted.content
    return normalized(poll(client, peer, envelope["payload"]["call_id"]))


def test_a_seat_redacts_its_row_over_the_wire_and_no_stored_copy_keeps_the_text(
        client, owner_conn, world):
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    marker = f"zq-wire-{uuid.uuid4().hex}"
    key = f"k-{uuid.uuid4().hex[:8]}"

    wrote = _do(client, caller, memory_peer, "memory.write",
                {"namespace": ns, "key": key, "value": {"body": marker, "from": "a"},
                 "tags": [marker]})
    assert wrote["status"] == "completed", wrote
    read = _do(client, caller, memory_peer, "memory.read", {"namespace": ns, "key": key})
    assert marker in json.dumps(read["result"]), "control: the read returned the text"

    before = census(owner_conn, marker)
    for must in ("shared_memory.value", "shared_memory.signed_bytes",
                 "shared_memory.provenance_jsonb", "skill_calls.args", "skill_calls.result",
                 "skill_calls.result_payload"):
        assert must in before, f"control: {must} held the text before, found {before}"

    done = _do(client, caller, memory_peer, "memory.redact",
               {"namespace": ns, "key": key, "reason_code": "sent_in_error"})
    assert done["status"] == "completed", done
    result = done["result"]
    assert [r["version"] for r in result["redacted"]] == [1]
    assert result["stored_calls_blanked"] == {"write_calls": 1, "read_results": 1}

    assert census(owner_conn, marker) == [], "a stored copy kept the text"

    after = _do(client, caller, memory_peer, "memory.read", {"namespace": ns, "key": key})
    memo = after["result"]["memo"]
    assert memo["value"]["redacted"] is True and memo["tags"] == ["redacted"]
    assert marker not in json.dumps(after)

    # The write call's row is still there, signed as it was, with the text gone.
    stored = call_row(owner_conn, wrote["call_id"])
    assert stored["status"] == "completed" and stored["args_signature"]
    assert stored["args"]["args"] == {"namespace": ns, "key": key, "redacted": True}


def test_a_seat_without_the_grant_cannot_redact_over_the_wire(client, owner_conn, world):
    caller, outsider = world["caller"], world["outsider"]
    memory_peer, ns = world["memory_peer"], world["namespace"]
    marker = f"zq-wire-{uuid.uuid4().hex}"
    key = f"k-{uuid.uuid4().hex[:8]}"
    _do(client, caller, memory_peer, "memory.write",
        {"namespace": ns, "key": key, "value": {"body": marker}})
    refused = _do(client, outsider, memory_peer, "memory.redact",
                  {"namespace": ns, "key": key, "reason_code": "sent_in_error"})
    assert refused["status"] == "failed" and refused["error_code"] == "E_PERMISSION_DENIED"
    assert "shared_memory.value" in census(owner_conn, marker), "the row is unchanged"
