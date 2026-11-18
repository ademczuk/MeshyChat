"""Divergence 041 S9: the invocation webhook doorbell.

Design record `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6): section 7
row S9;
4.4 (step 9 inserts the due attempt); 4.11 (registration, proof of control, signing, body,
delivery record, where it runs); 5.4; 5.5; the approved Q2, Q3, Q4 (the webhook half), Q9, Q10
and Q16; critique F3, F10, F11.

A target with no socket open gets a POINTER-ONLY doorbell at a URL it registered and proved it
controls, signed with a dedicated per-host Ed25519 key that the JWKS publishes under its own
kid, from `run_call_worker`, never from a request thread. The call stays `queued`; the woken
target connects and redelivery hands it the call over its socket.

  schema  the three tables are server_internal only (ENABLE and FORCE); at most one due or
          claimed attempt per peer, held by the database; a registration and a due attempt
          NOTIFY the worker with a pointer, at commit.
  route   PUT and DELETE /v1/peers/me/invocation-webhook: DPoP-bound peer writes, no cookie
          path, S8's URL rules, an unverified row with a fresh 32-byte challenge, nothing POSTed
          by the request.
  key     created once per host with an exclusive create, never overwritten, rotated by
          deletion, never the token key; the JWKS publishes it second under its own kid.
  W1      one doorbell POST; its JWT verifies under the WEBHOOK kid from the served JWKS, with
          iss, sub, aud (the exact URL), iat, exp = iat + 120, jti and body_sha256; a one-byte
          body change fails the receiver's check.
  W2      a 2xx writes webhook_fired and nothing else: the call stays `queued`.
  W3      two workers claim one attempt; exactly one POSTs.
  W4      a worker that crashed after its claim is re-sent by the sweep, and only after 30 s.
  W5      skill_calls.webhook_url (stored, unread) and peers.invocation_delivery_webhook are
          never contacted.
  W6      an unverified URL is never rung; only an exact echo of the challenge verifies it.
  W7      two queued calls give ONE due attempt, and a third within 60 s none.
  W8      the doorbell's JWT does not verify under the token key, and authenticate_peer
          refuses it as a bearer at the signature.
  W9      run_call_worker LISTENs (a doorbell within 2 s) and sweeps every 5 s (a due row with
          no notification is sent within one sweep); its first start creates the key.
  review  the S9 review at a6bb87af: a doorbell waits behind at most one verification, and a
          peer is verified at most once a minute whatever it PUTs or DELETEs (F1); a failed
          ring holds the floor (F2); a verification claimed and never finished is taken again,
          and a stopping sender starts no new POST (F3); a queued call past its deadline is not
          rung, a stale claim cannot record over the claim that took it, and a verification
          another worker holds is skipped, not sent twice (F6: R1, R4, R7).

Every test here runs against a loopback recorder with the egress test hatch on (DEBUG and
MESHY_EGRESS_ALLOW_LOOPBACK), so the production send path (apps/core/egress.post) is the one
that dials.

Run:  .venv/Scripts/python.exe -m pytest tests/test_invoke_webhook.py -q -p no:cacheprovider
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import http.server
import importlib
import io
import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

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
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.authapi import tokens  # noqa: E402
from apps.authapi.didkey import to_did_key  # noqa: E402
from apps.calls import wire  # noqa: E402
from apps.core import dbconn, rls  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from tests.fixtures.call_rows import seed_call  # noqa: E402

ISSUER = "meshy-invoke-webhook-test"
_token_key = ed25519.Ed25519PrivateKey.generate()
TOKEN_PRIVATE_PEM = _token_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
TOKEN_PUBLIC_PEM = _token_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
TOKEN_KID = P.server_kid(tokens.public_key_raw(TOKEN_PUBLIC_PEM))

ROUTE = "/v1/peers/me/invocation-webhook"
CHANNEL = "meshy_call_webhook"
DOORBELL = "skill_invoke_pending"
VERIFY = "invocation_webhook_verify"
#: Planted in every call's args. A doorbell body that carried it would carry caller content.
ARGS_MARKER = "s9-args-marker-must-never-ride-a-doorbell"


# ============================================================================ fixtures

@pytest.fixture
def key_path(tmp_path) -> Path:
    return tmp_path / "invoke-webhook-ed25519.pem"


@pytest.fixture(autouse=True)
def server_config(key_path):
    """The token key both as the minter's configuration and as the verifiers' settings, the
    egress hatch on (a test box: DEBUG too), and this test's own webhook key path."""
    with authconfig.override_config(jwt_issuer=ISSUER, jwt_signing_key_pem=TOKEN_PRIVATE_PEM,
                                    jwt_public_key_pem=TOKEN_PUBLIC_PEM,
                                    jwt_public_key_history_pem=()):
        with override_settings(ALLOWED_HOSTS=["*"],
                               MESHY_JWT_PUBLIC_KEY_PEM=TOKEN_PUBLIC_PEM.decode("ascii"),
                               MESHY_JWT_ISSUER=ISSUER, MESHY_DPOP_ENFORCE=False, DEBUG=True,
                               MESHY_EGRESS_ALLOW_LOOPBACK=True,
                               MESHY_INVOKE_WEBHOOK_KEY_PATH=str(key_path)):
            yield


@pytest.fixture(autouse=True)
def no_doorbell_left_behind():
    """Only this file writes the two tables. A pass claims ANY due row in the database, so
    what a test leaves outstanding is closed after it, and a later test's pass rings only its
    own rows. Absent tables (the RED run) are not a fixture failure."""
    yield
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("UPDATE peer_webhook_attempts SET state = 'skipped', "
                         "finished_at = now(), error_class = 'test_cleanup' "
                         "WHERE state IN ('due', 'claimed')")
            conn.execute("UPDATE peer_invocation_webhooks SET disabled_at = now() "
                         "WHERE verified_at IS NULL AND disabled_at IS NULL")
            # A verification claim left open would be taken again 60 s later (review F3).
            conn.execute("UPDATE peer_webhook_verifications SET finished_at = now(), "
                         "outcome = 'test_cleanup' WHERE finished_at IS NULL")
    except psycopg.errors.UndefinedTable:
        pass


@pytest.fixture
def owner():
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture
def wh():
    """The module under test, imported per test so its absence fails each test (RED)."""
    return importlib.import_module("apps.calls.webhooks")


@pytest.fixture
def wk():
    return importlib.import_module("apps.calls.webhook_key")


@pytest.fixture
def signing_key(wk, key_path):
    """What run_call_worker's first start does: create the host's key."""
    return wk.ensure(key_path)


# --------------------------------------------------------------------- the recorder

@dataclass
class Seen:
    method: str
    path: str
    headers: dict
    body: bytes


def _type(seen: Seen):
    try:
        return json.loads(seen.body).get("type")
    except (ValueError, AttributeError):
        return None


def echo(seen: Seen) -> tuple[int, bytes]:
    """A receiver that controls its URL: the verify body's challenge back, byte for byte, and
    200 to a doorbell."""
    doc = json.loads(seen.body)
    if doc.get("type") == VERIFY:
        return 200, doc["challenge"].encode("ascii")
    return 200, b"ok"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        seen = Seen(self.command, self.path,
                    {k.lower(): v for k, v in self.headers.items()}, body)
        self.server.requests.append(seen)
        if self.server.delay and _type(seen) == DOORBELL:
            time.sleep(self.server.delay)
        status, payload = self.server.respond(seen)
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass

    do_GET = do_PUT = do_POST


class Recorder(http.server.ThreadingHTTPServer):
    """A loopback receiver that records every request and answers from `respond(seen)`."""

    daemon_threads = True

    def __init__(self, respond=None, delay: float = 0.0):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.respond = respond or echo
        self.delay = delay
        self.requests: list[Seen] = []
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def url(self, path: str = "/hook") -> str:
        return f"http://127.0.0.1:{self.server_address[1]}{path}"

    def of_type(self, kind: str) -> list[Seen]:
        return [s for s in self.requests if _type(s) == kind]

    def handle_error(self, request, client_address):
        pass

    def stop(self):
        self.shutdown()
        self.server_close()


@pytest.fixture
def recorders():
    made = []

    def make(respond=None, delay: float = 0.0) -> Recorder:
        rec = Recorder(respond, delay)
        made.append(rec)
        return rec

    yield make
    for rec in made:
        rec.stop()


# ------------------------------------------------------------------------ the world

def _peer(conn, tag: str, group: uuid.UUID) -> dict:
    owner_id, peer_id, seed = uuid.uuid4(), uuid.uuid4(), os.urandom(32)
    public = P.public_key_of(seed)
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'dev',%s)",
                 (owner_id, f"s9-{tag}-{owner_id.hex[:8]}@example.test", group))
    conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                 (peer_id, owner_id, f"s9-{tag}-{peer_id.hex[:8]}"))
    kid = P.kid_for_peer_key(public)
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    return {"peer_id": peer_id, "owner_id": owner_id, "seed": seed, "kid": kid}


