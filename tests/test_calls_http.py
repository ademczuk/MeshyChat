"""The invoke + `/v1/calls/*` HTTP surface, over real HTTP, the real URLconf and a real
database.

TWO RULES THIS FILE IS WRITTEN TO, both of which came out of defects in this project.

1. EVERY NEGATIVE ASSERTION CARRIES A POSITIVE CONTROL IN THE SAME TEST. A 403 is also
   what a missing grant, an unseeded key, a typo'd URL or a dead connection produces, so
   a rejection on its own proves the pipeline is BROKEN, not that it is safe. Each test
   below therefore builds two requests differing in exactly ONE property, asserts the
   control is ACCEPTED, and only then asserts the variant is REJECTED. The delta is the
   finding.

2. NO `ROOT_URLCONF` OVERRIDE ANYWHERE IN THIS FILE. The previous round of route modules
   pinned their own URLconf per test: every route test passed while the application
   served 404, because a test that supplies its own URLconf proves the view and says
   nothing about the wiring. These tests resolve against `meshychat.urls`, the same
   table a request hits, and `test_the_project_urlconf_is_what_these_tests_resolve_against`
   is the control that says so out loud - if it fails, every other failure in this file
   reads as one named cause instead of twenty 404s.

NO MOCKS. Every row is written to the migrated Postgres named by MESHY_TEST_DSN; only
key material is generated per run.

Run: .venv/Scripts/python.exe -m pytest tests/test_calls_http.py -q
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("MESHY_TEST_DSN", DSN)

# Process-global settings, chosen with `setdefault` so whichever test module imports
# first wins and the run does not depend on pytest's collection order (the convention
# tests/test_auth_flows.py already established). `meshychat.settings` is named here
# because these tests must resolve against the PROJECT urlconf; nothing below overrides
# ROOT_URLCONF, and nothing may.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
os.environ.setdefault("MESHY_DSN", DSN)

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.calls import reads, repository, wire  # noqa: E402
from apps.core import dbconn  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402

ISSUER = "meshy-calls-http-test"
SKILL_NAME = "calls.http.skill"
OTHER_SKILL = "calls.http.other"
SKILL_VERSION = "1.0.0"

_jwt_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _jwt_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")
JWT_PUBLIC_PEM = _jwt_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("ascii")


# --------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def calls_settings():
    """The JWT verification key and the host allow-list, layered on top of whatever
    base configuration won the import race.

    `override_settings` LAYERS, so these tests behave the same alone or in a full run.
    ROOT_URLCONF is conspicuously ABSENT from this list and must stay absent: pinning it
    is the defect rule 2 above exists to prevent.

    MESHY_ENVELOPE_NONCE_ENFORCE is ABSENT too, on purpose (divergence 036). It was pinned
    False here and in the four other HTTP test files, so the nonce gate never ran over
    HTTP, and hard-wiring it off survived every test in them. Every test in this file
    runs the shipped default, which is ON; the blocks that turn it off are the explicit
    opt-out, and each says so.
    """
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_SYNC_TIMEOUT_SECONDS=0.0,
    ):
        yield


@pytest.fixture()
def owner_conn():
    """The bootstrap role: BYPASSES row-level security. Seeding and independent
    read-back only - a fixture reading through the same context as the code under test
    could not tell "row absent" from "row invisible"."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def client():
    return Client()


def _owner(conn, tag: str) -> uuid.UUID:
    """`dashboard_users`, not `owners` - the FK target read off the live schema. The
    live CHECK ties plane to trust group: a 'product' owner MUST have a NULL one."""
    owner_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'product',NULL)",
                    (owner_id, f"callshttp-{tag}-{owner_id.hex[:8]}@example.test"))
    conn.commit()
    return owner_id


def _peer(conn, owner_id: uuid.UUID, tag: str) -> uuid.UUID:
    peer_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                    "VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"callshttp-{tag}-{peer_id.hex[:8]}", tag))
    conn.commit()
    return peer_id


def _publish_key(conn, peer_id: uuid.UUID) -> tuple[bytes, str]:
    """An ACTIVE Ed25519 key. `public_key` is stored base64url with NO padding, which
    is how the verifier decodes it."""
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    conn.commit()
    return seed, kid


def _skill(conn, peer_id: uuid.UUID, name: str, *, mode: str = "async",
           max_runtime_ms: int | None = None) -> uuid.UUID:
    """ASYNC by default, since divergence 017. Step 9 now ends every call before the
    response is built, so a SYNC invoke finds its call terminal on the first read and
    answers 200 with the terminal body (apps/calls/views.py `_terminal_body`). An async
    skill keeps the 202 + Location acceptance these tests use as their control; the sync
    branch is asserted on its own in `test_a_signed_invoke_returns_the_documented_shape`."""
    skill_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
            "input_schema, is_current, max_runtime_ms) "
            "VALUES (%s,%s,%s,%s,%s,'grants_only','{}',TRUE,%s)",
            (skill_id, peer_id, name, SKILL_VERSION, mode, max_runtime_ms))
    conn.commit()
    return skill_id


def _grant(conn, *, grantor_owner, grantee_owner, granter_peer, skill_name) -> uuid.UUID:
    """A DIRECT grant. The live trigger `skill_grants_granter_ownership_guard` refuses a
    non-delegated grant whose granter_peer is not owned by the grantor, so the direction
    of these four columns is enforced by the database rather than by convention."""
    grant_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_grants (id, grantor_owner_id, grantee_owner_id, "
            "granter_peer_id, skill_name, source) VALUES (%s,%s,%s,%s,%s,'direct')",
            (grant_id, grantor_owner, grantee_owner, granter_peer, skill_name))
    conn.commit()
    return grant_id


@pytest.fixture()
def world(owner_conn):
    """Two SEPARATE product-plane tenants, so `meshy_same_tenant_or_devgroup` is FALSE and
    the grant gate is actually exercised. Same-tenant peers are allowed grant-free and
    every authorization assertion here would pass vacuously.

    A THIRD tenant (`stranger`) exists to be the party that is neither caller nor target
    - the subject of the ownership tests.
    """
    caller_owner = _owner(owner_conn, "caller")
    target_owner = _owner(owner_conn, "target")
    stranger_owner = _owner(owner_conn, "stranger")
    caller_peer = _peer(owner_conn, caller_owner, "caller")
    target_peer = _peer(owner_conn, target_owner, "target")
    stranger_peer = _peer(owner_conn, stranger_owner, "stranger")
    seed, kid = _publish_key(owner_conn, caller_peer)
    skill_id = _skill(owner_conn, target_peer, SKILL_NAME)
    other_skill_id = _skill(owner_conn, target_peer, OTHER_SKILL)
    return {
        "caller_owner": caller_owner, "target_owner": target_owner,
        "stranger_owner": stranger_owner, "caller_peer": caller_peer,
        "target_peer": target_peer, "stranger_peer": stranger_peer,
        "seed": seed, "kid": kid, "skill_id": skill_id,
        "other_skill_id": other_skill_id,
    }


@pytest.fixture()
def granted(owner_conn, world):
    """The world plus a grant for BOTH skills, so a test can vary the skill in the PATH
    without also varying whether that skill is granted."""
    for name in (SKILL_NAME, OTHER_SKILL):
        _grant(owner_conn, grantor_owner=world["target_owner"],
               grantee_owner=world["caller_owner"],
               granter_peer=world["target_peer"], skill_name=name)
    return world


# ----------------------------------------------------------------------- helpers

def mint(peer_id, *, aud="peer", ttl=300) -> str:
    """A REAL EdDSA peer JWT that `settings.MESHY_JWT_PUBLIC_KEY_PEM` verifies."""
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return pyjwt.encode(
        {"sub": str(peer_id), "aud": aud, "iss": ISSUER, "iat": now, "exp": now + ttl,
         "scopes": ["skills:invoke"]},
        JWT_PRIVATE_PEM, algorithm="EdDSA")


def bearer(peer_id) -> dict:
    return {"authorization": f"Bearer {mint(peer_id)}"}


#: `sign_call(nonce=OMIT)` signs a payload with no `nonce` field at all.
OMIT = object()


def sign_call(world, *, args=None, call_id=None, target_peer=None,
              skill_name=SKILL_NAME, seed=None, kid=None, issued_at=None, nonce=None):
    """Build and sign a `skill.call` envelope.

    The signature covers the JCS bytes of the payload and NOTHING else: there is no
    domain prefix - `kind` INSIDE the object is the domain separator.

    Every envelope carries a FRESH nonce, as a real signer's does. `nonce=` exists for the
    replay tests only: a string re-signs that nonce, `OMIT` signs a pre-nonce payload.
    """
    payload = {
        "v": 1,
        "kind": "skill.call",
        "call_id": call_id or str(uuid.uuid4()),
        "caller_peer_id": str(world["caller_peer"]),
        "target_peer_id": str(target_peer or world["target_peer"]),
        "skill_name": skill_name,
        "skill_version": SKILL_VERSION,
        "args": {"text": "hello"} if args is None else args,
        "args_attachments": [],
        "issued_at": (issued_at or _dt.datetime.now(_dt.timezone.utc)).isoformat(),
        "nonce": nonce or f"n-{uuid.uuid4()}",
    }
    if nonce is OMIT:
        del payload["nonce"]
    signature = P.sign(seed or world["seed"], P.jcs(payload))
    envelope = {"payload": payload, "signature": P.b64u(signature),
                "signing_kid": kid or world["kid"]}
    return payload, envelope


def invoke_url(peer_id, skill_name=SKILL_NAME) -> str:
    return f"/v1/agents/{peer_id}/skills/{skill_name}/invoke"


def post_invoke(client, world, envelope, *, target=None, skill_name=SKILL_NAME,
                as_peer=None, send_call_id=True, **extra):
    """POST the envelope the way a real client does - INCLUDING `call_id`.

    `InvokeBody.call_id` is not optional decoration on the HTTP path. Step 1 of the
    pipeline generates a fresh UUID when the body omits it, and step 4b then compares
    the SIGNED call_id against that generated one and refuses the mismatch with 403.
    So an HTTP caller that signs a call_id must echo it in the body;
    `send_call_id=False` below asserts that requirement.
    """
    body = {"envelope": envelope, **extra}
    if send_call_id:
        body["call_id"] = envelope["payload"]["call_id"]
    return client.post(
        invoke_url(target or world["target_peer"], skill_name),
        data=json.dumps(body), content_type="application/json",
        headers=bearer(as_peer or world["caller_peer"]))


def body_of(response) -> dict | list:
    return json.loads(response.content.decode("utf-8"))


# The accepted body's keys before `replayed` existed, in their order. `replayed` is appended after them (and after
# `chunks_url` on a stream call), so the old body is a prefix of the new one: a client that compares the keys it knows
# in order and ignores unknown trailing keys keeps working (kxxx, msg 3c5e5a).
ACCEPTED_KEYS_BEFORE_REPLAYED = ["call_id", "status", "skill_version", "mode", "deadline_at", "return_via"]


def assert_replayed_is_appended_last(body):
    keys = list(body)
    assert keys[-1] == "replayed", keys
    known = [k for k in keys[:-1] if k != "chunks_url"]
    assert known == ACCEPTED_KEYS_BEFORE_REPLAYED, keys
    if "chunks_url" in body:
        assert keys[-2] == "chunks_url", keys


def accepted_call(client, world, **kw) -> str:
    """Drive one full accepted invoke and return its call_id. Used where a test needs a
    call to EXIST before it can test a read - the acceptance is asserted here so a
    downstream 404 can never be mistaken for the read being broken."""
    payload, envelope = sign_call(world, **kw)
    response = post_invoke(client, world, envelope,
                           skill_name=kw.get("skill_name", SKILL_NAME))
    assert response.status_code == 202, response.content
    return payload["call_id"]


def in_flight_call(world, *, skill_name=SKILL_NAME) -> str:
    """A call LEFT IN FLIGHT, `pending`, made by the pipeline's own INSERT and not routed.

    Since divergence 041 S5 an invoke to a remote target leaves its call in flight too
    (`queued` or `routed`); these tests keep the `pending` row a crash between step 8 and
    step 9 leaves, which is the state divergence 017's cancel tests were written against.
    `repository.insert_call` is the function step 8 calls, on a meshychat_app connection, so the
    row is the one the pipeline writes, minus the route.
    """
    payload, envelope = sign_call(world, skill_name=skill_name)
    skill_id = world["skill_id"] if skill_name == SKILL_NAME else world["other_skill_id"]
    with dbconn.serving_connection() as conn:
        repository.insert_call(
            conn, call_id=payload["call_id"], caller_peer_id=world["caller_peer"],
            caller_owner_id=world["caller_owner"], target_peer_id=world["target_peer"],
            target_owner_id=world["target_owner"], skill_id=skill_id,
            skill_name=skill_name, skill_version=SKILL_VERSION, mode="async",
            transport="http", payload=payload, args_signature=envelope["signature"],
            args_signing_kid=envelope["signing_kid"], args_attachments=[],
            grant_id=None,
            deadline_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=5))
    return payload["call_id"]


