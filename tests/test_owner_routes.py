"""Owner routes covered here (F05, divergence 016):

    POST /v1/dashboard/pairing/generate
    POST /v1/peers/{peer_id}/tokens
    POST|GET /v1/owner/human-keys, DELETE /v1/owner/human-keys/{kid}
    POST|GET /v1/peers
    POST /v1/peers/{peer_id}/keys
    POST /v1/auth/redeem-invitation
    DELETE /v1/peers/{peer_id}            (F35, OI-67)
    POST /v1/invites                      (F35, OI-67)

Each section was written BEFORE its route and seen to fail on the old code, and each route
has one named mutant that turns a test here red; the runs, their exit codes and the mutants
are in the commit that adds each section. Every refusal sits beside the control one
variable away (docs/V1-DESIGN-RULES.md C1), and state is read back from the database, not
from what the response said it did.

The last two were added on 2026-10-03 (the operator's ruling D-2026-10-03-1, sections 8 and
9). The redeem tests of section 7 still write their invitation rows as fixtures, so each keeps
its exact code and counts; section 9 proves a MINTED code redeems.

Run:  .venv/Scripts/python.exe -m pytest tests/test_owner_routes.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as dt
import json
import os
import uuid

import pytest

from tests.test_onboard import (  # noqa: F401 - fixtures are used by name; sets up Django
    DSN,
    PRESENCE,
    _fresh_limiters,
    _pat,
    claims_of,
    csrf_for,
    outcome,
    owner_conn,
    owner_sign_in,
    post,
    redeem,
    seed_signer,
    server,
    signed_in_owner,
)
from django.test import Client, override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.authapi.didkey import to_did_key  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402

PAIRING = "/v1/dashboard/pairing/generate"


def _bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _cookie_post(client, path: str, payload=None):
    """An owner-cookie write, with the CSRF token the session learns from /v1/auth/me."""
    return post(client, path, payload, HTTP_X_CSRF_TOKEN=csrf_for(client))


def _keyless_peer(conn, owner_id, *, status: str = "active") -> uuid.UUID:
    peer_id = uuid.uuid4()
    conn.execute("INSERT INTO peers (id, owner_id, name, status) VALUES (%s, %s, %s, %s)",
                 (peer_id, owner_id, f"own-{peer_id.hex[:8]}", status))
    return peer_id


def _parse_rfc3339(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


# ================================================ 1. POST /v1/dashboard/pairing/generate

def test_pairing_generate_mints_a_code_a_node_can_redeem(server, owner_conn):
    """The dashboard pairing path uses the same table and semantics as POST /v1/auth/onetime, on the
    path the dashboard wizard calls. The code it returns must onboard a node."""
    signer, owner = signed_in_owner(owner_conn, "pair-gen")
    before = dt.datetime.now(dt.timezone.utc)
    r = post(owner, PAIRING, HTTP_X_CSRF_TOKEN=csrf_for(owner))
    assert r.status_code == 201, r.content
    body = r.json()
    assert list(body) == ["code", "expires_at", "deep_link"]
    assert body["deep_link"] == "agentmeshy-mentee://onboard?code=" + body["code"]
    # The pairing code has the same 300 s TTL as onetime.
    ttl = (_parse_rfc3339(body["expires_at"]) - before).total_seconds()
    assert 295 <= ttl <= 305, ttl
    row = owner_conn.execute("SELECT owner_id, used_at FROM pairing_codes WHERE code = %s",
                             (body["code"],)).fetchone()
    assert row == (signer["owner_id"], None)

    redeemed, _ = redeem(body["code"])
    assert redeemed.status_code == 201, redeemed.content
    assert redeemed.json()["owner_id"] == str(signer["owner_id"])


def test_pairing_generate_needs_an_owner_csrf_and_both_scopes(server, owner_conn):
    """A PAT needs peers:write AND tokens:mint; a cookie needs CSRF.
    Each refusal is paired with the credential one variable away that is admitted."""
    signer, owner = signed_in_owner(owner_conn, "pair-auth")
    assert outcome(post(Client(), PAIRING)) == (401, "E_AUTH_MISSING")
    assert outcome(post(owner, PAIRING)) == (403, "E_CSRF_REQUIRED")
    for scopes, want in (([], (403, "E_SCOPE_REQUIRED")),
                         (["peers:write"], (403, "E_SCOPE_REQUIRED")),
                         (["tokens:mint"], (403, "E_SCOPE_REQUIRED")),
                         (["peers:write", "tokens:mint"], (201, None))):
        pat = _pat(owner_conn, signer["owner_id"], scopes)
        assert outcome(post(Client(), PAIRING, **_bearer(pat))) == want, scopes
    # The control for the CSRF refusal: the same cookie, with its token.
    assert post(owner, PAIRING, HTTP_X_CSRF_TOKEN=csrf_for(owner)).status_code == 201


def test_pairing_generate_is_post_only(server, owner_conn):
    """The pairing route accepts POST only; any other method is 405."""
    _, owner = signed_in_owner(owner_conn, "pair-method")
    assert owner.get(PAIRING).status_code == 405


# ================================================== 2. POST /v1/peers/{peer_id}/tokens

def _tokens(peer_id) -> str:
    return f"/v1/peers/{peer_id}/tokens"


def test_an_owner_mints_a_working_token_for_its_own_peer(server, owner_conn):
    """The token has sub as the OWNER, act.sub as the PEER, aud "peer", cnf as the
    peer's active key, 3600 s by default, 201 with the body in declaration order. The
    token then works on a peer route under DPoP, signed by that key (the positive half),
    and a proof by another key is refused (the control)."""
    signer, owner = signed_in_owner(owner_conn, "tok-journey")
    before = owner_conn.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0]
    r = _cookie_post(owner, _tokens(signer["peer_id"]), {"scopes": ["peers:read"]})
    assert r.status_code == 201, r.content
    body = r.json()
    assert list(body) == ["token", "token_type", "peer_id", "scopes", "expires_at",
                          "issued_at"]
    assert body["token_type"] == "Bearer"
    assert body["peer_id"] == str(signer["peer_id"])
    assert body["scopes"] == ["peers:read"]
    life = _parse_rfc3339(body["expires_at"]) - _parse_rfc3339(body["issued_at"])
    assert life == dt.timedelta(seconds=3600)
    claims = claims_of(body["token"])
    assert claims["sub"] == str(signer["owner_id"])
    assert claims["act"] == {"sub": str(signer["peer_id"])}
    assert claims["aud"] == "peer" and claims["scopes"] == ["peers:read"]
    assert claims["cnf"] == to_did_key(signer["pub"])
    assert claims["exp"] - claims["iat"] == 3600
    assert "lin" not in claims
    # :17-19 "returned ONCE, never persisted": no refresh token, no row of any kind.
    assert owner_conn.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0] == before

    node = Client()
    auth = _bearer(body["token"])
    with override_settings(MESHY_DPOP_ENFORCE=True):
        wrong = P.dpop_wire(os.urandom(32), "POST", PRESENCE, body["token"])
        r = node.post(PRESENCE, data="{}", content_type="application/json",
                      HTTP_DPOP=wrong, **auth)
        assert outcome(r) == (403, "E_FORBIDDEN")
        right = P.dpop_wire(signer["seed"], "POST", PRESENCE, body["token"])
        r = node.post(PRESENCE, data="{}", content_type="application/json",
                      HTTP_DPOP=right, **auth)
        assert r.status_code == 200, r.content


def test_token_lifetime_is_clamped_to_one_second_and_a_day(server, owner_conn):
    """ttl_secs defaults to 3600 and is clamped to 1 through 86400."""
    signer, owner = signed_in_owner(owner_conn, "tok-ttl")
    csrf = csrf_for(owner)
    for asked, want in ((120, 120), (10 ** 9, 86_400), (0, 1), (-5, 1)):
        r = post(owner, _tokens(signer["peer_id"]), {"ttl_secs": asked},
                 HTTP_X_CSRF_TOKEN=csrf)
        assert r.status_code == 201, (asked, r.content)
        claims = claims_of(r.json()["token"])
        assert claims["exp"] - claims["iat"] == want, asked


def test_a_token_is_only_minted_for_an_active_peer_the_caller_owns(server, owner_conn):
    """Another owner's peer, an unknown id and an inactive peer are
    one uniform 404 (no enumeration oracle); a malformed id is 400."""
    signer, owner = signed_in_owner(owner_conn, "tok-own")
    other = seed_signer(owner_conn, "tok-other")
    suspended = _keyless_peer(owner_conn, signer["owner_id"], status="suspended")
    deleted = _keyless_peer(owner_conn, signer["owner_id"], status="deleted")
    csrf = csrf_for(owner)
    for peer_id in (other["peer_id"], uuid.uuid4(), suspended, deleted):
        r = post(owner, _tokens(peer_id), {}, HTTP_X_CSRF_TOKEN=csrf)
        assert outcome(r) == (404, "E_NOT_FOUND"), peer_id
    assert outcome(post(owner, _tokens("not-a-uuid"), {}, HTTP_X_CSRF_TOKEN=csrf))[0] == 400
    # Control: the caller's own active peer, same session, same body.
    assert post(owner, _tokens(signer["peer_id"]), {},
                HTTP_X_CSRF_TOKEN=csrf).status_code == 201
    # And the other owner mints for ITS peer, so the 404 above was ownership, not the peer.
    other_owner = Client()
    r, _ = owner_sign_in(other_owner, other)
    assert r.status_code == 200, r.content
    assert _cookie_post(other_owner, _tokens(other["peer_id"]), {}).status_code == 201


def test_a_keyless_peer_gets_a_bearer_token_without_cnf(server, owner_conn):
    """No parseable active key means no cnf, not a refusal."""
    signer, owner = signed_in_owner(owner_conn, "tok-keyless")
    keyless = _keyless_peer(owner_conn, signer["owner_id"])
    csrf = csrf_for(owner)
    r = post(owner, _tokens(keyless), {}, HTTP_X_CSRF_TOKEN=csrf)
    assert r.status_code == 201, r.content
    assert "cnf" not in claims_of(r.json()["token"])
    # Control: the keyed peer of the same owner does get cnf.
    keyed = post(owner, _tokens(signer["peer_id"]), {}, HTTP_X_CSRF_TOKEN=csrf)
    assert claims_of(keyed.json()["token"])["cnf"] == to_did_key(signer["pub"])


def test_who_may_mint_and_which_scopes_a_pat_may_grant(server, owner_conn):
    """tokens:mint is required; a PAT may grant only a SUBSET of its
    own scopes (E_SCOPE_ESCALATION), while the human (cookie) may grant any."""
    signer, owner = signed_in_owner(owner_conn, "tok-scope")
    path = _tokens(signer["peer_id"])
    assert outcome(post(Client(), path, {})) == (401, "E_AUTH_MISSING")
    assert outcome(post(owner, path, {})) == (403, "E_CSRF_REQUIRED")
    no_mint = _pat(owner_conn, signer["owner_id"], ["peers:write"])
    assert outcome(post(Client(), path, {}, **_bearer(no_mint))) == (403, "E_SCOPE_REQUIRED")
    minter = _pat(owner_conn, signer["owner_id"], ["tokens:mint", "peers:read"])
    r = post(Client(), path, {"scopes": ["peers:read", "mesh:write"]}, **_bearer(minter))
    assert outcome(r) == (403, "E_SCOPE_ESCALATION")
    assert r.json()["message"] == ("peer token cannot carry a scope the minting "
                                   "credential does not hold: mesh:write")
    # Controls, one variable each: the same PAT asking only for what it holds; and the
    # cookie owner asking for the scope the PAT could not grant.
    r = post(Client(), path, {"scopes": ["peers:read"]}, **_bearer(minter))
    assert r.status_code == 201, r.content
    assert claims_of(r.json()["token"])["scopes"] == ["peers:read"]
    assert _cookie_post(owner, path, {"scopes": ["mesh:write"]}).status_code == 201


def test_no_signing_key_is_503_after_the_ownership_check(server, owner_conn):
    """The ownership check (404) comes before the signing-key check
    (503 E_SIGNING_DISABLED), so an unowned peer still answers 404 without a key."""
    signer, owner = signed_in_owner(owner_conn, "tok-nokey")
    csrf = csrf_for(owner)
    with authconfig.override_config(jwt_signing_key_pem=None):
        r = post(owner, _tokens(signer["peer_id"]), {}, HTTP_X_CSRF_TOKEN=csrf)
        assert outcome(r) == (503, "E_SIGNING_DISABLED")
        assert r.json()["message"] == ("peer-token minting is not configured in this "
                                       "environment")
        r = post(owner, _tokens(uuid.uuid4()), {}, HTTP_X_CSRF_TOKEN=csrf)
        assert outcome(r) == (404, "E_NOT_FOUND")
    assert post(owner, _tokens(signer["peer_id"]), {},
                HTTP_X_CSRF_TOKEN=csrf).status_code == 201


def test_token_request_body_shape(server, owner_conn):
    """A syntax error is 400, and a body that does not fit is 422."""
    signer, owner = signed_in_owner(owner_conn, "tok-body")
    path = _tokens(signer["peer_id"])
    csrf = csrf_for(owner)
    bad = owner.post(path, data="{", content_type="application/json",
                     HTTP_X_CSRF_TOKEN=csrf)
    assert outcome(bad)[0] == 400
    for payload in ([], {"scopes": "peers:read"}, {"scopes": [1]}, {"ttl_secs": "60"},
                    {"ttl_secs": 1.5}, {"ttl_secs": True}):
        r = post(owner, path, payload, HTTP_X_CSRF_TOKEN=csrf)
        assert outcome(r) == (422, "E_VALIDATION"), payload
    # Control: absent optional fields and explicit nulls are both fine.
    assert post(owner, path, {"scopes": None, "ttl_secs": None},
                HTTP_X_CSRF_TOKEN=csrf).status_code == 201


# ======================================= 3. /v1/owner/human-keys (POST, GET, DELETE {kid})

HUMAN_KEYS = "/v1/owner/human-keys"


def _second_peer(conn, owner_id) -> dict:
    """Another peer of the SAME owner, with an active key that is NOT on the owner-sign-in
    allowlist: the key a human registers to let that peer sign in as the owner."""
    peer_id, seed = uuid.uuid4(), os.urandom(32)
    pub = P.public_key_of(seed)
    conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s, %s, %s)",
                 (peer_id, owner_id, f"hk-{peer_id.hex[:8]}"))
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s, %s, %s, TRUE)", (peer_id, P.kid_for_peer_key(pub), P.b64u(pub)))
    return {"owner_id": owner_id, "peer_id": peer_id, "seed": seed, "pub": pub}


def _not_a_point() -> bytes:
    """32 bytes that cannot encode a valid verification key (no x for this y)."""
    from apps.authapi.onboard import is_edwards_point
    while True:
        raw = os.urandom(32)
        if not is_edwards_point(raw):
            return raw


def _delete(client, path: str, csrf: str | None = None, **extra):
    if csrf is not None:
        extra["HTTP_X_CSRF_TOKEN"] = csrf
    return client.delete(path, **extra)


def test_a_registered_human_key_signs_in_and_a_revoked_one_does_not(server, owner_conn):
    """This route IS the owner-sign-in allowlist. Registering a
    peer's key lets that peer sign in as the owner; revoking it takes that away; the
    listing never carries key bytes."""
    signer, owner = signed_in_owner(owner_conn, "hk-journey")
    second = _second_peer(owner_conn, signer["owner_id"])
    r, _ = owner_sign_in(Client(), second)
    assert outcome(r) == (403, "E_OWNER_KEY_UNREGISTERED"), "the premise: not yet allowed"

    r = _cookie_post(owner, HUMAN_KEYS, {"kid": "  laptop  ", "public_key":
                                         P.b64u(second["pub"]), "label": " the laptop "})
    assert r.status_code == 201, r.content
    assert r.json() == {"kid": "laptop", "label": "the laptop"}       # both trimmed
    assert owner_conn.execute(
        "SELECT kid, public_key, label, revoked_at FROM owner_human_keys "
        "WHERE owner_id = %s AND kid = 'laptop'", (signer["owner_id"],)).fetchone() == \
        ("laptop", P.b64u(second["pub"]), "the laptop", None)
    r, _ = owner_sign_in(Client(), second)
    assert r.status_code == 200, r.content

    listing = owner.get(HUMAN_KEYS)
    assert listing.status_code == 200, listing.content
    rows = listing.json()
    # Only this owner's keys are listed, ordered by created_at and kid.
    expected = [r[0] for r in owner_conn.execute(
        "SELECT kid FROM owner_human_keys WHERE owner_id = %s ORDER BY created_at, kid",
        (signer["owner_id"],)).fetchall()]
    assert [k["kid"] for k in rows] == expected and len(expected) == 2
    mine = next(k for k in rows if k["kid"] == "laptop")
    assert list(mine) == ["kid", "label", "created_at", "revoked_at"]
    assert mine["revoked_at"] is None
    assert all("public_key" not in k for k in rows)

    csrf = csrf_for(owner)
    assert _delete(owner, f"{HUMAN_KEYS}/laptop", csrf).status_code == 204
    r, _ = owner_sign_in(Client(), second)
    assert outcome(r) == (403, "E_OWNER_KEY_UNREGISTERED"), "a revoked key must not sign in"
    # Revoking again is 404 (no active key under that kid), not a second 204.
    assert outcome(_delete(owner, f"{HUMAN_KEYS}/laptop", csrf)) == (404, "E_NOT_FOUND")
    revoked = next(k for k in owner.get(HUMAN_KEYS).json() if k["kid"] == "laptop")
    assert revoked["revoked_at"] is not None


def test_an_api_key_may_never_manage_human_keys(server, owner_conn):
    """All three routes require a human: a PAT holding every scope is
    still refused, because registering a key is a path to full owner authority."""
    signer, owner = signed_in_owner(owner_conn, "hk-pat")
    pat = _pat(owner_conn, signer["owner_id"],
               ["peers:write", "tokens:mint", "peers:read", "owner:admin"])
    body = {"kid": "via-pat", "public_key": P.b64u(P.public_key_of(os.urandom(32)))}
    refusal = (403, "E_SCOPE_REQUIRED")
    assert outcome(post(Client(), HUMAN_KEYS, body, **_bearer(pat))) == refusal
    assert outcome(Client().get(HUMAN_KEYS, **_bearer(pat))) == refusal
    assert outcome(Client().delete(f"{HUMAN_KEYS}/via-pat", **_bearer(pat))) == refusal
    assert owner_conn.execute("SELECT count(*) FROM owner_human_keys WHERE kid = 'via-pat'"
                              ).fetchone()[0] == 0
    # Controls: no credential at all is 401; the cookie owner, same body, is admitted.
    assert outcome(post(Client(), HUMAN_KEYS, body)) == (401, "E_AUTH_MISSING")
    assert outcome(post(owner, HUMAN_KEYS, body)) == (403, "E_CSRF_REQUIRED")
    assert _cookie_post(owner, HUMAN_KEYS, body).status_code == 201


def test_human_key_registration_validates_before_it_writes(server, owner_conn):
    """Each malformed key is a 400 E_VALIDATION beside a control that differs in one field.
    Lengths are measured in bytes."""
    signer, owner = signed_in_owner(owner_conn, "hk-valid")
    csrf = csrf_for(owner)
    good = P.b64u(P.public_key_of(os.urandom(32)))

    def reg(**fields):
        payload = {"kid": "k", "public_key": good, **fields}
        return post(owner, HUMAN_KEYS, payload, HTTP_X_CSRF_TOKEN=csrf)

    for fields, message in (
            ({"kid": "   "}, "kid must not be empty"),
            ({"kid": "x" * 129}, "kid must be at most 128 characters"),
            ({"kid": "é" * 65}, "kid must be at most 128 characters"),   # 130 bytes
            ({"kid": "lbl", "label": "y" * 257}, "label must be at most 256 characters"),
            ({"kid": "pad", "public_key": good + "="}, "public_key must be base64url-no-pad"),
            ({"kid": "std", "public_key": good.replace("-", "+").replace("_", "/")
              if ("-" in good or "_" in good) else "+" + good[1:]},
             "public_key must be base64url-no-pad"),
            ({"kid": "short", "public_key": P.b64u(os.urandom(31))},
             "public_key must decode to 32 bytes"),
            ({"kid": "curve", "public_key": P.b64u(_not_a_point())},
             "public_key is not a valid Ed25519 point")):
        r = reg(**fields)
        assert outcome(r) == (400, "E_VALIDATION"), fields
        assert r.json()["message"] == message, fields
    assert owner_conn.execute("SELECT count(*) FROM owner_human_keys WHERE owner_id = %s",
                              (signer["owner_id"],)).fetchone()[0] == 1, \
        "only the signer's own key: no refused registration wrote a row"

    for payload in ({"public_key": good}, {"kid": "k"}, {"kid": 5, "public_key": good},
                    {"kid": "k", "public_key": good, "label": 5}):
        r = post(owner, HUMAN_KEYS, payload, HTTP_X_CSRF_TOKEN=csrf)
        assert outcome(r) == (422, "E_VALIDATION"), payload

    # Controls at the edges: 128 bytes and 256 bytes pass; a blank label is stored NULL.
    assert reg(kid="x" * 128).status_code == 201
    assert reg(kid="lbl-ok", label="y" * 256).status_code == 201
    r = reg(kid="blank-label", label="   ")
    assert r.status_code == 201 and r.json() == {"kid": "blank-label", "label": None}


def test_a_kid_is_unique_per_owner_and_invisible_across_owners(server, owner_conn):
    """UNIQUE (owner_id, kid) (sql/066) is 409; the same kid
    under ANOTHER owner is fine, and neither owner can list or revoke the other's."""
    signer, owner = signed_in_owner(owner_conn, "hk-uniq")
    other_signer, other = signed_in_owner(owner_conn, "hk-uniq-other")
    body = {"kid": "phone", "public_key": P.b64u(P.public_key_of(os.urandom(32)))}
    assert _cookie_post(owner, HUMAN_KEYS, body).status_code == 201
    r = _cookie_post(owner, HUMAN_KEYS, body)
    assert outcome(r) == (409, "E_CONFLICT")
    assert r.json()["message"] == ("a human key with this kid is already registered for "
                                   "this owner")
    assert _cookie_post(other, HUMAN_KEYS, body).status_code == 201   # control: per owner

    assert outcome(_delete(other, f"{HUMAN_KEYS}/phone-x", csrf_for(other))) == \
        (404, "E_NOT_FOUND")
    other_kids = {k["kid"] for k in other.get(HUMAN_KEYS).json()}
    assert "phone" in other_kids
    assert all(k["kid"] != "phone" or k["revoked_at"] is None
               for k in owner.get(HUMAN_KEYS).json())
    # Revoking MY "phone" leaves the other owner's "phone" active.
    assert _delete(owner, f"{HUMAN_KEYS}/phone", csrf_for(owner)).status_code == 204
    assert owner_conn.execute(
        "SELECT revoked_at IS NULL FROM owner_human_keys WHERE owner_id = %s AND kid = 'phone'",
        (other_signer["owner_id"],)).fetchone()[0] is True
    # And a DELETE needs CSRF like any cookie write.
    assert outcome(_delete(other, f"{HUMAN_KEYS}/phone")) == (403, "E_CSRF_REQUIRED")


