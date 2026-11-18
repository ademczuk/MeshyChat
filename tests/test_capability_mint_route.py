"""The mint: `POST /v1/memory/capabilities` (B07 slice 5; divergence 040).

Design docs/design/biscuit-capabilities.md 2.2 ("who mints"), 2.3 (the authority block), 3.1 (the
mint bullet), 4.2 (I2's grant half); Q2 (only namespace admins mint in v1), Q3 (an authority
lives 24 hours at most); critique C2 (a product-plane subject on shared/* is refused), C3 (the
mint writes the issuance register) and C9 (one namespace per token). T19 through the route: a
route-minted token is allowed, and a same-shape token signed by the same root but never minted
is refused.

Every refusal sits beside its passing control in the same test, differing in the one property
under test (docs/V1-DESIGN-RULES.md C1): "refused" is also what a broken route answers. And a
refused mint writes nothing: no register row.

The routes run through the project urlconf and middleware (tests/capability_routes.py). DPoP is
off here as in the whole suite (tests/conftest.py), except in the test that shows the mint, a
POST, needs a proof when it is on.

Run:  pytest tests/test_capability_mint_route.py -q
Needs the dev database (tests/conftest.py builds and drops its own) and CPython 3.13.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.memoryskills.permissions import Key  # noqa: E402
from tests.capability_routes import (  # noqa: E402
    MINT,
    admin_on,
    REVOKE,
    iso,
    peer_jwt,
    random_peer_id,
    routes_context,
    scan,
)
from tests.capability_world import ROOT_KID, utcnow  # noqa: E402

VALIDATION, FORBIDDEN = "E_VALIDATION", "E_FORBIDDEN"
DENIED_NS, UNCLAIMED = "E_PERMISSION_DENIED", "E_NAMESPACE_UNCLAIMED"
PLANE, UNKNOWN_PEER = "E_PRODUCT_NO_SHARED_GRANT", "E_UNKNOWN_PEER"
UNAVAILABLE = "E_CAPABILITY_UNAVAILABLE"
CHILD = ROOT / "tests" / "fixtures" / "capability_routes_child.py"


@pytest.fixture()
def routes():
    with routes_context() as r:
        yield r


def frozen_now(monkeypatch):
    """The mint's clock, held at a whole second near real time, so a test can ask for an
    expiry exactly 24 hours after it."""
    now = utcnow().replace(microsecond=0)
    monkeypatch.setattr("apps.memoryskills.capability_issue.current_time", lambda: now)
    return now


def register_message():
    from apps.memoryskills import capability
    return capability.CLAUSE_MESSAGES["register"]


# ================================================================== T19 through the route

def test_t19_a_route_minted_token_is_allowed_and_the_same_shape_never_minted_is_refused(routes):
    """2.5 (c), critique C3: a token is honoured only if this server's mint route recorded its
    authority id. The route-minted token is allowed (the positive control); a token of the same
    shape, signed by the SAME root key in the test but never minted, names the namespace's real
    owner as issuer and is refused by the register clause."""
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    token, tok, got = routes.minted(o, subject=a, ns=ns, rights=(("", "read"),))
    assert w.decide(token, a, ns, Key("k"))[0] == "ALLOW", w.decide(token, a, ns, Key("k"))
    forged = w.mint(issuer=o, subject=a, ns=ns, rights=(("", "read"),), register=False)
    assert w.decide(forged, a, ns, Key("k")) == ("E_CAPABILITY_DENIED", register_message())
    assert got.body["cap_id"] == tok.revocation_ids[0]
    assert len(routes.issue_rows(tok.revocation_ids[0])) == 1
    assert routes.issue_rows(forged.revocation_ids[0]) == []


def test_the_response_carries_the_token_once_and_says_what_it_minted(routes, monkeypatch):
    """The token is returned in the 201 and nowhere else; the response is not to be cached
    (RFC 9111 no-store, as an OAuth token response is), and it states what the token says."""
    w = routes.w
    now = frozen_now(monkeypatch)
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    exp = now + _dt.timedelta(hours=2)
    token, tok, got = routes.minted(o, subject=a, ns=ns, depth_cap=3, expires_at=iso(exp),
                                    rights=(("task/42/", "read"), ("task/42/", "write")))
    assert got.headers.get("Cache-Control") == "no-store", got.headers
    assert got.body == {
        "token": token, "cap_id": tok.revocation_ids[0], "issuer": o.id, "subject": a.id,
        "namespace": ns, "rights": [{"prefix": "task/42/", "action": "read"},
                                    {"prefix": "task/42/", "action": "write"}],
        "depth_cap": 3, "expires_at": iso(exp), "kid": ROOT_KID}, got.body


def test_the_authority_block_is_2_3s_block_every_term_a_parameter(routes, monkeypatch):
    """Design 2.3: cap_version(1); issuer and subject as lowercase hyphenated uuid STRINGS; one
    right per (prefix, action) on the ONE namespace; depth_cap; the mandatory time check; the
    root key id. A prefix holding Datalog punctuation round-trips as ONE term (T10 at the mint):
    the block states exactly the one right, with the prefix as it was sent. Its non-ASCII
    letter, backslash, tab and braces are there for the text a QUOTING function would get
    wrong: a JSON-quoted prefix spliced into the source survives the punctuation and fails on
    these (mutant M28)."""
    w = routes.w
    now = frozen_now(monkeypatch)
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    evil = 'x"); right("' + ns + '", "", "write"); check if true; // caf\u00e9 \\ \t {ns}'
    exp = now + _dt.timedelta(hours=1)
    token, tok, _ = routes.minted(o, subject=a, ns=ns, depth_cap=1, expires_at=iso(exp),
                                  rights=((evil, "read"),))
    kids = []

    def provider(kid):
        kids.append(kid)
        return w.store.provider(kid)

    parsed = w.b.Biscuit.from_base64(token, provider)
    assert kids == [ROOT_KID] and parsed.block_count() == 1
    authorizer = w.b.AuthorizerBuilder().build(parsed)

    def facts(rule):
        return sorted(tuple(t for t in row.terms) for row in authorizer.query(w.b.Rule(rule)))

    assert facts("f($v) <- cap_version($v)") == [(1,)]
    assert facts("f($v) <- issuer($v)") == [(o.id,)]
    assert facts("f($v) <- subject($v)") == [(a.id,)]
    assert facts("f($n, $p, $a) <- right($n, $p, $a)") == [(ns, evil, "read")]
    assert facts("f($v) <- depth_cap($v)") == [(1,)]
    assert facts("f($x) <- delegate($x)") == []
    source = tok.block_source(0)
    assert source.endswith(f"check if time($t), $t < {iso(exp)};\n"), source
    assert source.startswith("cap_version(1);\n"), source
    # The token WORKS as minted (the control that the facts above are the ones it is read by),
    # and the punctuation in the prefix added no right: a write anywhere is refused.
    assert w.decide(token, a, ns, Key(evil + "k"))[0] == "ALLOW"
    assert w.decide(token, a, ns, Key(evil + "k"), action="write")[0] == "E_CAPABILITY_DENIED"
    assert w.decide(token, a, ns, Key("k"), action="write")[0] == "E_CAPABILITY_DENIED"


def test_the_register_row_is_written_by_the_mint_and_holds_what_the_token_says(routes,
                                                                              monkeypatch):
    """2.5 (c): the issue row names issuer, subject, the ONE namespace, the rights, the depth
    cap, the expiry, the kid and the authority revocation id; its actor is the issuer and its
    time the mint's own clock. Never the token (T14 scans for it)."""
    w = routes.w
    now = frozen_now(monkeypatch)
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    exp = now + _dt.timedelta(hours=6)
    _, tok, _ = routes.minted(o, subject=a, ns=ns, depth_cap=4, expires_at=iso(exp),
                              rights=(("", "write"), ("docs/", "read")))
    rows = routes.issue_rows(tok.revocation_ids[0])
    assert len(rows) == 1, rows
    row = rows[0]
    # Instants, not wall clocks: the session's zone may not be UTC, and in the hour a clock
    # change repeats Python never equates an aware time with one in another zone (PEP 495).
    utc = _dt.timezone.utc
    assert (row["event"], str(row["actor_peer_id"]), str(row["issuer_peer_id"]),
            str(row["subject_peer_id"]), row["namespace"], row["rights"], row["depth_cap"],
            row["expires_at"].astimezone(utc), row["kid"],
            bytes(row["authority_revocation_id"]).hex(),
            row["at"].astimezone(utc), row["revocation_id"], row["reason"]) == (
        "issue", o.id, o.id, a.id, ns,
        [{"prefix": "", "action": "write"}, {"prefix": "docs/", "action": "read"}], 4, exp,
        ROOT_KID, tok.revocation_ids[0], now, None, None), row


