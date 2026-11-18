"""The token is not the whole truth: the live rows decide at every use (B07 slice 4; design
docs/design/biscuit-capabilities.md 2.5 (b), 4.2; critique C2, C6; divergence 040).

ONE SQL statement, run on every use and never cached, confirms the six clauses of 2.5 (b):

  1. no revocation id the token presents is in capability_revocation   else E_CAPABILITY_REVOKED
  2. the authority id is in the issuance register (an issue row naming  else E_CAPABILITY_DENIED
     the token's issuer and subject and the REQUEST's namespace: review F3)
  3. the issuer holds admin on the namespace by meshy_namespace_permission AND is active
  4. every peer in the holder chain is active
  5. every delegation key is live by the grace-aware rule, and a key with NO row is DEAD
     (never 030's key_states reading, where unknown is "not revoked")
  6. on shared/*, every chain peer's owner is dev-plane                  else
     E_PRODUCT_NO_SHARED_GRANT, with an audit_tenancy_denial row (I2's grant half, at use)

And check_permission with a principal never falls through to the caller's own grants (4.2).

The tests, by the design's plan (6.1): T6 (no fall-through, unit level), T7 (liveness through
DB state, each case beside an unrelated token that keeps working), T18 (plane at use), T14 (the
token is never in a refusal, a register or revocation row, an audit row or a log line).

Run:  pytest tests/test_capability_liveness.py -q
Needs the dev database (tests/conftest.py builds and drops its own) and CPython 3.13.
"""
from __future__ import annotations

import ast
import datetime as _dt
import io
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.memoryskills.permissions import Key, Prefix  # noqa: E402
from tests.capability_world import ROOT_KID, b64u, utcnow, world_context  # noqa: E402

INVALID, HOLDER, REVOKED, DENIED = ("E_CAPABILITY_INVALID", "E_CAPABILITY_HOLDER",
                                    "E_CAPABILITY_REVOKED", "E_CAPABILITY_DENIED")
NOT_ACCEPTED, UNAVAILABLE = "E_CAPABILITY_NOT_ACCEPTED", "E_CAPABILITY_UNAVAILABLE"
PLANE = "E_PRODUCT_NO_SHARED_GRANT"


@pytest.fixture()
def world():
    with world_context() as w:
        yield w


def _code(result) -> str:
    return result[0]


# ============================================================================ T6: no fall-through

def test_t6_a_caller_with_write_presents_a_read_token_and_its_write_is_refused(world):
    """C holds namespace-wide WRITE by a grant row. With a read-only token, its write is
    refused: the token is the whole authority for the request. The same write with no
    principal is allowed by the grant (the control), and the token's own read is allowed."""
    o, c = world.peer("O"), world.peer("C")
    ns = world.claim(o)
    world.grant(ns, c, o, "write")
    tok = world.mint(issuer=o, subject=c, ns=ns, rights=(("", "read"),))
    assert _code(world.check(tok, c, ns, "write", Key("k"))) == DENIED
    assert world.check_without(c, ns, "write", Key("k"))[0] == "ALLOW"
    assert world.check(tok, c, ns, "read", Key("k"))[0] == "ALLOW"


def test_t6_a_token_that_fails_verification_never_falls_through_to_the_grant(world):
    """Invalid, not-the-holder and revoked: each refused with the token's code, although the
    caller's own grant would allow the request."""
    o, c, t = world.peer("O"), world.peer("C"), world.peer("T")
    ns = world.claim(o)
    world.grant(ns, c, o, "write")
    good = world.mint(issuer=o, subject=c, ns=ns, rights=(("", "write"),))
    tampered = good.to_base64()[:-6] + "AAAAAA"
    stolen = world.mint(issuer=o, subject=t, ns=ns, rights=(("", "write"),))
    revoked = world.mint(issuer=o, subject=c, ns=ns, rights=(("", "write"),))
    world.revoke(revoked.revocation_ids[0], o)
    assert _code(world.check(tampered, c, ns, "write", Key("k"))) == INVALID
    assert _code(world.check(stolen, c, ns, "write", Key("k"))) == HOLDER
    assert _code(world.check(revoked, c, ns, "write", Key("k"))) == REVOKED
    assert world.check(good, c, ns, "write", Key("k"))[0] == "ALLOW"            # control


