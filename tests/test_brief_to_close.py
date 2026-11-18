"""Tests for `to_close` in GET /v1/mesh/brief, `close-thread`, and `declines` edges."""
from __future__ import annotations

import datetime as _dt
import json
import uuid

import pytest

from apps.memoryskills import redact, validation
from apps.memoryskills.errors import HandlerError
from apps.mesh import graph, normalize, projection
from tools.meshy_node import cli, node, wire
from tests.test_call_routing import bearer, client, invoke, owner_conn, server_config, signed, world  # noqa: F401
from tests.test_mesh_graphread import _grant, _mesh_claimant, _peer, _row
from tests.test_meshy_node_owed import _state


def _valid_msg(**extra) -> dict:
    base = {
        "schema_version": 1,
        "to": ["bob"],
        "from": "alice",
        "type": "note",
        "thread_id": "t-declines",
        "ts": "2026-11-14T06:00:00Z",
        "body": "hello",
        "author_family": "claude",
    }
    base.update(extra)
    return base


def test_declines_schema_wire_redact_and_normalize():
    now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
    k1 = f"msg/{now_ms}-alice-111111"
    k2 = f"msg/{now_ms - 1000}-alice-222222"

    msg = wire.mesh_message(
        to=["bob"], sender="alice", msg_type="note", thread_id="t-1",
        body="declining out-of-scope ask", author_family="claude", declines=[k1],
    )
    assert msg["declines"] == [k1]
    validation.validate_mesh_message_v1(validation.MESH_CHAT_NAMESPACE, k1, msg)

    tomb = redact.tombstone(msg, reason_code="sent_in_error", redacted_at="2026-11-14T06:05:00Z")
    assert tomb["declines"] == [k1]

    with pytest.raises(wire.NotV1, match="closes and declines"):
        wire.mesh_message(
            to=["bob"], sender="alice", msg_type="note", thread_id="t-1",
            body="overlap", author_family="claude", closes=[k1], declines=[k1],
        )

    with pytest.raises(HandlerError) as exc_info:
        validation.validate_mesh_message_v1(
            validation.MESH_CHAT_NAMESPACE, k1, _valid_msg(closes=[k1], declines=[k1])
        )
    assert exc_info.value.code == "E_MESH_SCHEMA"

    with pytest.raises(HandlerError):
        validation.validate_mesh_message_v1(
            validation.MESH_CHAT_NAMESPACE, k1, _valid_msg(declines=[k1, k1])
        )

    with pytest.raises(HandlerError):
        validation.validate_mesh_message_v1(
            validation.MESH_CHAT_NAMESPACE, k1, _valid_msg(declines=[])
        )

    proj = normalize.edges_for(_valid_msg(closes=[k1], declines=[k1, k2]), author_label="alice")
    edge_pairs = [(e.edge_type, e.dst) for e in proj.edges]
    assert ("closes", k1) in edge_pairs
    assert ("declines", k2) in edge_pairs
    assert ("declines", k1) not in edge_pairs
    assert proj.skipped.get("declines:also_closed") == 1


