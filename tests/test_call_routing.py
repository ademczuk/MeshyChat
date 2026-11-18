"""Step 9: a signed memory.* call EXECUTES, and nothing is left pending. Divergence 017.

THE GAP THIS FILE IS PROVED AGAINST, measured on the pre-change code: a signed
`memory.write` sent to `POST /v1/agents/{memory_peer}/skills/memory.write/invoke` passed
verification and authorization, landed in `skill_calls` as `pending`, and stayed there.
Nothing dispatched to apps/memoryskills, no row reached `shared_memory`, and the node
client polled `GET /v1/calls/{id}` until its own 40 s timeout. A call to any other peer
stayed `pending` the same way, and no janitor existed to end either.

HOUSE RULES (docs/V1-DESIGN-RULES.md). Every refusal sits beside its positive control in the
SAME test, differing in the one variable under test (C1): the namespace-grant refusal
beside a granted write, the unroutable target beside a memory target, the missing signer
beside a present one, the expired call beside a live one. Fields are read by explicit
path, never by substring (C4). The signature is checked with a DIFFERENT instrument from
the one that made it: the public key is fetched from the served JWKS route, not from the
signer object (C5).

NO ROOT_URLCONF OVERRIDE: every request resolves through `meshychat.urls`, as a node's does.

Run:  .venv/Scripts/python.exe -m pytest tests/test_call_routing.py -q
"""
from __future__ import annotations

import base64
import datetime as _dt
import io
import json
import os
import runpy
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("MESHY_TEST_DSN", DSN)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
os.environ.setdefault("MESHY_DSN", DSN)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover - depends on collection order
    django.setup()

import jwt as pyjwt  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.calls import janitor, reads, repository  # noqa: E402
from apps.calls import route as routing  # noqa: E402
from apps.calls.bootstrap import MANIFEST_SKILLS  # noqa: E402
from apps.calls.envelope import SignedEnvelope  # noqa: E402
from apps.core import dbconn, rls  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.jobs import registry  # noqa: E402

ISSUER = "meshy-routing-test"

#: ONE server key, as in production: it mints the peer JWTs these tests present AND signs
#: the results the router records, and /.well-known/jwks.json publishes it.
_server_key = ed25519.Ed25519PrivateKey.generate()
SERVER_PRIVATE_PEM = _server_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
SERVER_PUBLIC_PEM = _server_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

# ---------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def server_config():
    """The verify key for peer tokens (settings) and the SAME key as the minter's signing
    key (authapi config), which is where the result signer reads it from."""
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=SERVER_PUBLIC_PEM.decode("ascii"),
        MESHY_JWT_ISSUER=ISSUER,
        # MESHY_ENVELOPE_NONCE_ENFORCE is deliberately NOT pinned: the nonce gate runs
        # at its shipped default, ON (divergence 036).
        MESHY_SYNC_TIMEOUT_SECONDS=0.0,
    ), authconfig.override_config(jwt_signing_key_pem=SERVER_PRIVATE_PEM,
                                  jwt_public_key_pem=SERVER_PUBLIC_PEM,
                                  jwt_issuer=ISSUER):
        yield