# ============================================================================ T7: liveness

@pytest.fixture()
def t7(world):
    """Owner O; issuer I, an admin by grant (not the owner, so it can be demoted); a chain
    I -> A -> E -> F, where F presents; and an UNRELATED token O minted for itself, which must
    keep working after every change below."""
    o, i = world.peer("O"), world.peer("I")
    ns = world.claim(o)
    world.grant(ns, i, o, "admin")
    a, e, f = world.peer("A"), world.peer("E"), world.peer("F")
    tok = world.mint(issuer=i, subject=a, ns=ns, rights=(("", "read"),), depth_cap=4)
    chain = world.delegate(world.delegate(tok, a, e), e, f)
    unrelated = world.mint(issuer=o, subject=o, ns=ns)

    class T7:
        pass

    t = T7()
    t.world, t.o, t.i, t.a, t.e, t.f, t.ns, t.tok, t.chain, t.unrelated = (
        world, o, i, a, e, f, ns, tok, chain, unrelated)

    def presents():
        return world.decide(chain, f, ns, Key("k"))

    def unrelated_works():
        return world.decide(unrelated, o, ns, Key("k"))[0] == "ALLOW"

    t.presents, t.unrelated_works = presents, unrelated_works
    assert presents()[0] == "ALLOW", presents()                   # the control, before
    assert unrelated_works()
    return t


def test_t7_revoking_the_authority_id_kills_the_token_and_every_descendant(t7):
    t7.world.revoke(t7.tok.revocation_ids[0], t7.i)
    assert _code(t7.presents()) == REVOKED
    assert _code(t7.world.decide(t7.tok, t7.a, t7.ns, Key("k"))) == REVOKED
    assert t7.unrelated_works()


def test_t7_revoking_a_mid_chain_id_kills_that_branch_only(t7):
    w = t7.world
    g = w.peer("G")
    sibling = w.delegate(t7.tok, t7.a, g)                  # A's other branch
    assert t7.chain.revocation_ids[1] != sibling.revocation_ids[1]
    w.revoke(t7.chain.revocation_ids[1], t7.e)             # the A -> E block
    assert _code(t7.presents()) == REVOKED
    assert w.decide(sibling, g, t7.ns, Key("k"))[0] == "ALLOW"
    assert t7.unrelated_works()


def test_t7_demoting_the_issuer_ends_its_tokens(t7):
    t7.world.sql("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = now() "
                 "WHERE namespace = %s AND grantee_peer_id = %s", (t7.ns, t7.i.id))
    assert _code(t7.presents()) == DENIED
    assert t7.unrelated_works()


def test_t7_suspending_the_issuer_ends_its_tokens_while_its_grant_row_stays_live(t7):
    """Critique C6: check_permission reads no peer status, so a suspended admin still passes
    it; the capability's issuer clause reads status too."""
    t7.world.sql("UPDATE peers SET status = 'suspended' WHERE id = %s", (t7.i.id,))
    assert t7.world.check_without(t7.i, t7.ns, "admin", Key("k"))[0] == "ALLOW", (
        "the case check_permission alone passes")
    assert _code(t7.presents()) == DENIED
    assert t7.unrelated_works()


@pytest.mark.parametrize("who", ["a", "e", "f"])
def test_t7_suspending_any_peer_in_the_holder_chain_ends_it(t7, who):
    t7.world.sql("UPDATE peers SET status = 'suspended' WHERE id = %s",
                 (getattr(t7, who).id,))
    assert _code(t7.presents()) == DENIED
    assert t7.unrelated_works()


