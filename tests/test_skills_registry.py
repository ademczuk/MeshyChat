"""The skills registry, proved against the real database and the real routing table.

WHAT MAKES A TEST HERE COUNT

1. EVERY NEGATIVE ASSERTION HAS A POSITIVE CONTROL IN THE SAME TEST. A 403 is also what
   an unseeded peer, a mistyped URL, an expired token or a crashed connection produces,
   so "it was rejected" on its own proves nothing. Where the control is the interesting
   half it is asserted FIRST, so a failure reads as "the happy path broke" rather than
   "the guard fired".

2. NO PINNED APP URLCONF. `ROOT_URLCONF` is set to `meshychat.urls` - the PROJECT's own
   map, the one a request actually takes - and never to `apps.skillsreg.urls`. It is set
   at all only because several modules in this suite configure Django at import time and
   whichever is imported FIRST wins (`apps/authapi/testsettings` re-points
   DJANGO_SETTINGS_MODULE), so the loser's configuration is skipped silently; an earlier
   round of this project had every route test passing while the application served 404
   for all of them. `test_the_routing_table_is_the_projects_own` is the control: it
   asserts that routes belonging to OTHER apps resolve through the same table, which a
   self-serving stub urlconf could not do.

3. NO DATABASE MOCKS. Every row goes to the migrated Postgres named by MESHY_TEST_DSN.
   Only key material and ids are generated per run.

Run: .venv/Scripts/python.exe -m pytest tests/test_skills_registry.py -q
"""
from __future__ import annotations

import base64
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
# PROMOTE IT TO THE VARIABLE THE APPLICATION ACTUALLY READS, as every other test module
# here does. `MESHY_TEST_DSN` belongs to tests/conftest.py; the application resolves its
# connection through apps/core/dbconn.dsn(), which has never heard of it.
#
# This line was missing, and apps/skillsreg/db.py was reading MESHY_TEST_DSN itself to
# compensate - so this module passed in a configuration where every other app would have
# connected to the wrong database. The app-level fallback has been removed, which is what
# surfaced the gap: a fallback that hides a misconfiguration is worse than the
# misconfiguration, because it makes exactly one surface behave differently from the rest.
os.environ.setdefault("MESHY_DSN", DSN)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

ISSUER = "meshy-test-issuer"

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

if not dj_settings.configured:                     # standalone-run fallback only
    os.environ["DJANGO_SETTINGS_MODULE"] = "meshychat.settings"

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.urls import resolve  # noqa: E402

from apps.crypto import primitives as P  # noqa: E402
from apps.skillsreg import manifest, repository, schema_validate, semver  # noqa: E402
from apps.skillsreg import runtime as rt  # noqa: E402
from apps.skillsreg import views as V  # noqa: E402

DECLARE = "skills:declare"
PUBLISH = "skills:publish"
BOTH = [DECLARE, PUBLISH]


# ------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def skillsreg_settings():
    """Layered on top of whatever base configuration won the import race.

    ROOT_URLCONF is the PROJECT's, deliberately - see the module docstring. The other
    two are the JWT verification key and issuer `apps.peers.auth.verify_token` reads.
    """
    with override_settings(
        ROOT_URLCONF="meshychat.urls",
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        # MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE's documented default, as the parsed setting
        # (B09): a value in the developer's environment must not change what is measured.
        MESHY_MANIFEST_ATTESTATION_ENFORCE=False,
    ):
        yield


@pytest.fixture()
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def client():
    return Client()