def nonces_recorded(owner_conn, peer_id, nonce: str) -> int:
    """Rows in the envelope nonce ledger for (peer, nonce), read as the bootstrap role. A 1
    is the direct evidence that pipeline step 4c RAN for a request: a status code alone
    is also what a skipped gate produces for most requests."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM envelope_nonce_seen "
                    "WHERE peer_id = %s AND nonce = %s", (peer_id, nonce))
        (count,) = cur.fetchone()
    owner_conn.commit()
    return count


def row_of(owner_conn, call_id: str) -> dict | None:
    """Read the row as the bootstrap role (RLS bypassed), so absence means the row is
    not there rather than merely invisible to whatever context the app last used."""
    cols = ", ".join(reads.CALL_COLUMNS)
    with owner_conn.cursor() as cur:
        cur.execute(f"SELECT {cols} FROM skill_calls WHERE call_id = %s", (call_id,))
        row = cur.fetchone()
    owner_conn.commit()
    return None if row is None else dict(zip(reads.CALL_COLUMNS, row))


# ==========================================================================
# 0. the control that makes every other test in this file mean something
# ==========================================================================

def test_the_project_urlconf_is_what_these_tests_resolve_against(client):
    """No test here pins a URLconf, so this asserts the one in force IS the project's.

    Without it, a misconfigured run would produce a wall of 404s with no named cause -
    which is exactly how the previous round's routes shipped unmounted.
    """
    from django.conf import settings as live
    from django.urls import Resolver404, resolve

    assert live.ROOT_URLCONF == "meshychat.urls"

    expected = {
        f"/v1/agents/{uuid.uuid4()}/skills/some.skill/invoke": "agent-skill-invoke",
        "/v1/calls": "calls-list",
        "/v1/calls/abc": "call-get",
        "/v1/calls/abc/receipt": "call-receipt",
        "/v1/calls/abc/events": "call-events",
        "/v1/calls/abc/chunks": "call-chunks",
        "/v1/calls/abc/cancel": "call-cancel",
    }
    for path, name in expected.items():
        assert resolve(path).url_name == name, path

    # Positive control for the loop above: resolve() must be CAPABLE of failing here,
    # otherwise "it resolved" proves nothing about the routing table.
    with pytest.raises(Resolver404):
        resolve("/v1/calls-definitely-not-mounted")


# ==========================================================================
# 1. invoke - the happy path and its wire shape
# ==========================================================================

def test_a_signed_invoke_returns_the_documented_shape(client, owner_conn, granted):
    """202 + Location + the `accepted_response` body, for an ASYNC
    skill and for a SYNC one whose call is still in flight when the response is built.

    Since divergence 041 S5 step 9 QUEUES a call to a remote target that holds no socket
    (this one), instead of failing it: the async body carries
    `status: queued`. The sync call is not terminal either, so at the default sync wait
    (0 s, Q1) `_await_terminal`'s one read finds it in flight and the route answers 202 with
    Location and the same accepted body, and the client polls. The 200 + terminal body branch is
    `test_a_terminal_call_invoked_again_returns_its_result_inline`.
    """
    payload, envelope = sign_call(granted)
    response = post_invoke(client, granted, envelope)

    assert response.status_code == 202, response.content
    assert response["Location"] == f"/v1/calls/{payload['call_id']}"
    body = body_of(response)
    assert body["call_id"] == payload["call_id"]
    assert body["status"] == "queued"
    assert body["replayed"] is False
    assert_replayed_is_appended_last(body)
    assert body["skill_version"] == SKILL_VERSION
    assert body["mode"] == "async"
    assert body["return_via"] == ["ws", "webhook", "poll"]
    # `chunks_url` is emitted for STREAM mode only.
    assert "chunks_url" not in body
    assert body["deadline_at"].endswith("Z"), body["deadline_at"]

    row = row_of(owner_conn, payload["call_id"])
    assert row is not None, "an accepted invoke must be recorded in skill_calls"
    assert row["caller_peer_id"] == granted["caller_peer"]
    assert row["target_peer_id"] == granted["target_peer"]
    assert row["transport"] == "http"
    assert row["status"] == "queued"
    assert row["error_code"] is None
    # `args` holds the WHOLE SIGNED PAYLOAD, not payload["args"].
    assert row["args"] == payload
    assert row["args_signature"] == envelope["signature"]
    assert row["args_signing_kid"] == envelope["signing_kid"]
    # The grant that authorized the call is recorded ON the call: a NULL here would
    # mean a grant-free allow, which is a different decision.
    assert row["grant_id"] is not None

    # The SYNC branch: the same call on a sync skill is still queued when the response is
    # built, so it is 202 with Location and the accepted body, not 200 (Q1).
    sync_name = f"{SKILL_NAME}.sync"
    _skill(owner_conn, granted["target_peer"], sync_name, mode="sync")
    _grant(owner_conn, grantor_owner=granted["target_owner"],
           grantee_owner=granted["caller_owner"], granter_peer=granted["target_peer"],
           skill_name=sync_name)
    sync_payload, sync_envelope = sign_call(granted, skill_name=sync_name)
    deferred = post_invoke(client, granted, sync_envelope, skill_name=sync_name)
    assert deferred.status_code == 202, deferred.content
    assert deferred["Location"] == f"/v1/calls/{sync_payload['call_id']}"
    assert body_of(deferred) == {
        "call_id": sync_payload["call_id"], "status": "queued",
        "replayed": False,
        "skill_version": SKILL_VERSION, "mode": "sync",
        "deadline_at": body_of(deferred)["deadline_at"],
        "return_via": ["ws", "webhook", "poll"],
    }
    assert_replayed_is_appended_last(body_of(deferred))


def test_a_stream_skill_gets_a_chunks_url_and_a_different_return_via(
        client, owner_conn, world):
    """The one branch in `accepted_response`.

    STATUS C11 (`:385-401` in the review's numbering): this test's control used to be "the
    SYNC call above", a DIFFERENT test, against rule 1 of this file - run alone, it passed
    on an `accepted_response` that gave EVERY call the stream fields. The control is now
    here: the same caller, the same target, a granted ASYNC skill, accepted in this test
    with neither `chunks_url` nor the stream `return_via`. ONE property differs: the mode.
    """
    stream_name = f"{SKILL_NAME}.stream"
    _skill(owner_conn, world["target_peer"], stream_name, mode="stream")
    for name in (stream_name, SKILL_NAME):
        _grant(owner_conn, grantor_owner=world["target_owner"],
               grantee_owner=world["caller_owner"], granter_peer=world["target_peer"],
               skill_name=name)

    _, control_envelope = sign_call(world)
    control = post_invoke(client, world, control_envelope)
    assert control.status_code == 202, control.content
    assert body_of(control)["mode"] == "async"
    assert body_of(control)["return_via"] == ["ws", "webhook", "poll"]
    assert "chunks_url" not in body_of(control)

    payload, envelope = sign_call(world, skill_name=stream_name)
    response = post_invoke(client, world, envelope, skill_name=stream_name)

    assert response.status_code == 202, response.content
    body = body_of(response)
    assert body["mode"] == "stream"
    assert body["return_via"] == ["ws", "poll-chunks"]
    assert body["chunks_url"] == f"/v1/calls/{payload['call_id']}/chunks?since=0"
    assert_replayed_is_appended_last(body)


def test_an_idempotent_replay_returns_the_same_call(client, owner_conn, granted):
    """Same envelope twice: one row, one call_id, no 409. The CONTROL is the first
    request; the finding is that the second neither duplicates nor errors.

    WITH THE NONCE GATE ON, the default (divergence 036). This is the request a client
    retry produces when a response was lost: the same bytes go again. Step 2
    (idempotency) answers it from the row the first request created, BEFORE step 4c, so
    the ledger never sees the second presentation and nothing runs twice. The ledger
    count proves the gate DID run on the first request; without it this test passes
    just as well with the gate hard-wired off.
    """
    payload, envelope = sign_call(granted)

    first = post_invoke(client, granted, envelope)
    assert first.status_code == 202, first.content
    assert nonces_recorded(owner_conn, granted["caller_peer"], payload["nonce"]) == 1, (
        "the default did not record the nonce: the gate did not run on this request")
    second = post_invoke(client, granted, envelope)
    assert second.status_code == 202, second.content
    assert nonces_recorded(owner_conn, granted["caller_peer"], payload["nonce"]) == 1

    assert body_of(first)["call_id"] == body_of(second)["call_id"] == payload["call_id"]
    assert body_of(first)["replayed"] is False and body_of(second)["replayed"] is True
    assert_replayed_is_appended_last(body_of(second))
    with owner_conn.cursor() as cur:
        cur.execute("SELECT id FROM skill_calls WHERE call_id = %s",
                    (payload["call_id"],))
        assert len(cur.fetchall()) == 1
    owner_conn.commit()


# ==========================================================================
# 2. invoke - refusals, each against an accepted twin
# ==========================================================================

def test_a_tampered_signature_is_refused_with_E_SIG_BAD(client, granted):
    """ONE byte differs between the two requests."""
    _, control = sign_call(granted)
    assert post_invoke(client, granted, control).status_code == 202

    _, tampered = sign_call(granted)
    raw = bytearray(P.b64u_decode(tampered["signature"]))
    raw[0] ^= 0x01
    tampered["signature"] = P.b64u(bytes(raw))

    response = post_invoke(client, granted, tampered)
    assert response.status_code == 401, response.content
    assert body_of(response)["error"] == "E_SIG_BAD"


def test_a_tampered_payload_is_refused_with_E_SIG_BAD(client, granted):
    """The args are edited AFTER signing: same signature, different canonical bytes."""
    _, control = sign_call(granted)
    assert post_invoke(client, granted, control).status_code == 202

    _, tampered = sign_call(granted)
    tampered["payload"]["args"] = {"text": "goodbye"}

    response = post_invoke(client, granted, tampered)
    assert response.status_code == 401, response.content
    assert body_of(response)["error"] == "E_SIG_BAD"


def test_an_unsigned_invoke_is_refused(client, granted):
    """No envelope at all. `InvokeBody.envelope` has no default, so this is a
    deserialization failure (422), not a signature failure - and the control proves the
    same request WITH an envelope is accepted."""
    _, control = sign_call(granted)
    assert post_invoke(client, granted, control).status_code == 202

    response = client.post(invoke_url(granted["target_peer"]), data=json.dumps({}),
                           content_type="application/json",
                           headers=bearer(granted["caller_peer"]))
    assert response.status_code == 422, response.content
    assert body_of(response)["error"] == "E_VALIDATION"


def test_an_envelope_signed_for_another_skill_is_refused_403(client, granted):
    """The confused-deputy guard (pipeline step 4b). The ONLY difference between the two
    requests is the skill segment of the URL.

    BOTH skills exist and BOTH are granted - that is what the second control proves - so
    a 403 here is the rebind check firing and not a 404 in disguise.
    """
    _, control = sign_call(granted, skill_name=SKILL_NAME)
    assert post_invoke(client, granted, control, skill_name=SKILL_NAME).status_code == 202

    _, other_control = sign_call(granted, skill_name=OTHER_SKILL)
    assert post_invoke(client, granted, other_control,
                       skill_name=OTHER_SKILL).status_code == 202

    _, crossed = sign_call(granted, skill_name=SKILL_NAME)
    response = post_invoke(client, granted, crossed, skill_name=OTHER_SKILL)
    assert response.status_code == 403, response.content
    assert body_of(response)["error"] == "E_FORBIDDEN"
    # The engine's reason is NOT leaked: the forbidden error renders a fixed message.
    assert body_of(response)["message"] == "forbidden"


def test_an_envelope_whose_call_id_is_not_echoed_is_refused_403(client, granted):
    """The other half of the rebind guard, and the one that surprises clients.

    When the body omits `call_id`, step 1 generates a fresh UUID and step 4b compares
    the SIGNED call_id against it - so a signed envelope submitted without its key is
    a mismatch, not a convenience. The control is the SAME
    envelope WITH the key echoed, so the delta is one body field.
    """
    _, envelope = sign_call(granted)
    without = post_invoke(client, granted, envelope, send_call_id=False)
    assert without.status_code == 403, without.content
    assert body_of(without)["error"] == "E_FORBIDDEN"

    with_key = post_invoke(client, granted, envelope)
    assert with_key.status_code == 202, with_key.content


def test_the_wrong_method_is_405_with_an_allow_header(client, granted):
    """A known path with an unknown method is 405, not 404: the route EXISTS. Control:
    the allowed method on the very same path is accepted."""
    call_id = accepted_call(client, granted)

    not_allowed = client.get(invoke_url(granted["target_peer"]),
                             headers=bearer(granted["caller_peer"]))
    assert not_allowed.status_code == 405
    assert not_allowed["Allow"] == "POST"

    read_only = client.post(f"/v1/calls/{call_id}", data=json.dumps({}),
                            content_type="application/json",
                            headers=bearer(granted["caller_peer"]))
    assert read_only.status_code == 405
    assert read_only["Allow"] == "GET"
    assert client.get(f"/v1/calls/{call_id}",
                      headers=bearer(granted["caller_peer"])).status_code == 200


def test_an_invoke_without_a_grant_is_refused_403(client, owner_conn, world):
    """Cross-tenant, grant-free. The control is the SAME call once the grant exists."""
    _, envelope = sign_call(world)
    denied = post_invoke(client, world, envelope)
    assert denied.status_code == 403, denied.content
    assert body_of(denied)["error"] == "E_FORBIDDEN"

    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"],
           skill_name=SKILL_NAME)
    _, allowed_envelope = sign_call(world)
    allowed = post_invoke(client, world, allowed_envelope)
    assert allowed.status_code == 202, allowed.content


def test_an_unknown_skill_is_404_not_403(client, granted):
    """404 and 403 are different answers and must stay different: `resolve_for_call`
    runs before authorization, so a skill that does not exist never reaches the grant
    lookup. Control: the same request against the skill that does exist."""
    _, control = sign_call(granted)
    assert post_invoke(client, granted, control).status_code == 202

    _, envelope = sign_call(granted, skill_name="no.such.skill")
    response = post_invoke(client, granted, envelope, skill_name="no.such.skill")
    assert response.status_code == 404, response.content
    assert body_of(response)["error"] == "E_NOT_FOUND"


def test_mode_override_separates_a_bad_variant_from_a_disallowed_one(
        client, owner_conn, world):
    """Two different rejections that are easy to collapse into one:

      * `mode_override: "turbo"` does not DESERIALIZE into SkillMode  -> 422
      * `mode_override: "sync"` on a skill whose max_runtime_ms > 30s -> 400 E_VALIDATION

    Control: a valid, allowed override is accepted.
    """
    slow = f"{SKILL_NAME}.slow"
    _skill(owner_conn, world["target_peer"], slow, mode="async", max_runtime_ms=60_000)
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"],
           skill_name=slow)

    _, control = sign_call(world, skill_name=slow)
    ok = post_invoke(client, world, control, skill_name=slow, mode_override="async")
    assert ok.status_code == 202, ok.content

    _, bad_variant = sign_call(world, skill_name=slow)
    unknown = post_invoke(client, world, bad_variant, skill_name=slow,
                          mode_override="turbo")
    assert unknown.status_code == 422, unknown.content

    _, disallowed = sign_call(world, skill_name=slow)
    refused = post_invoke(client, world, disallowed, skill_name=slow,
                          mode_override="sync")
    assert refused.status_code == 400, refused.content
    assert body_of(refused)["error"] == "E_VALIDATION"


def test_args_attachments_are_capped_before_any_database_work(client, granted):
    """#156 (divergence 088): admission checks each attached file (#151), so the list is capped at
    parse time. At the cap the body parses and reaches admission, which refuses it for its own
    reason (this skill allows no attachments: 400). One past the cap is refused as a malformed
    body (422) before admission is reached."""
    from apps.calls.views import MAX_ARGS_ATTACHMENTS
    world = granted
    _, at_cap = sign_call(world)
    reached = post_invoke(client, world, at_cap,
                          args_attachments=[str(uuid.uuid4()) for _ in range(MAX_ARGS_ATTACHMENTS)])
    assert reached.status_code == 400, reached.content
    assert body_of(reached)["error"] == "E_FILE_ATTACHMENT_INVALID"

    _, over = sign_call(world)
    refused = post_invoke(client, world, over,
                          args_attachments=[str(uuid.uuid4()) for _ in range(MAX_ARGS_ATTACHMENTS + 1)])
    assert refused.status_code == 422, refused.content
    assert f"at most {MAX_ARGS_ATTACHMENTS}" in refused.content.decode("utf-8")


def test_a_malformed_peer_id_in_the_path_is_400(client, granted):
    """A malformed id is a 400 from route validation - NOT a 404, which would claim the
    peer does not exist. Control: the
    well-formed id on the same route is accepted."""
    _, control = sign_call(granted)
    assert post_invoke(client, granted, control).status_code == 202

    _, envelope = sign_call(granted)
    response = client.post("/v1/agents/not-a-uuid/skills/x/invoke",
                           data=json.dumps({"envelope": envelope}),
                           content_type="application/json",
                           headers=bearer(granted["caller_peer"]))
    assert response.status_code == 400, response.content
    assert body_of(response)["error"] == "E_VALIDATION"


# ==========================================================================
# 3. authentication
# ==========================================================================

def test_missing_and_wrong_audience_tokens_are_refused(client, granted):
    """Three variants against one control, and the control is asserted FIRST so a
    failure reads as "the happy path broke" rather than "a guard fired"."""
    call_id = accepted_call(client, granted)
    url = f"/v1/calls/{call_id}"

    ok = client.get(url, headers=bearer(granted["caller_peer"]))
    assert ok.status_code == 200, ok.content

    missing = client.get(url)
    assert missing.status_code == 401
    assert body_of(missing)["error"] == "E_AUTH_MISSING"

    not_bearer = client.get(url, headers={"authorization": "Basic abc"})
    assert not_bearer.status_code == 401
    assert body_of(not_bearer)["error"] == "E_AUTH_INVALID"

    # aud must be exactly "peer"; a dashboard token is 403.
    wrong_aud = client.get(url, headers={
        "authorization": f"Bearer {mint(granted['caller_peer'], aud='dashboard')}"})
    assert wrong_aud.status_code == 403
    assert body_of(wrong_aud)["error"] == "E_FORBIDDEN"


# ==========================================================================
# 4. reading a call - ownership
# ==========================================================================

def test_a_call_the_viewer_does_not_own_is_not_readable(client, granted):
    """THE OWNERSHIP TEST, with both entitled parties as positive controls.

    A stranger's 403 alone would also be produced by a broken read, an unseeded call or
    a 500. So the caller AND the target are asserted to get 200 on the very same URL
    first; only then is the stranger's 403 a statement about authorization.
    """
    call_id = accepted_call(client, granted)
    url = f"/v1/calls/{call_id}"

    as_caller = client.get(url, headers=bearer(granted["caller_peer"]))
    assert as_caller.status_code == 200, as_caller.content
    assert body_of(as_caller)["call_id"] == call_id

    as_target = client.get(url, headers=bearer(granted["target_peer"]))
    assert as_target.status_code == 200, as_target.content

    as_stranger = client.get(url, headers=bearer(granted["stranger_peer"]))
    assert as_stranger.status_code == 403, as_stranger.content
    assert body_of(as_stranger)["error"] == "E_FORBIDDEN"

    # The same 403/200 split holds on every sub-resource, not just the call itself.
    for suffix in ("/events", "/chunks", "/receipt"):
        assert client.get(url + suffix,
                          headers=bearer(granted["caller_peer"])).status_code == 200
        assert client.get(url + suffix,
                          headers=bearer(granted["stranger_peer"])).status_code == 403


def test_an_unknown_call_id_is_404_on_every_read(client, granted):
    """404 (no such call) must stay distinguishable from 403 (not yours). Control: the
    known id answers 200 on each of the same four routes."""
    known = accepted_call(client, granted)
    unknown = f"missing-{uuid.uuid4()}"
    headers = bearer(granted["caller_peer"])

    for suffix in ("", "/events", "/chunks", "/receipt"):
        assert client.get(f"/v1/calls/{known}{suffix}", headers=headers).status_code == 200
        missing = client.get(f"/v1/calls/{unknown}{suffix}", headers=headers)
        assert missing.status_code == 404, (suffix, missing.content)
        assert body_of(missing)["error"] == "E_NOT_FOUND"


def test_the_continuation_nonce_is_visible_to_the_target_only(client, owner_conn, granted):
    """The nonce authorizes the TARGET to
    continue a flow, so handing it to the caller - who is equally entitled to READ the
    call - would hand them the target's continuation authority.

    The control is the target's 200 CARRYING the nonce: without it, the caller's
    "nonce absent" would also be satisfied by a column that was never populated.
    """
    call_id = accepted_call(client, granted)
    nonce = f"cn-{uuid.uuid4()}"
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET continuation_nonce = %s WHERE call_id = %s",
                    (nonce, call_id))
    owner_conn.commit()

    as_target = body_of(client.get(f"/v1/calls/{call_id}",
                                   headers=bearer(granted["target_peer"])))
    assert as_target["continuation_nonce"] == nonce

    as_caller = body_of(client.get(f"/v1/calls/{call_id}",
                                   headers=bearer(granted["caller_peer"])))
    assert "continuation_nonce" not in as_caller


# ==========================================================================
# 5. GET /v1/calls
# ==========================================================================

def test_the_list_is_scoped_to_the_viewer_and_the_role(client, granted):
    """Four assertions that only mean something together: the caller sees the call as
    `role=caller` and NOT as `role=target`; the target is the mirror image; the stranger
    sees neither. The first two are each other's control."""
    call_id = accepted_call(client, granted)

    def ids(peer, role):
        response = client.get(f"/v1/calls?role={role}", headers=bearer(peer))
        assert response.status_code == 200, response.content
        return [row["call_id"] for row in body_of(response)]

    assert call_id in ids(granted["caller_peer"], "caller")
    assert call_id not in ids(granted["caller_peer"], "target")
    assert call_id in ids(granted["target_peer"], "target")
    assert call_id not in ids(granted["target_peer"], "caller")
    assert call_id not in ids(granted["stranger_peer"], "caller")
    assert call_id not in ids(granted["stranger_peer"], "target")


def test_the_list_validates_role_status_and_since(client, granted):
    """Every rejection here is paired with the accepted form of the same parameter."""
    call_id = accepted_call(client, granted)
    headers = bearer(granted["caller_peer"])

    missing_role = client.get("/v1/calls", headers=headers)
    assert missing_role.status_code == 400
    assert body_of(missing_role)["error"] == "E_VALIDATION"

    bad_role = client.get("/v1/calls?role=both", headers=headers)
    assert bad_role.status_code == 400
    assert body_of(bad_role)["message"] == "role must be 'caller' or 'target'"

    # Queued by step 9: its target holds no socket (divergence 041 S5).
    good_status = client.get("/v1/calls?role=caller&status=queued", headers=headers)
    assert good_status.status_code == 200
    assert call_id in [r["call_id"] for r in body_of(good_status)]
    # A VALID status that this call is not in: 200 with the call absent, which is a
    # different thing from the 400 an unknown status gets.
    other_status = client.get("/v1/calls?role=caller&status=completed", headers=headers)
    assert other_status.status_code == 200
    assert call_id not in [r["call_id"] for r in body_of(other_status)]

    bad_status = client.get("/v1/calls?role=caller&status=nonsense", headers=headers)
    assert bad_status.status_code == 400
    assert body_of(bad_status)["message"] == "unknown status: nonsense"

    since_past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1))
    ok_since = client.get(
        f"/v1/calls?role=caller&since={wire.optional_rfc3339(since_past)}", headers=headers)
    assert ok_since.status_code == 200
    assert call_id in [r["call_id"] for r in body_of(ok_since)]

    bad_since = client.get("/v1/calls?role=caller&since=yesterday", headers=headers)
    assert bad_since.status_code == 400
    # The message is calls/wire.parse_rfc3339's text after "invalid `since`: " - the one
    # place that parser's wording reaches a client (tests/test_rfc3339_one_rule.py).
    assert body_of(bad_since)["message"] == \
        "invalid `since`: not an RFC 3339 timestamp: 'yesterday'"

    # STATUS C10: the bad `limit` below had no accepted twin anywhere in this file, so a
    # route refusing EVERY limit passed. The control is a numeric limit, accepted, carrying
    # the call. `test_a_numeric_limit_bounds_the_list_and_is_clamped` proves it is APPLIED.
    good_limit = client.get("/v1/calls?role=caller&limit=5", headers=headers)
    assert good_limit.status_code == 200, good_limit.content
    assert call_id in [r["call_id"] for r in body_of(good_limit)]

    bad_limit = client.get("/v1/calls?role=caller&limit=lots", headers=headers)
    assert bad_limit.status_code == 400
    assert body_of(bad_limit)["error"] == "E_VALIDATION"


