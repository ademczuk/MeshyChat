"""Tests for causal/provenance thread envelopes and deterministic meshsum."""
from __future__ import annotations

import copy
import datetime as _dt
import json
import uuid
from pathlib import Path

import pytest

from apps.memoryskills import validation
from apps.mesh import graph, projection
from apps.meshsum import record as meshsum_record
from tools.meshy_node import cli, node, wire
from tests.fixtures import signed_rows as sr
from tests.test_call_routing import bearer, client, invoke, owner_conn, poll, server_config, signed, world  # noqa: F401
from tests.test_mesh_graphread import _grant, _mesh_claimant, _peer, _row


def _row_with_policy(conn, peer, value: dict, *, embed_policy: str = "allowed", created_at=None) -> dict:
    rid = uuid.uuid4()
    when = created_at or _dt.datetime.now(_dt.timezone.utc)
    key = f"msg/{int(when.timestamp() * 1000)}-{rid.hex}"
    sr.insert(conn, sr.row(
        peer["peer_id"], peer["owner_id"], "shared/mesh-chat", key, value,
        rid=rid, call_id=f"call-{rid}", author_label=peer["label"],
        created_at=when, embed_policy=embed_policy,
    ))
    return {"key": key, "id": rid, "created_at": when}


def _minted_state_for_caller(tmp_path: Path, caller: dict, base_url: str = "http://testserver") -> tuple[Path, dict]:
    hdrs = bearer(caller)
    token = (hdrs.get("Authorization") or hdrs.get("authorization")).split(" ", 1)[1]
    seed = caller["seed"]
    st = {
        "schema": node.STATE_SCHEMA, "base_url": base_url, "status": "minted",
        "signing_seed_b64url": wire.b64u(seed), "public_key_b64url": wire.b64u(wire.public_key_of(seed)),
        "peer_id": str(caller["peer_id"]), "owner_id": str(caller["owner_id"]),
        "kid": caller["kid"], "jwt": token, "jwt_expires_at": "2999-01-01T00:00:00Z",
        "memory_peer_id": node.DEFAULT_MEMORY_PEER_ID,
    }
    path = tmp_path / "caller-state.json"
    path.write_text(json.dumps(st), encoding="utf-8")
    return path, st


def _invoke_ok(client, caller: dict, memory_peer, skill: str, args: dict) -> dict:
    resp = invoke(client, caller, memory_peer, skill, signed(caller, memory_peer, skill, args))
    state = poll(client, caller, resp.json()["call_id"])
    assert resp.status_code == 202 and state["status"] == "completed", state
    return state