@pytest.fixture()
def owner_conn():
    """The bootstrap role, which BYPASSES RLS: seeding and independent read-back only, so
    "the row is absent" means absent rather than invisible to the app's context."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def client():
    return Client()


def _owner(conn, tag: str, trust_group: uuid.UUID) -> uuid.UUID:
    owner_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"routing-{tag}-{owner_id.hex[:8]}@example.test", trust_group))
    conn.commit()
    return owner_id


def _peer_with_key(conn, owner_id: uuid.UUID, tag: str) -> dict:
    peer_id = uuid.uuid4()
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"routing-{tag}-{peer_id.hex[:8]}", tag))
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    conn.commit()
    return {"peer_id": peer_id, "owner_id": owner_id, "seed": seed, "kid": kid}


@pytest.fixture()
def world(owner_conn):
    """A memory authority made by THE COMMAND, not by hand, plus the peers around it.

    Every peer here is in ONE dev trust group with the memory owner, so step 5's
    same-group allow passes for all of them and the namespace grant is the ONLY thing
    that differs between `caller` and `outsider`. `other` is an ordinary peer in the same
    group that owns a skill named `memory.write` of its own: the one routing must NOT
    execute, because routing is by TARGET PEER, never by skill name.
    """
    trust_group = uuid.uuid4()
    memory_peer = uuid.uuid4()
    out = io.StringIO()
    with override_settings(MESHY_MEMORY_PEER_ID=str(memory_peer)):
        call_command("bootstrap_memory_peer", "--trust-group", str(trust_group),
                     "--owner-email", f"routing-mem-{memory_peer.hex[:8]}@example.test",
                     stdout=out, stderr=io.StringIO())
    assert "CREATED" in out.getvalue(), out.getvalue()
    with owner_conn.cursor() as cur:
        cur.execute("SELECT owner_id FROM peers WHERE id = %s", (memory_peer,))
        memory_owner = cur.fetchone()[0]
    owner_conn.commit()

    caller = _peer_with_key(owner_conn, _owner(owner_conn, "caller", trust_group), "caller")
    outsider = _peer_with_key(owner_conn, _owner(owner_conn, "outsider", trust_group),
                              "outsider")
    other = _peer_with_key(owner_conn, _owner(owner_conn, "other", trust_group), "other")
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
            "  is_current) VALUES (%s, 'memory.write', '0.2.0', 'sync', 'grants_only', "
            "  '{}', TRUE)", (other["peer_id"],))
    owner_conn.commit()

    namespace = f"routingtest/{uuid.uuid4().hex[:12]}"
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                    "  owner_owner_id) VALUES (%s,%s,%s)",
                    (namespace, memory_peer, memory_owner))
        cur.execute(
            "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
            "  permission, granted_by_peer_id, granted_by_owner_id) "
            "VALUES (%s,%s,%s,'write',%s,%s)",
            (namespace, caller["peer_id"], caller["owner_id"], memory_peer, memory_owner))
    owner_conn.commit()
    with override_settings(MESHY_MEMORY_PEER_ID=str(memory_peer)):
        yield {"memory_peer": memory_peer, "memory_owner": memory_owner,
               "trust_group": trust_group, "caller": caller, "outsider": outsider,
               "other": other, "namespace": namespace}


# ----------------------------------------------------------------------------- helpers

def bearer(peer: dict) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    token = pyjwt.encode({"sub": str(peer["peer_id"]), "aud": "peer", "iss": ISSUER,
                          "iat": now, "exp": now + 300, "scopes": ["skills:invoke"]},
                         SERVER_PRIVATE_PEM, algorithm="EdDSA")
    return {"authorization": f"Bearer {token}"}


def signed(peer: dict, target, skill: str, args: dict) -> dict:
    """The envelope exactly as mem_client.build_envelope builds it (mem_client.py:552-581):
    the same field set, JCS bytes, Ed25519 over nothing else."""
    payload = {
        "v": 1, "kind": "skill.call", "call_id": str(uuid.uuid4()),
        "caller_peer_id": str(peer["peer_id"]), "target_peer_id": str(target),
        "skill_name": skill, "skill_version": "0.2.0", "args": args,
        "args_attachments": [],
        "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "nonce": uuid.uuid4().hex,
    }
    return {"payload": payload, "signature": P.b64u(P.sign(peer["seed"], P.jcs(payload))),
            "signing_kid": peer["kid"]}


def invoke(client, peer: dict, target, skill: str, envelope: dict, mode="async"):
    """POST as mem_client.invoke does (mem_client.py:839-849): mode_override, and the
    transport call_id echoing the signed one."""
    body = {"envelope": envelope, "mode_override": mode,
            "call_id": envelope["payload"]["call_id"]}
    return client.post(f"/v1/agents/{target}/skills/{skill}/invoke",
                       data=json.dumps(body), content_type="application/json",
                       headers=bearer(peer))


def poll(client, peer: dict, call_id: str) -> dict:
    """GET /v1/calls/{id}, the request mem_client.poll_call makes (mem_client.py:652-661)."""
    response = client.get(f"/v1/calls/{call_id}", headers=bearer(peer))
    assert response.status_code == 200, response.content
    return json.loads(response.content)


def normalized(call_json: dict) -> dict:
    """The fields `mem_client._normalize` reads, by the paths it reads them
    (mem_client.py:695-727). KeyError on absence, never a default: a field the client
    reads must be PRESENT on the wire, even when null."""
    return {
        "call_id": call_json["call_id"],
        "status": call_json["status"],
        "result": call_json["result"],
        "error_code": call_json["error_code"],
        "error_message": call_json["error_message"],
        "result_signature": call_json["result_signature"],
        "result_signing_kid": call_json["result_signing_kid"],
    }


def server_key_from_jwks(client, kid: str) -> bytes:
    """Resolve the kid against the SERVED key set - a different instrument from the signer
    that produced the signature (C5)."""
    response = client.get("/.well-known/jwks.json")
    assert response.status_code == 200, response.content
    keys = {k["kid"]: k for k in json.loads(response.content)["keys"]}
    assert kid in keys, f"result_signing_kid {kid!r} is not in the published JWKS {list(keys)}"
    x = keys[kid]["x"]
    return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))


def events_of(owner_conn, call_id: str) -> list[tuple]:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT event_type, source, details FROM skill_call_events "
                    "WHERE call_id = %s ORDER BY id", (call_id,))
        rows = cur.fetchall()
    owner_conn.commit()
    return rows


def memory_rows(owner_conn, namespace: str, key: str) -> list[tuple]:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT id, version, created_by_peer_id, "
                    "       provenance_jsonb->'payload'->>'call_id', value "
                    "FROM shared_memory WHERE namespace = %s AND key = %s ORDER BY version",
                    (namespace, key))
        rows = cur.fetchall()
    owner_conn.commit()
    return rows


def call_row(owner_conn, call_id: str) -> dict:
    cols = ", ".join(reads.CALL_COLUMNS)
    with owner_conn.cursor() as cur:
        cur.execute(f"SELECT {cols} FROM skill_calls WHERE call_id = %s", (call_id,))
        row = cur.fetchone()
    owner_conn.commit()
    assert row is not None, f"call {call_id} was never recorded"
    return dict(zip(reads.CALL_COLUMNS, row))


# ================================================================ the memory authority

def test_the_memory_authority_defaults_to_the_id_node_clients_address(monkeypatch):
    """The default is the roster's `meshy-mem-daemon` id, the one every node client
    resolves (mem_client._resolve_daemon_peer -> fleet_roster.json). A malformed override
    refuses to LOAD rather than becoming a comparison that never matches at request time."""
    assert routing.DEFAULT_MEMORY_PEER_ID == uuid.UUID("29d6a9f7-ed27-5153-a9f2-f6feb3175e77")

    monkeypatch.delenv("MESHY_MEMORY_PEER_ID", raising=False)
    loaded = runpy.run_path(str(ROOT / "meshychat" / "settings.py"))
    assert loaded["MESHY_MEMORY_PEER_ID"] == str(routing.DEFAULT_MEMORY_PEER_ID)

    chosen = uuid.uuid4()
    monkeypatch.setenv("MESHY_MEMORY_PEER_ID", str(chosen).upper())    # control: honoured
    assert runpy.run_path(str(ROOT / "meshychat" / "settings.py"))[
        "MESHY_MEMORY_PEER_ID"] == str(chosen)

    monkeypatch.setenv("MESHY_MEMORY_PEER_ID", "not-a-peer-id")         # the refusal
    with pytest.raises(ValueError):
        runpy.run_path(str(ROOT / "meshychat" / "settings.py"))


def test_a_signed_memory_write_executes_and_the_call_reads_back_completed(
        client, owner_conn, world):
    """The whole gap, closed: invoke -> the handler runs -> the row lands -> the poll the
    node client makes reads `completed` with the write result and a verifiable signature.
    An idempotent re-send of the same envelope replays the result and does NOT write a
    second version."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    key = f"k-{uuid.uuid4().hex[:8]}"
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": key, "value": {"text": "routed"}})
    call_id = envelope["payload"]["call_id"]

    accepted = invoke(client, caller, memory_peer, "memory.write", envelope)
    assert accepted.status_code == 202, accepted.content
    assert json.loads(accepted.content)["status"] == "completed"

    rows = memory_rows(owner_conn, ns, key)
    assert len(rows) == 1, f"memory.write must land exactly one version, got {rows}"
    row_id, version, created_by, provenance_call_id, value = rows[0]
    assert version == 1 and created_by == caller["peer_id"]
    assert provenance_call_id == call_id, "provenance must be the envelope this call carried"
    assert value == {"text": "routed"}

    got = normalized(poll(client, caller, call_id))
    assert got["call_id"] == call_id
    assert got["status"] == "completed"
    assert got["error_code"] is None and got["error_message"] is None
    assert got["result"]["op"] == "memory.write"
    assert got["result"]["namespace"] == ns and got["result"]["key"] == key
    assert got["result"]["version"] == 1 and got["result"]["id"] == str(row_id)
    assert got["result"]["replay"] is False

    # The signature: over the STORED canonical payload, by the key the JWKS publishes.
    stored = call_row(owner_conn, call_id)
    assert stored["started_at"] is not None and stored["completed_at"] is not None
    payload = stored["result_payload"]
    assert payload["kind"] == "skill.result" and payload["v"] == 1
    assert payload["call_id"] == call_id and payload["status"] == "completed"
    assert payload["target_peer_id"] == str(memory_peer)
    assert payload["result"] == got["result"], "result and result_payload must be a PAIR"
    public = server_key_from_jwks(client, got["result_signing_kid"])
    signature = P.b64u_decode(got["result_signature"])
    assert P.verify_strict(public, P.jcs(payload), signature)
    # Negative control for the verification itself: the same signature over a payload
    # claiming the other terminal must NOT verify, or the check above proves nothing (E2).
    assert not P.verify_strict(public, P.jcs({**payload, "status": "failed"}), signature)

    assert [e[0] for e in events_of(owner_conn, call_id)] == \
        ["created", "running", "completed"]

    # Idempotent retry: the same envelope again replays; nothing executes twice.
    again = invoke(client, caller, memory_peer, "memory.write", envelope)
    assert again.status_code == 202, again.content
    assert json.loads(again.content)["status"] == "completed"
    assert len(memory_rows(owner_conn, ns, key)) == 1, "a replay re-executed the write"