def test_the_issuer_is_the_authenticated_peer_act_sub_else_sub(routes):
    """3.1: the issuer is the authenticated caller, `act.sub` when the token is an owner's
    on-behalf-of token, else `sub`. The register names the PEER in both cases."""
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    act = peer_jwt(o.id, act_for=o.owner_id)
    on_behalf = routes.mint(None, body=routes.mint_body(subject=a, ns=ns),
                            headers={"authorization": f"Bearer {act}"})
    plain = routes.mint(o, subject=a, ns=ns)
    assert on_behalf.status == plain.status == 201, (on_behalf, plain)
    for got in (on_behalf, plain):
        assert got.body["issuer"] == o.id
        assert str(routes.issue_rows(got.body["cap_id"])[0]["issuer_peer_id"]) == o.id


def test_a_retried_mint_mints_and_registers_a_second_token(routes):
    """3.1: a retried mint is a second token, each registered and each revocable by its own
    authority id; no mint is answered from an earlier one."""
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    body = routes.mint_body(subject=a, ns=ns)
    first, second = routes.mint(o, body=body), routes.mint(o, body=body)
    assert first.status == second.status == 201, (first, second)
    assert first.body["token"] != second.body["token"]
    assert first.body["cap_id"] != second.body["cap_id"]
    assert len(routes.issue_rows(first.body["cap_id"])) == 1
    assert len(routes.issue_rows(second.body["cap_id"])) == 1
    for got in (first, second):
        assert w.decide(got.body["token"], a, ns, Key("k"))[0] == "ALLOW"