def make_world(conn) -> dict:
    """C calls T's skill, whose deadline is 60 s out. T never connects."""
    group = uuid.uuid4()
    caller, target = _peer(conn, "caller", group), _peer(conn, "target", group)
    skill = f"s9.ring{uuid.uuid4().hex[:6]}"
    skill_id = conn.execute(
        "INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
        "is_current, max_runtime_ms) VALUES (%s,%s,'1.0.0','async','grants_only','{}',TRUE,"
        "60000) RETURNING id", (target["peer_id"], skill)).fetchone()[0]
    return {"caller": caller, "target": target, "skill": skill, "skill_id": skill_id}


@pytest.fixture
def world(owner):
    return make_world(owner)


def _token(peer: dict, *, cnf: str | None = None) -> str:
    now = int(time.time())
    claims = {"sub": str(peer["peer_id"]), "aud": "peer", "iss": ISSUER, "iat": now,
              "exp": now + 600, "scopes": ["skills:invoke"]}
    if cnf is not None:
        claims["cnf"] = cnf
    return pyjwt.encode(claims, TOKEN_PRIVATE_PEM, algorithm="EdDSA")


def put_webhook(peer: dict, url, *, raw: bytes | None = None, **extra):
    data = raw if raw is not None else json.dumps({"url": url})
    return Client().put(ROUTE, data=data, content_type="application/json",
                        HTTP_AUTHORIZATION="Bearer " + _token(peer), **extra)


def invoke(world: dict, *, webhook_url: str | None = None) -> str:
    """A signed HTTP invoke to T, which has no socket: step 9 writes `queued`."""
    caller, target = world["caller"], world["target"]
    payload = {"v": 1, "kind": "skill.call", "call_id": f"s9-{uuid.uuid4()}",
               "caller_peer_id": str(caller["peer_id"]),
               "target_peer_id": str(target["peer_id"]), "skill_name": world["skill"],
               "skill_version": "1.0.0", "args": {"note": ARGS_MARKER},
               "args_attachments": [],
               "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
                   "+00:00", "Z"),
               "nonce": uuid.uuid4().hex}
    envelope = {"payload": payload, "signature": P.b64u(P.sign(caller["seed"], P.jcs(payload))),
                "signing_kid": caller["kid"]}
    body = {"envelope": envelope, "call_id": payload["call_id"]}
    if webhook_url is not None:
        body["webhook_url"] = webhook_url
    response = Client().post(
        f"/v1/agents/{target['peer_id']}/skills/{world['skill']}/invoke",
        data=json.dumps(body), content_type="application/json",
        HTTP_AUTHORIZATION="Bearer " + _token(caller))
    assert response.status_code == 202, response.content
    assert json.loads(response.content)["status"] == "queued", response.content
    return payload["call_id"]


def registration(owner, peer_id) -> dict | None:
    row = owner.execute("SELECT url, challenge, requested_at, verified_at, disabled_at "
                        "FROM peer_invocation_webhooks WHERE peer_id = %s",
                        (peer_id,)).fetchone()
    if row is None:
        return None
    return dict(zip(("url", "challenge", "requested_at", "verified_at", "disabled_at"), row))


def attempts(owner, peer_id) -> list[dict]:
    cols = ("id", "call_id", "state", "claimed_by", "claimed_at", "finished_at",
            "http_status", "error_class")
    rows = owner.execute(f"SELECT {', '.join(cols)} FROM peer_webhook_attempts "
                         "WHERE peer_id = %s ORDER BY id", (peer_id,)).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def events(owner, call_id: str) -> list[tuple]:
    return owner.execute("SELECT event_type, source, details FROM skill_call_events "
                         "WHERE call_id = %s ORDER BY id", (call_id,)).fetchall()


def status_of(owner, call_id: str) -> str:
    return owner.execute("SELECT status FROM skill_calls WHERE call_id = %s",
                         (call_id,)).fetchone()[0]


def run_pass(wh, **kwargs):
    return wh.run_pass(dbconn.connect, **kwargs)


def verified(wh, owner, world: dict, rec: Recorder) -> str:
    """T registers `rec`'s URL and one worker pass proves it; returns the URL."""
    url = rec.url()
    response = put_webhook(world["target"], url)
    assert response.status_code == 202, response.content
    run_pass(wh)
    row = registration(owner, world["target"]["peer_id"])
    assert row["verified_at"] is not None and row["disabled_at"] is None, row
    return url


def bearer_of(seen: Seen) -> str:
    auth = seen.headers["authorization"]
    assert auth.startswith("Bearer "), auth
    return auth[len("Bearer "):]


#: The doorbell key's own key set (operator decision 2026-09-25): never the token JWKS.
DOORBELL_JWKS = "/.well-known/meshy-invoke-jwks.json"


def served_keys() -> list[dict]:
    """The TOKEN key set, /.well-known/jwks.json."""
    response = Client().get("/.well-known/jwks.json")
    assert response.status_code == 200, response.content
    return json.loads(response.content)["keys"]


def doorbell_keys() -> list[dict]:
    """The doorbell key set, DOORBELL_JWKS."""
    response = Client().get(DOORBELL_JWKS)
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "application/json", response["Content-Type"]
    return json.loads(response.content)["keys"]


def _public(jwk: dict) -> ed25519.Ed25519PublicKey:
    x = jwk["x"]
    return ed25519.Ed25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(x + "=" * (-len(x) % 4)))


def verify_doorbell(token: str, url: str) -> dict:
    """What a receiver does (4.11): resolve the kid against the SERVED doorbell key set
    (DOORBELL_JWKS, operator decision 2026-09-25), verify EdDSA, check aud is its own URL, iss
    and exp. A different instrument from the signer (C5)."""
    kid = pyjwt.get_unverified_header(token)["kid"]
    keys = {k["kid"]: k for k in doorbell_keys()}
    return pyjwt.decode(token, _public(keys[kid]), algorithms=["EdDSA"], audience=url,
                        issuer=ISSUER,
                        options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]})


def receiver_accepts(token: str, body: bytes, url: str) -> bool:
    try:
        claims = verify_doorbell(token, url)
    except (pyjwt.PyJWTError, KeyError):
        return False
    return claims.get("body_sha256") == hashlib.sha256(body).hexdigest()


def _wait(predicate, what: str, timeout: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return time.monotonic() - start
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# ============================================================================== schema

def test_both_tables_are_server_internal_only_in_every_command_enable_and_force(owner, world):
    """RLS on, written under server_internal (4.11; the attempts table the same, stated in
    the divergence; and the verification record the review added). A meshychat_app connection
    without the context sees no row and cannot write one. CONTROL: under server_internal the
    same connection sees it."""
    for table in ("peer_invocation_webhooks", "peer_webhook_attempts",
                  "peer_webhook_verifications"):
        enabled, forced = owner.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = %s AND relnamespace = 'public'::regnamespace",
            (table,)).fetchone()
        assert (enabled, forced) == (True, True), table
        policies = owner.execute(
            "SELECT cmd, qual, with_check FROM pg_policies WHERE schemaname = 'public' "
            "AND tablename = %s ORDER BY cmd", (table,)).fetchall()
        assert [p[0] for p in policies] == ["DELETE", "INSERT", "SELECT", "UPDATE"], policies
        for cmd, qual, check in policies:
            exprs = [e for e in (qual, check) if e is not None]
            assert exprs, (table, cmd)
            for expr in exprs:
                assert "app.server_internal" in expr and "'1'" in expr, (table, cmd, expr)

    target = world["target"]["peer_id"]
    call_id = seed_call(owner, world["caller"], world["target"], world["skill_id"],
                        status="queued", call_id=f"s9-{uuid.uuid4()}")
    owner.execute("INSERT INTO peer_invocation_webhooks (peer_id, url, challenge) "
                  "VALUES (%s, 'https://hook.example.test/x', %s)", (target, "A" * 43))
    conn = dbconn.connect()
    try:
        count = "SELECT count(*) FROM peer_invocation_webhooks WHERE peer_id = %s"
        assert conn.execute(count, (target,)).fetchone()[0] == 0
        with rls.server_internal(conn):
            assert conn.execute(count, (target,)).fetchone()[0] == 1, "control"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO peer_webhook_attempts (peer_id, call_id) VALUES (%s,%s)",
                         (target, call_id))
        conn.execute("DELETE FROM peer_invocation_webhooks WHERE peer_id = %s", (target,))
        with rls.server_internal(conn):
            assert conn.execute(count, (target,)).fetchone()[0] == 1, "a blind delete removed it"
            conn.execute("INSERT INTO peer_webhook_attempts (peer_id, call_id) VALUES (%s,%s)",
                         (target, call_id))
    finally:
        conn.close()
    assert [a["state"] for a in attempts(owner, target)] == ["due"]