def test_memory_query_via_invoke_returns_rows_under_result_results(
        client, owner_conn, world):
    """`result.results` is the path mem_cli reads rows from. The query runs SYNC, so the
    rows also come back INLINE in the 200 under the names the inline branch of
    `_normalize` reads (`signature`, `signing_kid`)."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    key = f"q-{uuid.uuid4().hex[:8]}"
    write = signed(caller, memory_peer, "memory.write",
                   {"namespace": ns, "key": key, "value": {"text": "find me"}})
    assert invoke(client, caller, memory_peer, "memory.write", write).status_code == 202

    query = signed(caller, memory_peer, "memory.query", {"namespace": ns, "key_prefix": "q-"})
    inline = invoke(client, caller, memory_peer, "memory.query", query, mode="sync")
    assert inline.status_code == 200, inline.content
    body = json.loads(inline.content)
    assert body["status"] == "completed"
    assert isinstance(body["signature"], str) and isinstance(body["signing_kid"], str)
    assert [r["key"] for r in body["result"]["results"]] == [key]
    assert body["result"]["results"][0]["value"] == {"text": "find me"}
    assert body["result"]["count"] == 1 and body["result"]["next_cursor"] is None

    polled = normalized(poll(client, caller, query["payload"]["call_id"]))
    assert polled["result"]["results"] == body["result"]["results"]


def test_a_caller_without_the_namespace_grant_is_failed_not_pending(
        client, owner_conn, world):
    """Two callers, one difference: the namespace grant. Both pass step 5 (same dev trust
    group, no skill grant needed), so the refusal is the HANDLER's namespace gate, and the
    call ends `failed` with a signed domain error rather than sitting `pending`."""
    memory_peer, ns = world["memory_peer"], world["namespace"]

    ok = signed(world["caller"], memory_peer, "memory.write",
                {"namespace": ns, "key": "granted", "value": {"n": 1}})
    assert invoke(client, world["caller"], memory_peer, "memory.write", ok).status_code == 202
    assert poll(client, world["caller"], ok["payload"]["call_id"])["status"] == "completed"
    assert len(memory_rows(owner_conn, ns, "granted")) == 1          # the control landed

    denied = signed(world["outsider"], memory_peer, "memory.write",
                    {"namespace": ns, "key": "ungranted", "value": {"n": 2}})
    response = invoke(client, world["outsider"], memory_peer, "memory.write", denied)
    assert response.status_code == 202, response.content    # accepted by the pipeline
    got = normalized(poll(client, world["outsider"], denied["payload"]["call_id"]))
    assert got["status"] == "failed", got
    assert got["error_code"] == "E_PERMISSION_DENIED"
    assert got["result"] == {"ok": False, "error_code": "E_PERMISSION_DENIED",
                             "error_message": got["error_message"]}
    assert got["result_signature"] is not None, "a domain failure is SIGNED, as the daemon's is"
    assert memory_rows(owner_conn, ns, "ungranted") == [], "a refused write must not land"

    row = call_row(owner_conn, denied["payload"]["call_id"])
    assert row["grant_id"] is None, "step 5 allowed it by trust group: the refusal is step 9's"
    public = server_key_from_jwks(client, row["result_signing_kid"])
    assert P.verify_strict(public, P.jcs(row["result_payload"]),
                           P.b64u_decode(row["result_signature"]))
    assert row["result_payload"]["status"] == "failed"
    assert row["result_payload"]["error_code"] == "E_PERMISSION_DENIED"


def test_a_call_to_any_other_peer_is_queued_for_it_and_never_executes_here(
        client, owner_conn, world):
    """Routing is by TARGET PEER. `other` registered its own `memory.write`, and an envelope
    signed for it carries the same skill name and the same args a real memory write would.
    It must NOT run here: since divergence 041 S5 it is queued for `other` (which holds no
    socket) until its deadline, where divergence 017 failed it with
    E_TARGET_UNREACHABLE. The identical call addressed to the memory authority (the
    control) executes."""
    caller, ns = world["caller"], world["namespace"]
    args = {"namespace": ns, "key": "routed-by-peer", "value": {"n": 1}}

    control = signed(caller, world["memory_peer"], "memory.write", args)
    assert invoke(client, caller, world["memory_peer"], "memory.write",
                  control).status_code == 202
    assert len(memory_rows(owner_conn, ns, "routed-by-peer")) == 1

    elsewhere = signed(caller, world["other"]["peer_id"], "memory.write",
                       {**args, "key": "must-not-land"})
    response = invoke(client, caller, world["other"]["peer_id"], "memory.write", elsewhere)
    assert response.status_code == 202, response.content
    assert json.loads(response.content)["status"] == "queued"

    got = normalized(poll(client, caller, elsewhere["payload"]["call_id"]))
    assert got["status"] == "queued"
    assert got["error_code"] is None
    assert got["result"] is None and got["result_signature"] is None, \
        "nothing executed, so nothing is signed"
    assert memory_rows(owner_conn, ns, "must-not-land") == []
    row = call_row(owner_conn, elsewhere["payload"]["call_id"])
    assert row["started_at"] is None, "a queued call never enters running here"
    assert row["completed_at"] is None
    assert [(e[0], e[1]) for e in events_of(owner_conn, row["call_id"])] == \
        [("created", "meshy"), ("queued", "meshy")]


def test_no_signer_fails_the_call_before_the_skill_runs(client, owner_conn, world):
    """FAIL CLOSED. Without a server key the result could not be signed, so the write must
    not happen at all - not happen and then report unsigned. Control: the same call shape
    with the key present lands."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]

    control = signed(caller, memory_peer, "memory.write",
                     {"namespace": ns, "key": "signed-ok", "value": {"n": 1}})
    assert invoke(client, caller, memory_peer, "memory.write", control).status_code == 202
    assert len(memory_rows(owner_conn, ns, "signed-ok")) == 1

    unsigned = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": "no-signer", "value": {"n": 2}})
    with authconfig.override_config(jwt_signing_key_pem=None):
        response = invoke(client, caller, memory_peer, "memory.write", unsigned)
    assert response.status_code == 202, response.content
    got = normalized(poll(client, caller, unsigned["payload"]["call_id"]))
    assert got["status"] == "failed" and got["error_code"] == routing.E_NO_SIGNING_KEY
    assert memory_rows(owner_conn, ns, "no-signer") == [], "executed without a signer"
    assert call_row(owner_conn, unsigned["payload"]["call_id"])["started_at"] is None