# ================================================================== who mints (Q2)

def test_only_an_admin_of_the_namespace_mints(routes):
    """Q2 and design 2.2: the issuer must hold admin on the ONE namespace by
    meshy_namespace_permission, the bar memory.grant sets. A peer with no grant and a peer with a
    WRITE grant are refused; an unclaimed namespace is refused as unclaimed. CONTROLS: the owner
    (implicit admin) and a peer with an ADMIN grant mint, on the same namespace."""
    w = routes.w
    o, a, x, wr, ad = (w.peer(t) for t in ("O", "A", "X", "W", "I"))
    ns = w.claim(o)
    w.grant(ns, wr, o, "write")
    w.grant(ns, ad, o, "admin")
    got = {name: routes.mint(peer, subject=a, ns=ns).outcome()
           for name, peer in (("stranger", x), ("writer", wr))}
    assert got == {"stranger": (403, DENIED_NS), "writer": (403, DENIED_NS)}, got
    unclaimed = routes.mint(o, subject=a, ns=f"captest/unclaimed-{o.id[:8]}")
    assert unclaimed.outcome() == (403, UNCLAIMED), unclaimed
    assert routes.issue_rows(ns=ns) == [], "a refused mint wrote a register row"
    assert routes.mint(o, subject=a, ns=ns).status == 201
    assert routes.mint(ad, subject=a, ns=ns).status == 201
    expired = w.peer("E")
    w.sql("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
          "permission, granted_by_peer_id, granted_by_owner_id, expires_at) "
          "VALUES (%s,%s,%s,'admin',%s,%s, now() - interval '1 minute')",
          (ns, expired.id, expired.owner_id, o.id, o.owner_id))
    assert routes.mint(expired, subject=a, ns=ns).outcome() == (403, DENIED_NS), (
        "an expired admin grant is history, not capability")


def test_an_admin_of_another_namespace_does_not_mint_on_this_one(routes):
    """Critique C9's negative control: admin is read on the ONE namespace the token names, never
    on any namespace the issuer holds. Q owns a namespace of its own (implicit admin) and holds
    admin by grant on a third; on O's it is refused and registers nothing. CONTROLS: Q mints on
    its own namespace and on the one it holds admin on by grant (slice 5's review, F3)."""
    w = routes.w
    o, q, a = w.peer("O"), w.peer("Q"), w.peer("A")
    ns, own, granted = w.claim(o), w.claim(q), w.claim(o)
    w.grant(granted, q, o, "admin")
    got = routes.mint(q, subject=a, ns=ns)
    assert got.outcome() == (403, DENIED_NS), got
    assert routes.issue_rows(ns=ns) == []
    assert routes.mint(q, subject=a, ns=own).status == 201
    assert routes.mint(q, subject=a, ns=granted).status == 201