def _seed_peer(conn, tag: str) -> dict:
    """An owner + an active peer.

    Column names read off the live schema, not guessed: the owner FK targets
    `dashboard_users` (NOT `owners`), and a 'product'-plane user must carry a NULL
    trust_group_id - CHECK ((plane='dev') = (trust_group_id IS NOT NULL)).
    """
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'product',NULL)",
                    (owner_id, f"skillsreg-{tag}-{owner_id.hex[:8]}@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (peer_id, owner_id, f"skillsreg-{tag}-{peer_id.hex[:8]}"))
    return {"owner_id": owner_id, "peer_id": peer_id}


def _mint(peer_id, *, scopes=None, aud="peer", ttl=300) -> str:
    """A real EdDSA peer JWT with the claim names used by this application."""
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims = {"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + ttl,
              "scopes": list(scopes or [])}
    if aud is not None:
        claims["aud"] = aud
    return pyjwt.encode(claims, JWT_PRIVATE_PEM, algorithm="EdDSA")


def _auth(peer_id, scopes=None) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {_mint(peer_id, scopes=scopes)}"}


def _declare_body(name, version="1.0.0", **over) -> dict:
    body = {"name": name, "version": version, "mode": "sync", "share_mode": "private",
            "input_schema": {"type": "object"}}
    body.update(over)
    return body


def _post(client, peer_id, body, scopes=BOTH):
    return client.post("/v1/peers/me/skills", data=json.dumps(body),
                       content_type="application/json", **_auth(peer_id, scopes))


def _skill_name(tag: str) -> str:
    return f"skill-{tag}-{uuid.uuid4().hex[:10]}"


# ============================================================ controls for the file

def test_the_settings_override_is_actually_in_effect(client):
    """Positive control for the fixture, and for every other test here.

    Without it, a silent override failure shows up as twenty unrelated-looking 401s
    and 404s rather than as one named cause.
    """
    from django.conf import settings
    assert settings.ROOT_URLCONF == "meshychat.urls"
    assert settings.MESHY_JWT_ISSUER == ISSUER
    assert settings.MESHY_JWT_PUBLIC_KEY_PEM == JWT_PUBLIC_PEM


@pytest.mark.parametrize("path,name", [
    ("/v1/peers/me/skills", "my-skills"),
    ("/v1/peers/me/skills/sync", "skills-sync"),
    ("/v1/peers/me/skills/summarize/versions", "skill-release-version"),
    ("/v1/peers/me/skills/summarize", "skill-detail"),
    ("/v1/skills/11111111-1111-1111-1111-111111111111/summarize", "skill-get-current"),
])
def test_route_is_reachable_through_the_project_urlconf(path, name):
    assert resolve(path).url_name == name


def test_the_routing_table_is_the_projects_own(client):
    """The control that makes the parametrised test above mean something.

    Routes owned by OTHER apps must resolve through the SAME table. A urlconf that
    only knew about apps.skillsreg would pass every test above and fail this one, which
    is exactly the failure mode - a route test that supplies its own URL map - that let
    a previous round ship modules the application served 404 for.
    """
    assert resolve("/v1/health").url_name == "health"
    assert resolve("/v1/peers/me/presence").url_name == "presence-beat"
    from django.urls.exceptions import Resolver404
    with pytest.raises(Resolver404):
        resolve("/v1/skills-definitely-not-mounted")


def test_skills_has_no_row_level_security_so_the_route_is_the_only_guard(conn):
    """A MEASUREMENT, recorded as a test so it cannot quietly stop being true.

    `apps/skillsreg/db.py` argues that writes here need no `app.current_peer_id` context
    because `skills` has no RLS and no policies - the attribution is the route's
    (`peer_id` comes from the verified JWT, every UPDATE carries `WHERE peer_id = ...`).
    That argument is only sound while the measurement holds. If someone later enables
    RLS on `skills`, this test fails and says what to do; without it, every write in
    this app would start matching zero rows and look like "no data".

    The positive control is the second half: a table that DOES force RLS, proving the
    query can distinguish the two states rather than always reporting False.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'skills'")
        assert cur.fetchone() == (False, False), (
            "skills now has row-level security. apps/skillsreg writes bind no peer "
            "context, so they will silently affect zero rows until they do.")
        cur.execute("SELECT count(*) FROM pg_policies WHERE tablename = 'skills'")
        assert cur.fetchone()[0] == 0
        # Control: the probe is capable of returning True.
        cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'shared_memory'")
        assert cur.fetchone() == (True, True), (
            "control failed: the RLS probe never reports True, so the assertion above "
            "proves nothing about skills")


# ================================================================== semver ordering

def test_semver_is_numeric_not_string_9_0_0_versus_10_0_0():
    """#410 / PRs #412-#413, the exact pair.

    Under STRING ordering "10.0.0" < "9.0.0" because '1' < '9', and a peer that had
    released 9.0.0 could never release 10.0.0 - the server answered 409 for a version
    that plainly was newer.
    """
    assert semver.parse_semver("10.0.0") > semver.parse_semver("9.0.0")
    # The control: the comparison must still be capable of returning False, and the
    # STRING comparison this replaced must still be wrong, or the assertion above
    # would pass for a string implementation too.
    assert semver.parse_semver("9.0.0") < semver.parse_semver("9.0.1")
    assert "10.0.0" < "9.0.0", "string ordering is what the numeric parse exists to fix"


def test_semver_rejects_non_numeric_and_two_part_from_a_caller():
    for bad in ("1.0", "1.0.0.0", "1.0.x", "1..0", "v1.0.0", "1.0.-1", ""):
        with pytest.raises(semver.InvalidVersion):
            semver.parse_semver(bad)
    # Control: the strict parser accepts the shape it is supposed to, so the rejections
    # above are about the inputs and not about a parser that rejects everything.
    assert semver.parse_semver("0.0.0") == (0, 0, 0)
    # And the STORED parser is deliberately laxer for the legacy two-part form that
    # 249 of 265 production skills carry.
    assert semver.parse_stored_semver("1.0") == (1, 0, 0)
    with pytest.raises(semver.InvalidVersion):
        semver.parse_stored_semver("1.x")


def test_release_10_0_0_over_9_0_0_is_accepted_end_to_end(conn, client):
    """The same bug at the wire, where it actually bit."""
    peer = _seed_peer(conn, "semver")
    name = _skill_name("semver")
    assert _post(client, peer["peer_id"], _declare_body(name, "9.0.0")).status_code == 201

    ok = client.post(f"/v1/peers/me/skills/{name}/versions",
                     data=json.dumps(_declare_body(name, "10.0.0")),
                     content_type="application/json", **_auth(peer["peer_id"], BOTH))
    assert ok.status_code == 201, ok.content
    assert ok.json()["version"] == "10.0.0"

    # Control: the 409 arm still fires, so the 201 above is not a server that accepts
    # every version.
    stale = client.post(f"/v1/peers/me/skills/{name}/versions",
                        data=json.dumps(_declare_body(name, "9.5.0")),
                        content_type="application/json", **_auth(peer["peer_id"], BOTH))
    assert stale.status_code == 409
    assert stale.json()["error"] == "E_CONFLICT"
    assert "current version is 10.0.0" in stale.json()["message"]


# ======================================================================== declare

def test_declare_returns_201_and_the_full_skill_wire_shape(conn, client):
    peer = _seed_peer(conn, "declare")
    name = _skill_name("declare")
    res = _post(client, peer["peer_id"], _declare_body(name, tags=["b", "a"],
                                                       description="d"))
    assert res.status_code == 201, res.content
    body = res.json()
    # Field order is part of the wire, so the keys are
    # compared as a sequence rather than as a set.
    assert list(body.keys()) == [
        "id", "peer_id", "name", "version", "display_name", "description", "mode",
        "share_mode", "input_schema", "output_schema", "attachments_allowed",
        "returns_attachments", "requires_human_auth", "max_runtime_ms", "price_cents",
        "currency", "tags", "examples", "manifest_sig", "signing_kid", "is_current",
        "deleted_at", "created_at", "updated_at"]
    assert body["peer_id"] == str(peer["peer_id"])
    assert body["is_current"] is True and body["deleted_at"] is None
    # tags are stored as posted (the manifest hash sorts them, the column does not).
    assert body["tags"] == ["b", "a"]
    assert body["created_at"].endswith("Z")
    # meshychat_guard_status is a column (default 'quarantined') and is not part of the
    # skill wire shape. Asserting its absence, not
    # just the presence of the rest, is what catches a "helpfully" added field.
    assert "meshychat_guard_status" not in body
    with conn.cursor() as cur:
        cur.execute("SELECT meshychat_guard_status FROM skills WHERE id = %s", (body["id"],))
        assert cur.fetchone()[0] == "quarantined"


def test_declaring_a_duplicate_peer_name_version_is_409(conn, client):
    peer = _seed_peer(conn, "dup")
    name = _skill_name("dup")
    first = _post(client, peer["peer_id"], _declare_body(name))
    assert first.status_code == 201, first.content            # control, asserted first

    again = _post(client, peer["peer_id"], _declare_body(name))
    assert again.status_code == 409
    assert again.json()["error"] == "E_CONFLICT"
    assert "already exists" in again.json()["message"]

    # A DIFFERENT version of the same name is ALSO 409 from declare - the guard is
    # "a current version of this name exists", not "this exact triple exists".
    # New versions go through the versions route.
    other_version = _post(client, peer["peer_id"], _declare_body(name, "2.0.0"))
    assert other_version.status_code == 409

    # Control: a different NAME for the same peer still succeeds, so the 409s above are
    # about the duplicate rather than about a peer that can no longer declare anything.
    assert _post(client, peer["peer_id"], _declare_body(_skill_name("dup2"))).status_code == 201


def test_redeclaring_a_soft_deleted_name_and_version_is_409_not_500(conn, client):
    """The live-current guard cannot see soft-deleted rows (its
    WHERE requires deleted_at IS NULL) but UNIQUE (peer_id, name, version) counts them,
    so this lands on a 23505 that must be translated rather than escaping as a 500."""
    peer = _seed_peer(conn, "undel")
    name = _skill_name("undel")
    assert _post(client, peer["peer_id"], _declare_body(name)).status_code == 201
    assert client.delete(f"/v1/peers/me/skills/{name}",
                         **_auth(peer["peer_id"], BOTH)).status_code == 204

    clash = _post(client, peer["peer_id"], _declare_body(name, "1.0.0"))
    assert clash.status_code == 409, clash.content
    assert clash.json()["error"] == "E_CONFLICT"

    # Control: a different VERSION of the same soft-deleted name declares cleanly, so
    # the 409 above is the unique constraint and not "deleted names are poisoned".
    fresh = _post(client, peer["peer_id"], _declare_body(name, "1.0.1"))
    assert fresh.status_code == 201, fresh.content


# =============================================================== version + is_current

def test_is_current_moves_to_the_new_version_and_only_one_row_holds_it(conn, client):
    peer = _seed_peer(conn, "current")
    name = _skill_name("current")
    v1 = _post(client, peer["peer_id"], _declare_body(name, "1.0.0")).json()
    v2 = client.post(f"/v1/peers/me/skills/{name}/versions",
                     data=json.dumps(_declare_body(name, "1.1.0")),
                     content_type="application/json", **_auth(peer["peer_id"], BOTH))
    assert v2.status_code == 201, v2.content
    v2 = v2.json()

    with conn.cursor() as cur:
        cur.execute("SELECT version, is_current FROM skills WHERE peer_id = %s AND name = %s "
                    "ORDER BY version", (str(peer["peer_id"]), name))
        rows = dict(cur.fetchall())
    assert rows == {"1.0.0": False, "1.1.0": True}, rows
    assert v1["is_current"] is True and v2["is_current"] is True   # each at ITS moment

    # The list route returns only current, non-deleted versions.
    listed = client.get("/v1/peers/me/skills", **_auth(peer["peer_id"], BOTH)).json()
    assert [s["version"] for s in listed if s["name"] == name] == ["1.1.0"]


def test_releasing_over_a_missing_skill_is_404_and_over_an_existing_one_is_201(conn, client):
    peer = _seed_peer(conn, "rel404")
    name = _skill_name("rel404")
    missing = client.post(f"/v1/peers/me/skills/{name}/versions",
                          data=json.dumps(_declare_body(name, "1.0.0")),
                          content_type="application/json", **_auth(peer["peer_id"], BOTH))
    assert missing.status_code == 404
    assert missing.json()["error"] == "E_NOT_FOUND"

    # Control: the same request against a declared skill succeeds, so the 404 is about
    # the missing skill and not about the route.
    assert _post(client, peer["peer_id"], _declare_body(name, "1.0.0")).status_code == 201
    ok = client.post(f"/v1/peers/me/skills/{name}/versions",
                     data=json.dumps(_declare_body(name, "1.0.1")),
                     content_type="application/json", **_auth(peer["peer_id"], BOTH))
    assert ok.status_code == 201, ok.content


def test_a_failed_release_leaves_the_previous_current_version_intact(conn, client):
    """The flip and the insert must share one transaction. A
    separately-committed flip followed by a failed insert leaves the skill with no
    is_current=TRUE row - it vanishes from list/resolve and every later release 404s.

    The failure is induced with a version that is rejected AFTER the current row is
    read: a not-newer version. The assertion is that the previous current survived.
    """
    peer = _seed_peer(conn, "atomic")
    name = _skill_name("atomic")
    assert _post(client, peer["peer_id"], _declare_body(name, "2.0.0")).status_code == 201
    bad = client.post(f"/v1/peers/me/skills/{name}/versions",
                      data=json.dumps(_declare_body(name, "1.0.0")),
                      content_type="application/json", **_auth(peer["peer_id"], BOTH))
    assert bad.status_code == 409
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skills WHERE peer_id = %s AND name = %s "
                    "AND is_current = TRUE AND deleted_at IS NULL",
                    (str(peer["peer_id"]), name))
        assert cur.fetchone()[0] == 1, "the skill lost its current version"
    # Control: it is still releasable, which is the property the wedge destroys.
    assert client.post(f"/v1/peers/me/skills/{name}/versions",
                       data=json.dumps(_declare_body(name, "2.0.1")),
                       content_type="application/json",
                       **_auth(peer["peer_id"], BOTH)).status_code == 201


# ============================================================ cross-peer ownership

def test_a_skill_from_another_peer_cannot_be_updated(conn, client):
    """The whole cross-peer guard is `WHERE peer_id = <the authenticated peer>`, because
    `skills` has no row-level security to back it up (see the RLS measurement above).

    The OWNER's update is asserted FIRST: without it, a 404 for the stranger would be
    indistinguishable from a broken fixture, a wrong URL or a skill that was never
    created.
    """
    owner = _seed_peer(conn, "owner")
    stranger = _seed_peer(conn, "stranger")
    name = _skill_name("owned")
    assert _post(client, owner["peer_id"], _declare_body(name)).status_code == 201

    patch = {"share_mode": "private", "description": "by the owner", "tags": []}

    # POSITIVE CONTROL: the owner can.
    mine = client.patch(f"/v1/peers/me/skills/{name}", data=json.dumps(patch),
                        content_type="application/json", **_auth(owner["peer_id"], BOTH))
    assert mine.status_code == 200, mine.content
    assert mine.json()["description"] == "by the owner"

    # The stranger holds the same scopes and sends the same body to the same URL.
    theirs = client.patch(f"/v1/peers/me/skills/{name}",
                          data=json.dumps({**patch, "description": "by the stranger"}),
                          content_type="application/json",
                          **_auth(stranger["peer_id"], BOTH))
    assert theirs.status_code == 404, theirs.content
    assert theirs.json()["error"] == "E_NOT_FOUND"

    # And the row is untouched: a 404 that still wrote would be the worst outcome, and
    # a status-only assertion cannot tell the two apart.
    with conn.cursor() as cur:
        cur.execute("SELECT description, peer_id FROM skills WHERE name = %s AND is_current",
                    (name,))
        description, row_peer = cur.fetchone()
    assert description == "by the owner"
    assert row_peer == owner["peer_id"]


def test_a_skill_from_another_peer_cannot_be_deleted(conn, client):
    owner = _seed_peer(conn, "delowner")
    stranger = _seed_peer(conn, "delstranger")
    name = _skill_name("del")
    assert _post(client, owner["peer_id"], _declare_body(name)).status_code == 201

    theirs = client.delete(f"/v1/peers/me/skills/{name}", **_auth(stranger["peer_id"], BOTH))
    assert theirs.status_code == 404
    with conn.cursor() as cur:
        cur.execute("SELECT deleted_at FROM skills WHERE name = %s", (name,))
        assert cur.fetchone()[0] is None, "a 404 that still soft-deleted the row"

    # POSITIVE CONTROL: the owner can, and the second delete is then a 404 for the
    # owner too - which also proves the 404 above is reachable for the right reason.
    assert client.delete(f"/v1/peers/me/skills/{name}",
                         **_auth(owner["peer_id"], BOTH)).status_code == 204
    assert client.delete(f"/v1/peers/me/skills/{name}",
                         **_auth(owner["peer_id"], BOTH)).status_code == 404


# ==================================================================== scope gates

def test_declare_needs_the_declare_scope(conn, client):
    peer = _seed_peer(conn, "scope")
    with_scope = _post(client, peer["peer_id"], _declare_body(_skill_name("s1")), [DECLARE])
    assert with_scope.status_code == 201, with_scope.content       # control first

    without = _post(client, peer["peer_id"], _declare_body(_skill_name("s2")), [])
    assert without.status_code == 403
    assert without.json()["error"] == "E_SCOPE_REQUIRED"
    assert "skills:declare" in without.json()["message"]


def test_publication_share_modes_need_the_publish_scope_too(conn, client):
    """Declaring a skill and publishing it are separate authorities."""
    peer = _seed_peer(conn, "pub")
    # Control: self-description needs declare ALONE.
    for mode in V.SELF_DESCRIPTION_SHARE_MODES:
        res = _post(client, peer["peer_id"],
                    _declare_body(_skill_name("sd"), share_mode=mode), [DECLARE])
        assert res.status_code == 201, (mode, res.content)

    # Publication needs the extra authority...
    for mode in V.PUBLICATION_SHARE_MODES:
        body = _declare_body(_skill_name("pb"), share_mode=mode,
                             price_cents=100, currency="usd")
        denied = _post(client, peer["peer_id"], body, [DECLARE])
        assert denied.status_code == 403, (mode, denied.content)
        assert "skills:publish" in denied.json()["message"]
        # ...and is accepted when it is held. Same body, one extra scope.
        ok = _post(client, peer["peer_id"], body, BOTH)
        assert ok.status_code == 201, (mode, ok.content)


def test_publish_scope_alone_is_not_enough(conn, client):
    """The declare check is unconditional and FIRST: publish is an additional authority,
    not a substitute. Getting this backwards would widen access for anyone granted the
    new scope without the old one - and the message must name `skills:declare`, not
    `skills:publish`, or it leaks the shape of what the caller is missing."""
    peer = _seed_peer(conn, "publonly")
    denied = _post(client, peer["peer_id"],
                   _declare_body(_skill_name("po"), share_mode="public"), [PUBLISH])
    assert denied.status_code == 403
    assert denied.json()["message"].endswith("skills:declare")
    # Control: adding declare (keeping publish) makes the identical request succeed.
    assert _post(client, peer["peer_id"],
                 _declare_body(_skill_name("po2"), share_mode="public"),
                 BOTH).status_code == 201


def test_the_two_share_mode_sets_are_disjoint_and_cover_every_variant():
    """Control for every scope test above.

    Without it each of those assertions could be vacuously true for a build where the
    two tuples overlapped, or where a share_mode was added and tested by neither.
    """
    assert not set(V.PUBLICATION_SHARE_MODES) & set(V.SELF_DESCRIPTION_SHARE_MODES)
    assert (set(V.PUBLICATION_SHARE_MODES) | set(V.SELF_DESCRIPTION_SHARE_MODES)
            == set(V.SHARE_MODES))
    assert len(V.SHARE_MODES) == 5, (
        "a share_mode was added without deciding which authority it needs")


def test_delete_needs_declare_but_not_publish(conn, client):
    """Removing a skill from discovery is not publishing, and requiring
    publish would strand a peer whose publish grant lapsed with a public skill it could
    not withdraw."""
    peer = _seed_peer(conn, "delscope")
    name = _skill_name("delscope")
    assert _post(client, peer["peer_id"],
                 _declare_body(name, share_mode="public"), BOTH).status_code == 201

    no_scope = client.delete(f"/v1/peers/me/skills/{name}", **_auth(peer["peer_id"], []))
    assert no_scope.status_code == 403
    assert no_scope.json()["error"] == "E_SCOPE_REQUIRED"
    # Control: declare ALONE is enough, even though the skill is public.
    assert client.delete(f"/v1/peers/me/skills/{name}",
                         **_auth(peer["peer_id"], [DECLARE])).status_code == 204


def test_an_unauthenticated_or_wrong_audience_request_never_reaches_the_registry(conn, client):
    peer = _seed_peer(conn, "auth")
    anon = client.get("/v1/peers/me/skills")
    assert anon.status_code == 401 and anon.json()["error"] == "E_AUTH_MISSING"

    wrong_aud = client.get("/v1/peers/me/skills", HTTP_AUTHORIZATION="Bearer " + _mint(
        peer["peer_id"], scopes=BOTH, aud="dashboard"))
    assert wrong_aud.status_code == 403

    # Control: the same peer with the right audience is served.
    assert client.get("/v1/peers/me/skills", **_auth(peer["peer_id"], BOTH)).status_code == 200


# =================================================================== validation

def test_marketplace_paid_requires_price_and_currency(conn, client):
    peer = _seed_peer(conn, "paid")
    ok = _post(client, peer["peer_id"],
               _declare_body(_skill_name("paid"), share_mode="marketplace_paid",
                             price_cents=500, currency="usd"), BOTH)
    assert ok.status_code == 201, ok.content                       # control first

    bad = _post(client, peer["peer_id"],
                _declare_body(_skill_name("paid2"), share_mode="marketplace_paid"), BOTH)
    assert bad.status_code == 400
    assert bad.json() == {
        "error": "E_VALIDATION",
        "message": "share_mode = marketplace_paid requires price_cents and currency"}


def test_a_half_manifest_attestation_pair_is_rejected(conn, client):
    """Dropping the supplied half silently would store an unattested
    skill while the publisher believed it had attested one."""
    peer = _seed_peer(conn, "attest")
    sig = P.b64u(bytes(range(64)))

    # POSITIVE CONTROL: the complete pair is accepted and stored.
    ok = _post(client, peer["peer_id"],
               _declare_body(_skill_name("at"), manifest_sig=sig, signing_kid="kid-1"), BOTH)
    assert ok.status_code == 201, ok.content
    assert ok.json()["manifest_sig"] == list(bytes(range(64)))
    assert ok.json()["signing_kid"] == "kid-1"

    for body in ({"manifest_sig": sig}, {"signing_kid": "kid-1"}):
        half = _post(client, peer["peer_id"], _declare_body(_skill_name("at"), **body), BOTH)
        assert half.status_code == 400, (body, half.content)
        assert half.json()["message"] == \
            "manifest_sig and signing_kid must be supplied together"

    # A present-but-malformed signature is a 400 too, not a stored half-row.
    short = _post(client, peer["peer_id"],
                  _declare_body(_skill_name("at"), manifest_sig=P.b64u(b"tooshort"),
                                signing_kid="kid-1"), BOTH)
    assert short.status_code == 400
    assert "64 raw Ed25519 bytes" in short.json()["message"]


def test_a_malformed_body_is_400_and_a_wrong_shaped_one_is_422(conn, client):
    """STATUS distinguishes malformed JSON from a wrongly shaped body. The body uses this
    app's JSON envelope rather than plain text, matching apps/authapi."""
    peer = _seed_peer(conn, "shape")
    ok = _post(client, peer["peer_id"], _declare_body(_skill_name("sh")), BOTH)
    assert ok.status_code == 201, ok.content                       # control first

    syntax = client.post("/v1/peers/me/skills", data="{not json",
                         content_type="application/json", **_auth(peer["peer_id"], BOTH))
    assert syntax.status_code == 400

    missing = _post(client, peer["peer_id"],
                    {k: v for k, v in _declare_body("x").items() if k != "version"}, BOTH)
    assert missing.status_code == 422
    assert "version" in missing.json()["message"]

    unknown_variant = _post(client, peer["peer_id"],
                            _declare_body(_skill_name("sh"), mode="telepathy"), BOTH)
    assert unknown_variant.status_code == 422


def test_a_declared_runtime_profile_is_scrubbed_of_node_local_material(conn, client):
    """The profile declares classes. Concrete node-local material
    (absolute paths, credential-shaped values, userinfo URLs) is rejected, and the
    rejection names the FIELD PATH without echoing the value back."""
    peer = _seed_peer(conn, "runtime")
    clean = {"harness": "claude_code", "provider_class": "anthropic",
             "policy_tags": ["policy:cost_budget"],
             "sandbox_constraints": {"backend": "container", "network_mode": "none"}}
    ok = _post(client, peer["peer_id"],
               _declare_body(_skill_name("rt"), runtime_profile=clean), BOTH)
    assert ok.status_code == 201, ok.content                       # control first
    assert ok.json()["runtime_profile"]["harness"] == "claude_code"

    dirty = {**clean, "harness": "custom:weird", "provider_class": "local",
             "policy_tags": ["policy:ok"],
             "sandbox_constraints": {"backend": "container"},
             "session_affordances": {"terminal_access": "interactive"},
             "tool_interfaces": ["mcp"]}
    # A secret-shaped string anywhere in the object is refused.
    leaky = {**dirty, "harness": "sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"}
    res = _post(client, peer["peer_id"],
                _declare_body(_skill_name("rt"), runtime_profile=leaky), BOTH)
    assert res.status_code == 400, res.content
    assert "sk-ant" not in res.json()["message"], "the rejection echoed the secret back"

    # An unknown KEY is a deserialization failure (deny_unknown_fields) -> 422, a
    # different status from the validation failure above, and the distinction is the
    # point: collapsing them would move a whole class of request from 422 to 400.
    unknown = _post(client, peer["peer_id"],
                    _declare_body(_skill_name("rt"),
                                  runtime_profile={"harness": "codex", "api_key": "x"}), BOTH)
    assert unknown.status_code == 422, unknown.content

    # Control for the control: `dirty` itself (well-formed, unknown-but-valid slug)
    # is ACCEPTED, so the 400 above is about the secret and not about the shape.
    assert _post(client, peer["peer_id"],
                 _declare_body(_skill_name("rt"), runtime_profile=dirty),
                 BOTH).status_code == 201


def test_an_empty_runtime_profile_normalizes_to_absent(conn, client):
    """The ABSENCE LAW (manifest hash rule v1.1 point 2). An "declared but empty"
    profile must collapse to NULL, or a legacy skill's manifest hash changes and every
    pinned grant and published attestation on it breaks at once."""
    peer = _seed_peer(conn, "empty")
    res = _post(client, peer["peer_id"],
                _declare_body(_skill_name("mt"), runtime_profile={}), BOTH)
    assert res.status_code == 201, res.content
    assert "runtime_profile" not in res.json()
    with conn.cursor() as cur:
        cur.execute("SELECT runtime_profile FROM skills WHERE id = %s", (res.json()["id"],))
        assert cur.fetchone()[0] is None
    # Control: a NON-empty profile is stored, so the NULL above is normalization and
    # not a write path that drops the column.
    ok = _post(client, peer["peer_id"],
               _declare_body(_skill_name("mt"), runtime_profile={"harness": "codex"}), BOTH)
    assert ok.json()["runtime_profile"] == {"harness": "codex"}


def test_the_server_stamps_the_lease_and_ignores_the_clients_timestamp(conn, client):
    peer = _seed_peer(conn, "lease")
    res = _post(client, peer["peer_id"],
                _declare_body(_skill_name("ls"),
                              runtime_status={"lease_ttl_secs": 60,
                                              "lease_declared_at": "1999-01-01T00:00:00Z",
                                              "host_label": "hqd"}), BOTH)
    assert res.status_code == 201, res.content
    stamped = res.json()["runtime_status"]["lease_declared_at"]
    assert not stamped.startswith("1999"), "a client backdated its own freshness lease"
    # Control: a status WITHOUT a ttl carries no declared_at at all - a declared_at
    # without a ttl is meaningless, so it is cleared rather than carried.
    no_ttl = _post(client, peer["peer_id"],
                   _declare_body(_skill_name("ls"), runtime_status={"host_label": "hqd"}),
                   BOTH)
    assert "lease_declared_at" not in no_ttl.json()["runtime_status"]
    # And a host_label that is really an ADDRESS is refused.
    addr = _post(client, peer["peer_id"],
                 _declare_body(_skill_name("ls"),
                               runtime_status={"host_label": "10.0.0.1"}), BOTH)
    assert addr.status_code == 400


# ======================================================= runtime redaction, per branch
#
# `runtime.py:_redaction_scan` (337-356) is four independent string patterns and a key
# denylist. The existing probe above puts
# `sk-ant-...` in `harness`, which is ALSO rejected by the vocabulary slug check two
# lines later (`_check_vocab_slug`, runtime.py:373-376) - so it cannot tell the
# credential-prefix branch from the slug branch. Every probe below therefore goes in
# `runtime_status.harness_version`, an `_opt_str` with no check after the scan
# (`validate_runtime_status`, runtime.py:401-416), and each probe matches exactly ONE
# pattern, so deleting that pattern turns the 400 into a 201.

@pytest.mark.parametrize("probe,reason", [
    ("/home/mentor/.claude/credentials.json",
     "absolute paths are node-local material"),
    ("https://svc:hunter2@meshy.example/v1", "URLs with userinfo can carry credentials"),
    ("eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl", "JWT-shaped value"),
    ("ghp_" + "A" * 36, "credential-prefix-shaped value"),
])
def test_each_runtime_redaction_pattern_refuses_on_its_own(conn, client, probe, reason):
    """One branch per case.

    The POSITIVE CONTROL is asserted first and carries the same field: an ordinary
    version string in `harness_version` publishes, so the 400 below is the scan and not
    "this field is unusable".
    """
    peer = _seed_peer(conn, "redact")
    ok = _post(client, peer["peer_id"],
               _declare_body(_skill_name("rd"),
                             runtime_status={"harness_version": "1.4.2"}), BOTH)
    assert ok.status_code == 201, ok.content
    assert ok.json()["runtime_status"]["harness_version"] == "1.4.2"

    res = _post(client, peer["peer_id"],
                _declare_body(_skill_name("rd"),
                              runtime_status={"harness_version": probe}), BOTH)
    assert res.status_code == 400, res.content
    message = res.json()["message"]
    # The PATH is named and the value is not: an error that echoed the match would
    # copy the credential into the response body and the server log.
    assert "runtime_status.harness_version" in message
    assert reason in message, message
    assert probe not in message, "the rejection echoed the probe back"


def test_the_denylisted_key_scan_fires_and_exempts_credential_proxy(conn, client):
    """The scanner checks both keys and values.

    TWO HALVES, and they are tested at different layers on purpose.

    The key half is unreachable over HTTP: `runtime_profile` is strongly
    typed to reject unknown keys, so a key like `api_key` is a 422 from
    `_deny_unknown` (runtime.py:149-152) before any validator runs - which
    `test_a_declared_runtime_profile_is_scrubbed_of_node_local_material` already
    asserts. So the denylist itself is exercised directly on `_redaction_scan`, and
    that is a weaker claim than a route test: it proves the scanner fires, not that a
    request reaches it.

    The EXEMPTION half IS reachable, and is the one that matters in production:
    `credential_proxy` is a legitimate sandbox field whose name matches the denylist
    (`DENYLIST_BENIGN_KEYS`). Dropping the exemption would
    reject every profile that declares a credential proxy.
    """
    with pytest.raises(rt.RuntimeValidateError) as caught:
        rt._redaction_scan({"api_key": "anything"}, "probe")
    assert "key matches the secret-name denylist" in str(caught.value)
    # Control: the same scanner passes a key that is not on the denylist, so the raise
    # above is about the NAME and not about a scanner that rejects every object.
    rt._redaction_scan({"harness": "codex"}, "probe")

    # ...and the benign exemption, end to end.
    peer = _seed_peer(conn, "benign")
    res = _post(client, peer["peer_id"],
                _declare_body(_skill_name("bn"),
                              runtime_profile={"harness": "claude_code",
                                               "sandbox_constraints": {
                                                   "backend": "container",
                                                   "credential_proxy": True}}), BOTH)
    assert res.status_code == 201, res.content
    assert res.json()["runtime_profile"]["sandbox_constraints"]["credential_proxy"] is True


def test_a_runtime_object_over_the_8_kib_cap_is_refused(conn, client):
    """The cap is on the serialized
    object, so it has to be measured after `status_to_wire`, and the content here is
    deliberately innocuous - the only thing wrong with it is its size."""
    peer = _seed_peer(conn, "cap")
    # Control first: 40 entries is ~4.9 KiB and publishes. Every entry is a legal
    # model_ref (non-empty, <=128 chars), so nothing but the byte count differs.
    under = _post(client, peer["peer_id"],
                  _declare_body(_skill_name("cap"),
                                runtime_status={"model_refs": ["m" * 120] * 40}), BOTH)
    assert under.status_code == 201, under.content

    over = _post(client, peer["peer_id"],
                 _declare_body(_skill_name("cap"),
                               runtime_status={"model_refs": ["m" * 120] * 80}), BOTH)
    assert over.status_code == 400, over.content
    assert "exceeds the 8192-byte cap" in over.json()["message"]


# ======================================================= lesson-mode publish gates
#
# Lesson payloads are stored verbatim in `skills.examples`. This app does not fan them out over
# WebSocket to subscribers (lessons.py:1-23; views.py:47-51). The gates below prevent a
# credential from reaching storage, so each gate is tested BOTH ways round in the same
# test: a payload carrying a credential is REFUSED, and the same payload with the
# credential replaced by prose is ACCEPTED. Without the second half a 400 proves
# nothing - it is also what a malformed payload, a missing mentor key or a bad
# signature produces, and all four of those are live failure modes here.

#: Deliberately fake and filler-only, shaped to match `SECRET_RE` (lessons.py:46-48).
#: Nothing here is or was a
#: live credential.
FAKE_AWS_KEY = "AKIA" + "Q" * 16
FAKE_PEM_HEADER = "-----BEGIN OPENSSH PRIVATE KEY-----"
FAKE_LLM_KEY = "sk-ant-" + "0" * 24


def _seed_peer_key(conn, peer_id) -> bytes:
    """One ACTIVE `peer_keys` row for `peer_id`, and the Ed25519 seed behind it.

    `lessons.find_active_peer_key` (lessons.py:170-186) resolves the mentor key from exactly this
    table, so no lesson signature verifies without a row here.
    """
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s,%s,%s,TRUE)",
                    (str(peer_id), P.kid_for_peer_key(public), P.b64u(public)))
    return seed