def test_the_database_holds_one_outstanding_doorbell_per_peer(owner, world):
    """The partial UNIQUE (peer_id) WHERE state IN ('due', 'claimed') (4.11, F11): a second
    due or claimed row for a peer is refused whatever wrote it. CONTROLS: finished rows do not
    count, and another peer is not affected."""
    target, caller = world["target"], world["caller"]
    calls = [seed_call(owner, caller, target, world["skill_id"], status="queued",
                       call_id=f"s9-{uuid.uuid4()}") for _ in range(3)]
    insert = "INSERT INTO peer_webhook_attempts (peer_id, call_id, state) VALUES (%s,%s,%s)"
    owner.execute(insert, (target["peer_id"], calls[0], "due"))
    for state in ("due", "claimed"):
        with pytest.raises(psycopg.errors.UniqueViolation):
            owner.execute(insert, (target["peer_id"], calls[1], state))
    for state in ("sent", "failed", "skipped"):
        owner.execute(insert, (target["peer_id"], calls[1], state))
    owner.execute(insert, (caller["peer_id"], calls[2], "due"))
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute(insert, (caller["peer_id"], calls[2], "routed"))
    assert [a["state"] for a in attempts(owner, target["peer_id"])] == [
        "due", "sent", "failed", "skipped"]


def _notes(conn, timeout: float) -> list[dict]:
    return [json.loads(n.payload) for n in conn.notifies(timeout=timeout)
            if n.channel == CHANNEL]


def test_a_registration_and_a_due_attempt_notify_the_worker_with_a_pointer_at_commit(
        owner, world):
    """4.11: the PUT's commit and the due insert's commit each NOTIFY `meshy_call_webhook`,
    a pointer only (a peer id, an attempt id), never before the commit. The worker's own
    writes (verified_at, disabled_at, a finished attempt) notify nothing."""
    target = world["target"]["peer_id"]
    call_id = seed_call(owner, world["caller"], world["target"], world["skill_id"],
                        status="queued", call_id=f"s9-{uuid.uuid4()}")
    with psycopg.connect(DSN, autocommit=True) as listen:
        listen.execute(f"LISTEN {CHANNEL}")
        with psycopg.connect(DSN) as tx:
            tx.execute("INSERT INTO peer_invocation_webhooks (peer_id, url, challenge) "
                       "VALUES (%s, 'https://hook.example.test/x', %s)", (target, "B" * 43))
            assert _notes(listen, 0.3) == [], "a pointer before the commit"
            tx.commit()
        assert _notes(listen, 2.0) == [{"verify": str(target)}]

        owner.execute("UPDATE peer_invocation_webhooks SET verified_at = now() "
                      "WHERE peer_id = %s", (target,))
        owner.execute("UPDATE peer_invocation_webhooks SET disabled_at = now() "
                      "WHERE peer_id = %s", (target,))
        assert _notes(listen, 0.3) == []
        # A second PUT resets the row to unverified: the worker is told again.
        owner.execute("UPDATE peer_invocation_webhooks SET challenge = %s, verified_at = NULL, "
                      "disabled_at = NULL, requested_at = now() WHERE peer_id = %s",
                      ("C" * 43, target))
        assert _notes(listen, 2.0) == [{"verify": str(target)}]

        attempt = owner.execute("INSERT INTO peer_webhook_attempts (peer_id, call_id) "
                                "VALUES (%s,%s) RETURNING id", (target, call_id)).fetchone()[0]
        assert _notes(listen, 2.0) == [{"attempt": attempt}]
        owner.execute("UPDATE peer_webhook_attempts SET state = 'sent' WHERE id = %s",
                      (attempt,))
        assert _notes(listen, 0.3) == []


# =============================================================================== route

def test_a_put_stores_the_url_unverified_with_a_fresh_challenge_and_posts_nothing(
        owner, world, recorders):
    """Q9: PUT stores the URL unverified with a fresh 32-byte challenge, answers 202, and the
    request thread POSTs nothing (rule E7). The challenge never reaches the peer. A second
    PUT is a new registration: a new challenge, unverified again.
    `peers.invocation_delivery_webhook` is never written (5.5)."""
    rec = recorders()
    target = world["target"]
    response = put_webhook(target, rec.url())
    assert response.status_code == 202, response.content
    assert json.loads(response.content) == {"url": rec.url(), "verified": False}
    row = registration(owner, target["peer_id"])
    assert row["url"] == rec.url() and row["requested_at"] is not None
    assert row["verified_at"] is None and row["disabled_at"] is None
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", row["challenge"]), row["challenge"]
    assert len(P.b64u_decode(row["challenge"])) == 32
    assert row["challenge"] not in response.content.decode("utf-8")
    time.sleep(0.3)
    assert rec.requests == [], "the request thread POSTed"

    owner.execute("UPDATE peer_invocation_webhooks SET verified_at = now() WHERE peer_id = %s",
                  (target["peer_id"],))
    again = put_webhook(target, rec.url())
    assert again.status_code == 202, again.content
    second = registration(owner, target["peer_id"])
    assert second["challenge"] != row["challenge"] and second["verified_at"] is None
    assert owner.execute("SELECT invocation_delivery_webhook FROM peers WHERE id = %s",
                         (target["peer_id"],)).fetchone()[0] is None


BAD_URLS = {
    "http": "http://hook.example.test/x",
    "another port": "https://hook.example.test:8443/x",
    "userinfo": "https://user:pw@hook.example.test/x",
    "fragment": "https://hook.example.test/x#part",
    "2049 characters": "https://hook.example.test/" + "a" * (2049 - 26),
    "a loopback literal": "https://127.0.0.1/x",
    "the metadata address": "https://169.254.169.254/latest/meta-data",
    "a private literal": "https://10.0.0.5/x",
    "v6 loopback": "https://[::1]/x",
    "ftp": "ftp://hook.example.test/x",
    "past ASCII": "https://hook.example.test/é",
    "empty": "",
}


def test_the_put_refuses_what_the_egress_rules_refuse(owner, world):
    """Q9 and S8's `check_url`: https, 443, no userinfo, no fragment, at most 2048 characters,
    no blocked literal; and the body is `{url}` and nothing else. CONTROLS: an https URL on
    443, written or not, and one of exactly 2048 characters are stored."""
    target = world["target"]
    assert len(BAD_URLS["2049 characters"]) == 2049
    with override_settings(DEBUG=False, MESHY_EGRESS_ALLOW_LOOPBACK=False):
        for name, url in BAD_URLS.items():
            response = put_webhook(target, url)
            assert response.status_code == 400, (name, response.content)
            assert json.loads(response.content)["error"] == "E_VALIDATION", name
        for name, raw in {"not JSON": b"{", "a list": b"[]", "no url": b"{}",
                          "a number": b'{"url": 5}',
                          "another key": b'{"url": "https://hook.example.test/x", "x": 1}'
                          }.items():
            response = put_webhook(target, None, raw=raw)
            assert response.status_code == 400, (name, response.content)
        assert registration(owner, target["peer_id"]) is None
        exact = "https://hook.example.test/" + "a" * (2048 - 26)
        for url in ("https://hook.example.test/x", "https://hook.example.test:443/x", exact):
            response = put_webhook(target, url)
            assert response.status_code == 202, (url, response.content)
            assert registration(owner, target["peer_id"])["url"] == url


def test_under_the_hatch_only_a_loopback_literal_may_be_http_or_another_port(owner, world):
    """The test hatch (S8) is how a recorder gets registered. It opens http and any port for a
    LOOPBACK literal only; the same URL shapes for anything else are still refused."""
    target = world["target"]
    assert put_webhook(target, "http://127.0.0.1:18999/hook").status_code == 202
    for url in ("http://hook.example.test:8080/x", "http://10.0.0.5:8080/x",
                "https://hook.example.test:8443/x", "http://[::ffff:127.0.0.1]:80/x"):
        response = put_webhook(target, url)
        assert response.status_code == 400, (url, response.content)
    assert registration(owner, target["peer_id"])["url"] == "http://127.0.0.1:18999/hook"


def test_a_delete_removes_the_registration(owner, world):
    target = world["target"]
    assert put_webhook(target, "https://hook.example.test/x").status_code == 202
    client = Client()
    auth = "Bearer " + _token(target)
    response = client.delete(ROUTE, HTTP_AUTHORIZATION=auth)
    assert response.status_code == 204, response.content
    assert registration(owner, target["peer_id"]) is None
    response = client.delete(ROUTE, HTTP_AUTHORIZATION=auth)
    assert (response.status_code, json.loads(response.content)["error"]) == (404, "E_NOT_FOUND")