def test_a_suspended_issuer_is_refused_even_when_the_suspension_lands_after_authentication(
        routes, monkeypatch):
    """The issuer must be `active` (critique C6: the namespace predicate reads no peer status).
    Authentication already refuses a suspended peer; the mint reads the issuer's status again in
    the statement that reads its admin, so a suspension that commits between the two is
    refused too. The race is made by suspending the issuer right after authentication. CONTROL:
    the same wrapper, not suspending, mints."""
    from apps.memoryskills import views
    w = routes.w
    o, a, s = w.peer("O"), w.peer("A"), w.peer("S")
    ns = w.claim(o)
    w.grant(ns, s, o, "admin")
    real = views.authenticate_peer
    suspend = {"on": False}

    def authenticate_then_suspend(conn, request):
        auth = real(conn, request)
        if suspend["on"]:
            w.sql("UPDATE peers SET status = 'suspended' WHERE id = %s", (str(auth.peer_id),))
        return auth

    monkeypatch.setattr(views, "authenticate_peer", authenticate_then_suspend)
    assert routes.mint(s, subject=a, ns=ns).status == 201                 # control
    suspend["on"] = True
    raced = routes.mint(s, subject=a, ns=ns)
    assert raced.outcome() == (403, FORBIDDEN), raced
    assert "not active" in raced.body["message"], raced
    assert len(routes.issue_rows(ns=ns)) == 1, "the raced mint wrote a register row"
    # Suspended before the request: authentication refuses first, with its own answer.
    assert routes.mint(s, subject=a, ns=ns).outcome() == (403, FORBIDDEN)


# ================================================================== what a token may say

def test_a_second_namespace_is_refused(routes):
    """Critique C9: a token names ONE namespace. Every way to ask for a second is refused
    before anything is signed: a list for `namespace`, a `namespaces` field, and a right that
    names its own namespace. CONTROL: the same request with one namespace mints."""
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns, other = w.claim(o), w.claim(o)
    base = routes.mint_body(subject=a, ns=ns)
    asks = {
        "a list": dict(base, namespace=[ns, other]),
        "namespaces": dict(base, namespaces=[other]),
        "a right's own": dict(base, rights=[{"prefix": "", "action": "read"},
                                            {"namespace": other, "prefix": "",
                                             "action": "read"}]),
    }
    for name, body in asks.items():
        got = routes.mint(o, body=body)
        assert got.outcome() == (422, VALIDATION), (name, got)
        assert "one namespace" in got.body["message"], (name, got.body)
    assert routes.issue_rows(ns=[ns, other]) == []
    assert routes.mint(o, body=base).status == 201


def test_admin_is_never_mintable_and_write_does_not_imply_read(routes):
    """Design 2.1: a token's actions are read and write only; `admin` is never mintable, alone
    or beside a read. CONTROL: read and write together mint. A write-only token reads nothing:
    the minter states both if both are meant."""
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    for rights in ((("", "admin"),), (("", "read"), ("", "admin")), (("docs/", "ADMIN"),),
                   (("", "delete"),)):
        got = routes.mint(o, subject=a, ns=ns, rights=rights)
        assert got.outcome() == (422, VALIDATION), (rights, got)
    assert routes.issue_rows(ns=ns) == []
    both = routes.mint(o, subject=a, ns=ns, rights=(("", "read"), ("", "write")))
    assert both.status == 201, both
    write_only, _, _ = routes.minted(o, subject=a, ns=ns, rights=(("", "write"),))
    assert w.decide(write_only, a, ns, Key("k"), action="write")[0] == "ALLOW"
    assert w.decide(write_only, a, ns, Key("k"), action="read")[0] == "E_CAPABILITY_DENIED"


