"""The revoke: `POST /v1/memory/capabilities/revoke` (B07 slice 5; divergence 040).

Design docs/design/biscuit-capabilities.md 2.5 (a) "who may revoke" and (c), 3.1 (the revoke
bullet). Allowed callers: the token's issuer; any live admin of its one namespace; and any peer
that signed a delegation block in the presented token, for its own block and the blocks after
it. The signer PRESENTS the token and the server computes the ids; an issuer or admin may
instead revoke by authority id, which the register resolves.

The design leaves open whether the token comes in a header or the body. It comes in the
`Meshychat-Capability` header, the one channel a token is presented in (3.1), so the revoke route is
the one entry on the header middleware's accept list (apps/core/capability_header.py). The
body names the block (`block`, its index in the token, 0 the authority) and a `reason`; or, with
no header, the `authority_id` and a reason.

T7 through the route: revoke the authority id, then a mid-chain block id; each refuses on its
next use, and an unrelated token keeps working. "Use" is the verifier's decision
(authorize_capability, as check_permission calls it); the handlers' use of a token is slice 6's
(tests/test_capability_envelope_path.py), and T14 over HTTP revokes through this route.

Every refusal sits beside its passing control. Rows are read scoped to this test's own
namespaces and peers: the tables are the whole run's.

Run:  pytest tests/test_capability_revoke_route.py -q
Needs the dev database (tests/conftest.py builds and drops its own) and CPython 3.13.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import os
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
    REVOKE,
    b64u_key,
    peer_jwt,
    random_peer_id,
    routes_context,
    scan,
)
from tests.capability_world import ROOT_KID  # noqa: E402

VALIDATION, NOT_FOUND, CONFLICT = "E_VALIDATION", "E_NOT_FOUND", "E_CONFLICT"
DENIED_NS, INVALID = "E_PERMISSION_DENIED", "E_CAPABILITY_INVALID"
REVOKED = "E_CAPABILITY_REVOKED"


@pytest.fixture()
def routes():
    with routes_context() as r:
        yield r


class Chain:
    """O owns ns and mints, through the route, a read token for A (depth 4). A delegates to E
    (block 1, signed by A) and E to F (block 2, signed by E); F presents `chain2`, E holds
    `chain1`. A's other branch goes to G. O also mints an UNRELATED token for itself."""


@pytest.fixture()
def chain(routes):
    w = routes.w
    c = Chain()
    c.routes, c.w = routes, w
    c.o, c.a, c.e, c.f, c.g, c.x = (w.peer(t) for t in ("O", "A", "E", "F", "G", "X"))
    c.ns = w.claim(c.o)
    c.token, c.base, c.minted = routes.minted(c.o, subject=c.a, ns=c.ns, depth_cap=4)
    c.chain1 = w.delegate(c.base, c.a, c.e)
    c.chain2 = w.delegate(c.chain1, c.e, c.f)
    c.sibling = w.delegate(c.base, c.a, c.g)
    c.unrelated, _, _ = routes.minted(c.o, subject=c.o, ns=c.ns)

    def use(tok, peer):
        return w.decide(tok, peer, c.ns, Key("k"))[0]

    c.use = use
    c.uses = lambda: {"base": use(c.base, c.a), "chain1": use(c.chain1, c.e),
                      "chain2": use(c.chain2, c.f), "sibling": use(c.sibling, c.g),
                      "unrelated": use(c.unrelated, c.o)}
    assert c.uses() == dict.fromkeys(("base", "chain1", "chain2", "sibling", "unrelated"),
                                     "ALLOW"), c.uses()                    # before: all live
    return c


# ================================================================== T7 through the route

def test_t7_revoking_the_authority_id_ends_the_token_and_every_descendant(chain):
    """2.5 (a): the authority id kills the token and everything derived from it. The issuer
    revokes it by id, as the register resolves it; the unrelated token keeps working."""
    got = chain.routes.revoke(chain.o, authority_id=chain.base.revocation_ids[0])
    assert got.status == 200, got
    assert got.body["standing"] == "issuer" and got.body["revoked"]["block"] == 0, got.body
    assert chain.uses() == {"base": REVOKED, "chain1": REVOKED, "chain2": REVOKED,
                            "sibling": REVOKED, "unrelated": "ALLOW"}