def test_the_route_is_a_dpop_bound_peer_write_with_no_cookie_path(owner, world):
    """Q9: through authenticate_peer with DPoP (apps/peers/auth.py), the write boundary.
    Under enforcement a bearer token without cnf is refused, a bound one needs a proof for
    this method and path, and a proof is spent once. No cookie reaches it: without an
    Authorization header it is 401 whatever cookie is sent. GET is not a method of it."""
    target = world["target"]
    did = to_did_key(P.public_key_of(target["seed"]))
    body = json.dumps({"url": "https://hook.example.test/x"})

    def put(token, proof=None):
        extra = {"HTTP_DPOP": proof} if proof is not None else {}
        return Client().put(ROUTE, data=body, content_type="application/json",
                            HTTP_AUTHORIZATION=f"Bearer {token}", **extra)

    def outcome(response):
        return response.status_code, json.loads(response.content).get("error") \
            if response.content else None

    with override_settings(MESHY_DPOP_ENFORCE=True):
        bearer = _token(target)
        assert outcome(put(bearer, P.dpop_wire(target["seed"], "PUT", ROUTE, bearer))) == \
            (403, "E_FORBIDDEN")
        bound = _token(target, cnf=did)
        assert outcome(put(bound)) == (403, "E_FORBIDDEN"), "no proof"
        assert outcome(put(bound, P.dpop_wire(target["seed"], "POST", ROUTE, bound))) == \
            (403, "E_FORBIDDEN"), "a proof for another method"
        assert registration(owner, target["peer_id"]) is None
        proof = P.dpop_wire(target["seed"], "PUT", ROUTE, bound)
        assert put(bound, proof).status_code == 202
        assert outcome(put(bound, proof)) == (403, "E_FORBIDDEN"), "a proof spent twice"
        wrong = Client().delete(ROUTE, HTTP_AUTHORIZATION=f"Bearer {bound}",
                                HTTP_DPOP=P.dpop_wire(target["seed"], "PUT", ROUTE, bound))
        assert wrong.status_code == 403
        gone = Client().delete(ROUTE, HTTP_AUTHORIZATION=f"Bearer {bound}",
                               HTTP_DPOP=P.dpop_wire(target["seed"], "DELETE", ROUTE, bound))
        assert gone.status_code == 204
    cookie = Client()
    cookie.cookies["meshy_session"] = "an-owner-session"
    response = cookie.put(ROUTE, data=body, content_type="application/json")
    assert (response.status_code, json.loads(response.content)["error"]) == \
        (401, "E_AUTH_MISSING")
    assert Client().get(ROUTE, HTTP_AUTHORIZATION="Bearer " + _token(target)).status_code == 405


# ================================================================================= key

def test_the_webhook_key_is_created_once_by_an_exclusive_create_and_never_overwritten(
        wk, key_path):
    """Q3, 4.11 custody: generated at the first start with an exclusive create, never
    overwritten, rotated by deletion. Eight first starts racing on one path end with one key,
    and a file that is not an Ed25519 key refuses and is left as it was."""
    assert not key_path.exists()
    first = wk.ensure(key_path)
    data = key_path.read_bytes()
    assert data.startswith(b"-----BEGIN PRIVATE KEY-----"), data[:40]
    assert first.kid == P.server_kid(first.public_raw()) and len(first.kid) == 16
    again = wk.ensure(key_path)
    assert again.kid == first.kid and key_path.read_bytes() == data

    race = key_path.with_name("race.pem")
    barrier, kids, errors = threading.Barrier(8), [], []

    def start():
        barrier.wait()
        try:
            kids.append(wk.ensure(race).kid)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == [] and len(kids) == 8 and len(set(kids)) == 1, (errors, kids)
    assert sorted(p.name for p in key_path.parent.iterdir()) == sorted(
        [key_path.name, race.name]), "a temporary file was left behind"

    bad = key_path.with_name("bad.pem")
    bad.write_bytes(b"not a key")
    with pytest.raises(wk.WebhookKeyError):
        wk.ensure(bad)
    assert bad.read_bytes() == b"not a key"

    key_path.unlink()
    rotated = wk.ensure(key_path)
    assert rotated.kid != first.kid


def test_the_doorbell_key_is_published_at_its_own_jwks_path_and_never_in_the_tokens(
        wk, key_path):
    """Operator decision 2026-09-25 (the S9 review's F7, reversed): the webhook key is
    published ALONE at DOORBELL_JWKS under its own server-rule kid, and /.well-known/jwks.json
    carries the token key only, so a verifier that trusts the token JWKS cannot resolve a
    doorbell's kid. CONTROLS: before the key exists, and with no path configured, the
    doorbell set is empty and the token set unchanged; the route is GET only."""
    assert [k["kid"] for k in served_keys()] == [TOKEN_KID]
    assert doorbell_keys() == []
    key = wk.ensure(key_path)
    assert key.kid != TOKEN_KID
    assert [k["kid"] for k in served_keys()] == [TOKEN_KID]
    assert doorbell_keys() == [{"kty": "OKP", "crv": "Ed25519", "x": P.b64u(key.public_raw()),
                                "kid": key.kid, "alg": "EdDSA", "use": "sig"}]
    with override_settings(MESHY_INVOKE_WEBHOOK_KEY_PATH=None):
        assert doorbell_keys() == []
        assert [k["kid"] for k in served_keys()] == [TOKEN_KID]
    assert Client().post(DOORBELL_JWKS).status_code == 405


def test_the_token_key_is_never_the_webhook_key(wk, wh, key_path, owner, world, recorders):
    """F3: the doorbell is NEVER signed by the token-minting key. A webhook key file that holds
    the token key is refused at the worker's start and by every pass, and nothing is rung;
    the JWKS does not publish it a second time."""
    key_path.write_bytes(TOKEN_PRIVATE_PEM)
    with pytest.raises(wk.WebhookKeyError, match="token"):
        wk.ensure_configured()
    rec = recorders()
    assert put_webhook(world["target"], rec.url()).status_code == 202
    with pytest.raises(wk.WebhookKeyError, match="token"):
        run_pass(wh)
    assert rec.requests == []
    assert [k["kid"] for k in served_keys()] == [TOKEN_KID]
    assert doorbell_keys() == [], "the token key published as a doorbell key"


# ================================================================================== W1

