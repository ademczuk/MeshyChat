"""Grants and access requests, proved against the real database and the real routing table.

WHAT MAKES A TEST HERE COUNT

1. EVERY NEGATIVE ASSERTION HAS A POSITIVE CONTROL IN THE SAME TEST. A 403 is also
   what a missing fixture, an unseeded key, a typo in a path or a dead connection
   produces, so "it was refused" proves nothing on its own. Where the control is the
   interesting half it runs FIRST, so a failure reads as "the happy path broke" rather
   than "the guard fired". Two of these controls are not decoration: the
   revoked-grant test passes trivially if the grant never authorised in the first
   place, and the wrong-party tests pass trivially if approve is broken for everyone.

2. NOTHING HERE OVERRIDES ROOT_URLCONF. The previous round pinned each app's own
   urlconf in its tests, so every route test passed while the application served 404.
   These resolve and fetch through `meshychat.urls`, and
   `test_the_routes_are_reachable_through_the_project_urlconf` asserts that directly
   with a negative control that the resolver CAN fail.

3. NO DATABASE MOCKS. Every row goes to the migrated Postgres named by MESHY_TEST_DSN.
   Fixtures create their own owners and peers and delete them afterwards, in FK order
   (`skill_access_requests.decided_by` has no ON DELETE clause, so a cascade from
   dashboard_users would be REFUSED rather than silently cleaning up).

Run: .venv/Scripts/python.exe -m pytest tests/test_grants.py -q
"""
from __future__ import annotations

import datetime as _dt
import hashlib
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
os.environ.setdefault("MESHY_DSN", DSN)
# The project's own settings module - NOT a test-local one. ROOT_URLCONF must come
# from meshychat.settings, because the routing table is part of what is under test.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

ISSUER = "meshy-grants-test-issuer"

_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("ascii")

from django.conf import settings as dj_settings  # noqa: E402

# No `configure()` fallback here on purpose. Every other module in this suite that
# needs one supplies its OWN ROOT_URLCONF, and whichever module configures Django first
# wins silently. This file must run against meshychat.settings or not at all, so the
# settings module is named in the environment above and `django.setup()` is the only
# path; `test_the_routes_are_reachable_through_the_project_urlconf` asserts the result.
import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.urls import resolve  # noqa: E402
from django.urls.exceptions import Resolver404  # noqa: E402

from apps.grants import db as gdb  # noqa: E402
from apps.grants import config as gconfig  # noqa: E402
from apps.grants import manifest as gmanifest  # noqa: E402
from apps.grants import owner_auth, repository, service, wire  # noqa: E402


# --------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def grants_settings():
    """Layered with override_settings so this file behaves the same alone or after any
    other module in the suite (whichever configures Django first wins, and the loser's
    configure() is skipped SILENTLY). ROOT_URLCONF is deliberately NOT in this list."""
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_DSN=DSN,
        # Parsed once at boot now (B09), so deleting the variable below no longer resets
        # it: the documented default is set here instead.
        MESHY_MANIFEST_ATTESTATION_ENFORCE=False,
    ):
        yield


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Every flag this surface reads, forced to its documented default, so a value left
    in the developer's environment cannot change what these tests measure."""
    for name in ("MESHYCHAT_MANDATE_REQUIRE_AT_ISSUANCE", "MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE",
                 "MESHYCHAT_BISCUIT_ROOT_SK", "MESHYCHAT_ALLOW_DEV_JWT", "MESHYCHAT_STATE_SECRET",
                 "MESHYCHAT_SESSION_COOKIE_NAME"):
        monkeypatch.delenv(name, raising=False)


class World:
    """Two owners, each with a peer, plus a third owner who is party to nothing."""

    def __init__(self, conn):
        self.conn = conn
        self.owner_a = self._owner("a")
        self.owner_b = self._owner("b")
        self.owner_c = self._owner("c")
        self.peer_a = self._peer(self.owner_a, "peer-a")
        self.peer_b = self._peer(self.owner_b, "peer-b")
        self.api_keys: list[uuid.UUID] = []

    def _owner(self, tag: str) -> uuid.UUID:
        oid = uuid.uuid4()
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO dashboard_users (id, email) VALUES (%s, %s)",
                        (str(oid), f"grants-test-{tag}-{oid}@example.test"))
        return oid

    def _peer(self, owner: uuid.UUID, name: str) -> uuid.UUID:
        pid = uuid.uuid4()
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s, %s, %s)",
                        (str(pid), str(owner), f"{name}-{pid}"))
        return pid

    def issue_pat(self, owner: uuid.UUID, scopes) -> str:
        raw = f"meshypat_{uuid.uuid4().hex}{uuid.uuid4().hex}"
        kid = uuid.uuid4()
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO owner_api_keys (id, owner_id, name, token_hash, token_prefix, "
                "scopes, created_at) VALUES (%s,%s,%s,%s,%s,%s, NOW())",
                (str(kid), str(owner), "test-key", owner_auth.sha256_hex(raw), raw[:12],
                 list(scopes)))
        self.api_keys.append(kid)
        return raw

    def revoke_pat(self, raw: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE owner_api_keys SET revoked_at = NOW() WHERE token_hash = %s",
                        (owner_auth.sha256_hex(raw),))

    def cleanup(self) -> None:
        owners = [str(self.owner_a), str(self.owner_b), str(self.owner_c)]
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM skill_access_requests WHERE requester_owner_id = ANY(%s) "
                        "OR granter_owner_id = ANY(%s) OR decided_by = ANY(%s)",
                        (owners, owners, owners))
            cur.execute("DELETE FROM skill_grants WHERE grantor_owner_id = ANY(%s) "
                        "OR grantee_owner_id = ANY(%s) OR revoked_by = ANY(%s)",
                        (owners, owners, owners))
            cur.execute("DELETE FROM owner_api_keys WHERE owner_id = ANY(%s)", (owners,))
            cur.execute("DELETE FROM owner_human_keys WHERE owner_id = ANY(%s)", (owners,))
            cur.execute("DELETE FROM peers WHERE owner_id = ANY(%s)", (owners,))
            cur.execute("DELETE FROM dashboard_users WHERE id = ANY(%s)", (owners,))


@pytest.fixture()
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def world(conn):
    w = World(conn)
    try:
        yield w
    finally:
        w.cleanup()


@pytest.fixture()
def client():
    return Client()


def token_for(owner_id, *, aud="dashboard", ttl=3600, scopes=()) -> str:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims = {"sub": str(owner_id), "iss": ISSUER, "iat": now, "exp": now + ttl,
              "scopes": list(scopes), "aud": aud}
    return pyjwt.encode(claims, JWT_PRIVATE_PEM, algorithm="EdDSA")


def bearer(owner_id, **kw) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token_for(owner_id, **kw)}"}


def post_json(client, path, body, headers):
    return client.post(path, data=json.dumps(body), content_type="application/json", **headers)


def iso(dt: _dt.datetime) -> str:
    return wire.optional_rfc3339(dt)


def now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def issue_grant_via_api(client, world, *, grantor, grantee, peer, skill, expires_at=None,
                        source="direct", extra=None):
    body = {"grantee_owner_id": str(grantee), "granter_peer_id": str(peer),
            "skill_name": skill, "source": source}
    if expires_at is not None:
        body["expires_at"] = iso(expires_at)
    if extra:
        body.update(extra)
    return post_json(client, "/v1/grants", body, bearer(grantor))


# ------------------------------------------------------- wiring + instrument controls

def test_the_routes_are_reachable_through_the_project_urlconf():
    """Resolves against meshychat.urls - no fixture here supplies a URLconf."""
    assert dj_settings.ROOT_URLCONF == "meshychat.urls", (
        "another module re-pointed ROOT_URLCONF; these tests would then prove nothing "
        "about the real routing table")
    expected = {
        "/v1/grants": "grants-issue",
        "/v1/grants/issued": "grants-issued",
        "/v1/grants/held": "grants-held",
        "/v1/grants/11111111-1111-1111-1111-111111111111": "grants-item",
        "/v1/access-requests": "access-requests-submit",
        "/v1/access-requests/sent": "access-requests-sent",
        "/v1/access-requests/received": "access-requests-received",
        "/v1/access-requests/11111111-1111-1111-1111-111111111111/approve":
            "access-requests-approve",
        "/v1/access-requests/11111111-1111-1111-1111-111111111111/deny": "access-requests-deny",
        "/v1/access-requests/11111111-1111-1111-1111-111111111111/withdraw":
            "access-requests-withdraw",
    }
    for path, name in expected.items():
        assert resolve(path).url_name == name, f"{path} resolved to the wrong view"
    # Positive control: resolve() must be capable of FAILING, or "it resolved" is empty.
    with pytest.raises(Resolver404):
        resolve("/v1/grants-not-a-route")


def test_static_segments_win_over_the_id_pattern():
    """`/v1/grants/issued` must NOT be captured by `<str:grant_id>`. Django takes the
    first match, so this is about declaration ORDER; a static segment must therefore
    appear before the id pattern."""
    assert resolve("/v1/grants/issued").url_name == "grants-issued"
    assert resolve("/v1/grants/held").url_name == "grants-held"
    assert resolve("/v1/grants/not-a-uuid").url_name == "grants-item"


def test_manifest_hash_matches_the_known_vector():
    """The expected hash is a recorded known answer for a named fixture, held in
    `apps/grants/manifest.py`. A hash that is merely self-consistent would brick every pinned
    grant at the invoke gate with no visible cause."""
    fixture = {
        "peer_id": str(uuid.UUID(int=0x11)),
        "name": "summarize", "version": "1.0.0", "mode": "sync",
        "description": "Summarize a document",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "output_schema": {"type": "string"},
        "attachments_allowed": False, "returns_attachments": False,
        "requires_human_auth": False,
        "tags": ["nlp", "text"], "runtime_profile": None,
    }
    assert gmanifest.manifest_hash(fixture).hex() == (
        "1b7afe0f1858394e4b4a21d4041fbf6267091901b4c7de80cbae946487d4c8fa")
    # Negative control: the hash must actually MOVE when a covered field drifts,
    # otherwise the assertion above would pass for a constant function.
    drifted = dict(fixture, description="Now it also exfiltrates your data")
    assert gmanifest.manifest_hash(drifted).hex() != gmanifest.manifest_hash(fixture).hex()
    # The exclusion law is not asserted here.
    # It used to be, by adding `price_cents` to this dict, which could not fail: the hash
    # reads a fixed key list. test_the_manifest_exclusion_law_on_a_real_skills_row
    # (STATUS C44) holds it on a real `skills` row instead.


def test_route_scoped_tables_are_still_rls_free(conn):
    """The assumption repository.Empty rests on: a zero-row read on these tables means
    'no data', not 'no context bound'."""
    gdb.assert_route_scoped_tables_are_not_rls_bound(conn)
    # Positive control: the checker must be able to FAIL. shared_memory IS RLS-bound
    # (apps/core/rls.py), so pointing the same check at it must raise.
    with pytest.raises(gdb.RlsAssumptionBroken):
        gdb.assert_route_scoped_tables_are_not_rls_bound(conn, ("shared_memory",))


# test_the_authorize_lookup_plans_without_an_aggregate lived here. It EXPLAINed a SQL
# string retyped inside the test, not the one find_active_grant runs (STATUS C44), and is
# replaced by test_the_authorize_lookup_it_runs_is_an_index_probe below.


def test_a_malformed_expires_at_is_422_with_the_parsers_text(client, world):
    """views.py `_ts_field` wraps wire.parse_rfc3339's ValueError as a 422 whose message
    is "invalid value for `expires_at`: " plus the parser's own words. That text reaches
    the client, so it is pinned here end to end now that the parser's body is
    apps/peers/timefmt.py's (tests/test_rfc3339_one_rule.py pins the parser's half)."""
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="summarize",
                            extra={"expires_at": "yesterday"})
    assert r.status_code == 422, r.content
    body = json.loads(r.content)
    assert body["error"] == "E_VALIDATION"
    assert body["message"] == \
        "invalid value for `expires_at`: not an RFC 3339 timestamp: 'yesterday'"
    # A non-string takes the wrapper's other text, ahead of any matching.
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="summarize", extra={"expires_at": 7})
    assert r.status_code == 422, r.content
    assert json.loads(r.content)["message"] == \
        "invalid value for `expires_at`: expected an RFC 3339 timestamp string"


# ------------------------------------------------------------ THE REVOCATION TEST

def test_a_revoked_grant_stops_authorising(client, conn, world):
    """The assignment's first case. The POSITIVE CONTROL is the whole test: without it,
    a lookup that never matched anything would pass the 'no longer authorises' half."""
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="summarize")
    assert r.status_code == 201, r.content
    grant_id = json.loads(r.content)["id"]

    # CONTROL: it authorises BEFORE revocation.
    before = service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                         world.peer_a, "summarize")
    assert isinstance(before, repository.Found), before
    assert str(before.value.id) == grant_id

    d = client.delete(f"/v1/grants/{grant_id}?reason=no+longer+needed", **bearer(world.owner_a))
    assert d.status_code == 200, d.content
    revoked = json.loads(d.content)
    assert revoked["revoked_at"] is not None
    assert revoked["revoked_by"] == str(world.owner_a)
    assert revoked["revoke_reason"] == "no longer needed"

    after = service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                        world.peer_a, "summarize")
    assert isinstance(after, repository.Empty), f"a revoked grant still authorises: {after}"

    # The ROW still exists: the lookup filters CAPABILITY, it does not delete HISTORY.
    # Without this, a lookup that worked by deleting the row would also pass above.
    assert isinstance(repository.find_grant_by_id(conn, grant_id), repository.Found)


def test_an_expired_grant_does_not_authorise(client, conn, world):
    """Both arms of the liveness predicate matter: an expired grant has revoked_at NULL,
    so filtering on revocation alone would still authorise it."""
    # CONTROL first: an otherwise-identical grant with a FUTURE expiry authorises.
    ok = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                             peer=world.peer_a, skill="live-skill",
                             expires_at=now() + _dt.timedelta(hours=1))
    assert ok.status_code == 201, ok.content
    live = service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                       world.peer_a, "live-skill")
    assert isinstance(live, repository.Found)

    past = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                               peer=world.peer_a, skill="stale-skill",
                               expires_at=now() - _dt.timedelta(seconds=30))
    assert past.status_code == 201, past.content
    stale_id = json.loads(past.content)["id"]

    dead = service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                       world.peer_a, "stale-skill")
    assert isinstance(dead, repository.Empty), f"an expired grant still authorises: {dead}"
    # It is EXPIRED, not revoked - so the Empty above came from the expiry arm.
    row = repository.find_grant_by_id(conn, stale_id).value
    assert row.revoked_at is None
    assert row.expires_at < now()


def test_a_grants_request_on_an_rls_bypassing_connection_is_refused(
        client, world, monkeypatch):
    """The project's serving-connection guard must cover this surface too (review queue:
    `apps/grants/db.py` never crossed `dbconn.assert_serving_connection`, unlike files,
    a2a and calls). The route is handed an autocommit connection left on the bootstrap
    role, which bypasses row-level security, and must REFUSE rather than serve on it.

    Controls: the unpatched route answers 200 and lists the grant; the injected
    connection really bypasses RLS; and the patch was reached, so a route that stopped
    taking its connection from `dbconn.connect` fails naming that.
    """
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="guarded")
    grant_id = json.loads(r.content)["id"]
    ok = client.get("/v1/grants/issued", **bearer(world.owner_a))
    assert ok.status_code == 200, ok.content
    assert grant_id in [g["id"] for g in json.loads(ok.content)], "CONTROL FAILED"

    with psycopg.connect(DSN, autocommit=True) as probe, probe.cursor() as cur:
        cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone()[0] is True, "the injected connection would not bypass RLS"

    opened = []

    def bypassing_connect(target=None, **kwargs):
        conn = psycopg.connect(target or DSN, autocommit=True)
        opened.append(conn)
        return conn

    monkeypatch.setattr("apps.core.dbconn.connect", bypassing_connect)
    refused = client.get("/v1/grants/issued", **bearer(world.owner_a))
    assert opened, "the grants route did not take its connection from dbconn.connect"
    assert refused.status_code == 500, (
        "a grants request was SERVED on a connection that bypasses row-level security: "
        f"{refused.status_code} {refused.content[:200]!r}")
    assert grant_id not in refused.content.decode("utf-8")


def test_the_grants_surface_has_no_owner_extractor_of_its_own(client, world):
    """REFACTOR-QUEUE-2026-11 B1. apps/grants/owner_auth.py was a step-for-step copy of the dashboard extractor;
    it now binds the dashboard's names, so the two cannot drift again (the cookie name, the CSRF secret and the JWT
    verifier each drifted before being folded back). The copy's one wire difference goes with it: a dev JWT whose
    `sub` is not a uuid says "token subject is not a uuid" here too, where the copy said "unknown owner"."""
    from apps.dashboard import auth as dash_auth
    assert owner_auth.authenticate_owner is dash_auth.authenticate_owner
    assert owner_auth.AuthenticatedOwner is dash_auth.AuthenticatedOwner
    assert owner_auth._verify_dashboard_jwt is dash_auth.verify_dashboard_jwt
    assert owner_auth.sha256_hex is dash_auth._sha256_hex
    assert owner_auth._bearer is dash_auth._bearer
    assert client.get("/v1/grants/issued", **bearer(world.owner_a)).status_code == 200   # control
    refused = client.get("/v1/grants/issued", **bearer("not-a-uuid"))
    assert refused.status_code == 401, refused.content
    body = json.loads(refused.content)
    assert body["error"] == "E_AUTH_INVALID", body
    assert "token subject is not a uuid" in body["message"], body


def test_expired_and_revoked_grants_are_still_listed_as_history(client, world):
    """`include_revoked` is the only thing that changes what /issued returns, and it
    must be exact `true`/`false`, not any truthy string."""
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="listed")
    grant_id = json.loads(r.content)["id"]
    client.delete(f"/v1/grants/{grant_id}", **bearer(world.owner_a))

    default = client.get("/v1/grants/issued", **bearer(world.owner_a))
    assert default.status_code == 200
    assert [g["id"] for g in json.loads(default.content)] == []

    # CONTROL: the row is there, and asking for it returns it - so the empty list above
    # is a filter doing its job, not a broken query or the wrong owner.
    withrev = client.get("/v1/grants/issued?include_revoked=true", **bearer(world.owner_a))
    assert [g["id"] for g in json.loads(withrev.content)] == [grant_id]

    bad = client.get("/v1/grants/issued?include_revoked=1", **bearer(world.owner_a))
    assert bad.status_code == 400
    assert json.loads(bad.content)["error"] == "E_VALIDATION"


def test_held_and_issued_are_scoped_to_the_authenticated_owner(client, world):
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="scoped")
    grant_id = json.loads(r.content)["id"]

    issued_a = json.loads(client.get("/v1/grants/issued", **bearer(world.owner_a)).content)
    held_b = json.loads(client.get("/v1/grants/held", **bearer(world.owner_b)).content)
    assert [g["id"] for g in issued_a] == [grant_id]
    assert [g["id"] for g in held_b] == [grant_id]

    # The mirror image is EMPTY - and the two assertions above are the control that an
    # empty list here means scoping rather than an empty table.
    assert json.loads(client.get("/v1/grants/held", **bearer(world.owner_a)).content) == []
    assert json.loads(client.get("/v1/grants/issued", **bearer(world.owner_b)).content) == []


def test_only_the_grantor_can_revoke(client, world):
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="revoke-me")
    grant_id = json.loads(r.content)["id"]

    # The GRANTEE is not the grantor: 403, and the grant is untouched.
    denied = client.delete(f"/v1/grants/{grant_id}", **bearer(world.owner_b))
    assert denied.status_code == 403
    assert json.loads(denied.content)["error"] == "E_FORBIDDEN"

    # CONTROL: the grantor succeeds on the identical call.
    ok = client.delete(f"/v1/grants/{grant_id}", **bearer(world.owner_a))
    assert ok.status_code == 200, ok.content

    # And a second revoke is a 409 with the fixed conflict text.
    again = client.delete(f"/v1/grants/{grant_id}", **bearer(world.owner_a))
    assert again.status_code == 409
    assert json.loads(again.content) == {"error": "E_CONFLICT",
                                         "message": "grant already revoked"}


