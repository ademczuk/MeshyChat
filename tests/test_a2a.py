"""The A2A shim - the projection types, the two task routes and `message:send` - over
real HTTP, the real URLconf and a real database.

THREE RULES THIS FILE IS WRITTEN TO, all three from defects in this project.

1. EVERY NEGATIVE ASSERTION CARRIES A POSITIVE CONTROL IN THE SAME TEST. A 403 is also
   what an unseeded peer, a typo'd URL, a dead connection or a broken fixture produces,
   so a rejection ON ITS OWN proves the surface is BROKEN, not that it is safe. Each
   test below builds two requests differing in exactly ONE property, asserts the
   control is ACCEPTED, and only then asserts the variant is REJECTED. The delta is the
   finding. The same rule applies to "field absent" assertions: an absence is only
   meaningful next to a sibling field that IS present, otherwise an empty body passes.

2. NO `ROOT_URLCONF` OVERRIDE ANYWHERE IN THIS FILE, and none may be added. The
   previous round of route modules pinned their own URLconf per test: every route test
   passed while the application served 404. `test_the_project_urlconf_is_what_these_
   tests_resolve_against` is the named control that makes every other failure here
   readable.

3. THE CONFORMANCE FIXTURE IS THE ORACLE, NOT A COPY OF IT. The wire vectors are LOADED
   from `tests/fixtures/a2a/reference-tasks.json`; before they were added to the fixture,
   `a2a_lifecycle_verify.py` checked them against the ProtoJSON parser outside this
   repository. Re-typing the expected JSON into this file would make it a test of my own
   transcription: it would pass just as happily if I copied a mistake from the code under test.

NO MOCKS. Every row is written to the migrated Postgres named by MESHY_TEST_DSN.

Run: .venv/Scripts/python.exe -m pytest tests/test_a2a.py -q
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import threading
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
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
os.environ.setdefault("MESHY_DSN", DSN)

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.a2a import types  # noqa: E402
from apps.calls import nonce_ledger, reads  # noqa: E402
from apps.core import dbconn  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402

ISSUER = "meshy-a2a-test"
SKILL_NAME = "a2a.test.skill"
SKILL_VERSION = "1.0.0"

#: The conformance oracle was added to this repository on 2026-09-21 and is pinned at
#: sha256 444b7b1dedca.... It carries its own `provenance` block naming the A2A v1.0.1
#: proto and the ProtoJSON parser.
#:
#: WHY LOCAL FIXTURE. The previous path was `parents[2] / "meshychat-graphfresh-clone" / ...`,
#: which resolves to a SIBLING DIRECTORY named after one contributor's clone. It existed on
#: exactly one machine, so three tests here errored on every other checkout, CI included,
#: with "the conformance oracle is missing". The loud-fail below is kept on purpose: a
#: conformance suite that silently skips when its oracle is missing is not a conformance
#: suite. The oracle must remain in this repository; its sha identifies the fixture version.
REFERENCE_TASKS = Path(__file__).resolve().parent / "fixtures" / "a2a" / "reference-tasks.json"

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
def a2a_settings():
    """`override_settings` LAYERS, so these tests behave the same alone or in a full
    run. ROOT_URLCONF is conspicuously ABSENT and must stay absent - see rule 2.

    MESHY_ENVELOPE_NONCE_ENFORCE is ABSENT as well (divergence 036): it was pinned False
    here, so message:send never ran the nonce gate in any test. Every send below runs the
    shipped default, which is ON; the one block that turns it off is the explicit
    opt-out, and says so."""
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_SYNC_TIMEOUT_SECONDS=0.0,
        # ORDER INDEPENDENCE, added 2026-09-21. The routes under test open their
        # connection through apps.core.dbconn, whose dsn() reads settings.MESHY_DSN
        # first, and settings.py freezes that value from the environment when Django
        # loads. The `os.environ.setdefault("MESHY_DSN", DSN)` above reaches the setting
        # only if THIS module imports before Django is set up. Listed after any module
        # that configures Django, every route here ran against the shared database while
        # the fixtures seeded the per-run clone: 28 of 37 tests failed with 403. Overriding
        # the setting, not just the environment, is what makes this layer regardless of
        # who imported first, which is the property this fixture's docstring promises.
        MESHY_DSN=DSN,
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
    """`dashboard_users`, not `owners` - the FK target read off the live schema
    (`skill_calls_caller_owner_id_fkey REFERENCES dashboard_users(id)`). The live CHECK
    ties plane to trust group: a 'product' owner MUST have a NULL one."""
    owner_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'product',NULL)",
                    (owner_id, f"a2a-{tag}-{owner_id.hex[:8]}@example.test"))
    conn.commit()
    return owner_id


def _peer(conn, owner_id: uuid.UUID, tag: str) -> uuid.UUID:
    peer_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                    "VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"a2a-{tag}-{peer_id.hex[:8]}", tag))
    conn.commit()
    return peer_id


def _publish_key(conn, peer_id: uuid.UUID) -> tuple[bytes, str]:
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    conn.commit()
    return seed, kid


def _skill(conn, peer_id: uuid.UUID, name: str, *, mode: str = "sync") -> uuid.UUID:
    skill_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
            "input_schema, is_current) VALUES (%s,%s,%s,%s,%s,'grants_only','{}',TRUE)",
            (skill_id, peer_id, name, SKILL_VERSION, mode))
    conn.commit()
    return skill_id


def _grant(conn, *, grantor_owner, grantee_owner, granter_peer, skill_name) -> uuid.UUID:
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
    """Two SEPARATE product-plane tenants (so the grant gate is not vacuous) plus a
    THIRD, the `stranger`, who is a REAL active peer with a REAL token and is simply not
    a party to the call. A stranger who did not exist would be rejected by the auth
    extractor, and the 403 would prove nothing about the caller-or-target gate."""
    caller_owner = _owner(owner_conn, "caller")
    target_owner = _owner(owner_conn, "target")
    stranger_owner = _owner(owner_conn, "stranger")
    caller_peer = _peer(owner_conn, caller_owner, "caller")
    target_peer = _peer(owner_conn, target_owner, "target")
    stranger_peer = _peer(owner_conn, stranger_owner, "stranger")
    seed, kid = _publish_key(owner_conn, caller_peer)
    skill_id = _skill(owner_conn, target_peer, SKILL_NAME)
    return {
        "caller_owner": caller_owner, "target_owner": target_owner,
        "stranger_owner": stranger_owner, "caller_peer": caller_peer,
        "target_peer": target_peer, "stranger_peer": stranger_peer,
        "seed": seed, "kid": kid, "skill_id": skill_id,
    }


@pytest.fixture()
def granted(owner_conn, world):
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"],
           granter_peer=world["target_peer"], skill_name=SKILL_NAME)
    return world


def seed_call(conn, world, *, status: str, result=None) -> str:
    """One `skill_calls` row, written as the BOOTSTRAP role.

    Not written through the app: `sql_meshychat/001_write_side_ownership.sql` binds an
    INSERT to the peer the connection is bound to, so a fixture inserting through
    `meshychat_app` would have to bind a context and would then be testing the write path
    rather than seeding for the read path. The bootstrap role bypasses RLS, which is
    what a fixture wants: the row is unambiguously there.
    """
    call_id = f"call-{uuid.uuid4()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_calls (call_id, caller_peer_id, caller_owner_id, "
            "  target_peer_id, target_owner_id, skill_id, skill_name, skill_version, "
            "  mode, transport, status, args, result, deadline_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'sync','http',%s,'{}',%s, "
            "        NOW() + INTERVAL '1 hour')",
            (call_id, world["caller_peer"], world["caller_owner"], world["target_peer"],
             world["target_owner"], world["skill_id"], SKILL_NAME, SKILL_VERSION,
             status, None if result is None else json.dumps(result)))
    conn.commit()
    return call_id


def seed_chunk(conn, call_id: str, seq: int, content, *, chunk_type: str = "text",
               file_artifact_id=None) -> None:
    """`chunk_signature`/`chunk_signing_kid` are NOT NULL on the live table but the SSE
    projection never verifies them, so dummy values suffice."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_call_chunks (call_id, sequence, chunk_type, content, "
            "  file_artifact_id, chunk_signature, chunk_signing_kid) "
            "VALUES (%s,%s,%s,%s,%s,'sig','kid')",
            (call_id, seq, chunk_type, content, file_artifact_id))
    conn.commit()


# ----------------------------------------------------------------------- helpers

def mint(peer_id, *, aud="peer", ttl=300) -> str:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return pyjwt.encode(
        {"sub": str(peer_id), "aud": aud, "iss": ISSUER, "iat": now, "exp": now + ttl,
         "scopes": ["skills:invoke"]},
        JWT_PRIVATE_PEM, algorithm="EdDSA")


def bearer(peer_id) -> dict:
    return {"authorization": f"Bearer {mint(peer_id)}"}


def task_url(call_id: str) -> str:
    return f"/a2a/tasks/{call_id}"


def stream_url(call_id: str) -> str:
    return f"/a2a/tasks/{call_id}/stream"


def send_url(peer_id, skill_name: str = SKILL_NAME) -> str:
    return f"/a2a/agents/{peer_id}/skills/{skill_name}/message:send"


def body_of(response) -> dict | list:
    return json.loads(response.content.decode("utf-8"))


def sse_text(response) -> str:
    return b"".join(response.streaming_content).decode("utf-8")


def sign_call(world, *, args, call_id=None, target_peer=None, skill_name=SKILL_NAME,
              nonce=None):
    """Build and sign a `skill.call` envelope: the signature
    covers the JCS bytes of the payload and nothing else - `kind` INSIDE the object is
    the domain separator. A fresh nonce every time unless `nonce=` re-signs an old one,
    which only the replay tests do."""
    payload = {
        "v": 1,
        "kind": "skill.call",
        "call_id": call_id or str(uuid.uuid4()),
        "caller_peer_id": str(world["caller_peer"]),
        "target_peer_id": str(target_peer or world["target_peer"]),
        "skill_name": skill_name,
        "skill_version": SKILL_VERSION,
        "args": args,
        "args_attachments": [],
        "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "nonce": nonce or f"n-{uuid.uuid4()}",
    }
    signature = P.sign(world["seed"], P.jcs(payload))
    return payload, {"payload": payload, "signature": P.b64u(signature),
                     "signing_kid": world["kid"]}