def test_w1_one_doorbell_whose_jwt_verifies_under_the_webhook_kid_and_binds_the_body(
        wh, signing_key, owner, world, recorders):
    rec = recorders()
    url = verified(wh, owner, world, rec)
    target = world["target"]["peer_id"]
    call_id = invoke(world)
    assert rec.of_type(DOORBELL) == [], "the request thread POSTed"
    run_pass(wh)
    bells = rec.of_type(DOORBELL)
    assert len(bells) == 1, rec.requests
    seen = bells[0]
    assert (seen.method, seen.path) == ("POST", "/hook")
    assert seen.headers["content-type"] == "application/json"

    deadline = owner.execute("SELECT deadline_at FROM skill_calls WHERE call_id = %s",
                             (call_id,)).fetchone()[0]
    doc = json.loads(seen.body)
    assert doc == {"type": DOORBELL, "call_id": call_id, "target_peer_id": str(target),
                   "skill_name": world["skill"], "deadline_at": wire.optional_rfc3339(deadline)}
    assert seen.body == P.jcs(doc), "the body is sent as its RFC 8785 bytes"
    assert ARGS_MARKER.encode() not in seen.body and b"args" not in seen.body

    token = bearer_of(seen)
    assert pyjwt.get_unverified_header(token) == {"alg": "EdDSA", "kid": signing_key.kid,
                                                  "typ": "meshy-invoke+jwt"}
    assert signing_key.kid != TOKEN_KID
    claims = verify_doorbell(token, url)
    assert set(claims) == {"iss", "sub", "aud", "iat", "exp", "jti", "body_sha256"}, claims
    assert (claims["iss"], claims["sub"], claims["aud"]) == (ISSUER, str(target), url)
    assert claims["exp"] - claims["iat"] == 120 and abs(claims["iat"] - time.time()) < 60
    assert uuid.UUID(claims["jti"]).version == 4
    assert claims["body_sha256"] == hashlib.sha256(seen.body).hexdigest()

    assert receiver_accepts(token, seen.body, url)
    for i in (0, len(seen.body) // 2, len(seen.body) - 1):
        changed = bytearray(seen.body)
        changed[i] ^= 0x01
        assert not receiver_accepts(token, bytes(changed), url), i
    assert not receiver_accepts(token, seen.body, url + "x"), "aud is the exact URL"
    with pytest.raises(tokens.JwtError):
        tokens.verify_token(token, TOKEN_PUBLIC_PEM, ISSUER)


# ================================================================================== W2

def test_w2_a_2xx_records_webhook_fired_and_the_call_stays_queued(
        wh, signing_key, owner, world, recorders):
    """4.11: on a 2xx a `webhook_fired` event on the call and NOTHING else; `routed` keeps its
    one meaning (a socket was offered the call). One attempt, never retried: a second pass
    sends nothing."""
    rec = recorders()
    verified(wh, owner, world, rec)
    call_id = invoke(world)
    run_pass(wh)
    assert status_of(owner, call_id) == "queued"
    kinds = events(owner, call_id)
    assert [(k, s) for k, s, _ in kinds] == [("created", "meshy"), ("queued", "meshy"),
                                             ("webhook_fired", "meshy")], kinds
    [row] = attempts(owner, world["target"]["peer_id"])
    assert kinds[2][2] == {"attempt_id": row["id"], "http_status": 200}
    assert (row["call_id"], row["state"], row["http_status"], row["error_class"]) == (
        call_id, "sent", 200, None)
    utc = _dt.timezone.utc    # same-zone datetimes compare as wall-clock times
    assert row["claimed_by"]
    assert row["claimed_at"].astimezone(utc) <= row["finished_at"].astimezone(utc)
    run_pass(wh)
    assert len(rec.of_type(DOORBELL)) == 1


def test_a_failed_ring_is_recorded_fires_nothing_and_is_not_retried(
        wh, signing_key, owner, world, recorders):
    rec = recorders(respond=lambda s: echo(s) if _type(s) == VERIFY else (500, b"no"))
    verified(wh, owner, world, rec)
    call_id = invoke(world)
    run_pass(wh)
    run_pass(wh)
    assert len(rec.of_type(DOORBELL)) == 1
    [row] = attempts(owner, world["target"]["peer_id"])
    assert (row["state"], row["http_status"], row["error_class"]) == ("failed", 500, "status")
    assert status_of(owner, call_id) == "queued"
    assert [k for k, _s, _d in events(owner, call_id)] == ["created", "queued"]


def test_the_send_re_checks_the_url_against_the_egress_rules(
        wh, signing_key, owner, world, recorders):
    """4.11: registration-time validation is a convenience; the network check at every send
    is the guard. A verified row whose URL now names a blocked address fails with nothing
    dialed."""
    rec = recorders()
    verified(wh, owner, world, rec)
    owner.execute("UPDATE peer_invocation_webhooks SET url = 'https://10.0.0.5/hook' "
                  "WHERE peer_id = %s", (world["target"]["peer_id"],))
    invoke(world)
    run_pass(wh)
    [row] = attempts(owner, world["target"]["peer_id"])
    assert (row["state"], row["http_status"], row["error_class"]) == (
        "failed", None, "blocked_address")
    assert rec.of_type(DOORBELL) == []


def test_the_ring_re_checks_that_the_peer_still_has_a_queued_call(
        wh, signing_key, owner, recorders):
    """4.11: the worker re-checks the peer still has a `queued` call with a future deadline.
    The call that rang it gone, it rings for the peer's next queued call and records the
    event there; none left, the attempt is skipped and nothing is POSTed."""
    world = make_world(owner)
    rec = recorders()
    verified(wh, owner, world, rec)
    first, second = invoke(world), invoke(world)
    owner.execute("UPDATE skill_calls SET status = 'timeout' WHERE call_id = %s", (first,))
    run_pass(wh)
    [seen] = rec.of_type(DOORBELL)
    assert json.loads(seen.body)["call_id"] == second
    assert [k for k, _s, _d in events(owner, second)][-1] == "webhook_fired"
    assert "webhook_fired" not in [k for k, _s, _d in events(owner, first)]
    [row] = attempts(owner, world["target"]["peer_id"])
    assert (row["call_id"], row["state"]) == (first, "sent")

    lone = make_world(owner)
    lonely = recorders()
    verified(wh, owner, lone, lonely)
    call_id = invoke(lone)
    owner.execute("UPDATE skill_calls SET status = 'timeout' WHERE call_id = %s", (call_id,))
    run_pass(wh)
    [row] = attempts(owner, lone["target"]["peer_id"])
    assert (row["state"], row["error_class"]) == ("skipped", "nothing_queued")
    assert lonely.of_type(DOORBELL) == []


def test_a_suspended_target_is_not_rung(wh, signing_key, owner, world, recorders):
    """Design 4.4: a target suspended after step 9 is caught by the delivery side, which
    re-checks `active`. CONTROL: active again, its next doorbell is sent."""
    rec = recorders()
    verified(wh, owner, world, rec)
    target = world["target"]["peer_id"]
    invoke(world)
    owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (target,))
    run_pass(wh)
    [row] = attempts(owner, target)
    assert (row["state"], row["error_class"]) == ("skipped", "not_active")
    assert rec.of_type(DOORBELL) == []
    owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (target,))
    invoke(world)
    run_pass(wh)
    assert len(rec.of_type(DOORBELL)) == 1


def test_a_suspended_callers_queued_call_rings_nobody(wh, signing_key, owner, world,
                                                     recorders):
    """Operator decision 2026-09-25 (divergence 041, F23 F8): OFFERABLE's caller clause holds
    for the doorbell too. A caller suspended after step 9 leaves its call `queued`, the ring
    finds no call it may name, the attempt is skipped and nothing is POSTed; the deadline
    sweep ends the call. CONTROL: the caller active again, its next call rings."""
    rec = recorders()
    verified(wh, owner, world, rec)
    target, caller = world["target"]["peer_id"], world["caller"]["peer_id"]
    call_id = invoke(world)
    owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (caller,))
    try:
        run_pass(wh)
        [row] = attempts(owner, target)
        assert (row["state"], row["error_class"]) == ("skipped", "nothing_queued")
        assert rec.of_type(DOORBELL) == []
        assert status_of(owner, call_id) == "queued", "the sender moves no status"
    finally:
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (caller,))
    second = invoke(world)
    run_pass(wh)
    [bell] = rec.of_type(DOORBELL)
    assert json.loads(bell.body)["call_id"] in (call_id, second)


# ================================================================================== W3

def test_w3_two_workers_claim_one_attempt_and_exactly_one_posts(
        wh, signing_key, owner, world, recorders):
    """Rule B7: the claim is written BEFORE the POST, with FOR UPDATE SKIP LOCKED. Two passes
    released together against one due attempt; the receiver holds each doorbell 1 s, so a
    pass that POSTed before claiming would overlap the other."""
    rec = recorders()
    verified(wh, owner, world, rec)
    rec.delay = 1.0
    invoke(world)
    barrier, reports = threading.Barrier(2), [None, None]

    def work(i):
        barrier.wait()
        reports[i] = run_pass(wh, holder=f"w3-{i}")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(rec.of_type(DOORBELL)) == 1, rec.requests
    rings = [a for r in reports for a in r.attempts]
    assert [a.state for a in rings] == ["sent"], reports
    [row] = attempts(owner, world["target"]["peer_id"])
    assert row["state"] == "sent" and row["claimed_by"].startswith("w3-"), row


# ================================================================================== W4

def test_w4_a_worker_that_crashed_after_its_claim_is_resent_by_the_sweep(
        wh, signing_key, owner, world, recorders):
    """4.11: the sweep takes claimed rows older than 30 s (a crash between claim and send).
    CONTROL: a claim younger than that is not taken. The crashed worker's late record then
    cannot overwrite the one that sent it."""
    rec = recorders()
    verified(wh, owner, world, rec)
    invoke(world)
    conn = dbconn.connect()
    try:
        claim = wh.claim_attempt(conn, "w4-crashed")
    finally:
        conn.close()
    assert claim is not None
    [row] = attempts(owner, world["target"]["peer_id"])
    assert row["state"] == "claimed" and row["claimed_by"].startswith("w4-crashed")
    report = run_pass(wh, holder="w4-early")
    assert report.attempts == [] and rec.of_type(DOORBELL) == [], "a live claim was taken"
    owner.execute("UPDATE peer_webhook_attempts SET claimed_at = claimed_at - "
                  "interval '31 seconds' WHERE id = %s", (claim.id,))
    run_pass(wh, holder="w4-sweeper")
    assert len(rec.of_type(DOORBELL)) == 1
    [row] = attempts(owner, world["target"]["peer_id"])
    assert row["state"] == "sent" and row["claimed_by"].startswith("w4-sweeper"), row
    conn = dbconn.connect()
    try:
        assert wh.record_attempt(conn, claim, "failed", error_class="late") is False
    finally:
        conn.close()
    [row] = attempts(owner, world["target"]["peer_id"])
    assert (row["state"], row["error_class"]) == ("sent", None)


# ================================================================================== W5

def test_w5_the_calls_webhook_url_and_unread_column_are_never_contacted(
        wh, signing_key, owner, world, recorders):
    """Q10 and 5.5: `skill_calls.webhook_url` (caller-supplied) stays stored and unread, and
    `peers.invocation_delivery_webhook` is never read, because anything reading that column
    would ring it unsigned. Only the verified registration is rung. A peer with no registration
    and both other URLs set is rung nowhere."""
    registered, per_call, upstream = recorders(), recorders(), recorders()
    verified(wh, owner, world, registered)
    target = world["target"]["peer_id"]
    owner.execute("UPDATE peers SET invocation_delivery_webhook = %s WHERE id = %s",
                  (upstream.url(), target))
    call_id = invoke(world, webhook_url=per_call.url())
    assert owner.execute("SELECT webhook_url FROM skill_calls WHERE call_id = %s",
                         (call_id,)).fetchone()[0] == per_call.url()
    run_pass(wh)
    assert len(registered.of_type(DOORBELL)) == 1
    assert per_call.requests == [] and upstream.requests == []

    bare = make_world(owner)
    owner.execute("UPDATE peers SET invocation_delivery_webhook = %s WHERE id = %s",
                  (upstream.url(), bare["target"]["peer_id"]))
    invoke(bare, webhook_url=per_call.url())
    assert attempts(owner, bare["target"]["peer_id"]) == []
    run_pass(wh)
    assert per_call.requests == [] and upstream.requests == []


