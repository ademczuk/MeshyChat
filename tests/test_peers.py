"""Peers, presence and agent cards, proved against the real database and real HTTP.

WHAT MAKES A TEST HERE COUNT

Every rejection below is paired with a POSITIVE CONTROL in the same test: an
otherwise-identical request that must SUCCEED. A rejection test alone proves nothing,
because a 400 is also what a typo in the URL, a missing fixture, an unseeded key or a
crashed connection produces. Where the control is the interesting half it is asserted
FIRST, so a failure reads as "the happy path broke" rather than "the guard fired".

Two of these controls were not decoration. The wrong-jku test passes trivially if the
card never verified in the first place, and the missing-co-signature test passes
trivially if the peer had no active key to co-sign with (the bootstrap path returns 403,
not 401, and would have looked like a pass).

NO DATABASE MOCKS. Every row is written to the migrated Postgres named by
MESHY_TEST_DSN. The only thing generated per-run is key material.

Run: pytest tests/test_peers.py -q
"""
from __future__ import annotations

import base64
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
# Promote the conftest variable to the one the APPLICATION reads. `MESHY_TEST_DSN` belongs
# to tests/conftest.py; apps resolve through apps/core/dbconn.dsn(), which has never heard
# of it. The app modules used to read it themselves, which is why this line was not needed
# - that fallback has been removed so every surface resolves the same way.
os.environ.setdefault("MESHY_DSN", DSN)

BASE_URL = "https://meshy.test"
ISSUER = "meshy-test-issuer"

# --- Django settings: NEVER assume this module won the configuration race ------
# Several test modules in this suite configure Django at import time
# (`DJANGO_SETTINGS_MODULE=meshychat.settings`, and authapi re-points it to its own
# testsettings). Whichever is imported FIRST wins, and the loser's `configure()` call is
# skipped silently. An earlier draft of this file configured settings at import and
# passed alone while failing in the full-suite run - a green result that depended
# entirely on collection order.
#
# So the block below is only a FALLBACK for running this file standalone, and every
# setting these tests actually depend on is applied per-test by the autouse
# `peers_settings` fixture via `override_settings`, which layers on top of whatever base
# configuration won. ROOT_URLCONF = meshychat.urls is what makes the ROUTE PATHS part
# of what is tested rather than an assumption.
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

# The PROJECT settings module, as test_grants.py and test_dashboard.py do, so requests
# resolve through meshychat.urls and the peers routes are tested as MOUNTED. This file
# used to configure() a stand-in with ROOT_URLCONF = apps.peers.urls, the pattern the
# review queue bans: a route that was never mounted would still pass here.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

from django.conf import settings as dj_settings  # noqa: E402

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.crypto import primitives as P  # noqa: E402
from apps.peers import cards, db, keys, presence  # noqa: E402
from apps.peers.timefmt import format_rfc3339, now_utc, parse_rfc3339  # noqa: E402

# NOTE: `setup_test_environment()` is NOT called here. pytest-django is installed and
# calls it from a session fixture; calling it twice raises. Leaving it to the plugin
# also means this file behaves the same whether or not the plugin is active, because
# Django's test Client works without it - only the template/email instrumentation
# depends on it, and this app uses neither.


# ------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def peers_settings():
    """Apply this app's settings on top of whatever base configuration won the race.

    `override_settings` is used rather than a module-level `configure()` because it
    layers, so these tests behave identically whether they run alone, after
    test_auth_flows.py (which re-points DJANGO_SETTINGS_MODULE at
    apps.authapi.testsettings), or after any other module. Autouse, because every test
    here needs at least ROOT_URLCONF and the JWT key.
    """
    with override_settings(
        ROOT_URLCONF="meshychat.urls",
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_PUBLIC_BASE_URL=BASE_URL,
    ):
        yield


def test_the_settings_override_is_actually_in_effect(client):
    """Positive control for the fixture above, and for the whole file.

    Every other test in this module would fail in a confusing way (404s, 500s, 401s) if
    the override silently did not apply - which is exactly what happened when settings
    were configured at import time and another module won the race. This asserts the
    three settings directly, so that failure mode reports itself as one named cause
    rather than as twenty unrelated-looking assertion errors.
    """
    from django.conf import settings as live
    assert live.ROOT_URLCONF == "meshychat.urls"
    assert live.MESHY_PUBLIC_BASE_URL == BASE_URL
    assert live.MESHY_JWT_PUBLIC_KEY_PEM == JWT_PUBLIC_PEM
    # And the URLconf really is the one being resolved, not just the setting's value.
    assert client.get(f"/agents/{uuid.uuid4()}/jwks.json").status_code == 200