def nonces_recorded(conn, peer_id, nonce: str) -> int:
    """Rows in the envelope nonce ledger for (peer, nonce), as the bootstrap role: the
    direct evidence that pipeline step 4c ran for a request."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM envelope_nonce_seen "
                    "WHERE peer_id = %s AND nonce = %s", (peer_id, nonce))
        (count,) = cur.fetchone()
    conn.commit()
    return count


def calls_with_id(conn, call_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_calls WHERE call_id = %s", (call_id,))
        (count,) = cur.fetchone()
    conn.commit()
    return count


def spy_on_nonce_refusals(monkeypatch) -> list[str]:
    """The reason each nonce refusal carried, through the function pipeline step 4c
    calls, passed through unchanged: the wire renders every NotAuthorized as one fixed
    403, so this is the only place the typed reason is visible."""
    reasons: list[str] = []
    real = nonce_ledger.to_rpc_error

    def spy(exc):
        err = real(exc)
        reasons.append(err.message)
        return err

    monkeypatch.setattr(nonce_ledger, "to_rpc_error", spy)
    return reasons


def a2a_message(*, parts, context_id=None) -> dict:
    msg = {"messageId": f"m-{uuid.uuid4()}", "role": types.ROLE_USER, "parts": parts}
    if context_id is not None:
        msg["contextId"] = context_id
    return msg


def post_send(client, world, *, message, envelope, call_id, as_peer=None,
              target=None, skill_name=SKILL_NAME):
    body = {"message": message, "envelope": envelope, "call_id": call_id}
    return client.post(
        send_url(target or world["target_peer"], skill_name),
        data=json.dumps(body), content_type="application/json",
        headers=bearer(as_peer or world["caller_peer"]))


@pytest.fixture(scope="session")
def vectors():
    """The conformance vectors, keyed by name. FAILS if the fixture is not present -
    silently skipping would turn the whole conformance section into a no-op that
    reports green."""
    assert REFERENCE_TASKS.is_file(), (
        f"the conformance oracle is missing: {REFERENCE_TASKS}. Every wire assertion "
        "in this file is derived from it; without it this suite proves nothing.")
    doc = json.loads(REFERENCE_TASKS.read_text(encoding="utf-8"))
    return {v["name"]: v for v in doc["vectors"]}


# ==========================================================================
# 0. the controls that make every other test in this file mean something
# ==========================================================================

def test_the_project_urlconf_is_what_these_tests_resolve_against(client):
    """No test here pins a URLconf, so this asserts the one in force IS the project's,
    and that all three A2A paths resolve to THIS app through it.

    Without this control a misconfigured run would produce a wall of 404s with no named
    cause - which is exactly how the previous round's routes shipped unmounted.
    """
    from django.conf import settings as live
    from django.urls import resolve

    assert live.ROOT_URLCONF == "meshychat.urls"

    peer = uuid.uuid4()
    expected = {
        "/a2a/tasks/abc": ("a2a-task-get", "apps.a2a.views"),
        "/a2a/tasks/abc/stream": ("a2a-task-stream", "apps.a2a.views"),
        f"/a2a/agents/{peer}/skills/some.skill/message:send":
            ("a2a-message-send", "apps.a2a.views"),
    }
    for path, (name, module) in expected.items():
        match = resolve(path)
        assert match.url_name == name, path
        assert match.func.__module__.startswith("apps.a2a") or \
            module in repr(match.func), f"{path} resolved outside apps.a2a: {match.func}"


def test_the_discovery_routes_are_served_by_peers_not_duplicated_here():
    """The agent-card discovery routes are NOT in this app, and that is the point.

    `apps/a2a/urls.py` deliberately declines to re-declare them; if a later edit adds a
    second handler for one of these paths, Django resolves to whichever `include()`
    comes first and the other silently never runs. This asserts the owner of each path
    is the peers/authapi app - a shadowing bug no route test would otherwise catch.
    """
    from django.urls import resolve

    peer = uuid.uuid4()
    owners = {
        f"/agents/{peer}/jwks.json": "apps.peers",
        f"/agents/{peer}/agent-card.json": "apps.peers",
        "/v1/peers/me/agent-card": "apps.peers",
        "/.well-known/jwks.json": "apps.authapi",
    }
    for path, owner in owners.items():
        func = resolve(path).func
        module = getattr(func, "__module__", "")
        assert module.startswith(owner), f"{path} is served by {module}, not {owner}"


# ==========================================================================
# 1. the projection types, against the conformance oracle
# ==========================================================================

def test_task_vectors_match_the_conformance_fixture_byte_for_byte(vectors):
    """The four `Task` vectors, each built through `task_from_meshy_call` from the call
    status that produces it, compared to the fixture as EXACT JSON.

    Compared as serialized bytes, not as parsed dicts: key ORDER is part of the wire
    and a dict comparison is order-blind, so it would
    pass on a body no byte of which matched.
    """
    cases = [
        ("task_working", ("call-1", "ctx", "running", None)),
        ("task_completed_with_result_artifact",
         ("call-9", None, "completed", {"ok": True})),
        ("task_input_required", ("call-5", "ctx", "input_required", None)),
        ("task_auth_required", ("call-6", "ctx", "auth_required", None)),
    ]
    for name, (call_id, ctx, status, result) in cases:
        built = types.task_from_meshy_call(call_id, ctx, status, result)
        expected = vectors[name]["json"]
        assert json.dumps(built, separators=(",", ":")) == \
            json.dumps(expected, separators=(",", ":")), name


def test_part_message_artifact_and_event_vectors_match_the_fixture(vectors):
    """The remaining seven vectors: the two Part shapes, a Message, an Artifact with
    extensions, both artifact-update chunks and the terminal status update."""
    built = {
        "part_text": types.part_text("hello"),
        "part_data": types.part_data({"k": 1}),
        "message_user_text": types.message(
            message_id="m1", context_id="c1", role=types.ROLE_USER,
            parts=[types.part_text("hi")]),
        "artifact_with_extensions": types.artifact(
            artifact_id="a1", parts=[types.part_text("x")],
            extensions=["https://ext.example/v1"]),
        "artifact_update_first_chunk": types.task_artifact_update_event(
            task_id="call-1",
            artifact_=types.artifact(artifact_id="call-1-stream",
                                     parts=[types.part_text("hello ")])),
        "artifact_update_append_chunk": types.task_artifact_update_event(
            task_id="call-1",
            artifact_=types.artifact(artifact_id="call-1-stream",
                                     parts=[types.part_text("world")]),
            append=True),
        "status_update_completed": types.task_status_update_event(
            task_id="call-1",
            status=types.task_status(state=types.TASK_STATE_COMPLETED)),
    }
    for name, value in built.items():
        assert json.dumps(value, separators=(",", ":")) == \
            json.dumps(vectors[name]["json"], separators=(",", ":")), name


def test_every_fixture_vector_is_covered_by_the_two_tests_above(vectors):
    """A coverage control. The two tests above name their vectors by hand, so a vector
    ADDED to the fixture would otherwise be silently untested and this suite
    would keep reporting green against an oracle it no longer fully checks."""
    covered = {
        "task_working", "task_completed_with_result_artifact", "task_input_required",
        "task_auth_required", "message_user_text", "part_text", "part_data",
        "artifact_with_extensions", "artifact_update_first_chunk",
        "artifact_update_append_chunk", "status_update_completed",
    }
    assert set(vectors) == covered, (
        "the conformance fixture changed; the vectors above must be updated to match: "
        f"missing={sorted(set(vectors) - covered)} stale={sorted(covered - set(vectors))}")


_STATUS_LITERAL = re.compile(r"'([a-z_]+)'::text")


def live_call_statuses(conn) -> tuple[str, ...]:
    """The status vocabulary the LIVE `skill_calls_status_check` admits, read off
    `pg_constraint` in the order the CHECK lists it. RAISES when the constraint is absent
    or yields nothing, so an empty vocabulary can never make the loops below vacuous."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'skill_calls'::regclass "
                    "  AND conname = 'skill_calls_status_check'")
        rows = cur.fetchall()
    conn.commit()
    assert len(rows) == 1, f"skill_calls_status_check: {len(rows)} rows"
    found = tuple(_STATUS_LITERAL.findall(rows[0][0]))
    assert found, f"no status literal parsed from {rows[0][0]!r}"
    return found


def test_the_status_vocabulary_reader_parses_a_check_definition():
    """The parser's own positive and negative control, on definitions whose answer is
    known: a two-literal CHECK yields both in order, and a definition with no `::text`
    literal yields nothing (which `live_call_statuses` then refuses)."""
    assert _STATUS_LITERAL.findall(
        "CHECK ((status = ANY (ARRAY['pending'::text, 'input_required'::text])))") == [
        "pending", "input_required"]
    assert _STATUS_LITERAL.findall("CHECK ((status IS NOT NULL))") == []


def test_every_live_call_status_projects_and_unknown_ones_raise(owner_conn):
    """POSITIVE CONTROL AND NEGATIVE IN ONE. The control is that every status the LIVE
    `skill_calls_status_check` CHECK admits has a projection; the negative is that an
    unknown one RAISES rather than defaulting to `TASK_STATE_UNSPECIFIED`.

    STATUS C72 (`:451` in the review's numbering): this used to loop over
    `reads.CALL_STATUSES` while saying it read `pg_constraint`. That is a tuple typed in
    `apps/calls/reads.py`, so a status a migration added to the table was never looped
    over. The vocabulary is now READ OFF THE DATABASE (`live_call_statuses`), and the
    hand-typed tuple is held equal to it, so it cannot drift silently either.

    A default would be the dangerous direction: a new call state would render as a
    valid-looking A2A task instead of failing, which is the exact drift this test catches:
    an unmapped status fails here instead of rendering as a task.
    """
    live = live_call_statuses(owner_conn)
    assert set(live) == set(reads.CALL_STATUSES), (
        f"apps/calls/reads.CALL_STATUSES {sorted(reads.CALL_STATUSES)} is not the live "
        f"CHECK {sorted(live)}")
    for status in live:
        assert types.task_state_for(status).startswith("TASK_STATE_")
    with pytest.raises(types.UnknownCallStatus):
        types.task_state_for("elicit_pending")


def test_terminality_matches_the_known_table(owner_conn):
    """Terminality is checked for every live call status, including the two interactive-pause
    states TASK-28 added.

    STATUS C72: the table below was compared to `reads.CALL_STATUSES`, one
    hand-typed literal against another, while claiming to track the live vocabulary. It
    is compared to the LIVE CHECK now."""
    expected = {
        "pending": False, "routed": False, "queued": False, "running": False,
        "input_required": False, "auth_required": False,
        "completed": True, "failed": True, "timeout": True, "abandoned": True,
    }
    assert set(expected) == set(live_call_statuses(owner_conn)), \
        "the live status vocabulary changed; this table must change with it"
    for status, terminal in expected.items():
        assert types.is_terminal(types.task_state_for(status)) is terminal, status


def test_v1_dropped_fields_are_absent_while_their_siblings_are_present():
    """Three v1.0 REMOVALS, each asserted next to a field that IS present.

    An absence assertion alone passes on an empty object, so each `not in` below is
    paired with an `in` on the same body - the pairing is what distinguishes "this
    field was correctly dropped" from "nothing was serialized at all".
    """
    task = types.task_from_meshy_call("c", "ctx", "running", None)
    assert "kind" not in task and "id" in task          # A.2.1 removed the discriminator
    assert "kind" not in task["status"] and "state" in task["status"]

    ev = types.task_status_update_event(
        task_id="c", status=types.task_status(state=types.TASK_STATE_COMPLETED))
    assert "final" not in ev and "status" in ev          # v1.0 dropped `final`

    first = types.task_artifact_update_event(
        task_id="c", artifact_=types.artifact(artifact_id="a", parts=[]))
    later = types.task_artifact_update_event(
        task_id="c", artifact_=types.artifact(artifact_id="a", parts=[]), append=True)
    # proto3 omits default-valued scalars: false is ABSENT, true is present.
    assert "append" not in first and "taskId" in first
    assert later["append"] is True


def test_single_data_args_needs_exactly_one_data_part():
    """The control is the ACCEPTED one-data-part message; the three rejections differ from
    it in exactly one property each. `{"data": null}` is in the set on purpose: an explicit
    null becomes `None`, i.e. no data part at all, so an implementation
    that tested `"data" in part` instead of `is not None` would accept it.
    """
    ok = a2a_message(parts=[types.part_data({"q": "hi"})])
    assert types.single_data_args(ok) == {"q": "hi"}

    for bad in ([types.part_text("hi")],
                [types.part_data({"a": 1}), types.part_data({"b": 2})],
                [],
                [{"data": None}]):
        with pytest.raises(types.NoSingleDataPart):
            types.single_data_args(a2a_message(parts=bad))


def test_chunk_projection_covers_the_three_live_chunk_types():
    """`chunk_to_artifact_event` per ChunkType, plus the missing-content case and an unknown
    type raising rather than emitting an empty part."""
    def ev(chunk_type, content, file_id=None, append=False):
        return types.chunk_to_artifact_event(
            "call-1", {"chunk_type": chunk_type, "content": content,
                       "file_artifact_id": file_id, "sequence": 1}, append)

    text = ev("text", b"hello")
    assert text["artifact"]["artifactId"] == "call-1-stream"
    assert text["artifact"]["parts"][0] == {"text": "hello"}
    assert "append" not in text and "lastChunk" not in text

    assert ev("json", b'{"ok":true}', append=True)["artifact"]["parts"][0] == \
        {"data": {"ok": True}}
    assert ev("json", b"not json")["artifact"]["parts"][0] == {"data": None}

    fid = uuid.uuid4()
    assert ev("file_ref", None, fid)["artifact"]["parts"][0] == \
        {"data": {"fileArtifactId": str(fid)}}

    # An absent text content is `{"text": ""}`, NOT an absent part.
    assert ev("text", None)["artifact"]["parts"][0] == {"text": ""}

    with pytest.raises(types.UnknownChunkType):
        ev("protobuf", b"\x00")