# ================================================================================== W6

def test_w6_an_unverified_url_is_never_rung(wh, signing_key, owner, world, recorders):
    """Proof of control before the first ring (4.11, F11). A receiver that does not echo the
    challenge stays unverified (disabled, one verification per PUT); step 9 inserts no
    attempt for it, and an attempt inserted by hand is skipped by the sender, not rung."""
    wrong = recorders(respond=lambda s: (200, b"not the challenge"))
    target = world["target"]["peer_id"]
    assert put_webhook(world["target"], wrong.url()).status_code == 202
    run_pass(wh)
    row = registration(owner, target)
    assert row["verified_at"] is None and row["disabled_at"] is not None, row
    assert len(wrong.of_type(VERIFY)) == 1
    call_id = invoke(world)
    assert attempts(owner, target) == []
    run_pass(wh)
    assert len(wrong.requests) == 1, "rung, or verified a second time"
    owner.execute("INSERT INTO peer_webhook_attempts (peer_id, call_id) VALUES (%s,%s)",
                  (target, call_id))
    run_pass(wh)
    [attempt] = attempts(owner, target)
    assert (attempt["state"], attempt["error_class"]) == ("skipped", "not_verified")
    assert len(wrong.requests) == 1


def _answer(make):
    """A receiver answering the verification with `make(challenge_bytes, seen)`."""
    def respond(seen):
        if _type(seen) != VERIFY:
            return 200, b"ok"
        return make(json.loads(seen.body)["challenge"].encode("ascii"), seen)
    return respond


NEAR_MISSES = {
    "a trailing newline": lambda c, s: (200, c + b"\n"),
    "a JSON body naming it": lambda c, s: (200, json.dumps({"challenge": c.decode()}).encode()),
    "the request body echoed": lambda c, s: (200, s.body),
    "another challenge": lambda c, s: (200, P.b64u(os.urandom(32)).encode()),
    "one character short": lambda c, s: (200, c[:-1]),
    "an empty body": lambda c, s: (200, b""),
    "a 500 carrying it": lambda c, s: (500, c),
    "a 302 carrying it": lambda c, s: (302, c),
}


def test_w6_only_an_exact_echo_of_the_challenge_verifies(
        wh, signing_key, owner, world, recorders):
    """The verification POST is `{type: invocation_webhook_verify, challenge}`, signed like a
    doorbell. `verified_at` is set only when a 2xx body is the challenge, byte for byte."""
    target = world["target"]["peer_id"]
    for name, make in NEAR_MISSES.items():
        rec = recorders(respond=_answer(make))
        assert put_webhook(world["target"], rec.url()).status_code == 202
        run_pass(wh)
        assert len(rec.of_type(VERIFY)) == 1, name
        assert registration(owner, target)["verified_at"] is None, name
        age_verification_floor(owner, target)     # one peer, re-PUT: past the review's floor

    good = recorders()
    assert put_webhook(world["target"], good.url()).status_code == 202
    challenge = registration(owner, target)["challenge"]
    run_pass(wh)
    row = registration(owner, target)
    assert row["verified_at"] is not None and row["disabled_at"] is None, row
    [seen] = good.of_type(VERIFY)
    assert json.loads(seen.body) == {"type": VERIFY, "challenge": challenge}
    assert seen.body == P.jcs({"type": VERIFY, "challenge": challenge})
    token = bearer_of(seen)
    assert pyjwt.get_unverified_header(token)["kid"] == signing_key.kid
    assert pyjwt.get_unverified_header(token)["typ"] == "meshy-invoke+jwt"
    claims = verify_doorbell(token, good.url())
    assert claims["sub"] == str(target)
    assert claims["body_sha256"] == hashlib.sha256(seen.body).hexdigest()


def test_an_echo_of_a_superseded_challenge_verifies_nothing(
        wh, signing_key, owner, world, recorders):
    """The peer PUTs again while its first verification is in flight. The first echo is of a
    challenge that no longer stands, so it verifies nothing; the second PUT's own
    verification does."""
    target = world["target"]
    state = {"reput": True}

    def respond(seen):
        if _type(seen) != VERIFY:
            return 200, b"ok"
        if state.pop("reput", False):
            assert put_webhook(target, rec.url()).status_code == 202
        return 200, json.loads(seen.body)["challenge"].encode("ascii")

    rec = recorders(respond=respond)
    assert put_webhook(target, rec.url()).status_code == 202
    first = registration(owner, target["peer_id"])["challenge"]
    run_pass(wh, limit=1)
    row = registration(owner, target["peer_id"])
    assert row["verified_at"] is None and row["challenge"] != first, row
    assert row["disabled_at"] is None, "the second PUT's verification is still owed"
    assert [v["outcome"] for v in verifications(owner, target["peer_id"])] == ["superseded"]
    age_verification_floor(owner, target["peer_id"])   # the review's floor, passed
    run_pass(wh)
    assert registration(owner, target["peer_id"])["verified_at"] is not None
    assert len(rec.of_type(VERIFY)) == 2


# ================================================================================== W7

def test_w7_two_queued_calls_ring_once_and_a_third_within_sixty_seconds_not_at_all(
        wh, signing_key, owner, world, recorders):
    """4.11: one doorbell outstanding per peer (the partial unique index, ON CONFLICT DO
    NOTHING) and none when the peer was rung in the last 60 s. CONTROL: past the floor a new
    queued call rings again."""
    rec = recorders()
    verified(wh, owner, world, rec)
    target = world["target"]["peer_id"]
    first, _second = invoke(world), invoke(world)
    assert [(a["call_id"], a["state"]) for a in attempts(owner, target)] == [(first, "due")]
    run_pass(wh)
    assert len(rec.of_type(DOORBELL)) == 1
    invoke(world)
    assert [(a["call_id"], a["state"]) for a in attempts(owner, target)] == [(first, "sent")]
    run_pass(wh)
    assert len(rec.of_type(DOORBELL)) == 1

    owner.execute("UPDATE peer_webhook_attempts SET claimed_at = claimed_at - "
                  "interval '61 seconds' WHERE peer_id = %s", (target,))
    fourth = invoke(world)
    assert [(a["call_id"], a["state"]) for a in attempts(owner, target)] == [
        (first, "sent"), (fourth, "due")]
    run_pass(wh)
    assert len(rec.of_type(DOORBELL)) == 2


# ================================================================================== W8

def test_w8_the_doorbell_is_not_a_token(wh, signing_key, owner, world, recorders):
    """F3, 5.4: the doorbell JWT does not verify under the token key, so authenticate_peer
    refuses it at the SIGNATURE (401), before any claim is read. CONTROLS: the same claims
    under the token key get past the signature and are refused only by `aud` (403), which
    is all that would stand between a token-key doorbell and a token; a real token is 200."""
    rec = recorders()
    verified(wh, owner, world, rec)
    invoke(world)
    run_pass(wh)
    [seen] = rec.of_type(DOORBELL)
    token = bearer_of(seen)
    with pytest.raises(tokens.JwtError, match="signature"):
        tokens.verify_token(token, TOKEN_PUBLIC_PEM, ISSUER)
    # Operator decision 2026-09-25: the token JWKS cannot resolve a doorbell's kid at all.
    assert pyjwt.get_unverified_header(token)["kid"] not in {k["kid"] for k in served_keys()}

    def presence(bearer):
        response = Client().get("/v1/peers/presence", HTTP_AUTHORIZATION=f"Bearer {bearer}")
        return response.status_code, json.loads(response.content).get("error")

    assert presence(token) == (401, "E_AUTH_INVALID")
    claims = pyjwt.decode(token, options={"verify_signature": False})
    forged = pyjwt.encode(claims, TOKEN_PRIVATE_PEM, algorithm="EdDSA")
    assert presence(forged) == (403, "E_FORBIDDEN")
    assert presence(_token(world["target"])) == (200, None)


# ================================================================================== W9