# ============================================================ 4. POST and GET /v1/peers

PEERS = "/v1/peers"


def _usage(conn, owner_id) -> int:
    first_of_month = dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    row = conn.execute("SELECT counter FROM owner_usage WHERE owner_id = %s AND capability "
                       "= 'peers_per_owner' AND cycle_start = %s",
                       (owner_id, first_of_month)).fetchone()
    return 0 if row is None else row[0]


def test_an_owner_creates_a_peer_lists_it_and_mints_its_token(server, owner_conn):
    """Creating a peer returns 201 PeerView in declaration order, with the name trimmed,
    display_name and metadata stored as sent, one peers_per_owner unit reserved; GET lists
    the owner's non-deleted peers by created_at; the new (keyless) peer takes a token."""
    signer, owner = signed_in_owner(owner_conn, "peers-create")
    gone = _keyless_peer(owner_conn, signer["owner_id"], status="deleted")
    used = _usage(owner_conn, signer["owner_id"])
    r = _cookie_post(owner, PEERS, {"name": "  box  ", "display_name": " Box ",
                                    "metadata": {"rack": 3}})
    assert r.status_code == 201, r.content
    body = r.json()
    assert list(body) == ["id", "owner_id", "name", "display_name", "tier", "status",
                          "created_at"]
    assert body["owner_id"] == str(signer["owner_id"])
    assert (body["name"], body["display_name"]) == ("box", " Box ")
    assert (body["tier"], body["status"]) == ("free", "active")
    row = owner_conn.execute("SELECT owner_id, name, display_name, metadata FROM peers "
                             "WHERE id = %s", (body["id"],)).fetchone()
    assert row == (signer["owner_id"], "box", " Box ", {"rack": 3})
    assert _usage(owner_conn, signer["owner_id"]) == used + 1

    listed = owner.get(PEERS)
    assert listed.status_code == 200, listed.content
    ids = [p["id"] for p in listed.json()]
    expected = [str(r[0]) for r in owner_conn.execute(
        "SELECT id FROM peers WHERE owner_id = %s AND status <> 'deleted' "
        "ORDER BY created_at", (signer["owner_id"],)).fetchall()]
    assert ids == expected and body["id"] in ids and str(gone) not in ids
    assert list(listed.json()[0]) == list(body)

    token = _cookie_post(owner, f"/v1/peers/{body['id']}/tokens", {})
    assert token.status_code == 201, token.content
    assert claims_of(token.json()["token"])["act"] == {"sub": body["id"]}