def test_a_numeric_limit_bounds_the_list_and_is_clamped(client, granted):
    """STATUS C10. `limit` is read AND applied, clamped to 1..500.

    Two calls by one caller, newest first (`ORDER BY created_at DESC`). `limit=2` returns
    both (the control: two rows exist to be cut), `limit=1` exactly the newer one, `limit=0`
    is clamped UP to 1 rather than refused or read as "no limit", and a limit beyond the
    ceiling is clamped DOWN, a 200 and not a 400. A route that parsed `limit` and then
    ignored it would pass the 200s above and fail the `limit=1` count here.
    """
    older = accepted_call(client, granted)
    newer = accepted_call(client, granted)
    headers = bearer(granted["caller_peer"])

    def listed(limit):
        response = client.get(f"/v1/calls?role=caller&limit={limit}", headers=headers)
        assert response.status_code == 200, (limit, response.content)
        return [r["call_id"] for r in body_of(response)]

    assert listed(2) == [newer, older]
    assert listed(1) == [newer]
    assert listed(0) == [newer], "limit=0 must clamp to 1, not mean unlimited"
    assert listed(reads.MAX_LIST_LIMIT * 10) == [newer, older]


# ==========================================================================
# 6. events + chunks
# ==========================================================================

def test_events_carry_the_created_row_and_honour_since(client, owner_conn, granted):
    call_id = accepted_call(client, granted)
    headers = bearer(granted["caller_peer"])

    response = client.get(f"/v1/calls/{call_id}/events", headers=headers)
    assert response.status_code == 200, response.content
    events = body_of(response)
    # 'created' by the insert, then 'queued' by step 9: this target is not the memory
    # authority and holds no socket (divergence 041 S5). Both are the server's own, so
    # both use the compatibility source name 'meshy'.
    assert [e["event_type"] for e in events] == ["created", "queued"]
    assert [e["source"] for e in events] == ["meshy", "meshy"]
    assert events[0]["call_id"] == call_id
    assert events[0]["occurred_at"].endswith("Z")

    # `occurred_at > since`, STRICTLY - the SQL says `>` even though the route's
    # doc-comment says "at or after". An event's own timestamp therefore excludes it
    # and keeps everything after it.
    at = events[0]["occurred_at"]
    after = client.get(f"/v1/calls/{call_id}/events?since={at}", headers=headers)
    assert after.status_code == 200
    assert [e["event_type"] for e in body_of(after)] == ["queued"]
    last = events[-1]["occurred_at"]
    after_last = client.get(f"/v1/calls/{call_id}/events?since={last}", headers=headers)
    assert after_last.status_code == 200
    assert body_of(after_last) == []