def test_an_authority_lives_at_most_24_hours_to_the_second(routes, monkeypatch):
    """Q3: expires_at - now at most 24 hours, now the mint's own clock in whole seconds.
    Exactly 24 hours mints (the boundary's control); 24 hours and one second is refused, and so
    are an expiry that is now or past. A fractional expiry is minted truncated to its second,
    never rounded up."""
    w = routes.w
    now = frozen_now(monkeypatch)
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    day = _dt.timedelta(hours=24)
    for exp in (now + day + _dt.timedelta(seconds=1), now, now - _dt.timedelta(seconds=1)):
        got = routes.mint(o, subject=a, ns=ns, expires_at=iso(exp))
        assert got.outcome() == (422, VALIDATION), (exp, got)
    assert routes.issue_rows(ns=ns) == []
    exact = routes.mint(o, subject=a, ns=ns, expires_at=iso(now + day))
    assert exact.status == 201 and exact.body["expires_at"] == iso(now + day), exact
    fractional = routes.mint(o, subject=a, ns=ns,
                             expires_at=(now + day).strftime("%Y-%m-%dT%H:%M:%S") + ".999Z")
    assert fractional.status == 201 and fractional.body["expires_at"] == iso(now + day), (
        fractional)
    offset = routes.mint(o, subject=a, ns=ns, expires_at=(now + _dt.timedelta(hours=1)).astimezone(
        _dt.timezone(_dt.timedelta(hours=2))).isoformat())
    assert offset.status == 201 and offset.body["expires_at"] == iso(
        now + _dt.timedelta(hours=1)), offset
    naive = routes.mint(o, subject=a, ns=ns, expires_at="2026-09-26T12:00:00")
    assert naive.outcome() == (422, VALIDATION), naive
    # An offset that carries the moment outside what a datetime holds, once in UTC: without a
    # check, astimezone raised OverflowError, a 500.
    for edge in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
        got = routes.mint(o, subject=a, ns=ns, expires_at=edge)
        assert got.outcome() == (422, VALIDATION), (edge, got)


def test_depth_cap_is_0_to_4(routes):
    """2.3: depth_cap(n), n in 0..4. 5, a negative, a bool and a string are
    refused. CONTROLS: 0 and 4 mint."""
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    for cap in (5, -1, True, "2", 2.0, None):
        got = routes.mint(o, subject=a, ns=ns, depth_cap=cap)
        assert got.outcome() == (422, VALIDATION), (cap, got)
    assert routes.issue_rows(ns=ns) == []
    for cap in (0, 4):
        got = routes.mint(o, subject=a, ns=ns, depth_cap=cap)
        assert got.status == 201 and got.body["depth_cap"] == cap, got


def test_the_request_is_bounded_so_a_token_stays_inside_8_kib(routes):
    """3.1: the verifier refuses a token over 8 KiB, so the mint refuses to sign one, and bounds
    the request that could: at most 16 rights, and a prefix or namespace of at most 512
    characters. Each bound is claimed in divergence 040 and each sits beside its control
    (slice 5's review, F5):
      * 16 rights of 505-character prefixes, each inside the text bound, would sign a token
        over 8 KiB: 422 and no register row. 16 rights of 256 characters mint.
      * 17 rights are refused before anything is signed; 16 mint.
      * A 513-character prefix is refused; 512 characters mint."""
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)

    def rights(count, length):
        return tuple((f"{n:02d}" + "p" * (length - 2), "read") for n in range(count))

    big = routes.mint(o, subject=a, ns=ns, rights=rights(16, 505))
    assert big.outcome() == (422, VALIDATION) and "bytes" in big.body["message"], big
    many = routes.mint(o, subject=a, ns=ns, rights=rights(17, 4))
    assert many.outcome() == (422, VALIDATION) and "16" in many.body["message"], many
    long = routes.mint(o, subject=a, ns=ns, rights=rights(1, 513))
    assert long.outcome() == (422, VALIDATION) and "512" in long.body["message"], long
    assert routes.issue_rows(ns=ns) == []
    fits, _, got = routes.minted(o, subject=a, ns=ns, rights=rights(16, 256))
    assert len(fits) <= 8192 and len(got.body["rights"]) == 16, len(fits)
    assert routes.mint(o, subject=a, ns=ns, rights=rights(16, 4)).status == 201
    assert routes.mint(o, subject=a, ns=ns, rights=rights(1, 512)).status == 201


@pytest.mark.parametrize("which", ["shared/wiki", "a shared/* of its own"])
def test_a_product_plane_subject_on_shared_is_refused_with_its_audit_row(routes, which):
    """Critique C2, I2's grant half: no product-plane peer holds a grant on shared/*, and a
    token is a grant. The mint refuses a product-plane subject on a shared/* namespace, on
    shared/wiki itself and on one this test claims, with E_PRODUCT_NO_SHARED_GRANT and the same
    tenancy_denials row memory.grant writes (the issuer the actor, the subject's owner the
    target), and registers nothing. CONTROLS: a dev-plane subject on the same namespace mints;
    the product-plane subject on an ordinary namespace mints (the clause is shared/* only)."""
    w = routes.w
    o, p, d = w.peer("O"), w.peer("P", plane="product"), w.peer("D")
    shared = (admin_on(w, o, "shared/wiki") if which == "shared/wiki"
              else w.claim(o, f"shared/mint-{o.id[:8]}"))
    plain = w.claim(o)
    refused = routes.mint(o, subject=p, ns=shared)
    assert refused.outcome() == (403, PLANE), refused
    assert routes.audits(o) == [("product_no_shared_grant", "memory.grant", o.owner_id, o.id,
                                 p.owner_id, shared)], routes.audits(o)
    assert [r for r in routes.issue_rows(ns=[shared, plain])
            if str(r["issuer_peer_id"]) == o.id] == []
    assert routes.mint(o, subject=d, ns=shared).status == 201
    assert routes.mint(o, subject=p, ns=plain).status == 201
    assert len(routes.audits(o)) == 1, "a control wrote an audit row"