def test_peers_create_needs_peers_write_and_list_needs_peers_read(server, owner_conn):
    """Each PAT is refused for the scope it lacks, beside the PAT one
    scope away that is admitted; the cookie owner passes both, GET needs no CSRF."""
    signer, owner = signed_in_owner(owner_conn, "peers-scope")
    body = {"name": "scoped"}
    assert outcome(post(Client(), PEERS, body)) == (401, "E_AUTH_MISSING")
    assert outcome(post(owner, PEERS, body)) == (403, "E_CSRF_REQUIRED")
    reader = _pat(owner_conn, signer["owner_id"], ["peers:read"])
    writer = _pat(owner_conn, signer["owner_id"], ["peers:write"])
    assert outcome(post(Client(), PEERS, body, **_bearer(reader))) == (403, "E_SCOPE_REQUIRED")
    assert post(Client(), PEERS, body, **_bearer(writer)).status_code == 201
    assert outcome(Client().get(PEERS, **_bearer(writer))) == (403, "E_SCOPE_REQUIRED")
    assert Client().get(PEERS, **_bearer(reader)).status_code == 200
    assert owner.get(PEERS).status_code == 200


def test_peers_create_validates_the_body_and_writes_nothing_on_refusal(server, owner_conn):
    """An empty name is 400, and a malformed CreatePeerRequest
    (a missing or mistyped field is 422). metadata is any JSON value."""
    signer, owner = signed_in_owner(owner_conn, "peers-valid")
    csrf = csrf_for(owner)
    count = lambda: owner_conn.execute("SELECT count(*) FROM peers WHERE owner_id = %s",  # noqa: E731
                                       (signer["owner_id"],)).fetchone()[0]
    before = count()
    r = post(owner, PEERS, {"name": "   "}, HTTP_X_CSRF_TOKEN=csrf)
    assert outcome(r) == (400, "E_VALIDATION")
    assert r.json()["message"] == "name must not be empty"
    for payload in ({}, {"name": 5}, {"name": "n", "display_name": 5}, []):
        assert outcome(post(owner, PEERS, payload, HTTP_X_CSRF_TOKEN=csrf)) == \
            (422, "E_VALIDATION"), payload
    assert count() == before, "no refusal wrote a peer"
    # Controls: a list, a number and null are all valid metadata.
    for meta in ([1, 2], 7, None):
        r = post(owner, PEERS, {"name": f"m-{meta}", "metadata": meta},
                 HTTP_X_CSRF_TOKEN=csrf)
        assert r.status_code == 201, (meta, r.content)
    assert count() == before + 3