# ==========================================================================
# 2. GET /a2a/tasks/{call_id}
# ==========================================================================

def test_completed_call_is_served_as_an_a2a_task_with_its_result_artifact(
        client, owner_conn, world):
    call_id = seed_call(owner_conn, world, status="completed", result={"echo": "hi"})
    response = client.get(task_url(call_id), headers=bearer(world["caller_peer"]))
    assert response.status_code == 200, response.content
    task = body_of(response)
    assert task["id"] == call_id
    assert task["status"]["state"] == types.TASK_STATE_COMPLETED
    assert task["artifacts"][0]["artifactId"] == f"{call_id}-result"
    assert task["artifacts"][0]["parts"][0]["data"] == {"echo": "hi"}
    assert task["artifacts"][0]["name"] == "result"
    # v1.0 has no discriminator - asserted beside a key that IS there.
    assert "kind" not in task and "status" in task
    # This route passes context_id=None, so `contextId` is absent.
    assert "contextId" not in task


def test_the_target_peer_can_read_the_task_too(client, owner_conn, world):
    """`assert_peer_can_see` is caller OR target. Testing only the caller would leave an
    implementation that keys on `caller_peer_id` alone passing every other test here."""
    call_id = seed_call(owner_conn, world, status="completed", result={"echo": "hi"})
    response = client.get(task_url(call_id), headers=bearer(world["target_peer"]))
    assert response.status_code == 200, response.content
    assert body_of(response)["id"] == call_id


def test_every_non_completed_state_maps_and_carries_no_result_artifact(
        client, owner_conn, world):
    """The DB-string -> CallStatus -> SCREAMING_SNAKE path through the SERVED route, for
    every status in the live vocabulary.

    The result is seeded NON-NULL on every one of them, which is what makes the
    "no artifact" assertion mean something: the artifact rule is a CONJUNCTION (state is
    COMPLETED **and** result is present), and an implementation that keyed on the result alone
    would attach an artifact to a failed call. The completed row in the same test is
    the positive control that proves the artifact CAN appear with this exact result.
    """
    for status in reads.CALL_STATUSES:
        call_id = seed_call(owner_conn, world, status=status, result={"echo": "hi"})
        response = client.get(task_url(call_id), headers=bearer(world["caller_peer"]))
        assert response.status_code == 200, (status, response.content)
        task = body_of(response)
        assert task["status"]["state"] == types.task_state_for(status), status
        if status == "completed":
            assert task["artifacts"][0]["parts"][0]["data"] == {"echo": "hi"}
        else:
            assert "artifacts" not in task, status


def test_unknown_call_id_is_404_and_a_known_one_is_200(client, owner_conn, world):
    """POSITIVE CONTROL IN THE SAME TEST: the two requests differ only in the call_id,
    carry the same token and hit the same route, so the 404 is attributable to the id
    and not to auth, wiring or a dead fixture."""
    known = seed_call(owner_conn, world, status="completed", result={})
    headers = bearer(world["caller_peer"])

    control = client.get(task_url(known), headers=headers)
    assert control.status_code == 200, control.content

    missing = client.get(task_url("no-such-call-id"), headers=headers)
    assert missing.status_code == 404, missing.content
    assert body_of(missing)["error"] == "E_NOT_FOUND"


def test_a_non_party_peer_is_forbidden_while_the_caller_is_not(
        client, owner_conn, world):
    """The stranger is a REAL, ACTIVE peer with a REAL token; the only difference from
    the control is that they are neither caller nor target of THIS call. Without the
    control, an unseeded peer or a broken key would produce the same 403."""
    call_id = seed_call(owner_conn, world, status="completed", result={})

    control = client.get(task_url(call_id), headers=bearer(world["caller_peer"]))
    assert control.status_code == 200, control.content

    denied = client.get(task_url(call_id), headers=bearer(world["stranger_peer"]))
    assert denied.status_code == 403, denied.content
    assert body_of(denied)["error"] == "E_FORBIDDEN"


def test_404_and_403_are_not_collapsed(client, owner_conn, world):
    """The two rejections are DIFFERENT codes on this surface and a client may rely on
    that. Asserted together so a future "privacy tidy-up" that maps both to 404 fails
    here rather than silently changing the contract."""
    call_id = seed_call(owner_conn, world, status="completed", result={})
    assert client.get(task_url(call_id),
                      headers=bearer(world["stranger_peer"])).status_code == 403
    assert client.get(task_url("nope"),
                      headers=bearer(world["stranger_peer"])).status_code == 404


def test_the_task_route_needs_a_token(client, owner_conn, world):
    """Control: the same URL with a valid bearer is 200."""
    call_id = seed_call(owner_conn, world, status="completed", result={})
    assert client.get(task_url(call_id),
                      headers=bearer(world["caller_peer"])).status_code == 200
    assert client.get(task_url(call_id)).status_code == 401


def test_the_task_route_rejects_a_non_get_method(client, owner_conn, world):
    call_id = seed_call(owner_conn, world, status="completed", result={})
    headers = bearer(world["caller_peer"])
    assert client.get(task_url(call_id), headers=headers).status_code == 200
    bad = client.post(task_url(call_id), data="{}", content_type="application/json",
                      headers=headers)
    assert bad.status_code == 405
    assert bad.headers["Allow"] == "GET"


# ==========================================================================
# 3. GET /a2a/tasks/{call_id}/stream
# ==========================================================================

def test_stream_drains_persisted_chunks_then_emits_a_terminal_status(
        client, owner_conn, world):
    """A TERMINAL call drains its buffer and closes, so the body is finite and the
    assertion is deterministic - no concurrency in this test.

    The event-type name appears once per SSE frame (on the `event:` line) and never
    inside the JSON payloads, so counting the name counts the frames.
    """
    call_id = seed_call(owner_conn, world, status="completed", result={"echo": "hi"})
    seed_chunk(owner_conn, call_id, 1, b"hello ")
    seed_chunk(owner_conn, call_id, 2, b"world")

    response = client.get(stream_url(call_id), headers=bearer(world["caller_peer"]))
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["Content-Type"]
    assert response.headers["Cache-Control"] == "no-cache"

    body = sse_text(response)
    assert body.count("event: TaskArtifactUpdateEvent") == 2, body
    assert body.count("event: TaskStatusUpdateEvent") == 1, body
    assert "hello " in body and "world" in body, body
    # The first artifact is NEW (append omitted per proto3); the second appends to it.
    assert body.count('"append":true') == 1, body
    assert types.TASK_STATE_COMPLETED in body, body
    # The frames are the LAST thing in the body and the stream really ended.
    assert body.endswith("\n\n"), repr(body[-40:])


def test_a_stream_with_no_chunks_still_closes_with_one_status_event(
        client, owner_conn, world):
    """The positive control for the counting in the test above: with the SAME route and
    the SAME terminal call but zero chunks, the artifact count is zero and the status
    count is still one. If the counter were broken, both tests could not pass."""
    call_id = seed_call(owner_conn, world, status="failed", result=None)
    body = sse_text(client.get(stream_url(call_id),
                               headers=bearer(world["caller_peer"])))
    assert body.count("event: TaskArtifactUpdateEvent") == 0, body
    assert body.count("event: TaskStatusUpdateEvent") == 1, body
    assert types.TASK_STATE_FAILED in body, body


def test_the_closing_status_frame_has_no_final_field(client, owner_conn, world):
    """v1.0 dropped `final`; terminality is read from `status.state`. Asserted on the
    PARSED frame beside the `status` field that IS present, so an empty frame cannot
    pass."""
    call_id = seed_call(owner_conn, world, status="completed", result={})
    body = sse_text(client.get(stream_url(call_id),
                               headers=bearer(world["caller_peer"])))
    frames = [line[len("data: "):] for line in body.splitlines()
              if line.startswith("data: ")]
    assert len(frames) == 1, body
    event = json.loads(frames[0])
    assert event["taskId"] == call_id
    assert event["status"]["state"] == types.TASK_STATE_COMPLETED
    assert "final" not in event


def test_stream_is_forbidden_for_a_non_party_peer_and_open_for_the_caller(
        client, owner_conn, world):
    """The 403 must be a JSON body with a 403 STATUS, not a 200 stream that says
    nothing - which is what an authorization check placed inside the generator would
    produce, because the status line is sent before the generator first runs."""
    call_id = seed_call(owner_conn, world, status="completed", result={})

    control = client.get(stream_url(call_id), headers=bearer(world["caller_peer"]))
    assert control.status_code == 200
    assert "text/event-stream" in control.headers["Content-Type"]
    sse_text(control)   # drain, so the generator's connection is closed

    denied = client.get(stream_url(call_id), headers=bearer(world["stranger_peer"]))
    assert denied.status_code == 403, denied
    assert "application/json" in denied.headers["Content-Type"]
    assert body_of(denied)["error"] == "E_FORBIDDEN"


def test_stream_404s_an_unknown_call_id_with_a_known_one_as_control(
        client, owner_conn, world):
    known = seed_call(owner_conn, world, status="completed", result={})
    headers = bearer(world["caller_peer"])
    control = client.get(stream_url(known), headers=headers)
    assert control.status_code == 200
    sse_text(control)
    missing = client.get(stream_url("no-such-call-id"), headers=headers)
    assert missing.status_code == 404, missing
    assert body_of(missing)["error"] == "E_NOT_FOUND"


def test_stream_tails_live_chunks_and_closes_on_the_terminal_transition(
        client, owner_conn, world):
    """The LIVE path: chunks arriving WHILE the stream is open, then the call going
    terminal mid-stream. This is the only test that exercises the multi-poll tail and
    the FINAL DRAIN BEFORE CLOSE - the replay test above is all-pre-persisted and
    returns on its first poll, so it never re-drains.

    The COUNTS are timing-independent: every chunk and the terminal status drain
    eventually, however the polls interleave. The producer writes the last chunk 100ms
    before the terminal UPDATE precisely to land inside the window the final drain
    exists to cover.
    """
    call_id = seed_call(owner_conn, world, status="running", result=None)

    def produce():
        import time as _t
        with psycopg.connect(DSN) as conn:
            _t.sleep(0.4)
            seed_chunk(conn, call_id, 1, b"alpha ")
            _t.sleep(0.3)
            seed_chunk(conn, call_id, 2, b"omega")
            _t.sleep(0.1)
            with conn.cursor() as cur:
                cur.execute("UPDATE skill_calls SET status='completed', "
                            "completed_at=NOW() WHERE call_id=%s", (call_id,))
            conn.commit()

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    try:
        response = client.get(stream_url(call_id), headers=bearer(world["caller_peer"]))
        assert response.status_code == 200
        body = sse_text(response)
    finally:
        producer.join(timeout=30)

    assert body.count("event: TaskArtifactUpdateEvent") == 2, body
    assert "alpha " in body, body
    assert "omega" in body, body          # the tail is not dropped on close
    assert body.count("event: TaskStatusUpdateEvent") == 1, body
    assert types.TASK_STATE_COMPLETED in body, body


def test_stream_projects_json_and_file_ref_chunks(client, owner_conn, world):
    """The three ChunkType branches through the SERVED route, not just the unit test.
    A text chunk is included as the control: it is the branch every other stream test
    already exercises, so if all three parse here the projection is wired, not lucky."""
    file_id = uuid.uuid4()
    call_id = seed_call(owner_conn, world, status="completed", result={})
    seed_chunk(owner_conn, call_id, 1, b"plain")
    seed_chunk(owner_conn, call_id, 2, b'{"ok":true}', chunk_type="json")
    with owner_conn.cursor() as cur:
        # Columns read off information_schema, not guessed: the live table has
        # `r2_key` / `content_type` (NOT NULL), not `storage_key` / `mime_type`, and
        # `status` is CHECK-constrained to pending|ready|expired|failed.
        cur.execute("INSERT INTO file_artifacts (id, owner_id, r2_key, sha256, "
                    "size_bytes, content_type, status) "
                    "VALUES (%s,%s,%s,%s,1,'text/plain','ready')",
                    (file_id, world["caller_owner"], f"k/{file_id}", "0" * 64))
    owner_conn.commit()
    seed_chunk(owner_conn, call_id, 3, None, chunk_type="file_ref",
               file_artifact_id=file_id)

    body = sse_text(client.get(stream_url(call_id),
                               headers=bearer(world["caller_peer"])))
    parts = [json.loads(line[len("data: "):])["artifact"]["parts"][0]
             for line in body.splitlines()
             if line.startswith("data: ") and "artifact" in line]
    assert parts == [{"text": "plain"},
                     {"data": {"ok": True}},
                     {"data": {"fileArtifactId": str(file_id)}}], body