def test_a_grant_on_a_peer_you_do_not_own_is_refused(client, world):
    """spec 6.5 / the IG4 DB trigger: a root issuance must be on the grantor's OWN peer."""
    bad = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                              peer=world.peer_b, skill="not-mine")
    assert bad.status_code == 403
    assert json.loads(bad.content)["error"] == "E_FORBIDDEN"
    # CONTROL: the same call on a peer owner_a DOES own is a 201.
    ok = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                             peer=world.peer_a, skill="not-mine")
    assert ok.status_code == 201, ok.content


def test_a_duplicate_active_grant_is_a_conflict(client, world):
    first = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                peer=world.peer_a, skill="dup")
    assert first.status_code == 201, first.content
    second = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                 peer=world.peer_a, skill="dup")
    assert second.status_code == 409
    assert json.loads(second.content)["message"] == (
        "active grant already exists for (grantor, grantee, peer, skill_name)")
    # CONTROL: the uniqueness is on the four-column key, so a different skill is fine -
    # the 409 above is the index doing its job, not issuance being broken.
    other = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                peer=world.peer_a, skill="dup-2")
    assert other.status_code == 201, other.content


# ---------------------------------------------------------------- access requests

def submit_request(client, world, *, requester, granter_owner, granter_peer, skill,
                   message=None, expires_at=None):
    body = {"granter_owner_id": str(granter_owner), "granter_peer_id": str(granter_peer),
            "skill_name": skill}
    if message is not None:
        body["message"] = message
    if expires_at is not None:
        body["expires_at"] = iso(expires_at)
    return post_json(client, "/v1/access-requests", body, bearer(requester))


def test_approve_is_refused_from_the_wrong_party(client, conn, world):
    """The assignment's third case. Owner B requests access to owner A's peer; only
    owner A may approve."""
    r = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                       granter_peer=world.peer_a, skill="approve-me")
    assert r.status_code == 201, r.content
    req = json.loads(r.content)
    rid = req["id"]
    assert req["status"] == "pending"

    for wrong_party, label in ((world.owner_b, "the requester"), (world.owner_c, "a stranger")):
        resp = post_json(client, f"/v1/access-requests/{rid}/approve", {}, bearer(wrong_party))
        assert resp.status_code == 403, f"{label} approved the request: {resp.content}"
        assert json.loads(resp.content)["error"] == "E_FORBIDDEN"

    # The refusals above left the request PENDING - so the 200 below is not just "the
    # last call wins".
    assert repository.find_request_by_id(conn, rid).value.status == "pending"

    # CONTROL: the granter succeeds on the identical call, and it mints the grant.
    ok = post_json(client, f"/v1/access-requests/{rid}/approve", {"note": "sure"},
                   bearer(world.owner_a))
    assert ok.status_code == 200, ok.content
    payload = json.loads(ok.content)
    assert payload["request"]["status"] == "approved"
    assert payload["request"]["decided_by"] == str(world.owner_a)
    assert payload["request"]["decision_note"] == "sure"
    assert payload["request"]["resulting_grant_id"] == payload["grant"]["id"]
    grant = payload["grant"]
    assert grant["source"] == "approved_request"
    assert grant["source_ref"] == rid
    assert grant["grantor_owner_id"] == str(world.owner_a)
    assert grant["grantee_owner_id"] == str(world.owner_b)
    # TASK-4: requester-supplied scopes never ride into approval, and approval-derived
    # grants are uncapped and unexpiring.
    assert grant["scopes"] is None
    assert grant["max_executions"] is None
    assert grant["expires_at"] is None

    # And the minted grant actually authorises.
    live = service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                       world.peer_a, "approve-me")
    assert isinstance(live, repository.Found)


def test_deny_is_refused_from_the_wrong_party(client, conn, world):
    r = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                       granter_peer=world.peer_a, skill="deny-me")
    rid = json.loads(r.content)["id"]

    wrong = post_json(client, f"/v1/access-requests/{rid}/deny", {}, bearer(world.owner_b))
    assert wrong.status_code == 403
    assert repository.find_request_by_id(conn, rid).value.status == "pending"

    # CONTROL: the granter's identical call succeeds.
    ok = post_json(client, f"/v1/access-requests/{rid}/deny", {"note": "no"},
                   bearer(world.owner_a))
    assert ok.status_code == 200, ok.content
    assert json.loads(ok.content)["status"] == "denied"
    # A denied request mints NOTHING.
    assert json.loads(ok.content)["resulting_grant_id"] is None
    assert isinstance(service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                                  world.peer_a, "deny-me"), repository.Empty)


def test_withdraw_is_the_requesters_move_not_the_granters(client, conn, world):
    """The mirror of approve/deny: here it is the GRANTER who is the wrong party."""
    r = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                       granter_peer=world.peer_a, skill="withdraw-me")
    rid = json.loads(r.content)["id"]

    wrong = client.post(f"/v1/access-requests/{rid}/withdraw", **bearer(world.owner_a))
    assert wrong.status_code == 403, wrong.content
    assert repository.find_request_by_id(conn, rid).value.status == "pending"

    # CONTROL: the requester's identical call succeeds.
    ok = client.post(f"/v1/access-requests/{rid}/withdraw", **bearer(world.owner_b))
    assert ok.status_code == 200, ok.content
    assert json.loads(ok.content)["status"] == "withdrawn"


def test_a_decided_request_cannot_be_decided_again(client, world):
    r = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                       granter_peer=world.peer_a, skill="once")
    rid = json.loads(r.content)["id"]

    first = post_json(client, f"/v1/access-requests/{rid}/deny", {}, bearer(world.owner_a))
    assert first.status_code == 200, first.content

    again = post_json(client, f"/v1/access-requests/{rid}/approve", {}, bearer(world.owner_a))
    assert again.status_code == 409
    assert json.loads(again.content) == {
        "error": "E_CONFLICT", "message": "request status is Denied; expected pending"}


def test_an_expired_request_cannot_be_approved_and_is_recorded_expired(client, conn, world):
    """A pending request past its deadline is flipped to `expired` DURABLY and then
    refused - the stored state must reflect reality, not just the answer."""
    r = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                       granter_peer=world.peer_a, skill="too-late",
                       expires_at=now() - _dt.timedelta(minutes=1))
    assert r.status_code == 201, r.content
    rid = json.loads(r.content)["id"]
    assert repository.find_request_by_id(conn, rid).value.status == "pending"

    resp = post_json(client, f"/v1/access-requests/{rid}/approve", {}, bearer(world.owner_a))
    assert resp.status_code == 409
    assert json.loads(resp.content)["message"] == "request status is Expired; expected pending"
    assert repository.find_request_by_id(conn, rid).value.status == "expired"
    # No grant was minted by the refused approval.
    assert isinstance(service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                                  world.peer_a, "too-late"), repository.Empty)

    # CONTROL: an identical request that has NOT expired approves cleanly, so the 409
    # above is the deadline and not a broken approve path.
    ok_req = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                            granter_peer=world.peer_a, skill="in-time",
                            expires_at=now() + _dt.timedelta(hours=1))
    ok_rid = json.loads(ok_req.content)["id"]
    ok = post_json(client, f"/v1/access-requests/{ok_rid}/approve", {}, bearer(world.owner_a))
    assert ok.status_code == 200, ok.content


def test_a_second_pending_request_for_the_same_skill_is_a_conflict(client, world):
    first = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                           granter_peer=world.peer_a, skill="twice")
    assert first.status_code == 201, first.content
    second = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                            granter_peer=world.peer_a, skill="twice")
    assert second.status_code == 409
    assert json.loads(second.content)["message"] == (
        "pending request already exists for this (requester, peer, skill)")
    # CONTROL: the partial index is scoped to (requester, peer, skill) WHERE pending -
    # a different skill still works, so the 409 is the index, not a broken submit.
    other = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                           granter_peer=world.peer_a, skill="twice-b")
    assert other.status_code == 201, other.content


def test_a_request_naming_a_peer_the_granter_does_not_own_is_404(client, world):
    """Pairing granter_owner_id=A with a peer owned by B would let A's approval persist
    an inconsistent grant. The request is not found, not forbidden."""
    bad = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                         granter_peer=world.peer_b, skill="mismatch")
    assert bad.status_code == 404
    assert json.loads(bad.content)["error"] == "E_NOT_FOUND"
    # CONTROL: the correctly-paired request succeeds.
    ok = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                        granter_peer=world.peer_a, skill="mismatch")
    assert ok.status_code == 201, ok.content


def test_sent_and_received_are_two_different_views_of_the_same_row(client, world):
    r = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                       granter_peer=world.peer_a, skill="both-views", message="please")
    rid = json.loads(r.content)["id"]

    sent = json.loads(client.get("/v1/access-requests/sent", **bearer(world.owner_b)).content)
    received = json.loads(
        client.get("/v1/access-requests/received", **bearer(world.owner_a)).content)
    assert [x["id"] for x in sent] == [rid]
    assert [x["id"] for x in received] == [rid]
    assert sent[0]["message"] == "please"

    # Swapped, both empty - with the two assertions above as the control.
    assert json.loads(
        client.get("/v1/access-requests/received", **bearer(world.owner_b)).content) == []
    assert json.loads(
        client.get("/v1/access-requests/sent", **bearer(world.owner_a)).content) == []

    # The status filter is an enum: a real value filters, an unknown one is a 400 and
    # NOT a silently-ignored filter that would return every status.
    assert json.loads(client.get("/v1/access-requests/sent?status=pending",
                                 **bearer(world.owner_b)).content) != []
    assert json.loads(client.get("/v1/access-requests/sent?status=denied",
                                 **bearer(world.owner_b)).content) == []
    assert client.get("/v1/access-requests/sent?status=nope",
                      **bearer(world.owner_b)).status_code == 400


# ------------------------------------------------------------------------- auth

def test_owner_authentication_is_required_and_audience_bound(client, world):
    """Every arm here needs the control below, because a 401/403 is also what a broken
    route or an unconfigured key produces."""
    # CONTROL, first: a well-formed dashboard token works.
    assert client.get("/v1/grants/issued", **bearer(world.owner_a)).status_code == 200

    anon = client.get("/v1/grants/issued")
    assert anon.status_code == 401
    assert json.loads(anon.content)["error"] == "E_AUTH_MISSING"

    not_bearer = client.get("/v1/grants/issued", HTTP_AUTHORIZATION="Basic abc")
    assert not_bearer.status_code == 401
    assert json.loads(not_bearer.content)["error"] == "E_AUTH_INVALID"

    # A PEER token must not reach an owner route: the audience is the boundary.
    peer_aud = client.get("/v1/grants/issued", **bearer(world.owner_a, aud="peer"))
    assert peer_aud.status_code == 403

    # A sub with no dashboard_users row is a clean 401, not a 500 from a later FK.
    ghost = client.get("/v1/grants/issued", **bearer(uuid.uuid4()))
    assert ghost.status_code == 401
    assert json.loads(ghost.content)["error"] == "E_AUTH_INVALID"

    expired = client.get("/v1/grants/issued", **bearer(world.owner_a, ttl=-3600))
    assert expired.status_code == 401


def test_a_pat_is_constrained_to_its_scopes(client, world):
    """Cookie and dev-JWT owners have full authority; a PAT is constrained to its scopes."""
    read_only = world.issue_pat(world.owner_a, ["grants:read"])
    headers = {"HTTP_AUTHORIZATION": f"Bearer {read_only}"}

    # CONTROL: the read scope it DOES hold works, so the refusal below is the scope
    # gate and not a broken PAT lookup.
    assert client.get("/v1/grants/issued", **headers).status_code == 200

    write = post_json(client, "/v1/grants",
                      {"grantee_owner_id": str(world.owner_b),
                       "granter_peer_id": str(world.peer_a),
                       "skill_name": "pat-scoped", "source": "direct"}, headers)
    assert write.status_code == 403
    assert json.loads(write.content) == {
        "error": "E_SCOPE_REQUIRED",
        "message": "API key is missing the required scope: grants:write"}

    # A PAT with the write scope succeeds - the second control.
    rw = world.issue_pat(world.owner_a, ["grants:read", "grants:write"])
    ok = post_json(client, "/v1/grants",
                   {"grantee_owner_id": str(world.owner_b),
                    "granter_peer_id": str(world.peer_a),
                    "skill_name": "pat-scoped", "source": "direct"},
                   {"HTTP_AUTHORIZATION": f"Bearer {rw}"})
    assert ok.status_code == 201, ok.content

    # A revoked PAT is rejected HERE and never falls through to the JWT path.
    world.revoke_pat(rw)
    dead = client.get("/v1/grants/issued", HTTP_AUTHORIZATION=f"Bearer {rw}")
    assert dead.status_code == 401
    assert json.loads(dead.content)["error"] == "E_API_KEY_INVALID"


def test_the_access_request_routes_are_deliberately_not_scope_gated(client, world):
    """These handlers deliberately call no `require_scope`, per the specified behavior.
    Adding one would reject scopeless PAT callers that the specified behavior accepts."""
    scopeless = world.issue_pat(world.owner_b, [])
    headers = {"HTTP_AUTHORIZATION": f"Bearer {scopeless}"}
    ok = post_json(client, "/v1/access-requests",
                   {"granter_owner_id": str(world.owner_a),
                    "granter_peer_id": str(world.peer_a),
                    "skill_name": "unscoped-pat"}, headers)
    assert ok.status_code == 201, ok.content
    # CONTROL: the same scopeless key IS refused on a grants route, so the 201 above
    # is not "scope checks never run".
    assert client.get("/v1/grants/issued", **headers).status_code == 403


def test_a_db_check_violation_is_the_json_envelope_and_not_an_html_page(
        client, world, monkeypatch):
    """END-TO-END PROOF for apps/core/jsonerrors.JsonErrorEnvelopeMiddleware, on the exact
    CHECK that was MEASURED returning HTML.

    `max_executions=0` tripped the DB CHECK `skill_grants_max_executions_positive`:
    `repository.py` re-raises anything that is not the IG4 trigger text, and `_route`
    catches only ApiError and DomainError - so `psycopg.errors.CheckViolation` walked into
    Django's default handler, and under MESHY_DEBUG=1 the rendered traceback carried the
    CheckViolation DETAIL line, which lists EVERY COLUMN VALUE of the attempted grant.

    Since STATUS C36 the route refuses that value with a 400 before the database
    (test_max_executions_is_refused_before_the_database_unless_a_positive_i32), so the
    request here sends a LEGAL cap and the insert is handed the illegal one underneath
    the route. The CHECK, the psycopg error and the middleware are all real; only the
    path to the CHECK changed. An unforeseen database error is still what this proves.

    This asserts the three things that matter and are separable: the CONTENT TYPE (a JSON
    client must not be handed HTML), the BODY (the constant envelope), and the ABSENCE of
    the detail. A test asserting only the status code would pass on the HTML page.
    """
    real_insert = repository.insert_grant
    forced = {}

    def insert_with_the_illegal_cap(*args, **kwargs):
        if "value" in forced:
            kwargs["max_executions"] = forced["value"]
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(repository, "insert_grant", insert_with_the_illegal_cap)
    for bad in (0, -5):
        forced["value"] = bad
        r = issue_grant_via_api(client, world, grantor=world.owner_a,
                                grantee=world.owner_b, peer=world.peer_a,
                                skill="checkviolation",
                                extra={"max_executions": 1})
        assert r.status_code == 500, (bad, r.status_code, r.content[:200])
        assert r["Content-Type"] == "application/json",             f"max_executions={bad} answered {r['Content-Type']!r} - the HTML page is back"
        assert json.loads(r.content) == {"error": "E_INTERNAL",
                                         "message": "internal error"}
        text = r.content.decode()
        for leaked in ("skill_grants", "DETAIL", "check constraint", "Traceback",
                       str(world.owner_a)):
            assert leaked not in text, f"{leaked!r} reached the client"

    # POSITIVE CONTROL: the identical call with a LEGAL max_executions succeeds, so the
    # 500s above are about the value and not about a broken fixture, a wrong scope or a
    # route that has started failing for everyone.
    forced.clear()
    ok = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                             peer=world.peer_a, skill="checkviolation",
                             extra={"max_executions": 1})
    assert ok.status_code == 201, ok.content


def test_every_response_carries_the_security_headers(client, world):
    """`SecurityHeadersMiddleware`, asserted on BOTH a success and a refusal.

    A header present only on 200s is not a control - and this project had none anywhere,
    while apps/files justifies serving opaque bytes as application/octet-stream with an
    argument that is only true when `nosniff` is set.
    """
    ok = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                             peer=world.peer_a, skill="headers")
    assert ok.status_code == 201, ok.content
    refused = client.get("/v1/grants/issued")           # no credential at all
    assert refused.status_code in (401, 403), refused.content

    for response in (ok, refused):
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Referrer-Policy"] == "no-referrer"


# ------------------------------------------------------------ shapes and rejections

def test_the_grant_body_has_the_expected_shape(client, world):
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="shape",
                            extra={"scopes": ["read"], "max_executions": 3})
    assert r.status_code == 201, r.content
    body = json.loads(r.content)
    assert list(body.keys()) == [
        "id", "grantor_owner_id", "grantee_owner_id", "granter_peer_id", "skill_name",
        "version_pin", "source", "source_ref", "scopes", "granted_at", "expires_at",
        "revoked_at", "revoked_by", "revoke_reason", "max_executions", "used_executions",
        "max_spend_cents", "used_spend_cents", "approved_requires_human_auth",
    ], "the response fields must use the declared wire order"
    # The two optional fields are ABSENT, not null...
    assert "freshness_ceiling_secs" not in body
    assert "approved_manifest_hash" not in body
    # ...while every other optional field is present as null. A rewrite that skipped them all
    # would pass an "is it absent" test and break every client reading `version_pin`.
    assert body["version_pin"] is None and body["source_ref"] is None
    assert body["scopes"] == ["read"] and body["max_executions"] == 3
    assert body["used_executions"] == 0 and body["used_spend_cents"] == 0
    assert body["approved_requires_human_auth"] is False
    # Timestamps use RFC 3339 with a literal Z rather than +00:00.
    assert body["granted_at"].endswith("Z")
    # biscuit_token is skipped when None, and minting is deploy-dark here.
    assert "biscuit_token" not in body


def test_malformed_requests_get_the_extractor_status(client, world):
    headers = bearer(world.owner_a)
    # CONTROL: the well-formed version of this body is a 201, so each rejection below
    # is about the specific thing it removes.
    good = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "malformed", "source": "direct"}
    assert post_json(client, "/v1/grants", dict(good), headers).status_code == 201

    no_ctype = client.post("/v1/grants", data=json.dumps(good),
                           content_type="text/plain", **headers)
    assert no_ctype.status_code == 415

    syntax = client.post("/v1/grants", data="{not json", content_type="application/json",
                         **headers)
    assert syntax.status_code == 400

    missing = post_json(client, "/v1/grants", {k: v for k, v in good.items()
                                               if k != "skill_name"}, headers)
    assert missing.status_code == 422, "valid JSON that does not deserialize is 422"

    unknown_source = post_json(client, "/v1/grants", dict(good, source="gift"), headers)
    assert unknown_source.status_code == 422
    assert "unknown variant `gift`" in json.loads(unknown_source.content)["message"]

    bad_id = client.delete("/v1/grants/not-a-uuid", **headers)
    assert bad_id.status_code == 400
    assert json.loads(bad_id.content)["error"] == "E_VALIDATION"

    missing_grant = client.delete(f"/v1/grants/{uuid.uuid4()}", **headers)
    assert missing_grant.status_code == 404


def test_methods_a_route_does_not_have_are_405(client, world):
    headers = bearer(world.owner_a)
    # CONTROL: the method the route DOES have works.
    assert client.get("/v1/grants/issued", **headers).status_code == 200
    resp = client.get("/v1/grants", **headers)
    assert resp.status_code == 405
    assert resp.headers["Allow"] == "POST"
    assert client.get(f"/v1/access-requests/{uuid.uuid4()}/approve",
                      **headers).status_code == 405