def test_peers_create_is_refused_at_the_cap_and_rolls_back(server, owner_conn):
    """A full quota returns 402 E_QUOTA_EXCEEDED; no peer row and no
    unit spent. Control: the same request under a raised cap is 201."""
    signer, owner = signed_in_owner(owner_conn, "peers-quota")
    first_of_month = dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    owner_conn.execute(
        "INSERT INTO owner_usage (owner_id, capability, cycle_start, counter) "
        "VALUES (%s, 'peers_per_owner', %s, 5) ON CONFLICT (owner_id, capability, "
        "cycle_start) DO UPDATE SET counter = EXCLUDED.counter",
        (signer["owner_id"], first_of_month))
    r = _cookie_post(owner, PEERS, {"name": "one-too-many"})
    assert outcome(r) == (402, "E_QUOTA_EXCEEDED")
    assert r.json()["message"] == "quota exceeded for peers_per_owner: 6 would exceed cap 5"
    assert owner_conn.execute("SELECT count(*) FROM peers WHERE name = 'one-too-many' "
                              "AND owner_id = %s", (signer["owner_id"],)).fetchone()[0] == 0
    assert _usage(owner_conn, signer["owner_id"]) == 5
    owner_conn.execute("INSERT INTO owner_tier_overrides (owner_id, capability, value) "
                       "VALUES (%s, 'peers_per_owner', 10)", (signer["owner_id"],))
    assert _cookie_post(owner, PEERS, {"name": "one-too-many"}).status_code == 201
    assert _usage(owner_conn, signer["owner_id"]) == 6


# ============================================ 5. POST /v1/peers/{peer_id}/keys (break-glass)

def _peer_keys(peer_id) -> str:
    return f"/v1/peers/{peer_id}/keys"


def _publish_body(seed: bytes, peer_id) -> tuple[dict, dict]:
    """(the JSON body, the full request incl. the canonical bytes a co-signer signs)."""
    from apps.peers import keys as K
    req = K.build_publish_request(seed, peer_id)
    return {k: v for k, v in req.items() if not k.startswith("_")}, req


def _active_keys(conn, peer_id) -> list:
    return conn.execute("SELECT public_key FROM peer_keys WHERE peer_id = %s AND active "
                        "AND revoked_at IS NULL", (peer_id,)).fetchall()


def test_an_owner_publishes_the_first_key_of_a_keyless_peer(server, owner_conn):
    """owner_scoped=true lets a peer with NO key get its first one here; the peer route
    refuses that. It returns 201 {kid, active, created_at}, and
    the peer's next token is bound to it."""
    signer, owner = signed_in_owner(owner_conn, "pk-first")
    created = _cookie_post(owner, PEERS, {"name": "fresh"}).json()
    seed = os.urandom(32)
    body, _ = _publish_body(seed, created["id"])
    r = _cookie_post(owner, _peer_keys(created["id"]), body)
    assert r.status_code == 201, r.content
    out = r.json()
    assert list(out) == ["kid", "active", "created_at"] and out["active"] is True
    assert out["kid"] == P.kid_for_peer_key(P.public_key_of(seed))
    assert _active_keys(owner_conn, created["id"]) == [(P.b64u(P.public_key_of(seed)),)]
    tok = _cookie_post(owner, _tokens(created["id"]), {}).json()["token"]
    assert claims_of(tok)["cnf"] == to_did_key(P.public_key_of(seed))