def test_w9_run_call_worker_listens_for_the_doorbell_and_sweeps_every_five_seconds(
        wh, wk, key_path, owner, world, recorders):
    """Q4: one process, `run_call_worker`, runs the deadline sweep and, beside it, the
    doorbell sender. Its first start creates the key. The sender LISTENs, so a queued call
    rings within 2 s, well inside one 5 s sweep; and it sweeps every 5 s, so a due row whose
    notification was lost (written with triggers off for its session) is sent within one
    sweep. The process stops when asked."""
    from apps.calls import worker
    assert (wh.SWEEP_SECS, wh.STALE_CLAIM_SECS, wh.RATE_FLOOR_SECS) == (5, 30, 60)
    assert not key_path.exists()
    rec = recorders()
    assert put_webhook(world["target"], rec.url()).status_code == 202
    stop = threading.Event()
    proc = threading.Thread(target=worker.run_process, args=(dbconn.connect,),
                            kwargs={"stop": stop}, name="w9-call-worker", daemon=True)
    proc.start()
    try:
        _wait(lambda: registration(owner, world["target"]["peer_id"])["verified_at"]
              is not None, "the first pass to verify the registration", 10)
        assert key_path.exists(), "the first start did not create the key"
        invoke(world)
        waited = _wait(lambda: len(rec.of_type(DOORBELL)) == 1, "the doorbell", 2.0)
        assert waited < 2.0

        quiet = make_world(owner)
        rec2 = recorders()
        assert put_webhook(quiet["target"], rec2.url()).status_code == 202
        _wait(lambda: registration(owner, quiet["target"]["peer_id"])["verified_at"]
              is not None, "the second registration's verification", 10)
        call_id = seed_call(owner, quiet["caller"], quiet["target"], quiet["skill_id"],
                            status="queued", call_id=f"s9-{uuid.uuid4()}")
        with psycopg.connect(DSN, autocommit=True) as silent:
            silent.execute("SET session_replication_role = replica")
            silent.execute("INSERT INTO peer_webhook_attempts (peer_id, call_id) "
                           "VALUES (%s,%s)", (quiet["target"]["peer_id"], call_id))
        _wait(lambda: len(rec2.of_type(DOORBELL)) == 1, "the sweep", wh.SWEEP_SECS * 1.2 + 1.5)
    finally:
        stop.set()
        proc.join(30)
    assert not proc.is_alive(), "the call worker did not stop"
    kid = pyjwt.get_unverified_header(bearer_of(rec.of_type(DOORBELL)[0]))["kid"]
    assert kid == wk.ensure(key_path).kid


def test_a_doorbell_configuration_that_cannot_sign_refuses_to_start(wh, wk, key_path):
    """Fail closed and typed (A4, E1): with the key path set, a process that could not sign a
    doorbell (no issuer; the token key as the webhook key) refuses to start, before its loop
    runs anything, rather than run the sweep and log the doorbells' failure every pass."""
    from apps.calls import worker

    def start() -> BaseException | None:
        """run_process in a thread: what it raised, or None if it was still running after
        3 s (then it is stopped)."""
        stop, raised = threading.Event(), []

        def run():
            try:
                worker.run_process(dbconn.connect, stop=stop)
            except BaseException as exc:  # noqa: BLE001 - the verdict
                raised.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(3)
        stop.set()
        thread.join(30)
        return raised[0] if raised else None

    with override_settings(MESHY_JWT_ISSUER=None):
        assert isinstance(start(), wh.DoorbellConfigError)
    key_path.unlink()
    key_path.write_bytes(TOKEN_PRIVATE_PEM)
    refused = start()
    assert isinstance(refused, wk.WebhookKeyError) and "token" in str(refused), refused


def test_run_call_worker_once_runs_one_doorbell_pass(wh, owner, world, recorders, key_path):
    """`--once` runs one deadline tick and one doorbell pass, creating the key at its first
    start. With no key path configured, doorbells are off and nothing is rung."""
    rec = recorders()
    assert put_webhook(world["target"], rec.url()).status_code == 202
    with override_settings(MESHY_INVOKE_WEBHOOK_KEY_PATH=None):
        out = io.StringIO()
        with pytest.raises(SystemExit) as done:
            call_command("run_call_worker", "--once", stdout=out)
        assert done.value.code == 0
        assert "doorbells off" in out.getvalue(), out.getvalue()
        assert rec.requests == []
    out = io.StringIO()
    with pytest.raises(SystemExit) as done:
        call_command("run_call_worker", "--once", stdout=out)
    assert done.value.code == 0, out.getvalue()
    assert key_path.exists()
    assert registration(owner, world["target"]["peer_id"])["verified_at"] is not None
    invoke(world)
    with pytest.raises(SystemExit) as done:
        call_command("run_call_worker", "--once", stdout=io.StringIO())
    assert done.value.code == 0
    assert len(rec.of_type(DOORBELL)) == 1



# ======================================================= review of S9 at a6bb87af (041 S9)

def verifications(owner, peer_id) -> list[dict]:
    """The verification delivery record (the review's F1 and F3): one row per verification
    POST claimed, the floor's memory and the outcome's."""
    cols = ("id", "url", "claimed_by", "claimed_at", "finished_at", "outcome", "http_status")
    rows = owner.execute(f"SELECT {', '.join(cols)} FROM peer_webhook_verifications "
                         "WHERE peer_id = %s ORDER BY id", (peer_id,)).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def age_verification_floor(owner, peer_id, secs: int = 61) -> None:
    """Move a peer's verification claims `secs` into the past, and the registration's claim
    stamp with them where it is one of those claims, so the link between the two holds."""
    owner.execute(
        "UPDATE peer_invocation_webhooks w SET disabled_at = w.disabled_at - "
        "make_interval(secs => %s) WHERE w.peer_id = %s AND EXISTS (SELECT 1 FROM "
        "peer_webhook_verifications v WHERE v.peer_id = w.peer_id "
        "AND v.claimed_at = w.disabled_at)", (secs, peer_id))
    owner.execute("UPDATE peer_webhook_verifications SET claimed_at = claimed_at - "
                  "make_interval(secs => %s) WHERE peer_id = %s", (secs, peer_id))


def test_review_f1_a_doorbell_waits_behind_at_most_one_verification(
        wh, signing_key, owner, recorders):
    """Review F1 (MEDIUM). A pass ran up to 100 verifications, 15 s each at worst, before it
    claimed its first doorbell, so unverified registrations (one peer re-PUTting, or a
    backlog at switch-on, F10) held every other peer's doorbell past a 30 s deadline. A pass
    now takes a due attempt before EACH verification. A's call is queued before the pass and
    three registrations wait; while the first is being verified, B's call is queued. A is
    rung before any verification (the doorbell comes first in a turn), and B before the
    second verification starts, not after the third."""
    early, bell = make_world(owner), make_world(owner)
    early_rec, bell_rec = recorders(), recorders()
    verified(wh, owner, early, early_rec)
    verified(wh, owner, bell, bell_rec)
    order: list[tuple] = []
    early_rec.respond = lambda s: (order.append((_type(s), "early")), (200, b"ok"))[1]
    bell_rec.respond = lambda s: (order.append((_type(s), "bell")), (200, b"ok"))[1]
    invoke(early)
    state = {"queued": False}

    def pending(tag):
        def respond(seen):
            order.append((_type(seen), tag))
            if _type(seen) == VERIFY and not state["queued"]:
                state["queued"] = True
                invoke(bell)          # B's call goes queued while this POST is in flight
            return 200, b"not the challenge"
        return respond

    for tag in ("v1", "v2", "v3"):
        world = make_world(owner)
        rec = recorders(respond=pending(tag))
        assert put_webhook(world["target"], rec.url()).status_code == 202
    report = run_pass(wh)
    assert state["queued"], "control: B's call was queued during the first verification"
    assert order == [(DOORBELL, "early"), (VERIFY, "v1"), (DOORBELL, "bell"), (VERIFY, "v2"),
                     (VERIFY, "v3")], order
    assert [r.state for r in report.attempts] == ["sent", "sent"]
    assert len(report.verifications) == 3


def test_review_f1_a_peer_is_verified_at_most_once_a_minute_whatever_it_puts(
        wh, signing_key, owner, world, recorders):
    """Review F1: every PUT reset the row and bought one signed POST to any https:443 URL,
    with no limit (the reviewer's P2: 20 PUTs, 20 POSTs). Now a peer is verified at most once
    per RATE_FLOOR_SECS, whatever it PUTs: a PUT inside the floor is stored and its
    verification DEFERRED, not refused and not sent. The floor is the peer's, kept in the
    verification record, so DELETE then PUT does not reset it. CONTROL: past the floor the
    latest PUT is verified, and each verification's outcome is recorded (F3)."""
    rec = recorders(respond=lambda s: (200, b"not the challenge"))
    target = world["target"]
    peer = target["peer_id"]
    assert put_webhook(target, rec.url()).status_code == 202
    run_pass(wh)
    assert len(rec.of_type(VERIFY)) == 1
    for _ in range(3):
        assert put_webhook(target, rec.url()).status_code == 202
        run_pass(wh)
    assert len(rec.of_type(VERIFY)) == 1, "a PUT inside the floor bought a POST"
    gone = Client().delete(ROUTE, HTTP_AUTHORIZATION="Bearer " + _token(target))
    assert gone.status_code == 204
    assert put_webhook(target, rec.url()).status_code == 202
    run_pass(wh)
    assert len(rec.of_type(VERIFY)) == 1, "DELETE then PUT reset the floor"
    row = registration(owner, peer)
    assert row["verified_at"] is None and row["disabled_at"] is None, "owed, not dropped"

    rec.respond = echo
    age_verification_floor(owner, peer)
    run_pass(wh)
    assert len(rec.of_type(VERIFY)) == 2
    assert json.loads(rec.of_type(VERIFY)[1].body)["challenge"] == row["challenge"], \
        "the latest PUT's challenge"
    assert registration(owner, peer)["verified_at"] is not None
    record = verifications(owner, peer)
    assert [(v["url"], v["outcome"], v["http_status"]) for v in record] == [
        (rec.url(), "not_echoed", 200), (rec.url(), "verified", 200)], record
    assert all(v["claimed_by"] and v["finished_at"] for v in record), record