def _sse_frames(body: str) -> list[dict]:
    """The parsed `data:` payloads, in order. Parsed rather than substring-matched so a
    truncated or half-written frame cannot pass as a well-formed one."""
    return [json.loads(line[len("data: "):]) for line in body.splitlines()
            if line.startswith("data: ")]


def test_a_failed_read_inside_the_generator_closes_the_stream_as_failed(
        client, owner_conn, world, monkeypatch, caplog):
    """A `200 text/event-stream` IS ALREADY ON THE WIRE when the generator first runs.

    The status line and the headers are flushed when the view returns; the poll loop
    only starts afterwards. An exception there therefore cannot become a 5xx - there is
    no status left to change - and if it propagates it reaches the WSGI server, which
    drops the connection mid-body. An SSE consumer cannot distinguish that drop from a
    stream that finished, so the failure is INVISIBLE to it.

    The generator catches an exception at the stream boundary, logs it, and closes the stream
    with a terminal `TaskStatusUpdateEvent` carrying `TASK_STATE_FAILED`; that is what is
    asserted here.

    THE CONTROL IS THE SAME CALL OVER THE SAME ROUTE, un-patched, in the same test: it
    streams its chunk and closes `completed`. The only delta between the two requests is
    the failing read, so the FAILED close cannot be an artefact of a broken fixture.
    """
    call_id = seed_call(owner_conn, world, status="completed", result={})
    seed_chunk(owner_conn, call_id, 1, b"hello")

    control = sse_text(client.get(stream_url(call_id),
                                  headers=bearer(world["caller_peer"])))
    assert control.count("event: TaskArtifactUpdateEvent") == 1, control
    assert _sse_frames(control)[-1]["status"]["state"] == types.TASK_STATE_COMPLETED

    def explode(conn, cid, since_seq, **kwargs):
        # `**kwargs`: the stream passes its page size as `limit=` (STATUS C73). Without
        # it this stub would raise a TypeError of its own and the FAILED close below
        # would be about the stub, not the read.
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr("apps.calls.reads.list_chunks", explode)

    with caplog.at_level("ERROR", logger="meshychat.a2a"):
        response = client.get(stream_url(call_id), headers=bearer(world["caller_peer"]))
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["Content-Type"]
        # PRE-FIX THIS LINE RAISES psycopg.OperationalError: the generator is consumed
        # here, which is exactly where the WSGI server would consume it.
        body = sse_text(response)

    frames = _sse_frames(body)
    assert body.count("event: TaskArtifactUpdateEvent") == 0, body
    assert body.count("event: TaskStatusUpdateEvent") == 1, body
    assert len(frames) == 1, body
    assert frames[0]["taskId"] == call_id
    assert frames[0]["status"]["state"] == types.TASK_STATE_FAILED, body
    # A CLEAN close: the last frame is whole, not cut off mid-write.
    assert body.endswith("\n\n"), repr(body[-40:])

    # NOT A SWALLOW. The cause reaches the log with its type, its message and the call
    # it belongs to - a generator that hid a psycopg failure would be worse than one
    # that crashed loudly.
    assert "OperationalError" in caplog.text, caplog.text
    assert "server closed the connection unexpectedly" in caplog.text, caplog.text
    assert call_id in caplog.text, caplog.text


def test_an_unprojectable_chunk_keeps_the_frames_already_emitted_and_then_closes(
        client, owner_conn, world, monkeypatch, caplog):
    """The OTHER arm: the failure is in the PROJECTION, not the read, and it happens
    after frames have already gone out.

    `types.UnknownChunkType` raises in `types.py:part_for_chunk`, which is the deliberate
    choice recorded on `UnknownCallStatus`: fail when the vocabulary
    drifts rather than render the drift as a valid-looking task. That is only true if the
    failure is CONTAINED; uncontained it is a truncated stream and no message anywhere.

    A bad `chunk_type` cannot be seeded - the live CHECK permits exactly
    text|json|file_ref - so the drift is injected at the projection instead, which is the
    function the CHECK is the only thing guarding.
    """
    call_id = seed_call(owner_conn, world, status="completed", result={})
    seed_chunk(owner_conn, call_id, 1, b"first")
    seed_chunk(owner_conn, call_id, 2, b"second")

    control = sse_text(client.get(stream_url(call_id),
                                  headers=bearer(world["caller_peer"])))
    assert control.count("event: TaskArtifactUpdateEvent") == 2, control
    assert _sse_frames(control)[-1]["status"]["state"] == types.TASK_STATE_COMPLETED

    real_part_for_chunk = types.part_for_chunk

    def drifted(chunk_type, content, file_artifact_id):
        if bytes(content or b"") == b"first":
            return real_part_for_chunk(chunk_type, content, file_artifact_id)
        raise types.UnknownChunkType("no A2A Part projection for chunk_type 'sentinel'")

    monkeypatch.setattr("apps.a2a.types.part_for_chunk", drifted)

    with caplog.at_level("ERROR", logger="meshychat.a2a"):
        response = client.get(stream_url(call_id), headers=bearer(world["caller_peer"]))
        assert response.status_code == 200
        body = sse_text(response)   # PRE-FIX: raises types.UnknownChunkType

    frames = _sse_frames(body)
    # The frame that WAS projectable is still delivered - the guard closes the stream,
    # it does not discard what the consumer has already been sent.
    assert body.count("event: TaskArtifactUpdateEvent") == 1, body
    assert frames[0]["artifact"]["parts"] == [{"text": "first"}], body
    assert frames[-1]["status"]["state"] == types.TASK_STATE_FAILED, body
    assert body.count("event: TaskStatusUpdateEvent") == 1, body
    assert body.endswith("\n\n"), repr(body[-40:])
    assert "UnknownChunkType" in caplog.text, caplog.text
    assert "sentinel" in caplog.text, caplog.text


# ==========================================================================
# 4. POST /a2a/agents/{peer_id}/skills/{skill_name}/message:send
# ==========================================================================

def test_message_send_starts_a_call_and_returns_its_a2a_task(
        client, owner_conn, granted):
    """The A2A round trip: args -> one data Part -> the SAME `start_call` seam -> Task.

    The row is read back as the BOOTSTRAP role so "the call exists" is a fact about the
    database rather than about whatever context the app last bound.
    """
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    message = a2a_message(parts=[types.part_data(args)], context_id="ctx-42")

    response = post_send(client, granted, message=message, envelope=envelope,
                         call_id=payload["call_id"])
    assert response.status_code == 200, response.content
    task = body_of(response)
    assert task["id"] == payload["call_id"]
    # contextId RIDES THROUGH from the A2A message on this route; the read route passes None.
    assert task["contextId"] == "ctx-42"
    # Step 9 (divergence 041 S5): this target is not the memory authority and holds no
    # socket, so the call is QUEUED for it until its deadline, and the Task reports that
    # in-flight state as SUBMITTED (types.py maps pending, routed and queued there).
    assert task["status"]["state"] == types.TASK_STATE_SUBMITTED
    assert "kind" not in task

    with owner_conn.cursor() as cur:
        cur.execute("SELECT caller_peer_id, target_peer_id, skill_name, args "
                    "FROM skill_calls WHERE call_id = %s", (payload["call_id"],))
        row = cur.fetchone()
    owner_conn.commit()
    assert row is not None, "the A2A route did not reach the pipeline"
    # AC#6: the caller is the AUTHENTICATED CHANNEL identity and the target is the
    # ROUTE PATH - never the message body.
    assert row[0] == granted["caller_peer"]
    assert row[1] == granted["target_peer"]
    assert row[2] == SKILL_NAME
    # `skill_calls.args` holds the WHOLE SIGNED PAYLOAD, not `payload["args"]`
    # (apps/calls/repository.py:142-146 - idempotency re-canonicalizes this column
    # against the envelope bytes). The A2A data part is the nested `args`.
    assert row[3]["args"] == args
    assert row[3]["kind"] == "skill.call"


def test_the_task_the_send_returns_is_readable_on_the_get_route(
        client, owner_conn, granted):
    """The two routes agree about the same call. This is what makes the shim a shim:
    a client that sends and then polls must see one task, not two views of two calls."""
    args = {"text": "poll me"}
    payload, envelope = sign_call(granted, args=args)
    sent = post_send(client, granted, message=a2a_message(parts=[types.part_data(args)]),
                     envelope=envelope, call_id=payload["call_id"])
    assert sent.status_code == 200, sent.content

    polled = client.get(task_url(payload["call_id"]),
                        headers=bearer(granted["caller_peer"]))
    assert polled.status_code == 200, polled.content
    assert body_of(polled)["id"] == body_of(sent)["id"]
    assert body_of(polled)["status"]["state"] == body_of(sent)["status"]["state"]


def test_message_send_rejects_a_message_that_is_not_exactly_one_data_part(
        client, granted):
    """AC#4. Three variants, each differing from the ACCEPTED control in exactly one
    property: the number of data parts. Every request carries the same valid, signed
    envelope, so a 400 is attributable to the message shape and nothing else."""
    args = {"text": "hello"}

    payload, envelope = sign_call(granted, args=args)
    control = post_send(client, granted,
                        message=a2a_message(parts=[types.part_data(args)]),
                        envelope=envelope, call_id=payload["call_id"])
    assert control.status_code == 200, control.content

    for parts in ([types.part_text("hello")],
                  [types.part_data(args), types.part_data(args)],
                  []):
        payload, envelope = sign_call(granted, args=args)
        bad = post_send(client, granted, message=a2a_message(parts=parts),
                        envelope=envelope, call_id=payload["call_id"])
        assert bad.status_code == 400, (parts, bad.content)
        assert body_of(bad)["error"] == "E_VALIDATION"


def test_message_send_rejects_a_data_part_that_is_not_the_signed_args(client, granted):
    """THE BINDING THAT MAKES THE A2A PAYLOAD NON-DECORATIVE. Control and variant share
    the same signed envelope; only the data Part differs. Without this check a caller
    could sign one call and present a different one in A2A clothing.

    The `{}` case is here on purpose: an implementation comparing with `or`/truthiness rather than
    equality would accept an empty object against non-empty signed args.
    """
    args = {"text": "hello"}

    payload, envelope = sign_call(granted, args=args)
    control = post_send(client, granted,
                        message=a2a_message(parts=[types.part_data(args)]),
                        envelope=envelope, call_id=payload["call_id"])
    assert control.status_code == 200, control.content

    for wrong in ({"text": "goodbye"}, {}, {"text": "hello", "extra": 1}):
        payload, envelope = sign_call(granted, args=args)
        bad = post_send(client, granted,
                        message=a2a_message(parts=[types.part_data(wrong)]),
                        envelope=envelope, call_id=payload["call_id"])
        assert bad.status_code == 400, (wrong, bad.content)
        assert body_of(bad)["error"] == "E_VALIDATION"
        assert "does not match the signed envelope args" in body_of(bad)["message"]


def test_message_send_is_forbidden_without_a_grant(client, owner_conn, world):
    """The AUTHORIZE seam is reached, not bypassed. `world` (no grant) vs `granted`
    differ in exactly one row in `skill_grants`, so the 403 names the grant.

    This is the assertion that says the shim rides the SAME `authorize()` seam as the
    native route rather than being a parallel unguarded door.
    """
    args = {"text": "hello"}
    payload, envelope = sign_call(world, args=args)
    denied = post_send(client, world, message=a2a_message(parts=[types.part_data(args)]),
                       envelope=envelope, call_id=payload["call_id"])
    assert denied.status_code == 403, denied.content
    assert body_of(denied)["error"] == "E_FORBIDDEN"

    # POSITIVE CONTROL: add the one missing row and the identical request is accepted.
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"],
           granter_peer=world["target_peer"], skill_name=SKILL_NAME)
    payload, envelope = sign_call(world, args=args)
    allowed = post_send(client, world,
                        message=a2a_message(parts=[types.part_data(args)]),
                        envelope=envelope, call_id=payload["call_id"])
    assert allowed.status_code == 200, allowed.content