def test_an_owner_rotation_needs_the_current_keys_co_signature(server, owner_conn):
    """The owner route also requires a peer WITH an active key to rotate only
    with X-Key-CoSig by that key. Without it 401 E_SIG_BAD and nothing changes; with it 201,
    and the superseded key leaves the active set (grace window, keys.revoke_old_active)."""
    signer, owner = signed_in_owner(owner_conn, "pk-rotate")
    new_seed = os.urandom(32)
    body, req = _publish_body(new_seed, signer["peer_id"])
    r = _cookie_post(owner, _peer_keys(signer["peer_id"]), body)
    assert outcome(r) == (401, "E_SIG_BAD")
    assert _active_keys(owner_conn, signer["peer_id"]) == [(P.b64u(signer["pub"]),)]
    from apps.peers import keys as K
    r = post(owner, _peer_keys(signer["peer_id"]), body, HTTP_X_CSRF_TOKEN=csrf_for(owner),
             HTTP_X_KEY_COSIG=K.co_sign(signer["seed"], req))
    assert r.status_code == 201, r.content
    assert _active_keys(owner_conn, signer["peer_id"]) == \
        [(P.b64u(P.public_key_of(new_seed)),)]


def test_an_owner_rotation_is_exempt_from_the_lineage_guard(server, owner_conn):
    """The owner publish carries no peer lineage and re-anchors nothing, so
    the migration-031 guard that refuses a lin-less PEER rotation over live refresh tokens
    (409) does not apply. Control: the same peer rotating on its own route with a lin-less
    token IS refused."""
    signer, owner = signed_in_owner(owner_conn, "pk-lineage")
    old_kid = P.kid_for_peer_key(signer["pub"])
    refresh_hash = os.urandom(32)
    owner_conn.execute(
        "INSERT INTO refresh_tokens (token_hash, peer_id, owner_id, expires_at, "
        "issued_under_kid) VALUES (%s, %s, %s, NOW() + interval '1 day', %s)",
        (refresh_hash, signer["peer_id"], signer["owner_id"], old_kid))
    token = _cookie_post(owner, _tokens(signer["peer_id"]), {}).json()["token"]
    assert "lin" not in claims_of(token)
    from apps.peers import keys as K
    body, req = _publish_body(os.urandom(32), signer["peer_id"])
    r = Client().post("/v1/peers/me/keys", data=__import__("json").dumps(body),
                      content_type="application/json", HTTP_X_KEY_COSIG=K.co_sign(
                          signer["seed"], req), **_bearer(token))
    assert outcome(r) == (409, "E_CONFLICT"), "the premise: a lin-less peer rotation"

    body, req = _publish_body(os.urandom(32), signer["peer_id"])
    r = post(owner, _peer_keys(signer["peer_id"]), body, HTTP_X_CSRF_TOKEN=csrf_for(owner),
             HTTP_X_KEY_COSIG=K.co_sign(signer["seed"], req))
    assert r.status_code == 201, r.content
    assert owner_conn.execute("SELECT issued_under_kid FROM refresh_tokens WHERE token_hash "
                              "= %s", (refresh_hash,)).fetchone()[0] == old_kid, \
        "an owner rotation re-anchors nothing"


def test_an_owner_publishes_only_for_its_own_active_peer(server, owner_conn):
    """Another owner's, an unknown or an inactive peer all receive one 404; a
    malformed id is 400; peers:write is required, and a non-ASCII X-Key-CoSig is 401."""
    signer, owner = signed_in_owner(owner_conn, "pk-own")
    other = seed_signer(owner_conn, "pk-other")
    suspended = _keyless_peer(owner_conn, signer["owner_id"], status="suspended")
    csrf = csrf_for(owner)
    for peer_id in (other["peer_id"], uuid.uuid4(), suspended):
        body, _ = _publish_body(os.urandom(32), peer_id)
        assert outcome(post(owner, _peer_keys(peer_id), body, HTTP_X_CSRF_TOKEN=csrf)) == \
            (404, "E_NOT_FOUND"), peer_id
    assert _active_keys(owner_conn, other["peer_id"]) == [(P.b64u(other["pub"]),)]
    body, _ = _publish_body(os.urandom(32), signer["peer_id"])
    assert outcome(post(owner, _peer_keys("nope"), body, HTTP_X_CSRF_TOKEN=csrf))[0] == 400

    keyless = _keyless_peer(owner_conn, signer["owner_id"])
    body, _ = _publish_body(os.urandom(32), keyless)
    reader = _pat(owner_conn, signer["owner_id"], ["peers:read", "tokens:mint"])
    writer = _pat(owner_conn, signer["owner_id"], ["peers:write"])
    assert outcome(post(Client(), _peer_keys(keyless), body)) == (401, "E_AUTH_MISSING")
    assert outcome(post(owner, _peer_keys(keyless), body)) == (403, "E_CSRF_REQUIRED")
    assert outcome(post(Client(), _peer_keys(keyless), body, **_bearer(reader))) == \
        (403, "E_SCOPE_REQUIRED")
    r = post(Client(), _peer_keys(keyless), body, HTTP_X_KEY_COSIG="é",
             **_bearer(writer))
    assert outcome(r) == (401, "E_AUTH_INVALID")
    # Control: the same PAT, the same body, an ASCII (absent) co-signature.
    assert post(Client(), _peer_keys(keyless), body, **_bearer(writer)).status_code == 201


# ============================================== 6. POST /v1/auth/redeem-invitation (no auth)

REDEEM = "/v1/auth/redeem-invitation"


def _invitation(conn, mentor_peer_id, *, code: str | None = None, max_uses: int = 1,
                uses: int = 0, expires_in: dt.timedelta | None = dt.timedelta(days=7),
                handle: str | None = None) -> str:
    """An invitation row as `POST /v1/invites` writes it, written
    here as the database owner so a test controls its code, uses and expiry."""
    code = code or f"Inv-{uuid.uuid4().hex[:10]}"
    expires = None if expires_in is None else dt.datetime.now(dt.timezone.utc) + expires_in
    conn.execute("INSERT INTO invitations (code, issuer_peer_id, owner_handle, expires_at, "
                 "max_uses, uses) VALUES (%s, %s, %s, %s, %s, %s)",
                 (code, mentor_peer_id, handle, expires, max_uses, uses))
    return code


def _redeem_invite(code, **extra):
    return post(Client(), REDEEM, {"code": code, **extra})


def _owners(conn) -> int:
    return conn.execute("SELECT count(*) FROM dashboard_users").fetchone()[0]