def test_t7_a_delegation_key_rotated_past_grace_ends_the_chain_inside_grace_it_does_not(t7):
    w = t7.world
    w.sql("UPDATE peer_keys SET active = FALSE, revoked_at = now() + interval '1 hour' "
          "WHERE peer_id = %s", (t7.e.id,))
    assert t7.presents()[0] == "ALLOW", "inside the revocation grace window the key is live"
    w.sql("UPDATE peer_keys SET revoked_at = now() - interval '1 minute' WHERE peer_id = %s",
          (t7.e.id,))
    # The walk refuses first: E's block is no longer signed by a LIVE key of E's. (Clause 5
    # of the liveness statement would refuse it too; the test below holds that on its own.)
    assert _code(t7.presents()) == HOLDER
    assert t7.unrelated_works()


def test_t7_deleting_the_delegation_keys_row_ends_the_chain(t7):
    t7.world.sql("DELETE FROM peer_keys WHERE peer_id = %s", (t7.e.id,))
    assert _code(t7.presents()) == HOLDER
    assert t7.unrelated_works()


def test_the_liveness_statement_holds_an_unknown_or_rotated_key_dead_on_its_own(t7,
                                                                             monkeypatch):
    """Clause 5 is not the walk's check twice: between the walk and the statement a key's row
    can go (a rotation, a delete). The walk's key read is held at its answer from BEFORE the
    change, so only the statement stands between the chain and an ALLOW. Unknown is DEAD."""
    from apps.memoryskills import capability
    w = t7.world
    snapshot = {}
    real = capability._live_keys

    def frozen(conn, peer_ids, now):
        key = tuple(sorted(peer_ids))
        if key not in snapshot:
            snapshot[key] = real(conn, peer_ids, now)
        return snapshot[key]

    monkeypatch.setattr(capability, "_live_keys", frozen)
    assert t7.presents()[0] == "ALLOW"                      # control: the snapshot is taken
    w.sql("DELETE FROM peer_keys WHERE peer_id = %s", (t7.e.id,))
    assert t7.presents() == (DENIED, capability.CLAUSE_MESSAGES["keys"])
    w.sql("INSERT INTO peer_keys (peer_id, kid, public_key, active, revoked_at) "
          "VALUES (%s,%s,%s,FALSE, now() - interval '1 minute')",
          (t7.e.id, "k-rotated", b64u(t7.e.public_bytes)))
    assert t7.presents() == (DENIED, capability.CLAUSE_MESSAGES["keys"])


def test_the_liveness_is_one_statement_holding_all_six_clauses():
    """3.2 step 4 and 2.5 (b): ONE SQL statement. Exactly one string in the module reads
    capability_revocation, and it reads every table the six clauses need."""
    src = (ROOT / "apps" / "memoryskills" / "capability.py").read_text(encoding="utf-8")
    strings = [n.value for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "capability_revocation" in n.value and "SELECT" in n.value]
    assert len(strings) == 1, strings
    sql = strings[0]
    for needle in ("capability_revocation", "capability_event", "meshychat_namespace_permission",
                   "status", "peer_keys", "revoked_at", "dashboard_users", "plane",
                   "shared/"):
        assert needle in sql, needle


# ============================================================================ T18: plane at use

def _dispatch_check(world, monkeypatch, caller, tok, ns, resource):
    """check_permission with a principal, reached through handlers.dispatch as slice 6's
    handlers reach it: the handler raises inside server_internal, the transaction rolls back, and
    dispatch then writes the refusal's deferred audit row."""
    from apps.core import rls
    from apps.memoryskills import capability, handlers
    from apps.memoryskills.errors import HandlerError
    from apps.memoryskills.permissions import check_permission

    token = tok.to_base64()

    def reads(verified_payload, caller_peer_id, conn):
        with rls.server_internal(conn):
            return check_permission(ns, caller_peer_id, "read", conn, resource=resource,
                                    principal=capability.PresentedCapability(token,
                                                                             "memory.read"))

    monkeypatch.setitem(handlers.DISPATCH, "memory.read", reads)
    envelope = {"payload": {"kind": "skill.call", "caller_peer_id": caller.id,
                            "skill_name": "memory.read"}}
    try:
        return "ALLOW", handlers.dispatch("memory.read", envelope, caller.id, world.app)
    except HandlerError as exc:
        return exc.code, exc.message