def test_t7_revoking_a_mid_chain_block_ends_that_branch_only(chain):
    """A signed block 1 (A -> E) and presents a token holding it, naming block 1: E's and F's
    tokens die; A's own token, A's other branch (to G) and the unrelated token keep working."""
    got = chain.routes.revoke(chain.a, token=chain.chain1, block=1)
    assert got.status == 200, got
    assert got.body["standing"] == "signer", got.body
    assert got.body["revoked"]["revocation_id"] == chain.chain1.revocation_ids[1]
    assert chain.uses() == {"base": "ALLOW", "chain1": REVOKED, "chain2": REVOKED,
                            "sibling": "ALLOW", "unrelated": "ALLOW"}


def test_t7_the_last_signer_ends_its_own_hop_and_the_hop_before_lives(chain):
    got = chain.routes.revoke(chain.e, token=chain.chain2, block=2)
    assert got.status == 200 and got.body["standing"] == "signer", got
    assert chain.uses() == {"base": "ALLOW", "chain1": "ALLOW", "chain2": REVOKED,
                            "sibling": "ALLOW", "unrelated": "ALLOW"}


# ================================================================== who may revoke

def test_a_peer_that_is_neither_issuer_admin_nor_signer_is_refused(chain):
    """A stranger, the final holder F (who signed nothing), a WRITER of the namespace, and the
    SUBJECT A (the first holder, not the issuer: it signed block 1 and may revoke from there,
    never the authority) are refused, by the presented token and by id, and nothing is
    written. CONTROLS: the signer E revokes block 2, and the issuer O the authority, on the
    same token."""
    r, w = chain.routes, chain.w
    writer = w.peer("W")
    w.grant(chain.ns, writer, chain.o, "write")
    refused = {
        "stranger, token": r.revoke(chain.x, token=chain.chain2, block=2),
        "stranger, id": r.revoke(chain.x, authority_id=chain.base.revocation_ids[0]),
        "holder, token": r.revoke(chain.f, token=chain.chain2, block=2),
        "holder, id": r.revoke(chain.f, authority_id=chain.base.revocation_ids[0]),
        "writer, id": r.revoke(writer, authority_id=chain.base.revocation_ids[0]),
        "writer, token": r.revoke(writer, token=chain.chain2, block=0),
        "subject, id": r.revoke(chain.a, authority_id=chain.base.revocation_ids[0]),
        "subject, token": r.revoke(chain.a, token=chain.chain2, block=0),
    }
    assert {k: v.outcome() for k, v in refused.items()} == dict.fromkeys(
        refused, (403, DENIED_NS)), refused
    assert r.revocations(by=[chain.x, chain.f, writer, chain.a]) == []
    assert chain.uses()["chain2"] == "ALLOW"
    assert r.revoke(chain.e, token=chain.chain2, block=2).status == 200
    assert r.revoke(chain.o, authority_id=chain.base.revocation_ids[0]).status == 200


def test_a_signer_revokes_its_own_block_and_below_never_above(chain):
    """E signed block 2 of A -> E -> F -> G. It may revoke block 2 and block 3 (below its own),
    never block 1 (A's) or block 0 (the authority): those would end A's other branches, which
    E never held. CONTROLS: A, who signed block 1, revokes block 1."""
    r, w = chain.routes, chain.w
    chain3 = w.delegate(chain.chain2, chain.f, chain.g)
    for block in (0, 1):
        got = r.revoke(chain.e, token=chain3, block=block)
        assert got.outcome() == (403, DENIED_NS), (block, got)
        assert "its own block" in got.body["message"], got.body
    assert r.revocations(by=chain.e) == []
    assert r.revoke(chain.e, token=chain3, block=3).status == 200            # below its own
    assert r.revoke(chain.e, token=chain3, block=2).status == 200            # its own
    assert chain.uses()["chain1"] == "ALLOW", "E's revokes reached above its block"
    assert r.revoke(chain.a, token=chain3, block=1).status == 200            # A's own