def test_chunks_are_empty_by_default_and_serialize_content_as_bytes(
        client, owner_conn, granted):
    """`content` renders as a SEQUENCE OF NUMBERS - not base64. The empty case is the
    control for the populated one: it proves the route answers before any chunk exists,
    so a later `[]` could not be mistaken for the reader being broken."""
    call_id = accepted_call(client, granted)
    headers = bearer(granted["caller_peer"])

    empty = client.get(f"/v1/calls/{call_id}/chunks", headers=headers)
    assert empty.status_code == 200
    assert body_of(empty) == []

    with owner_conn.cursor() as cur:
        for seq in (1, 2):
            cur.execute(
                "INSERT INTO skill_call_chunks (call_id, sequence, chunk_type, content,"
                " chunk_signature, chunk_signing_kid) VALUES (%s,%s,'text',%s,%s,%s)",
                (call_id, seq, b"hi", "sig", "kid"))
    owner_conn.commit()

    both = body_of(client.get(f"/v1/calls/{call_id}/chunks", headers=headers))
    assert [c["sequence"] for c in both] == [1, 2]
    assert both[0]["content"] == [104, 105]      # b"hi", not "aGk"
    assert both[0]["chunk_type"] == "text"

    since = body_of(client.get(f"/v1/calls/{call_id}/chunks?since=1", headers=headers))
    assert [c["sequence"] for c in since] == [2]


# ==========================================================================
# 7. receipt
# ==========================================================================

def test_the_receipt_binds_the_args_digest_and_stays_unsigned(client, owner_conn, granted):
    """The receipt is DERIVED and unsigned at the receipt level - the server holds only
    peer PUBLIC keys and cannot sign as the executor.

    `args_sha256` is recomputed here from the payload the test itself signed, so this
    asserts the server hashed the RIGHT BYTES rather than merely returning some hash.
    """
    payload, envelope = sign_call(granted)
    assert post_invoke(client, granted, envelope).status_code == 202
    call_id = payload["call_id"]

    response = client.get(f"/v1/calls/{call_id}/receipt",
                          headers=bearer(granted["caller_peer"]))
    assert response.status_code == 200, response.content
    body = body_of(response)
    receipt = body["receipt"]

    # THE KEY SET IS THE CONTRACT, so it is asserted as a SET EQUALITY rather than field
    # by field. Asserting only the fields this test happens to care about is what let the
    # whole receipt ship in snake_case: every individual assertion agreed with the code,
    # and the code disagreed with the wire.
    #
    # This list is `REQUIRED_RECEIPT_FIELDS` from the receipt conformance verifier, which is
    # not in this repository. It lives here as a literal; if the verifier is ever added,
    # import the constant instead of restating it.
    REQUIRED_RECEIPT_FIELDS = [
        "callId", "callerPeerId", "targetPeerId", "skillName", "skillVersion", "grantId",
        "parentCallId", "rootMandateHash", "chainId",
        "argsSha256", "resultSha256", "completedAt", "issuer", "kid",
    ]
    assert sorted(receipt) == sorted(REQUIRED_RECEIPT_FIELDS), (
        "receipt key set diverged from the conformance verifier's required fields")

    assert receipt["callId"] == call_id
    assert receipt["callerPeerId"] == str(granted["caller_peer"])
    assert receipt["targetPeerId"] == str(granted["target_peer"])
    assert receipt["issuer"] == receipt["targetPeerId"]
    assert receipt["skillName"] == SKILL_NAME
    assert receipt["argsSha256"] == P.sha256(P.jcs(payload)).hex()
    # Absent metadata is "", never null: the receipt BINDS these fields, and a null and
    # an empty string canonicalize to different bytes.
    assert receipt["resultSha256"] == ""
    # The call is in flight (step 9 queued it, divergence 041 S5), so completedAt is the
    # empty string on a NULL, not null.
    assert receipt["completedAt"] == "", receipt["completedAt"]
    assert receipt["parentCallId"] == ""
    assert receipt["chainId"] == ""
    assert receipt["kid"] == ""
    # `signature` is the one optional key, so it is excluded from
    # the set above and asserted absent here rather than being allowed to drift silently.
    assert "signature" not in receipt

    # The per-field signatures are what carry non-repudiation; they are present because
    # the call was signed, and the result ones are absent because there is no result.
    assert body["args_signature"] == envelope["signature"]
    assert body["args_signing_kid"] == envelope["signing_kid"]
    assert "result_signature" not in body
    assert "result_signing_kid" not in body

    # Once the call ends, completedAt is SET, in `to_rfc3339()`'s `+00:00` spelling - not the
    # `Z` every SkillCall field uses. (Before 041 S5 step 9 failed this call at once, so the
    # first read above covered this; now the row is ended by hand, as a target would.)
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET status = 'failed', completed_at = NOW() "
                    "WHERE call_id = %s", (call_id,))
    owner_conn.commit()
    ended = body_of(client.get(f"/v1/calls/{call_id}/receipt",
                               headers=bearer(granted["caller_peer"])))["receipt"]
    assert ended["completedAt"].endswith("+00:00"), ended["completedAt"]


# ==========================================================================
# 8. cancel
# ==========================================================================

def test_cancel_is_caller_only(client, granted):
    """The TARGET can READ the call and may NOT cancel it (the not-the-caller refusal), and
    the stranger cannot do either. The caller's 200 is the control, asserted last so
    the two refusals are made against a call that was still cancellable."""
    call_id = in_flight_call(granted)
    url = f"/v1/calls/{call_id}/cancel"
    payload = json.dumps({"reason": "changed my mind"})

    assert client.get(f"/v1/calls/{call_id}",
                      headers=bearer(granted["target_peer"])).status_code == 200
    by_target = client.post(url, data=payload, content_type="application/json",
                            headers=bearer(granted["target_peer"]))
    assert by_target.status_code == 403, by_target.content
    assert body_of(by_target)["error"] == "E_FORBIDDEN"

    by_stranger = client.post(url, data=payload, content_type="application/json",
                              headers=bearer(granted["stranger_peer"]))
    assert by_stranger.status_code == 403

    by_caller = client.post(url, data=payload, content_type="application/json",
                            headers=bearer(granted["caller_peer"]))
    assert by_caller.status_code == 200, by_caller.content
    assert body_of(by_caller)["status"] == "abandoned"


def test_a_second_cancel_is_409_and_the_audit_event_names_the_canceller(
        client, owner_conn, granted):
    """A RETRY is a conflict, not a quiet success, and the audit event must name the
    AUTHENTICATED canceller rather than anything from the body.

    A lost race - the call was in flight when it was read and something else abandoned it
    in between - is success. A cancel whose pre-check sees a terminal status is rejected
    with `NotInFlight` for EVERY terminal status, `Abandoned` included, and maps to
    Conflict. Returning 200 told a caller its second cancel had done something.

    The first cancel is the control for the second.
    """
    call_id = in_flight_call(granted)
    url = f"/v1/calls/{call_id}/cancel"
    headers = bearer(granted["caller_peer"])

    first = client.post(url, data=json.dumps({"reason": "oops"}),
                        content_type="application/json", headers=headers)
    assert first.status_code == 200, first.content

    second = client.post(url, data=json.dumps({}), content_type="application/json",
                         headers=headers)
    assert second.status_code == 409, second.content
    assert body_of(second)["error"] == "E_CONFLICT"
    assert body_of(second)["message"] == "call not in flight (status: Abandoned)"

    with owner_conn.cursor() as cur:
        cur.execute("SELECT event_type, source, source_peer_id, details "
                    "FROM skill_call_events WHERE call_id = %s AND "
                    "event_type = 'caller_disconnected'", (call_id,))
        rows = cur.fetchall()
    owner_conn.commit()
    assert len(rows) == 1, "the refused second cancel must not re-emit the event"
    assert rows[0][1] == "caller"
    assert rows[0][2] == granted["caller_peer"]
    assert rows[0][3] == {"reason": "oops"}


def test_cancelling_a_terminal_call_is_409(client, owner_conn, granted):
    """A call that reached a DIFFERENT terminal state is a conflict, not an idempotent
    success. Control: the same route on an in-flight call returns 200."""
    cancellable = in_flight_call(granted)
    headers = bearer(granted["caller_peer"])
    ok = client.post(f"/v1/calls/{cancellable}/cancel", data=json.dumps({}),
                     content_type="application/json", headers=headers)
    assert ok.status_code == 200, ok.content

    completed = accepted_call(client, granted)
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET status='completed', completed_at=NOW() "
                    "WHERE call_id = %s", (completed,))
    owner_conn.commit()

    response = client.post(f"/v1/calls/{completed}/cancel", data=json.dumps({}),
                           content_type="application/json", headers=headers)
    assert response.status_code == 409, response.content
    body = body_of(response)
    assert body["error"] == "E_CONFLICT"
    # The not-in-flight refusal interpolates the DEBUG spelling of the status.
    assert body["message"] == "call not in flight (status: Completed)"