def _audits(world, caller) -> list:
    with world.owner.cursor() as cur:
        cur.execute("SELECT denial_class, op, caller_peer_id::text, namespace, "
                    "target_owner_id::text FROM tenancy_denials WHERE caller_peer_id = %s",
                    (caller.id,))
        return cur.fetchall()


def test_t18_a_hand_off_on_shared_to_a_product_plane_peer_is_refused_with_an_audit_row(
        world, monkeypatch):
    """Critique C2: I2's grant half forbids a product-plane peer holding ANY grant on shared/*,
    and a token is a grant the server never sees being passed on. A dev-plane holder delegates
    read on a shared namespace to a product-plane peer P: refused at use, and audited. The same
    hand-off to a dev-plane peer D reads (the control)."""
    o, a = world.peer("O"), world.peer("A")
    ns = world.claim(o, f"shared/t18-{o.id[:8]}")
    tok = world.mint(issuer=o, subject=a, ns=ns, rights=(("", "read"),))
    p = world.peer("P", plane="product")
    d = world.peer("D")
    to_p, to_d = world.delegate(tok, a, p), world.delegate(tok, a, d)
    assert _dispatch_check(world, monkeypatch, p, to_p, ns, Key("k"))[0] == PLANE
    assert _audits(world, p) == [("product_no_shared_grant", "memory.grant", p.id, ns,
                                  p.owner_id)]
    assert _dispatch_check(world, monkeypatch, d, to_d, ns, Key("k"))[0] == "ALLOW"
    assert _audits(world, d) == []


def test_t18_a_product_plane_subject_on_shared_is_refused_at_use(world):
    """The mint route refuses such a subject (slice 5); the verifier refuses it again at use,
    since the register row could be written by any path."""
    o, p = world.peer("O"), world.peer("P", plane="product")
    ns = world.claim(o, f"shared/t18s-{o.id[:8]}")
    tok = world.mint(issuer=o, subject=p, ns=ns)
    assert _code(world.decide(tok, p, ns, Key("k"))) == PLANE
    mid = world.peer("M", plane="product")
    d = world.peer("D")
    via = world.delegate(world.delegate(world.mint(issuer=o, subject=o, ns=ns), o, mid), mid, d)
    assert _code(world.decide(via, d, ns, Key("k"))) == PLANE, (
        "a product-plane peer in the MIDDLE of the chain is in the chain")


def test_t18_the_plane_clause_is_shared_only(world):
    """Outside shared/* a product-plane holder is decided like any other (the control that
    the clause keys on the namespace, not on the plane alone)."""
    o, p = world.peer("O"), world.peer("P", plane="product")
    ns = world.claim(o)
    tok = world.mint(issuer=o, subject=p, ns=ns)
    assert world.decide(tok, p, ns, Key("k"))[0] == "ALLOW"


# ================================================ the register row binds the request (review F3)

def _register_message():
    from apps.memoryskills import capability
    return capability.CLAUSE_MESSAGES["register"]


def test_a_token_on_another_namespace_learns_nothing_of_its_issuers_standing_there(world):
    """Review F3 (a). Clause 3 reads the issuer's admin on the REQUEST's namespace, and ran
    before the Datalog that compares it with the token's one namespace (2.3). So a holder
    presenting its token on other namespaces learned where the issuer is admin: "does not
    allow this request" where it is, "no longer holds admin" where it is not. The register
    row names the token's namespace, and the register clause now requires it to be the
    request's: both answers are the register's. CONTROL: the token's own namespace allows."""
    i, h, x = world.peer("I"), world.peer("H"), world.peer("X")
    home = world.claim(i)
    also_admin = world.claim(i)
    not_admin = world.claim(x)
    tok = world.mint(issuer=i, subject=h, ns=home)
    assert world.decide(tok, h, home, Key("k"))[0] == "ALLOW"
    there = world.decide(tok, h, also_admin, Key("k"))
    elsewhere = world.decide(tok, h, not_admin, Key("k"))
    assert there == elsewhere == (DENIED, _register_message()), (there, elsewhere)