def test_a_signers_standing_ends_when_the_token_expires(chain, monkeypatch):
    """A signer's standing is read from the token it presents, so it ends with the token: from
    the register's expiry on (the authority's `check if time($t), $t < expiry` already refuses
    every use, so the token is dead AT its expiry), a signer's revoke is refused and writes
    nothing. Without this any holder could append a block to a peer that does not exist,
    present the fork and revoke its own block, one permanent row each (Q8), for as long as it
    stays active after the token died (slice 5's review, F2; decision 8 narrowed).
    CONTROLS: a second before the expiry a signer revokes; at and after it, a live admin and
    the issuer still revoke the same token (decision 8 stands for them)."""
    from apps.memoryskills import capability_issue
    r, w = chain.routes, chain.w
    expiry = r.issue_rows(chain.base.revocation_ids[0])[0]["expires_at"]
    fork = w.delegate(chain.base, chain.a, random_peer_id())      # A -> a peer that is no one
    clock = {"now": expiry - _dt.timedelta(seconds=1)}
    monkeypatch.setattr(capability_issue, "current_time", lambda: clock["now"])
    assert r.revoke(chain.e, token=chain.chain2, block=2).status == 200    # before: control
    for moment in (expiry, expiry + _dt.timedelta(days=1)):
        clock["now"] = moment
        for tok in (chain.chain1, fork):
            got = r.revoke(chain.a, token=tok, block=1)
            assert got.outcome() == (403, DENIED_NS), (moment, got)
            assert "expired" in got.body["message"], got.body
    assert r.revocations(by=chain.a) == []
    i = w.peer("I")
    w.grant(chain.ns, i, chain.o, "admin")
    by_admin = r.revoke(i, token=chain.chain1, block=1)
    assert by_admin.status == 200 and by_admin.body["standing"] == "admin", by_admin
    by_issuer = r.revoke(chain.o, authority_id=chain.base.revocation_ids[0])
    assert by_issuer.status == 200 and by_issuer.body["standing"] == "issuer", by_issuer


def test_a_live_admin_revokes_and_an_admin_no_longer_live_does_not(chain):
    """Any LIVE admin of the token's namespace may revoke (2.5 (a)): by the predicate, so a
    revoked or expired admin grant is history, not capability. CONTROL: the live admin I
    revokes the same token, by id and by the presented token."""
    r, w = chain.routes, chain.w
    i, j, k = w.peer("I"), w.peer("J"), w.peer("K")
    w.grant(chain.ns, i, chain.o, "admin")
    w.grant(chain.ns, j, chain.o, "admin")
    w.sql("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = now() "
          "WHERE namespace = %s AND grantee_peer_id = %s", (chain.ns, j.id))
    w.sql("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
          "permission, granted_by_peer_id, granted_by_owner_id, expires_at) "
          "VALUES (%s,%s,%s,'admin',%s,%s, now() - interval '1 minute')",
          (chain.ns, k.id, k.owner_id, chain.o.id, chain.o.owner_id))
    for peer in (j, k):
        got = r.revoke(peer, authority_id=chain.base.revocation_ids[0])
        assert got.outcome() == (403, DENIED_NS), (peer, got)
    by_token = r.revoke(i, token=chain.chain2, block=2)
    assert by_token.status == 200 and by_token.body["standing"] == "admin", by_token
    by_id = r.revoke(i, authority_id=chain.base.revocation_ids[0])
    assert by_id.status == 200 and by_id.body["standing"] == "admin", by_id


def test_an_admin_of_another_namespace_does_not_revoke(chain):
    """Critique C9's negative control at the revoke: "any live admin of the token's one
    namespace" is read on the namespace the register names for the token, never on one the
    caller holds. Q owns a namespace of its own and holds admin by grant on another of O's; it
    is refused by id and by the presented token, and writes nothing. CONTROL: the same Q,
    granted admin on the token's namespace, revokes (slice 5's review, F3)."""
    r, w = chain.routes, chain.w
    q = w.peer("Q")
    w.claim(q)
    other = w.claim(chain.o)
    w.grant(other, q, chain.o, "admin")
    refused = [r.revoke(q, authority_id=chain.base.revocation_ids[0]),
               r.revoke(q, token=chain.chain2, block=2)]
    assert [g.outcome() for g in refused] == [(403, DENIED_NS)] * 2, refused
    assert r.revocations(by=q) == []
    w.grant(chain.ns, q, chain.o, "admin")
    got = r.revoke(q, token=chain.chain2, block=2)
    assert got.status == 200 and got.body["standing"] == "admin", got