#: Every route of the surface and the methods it declares on `@_route(...)`, in that order
#: (the order is the `Allow` header's).
_ROUTE_METHODS = {
    "/v1/grants": ("POST",),
    "/v1/grants/issued": ("GET",),
    "/v1/grants/held": ("GET",),
    f"/v1/grants/{uuid.UUID(int=1)}": ("PATCH", "DELETE"),
    "/v1/access-requests": ("POST",),
    "/v1/access-requests/sent": ("GET",),
    "/v1/access-requests/received": ("GET",),
    f"/v1/access-requests/{uuid.UUID(int=2)}/approve": ("POST",),
    f"/v1/access-requests/{uuid.UUID(int=3)}/deny": ("POST",),
    f"/v1/access-requests/{uuid.UUID(int=4)}/withdraw": ("POST",),
}


@pytest.mark.parametrize("path", list(_ROUTE_METHODS))
def test_every_route_answers_a_foreign_method_405_before_the_credential(client, path):
    """The method check is the first thing every route answers: an empty 405 with the
    route's own `Allow`, even with NO credential, so it precedes the 401. It used to be the
    first line of each of the ten handlers and is now `_route`'s. This pins the ORDER
    (method before credential) and the `Allow` text; it cannot see whether the check runs
    inside or outside the connection block, which this test observes no differently.
    CONTROL: the route's own method, also uncredentialed, is the 401 - so the 405 is the
    method check's, not a missing route's."""
    allowed = _ROUTE_METHODS[path]
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        resp = client.generic(method, path)
        if method in allowed:
            assert resp.status_code == 401, (method, path, resp.content)
            assert json.loads(resp.content)["error"] == "E_AUTH_MISSING"
            continue
        assert resp.status_code == 405, (method, path, resp.content)
        assert resp.headers["Allow"] == ", ".join(allowed)
        assert resp.content == b""


def test_the_dead_names_are_gone():
    """Names that nothing produced or read were deleted rather than kept as documentation:
    wire.now_utc (service.py has its own `_now`), repository.Denied (no reader returns it;
    the union is Found | NotFound | Empty), owner_auth's JWT_ALGORITHM and CSRF_DOMAIN (the
    JWT pin is tokens.verify_token's, the CSRF MAC websec's), and views' re-export of
    Found/Empty, which nothing imported from views. A re-added copy is a second spelling
    of a rule that lives elsewhere."""
    from apps.grants import views

    for module, name in ((wire, "now_utc"), (repository, "Denied"),
                         (owner_auth, "JWT_ALGORITHM"), (owner_auth, "CSRF_DOMAIN"),
                         (views, "Found"), (views, "_method")):
        assert not hasattr(module, name), f"{module.__name__}.{name}"
    assert not {"Found", "Empty"} & set(views.__all__)
    # CONTROL: the live names beside them are still there.
    assert callable(wire.optional_rfc3339) and repository.Empty and views.Empty
    assert owner_auth.GRANTS_READ == "grants:read"


def test_patch_updates_the_expiry_and_refuses_another_owners_grant(client, conn, world):
    """The wire contract of PATCH. The other-owner 404 below is answered by the SERVICE
    owner check and never reaches the repository's `AND grantor_owner_id = %s`, so this
    test cannot see that clause go (STATUS C44, the old `:806`);
    test_patch_owner_scoping_holds_at_each_layer_on_its_own tests each layer alone."""
    r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                            peer=world.peer_a, skill="patchable",
                            expires_at=now() + _dt.timedelta(hours=2))
    grant_id = json.loads(r.content)["id"]

    new_expiry = now() + _dt.timedelta(minutes=5)
    ok = client.patch(f"/v1/grants/{grant_id}", data=json.dumps({"expires_at": iso(new_expiry)}),
                      content_type="application/json", **bearer(world.owner_a))
    assert ok.status_code == 200, ok.content
    assert json.loads(ok.content)["expires_at"] == iso(new_expiry)

    # Another owner gets 404, not 403: a PATCH must not be a probe for grant ids.
    other = client.patch(f"/v1/grants/{grant_id}", data=json.dumps({"expires_at": None}),
                         content_type="application/json", **bearer(world.owner_b))
    assert other.status_code == 404

    empty = client.patch(f"/v1/grants/{grant_id}", data=json.dumps({}),
                         content_type="application/json", **bearer(world.owner_a))
    assert empty.status_code == 400
    assert json.loads(empty.content)["message"] == (
        "provide at least one of expires_at or version_pin")

    # `null` CLEARS the expiry; absent would have left it. The two are different.
    cleared = client.patch(f"/v1/grants/{grant_id}", data=json.dumps({"expires_at": None}),
                           content_type="application/json", **bearer(world.owner_a))
    assert cleared.status_code == 200
    assert json.loads(cleared.content)["expires_at"] is None


# ------------------------------------------------------------------ typed absence

def test_repository_absence_is_typed_not_an_empty_collection(client, conn, world):
    """A bare [] cannot distinguish 'this owner has none' from 'the read returned
    nothing for another reason'. Both branches are asserted, so neither is vacuous."""
    empty = repository.list_issued(conn, world.owner_c, False)
    assert isinstance(empty, repository.Empty)
    assert str(world.owner_c) in empty.scope

    issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                        peer=world.peer_a, skill="typed")
    found = repository.list_issued(conn, world.owner_a, False)
    assert isinstance(found, repository.Found) and len(found.value) == 1

    assert isinstance(repository.find_grant_by_id(conn, uuid.uuid4()), repository.NotFound)
    assert isinstance(repository.find_request_by_id(conn, uuid.uuid4()), repository.NotFound)


def test_malformed_mandates_are_400_and_an_owner_with_no_human_key_is_403(client, conn, world):
    """Malformed shapes are 400s before any DB work, and an owner with NO registered human
    key is refused (the empty-key-set arm). Named for what it
    measures (STATUS C44, the old `:854`, called `test_the_mandate_path_is_fail_closed`):
    it never asks the verifier a question it could answer wrongly, so it cannot see
    `verify_strict` go. The MANDATE CRYPTOGRAPHY section below does."""
    from apps.crypto.primitives import b64u, public_key_of
    from apps.grants import intent

    base = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "mandated", "source": "direct",
            "expires_at": iso(now() + _dt.timedelta(minutes=10))}
    headers = bearer(world.owner_a)

    # 400s: shape, then base64, then length. Each check is deliberately distinct.
    assert post_json(client, "/v1/grants", dict(base, mandate={"sig": "x"}),
                     headers).status_code == 400
    mandate_obj = {"owner": str(world.owner_a), "grantee": str(world.owner_b),
                   "target_peer": str(world.peer_a), "skill": "mandated",
                   "scopes": [], "expires_at": int((now() + _dt.timedelta(hours=1)).timestamp()),
                   "budget": None}
    assert post_json(client, "/v1/grants",
                     dict(base, mandate={"mandate": mandate_obj, "sig": "!!!"}),
                     headers).status_code == 400
    import base64
    short = base64.b64encode(b"\x00" * 10).decode()
    assert post_json(client, "/v1/grants",
                     dict(base, mandate={"mandate": mandate_obj, "sig": short}),
                     headers).status_code == 400

    # A well-formed but unverifiable mandate (no registered human key) is a 403.
    seed = hashlib.sha256(b"grants-test-human-key").digest()
    m = intent.IntentMandate.from_json(mandate_obj)
    from apps.crypto.primitives import sign as ed_sign
    sig = base64.b64encode(ed_sign(seed, m.signing_input())).decode()
    denied = post_json(client, "/v1/grants",
                       dict(base, mandate={"mandate": mandate_obj, "sig": sig}), headers)
    assert denied.status_code == 403, denied.content

    # CONTROL: register that very key and the SAME call succeeds - so the 403 above is
    # the missing registration and not "mandates are always refused".
    with conn.cursor() as cur:
        cur.execute("INSERT INTO owner_human_keys (id, owner_id, kid, public_key) "
                    "VALUES (%s,%s,%s,%s)",
                    (str(uuid.uuid4()), str(world.owner_a), "test-kid",
                     b64u(public_key_of(seed))))
    ok = post_json(client, "/v1/grants",
                   dict(base, mandate={"mandate": mandate_obj, "sig": sig}), headers)
    assert ok.status_code == 201, ok.content
    grant_id = json.loads(ok.content)["id"]
    # The provenance really was stored - a mandate that is accepted but not recorded
    # would pass the status assertion above.
    assert repository.grant_is_mandate_bound(conn, grant_id)


# ------------------------------------------------------- MANDATE CRYPTOGRAPHY
#
# WHY THIS SECTION EXISTS. `test_malformed_mandates_are_400_and_an_owner_with_no_human_key_is_403`
# above (formerly `test_the_mandate_path_is_fail_closed`) produces its
# 403 from the EMPTY-KEY-SET branch (service.verify_mandate_against_owner, the
# `isinstance(keys, Empty)` arm) and its 201 from a key that happens to verify. Neither
# half ever asks the verifier a question it could answer WRONG: with the Ed25519 check
# stubbed out to "always valid", both of its assertions still hold, because the denial
# it measures comes from a row that is missing rather than from bytes that do not match.
# Every test below therefore holds a REGISTERED, ACTIVE human key fixed and varies only
# the thing under test, so the refusal has exactly one possible source.
#
# ORDER. The file's rule is control-first, and the two tests here that invert it say so
# in place: their negative and their control are the SAME request body but for one
# field, so the control cannot run first without consuming the
# (grantor, grantee, peer, skill) slot the partial unique index guards - a mutant's 201
# would then surface as a 409 and the test would pass without the guard existing.

def human_seed(tag: str) -> bytes:
    """A deterministic 32-byte Ed25519 seed. Deterministic so a failure is reproducible;
    per-tag so a test that needs two distinct keys gets two distinct keys."""
    return hashlib.sha256(f"grants-test-human-key/{tag}".encode()).digest()


def register_human_key(conn, owner_id, seed32: bytes, *, kid="human-kid", revoked=False):
    """One row in `owner_human_keys`, the table migration 066 added and that
    repository.active_human_keys reads. `public_key` is base64url, matching what
    service.verify_mandate_against_owner feeds to b64u_decode."""
    from apps.crypto.primitives import b64u, public_key_of

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO owner_human_keys (id, owner_id, kid, public_key, revoked_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (str(uuid.uuid4()), str(owner_id), kid, b64u(public_key_of(seed32)),
             now() if revoked else None))


def mandate_fields(*, owner, grantee, target_peer, skill, expires_at, scopes=(), budget=None):
    """The seven `IntentMandate` field names. `budget` is present as null rather than
    omitted, because it is part of the signed bytes."""
    return {"owner": str(owner), "grantee": str(grantee), "target_peer": str(target_peer),
            "skill": skill, "scopes": list(scopes),
            "expires_at": int(expires_at.timestamp()) if hasattr(expires_at, "timestamp")
            else int(expires_at),
            "budget": budget}


def sign_mandate(seed32: bytes, fields: dict) -> str:
    """Return base64 STANDARD of Ed25519(seed, MANDATE_DOMAIN || JCS(fields)), the signing
    input the verifier rebuilds."""
    import base64

    from apps.crypto.primitives import sign as ed_sign
    from apps.grants import intent

    m = intent.IntentMandate.from_json(fields)
    return base64.b64encode(ed_sign(seed32, m.signing_input())).decode("ascii")


def test_a_wrong_signature_under_a_registered_key_is_refused(client, conn, world):
    """THE test for `verify_strict`. An ACTIVE human key
    is registered throughout, so the empty-key-set branch cannot be the source of any
    403 here; the only thing left that can refuse is the signature check itself.

    The first arm is the attack the signature exists to stop. `budget` is signed
    (it is in the canonical bytes as null) but it is read by NO other check:
    `mandate_authorizes` inspects expiry, grantee, peer, skill and scopes and never looks
    at budget. So a holder who takes a mandate their owner signed for budget=null and
    re-sends it claiming a budget is invisible to every non-cryptographic guard in the
    request path. If this call is accepted, Ed25519 is not being consulted at all.

    The second arm is the other half: the right bytes signed by a key this owner never
    registered. The control is the identical body with the signature the owner's own
    registered key produced.
    """
    seed = human_seed("registered")
    stranger = human_seed("never-registered")
    assert stranger != seed
    register_human_key(conn, world.owner_a, seed)

    expiry = now() + _dt.timedelta(minutes=10)
    honest = mandate_fields(owner=world.owner_a, grantee=world.owner_b,
                            target_peer=world.peer_a, skill="sig-checked",
                            expires_at=now() + _dt.timedelta(hours=1))
    body = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "sig-checked", "source": "direct", "expires_at": iso(expiry)}
    headers = bearer(world.owner_a)

    # ARM 1 - a field the owner never signed, carrying a signature that is valid over
    # the version they DID sign. Well-formed base64, exactly 64 bytes, so the 400 decode
    # path is not what answers.
    tampered = dict(honest, budget=999_999)
    stale_sig = sign_mandate(seed, honest)
    forged = post_json(client, "/v1/grants",
                       dict(body, mandate={"mandate": tampered, "sig": stale_sig}), headers)
    assert forged.status_code == 403, (
        "a mandate whose budget was changed after signing was accepted - the Ed25519 "
        f"check is not running: {forged.content}")
    assert json.loads(forged.content)["error"] == "E_FORBIDDEN", (
        "this must be the crypto refusal, not the 400 decode path")

    # ARM 2 - the honest bytes, signed by a key that is not registered to this owner.
    outsider = post_json(client, "/v1/grants",
                         dict(body, mandate={"mandate": honest,
                                             "sig": sign_mandate(stranger, honest)}), headers)
    assert outsider.status_code == 403, (
        f"an unregistered key's signature was accepted: {outsider.content}")

    # Neither refusal wrote anything. A 403 rendered AFTER an insert would pass the two
    # assertions above while leaving the grant live.
    assert isinstance(service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                                  world.peer_a, "sig-checked"),
                      repository.Empty)

    # CONTROL - byte-identical to ARM 2 except for the signature, which the registered
    # key produced. Without this the two 403s above are also what a broken fixture, an
    # unmigrated table or a mis-encoded public key produces.
    ok = post_json(client, "/v1/grants",
                   dict(body, mandate={"mandate": honest, "sig": sign_mandate(seed, honest)}),
                   headers)
    assert ok.status_code == 201, ok.content
    assert repository.grant_is_mandate_bound(conn, json.loads(ok.content)["id"])


def test_the_mandate_signer_must_be_the_authenticated_grantor(client, conn, world):
    """TASK-84 / CRITICAL-1 (recorded in docs/REVIEW-FIX-QUEUE.md): the mandate's claimed human signer is bound to
    `auth.owner_id` BEFORE any key is resolved, and the resolved key set is the
    AUTHENTICATED owner's, not the mandate's.

    That asymmetry is why the binding is load-bearing rather than redundant. Drop it and
    a mandate naming owner_c as the human authority is verified against owner_a's keys:
    owner_a signs a mandate that reads as owner_c's instruction, it verifies, and the
    stored provenance now attributes a human decision to someone who never made one.
    Nothing downstream re-checks the owner field, so the row is indistinguishable from a
    genuine one.

    The negative runs first on purpose - see the section note above; it and the control
    differ only in the mandate's `owner` field, so they must not both be issuable.
    """
    seed = human_seed("grantor-binding")
    register_human_key(conn, world.owner_a, seed)

    expiry = now() + _dt.timedelta(minutes=10)
    mandate_expiry = now() + _dt.timedelta(hours=1)
    body = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "signer-bound", "source": "direct", "expires_at": iso(expiry)}
    headers = bearer(world.owner_a)

    # owner_c is party to nothing and has no keys; owner_a's registered key signs a
    # mandate that CLAIMS owner_c as the human signer. The signature is genuine over
    # those bytes, so the verify loop alone would let it through.
    impersonating = mandate_fields(owner=world.owner_c, grantee=world.owner_b,
                                   target_peer=world.peer_a, skill="signer-bound",
                                   expires_at=mandate_expiry)
    denied = post_json(client, "/v1/grants",
                       dict(body, mandate={"mandate": impersonating,
                                           "sig": sign_mandate(seed, impersonating)}), headers)
    assert denied.status_code == 403, (
        "a mandate naming another owner as its human signer was accepted under this "
        f"owner's key: {denied.content}")
    assert isinstance(service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                                  world.peer_a, "signer-bound"),
                      repository.Empty)

    # CONTROL: the same mandate with `owner` set to the authenticated grantor, signed by
    # the same key, in the same request body - so the refusal above is the binding and
    # not the key, the peer, the skill or the expiry.
    honest = dict(impersonating, owner=str(world.owner_a))
    ok = post_json(client, "/v1/grants",
                   dict(body, mandate={"mandate": honest, "sig": sign_mandate(seed, honest)}),
                   headers)
    assert ok.status_code == 201, ok.content


def test_a_revoked_human_key_no_longer_verifies_a_mandate(client, conn, world):
    """Key resolution requires `revoked_at IS NULL`. Revoking a human
    key is the owner's only lever after a signing key is lost or a person leaves; if the
    filter is dropped, every signature that key ever made keeps authorising mandates
    forever, and the revocation row sits in the table looking like it worked.

    The control runs first because revocation is a state change on the one key here:
    the same key, the same signing procedure, the same request shape - before and after.
    """
    seed = human_seed("revocable")
    register_human_key(conn, world.owner_a, seed, kid="to-be-revoked")
    headers = bearer(world.owner_a)

    def issue(skill):
        expiry = now() + _dt.timedelta(minutes=10)
        fields = mandate_fields(owner=world.owner_a, grantee=world.owner_b,
                                target_peer=world.peer_a, skill=skill,
                                expires_at=now() + _dt.timedelta(hours=1))
        return post_json(client, "/v1/grants",
                         {"grantee_owner_id": str(world.owner_b),
                          "granter_peer_id": str(world.peer_a), "skill_name": skill,
                          "source": "direct", "expires_at": iso(expiry),
                          "mandate": {"mandate": fields, "sig": sign_mandate(seed, fields)}},
                         headers)

    # CONTROL: while the key is active it authorises.
    live = issue("revocable-before")
    assert live.status_code == 201, live.content

    with conn.cursor() as cur:
        cur.execute("UPDATE owner_human_keys SET revoked_at = NOW() "
                    " WHERE owner_id = %s AND kid = %s",
                    (str(world.owner_a), "to-be-revoked"))
        assert cur.rowcount == 1, "the revocation updated no row; the test below is vacuous"

    dead = issue("revocable-after")
    assert dead.status_code == 403, (
        f"a revoked human key still verifies mandates: {dead.content}")

    # The ROW is still there - so the 403 is the filter, not a key that vanished. This
    # is the same distinction the revoked-grant test draws: capability, not history.
    with conn.cursor() as cur:
        cur.execute("SELECT revoked_at IS NOT NULL FROM owner_human_keys "
                    " WHERE owner_id = %s AND kid = %s", (str(world.owner_a), "to-be-revoked"))
        assert cur.fetchone() == (True,)


def test_an_expired_mandate_does_not_authorise(client, conn, world):
    """`now > mandate.expires_at` is the mandate's OWN lifetime, and it is a different
    question from the check that compares the requested grant
    expiry against the mandate. A grant asking for an expiry in the past satisfies the
    grant-expiry-within-mandate check against any mandate, so without the mandate-lifetime
    check a mandate signed and expired last
    year still issues grants - the signature stays valid forever, which is exactly why
    the mandate carries an expiry at all.

    Both arms use a past requested expiry (the surface accepts one - see
    `test_an_expired_grant_does_not_authorise`) so that the grant-expiry-within-mandate check
    is satisfied in both, and the only variable left is the mandate's own expiry.
    """
    seed = human_seed("mandate-lifetime")
    register_human_key(conn, world.owner_a, seed)
    headers = bearer(world.owner_a)

    def attempt(skill, mandate_expiry, grant_expiry):
        fields = mandate_fields(owner=world.owner_a, grantee=world.owner_b,
                                target_peer=world.peer_a, skill=skill,
                                expires_at=mandate_expiry)
        return post_json(client, "/v1/grants",
                         {"grantee_owner_id": str(world.owner_b),
                          "granter_peer_id": str(world.peer_a), "skill_name": skill,
                          "source": "direct", "expires_at": iso(grant_expiry),
                          "mandate": {"mandate": fields, "sig": sign_mandate(seed, fields)}},
                         headers)

    # CONTROL: a live mandate, same signing key, same shape.
    live = attempt("lifetime-live", now() + _dt.timedelta(hours=1),
                   now() - _dt.timedelta(hours=2))
    assert live.status_code == 201, live.content

    # An hour-old mandate, and a requested expiry two hours old so that the
    # expiry-subset check cannot be what refuses.
    stale = attempt("lifetime-stale", now() - _dt.timedelta(hours=1),
                    now() - _dt.timedelta(hours=2))
    assert stale.status_code == 403, f"an expired mandate still authorises: {stale.content}"
    assert isinstance(service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                                  world.peer_a, "lifetime-stale"),
                      repository.Empty)