def _lesson_file(content: str, *, encoding="utf8") -> dict:
    """One `files[]` entry. The sha256 is the real digest of the DECODED content."""
    stored = (base64.b64encode(content.encode("utf-8")).decode("ascii")
              if encoding == "base64" else content)
    return {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": stored, "encoding": encoding}


def _lesson_payload(mentor_peer_id, *, fragment="Keep the diff small.", files=None) -> dict:
    """An unsigned `LessonPayload`. `target_harness` is `claude_code` because v1
    accepts nothing else (lessons.py:81-83)."""
    return {"target_harness": "claude_code",
            "claude_md_fragments": [{"content": fragment}],
            "files": list(files or []),
            "mentor_peer_id": str(mentor_peer_id),
            "signature": None}


def _sign_lesson(payload: dict, seed: bytes) -> dict:
    """Sign the bytes `canonical_signing_bytes` will hash: `signature` REPLACED WITH
    NULL, never deleted (lessons.py:151-167). A producer that
    deletes the key signs a different message than the verifier reconstructs."""
    return {**payload,
            "signature": P.b64u(P.sign(seed, P.jcs({**payload, "signature": None})))}


def _declare_lesson(client, peer_id, payload, name=None, scopes=BOTH):
    return _post(client, peer_id,
                 _declare_body(name or _skill_name("lesson"), mode="lesson",
                               examples=payload), scopes)


def _skill_rows(conn, name) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skills WHERE name = %s", (name,))
        return cur.fetchone()[0]


@pytest.mark.parametrize("secret", [FAKE_AWS_KEY, FAKE_PEM_HEADER, FAKE_LLM_KEY])
def test_a_credential_in_a_claude_md_fragment_is_refused(conn, client, secret):
    """Lesson fragment credential scanning.

    The fragment is republished verbatim into every subscriber's CLAUDE.md, so an AWS
    key, a PEM private-key header or an LLM key pasted into one is a fan-out of that
    credential. The refused and the accepted payload differ ONLY in the fragment text -
    both are signed by the same active mentor key and both name the publishing peer as
    mentor - so the 400 can only be the scan.
    """
    peer = _seed_peer(conn, "lesson-frag")
    seed = _seed_peer_key(conn, peer["peer_id"])

    clean = _sign_lesson(_lesson_payload(peer["peer_id"],
                                         fragment="Run the tests before you push."), seed)
    ok = _declare_lesson(client, peer["peer_id"], clean)
    assert ok.status_code == 201, ok.content                       # control first
    assert ok.json()["examples"]["claude_md_fragments"][0]["content"] \
        == "Run the tests before you push."

    name = _skill_name("lesson-frag")
    leaky = _sign_lesson(_lesson_payload(peer["peer_id"],
                                         fragment=f"export CRED={secret}"), seed)
    res = _declare_lesson(client, peer["peer_id"], leaky, name=name)
    assert res.status_code == 400, res.content
    assert res.json()["error"] == "E_VALIDATION"
    message = res.json()["message"]
    # The INDEX is named and the matched bytes are not (lessons.py:21-22): echoing the
    # match would copy the credential into the response body and the server log.
    assert "claude_md_fragments[0]" in message, message
    assert secret not in message, "the rejection echoed the credential back"
    # And nothing was persisted - a 400 that still wrote the row would have leaked it
    # into `skills.examples` anyway, and a status-only assertion cannot tell them apart.
    assert _skill_rows(conn, name) == 0


@pytest.mark.parametrize("encoding", ["utf8", "base64"])
def test_a_credential_in_an_inline_file_body_is_refused_including_base64(
        conn, client, encoding):
    """Inline-file credential scanning.

    THE FRAGMENTS IN THESE PAYLOADS ARE CLEAN. Scanning only `claude_md_fragments` let
    an author smuggle a credential past the control by moving it into `files[].content`,
    and the base64 case is the same evasion one layer down - the body is DECODED before
    the scan, so declaring `encoding=base64` does not hide it.
    """
    peer = _seed_peer(conn, "lesson-file")
    seed = _seed_peer_key(conn, peer["peer_id"])

    clean = _sign_lesson(
        _lesson_payload(peer["peer_id"],
                        files=[_lesson_file("print('hello')", encoding=encoding)]), seed)
    ok = _declare_lesson(client, peer["peer_id"], clean)
    assert ok.status_code == 201, ok.content                       # control first

    name = _skill_name("lesson-file")
    leaky = _sign_lesson(
        _lesson_payload(peer["peer_id"],
                        files=[_lesson_file(f"AWS_ACCESS_KEY_ID={FAKE_AWS_KEY}",
                                            encoding=encoding)]), seed)
    assert FAKE_AWS_KEY not in json.dumps(leaky["claude_md_fragments"]), \
        "this test only means something while the fragments are clean"
    res = _declare_lesson(client, peer["peer_id"], leaky, name=name)
    assert res.status_code == 400, res.content
    message = res.json()["message"]
    assert "files[0]" in message, message
    assert FAKE_AWS_KEY not in message
    assert _skill_rows(conn, name) == 0


def test_a_base64_file_body_that_is_not_base64_is_a_hard_error(conn, client):
    """A decode failure must be a refusal,
    not a silent skip. If it fell through to "treat it as text", mislabelling `encoding`
    would be enough to walk a credential past the scan."""
    peer = _seed_peer(conn, "lesson-b64")
    seed = _seed_peer_key(conn, peer["peer_id"])

    bad = _sign_lesson(_lesson_payload(
        peer["peer_id"],
        files=[{"sha256": "0" * 64, "content": "not base64 !!!", "encoding": "base64"}]), seed)
    res = _declare_lesson(client, peer["peer_id"], bad)
    assert res.status_code == 400, res.content
    assert "not valid base64" in res.json()["message"]

    # Control: the same content declared as utf8 publishes, so the 400 above is the
    # decode and not the content.
    ok = _sign_lesson(_lesson_payload(
        peer["peer_id"],
        files=[{"sha256": "0" * 64, "content": "not base64 !!!", "encoding": "utf8"}]), seed)
    assert _declare_lesson(client, peer["peer_id"], ok).status_code == 201


def test_a_mentors_signed_lesson_cannot_be_republished_by_another_peer(
        conn, client, monkeypatch):
    """The re-attribution guard.

    The signature proves that `mentor_peer_id` signed those bytes. It does NOT prove
    that the peer presenting them IS that mentor, and lesson payloads are fanned out to
    every subscriber, so any subscriber can observe a respected mentor's validly-signed
    payload and re-POST it verbatim. Without this check the signature still verifies,
    and the row persists owned by the attacker while carrying the victim's attribution.

    The bytes posted by the thief are IDENTICAL to the bytes the mentor published - the
    same dict object - so the only difference between the 201 and the 400 is which JWT
    carried it.
    """
    mentor = _seed_peer(conn, "lesson-mentor")
    thief = _seed_peer(conn, "lesson-thief")
    _seed_peer_key(conn, thief["peer_id"])          # the thief has its own valid key
    seed = _seed_peer_key(conn, mentor["peer_id"])
    payload = _sign_lesson(_lesson_payload(mentor["peer_id"],
                                           fragment="Name the rival hypothesis."), seed)

    published = _declare_lesson(client, mentor["peer_id"], payload)
    assert published.status_code == 201, published.content         # control first

    stolen_name = _skill_name("lesson-stolen")
    theirs = _declare_lesson(client, thief["peer_id"], payload, name=stolen_name)
    assert theirs.status_code == 400, theirs.content
    assert theirs.json()["message"] == (
        "lesson signature invalid: mentor_peer_id does not match the authenticated "
        "publishing peer")
    assert _skill_rows(conn, stolen_name) == 0

    # And the check sits BEFORE the placeholder hatch (lessons.py:207 precedes :212),
    # so dev mode cannot re-attribute either. Same mismatch, signature='placeholder',
    # hatch open: still the mismatch message, not the placeholder one.
    monkeypatch.setenv("MESHYCHAT_LESSON_INSECURE_PLACEHOLDER", "true")
    hatch_name = _skill_name("lesson-hatch")
    via_hatch = _declare_lesson(
        client, thief["peer_id"],
        {**_lesson_payload(mentor["peer_id"]), "signature": "placeholder"},
        name=hatch_name)
    assert via_hatch.status_code == 400, via_hatch.content
    assert "does not match the authenticated publishing peer" in via_hatch.json()["message"]
    assert _skill_rows(conn, hatch_name) == 0
    # Control for the hatch itself: with the SAME env var and the SAME signature, a peer
    # publishing its OWN lesson is accepted - so the 400s above are the binding, not a
    # hatch that never opens.
    assert _declare_lesson(
        client, thief["peer_id"],
        {**_lesson_payload(thief["peer_id"]), "signature": "placeholder"}).status_code == 201


def test_the_placeholder_signature_is_only_accepted_in_declared_dev_mode(
        conn, client, monkeypatch):
    """`_placeholder_allowed` reads literally `"true"`.

    An unconditional hatch would make every signature check above optional: publish with
    `signature: "placeholder"` and nothing is verified at all. The env var is read at
    CALL time, which is what lets one test exercise both arms.
    """
    peer = _seed_peer(conn, "lesson-ph")
    _seed_peer_key(conn, peer["peer_id"])
    payload = {**_lesson_payload(peer["peer_id"]), "signature": "placeholder"}

    monkeypatch.delenv("MESHYCHAT_LESSON_INSECURE_PLACEHOLDER", raising=False)
    closed = _declare_lesson(client, peer["peer_id"], payload)
    assert closed.status_code == 400, closed.content
    assert closed.json()["message"] == (
        "lesson signature invalid: placeholder signature rejected "
        "(set MESHYCHAT_LESSON_INSECURE_PLACEHOLDER=true for dev mode)")

    # Control: the identical payload with the hatch declared open is accepted, so the
    # 400 above is the gate and not the payload.
    monkeypatch.setenv("MESHYCHAT_LESSON_INSECURE_PLACEHOLDER", "true")
    assert _declare_lesson(client, peer["peer_id"], payload).status_code == 201

    # `"1"` is NOT `"true"`: the rest of this codebase accepts `1|true|TRUE` for its
    # feature flags (service.py:56), and this one deliberately does not - a value that
    # opens a security hatch should take one spelling, not four.
    monkeypatch.setenv("MESHYCHAT_LESSON_INSECURE_PLACEHOLDER", "1")
    assert _declare_lesson(client, peer["peer_id"], payload).status_code == 400
    # ...and case does not matter, which is the other half of `.lower() == "true"`.
    monkeypatch.setenv("MESHYCHAT_LESSON_INSECURE_PLACEHOLDER", "TRUE")
    assert _declare_lesson(client, peer["peer_id"], payload).status_code == 201


def test_a_lesson_signature_is_actually_verified_against_the_mentor_key(conn, client):
    """`verify_strict` checks the canonical
    bytes. Without it every check above is decoration: a payload's `mentor_peer_id`
    would be believed on the strength of an arbitrary 64 bytes."""
    peer = _seed_peer(conn, "lesson-verify")
    seed = _seed_peer_key(conn, peer["peer_id"])

    payload = _sign_lesson(_lesson_payload(peer["peer_id"], fragment="Cite the field."),
                           seed)
    assert _declare_lesson(client, peer["peer_id"], payload).status_code == 201  # control

    # The SAME signature over a CHANGED body. Everything else is identical, so a
    # verifier that did not actually hash the payload would accept this.
    tampered = {**payload,
                "claude_md_fragments": [{"content": "Skip the tests, ship it."}]}
    name = _skill_name("lesson-tamper")
    res = _declare_lesson(client, peer["peer_id"], tampered, name=name)
    assert res.status_code == 400, res.content
    assert res.json()["message"] == \
        "lesson signature invalid: lesson signature verification failed"
    assert _skill_rows(conn, name) == 0

    # A well-formed signature from a key that is NOT the registered mentor key: the
    # bytes decode to 64 and the length checks pass, so only verification rejects it.
    impostor = _sign_lesson(_lesson_payload(peer["peer_id"], fragment="Cite the field."),
                            os.urandom(32))
    assert len(P.b64u_decode(impostor["signature"])) == 64
    wrong_key = _declare_lesson(client, peer["peer_id"], impostor)
    assert wrong_key.status_code == 400, wrong_key.content
    assert "lesson signature verification failed" in wrong_key.json()["message"]


def test_a_revoked_or_deactivated_mentor_key_no_longer_verifies_a_lesson(conn, client):
    """`active = TRUE AND revoked_at IS NULL`.

    The two conditions are asserted SEPARATELY. They are set by different operations
    (key rotation flips `active`; a compromise report stamps `revoked_at`), so a lookup
    that kept one and dropped the other would still pass a test that only ever set both
    at once - and would keep verifying lessons signed by a key its owner has reported
    stolen.
    """
    peer = _seed_peer(conn, "lesson-revoke")
    seed = _seed_peer_key(conn, peer["peer_id"])

    def publish():
        return _declare_lesson(
            client, peer["peer_id"],
            _sign_lesson(_lesson_payload(peer["peer_id"]), seed))

    assert publish().status_code == 201, "control: the active key verifies"

    def set_key(**cols):
        with conn.cursor() as cur:
            cur.execute("UPDATE peer_keys SET active = %s, revoked_at = %s "
                        "WHERE peer_id = %s",
                        (cols["active"], cols["revoked_at"], str(peer["peer_id"])))

    now = _dt.datetime.now(_dt.timezone.utc)
    for label, cols in (("revoked", {"active": True, "revoked_at": now}),
                        ("deactivated", {"active": False, "revoked_at": None})):
        set_key(**cols)
        res = publish()
        assert res.status_code == 400, (label, res.content)
        assert res.json()["message"] == (
            f"mentor peer {peer['peer_id']} has no active signing key in peer_keys"), label

    # Control, asserted last as well as first: restoring the row makes the identical
    # request succeed again, so the 400s are the predicate and not a broken fixture.
    set_key(active=True, revoked_at=None)
    assert publish().status_code == 201


def test_with_two_active_mentor_keys_the_newest_verifies_and_the_older_does_not(conn, client):
    """Migration 030 lets a key rotation hold two ACTIVE keys for one peer, so `find_active_peer_key` picks the one
    that verifies by its ORDER BY alone (created_at DESC, id DESC); only a comment defended it
    (REFACTOR-QUEUE-2026-11 A8). The older key is inserted FIRST, so a lookup with no ORDER BY, or an ascending one,
    returns it and the newest key's lesson is refused."""
    peer = _seed_peer(conn, "lesson-rotate")
    old_seed, new_seed = os.urandom(32), os.urandom(32)
    with conn.cursor() as cur:
        for seed, age in ((old_seed, "1 hour"), (new_seed, "0 seconds")):
            public = P.public_key_of(seed)
            cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active, created_at) "
                        "VALUES (%s,%s,%s,TRUE, now() - %s::interval)",
                        (str(peer["peer_id"]), P.kid_for_peer_key(public), P.b64u(public), age))
        cur.execute("SELECT count(*) FROM peer_keys WHERE peer_id = %s AND active AND revoked_at IS NULL",
                    (str(peer["peer_id"]),))
        assert cur.fetchone()[0] == 2, "the control: both keys are active, so ordering decides"

    newest = _declare_lesson(client, peer["peer_id"],
                             _sign_lesson(_lesson_payload(peer["peer_id"]), new_seed))
    assert newest.status_code == 201, newest.content
    older = _declare_lesson(client, peer["peer_id"],
                            _sign_lesson(_lesson_payload(peer["peer_id"], fragment="Signed by the older key."),
                                         old_seed))
    assert older.status_code == 400, older.content
    assert older.json()["message"] == "lesson signature invalid: lesson signature verification failed"