def test_a_cancel_that_loses_the_race_after_its_pre_check_is_refused_by_the_database(
        client, owner_conn, granted, monkeypatch):
    """STATUS C04 (review M11). `cancel_to_abandoned`'s `WHERE status = ANY(in_flight)` is
    the guard its docstring says the DATABASE arbitrates with, and no test ever reached it:
    the view's pre-check refuses a terminal call first, so deleting the predicate survived.

    The race is made DETERMINISTIC by committing the target's completion in the one window
    that matters - AFTER the view's pre-check read the call as in flight, BEFORE the
    guarded UPDATE runs - through the module attribute the view calls.

      CONTROL - the same wrapper, committing nothing: the cancel wins, 200 `abandoned`, one
                audit row. So the wrapper is on the path and the path works.
      FINDING - the completion lands in the window: the UPDATE matches no row, the view
                re-reads and answers 409 `Completed`, the completed row is untouched and
                no `caller_disconnected` row claims a cancel that did not happen.
    """
    from apps.calls import reads as _reads

    real = _reads.cancel_to_abandoned
    window: dict = {}

    def race_then_cancel(conn, call_id, source_peer_id, reason):
        if window.get("complete") == call_id:
            with owner_conn.cursor() as cur:
                cur.execute("UPDATE skill_calls SET status='completed', "
                            "result='{\"text\":\"won\"}', completed_at=NOW() "
                            "WHERE call_id = %s AND status = 'pending'", (call_id,))
                assert cur.rowcount == 1, "the race setup did not complete the call"
            owner_conn.commit()
        return real(conn, call_id, source_peer_id, reason)

    monkeypatch.setattr(_reads, "cancel_to_abandoned", race_then_cancel)
    headers = bearer(granted["caller_peer"])

    def disconnected_rows(call_id):
        with owner_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM skill_call_events WHERE call_id = %s AND "
                        "event_type = 'caller_disconnected'", (call_id,))
            (count,) = cur.fetchone()
        owner_conn.commit()
        return count

    # ---- CONTROL: nothing races; the guarded UPDATE wins.
    control_id = in_flight_call(granted)
    ok = client.post(f"/v1/calls/{control_id}/cancel", data=json.dumps({}),
                     content_type="application/json", headers=headers)
    assert ok.status_code == 200, ok.content
    assert body_of(ok)["status"] == "abandoned"
    assert disconnected_rows(control_id) == 1

    # ---- FINDING: the target completes the call between the pre-check and the UPDATE.
    raced_id = in_flight_call(granted)
    window["complete"] = raced_id
    lost = client.post(f"/v1/calls/{raced_id}/cancel", data=json.dumps({"reason": "late"}),
                       content_type="application/json", headers=headers)
    assert lost.status_code == 409, lost.content
    assert body_of(lost) == {"error": "E_CONFLICT",
                             "message": "call not in flight (status: Completed)"}
    row = row_of(owner_conn, raced_id)
    assert row["status"] == "completed", "the cancel overwrote a completed call"
    assert row["result"] == {"text": "won"}
    assert disconnected_rows(raced_id) == 0


def test_a_terminal_call_invoked_again_returns_its_result_inline(
        client, owner_conn, granted):
    """The sync-mode already-terminal branch:
    200 with the result inline rather than 202 + Location.

    The first invoke is the control: step 9 QUEUES the call, its target holding no socket
    (divergence 041 S5), so at the default sync wait it answers 202 with Location. The
    same envelope replayed after the row is moved to `completed` must change ONLY because
    the call's state changed.

    STATUS C07: THE BODY IS THAT BRANCH'S OWN, NOT `terminal_to_response`'s. It is built
    from a re-fetch of the row: `call_id, status, skill_version, mode "sync",
    deadline_at, result, error_code, error_message` - `deadline_at` and the two error keys
    present (null on a completed call), and NO `signature` / `signing_kid` / `completed_at`.
    This test used to pin the other shape, which this route served on both branches. The
    row here HAS a result signature, so its absence from the body is the builder's choice
    and not a missing column. A call that completes DURING the wait is still
    `terminal_to_response` (`test_a_sync_invoke_polls_until_a_concurrent_completion_lands`),
    as is a fresh memory-authority call (divergence 017, tests/test_call_routing.py).
    """
    sync_name = f"{SKILL_NAME}.sync"
    _skill(owner_conn, granted["target_peer"], sync_name, mode="sync")
    _grant(owner_conn, grantor_owner=granted["target_owner"],
           grantee_owner=granted["caller_owner"], granter_peer=granted["target_peer"],
           skill_name=sync_name)
    payload, envelope = sign_call(granted, skill_name=sync_name)
    first = post_invoke(client, granted, envelope, skill_name=sync_name)
    assert first.status_code == 202, first.content
    assert body_of(first)["status"] == "queued"
    assert "result" not in body_of(first)
    deadline_at = body_of(first)["deadline_at"]

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET status='completed', result=%s, "
                    "result_signature=%s, result_signing_kid=%s, completed_at=NOW() "
                    "WHERE call_id = %s",
                    (json.dumps({"text": "done"}), "rsig", "rkid", payload["call_id"]))
    owner_conn.commit()

    second = post_invoke(client, granted, envelope, skill_name=sync_name)
    assert second.status_code == 200, second.content
    expected = {
        "call_id": payload["call_id"], "status": "completed",
        "skill_version": SKILL_VERSION, "mode": "sync", "deadline_at": deadline_at,
        "result": {"text": "done"}, "error_code": None, "error_message": None,
    }
    # BYTES, so the hand-built response body's key order is held too, not only the key set.
    assert second.content.decode("utf-8") == json.dumps(
        expected, separators=(",", ":"), ensure_ascii=False)

    # The same branch on a FAILED call carries the error pair from the row.
    failed_payload, failed_envelope = sign_call(granted, skill_name=sync_name)
    assert post_invoke(client, granted, failed_envelope,
                       skill_name=sync_name).status_code == 202
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET status='failed', error_code='E_TARGET', "
                    "error_message='it broke', completed_at=NOW() WHERE call_id = %s",
                    (failed_payload["call_id"],))
    owner_conn.commit()
    failed = body_of(post_invoke(client, granted, failed_envelope, skill_name=sync_name))
    assert list(failed) == list(expected)
    assert (failed["status"], failed["result"], failed["error_code"],
            failed["error_message"]) == ("failed", None, "E_TARGET", "it broke")


@pytest.mark.parametrize("paused", ["input_required", "auth_required"])
def test_a_paused_call_invoked_again_is_answered_by_the_refetch_arm_not_the_wait(
        client, owner_conn, granted, monkeypatch, paused):
    """STATUS C07, review finding 1. ONLY routed, queued, pending and running calls go to the
    await arm; every other status of a sync call is re-fetched and answered 200 with the
    hand-built response body. That includes the two PAUSED statuses, which are neither in
    flight nor terminal.
    This route used to gate the re-fetch arm on a TERMINAL status, so a replayed paused
    call fell into `_await_terminal`: 202 at the default wait, and a block of up to 30 s
    at a configured one on a call that cannot finish inside it.

    ONE VARIABLE, the row's status at replay time. `_await_terminal` is wrapped so the arm
    each replay takes is observed directly, not inferred from a status code.
      CONTROL - replayed while still `queued`: the await arm (the spy records it) and 202.
      VARIANT - replayed at `input_required` / `auth_required`: the re-fetch arm, 200, the
                response body's bytes with the row's status, and the spy records nothing.
    """
    from apps.calls import views as _views

    awaited: list[str] = []
    real_await = _views._await_terminal

    def spying_await(conn, call_id, timeout):
        awaited.append(call_id)
        return real_await(conn, call_id, timeout)

    monkeypatch.setattr(_views, "_await_terminal", spying_await)

    sync_name = f"{SKILL_NAME}.sync"
    _skill(owner_conn, granted["target_peer"], sync_name, mode="sync")
    _grant(owner_conn, grantor_owner=granted["target_owner"],
           grantee_owner=granted["caller_owner"], granter_peer=granted["target_peer"],
           skill_name=sync_name)
    payload, envelope = sign_call(granted, skill_name=sync_name)
    first = post_invoke(client, granted, envelope, skill_name=sync_name)
    assert first.status_code == 202, first.content
    deadline_at = body_of(first)["deadline_at"]

    # ---- CONTROL: a replay of an IN-FLIGHT call waits, and at 0 s defers.
    awaited.clear()
    control = post_invoke(client, granted, envelope, skill_name=sync_name)
    assert control.status_code == 202, control.content
    assert body_of(control)["status"] == "queued"
    assert awaited == [payload["call_id"]], "the spy is not on the await arm"

    # ---- VARIANT: the same replay once the call is paused.
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET status=%s WHERE call_id = %s",
                    (paused, payload["call_id"]))
    owner_conn.commit()
    awaited.clear()
    replay = post_invoke(client, granted, envelope, skill_name=sync_name)
    assert replay.status_code == 200, replay.content
    assert awaited == [], f"a paused replay entered the await arm: {awaited}"
    expected = {
        "call_id": payload["call_id"], "status": paused,
        "skill_version": SKILL_VERSION, "mode": "sync", "deadline_at": deadline_at,
        "result": None, "error_code": None, "error_message": None,
    }
    assert replay.content.decode("utf-8") == json.dumps(
        expected, separators=(",", ":"), ensure_ascii=False)


def test_a_sync_invoke_polls_until_a_concurrent_completion_lands(
        client, owner_conn, granted, monkeypatch):
    """STATUS C12. Every other test here pins `MESHY_SYNC_TIMEOUT_SECONDS=0.0` (the
    fixture), so `_await_terminal`'s poll loop - the thing its divergence is ABOUT - never
    iterated in any test, and a version that read once and gave up passed them all.

    DETERMINISTIC, no second thread and no wall-clock race: the poll's own sleep is the
    hook. The SECOND sleep COMMITS the completion (as the target would, from another
    request), so the loop's next read must find it and answer 200 with the terminal body.
    The second, not the first: a fixed "read, sleep once, read" with no loop would pass a
    first-sleep completion (review of 129e325, finding 2), and fails this one with 202.

    The hook is `views._poll_sleep`, a name local to this view, NOT the process-wide
    `time.sleep`: patching `time.sleep` counts every sleep any thread in the process takes
    while the request runs, so `sleeps == []` and the count below would measure the
    process, not this loop.

      CONTROL - the same kind of call at the default wait (0 s): one read, 202 + Location,
                and the loop never sleeps.
      FINDING - at a real wait: the loop sleeps, re-reads, sleeps again, and returns the
                terminal row.
    """
    import time as _time

    from apps.calls import views as _views

    real_time_sleep = _time.sleep

    sync_name = f"{SKILL_NAME}.sync"
    _skill(owner_conn, granted["target_peer"], sync_name, mode="sync")
    _grant(owner_conn, grantor_owner=granted["target_owner"],
           grantee_owner=granted["caller_owner"], granter_peer=granted["target_peer"],
           skill_name=sync_name)

    sleeps: list[float] = []
    completing: dict = {}

    def completing_sleep(seconds):
        sleeps.append(seconds)
        call_id = completing.get("call_id")
        if call_id and len(sleeps) == 2:
            with owner_conn.cursor() as cur:
                cur.execute("UPDATE skill_calls SET status='completed', result=%s, "
                            "result_signature='rsig', result_signing_kid='rkid', "
                            "completed_at=NOW() WHERE call_id = %s",
                            (json.dumps({"text": "polled"}), call_id))
            owner_conn.commit()

    monkeypatch.setattr(_views, "_poll_sleep", completing_sleep)
    assert _time.sleep is real_time_sleep, "the hook must not replace the process's sleep"

    # ---- CONTROL: the default wait reads once and defers.
    _, control_envelope = sign_call(granted, skill_name=sync_name)
    control = post_invoke(client, granted, control_envelope, skill_name=sync_name)
    assert control.status_code == 202, control.content
    assert sleeps == [], "at a 0 s wait the loop must not sleep"

    # ---- FINDING: a real wait polls through the completion.
    payload, envelope = sign_call(granted, skill_name=sync_name)
    completing["call_id"] = payload["call_id"]
    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS=5.0):
        polled = post_invoke(client, granted, envelope, skill_name=sync_name)
    assert polled.status_code == 200, polled.content
    assert len(sleeps) == 2, sleeps
    body = body_of(polled)
    assert body["status"] == "completed"
    assert body["result"] == {"text": "polled"}
    # A call that was IN FLIGHT when start_call returned and completed during the wait is
    # rendered by `terminal_to_response`, signature and all.
    assert body["signature"] == "rsig"
    assert body["signing_kid"] == "rkid"


def test_the_sync_wait_is_bounded_by_the_configured_ceiling():
    """STATUS C12, the ceiling half: SYNC_TIMEOUT = 30 s bounds the poll, so
    a configured 999 waits 30, not 999. The controls are a value inside the bound, read
    as given, and the two values that cannot be a wait (negative, not a number), read as
    the 0 s default."""
    from apps.calls import views as _views

    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS=2.5):
        assert _views._sync_poll_seconds() == 2.5
    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS=999):
        assert _views._sync_poll_seconds() == _views.SYNC_TIMEOUT_SECONDS == 30.0
    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS=-1):
        assert _views._sync_poll_seconds() == 0.0
    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS="soon"):
        assert _views._sync_poll_seconds() == 0.0


# ==========================================================================
# 9. the encoder itself
# ==========================================================================