def test_an_admin_suspended_after_authentication_is_no_longer_live(chain, monkeypatch):
    """"Live admin" reads the caller's status in the statement that reads its admin, as the
    mint reads its issuer's: an admin suspended between authentication and the revoke is
    refused (it is neither issuer nor signer). The race is made by suspending the caller right
    after authentication. CONTROL: the same wrapper, not suspending, lets the admin revoke."""
    from apps.memoryskills import views
    r, w = chain.routes, chain.w
    i = w.peer("I")
    w.grant(chain.ns, i, chain.o, "admin")
    real = views.authenticate_peer
    suspend = {"on": True}

    def authenticate_then_suspend(conn, request):
        auth = real(conn, request)
        if suspend["on"]:
            w.sql("UPDATE peers SET status = 'suspended' WHERE id = %s", (str(auth.peer_id),))
        return auth

    monkeypatch.setattr(views, "authenticate_peer", authenticate_then_suspend)
    raced = r.revoke(i, token=chain.chain2, block=2)
    assert raced.outcome() == (403, DENIED_NS), raced
    assert r.revocations(by=i) == []
    w.sql("UPDATE peers SET status = 'active' WHERE id = %s", (i.id,))
    suspend["on"] = False
    got = r.revoke(i, token=chain.chain2, block=2)
    assert got.status == 200 and got.body["standing"] == "admin", got


def test_the_issuer_revokes_its_token_even_after_losing_admin(routes):
    """"The issuer" (2.5 (a)) is a standing of its own: revoking only narrows, so an issuer
    who has since lost admin may still end what it issued. CONTROL: another former admin, not
    the issuer, may not."""
    w = routes.w
    o, i, h, a = w.peer("O"), w.peer("I"), w.peer("H"), w.peer("A")
    ns = w.claim(o)
    w.grant(ns, i, o, "admin")
    w.grant(ns, h, o, "admin")
    _, tok, _ = routes.minted(i, subject=a, ns=ns)
    w.sql("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = now() "
          "WHERE namespace = %s AND grantee_peer_id = ANY(%s::uuid[])", (ns, [i.id, h.id]))
    assert routes.revoke(h, authority_id=tok.revocation_ids[0]).outcome() == (403, DENIED_NS)
    got = routes.revoke(i, authority_id=tok.revocation_ids[0])
    assert got.status == 200 and got.body["standing"] == "issuer", got


def test_a_signer_is_the_holder_that_signed_the_block_with_its_own_key(chain):
    """"Signed a delegation block" is two facts the server checks, never one: the block's
    external key is a live key of the caller, AND the caller held the token at that block
    (the peer before the delegate the block names).
      * The key without the position: M, holding a peer_keys row with A's public key (key
        publication proves possession, so this is a planted row), is not the signer of A's
        block.
      * The position without the key: a block appended to A's token and signed by X's key is
        not A's block, although A held the token there; and X, who signed it, did not hold it.
    CONTROL: A, who did sign block 1 of chain1, revokes it."""
    r, w = chain.routes, chain.w
    m = w.peer("M")
    w.sql("INSERT INTO peer_keys (peer_id, kid, public_key, active) VALUES (%s,%s,%s,TRUE)",
          (m.id, "k-planted", b64u_key(chain.a)))
    assert r.revoke(m, token=chain.chain1, block=1).outcome() == (403, DENIED_NS)
    forged = w.delegate(chain.base, chain.a, chain.x, signer=chain.x)
    assert r.revoke(chain.a, token=forged, block=1).outcome() == (403, DENIED_NS)
    assert r.revoke(chain.x, token=forged, block=1).outcome() == (403, DENIED_NS)
    assert r.revocations(by=[m, chain.a, chain.x]) == []
    assert r.revoke(chain.a, token=chain.chain1, block=1).status == 200