def test_a_lesson_gate_runs_on_release_and_on_metadata_update_too(conn, client):
    """service.py:173 and service.py:245-250 reach the same two validators as
    service.py:121. A guard wired into `create_skill` alone would leave a peer able to
    declare a clean lesson and then push the credential through the versions route or a
    PATCH - the row is fanned out from whichever write lands last."""
    peer = _seed_peer(conn, "lesson-routes")
    seed = _seed_peer_key(conn, peer["peer_id"])
    name = _skill_name("lesson-routes")
    clean = _sign_lesson(_lesson_payload(peer["peer_id"]), seed)
    leaky = _sign_lesson(_lesson_payload(peer["peer_id"],
                                         fragment=f"key={FAKE_LLM_KEY}"), seed)

    assert _declare_lesson(client, peer["peer_id"], clean, name=name).status_code == 201

    def release(examples, version):
        return client.post(f"/v1/peers/me/skills/{name}/versions",
                           data=json.dumps(_declare_body(name, version, mode="lesson",
                                                         examples=examples)),
                           content_type="application/json",
                           **_auth(peer["peer_id"], BOTH))

    assert release(clean, "1.1.0").status_code == 201, "control: the clean release lands"
    refused = release(leaky, "1.2.0")
    assert refused.status_code == 400, refused.content
    assert "claude_md_fragments[0]" in refused.json()["message"]

    def patch(examples):
        return client.patch(f"/v1/peers/me/skills/{name}",
                            data=json.dumps({"share_mode": "private", "tags": [],
                                             "examples": examples}),
                            content_type="application/json",
                            **_auth(peer["peer_id"], BOTH))

    assert patch(clean).status_code == 200, "control: the clean update lands"
    patched = patch(leaky)
    assert patched.status_code == 400, patched.content
    assert "claude_md_fragments[0]" in patched.json()["message"]
    # The stored payload is still the clean one: a refused PATCH must not have written.
    with conn.cursor() as cur:
        cur.execute("SELECT examples FROM skills WHERE name = %s AND is_current", (name,))
        assert FAKE_LLM_KEY not in json.dumps(cur.fetchone()[0])


# ================================================================= manifest hash

def test_manifest_hash_matches_the_known_golden_vector():
    """The legacy fixture must hash to these exact known-answer bytes.

    This assertion pins the grant hash, so a change to its encoding or field order cannot merely
    look reasonable while producing different bytes.
    """
    actual = manifest.manifest_hash_for_fields(
        uuid.UUID(int=0x11), "summarize", "1.0.0", "sync", "Summarize a document",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        {"type": "string"}, False, False, False, ["nlp", "text"], None).hex()
    assert actual == "1b7afe0f1858394e4b4a21d4041fbf6267091901b4c7de80cbae946487d4c8fa"
    # Control: the hash is capable of CHANGING, so the equality above is not a constant
    # function. A hash-covered field moves it...
    moved = manifest.manifest_hash_for_fields(
        uuid.UUID(int=0x11), "summarize", "1.0.0", "sync", "Summarize a document",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        {"type": "string"}, False, False, True, ["nlp", "text"], None).hex()
    assert moved != actual
    # ...and tag ORDER does not, because the view sorts them.
    reordered = manifest.manifest_hash_for_fields(
        uuid.UUID(int=0x11), "summarize", "1.0.0", "sync", "Summarize a document",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        {"type": "string"}, False, False, False, ["text", "nlp"], None).hex()
    assert reordered == actual