def test_declines_state_precedence_and_neighbours(client, owner_conn):
    ns = "shared/mesh-chat"
    claimant = _mesh_claimant(owner_conn)
    asker = _peer(owner_conn, "tc-asker-" + uuid.uuid4().hex[:6])
    recip = _peer(owner_conn, "tc-recip-" + uuid.uuid4().hex[:6])
    _grant(owner_conn, ns, asker, claimant, permission="write")
    _grant(owner_conn, ns, recip, claimant, permission="write")

    thread = "tc-declines-" + uuid.uuid4().hex[:10]
    t0 = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=20)
    ask = _row(
        owner_conn,
        {**asker, "label": asker["name"]},
        {"to": [recip["name"]], "thread_id": thread, "requires_ack": True,
         "type": "question", "body": "can you take this?"},
        created_at=t0,
    )
    dec = _row(
        owner_conn,
        {**recip, "label": recip["name"]},
        {"to": [asker["name"]], "thread_id": thread, "type": "note",
         "declines": [ask["key"]], "body": "out of my lane"},
        created_at=t0 + _dt.timedelta(minutes=1),
    )
    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()

    # Default owed/awaiting omit declined; explicit state=declined surfaces it.
    assert graph.owed_acks(owner_conn, [recip["name"]], keys=[ask["key"]]) == ()
    owed_dec = graph.owed_acks(owner_conn, [recip["name"]], keys=[ask["key"]], states=["declined"])
    assert len(owed_dec) == 1 and owed_dec[0].state == "declined"
    await_dec = graph.awaiting(owner_conn, [asker["name"]], states=["declined"])
    assert any(a.hit.key == ask["key"] and a.state == "declined" for a in await_dec)
    assert graph.owed_counts(owner_conn, [recip["name"]])["declined"] >= 1
    assert graph.awaiting_counts(owner_conn, [asker["name"]])["declined"] >= 1

    # Neighbours exposes declared.declines on the declining message. (060 rollback round-trip lives in test_rollback_gate.py.)
    resp = client.get(f"/v1/mesh/neighbours?key={dec['key']}", headers=bearer(asker))
    assert resp.status_code == 200, resp.content
    assert resp.json()["declared"]["declines"] == [ask["key"]]

    # Explicit close supersedes declined -> closed.
    _row(
        owner_conn,
        {**asker, "label": asker["name"]},
        {"to": [recip["name"]], "thread_id": thread, "type": "note",
         "closes": [ask["key"]], "body": "withdrawn"},
        created_at=t0 + _dt.timedelta(minutes=2),
    )
    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()
    owed_closed = graph.owed_acks(owner_conn, [recip["name"]], keys=[ask["key"]], states=["closed"])
    assert len(owed_closed) == 1 and owed_closed[0].state == "closed"


def test_brief_to_close_settle_horizon_ordering_and_multi_recipient_guard(client, owner_conn):
    ns = "shared/mesh-chat"
    claimant = _mesh_claimant(owner_conn)
    asker = _peer(owner_conn, "brief-asker-" + uuid.uuid4().hex[:6])
    r1 = _peer(owner_conn, "brief-r1-" + uuid.uuid4().hex[:6])
    r2 = _peer(owner_conn, "brief-r2-" + uuid.uuid4().hex[:6])
    for p in (asker, r1, r2):
        _grant(owner_conn, ns, p, claimant, permission="write")

    now = _dt.datetime.now(_dt.timezone.utc)
    thread = "tc-brief-" + uuid.uuid4().hex[:10]

    # Ask 1: settled reply 15 minutes ago (second in oldest-reply-first order).
    ask_newer_reply = _row(
        owner_conn, {**asker, "label": asker["name"]},
        {"to": [r1["name"]], "thread_id": thread, "requires_ack": True, "type": "question", "body": "q1"},
        created_at=now - _dt.timedelta(minutes=30),
    )
    rep_newer = _row(
        owner_conn, {**r1, "label": r1["name"]},
        {"to": [asker["name"]], "thread_id": thread, "in_reply_to": ask_newer_reply["key"],
         "type": "answer", "body": "a1"},
        created_at=now - _dt.timedelta(minutes=15),
    )

    # Ask 2: settled reply 25 minutes ago (first in oldest-reply-first order).
    ask_older_reply = _row(
        owner_conn, {**asker, "label": asker["name"]},
        {"to": [r1["name"]], "thread_id": thread, "requires_ack": True, "type": "question", "body": "q2"},
        created_at=now - _dt.timedelta(minutes=28),
    )
    rep_older = _row(
        owner_conn, {**r1, "label": r1["name"]},
        {"to": [asker["name"]], "thread_id": thread, "in_reply_to": ask_older_reply["key"],
         "type": "answer", "body": "a2"},
        created_at=now - _dt.timedelta(minutes=25),
    )

    # Ask 3: fresh reply 30 seconds ago (inside 300s settle window -> excluded).
    ask_fresh = _row(
        owner_conn, {**asker, "label": asker["name"]},
        {"to": [r1["name"]], "thread_id": thread, "requires_ack": True, "type": "question", "body": "q3"},
        created_at=now - _dt.timedelta(minutes=10),
    )
    _row(
        owner_conn, {**r1, "label": r1["name"]},
        {"to": [asker["name"]], "thread_id": thread, "in_reply_to": ask_fresh["key"],
         "type": "answer", "body": "a3"},
        created_at=now - _dt.timedelta(seconds=30),
    )

    # Ask 4: multi-recipient ask where r1 answered 20m ago, but r2 has NOT answered yet -> excluded!
    ask_multi = _row(
        owner_conn, {**asker, "label": asker["name"]},
        {"to": [r1["name"], r2["name"]], "thread_id": thread, "requires_ack": True,
         "type": "question", "body": "q4"},
        created_at=now - _dt.timedelta(minutes=26),
    )
    _row(
        owner_conn, {**r1, "label": r1["name"]},
        {"to": [asker["name"]], "thread_id": thread, "in_reply_to": ask_multi["key"],
         "type": "answer", "body": "a4-r1"},
        created_at=now - _dt.timedelta(minutes=20),
    )

    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()

    resp = client.get("/v1/mesh/brief", headers=bearer(asker))
    assert resp.status_code == 200, resp.content
    body = resp.json()
    tc = body["to_close"]
    assert tc["due"] is False
    assert tc["settle_seconds"] == 300
    keys = [item["key"] for item in tc["items"]]
    assert keys == [ask_older_reply["key"], ask_newer_reply["key"]]
    assert tc["items"][0]["latest_reply_key"] == rep_older["key"]
    assert tc["items"][0]["due"] is False
    assert tc["items"][1]["latest_reply_key"] == rep_newer["key"]
    assert ask_fresh["key"] not in keys
    assert ask_multi["key"] not in keys

    # Once r2 also posts a settled direct reply on ask_multi, ask_multi becomes eligible in to_close.
    _row(
        owner_conn, {**r2, "label": r2["name"]},
        {"to": [asker["name"]], "thread_id": thread, "in_reply_to": ask_multi["key"],
         "type": "answer", "body": "a4-r2"},
        created_at=now - _dt.timedelta(minutes=12),
    )
    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()

    resp2 = client.get("/v1/mesh/brief", headers=bearer(asker))
    assert resp2.status_code == 200
    keys2 = [item["key"] for item in resp2.json()["to_close"]["items"]]
    assert ask_multi["key"] in keys2