@pytest.fixture()
def owner_conn():
    """Owner role: bypasses RLS. Seeding only, exactly as test_rls_isolation.py does."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """The connection a request actually runs on: meshychat_app, autocommit."""
    with db.connection() as conn:
        yield conn


@pytest.fixture()
def client():
    return Client()


def _seed_peer(conn, tag: str) -> dict:
    """An owner + an active peer.

    Column names read off the live schema, not guessed: the owner FK targets
    `dashboard_users` (NOT `owners`), and a 'product'-plane user must have a NULL
    trust_group_id - CHECK ((plane='dev') = (trust_group_id IS NOT NULL)).
    """
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'product',NULL)",
                    (owner_id, f"peers-{tag}-{owner_id.hex[:8]}@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"peers-{tag}-{peer_id.hex[:8]}", f"peers {tag}"))
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id,
            "name": f"peers-{tag}-{peer_id.hex[:8]}", "display_name": f"peers {tag}"}


def _mint(peer_id, *, aud="peer", lin=None, scopes=None, ttl=300, act_owner=None) -> str:
    """A REAL EdDSA peer JWT, signed with the key `settings.MESHY_JWT_PUBLIC_KEY_PEM`
    verifies against. Claims are sub, iss, iat, exp, and scopes; aud, lin, and act are optional
    (act.sub is the peer when an owner mints for it)."""
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims = {"sub": str(act_owner or peer_id), "iss": ISSUER, "iat": now,
              "exp": now + ttl, "scopes": scopes or []}
    if aud is not None:
        claims["aud"] = aud
    if lin is not None:
        claims["lin"] = str(lin)
    if act_owner is not None:
        claims["act"] = {"sub": str(peer_id)}
    return pyjwt.encode(claims, JWT_PRIVATE_PEM, algorithm="EdDSA")


def _auth(peer_id, **kw) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {_mint(peer_id, **kw)}"}


def _bootstrap_key(conn, peer_id) -> bytes:
    """Give a peer its FIRST active key and return the 32-byte seed.

    This goes through `keys.publish(..., owner_scoped=True)` rather than an INSERT,
    because the first publish is exactly the path the peer route refuses
    with BootstrapRequiresOwnerScope and it must still be the real
    proof-checked code that seeds the fixture. The owner-scoped HTTP route is not in
    this agent's file set (it needs AuthenticatedOwner, which apps/authapi owns), so the
    service is called directly.
    """
    seed = os.urandom(32)
    req = keys.build_publish_request(seed, peer_id)
    keys.publish(conn, peer_id, req, None, owner_scoped=True, caller_lineage=None)
    return seed


# ====================================================================== timefmt
# The signed bytes contain a formatted timestamp, so the formatter is part of the
# signature contract and gets its own known-answer tests.

def test_issued_at_format_matches_the_production_client_byte_for_byte():
    """The live daemon formats issued_at as "%Y-%m-%dT%H:%M:%SZ"
    (tools/memory/daemon/meshy_memory_daemon.py:629), so the no-fraction formatter must produce
    EXACTLY that or live client stamps stop verifying."""
    t = _dt.datetime(2026, 9, 21, 15, 45, 26, 0, tzinfo=_dt.timezone.utc)
    assert format_rfc3339(t) == "2026-09-21T15:45:26Z"
    # Control that the formatter is not simply hard-coding the no-fraction form:
    # The formatter emits 3 digits for whole milliseconds and 6 otherwise.
    assert format_rfc3339(t.replace(microsecond=500000)) == "2026-09-21T15:45:26.500Z"
    assert format_rfc3339(t.replace(microsecond=500001)) == "2026-09-21T15:45:26.500001Z"
    # And that it is NOT Python's isoformat(), which is the trap:
    assert t.isoformat() == "2026-09-21T15:45:26+00:00" != format_rfc3339(t)


def test_issued_at_round_trips_so_the_server_rebuilds_the_bytes_the_client_signed():
    for text in ("2026-09-21T15:45:26Z", "2026-09-21T15:45:26.500Z",
                 "2026-09-21T15:45:26.500001Z"):
        assert format_rfc3339(parse_rfc3339(text)) == text


# ===================================================================== presence

def test_heartbeat_then_read_back_live_true(owner_conn, app_conn, client):
    """POST /v1/peers/me/presence, then GET /v1/peers/presence -> live is true."""
    p = _seed_peer(owner_conn, "beat")

    r = client.post("/v1/peers/me/presence", data=json.dumps({"state": "active"}),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 200, r.content
    assert r.json() == {"ok": True}

    r = client.get("/v1/peers/presence", **_auth(p["peer_id"]))
    assert r.status_code == 200, r.content
    payload = r.json()
    row = next(x for x in payload["presence"] if x["peer_id"] == str(p["peer_id"]))

    assert row["live"] is True, f"a beat one moment old read as stale: {row}"
    assert row["state"] == "active"
    assert row["last_beat"] is not None and row["last_beat"].endswith("Z")
    assert row["age_secs"] is not None and row["age_secs"] <= 2
    # last_activity is a SEPARATE column this route never writes; it sets only last_beat,
    # so a peer that has beaten but done no work is NOT "working".
    assert row["last_activity"] is None
    assert row["working"] is False
    # The verdict carries its own provenance.
    assert payload["verified_via"] == "server-staleness-rule"
    assert payload["probed_by"] == "meshy-server"
    assert payload["stale_after_secs"] == presence.DEFAULT_STALE_AFTER_SECS == 20
    assert payload["active_after_secs"] == presence.DEFAULT_ACTIVE_AFTER_SECS == 900
    assert payload["computed_at"].endswith("Z")


def test_a_stale_peer_reads_live_false(owner_conn, app_conn, client):
    """A peer whose last_beat is older than the window is not live.

    POSITIVE CONTROL: the SAME peer, read with a window wide enough to cover the same
    row, must read live=true. Without it, live=false would also be what a missing row,
    a NULL last_beat or a broken query produces.
    """
    p = _seed_peer(owner_conn, "stale")
    assert client.post("/v1/peers/me/presence", data="{}", content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 200

    # Age the row past DEFAULT_STALE_AFTER_SECS (20) without sleeping for it.
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE mesh_presence SET last_beat = NOW() - interval '60 seconds' "
                    "WHERE peer_id = %s", (p["peer_id"],))
    owner_conn.commit()

    def read(query=""):
        r = client.get("/v1/peers/presence" + query, **_auth(p["peer_id"]))
        assert r.status_code == 200, r.content
        return next(x for x in r.json()["presence"] if x["peer_id"] == str(p["peer_id"]))

    wide = read("?stale_after_secs=600")
    default = read()

    assert wide["live"] is True, (
        "the row is not visible or not aged as expected even under a 600s window - the "
        "live=false assertion below would pass vacuously")
    assert wide["age_secs"] >= 59
    assert default["live"] is False, "a 60s-old beat read as live under the 20s default"


def _presence_row(client, peer_id):
    r = client.get("/v1/peers/presence", **_auth(peer_id))
    assert r.status_code == 200, r.content
    return next(x for x in r.json()["presence"] if x["peer_id"] == str(peer_id))


def _beat(client, peer_id, body):
    return client.post("/v1/peers/me/presence", data=json.dumps(body),
                       content_type="application/json", **_auth(peer_id))


def test_a_state_with_a_ttl_reads_as_published_until_it_expires_then_unknown(owner_conn, client):
    """Divergence 082 (issue #139): a seat publishes `asleep` + `compacting` with ttl_secs. Before
    the expiry `effective_state` is what it said; past it (aged in the database, no sleeping) it
    is `unknown`, while `state` still says what was written."""
    p = _seed_peer(owner_conn, "ttl")
    assert _beat(client, p["peer_id"], {"state": "asleep", "note": "compacting",
                                        "ttl_secs": 900}).status_code == 200
    row = _presence_row(client, p["peer_id"])
    assert row["effective_state"] == "asleep" and row["state"] == "asleep" and row["note"] == "compacting"
    assert row["state_expires_at"] is not None and row["state_expires_at"].endswith("Z")

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE mesh_presence SET state_expires_at = NOW() - interval '1 second' "
                    "WHERE peer_id = %s", (p["peer_id"],))
    owner_conn.commit()
    row = _presence_row(client, p["peer_id"])
    assert row["effective_state"] == "unknown", f"an expired state still read as published: {row}"
    assert row["state"] == "asleep", "the wire field `state` changed meaning"


def test_a_missed_hook_leaves_the_seat_unknown_not_stuck_asleep(owner_conn, client):
    """The acceptance case: `asleep` was published before a compaction and the session-start hook
    that should have said `active` never ran. Once the ttl passes the seat is `unknown`. A later
    beat with no ttl (the old client) clears the expiry, so its state never expires (control)."""
    p = _seed_peer(owner_conn, "missedhook")
    assert _beat(client, p["peer_id"], {"state": "asleep", "note": "compacting", "ttl_secs": 1}).status_code == 200
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE mesh_presence SET state_expires_at = NOW() - interval '5 seconds' "
                    "WHERE peer_id = %s", (p["peer_id"],))
    owner_conn.commit()
    assert _presence_row(client, p["peer_id"])["effective_state"] == "unknown"
    assert _beat(client, p["peer_id"], {"state": "active"}).status_code == 200
    row = _presence_row(client, p["peer_id"])
    assert row["effective_state"] == "active" and row["state_expires_at"] is None, row


def test_a_peer_that_never_beat_reads_unknown_not_the_column_default(owner_conn, client):
    """A row made by touch_last_activity (any authenticated call) holds the column default
    `active`, which no seat said. `effective_state` must not repeat it."""
    from apps.calls import reads
    p = _seed_peer(owner_conn, "nobeat")
    with owner_conn.cursor() as cur:
        reads.touch_last_activity(owner_conn, p["peer_id"])
    owner_conn.commit()
    row = _presence_row(client, p["peer_id"])
    assert row["state"] == "active" and row["last_beat"] is None
    assert row["effective_state"] == "unknown", f"a peer that never beat read as {row['effective_state']}"


@pytest.mark.parametrize("ttl", [0, -1, 86401, "60", 1.5, True, None])
def test_ttl_secs_must_be_an_integer_within_bounds(owner_conn, client, ttl):
    """1..86400, an int (a bool is refused), or absent. `None` is the explicit-null control: it
    is accepted and means no expiry."""
    p = _seed_peer(owner_conn, f"ttlbad{abs(hash(repr(ttl))) % 10000}")
    r = _beat(client, p["peer_id"], {"state": "active", "ttl_secs": ttl})
    if ttl is None:
        assert r.status_code == 200, r.content
        assert _presence_row(client, p["peer_id"])["state_expires_at"] is None
    else:
        assert r.status_code == 400, (ttl, r.status_code, r.content)
        assert "ttl_secs" in r.json().get("message", json.dumps(r.json()))


def test_working_keys_on_last_activity_and_its_own_separate_window(owner_conn, client):
    """`working` is `activity_age <= active_after`, a SECOND
    window over a DIFFERENT column from `live`.

    Every other presence test can only ever see `working: False`, because the beat route
    never writes `last_activity` - so without this test the field is asserted only in
    the state a bug that hard-coded `False` would also produce. Here the column is set
    directly (nothing in this app's routes writes it) and the two windows are moved
    independently, so `live` and `working` are shown to be genuinely separate verdicts.
    """
    p = _seed_peer(owner_conn, "working")
    assert client.post("/v1/peers/me/presence", data="{}", content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 200
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE mesh_presence SET last_activity = NOW() - interval '300 seconds' "
                    "WHERE peer_id = %s", (p["peer_id"],))
    owner_conn.commit()

    def read(query=""):
        r = client.get("/v1/peers/presence" + query, **_auth(p["peer_id"]))
        assert r.status_code == 200, r.content
        return next(x for x in r.json()["presence"] if x["peer_id"] == str(p["peer_id"]))

    default = read()                       # 300s activity age, 900s default window
    assert default["working"] is True, f"a 300s-old activity read as idle: {default}"
    assert default["activity_age_secs"] >= 299
    assert default["last_activity"] is not None and default["last_activity"].endswith("Z")
    assert default["live"] is True         # the beat is fresh; the two are independent

    narrow = read("?active_after_secs=60")
    assert narrow["working"] is False, "active_after_secs did not narrow the window"
    assert narrow["live"] is True, "narrowing the ACTIVITY window must not affect liveness"

    # And the inverse: narrowing the liveness window must not touch `working`.
    both = read("?stale_after_secs=1&active_after_secs=900")
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE mesh_presence SET last_beat = NOW() - interval '60 seconds' "
                    "WHERE peer_id = %s", (p["peer_id"],))
    owner_conn.commit()
    stale = read()
    assert both["working"] is True
    assert stale["live"] is False and stale["working"] is True, (
        "a peer that stopped beating but has recent work must still read working=True")


def test_blocked_on_is_nulled_for_a_peer_that_is_not_live(owner_conn, app_conn, client):
    """The abandoned-blocker rule: a non-live row's
    blocked_on is suppressed in the response so nobody chases a ghost.

    POSITIVE CONTROL: the identical row, read under a window that makes it live, must
    show blocked_on - otherwise "null" would just mean the write never happened.
    """
    a = _seed_peer(owner_conn, "blk-a")
    b = _seed_peer(owner_conn, "blk-b")
    r = client.post("/v1/peers/me/presence",
                    data=json.dumps({"state": "idle", "blocked_on": str(b["peer_id"]),
                                     "note": "waiting"}),
                    content_type="application/json", **_auth(a["peer_id"]))
    assert r.status_code == 200, r.content

    # It is stored regardless of the response rule.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT blocked_on, note, state FROM mesh_presence WHERE peer_id=%s",
                    (a["peer_id"],))
        stored = cur.fetchone()
    assert str(stored[0]) == str(b["peer_id"]) and stored[1] == "waiting" and stored[2] == "idle"

    def read(query=""):
        r = client.get("/v1/peers/presence" + query, **_auth(a["peer_id"]))
        return next(x for x in r.json()["presence"] if x["peer_id"] == str(a["peer_id"]))

    live_row = read()
    assert live_row["live"] is True and live_row["blocked_on"] == str(b["peer_id"]), (
        f"blocked_on was not returned even while live: {live_row}")

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE mesh_presence SET last_beat = NOW() - interval '60 seconds' "
                    "WHERE peer_id = %s", (a["peer_id"],))
    owner_conn.commit()

    stale_row = read()
    assert stale_row["live"] is False
    assert stale_row["blocked_on"] is None, "an abandoned blocker was still advertised"


def test_invalid_state_rejected_and_a_valid_one_accepted(owner_conn, client):
    """Control: 'asleep' is in STATES and must succeed."""
    p = _seed_peer(owner_conn, "state")
    ok = client.post("/v1/peers/me/presence", data=json.dumps({"state": "asleep"}),
                     content_type="application/json", **_auth(p["peer_id"]))
    assert ok.status_code == 200, ok.content

    bad = client.post("/v1/peers/me/presence", data=json.dumps({"state": "busy"}),
                      content_type="application/json", **_auth(p["peer_id"]))
    assert bad.status_code == 400, bad.content
    assert bad.json()["error"] == "E_VALIDATION"
    assert "invalid state: busy" in bad.json()["message"]


def test_a_peer_cannot_declare_itself_blocked_on_itself(owner_conn, client):
    """Control: blocked_on another peer is accepted."""
    a = _seed_peer(owner_conn, "self-a")
    b = _seed_peer(owner_conn, "self-b")
    ok = client.post("/v1/peers/me/presence",
                     data=json.dumps({"blocked_on": str(b["peer_id"])}),
                     content_type="application/json", **_auth(a["peer_id"]))
    assert ok.status_code == 200, ok.content

    bad = client.post("/v1/peers/me/presence",
                      data=json.dumps({"blocked_on": str(a["peer_id"])}),
                      content_type="application/json", **_auth(a["peer_id"]))
    assert bad.status_code == 400, bad.content
    assert bad.json()["message"] == "cannot be blocked on self"


def test_presence_requires_authentication(owner_conn, client):
    """Control-first: the same GET with a token is 200, without one is 401."""
    p = _seed_peer(owner_conn, "auth")
    assert client.get("/v1/peers/presence", **_auth(p["peer_id"])).status_code == 200
    anon = client.get("/v1/peers/presence")
    assert anon.status_code == 401, anon.content
    assert anon.json()["error"] == "E_AUTH_MISSING"


def test_a_dashboard_audience_token_is_refused_on_a_peer_route(owner_conn, client):
    """aud must be exactly "peer"; absent is rejected too.

    Control: the identical claims with aud="peer" succeed, so the 403 is the audience
    check and not a broken signature or a missing peer row.
    """
    p = _seed_peer(owner_conn, "aud")
    assert client.get("/v1/peers/presence", **_auth(p["peer_id"])).status_code == 200
    for aud in ("dashboard", None):
        r = client.get("/v1/peers/presence", **_auth(p["peer_id"], aud=aud))
        assert r.status_code == 403, f"aud={aud!r} was accepted: {r.content}"


def test_a_deleted_peer_keeps_a_valid_jwt_but_loses_access(owner_conn, client):
    """The token is not the last word on whether the peer exists.

    The SAME token is used before and after the status change, so the difference cannot
    be a re-mint, an expiry or a different key.
    """
    p = _seed_peer(owner_conn, "deleted")
    headers = _auth(p["peer_id"])
    assert client.get("/v1/peers/presence", **headers).status_code == 200
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE peers SET status='deleted' WHERE id=%s", (p["peer_id"],))
    owner_conn.commit()
    r = client.get("/v1/peers/presence", **headers)
    assert r.status_code == 403, r.content


# ========================================================================= keys

def test_kid_is_the_first_16_b64url_chars_of_sha256_of_the_raw_key():
    """Recomputed here from the definition rather than by calling
    the same helper the server calls, so the two cannot agree on a shared mistake."""
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    import hashlib
    expected = base64.urlsafe_b64encode(hashlib.sha256(pub).digest()).rstrip(b"=").decode()[:16]
    kid = keys.derive_kid(P.b64u(pub))
    assert kid == expected and len(kid) == 16


def test_rotation_without_the_co_signature_is_rejected(owner_conn, app_conn, client):
    """A missing co-signature returns 401 E_SIG_BAD, not a silent skip.

    TWO positive controls, because this rejection has two ways to pass vacuously:
      1. the peer must ALREADY have an active key, or the publish returns 403
         BootstrapRequiresOwnerScope and the co-signature is never reached at all;
      2. the same request WITH a valid co-signature must succeed, or the rejection
         could be any other defect in the proof.
    """
    p = _seed_peer(owner_conn, "cosig")
    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    assert keys.find_active(app_conn, p["peer_id"]) is not None, (
        "control 1 failed: no active key, so this would test the bootstrap path instead")

    new_seed = os.urandom(32)
    req = keys.build_publish_request(new_seed, p["peer_id"])
    body = {k: v for k, v in req.items() if not k.startswith("_")}

    # (a) no co-signature header at all
    r = client.post("/v1/peers/me/keys", data=json.dumps(body),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 401, r.content
    assert r.json()["error"] == "E_SIG_BAD"
    assert r.json()["message"] == "key publish co-signature is required"

    # (b) whitespace-only co-signature must NOT satisfy it.
    r = client.post("/v1/peers/me/keys", data=json.dumps(body),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG="   ", **_auth(p["peer_id"]))
    assert r.status_code == 401 and r.json()["message"] == "key publish co-signature is required"

    # (c) a co-signature by the WRONG key (the new key, not the current active one)
    #     is a signature failure, proving the header value is actually verified.
    wrong = keys.co_sign(new_seed, req)
    r = client.post("/v1/peers/me/keys", data=json.dumps(body),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=wrong, **_auth(p["peer_id"]))
    assert r.status_code == 401 and r.json()["error"] == "E_SIG_BAD"
    assert "signature verification failed" in r.json()["message"]

    # POSITIVE CONTROL 2: identical body, correct co-signature -> 201 Created.
    fresh = keys.build_publish_request(new_seed, p["peer_id"])
    r = client.post("/v1/peers/me/keys",
                    data=json.dumps({k: v for k, v in fresh.items() if not k.startswith("_")}),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, fresh), **_auth(p["peer_id"]))
    assert r.status_code == 201, r.content
    assert r.json()["kid"] == keys.derive_kid(fresh["public_key"])
    assert r.json()["active"] is True


def test_a_bad_proof_of_possession_is_rejected(owner_conn, app_conn, client):
    """The pop_signature must be by the NEW key over the canonical
    payload. Control: the untampered request succeeds."""
    p = _seed_peer(owner_conn, "pop")
    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    new_seed = os.urandom(32)

    tampered = keys.build_publish_request(new_seed, p["peer_id"])
    cosig = keys.co_sign(old_seed, tampered)
    body = {k: v for k, v in tampered.items() if not k.startswith("_")}
    body["nonce"] = body["nonce"] + "x"        # signature now covers different bytes
    r = client.post("/v1/peers/me/keys", data=json.dumps(body),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=cosig, **_auth(p["peer_id"]))
    assert r.status_code == 401 and r.json()["error"] == "E_SIG_BAD", r.content

    good = keys.build_publish_request(new_seed, p["peer_id"])
    r = client.post("/v1/peers/me/keys",
                    data=json.dumps({k: v for k, v in good.items() if not k.startswith("_")}),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, good), **_auth(p["peer_id"]))
    assert r.status_code == 201, r.content


def test_missing_proof_fields_and_a_stale_issued_at_are_different_errors(owner_conn, app_conn,
                                                                         client):
    """A malformed proof is 400 E_SIG_BAD; an
    out-of-window issued_at is 401 E_SIG_REPLAY. Control: in-window succeeds."""
    p = _seed_peer(owner_conn, "window")
    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    new_seed = os.urandom(32)

    stale_at = now_utc() - _dt.timedelta(seconds=keys.REPLAY_WINDOW_SECS + 5)
    stale = keys.build_publish_request(new_seed, p["peer_id"], issued_at=stale_at)
    r = client.post("/v1/peers/me/keys",
                    data=json.dumps({k: v for k, v in stale.items() if not k.startswith("_")}),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, stale), **_auth(p["peer_id"]))
    assert r.status_code == 401 and r.json()["error"] == "E_SIG_REPLAY", r.content

    naked = keys.build_publish_request(new_seed, p["peer_id"])
    body = {"public_key": naked["public_key"]}          # the shape that failed 384 times
    r = client.post("/v1/peers/me/keys", data=json.dumps(body),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, naked), **_auth(p["peer_id"]))
    assert r.status_code == 400 and r.json()["error"] == "E_SIG_BAD", r.content
    assert r.json()["message"] == "key publish proof is missing or malformed"

    good = keys.build_publish_request(new_seed, p["peer_id"])
    r = client.post("/v1/peers/me/keys",
                    data=json.dumps({k: v for k, v in good.items() if not k.startswith("_")}),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, good), **_auth(p["peer_id"]))
    assert r.status_code == 201, r.content


def test_a_replayed_nonce_is_refused_the_second_time(owner_conn, app_conn, client):
    """(peer_id, nonce) is spent once. The FIRST call is its own
    positive control: it must be a 201 before the second can mean anything."""
    p = _seed_peer(owner_conn, "replay")
    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    new_seed = os.urandom(32)
    nonce = uuid.uuid4().hex

    req = keys.build_publish_request(new_seed, p["peer_id"], nonce=nonce)
    body = json.dumps({k: v for k, v in req.items() if not k.startswith("_")})
    cosig = keys.co_sign(old_seed, req)

    first = client.post("/v1/peers/me/keys", data=body, content_type="application/json",
                        HTTP_X_KEY_COSIG=cosig, **_auth(p["peer_id"]))
    assert first.status_code == 201, first.content

    # Replay the identical request. The key it publishes is now the active one, so the
    # co-signature is re-made by it; only the nonce is reused.
    replay_req = keys.build_publish_request(new_seed, p["peer_id"], nonce=nonce)
    second = client.post(
        "/v1/peers/me/keys",
        data=json.dumps({k: v for k, v in replay_req.items() if not k.startswith("_")}),
        content_type="application/json",
        HTTP_X_KEY_COSIG=keys.co_sign(new_seed, replay_req), **_auth(p["peer_id"]))
    assert second.status_code == 401, second.content
    assert second.json()["error"] == "E_SIG_REPLAY"


def test_first_publish_on_the_peer_route_is_forbidden(owner_conn, app_conn, client):
    """Bootstrap requires the owner-scoped route.

    Control: the SAME peer, once it holds a key, can publish through this route, so the
    403 is the bootstrap rule and not a blanket denial.
    """
    p = _seed_peer(owner_conn, "bootstrap")
    assert keys.find_active(app_conn, p["peer_id"]) is None

    seed = os.urandom(32)
    req = keys.build_publish_request(seed, p["peer_id"])
    r = client.post("/v1/peers/me/keys",
                    data=json.dumps({k: v for k, v in req.items() if not k.startswith("_")}),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 403, r.content
    assert r.json()["error"] == "E_FORBIDDEN"
    assert keys.find_active(app_conn, p["peer_id"]) is None, "a forbidden publish still wrote"

    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    nxt = keys.build_publish_request(os.urandom(32), p["peer_id"])
    r = client.post("/v1/peers/me/keys",
                    data=json.dumps({k: v for k, v in nxt.items() if not k.startswith("_")}),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, nxt), **_auth(p["peer_id"]))
    assert r.status_code == 201, r.content


def test_after_rotation_the_old_key_still_verifies_inside_the_grace_window(owner_conn, app_conn,
                                                                          client):
    """Rotation sets the superseded key to active=FALSE with revoked_at set to 24 hours in the future.
    `revoked_at` is the grace-window END, so the key remains RESOLVABLE and a signature
    made under it still verifies for 24 hours - that is what keeps in-flight calls
    working across a rotation.

    NEGATIVE CONTROL in the same test: push revoked_at into the past and the same lookup
    must stop resolving the same row. Without it, "still resolves" would be
    indistinguishable from "revocation was never applied at all".
    """
    p = _seed_peer(owner_conn, "grace")
    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    old_kid = keys.find_active(app_conn, p["peer_id"]).kid

    new_seed = os.urandom(32)
    req = keys.build_publish_request(new_seed, p["peer_id"])
    r = client.post("/v1/peers/me/keys",
                    data=json.dumps({k: v for k, v in req.items() if not k.startswith("_")}),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, req), **_auth(p["peer_id"]))
    assert r.status_code == 201, r.content
    new_kid = r.json()["kid"]
    assert new_kid != old_kid

    # The new key is the active one.
    active = keys.find_active(app_conn, p["peer_id"])
    assert active.kid == new_kid

    # The old key is inactive but its revoked_at is 24h in the FUTURE.
    old = keys.find_by_kid(app_conn, p["peer_id"], old_kid)
    assert old is not None, "the superseded key stopped resolving immediately - no grace window"
    assert old.active is False
    assert old.revoked_at is not None and old.revoked_at > now_utc()
    # ONE CLOCK, END TO END. revoked_at is written by Postgres as NOW() + 24h
    # (apps/peers/keys.py:229) while now_utc() is Python's own clock
    # (apps/peers/timefmt.py:80-81). Comparing them makes this assertion a measurement
    # of the OFFSET BETWEEN TWO CLOCKS, and the bound `<= 24.0` allows that offset
    # exactly zero room.
    #
    # It failed intermittently at "grace window was 24.000010762222225h, expected 24",
    # about 39ms over, reproduced 2 runs in 5 in isolation. Measured on this box over 12
    # samples, the database clock sits a median 22.9ms BEHIND Python's with a range of
    # -24.5ms to -2.7ms, so the naive reading, that the container clock runs ahead, is
    # contradicted by the data and is NOT the claim here. What the measurement does
    # establish is the part that matters: the skew between these two clocks varies over
    # a range of tens of milliseconds, and an exact bound with zero tolerance against a
    # quantity that varies at all will fail eventually whichever way it sits.
    #
    # Taking both timestamps from the database removes the class rather than padding it.
    # A tolerance wide enough to absorb clock skew would also be wide enough to hide a
    # real regression in the grace window.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT now()")
        db_now = cur.fetchone()[0]
    # Both values come back in the server's TimeZone (Europe/Vienna on HQB), and Python
    # subtracts two datetimes in the same zone as wall-clock times: across the 25 Oct
    # CEST->CET change 24 real hours read as 23. Subtract in UTC.
    utc = _dt.timezone.utc
    delta_hours = (old.revoked_at.astimezone(utc) - db_now.astimezone(utc)).total_seconds() / 3600
    assert 23.9 < delta_hours <= 24.0, f"grace window was {delta_hours}h, expected 24"
    assert old.is_revoked_now() is False

    # And a signature made under it verifies against the key that lookup returns.
    message = b"an in-flight call signed just before the rotation"
    signature = P.sign(old_seed, message)
    assert P.verify_strict(keys.decode_public_key(old.public_key), message, signature) is True

    # NEGATIVE CONTROL: past the grace window the very same lookup must go dark.
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE peer_keys SET revoked_at = NOW() - interval '1 second' "
                    "WHERE peer_id=%s AND kid=%s", (p["peer_id"], old_kid))
    owner_conn.commit()
    assert keys.find_by_kid(app_conn, p["peer_id"], old_kid) is None, (
        "an expired key still resolved - the grace check is not a window, it is an "
        "unconditional pass")


def test_self_revoke_is_immediate_and_has_no_grace_window(owner_conn, app_conn, client):
    """DELETE passes force_immediate=true.

    Control: a key superseded by ROTATION in the same test keeps its future cutoff, so
    "revoked_at is in the past" is a property of THIS path, not of revocation generally.
    """
    p = _seed_peer(owner_conn, "revoke")
    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    old_kid = keys.find_active(app_conn, p["peer_id"]).kid

    new_seed = os.urandom(32)
    req = keys.build_publish_request(new_seed, p["peer_id"])
    assert client.post("/v1/peers/me/keys",
                       data=json.dumps({k: v for k, v in req.items()
                                        if not k.startswith("_")}),
                       content_type="application/json",
                       HTTP_X_KEY_COSIG=keys.co_sign(old_seed, req),
                       **_auth(p["peer_id"])).status_code == 201
    new_kid = keys.find_active(app_conn, p["peer_id"]).kid

    superseded = keys.find_by_kid(app_conn, p["peer_id"], old_kid)
    assert superseded.revoked_at > now_utc(), "control: rotation must leave a FUTURE cutoff"

    r = client.delete(f"/v1/peers/me/keys/{new_kid}", **_auth(p["peer_id"]))
    assert r.status_code == 204, r.content
    assert r.content == b""

    assert keys.find_by_kid(app_conn, p["peer_id"], new_kid) is None
    assert keys.find_active(app_conn, p["peer_id"]) is None

    # 404 for a kid this peer does not hold.
    assert client.delete("/v1/peers/me/keys/not-a-real-kid",
                         **_auth(p["peer_id"])).status_code == 404


def test_a_lineage_null_rotation_with_live_refresh_tokens_is_refused(owner_conn, app_conn,
                                                                     client):
    """The migration-031 binding guard returns 409 E_CONFLICT.

    Control: the identical rotation under a JWT that DOES carry `lin` succeeds, so the
    409 is the lineage rule and not the presence of a refresh token breaking something
    else.
    """
    p = _seed_peer(owner_conn, "lineage")
    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    old_kid = keys.find_active(app_conn, p["peer_id"]).kid

    lineage = uuid.uuid4()
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refresh_tokens (token_hash, peer_id, owner_id, expires_at, "
            "issued_under_kid, lineage_id) VALUES (%s,%s,%s,NOW() + interval '7 days',%s,%s)",
            (os.urandom(32), p["peer_id"], p["owner_id"], old_kid, lineage))
    owner_conn.commit()

    new_seed = os.urandom(32)
    req = keys.build_publish_request(new_seed, p["peer_id"])
    body = json.dumps({k: v for k, v in req.items() if not k.startswith("_")})
    r = client.post("/v1/peers/me/keys", data=body, content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, req), **_auth(p["peer_id"]))
    assert r.status_code == 409, r.content
    assert r.json()["error"] == "E_CONFLICT"
    assert "lineage" in r.json()["message"]
    assert keys.find_active(app_conn, p["peer_id"]).kid == old_kid, (
        "the refused rotation still rotated - the guard is not inside the transaction")

    # Control: same rotation, lineage-stamped JWT.
    req2 = keys.build_publish_request(new_seed, p["peer_id"])
    r = client.post("/v1/peers/me/keys",
                    data=json.dumps({k: v for k, v in req2.items() if not k.startswith("_")}),
                    content_type="application/json",
                    HTTP_X_KEY_COSIG=keys.co_sign(old_seed, req2),
                    **_auth(p["peer_id"], lin=lineage))
    assert r.status_code == 201, r.content
    new_kid = r.json()["kid"]

    # ...and the caller's OWN lineage was re-anchored onto the new kid.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT issued_under_kid FROM refresh_tokens WHERE lineage_id=%s", (lineage,))
        assert cur.fetchone()[0] == new_kid


def test_list_and_fetch_active_key_routes(owner_conn, app_conn, client):
    """GET /v1/peers/{id}/keys/active is 404 BEFORE a
    key exists and 200 after - the 404 is its own control."""
    p = _seed_peer(owner_conn, "list")
    assert client.get(f"/v1/peers/{p['peer_id']}/keys/active",
                      **_auth(p["peer_id"])).status_code == 404

    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    kid = keys.find_active(app_conn, p["peer_id"]).kid

    r = client.get(f"/v1/peers/{p['peer_id']}/keys/active", **_auth(p["peer_id"]))
    assert r.status_code == 200, r.content
    assert r.json()["kid"] == kid and r.json()["active"] is True
    assert r.json()["revoked_at"] is None

    req = keys.build_publish_request(os.urandom(32), p["peer_id"])
    client.post("/v1/peers/me/keys",
                data=json.dumps({k: v for k, v in req.items() if not k.startswith("_")}),
                content_type="application/json",
                HTTP_X_KEY_COSIG=keys.co_sign(old_seed, req), **_auth(p["peer_id"]))

    listed = client.get("/v1/peers/me/keys", **_auth(p["peer_id"])).json()
    assert len(listed) == 2, listed
    assert {k["active"] for k in listed} == {True, False}
    revoked = next(k for k in listed if not k["active"])
    assert revoked["revoked_at"] is not None, "the list hides the grace-window cutoff"


# ================================================================== agent cards

# The byte-exact RFC-8785 payload is a captured known answer from the independent Python
# conformance oracle (tools/a2a-conformance/, jwcrypto + rfc8785). If `cards.jcs_payload` does
# not byte-match it, no external A2A verifier accepts a card this server publishes.
ORACLE_JCS = (
    '{"capabilities":{"pushNotifications":false,"streaming":true},'
    '"defaultInputModes":["application/json"],"defaultOutputModes":["application/json"],'
    '"description":"kxxx","name":"kxxx","preferredTransport":"JSONRPC",'
    '"protocolVersion":"0.3.0","skills":['
    '{"description":"translate text","id":"11111111-1111-1111-1111-111111111111",'
    '"name":"translate","tags":["text","nlp"]},'
    '{"description":"","id":"0447bf92-25fb-5230-b531-5a0d9c00d8c7",'
    '"name":"summarize","tags":[]}],'
    '"url":"https://meshy.example/peers/a2b5be8f-aa4f-5634-8b28-97de57204a14",'
    '"version":"1.0.0"}'
)


def _fixture_card() -> dict:
    """Return the fixed card whose known-answer bytes are pinned above."""
    return cards.build_card(
        "mentee-a40c72b89050", "kxxx", "a2b5be8f-aa4f-5634-8b28-97de57204a14",
        "https://meshy.example",
        [{"id": "11111111-1111-1111-1111-111111111111", "name": "translate",
          "description": "translate text", "tags": ["text", "nlp"]},
         {"id": "0447bf92-25fb-5230-b531-5a0d9c00d8c7", "name": "summarize",
          "description": "", "tags": []}])


def test_card_jcs_payload_byte_matches_the_conformance_oracle():
    """A known-answer test: the card SHAPE and field-presence
    rules are as load-bearing as the signature, because they change the signed bytes."""
    assert cards.jcs_payload(_fixture_card()).decode("utf-8") == ORACLE_JCS


def test_a_bare_jcs_signature_does_not_verify_as_a_jws():
    """Signing the BARE JCS must NOT pass as a JWS, or MeshyChat would accept cards no external
    JOSE verifier accepts.

    Control: the SAME key over the SAME card, signed as a proper detached JWS, verifies.
    """
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    card = _fixture_card()

    proper = cards.sign_card(card, seed, "kid", "https://meshy.example/x/jwks.json")
    cards.verify_card(proper, pub)          # control: raises on failure

    payload = cards.jcs_payload(card)
    forged = dict(proper)
    forged["signatures"] = [{"protected": proper["signatures"][0]["protected"],
                             "signature": P.b64u(P.sign(seed, payload))}]
    with pytest.raises(cards.CardVerifyError):
        cards.verify_card(forged, pub)


def test_card_verify_rejects_a_hostile_alg_with_a_cryptographically_valid_signature():
    """The RED anchor matters: the signature below IS valid over
    the signing input, so the rejection can only be the alg check."""
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    card = _fixture_card()
    jku = "https://meshy.example/x/jwks.json"

    for bad_header in ({"alg": "none", "typ": "JOSE", "kid": "kid", "jku": jku},
                       {"alg": "HS256", "typ": "JOSE", "kid": "kid", "jku": jku},
                       {"alg": "EdDSA", "typ": "JWT", "kid": "kid", "jku": jku}):
        protected_b64 = P.b64u(json.dumps(bad_header, separators=(",", ":")).encode())
        payload_b64 = P.b64u(cards.jcs_payload(card))
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        hostile = dict(card)
        hostile["signatures"] = [{"protected": protected_b64,
                                  "signature": P.b64u(P.sign(seed, signing_input))}]
        # RED anchor: the raw signature really does verify over these bytes.
        assert P.verify_strict(pub, signing_input,
                               P.b64u_decode(hostile["signatures"][0]["signature"]))
        with pytest.raises(cards.CardVerifyError):
            cards.verify_card(hostile, pub)

    # Control: a well-formed header over the same card passes.
    cards.verify_card(cards.sign_card(card, seed, "kid", jku), pub)


def test_card_verify_rejects_a_second_trailing_signature():
    """A republished card must not pass on signatures[0]."""
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    signed = cards.sign_card(_fixture_card(), seed, "kid", "https://meshy.example/x/jwks.json")
    cards.verify_card(signed, pub)                       # control
    doubled = dict(signed)
    doubled["signatures"] = [signed["signatures"][0], dict(signed["signatures"][0])]
    with pytest.raises(cards.CardVerifyError):
        cards.verify_card(doubled, pub)
    empty = dict(signed)
    empty["signatures"] = []
    with pytest.raises(cards.CardVerifyError):
        cards.verify_card(empty, pub)


def test_a_card_with_the_wrong_jku_is_rejected(owner_conn, app_conn, client):
    """The pin stops a peer pointing external verifiers at
    a JWKS it controls.

    POSITIVE CONTROL, asserted FIRST: the same card signed with the SERVER's expected
    jku must publish (204) and be served back. Without it, the rejection could be an
    unsigned card, a missing key, a bad route or a broken verifier, and the test would
    still be green.
    """
    p = _seed_peer(owner_conn, "jku")
    seed = _bootstrap_key(app_conn, p["peer_id"])
    key = keys.find_active(app_conn, p["peer_id"])

    card = cards.build_card(p["name"], p["display_name"], str(p["peer_id"]), BASE_URL, [])
    good_jku = f"{BASE_URL}/agents/{p['peer_id']}/jwks.json"
    assert cards.expected_jku(BASE_URL + "/", p["peer_id"]) == good_jku

    # --- control -------------------------------------------------------------
    good = cards.sign_card(card, seed, key.kid, good_jku)
    r = client.post("/v1/peers/me/agent-card", data=json.dumps(good),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 204, r.content

    served = client.get(f"/agents/{p['peer_id']}/agent-card.json")
    assert served.status_code == 200, served.content
    assert served.json()["signatures"] == good["signatures"]

    # --- the rejection -------------------------------------------------------
    for hostile_jku in ("https://evil.example/agents/x/jwks.json",
                        f"http://meshy.test/agents/{p['peer_id']}/jwks.json",
                        f"{BASE_URL}/agents/{uuid.uuid4()}/jwks.json"):
        bad = cards.sign_card(card, seed, key.kid, hostile_jku)
        # The signature itself is perfectly valid - only the pin is wrong.
        cards.verify_card(bad, P.public_key_of(seed))
        r = client.post("/v1/peers/me/agent-card", data=json.dumps(bad),
                        content_type="application/json", **_auth(p["peer_id"]))
        assert r.status_code == 400, (hostile_jku, r.content)
        assert r.json()["error"] == "E_VALIDATION"
        assert r.json()["message"] == f"card protected `jku` must be {good_jku}"

    # The stored card is still the good one: a rejected publish must not overwrite.
    served = client.get(f"/agents/{p['peer_id']}/agent-card.json")
    assert served.json()["signatures"] == good["signatures"]


def test_a_card_whose_kid_is_not_the_active_kid_is_rejected(owner_conn, app_conn, client):
    """Control: the same card with the right kid publishes."""
    p = _seed_peer(owner_conn, "cardkid")
    seed = _bootstrap_key(app_conn, p["peer_id"])
    key = keys.find_active(app_conn, p["peer_id"])
    jku = f"{BASE_URL}/agents/{p['peer_id']}/jwks.json"
    card = cards.build_card(p["name"], p["display_name"], str(p["peer_id"]), BASE_URL, [])

    ok = cards.sign_card(card, seed, key.kid, jku)
    assert client.post("/v1/peers/me/agent-card", data=json.dumps(ok),
                       content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 204

    bad = cards.sign_card(card, seed, "0000000000000000", jku)
    r = client.post("/v1/peers/me/agent-card", data=json.dumps(bad),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 400, r.content
    assert r.json()["message"] == "card protected `kid` does not match your active signing key"


def test_a_card_signed_by_the_wrong_key_is_rejected(owner_conn, app_conn, client):
    """Control: the right key, same card, publishes."""
    p = _seed_peer(owner_conn, "cardkey")
    seed = _bootstrap_key(app_conn, p["peer_id"])
    key = keys.find_active(app_conn, p["peer_id"])
    jku = f"{BASE_URL}/agents/{p['peer_id']}/jwks.json"
    card = cards.build_card(p["name"], p["display_name"], str(p["peer_id"]), BASE_URL, [])

    assert client.post("/v1/peers/me/agent-card",
                       data=json.dumps(cards.sign_card(card, seed, key.kid, jku)),
                       content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 204

    imposter = cards.sign_card(card, os.urandom(32), key.kid, jku)
    r = client.post("/v1/peers/me/agent-card", data=json.dumps(imposter),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 400, r.content
    assert r.json()["message"] == "card signature does not verify under your active key"


def test_publishing_a_card_without_an_active_key_is_rejected(owner_conn, app_conn, client):
    """Control: after the peer gets a key, the same card path
    reaches the signature check instead (a different 400 message), proving the first
    rejection was the missing key and not the card."""
    p = _seed_peer(owner_conn, "nokey")
    card = cards.build_card(p["name"], p["display_name"], str(p["peer_id"]), BASE_URL, [])
    unsigned = json.dumps(card)

    r = client.post("/v1/peers/me/agent-card", data=unsigned,
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 400 and r.json()["message"] == "peer has no active signing key"

    _bootstrap_key(app_conn, p["peer_id"])
    r = client.post("/v1/peers/me/agent-card", data=unsigned,
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 400
    assert r.json()["message"] == "card signature does not verify under your active key"


def test_jwks_serves_the_active_key_only(owner_conn, app_conn, client):
    """The active key is served alone. The negative here is that a superseded key - still inside
    its 24h grace window and
    still resolvable by `find_by_kid` - does NOT appear. The grace-window test above is
    the control that proves the old key really is still live for verification, so this
    is a difference between the two lookups, not a key that simply vanished.
    """
    p = _seed_peer(owner_conn, "jwks")

    empty = client.get(f"/agents/{p['peer_id']}/jwks.json")
    assert empty.status_code == 200, empty.content
    assert empty.json() == {"keys": []}, "a keyless peer must be 200 with an empty set"

    old_seed = _bootstrap_key(app_conn, p["peer_id"])
    old_kid = keys.find_active(app_conn, p["peer_id"]).kid

    r = client.get(f"/agents/{p['peer_id']}/jwks.json")
    assert r.status_code == 200
    assert len(r.json()["keys"]) == 1
    jwk = r.json()["keys"][0]
    assert jwk == {"kty": "OKP", "crv": "Ed25519", "x": P.b64u(P.public_key_of(old_seed)),
                   "kid": old_kid, "alg": "EdDSA", "use": "sig"}

    new_seed = os.urandom(32)
    req = keys.build_publish_request(new_seed, p["peer_id"])
    assert client.post("/v1/peers/me/keys",
                       data=json.dumps({k: v for k, v in req.items()
                                        if not k.startswith("_")}),
                       content_type="application/json",
                       HTTP_X_KEY_COSIG=keys.co_sign(old_seed, req),
                       **_auth(p["peer_id"])).status_code == 201

    # Control: the old key IS still resolvable for verification...
    assert keys.find_by_kid(app_conn, p["peer_id"], old_kid) is not None
    # ...and yet JWKS serves only the new one.
    served = client.get(f"/agents/{p['peer_id']}/jwks.json").json()["keys"]
    assert len(served) == 1, served
    assert served[0]["kid"] != old_kid
    assert served[0]["x"] == P.b64u(P.public_key_of(new_seed))


def test_public_discovery_routes_need_no_token(owner_conn, app_conn, client):
    """Both are on the PUBLIC router, because an external A2A verifier resolving a card's
    `jku` holds no peer JWT.

    Control: the peer-authenticated publish route on the same app DOES 401 without a
    token, so "no token needed" is a property of these two routes and not of the test
    client skipping authentication everywhere.
    """
    p = _seed_peer(owner_conn, "public")
    seed = _bootstrap_key(app_conn, p["peer_id"])
    key = keys.find_active(app_conn, p["peer_id"])
    jku = f"{BASE_URL}/agents/{p['peer_id']}/jwks.json"
    signed = cards.sign_card(
        cards.build_card(p["name"], p["display_name"], str(p["peer_id"]), BASE_URL, []),
        seed, key.kid, jku)
    assert client.post("/v1/peers/me/agent-card", data=json.dumps(signed),
                       content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 204

    assert client.get(f"/agents/{p['peer_id']}/jwks.json").status_code == 200
    assert client.get(f"/agents/{p['peer_id']}/agent-card.json").status_code == 200
    assert client.post("/v1/peers/me/agent-card", data=json.dumps(signed),
                       content_type="application/json").status_code == 401

    # A peer that published nothing is a 404 on the card route.
    other = _seed_peer(owner_conn, "public2")
    assert client.get(f"/agents/{other['peer_id']}/agent-card.json").status_code == 404

    # A MALFORMED id is a different answer from an unknown one. The route takes any string, and
    # the view's UUID parse answers 400, not 404. Asserting both in one
    # place keeps the two from collapsing into "some 4xx".
    bad = client.get("/agents/not-a-uuid/agent-card.json")
    assert bad.status_code == 400, bad.content
    assert bad.json()["error"] == "E_VALIDATION"
    assert client.get("/agents/not-a-uuid/jwks.json").status_code == 400
    assert client.get("/v1/peers/not-a-uuid/keys/active",
                      **_auth(p["peer_id"])).status_code == 400


def test_a_served_card_verifies_end_to_end_the_way_an_external_verifier_would(
        owner_conn, app_conn, client):
    """The whole point of the card surface: fetch the card, fetch the JWKS its `jku`
    names, pick the key by `kid`, reconstruct the JWS signing input and verify.

    NEGATIVE CONTROL: one byte tampered in the served card must break it, so the
    verification is reading the payload and not just the signature blob.
    """
    p = _seed_peer(owner_conn, "e2e")
    seed = _bootstrap_key(app_conn, p["peer_id"])
    key = keys.find_active(app_conn, p["peer_id"])
    jku = cards.expected_jku(BASE_URL, p["peer_id"])
    signed = cards.sign_card(
        cards.build_card(p["name"], p["display_name"], str(p["peer_id"]), BASE_URL,
                         [{"id": str(uuid.uuid4()), "name": "translate",
                           "description": "translate text", "tags": ["text"]}]),
        seed, key.kid, jku)
    assert client.post("/v1/peers/me/agent-card", data=json.dumps(signed),
                       content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 204

    served = client.get(f"/agents/{p['peer_id']}/agent-card.json").json()
    header = cards.protected_header(served)
    assert header["jku"] == jku and header["alg"] == "EdDSA" and header["typ"] == "JOSE"

    # Resolve the key the way an external verifier does: follow the jku, match the kid.
    jwks_path = header["jku"][len(BASE_URL):]
    jwks_doc = client.get(jwks_path).json()
    jwk = next(k for k in jwks_doc["keys"] if k["kid"] == header["kid"])
    raw = P.b64u_decode(jwk["x"])

    signing_input = (f"{served['signatures'][0]['protected']}."
                     f"{P.b64u(cards.jcs_payload(served))}").encode("ascii")
    sig = P.b64u_decode(served["signatures"][0]["signature"])
    assert P.verify_strict(raw, signing_input, sig) is True

    tampered = json.loads(json.dumps(served))
    tampered["skills"][0]["name"] = "translatE"
    tampered_input = (f"{tampered['signatures'][0]['protected']}."
                      f"{P.b64u(cards.jcs_payload(tampered))}").encode("ascii")
    assert P.verify_strict(raw, tampered_input, sig) is False
    with pytest.raises(cards.CardVerifyError):
        cards.verify_card(tampered, raw)


def test_publishing_a_card_replaces_the_previous_one(owner_conn, app_conn, client):
    """ON CONFLICT (peer_id) DO UPDATE keeps one card per peer."""
    p = _seed_peer(owner_conn, "upsert")
    seed = _bootstrap_key(app_conn, p["peer_id"])
    key = keys.find_active(app_conn, p["peer_id"])
    jku = cards.expected_jku(BASE_URL, p["peer_id"])

    first = cards.sign_card(
        cards.build_card(p["name"], p["display_name"], str(p["peer_id"]), BASE_URL, []),
        seed, key.kid, jku)
    second = cards.sign_card(
        cards.build_card(p["name"], p["display_name"], str(p["peer_id"]), BASE_URL,
                         [{"id": str(uuid.uuid4()), "name": "s", "description": "d",
                           "tags": []}]),
        seed, key.kid, jku)
    for card in (first, second):
        assert client.post("/v1/peers/me/agent-card", data=json.dumps(card),
                           content_type="application/json",
                           **_auth(p["peer_id"])).status_code == 204

    served = client.get(f"/agents/{p['peer_id']}/agent-card.json").json()
    assert len(served["skills"]) == 1
    assert cards.protocol_hash(served) == cards.protocol_hash(second) != \
        cards.protocol_hash(first)

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_cards WHERE peer_id=%s", (p["peer_id"],))
        assert cur.fetchone()[0] == 1


# ====================================================================== route scaffolding

#: Every route this app mounts, a method it does not route, and the Allow it answers.
_WRONG_METHOD = [
    ("post", "/v1/peers/presence", "GET"),
    ("get", "/v1/peers/me/presence", "POST"),
    ("delete", "/v1/peers/me/keys", "POST, GET"),
    ("get", "/v1/peers/me/keys/some-kid", "DELETE"),
    ("post", f"/v1/peers/{uuid.uuid4()}/keys/active", "GET"),
    ("post", f"/agents/{uuid.uuid4()}/jwks.json", "GET"),
    ("post", f"/agents/{uuid.uuid4()}/agent-card.json", "GET"),
    ("get", "/v1/peers/me/agent-card", "POST"),
    ("post", "/v1/peers/me/invocation-webhook", "PUT, DELETE"),
    ("delete", "/v1/peers", "POST, GET"),
    ("get", f"/v1/peers/{uuid.uuid4()}/keys", "POST"),
    ("get", f"/v1/peers/{uuid.uuid4()}/tokens", "POST"),
]


@pytest.mark.parametrize("method,path,allow", _WRONG_METHOD)
def test_a_wrong_method_is_405_with_the_routes_allow_and_no_body(client, method, path, allow):
    """A wrong method returns 405, an `Allow` naming the routed methods in route order, an
    empty body, and no credential needed to learn it (the check precedes authentication).
    One route wrapper answers it for all twelve views, peer and owner alike."""
    r = getattr(client, method)(path)
    assert r.status_code == 405, (path, r.status_code, r.content)
    assert r["Allow"] == allow
    assert r.content == b""


def test_a_non_ascii_co_signature_on_the_peer_route_is_401_before_the_body_is_read(
        owner_conn, client):
    """On POST /v1/peers/me/keys, the co-signature check runs after the token and before the
    body, so a non-ASCII X-Key-CoSig with an unparseable body is the 401, not the 400.
    Control: the same request with an ASCII co-signature reaches the body and is the 400."""
    p = _seed_peer(owner_conn, "cosig-ascii")
    r = client.post("/v1/peers/me/keys", data=b"{not json", content_type="application/json",
                    HTTP_X_KEY_COSIG="x", **_auth(p["peer_id"]))
    assert r.status_code == 400 and r.json()["error"] == "E_VALIDATION", r.content
    r = client.post("/v1/peers/me/keys", data=b"{not json", content_type="application/json",
                    HTTP_X_KEY_COSIG="é", **_auth(p["peer_id"]))
    assert r.status_code == 401, r.content
    assert r.json() == {"error": "E_AUTH_INVALID", "message": "X-Key-CoSig must be ASCII"}


def test_peer_and_owner_routes_share_one_wrapper_one_co_signature_check_and_one_201_body():
    """The owner surface is `_route` under a second name, and both key-publish routes read
    X-Key-CoSig and answer their 201 through one helper each, so the two copies cannot
    drift apart."""
    from apps.peers import views
    assert views._owner_route is views._route
    source = Path(views.__file__).read_text(encoding="utf-8")
    assert source.count('"X-Key-CoSig must be ASCII"') == 1
    assert source.count('"created_at": key.view()["created_at"]') == 1
    assert "_method(" not in source


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