def test_an_unverified_call_to_the_memory_authority_is_not_executed(owner_conn, world):
    """The one unsigned transport (dashboard_test) reaches step 9 with `verified=None`. The
    handler writes provenance asserting a verified signature, so it must not run. Driven
    at the router directly, over a call made by the real insert; control: the same router
    with a verified payload executes."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    with dbconn.serving_connection() as conn:
        outcomes = {}
        for label, key in (("verified", "direct-ok"), ("unverified", "direct-no")):
            env = signed(caller, memory_peer, "memory.write",
                         {"namespace": ns, "key": key, "value": {"n": 1}})
            _insert_pending(conn, world, env["payload"]["call_id"], payload=env["payload"],
                            target=memory_peer, skill="memory.write")
            verified = None
            if label == "verified":
                from apps.calls.envelope import verify_payload
                verified = verify_payload(conn, SignedEnvelope(**env), caller["peer_id"],
                                          "skill.call")
            outcomes[label] = routing.route(
                conn, memory_peer_id=memory_peer, call_id=env["payload"]["call_id"],
                caller_peer_id=caller["peer_id"], target_peer_id=memory_peer,
                skill_name="memory.write", envelope=SignedEnvelope(**env),
                verified=verified, verified_at=_dt.datetime.now(_dt.timezone.utc))
    assert outcomes == {"verified": "completed", "unverified": "failed"}
    assert len(memory_rows(owner_conn, ns, "direct-ok")) == 1
    assert memory_rows(owner_conn, ns, "direct-no") == []


def test_a_call_that_left_pending_before_routing_is_not_executed(owner_conn, world):
    """Every transition is guarded on the status it expects. A call cancelled (or timed
    out) between its insert and its route must not run: the pending -> running UPDATE
    matches nothing and the router reports the status it found. Control: the identical
    call, still pending, executes."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    from apps.calls.envelope import verify_payload
    outcomes = {}
    with dbconn.serving_connection() as conn:
        for label, key in (("pending", "still-pending"), ("abandoned", "was-cancelled")):
            env = signed(caller, memory_peer, "memory.write",
                         {"namespace": ns, "key": key, "value": {"n": 1}})
            call_id = env["payload"]["call_id"]
            _insert_pending(conn, world, call_id, payload=env["payload"],
                            target=memory_peer, skill="memory.write")
            if label == "abandoned":
                with rls.server_internal(conn), conn.cursor() as cur:
                    cur.execute("UPDATE skill_calls SET status = 'abandoned', "
                                "completed_at = NOW() WHERE call_id = %s", (call_id,))
            outcomes[label] = routing.route(
                conn, memory_peer_id=memory_peer, call_id=call_id,
                caller_peer_id=caller["peer_id"], target_peer_id=memory_peer,
                skill_name="memory.write", envelope=SignedEnvelope(**env),
                verified=verify_payload(conn, SignedEnvelope(**env), caller["peer_id"],
                                        "skill.call"),
                verified_at=_dt.datetime.now(_dt.timezone.utc))
    assert outcomes == {"pending": "completed", "abandoned": "abandoned"}
    assert len(memory_rows(owner_conn, ns, "still-pending")) == 1
    assert memory_rows(owner_conn, ns, "was-cancelled") == [],         "a call that was no longer pending was executed anyway"