def test_an_invitation_mints_a_mentee_owner_peer_and_token(server, owner_conn):
    """Redeeming happens in one transaction: a synthetic owner, its mentee peer, the
    lesson subscription, the invitation's use, a keyless peer JWT and a refresh token, and
    the mentor's active key for lesson verification. The code is trimmed and matched
    case-insensitively (:48, :65)."""
    mentor = seed_signer(owner_conn, "inv-mentor")
    code = _invitation(owner_conn, mentor["peer_id"], code="MiXeD-Code-1", handle="Ada")
    r = _redeem_invite("  mixed-CODE-1 ")
    assert r.status_code == 201, r.content
    body = r.json()
    assert list(body) == ["peer_id", "owner_id", "jwt", "refresh_token", "mentor_peer_id",
                          "mentor_public_key", "mentor_kid", "jwt_expires_at"]
    assert body["mentor_peer_id"] == str(mentor["peer_id"])
    assert body["mentor_public_key"] == P.b64u(mentor["pub"])
    assert body["mentor_kid"] == P.kid_for_peer_key(mentor["pub"])
    claims = claims_of(body["jwt"])
    assert claims["sub"] == body["owner_id"] and claims["act"] == {"sub": body["peer_id"]}
    assert claims["aud"] == "peer" and claims["scopes"] == []
    assert "cnf" not in claims and "lin" not in claims
    assert claims["exp"] - claims["iat"] == 3600

    short = uuid.UUID(body["owner_id"]).hex[:12]
    owner_row = owner_conn.execute("SELECT email, display_name, role, plane FROM "
                                   "dashboard_users WHERE id = %s",
                                   (body["owner_id"],)).fetchone()
    assert owner_row == (f"mentee-{short}@invite.meshy", "Ada", "owner", "product")
    assert owner_conn.execute("SELECT owner_id, name, display_name FROM peers WHERE id = %s",
                              (body["peer_id"],)).fetchone() == \
        (uuid.UUID(body["owner_id"]), f"mentee-{short}", "Ada")
    assert owner_conn.execute(
        "SELECT count(*) FROM lesson_subscriptions WHERE mentor_peer_id = %s AND "
        "mentee_peer_id = %s AND mentee_owner_id = %s",
        (mentor["peer_id"], body["peer_id"], body["owner_id"])).fetchone()[0] == 1
    inv = owner_conn.execute("SELECT uses, redeemed_at IS NOT NULL, redeemed_by_owner FROM "
                             "invitations WHERE code = %s", (code,)).fetchone()
    assert inv == (1, True, uuid.UUID(body["owner_id"]))
    import hashlib
    rt = owner_conn.execute("SELECT peer_id, owner_id, issued_under_kid FROM refresh_tokens "
                            "WHERE token_hash = %s",
                            (hashlib.sha256(body["refresh_token"].encode()).digest(),)
                            ).fetchone()
    assert rt == (uuid.UUID(body["peer_id"]), uuid.UUID(body["owner_id"]), None)


def test_redeem_display_name_precedence(server, owner_conn):
    """The display name is the request's display_name, else the invitation's owner_handle,
    else "mentee-<short>"."""
    mentor = seed_signer(owner_conn, "inv-names")
    asked = _redeem_invite(_invitation(owner_conn, mentor["peer_id"], handle="Handle"),
                           display_name="Asked").json()
    handled = _redeem_invite(_invitation(owner_conn, mentor["peer_id"], handle="Handle")
                             ).json()
    neither = _redeem_invite(_invitation(owner_conn, mentor["peer_id"])).json()
    name = lambda b: owner_conn.execute("SELECT display_name FROM peers WHERE id = %s",  # noqa: E731
                                        (b["peer_id"],)).fetchone()[0]
    assert name(asked) == "Asked" and name(handled) == "Handle"
    assert name(neither) == f"mentee-{uuid.UUID(neither['owner_id']).hex[:12]}"


def test_optional_string_fields_refuse_a_wrong_type_and_accept_null(server, owner_conn):
    """Optional JSON text fields: a non-string `label` (human keys) or `display_name`
    (redeem) is 422 "invalid type for field `<name>`"; an explicit null is the absent case.
    Each refusal sits beside the same request with null, which succeeds."""
    signer, owner = signed_in_owner(owner_conn, "opt-fields")
    csrf = csrf_for(owner)
    good = P.b64u(P.public_key_of(os.urandom(32)))
    r = post(owner, HUMAN_KEYS, {"kid": "opt", "public_key": good, "label": 5},
             HTTP_X_CSRF_TOKEN=csrf)
    assert r.status_code == 422 and r.json() == {
        "error": "E_VALIDATION", "message": "invalid type for field `label`"}
    r = post(owner, HUMAN_KEYS, {"kid": "opt", "public_key": good, "label": None},
             HTTP_X_CSRF_TOKEN=csrf)
    assert r.status_code == 201 and r.json() == {"kid": "opt", "label": None}

    mentor = seed_signer(owner_conn, "opt-mentor")
    code = _invitation(owner_conn, mentor["peer_id"])
    r = _redeem_invite(code, display_name=["x"])
    assert r.status_code == 422 and r.json() == {
        "error": "E_VALIDATION", "message": "invalid type for field `display_name`"}
    assert _redeem_invite(code, display_name=None).status_code == 201


def test_an_invitation_has_as_many_seats_as_max_uses(server, owner_conn):
    """uses >= max_uses is 410 E_INVITATION_CONSUMED; redeemed_at and
    redeemed_by_owner keep the FIRST redeem (COALESCE in the redeem UPDATE)."""
    mentor = seed_signer(owner_conn, "inv-seats")
    code = _invitation(owner_conn, mentor["peer_id"], max_uses=2)
    first = _redeem_invite(code)
    second = _redeem_invite(code)
    assert (first.status_code, second.status_code) == (201, 201)
    owners = _owners(owner_conn)
    third = _redeem_invite(code)
    assert outcome(third) == (410, "E_INVITATION_CONSUMED")
    assert third.json()["message"] == "invitation already consumed"
    assert _owners(owner_conn) == owners, "a refused redeem creates no owner"
    assert owner_conn.execute("SELECT uses, redeemed_by_owner FROM invitations WHERE code "
                              "= %s", (code,)).fetchone() == \
        (2, uuid.UUID(first.json()["owner_id"]))


def test_expired_unknown_and_empty_codes_are_refused_without_writing(server, owner_conn):
    """An empty code is 404 "missing invitation code", an unknown code is 404 "invitation not
    found", expired 410 (checked BEFORE the seat count), each writing nothing; an invitation
    with no expiry never expires (the control)."""
    mentor = seed_signer(owner_conn, "inv-refuse")
    expired = _invitation(owner_conn, mentor["peer_id"], expires_in=dt.timedelta(seconds=-1))
    both = _invitation(owner_conn, mentor["peer_id"], expires_in=dt.timedelta(seconds=-1),
                       max_uses=1, uses=1)
    owners = _owners(owner_conn)
    for code, want, message in (
            ("   ", (404, "E_INVITATION_INVALID"), "missing invitation code"),
            ("no-such-code", (404, "E_INVITATION_INVALID"), "invitation not found"),
            (expired, (410, "E_INVITATION_EXPIRED"), "invitation expired"),
            (both, (410, "E_INVITATION_EXPIRED"), "invitation expired")):
        r = _redeem_invite(code)
        assert outcome(r) == want, code
        assert r.json()["message"] == message, code
    assert _owners(owner_conn) == owners
    assert owner_conn.execute("SELECT uses FROM invitations WHERE code = %s",
                              (expired,)).fetchone()[0] == 0
    forever = _invitation(owner_conn, mentor["peer_id"], expires_in=None)
    assert _redeem_invite(forever).status_code == 201


def test_a_mentor_without_a_key_rolls_the_whole_redeem_back(server, owner_conn):
    """No active mentor key is 422 E_MENTOR_KEY_MISSING AFTER the owner,
    peer, subscription and use were written, so all four roll back. Control: once the
    mentor has a key, the SAME code redeems."""
    owner_id, mentor_peer = uuid.uuid4(), uuid.uuid4()
    owner_conn.execute("INSERT INTO dashboard_users (id, email) VALUES (%s, %s)",
                       (owner_id, f"inv-keyless-{owner_id.hex[:8]}@example.test"))
    owner_conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s, %s, %s)",
                       (mentor_peer, owner_id, f"inv-keyless-{mentor_peer.hex[:8]}"))
    code = _invitation(owner_conn, mentor_peer)
    owners, peers = _owners(owner_conn), owner_conn.execute(
        "SELECT count(*) FROM peers").fetchone()[0]
    r = _redeem_invite(code)
    assert outcome(r) == (422, "E_MENTOR_KEY_MISSING")
    assert r.json()["message"] == "mentor has no active signing key"
    assert _owners(owner_conn) == owners
    assert owner_conn.execute("SELECT count(*) FROM peers").fetchone()[0] == peers
    assert owner_conn.execute("SELECT count(*) FROM lesson_subscriptions WHERE "
                              "mentor_peer_id = %s", (mentor_peer,)).fetchone()[0] == 0
    assert owner_conn.execute("SELECT uses FROM invitations WHERE code = %s",
                              (code,)).fetchone()[0] == 0
    pub = P.public_key_of(os.urandom(32))
    owner_conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) VALUES "
                       "(%s, %s, %s, TRUE)", (mentor_peer, P.kid_for_peer_key(pub), P.b64u(pub)))
    assert _redeem_invite(code).status_code == 201