def test_message_send_refuses_a_byte_identical_envelope_the_second_time_it_reaches_the_gate(
        client, owner_conn, world, monkeypatch):
    """Envelope replay on the A2A route, with the default in force (divergence 036).

    The first send is refused at step 5 (no grant) AFTER step 4c recorded its nonce, so
    no call row exists and idempotency cannot answer a resend. The grant then appears.
    Three requests make the delta: a FRESH envelope is accepted (the grant works), the
    byte-identical resend - same message, same envelope, same call_id - is refused 403
    E_FORBIDDEN carrying NotAuthorized("envelope replay"), and the same resend under the
    explicit opt-out is accepted, so the refusal is the nonce gate and nothing else.
    """
    reasons = spy_on_nonce_refusals(monkeypatch)
    args = {"text": "hello"}
    payload, envelope = sign_call(world, args=args)
    message = a2a_message(parts=[types.part_data(args)])

    first = post_send(client, world, message=message, envelope=envelope,
                      call_id=payload["call_id"])
    assert first.status_code == 403, first.content
    assert calls_with_id(owner_conn, payload["call_id"]) == 0
    assert nonces_recorded(owner_conn, world["caller_peer"], payload["nonce"]) == 1, (
        "the shipped default did not run the nonce gate on this send")
    assert reasons == [], "the first send was refused by the grant, not the gate"

    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"],
           granter_peer=world["target_peer"], skill_name=SKILL_NAME)

    # POSITIVE CONTROL: a fresh envelope, same caller, skill and grant.
    fresh_payload, fresh = sign_call(world, args=args)
    control = post_send(client, world, message=a2a_message(parts=[types.part_data(args)]),
                        envelope=fresh, call_id=fresh_payload["call_id"])
    assert control.status_code == 200, control.content

    replay = post_send(client, world, message=message, envelope=envelope,
                       call_id=payload["call_id"])
    assert replay.status_code == 403, replay.content
    assert body_of(replay) == {"error": "E_FORBIDDEN", "message": "forbidden"}
    assert reasons == ["envelope replay"]
    assert calls_with_id(owner_conn, payload["call_id"]) == 0, (
        "a refused replay started a call")

    # THE DELTA: the explicit opt-out, and nothing else changed.
    with override_settings(MESHY_ENVELOPE_NONCE_ENFORCE=False):
        opted_out = post_send(client, world, message=message, envelope=envelope,
                              call_id=payload["call_id"])
    assert opted_out.status_code == 200, opted_out.content
    assert calls_with_id(owner_conn, payload["call_id"]) == 1


def test_message_send_refuses_an_old_nonce_re_signed_under_a_fresh_call_id(
        client, owner_conn, granted, monkeypatch):
    """Gate 8's other half on this route: a reused nonce under a NEW call_id reaches the
    ledger because idempotency keys on call_id. Control: a fresh nonce is accepted."""
    reasons = spy_on_nonce_refusals(monkeypatch)
    args = {"text": "hello"}
    nonce = f"replay-{uuid.uuid4()}"
    first_payload, first = sign_call(granted, args=args, nonce=nonce)
    accepted = post_send(client, granted,
                         message=a2a_message(parts=[types.part_data(args)]),
                         envelope=first, call_id=first_payload["call_id"])
    assert accepted.status_code == 200, accepted.content

    reused_payload, reused = sign_call(granted, args=args, nonce=nonce)
    assert reused_payload["call_id"] != first_payload["call_id"]
    refused = post_send(client, granted,
                        message=a2a_message(parts=[types.part_data(args)]),
                        envelope=reused, call_id=reused_payload["call_id"])
    assert refused.status_code == 403, refused.content
    assert body_of(refused) == {"error": "E_FORBIDDEN", "message": "forbidden"}
    assert reasons == ["envelope replay"]
    assert calls_with_id(owner_conn, reused_payload["call_id"]) == 0

    fresh_payload, fresh = sign_call(granted, args=args)
    control = post_send(client, granted,
                        message=a2a_message(parts=[types.part_data(args)]),
                        envelope=fresh, call_id=fresh_payload["call_id"])
    assert control.status_code == 200, control.content


def test_message_send_resent_after_its_call_exists_returns_that_call_with_the_gate_on(
        client, owner_conn, granted, monkeypatch):
    """The retry a client makes when a response was lost: the same message and envelope
    again. Step 2 answers it from the call the first send created, before step 4c, so it
    is the same Task and not a replay refusal, and nothing runs twice. The ledger row
    proves the gate ran on the first send (a gate that never ran passes this too)."""
    reasons = spy_on_nonce_refusals(monkeypatch)
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    message = a2a_message(parts=[types.part_data(args)])
    first = post_send(client, granted, message=message, envelope=envelope,
                      call_id=payload["call_id"])
    assert first.status_code == 200, first.content
    assert nonces_recorded(owner_conn, granted["caller_peer"], payload["nonce"]) == 1

    again = post_send(client, granted, message=message, envelope=envelope,
                      call_id=payload["call_id"])
    assert again.status_code == 200, again.content
    assert body_of(again)["id"] == body_of(first)["id"] == payload["call_id"]
    assert calls_with_id(owner_conn, payload["call_id"]) == 1
    assert reasons == []


def test_message_send_same_bytes_retry_after_a_server_failure_is_accepted(
        owner_conn, granted, monkeypatch):
    """Review finding 1 on this route (the invoke twin is in tests/test_calls_http.py). A
    failure inside step 8, before the call row commits, is a 500; the same message and
    envelope sent again must run, not be refused as a replay of a call that never
    existed. At d447af0 the nonce was already on record from step 4c and the resend got
    403. Control: the resend after the call exists is answered from it."""
    from apps.calls import repository

    real = repository.Jsonb
    budget = {"left": 1}

    def failing(value):
        if budget["left"] > 0:
            budget["left"] -= 1
            raise psycopg.OperationalError("simulated: server closed the connection")
        return real(value)

    monkeypatch.setattr(repository, "Jsonb", failing)
    c = Client(raise_request_exception=False)
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    message = a2a_message(parts=[types.part_data(args)])

    first = post_send(c, granted, message=message, envelope=envelope,
                      call_id=payload["call_id"])
    assert first.status_code == 500, first.content
    assert budget["left"] == 0, "positive control: the failure fired, inside step 8"
    assert calls_with_id(owner_conn, payload["call_id"]) == 0
    assert nonces_recorded(owner_conn, granted["caller_peer"], payload["nonce"]) == 0

    retry = post_send(c, granted, message=message, envelope=envelope,
                      call_id=payload["call_id"])
    assert retry.status_code == 200, retry.content
    assert body_of(retry)["id"] == payload["call_id"]
    assert calls_with_id(owner_conn, payload["call_id"]) == 1
    assert nonces_recorded(owner_conn, granted["caller_peer"], payload["nonce"]) == 1

    again = post_send(c, granted, message=message, envelope=envelope,
                      call_id=payload["call_id"])
    assert again.status_code == 200, again.content
    assert calls_with_id(owner_conn, payload["call_id"]) == 1


def test_message_send_takes_the_gate_from_the_one_reader(client, owner_conn, granted,
                                                        monkeypatch):
    """Review mutant M5: this view reading the setting itself, with a getattr default of
    False and the name split in two, passed every test, because it behaves the same while
    settings.py defines the name and the "one reader" test is a text scan. So the reader
    answers OFF here while the setting says ON: the route must ask it once for the send
    and obey it. A nonceless payload is the witness - accepted only when the route used
    the reader's answer. Control: the setting is ON in this run."""
    assert nonce_ledger.enforce_from_settings() is True
    asked = []

    def reader_says_off():
        asked.append(1)
        return False

    monkeypatch.setattr(nonce_ledger, "enforce_from_settings", reader_says_off)
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    del payload["nonce"]
    envelope = {"payload": payload,
                "signature": P.b64u(P.sign(granted["seed"], P.jcs(payload))),
                "signing_kid": granted["kid"]}
    sent = post_send(client, granted, message=a2a_message(parts=[types.part_data(args)]),
                     envelope=envelope, call_id=payload["call_id"])
    assert sent.status_code == 200, sent.content
    assert asked == [1]
    assert calls_with_id(owner_conn, payload["call_id"]) == 1


def test_message_send_still_verifies_the_envelope_signature(client, granted):
    """A2A framing does not buy a caller out of authentication. Control and variant
    differ only in one byte of the signature."""
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    control = post_send(client, granted,
                        message=a2a_message(parts=[types.part_data(args)]),
                        envelope=envelope, call_id=payload["call_id"])
    assert control.status_code == 200, control.content

    payload, envelope = sign_call(granted, args=args)
    sig = envelope["signature"]
    envelope["signature"] = ("B" if sig[0] != "B" else "C") + sig[1:]
    bad = post_send(client, granted, message=a2a_message(parts=[types.part_data(args)]),
                    envelope=envelope, call_id=payload["call_id"])
    assert bad.status_code == 401, bad.content
    assert body_of(bad)["error"].startswith("E_SIG_")


def test_message_send_rejects_a_malformed_body_shape(client, granted):
    """`MessageSendBody` deserialization: a body that parsed as JSON but is not this
    type is 422, distinct from the 400 the CONTENT checks above
    return. Control first, so the 422s are attributable to the missing field."""
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    message = a2a_message(parts=[types.part_data(args)])
    control = post_send(client, granted, message=message, envelope=envelope,
                        call_id=payload["call_id"])
    assert control.status_code == 200, control.content

    url = send_url(granted["target_peer"])
    headers = bearer(granted["caller_peer"])
    for body in ({"envelope": envelope},                       # no message
                 {"message": message},                         # no envelope
                 {"message": {"role": types.ROLE_USER, "parts": []},
                  "envelope": envelope},                       # no messageId
                 {"message": {**message, "role": "user"},      # v0.3.x lowercase role
                  "envelope": envelope}):
        bad = client.post(url, data=json.dumps(body),
                          content_type="application/json", headers=headers)
        assert bad.status_code == 422, (body.keys(), bad.content)


def test_message_send_separates_415_422_and_the_field_type_checks(client, owner_conn,
                                                                  granted):
    """STATUS C71: the 415, the JSON-object 422, the `envelope.signature` /
    `envelope.signing_kid` type checks and the `message.parts` shape check each survived
    DELETION, because no test sent the body each one refuses. One body per check, each a
    ONE-FIELD edit of the accepted control, each asserted on its own status and message -
    "any 4xx" would pass with the check deleted and a different gate refusing downstream
    (a non-string signature still fails signature verification, as 401)."""
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    message = a2a_message(parts=[types.part_data(args)])
    url = send_url(granted["target_peer"])
    headers = bearer(granted["caller_peer"])

    def post_raw(data, content_type="application/json"):
        return client.post(url, data=data, content_type=content_type, headers=headers)

    def refused(response, status, message_text):
        assert response.status_code == status, response.content
        assert body_of(response) == {"error": "E_VALIDATION", "message": message_text}

    good = {"message": message, "envelope": envelope, "call_id": payload["call_id"]}
    # CONTROL: the same bytes are accepted as application/json - and as a `+json` subtype,
    # which the content-type check admits too.
    bad_type = post_raw(json.dumps(good), content_type="text/plain")
    refused(bad_type, 415, "expected `application/json` content-type, got 'text/plain'")
    assert calls_with_id(owner_conn, payload["call_id"]) == 0, "a 415 started a call"
    control = post_raw(json.dumps(good), content_type="application/a2a+json")
    assert control.status_code == 200, control.content

    # 422: JSON, but not an object.
    refused(post_raw(json.dumps([good])), 422, "body must be a JSON object")

    def variant(**edits):
        p, e = sign_call(granted, args=args)
        body = {"message": a2a_message(parts=[types.part_data(args)]), "envelope": e,
                "call_id": p["call_id"]}
        for path, value in edits.items():
            where, key = path.split("__")
            body[where][key] = value
        return post_raw(json.dumps(body))

    refused(variant(envelope__signature=12345), 422,
            "`envelope.signature` must be a string")
    refused(variant(envelope__signing_kid=["kid"]), 422,
            "`envelope.signing_kid` must be a string")
    refused(variant(message__parts={"data": args}), 422,
            "`message.parts` must be an array of objects")
    refused(variant(message__parts=["not an object"]), 422,
            "`message.parts` must be an array of objects")