def test_the_subject_is_an_active_peer_named_by_its_canonical_id(routes):
    """The subject is the token's first holder (2.1). An unknown peer, a suspended one, a
    string that is not a uuid, and a uuid not in its lowercase hyphenated form (the form the
    token states, 2.3) are refused. CONTROL: the same subject, active and canonical, mints."""
    w = routes.w
    o, a, s = w.peer("O"), w.peer("A"), w.peer("S", status="suspended")
    ns = w.claim(o)
    for subject, want in ((random_peer_id(), (422, UNKNOWN_PEER)), (s.id, (422, UNKNOWN_PEER)),
                          ("peer-a", (422, VALIDATION)), (a.id.upper(), (422, VALIDATION)),
                          (a.id.replace("-", ""), (422, VALIDATION))):
        got = routes.mint(o, subject=subject, ns=ns)
        assert got.outcome() == want, (subject, got)
    assert routes.issue_rows(ns=ns) == []
    assert routes.mint(o, subject=a, ns=ns).status == 201


def test_a_non_admin_learns_nothing_about_the_subject(routes):
    """The admin check comes first: a stranger asking to mint for an unknown subject gets the
    same answer as for a known one."""
    w = routes.w
    o, a, x = w.peer("O"), w.peer("A"), w.peer("X")
    ns = w.claim(o)
    assert routes.mint(x, subject=a, ns=ns).outcome() == \
        routes.mint(x, subject=random_peer_id(), ns=ns).outcome() == (403, DENIED_NS)


def test_the_request_is_one_json_object_of_the_named_fields(routes):
    """The body's shape: the three statuses the other routes answer (415 content type, 400 not
    JSON, 422 not an object) and 422 for a missing or unknown field, an empty or repeated right,
    a right that is not {prefix, action}, and a prefix that is not a string. JSON the parser
    will not take (an integer over 4300 digits, nesting past the recursion limit) is 400 like
    any body that is not JSON, and text that has no UTF-8 form (a lone surrogate, which a JSON
    escape can spell) is 422 like NUL: each was a 500 (slice 5's review, I3 and F1).
    CONTROL: the base body mints."""
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    base = routes.mint_body(subject=a, ns=ns)
    assert routes.post(MINT, None, caller=o, raw="{", content_type="application/json"
                       ).outcome() == (400, VALIDATION)
    assert routes.post(MINT, None, caller=o, raw=json.dumps(base), content_type="text/plain"
                       ).outcome() == (415, VALIDATION)
    # json.loads raises ValueError for the first and RecursionError for the second, neither of
    # them the JSONDecodeError the route caught.
    for name, raw in (("an integer over 4300 digits", '{"depth_cap": ' + "1" * 5000 + "}"),
                      ("nesting past the recursion limit", "[" * 100000 + "]" * 100000)):
        got = routes.post(MINT, None, caller=o, raw=raw, content_type="application/json")
        assert got.outcome() == (400, VALIDATION), (name, got)
    bodies = {
        "an array": [base],
        "no subject": {k: v for k, v in base.items() if k != "subject"},
        "no expiry": {k: v for k, v in base.items() if k != "expires_at"},
        "an unknown field": dict(base, admin=True),
        "no rights": dict(base, rights=[]),
        "a repeated right": dict(base, rights=[{"prefix": "", "action": "read"}] * 2),
        "a right with an extra key": dict(base, rights=[{"prefix": "", "action": "read",
                                                          "why": 1}]),
        "a right that is a list": dict(base, rights=[["", "read"]]),
        "a prefix that is not a string": dict(base, rights=[{"prefix": 7, "action": "read"}]),
        "a namespace that is not a string": dict(base, namespace=7),
        "an empty namespace": dict(base, namespace=""),
        "an expiry that is not a string": dict(base, expires_at=1790000000),
        # Postgres text holds no NUL: without a check these were a 500 (psycopg DataError).
        "a namespace holding NUL": dict(base, namespace=ns + "\x00"),
        "a prefix holding NUL": dict(base, rights=[{"prefix": "a\x00", "action": "read"}]),
        # json.dumps writes these as \ud800 escapes, which json.loads reads back as a str that
        # has no UTF-8 form: psycopg (the namespace) and the Biscuit builder (the prefix) raised.
        "a namespace holding a lone surrogate": dict(base, namespace=ns + "\ud800"),
        "a prefix holding a lone surrogate": dict(base, rights=[{"prefix": "a\udfff",
                                                                 "action": "read"}]),
    }
    for name, body in bodies.items():
        got = routes.mint(o, body=body)
        assert got.outcome() == (422, VALIDATION), (name, got)
    assert routes.issue_rows(ns=ns) == []
    assert routes.mint(o, body=base).status == 201