def test_cli_close_thread_and_send_declines_enforce_read_ledger(
    client, owner_conn, world, monkeypatch, tmp_path, capsys
):
    caller = world["caller"]
    peer = caller["peer_id"]
    ns = "shared/mesh-chat"
    claimant = _mesh_claimant(owner_conn)
    _grant(owner_conn, ns, caller, claimant, permission="write")
    caller_label = f"peer-{str(peer)[:8]}"
    other = _peer(owner_conn, "cli-other-" + uuid.uuid4().hex[:6])
    _grant(owner_conn, ns, other, claimant, permission="write")

    now = _dt.datetime.now(_dt.timezone.utc)
    thread = "cli-tc-" + uuid.uuid4().hex[:10]

    # 1. Caller authored an ask; `other` answered it 15 minutes ago without closing.
    ask = _row(owner_conn, {**caller, "label": caller_label},
               {"to": [other["name"]], "thread_id": thread, "requires_ack": True, "type": "question", "body": "status?"},
               created_at=now - _dt.timedelta(minutes=20))
    reply = _row(owner_conn, {**other, "label": other["name"]},
                 {"to": [caller_label], "thread_id": thread, "in_reply_to": ask["key"], "type": "answer", "body": "done"},
                 created_at=now - _dt.timedelta(minutes=15))
    # 2. `other` authored an ask to caller so we can test `send --declines` anti-blind-close.
    inbound_ask = _row(owner_conn, {**other, "label": other["name"]},
                       {"to": [caller_label], "thread_id": thread, "requires_ack": True, "type": "request", "body": "please review"},
                       created_at=now - _dt.timedelta(minutes=10))
    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()

    path = _state(tmp_path, "http://testserver")

    def fake_get(p, st, route, query=None, **kw):
        resp = client.get(route, query or {}, headers=bearer(caller))
        assert resp.status_code == 200, resp.content
        return resp.json()

    landed = []

    def fake_write(p, st, **kw):
        args = dict(kw["args"])
        args["value"] = dict(args["value"], **{"from": caller_label})
        args["key"] = f"msg/{int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)}-{caller_label}-{uuid.uuid4().hex[:6]}"
        env = signed(caller, world["memory_peer"], "memory.write", args)
        resp = invoke(client, caller, world["memory_peer"], "memory.write", env)
        assert resp.status_code == 202 and resp.json()["status"] == "completed", resp.content
        landed.append(args)
        return {"status": "completed", "result": {"key": args["key"], "version": 1}, "result_signature_verified": True}

    monkeypatch.setattr(node, "_get", fake_get)
    monkeypatch.setattr(node, "invoke", fake_write)

    # Empty read ledger -> `close-thread` and `send --declines` both refuse blind settlement (exit 2)!
    monkeypatch.setattr(node, "ledger_records", lambda *a: [])
    rc = cli.main(["close-thread", "--state-file", str(path), "--base-url", "http://testserver", "--thread", thread])
    out = capsys.readouterr()
    assert rc == 2 and reply["key"] in (out.out + out.err) and landed == []

    rc_dec = cli.main(["send", "--state-file", str(path), "--base-url", "http://testserver",
                       "--to", other["name"], "--thread", thread, "--type", "note",
                       "--declines", inbound_ask["key"], "--body", "cannot take this",
                       "--no-recipient-check", "--no-card-check"])
    out_dec = capsys.readouterr()
    assert rc_dec == 2 and inbound_ask["key"] in (out_dec.out + out_dec.err) and landed == []

    # Populate read ledger with latest reply and inbound_ask -> both succeed!
    monkeypatch.setattr(node, "ledger_records", lambda *a: [
        {"key": reply["key"], "version": 1, "at": "2026-11-14T06:00:00Z"},
        {"key": inbound_ask["key"], "version": 1, "at": "2026-11-14T06:01:00Z"},
    ])
    caller_primary = client.get("/v1/mesh/brief", headers=bearer(caller)).json()["label"]
    base_args = ["close-thread", "--state-file", str(path), "--base-url", "http://testserver", "--thread", thread]
    for override in (["--to", "bob,alice"], ["--to", "bob", "--to", "alice"]):
        assert cli.main([*base_args, *override, "--dry-run"]) == 0
        assert json.loads(capsys.readouterr().out)["batches"][0]["to"] == ["bob", "alice"]
    assert cli.main([*base_args, "--to", "bob", "--notify"]) == 2
    capsys.readouterr()
    assert cli.main([*base_args, "--notify", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["batches"][0]["to"] == [other["name"]]

    rc_dry = cli.main([*base_args, "--dry-run"])
    dry_json = json.loads(capsys.readouterr().out)
    assert rc_dry == 0 and dry_json["dry_run"] is True and dry_json["sent"] is False
    assert dry_json["batches"][0]["closes"] == [ask["key"]] and dry_json["batches"][0]["to"] == [caller_primary]

    other_inbox_before = [h["key"] for h in client.get("/v1/mesh/inbox", headers=bearer(other)).json()["hits"]]
    other_unread_before = client.get("/v1/mesh/unread", headers=bearer(other)).json()["unread"]["total"]

    rc_ok = cli.main(base_args)
    ok_json = json.loads(capsys.readouterr().out)
    assert rc_ok == 0 and ok_json["sent"] is True and ok_json["closed"] == [ask["key"]]
    assert landed[-1]["value"]["to"] == [caller_primary]

    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()
    assert [h["key"] for h in client.get("/v1/mesh/inbox", headers=bearer(other)).json()["hits"]] == other_inbox_before
    assert client.get("/v1/mesh/unread", headers=bearer(other)).json()["unread"]["total"] == other_unread_before
    assert graph.owed_acks(owner_conn, [other["name"]], keys=[ask["key"]], states=["closed"])[0].state == "closed"
    assert any(a.hit.key == ask["key"] and a.state == "closed"
               for a in graph.awaiting(owner_conn, [caller_label], states=["closed"]))
    assert client.get("/v1/mesh/brief", headers=bearer(caller)).json()["to_close"]["items"] == []

    # Second settled ask closed with `--notify` addresses the owing seat explicitly.
    ask2 = _row(owner_conn, {**caller, "label": caller_label},
                {"to": [other["name"]], "thread_id": thread, "requires_ack": True, "type": "question", "body": "q2"},
                created_at=now - _dt.timedelta(minutes=18))
    rep2 = _row(owner_conn, {**other, "label": other["name"]},
                {"to": [caller_label], "thread_id": thread, "in_reply_to": ask2["key"], "type": "answer", "body": "a2"},
                created_at=now - _dt.timedelta(minutes=12))
    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()
    monkeypatch.setattr(node, "ledger_records", lambda *a: [
        {"key": rep2["key"], "version": 1, "at": "2026-11-14T06:02:00Z"},
        {"key": inbound_ask["key"], "version": 1, "at": "2026-11-14T06:01:00Z"},
    ])
    assert cli.main(["close-thread", "--state-file", str(path), "--base-url", "http://testserver",
                     "--key", ask2["key"], "--notify"]) == 0
    capsys.readouterr()
    assert landed[-1]["value"]["to"] == [other["name"]] and landed[-1]["value"]["closes"] == [ask2["key"]]

    rc_dec_ok = cli.main(["send", "--state-file", str(path), "--base-url", "http://testserver",
                          "--to", other["name"], "--thread", thread, "--type", "note",
                          "--declines", inbound_ask["key"], "--body", "declining after reading",
                          "--no-recipient-check", "--no-card-check"])
    capsys.readouterr()
    assert rc_dec_ok == 0
    assert landed[-1]["value"]["declines"] == [inbound_ask["key"]]

    owner_conn.commit()
    projection.rebuild(owner_conn)
    owner_conn.commit()
    assert graph.owed_acks(owner_conn, [caller_label], keys=[inbound_ask["key"]], states=["declined"])[0].state == "declined"


def test_cli_close_thread_refuses_truncated_and_partial_multi_recipient_custody(monkeypatch, tmp_path, capsys):
    now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
    ask_k = f"msg/{now_ms - 9000}-alice-111111"
    r1_k = f"msg/{now_ms - 8000}-bob-222222"
    r2_k = f"msg/{now_ms - 7000}-carol-333333"
    path = _state(tmp_path, "http://testserver")
    row1 = {"key": ask_k, "version": 1, "thread_id": "t-trunc", "owing_label": "bob",
            "latest_reply_key": r1_k, "latest_reply_version": 1}
    row2 = {"key": ask_k, "version": 1, "thread_id": "t-trunc", "owing_label": "carol",
            "latest_reply_key": r2_k, "latest_reply_version": 1}

    # Complete multi-recipient ask: bob's reply read, carol's unread -> refuses naming carol's reply.
    monkeypatch.setattr(node, "brief", lambda *a, **kw: {"label": "alice", "to_close": {"items": [row1, row2], "truncated": False}})
    monkeypatch.setattr(node, "ledger_records", lambda *a: [{"key": r1_k, "version": 1}])
    rc_part = cli.main(["close-thread", "--state-file", str(path), "--base-url", "http://testserver", "--key", ask_k])
    out_part = capsys.readouterr()
    assert rc_part == 2 and r2_k in (out_part.out + out_part.err)

    # Truncated to_close (>100 rows; carol's unread reply omitted from page 1) -> refuses both --key and --thread!
    padded = [row1] + [{"key": f"msg/{now_ms - i}-alice-000000", "version": 1, "thread_id": "other",
                        "owing_label": "dave", "latest_reply_key": r1_k, "latest_reply_version": 1} for i in range(1, 100)]
    monkeypatch.setattr(node, "brief", lambda *a, **kw: {"label": "alice", "to_close": {"items": padded, "truncated": True}})
    for target in (["--key", ask_k], ["--thread", "t-trunc"]):
        rc_tr = cli.main(["close-thread", "--state-file", str(path), "--base-url", "http://testserver", *target])
        out_tr = capsys.readouterr()
        assert rc_tr == 2 and "100 row(s)" in (out_tr.out + out_tr.err) and "truncated" in (out_tr.out + out_tr.err)
    assert cli.main(["close-thread", "--state-file", str(path), "--base-url", "http://testserver",
                     "--key", ask_k, "--force-blind-close", "--notify"]) == 2
    capsys.readouterr()
    assert cli.main(["close-thread", "--state-file", str(path), "--base-url", "http://testserver",
                     "--key", ask_k, "--force-blind-close", "--dry-run"]) == 0
    forced = json.loads(capsys.readouterr().out)
    assert forced["to_close_truncated"] is True and forced["batches"][0]["to"] == ["alice"]