def test_is_terminal_refuses_a_string_that_is_not_a_task_state():
    """STATUS C71: `types.is_terminal` without its `TASK_STATES` guard answered False for a
    typo'd state, which keeps a finished stream open, and survived. CONTROL: every real
    TaskState answers, terminal or not, before the typo is shown to raise."""
    assert types.is_terminal(types.TASK_STATE_COMPLETED) is True
    assert types.is_terminal(types.TASK_STATE_WORKING) is False
    for state in types.TASK_STATES:
        assert types.is_terminal(state) in (True, False)
    with pytest.raises(types.UnknownCallStatus):
        types.is_terminal("TASK_STATE_COMPLETD")
    with pytest.raises(types.UnknownCallStatus):
        types.is_terminal("completed")        # a CallStatus is not a TaskState


def test_the_message_send_wait_is_bounded_by_a2a_sync_timeout():
    """STATUS C71: A2A_SYNC_TIMEOUT = 30 s bounds the poll; dropping the
    `min` survived. Controls: a value inside the bound is read as given, and the two values
    that cannot be a wait read as the 0 s default."""
    from apps.a2a import views as a2a_views

    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS=2.5):
        assert a2a_views._sync_poll_seconds() == 2.5
    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS=999):
        assert a2a_views._sync_poll_seconds() == a2a_views.A2A_SYNC_TIMEOUT_SECONDS == 30.0
    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS=-1):
        assert a2a_views._sync_poll_seconds() == 0.0
    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS="soon"):
        assert a2a_views._sync_poll_seconds() == 0.0


def test_message_send_polls_a_sync_call_until_a_concurrent_completion_lands(
        client, owner_conn, granted, monkeypatch):
    """STATUS C12, the a2a half: the fixture pins the wait to 0, so `_await_terminal`'s
    loop never iterated here either. The poll's own sleep is the hook that COMMITS the
    completion, so this is deterministic. CONTROL: at the default 0 s wait the Task comes
    back in flight and the loop never sleeps; at a real wait it comes back completed.

    The completion lands at the SECOND sleep, so a fixed "read, sleep once, read" with no
    loop comes back in flight and fails; and the hook is `views._poll_sleep`, local to the
    view, not the process-wide `time.sleep` that every thread shares (review of 129e325,
    finding 2)."""
    import time as _time

    from apps.a2a import views as a2a_views

    real_time_sleep = _time.sleep

    sleeps: list[float] = []
    completing: dict = {}

    def completing_sleep(seconds):
        sleeps.append(seconds)
        if completing.get("call_id") and len(sleeps) == 2:
            with owner_conn.cursor() as cur:
                cur.execute("UPDATE skill_calls SET status='completed', result=%s, "
                            "completed_at=NOW() WHERE call_id = %s",
                            (json.dumps({"ok": True}), completing["call_id"]))
            owner_conn.commit()

    monkeypatch.setattr(a2a_views, "_poll_sleep", completing_sleep)
    assert _time.sleep is real_time_sleep, "the hook must not replace the process's sleep"
    args = {"text": "hello"}

    payload, envelope = sign_call(granted, args=args)
    control = post_send(client, granted, message=a2a_message(parts=[types.part_data(args)]),
                        envelope=envelope, call_id=payload["call_id"])
    assert control.status_code == 200, control.content
    assert body_of(control)["status"]["state"] != types.TASK_STATE_COMPLETED
    assert sleeps == []

    payload, envelope = sign_call(granted, args=args)
    completing["call_id"] = payload["call_id"]
    with override_settings(MESHY_SYNC_TIMEOUT_SECONDS=5.0):
        polled = post_send(client, granted,
                           message=a2a_message(parts=[types.part_data(args)]),
                           envelope=envelope, call_id=payload["call_id"])
    assert polled.status_code == 200, polled.content
    assert len(sleeps) == 2, sleeps
    assert body_of(polled)["status"]["state"] == types.TASK_STATE_COMPLETED


@pytest.mark.parametrize("status", ["suspended", "deleted"])
def test_message_send_to_an_inactive_target_is_403_and_writes_no_call(
        client, owner_conn, granted, status):
    """STATUS C72: the 404 that the API description would suggest is deliberately a 403. An
    inactive TARGET is refused in
    `start_call`'s step 5 with NotAuthorized -> 403 (module docstring, item 1b).
    ONE VARIABLE, the target's `peers.status`:
      CONTROL 1 - active: 200 and a call row.
      VARIANT   - suspended / deleted: 403 E_FORBIDDEN (not 404) and NO call row.
      CONTROL 2 - active again: 200, so the 403 was the status and nothing else."""
    args = {"text": "hello"}

    def send():
        payload, envelope = sign_call(granted, args=args)
        response = post_send(client, granted,
                             message=a2a_message(parts=[types.part_data(args)]),
                             envelope=envelope, call_id=payload["call_id"])
        return response, payload["call_id"]

    def set_target_status(value):
        with owner_conn.cursor() as cur:
            cur.execute("UPDATE peers SET status = %s WHERE id = %s",
                        (value, granted["target_peer"]))
            assert cur.rowcount == 1
        owner_conn.commit()

    ok, ok_id = send()
    assert ok.status_code == 200, ok.content
    assert calls_with_id(owner_conn, ok_id) == 1

    set_target_status(status)
    refused, refused_id = send()
    assert refused.status_code == 403, refused.content
    assert body_of(refused) == {"error": "E_FORBIDDEN", "message": "forbidden"}
    assert calls_with_id(owner_conn, refused_id) == 0, "a refused send wrote its call"

    set_target_status("active")
    again, _ = send()
    assert again.status_code == 200, again.content


def test_message_send_rejects_a_bad_peer_id_and_a_bad_method(client, granted):
    """A malformed `{peer_id}` is a 400 from the extractor, NOT a 404 - a 404 would
    claim the peer does not exist when the truth is the id was never well-formed."""
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    message = a2a_message(parts=[types.part_data(args)])
    assert post_send(client, granted, message=message, envelope=envelope,
                     call_id=payload["call_id"]).status_code == 200

    bad = client.post(send_url("not-a-uuid"),
                      data=json.dumps({"message": message, "envelope": envelope}),
                      content_type="application/json",
                      headers=bearer(granted["caller_peer"]))
    assert bad.status_code == 400, bad.content

    wrong_method = client.get(send_url(granted["target_peer"]),
                              headers=bearer(granted["caller_peer"]))
    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"


def test_message_send_needs_a_token(client, granted):
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    message = a2a_message(parts=[types.part_data(args)])
    assert post_send(client, granted, message=message, envelope=envelope,
                     call_id=payload["call_id"]).status_code == 200
    anon = client.post(send_url(granted["target_peer"]),
                       data=json.dumps({"message": message, "envelope": envelope}),
                       content_type="application/json")
    assert anon.status_code == 401, anon.content


# ==========================================================================
# 5. AC#6: WHICH SOURCE each of the three call-identity fields is read from
# ==========================================================================
#
# EVERYTHING ABOVE THIS LINE AGREES WITH ITSELF, AND THAT IS THE GAP. `sign_call`
# (tests/test_a2a.py:285-287) writes `caller_peer_id` from the fixture and
# `target_peer_id` / `skill_name` from arguments that DEFAULT to the fixture, and
# `post_send` (:306-312) then mints the token for that same caller and builds the URL
# from that same target and skill. So in every test in section 4 the AUTHENTICATED
# CHANNEL, the ROUTE PATH and the SIGNED ENVELOPE carry identical ids - and while three
# sources agree, no assertion downstream of them can say which one was read. The
# read-back at :861-865 is the clearest case: it asserts the row names the right caller,
# target and skill, and it would assert exactly that whichever source
# `apps/a2a/views.py:448-450` had taken them from. A mutant sourcing all three from
# `envelope.payload` passes this file as it stood.
#
# The three tests below make the sources DISAGREE, one axis per test, and assert on that
# axis's own field with the other two held fixed. They pin caller identity from the
# authenticated CHANNEL (`auth.peer_id`) and target identity from the ROUTE PATH, never
# from the signed envelope's payload. The refusals come from two different gates, which is
# why the two expected statuses differ: the caller axis is caught by the
# identity binding inside signature verification
# (401 E_SIG_IDENTITY_MISMATCH),
# the target and skill axes by the confused-deputy rebinding gate that runs after it
# (403 E_FORBIDDEN). A test here that accepted "any 4xx" would not notice if the
# sourcing moved and only the gate order changed.