def test_the_mint_is_a_post(routes):
    w = routes.w
    o = w.peer("O")
    got = routes.client.get(MINT, headers={"authorization": f"Bearer {peer_jwt(o.id)}"})
    assert got.status_code == 405 and got.headers.get("Allow") == "POST"


def test_an_unauthenticated_mint_is_refused(routes):
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    got = routes.post(MINT, routes.mint_body(subject=a, ns=ns))
    assert got.outcome() == (401, "E_AUTH_MISSING"), got
    assert routes.issue_rows(ns=ns) == []
    assert routes.mint(o, subject=a, ns=ns).status == 201                  # control


def test_the_mint_is_a_post_so_dpop_is_enforced(routes):
    """3.1: "POSTs, so DPoP is already enforced". With enforcement on (the production default),
    a cnf-bound JWT without a proof is refused and registers nothing; with a proof bound to
    this method, this path and this JWT, it mints."""
    from apps.authapi.didkey import to_did_key
    from apps.crypto import primitives as P
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    seed = os.urandom(32)
    jwt = peer_jwt(o.id, cnf=to_did_key(P.public_key_of(seed)))
    body = routes.mint_body(subject=a, ns=ns)
    with override_settings(MESHY_DPOP_ENFORCE=True):
        bare = routes.mint(None, body=body, headers={"authorization": f"Bearer {jwt}"})
        proved = routes.mint(None, body=body, headers={
            "authorization": f"Bearer {jwt}", "DPoP": P.dpop_wire(seed, "POST", MINT, jwt)})
    assert bare.outcome() == (403, FORBIDDEN), bare
    assert proved.status == 201, proved
    assert len(routes.issue_rows(ns=ns)) == 1


# ================================================================== tokens off

def test_with_tokens_off_both_routes_refuse_unavailable(routes):
    """With MESHY_CAPABILITIES off both routes are E_CAPABILITY_UNAVAILABLE, before anything
    else: an unauthenticated request gets the same answer, and nothing is registered or revoked.
    A flag that is on with no key store loaded is the same (only a test can make that state:
    ready() refuses to start it). CONTROL: the flag on, the same mint is 201."""
    from apps.memoryskills import capability_keys
    w = routes.w
    o, a = w.peer("O"), w.peer("A")
    ns = w.claim(o)
    body = routes.mint_body(subject=a, ns=ns)
    revoke_body = {"authority_id": "00" * 64, "reason": "off"}
    with override_settings(MESHY_CAPABILITIES=False):
        got = [routes.mint(o, body=body), routes.post(MINT, body),
               routes.post(REVOKE, revoke_body, caller=o), routes.post(REVOKE, revoke_body)]
    previous = capability_keys.install(None)
    try:
        got.append(routes.mint(o, body=body))
        got.append(routes.post(REVOKE, revoke_body, caller=o))
    finally:
        capability_keys.install(previous)
    assert [g.outcome() for g in got] == [(403, UNAVAILABLE)] * 6, got
    assert routes.issue_rows(ns=ns) == [] and routes.revocations(by=o) == []
    assert routes.mint(o, body=body).status == 201


def _child_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_") and not k.startswith("MESHY_CAPABILIT")
           and k not in ("MESHYCHAT_BISCUIT_ROOT_SK", "MESHY_DEBUG")}
    env.update({"DJANGO_SETTINGS_MODULE": "meshychat.settings",
                "MESHY_ALLOWED_HOSTS": "testserver", **extra})
    return env