def test_a_grant_may_not_outlive_the_mandate_that_authorised_it(client, conn, world):
    """The mandate is the human's consent and its expiry is the outer bound on it; a grant
    that outlives it keeps acting on consent that has run out, and
    nothing at invoke time re-reads the mandate to notice.

    The third arm is service.py's own guard (`expires_at is None` -> ExpiryExceedsMandate,
    mirroring that a never-expiring grant is unbounded by definition): a mandate-bearing
    request that omits `expires_at` entirely cannot be inside any mandate's window, and
    the field is optional on this route - `test_the_grant_body_has_the_expected_shape`
    issues without it.
    """
    seed = human_seed("outliving")
    register_human_key(conn, world.owner_a, seed)
    headers = bearer(world.owner_a)
    mandate_expiry = now() + _dt.timedelta(minutes=10)

    def attempt(skill, grant_expiry):
        fields = mandate_fields(owner=world.owner_a, grantee=world.owner_b,
                                target_peer=world.peer_a, skill=skill,
                                expires_at=mandate_expiry)
        body = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
                "skill_name": skill, "source": "direct",
                "mandate": {"mandate": fields, "sig": sign_mandate(seed, fields)}}
        if grant_expiry is not None:
            body["expires_at"] = iso(grant_expiry)
        return post_json(client, "/v1/grants", body, headers)

    # CONTROL: inside the mandate's window.
    inside = attempt("outlive-inside", mandate_expiry - _dt.timedelta(minutes=5))
    assert inside.status_code == 201, inside.content

    beyond = attempt("outlive-beyond", mandate_expiry + _dt.timedelta(minutes=5))
    assert beyond.status_code == 403, (
        f"a grant was issued past the expiry of the mandate authorising it: {beyond.content}")

    unbounded = attempt("outlive-forever", None)
    assert unbounded.status_code == 403, (
        f"a never-expiring grant was issued under a mandate that expires: "
        f"{unbounded.content}")

    for skill in ("outlive-beyond", "outlive-forever"):
        assert isinstance(service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                                      world.peer_a, skill),
                          repository.Empty), skill


def test_the_mandate_grantee_binding_is_enforced(client, conn, world):
    """The grantee is WHO the human consented to lend the skill to, and it is the field an
    attacker would change: the request body's `grantee_owner_id` is
    attacker-chosen, and the mandate is the only thing that constrains it. Remove the
    comparison and a mandate authorising a loan to owner_c issues a grant to owner_b,
    with a valid signature and a stored mandate_hash to vouch for it.

    Negative first - see the section note; the two calls are the same request body and
    differ only in the mandate's `grantee`.
    """
    seed = human_seed("grantee-binding")
    register_human_key(conn, world.owner_a, seed)
    headers = bearer(world.owner_a)

    expiry = now() + _dt.timedelta(minutes=10)
    body = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "grantee-bound", "source": "direct", "expires_at": iso(expiry)}

    # Signed for owner_c, redeemed for owner_b.
    for_c = mandate_fields(owner=world.owner_a, grantee=world.owner_c,
                           target_peer=world.peer_a, skill="grantee-bound",
                           expires_at=now() + _dt.timedelta(hours=1))
    denied = post_json(client, "/v1/grants",
                       dict(body, mandate={"mandate": for_c, "sig": sign_mandate(seed, for_c)}),
                       headers)
    assert denied.status_code == 403, (
        f"a mandate naming a different grantee authorised this grant: {denied.content}")
    assert isinstance(service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                                  world.peer_a, "grantee-bound"),
                      repository.Empty)

    # CONTROL: the same mandate naming the grantee the request actually asks for.
    for_b = dict(for_c, grantee=str(world.owner_b))
    ok = post_json(client, "/v1/grants",
                   dict(body, mandate={"mandate": for_b, "sig": sign_mandate(seed, for_b)}),
                   headers)
    assert ok.status_code == 201, ok.content


def test_mandate_required_at_issuance_gate(client, world, monkeypatch):
    """The deploy-dark MESHYCHAT_MANDATE_REQUIRE_AT_ISSUANCE flag, default OFF."""
    # CONTROL: with the flag off (the default the clean_env fixture enforces) a
    # mandate-less issuance is a 201.
    assert not gconfig.mandate_require_at_issuance()
    first = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                peer=world.peer_a, skill="gated")
    assert first.status_code == 201, first.content

    monkeypatch.setenv("MESHYCHAT_MANDATE_REQUIRE_AT_ISSUANCE", "1")
    gated = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                peer=world.peer_a, skill="gated-2")
    assert gated.status_code == 400
    assert "MESHYCHAT_MANDATE_REQUIRE_AT_ISSUANCE" in json.loads(gated.content)["message"]


def test_capability_minting_is_refused_rather_than_silently_omitted(client, world, monkeypatch):
    """This application does not mint biscuits. If a root key is configured, answering
    with a token-less body
    would be a silent capability loss, so issuance is refused instead."""
    # CONTROL: deploy-dark (unset) is the supported configuration and it issues.
    assert issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                               peer=world.peer_a, skill="dark").status_code == 201
    monkeypatch.setenv("MESHYCHAT_BISCUIT_ROOT_SK", "deadbeef")
    refused = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                  peer=world.peer_a, skill="lit")
    assert refused.status_code == 500
    assert json.loads(refused.content) == {"error": "E_INTERNAL", "message": "internal error"}


def test_a_delegation_needs_a_resolvable_parent_and_the_300s_ceiling(client, conn, world):
    """A `delegated` row whose source_ref does not resolve to a parent is incoherent and
    is rejected - never demoted to a root issuance (which would skip the owns-the-peer
    check entirely) - and a child past the absolute 300 s ceiling is refused. Named for
    what it measures (STATUS C44, the old `:936`, called
    `test_delegated_issuance_must_attenuate_its_parent`): neither refusal comes from
    comparing the child with its parent, so it cannot see any of the seven attenuation
    checks go. The section below tests each of them."""
    dangling = issue_grant_via_api(
        client, world, grantor=world.owner_b, grantee=world.owner_c, peer=world.peer_a,
        skill="delegated-skill", source="delegated",
        extra={"source_ref": str(uuid.uuid4()),
               "expires_at": iso(now() + _dt.timedelta(seconds=120))})
    assert dangling.status_code == 400
    assert "does not attenuate" in json.loads(dangling.content)["message"]

    # A real parent: owner_a lends peer_a to owner_b, who re-delegates to owner_c.
    parent = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                 peer=world.peer_a, skill="delegated-skill",
                                 expires_at=now() + _dt.timedelta(minutes=10))
    assert parent.status_code == 201, parent.content
    parent_id = json.loads(parent.content)["id"]

    # Widening past the 300s sub-delegation ceiling is refused...
    too_long = issue_grant_via_api(
        client, world, grantor=world.owner_b, grantee=world.owner_c, peer=world.peer_a,
        skill="delegated-skill", source="delegated",
        extra={"source_ref": parent_id, "expires_at": iso(now() + _dt.timedelta(minutes=9))})
    assert too_long.status_code == 400
    assert "300s ceiling" in json.loads(too_long.content)["message"]

    # CONTROL: inside the ceiling, the identical delegation succeeds.
    ok = issue_grant_via_api(
        client, world, grantor=world.owner_b, grantee=world.owner_c, peer=world.peer_a,
        skill="delegated-skill", source="delegated",
        extra={"source_ref": parent_id, "expires_at": iso(now() + _dt.timedelta(seconds=120))})
    assert ok.status_code == 201, ok.content
    assert json.loads(ok.content)["source"] == "delegated"


# ------------------------------------------------ delegation attenuation, check by check
#
# STATUS C39 (RFQ:339-343). `apps/grants/delegation.py:validate_delegation_provenance`
# makes eight checks and only the last one - the 300 s TTL ceiling - was tested: seven
# could each be deleted with the suite green (the scope subset at :84 measured 40 of 40).
# The database cannot catch any of them, because `skill_grants_granter_ownership_guard`
# (sql/047) RETURNs early for `source = 'delegated'`: for a delegated row this function
# IS the authorisation.
#
# Every test below goes through `POST /v1/grants`, so it also proves `service.issue_grant`
# reaches the check with the right arguments (a unit test of the function could not).
# Each one has the same shape:
#   CONTROL - a delegation that attenuates correctly is a 201;
#   the control child is revoked, so the variant cannot be refused as a DUPLICATE, and a
#   deleted check shows up as a clean 201 rather than a 409;
#   VARIANT - ONE property changed; a 400 whose message is THIS check's own reason (the
#   checks are ordered so that a more specific failure reports itself, and the message
#   is the wire body).

_UNSET = object()
DELEGATED_SKILL = "attenuate"


def _created_id(response) -> str:
    assert response.status_code == 201, response.content
    return json.loads(response.content)["id"]


def _revoke_row(conn, grant_id, by) -> None:
    """Revoke as the bootstrap role. The revoke ROUTE is not under test here, and must
    not be able to make a delegation test pass or fail."""
    with conn.cursor() as cur:
        cur.execute("UPDATE skill_grants SET revoked_at = NOW(), revoked_by = %s "
                    "WHERE id = %s", (str(by), str(grant_id)))
        assert cur.rowcount == 1, grant_id


def _parent(client, world, *, skill=DELEGATED_SKILL, ttl_s=600, scopes=_UNSET,
            max_executions=_UNSET) -> str:
    """owner_a lends peer_a's `skill` to owner_b: the grant owner_b may re-delegate."""
    extra = {}
    if scopes is not _UNSET:
        extra["scopes"] = scopes
    if max_executions is not _UNSET:
        extra["max_executions"] = max_executions
    return _created_id(issue_grant_via_api(
        client, world, grantor=world.owner_a, grantee=world.owner_b, peer=world.peer_a,
        skill=skill,
        expires_at=None if ttl_s is None else now() + _dt.timedelta(seconds=ttl_s),
        extra=extra))


def _delegate(client, world, parent_id, *, grantor=None, grantee=None, peer=None,
              skill=DELEGATED_SKILL, ttl_s=120, scopes=_UNSET, max_executions=_UNSET):
    """owner_b re-delegates to owner_c, inside the 300 s ceiling, unless told otherwise."""
    extra = {"source_ref": str(parent_id)}
    if ttl_s is not None:
        extra["expires_at"] = iso(now() + _dt.timedelta(seconds=ttl_s))
    if scopes is not _UNSET:
        extra["scopes"] = scopes
    if max_executions is not _UNSET:
        extra["max_executions"] = max_executions
    return issue_grant_via_api(
        client, world, grantor=grantor or world.owner_b, grantee=grantee or world.owner_c,
        peer=peer or world.peer_a, skill=skill, source="delegated", extra=extra)


def _refused_for(response, reason: str) -> None:
    assert response.status_code == 400, response.content
    message = json.loads(response.content)["message"]
    assert "does not attenuate" in message, message
    assert reason in message, message


def _control_then_revoke(client, conn, world, parent_id, **kw) -> None:
    _revoke_row(conn, _created_id(_delegate(client, world, parent_id, **kw)), world.owner_b)


@pytest.mark.parametrize("how", ["revoked", "expired"])
def test_a_delegation_from_a_parent_that_is_no_longer_live_is_refused(
        client, conn, world, how):
    """Check 1, parent liveness (delegation.py:56). Deleted, a REVOKED grant keeps
    minting children: revocation would stop the holder but not anything they re-lend."""
    parent = _parent(client, world)
    _control_then_revoke(client, conn, world, parent)

    if how == "revoked":
        _revoke_row(conn, parent, world.owner_a)
    else:
        # A minute, not a second: the row is dated by the DATABASE clock and the check
        # compares it with the SERVER's `_now()`, so a second would only hold while the
        # two clocks agree to within a second.
        with conn.cursor() as cur:
            cur.execute("UPDATE skill_grants SET expires_at = NOW() - INTERVAL '1 minute' "
                        "WHERE id = %s", (parent,))
            assert cur.rowcount == 1
    _refused_for(_delegate(client, world, parent),
                 "parent grant is not live (revoked or expired)")


def test_only_the_parents_grantee_may_delegate_it(client, conn, world):
    """Check 2, parent grantee == new grantor (delegation.py:60). The variant is owner_c
    - who holds nothing - citing owner_b's grant as its parent and granting ITSELF
    peer_a. Deleted, anyone who learns a grant id can lend themselves its slice."""
    parent = _parent(client, world)
    _control_then_revoke(client, conn, world, parent)
    _refused_for(_delegate(client, world, parent, grantor=world.owner_c),
                 "is not the new grantor")


def test_a_delegation_stays_on_the_parents_peer(client, conn, world):
    """Check 3, sec3c peer-constancy (delegation.py:65). The variant moves the child to
    peer_b - a peer the delegator really does own - which is the prohibited case: never the
    issuer's own peer."""
    parent = _parent(client, world)
    _control_then_revoke(client, conn, world, parent)
    _refused_for(_delegate(client, world, parent, peer=world.peer_b),
                 "differs from the parent's")


def test_a_delegation_cannot_name_another_skill(client, conn, world):
    """Check 4, skill coverage (delegation.py:71): a grant for one skill is not a grant
    for its neighbour on the same peer."""
    parent = _parent(client, world)
    _control_then_revoke(client, conn, world, parent)
    _refused_for(_delegate(client, world, parent, skill=f"{DELEGATED_SKILL}-other"),
                 "does not cover child skill")


def test_a_child_cannot_outlive_a_bounded_parent(client, conn, world):
    """Check 5 (delegation.py:75). The variant lives 200 s, INSIDE the 300 s ceiling, so
    only this check can refuse it; the parent ends in 90 s.

    The asymmetry check 5 is written around is the second control: the SAME 200 s child
    under an UNBOUNDED parent is accepted (an unbounded parent bounds nothing here)."""
    parent = _parent(client, world, ttl_s=90)
    _control_then_revoke(client, conn, world, parent, ttl_s=60)
    _refused_for(_delegate(client, world, parent, ttl_s=200),
                 "child expiry outlives the bounded parent")

    perpetual = _parent(client, world, skill=f"{DELEGATED_SKILL}-perpetual", ttl_s=None)
    _created_id(_delegate(client, world, perpetual, skill=f"{DELEGATED_SKILL}-perpetual",
                          ttl_s=200))


def test_a_child_cannot_add_a_scope_its_parent_lacks(client, conn, world):
    """Check 6, scope subset (delegation.py:84) - the STATUS C39 mutant, 40 of 40 green.

    Second variant: a parent with NO scopes. `None` is the EMPTY set, the least
    authority (scopes.py), so a child asking for any scope under it is widening; reading
    "no scopes" as "all scopes" would invert the check."""
    parent = _parent(client, world, scopes=["alpha"])
    _control_then_revoke(client, conn, world, parent, scopes=["alpha"])
    _refused_for(_delegate(client, world, parent, scopes=["alpha", "beta"]),
                 'child broadens scope beyond the parent (added: ["beta"])')

    unscoped = _parent(client, world, skill=f"{DELEGATED_SKILL}-unscoped")
    _refused_for(_delegate(client, world, unscoped, skill=f"{DELEGATED_SKILL}-unscoped",
                           scopes=["alpha"]),
                 'child broadens scope beyond the parent (added: ["alpha"])')


def test_a_child_execution_cap_is_bounded_by_the_parents_remaining(client, conn, world):
    """Check 7 (delegation.py:89): under a capped parent the child must be capped, and
    by what the parent has LEFT, not by its original cap."""
    parent = _parent(client, world, max_executions=3)
    _control_then_revoke(client, conn, world, parent, max_executions=3)

    _refused_for(_delegate(client, world, parent, max_executions=4),
                 "bounded by parent remaining executions (3); requested Some(4)")
    _refused_for(_delegate(client, world, parent),
                 "bounded by parent remaining executions (3); requested None")

    with conn.cursor() as cur:
        cur.execute("UPDATE skill_grants SET used_executions = 3 WHERE id = %s", (parent,))
        assert cur.rowcount == 1
    _refused_for(_delegate(client, world, parent, max_executions=1),
                 "parent execution cap is exhausted (3/3)")


def _patch_expiry(client, grant_id, expires_at, as_owner):
    return client.patch(f"/v1/grants/{grant_id}", data=json.dumps({"expires_at": expires_at}),
                        content_type="application/json", **bearer(as_owner))


def _stored_expiry(conn, grant_id):
    with conn.cursor() as cur:
        cur.execute("SELECT expires_at FROM skill_grants WHERE id = %s", (str(grant_id),))
        row = cur.fetchone()
    assert row is not None, grant_id
    return row[0]


@pytest.mark.parametrize("parent_ttl_s, reason", [
    (600, "child expiry outlives the bounded parent"),
    (None, "sub-delegation lifetime exceeds the 300s ceiling (or is unbounded)"),
], ids=["bounded-parent", "perpetual-parent"])
def test_a_patch_cannot_widen_a_delegated_child(client, conn, world, parent_ttl_s, reason):
    """STATUS C39, the SECOND entry point. `service.update_grant` re-runs
    `validate_delegation_provenance` on a delegated row (service.py:404-422), so a
    delegation that was attenuated at issuance cannot be widened afterwards by its
    grantor. Nothing tested it: turning `if existing.source == "delegated":` into
    `if False:` left test_grants green, and a PATCH with `expires_at: null` then made a
    120 s child PERPETUAL - past both its bounded parent and the 300 s ceiling that
    bounds the offline-revocation window.

    Two parents, because check 5 and the ceiling guard different things:
      * bounded (600 s): a widened child outlives it, and check 5 reports that first;
      * perpetual: check 5 bounds nothing, so only the 300 s ceiling can refuse.
    CONTROL: the grantor NARROWING the child (120 s -> 60 s) is a 200 that is stored.
    VARIANTS: clearing the expiry and moving it a year out, each a 400 carrying the
    check's own reason, with the stored expiry unchanged."""
    skill = f"{DELEGATED_SKILL}-patch"
    parent = _parent(client, world, skill=skill, ttl_s=parent_ttl_s)
    child = _created_id(_delegate(client, world, parent, skill=skill, ttl_s=120))

    narrowed = now() + _dt.timedelta(seconds=60)
    ok = _patch_expiry(client, child, iso(narrowed), world.owner_b)
    assert ok.status_code == 200, ok.content
    assert json.loads(ok.content)["expires_at"] == iso(narrowed)
    stored = _stored_expiry(conn, child)

    for widened in (None, iso(now() + _dt.timedelta(days=365))):
        _refused_for(_patch_expiry(client, child, widened, world.owner_b), reason)
        assert _stored_expiry(conn, child) == stored, f"a refused PATCH ({widened}) wrote"


# ------------------------------------------------- the cookie credential path (C40)
#
# STATUS C40 (RFQ:344-350). Before these tests, no test in this file set a cookie, so
# the first branch of `owner_auth.authenticate_owner` - the only AMBIENT credential - was
# never run: dropping `AND expires_at > NOW()` at owner_auth.py:177 left 40 of 40 green.
# The same RFQ row lists three more controls on this extractor that nothing falsified:
# the cookie-write CSRF check (:105), the PAT expiry (:203) and the MESHYCHAT_ALLOW_DEV_JWT
# gate (:215). Each gets a test here, each beside the legitimate request it must not
# break.
# Those line numbers are c5457ac's, where it was measured. Since STATUS B10 moved the
# cookie name and CSRF secret to the dashboard's readers, they are :161, :105, :187
# and :199.

STATE_SECRET = "grants-test-state-secret"


def _session(conn, owner_id, *, expires_in: _dt.timedelta) -> tuple[str, uuid.UUID]:
    """An `owner_sessions` row, stored as hex SHA-256 of the raw cookie value. Deleted with
    its owner (ON DELETE CASCADE)."""
    raw = f"sess_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    session_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO owner_sessions (id, owner_id, expires_at, token_hash) "
                    "VALUES (%s, %s, NOW() + %s, %s)",
                    (str(session_id), str(owner_id), expires_in, owner_auth.sha256_hex(raw)))
    return raw, session_id