def _call_row(conn, call_id: str):
    """The three identity columns of one `skill_calls` row, or None, read as the
    BOOTSTRAP role - so "no row" is a fact about the database and not about whichever
    RLS context the app last bound.

    Every absence asserted with this helper below is preceded, in the SAME test, by an
    accepted control whose row this SAME query returns. An absence from a query that
    could never find anything - a typo'd column, a committed-away row - is indis-
    tinguishable from a refusal, and would pass on a surface that started the call.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT caller_peer_id, target_peer_id, skill_name "
                    "FROM skill_calls WHERE call_id = %s", (call_id,))
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return None
    return {"caller_peer_id": row[0], "target_peer_id": row[1], "skill_name": row[2]}


def test_the_caller_is_the_authenticated_channel_not_the_signed_envelope(
        client, owner_conn, granted):
    """THE CALLER AXIS. `views.py:448` must read `auth.peer_id` and nothing else.

    Two controls and then two variants, because this axis has two directions and only
    the pair of them pins the field. Both variants hold the ROUTE PATH and the message
    fixed and move exactly one source:

      A. the CHANNEL moves. The stranger presents an envelope the CALLER signed. A
         surface reading the envelope attributes the call to the caller - a peer who
         never touched this request - and the stranger has borrowed an identity.
      B. the ENVELOPE moves. The caller presents an envelope the STRANGER signed, and
         the stranger is given a real published key and a real grant first, so the
         envelope is valid and authorized on its own terms. A surface reading the
         envelope attributes the call to the stranger: the caller has laundered a call
         under another peer's name, and the stranger's grant is what pays for it.

    Both directions are refused by ONE check - `verify_payload`'s identity binding,
    which compares the signed
    `caller_peer_id` against the AUTHENTICATED peer the pipeline was handed. That check
    is only load-bearing because the pipeline is handed the channel identity: fed the
    envelope's own claim it compares the value against itself and cannot fail, which is
    what makes a mutation here silent rather than noisy.

    Every request builds a FRESH envelope, so no variant can be answered by the
    idempotency arm (pipeline.py:139-146) - that runs before authentication and would
    make a replay look like an acceptance.
    """
    args = {"text": "whose call is this"}
    message = lambda: a2a_message(parts=[types.part_data(args)])  # noqa: E731

    # CONTROL 1: all three sources agree on the caller -> accepted, and the row names
    # the caller. This is also the control on `_call_row` itself.
    payload, envelope = sign_call(granted, args=args)
    control = post_send(client, granted, message=message(), envelope=envelope,
                        call_id=payload["call_id"])
    assert control.status_code == 200, control.content
    assert _call_row(owner_conn, payload["call_id"])["caller_peer_id"] == \
        granted["caller_peer"]

    # CONTROL 2: the stranger is made into a fully viable caller - own published key,
    # own grant - and all three sources agree on THEM. Accepted, attributed to the
    # stranger. Without this control the two refusals below could be "the stranger's
    # channel does not authenticate" or "the stranger has no grant", neither of which
    # would say anything about which source the caller is read from.
    stranger_seed, stranger_kid = _publish_key(owner_conn, granted["stranger_peer"])
    _grant(owner_conn, grantor_owner=granted["target_owner"],
           grantee_owner=granted["stranger_owner"],
           granter_peer=granted["target_peer"], skill_name=SKILL_NAME)
    as_stranger = {**granted, "caller_peer": granted["stranger_peer"],
                   "seed": stranger_seed, "kid": stranger_kid}
    payload, envelope = sign_call(as_stranger, args=args)
    control2 = post_send(client, granted, message=message(), envelope=envelope,
                         call_id=payload["call_id"], as_peer=granted["stranger_peer"])
    assert control2.status_code == 200, control2.content
    assert _call_row(owner_conn, payload["call_id"])["caller_peer_id"] == \
        granted["stranger_peer"]

    # VARIANT A: the caller's envelope over the STRANGER's channel. Differs from
    # control 1 in the token and in nothing else.
    payload, envelope = sign_call(granted, args=args)
    borrowed = post_send(client, granted, message=message(), envelope=envelope,
                         call_id=payload["call_id"], as_peer=granted["stranger_peer"])
    assert borrowed.status_code == 401, borrowed.content
    assert body_of(borrowed)["error"] == "E_SIG_IDENTITY_MISMATCH", borrowed.content
    assert _call_row(owner_conn, payload["call_id"]) is None, (
        "a call was started under a caller identity the channel never authenticated")

    # VARIANT B: the stranger's envelope over the CALLER's channel. Differs from
    # control 2 in the token and in nothing else.
    payload, envelope = sign_call(as_stranger, args=args)
    laundered = post_send(client, granted, message=message(), envelope=envelope,
                          call_id=payload["call_id"])
    assert laundered.status_code == 401, laundered.content
    assert body_of(laundered)["error"] == "E_SIG_IDENTITY_MISMATCH", laundered.content
    assert _call_row(owner_conn, payload["call_id"]) is None, (
        "a call was started under a caller identity taken from the request body")


def test_the_target_is_the_route_path_not_the_signed_envelope(
        client, owner_conn, granted):
    """THE TARGET AXIS. `views.py:449` must read `_peer_uuid(peer_id)` - the path
    segment - and not `envelope.payload["target_peer_id"]`.

    TWO controls, because the interesting refusal is a CROSS and a cross can be blamed
    on either end. The stranger peer is given the same skill and its own grant, so both
    peers are independently valid targets for this caller: control 1 sends to the target
    with both sources naming the target, control 2 sends to the stranger with both
    sources naming the stranger, and both are accepted and recorded against the peer
    they named. Only then is the cross - route path to the stranger, envelope naming the
    target - sent. It differs from control 1 in the route path alone and from control 2
    in the envelope alone, so its refusal is attributable to the DISAGREEMENT and not to
    either peer being unusable.

    What the cross would buy a caller on a surface that read the envelope: a grant is
    per (grantor owner, grantee owner, granter peer, skill) - `find_active_grant`,
    apps/calls/authorize.py:154-167 - so routing by the body means the URL a client (and
    anything logging or rate-limiting it) sees names one peer while the call, the grant
    it spends and the row it writes belong to another. Only the HTTP path has this gap,
    because the WS transport derives its target from the payload, so the rebinding check is a
    no-op there; on HTTP it closes the gap.
    """
    args = {"text": "which target"}
    stranger_peer = granted["stranger_peer"]
    message = lambda: a2a_message(parts=[types.part_data(args)])  # noqa: E731

    # The stranger becomes a real, independently invocable target for this caller.
    _skill(owner_conn, stranger_peer, SKILL_NAME)
    _grant(owner_conn, grantor_owner=granted["stranger_owner"],
           grantee_owner=granted["caller_owner"],
           granter_peer=stranger_peer, skill_name=SKILL_NAME)

    # CONTROL 1: route path and envelope both name the target peer.
    payload, envelope = sign_call(granted, args=args)
    control = post_send(client, granted, message=message(), envelope=envelope,
                        call_id=payload["call_id"])
    assert control.status_code == 200, control.content
    assert _call_row(owner_conn, payload["call_id"])["target_peer_id"] == \
        granted["target_peer"]

    # CONTROL 2: route path and envelope both name the stranger peer.
    payload, envelope = sign_call(granted, args=args, target_peer=stranger_peer)
    control2 = post_send(client, granted, message=message(), envelope=envelope,
                         call_id=payload["call_id"], target=stranger_peer)
    assert control2.status_code == 200, control2.content
    assert _call_row(owner_conn, payload["call_id"])["target_peer_id"] == stranger_peer

    # VARIANT: the route path says stranger, the envelope says target. Both are live
    # targets, so the ONLY thing wrong with this request is that the two disagree.
    payload, envelope = sign_call(granted, args=args)
    crossed = post_send(client, granted, message=message(), envelope=envelope,
                        call_id=payload["call_id"], target=stranger_peer)
    assert crossed.status_code == 403, crossed.content
    assert body_of(crossed)["error"] == "E_FORBIDDEN", crossed.content
    assert _call_row(owner_conn, payload["call_id"]) is None, (
        "a call was routed to a target taken from the request body, not the URL")


def test_the_skill_is_the_route_path_not_the_signed_envelope(
        client, owner_conn, granted):
    """THE SKILL AXIS. `views.py:450` must read the `{skill_name}` path segment.

    Same two-control shape as the target axis and for the same reason. A SECOND skill is
    published on the same target peer with its own grant, so both skills are
    independently invocable by this caller down the same route with the same token;
    control 1 invokes one with both sources agreeing, control 2 invokes the other with
    both sources agreeing, and each row records the skill it named. The cross - URL
    naming the second skill, envelope naming the first - then differs from each control
    in exactly one source.

    This axis is worth its own test rather than being folded into the target's because
    the skill name is what the GRANT is keyed on and what the version pin is checked
    against (`authorize.py:229-234`): a surface reading the skill from the envelope lets
    a caller point the URL at a skill they are entitled to and have the server resolve,
    authorize and execute a different one - the pin on the second skill would be
    consulted for a call that ran the first.
    """
    args = {"text": "which skill"}
    other_skill = "a2a.test.other.skill"
    message = lambda: a2a_message(parts=[types.part_data(args)])  # noqa: E731

    # A second, independently invocable skill on the SAME target peer.
    _skill(owner_conn, granted["target_peer"], other_skill)
    _grant(owner_conn, grantor_owner=granted["target_owner"],
           grantee_owner=granted["caller_owner"],
           granter_peer=granted["target_peer"], skill_name=other_skill)

    # CONTROL 1: route path and envelope both name the first skill.
    payload, envelope = sign_call(granted, args=args)
    control = post_send(client, granted, message=message(), envelope=envelope,
                        call_id=payload["call_id"])
    assert control.status_code == 200, control.content
    assert _call_row(owner_conn, payload["call_id"])["skill_name"] == SKILL_NAME

    # CONTROL 2: route path and envelope both name the second skill.
    payload, envelope = sign_call(granted, args=args, skill_name=other_skill)
    control2 = post_send(client, granted, message=message(), envelope=envelope,
                         call_id=payload["call_id"], skill_name=other_skill)
    assert control2.status_code == 200, control2.content
    assert _call_row(owner_conn, payload["call_id"])["skill_name"] == other_skill

    # VARIANT: the URL names the second skill, the envelope names the first.
    payload, envelope = sign_call(granted, args=args)
    crossed = post_send(client, granted, message=message(), envelope=envelope,
                        call_id=payload["call_id"], skill_name=other_skill)
    assert crossed.status_code == 403, crossed.content
    assert body_of(crossed)["error"] == "E_FORBIDDEN", crossed.content
    assert _call_row(owner_conn, payload["call_id"]) is None, (
        "a call resolved the skill named in the request body, not the one in the URL")


# ==========================================================================
# the layer underneath the SQL predicate: the connection a2a serves on
#
# STATUS C65. `_route` and `_stream_frames` each open their
# connection through `dbconn.serving_connection()`, which REFUSES a connection that could
# bypass row-level security. Replacing either with a raw connect as the bootstrap `meshy`
# compatibility role (`rolbypassrls = true`) left 39 of 39 green: nothing here observed where a
# request's connection came from, so a bypassing connection would have served every
# tenant's rows without an error. `test_calls_http.py` closed the same gap for calls
# (C01); these two are that test's shape, one per a2a connection site.
# ==========================================================================


def _record_roles(monkeypatch) -> list:
    """Wrap the call reads the a2a paths make on their own connections - `reads.load_call`
    (the views) and `reads.load_call_status` (the stream's poll, since STATUS C73) - so
    each call records the role that connection RUNS AS. This observes the connection
    itself, whichever constructor opened it - the constructor patch below cannot see a
    connection that was not opened through it."""
    seen: list = []

    def recording_of(real):
        def recording(conn, call_id):
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, (SELECT rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user)")
                seen.append(cur.fetchone())
            return real(conn, call_id)
        return recording

    monkeypatch.setattr("apps.calls.reads.load_call", recording_of(reads.load_call))
    monkeypatch.setattr("apps.calls.reads.load_call_status",
                        recording_of(reads.load_call_status))
    return seen


def _assert_bootstrap_bypasses_and_sees(call_id: str) -> None:
    """The injected connection is ALIVE and BYPASSING and can read the very row the
    refusal must not disclose - so a refusal is about its privilege, not a dead handle."""
    with psycopg.connect(DSN, autocommit=True) as probe, probe.cursor() as cur:
        cur.execute("SELECT current_user, (SELECT rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user)")
        role, bypasses = cur.fetchone()
        assert bypasses is True, (role, bypasses)
        cur.execute("SELECT count(*) FROM skill_calls WHERE call_id = %s", (call_id,))
        assert cur.fetchone()[0] == 1, "the bypassing connection cannot see the seeded call"


def test_a_task_read_served_on_an_rls_bypassing_connection_is_refused(
        client, owner_conn, world, monkeypatch):
    """`_route` (apps/a2a/views.py:115). THREE CONTROLS, as in the calls test:
      1. un-patched, the read is a 200 AND every connection it read on is observed to be
         a role that does not bypass RLS;
      2. the injected connection is alive, bypassing, and can see the row;
      3. the patched constructor was REACHED (`opened`) - if the route stops taking its
         connection from `apps.core.dbconn`, this names that, which is the mutation.
    Then the verdict: a 500 E_INTERNAL that discloses neither the call nor its result."""
    call_id = seed_call(owner_conn, world, status="completed",
                        result={"echo": "tenant-private-result"})
    seen = _record_roles(monkeypatch)

    ok = client.get(task_url(call_id), headers=bearer(world["caller_peer"]))
    assert ok.status_code == 200, ok.content
    assert body_of(ok)["id"] == call_id
    assert seen and all(bypasses is False for _role, bypasses in seen), seen

    _assert_bootstrap_bypasses_and_sees(call_id)

    opened: list = []

    def bypassing_connect(target=None, **kwargs):
        conn = psycopg.connect(target or DSN, autocommit=True)
        opened.append(conn)
        return conn

    monkeypatch.setattr("apps.core.dbconn.connect", bypassing_connect)
    refused = client.get(task_url(call_id), headers=bearer(world["caller_peer"]))

    assert opened, ("the a2a route did not take its connection from apps.core.dbconn."
                    "connect(), so nothing verifies the role it serves on")
    assert refused.status_code == 500, refused.content
    assert body_of(refused)["error"] == "E_INTERNAL"
    text = refused.content.decode("utf-8")
    assert call_id not in text and "tenant-private-result" not in text, text


def test_a_stream_whose_own_connection_bypasses_rls_closes_failed_without_a_chunk(
        client, owner_conn, world, monkeypatch, caplog):
    """`_stream_frames` (apps/a2a/views.py:257) opens a SECOND connection, after the view
    has returned, so the route's guard does not cover it.

    The route's connection is left REAL - the authorization in the view runs and passes
    exactly as in production - and only the generator's is swapped for a bypassing one.
    The `200 text/event-stream` is already on the wire by then, so the refusal is the
    terminal FAILED frame `_stream_body` closes with, and no chunk leaves.

    CONTROLS: un-patched, the same stream carries its chunk and closes COMPLETED, and
    both connections it read on are observed non-bypassing; the injected connection is
    alive and bypassing; the generator's connection came through the patched
    constructor (the second of exactly two opens)."""
    call_id = seed_call(owner_conn, world, status="completed", result={})
    seed_chunk(owner_conn, call_id, 1, b"tenant-private-chunk")
    seen = _record_roles(monkeypatch)

    control = sse_text(client.get(stream_url(call_id), headers=bearer(world["caller_peer"])))
    assert "tenant-private-chunk" in control, control
    assert _sse_frames(control)[-1]["status"]["state"] == types.TASK_STATE_COMPLETED
    assert len(seen) >= 2, ("expected a read on the route's connection AND on the "
                            f"generator's; saw {seen}")
    assert all(bypasses is False for _role, bypasses in seen), seen

    _assert_bootstrap_bypasses_and_sees(call_id)

    real_connect = dbconn.connect
    opens: list = []
    bypassing: list = []

    def route_real_then_bypassing(target=None, **kwargs):
        opens.append(kwargs)
        if len(opens) == 1:
            return real_connect(target, **kwargs)
        conn = psycopg.connect(target or DSN, autocommit=True)
        bypassing.append(conn)
        return conn

    monkeypatch.setattr("apps.core.dbconn.connect", route_real_then_bypassing)
    with caplog.at_level("ERROR", logger="meshychat.a2a"):
        response = client.get(stream_url(call_id), headers=bearer(world["caller_peer"]))
        assert response.status_code == 200, "the view's own authorization should pass"
        body = sse_text(response)

    assert len(opens) == 2 and bypassing, (
        f"expected the route's open and then the generator's, saw {len(opens)}: the "
        "stream did not take its connection from apps.core.dbconn.connect()")
    assert "tenant-private-chunk" not in body, body
    assert body.count("event: TaskArtifactUpdateEvent") == 0, body
    frames = _sse_frames(body)
    assert len(frames) == 1, body
    assert frames[0]["status"]["state"] == types.TASK_STATE_FAILED, body
    assert "RlsBypassError" in caplog.text, caplog.text


# ==========================================================================
# 8. COST (STATUS C73) and the final drain made observable (STATUS C69)
# ==========================================================================

def _count_connections(monkeypatch) -> list:
    """Every connection the routes open goes through `apps.core.dbconn.connect`; wrap it
    (real connection, real guard) and record each open."""
    from apps.core import dbconn as _dbconn

    opened: list = []
    real = _dbconn.connect

    def counting(*args, **kwargs):
        conn = real(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr("apps.core.dbconn.connect", counting)
    return opened


def test_a_wrong_method_is_answered_before_any_connection_is_opened(
        client, owner_conn, world, monkeypatch):
    """STATUS C73: `_method` ran INSIDE the handler, after `_route` had opened a guarded
    connection, so an unauthenticated 405 cost a full connect. Route dispatch answers an
    unknown method before any request validation runs.

    CONTROL: the counter SEES connections - the allowed method on each route opens one
    (the stream: one for the view, one for its generator). FINDING: the 405s open none,
    and still carry `Allow`."""
    call_id = seed_call(owner_conn, world, status="completed", result={})
    opened = _count_connections(monkeypatch)

    assert client.get(task_url(call_id),
                      headers=bearer(world["caller_peer"])).status_code == 200
    assert len(opened) == 1, len(opened)
    sse_text(client.get(stream_url(call_id), headers=bearer(world["caller_peer"])))
    assert len(opened) == 3, len(opened)

    opened.clear()
    for method, url, allow in (("post", task_url(call_id), "GET"),
                               ("delete", stream_url(call_id), "GET"),
                               ("get", send_url(world["target_peer"]), "POST")):
        response = getattr(client, method)(url)          # no token at all
        assert response.status_code == 405, (url, response.status_code)
        assert response.headers["Allow"] == allow
    assert opened == [], f"a 405 opened {len(opened)} connection(s)"


def test_a_stream_polls_the_status_not_the_whole_row(client, owner_conn, world,
                                                     monkeypatch):
    """STATUS C73: every poll re-read the call's 34 columns, `args` and `result` jsonb
    included, 5 times a second per open stream, to look at one of them. The poll now
    reads `reads.load_call_status` (call_id and status only).

    The FULL-ROW read still happens exactly once per request, in the view's authorization,
    which is the control that the spy is on the path. The call stays `running` for two
    polls and completes on the third, so a per-poll full read would count more than one."""
    from apps.a2a import views as a2a_views

    call_id = seed_call(owner_conn, world, status="running", result=None)
    full_reads: list = []
    status_reads: list = []
    real_load = reads.load_call
    real_status = getattr(reads, "load_call_status", None)
    assert real_status is not None, "reads.load_call_status does not exist"

    def counting_load(conn, cid):
        full_reads.append(cid)
        return real_load(conn, cid)

    def counting_status(conn, cid):
        status_reads.append(cid)
        if len(status_reads) == 3:
            with owner_conn.cursor() as cur:
                cur.execute("UPDATE skill_calls SET status='completed', completed_at=NOW() "
                            "WHERE call_id = %s", (cid,))
            owner_conn.commit()
        return real_status(conn, cid)

    monkeypatch.setattr("apps.calls.reads.load_call", counting_load)
    monkeypatch.setattr("apps.calls.reads.load_call_status", counting_status)
    monkeypatch.setattr(a2a_views, "_poll_sleep", lambda seconds: None)

    body = sse_text(client.get(stream_url(call_id), headers=bearer(world["caller_peer"])))
    assert _sse_frames(body)[-1]["status"]["state"] == types.TASK_STATE_COMPLETED, body
    assert len(status_reads) == 3, status_reads
    assert full_reads == [call_id], f"the poll re-read the full row: {full_reads}"


def test_the_stream_reads_chunks_in_bounded_pages_and_loses_none(
        client, owner_conn, world, monkeypatch):
    """STATUS C73: `list_chunks` was an unbounded fetchall - a reconnect to a long stream
    re-read every chunk the call ever stored in one statement. The stream now reads pages
    of `STREAM_CHUNK_PAGE`.

    PAGE SIZE 2 over FIVE persisted chunks: every chunk arrives once, in order, the first
    new and the rest appended, the status closes it - and no single read returned more
    than a page. The control for the bound is the count of reads: five chunks at two per
    page take more than one read, so a single unbounded read cannot pass."""
    from apps.a2a import views as a2a_views

    call_id = seed_call(owner_conn, world, status="completed", result={})
    for seq, text in enumerate(("a", "b", "c", "d", "e"), start=1):
        seed_chunk(owner_conn, call_id, seq, text.encode())

    page_sizes: list = []
    real = reads.list_chunks

    def recording(conn, cid, since_seq, **kwargs):
        out = real(conn, cid, since_seq, **kwargs)
        page_sizes.append(len(out.rows) if isinstance(out, reads.Rows) else 0)
        return out

    monkeypatch.setattr(a2a_views, "STREAM_CHUNK_PAGE", 2)
    monkeypatch.setattr("apps.calls.reads.list_chunks", recording)

    frames = _sse_frames(sse_text(client.get(stream_url(call_id),
                                             headers=bearer(world["caller_peer"]))))
    artifacts = [f for f in frames if "artifact" in f]
    assert [f["artifact"]["parts"][0]["text"] for f in artifacts] == list("abcde")
    assert [f.get("append", False) for f in artifacts] == [False, True, True, True, True]
    assert frames[-1]["status"]["state"] == types.TASK_STATE_COMPLETED
    assert max(page_sizes) <= 2, page_sizes
    assert len(page_sizes) > 1, page_sizes


def test_the_final_drain_emits_a_chunk_committed_after_the_polls_chunk_read(
        client, owner_conn, world, monkeypatch):
    """STATUS C69. THE FINAL DRAIN BEFORE CLOSE, observed deterministically. The window it
    exists for: a chunk commits AFTER the poll's `list_chunks` and BEFORE its status read
    finds the call terminal. The status read is the hook: on the generator's first status
    read it commits chunk 1 AND the terminal transition, in that order (as `report_chunk`
    then the terminal update do), then reads.

    CONTROL: the same call shape with the chunk committed BEFORE the stream opens arrives
    through the ordinary poll - so the chunk projection and the close both work, and the
    only thing the finding can be about is the re-drain. FINDING: the late chunks are
    emitted, in order, then the terminal status. Deleting the drain dropped them and passed
    every other test in this file (`test_stream_tails_live_chunks...` relies on timing).
    THREE late chunks at a page size of TWO, so the drain itself must page (C73)."""
    from apps.a2a import views as a2a_views

    monkeypatch.setattr(a2a_views, "STREAM_CHUNK_PAGE", 2)
    # If the hook is ever bypassed the call stays `running`: close after 10 idle polls,
    # not the production ceiling's three minutes.
    monkeypatch.setattr(a2a_views, "STREAM_MAX_POLLS", 10)
    early = seed_call(owner_conn, world, status="completed", result={})
    seed_chunk(owner_conn, early, 1, b"early")
    control = _sse_frames(sse_text(client.get(stream_url(early),
                                              headers=bearer(world["caller_peer"]))))
    assert [f["artifact"]["parts"][0]["text"] for f in control if "artifact" in f] == [
        "early"]
    assert control[-1]["status"]["state"] == types.TASK_STATE_COMPLETED

    late = seed_call(owner_conn, world, status="running", result=None)
    real_status = reads.load_call_status
    fired: list = []

    def commit_in_the_window(conn, cid):
        if cid == late and not fired:
            fired.append(cid)
            for seq, text in enumerate((b"late ", b"tail ", b"end"), start=1):
                seed_chunk(owner_conn, cid, seq, text)
            with owner_conn.cursor() as cur:
                cur.execute("UPDATE skill_calls SET status='completed', completed_at=NOW() "
                            "WHERE call_id = %s", (cid,))
            owner_conn.commit()
        return real_status(conn, cid)

    monkeypatch.setattr("apps.calls.reads.load_call_status", commit_in_the_window)
    frames = _sse_frames(sse_text(client.get(stream_url(late),
                                             headers=bearer(world["caller_peer"]))))
    assert fired == [late], "the hook never ran: the poll did not read the status"
    assert [f["artifact"]["parts"][0]["text"] for f in frames if "artifact" in f] == [
        "late ", "tail ", "end"], frames
    assert frames[-1]["status"]["state"] == types.TASK_STATE_COMPLETED


def test_the_sync_wait_reads_terminality_from_the_one_terminal_mapping():
    """`_await_terminal` polled against its own tuple of the four terminal statuses, a copy of
    `apps/calls/terminal.TERMINAL_STATUSES` (the ONE terminal mapping, divergence 041 S6) that a
    fifth status in sql's CHECK would have left out silently. It reads the owner's set now, and
    the A2A projection agrees with the owner on which statuses are terminal: the two had never
    been held against each other."""
    import inspect

    from apps.a2a import views as a2a_views
    from apps.calls import terminal as call_terminal

    projected = {s for s, t in types.CALL_STATUS_TO_TASK_STATE.items() if types.is_terminal(t)}
    assert projected == call_terminal.TERMINAL_STATUSES
    src = inspect.getsource(a2a_views._await_terminal)
    assert "terminal.TERMINAL_STATUSES" in src          # control: the scan reads the loop
    assert not re.search(r'"(completed|failed|timeout|abandoned)"', src), src


def test_an_unencodable_role_is_422_not_500(client, granted):
    """#252 (memory-bxxxxx's #275 review): the `unknown variant` message echoed the role
    raw, so a lone surrogate made the 422 response itself unencodable: a 500. It is now
    escaped. The marker `~` is in no base64url, hex or uuid."""
    args = {"text": "hello"}
    payload, envelope = sign_call(granted, args=args)
    message = {**a2a_message(parts=[types.part_data(args)]), "role": "x~"}
    body = json.dumps({"message": message, "envelope": envelope})
    assert body.count("~") == 1
    bad = client.post(send_url(granted["target_peer"]),
                      data=body.replace("~", chr(92) + "ud800"),
                      content_type="application/json", headers=bearer(granted["caller_peer"]))
    assert bad.status_code == 422, bad.content
    assert "unknown variant `x" + chr(92) * 2 + "ud800`" in bad.content.decode("utf-8")