def test_a_hash_covered_update_nulls_a_now_stale_signature(conn, client):
    """A signature that verifies a hash the update is about
    to invalidate must not be left looking valid for the new content."""
    peer = _seed_peer(conn, "stale")
    name = _skill_name("stale")
    sig = P.b64u(bytes(range(64)))
    assert _post(client, peer["peer_id"],
                 _declare_body(name, description="before", manifest_sig=sig,
                               signing_kid="kid-1"), BOTH).status_code == 201

    # CONTROL, asserted first: a purely COMMERCIAL change does not touch the hash, so
    # the signature survives. Without this, the null below could just mean "update
    # always nulls the signature".
    commercial = client.patch(f"/v1/peers/me/skills/{name}",
                              data=json.dumps({"share_mode": "private",
                                               "description": "before", "tags": [],
                                               "max_runtime_ms": 9000}),
                              content_type="application/json",
                              **_auth(peer["peer_id"], BOTH))
    assert commercial.status_code == 200, commercial.content
    assert commercial.json()["signing_kid"] == "kid-1"

    covered = client.patch(f"/v1/peers/me/skills/{name}",
                           data=json.dumps({"share_mode": "private",
                                            "description": "AFTER", "tags": []}),
                           content_type="application/json",
                           **_auth(peer["peer_id"], BOTH))
    assert covered.status_code == 200, covered.content
    assert covered.json()["signing_kid"] is None
    assert covered.json()["manifest_sig"] is None


# ================================================================== fetch one

def _grant(conn, *, grantor_owner, granter_peer, grantee_owner, skill_name,
           expires_at=None, revoked=False):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_grants (grantor_owner_id, grantee_owner_id, granter_peer_id, "
            "  skill_name, source, expires_at, revoked_at) "
            "VALUES (%s,%s,%s,%s,'direct',%s,%s)",
            (grantor_owner, grantee_owner, granter_peer, skill_name, expires_at,
             _dt.datetime.now(_dt.timezone.utc) if revoked else None))


def test_fetch_one_returns_the_current_skill_to_its_owner(conn, client):
    peer = _seed_peer(conn, "fetch")
    name = _skill_name("fetch")
    declared = _post(client, peer["peer_id"], _declare_body(name), BOTH).json()
    res = client.get(f"/v1/skills/{peer['peer_id']}/{name}", **_auth(peer["peer_id"], BOTH))
    assert res.status_code == 200, res.content
    assert res.json()["id"] == declared["id"]
    # Control: an unknown name on the same route is a 404, so the 200 above is a lookup
    # rather than an echo.
    assert client.get(f"/v1/skills/{peer['peer_id']}/nope",
                      **_auth(peer["peer_id"], BOTH)).status_code == 404


def test_fetch_one_hides_a_private_skill_from_another_peer(conn, client):
    owner = _seed_peer(conn, "vis-owner")
    viewer = _seed_peer(conn, "vis-viewer")
    private_name, public_name = _skill_name("priv"), _skill_name("pub")
    assert _post(client, owner["peer_id"],
                 _declare_body(private_name, share_mode="private"), BOTH).status_code == 201
    assert _post(client, owner["peer_id"],
                 _declare_body(public_name, share_mode="public"), BOTH).status_code == 201

    # CONTROL: the viewer CAN read the public skill from the same peer over the same
    # route with the same token. Without it, the 404 below could be a broken fixture.
    ok = client.get(f"/v1/skills/{owner['peer_id']}/{public_name}",
                    **_auth(viewer["peer_id"], BOTH))
    assert ok.status_code == 200, ok.content

    hidden = client.get(f"/v1/skills/{owner['peer_id']}/{private_name}",
                        **_auth(viewer["peer_id"], BOTH))
    assert hidden.status_code == 404, hidden.content
    # 404, NOT 403: a 403 would confirm the skill exists.
    assert hidden.json()["error"] == "E_NOT_FOUND"


def test_grants_only_visibility_follows_a_live_grant(conn, client):
    owner = _seed_peer(conn, "g-owner")
    viewer = _seed_peer(conn, "g-viewer")
    name = _skill_name("grant")
    assert _post(client, owner["peer_id"],
                 _declare_body(name, share_mode="grants_only"), BOTH).status_code == 201

    url = f"/v1/skills/{owner['peer_id']}/{name}"
    before = client.get(url, **_auth(viewer["peer_id"], BOTH))
    assert before.status_code == 404, "a non-grantee must not learn the skill exists"

    _grant(conn, grantor_owner=owner["owner_id"], granter_peer=owner["peer_id"],
           grantee_owner=viewer["owner_id"], skill_name=name)
    after = client.get(url, **_auth(viewer["peer_id"], BOTH))
    assert after.status_code == 200, after.content          # the control for the 404

    # An EXPIRED-but-unrevoked grant must not keep it visible: the catalog gate has to
    # match what authorize() would actually permit.
    expired_viewer = _seed_peer(conn, "g-expired")
    _grant(conn, grantor_owner=owner["owner_id"], granter_peer=owner["peer_id"],
           grantee_owner=expired_viewer["owner_id"], skill_name=name,
           expires_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1))
    assert client.get(url, **_auth(expired_viewer["peer_id"], BOTH)).status_code == 404

    # ...and so must a revoked one. Both share the live-grant predicate, so both are
    # asserted rather than one standing in for the other.
    revoked_viewer = _seed_peer(conn, "g-revoked")
    _grant(conn, grantor_owner=owner["owner_id"], granter_peer=owner["peer_id"],
           grantee_owner=revoked_viewer["owner_id"], skill_name=name, revoked=True)
    assert client.get(url, **_auth(revoked_viewer["peer_id"], BOTH)).status_code == 404


def test_fetch_one_rejects_a_malformed_peer_id_as_400(conn, client):
    peer = _seed_peer(conn, "badid")
    bad = client.get("/v1/skills/not-a-uuid/whatever", **_auth(peer["peer_id"], BOTH))
    assert bad.status_code == 400
    assert bad.json()["error"] == "E_VALIDATION"
    # Control: a WELL-FORMED but unknown peer id on the same route is a 404, so the 400
    # is about the id's form and not about the route rejecting everything.
    assert client.get(f"/v1/skills/{uuid.uuid4()}/whatever",
                      **_auth(peer["peer_id"], BOTH)).status_code == 404


# ====================================================== listing and typed absence

def test_listing_a_peer_with_no_skills_is_an_empty_array_not_an_error(conn, client):
    peer = _seed_peer(conn, "list")
    empty = client.get("/v1/peers/me/skills", **_auth(peer["peer_id"], BOTH))
    assert empty.status_code == 200 and empty.json() == []
    # Control: the same route returns the skill once one exists, so `[]` above means
    # "no rows" rather than "this route always answers []".
    name = _skill_name("list")
    assert _post(client, peer["peer_id"], _declare_body(name), BOTH).status_code == 201
    assert [s["name"] for s in
            client.get("/v1/peers/me/skills", **_auth(peer["peer_id"], BOTH)).json()] == [name]


def test_the_repository_reports_absence_as_a_typed_reason_not_an_empty_collection(conn):
    """House rule 3, asserted on the repository itself.

    An RLS-scoped read with no context returns zero rows and is indistinguishable from
    "no data"; a bare `[]` or `None` from a repository erases the difference before any
    caller can act on it. These reads return `Absence` singletons that the service layer
    turns into 404 / 409 / `[]` deliberately.
    """
    peer = _seed_peer(conn, "typed")
    assert repository.find_current_by_name(conn, peer["peer_id"], "nope") \
        is repository.NOT_FOUND
    assert repository.list_for_peer(conn, peer["peer_id"]) is repository.EMPTY
    assert repository.peer_owner_id(conn, uuid.uuid4()) is repository.NOT_FOUND
    # The reasons are distinct objects, so a caller cannot conflate them by accident...
    assert repository.NOT_FOUND is not repository.EMPTY
    # ...and `Absence` is deliberately NOT falsy: `if row:` must not compile into a
    # silent conflation of EMPTY with NOT_FOUND.
    assert bool(repository.NOT_FOUND) is True

    # Control: a POPULATED read returns real rows, so the singletons above are absence
    # and not the only thing this repository can produce.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO skills (peer_id, name, version, mode, share_mode, "
                    "input_schema) VALUES (%s,'typed-probe','1.0.0','sync','private','{}')",
                    (str(peer["peer_id"]),))
    found = repository.find_current_by_name(conn, peer["peer_id"], "typed-probe")
    assert isinstance(found, dict) and found["version"] == "1.0.0"
    assert isinstance(repository.list_for_peer(conn, peer["peer_id"]), list)


def test_no_route_in_this_app_issues_a_full_table_or_unbounded_aggregate():
    """House rule 4, as a static check over this app's SQL.

    A COUNT(*) over a whole call chain on every authorize can consume about 490 GB/day of disk
    reads,
    and the Django-shaped version of it -
    `.count()` on a related manager - is easy to write and hard to see. Every statement
    in `repository.py` is keyed on `peer_id`; this asserts there is no aggregate at all
    and that every SELECT/UPDATE carries a WHERE.
    """
    import ast
    # EVERY module of the app, not only repository.py (review queue: a statement written
    # in views.py or service.py was invisible to this scan). One synthetic Module holds
    # all their bodies, so the walks below are unchanged.
    pkg = Path(__file__).resolve().parent.parent / "apps/skillsreg"
    modules = sorted(pkg.glob("*.py"))
    tree = ast.Module(body=[], type_ignores=[])
    for module in modules:
        tree.body.extend(ast.parse(module.read_text(encoding="utf-8")).body)
    assert len(modules) >= 9, f"control failed: only {len(modules)} modules were read"
    # STRING LITERALS from the AST, not a regex over the file: a regex matches the
    # word "SELECT" inside a docstring and then reports prose as an unbounded query,
    # which is a scanner that fails on the wrong thing rather than a passing check.
    # Docstrings are excluded by walking Constant nodes and dropping any that is the
    # first statement of a module/class/function body.
    docstrings = {id(ast.get_docstring(n, clean=False)) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))}
    # An f-string is a JoinedStr whose Constant parts are separate nodes, so a naive
    # walk would see `f"SELECT {_COLS} FROM skills WHERE ..."` as the fragment
    # "SELECT " and report it as unbounded. The parts are rejoined, with each
    # interpolation standing in as a placeholder.
    in_fstring, literals = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    in_fstring.add(id(piece))
                    parts.append(piece.value)
                else:
                    parts.append(" <interpolated> ")
            literals.append("".join(parts))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in in_fstring and id(node.value) not in docstrings):
            literals.append(node.value)
    statements = [s for s in literals
                  if s.lstrip().upper().startswith(("SELECT ", "UPDATE ", "INSERT "))]
    assert len(statements) >= 6, (
        f"control failed: only {len(statements)} SQL statements were extracted, so a "
        "pass here would mean the scanner found nothing rather than nothing bad")
    for stmt in statements:
        assert "count(" not in stmt.lower(), f"aggregate in a request path: {stmt[:90]}"
        upper = stmt.upper()
        # A SELECT that reads a table needs a WHERE; one with no FROM (`SELECT now()`) reads
        # nothing. Every UPDATE needs one.
        if upper.lstrip().startswith("UPDATE") or (upper.lstrip().startswith("SELECT")
                                                    and " FROM " in upper):
            assert " WHERE " in upper, f"unbounded statement: {stmt[:90]}"


def test_this_app_contains_no_bare_except_that_swallows_absence():
    """House rule 3's other half. A `except: return []` turns a failure into data.

    The control is that the scan can FAIL: a planted snippet with the forbidden shape
    is checked to trip the same matcher.
    """
    import ast

    def offenders(source: str) -> list[str]:
        """Bare `except:`, or a handler whose whole body is `return []/{}/None`.

        Parsed, not grepped. A regex over the text matches the phrase `except:` inside
        this very docstring and inside repository.py's, which is a scanner that fails on
        prose rather than on code.
        """
        found = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                found.append(f"bare except at line {node.lineno}")
                continue
            # ANY return of an absence value directly in the handler, not only a handler
            # whose whole body is that return: `log.warning(...); return []` swallows the
            # failure just the same (review queue: the one-line-only match was vacuous).
            # Nested blocks too (an `if` inside the handler), but not into a function defined
            # there, whose return is its own (kxxx's review of #19).
            stack = list(node.body)
            while stack:
                stmt = stack.pop()
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                if not isinstance(stmt, ast.Return):
                    stack.extend(ast.iter_child_nodes(stmt))
                    continue
                value = stmt.value
                swallows = (value is None
                            or (isinstance(value, ast.Constant) and value.value is None)
                            or (isinstance(value, (ast.List, ast.Dict))
                                and not getattr(value, "elts", None)
                                and not getattr(value, "keys", None)))
                if swallows:
                    found.append(f"absence-swallowing handler at line {node.lineno}")
                    break
        return found

    pkg = Path(__file__).resolve().parent.parent / "apps/skillsreg"
    scanned = 0
    for path in sorted(pkg.glob("*.py")):
        assert offenders(path.read_text(encoding="utf-8")) == [], path.name
        scanned += 1
    assert scanned >= 9, f"control failed: only {scanned} modules were scanned"
    # The control that matters: the scanner FIRES on the shape it forbids. Without it,
    # a scanner that silently returned [] would pass this whole test.
    assert offenders("try:\n    x()\nexcept Exception:\n    return []\n")
    # ...and the same shape behind a log line, which the old one-line match missed.
    assert offenders("try:\n    x()\nexcept Exception:\n    log.warning('x')\n    return []\n")
    # ...and nested inside an `if` in the handler; a nested def's own return is not one.
    assert offenders("try:\n    x()\nexcept Exception:\n    if y:\n        return []\n")
    assert not offenders("try:\n    x()\nexcept Exception:\n    def f():\n        return []\n    raise\n")
    assert offenders("try:\n    x()\nexcept:\n    pass\n")


def test_the_sync_route_keeps_static_segment_precedence(conn, client):
    """`/v1/peers/me/skills/sync` is a POST-only route and a static segment beats the
    `{name}` capture, so PATCH there is a 405, never an update of a skill named "sync".
    Without the route, that URL would reach `skill_detail` and mutate a row.
    """
    peer = _seed_peer(conn, "sync")
    assert _post(client, peer["peer_id"], _declare_body("sync"), BOTH).status_code in (201, 409)
    patched = client.patch("/v1/peers/me/skills/sync",
                           data=json.dumps({"share_mode": "public", "tags": []}),
                           content_type="application/json", **_auth(peer["peer_id"], BOTH))
    assert patched.status_code == 405
    assert patched.headers["Allow"] == "POST"
    # Control: the SAME method against a real skill name is served, so the 405 is the
    # route's and not "PATCH is broken".
    name = _skill_name("sync2")
    assert _post(client, peer["peer_id"], _declare_body(name), BOTH).status_code == 201
    assert client.patch(f"/v1/peers/me/skills/{name}",
                        data=json.dumps({"share_mode": "private", "tags": []}),
                        content_type="application/json",
                        **_auth(peer["peer_id"], BOTH)).status_code == 200