def test_a_signer_whose_key_left_its_grace_window_cannot_revoke(chain):
    """Decision 6's other half: the signer's key must be LIVE now, by the walk's grace rule
    (active, or revoked with the window still open). A, whose only key was revoked a minute ago,
    cannot revoke the block it signed with it; that block already fails liveness at every use.
    CONTROL: the same key, revoked with its grace window still open (revoked_at an hour ahead),
    revokes (slice 5's review, F4)."""
    r, w = chain.routes, chain.w
    w.sql("UPDATE peer_keys SET active = FALSE, revoked_at = now() - interval '1 minute' "
          "WHERE peer_id = %s", (chain.a.id,))
    got = r.revoke(chain.a, token=chain.chain1, block=1)
    assert got.outcome() == (403, DENIED_NS), got
    assert "signed a delegation block" in got.body["message"], got.body
    assert r.revocations(by=chain.a) == []
    w.sql("UPDATE peer_keys SET revoked_at = now() + interval '1 hour' WHERE peer_id = %s",
          (chain.a.id,))
    got = r.revoke(chain.a, token=chain.chain1, block=1)
    assert got.status == 200 and got.body["standing"] == "signer", got


# ================================================================== what is written

def test_the_revocation_row_and_its_event_row(chain):
    """2.5 (a): capability_revocation (the block id, revoked_by_peer the caller, the reason,
    not_after the token's own expiry from the register) and an append-only revoke event (the
    actor, the token's authority id, the block id, the reason), in one transaction."""
    r = chain.routes
    register = r.issue_rows(chain.base.revocation_ids[0])[0]
    got = r.revoke(chain.e, token=chain.chain2, block=2, reason="task 42 closed")
    assert got.status == 200, got
    rows = r.revocations(chain.chain2.revocation_ids[2])
    assert [(bytes(x["revocation_id"]).hex(), str(x["revoked_by_peer"]), x["reason"],
             x["not_after"]) for x in rows] == [
        (chain.chain2.revocation_ids[2], chain.e.id, "task 42 closed", register["expires_at"])]
    revoke_events = [e for e in r.events(chain.base.revocation_ids[0]) if e["event"] == "revoke"]
    assert [(str(e["actor_peer_id"]), bytes(e["revocation_id"]).hex(), e["reason"],
             e["issuer_peer_id"], e["namespace"], e["expires_at"]) for e in revoke_events] == [
        (chain.e.id, chain.chain2.revocation_ids[2], "task 42 closed", None, None, None)]
    assert got.body["revoked"] == {
        "revocation_id": chain.chain2.revocation_ids[2], "block": 2,
        "authority_id": chain.base.revocation_ids[0], "namespace": chain.ns,
        "not_after": register["expires_at"].astimezone(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "revoked_at": rows[0]["revoked_at"].astimezone(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")}, got.body
    by_id = r.revoke(chain.o, authority_id=chain.base.revocation_ids[0], reason="done")
    assert by_id.status == 200
    assert [str(x["revoked_by_peer"]) for x in r.revocations(chain.base.revocation_ids[0])] == [
        chain.o.id]


def test_the_revocation_and_its_event_commit_together_or_not_at_all(chain, monkeypatch):
    """If the event row cannot be written, the revocation is not written either: one
    transaction. The event writer fails AFTER writing its row, and the error envelope
    (apps/core/jsonerrors.py) answers 500. CONTROL: the same revoke, with the event writer
    restored, lands."""
    from apps.memoryskills import capability_issue
    r = chain.routes
    real = capability_issue.write_revoke_event
    calls = []

    def failing(cur, **kwargs):
        real(cur, **kwargs)
        calls.append(kwargs["revocation_id"])
        raise RuntimeError("the event log refused the row")

    monkeypatch.setattr(capability_issue, "write_revoke_event", failing)
    got = r.revoke(chain.e, token=chain.chain2, block=2)
    assert calls == [chain.chain2.revocation_ids[2]], "the failing writer never ran"
    assert got.outcome() == (500, "E_INTERNAL"), got
    assert r.revocations(chain.chain2.revocation_ids[2]) == []
    assert [e for e in r.events(chain.base.revocation_ids[0]) if e["event"] == "revoke"] == []
    assert chain.uses()["chain2"] == "ALLOW"
    monkeypatch.setattr(capability_issue, "write_revoke_event", real)
    assert r.revoke(chain.e, token=chain.chain2, block=2).status == 200
    assert chain.uses()["chain2"] == REVOKED


def test_a_block_is_revoked_once(chain):
    """A second revoke of the same block is 409 and writes nothing more. CONTROL: another
    block of the same token is still revocable."""
    r = chain.routes
    assert r.revoke(chain.e, token=chain.chain2, block=2).status == 200
    again = r.revoke(chain.e, token=chain.chain2, block=2)
    assert again.outcome() == (409, CONFLICT), again
    assert len(r.revocations(chain.chain2.revocation_ids[2])) == 1
    assert len([e for e in r.events(chain.base.revocation_ids[0])
                if e["event"] == "revoke"]) == 1
    assert r.revoke(chain.a, token=chain.chain2, block=1).status == 200


def test_an_unregistered_token_and_an_unknown_authority_id_are_not_found(chain):
    """The register is where the revoke route finds a token's issuer (2.5 (c)): an authority id
    it does not hold, and a token signed by this root but never minted (T19's shape), are 404
    even for the peer the token names as issuer. CONTROL: the registered token, revoked by its
    issuer."""
    r, w = chain.routes, chain.w
    assert r.revoke(chain.o, authority_id="ab" * 64).outcome() == (404, NOT_FOUND)
    forged = w.mint(issuer=chain.o, subject=chain.a, ns=chain.ns, register=False)
    assert r.revoke(chain.o, token=forged, block=0).outcome() == (404, NOT_FOUND)
    assert r.revoke(chain.o, authority_id=forged.revocation_ids[0]).outcome() == (404,
                                                                                 NOT_FOUND)
    # A register row for the token's authority id that names ANOTHER issuer (or subject) is
    # not this token's issuance, as the verifier's register clause reads it (slice 4, F3): the
    # peer the row names is not made the token's issuer.
    misfiled = w.mint(issuer=chain.o, subject=chain.a, ns=chain.ns, register=False)
    w.register(misfiled, issuer=chain.x.id, subject=chain.a.id, ns=chain.ns,
               rights=(("", "read"),), depth_cap=2,
               expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=30),
               kid=ROOT_KID)
    assert r.revoke(chain.x, token=misfiled, block=0).outcome() == (404, NOT_FOUND)
    # And one naming the token's issuer but ANOTHER subject: the issuer presenting the token is
    # not answered from it (slice 5's review, I1).
    missubjected = w.mint(issuer=chain.o, subject=chain.a, ns=chain.ns, register=False)
    w.register(missubjected, issuer=chain.o.id, subject=chain.x.id, ns=chain.ns,
               rights=(("", "read"),), depth_cap=2,
               expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=30),
               kid=ROOT_KID)
    assert r.revoke(chain.o, token=missubjected, block=0).outcome() == (404, NOT_FOUND)
    assert r.revocations(by=[chain.o, chain.x]) == []
    assert r.revoke(chain.o, token=chain.base, block=0).status == 200


def test_an_invalid_presented_token_is_refused_as_invalid(chain):
    """The presented token must verify under this server's root keys (the verifier's parse,
    3.2 step 1): tampered bytes and a foreign root are E_CAPABILITY_INVALID, and the refusal
    repeats nothing of the token. CONTROL: the untampered token revokes."""
    import biscuit_auth
    r, w = chain.routes, chain.w
    text = chain.chain2.to_base64()
    foreign = biscuit_auth.BiscuitBuilder("cap_version(1);").build(
        biscuit_auth.KeyPair().private_key)
    for bad in (text[:-6] + "AAAAAA", foreign.to_base64(), "not-a-biscuit"):
        got = r.revoke(chain.e, token=bad, block=0)
        assert got.outcome() == (403, INVALID), got
        assert not scan(text, {"body": got.text()})
    assert r.revoke(chain.e, token=text, block=2).status == 200


def test_the_revoke_body(chain):
    """One form or the other: with the token in Meshychat-Capability, `block` and `reason`; without
    it, `authority_id` and `reason`. Each malformed body is 422 and writes nothing; a reason
    holding a window of the presented token is refused (the revocation row is permanent: Q8).
    A reason is one line: a Unicode line or paragraph separator (U+2028, U+2029) is refused as
    a newline is, and a lone surrogate, which has no UTF-8 form, as NUL is at the mint (each
    was accepted or a 500: slice 5's review, I2 and F1). JSON the parser will not take is 400.
    CONTROLS: each form, well formed, revokes."""
    r = chain.routes
    t = chain.chain2
    aid = chain.base.revocation_ids[0]
    text = t.to_base64()
    bad = {
        "token and id": dict(token=t, body={"block": 2, "authority_id": aid, "reason": "x"}),
        "token, no block": dict(token=t, body={"reason": "x"}),
        "block, no token": dict(body={"block": 2, "reason": "x"}),
        "neither": dict(body={"reason": "x"}),
        "block out of range": dict(token=t, body={"block": 3, "reason": "x"}),
        "block negative": dict(token=t, body={"block": -1, "reason": "x"}),
        "block a bool": dict(token=t, body={"block": True, "reason": "x"}),
        "block a string": dict(token=t, body={"block": "2", "reason": "x"}),
        "id not hex": dict(body={"authority_id": "zz" * 64, "reason": "x"}),
        "id too short": dict(body={"authority_id": aid[:-2], "reason": "x"}),
        "id upper case": dict(body={"authority_id": aid.upper(), "reason": "x"}),
        "no reason": dict(token=t, body={"block": 2}),
        "empty reason": dict(token=t, body={"block": 2, "reason": ""}),
        "long reason": dict(token=t, body={"block": 2, "reason": "r" * 201}),
        "reason not a string": dict(token=t, body={"block": 2, "reason": 5}),
        "reason with a newline": dict(token=t, body={"block": 2, "reason": "a\nb"}),
        "reason with a line separator": dict(token=t, body={"block": 2, "reason": "a\u2028b"}),
        "reason with a paragraph separator": dict(token=t, body={"block": 2,
                                                                 "reason": "a\u2029b"}),
        "reason with a lone surrogate": dict(token=t, body={"block": 2, "reason": "r\ud800"}),
        "the token in the reason": dict(token=t, body={"block": 2,
                                                       "reason": "see " + text[-40:-10]}),
        "unknown field": dict(token=t, body={"block": 2, "reason": "x", "cascade": True}),
    }
    for name, kw in bad.items():
        got = r.revoke(chain.e, **kw)
        assert got.outcome() == (422, VALIDATION), (name, got)
    # By id, from the issuer, so the request reaches the row the reason is written to.
    got = r.revoke(chain.o, authority_id=aid, reason="r\udc00")
    assert got.outcome() == (422, VALIDATION), got
    for name, raw in (("an integer over 4300 digits", '{"block": ' + "1" * 5000 + "}"),
                      ("nesting past the recursion limit", '{"a":' * 50000 + "1" + "}" * 50000)):
        got = r.post(REVOKE, None, caller=chain.e, raw=raw, token=text)
        assert got.outcome() == (400, VALIDATION), (name, got)
    assert r.revocations(by=[chain.e, chain.o]) == []
    assert r.revoke(chain.e, token=t, block=2, reason="r" * 200).status == 200
    assert r.revoke(chain.o, authority_id=aid).status == 200


def test_the_revoke_is_a_post_so_dpop_is_enforced(chain):
    """A POST: with DPoP on, a cnf-bound JWT without a proof is refused and revokes nothing;
    with a proof bound to this method, path and JWT, it revokes. The proof binds the JWT, not
    the capability (3.1)."""
    from apps.authapi.didkey import to_did_key
    from apps.crypto import primitives as P
    r = chain.routes
    seed = os.urandom(32)
    jwt = peer_jwt(chain.e.id, cnf=to_did_key(P.public_key_of(seed)))
    body = {"block": 2, "reason": "dpop"}
    with override_settings(MESHY_DPOP_ENFORCE=True):
        bare = r.revoke(None, token=chain.chain2, body=body,
                        headers={"authorization": f"Bearer {jwt}"})
        proved = r.revoke(None, token=chain.chain2, body=body, headers={
            "authorization": f"Bearer {jwt}",
            "DPoP": P.dpop_wire(seed, "POST", REVOKE, jwt)})
    assert bare.outcome() == (403, "E_FORBIDDEN"), bare
    assert proved.status == 200, proved
    assert len(r.revocations(by=chain.e)) == 1


def test_the_revoke_is_a_post(routes):
    o = routes.w.peer("O")
    got = routes.client.get(REVOKE, headers={"authorization": f"Bearer {peer_jwt(o.id)}"})
    assert got.status_code == 405 and got.headers.get("Allow") == "POST"


# ================================================================== T14 at the revoke

def test_t14_no_response_row_or_log_line_but_the_mint_response_holds_a_token(routes, caplog):
    """Drive the mint, the revokes by token and by id, and every refusal the revoke has
    (not found, no standing, above the signer's block, a second revoke, an invalid token, a
    malformed body), then scan every response but the mints', the register, revocation and
    event rows of this test, and the log records and formatted log lines, for every token
    used. POSITIVE CONTROLS: each mint response holds its token, and a planted window is found.
    """
    w = routes.w
    o, a, e, f, x = (w.peer(t) for t in ("O", "A", "E", "F", "X"))
    ns = w.claim(o)
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
            token, base, minted = routes.minted(o, subject=a, ns=ns, depth_cap=4)
            other, other_tok, other_minted = routes.minted(o, subject=o, ns=ns)
            chain1 = w.delegate(base, a, e)
            chain2 = w.delegate(chain1, e, f)
            outcomes = [
                routes.revoke(x, token=chain2, block=2).status,                    # 403
                routes.revoke(f, token=chain2, block=2).status,                    # 403
                routes.revoke(e, token=chain2, block=1).status,                    # 403 above
                routes.revoke(e, token=chain2.to_base64()[:-6] + "AAAAAA",
                              block=2).status,                                     # 403 invalid
                routes.revoke(e, token=chain2, block=9).status,                    # 422
                routes.revoke(o, authority_id="cd" * 64).status,                   # 404
                routes.revoke(e, token=chain2, block=2).status,                    # 200
                routes.revoke(e, token=chain2, block=2).status,                    # 409
                routes.revoke(a, token=chain1, block=1).status,                    # 200
                routes.revoke(o, authority_id=other_tok.revocation_ids[0]).status,  # 200
                routes.revoke(o, token=base, block=0).status,                      # 200
            ]
    finally:
        for lg, level in saved:
            lg.removeHandler(handler)
            lg.setLevel(level)
    assert outcomes == [403, 403, 403, 403, 422, 404, 200, 409, 200, 200, 200], outcomes
    mints = [minted, other_minted]
    aids = [base.revocation_ids[0], other_tok.revocation_ids[0]]
    events = [row for aid in aids for row in routes.events(aid)]
    revocations = routes.revocations(by=[o, a, e])

    def rows_text(rows):
        return "\n".join(json.dumps(row, default=lambda v: bytes(v).hex() if isinstance(
            v, (bytes, memoryview)) else str(v)) for row in rows)

    places = {"responses but the mints": "\n".join(s.text() for s in routes.seen
                                                   if s not in mints),
              "register and event rows": rows_text(events),
              "revocation rows": rows_text(revocations),
              "log records": "\n".join(f"{rec.name} {rec.getMessage()}"
                                       for rec in caplog.records),
              "formatted log": formatted.getvalue()}
    assert len(revocations) == 4 and len(events) == 6, (len(revocations), len(events))
    for where, text in places.items():
        assert text.strip(), f"control: nothing was read from {where}"
    for name, tok in (("token", token), ("other", other), ("chain1", chain1.to_base64()),
                      ("chain2", chain2.to_base64())):
        assert scan(tok, places) == {}, (name, scan(tok, places))
    assert scan(token, {"mint": minted.text()}) != {}                        # the one place
    assert scan(other, {"mint": other_minted.text()}) != {}
    assert scan(token, {"planted": "reason: " + token[-24:]}) == {"planted": ["last 24"]}