def test_the_response_encoder_is_compact_json(client, granted):
    """The response encoder emits raw UTF-8 and no whitespace; Python's json.dumps defaults to
    `ensure_ascii=True` and `", "` / `": "`. Both defaults are wrong on this wire, and
    both are invisible in any test that only reads the PARSED body - so this one reads
    the BYTES.

    The control is the parsed round trip: the same response must still decode to the
    string that was signed, so this cannot pass by mangling the value.

    STATUS C11 (`:994` in the review's numbering). The whitespace probe used to run on
    `raw.replace('": "caf', "X")`: it MASKED the one site where the non-ASCII value sits,
    so an encoder that put a space there passed. The mask is gone, and the probes are
    now PROVEN TO FIRE: each one must flag Python's own default encoding of the same
    parsed body before its silence on the served bytes means anything.
    """
    payload, envelope = sign_call(granted, args={"text": "café ✓"})
    assert post_invoke(client, granted, envelope).status_code == 202

    response = client.get(f"/v1/calls/{payload['call_id']}",
                          headers=bearer(granted["caller_peer"]))
    assert response.status_code == 200
    raw = response.content.decode("utf-8")
    parsed = body_of(response)
    assert parsed["args"]["args"]["text"] == "café ✓"

    def violations(text: str) -> set[str]:
        found = set()
        if "café ✓" not in text:
            found.add("escaped")          # non-ASCII must not be \u-escaped
        if '", "' in text or ', "' in text:
            found.add("item-space")       # no whitespace after `,`
        if '": ' in text:
            found.add("key-space")        # no whitespace after `:`
        return found

    # POSITIVE CONTROLS: every probe fires on the encoding it exists to catch.
    assert violations(json.dumps(parsed)) == {"escaped", "item-space", "key-space"}
    assert violations(json.dumps(parsed, ensure_ascii=False)) == {"item-space",
                                                                   "key-space"}
    assert violations(json.dumps(parsed, separators=(",", ":"))) == {"escaped"}
    # THE FINDING: none fires on the served bytes, with nothing masked.
    assert violations(raw) == set(), violations(raw)
    assert raw == json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)


# ==========================================================================
# 10. the layers underneath the SQL predicate
#
# Everything above this line tests the application's own filtering. A mutation
# campaign found that the SQL `WHERE` was the ONLY thing under test: serving every
# request on a connection that BYPASSES row-level security passed 28/28, because no
# test ever observed where the route's connection came from. These tests observe it.
# ==========================================================================

def test_a_request_served_on_an_rls_bypassing_connection_is_refused(
        client, owner_conn, granted, monkeypatch):
    """THE RLS LAYER, tested at RUNTIME rather than structurally.

    `tests/test_every_connection_is_role_bound.py` proves the `set_role` CALL is
    WRITTEN. It cannot prove the connection a request is served on is BOUND, because
    the mutation that matters does not delete the call - it opens a different
    connection entirely (a bare `psycopg.connect` as the bootstrap `meshy` role, which
    has `rolbypassrls = true`). That mutation passed every test in this file.

    So this test hands the route exactly that connection, through the ONE constructor
    `apps/calls/views.py:_route` is supposed to take it from, and requires a REFUSAL.

    THREE CONTROLS, because a 500 is also what a broken fixture, a dead database or a
    typo'd patch target produces:
      1. the unpatched route answers 200 with the call - the read works;
      2. the injected connection is ALIVE and BYPASSING - it reads the very row the
         refusal below must not disclose, and `rolbypassrls` is asserted, not assumed;
      3. the patch was actually REACHED (`opened`) - if the route stops using
         `dbconn.connect`, this fails naming that, which is the M4 mutation itself.
    """
    call_id = accepted_call(client, granted)
    url = f"/v1/calls/{call_id}"
    headers = bearer(granted["caller_peer"])

    # CONTROL 1 - the same request, same URL, same token, on the route's own connection.
    ok = client.get(url, headers=headers)
    assert ok.status_code == 200, ok.content
    assert body_of(ok)["call_id"] == call_id

    opened = []

    def bypassing_connect(target=None, **kwargs):
        """What `dbconn.connect` would be if nobody verified the role it set: an
        autocommit connection left on the bootstrap role. No SET ROLE, no probe."""
        conn = psycopg.connect(target or DSN, autocommit=True)
        opened.append(conn)
        return conn

    # CONTROL 2 - this connection is not broken and is not blind. It BYPASSES RLS and
    # can read the row, so the refusal below is about the connection's PRIVILEGE and
    # not about it being unusable.
    probe = psycopg.connect(DSN, autocommit=True)
    with probe.cursor() as cur:
        cur.execute("SELECT current_user, (SELECT rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user)")
        role, bypasses = cur.fetchone()
        assert bypasses is True, (role, bypasses)
        cur.execute("SELECT count(*) FROM skill_calls WHERE call_id = %s", (call_id,))
        assert cur.fetchone()[0] == 1, "control connection cannot see the seeded call"
    probe.close()

    monkeypatch.setattr("apps.core.dbconn.connect", bypassing_connect)
    refused = client.get(url, headers=headers)

    # CONTROL 3 - stated before the verdict so the M4 shape has its own message.
    assert opened, ("the route did not take its connection from apps.core.dbconn."
                    "connect(), so nothing verifies the role it serves on")
    assert refused.status_code == 500, refused.content
    assert body_of(refused)["error"] == "E_INTERNAL"
    # The refusal must not leak what the bypassing connection could see.
    assert call_id not in refused.content.decode("utf-8")


def test_a_deleted_peer_keeps_no_route_even_with_a_live_token(client, owner_conn,
                                                              granted):
    """`peers.status = 'active'` (apps/peers/auth.py), which nothing tested.

    The token is MINTED ONCE and reused unchanged across both halves, so the only
    difference between the accepted request and the refused one is a column in the
    database. Deleting the check let a revoked peer keep every route it had until its
    JWT expired.

    Two controls: the same peer with the same token before the status change (200), and
    the TARGET peer - untouched - still reading the same call afterwards (200), which is
    what separates "this peer was revoked" from "this call stopped being readable".
    """
    call_id = accepted_call(client, granted)
    url = f"/v1/calls/{call_id}"
    caller_token = {"authorization": f"Bearer {mint(granted['caller_peer'])}"}
    target_token = {"authorization": f"Bearer {mint(granted['target_peer'])}"}

    before = client.get(url, headers=caller_token)
    assert before.status_code == 200, before.content

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE peers SET status = 'deleted' WHERE id = %s",
                    (granted["caller_peer"],))
    owner_conn.commit()

    after = client.get(url, headers=caller_token)
    assert after.status_code == 403, after.content
    assert body_of(after)["error"] == "E_FORBIDDEN"

    # The invoke route is authenticated by the same extractor and must refuse too - a
    # revoked peer that can still WRITE is worse than one that can still read.
    _, envelope = sign_call(granted)
    invoked = client.post(
        invoke_url(granted["target_peer"]),
        data=json.dumps({"envelope": envelope,
                         "call_id": envelope["payload"]["call_id"]}),
        content_type="application/json", headers=caller_token)
    assert invoked.status_code == 403, invoked.content
    assert body_of(invoked)["error"] == "E_FORBIDDEN"

    # CONTROL - the call, the route and the database are all still fine.
    still_ok = client.get(url, headers=target_token)
    assert still_ok.status_code == 200, still_ok.content
    assert body_of(still_ok)["call_id"] == call_id


@pytest.mark.parametrize("status", ["suspended", "deleted"])
def test_an_invoke_on_an_inactive_target_is_refused_and_writes_no_call(
        client, owner_conn, granted, status):
    """STATUS C79. The pipeline's `status = 'active'` predicate on the TARGET peer.

    `apps/calls/authorize.py:lookup_owner_for_peer` is called TWICE at step 5 of
    `apps/calls/pipeline.py`: once for the target, once for the caller. The peer extractor
    also enforces the caller half (C02), so deleting the predicate was only ever observable
    on the TARGET - and no
    test invoked a skill on a target that was not active, so the deletion survived
    57 of 57. Nothing else on the invoke path reads `peers.status` for the target:
    `repository.resolve_for_call` selects the skill by `peer_id` alone and the grant
    lookup keys on `granter_peer_id`, so without this predicate a suspended or deleted
    peer keeps serving every skill it had granted.

    ONE VARIABLE: the target's `peers.status`. Same caller, same token signer, same
    grant, same skill, a fresh signed envelope each time.
      CONTROL 1 - the target is active: accepted (202).
      VARIANT   - the target is `suspended` / `deleted`: 403 E_FORBIDDEN, and NO
                  `skill_calls` row was written for the refused call_id (a 403 over a
                  written call would still have executed it).
      CONTROL 2 - the target is set back to active: accepted again, so the 403 was
                  the status and not a grant, key or skill that broke in between.
    """
    accepted_call(client, granted)

    def set_target_status(value):
        with owner_conn.cursor() as cur:
            cur.execute("UPDATE peers SET status = %s WHERE id = %s",
                        (value, granted["target_peer"]))
            assert cur.rowcount == 1, "the target peer row was not updated"
        owner_conn.commit()

    set_target_status(status)
    payload, envelope = sign_call(granted)
    refused = post_invoke(client, granted, envelope)
    assert refused.status_code == 403, refused.content
    assert body_of(refused)["error"] == "E_FORBIDDEN"
    assert row_of(owner_conn, payload["call_id"]) is None, (
        "the refused invoke still wrote its call row")

    set_target_status("active")
    accepted_call(client, granted)


def test_an_envelope_signed_for_another_target_is_refused_403(client, owner_conn,
                                                             granted):
    """The CROSS-TARGET half of the confused-deputy guard (pipeline step 4b).

    `test_an_envelope_signed_for_another_skill_is_refused_403` moves the SKILL segment.
    This moves the TARGET PEER, which is the rebind that matters most: the two targets
    are different tenants, so an envelope that could be replayed at the wrong one would
    let a caller spend a grant it was given for A against B.

    The second target is built to be IDENTICAL on every axis except identity - same
    skill name, same version, its own grant to the same caller - so the 403 cannot be
    a missing skill (404) or a missing grant (a different 403's cause). Both targets are
    asserted to ACCEPT their own envelope first; only then is the crossed one a finding.
    """
    other_target_owner = _owner(owner_conn, "target2")
    other_target_peer = _peer(owner_conn, other_target_owner, "target2")
    _skill(owner_conn, other_target_peer, SKILL_NAME)
    _grant(owner_conn, grantor_owner=other_target_owner,
           grantee_owner=granted["caller_owner"], granter_peer=other_target_peer,
           skill_name=SKILL_NAME)

    # CONTROL A - the original target accepts an envelope signed for it.
    _, for_a = sign_call(granted, target_peer=granted["target_peer"])
    a_ok = post_invoke(client, granted, for_a, target=granted["target_peer"])
    assert a_ok.status_code == 202, a_ok.content

    # CONTROL B - the new target accepts an envelope signed for IT, which proves its
    # skill exists and its grant is live.
    _, for_b = sign_call(granted, target_peer=other_target_peer)
    b_ok = post_invoke(client, granted, for_b, target=other_target_peer)
    assert b_ok.status_code == 202, b_ok.content

    # THE FINDING - signed for A, posted at B. One axis differs from control B.
    _, crossed = sign_call(granted, target_peer=granted["target_peer"])
    response = post_invoke(client, granted, crossed, target=other_target_peer)
    assert response.status_code == 403, response.content
    assert body_of(response)["error"] == "E_FORBIDDEN"
    assert body_of(response)["message"] == "forbidden"
    # And it must not have been recorded: a refused rebind that still wrote the call
    # would have bound A's signed args to B.
    assert row_of(owner_conn, crossed["payload"]["call_id"]) is None


def test_the_cancel_and_its_audit_row_commit_together(client, owner_conn, granted,
                                                      monkeypatch):
    """The abandon UPDATE and the `caller_disconnected` INSERT are ONE transaction.

    They used to be two statements on an AUTOCOMMIT connection: the UPDATE committed by
    itself and the audit INSERT was a separate commit issued afterwards. Anything in
    between - a raise, a dropped connection, a statement timeout - left the call
    terminal with nothing recording who abandoned it, which is the one direction an
    audit row must not fail in.

    The fault is injected at the audit write, which is where a real failure would land.
    CONTROL FIRST: an un-injected cancel on an identical call returns 200, writes the
    status and writes exactly one event - so the assertions below are about ATOMICITY
    and not about cancel being broken.
    """
    from apps.calls import reads as _reads

    # ---- CONTROL: the cancel works, and both writes land.
    control_id = in_flight_call(granted)
    headers = bearer(granted["caller_peer"])
    ok = client.post(f"/v1/calls/{control_id}/cancel",
                     data=json.dumps({"reason": "control"}),
                     content_type="application/json", headers=headers)
    assert ok.status_code == 200, ok.content
    assert row_of(owner_conn, control_id)["status"] == "abandoned"
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_call_events WHERE call_id = %s AND "
                    "event_type = 'caller_disconnected'", (control_id,))
        assert cur.fetchone()[0] == 1
    owner_conn.commit()

    # ---- THE FINDING: the audit write fails, so the status change must not survive.
    victim_id = in_flight_call(granted)
    assert row_of(owner_conn, victim_id)["status"] == "pending"

    def exploding_audit(conn, call_id, source_peer_id, reason):
        raise RuntimeError("audit write failed between the two statements")

    monkeypatch.setattr(_reads, "insert_caller_disconnected", exploding_audit)
    broken = client.post(f"/v1/calls/{victim_id}/cancel",
                         data=json.dumps({"reason": "victim"}),
                         content_type="application/json", headers=headers)
    assert broken.status_code == 500, broken.content
    assert body_of(broken)["error"] == "E_INTERNAL"

    row = row_of(owner_conn, victim_id)
    assert row["status"] == "pending", (
        "the abandon UPDATE outlived the audit INSERT that was supposed to accompany "
        "it: the call is terminal with no record of who cancelled it")
    assert row["completed_at"] is None
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_call_events WHERE call_id = %s AND "
                    "event_type = 'caller_disconnected'", (victim_id,))
        assert cur.fetchone()[0] == 0
    owner_conn.commit()

    # The call is therefore still cancellable, which is the point of rolling back.
    monkeypatch.undo()
    retried = client.post(f"/v1/calls/{victim_id}/cancel", data=json.dumps({}),
                          content_type="application/json", headers=headers)
    assert retried.status_code == 200, retried.content
    assert row_of(owner_conn, victim_id)["status"] == "abandoned"