def test_a_token_for_another_namespace_writes_no_plane_audit_on_shared(world, monkeypatch):
    """Review F3 (b). A product-plane holder of a token for an ordinary namespace presents it
    on a shared/* namespace its issuer administers. The plane clause refused it with
    E_PRODUCT_NO_SHARED_GRANT, and dispatch wrote a tenancy_denials row naming a namespace the
    token never covered: one audit row per request, on demand. Now the register refuses
    first, and nothing is written. CONTROL: T18, a token FOR shared/*, is still refused by the
    plane clause with its audit row."""
    o, p = world.peer("O"), world.peer("P", plane="product")
    home = world.claim(o)
    shared = world.claim(o, f"shared/f3-{o.id[:8]}")
    tok = world.mint(issuer=o, subject=p, ns=home)
    assert world.decide(tok, p, home, Key("k"))[0] == "ALLOW"
    got = _dispatch_check(world, monkeypatch, p, tok, shared, Key("k"))
    assert got == (DENIED, _register_message()), got
    assert _audits(world, p) == []


@pytest.mark.parametrize("column", ["namespace", "issuer", "subject"])
def test_the_register_row_must_name_the_tokens_namespace_issuer_and_subject(world, column):
    """The issue row is what the mint recorded for THIS token (2.5 (c)): the register clause
    matches its namespace to the request's, and its issuer and subject to the token's signed
    ones. A row with the token's authority id and one other value does not register it.
    CONTROL: the same token, registered with every value its own, is allowed."""
    o, s, x = world.peer("O"), world.peer("S"), world.peer("X")
    ns = world.claim(o)
    other_ns = world.claim(x)
    tok = world.mint(issuer=o, subject=s, ns=ns, register=False)
    row = {"issuer": o.id, "subject": s.id, "ns": ns}
    if column == "namespace":
        row["ns"] = other_ns
    else:
        row[column] = x.id
    world.register(tok, **row, rights=(("", "read"),), depth_cap=2,
                   expires_at=utcnow() + _dt.timedelta(minutes=30), kid=ROOT_KID)
    assert world.decide(tok, s, ns, Key("k")) == (DENIED, _register_message())
    good = world.mint(issuer=o, subject=s, ns=ns, register=False)
    world.register(good, issuer=o.id, subject=s.id, ns=ns, rights=(("", "read"),),
                   depth_cap=2, expires_at=utcnow() + _dt.timedelta(minutes=30), kid=ROOT_KID)
    assert world.decide(good, s, ns, Key("k"))[0] == "ALLOW"


# ============================================================================ T14: never echoed

def _scan(token: str, texts: dict) -> dict:
    """{where: what} for every place the token, or its first 16 characters, occurs."""
    found = {}
    for where, text in texts.items():
        hits = [label for label, needle in (("token", token), ("first 16", token[:16]))
                if needle in text]
        if hits:
            found[where] = hits
    return found