def test_causal_order_tree_unresolved_refs_cycles_and_validation(client, owner_conn):
    ns = "shared/mesh-chat"
    claimant = _mesh_claimant(owner_conn)
    alice = _peer(owner_conn, "causal-a-" + uuid.uuid4().hex[:6])
    bob = _peer(owner_conn, "causal-b-" + uuid.uuid4().hex[:6])
    _grant(owner_conn, ns, alice, claimant, permission="write")
    _grant(owner_conn, ns, bob, claimant, permission="write")

    now = _dt.datetime.now(_dt.timezone.utc)
    thread = "t-causal-" + uuid.uuid4().hex[:10]
    off_page_parent = f"msg/{int((now - _dt.timedelta(hours=2)).timestamp() * 1000)}-ghost-000001"

    # Interleaved chronological stream: root1 -> root2 -> reply1_to_root1 -> orphan_reply -> reply2_to_reply1.
    root1 = _row(owner_conn, {**alice, "label": alice["name"]},
                 {"to": [bob["name"]], "thread_id": thread, "type": "question", "body": "root one"},
                 created_at=now - _dt.timedelta(minutes=20))
    root2 = _row(owner_conn, {**bob, "label": bob["name"]},
                 {"to": [alice["name"]], "thread_id": thread, "type": "note", "body": "root two"},
                 created_at=now - _dt.timedelta(minutes=18))
    reply1 = _row(owner_conn, {**bob, "label": bob["name"]},
                  {"to": [alice["name"]], "thread_id": thread, "in_reply_to": root1["key"],
                   "type": "answer", "body": "reply to root one"},
                  created_at=now - _dt.timedelta(minutes=16))
    orphan = _row(owner_conn, {**alice, "label": alice["name"]},
                  {"to": [bob["name"]], "thread_id": thread, "in_reply_to": off_page_parent,
                   "type": "note", "body": "reply to off-page parent"},
                  created_at=now - _dt.timedelta(minutes=14))
    reply2 = _row(owner_conn, {**alice, "label": alice["name"]},
                  {"to": [bob["name"]], "thread_id": thread, "in_reply_to": reply1["key"],
                   "type": "note", "body": "nested reply under reply1"},
                  created_at=now - _dt.timedelta(minutes=12))
    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()

    # Stream order preserves chronological arrival.
    r_stream = client.get(f"/v1/mesh/threads/{thread}?order=stream", headers=bearer(alice))
    assert r_stream.status_code == 200 and r_stream.json()["order"] == "stream"
    assert [h["key"] for h in r_stream.json()["hits"]] == [
        root1["key"], root2["key"], reply1["key"], orphan["key"], reply2["key"]
    ]

    # Causal order groups subtrees under each root and reports off-page parent in unresolved_refs.
    r_causal = client.get(f"/v1/mesh/threads/{thread}?order=causal", headers=bearer(alice))
    assert r_causal.status_code == 200, r_causal.content
    cb = r_causal.json()
    assert cb["order"] == "causal"
    assert [h["key"] for h in cb["hits"]] == [
        root1["key"], reply1["key"], reply2["key"], root2["key"], orphan["key"]
    ]
    assert {h["key"]: h["causal_depth"] for h in cb["hits"]} == {
        root1["key"]: 0, reply1["key"]: 1, reply2["key"]: 2, root2["key"]: 0, orphan["key"]: 0,
    }
    orphan_hit = next(h for h in cb["hits"] if h["key"] == orphan["key"])
    assert orphan_hit["parent_unresolved"] is True and orphan_hit["reply_to"] == off_page_parent
    assert cb["unresolved_refs"] == [{"child_key": orphan["key"], "parent_key": off_page_parent}]

    # Validation checks on invalid parameters and provenance=1 page cap (1..100).
    for bad_qs in (
        "order=bogus", "order=causal&include_inferred=1", "provenance=1",
        "envelope=1&provenance=1&limit=101", "envelope=1&provenance=1&limit=0",
    ):
        assert client.get(f"/v1/mesh/threads/{thread}?{bad_qs}", headers=bearer(alice)).status_code == 422

    # Direct cycle-safety check: synthetic mutual replies_to edges never loop or drop hits.
    cyc_a = _row(owner_conn, {**alice, "label": alice["name"]},
                 {"to": [bob["name"]], "thread_id": thread + "-cyc", "type": "note", "body": "cycle node a"},
                 created_at=now - _dt.timedelta(minutes=5))
    cyc_b = _row(owner_conn, {**bob, "label": bob["name"]},
                 {"to": [alice["name"]], "thread_id": thread + "-cyc", "type": "note", "body": "cycle node b"},
                 created_at=now - _dt.timedelta(minutes=4))
    k_a, k_b = cyc_a["key"], cyc_b["key"]
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type, dst, src_field, recorded_at) "
            "VALUES (%s, %s, 1, 'replies_to', %s, 'in_reply_to', %s), "
            "       (%s, %s, 1, 'replies_to', %s, 'in_reply_to', %s)",
            (cyc_a["id"], k_a, k_b, now, cyc_b["id"], k_b, k_a, now),
        )
    owner_conn.commit()
    ordered_cyc, unres_cyc = graph.causal_order(owner_conn, [{"key": k_a, "version": 1}, {"key": k_b, "version": 1}])
    assert unres_cyc == [] and [h["key"] for h in ordered_cyc] == [k_a, k_b]
    assert ordered_cyc[0].get("cycle_broken") is True