# ========================================================= input_schema bounds
#
# `apps/skillsreg/schema_validate.py` had NO tests at all: its three guards could each
# be deleted with this suite green. They are reached over HTTP - `views._value` passes
# `input_schema` through as arbitrary JSON (views.py:222-225, :264), and
# `service.create_skill` runs `_schema_gate` on it (service.py:120, and :174 for
# release). So these are ROUTE tests, not unit tests: the guard existing is a different
# claim from the route enforcing it, and a unit test proves only the first.
#
# The gate ENFORCES by default (divergence 010): only `0`, `false`, `off`, `no` or
# `observe` in `MESHYCHAT_SKILL_SCHEMA_ENFORCE` relax it (`service.SCHEMA_ENFORCE_RELAXING_VALUES`),
# so setting it to `1` below only states what these tests rely on. The env var is read at
# CALL time, so both arms fit in one run.


def _nested_schema(levels: int):
    """`levels` nested objects with an EMPTY leaf.

    The leaf is `{}` on purpose. `_walk` recurses into every value including scalars
    (schema_validate.py:82-83), so a `{"type": "object"}` leaf would sit one level
    deeper than the object count suggests and put the boundary in a confusing place.
    With an empty leaf the depth IS the nesting count.
    """
    schema = {}
    for _ in range(levels):
        schema = {"properties": schema}
    return schema


def _schema_of_exactly(total_bytes: int):
    """A schema whose compact-JSON encoding is exactly `total_bytes` bytes.

    The cap measures `json.dumps(schema, separators=(",", ":")).encode("utf-8")`
    (schema_validate.py:92), so the padding must be ASCII for one char to be one byte.
    """
    schema = {"type": "object", "description": ""}
    pad = total_bytes - len(json.dumps(schema, separators=(",", ":")).encode("utf-8"))
    assert pad >= 0, "control failed: the empty frame is already over the target"
    schema["description"] = "x" * pad
    encoded = len(json.dumps(schema, separators=(",", ":")).encode("utf-8"))
    assert encoded == total_bytes, f"control failed: built {encoded}, wanted {total_bytes}"
    return schema


def test_an_external_ref_in_a_declared_input_schema_is_refused(conn, client, monkeypatch):
    """schema_validate.py:76-79 - `$ref` must start with `#`. This is the SSRF guard.

    A JSON Schema `$ref` is a fetch instruction to anything that resolves it. Let a peer
    put `http://169.254.169.254/...` in a declared schema and every downstream resolver -
    a validator, a form generator, a doc renderer - becomes a request the peer aimed.
    The guard refuses the ref at WRITE time, so the hostile string never reaches storage
    and no later consumer has to be trusted to be careful.

    """
    peer = _seed_peer(conn, "ref")
    monkeypatch.setenv("MESHYCHAT_SKILL_SCHEMA_ENFORCE", "1")

    # The POSITIVE CONTROL FIRST: the identical schema shape with a LEGAL internal ref
    # is accepted, so the refusals below are about where the ref points and not about
    # `$ref`, `$defs` or nested properties being rejected wholesale.
    legal = {"type": "object", "$defs": {"thing": {"type": "string"}},
             "properties": {"a": {"$ref": "#/$defs/thing"}}}
    ok = _post(client, peer["peer_id"], _declare_body(_skill_name("ref-ok"),
                                                      input_schema=legal))
    assert ok.status_code == 201, ok.content
    assert ok.json()["input_schema"] == legal
    # ...and so is the same schema with no ref at all.
    assert _post(client, peer["peer_id"], _declare_body(
        _skill_name("ref-none"),
        input_schema={"type": "object", "properties": {"a": {"type": "string"}}},
    )).status_code == 201

    # Now the negatives. An off-host URL, and the cloud metadata address that makes
    # server-side fetch a credential disclosure rather than a curiosity.
    for hostile in ("https://evil.example.test/schema.json",
                    "http://169.254.169.254/latest/meta-data/iam/security-credentials/"):
        res = _post(client, peer["peer_id"], _declare_body(
            _skill_name("ref-bad"),
            input_schema={"type": "object", "properties": {"a": {"$ref": hostile}}},
        ))
        assert res.status_code == 400, (hostile, res.content)
        assert res.json()["message"] == (
            "skill schema invalid: external $ref not allowed "
            f"(only internal '#...' refs): {hostile}")

    # A ref nested BELOW the top level is caught too - the guard is in the walk, not in
    # a top-level-key check, so burying it does not smuggle it past.
    buried = _post(client, peer["peer_id"], _declare_body(
        _skill_name("ref-deep"),
        input_schema={"type": "object", "properties": {"a": {"items": {"anyOf": [
            {"$ref": "https://evil.example.test/deep.json"}]}}}},
    ))
    assert buried.status_code == 400, buried.content
    assert "external $ref not allowed" in buried.json()["message"]

    # THE GUARD MUST NOT FETCH TO DECIDE. `validate_skill_schema` is pure, so it can be
    # called with sockets taken away entirely: if it refuses the ref with sockets dead,
    # the refusal was a string comparison and the validator is not itself an SSRF
    # primitive. (Asserted at unit level deliberately - the route needs its database
    # connection, so this check could not be written around a request.)
    import socket as _socket

    def _no_sockets(*a, **kw):
        raise AssertionError("schema validation attempted a network connection")

    monkeypatch.setattr(_socket, "socket", _no_sockets)
    monkeypatch.setattr(_socket, "create_connection", _no_sockets)
    with pytest.raises(schema_validate.SchemaValidateError):
        schema_validate.validate_skill_schema(
            {"$ref": "https://evil.example.test/schema.json"})
    # Control for the socket trap itself: it fires when something DOES connect. Without
    # this, a trap installed on the wrong name would make the line above meaningless.
    with pytest.raises(AssertionError):
        _socket.socket()

    # THE DEFAULT IS NOW ENFORCE - divergence 010, operator decision. This arm used to
    # assert the opposite: with the variable ABSENT the same hostile ref was accepted 201
    # and stored. That was the shipped default and it made the SSRF control decorative.
    monkeypatch.undo()          # puts the sockets back, and the env var with them
    monkeypatch.delenv("MESHYCHAT_SKILL_SCHEMA_ENFORCE", raising=False)
    absent = _post(client, peer["peer_id"], _declare_body(
        _skill_name("ref-default"),
        input_schema={"type": "object", "properties": {
            "a": {"$ref": "https://evil.example.test/schema.json"}}},
    ))
    assert absent.status_code == 400, (
        "with no flag set the hostile $ref must now be REFUSED: enforcement is the "
        f"default (divergence 010), got {absent.status_code}")

    # A TYPO MUST FAIL CLOSED. "flase" is not a relaxing value, so it enforces - the only
    # way to disable a security control should be to spell the disable correctly.
    monkeypatch.setenv("MESHYCHAT_SKILL_SCHEMA_ENFORCE", "flase")
    typo = _post(client, peer["peer_id"], _declare_body(
        _skill_name("ref-typo"),
        input_schema={"type": "object", "properties": {
            "a": {"$ref": "https://evil.example.test/schema.json"}}},
    ))
    assert typo.status_code == 400, "a misspelt flag value relaxed enforcement"

    # AND THE RELAX ARM STILL WORKS, explicitly. A gate with no documented way past it is a
    # gate somebody deletes; this is the supported escape and it is spelled out.
    monkeypatch.setenv("MESHYCHAT_SKILL_SCHEMA_ENFORCE", "observe")
    relaxed = _post(client, peer["peer_id"], _declare_body(
        _skill_name("ref-relaxed"),
        input_schema={"type": "object", "properties": {
            "a": {"$ref": "https://evil.example.test/schema.json"}}},
    ))
    assert relaxed.status_code == 201, relaxed.content


def test_the_schema_nesting_depth_cap_holds_at_32_in_both_directions(conn, client,
                                                                     monkeypatch):
    """schema_validate.py:73-74 - `depth > MAX_SCHEMA_DEPTH` (:22, 32). A DoS bound.

    `_walk` is unbounded recursion over peer-supplied JSON. Without the cap a schema
    nested a few thousand deep is a RecursionError inside a request handler, and one
    small body takes the worker down; measured here, the accepted side is 32 and the
    refused side is 33, so the assertion below pins the EDGE. A one-sided test would
    also pass against a guard that had been changed to refuse everything.

    """
    peer = _seed_peer(conn, "depth")
    monkeypatch.setenv("MESHYCHAT_SKILL_SCHEMA_ENFORCE", "1")

    inside = _post(client, peer["peer_id"], _declare_body(
        _skill_name("depth-32"), input_schema=_nested_schema(32)))
    assert inside.status_code == 201, inside.content

    outside = _post(client, peer["peer_id"], _declare_body(
        _skill_name("depth-33"), input_schema=_nested_schema(33)))
    assert outside.status_code == 400, outside.content
    assert outside.json()["message"] == "skill schema invalid: schema nesting exceeds depth 32"

    # The cap covers `output_schema` too (schema_validate.py:112-113): a bound that only
    # watched one of the two fields would be trivially stepped around.
    out = _post(client, peer["peer_id"], _declare_body(
        _skill_name("depth-out"), input_schema={"type": "object"},
        output_schema=_nested_schema(33)))
    assert out.status_code == 400, out.content
    assert "nesting exceeds depth 32" in out.json()["message"]
    # Control for that pair: a 32-deep OUTPUT schema is accepted, so the 400 is the
    # depth and not "declaring an output_schema at all is broken".
    assert _post(client, peer["peer_id"], _declare_body(
        _skill_name("depth-out-ok"), input_schema={"type": "object"},
        output_schema=_nested_schema(32))).status_code == 201


def test_the_64_kib_schema_cap_holds_at_the_byte_in_both_directions(conn, client,
                                                                    monkeypatch):
    """schema_validate.py:97-98 - `length > MAX_SCHEMA_BYTES` (:20, 65536). A DoS bound.

    It is checked FIRST, before the walk (schema_validate.py:91), which is the whole
    point: it bounds the work every later guard has to do on hostile input rather than
    discovering the size after paying for the traversal. Measured: 65536 bytes is
    accepted, 65537 is refused - so this asserts the exact byte either side, not a
    comfortable round number well clear of the edge.

    """
    peer = _seed_peer(conn, "bytes")
    monkeypatch.setenv("MESHYCHAT_SKILL_SCHEMA_ENFORCE", "1")

    at_limit = _post(client, peer["peer_id"], _declare_body(
        _skill_name("bytes-in"), input_schema=_schema_of_exactly(65536)))
    assert at_limit.status_code == 201, at_limit.content

    over = _post(client, peer["peer_id"], _declare_body(
        _skill_name("bytes-out"), input_schema=_schema_of_exactly(65537)))
    assert over.status_code == 400, over.content
    assert over.json()["message"] == (
        "skill schema invalid: schema exceeds 65536 bytes (got 65537)")


# ================================================= the sync route's own authentication
#
# This route is registered so static-segment precedence is preserved and answers 501 because
# the manifest diff subsystem is not implemented. That is not a reason to allow
# anonymous calls: `_route` authenticates and the handler checks `skills:declare` before its
# 501 answer, so an anonymous POST is refused first.


def test_the_sync_route_authenticates_before_it_answers_501(conn, client):
    """An unimplemented route must still refuse an anonymous caller.

    The route previously bypassed `_route` entirely, so it never opened a connection
    and never called `authenticate_peer`: an anonymous POST was told 501, which
    discloses that the path exists.
    """
    peer = _seed_peer(conn, "syncauth")

    anon = client.post("/v1/peers/me/skills/sync", data="[]",
                       content_type="application/json")
    assert anon.status_code == 401, anon.content
    assert anon.json()["error"] == "E_AUTH_MISSING"

    # The route requires `skills:declare` before anything else it does.
    no_scope = client.post("/v1/peers/me/skills/sync", data="[]",
                           content_type="application/json",
                           **_auth(peer["peer_id"], [PUBLISH]))
    assert no_scope.status_code == 403, no_scope.content
    assert no_scope.json()["error"] == "E_SCOPE_REQUIRED"

    # CONTROL, and it is the half that makes the two refusals mean anything: an
    # authenticated peer holding the scope reaches the route's OWN answer. Without it a
    # 401 from a route that had been deleted, misspelled or 404'd would read identically.
    served = client.post("/v1/peers/me/skills/sync", data="[]",
                         content_type="application/json",
                         **_auth(peer["peer_id"], BOTH))
    assert served.status_code == 501, served.content
    assert served.json()["error"] == "E_INTERNAL"
    assert served.json()["message"] == "manifest sync is not implemented by this server"

    # The method gate still runs before authentication: a wrong method is rejected by
    # URL routing and never reaches authentication.
    assert client.get("/v1/peers/me/skills/sync").status_code == 405


# ============================================ re-release onto a soft-deleted version
#
# `soft_delete` stamps `deleted_at` on EVERY version of a name, so the collision cannot
# be reached by deleting and re-releasing the same name in one step - the live-current
# lookup 404s first. The reachable sequence is: publish a high version, soft-delete the
# whole name, re-declare it at a LOWER version (which is legal - the unique key is
# (peer, name, version) and that version is fresh), then release the high version again.
# The live-current guard cannot see the soft-deleted row; `skills_peer_id_name_version_key`
# counts it.


def _release(client, peer_id, name, version, scopes=BOTH, **over):
    return client.post(f"/v1/peers/me/skills/{name}/versions",
                       data=json.dumps(_declare_body(name, version, **over)),
                       content_type="application/json", **_auth(peer_id, scopes))


def test_re_releasing_a_soft_deleted_version_is_409_not_500_and_keeps_the_current_row(
        conn, client):
    """Two findings in one sequence, because they share the one failing statement.

    (a) the 23505 out of `repository.insert` inside `release_version` had no handler -
        `create_skill` translates exactly this constraint to a 409 and the release path
        did not, so a client re-releasing a soft-deleted version got a 500;
    (b) the flip and the insert must share one transaction. The
        existing atomicity test induces its failure in the VERSION CHECK, which runs
        BEFORE `conn.transaction()` opens - so it passes even if the flip and the
        insert were committed separately. This one fails INSIDE the transaction, after
        `flip_previous_current` has already run, which is the only place the shared
        transaction is load-bearing.
    """
    peer = _seed_peer(conn, "resoft")
    name = _skill_name("resoft")

    assert _post(client, peer["peer_id"], _declare_body(name, "1.0.0")).status_code == 201
    assert _release(client, peer["peer_id"], name, "5.0.0").status_code == 201
    assert client.delete(f"/v1/peers/me/skills/{name}",
                         **_auth(peer["peer_id"], BOTH)).status_code == 204
    # Re-declared at a version that is BELOW the soft-deleted 5.0.0, so the release
    # below passes the newer-than check and still collides on the unique key.
    assert _post(client, peer["peer_id"], _declare_body(name, "2.0.0")).status_code == 201

    clash = _release(client, peer["peer_id"], name, "5.0.0")
    assert clash.status_code == 409, clash.content
    assert clash.json()["error"] == "E_CONFLICT"

    # (b): the flip must have rolled back with the insert. Exactly one live row holds
    # is_current, and it is still 2.0.0 - not zero rows, which is the wedge.
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM skills WHERE peer_id = %s AND name = %s "
                    "AND is_current = TRUE AND deleted_at IS NULL",
                    (str(peer["peer_id"]), name))
        live_current = [r[0] for r in cur.fetchall()]
    assert live_current == ["2.0.0"], live_current

    # CONTROL: a version that collides with nothing still releases, so the 409 is the
    # unique constraint and not "this skill can no longer be released at all" - which is
    # precisely what the wedge would look like from the outside.
    assert _release(client, peer["peer_id"], name, "6.0.0").status_code == 201