def test_redeem_without_a_signing_key_is_503_and_consumes_nothing(server, owner_conn):
    """The signing key is checked before the code is read, so an unconfigured server never
    spends a seat. Request-body validation runs first: a body that does not fit is 422."""
    mentor = seed_signer(owner_conn, "inv-nokey")
    code = _invitation(owner_conn, mentor["peer_id"])
    with authconfig.override_config(jwt_signing_key_pem=None):
        r = _redeem_invite(code)
        assert outcome(r) == (503, "E_REDEEM_FAILED")
        assert outcome(post(Client(), REDEEM, [])) == (422, "E_VALIDATION")
        assert outcome(post(Client(), REDEEM, {"display_name": "x"})) == (422, "E_VALIDATION")
    assert owner_conn.execute("SELECT uses FROM invitations WHERE code = %s",
                              (code,)).fetchone()[0] == 0
    assert outcome(post(Client(), REDEEM, {"code": 5})) == (422, "E_VALIDATION")
    assert _redeem_invite(code).status_code == 201


# ======================================= 8. DELETE /v1/peers/{peer_id} (F35, OI-67)

def _peer(peer_id) -> str:
    return f"/v1/peers/{peer_id}"


def _peer_token(owner, peer_id) -> str:
    """A peer JWT for `peer_id`, minted by its owner through the tokens route (section 2)."""
    r = _cookie_post(owner, _tokens(peer_id), {"scopes": []})
    assert r.status_code == 201, r.content
    return r.json()["token"]


def _beat(token: str):
    return Client().post(PRESENCE, data="{}", content_type="application/json", **_bearer(token))


def test_an_owner_deletes_its_peer_and_the_peer_loses_its_standing_at_once(server, owner_conn):
    """A SOFT delete (`status = 'deleted'`, the row stays) returns 204, and the
    peer's live sessions revoked: here the `mesh_presence` row is deleted and the revocation
    NOTIFY is sent in the same transaction (the sockets close on it, apps/wsgw/revocation.py).
    CONTROL: the peer's token beats presence before the delete; after it the same token is
    refused. The owner's OTHER peer is untouched by both."""
    signer, owner = signed_in_owner(owner_conn, "del-own")
    victim = _second_peer(owner_conn, signer["owner_id"])
    token = _peer_token(owner, victim["peer_id"])
    other = _peer_token(owner, signer["peer_id"])
    assert _beat(token).status_code == 200, "control: the token works before the delete"
    assert _beat(other).status_code == 200
    present = "SELECT count(*) FROM mesh_presence WHERE peer_id = %s"
    assert owner_conn.execute(present, (victim["peer_id"],)).fetchone()[0] == 1

    owner_conn.execute("LISTEN meshy_peer_revoked")
    r = _delete(owner, _peer(victim["peer_id"]), csrf_for(owner))
    assert r.status_code == 204, r.content
    assert r.content == b""
    assert owner_conn.execute("SELECT status FROM peers WHERE id = %s",
                              (victim["peer_id"],)).fetchone() == ("deleted",)
    assert owner_conn.execute(present, (victim["peer_id"],)).fetchone()[0] == 0
    notes = [json.loads(n.payload) for n in owner_conn.notifies(timeout=2, stop_after=1)]
    assert notes == [{"peer_id": str(victim["peer_id"])}], notes
    # A token whose peer is not active is 403 E_FORBIDDEN from the commit onward.
    assert outcome(_beat(token)) == (403, "E_FORBIDDEN"), "the deleted peer's token stops at the commit"
    assert _beat(other).status_code == 200, "the owner's other peer keeps its standing"
    assert owner_conn.execute(present, (signer["peer_id"],)).fetchone()[0] == 1


def test_a_peer_is_deleted_only_by_its_own_owner(server, owner_conn):
    """The predicate is `WHERE id AND owner_id`, with no status term. Another owner's peer and a
    missing id are both 404 and change nothing; deleting one's own peer twice is 204 both times
    (the predicate has no status term); a malformed id is 400."""
    signer, owner = signed_in_owner(owner_conn, "del-who")
    stranger = seed_signer(owner_conn, "del-stranger")
    csrf = csrf_for(owner)
    assert outcome(_delete(owner, _peer(stranger["peer_id"]), csrf)) == (404, "E_NOT_FOUND")
    assert owner_conn.execute("SELECT status FROM peers WHERE id = %s",
                              (stranger["peer_id"],)).fetchone() == ("active",)
    assert outcome(_delete(owner, _peer(uuid.uuid4()), csrf)) == (404, "E_NOT_FOUND")
    assert _delete(owner, _peer("not-a-uuid"), csrf).status_code == 400
    mine = _keyless_peer(owner_conn, signer["owner_id"])
    assert _delete(owner, _peer(mine), csrf).status_code == 204, "control: one's own peer"
    assert _delete(owner, _peer(mine), csrf).status_code == 204, "again: no status term"


def test_peer_delete_needs_an_owner_csrf_and_peers_write(server, owner_conn):
    """PEERS_WRITE is required; a cookie needs CSRF. Each refusal sits beside
    the credential one variable away that is admitted. DELETE only: GET is 405."""
    signer, owner = signed_in_owner(owner_conn, "del-auth")
    targets = [_keyless_peer(owner_conn, signer["owner_id"]) for _ in range(3)]
    assert outcome(_delete(Client(), _peer(targets[0]))) == (401, "E_AUTH_MISSING")
    assert outcome(_delete(owner, _peer(targets[0]))) == (403, "E_CSRF_REQUIRED")
    read_only = _pat(owner_conn, signer["owner_id"], ["peers:read"])
    assert outcome(_delete(Client(), _peer(targets[0]), **_bearer(read_only))) == \
        (403, "E_SCOPE_REQUIRED")
    assert owner_conn.execute("SELECT status FROM peers WHERE id = %s",
                              (targets[0],)).fetchone() == ("active",)
    writer = _pat(owner_conn, signer["owner_id"], ["peers:write"])
    assert _delete(Client(), _peer(targets[1]), **_bearer(writer)).status_code == 204
    assert _delete(owner, _peer(targets[2]), csrf_for(owner)).status_code == 204
    assert owner.get(_peer(targets[0])).status_code == 405


# ============================================== 9. POST /v1/invites (F35, OI-67)

INVITES = "/v1/invites"


def _mint(token: str, payload=None):
    return post(Client(), INVITES, {} if payload is None else payload, **_bearer(token))


def test_a_peer_mints_an_invitation_that_redeems(server, owner_conn):
    """A PEER mints (AuthenticatedPeer); 201 {code, expires_at,
    max_uses} has the defaults (1 use, 168 h); the code is six words; the
    row names the minting peer. The point of the route: the code then REDEEMS at
    /v1/auth/redeem-invitation, which could only refuse while nothing minted (section 7)."""
    from apps.peers import invite_code
    signer, owner = signed_in_owner(owner_conn, "inv-mint")
    before = dt.datetime.now(dt.timezone.utc)
    r = _mint(_peer_token(owner, signer["peer_id"]), {"owner_handle": "Grace"})
    assert r.status_code == 201, r.content
    body = r.json()
    assert list(body) == ["code", "expires_at", "max_uses"]
    assert body["max_uses"] == 1
    hours = (_parse_rfc3339(body["expires_at"]) - before).total_seconds() / 3600
    assert 167.9 < hours < 168.1, hours
    words = body["code"].split("-")
    assert len(words) == 6 and set(words) <= set(invite_code.wordlist())
    row = owner_conn.execute("SELECT issuer_peer_id, owner_handle, max_uses, uses FROM "
                             "invitations WHERE code = %s", (body["code"],)).fetchone()
    assert row == (signer["peer_id"], "Grace", 1, 0)
    redeemed = _redeem_invite(body["code"])
    assert redeemed.status_code == 201, redeemed.content
    assert redeemed.json()["mentor_peer_id"] == str(signer["peer_id"])
    assert _redeem_invite(body["code"]).status_code != 201, "one use, as minted"