def _cookie_client(raw: str) -> Client:
    browser = Client()
    browser.cookies[owner_auth.session_cookie_name()] = raw
    return browser


def test_a_session_cookie_authenticates_until_it_expires(client, conn, world):
    """ONE VARIABLE: the session row's `expires_at`. The same cookie on the same client is
    a 200 scoped to its owner, then a 401 E_SESSION_EXPIRED once the row is in the past.
    The last request adds a VALID dashboard Bearer token beside the dead cookie: a
    present-but-dead cookie must not fall through to another scheme."""
    _created_id(issue_grant_via_api(client, world, grantor=world.owner_a,
                                    grantee=world.owner_b, peer=world.peer_a,
                                    skill="cookie-read"))
    raw, session_id = _session(conn, world.owner_a, expires_in=_dt.timedelta(hours=1))
    browser = _cookie_client(raw)

    live = browser.get("/v1/grants/issued")
    assert live.status_code == 200, live.content
    assert [g["skill_name"] for g in json.loads(live.content)] == ["cookie-read"], (
        "the cookie did not resolve to the owner whose session it is")

    with conn.cursor() as cur:
        cur.execute("UPDATE owner_sessions SET expires_at = NOW() - INTERVAL '1 second' "
                    "WHERE id = %s", (str(session_id),))
        assert cur.rowcount == 1

    dead = browser.get("/v1/grants/issued")
    assert dead.status_code == 401, dead.content
    assert json.loads(dead.content)["error"] == "E_SESSION_EXPIRED"

    no_fallthrough = browser.get("/v1/grants/issued", **bearer(world.owner_a))
    assert no_fallthrough.status_code == 401, no_fallthrough.content
    assert json.loads(no_fallthrough.content)["error"] == "E_SESSION_EXPIRED"


def test_a_cookie_write_needs_the_csrf_token_bound_to_its_session(
        client, conn, world, monkeypatch):
    """The cookie is attached by the browser on its own, so a
    state-changing request riding it must also carry HMAC(secret, session id).

    Three refusals, then the control: no token; a token that is valid for ANOTHER live
    session of the same owner (the binding is to the session, not the owner); and, with
    no secret configured in a non-DEBUG build, even the right token (fail closed). The control
    is the same POST with the right token: 201."""
    monkeypatch.setenv("MESHYCHAT_STATE_SECRET", STATE_SECRET)
    raw, session_id = _session(conn, world.owner_a, expires_in=_dt.timedelta(hours=1))
    _other_raw, other_session = _session(conn, world.owner_a,
                                         expires_in=_dt.timedelta(hours=1))
    browser = _cookie_client(raw)
    body = json.dumps({"grantee_owner_id": str(world.owner_b),
                       "granter_peer_id": str(world.peer_a),
                       "skill_name": "cookie-write", "source": "direct"})

    def post(**headers):
        return browser.post("/v1/grants", data=body, content_type="application/json",
                            **headers)

    def refused(response):
        assert response.status_code == 403, response.content
        assert json.loads(response.content)["error"] == "E_CSRF_REQUIRED"

    refused(post())
    refused(post(HTTP_X_CSRF_TOKEN=owner_auth.csrf_token_for(other_session,
                                                             STATE_SECRET.encode())))
    good_token = owner_auth.csrf_token_for(session_id, STATE_SECRET.encode())
    monkeypatch.delenv("MESHYCHAT_STATE_SECRET")
    with override_settings(DEBUG=False):
        refused(post(HTTP_X_CSRF_TOKEN=good_token))
    monkeypatch.setenv("MESHYCHAT_STATE_SECRET", STATE_SECRET)

    created = post(HTTP_X_CSRF_TOKEN=good_token)
    assert created.status_code == 201, created.content
    assert json.loads(created.content)["grantor_owner_id"] == str(world.owner_a)


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_a_cookie_patch_or_delete_needs_the_csrf_token_too(
        client, conn, world, monkeypatch, method):
    """Only SAFE methods are exempt, so every state-changing method on
    a cookie needs the token, not just POST. The test above only sent a POST: rewriting
    the exemption as `if method != "POST": return True` left the suite green, and a
    cross-site page riding the victim's cookie could then PATCH a grant's expiry to
    `null` (perpetual) or DELETE (revoke) it.

    ONE VARIABLE: the `X-CSRF-Token` header. Without it, 403 E_CSRF_REQUIRED and the row
    is untouched. CONTROL: the same request with the session's token is a 200 that
    really cleared the expiry / revoked the grant."""
    monkeypatch.setenv("MESHYCHAT_STATE_SECRET", STATE_SECRET)
    grant_id = _created_id(issue_grant_via_api(
        client, world, grantor=world.owner_a, grantee=world.owner_b, peer=world.peer_a,
        skill=f"cookie-{method.lower()}", expires_at=now() + _dt.timedelta(hours=2)))
    raw, session_id = _session(conn, world.owner_a, expires_in=_dt.timedelta(hours=1))
    browser = _cookie_client(raw)

    def send(**headers):
        if method == "PATCH":
            return browser.patch(f"/v1/grants/{grant_id}",
                                 data=json.dumps({"expires_at": None}),
                                 content_type="application/json", **headers)
        return browser.delete(f"/v1/grants/{grant_id}", **headers)

    def row():
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at, revoked_at FROM skill_grants WHERE id = %s",
                        (grant_id,))
            return cur.fetchone()

    before = row()
    assert before[0] is not None and before[1] is None, before
    refused = send()
    assert refused.status_code == 403, refused.content
    assert json.loads(refused.content)["error"] == "E_CSRF_REQUIRED"
    assert row() == before, "a request refused for CSRF still changed the grant"

    done = send(HTTP_X_CSRF_TOKEN=owner_auth.csrf_token_for(session_id, STATE_SECRET.encode()))
    assert done.status_code == 200, done.content
    expires_at, revoked_at = row()
    if method == "PATCH":
        assert expires_at is None and revoked_at is None
    else:
        assert revoked_at is not None


def test_an_expired_pat_is_refused(client, conn, world):
    """A PAT is valid when it has no expiration or expires in the future. The suite revoked
    PATs but never expired one. ONE VARIABLE: the key's `expires_at`."""
    raw = world.issue_pat(world.owner_a, ["grants:read"])
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    assert client.get("/v1/grants/issued", **headers).status_code == 200

    with conn.cursor() as cur:
        cur.execute("UPDATE owner_api_keys SET expires_at = NOW() - INTERVAL '1 second' "
                    "WHERE token_hash = %s", (owner_auth.sha256_hex(raw),))
        assert cur.rowcount == 1
    dead = client.get("/v1/grants/issued", **headers)
    assert dead.status_code == 401, dead.content
    assert json.loads(dead.content)["error"] == "E_API_KEY_INVALID"


def test_meshy_allow_dev_jwt_off_closes_the_bearer_jwt_path_and_only_that(
        client, world, monkeypatch):
    """`MESHYCHAT_ALLOW_DEV_JWT=0` is the switch for the
    dev-JWT path (the dashboard's `MESHY_SSO_CONFIGURED` + `MESHY_ALLOW_DEV_JWT` posture
    is the other one; the test below holds the two together). `clean_env` deletes the
    variable, so no test ever set it.

    Control: with the variable unset the same Bearer JWT is a 200. With it off, 401
    E_AUTH_MISSING ("this server has no such credential"). The PAT path is ALWAYS ON
    so a PAT is still a 200 with the flag off: the gate closes
    one scheme, not authentication."""
    assert client.get("/v1/grants/issued", **bearer(world.owner_a)).status_code == 200
    pat = world.issue_pat(world.owner_a, ["grants:read"])

    monkeypatch.setenv("MESHYCHAT_ALLOW_DEV_JWT", "0")
    closed = client.get("/v1/grants/issued", **bearer(world.owner_a))
    assert closed.status_code == 401, closed.content
    assert json.loads(closed.content)["error"] == "E_AUTH_MISSING"
    assert client.get("/v1/grants/issued",
                      HTTP_AUTHORIZATION=f"Bearer {pat}").status_code == 200


#: The two owner extractors that accept a dashboard dev JWT, and a route on each.
_OWNER_SURFACES = {"grants": "/v1/grants/issued", "dashboard": "/v1/dashboard/agents"}

#: posture -> (environment, status EVERY owner surface must give the same dev JWT).
_DEV_JWT_POSTURES = {
    "no switch set": ({}, 200),
    "MESHYCHAT_ALLOW_DEV_JWT=1": ({"MESHYCHAT_ALLOW_DEV_JWT": "1"}, 200),
    "SSO configured, MESHY_ALLOW_DEV_JWT=1": (
        {"MESHY_SSO_CONFIGURED": "1", "MESHY_ALLOW_DEV_JWT": "1"}, 200),
    "MESHYCHAT_ALLOW_DEV_JWT=0": ({"MESHYCHAT_ALLOW_DEV_JWT": "0"}, 401),
    "SSO configured, MESHY_ALLOW_DEV_JWT=0": (
        {"MESHY_SSO_CONFIGURED": "1", "MESHY_ALLOW_DEV_JWT": "0"}, 401),
}


@pytest.mark.parametrize("posture", sorted(_DEV_JWT_POSTURES))
def test_one_dev_jwt_switch_closes_every_owner_surface(client, world, monkeypatch, posture):
    """The dashboard and /v1/grants have separate owner extractors but must reach one
    `bearer_allowed` decision. Grants read `MESHYCHAT_ALLOW_DEV_JWT`, while the dashboard (and /v1/auth/*,
    which use its extractor) read `MESHY_SSO_CONFIGURED` + `MESHY_ALLOW_DEV_JWT`.
    Measured on the unfixed tree with the same `aud=dashboard` token:

        MESHYCHAT_ALLOW_DEV_JWT=0                       grants 401   dashboard 200
        MESHY_SSO_CONFIGURED=1 MESHY_ALLOW_DEV_JWT=0   grants 200   dashboard 401

    so an operator who applied the dashboard's documented production posture (STATUS
    C77) still left grant issue and revoke open to dev JWTs.

    The rule now: EITHER switch turned off closes the path on BOTH surfaces. It is the
    conjunction of the two old answers, so no posture opens a surface that was closed.
    CONTROLS: the three postures that allow the dev JWT are a 200 on both surfaces, so
    the 401s are the switch and not a broken route or token."""
    for name in ("MESHY_SSO_CONFIGURED", "MESHY_ALLOW_DEV_JWT"):
        monkeypatch.delenv(name, raising=False)
    environment, expected = _DEV_JWT_POSTURES[posture]
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    token = bearer(world.owner_a)
    with override_settings(MESHY_SSO_CONFIGURED=None, MESHY_ALLOW_DEV_JWT=None):
        got = {surface: client.get(path, **token).status_code
               for surface, path in _OWNER_SURFACES.items()}
    assert got == {surface: expected for surface in _OWNER_SURFACES}, (posture, got)


# ====================================================== W1-grants hardening (2026-09-25)
#
# STATUS C36, C37, C41, C42, C43, C44 and C47 (RFQ:304-389, the `verify:grants` section).
# Each test below names its row. The rows fall into two kinds, and the tests say which:
#   * a LIVE DEFECT (C36, C37, C47's missing index): the test fails on the code before
#     the fix and passes after it;
#   * a SURVIVOR (C42-C44): the code was right and nothing could tell. There the test is
#     proved by the mutant it kills - the named mutation leaves every older test green
#     and turns this one red.
#   C41 is both: each PATCH tenant layer was a survivor, and a pin-CLEARING PATCH had no
#   service-level owner check at all, which the layer-2 test caught before the fix.