# ======================================================================== the janitor

def _insert_pending(conn, world, call_id: str, *, deadline=None, payload=None,
                    target=None, skill="memory.write") -> None:
    """A call made by the REAL insert (repository.insert_call), not hand-written SQL, so
    the janitor is tested against exactly the rows the pipeline writes."""
    target = target or world["other"]["peer_id"]
    with conn.cursor() as cur:
        cur.execute("SELECT id, version FROM skills WHERE peer_id = %s AND name = %s "
                    "AND is_current", (str(target), skill))
        skill_id, version = cur.fetchone()
        cur.execute("SELECT owner_id FROM peers WHERE id = %s", (str(target),))
        target_owner = cur.fetchone()[0]
    repository.insert_call(
        conn, call_id=call_id, caller_peer_id=world["caller"]["peer_id"],
        caller_owner_id=world["caller"]["owner_id"], target_peer_id=target,
        target_owner_id=target_owner, skill_id=skill_id, skill_name=skill,
        skill_version=version, mode="sync", transport="http",
        payload=payload or {"call_id": call_id}, args_signature=None,
        args_signing_kid=None, args_attachments=[], grant_id=None,
        deadline_at=deadline or _dt.datetime.now(_dt.timezone.utc)
        + _dt.timedelta(minutes=5))


