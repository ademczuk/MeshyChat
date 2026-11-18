"""`presence` from the real client against the real server with DPoP at its default, ENFORCED (Codex on #144).

The recording-server tests in tests/test_meshy_node_presence.py check what the client sends. This one checks that the
server takes it: a bound operator node runs `presence --state asleep --note compacting --ttl 900` over HTTP, and the
row it wrote is read back. Positive control: the same POST with a bearer and NO proof is refused 403, so the
server really does enforce DPoP here and the client's success is not a server that would take anything.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from django.test import override_settings  # noqa: E402

from apps.authapi import ratelimit  # noqa: E402
from tests.test_fresh_install_runbook import (Runbook, _empty_database,  # noqa: E402
                                              _jwt_pair, _serve)
from tools.meshy_node import node  # noqa: E402


def test_presence_reaches_a_server_that_enforces_dpop_and_a_bare_bearer_does_not(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    ratelimit.reset_all()
    with pytest.MonkeyPatch.context() as mp, _empty_database("presence") as dsn:
        rb = Runbook(f"http://127.0.0.1:{sock.getsockname()[1]}")
        code, out = rb.migrate(dsn)
        assert code == 0, out
        jwt_priv, jwt_pub = _jwt_pair(tmp_path)
        op, op_pub = tmp_path / "operator.json", tmp_path / "operator.pub"
        code, _ = rb.node("keygen", op, "--public-key-out", op_pub)
        assert code == 0
        mp.setenv("MESHY_DSN", dsn)
        with override_settings(MESHY_DSN=dsn):
            out, _ = rb.manage("bootstrap_owner", "--email", "ops@example.test",
                               "--public-key-file", str(op_pub), "--plane", "dev",
                               "--trust-group", str(uuid.uuid4()))
            owner = json.loads(out)
        env = {
            "MESHY_DSN": dsn, "MESHYCHAT_JWT_ISSUER": "meshychat-presence-test",
            "MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": str(jwt_priv),
            "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": str(jwt_pub),
            "MESHYCHAT_STATE_SECRET": os.urandom(32).hex(), "MESHY_ALLOWED_HOSTS": "127.0.0.1",
            "MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID": owner["peer_id"],
            "MESHY_EMBED_FORBIDDEN_ROWS": "true", "MESHY_MODEL_PROVIDERS": "[]",
        }
        with _serve(env, mp, sock):
            code, bound = rb.node("bind", op, "--peer-id", owner["peer_id"])
            assert code == 0, bound

            # Positive control: a bearer without a proof is refused, so DPoP is enforced on this server.
            state = node.load_state(op, rb.base_url)
            jwt = node.fresh_jwt(op, state)
            status, body = node.http_json("POST", rb.base_url, "/v1/peers/me/presence",
                                          body={"state": "active"}, headers=node._bearer(jwt))
            assert status == 403, (status, body)

            code, answer = rb.node("presence", op, "--state", "asleep", "--note", "compacting", "--ttl", "900")
            assert code == 0 and answer == {"ok": True}, (code, answer)

        with psycopg.connect(dsn) as conn:
            row = conn.execute("SELECT state, note, state_expires_at > NOW() + interval '800 seconds' "
                               "FROM mesh_presence WHERE peer_id = %s", (owner["peer_id"],)).fetchone()
        assert row == ("asleep", "compacting", True), row