class _RecordingConn:
    """A psycopg connection that records every (sql, params) its cursors execute.

    Used to EXPLAIN the statement a repository function REALLY runs. RFQ:367 (C44): the
    old plan test retyped the SQL inside the test, so it measured a string nobody
    executes and could not fail when the function's own query changed."""

    def __init__(self, inner):
        self._inner = inner
        self.executed: list[tuple[str, object]] = []

    def cursor(self, *args, **kwargs):
        return _RecordingCursor(self._inner.cursor(*args, **kwargs), self.executed)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _RecordingCursor:
    def __init__(self, inner, log):
        self._inner = inner
        self._log = log

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def execute(self, sql, params=None, **kwargs):
        self._log.append((sql, params))
        return self._inner.execute(sql, params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _the_one_statement(fn, conn) -> tuple[str, object]:
    rec = _RecordingConn(conn)
    fn(rec)
    assert len(rec.executed) == 1, f"expected one statement, ran {len(rec.executed)}"
    return rec.executed[0]


def _plan(conn, sql, params) -> str:
    """EXPLAIN with sequential scans priced out, so a plan that still scans the table has
    no index that can answer the query. On a near-empty test table the planner prefers a
    Seq Scan whatever indexes exist; `enable_seqscan = off` separates "chose not to" from
    "could not" - the RFQ's 1e10-cost Seq Scan is the second."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("EXPLAIN (FORMAT TEXT) " + sql, params)
        return "\n".join(r[0] for r in cur.fetchall())


def _index_cond_on(plan: str, column: str) -> bool:
    return any("Index Cond" in line and f"{column} =" in line for line in plan.splitlines())


# ------------------------------------------------------------------- C36 max_executions

def test_max_executions_is_refused_before_the_database_unless_a_positive_i32(client, conn, world):
    """STATUS C36 (RFQ:304-311), the 4xx half. 0 and negatives reached the DB CHECK
    `skill_grants_max_executions_positive` and answered 500; 2**31 reached an int4 column
    and answered 500 (NumericValueOutOfRange). 2**31 is refused as 422. 0 and -5 are
    400 E_VALIDATION; this app refuses them first
    (views.py module docstring, divergence 3)."""
    skill = "cap-validate"
    for bad, status in ((0, 400), (-5, 400), (2 ** 31, 422)):
        r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                peer=world.peer_a, skill=skill, extra={"max_executions": bad})
        assert r.status_code == status, (bad, r.status_code, r.content[:200])
        assert r["Content-Type"] == "application/json"
        body = json.loads(r.content)
        assert body["error"] == "E_VALIDATION", body
        assert "max_executions" in body["message"], body
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_grants WHERE grantor_owner_id = %s "
                    "AND skill_name = %s", (str(world.owner_a), skill))
        assert cur.fetchone()[0] == 0, "a refused cap wrote a row"

    # CONTROL: the largest legal value is a 201 and is stored as sent.
    ok = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                             peer=world.peer_a, skill=skill,
                             extra={"max_executions": 2 ** 31 - 1})
    assert ok.status_code == 201, ok.content
    assert json.loads(ok.content)["max_executions"] == 2 ** 31 - 1

    # ORDER: the scope gate comes first, so a read-only key learns nothing about the value.
    read_only = world.issue_pat(world.owner_a, ["grants:read"])
    gated = post_json(client, "/v1/grants",
                      {"grantee_owner_id": str(world.owner_b),
                       "granter_peer_id": str(world.peer_a), "skill_name": skill,
                       "source": "direct", "max_executions": 0},
                      {"HTTP_AUTHORIZATION": f"Bearer {read_only}"})
    assert gated.status_code == 403, gated.content
    assert json.loads(gated.content)["error"] == "E_SCOPE_REQUIRED"


# ------------------------------------------------------------ C37 extractor ordering

def test_a_malformed_request_is_refused_before_the_scope_gate(client, world):
    """STATUS C37 (RFQ:312-320). The request body, path and query are parsed before the
    handler body, where `require_scope` runs. A malformed request is therefore 400/415/422
    even from a key that lacks the scope; running the scope check first would answer 403."""
    read_only = world.issue_pat(world.owner_a, ["grants:read"])
    ro = {"HTTP_AUTHORIZATION": f"Bearer {read_only}"}
    scopeless = world.issue_pat(world.owner_a, [])
    none = {"HTTP_AUTHORIZATION": f"Bearer {scopeless}"}
    good = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "ordering", "source": "direct"}

    # CONTROLS: both keys are authentic, and the gate still refuses a WELL-FORMED write
    # and a well-formed list - so every 4xx below is the parse, not a dead gate.
    assert client.get("/v1/grants/issued", **ro).status_code == 200
    refused = post_json(client, "/v1/grants", good, ro)
    assert refused.status_code == 403
    assert json.loads(refused.content)["error"] == "E_SCOPE_REQUIRED"
    assert client.get("/v1/grants/issued", **none).status_code == 403
    assert client.delete(f"/v1/grants/{uuid.uuid4()}", **ro).status_code == 403

    cases = {
        "POST syntax": (client.post("/v1/grants", data="{not json",
                                    content_type="application/json", **ro), 400),
        "POST content-type": (client.post("/v1/grants", data=json.dumps(good),
                                          content_type="text/plain", **ro), 415),
        "POST missing field": (post_json(client, "/v1/grants",
                                         {k: v for k, v in good.items()
                                          if k != "grantee_owner_id"}, ro), 422),
        "POST bad uuid": (post_json(client, "/v1/grants",
                                    dict(good, granter_peer_id="nope"), ro), 422),
        "POST cap type": (post_json(client, "/v1/grants",
                                    dict(good, max_executions="3"), ro), 422),
        "DELETE path": (client.delete("/v1/grants/not-a-uuid", **ro), 400),
        "PATCH path": (client.patch("/v1/grants/not-a-uuid", data=json.dumps({}),
                                    content_type="application/json", **ro), 400),
        "PATCH syntax": (client.patch(f"/v1/grants/{uuid.uuid4()}", data="{x",
                                      content_type="application/json", **ro), 400),
        "PATCH type": (client.patch(f"/v1/grants/{uuid.uuid4()}",
                                    data=json.dumps({"expires_at": 7}),
                                    content_type="application/json", **ro), 422),
        "GET query": (client.get("/v1/grants/issued?include_revoked=yes", **none), 400),
        "GET held query": (client.get("/v1/grants/held?include_revoked=1", **none), 400),
    }
    wrong = {label: (r.status_code, want) for label, (r, want) in cases.items()
             if r.status_code != want}
    assert not wrong, f"(got, wanted) per case: {wrong}"


def test_the_whole_issue_body_is_parsed_before_the_mandate_is_verified(client, world):
    """STATUS C37 cases 3 and 4. `_uuid_field(...)` used to be evaluated as a call
    ARGUMENT, after the mandate block had already hit the database, and the mandate's
    UUID fields were checked for type only. A missing body field is 422 before the handler
    runs, and a non-UUID `owner` is rejected as `invalid mandate: ...` - 400."""
    import base64
    headers = bearer(world.owner_a)
    mandate = {"owner": str(world.owner_a), "grantee": str(world.owner_b),
               "target_peer": str(world.peer_a), "skill": "ordering-m", "scopes": [],
               "expires_at": int((now() + _dt.timedelta(hours=1)).timestamp()),
               "budget": None}
    sig = base64.b64encode(b"\x01" * 64).decode()
    body = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "ordering-m", "source": "direct",
            "expires_at": iso(now() + _dt.timedelta(minutes=10)),
            "mandate": {"mandate": mandate, "sig": sig}}

    # CONTROL: the well-formed body with an unverifiable signature is the 403, so the
    # two answers below are about the shape and not about mandates being refused.
    denied = post_json(client, "/v1/grants", body, headers)
    assert denied.status_code == 403, denied.content

    missing = post_json(client, "/v1/grants",
                        {k: v for k, v in body.items() if k != "grantee_owner_id"}, headers)
    assert missing.status_code == 422, missing.content
    assert "grantee_owner_id" in json.loads(missing.content)["message"]

    for field in ("owner", "grantee", "target_peer"):
        bad = dict(body, mandate={"mandate": dict(mandate, **{field: "not-a-uuid"}),
                                  "sig": sig})
        r = post_json(client, "/v1/grants", bad, headers)
        assert r.status_code == 400, (field, r.content)
        assert json.loads(r.content)["message"].startswith("invalid mandate"), r.content


# --------------------------------------------------------- C41 PATCH tenant layers

def _stored_grant(conn, grant_id):
    found = repository.find_grant_by_id(conn, grant_id)
    assert isinstance(found, repository.Found), grant_id
    return found.value


def test_patch_owner_scoping_holds_at_each_layer_on_its_own(client, conn, world, monkeypatch):
    """STATUS C41 (RFQ:353-355). PATCH is scoped twice - the service's owner check and
    the repository's `AND grantor_owner_id = %s` - and each hid the other's deletion.
    Worse, a `version_pin`-only PATCH that CLEARS the pin had no service-level check at
    all, so the WHERE clause was its only scoping. Each layer is now tested with the
    other one out of the way."""
    gid = _created_id(issue_grant_via_api(
        client, world, grantor=world.owner_a, grantee=world.owner_b, peer=world.peer_a,
        skill="two-layers", expires_at=now() + _dt.timedelta(hours=2)))
    before = _stored_grant(conn, gid)

    # LAYER 1, the repository alone: another owner's UPDATE matches no row...
    wrong = repository.update_grant(
        conn, gid, world.owner_b, update_expires_at=True, new_expires_at=None,
        update_version_pin=True, new_version_pin=None, update_manifest_hash=True,
        new_manifest_hash=None, update_approved_human_auth=False,
        new_approved_human_auth=False)
    assert isinstance(wrong, repository.NotFound), wrong
    assert _stored_grant(conn, gid) == before, "the repository wrote another owner's row"
    # ...and the grantor's identical UPDATE (a no-op pin clear) matches it: the CONTROL.
    right = repository.update_grant(
        conn, gid, world.owner_a, update_expires_at=False, new_expires_at=None,
        update_version_pin=True, new_version_pin=None, update_manifest_hash=True,
        new_manifest_hash=None, update_approved_human_auth=False,
        new_approved_human_auth=False)
    assert isinstance(right, repository.Found), right

    # LAYER 2, the service alone: with a spy in place of the repository write, another
    # owner is refused BEFORE any write is attempted, for every PATCH shape.
    writes = []
    real = repository.update_grant

    def spy(*args, **kwargs):
        writes.append(args[2])
        return real(*args, **kwargs)

    monkeypatch.setattr(repository, "update_grant", spy)
    shapes = {
        "expiry": dict(expires_at_present=True, new_expires_at=None,
                       version_pin_present=False, new_version_pin=None),
        "pin clear": dict(expires_at_present=False, new_expires_at=None,
                          version_pin_present=True, new_version_pin=None),
        "pin set": dict(expires_at_present=False, new_expires_at=None,
                        version_pin_present=True, new_version_pin="1.0.0"),
    }
    for label, shape in shapes.items():
        with pytest.raises(service.GrantNotFound):
            service.update_grant(conn, gid, world.owner_b, **shape)
        assert writes == [], f"{label}: the service let another owner reach the write"
    # CONTROL: the grantor's pin clear reaches the write and succeeds.
    service.update_grant(conn, gid, world.owner_a, **shapes["pin clear"])
    assert [str(o) for o in writes] == [str(world.owner_a)]

    # And over HTTP the pin-clear shape is the same 404 as the others, row unchanged.
    monkeypatch.setattr(repository, "update_grant", real)
    r = client.patch(f"/v1/grants/{gid}", data=json.dumps({"version_pin": None}),
                     content_type="application/json", **bearer(world.owner_b))
    assert r.status_code == 404, r.content
    assert _stored_grant(conn, gid).expires_at == before.expires_at


# --------------------------------------------------- C42 the pending guards, apart

def _pending(client, world, skill):
    r = submit_request(client, world, requester=world.owner_b, granter_owner=world.owner_a,
                       granter_peer=world.peer_a, skill=skill)
    assert r.status_code == 201, r.content
    return json.loads(r.content)["id"]


def test_the_sql_pending_guard_refuses_a_decided_request_on_its_own(client, conn, world):
    """STATUS C42 (RFQ:356-358), layer 1. `AND status = 'pending'` on
    update_request_decision and expire_request, called directly so the service
    pre-check cannot answer first."""
    rid = _pending(client, world, "cas-denied")
    assert post_json(client, f"/v1/access-requests/{rid}/deny", {"note": "no"},
                     bearer(world.owner_a)).status_code == 200
    denied = repository.find_request_by_id(conn, rid).value

    again = repository.update_request_decision(conn, rid, "approved", world.owner_a,
                                               "late", None)
    assert isinstance(again, repository.NotFound), again
    expired = repository.expire_request(conn, rid)
    assert isinstance(expired, repository.NotFound), expired
    assert repository.find_request_by_id(conn, rid).value == denied, "a terminal row moved"

    # CONTROLS: the same two calls on PENDING rows succeed.
    live = _pending(client, world, "cas-live")
    ok = repository.update_request_decision(conn, live, "approved", world.owner_a, "y", None)
    assert isinstance(ok, repository.Found) and ok.value.status == "approved"
    live2 = _pending(client, world, "cas-live-2")
    ok2 = repository.expire_request(conn, live2)
    assert isinstance(ok2, repository.Found) and ok2.value.status == "expired"
    assert ok2.value.decided_by is None


def test_an_approve_that_loses_the_race_rolls_its_grant_back(client, conn, world, monkeypatch):
    """STATUS C42, the mechanism the service docstring calls THE THREE TRANSACTIONS.
    The request is withdrawn by another connection AFTER approve's pre-check passed and
    AFTER it inserted the grant. The compare-and-set must miss, and the grant must roll
    back with the transaction: a withdrawn request with a live grant is an
    authorisation nobody approved."""
    rid = _pending(client, world, "race-lost")
    real = repository.insert_grant

    def insert_then_lose_the_race(*args, **kwargs):
        row = real(*args, **kwargs)
        with psycopg.connect(DSN, autocommit=True) as other:
            other.execute("UPDATE skill_access_requests SET status = 'withdrawn', "
                          "decided_at = NOW(), decided_by = %s WHERE id = %s",
                          (str(world.owner_b), rid))
        return row

    monkeypatch.setattr(repository, "insert_grant", insert_then_lose_the_race)
    lost = post_json(client, f"/v1/access-requests/{rid}/approve", {}, bearer(world.owner_a))
    assert lost.status_code == 409, lost.content
    assert json.loads(lost.content)["message"] == (
        "request status is Withdrawn; expected pending")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_grants WHERE source_ref = %s", (rid,))
        assert cur.fetchone()[0] == 0, "the grant of a lost approve survived"
    assert repository.find_request_by_id(conn, rid).value.status == "withdrawn"

    # CONTROL: the same approve without the interleaving mints its grant.
    monkeypatch.setattr(repository, "insert_grant", real)
    rid2 = _pending(client, world, "race-won")
    won = post_json(client, f"/v1/access-requests/{rid2}/approve", {}, bearer(world.owner_a))
    assert won.status_code == 200, won.content
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_grants WHERE source_ref = %s", (rid2,))
        assert cur.fetchone()[0] == 1


def test_the_pending_pre_check_refuses_before_any_write(client, conn, world, monkeypatch):
    """STATUS C42, layer 2: `_load_pending`'s status check, with the SQL guard unable to
    answer first. Deleted, a second approve of an APPROVED request reaches insert_grant,
    trips the active-grant index and answers the wrong 409 ("active grant already
    exists"), and a decided request always costs an insert that rolls back."""
    rid = _pending(client, world, "precheck")
    first = post_json(client, f"/v1/access-requests/{rid}/approve", {}, bearer(world.owner_a))
    assert first.status_code == 200, first.content   # CONTROL: approve works.

    inserts = []
    real = repository.insert_grant
    monkeypatch.setattr(repository, "insert_grant",
                        lambda *a, **k: inserts.append(1) or real(*a, **k))
    again = post_json(client, f"/v1/access-requests/{rid}/approve", {}, bearer(world.owner_a))
    assert again.status_code == 409
    assert json.loads(again.content)["message"] == (
        "request status is Approved; expected pending")
    assert inserts == [], "a decided request reached the grant insert"

    denied = _pending(client, world, "precheck-denied")
    assert post_json(client, f"/v1/access-requests/{denied}/deny", {},
                     bearer(world.owner_a)).status_code == 200
    assert post_json(client, f"/v1/access-requests/{denied}/approve", {},
                     bearer(world.owner_a)).status_code == 409
    assert inserts == [], "a denied request reached the grant insert"


# ---------------------------------------------------------- C43 six more survivors

def test_revoke_is_a_compare_and_set_that_keeps_the_first_reason(client, conn, world,
                                                                 monkeypatch):
    """STATUS C43 (RFQ:359), repository.py revoke_grant's `AND revoked_at IS NULL`. The
    service pre-check answered first, so the clause could go. Called directly, and then
    through a revoke that loses a race after its pre-check."""
    gid = _created_id(issue_grant_via_api(client, world, grantor=world.owner_a,
                                          grantee=world.owner_b, peer=world.peer_a,
                                          skill="cas-revoke"))
    first = repository.revoke_grant(conn, gid, world.owner_a, "first")
    assert isinstance(first, repository.Found)            # CONTROL: a live row revokes.
    second = repository.revoke_grant(conn, gid, world.owner_a, "second")
    assert isinstance(second, repository.NotFound), second
    stored = _stored_grant(conn, gid)
    assert stored.revoke_reason == "first" and stored.revoked_at == first.value.revoked_at

    # Through the service: the pre-check sees a LIVE row, another connection revokes it
    # before the UPDATE, and the loser must answer 409 without overwriting the reason.
    gid2 = _created_id(issue_grant_via_api(client, world, grantor=world.owner_a,
                                           grantee=world.owner_b, peer=world.peer_a,
                                           skill="cas-revoke-race"))
    real = repository.revoke_grant

    def lose_the_race(conn_, grant_id, revoked_by, reason):
        with psycopg.connect(DSN, autocommit=True) as other:
            other.execute("UPDATE skill_grants SET revoked_at = NOW(), revoked_by = %s, "
                          "revoke_reason = 'winner' WHERE id = %s",
                          (str(world.owner_a), str(grant_id)))
        return real(conn_, grant_id, revoked_by, reason)

    monkeypatch.setattr(repository, "revoke_grant", lose_the_race)
    r = client.delete(f"/v1/grants/{gid2}?reason=loser", **bearer(world.owner_a))
    assert r.status_code == 409, r.content
    assert json.loads(r.content)["message"] == "grant already revoked"
    assert _stored_grant(conn, gid2).revoke_reason == "winner"


def test_only_the_named_unique_index_is_a_duplicate(conn, world):
    """STATUS C43 (RFQ:359-360), the constraint-name discrimination in insert_grant and
    insert_request. The only other unique index on either table is the primary key,
    which a caller cannot reach, so a second partial unique index is created for the
    test and dropped after it: a violation of IT must propagate as itself, never as the
    domain's 409."""
    tag = uuid.uuid4().hex[:12]
    g_idx, r_idx = f"test_uv_grants_{tag}", f"test_uv_requests_{tag}"
    probe = f"uv-probe-{tag}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE UNIQUE INDEX {g_idx} ON skill_grants (source_ref) "
                    f"WHERE source_ref = '{probe}'")
        cur.execute(f"CREATE UNIQUE INDEX {r_idx} ON skill_access_requests (message) "
                    f"WHERE message = '{probe}'")
    try:
        def grant(skill, source_ref):
            return repository.insert_grant(
                conn, grantor_owner_id=world.owner_a, grantee_owner_id=world.owner_b,
                granter_peer_id=world.peer_a, skill_name=skill, version_pin=None,
                approved_manifest_hash=None, source="direct", source_ref=source_ref,
                expires_at=None, scopes=None, max_executions=None,
                approved_requires_human_auth=False)

        def request(skill, message):
            return repository.insert_request(
                conn, requester_owner_id=world.owner_b, granter_owner_id=world.owner_a,
                granter_peer_id=world.peer_a, skill_name=skill, message=message,
                expires_at=now() + _dt.timedelta(days=1))

        # CONTROLS: the domain index IS mapped to the domain error.
        grant("uv-a", None)
        with pytest.raises(repository.DuplicateActiveGrant):
            grant("uv-a", None)
        request("uv-a", None)
        with pytest.raises(repository.DuplicatePendingRequest):
            request("uv-a", None)

        # Any other unique violation keeps its own type and constraint name.
        grant("uv-b", probe)
        with pytest.raises(psycopg.errors.UniqueViolation) as g_exc:
            grant("uv-c", probe)
        assert g_exc.value.diag.constraint_name == g_idx
        request("uv-b", probe)
        with pytest.raises(psycopg.errors.UniqueViolation) as r_exc:
            request("uv-c", probe)
        assert r_exc.value.diag.constraint_name == r_idx
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {g_idx}")
            cur.execute(f"DROP INDEX IF EXISTS {r_idx}")


def test_a_malformed_flow_scope_is_refused_and_a_self_grant_cannot_stamp_one(client, world):
    """STATUS C43 (RFQ:360-361): scopes.py's two ScopeError raises and service.py's
    self-grant refusal. A `meshy:flow:` string must parse exactly or the issuance is
    refused (403); a well-formed one may not be stamped on a self-grant."""
    def issue(grantee, skill, scopes):
        return issue_grant_via_api(client, world, grantor=world.owner_a, grantee=grantee,
                                   peer=world.peer_a, skill=skill, extra={"scopes": scopes})

    flow = f"meshy:flow:accept-origin:owner:{world.owner_c}"
    # CONTROLS: a well-formed flow scope to ANOTHER owner, an inert non-flow string, and
    # a self-grant with no flow scope are all 201.
    assert issue(world.owner_b, "flow-ok", [flow]).status_code == 201
    assert issue(world.owner_b, "flow-inert", ["meshy:other:thing"]).status_code == 201
    assert issue(world.owner_a, "self-plain", ["read"]).status_code == 201

    refused = {
        "wrong family word": issue(world.owner_b, "flow-bad-1",
                                   [f"meshy:flow:accept-origin:peer:{world.owner_c}"]),
        "too few parts": issue(world.owner_b, "flow-bad-2", ["meshy:flow:accept-origin"]),
        "not a uuid": issue(world.owner_b, "flow-bad-3",
                            ["meshy:flow:accept-origin:owner:not-a-uuid"]),
        "self-grant": issue(world.owner_a, "flow-self", [flow]),
    }
    got = {label: r.status_code for label, r in refused.items()}
    assert got == {label: 403 for label in refused}, got
    for r in refused.values():
        assert json.loads(r.content) == {"error": "E_FORBIDDEN", "message": "forbidden"}


def test_tag_order_is_not_manifest_drift():
    """STATUS C43 (RFQ:362), manifest.py's `sorted(...)` on tags. The golden fixture's
    tags are already sorted, so the golden test cannot see the sort go."""
    fixture = {
        "peer_id": str(uuid.UUID(int=0x11)), "name": "summarize", "version": "1.0.0",
        "mode": "sync", "description": "Summarize a document",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "output_schema": {"type": "string"}, "attachments_allowed": False,
        "returns_attachments": False, "requires_human_auth": False,
        "tags": ["text", "nlp"], "runtime_profile": None,
    }
    assert gmanifest.manifest_hash(fixture).hex() == (
        "1b7afe0f1858394e4b4a21d4041fbf6267091901b4c7de80cbae946487d4c8fa")
    # CONTROL: a different tag SET is drift.
    assert gmanifest.manifest_hash(dict(fixture, tags=["nlp", "text", "x"])).hex() != (
        "1b7afe0f1858394e4b4a21d4041fbf6267091901b4c7de80cbae946487d4c8fa")


def test_a_declared_runtime_profile_is_refused_and_an_absent_one_is_legacy():
    """STATUS C43 (RFQ:362-363), manifest.py's ManifestUnsupported. The application does not
    implement the v1.1 normalization, so a declared profile must refuse rather than pin
    a hash it cannot reproduce; the absent forms must hash exactly as legacy."""
    fixture = {
        "peer_id": str(uuid.UUID(int=0x11)), "name": "summarize", "version": "1.0.0",
        "mode": "sync", "description": "Summarize a document",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "output_schema": {"type": "string"}, "attachments_allowed": False,
        "returns_attachments": False, "requires_human_auth": False,
        "tags": ["nlp", "text"], "runtime_profile": None,
    }
    golden = "1b7afe0f1858394e4b4a21d4041fbf6267091901b4c7de80cbae946487d4c8fa"
    for absent in (None, {}, {"policy_tags": [], "tool_interfaces": None}):
        assert gmanifest.manifest_hash(dict(fixture, runtime_profile=absent)).hex() == golden
    for declared in ({"policy_tags": ["net"]}, {"tool_interfaces": {"http": True}}, ["x"]):
        with pytest.raises(gmanifest.ManifestUnsupported):
            gmanifest.manifest_hash(dict(fixture, runtime_profile=declared))


# ---------------------------------------------------------------- C44 laws that can fail

def _skill_row(conn, peer_id, name, version="1.0.0", **extra):
    cols = {"peer_id": str(peer_id), "name": name, "version": version, "mode": "sync",
            "share_mode": "grants_only", "input_schema": json.dumps({"type": "object"}),
            "description": "does one thing", "tags": ["b", "a"]}
    cols.update(extra)
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO skills ({', '.join(cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))})", list(cols.values()))


def _pinned_hash(conn, peer_id, name, version="1.0.0") -> bytes:
    pin, h = service._resolve_manifest_pin(conn, peer_id, name, version)
    assert pin == version and len(h) == 32
    return h


def test_the_manifest_exclusion_law_on_a_real_skills_row(conn, world):
    """STATUS C44 (RFQ:366, the old `:284`). The old assertion added `price_cents` to a
    DICT and could not fail: manifest_hash reads a fixed key list, so any extra key is
    ignored by construction. The law is about the SKILL: changing a column the hash
    excludes must not move the pin, from the row in Postgres through the resolver that
    selects it. Changing an included column must."""
    _skill_row(conn, world.peer_a, "law")
    base = _pinned_hash(conn, world.peer_a, "law")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE skills SET display_name = 'Shiny', price_cents = 999, currency = 'usd', "
            "share_mode = 'marketplace_paid', max_runtime_ms = 5, "
            "examples = '[{\"in\": 1}]'::jsonb, runtime_status = '{\"ok\": true}'::jsonb, "
                "meshychat_guard_status = 'approved', is_current = FALSE, tags = ARRAY['a', 'b'] "
            "WHERE peer_id = %s AND name = 'law'", (str(world.peer_a),))
    assert _pinned_hash(conn, world.peer_a, "law") == base, "an excluded column moved the pin"
    # CONTROL: an INCLUDED column moves it, so the equality above is not a constant.
    with conn.cursor() as cur:
        cur.execute("UPDATE skills SET description = 'does another thing' "
                    "WHERE peer_id = %s AND name = 'law'", (str(world.peer_a),))
    assert _pinned_hash(conn, world.peer_a, "law") != base
    # And a declared runtime_profile refuses the pin (C43), end to end.
    with conn.cursor() as cur:
        cur.execute("UPDATE skills SET runtime_profile = '{\"policy_tags\": [\"net\"]}'::jsonb "
                    "WHERE peer_id = %s AND name = 'law'", (str(world.peer_a),))
    with pytest.raises(service.ManifestPinFailed):
        service._resolve_manifest_pin(conn, world.peer_a, "law", "1.0.0")


def test_the_authorize_lookup_it_runs_is_an_index_probe(conn, world):
    """STATUS C44 (RFQ:367, the old `:298`). The old test EXPLAINed a SQL string retyped
    inside the test. This one EXPLAINs the statement `find_active_grant` actually
    executes, and asserts both halves of the docstring's claim: no Aggregate node, and
    an index condition on the key rather than a filter over the table."""
    sql, params = _the_one_statement(
        lambda c: repository.find_active_grant(c, world.owner_a, world.owner_b,
                                               world.peer_a, "s"), conn)
    plan = _plan(conn, sql, params)
    assert "Aggregate" not in plan, plan
    assert _index_cond_on(plan, "grantee_owner_id"), plan
    # CONTROLS: the same instrument sees an Aggregate, and sees a table scan when no
    # index can answer (`source` is unindexed).
    assert "Aggregate" in _plan(conn, "SELECT count(*) FROM skill_grants", None)
    assert "Seq Scan" in _plan(conn, "SELECT id FROM skill_grants WHERE source = %s",
                               ("direct",))


# ------------------------------------------------------------- C47 the list reads' cost