def test_the_janitor_times_out_expired_calls_in_bounded_batches(owner_conn, world):
    """Three expired in-flight calls and one live one. A bound of 2 takes exactly the two
    oldest; the job body takes the third; the live call survives every sweep.

    The expired deadlines are in the year 2000 ON PURPOSE. The sweep is table-wide by
    design and ordered oldest-deadline first, so other tests' expired rows could otherwise
    sort ahead of these and make "exactly my two" a statement about test order.
    """
    epoch = _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc)
    ids = {name: f"janitor-{name}-{uuid.uuid4().hex[:8]}"
           for name in ("oldest", "middle", "newest", "live")}
    with dbconn.serving_connection() as conn:
        for offset, name in enumerate(("oldest", "middle", "newest")):
            _insert_pending(conn, world, ids[name],
                            deadline=epoch + _dt.timedelta(seconds=offset))
        _insert_pending(conn, world, ids["live"])
        with rls.server_internal(conn), conn.cursor() as cur:
            # One of them `running`: the sweep covers every in-flight status, not pending.
            cur.execute("UPDATE skill_calls SET status = 'running' WHERE call_id = %s",
                        (ids["middle"],))

        swept = janitor.sweep_expired_deadlines(conn, limit=2)
        assert sorted(swept) == sorted([ids["oldest"], ids["middle"]]), swept
        assert call_row(owner_conn, ids["newest"])["status"] == "pending", \
            "the bound was not honoured"

        job = registry.by_name("call_deadline_sweep")
        assert job.run is janitor.run_deadline_sweep_job
        assert job.run(conn, job) >= 1

    for name in ("oldest", "middle", "newest"):
        row = call_row(owner_conn, ids[name])
        assert row["status"] == "timeout", (name, row["status"])
        assert row["completed_at"] is not None
        assert row["error_code"] == janitor.E_DEADLINE_EXCEEDED
        assert [e[0] for e in events_of(owner_conn, ids[name])] == ["created", "timeout"]
    assert "while the call was running" in call_row(owner_conn, ids["middle"])["error_message"]
    assert "while the call was pending" in call_row(owner_conn, ids["oldest"])["error_message"]
    live = call_row(owner_conn, ids["live"])
    assert live["status"] == "pending" and live["error_code"] is None, \
        "a call inside its deadline must never be swept"

    with pytest.raises(ValueError):
        janitor.sweep_expired_deadlines(owner_conn, limit=0)


# ======================================================================= the bootstrap

def test_bootstrap_memory_peer_creates_once_and_refuses_to_reown(owner_conn):
    """A fresh id: created with the daemon's six manifest skills (not memory.prune). A
    second run creates nothing. Naming a different owner for an existing peer, or
    bootstrapping a suspended one, is refused and changes nothing."""
    peer_id = uuid.uuid4()
    first, second = io.StringIO(), io.StringIO()
    with override_settings(MESHY_MEMORY_PEER_ID=str(peer_id)):
        call_command("bootstrap_memory_peer", stdout=first, stderr=io.StringIO())
        call_command("bootstrap_memory_peer", stdout=second, stderr=io.StringIO())
    assert "CREATED" in first.getvalue() and "CREATED" not in second.getvalue(), \
        (first.getvalue(), second.getvalue())

    with owner_conn.cursor() as cur:
        cur.execute("SELECT p.owner_id, p.status, p.name, u.plane, u.trust_group_id "
                    "FROM peers p JOIN dashboard_users u ON u.id = p.owner_id "
                    "WHERE p.id = %s", (peer_id,))
        owner_id, status, name, plane, group = cur.fetchone()
        cur.execute("SELECT name FROM skills WHERE peer_id = %s AND is_current "
                    "AND deleted_at IS NULL ORDER BY name", (peer_id,))
        skills = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT id FROM dashboard_users WHERE id <> %s LIMIT 1", (owner_id,))
        someone_else = cur.fetchone()[0]
    owner_conn.commit()
    assert status == "active" and name == "meshy-mem-daemon"
    assert plane == "dev" and group is not None
    assert skills == sorted(MANIFEST_SKILLS), "one current row per manifest skill"
    assert "memory.prune" not in skills

    with override_settings(MESHY_MEMORY_PEER_ID=str(peer_id)):
        with pytest.raises(CommandError, match="Refusing to re-own"):
            call_command("bootstrap_memory_peer", "--owner-id", str(someone_else),
                         stdout=io.StringIO(), stderr=io.StringIO())
        with owner_conn.cursor() as cur:
            cur.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (peer_id,))
        owner_conn.commit()
        with pytest.raises(CommandError, match="suspended"):
            call_command("bootstrap_memory_peer", stdout=io.StringIO(),
                         stderr=io.StringIO())
    with owner_conn.cursor() as cur:
        cur.execute("SELECT owner_id, status FROM peers WHERE id = %s", (peer_id,))
        assert cur.fetchone() == (owner_id, "suspended"), "a refusal changed the peer"
    owner_conn.commit()


# ======================================================== F07: the receipt's issuer and kid

class ReceiptKeyUnresolved(AssertionError):
    """The receipt's (issuer, kid) names no key under that issuer. Its own type, kept from
    the strict xfail that pinned F07 before operator decision 38, so a regression of the
    registration fails with THIS name rather than as an ordinary control failure."""


def resolve_under_issuer(owner_conn, client, issuer: str, kid: str) -> bytes | None:
    """What a receipt verifier does (meshy_receipt_verify.py keys its map on (issuer, kid)):
    a PEER issuer resolves in that peer's active `peer_keys`; the SERVER, named by its JWT
    issuer, resolves in /.well-known/jwks.json. Nothing else resolves."""
    try:
        peer_id = uuid.UUID(issuer)
    except ValueError:
        peer_id = None
    if peer_id is not None:
        with owner_conn.cursor() as cur:
            cur.execute("SELECT public_key FROM peer_keys WHERE peer_id = %s AND kid = %s "
                        "AND active", (peer_id, kid))
            row = cur.fetchone()
        owner_conn.commit()
        return None if row is None else P.b64u_decode(row[0])
    if issuer == ISSUER:
        return server_key_from_jwks(client, kid)
    return None