def test_provenance_envelope_verifies_ed25519_offline_and_strips_relays(
    client, owner_conn, world, monkeypatch, tmp_path
):
    caller = world["caller"]
    peer_id = caller["peer_id"]
    ns = "shared/mesh-chat"
    claimant = _mesh_claimant(owner_conn)
    _grant(owner_conn, ns, caller, claimant, permission="write")

    with owner_conn.cursor() as cur:
        cur.execute("SELECT name FROM peers WHERE id = %s", (peer_id,))
        caller_name = cur.fetchone()[0]
    recipient = _peer(owner_conn, "prov-recip-" + uuid.uuid4().hex[:6])
    _grant(owner_conn, ns, recipient, claimant, permission="write")
    owner_conn.commit()

    # Authorize `caller` as operator voice + roster seat so hit 0 stores signed `operator_words` + `directive`.
    monkeypatch.setenv(validation.SEAT_DIRECTIVES_ENV, "1")
    monkeypatch.setattr(validation, "OPERATOR_VOICE_PEERS", validation.OPERATOR_VOICE_PEERS | {str(peer_id)})
    monkeypatch.setattr(validation, "SEAT_DIRECTIVE_PEERS", validation.SEAT_DIRECTIVE_PEERS | {str(peer_id)})

    now_utc = _dt.datetime.now(_dt.timezone.utc)
    thread = "t-prov-" + uuid.uuid4().hex[:10]
    now_ms = int(now_utc.timestamp() * 1000)
    msg_key = f"msg/{now_ms}-{caller_name}-{uuid.uuid4().hex[:6]}"
    exp_iso = (now_utc + _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg_val = wire.mesh_message(
        to=[recipient["name"]], sender=caller_name, msg_type="request", thread_id=thread,
        body="signed envelope whose relay fields thread must strip", author_family="claude",
        requires_ack=True, operator_words=["SECRET_OPERATOR_WORDS_77"],
        directive={"v": 1, "words": ["SECRET_DIRECTIVE_WORDS_88"], "target": recipient["name"],
                   "target_peer": str(recipient["peer_id"]), "expires_at": exp_iso},
    )
    _invoke_ok(client, caller, world["memory_peer"], "memory.write", {"namespace": ns, "key": msg_key, "value": msg_val})

    # Hit 1: plain message edited TWICE via `memory.edit` (version=3, two rows in `shared_memory_edit`).
    msg_key_b = f"msg/{now_ms + 1000}-{caller_name}-{uuid.uuid4().hex[:6]}"
    val_b = wire.mesh_message(
        to=[recipient["name"]], sender=caller_name, msg_type="note", thread_id=thread,
        in_reply_to=msg_key, body="v1 initial draft", author_family="claude",
    )
    _invoke_ok(client, caller, world["memory_peer"], "memory.write", {"namespace": ns, "key": msg_key_b, "value": val_b})
    _invoke_ok(client, caller, world["memory_peer"], "memory.edit",
               {"namespace": ns, "key": msg_key_b, "expected_head_version": 1, "value": {**val_b, "body": "v2 edit one"}})
    _invoke_ok(client, caller, world["memory_peer"], "memory.edit",
               {"namespace": ns, "key": msg_key_b, "expected_head_version": 2, "value": {**val_b, "body": "v3 edited twice"}})
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory_edit WHERE namespace = %s AND key = %s", (ns, msg_key_b))
        assert cur.fetchone()[0] == 2
    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()

    state_path, st = _minted_state_for_caller(tmp_path, caller)

    def base_http_json(method, base_url, route, *, headers=None, query=None, **kw):
        resp = client.get(route, query or {}, headers=dict(headers or {}))
        return resp.status_code, resp.json()

    def fail_author_keys(*a, **kw):
        raise AssertionError("author_keys HTTP fallback should not be called when author_jwks is bundled")

    monkeypatch.setattr(node, "http_json", base_http_json)
    monkeypatch.setattr(node, "author_keys", fail_author_keys)

    # (d) Untouched hits + memos (including v3 double-edited hit) verify offline with key_source="bundled".
    ans = node.thread(state_path, st, thread_id=thread, envelope=True, provenance=True, order="causal")
    assert ans["order"] == "causal" and [h["key"] for h in ans["hits"]] == [msg_key, msg_key_b]
    hit0_env, hit1_env = ans["hits"][0]["envelope"], ans["hits"][1]["envelope"]
    for env in (hit0_env, hit1_env):
        assert env["signature_verified"] is True
        assert env["signed_by"] == str(peer_id)
        assert env["key_source"] == "bundled"
        assert "provenance_memo" not in env
    assert hit1_env["version"] == 3 and hit1_env["edited"] is True
    assert hit1_env["body"] == "v3 edited twice" and hit1_env["edited_at"].endswith("Z")
    serialized = json.dumps(ans)
    for forbidden in ("operator_words", "directive", "operator_relay", "seat_directive",
                      "SECRET_OPERATOR_WORDS_77", "SECRET_DIRECTIVE_WORDS_88"):
        assert forbidden not in hit0_env and forbidden not in serialized

    # (a) Envelope tampering while `provenance_memo` stays intact MUST fail closed (never mask tampering as True).
    fake_att = [{"file_id": str(uuid.uuid4()), "name": "x.txt", "size_bytes": 1, "sha256": "a" * 64}]
    for field, forged_val in (("body", "server-edited body"), ("to", ["mallory"]), ("attachments", fake_att)):
        def env_tamper(method, base_url, route, *, headers=None, query=None, _f=field, _v=forged_val, **kw):
            s, b = base_http_json(method, base_url, route, headers=headers, query=query)
            b["hits"][0]["envelope"][_f] = _v
            return s, b

        monkeypatch.setattr(node, "http_json", env_tamper)
        bad_env = node.thread(state_path, st, thread_id=thread, envelope=True, provenance=True)["hits"][0]["envelope"]
        assert bad_env["signature_verified"] is False
        assert bad_env["signed_by"] is None and bad_env["key_source"] is None
        assert f"envelope_mismatch:{field}" in bad_env["signature_error"]

    # Memo value tampering fails closed AND still strips raw `directive` on the unverified fallback envelope.
    def memo_tamper(method, base_url, route, *, headers=None, query=None, **kw):
        s, b = base_http_json(method, base_url, route, headers=headers, query=query)
        b["hits"][0]["envelope"]["directive"] = {"v": 1, "words": ["RAW_DIRECTIVE_FALLBACK_LEAK"]}
        b["hits"][0]["envelope"]["provenance_memo"]["value"]["body"] = "forged memo body"
        return s, b

    monkeypatch.setattr(node, "http_json", memo_tamper)
    bad_memo = node.thread(state_path, st, thread_id=thread, envelope=True, provenance=True)["hits"][0]["envelope"]
    assert bad_memo["signature_verified"] is False and wire.SIG_NOT_THE_MESSAGE in bad_memo["signature_error"]
    assert "directive" not in bad_memo and "RAW_DIRECTIVE_FALLBACK_LEAK" not in json.dumps(bad_memo)

    # (b) Replaying hit 0's valid signed `provenance_memo` onto hit 1 fails with `key_mismatch`.
    def cross_hit_replay(method, base_url, route, *, headers=None, query=None, **kw):
        s, b = base_http_json(method, base_url, route, headers=headers, query=query)
        b["hits"][1]["envelope"]["provenance_memo"] = copy.deepcopy(b["hits"][0]["envelope"]["provenance_memo"])
        return s, b

    monkeypatch.setattr(node, "http_json", cross_hit_replay)
    replayed = node.thread(state_path, st, thread_id=thread, envelope=True, provenance=True)
    assert replayed["hits"][0]["envelope"]["signature_verified"] is True
    rep_env = replayed["hits"][1]["envelope"]
    assert rep_env["signature_verified"] is False and rep_env["key_source"] is None
    assert "key_mismatch" in rep_env["signature_error"]

    # (c) `memo["version"] != hit["version"]`, v1 `memory.write` replay on `version=2` (`edited_at` absent),
    # and signed edit `expected_head_version` mismatch all fail closed with `version_mismatch`.
    def version_tamper(method, base_url, route, *, headers=None, query=None, **kw):
        s, b = base_http_json(method, base_url, route, headers=headers, query=query)
        h0, h1 = b["hits"][0], b["hits"][1]
        h0["version"] = h0["envelope"]["version"] = h0["envelope"]["provenance_memo"]["version"] = 2
        h0["envelope"]["edited"] = True
        h0["envelope"].pop("edited_at", None)
        h0["envelope"]["provenance_memo"].pop("edited_at", None)
        h1["version"] = h1["envelope"]["version"] = h1["envelope"]["provenance_memo"]["version"] = 2
        return s, b

    monkeypatch.setattr(node, "http_json", version_tamper)
    ver_bad = node.thread(state_path, st, thread_id=thread, envelope=True, provenance=True)
    for idx in (0, 1):
        venv = ver_bad["hits"][idx]["envelope"]
        assert venv["signature_verified"] is False and venv["key_source"] is None
        assert "version_mismatch" in venv["signature_error"]

    def memo_ver_mismatch(method, base_url, route, *, headers=None, query=None, **kw):
        s, b = base_http_json(method, base_url, route, headers=headers, query=query)
        b["hits"][0]["envelope"]["provenance_memo"]["version"] = 99
        return s, b

    monkeypatch.setattr(node, "http_json", memo_ver_mismatch)
    mv_env = node.thread(state_path, st, thread_id=thread, envelope=True, provenance=True)["hits"][0]["envelope"]
    assert mv_env["signature_verified"] is False and "version_mismatch" in mv_env["signature_error"]

    # Trust-root `key_source`: `"fetched"` fallback when `author_jwks` is absent, and `"pinned"` override in state.
    def no_bundled_jwks(method, base_url, route, *, headers=None, query=None, **kw):
        s, b = base_http_json(method, base_url, route, headers=headers, query=query)
        b.pop("author_jwks", None)
        return s, b

    def live_author_keys(base_url, pid):
        r = client.get(f"/agents/{pid}/jwks.json")
        return (r.json(), None) if r.status_code == 200 else (None, f"HTTP {r.status_code}")

    monkeypatch.setattr(node, "http_json", no_bundled_jwks)
    monkeypatch.setattr(node, "author_keys", live_author_keys)
    fetched = node.thread(state_path, st, thread_id=thread, envelope=True, provenance=True)["hits"][0]["envelope"]
    assert fetched["signature_verified"] is True and fetched["key_source"] == "fetched"

    monkeypatch.setattr(node, "http_json", base_http_json)
    monkeypatch.setattr(node, "author_keys", fail_author_keys)
    real_jwks, _ = live_author_keys("http://testserver", peer_id)
    pinned_ok = node.thread(state_path, {**st, "pinned_jwks": {str(peer_id): real_jwks}},
                            thread_id=thread, envelope=True, provenance=True)["hits"][0]["envelope"]
    assert pinned_ok["signature_verified"] is True and pinned_ok["key_source"] == "pinned"
    pinned_bad = node.thread(state_path, {**st, "pinned_jwks": {str(peer_id): {"keys": []}}},
                             thread_id=thread, envelope=True, provenance=True)["hits"][0]["envelope"]
    assert pinned_bad["signature_verified"] is False and pinned_bad["key_source"] is None
    assert cli.main(["thread", "--state-file", str(state_path), "--base-url", "http://testserver",
                     "--id", thread, "--order", "causal", "--envelope", "--provenance"]) == 0


def test_deterministic_meshsum_without_llm_provider(
    client, owner_conn, world, monkeypatch, tmp_path, capsys
):
    caller = world["caller"]
    ns = "shared/mesh-chat"
    claimant = _mesh_claimant(owner_conn)
    _grant(owner_conn, ns, caller, claimant, permission="write")
    with owner_conn.cursor() as cur:
        cur.execute("SELECT name FROM peers WHERE id = %s", (caller["peer_id"],))
        caller_name = cur.fetchone()[0]
    sender = _peer(owner_conn, "det-sender-" + uuid.uuid4().hex[:6])
    _grant(owner_conn, ns, sender, claimant, permission="write")

    now = _dt.datetime.now(_dt.timezone.utc)
    thread = "t-detsum-" + uuid.uuid4().hex[:10]
    ask = _row_with_policy(
        owner_conn, {**sender, "label": sender["name"]},
        {"to": [caller_name], "thread_id": thread, "requires_ack": True,
         "type": "question", "body": "Can ixxxxxx check thread status deterministically?\nSecond line ignored."},
        embed_policy="allowed", created_at=now - _dt.timedelta(minutes=25),
    )
    decision = _row_with_policy(
        owner_conn, {**caller, "label": caller_name},
        {"to": [sender["name"]], "thread_id": thread, "in_reply_to": ask["key"],
         "type": "decision", "body": "Approved deterministic extractive digest mode."},
        embed_policy="allowed", created_at=now - _dt.timedelta(minutes=20),
    )
    forbidden_row = _row_with_policy(
        owner_conn, {**sender, "label": sender["name"]},
        {"to": [caller_name], "thread_id": thread, "type": "note",
         "body": "SECRET_EMBED_FORBIDDEN_BODY_MUST_NOT_LEAK"},
        embed_policy="forbidden", created_at=now - _dt.timedelta(minutes=18),
    )
    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()

    # Force model-based summaries disabled (no measurement pin / no chat provider).
    monkeypatch.setattr(meshsum_record, "current", lambda: (None, "no_measurement_pin_in_test"))
    state_path, st = _minted_state_for_caller(tmp_path, caller)

    def fake_http_json(method, base_url, route, *, headers=None, query=None, **kw):
        resp = client.get(route, query or {}, headers=headers or {})
        return resp.status_code, resp.json()

    monkeypatch.setattr(node, "http_json", fake_http_json)

    # Model mode refuses 503 E_SUMMARY_DISABLED, proving our negative control.
    with pytest.raises(node.ServerSaid) as exc_info:
        node.summarize(state_path, st, thread=thread, deterministic=False)
    assert exc_info.value.status == 503 and exc_info.value.body["error"] == "E_SUMMARY_DISABLED"

    # Deterministic thread summary succeeds in-process with zero LLM calls.
    det_thread = node.summarize(state_path, st, thread=thread, deterministic=True)
    assert isinstance(det_thread["provenance"]["latency_ms"], int) and det_thread["provenance"]["latency_ms"] >= 0
    assert det_thread["tier"] == "deterministic_extractive" and det_thread["mode"] == "deterministic"
    assert det_thread["unverified"] is True and det_thread["facts"]["messages"] == 3
    assert det_thread["excluded"]["embed_forbidden"] == 1
    assert "SECRET_EMBED_FORBIDDEN_BODY_MUST_NOT_LEAK" not in json.dumps(det_thread)
    cited = {c for s in det_thread["summary"]["sentences"] for c in s["cites"]}
    assert {ask["key"], decision["key"]} <= cited and forbidden_row["key"] not in cited

    # Deterministic backlog summary + CLI `--deterministic --text` formatting.
    rc = cli.main(["summarize", "--state-file", str(state_path), "--base-url", "http://testserver",
                   "--backlog", "--since", "start", "--deterministic", "--text"])
    text_out = capsys.readouterr().out
    assert rc == 0 and "[declared] backlog since start:" in text_out
    assert "[extractive, unverified]" in text_out and ask["key"] in text_out

    # Route usage is recorded in `mesh_route_usage`, while GPU/model `mesh_summary_requests` stays untouched.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT route, outcome, calls FROM mesh_route_usage WHERE peer_id = %s", (caller["peer_id"],))
        usage_rows = {(r, o): c for r, o, c in cur.fetchall()}
        cur.execute("SELECT count(*) FROM mesh_summary_requests WHERE peer_id = %s", (str(caller["peer_id"]),))
        summary_lease_rows = cur.fetchone()[0]
    owner_conn.commit()
    assert usage_rows.get(("summary_thread", "ok"), 0) >= 1
    assert usage_rows.get(("summary_backlog", "ok"), 0) >= 1
    assert summary_lease_rows == 0

    # Invalid mode parameter -> 422 E_VALIDATION.
    assert client.get(f"/v1/mesh/summaries/thread?thread_id={thread}&mode=hallucinate",
                      headers=bearer(caller)).status_code == 422