_LIST_READS = {
    "issued": ("grantor_owner_id",
               lambda c, w: repository.list_issued(c, w.owner_a, False)),
    "issued+revoked": ("grantor_owner_id",
                       lambda c, w: repository.list_issued(c, w.owner_a, True)),
    "held": ("grantee_owner_id", lambda c, w: repository.list_held(c, w.owner_b, False)),
    "held+revoked": ("grantee_owner_id",
                     lambda c, w: repository.list_held(c, w.owner_b, True)),
    "sent": ("requester_owner_id",
             lambda c, w: repository.list_requests_sent(c, w.owner_b, None)),
    "sent?status": ("requester_owner_id",
                    lambda c, w: repository.list_requests_sent(c, w.owner_b, "denied")),
    "received": ("granter_owner_id",
                 lambda c, w: repository.list_requests_received(c, w.owner_a, None)),
    "received?status": ("granter_owner_id",
                        lambda c, w: repository.list_requests_received(c, w.owner_a,
                                                                       "pending")),
}


@pytest.mark.parametrize("read", sorted(_LIST_READS))
def test_every_list_read_is_an_index_range_on_its_owner(conn, world, read):
    """STATUS C47 (RFQ:383-389). `/v1/access-requests/sent|received` could use no index
    at all (the RFQ measured a Seq Scan at cost 1e10: the table has only its primary key
    and a partial unique index WHERE status = 'pending'), and the two grant lists could
    use one only while `include_revoked` was false. Each read's REAL statement is
    EXPLAINed; the owner column must be an index condition (sql_meshychat/020)."""
    column, fn = _LIST_READS[read]
    sql, params = _the_one_statement(lambda c: fn(c, world), conn)
    plan = _plan(conn, sql, params)
    assert _index_cond_on(plan, column), f"{read}: no index condition on {column}\n{plan}"
    # CONTROL: the instrument reports a table scan on the same table when nothing indexes
    # the predicate, so a pass above is not "every plan looks indexed".
    table = "skill_grants" if column in ("grantor_owner_id", "grantee_owner_id") \
        else "skill_access_requests"
    unindexed = "source" if table == "skill_grants" else "message"
    control = _plan(conn, f"SELECT id FROM {table} WHERE {unindexed} = %s", ("x",))
    assert "Seq Scan" in control and not _index_cond_on(control, unindexed), control


# ------------------------------------------ C47 opt-in keyset pages (operator 2026-09-25)

def _ids(response) -> list[str]:
    assert response.status_code == 200, response.content
    return [row["id"] for row in json.loads(response.content)]


def _walk(client, path: str, headers: dict, limit: int) -> list[list[str]]:
    """Every page of `path` at `limit`, each next page `before=<the last row's id>`, until a
    page comes back empty. Returns the pages, the empty one last."""
    pages, before = [], None
    for _ in range(50):
        sep = "&" if "?" in path else "?"
        query = f"{sep}limit={limit}" + ("" if before is None else f"&before={before}")
        page = _ids(client.get(path + query, **headers))
        pages.append(page)
        if not page:
            return pages
        assert len(page) <= limit, (path, limit, page)
        before = page[-1]
    raise AssertionError(f"{path}: no empty page after 50")


def test_the_four_list_reads_page_by_keyset_when_asked_and_stay_whole_when_not(
        client, conn, world):
    """STATUS C47, operator decision 2026-09-25 (divergence 045): `?limit=&before=` pages
    /v1/grants/issued, /v1/grants/held, /v1/access-requests/sent and /received, newest first,
    ties broken by id, the next page being `before=<the last row's id>`. Without either
    parameter the answer is the whole list, as before (no wire change). CONTROLS: the pages,
    joined, are exactly the whole list in the paged order, with no row twice and none missing,
    across rows that share one timestamp; another owner's rows never appear."""
    a, b = bearer(world.owner_a), bearer(world.owner_b)
    grants = [json.loads(issue_grant_via_api(client, world, grantor=world.owner_a,
                                             grantee=world.owner_b, peer=world.peer_a,
                                             skill=f"paged-{i}").content)["id"]
              for i in range(5)]
    requests = [json.loads(submit_request(client, world, requester=world.owner_b,
                                          granter_owner=world.owner_a,
                                          granter_peer=world.peer_a,
                                          skill=f"paged-req-{i}").content)["id"]
                for i in range(3)]
    # Three grants and two requests share one timestamp: only the id orders them.
    tie = now() - _dt.timedelta(minutes=1)
    conn.execute("UPDATE skill_grants SET granted_at = %s WHERE id = ANY(%s::uuid[])",
                 (tie, grants[1:4]))
    conn.execute("UPDATE skill_access_requests SET created_at = %s WHERE id = ANY(%s::uuid[])",
                 (tie, requests[:2]))
    # Noise: a grant C issued to B, on B's held list and on no list of A's.
    noise = json.loads(issue_grant_via_api(client, world, grantor=world.owner_c,
                                           grantee=world.owner_b,
                                           peer=world._peer(world.owner_c, "peer-c"),
                                           skill="paged-noise").content)["id"]

    def keyset(table: str, column: str, owner, stamp: str) -> list[str]:
        return [str(r[0]) for r in conn.execute(
            f"SELECT id FROM {table} WHERE {column} = %s ORDER BY {stamp} DESC, id DESC",
            (str(owner),)).fetchall()]

    cases = [
        ("/v1/grants/issued", a, keyset("skill_grants", "grantor_owner_id", world.owner_a,
                                        "granted_at"), 2),
        ("/v1/grants/held", b, keyset("skill_grants", "grantee_owner_id", world.owner_b,
                                      "granted_at"), 3),
        ("/v1/access-requests/sent", b,
         keyset("skill_access_requests", "requester_owner_id", world.owner_b, "created_at"), 1),
        ("/v1/access-requests/received", a,
         keyset("skill_access_requests", "granter_owner_id", world.owner_a, "created_at"), 2),
    ]
    for path, headers, expected, limit in cases:
        whole = _ids(client.get(path, **headers))
        assert sorted(whole) == sorted(expected) and len(whole) >= 3, (path, whole)
        assert (noise in whole) == (path == "/v1/grants/held"), (path, whole)
        pages = _walk(client, path, headers, limit)
        joined = [i for page in pages for i in page]
        assert joined == expected, (path, pages, expected)
        assert pages[-1] == [] and all(len(p) == limit for p in pages[:-2]), (path, pages)
        # `before` alone: every row older than the cursor, unbounded.
        rest = _ids(client.get(f"{path}?before={expected[0]}", **headers))
        assert rest == expected[1:], (path, rest)

    # The filters still apply inside a page.
    client.delete(f"/v1/grants/{grants[4]}", **a)
    live = _ids(client.get("/v1/grants/issued?limit=10", **a))
    assert grants[4] not in live and len(live) == 4, live
    assert grants[4] in _ids(client.get("/v1/grants/issued?limit=10&include_revoked=true", **a))
    assert _ids(client.get("/v1/access-requests/sent?limit=10&status=denied", **b)) == []


def test_a_malformed_or_foreign_page_parameter_is_a_400_before_the_scope_gate(client, world):
    """`limit` is an integer from 1 to 500 and `before` a UUID naming a row of THIS list; else
    400 E_VALIDATION, before `require_scope` (C37: query parsing runs first).
    CONTROLS: a legal page is 200; a scopeless key with a legal page is the scope's 403."""
    grant = json.loads(issue_grant_via_api(client, world, grantor=world.owner_a,
                                           grantee=world.owner_b, peer=world.peer_a,
                                           skill="paged-bad").content)["id"]
    a = bearer(world.owner_a)
    for query in ("limit=0", "limit=501", "limit=abc", "limit=-1", "limit=", "limit=1.5",
                  "before=not-a-uuid", f"before={uuid.uuid4()}", f"limit=2&before={uuid.uuid4()}"):
        for path in ("/v1/grants/issued", "/v1/access-requests/received"):
            r = client.get(f"{path}?{query}", **a)
            assert r.status_code == 400, (path, query, r.status_code, r.content)
            assert json.loads(r.content)["error"] == "E_VALIDATION", (path, query)
    # A row of ANOTHER list is not a cursor of this one: A's issued grant on B's issued list.
    r = client.get(f"/v1/grants/issued?before={grant}", **bearer(world.owner_b))
    assert r.status_code == 400, r.content
    # CONTROLS.
    assert _ids(client.get("/v1/grants/issued?limit=500", **a)) == [grant]
    assert _ids(client.get(f"/v1/grants/held?limit=1&before={grant}",
                           **bearer(world.owner_b))) == []
    scopeless = {"HTTP_AUTHORIZATION": f"Bearer {world.issue_pat(world.owner_a, [])}"}
    assert client.get("/v1/grants/issued?limit=abc", **scopeless).status_code == 400
    assert client.get("/v1/grants/issued?limit=1", **scopeless).status_code == 403


def test_an_over_long_limit_is_a_400_on_all_four_paged_reads_and_not_a_500(world):
    """Review of fin/post-r10, finding 1: `int()` refuses a decimal string over 4300 digits
    (CPython's int_max_str_digits), so a 4301-digit `limit` escaped `_page_query` as an
    unhandled ValueError, a 500, on all four paged reads and, the parse running before the
    scope gate, for a scopeless key too. It is out of range, so it is the range's 400.
    CONTROLS: 4300 digits was already a 400; leading zeros still name the value they spell
    (`limit=` + 5000 zeros + `1` is a page of one), as they did below 4300 digits."""
    grant = json.loads(issue_grant_via_api(Client(), world, grantor=world.owner_a,
                                           grantee=world.owner_b, peer=world.peer_a,
                                           skill="paged-long").content)["id"]
    client = Client(raise_request_exception=False)
    a = bearer(world.owner_a)
    scopeless = {"HTTP_AUTHORIZATION": f"Bearer {world.issue_pat(world.owner_a, [])}"}
    for digits in (4300, 4301, 5000):
        for path in ("/v1/grants/issued", "/v1/grants/held", "/v1/access-requests/sent",
                     "/v1/access-requests/received"):
            for auth in (a, scopeless):
                r = client.get(f"{path}?limit={'9' * digits}", **auth)
                assert r.status_code == 400, (path, digits, r.status_code, r.content[:200])
                assert json.loads(r.content)["error"] == "E_VALIDATION", (path, digits)
    # CONTROL: the value, not the spelling, is what is range-checked.
    assert _ids(client.get(f"/v1/grants/issued?limit={'0' * 5000}1", **a)) == [grant]
    assert client.get(f"/v1/grants/issued?limit={'0' * 5000}", **a).status_code == 400


_PAGED_READS = {
    "issued": ("grantor_owner_id", "grant",
               lambda c, w, b: repository.list_issued(c, w.owner_a, True, limit=2, before=b)),
    "held": ("grantee_owner_id", "grant",
             lambda c, w, b: repository.list_held(c, w.owner_b, False, limit=2, before=b)),
    "sent": ("requester_owner_id", "request",
             lambda c, w, b: repository.list_requests_sent(c, w.owner_b, None, limit=2,
                                                           before=b)),
    "received": ("granter_owner_id", "request",
                 lambda c, w, b: repository.list_requests_received(c, w.owner_a, None,
                                                                   limit=2, before=b)),
}


@pytest.mark.parametrize("read", sorted(_PAGED_READS))
def test_every_keyset_page_is_an_index_range_on_its_owner(client, conn, world, read):
    """Divergence 045: a page with `before` runs two statements, the cursor row's lookup
    (a primary-key probe) and the page, which keeps sql_meshychat/020's index condition on
    the owner; a page with `limit` alone runs one. Each REAL statement is EXPLAINed."""
    column, kind, fn = _PAGED_READS[read]
    if kind == "grant":
        row = json.loads(issue_grant_via_api(client, world, grantor=world.owner_a,
                                             grantee=world.owner_b, peer=world.peer_a,
                                             skill="plan-paged").content)["id"]
    else:
        row = json.loads(submit_request(client, world, requester=world.owner_b,
                                        granter_owner=world.owner_a, granter_peer=world.peer_a,
                                        skill="plan-paged").content)["id"]
    rec = _RecordingConn(conn)
    assert isinstance(fn(rec, world, uuid.UUID(row)), repository.Empty)
    assert len(rec.executed) == 2, rec.executed
    lookup, page = rec.executed
    assert _index_cond_on(_plan(conn, *lookup), "id"), lookup
    assert _index_cond_on(_plan(conn, *page), column), f"{read}\n{_plan(conn, *page)}"
    only_limit = _the_one_statement(lambda c: fn(c, world, None), conn)
    assert _index_cond_on(_plan(conn, *only_limit), column), only_limit


# ---------------------------------------------- review of 4cbbc9e5: mandate id bytes
#
# The parsed `owner`, `grantee`, and `target_peer` ids are serialized lowercase and
# hyphenated before an owner's key covers them. The accepted forms are 32 hex digits,
# 36 hyphenated, 38 braced-hyphenated, and 45 `urn:uuid:`-hyphenated; hex may use either
# case, but the `urn:uuid:` prefix is lowercase only. Python's `uuid.UUID` accepts a
# strict superset, so this test holds the four-form boundary. apps/mesh/wait.py applies
# the same rule to cursor ids.

def _signing_input_as_sent(fields: dict) -> bytes:
    """The mandate signing input built from `fields` exactly as given, WITHOUT going
    through IntentMandate: a test that let intent.py build the expected bytes would
    agree with whatever intent.py does."""
    from apps.crypto.primitives import jcs
    from apps.grants import intent

    return intent.MANDATE_DOMAIN + jcs(fields)


def _sign_as_sent(seed32: bytes, fields: dict) -> str:
    import base64

    from apps.crypto.primitives import sign as ed_sign

    return base64.b64encode(ed_sign(seed32, _signing_input_as_sent(fields))).decode("ascii")


def _accepted_forms(u) -> dict:
    """The four forms uuid 1.23 parses, each naming the same id as `str(u)`."""
    u = uuid.UUID(str(u))
    return {"upper": str(u).upper(), "braced": "{" + str(u) + "}", "simple": u.hex,
            "urn": "urn:uuid:" + str(u)}


def _refused_forms(u) -> dict:
    """Forms Python's uuid.UUID reads as the SAME id and uuid 1.23 refuses. Each is
    asserted below to be the same id to Python, so a refusal is the parser's and not a
    different id failing some later binding."""
    u = uuid.UUID(str(u))
    h, s = u.hex, str(u)
    return {"hyphen-misplaced": h[:4] + "-" + h[4:],
            "braced-simple": "{" + h + "}",
            "urn-simple": "urn:uuid:" + h,
            "uuid-prefix": "uuid:" + s,
            "unbalanced-brace": "{" + s,
            "hyphens-anywhere": "-".join((h[:7], h[7:12], h[12:17], h[17:22], h[22:]))}


def test_uuid_parse_str_takes_the_uuid_crates_four_forms_and_nothing_else():
    """wire.parse_uuid_str against the four accepted UUID forms, form by form."""
    u = uuid.UUID("0f8fad5b-d9cb-469f-a165-70867728950e")
    # CONTROL: the canonical form and the four accepted spellings of it.
    assert wire.parse_uuid_str(str(u)) == u
    for label, form in _accepted_forms(u).items():
        assert wire.parse_uuid_str(form) == u, label
    for label, form in _refused_forms(u).items():
        assert uuid.UUID(form) == u, f"precondition: Python reads {label} as the same id"
        with pytest.raises(ValueError):
            wire.parse_uuid_str(form)
    # Python reads these as a DIFFERENT id (the integer parser takes 0x, a sign and a space);
    # `wire.parse_uuid_str` reads none of them. The rest Python refuses too.
    for form in ("0x" + "b" * 30, "+" + "b" * 31, " " + "b" * 31, "URN:UUID:" + str(u),
                 str(u)[:-1] + "g", "", str(u) + " "):
        with pytest.raises(ValueError):
            wire.parse_uuid_str(form)
    for not_a_string in (None, 7, u.int, [str(u)]):
        with pytest.raises(ValueError):
            wire.parse_uuid_str(not_a_string)


def test_a_mandate_is_verified_over_its_canonical_ids_whatever_form_was_sent(
        client, conn, world):
    """Review of 4cbbc9e5, MEDIUM: mutant A5 (validate each mandate id, then keep the raw
    string) passed all 82 tests, and it changes which bytes the owner's Ed25519 signature
    must cover.

    ARM 1 runs first because it writes nothing: uppercase ids, signed over those
    uppercase bytes. Parsed ids are re-serialized lowercase, so that signature covers
    bytes the verifier never builds: 403, and no row.
    ARM 2 is the positive: each accepted form, signed over the CANONICAL bytes (built
    here from the lowercase strings), is a 201, and the stored mandate_hash is the
    SHA-256 of the canonical signing input."""
    seed = human_seed("canonical-ids")
    register_human_key(conn, world.owner_a, seed)
    headers = bearer(world.owner_a)
    expiry = iso(now() + _dt.timedelta(minutes=10))
    mandate_expiry = now() + _dt.timedelta(hours=1)

    def request(skill, form):
        canonical = mandate_fields(owner=world.owner_a, grantee=world.owner_b,
                                   target_peer=world.peer_a, skill=skill,
                                   expires_at=mandate_expiry)
        sent = dict(canonical, owner=_accepted_forms(world.owner_a)[form],
                    grantee=_accepted_forms(world.owner_b)[form],
                    target_peer=_accepted_forms(world.peer_a)[form])
        body = {"grantee_owner_id": str(world.owner_b),
                "granter_peer_id": str(world.peer_a), "skill_name": skill,
                "source": "direct", "expires_at": expiry}
        return canonical, sent, body

    canonical, sent, body = request("canon-raw-upper", "upper")
    assert _signing_input_as_sent(sent) != _signing_input_as_sent(canonical)
    raw = post_json(client, "/v1/grants",
                    dict(body, mandate={"mandate": sent, "sig": _sign_as_sent(seed, sent)}),
                    headers)
    assert raw.status_code == 403, raw.content
    assert json.loads(raw.content)["error"] == "E_FORBIDDEN"
    assert isinstance(service.lookup_active_grant(conn, world.owner_a, world.owner_b,
                                                  world.peer_a, "canon-raw-upper"),
                      repository.Empty)

    for form in ("upper", "braced", "simple", "urn"):
        skill = f"canon-{form}"
        canonical, sent, body = request(skill, form)
        ok = post_json(client, "/v1/grants",
                       dict(body, mandate={"mandate": sent,
                                           "sig": _sign_as_sent(seed, canonical)}),
                       headers)
        assert ok.status_code == 201, (form, ok.content)
        with conn.cursor() as cur:
            cur.execute("SELECT mandate_hash FROM skill_grants WHERE id = %s",
                        (json.loads(ok.content)["id"],))
            stored = bytes(cur.fetchone()[0])
        assert stored == hashlib.sha256(_signing_input_as_sent(canonical)).digest(), form