def test_review_f2_a_failed_ring_holds_the_sixty_second_floor_too(
        wh, signing_key, owner, world, recorders):
    """Review F2: the floor counts a `failed` ring, not only a `sent` one. A verified URL
    whose DNS now names a third party that answers 5xx (the design's accepted leftover
    risk) is rung at most once a minute. CONTROL: 61 s later a queued call rings again."""
    rec = recorders(respond=lambda s: echo(s) if _type(s) == VERIFY else (503, b"down"))
    verified(wh, owner, world, rec)
    peer = world["target"]["peer_id"]
    first = invoke(world)
    run_pass(wh)
    assert [(a["call_id"], a["state"]) for a in attempts(owner, peer)] == [(first, "failed")]
    invoke(world)
    assert [(a["call_id"], a["state"]) for a in attempts(owner, peer)] == [(first, "failed")]
    run_pass(wh)
    assert len(rec.of_type(DOORBELL)) == 1
    owner.execute("UPDATE peer_webhook_attempts SET claimed_at = claimed_at - "
                  "interval '61 seconds' WHERE peer_id = %s", (peer,))
    third = invoke(world)
    assert [(a["call_id"], a["state"]) for a in attempts(owner, peer)] == [
        (first, "failed"), (third, "due")]


def test_review_f3_a_verification_claimed_and_never_finished_is_taken_again(
        wh, signing_key, owner, recorders):
    """Review F3: a verification claimed and never finished (a crash, or a restart that
    outlived the join) left the registration disabled for good. It is taken again once the
    floor has passed, as a crashed doorbell claim is. CONTROLS: not while the claim is live;
    and a verification that FINISHED unechoed is not retried, floor or no floor (one per
    PUT)."""
    world = make_world(owner)
    rec = recorders()
    peer = world["target"]["peer_id"]
    assert put_webhook(world["target"], rec.url()).status_code == 202
    conn = dbconn.connect()
    try:
        claim = wh.claim_verification(conn)
    finally:
        conn.close()
    assert claim is not None and registration(owner, peer)["disabled_at"] is not None
    run_pass(wh)
    assert rec.requests == [], "a live verification claim was taken"
    age_verification_floor(owner, peer)
    run_pass(wh)
    assert len(rec.of_type(VERIFY)) == 1
    row = registration(owner, peer)
    assert row["verified_at"] is not None and row["disabled_at"] is None, row
    record = verifications(owner, peer)
    assert [(v["claimed_by"] == claim.token, v["outcome"]) for v in record] == [
        (True, "abandoned"), (False, "verified")], record

    failed = make_world(owner)
    wrong = recorders(respond=lambda s: (200, b"not the challenge"))
    assert put_webhook(failed["target"], wrong.url()).status_code == 202
    run_pass(wh)
    age_verification_floor(owner, failed["target"]["peer_id"])
    run_pass(wh)
    assert len(wrong.requests) == 1, "a finished verification was retried"


def test_review_f3_a_stopping_sender_starts_no_new_post(wh, signing_key, owner, recorders):
    """Review F3: a pass never looked at `stop`, so a worker asked to stop kept verifying
    and ringing until the pass ran out, and `run_process` abandoned the thread 30 s later,
    possibly mid-POST. The sender now finishes the POST in flight and starts no other."""
    stop = threading.Event()
    first, second = make_world(owner), make_world(owner)

    def stopping(seen):
        stop.set()                    # asked to stop while this verification is in flight
        return echo(seen)

    rec1, rec2 = recorders(respond=stopping), recorders()
    assert put_webhook(first["target"], rec1.url()).status_code == 202
    assert put_webhook(second["target"], rec2.url()).status_code == 202
    sender = threading.Thread(target=wh.run_sender, args=(dbconn.connect,),
                              kwargs={"stop": stop}, daemon=True)
    sender.start()
    sender.join(30)
    assert not sender.is_alive()
    assert len(rec1.of_type(VERIFY)) == 1
    assert registration(owner, first["target"]["peer_id"])["verified_at"] is not None, \
        "the POST in flight is finished and recorded"
    assert rec2.requests == [], "a stopping sender started another POST"
    row = registration(owner, second["target"]["peer_id"])
    assert row["verified_at"] is None and row["disabled_at"] is None, row


def test_review_f6_r1_a_queued_call_past_its_deadline_is_not_rung(
        wh, signing_key, owner, world, recorders):
    """Review F6, R1: the re-check reads a `queued` call with a FUTURE deadline. A call still
    `queued` (the sweep has not reached it) whose deadline has passed is not rung."""
    rec = recorders()
    verified(wh, owner, world, rec)
    call_id = invoke(world)
    owner.execute("UPDATE skill_calls SET deadline_at = now() - interval '1 second' "
                  "WHERE call_id = %s", (call_id,))
    run_pass(wh)
    [row] = attempts(owner, world["target"]["peer_id"])
    assert (row["state"], row["error_class"]) == ("skipped", "nothing_queued")
    assert rec.of_type(DOORBELL) == []
    assert status_of(owner, call_id) == "queued", "the sender moves no status"


def test_review_f6_r4_a_stale_claim_cannot_record_over_the_claim_that_took_it(
        wh, signing_key, owner, world, recorders):
    """Review F6, R4: W4's late record was refused by `state = 'claimed'` alone, because the
    sweeper had already written `sent`. Here both claims are still `claimed`: the stale one's
    record is refused on `claimed_by`, and the row stays the sweeper's. CONTROL: the
    sweeper's own record lands."""
    rec = recorders()
    verified(wh, owner, world, rec)
    invoke(world)
    peer = world["target"]["peer_id"]
    conn = dbconn.connect()
    try:
        stale = wh.claim_attempt(conn, "r4-crashed")
        owner.execute("UPDATE peer_webhook_attempts SET claimed_at = claimed_at - "
                      "interval '31 seconds' WHERE id = %s", (stale.id,))
        taken = wh.claim_attempt(conn, "r4-sweeper")
        assert taken is not None and taken.id == stale.id and taken.token != stale.token
        assert wh.record_attempt(conn, stale, "failed", error_class="late") is False
        [row] = attempts(owner, peer)
        assert (row["state"], row["claimed_by"], row["error_class"]) == (
            "claimed", taken.token, None), row
        assert wh.record_attempt(conn, taken, "skipped", error_class="control") is True
    finally:
        conn.close()
    [row] = attempts(owner, peer)
    assert (row["state"], row["error_class"]) == ("skipped", "control")
    assert rec.of_type(DOORBELL) == []


def test_review_f6_r7_a_verification_another_worker_holds_is_skipped_not_sent_twice(
        wh, signing_key, owner, world, recorders):
    """Review F6, R7: no test ran two verifications at once. Another worker's claim is held
    open (the row locked, not yet committed) while a pass runs: the pass SKIPS the row at
    once rather than waiting on it, and after that worker commits, nothing verifies the
    registration a second time."""
    rec = recorders()
    peer = world["target"]["peer_id"]
    assert put_webhook(world["target"], rec.url()).status_code == 202
    other, reports = dbconn.connect(), []
    pass_thread = threading.Thread(
        target=lambda: reports.append(run_pass(wh, holder="r7")), daemon=True)
    try:
        with other.transaction():
            other.execute("SELECT set_config('app.server_internal', '1', true)")
            held = other.execute(
                "UPDATE peer_invocation_webhooks SET disabled_at = clock_timestamp() "
                "WHERE peer_id = %s AND verified_at IS NULL AND disabled_at IS NULL "
                "RETURNING peer_id", (peer,)).fetchone()
            assert held is not None, "control: the other worker holds the claim"
            pass_thread.start()
            pass_thread.join(5)
            skipped_while_held = not pass_thread.is_alive()
        pass_thread.join(30)
    finally:
        other.close()
    assert skipped_while_held, "the pass waited on another worker's claim"
    assert rec.requests == [], "a registration another worker claimed was verified again"
    assert reports and reports[0].verifications == [], reports