def test_invite_limits_are_clamped_not_refused(server, owner_conn):
    """max_uses is clamped to 1..50 and expires_in_hours to 1..720."""
    signer, owner = signed_in_owner(owner_conn, "inv-clamp")
    token = _peer_token(owner, signer["peer_id"])
    for asked, want_uses, want_hours in (({"max_uses": 0, "expires_in_hours": 0}, 1, 1),
                                         ({"max_uses": 99, "expires_in_hours": 9999}, 50, 720),
                                         ({"max_uses": 7, "expires_in_hours": 24}, 7, 24)):
        before = dt.datetime.now(dt.timezone.utc)
        r = _mint(token, asked)
        assert r.status_code == 201, (asked, r.content)
        hours = (_parse_rfc3339(r.json()["expires_at"]) - before).total_seconds() / 3600
        assert r.json()["max_uses"] == want_uses and abs(hours - want_hours) < 0.1, asked


def test_invite_body_shape_is_refused_before_anything_is_written(server, owner_conn):
    """Optional JSON number and text fields: a negative, a string, a float or a bool for a
    number, or a number for the handle, is 422 and writes no row. CONTROL: the same token with
    a well-formed body mints."""
    signer, owner = signed_in_owner(owner_conn, "inv-shape")
    token = _peer_token(owner, signer["peer_id"])
    count = "SELECT count(*) FROM invitations WHERE issuer_peer_id = %s"
    for bad in ({"max_uses": -1}, {"max_uses": "3"}, {"max_uses": 1.5}, {"max_uses": True},
                {"expires_in_hours": 2 ** 32}, {"owner_handle": 5}):
        assert outcome(_mint(token, bad)) == (422, "E_VALIDATION"), bad
    assert owner_conn.execute(count, (signer["peer_id"],)).fetchone()[0] == 0
    assert _mint(token, {"max_uses": 2}).status_code == 201
    assert owner_conn.execute(count, (signer["peer_id"],)).fetchone()[0] == 1


def test_only_a_peer_mints_and_only_by_post(server, owner_conn):
    """AuthenticatedPeer: no credential is 401, and an owner session (a cookie, not a peer JWT)
    is refused the same way; a deleted peer's token is 403 E_FORBIDDEN (see section 8 of this
    file). POST only."""
    signer, owner = signed_in_owner(owner_conn, "inv-auth")
    assert _mint_status(Client()) == 401
    assert _mint_status(owner, HTTP_X_CSRF_TOKEN=csrf_for(owner)) == 401
    victim = _second_peer(owner_conn, signer["owner_id"])
    token = _peer_token(owner, victim["peer_id"])
    assert _mint(token).status_code == 201, "control: the token mints while the peer is active"
    assert _delete(owner, _peer(victim["peer_id"]), csrf_for(owner)).status_code == 204
    assert outcome(_mint(token)) == (403, "E_FORBIDDEN"), "an inactive peer cannot mint"
    assert Client().get(INVITES, **_bearer(_peer_token(owner, signer["peer_id"]))
                        ).status_code == 405


def _mint_status(client, **extra) -> int:
    return post(client, INVITES, {}, **extra).status_code


def test_a_code_collision_draws_a_fresh_code_and_five_collisions_are_a_500(server, owner_conn,
                                                                       monkeypatch):
    """UNIQUE(code) is retried with a fresh code up to five times, and the sixth
    failure is a 500 whose body names no detail."""
    from apps.peers import invite_code
    signer, owner = signed_in_owner(owner_conn, "inv-collide")
    token = _peer_token(owner, signer["peer_id"])
    taken = _invitation(owner_conn, signer["peer_id"], code="taken-" + uuid.uuid4().hex[:8])
    fresh = "fresh-" + uuid.uuid4().hex[:8]
    draws = iter([taken, taken, fresh])
    monkeypatch.setattr(invite_code, "generate_code", lambda: next(draws))
    r = _mint(token)
    assert r.status_code == 201 and r.json()["code"] == fresh, r.content

    monkeypatch.setattr(invite_code, "generate_code", lambda: taken)
    r = _mint(token)
    assert r.status_code == 500 and r.json() == {"error": "E_INTERNAL",
                                                 "message": "internal error"}, r.content


def test_the_wordlist_meets_its_minimums():
    """The word list has at least 7,000 entries and 75 bits for six words; it is lowercase
    ASCII and unique; its bytes are pinned by sha256 and the exact count, so any edit to the list
    fails here."""
    import hashlib
    import math
    from apps.peers import invite_code
    words = invite_code.wordlist()
    assert len(words) == 7772 and math.log2(len(words)) * 6 >= 75.0
    assert len(set(words)) == len(words)
    raw = invite_code._PATH.read_bytes().replace(b"\r\n", b"\n")  # a CRLF checkout hashes as LF
    assert hashlib.sha256(raw).hexdigest().startswith("18586c092f641ecd")
    codes = {invite_code.generate_code() for _ in range(200)}
    assert len(codes) == 200, "200 draws from 77 bits do not repeat"


def test_a_deleted_peers_refresh_token_still_rotates_but_its_jwt_is_refused(server, owner_conn):
    """Pins what `delete_peer`'s docstring says. The delete does not revoke the peer's refresh
    tokens (`POST /v1/auth/refresh` reads the token and the peer's key, never `peers.status`),
    so a refresh token of a deleted peer still rotates into a fresh
    30-day successor that rotates again. Every JWT that yields is refused by the peer routes
    (403 E_FORBIDDEN), so on this server nothing can be done with it.
    CONTROL: the same route refuses a token that was never issued (401), so the 200 is the
    token's, not the route's."""
    import hashlib
    signer, owner = signed_in_owner(owner_conn, "del-refresh")
    victim = _second_peer(owner_conn, signer["owner_id"])
    raw = P.b64u(os.urandom(32))
    owner_conn.execute(
        "INSERT INTO refresh_tokens (token_hash, peer_id, owner_id, expires_at, lineage_id) "
        "VALUES (%s, %s, %s, NOW() + interval '30 days', %s)",
        (hashlib.sha256(raw.encode()).digest(), victim["peer_id"], victim["owner_id"], uuid.uuid4()))

    def rotate(token: str):
        return Client().post("/v1/auth/refresh", data=json.dumps({"refresh_token": token}),
                             content_type="application/json")

    assert outcome(rotate(P.b64u(os.urandom(32)))) == (401, "E_REFRESH_TOKEN_INVALID"), "control"
    assert _delete(owner, _peer(victim["peer_id"]), csrf_for(owner)).status_code == 204
    first = rotate(raw)
    assert first.status_code == 200, first.content
    body = first.json()
    assert outcome(_beat(body["jwt"])) == (403, "E_FORBIDDEN"), "the refreshed JWT is refused"
    lifetime = owner_conn.execute(
        "SELECT expires_at - NOW() > interval '29 days' FROM refresh_tokens WHERE token_hash = %s",
        (hashlib.sha256(body["refresh_token"].encode()).digest(),)).fetchone()[0]
    assert lifetime is True, "the successor has a fresh lifetime"
    assert rotate(body["refresh_token"]).status_code == 200, "and it rotates again"