def _child(extra: dict) -> dict:
    proc = subprocess.run([sys.executable, str(CHILD)], env=_child_env(extra), cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("ROUTES_RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (
        f"child exit {proc.returncode}\nstdout:\n{proc.stdout[-1500:]}\n"
        f"stderr:\n{proc.stderr[-2500:]}")
    return json.loads(lines[0][len("ROUTES_RESULT "):])


def test_with_tokens_off_the_routes_import_nothing(tmp_path):
    """The served app, booted with tokens off, answers both routes E_CAPABILITY_UNAVAILABLE and
    has imported neither biscuit_auth nor the verifier nor the mint module, before or after the
    requests. CONTROL: booted with tokens on and a key file, the same requests import the mint
    module (and biscuit_auth, which the key store imports at startup), and are answered by the
    routes' own authentication (401)."""
    watched = ["apps.memoryskills.capability", "apps.memoryskills.capability_issue",
               "biscuit_auth"]
    off = _child({})
    assert off["before"] == [] and off["after"] == [], off
    assert off["answers"] == {MINT: [403, UNAVAILABLE], REVOKE: [403, UNAVAILABLE]}, off
    key = tmp_path / "root.key"
    key.write_text("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60\n",
                   encoding="ascii")
    on = _child({"MESHY_CAPABILITIES": "on", "MESHY_CAPABILITY_ROOT_KEY_PATH": str(key),
                 "MESHY_CAPABILITY_ROOT_KID": "7", "MESHY_CAPABILITY_ROOT_PUBKEYS":
                     "7=d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"})
    assert "biscuit_auth" in on["before"], on
    assert set(watched) - {"apps.memoryskills.capability"} <= set(on["after"]), on
    assert on["answers"] == {MINT: [401, "E_AUTH_MISSING"], REVOKE: [401, "E_AUTH_MISSING"]}, on


# ================================================================== T14 at the mint

def test_t14_the_token_is_in_the_one_mint_response_and_nowhere_else(routes, caplog):
    """The token is returned ONCE: every other response (refusals included), the register
    rows and the log never hold it or a 24-character window of its tail (the needles of
    tests/capability_routes.py: a token's head is shared by every token this route mints, so a
    scan for it finds another token). POSITIVE CONTROL: the scan finds it in the mint's own
    response, and in a planted string."""
    import io
    import logging
    w = routes.w
    o, a, x, p = w.peer("O"), w.peer("A"), w.peer("X"), w.peer("P", plane="product")
    ns = w.claim(o)
    shared = w.claim(o, f"shared/t14m-{o.id[:8]}")
    formatted = io.StringIO()
    handler = logging.StreamHandler(formatted)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    loggers = [logging.getLogger(n) for n in ("", "meshychat", "django")]
    saved = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG):
            token, _, minted = routes.minted(o, subject=a, ns=ns)
            again = routes.mint(o, subject=a, ns=ns)
            refusals = [routes.mint(x, subject=a, ns=ns), routes.mint(o, subject=p, ns=shared),
                        routes.mint(o, subject=a, ns=ns, depth_cap=9)]
    finally:
        for lg, level in saved:
            lg.removeHandler(handler)
            lg.setLevel(level)
    assert again.status == 201 and [r.status for r in refusals] == [403, 403, 422]
    others = [s for s in routes.seen if s is not minted]
    rows = "\n".join(json.dumps(r, default=lambda v: bytes(v).hex() if isinstance(
        v, (bytes, memoryview)) else str(v)) for r in routes.issue_rows(ns=[ns, shared]))
    places = {"other responses": "\n".join(s.text() for s in others),
              "register rows": rows,
              "log records": "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records),
              "formatted log": formatted.getvalue()}
    for where, text in places.items():
        assert text.strip(), f"control: nothing was read from {where}"
    assert scan(token, places) == {}, scan(token, places)
    # POSITIVE CONTROLS: the one place it is, found by every needle; a planted window of it;
    # and the second mint's response, which holds ITS token and none of this one's windows.
    assert scan(token, {"mint": minted.text()}) == {"mint": ["token", "signature 24",
                                                             "last 24"]}
    assert scan(token, {"x": "y" + token[-24:] + "z"}) == {"x": ["last 24"]}
    assert scan(again.body["token"], {"again": again.text()}) != {}