def test_a_memory_receipts_issuer_and_kid_resolve_to_the_key_that_signed_it(
        client, owner_conn, world):
    """F07, CLOSED by operator decision 38 (2026-09-26, option C; docs/DIVERGENCES.md 017,
    "Known and not fixed here"). The server's result-signing key is REGISTERED under the
    memory peer (apps/calls/result_signing.register_under_memory_peer, called by step 9
    before it signs), so a receipt naming the memory peer as issuer and carrying the server
    kid resolves, issuer and kid together, under the verifier's unchanged rule
    issuer == targetPeerId (meshy_receipt_verify.py:184-190).

    CONTROLS, unchanged from the strict xfail this replaces:
      * the call completed and its `result_signature` verifies under `result_signing_kid`
        resolved in the SERVED JWKS - the key is real and did sign;
      * the receipt's `kid` is that same kid, and its `issuer` is its `targetPeerId`;
      * the resolver resolves a PEER's own key under that peer (the caller's).
    THE CLAIM: resolved under the receipt's own issuer, the kid names the key that signed,
    and the result signature verifies under it.

    RED on the tree before the decision: ReceiptKeyUnresolved (the kid resolved only in the
    server JWKS). Mutant R38-M1 (the registration call removed from step 9) turns it red the
    same way.
    """
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": "f07", "value": {"n": 7}})
    assert invoke(client, caller, memory_peer, "memory.write", envelope).status_code == 202
    call_id = envelope["payload"]["call_id"]
    row = call_row(owner_conn, call_id)
    assert row["status"] == "completed", row["status"]
    public = server_key_from_jwks(client, row["result_signing_kid"])
    assert P.verify_strict(public, P.jcs(row["result_payload"]),
                           P.b64u_decode(row["result_signature"]))

    response = client.get(f"/v1/calls/{call_id}/receipt", headers=bearer(caller))
    assert response.status_code == 200, response.content
    receipt = json.loads(response.content)["receipt"]
    assert receipt["kid"] == row["result_signing_kid"]
    assert receipt["issuer"] == receipt["targetPeerId"] == str(memory_peer)

    own = resolve_under_issuer(owner_conn, client, str(caller["peer_id"]), caller["kid"])
    assert own == P.public_key_of(caller["seed"]), "the resolver cannot resolve anything"

    resolved = resolve_under_issuer(owner_conn, client, receipt["issuer"], receipt["kid"])
    if resolved is None:
        raise ReceiptKeyUnresolved(
            f"receipt issuer={receipt['issuer']} kid={receipt['kid']}: no such key under "
            "the issuer; the kid resolves only in the server JWKS")
    assert P.verify_strict(resolved, P.jcs(row["result_payload"]),
                           P.b64u_decode(row["result_signature"]))


def verify_memory_receipt(owner_conn, client, receipt: dict, row: dict) -> bool:
    """The verifier's decision for one receipt, rule for rule: the issuer is the attesting
    executor (issuer == targetPeerId, meshy_receipt_verify.py:184-190, NOT changed by
    decision 38), the key is looked up by (issuer, kid) and nothing else, and the result
    signature verifies under that key over the stored `result_payload`."""
    if receipt["issuer"] != receipt["targetPeerId"]:
        return False
    key = resolve_under_issuer(owner_conn, client, receipt["issuer"], receipt["kid"])
    if key is None:
        return False
    return P.verify_strict(key, P.jcs(row["result_payload"]),
                           P.b64u_decode(row["result_signature"]))


def _memory_call(client, owner_conn, world, key: str) -> tuple[dict, dict]:
    """One signed memory.write by `caller`, completed; returns (receipt, skill_calls row)."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": key, "value": {"k": key}})
    assert invoke(client, caller, memory_peer, "memory.write", envelope).status_code == 202
    call_id = envelope["payload"]["call_id"]
    row = call_row(owner_conn, call_id)
    assert row["status"] == "completed", row["status"]
    response = client.get(f"/v1/calls/{call_id}/receipt", headers=bearer(caller))
    assert response.status_code == 200, response.content
    return json.loads(response.content)["receipt"], row


def _memory_peer_keys(owner_conn, memory_peer) -> dict:
    """{kid: (active, revoked_at, public_key)} for every peer_keys row of the memory peer."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT kid, active, revoked_at, public_key FROM peer_keys "
                    "WHERE peer_id = %s", (memory_peer,))
        rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    owner_conn.commit()
    return rows


def test_a_receipt_whose_kid_is_not_registered_under_its_issuer_still_fails(
        client, owner_conn, world):
    """NEGATIVE CONTROLS for decision 38, each beside the receipt that verifies (the
    positive control, first). The registration is scoped: it makes the SERVER's kid resolve
    under the MEMORY PEER and nothing else.
      * a kid of a key registered nowhere, under the memory peer: fails;
      * the server's kid under a DIFFERENT peer (the caller) that names itself as both
        issuer and target: fails, so the server key is not a key of every peer;
      * the memory peer's own receipt with the kid of the CALLER's real key: fails, a key
        registered under someone else does not resolve under this issuer;
      * the server as issuer (its JWT issuer name): refused by issuer == targetPeerId, the
        verifier rule the decision leaves alone.
    """
    receipt, row = _memory_call(client, owner_conn, world, "f07-negative")
    assert verify_memory_receipt(owner_conn, client, receipt, row), \
        "positive control: the real receipt must verify"

    stranger = P.server_kid(P.public_key_of(os.urandom(32)))
    assert stranger != receipt["kid"]
    assert not verify_memory_receipt(owner_conn, client, dict(receipt, kid=stranger), row)

    caller_id = str(world["caller"]["peer_id"])
    as_caller = dict(receipt, issuer=caller_id, targetPeerId=caller_id)
    assert resolve_under_issuer(owner_conn, client, caller_id, receipt["kid"]) is None
    assert not verify_memory_receipt(owner_conn, client, as_caller, row)

    assert not verify_memory_receipt(owner_conn, client,
                                     dict(receipt, kid=world["caller"]["kid"]), row)
    assert not verify_memory_receipt(owner_conn, client, dict(receipt, issuer=ISSUER), row)