def test_an_empty_listing_carries_its_scope_instead_of_a_bare_empty_list(
        client, granted, caplog):
    """`reads.Empty.scope` reaches a caller.

    `reads.py` exists to make four different causes of `[]` distinguishable, and every
    list route here discarded that with `... if isinstance(listing, reads.Rows) else []`
    - so the one module written to prevent "an empty list means something, probably"
    was read by nobody. An `Empty` is a query that PROVED its context was bound and
    knows the search space it searched; a 200 `[]` must say which.

    THE PAIR IS THE TEST. The same route on the same call, once with rows and once
    without: the populated read must NOT log a scope (or the assertion below would pass
    on a line emitted unconditionally), and the empty read must.
    """
    import logging

    call_id = accepted_call(client, granted)
    headers = bearer(granted["caller_peer"])

    def scope_lines():
        return [r.getMessage() for r in caplog.records
                if r.name == "meshychat.calls" and "scope=" in r.getMessage()]

    caplog.set_level(logging.INFO, logger="meshychat.calls")

    # CONTROL - events EXIST on an accepted call ('created', then step 9's 'queued'),
    # so this listing is Rows.
    caplog.clear()
    populated = client.get(f"/v1/calls/{call_id}/events", headers=headers)
    assert populated.status_code == 200, populated.content
    assert [e["event_type"] for e in body_of(populated)] == ["created", "queued"]
    assert scope_lines() == [], (
        "a populated listing must not log a scope, or the empty case below proves "
        "nothing about which arm ran")

    # THE FINDING - chunks exist for no call here, so this listing is Empty.
    caplog.clear()
    empty = client.get(f"/v1/calls/{call_id}/chunks", headers=headers)
    assert empty.status_code == 200, empty.content
    assert body_of(empty) == []
    logged = scope_lines()
    assert len(logged) == 1, logged
    assert "skill_call_chunks" in logged[0]
    assert call_id in logged[0], (
        "the scope must name the search space that was actually queried")

    # An untyped absence must RAISE rather than render as 200 [] - the `else []` this
    # replaced could not tell a bare list from a proven-empty one.
    from apps.calls import views as _views
    with pytest.raises(TypeError):
        _views._listing_rows([], "probe")
    # Control for the raise: the two real arms do not raise.
    assert _views._listing_rows(reads.Rows(rows=[{"a": 1}]), "probe") == [{"a": 1}]
    assert _views._listing_rows(reads.Empty(scope="probe-scope"), "probe") == []


# ==========================================================================
# 12. envelope replay, with the default in force (divergence 036)
# ==========================================================================
#
# Nothing in this file sets MESHY_ENVELOPE_NONCE_ENFORCE except the blocks that are the
# explicit opt-out, so every request here runs the shipped default. WHAT A REPLAY IS ON
# THIS WIRE, measured rather than assumed: a byte-identical resend of an envelope whose
# first presentation CREATED a call is answered by step 2 (idempotency) from that row and
# never reaches the ledger (`test_an_idempotent_replay_returns_the_same_call` above, which
# is what keeps a lost-response retry working). The ledger decides every other resend:
# one whose first presentation passed step 4c and created no call, an old nonce re-signed
# under a new call_id, and a payload with no nonce at all.

def _spy_on_nonce_refusals(monkeypatch) -> list[str]:
    """The reason each nonce refusal carried, through the function step 4c calls. It
    passes the refusal through unchanged; the wire renders every NotAuthorized as the
    same fixed 403, so this is the only place the typed reason is visible."""
    from apps.calls import nonce_ledger

    reasons: list[str] = []
    real = nonce_ledger.to_rpc_error

    def spy(exc):
        err = real(exc)
        reasons.append(err.message)
        return err

    monkeypatch.setattr(nonce_ledger, "to_rpc_error", spy)
    return reasons


def test_a_byte_identical_envelope_is_refused_the_second_time_it_reaches_the_gate(
        client, owner_conn, world, monkeypatch):
    """THE REPLAY THE LEDGER EXISTS FOR, over HTTP with the default in force. The first
    presentation is refused at step 5 (no grant) AFTER step 4c has recorded its nonce, so
    no call row exists and idempotency cannot answer the resend. The grant then appears.
    Without the ledger the SAME bytes, captured or retried, would now start a call inside
    the 120 s window.

    The refusal is the delta between three requests: a FRESH envelope under the same
    grant is accepted (so the 403 is not the grant); the byte-identical resend is refused
    403 E_FORBIDDEN carrying NotAuthorized("envelope replay"); and the SAME resend under
    the explicit opt-out is accepted (so the refusal is the gate and nothing else).
    """
    reasons = _spy_on_nonce_refusals(monkeypatch)
    payload, envelope = sign_call(world)
    first = post_invoke(client, world, envelope)
    assert first.status_code == 403, first.content
    assert body_of(first)["error"] == "E_FORBIDDEN"
    assert row_of(owner_conn, payload["call_id"]) is None
    assert nonces_recorded(owner_conn, world["caller_peer"], payload["nonce"]) == 1, (
        "the shipped default did not run the nonce gate on this request")
    assert reasons == [], "the first presentation was refused by the grant, not the gate"

    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"],
           skill_name=SKILL_NAME)

    # POSITIVE CONTROL: a fresh envelope, same caller, same skill, same grant.
    _, fresh = sign_call(world)
    control = post_invoke(client, world, fresh)
    assert control.status_code == 202, control.content

    replay = post_invoke(client, world, envelope)
    assert replay.status_code == 403, replay.content
    assert body_of(replay) == {"error": "E_FORBIDDEN", "message": "forbidden"}
    assert reasons == ["envelope replay"]
    assert row_of(owner_conn, payload["call_id"]) is None, "a refused replay started a call"

    # THE DELTA: the explicit opt-out, and nothing else changed.
    with override_settings(MESHY_ENVELOPE_NONCE_ENFORCE=False):
        opted_out = post_invoke(client, world, envelope)
    assert opted_out.status_code == 202, opted_out.content
    assert row_of(owner_conn, payload["call_id"]) is not None
    assert reasons == ["envelope replay"], "the opt-out still called the gate"


def test_an_old_nonce_re_signed_under_a_fresh_call_id_is_refused_over_http(
        client, owner_conn, granted, monkeypatch):
    """The other half of gate 8: the signer reuses a nonce under a NEW call_id, so step 2
    sees a different key and only the ledger can refuse it. tests/test_invoke.py proves
    this at the pipeline; this proves the route runs it by default. Control: a fresh
    nonce from the same peer, same grant, is accepted after the refusal."""
    reasons = _spy_on_nonce_refusals(monkeypatch)
    nonce = f"replay-{uuid.uuid4()}"
    first_payload, first = sign_call(granted, nonce=nonce)
    assert post_invoke(client, granted, first).status_code == 202

    reused_payload, reused = sign_call(granted, nonce=nonce)
    assert reused_payload["call_id"] != first_payload["call_id"]
    refused = post_invoke(client, granted, reused)
    assert refused.status_code == 403, refused.content
    assert body_of(refused) == {"error": "E_FORBIDDEN", "message": "forbidden"}
    assert reasons == ["envelope replay"]
    assert row_of(owner_conn, reused_payload["call_id"]) is None

    _, fresh = sign_call(granted)
    assert post_invoke(client, granted, fresh).status_code == 202


def test_a_payload_with_no_nonce_is_refused_by_default_and_accepted_when_opted_out(
        client, owner_conn, granted, monkeypatch):
    """Enforcement ON means a pre-nonce signer is refused, not waved through. Control: the
    same caller's envelope WITH a nonce is accepted. The delta: the same nonceless bytes under
    the explicit opt-out are accepted: that is the deploy-dark mode, which exists so pre-nonce
    signers keep working until they are updated."""
    reasons = _spy_on_nonce_refusals(monkeypatch)
    _, control = sign_call(granted)
    assert post_invoke(client, granted, control).status_code == 202

    payload, nonceless = sign_call(granted, nonce=OMIT)
    assert "nonce" not in payload
    refused = post_invoke(client, granted, nonceless)
    assert refused.status_code == 403, refused.content
    assert body_of(refused) == {"error": "E_FORBIDDEN", "message": "forbidden"}
    assert reasons == ["missing envelope nonce"]
    assert row_of(owner_conn, payload["call_id"]) is None

    with override_settings(MESHY_ENVELOPE_NONCE_ENFORCE=False):
        opted_out = post_invoke(client, granted, nonceless)
    assert opted_out.status_code == 202, opted_out.content
    assert row_of(owner_conn, payload["call_id"]) is not None


def test_the_nonce_sweep_prunes_what_the_default_now_records(client, owner_conn, granted):
    """With the gate ON the ledger GROWS by one row per call that reaches step 4c, so the
    job that bounds it is load-bearing rather than idle. Two calls over HTTP, one of their
    rows aged past its window, one run of the REGISTERED job: the aged row goes and the
    live one stays (the control: a sweep that deleted everything would also "prune")."""
    from apps.jobs import registry

    aged_payload, aged = sign_call(granted)
    live_payload, live = sign_call(granted)
    assert post_invoke(client, granted, aged).status_code == 202
    assert post_invoke(client, granted, live).status_code == 202
    caller = granted["caller_peer"]
    assert nonces_recorded(owner_conn, caller, aged_payload["nonce"]) == 1
    assert nonces_recorded(owner_conn, caller, live_payload["nonce"]) == 1

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE envelope_nonce_seen "
                    "   SET expires_at = now() - interval '1 second' "
                    " WHERE peer_id = %s AND nonce = %s", (caller, aged_payload["nonce"]))
    owner_conn.commit()

    job = registry.by_name("envelope_nonce_sweep")
    with psycopg.connect(DSN, autocommit=True) as conn:
        deleted = job.run(conn, job)
    assert deleted >= 1

    assert nonces_recorded(owner_conn, caller, aged_payload["nonce"]) == 0
    assert nonces_recorded(owner_conn, caller, live_payload["nonce"]) == 1, (
        "the sweep deleted a nonce still inside its window")


# ==========================================================================
# 13. what the review of 036 found (the ledger's edges)
# ==========================================================================
#
# Each test below failed on the tree the review read (d447af0) or kills a mutant that
# survived every test there. The fleet's production client (mem_client) resends the SAME
# body on any 5xx: that is finding 1, and the reason the claim moved into the call row's
# transaction. The rest are the retention, the sweep, the empty nonce, the one reader and
# the server-side reason.

def _fail_the_call_insert_once(monkeypatch) -> dict:
    """Make step 8's INSERT fail ONCE, inside its transaction, the way a dropped database
    connection or a killed worker ends a request between the gate and the call row.
    `repository.Jsonb` is used by `insert_call` and nothing else on this path, and it is
    built inside the `rls.server_internal` transaction, after anything the pipeline wrote
    in that transaction and before the call row."""
    real = repository.Jsonb
    budget = {"left": 1}

    def failing(value):
        if budget["left"] > 0:
            budget["left"] -= 1
            raise psycopg.OperationalError("simulated: server closed the connection")
        return real(value)

    monkeypatch.setattr(repository, "Jsonb", failing)
    return budget