def test_releasing_the_version_that_is_already_current_is_409(conn, client):
    """`new_parsed <= current_parsed` - the comparison is NOT strict, and must not be.

    An equal version is refused. Changing the comparison to `<` would let a peer re-release
    the version that is already
    current: the flip would clear is_current on the live row and the insert would trip
    the same unique key, leaving the 500 and the wedge both. The equal case was the one
    boundary the suite never asserted - the existing test releases a STRICTLY LOWER
    version, which a `<` would refuse too.
    """
    peer = _seed_peer(conn, "eqver")
    name = _skill_name("eqver")
    assert _post(client, peer["peer_id"], _declare_body(name, "2.0.0")).status_code == 201

    same = _release(client, peer["peer_id"], name, "2.0.0")
    assert same.status_code == 409, same.content
    assert same.json()["error"] == "E_CONFLICT"
    # THE MESSAGE IS THE DISCRIMINATOR HERE, not the status - measured, not assumed.
    # Under `<`, the equal version passes this gate, the flip runs, and the insert
    # collides with the live current row's own (peer, name, version); the unique-key
    # handler added below then answers 409 as well. Status and code alone are identical
    # between "refused by the version rule" and "refused by the database after a flip
    # that had to be rolled back", so only the message tells the two apart.
    assert same.json()["message"].endswith("current version is 2.0.0")
    assert "already exists" not in same.json()["message"]

    # CONTROL: the smallest possible increment over that same version is accepted, so
    # the 409 is the equality and not "releasing over 2.0.0 is broken".
    assert _release(client, peer["peer_id"], name, "2.0.1").status_code == 201


def test_the_per_peer_list_never_returns_a_soft_deleted_skill(conn, client):
    """`GET /v1/peers/me/skills` calls `list_for_peer(include_deleted=false)`.

    A soft-deleted skill keeps `is_current = TRUE` - `soft_delete` only stamps
    `deleted_at` - so the view's `if s["is_current"]` filter does NOT hide it. The
    `deleted_at IS NULL` half of the repository predicate is the only thing that does.
    """
    peer = _seed_peer(conn, "leak")
    gone, kept = _skill_name("leak-gone"), _skill_name("leak-kept")
    assert _post(client, peer["peer_id"], _declare_body(gone)).status_code == 201
    assert _post(client, peer["peer_id"], _declare_body(kept)).status_code == 201
    assert client.delete(f"/v1/peers/me/skills/{gone}",
                         **_auth(peer["peer_id"], BOTH)).status_code == 204

    listed = client.get("/v1/peers/me/skills", **_auth(peer["peer_id"], BOTH))
    assert listed.status_code == 200, listed.content
    names = [s["name"] for s in listed.json()]
    # CONTROL first: the surviving skill IS listed, so an absent `gone` means "filtered"
    # rather than "this list is empty" or "the delete removed both".
    assert kept in names
    assert gone not in names, names

    # The row is still THERE - this is a soft delete, and the filter is what hides it.
    # Without this, a hard delete would pass the assertion above for the wrong reason.
    with conn.cursor() as cur:
        cur.execute("SELECT deleted_at IS NOT NULL, is_current FROM skills "
                    "WHERE peer_id = %s AND name = %s", (str(peer["peer_id"]), gone))
        assert cur.fetchall() == [(True, True)]
    all_versions = repository.list_for_peer(conn, peer["peer_id"], include_deleted=True)
    assert gone in [s["name"] for s in all_versions]


def test_error_bodies_are_compact_json_exactly_as_success_bodies_are(conn, client):
    """`views._json` emits no spaces; `JsonResponse` defaults to `", "` / `": "`.

    Every success body in this app goes through `views._json`, which sets compact
    separators, and every error body went through `JsonResponse` and did not - so the
    two halves of the same wire disagreed byte for byte, in a module whose docstrings
    argue byte-compatibility is the contract. `apps/core/jsonerrors.json_500` already
    passes `json_dumps_params`; this matches it.
    """
    peer = _seed_peer(conn, "compact")

    anon = client.post("/v1/peers/me/skills", data="{}",
                       content_type="application/json")
    assert anon.status_code == 401
    assert anon.content == json.dumps({"error": "E_AUTH_MISSING", "message": "missing auth"},
                                      separators=(",", ":")).encode()
    assert b", " not in anon.content and b'": ' not in anon.content

    # A second error, from a different constructor and with a longer message, so this
    # pins the RESPONSE helper rather than one short body that happens to have no
    # separator-sized gap in it.
    no_scope = _post(client, peer["peer_id"], _declare_body(_skill_name("compact")), [])
    assert no_scope.status_code == 403
    assert no_scope.content == json.dumps(no_scope.json(), separators=(",", ":")).encode()

    # CONTROL: a SUCCESS body on the same route is compact too, which is the property
    # the errors are being held to. If this ever stops being true the assertions above
    # are pinning the wrong shape.
    ok = client.get("/v1/peers/me/skills", **_auth(peer["peer_id"], BOTH))
    assert ok.status_code == 200
    assert ok.content == json.dumps(ok.json(), separators=(",", ":")).encode()


# ================================================== B09 the closed arms of two flags
#
# Until B09 every test here ran these two flags OFF (unset), so the arm that refuses had
# never been executed by a test. Operator decision 52 (2026-09-27): both stay off at
# cutover; after cutover vocabulary enforcement is flipped first, then attestation. These
# tests are what makes either flip a measured change.

_ATTEST_ENV = "MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE"
_VOCAB_ENV = "MESHYCHAT_RUNTIME_VOCAB_ENFORCE"
_PAIR = {"manifest_sig": P.b64u(bytes(range(64))), "signing_kid": "kid-1"}


def _attestation(monkeypatch, on: bool):
    """The setting settings.py parses once at boot. The environment carries the same value
    only so the unchanged code (which read it per call) is measured under the same flag."""
    if on:
        monkeypatch.setenv(_ATTEST_ENV, "true")
    else:
        monkeypatch.delenv(_ATTEST_ENV, raising=False)
    return override_settings(MESHY_MANIFEST_ATTESTATION_ENFORCE=on)


def _patch_skill(client, peer_id, name, body):
    return client.patch(f"/v1/peers/me/skills/{name}", data=json.dumps(body),
                        content_type="application/json", **_auth(peer_id, BOTH))


_REQUIRED = {"error": "E_VALIDATION", "message": "manifest attestation required"}


def test_b09_attestation_on_the_publish_gate_refuses_an_absent_pair(conn, client, monkeypatch):
    """With the flag on, no pair is 400 `manifest attestation required`, and a pair
    passes. The publish gate decodes the pair; verification is covered by the attestation-pin
    tests, so a well-formed pair is enough here."""
    peer = _seed_peer(conn, "b09-gate")
    pid = peer["peer_id"]
    # CONTROL, the open arm: without the flag an absent pair declares.
    with _attestation(monkeypatch, False):
        assert _post(client, pid, _declare_body(_skill_name("off"))).status_code == 201

    with _attestation(monkeypatch, True):
        # DECLARE
        name = _skill_name("on")
        bare = _post(client, pid, _declare_body(name))
        assert (bare.status_code, bare.json()) == (400, _REQUIRED), bare.content
        signed = _post(client, pid, _declare_body(name, description="v1", **_PAIR))
        assert signed.status_code == 201, signed.content

        # RELEASE
        bare = _release(client, pid, name, "1.1.0")
        assert (bare.status_code, bare.json()) == (400, _REQUIRED), bare.content
        signed = _release(client, pid, name, "1.1.0", description="v1", **_PAIR)
        assert signed.status_code == 201, signed.content

        # METADATA UPDATE. A change the manifest hash does not cover needs no pair (the
        # CONTROL that the gate is conditional on the hash, not on every PATCH)...
        commercial = _patch_skill(client, pid, name, {"share_mode": "private",
                                                      "description": "v1", "tags": [],
                                                      "max_runtime_ms": 9000})
        assert commercial.status_code == 200, commercial.content
        # ...a manifest-changing one without a pair is refused, and nothing is written...
        covered = {"share_mode": "private", "description": "v2", "tags": []}
        bare = _patch_skill(client, pid, name, covered)
        assert (bare.status_code, bare.json()) == (400, _REQUIRED), bare.content
        with conn.cursor() as cur:
            cur.execute("SELECT description FROM skills WHERE peer_id = %s AND name = %s "
                        "AND is_current", (str(pid), name))
            assert cur.fetchone()[0] == "v1", "a refused update was written"
        # ...and the same change with a pair is applied.
        signed = _patch_skill(client, pid, name, {**covered, **_PAIR})
        assert signed.status_code == 200 and signed.json()["description"] == "v2", \
            signed.content


def _profile_declare(client, pid, harness):
    return _post(client, pid, _declare_body(_skill_name("vocab"),
                                            runtime_profile={"harness": harness}))


@pytest.mark.parametrize("raw, enforced", [
    (None, False), ("1", True), ("true", True), ("TRUE", True),
    # The vocab-enforce flag parser reads exactly `1|true|TRUE`: any other spelling is off.
    ("yes", False), ("True", False), ("0", False),
])
def test_b09_vocab_enforcement_refuses_an_unknown_slug_and_a_malformed_one_either_way(
        conn, client, monkeypatch, raw, enforced):
    """A malformed slug is refused whatever the flag says; a
    well-formed UNKNOWN slug is refused only under MESHYCHAT_RUNTIME_VOCAB_ENFORCE, whose parser
    is read per call and is deliberately not the attestation flag's."""
    if raw is None:
        monkeypatch.delenv(_VOCAB_ENV, raising=False)
    else:
        monkeypatch.setenv(_VOCAB_ENV, raw)
    pid = _seed_peer(conn, "b09-vocab")["peer_id"]
    # CONTROLS: a well-known and a custom: slug are accepted in either arm.
    for harness in ("codex", "custom:my_harness"):
        ok = _profile_declare(client, pid, harness)
        assert ok.status_code == 201, (raw, harness, ok.content)

    unknown = _profile_declare(client, pid, "unregistered_harness")
    if enforced:
        assert (unknown.status_code, unknown.json()) == (400, {
            "error": "E_VALIDATION",
            "message": "unknown harness slug (set MESHYCHAT_RUNTIME_VOCAB_ENFORCE=0 to soft-allow): "
                       "unregistered_harness"}), (raw, unknown.content)
    else:
        assert unknown.status_code == 201, (raw, unknown.content)

    malformed = _profile_declare(client, pid, "Kimi Code")
    assert malformed.status_code == 400, (raw, malformed.content)
    assert malformed.json()["message"] == (
        "runtime vocabulary invalid: harness: malformed slug "
        "(want lowercase [a-z0-9_]+ or custom:<slug>)"), (raw, malformed.content)



# ------------------------------------ B09 flip prep: the grammar's `$` and a final newline
#
# Python's `$` also matches just before a final "\n", so `.match` let `custom:x\n` through as a
# custom slug and `policy:x\n` as a tag in EITHER arm, and let `unregistered_harness\n` through
# as a well-formed unknown slug (soft-warn, stored) with the flag off. All three must be 400
# "malformed"; each newline form is refused and each control is accepted. Found preparing the
# vocabulary flip (ASDLC OI-32, 2026-09-27).

_MALFORMED_SLUG = ("runtime vocabulary invalid: harness: malformed slug "
                   "(want lowercase [a-z0-9_]+ or custom:<slug>)")
_MALFORMED_TAG = ("runtime vocabulary invalid: policy_tags: malformed tag "
                  "(want namespace:slug, e.g. policy:cost_budget)")


@pytest.mark.parametrize("flag", [None, "1"], ids=["off", "on"])
@pytest.mark.parametrize("profile, control, message", [
    ({"harness": "unregistered_harness\n"}, {"harness": "unregistered_harness"},
     _MALFORMED_SLUG),
    ({"harness": "custom:my_harness\n"}, {"harness": "custom:my_harness"}, _MALFORMED_SLUG),
    ({"harness": "codex\n"}, {"harness": "codex"}, _MALFORMED_SLUG),
    ({"policy_tags": ["policy:cost_budget\n"]}, {"policy_tags": ["policy:cost_budget"]},
     _MALFORMED_TAG),
], ids=["unknown-slug", "custom-slug", "well-known-slug", "policy-tag"])
def test_b09_vocab_grammar_refuses_a_final_newline_in_either_arm(
        conn, client, monkeypatch, flag, profile, control, message):
    """A slug or tag with a final newline is malformed whatever
    MESHYCHAT_RUNTIME_VOCAB_ENFORCE is. The CONTROL is the same value without the newline, which
    declares in the arm under test (an unknown slug only while the flag is off)."""
    if flag is None:
        monkeypatch.delenv(_VOCAB_ENV, raising=False)
    else:
        monkeypatch.setenv(_VOCAB_ENV, flag)
    pid = _seed_peer(conn, "b09-nl")["peer_id"]

    ok = _post(client, pid, _declare_body(_skill_name("nl-ok"), runtime_profile=control))
    unknown_under_enforce = (flag is not None
                             and control.get("harness") == "unregistered_harness")
    assert ok.status_code == (400 if unknown_under_enforce else 201), (flag, ok.content)

    bad = _post(client, pid, _declare_body(_skill_name("nl"), runtime_profile=profile))
    assert (bad.status_code, bad.json()) == (400, {"error": "E_VALIDATION",
                                                   "message": message}), (flag, bad.content)