def test_the_registration_is_idempotent_scoped_and_follows_a_server_key_rotation(
        client, owner_conn, world):
    """Decision 38's lifecycle, measured on peer_keys rows:
      * two calls under one server key leave ONE row for (memory peer, server kid), live;
      * a key the memory peer holds IN ITS OWN RIGHT (peer-rule kid) is never touched;
      * a server key ROTATION registers the new kid on the next call and moves the old
        server registration into the 24 h grace window (active FALSE, revoked_at in
        the future), exactly as a peer key rotation does (apps/peers/keys.revoke_old_active):
        a receipt signed before the rotation still resolves through the grace-aware
        `find_by_kid`, and stops being the peer's current key.
    """
    from apps.peers import keys as peer_keys_mod

    memory_peer = world["memory_peer"]
    own_seed = os.urandom(32)
    own_public = P.public_key_of(own_seed)
    own_kid = P.kid_for_peer_key(own_public)
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active, created_at) "
                    "VALUES (%s,%s,%s,TRUE, NOW() - interval '1 day')",
                    (memory_peer, own_kid, P.b64u(own_public)))
    owner_conn.commit()

    first, row1 = _memory_call(client, owner_conn, world, "f07-rot-1")
    again, _ = _memory_call(client, owner_conn, world, "f07-rot-2")
    assert first["kid"] == again["kid"]
    rows = _memory_peer_keys(owner_conn, memory_peer)
    assert sorted(rows) == sorted([own_kid, first["kid"]]), rows
    assert rows[first["kid"]][:2] == (True, None), "the registration is live"
    assert rows[own_kid][:2] == (True, None), "the peer's own key is untouched"

    rotated = ed25519.Ed25519PrivateKey.generate()
    rotated_private = rotated.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    rotated_public = rotated.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with authconfig.override_config(jwt_signing_key_pem=rotated_private,
                                    jwt_public_key_pem=rotated_public, jwt_issuer=ISSUER):
        after, row2 = _memory_call(client, owner_conn, world, "f07-rot-3")
        assert after["kid"] != first["kid"]
        assert verify_memory_receipt(owner_conn, client, after, row2)

    rows = _memory_peer_keys(owner_conn, memory_peer)
    assert sorted(rows) == sorted([own_kid, first["kid"], after["kid"]]), rows
    assert rows[after["kid"]][:2] == (True, None), "the new server kid is registered live"
    old_active, old_revoked_at, _ = rows[first["kid"]]
    assert old_active is False and old_revoked_at is not None, rows[first["kid"]]
    assert old_revoked_at > _dt.datetime.now(_dt.timezone.utc), "inside the grace window"
    assert rows[own_kid][:2] == (True, None), "a rotation of the SERVER key spares the peer's"

    with psycopg.connect(DSN) as conn:
        graced = peer_keys_mod.find_by_kid(conn, memory_peer, first["kid"])
    assert graced is not None and P.b64u_decode(graced.public_key) == P.b64u_decode(
        rows[first["kid"]][2]), "a pre-rotation receipt still resolves inside the grace window"
    assert P.verify_strict(P.b64u_decode(graced.public_key), P.jcs(row1["result_payload"]),
                           P.b64u_decode(row1["result_signature"]))


def test_a_revoked_registration_is_not_resurrected(client, owner_conn, world):
    """Revocation is authoritative: an operator who revokes the server kid under the memory
    peer (the key route's force-immediate revoke) is not overruled by the next call. The
    call still completes and is still signed (its signature verifies in the server JWKS);
    its receipt stops resolving under the issuer, which is what the operator chose."""
    first, _ = _memory_call(client, owner_conn, world, "f07-revoke-1")
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE peer_keys SET active = FALSE, revoked_at = NOW() "
                    "WHERE peer_id = %s AND kid = %s", (world["memory_peer"], first["kid"]))
        assert cur.rowcount == 1
    owner_conn.commit()

    later, row = _memory_call(client, owner_conn, world, "f07-revoke-2")
    assert later["kid"] == first["kid"]
    public = server_key_from_jwks(client, row["result_signing_kid"])
    assert P.verify_strict(public, P.jcs(row["result_payload"]),
                           P.b64u_decode(row["result_signature"])), "still signed"
    active, revoked_at, _ = _memory_peer_keys(owner_conn, world["memory_peer"])[first["kid"]]
    assert active is False and revoked_at is not None, "the revocation was overruled"
    assert not verify_memory_receipt(owner_conn, client, later, row)