def test_t14_no_refusal_row_or_log_line_holds_the_token(world, monkeypatch, caplog):
    """Drive every refusal this slice has (invalid, holder, revoked, denied, not accepted,
    unavailable, and the plane refusal with its audit row), then scan the refusal messages,
    the exceptions' text, the register and revocation rows, the audit rows, the log records
    and the formatted log output for each token and for its first 16 characters."""
    from apps.memoryskills import capability_keys

    o, a, t = world.peer("O"), world.peer("A"), world.peer("T")
    ns = world.claim(o)
    shared = world.claim(o, f"shared/t14-{o.id[:8]}")
    tokens = {
        "invalid": world.mint(issuer=o, subject=a, ns=ns).to_base64()[:-6] + "AAAAAA",
        "holder": world.mint(issuer=o, subject=a, ns=ns),
        "revoked": world.mint(issuer=o, subject=a, ns=ns),
        "denied": world.mint(issuer=o, subject=a, ns=ns, rights=(("x/", "read"),)),
        "not accepted": world.mint(issuer=o, subject=a, ns=ns),
        "unavailable": world.mint(issuer=o, subject=a, ns=ns),
    }
    world.revoke(tokens["revoked"].revocation_ids[0], o)
    b64 = {k: v if isinstance(v, str) else v.to_base64() for k, v in tokens.items()}
    # The records (caplog) and the lines they become (a formatter like settings.LOGGING's),
    # from the root logger AND `meshychat`, which does not propagate to it (settings.LOGGING).
    formatted = io.StringIO()
    handler = logging.StreamHandler(formatted)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    loggers = [logging.getLogger(n) for n in ("", "meshychat")]
    saved = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.addHandler(caplog.handler)
        lg.setLevel(logging.DEBUG)
    caplog.handler.setLevel(logging.DEBUG)
    results = {}
    try:
        results["invalid"] = world.check(b64["invalid"], a, ns, "read", Key("k"))
        results["holder"] = world.check(b64["holder"], t, ns, "read", Key("k"))
        results["revoked"] = world.check(b64["revoked"], a, ns, "read", Key("k"))
        results["denied"] = world.check(b64["denied"], a, ns, "read", Key("k"))
        results["not accepted"] = world.check(b64["not accepted"], a, ns, "read",
                                              Prefix(""), operation="memory.read")
        previous = capability_keys.install(None)
        try:
            results["unavailable"] = world.check(b64["unavailable"], a, ns, "read", Key("k"))
        finally:
            capability_keys.install(previous)
        p_peer = world.peer("P2", plane="product")
        plane_tok = world.delegate(world.mint(issuer=o, subject=a, ns=shared), a, p_peer)
        b64["plane"] = plane_tok.to_base64()
        results["plane"] = _dispatch_check(world, monkeypatch, p_peer, plane_tok, shared,
                                           Key("k"))
    finally:
        for lg, level in saved:
            lg.removeHandler(handler)
            if lg.name != "root":
                lg.removeHandler(caplog.handler)
            lg.setLevel(level)
    assert {k: v[0] for k, v in results.items()} == {
        "invalid": INVALID, "holder": HOLDER, "revoked": REVOKED, "denied": DENIED,
        "not accepted": NOT_ACCEPTED, "unavailable": UNAVAILABLE, "plane": PLANE}, results

    with world.owner.cursor() as cur:
        cur.execute("SELECT row_to_json(r)::text FROM capability_event r")
        events = "\n".join(r[0] for r in cur.fetchall())
        cur.execute("SELECT row_to_json(r)::text FROM capability_revocation r")
        revocations = "\n".join(r[0] for r in cur.fetchall())
        cur.execute("SELECT row_to_json(r)::text FROM tenancy_denials r")
        audits = "\n".join(r[0] for r in cur.fetchall())
    records = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    places = {"refusal messages": "\n".join(v[1] for v in results.values()),
              "register rows": events, "revocation rows": revocations,
              "audit rows": audits, "log records": records,
              "formatted log": formatted.getvalue()}
    for name, token in b64.items():
        assert not _scan(token, places), (name, _scan(token, places))

    # POSITIVE CONTROL: the scan finds a planted token, whole or by its first 16 characters,
    # and every place it read was not empty.
    planted = dict(places, **{"refusal messages": places["refusal messages"]
                              + b64["denied"][:16]})
    assert _scan(b64["denied"], planted) == {"refusal messages": ["first 16"]}
    assert _scan(b64["denied"], {"x": "y" + b64["denied"]}) == {"x": ["token", "first 16"]}
    for where in ("refusal messages", "register rows", "revocation rows", "audit rows",
                  "log records", "formatted log"):
        assert places[where].strip(), f"control: nothing was read from {where}"