def test_an_id_form_the_uuid_crate_refuses_is_refused_where_the_grants_routes_parse_ids(
        client, conn, world):
    """Review of 4cbbc9e5, LOW: `uuid.UUID` accepted and silently rewrote ids outside the
    accepted forms. Three places parse one: the mandate (400 `invalid mandate`), a body
    field (422), and a path segment (400). Each refused form below is
    the SAME id to Python, so on the lax parser every one of them succeeded."""
    seed = human_seed("strict-forms")
    register_human_key(conn, world.owner_a, seed)
    headers = bearer(world.owner_a)
    expiry = iso(now() + _dt.timedelta(minutes=10))
    mandate_expiry = now() + _dt.timedelta(hours=1)

    def mandated(skill, owner_form):
        canonical = mandate_fields(owner=world.owner_a, grantee=world.owner_b,
                                   target_peer=world.peer_a, skill=skill,
                                   expires_at=mandate_expiry)
        body = {"grantee_owner_id": str(world.owner_b),
                "granter_peer_id": str(world.peer_a), "skill_name": skill,
                "source": "direct", "expires_at": expiry,
                "mandate": {"mandate": dict(canonical, owner=owner_form),
                            "sig": _sign_as_sent(seed, canonical)}}
        return post_json(client, "/v1/grants", body, headers)

    wrong = {}
    for i, (label, form) in enumerate(_refused_forms(world.owner_a).items()):
        r = mandated(f"strict-m-{i}", form)
        if r.status_code != 400 or not json.loads(r.content)["message"].startswith(
                "invalid mandate"):
            wrong[f"mandate {label}"] = (r.status_code, r.content[:120])
    for i, (label, form) in enumerate(_refused_forms(world.owner_b).items()):
        r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=form,
                                peer=world.peer_a, skill=f"strict-f-{i}")
        if r.status_code != 422:
            wrong[f"body {label}"] = (r.status_code, r.content[:120])

    target = issue_grant_via_api(client, world, grantor=world.owner_a,
                                 grantee=world.owner_b, peer=world.peer_a,
                                 skill="strict-path")
    assert target.status_code == 201, target.content
    grant_id = json.loads(target.content)["id"]
    for label, form in _refused_forms(grant_id).items():
        if "{" in form:
            continue            # not a path segment a client sends unencoded
        r = client.delete(f"/v1/grants/{form}", **headers)
        if r.status_code != 400:
            wrong[f"path {label}"] = (r.status_code, r.content[:120])
    assert not wrong, f"(status, body) per refused form: {wrong}"

    # CONTROLS, after the negatives because each writes: the canonical spelling of the
    # same ids is a 201 through the mandate and through the body field, and the same
    # grant revokes through its canonical path - so each refusal above is the parse.
    assert mandated("strict-m-control", str(world.owner_a)).status_code == 201
    assert issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                               peer=world.peer_a,
                               skill="strict-f-control").status_code == 201
    revoked = client.delete(f"/v1/grants/{grant_id}", **headers)
    assert revoked.status_code == 200, revoked.content


def test_a_cap_below_one_is_refused_before_the_mandate_is_verified(client, world):
    """Review of 4cbbc9e5, INFO, pinned because it is part of the C36 divergence
    (views.py module docstring): the cap check runs before the mandate block, so
    `max_executions: 0` with an unverifiable mandate is this app's 400; the cap check runs
    before mandate verification."""
    import base64
    mandate = mandate_fields(owner=world.owner_a, grantee=world.owner_b,
                             target_peer=world.peer_a, skill="cap-first",
                             expires_at=now() + _dt.timedelta(hours=1))
    body = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "cap-first", "source": "direct",
            "mandate": {"mandate": mandate, "sig": base64.b64encode(b"\x01" * 64).decode()}}
    # CONTROL: a legal cap with the same unverifiable mandate is the mandate's 403.
    denied = post_json(client, "/v1/grants", dict(body, max_executions=1),
                       bearer(world.owner_a))
    assert denied.status_code == 403, denied.content
    capped = post_json(client, "/v1/grants", dict(body, max_executions=0),
                       bearer(world.owner_a))
    assert capped.status_code == 400, capped.content
    assert "max_executions" in json.loads(capped.content)["message"]


# ---------------------------------------------------- review of 4cbbc9e5: migration 020

_MIGRATION_020 = (Path(__file__).resolve().parent.parent / "sql_meshychat"
                  / "020_grants_list_indexes.sql")
_DOWN_020 = (Path(__file__).resolve().parent.parent / "sql_meshychat" / "down"
             / "020_grants_list_indexes_down.sql")
_INDEXES_020 = ("skill_grants_issued_idx", "skill_grants_held_idx",
                "skill_access_requests_sent_idx", "skill_access_requests_received_idx")


def _present_020(cur) -> set:
    cur.execute("SELECT indexname FROM pg_indexes WHERE indexname = ANY(%s)",
                (list(_INDEXES_020),))
    return {r[0] for r in cur.fetchall()}


def _ledger_has_020(cur) -> bool:
    cur.execute("SELECT count(*) FROM meshychat_migrations WHERE version = 20")
    return cur.fetchone()[0] == 1


def test_the_020_down_script_removes_it_and_020_applies_again_over_the_result():
    """Review of 4cbbc9e5, LOW: 020 had no down script. It is a named script outside the
    migrate set, as 014's is (the runner had no down step then; since the rollback gate
    `sqlmigrate.py --rollback-to` runs it). All of it runs in ONE
    transaction that is rolled back, so the run's database keeps 020."""
    with psycopg.connect(DSN) as c:
        try:
            cur = c.cursor()
            # CONTROL: the run's database has 020, so "absent" below is the script.
            assert _present_020(cur) == set(_INDEXES_020)
            assert _ledger_has_020(cur)
            cur.execute(_DOWN_020.read_text(encoding="utf-8"))
            assert _present_020(cur) == set(), "the down script left an index"
            assert not _ledger_has_020(cur), "the ledger still says 020 is applied"
            cur.execute(_MIGRATION_020.read_text(encoding="utf-8"))
            assert _present_020(cur) == set(_INDEXES_020)
        finally:
            c.rollback()


def test_020_gives_up_on_a_held_table_lock_instead_of_queueing_writers_behind_it():
    """Review of 4cbbc9e5, LOW. CREATE INDEX takes SHARE on the table. Queued behind a
    transaction that holds a conflicting lock, it would hold every later writer of
    skill_grants behind ITSELF for as long as that transaction lasts. 020 sets a
    lock_timeout, as 014 does, so it fails and can be retried instead. The client-side
    statement_timeout is only the fence that stops the unfixed file hanging the run."""
    holder = psycopg.connect(DSN)
    try:
        holder.execute("LOCK TABLE skill_grants IN ROW EXCLUSIVE MODE")
        with psycopg.connect(DSN) as c:
            try:
                c.execute("SET statement_timeout = '15s'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    c.execute(_MIGRATION_020.read_text(encoding="utf-8"))
            finally:
                c.rollback()
    finally:
        holder.rollback()
        holder.close()
    # CONTROL: with the holder gone, the same file runs to the end.
    with psycopg.connect(DSN) as c:
        try:
            c.execute(_MIGRATION_020.read_text(encoding="utf-8"))
        finally:
            c.rollback()


# ------------------------------------------ B09 the publisher attestation at the pin
#
# A pinned grant's publisher attestation is verified whenever the skill row carries a
# signature pair, with MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE on or off. The flag only
# decides whether an ABSENT pair is refused. A forged or revoked-key signature must not
# be pinned with the flag off, and a valid pin must work with it on. Operator decision 52
# (2026-09-27): the flags stay off at cutover, and the gap closes first.
#
# Every timestamp below comes from the database's clock (`now()` in SQL), the same clock
# the verifier compares a key's `revoked_at` against.

from apps.crypto import primitives as P  # noqa: E402
from apps.skillsreg import attestation as sattest  # noqa: E402

_ATTEST_ENV = "MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE"


def _attestation(monkeypatch, on: bool):
    """The flag, set where the server reads it: the setting settings.py parses once at
    boot. The environment is set to the same value only so that the unchanged code (which
    read the environment per call) is measured under the same flag; a test in
    tests/test_attestation_flag.py proves the environment is NOT what is read now."""
    if on:
        monkeypatch.setenv(_ATTEST_ENV, "true")
    else:
        monkeypatch.delenv(_ATTEST_ENV, raising=False)
    return override_settings(MESHY_MANIFEST_ATTESTATION_ENFORCE=on)


def _publisher_key(conn, peer_id, *, active=True, revoked_at_sql="NULL"):
    """A peer_keys row for `peer_id` and the seed behind it. `revoked_at_sql` is SQL, so a
    revocation instant is the database's clock and never this process's."""
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active, revoked_at) "
                    f"VALUES (%s, %s, %s, %s, {revoked_at_sql})",
                    (str(peer_id), kid, P.b64u(public), active))
    return seed, kid


def _signed_skill(conn, peer_id, name, *, seed=None, kid=None):
    """A skills row at 1.0.0, signed (when `seed` is given) over its 32-byte manifest hash."""
    _skill_row(conn, peer_id, name)
    if seed is None and kid is None:
        return None
    found = repository.find_skill_for_call(conn, peer_id, name, "1.0.0")
    digest = gmanifest.manifest_hash(found.value)
    sig = P.sign(seed, digest)
    with conn.cursor() as cur:
        cur.execute("UPDATE skills SET manifest_sig = %s, signing_kid = %s "
                    "WHERE peer_id = %s AND name = %s", (sig, kid, str(peer_id), name))
    return digest


def _pin(client, world, name):
    return issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                               peer=world.peer_a, skill=name, extra={"version_pin": "1.0.0"})


def _grant_rows(conn, world, name) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_grants WHERE granter_peer_id = %s "
                    "AND skill_name = %s", (str(world.peer_a), name))
        return cur.fetchone()[0]


def _pin_passes(client, conn, world, name, digest=None):
    r = _pin(client, world, name)
    assert r.status_code == 201, (name, r.status_code, r.content)
    assert r.json()["version_pin"] == "1.0.0"
    if digest is not None:
        assert bytes(_stored_grant(conn, r.json()["id"]).approved_manifest_hash) == digest


def _pin_refused(client, conn, world, name, reason):
    """Refused for THIS reason: the service's own message (a 500 is also what any crash
    returns), then the wire response (ManifestAttestationFailed deliberately maps to 500
    E_INTERNAL), then no row written."""
    with pytest.raises(service.ManifestAttestationFailed) as exc:
        service._resolve_manifest_pin(conn, world.peer_a, name, "1.0.0")
    assert reason in str(exc.value), (name, str(exc.value))
    r = _pin(client, world, name)
    assert r.status_code == 500 and r.json()["error"] == "E_INTERNAL", (name, r.content)
    assert _grant_rows(conn, world, name) == 0, f"{name}: a refused pin wrote a grant"


def test_b09_attestation_off_verifies_a_present_pair_and_accepts_an_absent_one(
        client, conn, world, monkeypatch):
    """With the flag OFF, a present pair is verified: forged, unknown-key and
    revoked-key signatures are refused; a valid one and an absent pair pass."""
    seed, kid = _publisher_key(conn, world.peer_a)
    tag = uuid.uuid4().hex[:8]
    with _attestation(monkeypatch, False):
        # CONTROLS FIRST: an absent pair (the legacy sentinel) and a valid signature pin.
        _signed_skill(conn, world.peer_a, f"absent-{tag}")
        _pin_passes(client, conn, world, f"absent-{tag}")
        digest = _signed_skill(conn, world.peer_a, f"valid-{tag}", seed=seed, kid=kid)
        _pin_passes(client, conn, world, f"valid-{tag}", digest)

        # FORGED: the publisher's kid, somebody else's key.
        _signed_skill(conn, world.peer_a, f"forged-{tag}", seed=os.urandom(32), kid=kid)
        _pin_refused(client, conn, world, f"forged-{tag}",
                     "manifest attestation signature invalid: "
                     "manifest signature verification failed")
        # UNKNOWN KID.
        _signed_skill(conn, world.peer_a, f"nokid-{tag}", seed=seed, kid="no-such-kid")
        _pin_refused(client, conn, world, f"nokid-{tag}",
                     f"manifest attestation key missing for peer {world.peer_a} "
                     "kid no-such-kid")
        # REVOKED, inside its grace window: find_by_kid still returns the row, and an
        # inactive key is refused.
        g_seed, g_kid = _publisher_key(conn, world.peer_a, active=False,
                                       revoked_at_sql="now() + interval '1 hour'")
        _signed_skill(conn, world.peer_a, f"grace-{tag}", seed=g_seed, kid=g_kid)
        _pin_refused(client, conn, world, f"grace-{tag}",
                     f"manifest attestation key inactive or revoked for peer {world.peer_a} "
                     f"kid {g_kid}")
        # REVOKED, window closed: find_by_kid's predicate no longer returns the row, so
        # the lookup reports a missing key.
        r_seed, r_kid = _publisher_key(conn, world.peer_a, active=False,
                                       revoked_at_sql="now() - interval '1 minute'")
        _signed_skill(conn, world.peer_a, f"dead-{tag}", seed=r_seed, kid=r_kid)
        _pin_refused(client, conn, world, f"dead-{tag}",
                     f"manifest attestation key missing for peer {world.peer_a} kid {r_kid}")

        # The ACTIVE key with a revocation instant that has passed (a compromise stamp
        # without the rotation): `revoked_at <= now` refuses it on its own...
        with conn.cursor() as cur:
            cur.execute("UPDATE peer_keys SET revoked_at = now() - interval '1 second' "
                        "WHERE peer_id = %s AND kid = %s", (str(world.peer_a), kid))
        _signed_skill(conn, world.peer_a, f"stamped-{tag}", seed=seed, kid=kid)
        _pin_refused(client, conn, world, f"stamped-{tag}",
                     "manifest attestation key inactive or revoked")
        # ...and clearing the stamp makes the identical request pass: the CONTROL.
        with conn.cursor() as cur:
            cur.execute("UPDATE peer_keys SET revoked_at = NULL "
                        "WHERE peer_id = %s AND kid = %s", (str(world.peer_a), kid))
        _pin_passes(client, conn, world, f"stamped-{tag}")


def test_b09_a_repin_patch_verifies_the_attestation_too(client, conn, world, monkeypatch):
    """update_grant's set-the-pin arm runs the same verification. A forged signature must
    not reach a grant by issuing it unpinned and pinning it afterwards."""
    seed, kid = _publisher_key(conn, world.peer_a)
    tag = uuid.uuid4().hex[:8]
    with _attestation(monkeypatch, False):
        _signed_skill(conn, world.peer_a, f"forged-{tag}", seed=os.urandom(32), kid=kid)
        _signed_skill(conn, world.peer_a, f"valid-{tag}", seed=seed, kid=kid)
        ids = {}
        for name in (f"forged-{tag}", f"valid-{tag}"):
            r = issue_grant_via_api(client, world, grantor=world.owner_a,
                                    grantee=world.owner_b, peer=world.peer_a, skill=name)
            assert r.status_code == 201, r.content
            ids[name] = r.json()["id"]

        def repin(name):
            return client.patch(f"/v1/grants/{ids[name]}",
                                data=json.dumps({"version_pin": "1.0.0"}),
                                content_type="application/json", **bearer(world.owner_a))

        ok = repin(f"valid-{tag}")
        assert ok.status_code == 200 and ok.json()["version_pin"] == "1.0.0", ok.content
        bad = repin(f"forged-{tag}")
        assert bad.status_code == 500 and bad.json()["error"] == "E_INTERNAL", bad.content
        stored = _stored_grant(conn, ids[f"forged-{tag}"])
        assert stored.version_pin is None and stored.approved_manifest_hash is None


def test_b09_attestation_on_refuses_a_missing_pair_and_passes_a_valid_one(
        client, conn, world, monkeypatch):
    """With the flag ON, a missing pair is refused and a VALID signature pins: before B09
    every pinned grant was refused because verification had not been built."""
    seed, kid = _publisher_key(conn, world.peer_a)
    g_seed, g_kid = _publisher_key(conn, world.peer_a, active=False,
                                   revoked_at_sql="now() + interval '1 hour'")
    tag = uuid.uuid4().hex[:8]
    with _attestation(monkeypatch, True):
        digest = _signed_skill(conn, world.peer_a, f"valid-{tag}", seed=seed, kid=kid)
        _pin_passes(client, conn, world, f"valid-{tag}", digest)

        _signed_skill(conn, world.peer_a, f"absent-{tag}")
        _pin_refused(client, conn, world, f"absent-{tag}",
                     "manifest attestation malformed: manifest attestation required")
        _signed_skill(conn, world.peer_a, f"forged-{tag}", seed=os.urandom(32), kid=kid)
        _pin_refused(client, conn, world, f"forged-{tag}",
                     "manifest attestation signature invalid")
        _signed_skill(conn, world.peer_a, f"grace-{tag}", seed=g_seed, kid=g_kid)
        _pin_refused(client, conn, world, f"grace-{tag}",
                     "manifest attestation key inactive or revoked")

        # TASK-56 (the open item recorded in the project's design log (not part of this release), B09): an UNPINNED grant is not checked under enforcement. Pinned here so
        # that closing it is a visible change.
        r = issue_grant_via_api(client, world, grantor=world.owner_a, grantee=world.owner_b,
                                peer=world.peer_a, skill=f"absent-{tag}")
        assert r.status_code == 201, r.content


def test_b09_the_verifier_refuses_a_half_or_malformed_pair_before_any_key_lookup(
        conn, world):
    """The database's CHECK constraints keep these malformed arms out of a real row
    (skills_manifest_attestation_pair, skills_manifest_sig_len), so they are driven with
    a row value. `conn=None` proves they return before any database read."""
    seed, kid = _publisher_key(conn, world.peer_a)
    _signed_skill(conn, world.peer_a, "row-shape", seed=seed, kid=kid)
    row = dict(repository.find_skill_for_call(conn, world.peer_a, "row-shape", "1.0.0").value)
    # CONTROL: the stored pair verifies, required or not.
    for required in (False, True):
        sattest.verify_manifest_attestation(conn, row, required,
                                            manifest_hash=gmanifest.manifest_hash)
    cases = {
        "sig without kid": (dict(row, signing_kid=None),
                            "manifest_sig and signing_kid must be both present or both NULL"),
        "kid without sig": (dict(row, manifest_sig=None),
                            "manifest_sig and signing_kid must be both present or both NULL"),
        "63-byte sig": (dict(row, manifest_sig=bytes(row["manifest_sig"])[:63]),
                        "manifest_sig must be 64 raw Ed25519 bytes"),
        "blank kid": (dict(row, signing_kid="   "), "signing_kid must not be empty"),
    }
    for label, (bad, reason) in cases.items():
        for required in (False, True):
            with pytest.raises(sattest.ManifestAttestationError) as exc:
                sattest.verify_manifest_attestation(None, bad, required,
                                                    manifest_hash=gmanifest.manifest_hash)
            assert str(exc.value) == f"manifest attestation malformed: {reason}", label
    # The absent pair: accepted when not required, and the required refusal.
    absent = dict(row, manifest_sig=None, signing_kid=None)
    sattest.verify_manifest_attestation(None, absent, False,
                                        manifest_hash=gmanifest.manifest_hash)
    with pytest.raises(sattest.ManifestAttestationError,
                       match="^manifest attestation malformed: manifest attestation required$"):
        sattest.verify_manifest_attestation(None, absent, True,
                                            manifest_hash=gmanifest.manifest_hash)


def test_b09_a_malformed_publisher_key_verifies_nothing(conn, world):
    """The stored key is decoded as unpadded base64url, strictly, and must be 32 bytes. A padded
    spelling, a character outside the alphabet and a short key are
    each refused, and the same key spelled correctly verifies the same signature."""
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    cases = {
        "padded": (P.b64u(public) + "=",
                   "publisher public key is not valid base64url-no-pad"),
        "outside the alphabet": (P.b64u(public)[:-1] + "*",
                                 "publisher public key is not valid base64url-no-pad"),
        "31 bytes": (P.b64u(public[:31]), "publisher public key must decode to 32 bytes"),
        "control": (P.b64u(public), None),
    }
    for label, (stored, reason) in cases.items():
        peer = world._peer(world.owner_a, f"key-{label.replace(' ', '-')}")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                        "VALUES (%s, 'k', %s, TRUE)", (str(peer), stored))
        _signed_skill(conn, peer, "decode", seed=seed, kid="k")
        if reason is None:
            pin, digest = service._resolve_manifest_pin(conn, peer, "decode", "1.0.0")
            assert pin == "1.0.0" and len(digest) == 32
            continue
        with pytest.raises(service.ManifestAttestationFailed) as exc:
            service._resolve_manifest_pin(conn, peer, "decode", "1.0.0")
        assert str(exc.value) == ("manifest attestation verification failed: manifest "
                                  f"attestation signature invalid: {reason}"), label


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))


def test_an_unencodable_source_is_422_not_500(client, world):
    """#252 (memory-bxxxxx's #275 review): the `unknown variant` message echoed `source`
    raw, so a lone surrogate made the 422 response itself unencodable: a 500."""
    headers = bearer(world.owner_a)
    good = {"grantee_owner_id": str(world.owner_b), "granter_peer_id": str(world.peer_a),
            "skill_name": "unencodable", "source": "g~"}
    body = json.dumps(good)
    assert body.count("~") == 1
    bad = client.post("/v1/grants", data=body.replace("~", chr(92) + "udfff"),
                      content_type="application/json", **headers)
    assert bad.status_code == 422, bad.content
    assert "unknown variant `g" + chr(92) * 2 + "udfff`" in bad.content.decode("utf-8")
