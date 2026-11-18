"""B02 phase A1 over W1, `POST /v1/agents/{memory}/skills/memory.write/invoke`. Divergence
039, design docs/design/signed-bytes-status.md (fin/design-signed-bytes) section 4.4:

  15  drift surfaces typed: a handler whose Python derivation disagrees with the database
      trigger (patched here) gets the trigger's check_violation, nothing is written, and W1
      answers a SIGNED `E_INTERNAL`, never `E_WRITE_CONFLICT` (the handler maps only
      UniqueViolation).

Fixtures and helpers are tests/test_call_routing.py's, so these requests resolve through
`meshychat.urls` exactly as a node's do, and the result signature is checked under the key
the served JWKS publishes (C5).

Run:  .venv/Scripts/python.exe -m pytest tests/test_signed_bytes_w1.py -q
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_call_routing import (  # noqa: E402,F401
    call_row, client, invoke, memory_rows, owner_conn, server_config, server_key_from_jwks,
    signed, world,
)
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402


def _signed_result_ok(client, row: dict) -> bool:
    key = server_key_from_jwks(client, row["result_signing_kid"])
    return P.verify_strict(key, P.jcs(row["result_payload"]),
                           P.b64u_decode(row["result_signature"]))


def test_15_a_drifted_derivation_is_refused_by_the_database_and_answered_signed_internal(
        client, owner_conn, world, monkeypatch):
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]

    # Control first: the same write, undrifted, completes.
    key = f"k-{uuid.uuid4().hex[:8]}"
    env = signed(caller, memory_peer, "memory.write",
                 {"namespace": ns, "key": key, "value": {"x": 1}, "tags": ["a", "b"]})
    ok = invoke(client, caller, memory_peer, "memory.write", env)
    assert ok.status_code == 202, ok.content
    assert call_row(owner_conn, env["payload"]["call_id"])["status"] == "completed"
    assert len(memory_rows(owner_conn, ns, key)) == 1

    # The drift: Python now normalises tags differently from the rule the trigger holds.
    monkeypatch.setattr(handlers, "norm_tags", lambda raw: sorted(raw or [], reverse=True))
    key2 = f"k-{uuid.uuid4().hex[:8]}"
    env2 = signed(caller, memory_peer, "memory.write",
                  {"namespace": ns, "key": key2, "value": {"x": 1}, "tags": ["a", "b"]})
    answered = invoke(client, caller, memory_peer, "memory.write", env2)
    assert answered.status_code == 202, answered.content
    row = call_row(owner_conn, env2["payload"]["call_id"])
    assert (row["status"], row["error_code"]) == ("failed", "E_INTERNAL"), row
    assert row["error_code"] != "E_WRITE_CONFLICT"
    assert row["result_payload"]["status"] == "failed"
    assert _signed_result_ok(client, row), "the failure must be the server's SIGNED answer"
    assert memory_rows(owner_conn, ns, key2) == [], "the refused write must leave no row"
    assert json.loads(answered.content)["status"] == "failed"


# ================================================ 5 over W1: refused before the call row (Q4)

def _call_exists(owner_conn, call_id: str) -> bool:
    got = owner_conn.execute("SELECT 1 FROM skill_calls WHERE call_id = %s",
                             (call_id,)).fetchone()
    owner_conn.commit()
    return got is not None


def test_5_a_float_beyond_2_53_minus_1_is_refused_typed_and_its_retry_is_not_a_500(
        client, owner_conn, world):
    """Design G1 over W1 (probe P1): the write completed, every read of the row failed with
    `result is not canonicalizable`, and an idempotent retry answered HTTP 500, because the
    call row's args came back from JSONB as an integer rfc8785 refuses. Refused at intake,
    no call row holds the number, so the retry is refused the same way."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    key = f"k-{uuid.uuid4().hex[:8]}"
    env = signed(caller, memory_peer, "memory.write",
                 {"namespace": ns, "key": key, "value": {"n": 1e16}})
    for attempt in ("first", "retry"):
        r = invoke(client, caller, memory_peer, "memory.write", env)
        assert r.status_code == 400, (attempt, r.status_code, r.content)
        assert json.loads(r.content)["error"] == "E_NUMBER_OUT_OF_RANGE", (attempt, r.content)
    assert not _call_exists(owner_conn, env["payload"]["call_id"])
    assert memory_rows(owner_conn, ns, key) == []

    # The integer form used to fail while the call's hash was computed: an untyped 500.
    # A client that does not canonicalise with RFC 8785 can sign it with json.dumps(sort_keys=True)
    # bytes; rfc8785 cannot serialise it.
    env_int = signed(caller, memory_peer, "memory.write",
                     {"namespace": ns, "key": key, "value": {"n": 0}})
    env_int["payload"]["args"]["value"] = {"n": 2**53}
    env_int["signature"] = P.b64u(P.sign(caller["seed"], json.dumps(
        env_int["payload"], sort_keys=True, separators=(",", ":")).encode()))
    r = invoke(client, caller, memory_peer, "memory.write", env_int)
    assert (r.status_code, json.loads(r.content)["error"]) == (400, "E_NUMBER_OUT_OF_RANGE")

    # Control: 1e15 is written, read back completed over W1, and its retry replays (202).
    env_ok = signed(caller, memory_peer, "memory.write",
                    {"namespace": ns, "key": key, "value": {"n": 1e15}})
    assert invoke(client, caller, memory_peer, "memory.write", env_ok).status_code == 202
    assert call_row(owner_conn, env_ok["payload"]["call_id"])["status"] == "completed"
    again = invoke(client, caller, memory_peer, "memory.write", env_ok)
    assert again.status_code == 202, again.content
    read = signed(caller, memory_peer, "memory.read", {"namespace": ns, "key": key})
    assert invoke(client, caller, memory_peer, "memory.read", read).status_code == 202
    row = call_row(owner_conn, read["payload"]["call_id"])
    assert row["status"] == "completed", row["error_message"]
    assert row["result"]["memo"]["value"] == {"n": 1e15}
    assert _signed_result_ok(client, row)


# ============================================== G4 over W1: the server SIGNS what it stamped

def test_g4_the_signed_write_result_carries_the_author_label_and_tags_the_server_stored(
        client, owner_conn, world):
    """The author label is the server's own stamp when the writer signs none (roster label
    or peer-<id8>), and tags are normalised by the server: both were stored and neither was
    in the result the server signs (design G4, Q5)."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    key = f"k-{uuid.uuid4().hex[:8]}"
    env = signed(caller, memory_peer, "memory.write",
                 {"namespace": ns, "key": key, "value": {"x": 1}, "tags": " b, a ,"})
    assert invoke(client, caller, memory_peer, "memory.write", env).status_code == 202
    row = call_row(owner_conn, env["payload"]["call_id"])
    assert row["status"] == "completed", row
    assert _signed_result_ok(client, row)
    result = row["result_payload"]["result"]
    stored = owner_conn.execute(
        "SELECT author_label, tags FROM shared_memory WHERE namespace = %s AND key = %s",
        (ns, key)).fetchone()
    owner_conn.commit()
    assert (result["author_label"], result["tags"]) == (stored[0], list(stored[1]))
    assert result["author_label"] == f"peer-{str(caller['peer_id'])[:8]}"
    assert result["tags"] == ["b", "a"]