def test_a_same_bytes_retry_after_a_server_failure_before_the_call_row_is_accepted(
        owner_conn, granted, monkeypatch):
    """REVIEW FINDING 1. mem_client (the fleet's client, the one wire.call_envelope cites)
    re-POSTs the SAME body - same envelope, same nonce, fresh DPoP - on any 5xx. At
    d447af0 the nonce was recorded on the autocommit connection at step 4c, so a failure
    between 4c and the call row left a ledger row and no call, and that retry was refused
    403 as a replay of a call that never existed.

    The claim is now written in the SAME transaction as the call row: a request that dies
    before the row commits leaves nothing, and the resend runs. The control that the
    ledger still refuses what it should: `test_a_byte_identical_envelope_is_refused_the_
    second_time_it_reaches_the_gate` above (a REFUSED first presentation still burns its
    nonce), and the last request here, the same bytes once more after the call exists,
    answered from the row."""
    budget = _fail_the_call_insert_once(monkeypatch)
    c = Client(raise_request_exception=False)
    payload, envelope = sign_call(granted)

    first = post_invoke(c, granted, envelope)
    assert first.status_code == 500, first.content
    assert body_of(first)["error"] == "E_INTERNAL"
    assert budget["left"] == 0, "positive control: the failure fired, inside step 8"
    assert row_of(owner_conn, payload["call_id"]) is None
    assert nonces_recorded(owner_conn, granted["caller_peer"], payload["nonce"]) == 0, (
        "the failed request left its nonce on record: the same-bytes retry will be refused")

    retry = post_invoke(c, granted, envelope)
    assert retry.status_code == 202, retry.content
    assert body_of(retry)["call_id"] == payload["call_id"]
    assert row_of(owner_conn, payload["call_id"]) is not None
    assert nonces_recorded(owner_conn, granted["caller_peer"], payload["nonce"]) == 1, (
        "the accepted retry must leave its nonce on record, with its call")

    again = post_invoke(c, granted, envelope)
    assert again.status_code == 202, again.content
    assert body_of(again)["call_id"] == payload["call_id"]


def test_a_typed_server_error_after_the_gate_burns_nothing(owner_conn, granted,
                                                          monkeypatch):
    """The burn is for REFUSALS (4xx). An `RpcError` whose status is 5xx is the server
    failing, like the untyped failure above, and must leave the resend runnable. Step 5
    raises one here, once. Control: the resend, with nothing raised, is accepted."""
    from apps.calls import authorize as authz
    from apps.calls.errors import RpcError

    real = authz.authorize
    budget = {"left": 1}

    def failing(*a, **kw):
        if budget["left"] > 0:
            budget["left"] -= 1
            raise RpcError("simulated: the grant store did not answer")
        return real(*a, **kw)

    monkeypatch.setattr(authz, "authorize", failing)
    c = Client(raise_request_exception=False)
    payload, envelope = sign_call(granted)
    first = post_invoke(c, granted, envelope)
    assert first.status_code == 500, first.content
    assert nonces_recorded(owner_conn, granted["caller_peer"], payload["nonce"]) == 0
    retry = post_invoke(c, granted, envelope)
    assert retry.status_code == 202, retry.content
    assert row_of(owner_conn, payload["call_id"]) is not None


def test_the_claim_at_step_8_is_atomic_against_a_presentation_that_raced_it(
        owner_conn, granted, monkeypatch):
    """The step 4c check is a READ now; the claim is the INSERT ... ON CONFLICT DO NOTHING
    RETURNING in the call row's transaction. So a presentation of the same (peer, nonce)
    that lands between the two must still lose: the row is planted after step 4c passed
    (inside authorization, step 5) and the request must be refused as a replay with no
    call. Control: the same request with nothing planted is accepted."""
    from apps.calls import authorize as authz

    reasons = _spy_on_nonce_refusals(monkeypatch)
    real = authz.authorize
    plant = {"nonce": None}

    def authorize_then_race(*a, **kw):
        out = real(*a, **kw)
        if plant["nonce"] is not None:
            with owner_conn.cursor() as cur:
                # DO NOTHING: at d447af0 the request had already written this row at
                # step 4c, so there the plant is a no-op and the request goes on to win.
                cur.execute("INSERT INTO envelope_nonce_seen (peer_id, nonce, expires_at) "
                            "VALUES (%s, %s, now() + interval '4 minutes') "
                            "ON CONFLICT DO NOTHING",
                            (granted["caller_peer"], plant["nonce"]))
            owner_conn.commit()
        return out

    monkeypatch.setattr(authz, "authorize", authorize_then_race)

    control_payload, control = sign_call(granted)
    assert post_invoke(Client(), granted, control).status_code == 202
    assert row_of(owner_conn, control_payload["call_id"]) is not None

    payload, envelope = sign_call(granted)
    plant["nonce"] = payload["nonce"]
    raced = post_invoke(Client(), granted, envelope)
    assert raced.status_code == 403, raced.content
    assert body_of(raced) == {"error": "E_FORBIDDEN", "message": "forbidden"}
    assert reasons == ["envelope replay"]
    assert row_of(owner_conn, payload["call_id"]) is None, "the losing claim left a call"


def test_a_ledger_row_is_kept_for_two_windows_past_its_issued_at(
        client, owner_conn, world):
    """REVIEW FINDING 3, and mutants M1 (a quarter of the window) and HZ (anchored on the
    server's clock, not issued_at). Retention was exactly one window, while freshness is
    judged on the app's clock at the start of the request and the sweep deletes on the
    database's clock: a sweep landing in between could delete the first presentation's
    row under a replay that had already passed freshness. Two windows past `issued_at`,
    on BOTH ways a row is written: the claim that commits with an accepted call, and the
    burn a refused first presentation leaves."""
    issued = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=10)

    def expires_of(nonce):
        with owner_conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM envelope_nonce_seen "
                        "WHERE peer_id = %s AND nonce = %s", (world["caller_peer"], nonce))
            (expires_at,) = cur.fetchone()
        owner_conn.commit()
        return expires_at

    refused_payload, refused = sign_call(world, issued_at=issued)
    assert post_invoke(client, world, refused).status_code == 403     # step 5: no grant
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"],
           skill_name=SKILL_NAME)
    accepted_payload, accepted = sign_call(world, issued_at=issued)
    assert post_invoke(client, world, accepted).status_code == 202

    for payload in (refused_payload, accepted_payload):
        assert expires_of(payload["nonce"]) - issued == _dt.timedelta(seconds=240), (
            payload["nonce"])


def _sweep_in_the_gap(owner_conn, world, monkeypatch, *, sweep: bool,
                      skill_name: str) -> tuple[int, bool]:
    """A first presentation issued 118.5 s ago is refused at step 5 (its nonce burned);
    the grant appears; the same bytes come back. `verify_payload` passes them (inside the
    window by the app's clock), and then the gap between it and the ledger is widened past
    the end of the first window, where the registered sweep may run."""
    import time as _time

    from apps.calls import pipeline as pl
    from apps.jobs import registry

    real_verify = pl.verify_payload
    issued = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=118.5)

    def widened(*a, **kw):
        verified = real_verify(*a, **kw)
        wait = (issued + _dt.timedelta(seconds=121)
                - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        _time.sleep(max(0.0, wait))
        if sweep:
            job = registry.by_name("envelope_nonce_sweep")
            with psycopg.connect(DSN, autocommit=True) as conn:
                job.run(conn, job)
        return verified

    payload, envelope = sign_call(world, issued_at=issued, skill_name=skill_name)
    first = post_invoke(Client(), world, envelope, skill_name=skill_name)
    assert first.status_code == 403, first.content
    assert nonces_recorded(owner_conn, world["caller_peer"], payload["nonce"]) == 1
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"],
           skill_name=skill_name)
    monkeypatch.setattr(pl, "verify_payload", widened)
    try:
        replay = post_invoke(Client(), world, envelope, skill_name=skill_name)
    finally:
        monkeypatch.setattr(pl, "verify_payload", real_verify)
    return replay.status_code, row_of(owner_conn, payload["call_id"]) is not None


def test_a_sweep_between_the_freshness_check_and_the_ledger_cannot_readmit_a_replay(
        owner_conn, world, monkeypatch):
    """REVIEW FINDING 3, end to end. At d447af0 the replay here got 202 and started a
    call: the row expired at issued_at + 120 s, the sweep in the gap deleted it, and the
    ledger then saw a fresh nonce. The CONTROL is the same widened gap with no sweep in
    it, refused on both trees, so the 202 was the sweep and not the widening."""
    assert _sweep_in_the_gap(owner_conn, world, monkeypatch, sweep=False,
                             skill_name=OTHER_SKILL) == (403, False)
    assert _sweep_in_the_gap(owner_conn, world, monkeypatch, sweep=True,
                             skill_name=SKILL_NAME) == (403, False)


def test_an_empty_nonce_is_refused_as_a_missing_nonce(client, owner_conn, granted,
                                                      monkeypatch):
    """Mutant M3: `nonce == ""` accepted as a nonce survived every test. The empty string
    is MISSING, not a value every signer could share.
    Control: the same caller's payload with a real nonce is accepted."""
    reasons = _spy_on_nonce_refusals(monkeypatch)
    payload, envelope = sign_call(granted)
    payload["nonce"] = ""
    envelope = {"payload": payload, "signature": P.b64u(P.sign(granted["seed"],
                                                               P.jcs(payload))),
                "signing_kid": granted["kid"]}
    refused = post_invoke(client, granted, envelope)
    assert refused.status_code == 403, refused.content
    assert reasons == ["missing envelope nonce"]
    assert row_of(owner_conn, payload["call_id"]) is None
    assert nonces_recorded(owner_conn, granted["caller_peer"], "") == 0

    _, control = sign_call(granted)
    assert post_invoke(client, granted, control).status_code == 202
    assert reasons == ["missing envelope nonce"]


def test_invoke_takes_the_gate_from_the_one_reader(client, owner_conn, granted, monkeypatch):
    """Mutant M5's twin on this route: a view that reads the setting itself (a getattr
    with its own default, the name split so a text scan misses it) behaves the same while
    settings.py defines the name, and survived the name scan. So the reader is replaced
    here by one that answers OFF while the setting says ON: the route must ask it once per
    request and obey it. The nonceless payload is the witness - accepted only when the
    route used the reader's answer. Control: the setting is ON in this run."""
    from apps.calls import nonce_ledger

    assert nonce_ledger.enforce_from_settings() is True
    asked = []

    def reader_says_off():
        asked.append(1)
        return False

    monkeypatch.setattr(nonce_ledger, "enforce_from_settings", reader_says_off)
    payload, nonceless = sign_call(granted, nonce=OMIT)
    response = post_invoke(client, granted, nonceless)
    assert response.status_code == 202, response.content
    assert asked == [1]
    assert row_of(owner_conn, payload["call_id"]) is not None


def test_a_nonce_refusal_keeps_its_reason_in_the_server_log(client, owner_conn, world,
                                                            caplog):
    """REVIEW FINDING 5. The wire says 403 `forbidden` for a replay, a missing nonce and a
    missing grant alike. 036 said the reason stays on the server; at
    d447af0 it went nowhere - `_route` logs 5xx only. Now the pipeline logs it at WARNING
    with the call and the peer. Control: the grant refusal before it logs no gate-8 line,
    so the line names the gate that refused and is not a generic 403 log.

    The resend BEFORE the grant exists is refused at step 4c, ahead of authorization, because
    the pipeline orders the nonce check before the grant check: the claim at step 8 would also
    refuse a replay, but only one that got through authorization, and this one would have been
    reported as a missing grant."""
    import logging

    logger = logging.getLogger("meshychat.calls")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="meshychat.calls"):
            payload, envelope = sign_call(world)
            assert post_invoke(client, world, envelope).status_code == 403    # no grant
            assert "gate 8" not in caplog.text, caplog.text
            assert post_invoke(client, world, envelope).status_code == 403    # replay
            _grant(owner_conn, grantor_owner=world["target_owner"],
                   grantee_owner=world["caller_owner"], granter_peer=world["target_peer"],
                   skill_name=SKILL_NAME)
            assert post_invoke(client, world, envelope).status_code == 403    # replay
            _, nonceless = sign_call(world, nonce=OMIT)
            assert post_invoke(client, world, nonceless).status_code == 403
    finally:
        logger.removeHandler(caplog.handler)
    # By identity: when the "meshychat" logger propagates to the root, the handler added
    # above and pytest's own root handler both see each record.
    lines = list({id(r): r for r in caplog.records if "gate 8" in r.getMessage()}.values())
    assert len(lines) == 3, caplog.text
    for replay in lines[:2]:
        assert "envelope replay" in replay.getMessage()
        assert payload["call_id"] in replay.getMessage()
        assert str(world["caller_peer"]) in replay.getMessage()
    assert "missing envelope nonce" in lines[2].getMessage()
    assert all(r.levelno == logging.WARNING for r in lines)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