def test_b09_the_jwt_shape_scan_refuses_a_final_newline(
        conn, client, monkeypatch):
    """docs/DIVERGENCES.md 049, pinned so that changing it is a visible change. The JWT-shape
    redaction is a `search` for `^eyJ...$`, and Python's `$` still admits a final newline there.
    A JWT followed by one newline is refused so credential-like input is not stored. This strict
    choice remains separate from the vocabulary grammar fix."""
    monkeypatch.delenv(_VOCAB_ENV, raising=False)
    pid = _seed_peer(conn, "b09-jwt")["peer_id"]
    redacted = {"error": "E_VALIDATION",
                "message": "runtime vocabulary rejected (secret-like or node-local content): "
                           "runtime_status.model_refs[0]: JWT-shaped value"}
    jwt_like = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    # CONTROL: an ordinary model name declares.
    ok = _post(client, pid, _declare_body(_skill_name("jwt-ok"),
                                          runtime_status={"model_refs": ["gpt_model"]}))
    assert ok.status_code == 201, ok.content
    for value in (jwt_like, jwt_like + "\n"):
        bad = _post(client, pid, _declare_body(_skill_name("jwt"),
                                               runtime_status={"model_refs": [value]}))
        assert (bad.status_code, bad.json()) == (400, redacted), (value, bad.content)


# ===================================== the shared helpers keep what their callers had
#
# Five pieces of this app were de-duplicated (the publish-gate preamble and the insert
# kwargs in service.py, the request-body parsers and the error response in views.py, the
# strict base64url decoder in attestation.py, `_jsonb_or_null` in repository.py, and the
# stdlib imports hoisted in runtime.py / schema_validate.py). Each test below pins a
# contract the refactor had to keep that no earlier test stated on its own: the ORDER of
# the publish gates on both write routes, the exact 422 texts of the folded parsers, the
# error response's content type, the manifest_sig decoder's refusal text, SQL NULL for an
# absent JSON column, the logger the soft-warn goes to, and the digest's bytes.


def test_create_and_release_run_the_publish_gates_in_the_expected_order(conn, client, monkeypatch):
    """service.py's module docstring: the ORDER of the checks is the contract, because a
    body wrong in two ways gets the FIRST gate's answer. Both write routes run one shared
    preamble (`service._publish_gates`), so a body wrong at gate N and at gate N+1 must get
    gate N's message from EACH route - semver before paid consistency, paid consistency
    before the schema gate. The control is the good body landing on both routes, and the
    schema gate firing on its own, so the earlier answers were order and not a gate that
    never runs on that route."""
    monkeypatch.delenv("MESHYCHAT_SKILL_SCHEMA_ENFORCE", raising=False)   # enforce (010)
    peer = _seed_peer(conn, "gate-order")
    name = _skill_name("gate-order")
    assert _post(client, peer["peer_id"], _declare_body(name, "1.0.0"), BOTH).status_code == 201

    def declare(**over):
        return _post(client, peer["peer_id"], _declare_body(_skill_name("go"), **over), BOTH)

    def release(**over):
        return client.post(f"/v1/peers/me/skills/{name}/versions",
                           data=json.dumps(_declare_body(name, **over)),
                           content_type="application/json", **_auth(peer["peer_id"], BOTH))

    assert declare(version="1.0.0").status_code == 201                 # controls first
    assert release(version="2.0.0").status_code == 201

    paid = {"share_mode": "marketplace_paid"}                           # no price: gate 2
    external = {"input_schema": {"$ref": "https://evil.example.test/x"}}   # gate 4
    semver_msg = "invalid version 1.0: must be three dotted numerics (semver-lite)"
    paid_msg = "share_mode = marketplace_paid requires price_cents and currency"
    for route in (declare, release):
        wrong_at_1_and_2 = route(version="1.0", **paid)
        assert (wrong_at_1_and_2.status_code, wrong_at_1_and_2.json()["message"]) == \
            (400, semver_msg), (route.__name__, wrong_at_1_and_2.content)
        wrong_at_2_and_4 = route(version="3.0.0", **paid, **external)
        assert (wrong_at_2_and_4.status_code, wrong_at_2_and_4.json()["message"]) == \
            (400, paid_msg), (route.__name__, wrong_at_2_and_4.content)
        wrong_at_4 = route(version="3.0.0", **external)
        assert wrong_at_4.status_code == 400, (route.__name__, wrong_at_4.content)
        assert wrong_at_4.json()["message"].startswith(
            "skill schema invalid: external $ref not allowed"), wrong_at_4.content
    # Nothing above was stored: the peer's current versions are the two controls'.
    listed = client.get("/v1/peers/me/skills", **_auth(peer["peer_id"], BOTH)).json()
    assert {s["version"] for s in listed} == {"1.0.0", "2.0.0"}, listed


def test_the_folded_body_parsers_keep_expected_shapes_and_texts(conn, client):
    """`views._string(required=...)`, `_bool(default=None)` and `_value` each replaced a
    sibling helper that was its line-for-line copy. The 422 texts and the absent/null
    answers are the wire, so they are pinned here rather than inferred from the fold:
    an optional string, value or bool given `null` is None; a REQUIRED string given
    `null` is the type error, not the missing-field one; and the update boolean leaves the
    stored value unchanged when absent."""
    peer = _seed_peer(conn, "parsers")
    pid = peer["peer_id"]
    ok = _post(client, pid, _declare_body(_skill_name("pz"), display_name=None,
                                          description=None, currency=None,
                                          output_schema=None, examples=None), BOTH)
    assert ok.status_code == 201, ok.content                       # control first
    body = ok.json()
    assert (body["display_name"], body["description"], body["currency"],
            body["output_schema"], body["examples"]) == (None, None, None, None, None)

    for field, value in (("name", None), ("name", 5), ("version", ["1.0.0"]),
                         ("display_name", 5), ("currency", True)):
        bad = _post(client, pid, {**_declare_body(_skill_name("pt")), field: value}, BOTH)
        assert bad.status_code == 422, (field, bad.content)
        assert bad.json() == {"error": "E_VALIDATION",
                              "message": f"invalid type for `{field}`: expected a string"}, field

    name = _skill_name("pb")
    assert _post(client, pid, _declare_body(name, requires_human_auth=True),
                 BOTH).status_code == 201
    base = {"share_mode": "private", "tags": []}
    for value in ({}, {"requires_human_auth": None}):
        kept = _patch_skill(client, pid, name, {**base, **value})
        assert kept.status_code == 200, kept.content
        assert kept.json()["requires_human_auth"] is True, (value, kept.content)
    flipped = _patch_skill(client, pid, name, {**base, "requires_human_auth": False})
    assert flipped.status_code == 200 and flipped.json()["requires_human_auth"] is False
    bad = _patch_skill(client, pid, name, {**base, "requires_human_auth": "yes"})
    assert bad.status_code == 422, bad.content
    assert bad.json() == {"error": "E_VALIDATION",
                          "message": "invalid type for `requires_human_auth`: expected a boolean"}


def test_an_error_response_is_served_with_the_same_content_type_as_a_success(conn, client):
    """`views._error_response` returns through `views._json`, the success helper. Besides
    the bytes (pinned by the compact-body test above) the header is the one thing a client
    sees, so the two halves of the wire are held to the same `Content-Type`."""
    peer = _seed_peer(conn, "ctype")
    err = _post(client, peer["peer_id"], _declare_body(_skill_name("ct")), [])
    assert err.status_code == 403
    assert err["Content-Type"] == "application/json"
    assert err.content == json.dumps(err.json(), separators=(",", ":")).encode()
    ok = client.get("/v1/peers/me/skills", **_auth(peer["peer_id"], BOTH))
    assert ok.status_code == 200                                   # control
    assert ok["Content-Type"] == err["Content-Type"]


def test_a_manifest_sig_that_is_not_base64url_no_pad_is_refused_with_the_expected_text(conn, client):
    """`manifest_sig` is decoded as base64url without padding. The
    one strict decoder both verifiers share (`attestation._strict_b64u_no_pad`) refuses a
    padded spelling, a character outside the alphabet and a trailing newline with the
    `malformed` message, before the 64-byte check; the publisher-key side of the same decoder
    is pinned in tests/test_grants.py. The control is the same signature spelled
    correctly, stored."""
    from apps.skillsreg import attestation as sattest
    peer = _seed_peer(conn, "b64u")
    sig = P.b64u(bytes(range(64)))
    ok = _post(client, peer["peer_id"],
               _declare_body(_skill_name("b64"), manifest_sig=sig, signing_kid="kid-1"), BOTH)
    assert ok.status_code == 201, ok.content                       # control first
    assert ok.json()["manifest_sig"] == list(bytes(range(64)))

    refused = {"error": "E_VALIDATION",
               "message": "manifest attestation malformed: manifest_sig is not valid "
                          "base64url-no-pad"}
    for label, spelled in (("padded", sig + "="), ("outside the alphabet", sig[:-1] + "*"),
                           ("trailing newline", sig + "\n")):
        bad = _post(client, peer["peer_id"],
                    _declare_body(_skill_name("b64"), manifest_sig=spelled,
                                  signing_kid="kid-1"), BOTH)
        assert (bad.status_code, bad.json()) == (400, refused), (label, bad.content)
        with pytest.raises(ValueError):
            sattest._strict_b64u_no_pad(spelled)
    # The helper's own contract: bytes for a well-formed input, ValueError otherwise -
    # including a non-string, which each caller turns into its own class and text.
    assert sattest._strict_b64u_no_pad(sig) == bytes(range(64))
    with pytest.raises(ValueError):
        sattest._strict_b64u_no_pad(None)
    with pytest.raises(sattest.ManifestAttestationError, match="manifest_sig is not valid"):
        sattest._b64url_no_pad_decode(None)
    with pytest.raises(sattest.AttestationSignatureInvalid,
                       match="publisher public key is not valid"):
        sattest._publisher_public_key(None)


def test_an_absent_json_column_is_bound_as_sql_null_and_a_present_one_as_jsonb(conn, client):
    """`repository._jsonb_or_null` replaced six inline conditionals in `insert` and
    `update_metadata`. SQL NULL and JSON `null` are different stored values and the wire
    answers None for both, so the row is read back directly, on the declare path and on
    the full-replace update path."""
    from psycopg.types.json import Jsonb
    assert repository._jsonb_or_null(None) is None
    wrapped = repository._jsonb_or_null({"a": 1})
    assert isinstance(wrapped, Jsonb) and wrapped.obj == {"a": 1}

    peer = _seed_peer(conn, "jsonb")
    name = _skill_name("jsonb")
    ok = _post(client, peer["peer_id"], _declare_body(name, examples=None), BOTH)
    assert ok.status_code == 201, ok.content
    with conn.cursor() as cur:
        cur.execute("SELECT examples IS NULL, output_schema IS NULL, input_schema IS NULL "
                    "FROM skills WHERE id = %s", (ok.json()["id"],))
        assert cur.fetchone() == (True, True, False)
    present = _patch_skill(client, peer["peer_id"], name,
                           {"share_mode": "private", "tags": [], "examples": [{"input": 1}]})
    assert present.status_code == 200, present.content
    assert present.json()["examples"] == [{"input": 1}]
    absent = _patch_skill(client, peer["peer_id"], name, {"share_mode": "private", "tags": []})
    assert absent.status_code == 200 and absent.json()["examples"] is None, absent.content
    with conn.cursor() as cur:
        cur.execute("SELECT examples IS NULL FROM skills WHERE id = %s", (ok.json()["id"],))
        assert cur.fetchone() == (True,)


def test_input_schema_digest_is_sha256_over_the_jcs_bytes():
    """The catalog's `input_schema_hash` is defined in terms of
    this: key order in the input must not matter (JCS sorts) and the hex is lowercase."""
    schema = {"b": 1, "a": [2, {"d": 3, "c": 4}]}
    assert schema_validate.input_schema_digest(schema) == \
        hashlib.sha256(b'{"a":[2,{"c":4,"d":3}],"b":1}').hexdigest()
    assert schema_validate.input_schema_digest({"a": [2, {"c": 4, "d": 3}], "b": 1}) == \
        schema_validate.input_schema_digest(schema)


def test_the_unknown_slug_soft_warning_goes_to_the_skillsreg_logger_and_the_flag_is_read_per_call(
        monkeypatch):
    """A well-formed unknown slug produces a soft warning. The logger is
    `meshychat.skillsreg`, the one lessons.py and service.py bind too (settings.py routes
    `meshychat` to stdout with propagate off, so a handler is attached to it directly
    here rather than through caplog's root handler). And MESHYCHAT_RUNTIME_VOCAB_ENFORCE is
    read at CALL time: the same process flips from warning to refusing with no
    re-import."""
    import logging
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("meshychat.skillsreg")
    handler, old_level = Keep(level=logging.WARNING), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        monkeypatch.delenv(_VOCAB_ENV, raising=False)
        rt._check_vocab_slug("harness", "unregistered_harness", rt.WELL_KNOWN_HARNESS)
        assert [r.name for r in records] == ["meshychat.skillsreg"]
        assert records[0].getMessage() == (
            "runtime_vocab_unknown_slug (soft-warn; set MESHYCHAT_RUNTIME_VOCAB_ENFORCE=1 to "
            "reject): what=harness slug=unregistered_harness")
        # Controls: a well-known slug warns about nothing...
        rt._check_vocab_slug("harness", "codex", rt.WELL_KNOWN_HARNESS)
        assert len(records) == 1
        # ...and the flag, set after import, refuses instead of warning.
        monkeypatch.setenv(_VOCAB_ENV, "1")
        with pytest.raises(rt.RuntimeValidateError, match="^unknown harness slug"):
            rt._check_vocab_slug("harness", "unregistered_harness", rt.WELL_KNOWN_HARNESS)
        assert len(records) == 1
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_stamp_status_stamps_the_server_clock_in_timestamp_form_only_with_a_ttl():
    """The client's `lease_declared_at` never survives: with a
    TTL it is replaced by the server's `now` in RFC 3339 spelling (the formatter every timestamp
    on this wire goes through), and without a TTL it is
    cleared rather than carried."""
    from apps.peers.timefmt import format_rfc3339
    now = _dt.datetime(2026, 9, 21, 12, 30, 45, 123456, tzinfo=_dt.timezone.utc)
    stamped = rt.stamp_status({"lease_ttl_secs": 60, "lease_declared_at": "1999-01-01T00:00:00Z"},
                              now)
    assert stamped["lease_declared_at"] == format_rfc3339(now)
    assert stamped["lease_declared_at"].startswith("2026-09-21T12:30:45")
    assert stamped["lease_declared_at"].endswith("Z")
    assert stamped["lease_ttl_secs"] == 60
    cleared = rt.stamp_status({"lease_declared_at": "1999-01-01T00:00:00Z"}, now)
    assert cleared["lease_declared_at"] is None
    assert rt.stamp_status(None, now) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
