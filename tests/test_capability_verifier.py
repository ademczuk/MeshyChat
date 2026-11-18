"""The capability verifier, dark (B07 slice 4; docs/design/biscuit-capabilities.md 1.3, 2.3-2.5,
3.2, 3.3, 4.1, 4.2, 4.5; critique C1, C3, C17; divergence 040): parse, the holder walk, the
register, cap_version, the Datalog decision, its limits, and check_permission's principal
branch. The liveness clauses that read live rows (T6, T7, T18) and the token-never-echoed scan
(T14) are tests/test_capability_liveness.py.

`authorize_capability(conn, *, token, caller_peer_id, namespace, resource, action, operation,
now)` decides in the order of 3.2, each a typed refusal:

  1. size (8 KiB) and parse under the kid provider      E_CAPABILITY_INVALID
  2. a cache of the crypto result only, keyed by sha256(token)
  3. the holder walk: every third-party block signed by a LIVE key of the current holder, one
     delegate pair under that key, a hop's expiry in the future and at most 300 s away, no
     peer twice, hops <= min(depth_cap, 4), blocks <= 10  E_CAPABILITY_INVALID or _HOLDER;
     the caller must be the final holder              E_CAPABILITY_HOLDER
  4. liveness, one SQL statement                         E_CAPABILITY_REVOKED or _DENIED
  5. Datalog: host facts as PARAMETERS, the allow requires cap_version(1), explicit limits
     (1000 facts, 100 iterations, 100 ms) on every authorizer, the walk's included; a run-limit
     error denies and is counted                         E_CAPABILITY_DENIED
  6. the decision record {via, cap_id, issuer, holder_chain, hops}

The tests named for the design's test plan (6.1): T3 holder binding with spike rows 11 and 14;
T5 over-attenuation (4.5); T10 injection; T13 the fact bomb; T15 a lifted block and a legacy
(v0) third-party block, which settles the design's inference 7; T19 the issuance register;
and the cap_version rule. Every refusal has its control beside it: the same token, or the
parent, allowed.

Run:  pytest tests/test_capability_verifier.py -q
Needs the dev database (tests/conftest.py builds and drops its own) and CPython 3.13.
"""
from __future__ import annotations

import base64
import datetime as _dt
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.memoryskills.permissions import NAMESPACE_WIDE, Key, Prefix  # noqa: E402
from tests.capability_world import ROOT_KID, utcnow, world_context  # noqa: E402

INVALID, HOLDER, REVOKED, DENIED = ("E_CAPABILITY_INVALID", "E_CAPABILITY_HOLDER",
                                    "E_CAPABILITY_REVOKED", "E_CAPABILITY_DENIED")
NOT_ACCEPTED, UNAVAILABLE = "E_CAPABILITY_NOT_ACCEPTED", "E_CAPABILITY_UNAVAILABLE"
CODES = (INVALID, HOLDER, REVOKED, DENIED, NOT_ACCEPTED, UNAVAILABLE)


@pytest.fixture()
def world():
    with world_context() as w:
        yield w


def _cap():
    from apps.memoryskills import capability
    return capability


@pytest.fixture()
def base(world):
    """Owner A of a namespace, and a token A minted for itself: read and write under task/42/,
    depth cap 4. The control most tests refuse beside."""
    a = world.peer("A")
    ns = world.claim(a)
    tok = world.mint(issuer=a, subject=a, ns=ns, depth_cap=4,
                     rights=(("task/42/", "read"), ("task/42/", "write")))
    return world, a, ns, tok


def _code(result) -> str:
    return result[0]


# ============================================================================ the codes

def test_the_six_codes_are_registered_so_none_is_coerced_to_internal():
    """errors.py's registry is closed: an unregistered code reaches the caller as E_INTERNAL
    (apps/memoryskills/errors.py). CONTROL: an unregistered one still is."""
    from apps.memoryskills.errors import VALID_ERROR_CODES, HandlerError
    for code in CODES:
        assert code in VALID_ERROR_CODES, code
        assert HandlerError(code, "m").code == code
    assert HandlerError("E_CAPABILITY_TYPO", "m").code == "E_INTERNAL"


# ============================================================================ allow, and its record

def test_the_owners_own_token_allows_and_returns_the_decision_record(base):
    world, a, ns, tok = base
    code, record = world.decide(tok, a, ns, Key("task/42/x"), action="write")
    assert code == "ALLOW", record
    assert record == {"via": "capability", "cap_id": tok.revocation_ids[0], "issuer": a.id,
                      "holder_chain": [a.id], "hops": 0}
    assert world.decide(tok, a, ns, Prefix("task/42/"), operation="memory.query")[0] == "ALLOW"


def test_the_token_decides_the_key_and_the_action(base):
    world, a, ns, tok = base
    assert _code(world.decide(tok, a, ns, Key("task/7/x"))) == DENIED
    assert _code(world.decide(tok, a, ns, Prefix("task/"), operation="memory.query")) == DENIED
    assert _code(world.decide(tok, a, "captest/other", Key("task/42/x"))) == DENIED
    ro = world.mint(issuer=a, subject=a, ns=ns, rights=(("task/42/", "read"),))
    assert _code(world.decide(ro, a, ns, Key("task/42/x"), action="write")) == DENIED
    assert _code(world.decide(ro, a, ns, Key("task/42/x"))) == "ALLOW"          # control


# ============================================================================ step 1: parse

def test_a_token_over_8_kib_is_invalid_before_it_is_parsed(base, monkeypatch):
    world, a, ns, tok = base
    cap = _cap()
    assert cap.MAX_TOKEN_BYTES == 8192
    big = world.restrict(tok, "check if resource($ns, $r), $r != {pad};",
                         {"pad": "x" * 9000})
    assert len(big.to_base64()) > 8192
    calls = []
    import biscuit_auth
    real = biscuit_auth.Biscuit.from_base64
    monkeypatch.setattr(biscuit_auth.Biscuit, "from_base64",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    code, message = world.decide(big, a, ns, Key("task/42/x"))
    assert code == INVALID and "8192" in message, (code, message)
    assert calls == [], "an oversized token was parsed"
    assert world.decide(tok, a, ns, Key("task/42/x"))[0] == "ALLOW"            # control


def test_a_malformed_tampered_foreign_or_unknown_kid_token_is_invalid(base):
    world, a, ns, tok = base
    b64 = tok.to_base64()
    tampered = b64[:40] + ("A" if b64[40] != "A" else "B") + b64[41:]
    foreign = world.mint(issuer=a, subject=a, ns=ns, root=world.b.KeyPair())
    other_kid = world.mint(issuer=a, subject=a, ns=ns, kid=ROOT_KID + 1)
    no_kid = world.mint(issuer=a, subject=a, ns=ns, kid=None)
    for label, bad in (("empty", ""), ("not base64", "!!!"), ("tampered", tampered),
                       ("foreign root", foreign), ("unknown kid", other_kid),
                       ("no kid", no_kid), ("not a string", 42)):
        assert _code(world.decide(bad, a, ns, Key("task/42/x"))) == INVALID, label
    assert world.decide(tok, a, ns, Key("task/42/x"))[0] == "ALLOW"            # control


# ============================================================================ T3: holder binding

def test_t3_a_token_is_usable_only_by_its_holder(base):
    """Spike rows 1, 2 and 6: A's token presented by T is refused; by A it passes; once A
    delegates to E, E holds it and A (the delegator) is refused."""
    world, a, ns, tok = base
    t, e = world.peer("T"), world.peer("E")
    assert world.decide(tok, a, ns, Key("task/42/x"))[0] == "ALLOW"
    assert _code(world.decide(tok, t, ns, Key("task/42/x"))) == HOLDER
    ch = world.delegate(tok, a, e)
    assert world.decide(ch, e, ns, Key("task/42/x"))[0] == "ALLOW"
    assert _code(world.decide(ch, a, ns, Key("task/42/x"))) == HOLDER
    assert _code(world.decide(ch, t, ns, Key("task/42/x"))) == HOLDER


def test_t3_spike_row_11_a_block_the_thief_signs_naming_itself_is_refused(base):
    world, a, ns, tok = base
    e, t = world.peer("E"), world.peer("T")
    ch = world.delegate(tok, a, e)
    stolen = world.delegate(ch, e, t, signer=t)          # T signs delegate(T) with its own key
    assert _code(world.decide(stolen, t, ns, Key("task/42/x"))) == HOLDER
    assert _code(world.decide(stolen, e, ns, Key("task/42/x"))) == HOLDER
    assert world.decide(ch, e, ns, Key("task/42/x"))[0] == "ALLOW"             # control


def test_t3_spike_row_14_a_first_party_block_naming_a_delegate_changes_nothing(base):
    world, a, ns, tok = base
    e, t = world.peer("E"), world.peer("T")
    ch = world.delegate(tok, a, e)
    plain = world.restrict(ch, "delegate({t}); delegation_expires({x});",
                           {"t": t.id, "x": utcnow() + _dt.timedelta(seconds=60)})
    assert _code(world.decide(plain, t, ns, Key("task/42/x"))) == HOLDER
    assert world.decide(plain, e, ns, Key("task/42/x"))[0] == "ALLOW", (
        "the holder is still E: the first-party block is inert")


def test_a_block_that_restates_the_authority_changes_nothing(base):
    """subject, issuer and depth_cap are read from the AUTHORITY block only (the walk's
    queries trust nothing else): a keyless block restating them for T is inert."""
    world, a, ns, tok = base
    t = world.peer("T")
    restated = world.restrict(tok, "subject({t}); issuer({t}); depth_cap(4);", {"t": t.id})
    assert _code(world.decide(restated, t, ns, Key("task/42/x"))) == HOLDER
    code, record = world.decide(restated, a, ns, Key("task/42/x"))
    assert code == "ALLOW" and record["issuer"] == a.id, (code, record)


def test_a_block_signed_by_a_key_the_holder_does_not_hold_is_refused(base):
    """The external key must be a key of the CURRENT holder: A's own key cannot sign E's
    hop, and neither can a key no peer registered. (A's key then signs two blocks, so the
    delegate pairs `trusting` it are two and the walk cannot tell whose is whose: refused as
    a malformed chain, before the holder is compared.)"""
    world, a, ns, tok = base
    e, f = world.peer("E"), world.peer("F")
    ch = world.delegate(tok, a, e)
    assert _code(world.decide(world.delegate(ch, e, f, signer=a), f, ns,
                              Key("task/42/x"))) == INVALID
    g = world.peer("G")
    assert _code(world.decide(world.delegate(ch, e, f, signer=g), f, ns,
                              Key("task/42/x"))) == HOLDER
    unregistered = world.peer("U", key=False)
    unregistered.kp = world.b.KeyPair()
    assert _code(world.decide(world.delegate(tok, a, e, signer=unregistered), e, ns,
                              Key("task/42/x"))) == HOLDER
    assert world.decide(world.delegate(ch, e, f), f, ns, Key("task/42/x"))[0] == "ALLOW"


def test_a_hop_must_carry_exactly_one_delegate_pair_under_its_key(base):
    world, a, ns, tok = base
    e, f = world.peer("E"), world.peer("F")
    two = world.delegate(tok, a, e, checks="delegate({f});", params={"f": f.id})
    assert _code(world.decide(two, e, ns, Key("task/42/x"))) == INVALID
    assert _code(world.decide(two, f, ns, Key("task/42/x"))) == INVALID
    none = tok.append_third_party(
        a.kp.public_key, tok.third_party_request().create_block(
            a.kp.private_key, world.b.BlockBuilder('check if action("read");')))
    assert _code(world.decide(none, a, ns, Key("task/42/x"))) == INVALID


def test_the_host_enforces_a_hops_expiry_even_when_its_block_does_not(base):
    """A delegator builds its own block and may leave out `check if time`. Datalog would then
    never expire the hop (measured: such a block authorizes after its expiry), so the host
    refuses a hop whose delegation_expires has passed."""
    world, a, ns, tok = base
    e = world.peer("E")
    past = world.delegate(tok, a, e, ttl=-30, time_check=False)
    assert _code(world.decide(past, e, ns, Key("task/42/x"))) == DENIED
    fresh = world.delegate(tok, a, e, ttl=30, time_check=False)
    assert world.decide(fresh, e, ns, Key("task/42/x"))[0] == "ALLOW"          # control


def test_a_peer_may_not_appear_twice_in_the_chain(base):
    world, a, ns, tok = base
    e = world.peer("E")
    back = world.delegate(world.delegate(tok, a, e), e, a)
    assert _code(world.decide(back, a, ns, Key("task/42/x"))) == INVALID


def test_the_small_order_points_are_refused_as_signing_keys():
    """Q7: biscuit-auth 6.0.0 verifies with verify_strict, so a token signed by a small-order
    key cannot parse; the host keeps its own refusal anyway, and this is where it lives."""
    cap = _cap()
    from apps.crypto import primitives
    for point in primitives._SMALL_ORDER:
        with pytest.raises(Exception) as err:
            cap._delegation_key_bytes(point)
        assert getattr(err.value, "code", None) == INVALID
    real = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    assert cap._delegation_key_bytes(real) == real                              # control


# ============================================================================ T5: over-attenuation

@pytest.fixture()
def chain45(base):
    """4.5's parent: A delegated to E, read only, under task/42/, in A's signed block."""
    world, a, ns, tok = base
    e, f = world.peer("E"), world.peer("F")
    parent = world.delegate(tok, a, e, checks=(
        'check if action("read");\n'
        'check if resource($ns, $r), $r.starts_with("task/42/");\n'))
    return world, a, e, f, ns, tok, parent


def test_t5_the_parent_is_the_control(chain45):
    world, a, e, f, ns, tok, parent = chain45
    assert world.decide(parent, e, ns, Key("task/42/x"))[0] == "ALLOW"
    assert _code(world.decide(parent, e, ns, Key("task/42/x"), action="write")) == DENIED


def test_t5_asserted_write_and_admin_rights_gain_nothing(chain45):
    world, a, e, f, ns, tok, parent = chain45
    wide = world.delegate(parent, e, f, checks=(
        'right({ns}, "", "write");\nright({ns}, "", "admin");\n'), params={"ns": ns})
    assert world.decide(wide, f, ns, Key("task/42/x"))[0] == "ALLOW"           # control
    assert _code(world.decide(wide, f, ns, Key("task/42/x"), action="write")) == DENIED
    assert _code(world.decide(wide, f, ns, Key("task/42/x"), action="admin")) == NOT_ACCEPTED


def test_t5_a_wider_prefix_cannot_widen(chain45):
    world, a, e, f, ns, tok, parent = chain45
    wider = world.delegate(parent, e, f,
                           checks='check if resource($ns, $r), $r.starts_with("task/");\n')
    assert _code(world.decide(wider, f, ns, Key("task/7/x"))) == DENIED
    assert world.decide(wider, f, ns, Key("task/42/x"))[0] == "ALLOW"          # control


def test_t5_a_one_hour_hop_is_refused(chain45):
    world, a, e, f, ns, tok, parent = chain45
    cap = _cap()
    assert cap.MAX_HOP_SECONDS == 300
    long_hop = world.delegate(parent, e, f, ttl=3600)
    assert _code(world.decide(long_hop, f, ns, Key("task/42/x"))) == INVALID
    assert world.decide(world.delegate(parent, e, f, ttl=290), f, ns,
                        Key("task/42/x"))[0] == "ALLOW"                          # control


def test_t5_a_fifth_hop_past_the_cap_of_four_is_refused(base):
    """Row 13: five hops with depth_cap 4; four hops pass."""
    world, a, ns, tok = base
    cap = _cap()
    assert cap.MAX_HOPS == 4
    peers = [world.peer(f"H{i}") for i in range(5)]
    chain, holder = tok, a
    for p in peers[:4]:
        chain, holder = world.delegate(chain, holder, p), p
    assert world.decide(chain, holder, ns, Key("task/42/x")) == (
        "ALLOW", {"via": "capability", "cap_id": tok.revocation_ids[0], "issuer": a.id,
                  "holder_chain": [a.id] + [p.id for p in peers[:4]], "hops": 4})
    fifth = world.delegate(chain, holder, peers[4])
    assert _code(world.decide(fifth, peers[4], ns, Key("task/42/x"))) == INVALID


def test_the_tokens_own_depth_cap_binds_below_four(world):
    a = world.peer("A")
    ns = world.claim(a)
    tok = world.mint(issuer=a, subject=a, ns=ns, depth_cap=1, rights=(("", "read"),))
    e, f = world.peer("E"), world.peer("F")
    one = world.delegate(tok, a, e)
    assert world.decide(one, e, ns, Key("k"))[0] == "ALLOW"
    assert _code(world.decide(world.delegate(one, e, f), f, ns, Key("k"))) == INVALID
    zero = world.mint(issuer=a, subject=a, ns=ns, depth_cap=0, rights=(("", "read"),))
    assert _code(world.decide(world.delegate(zero, a, e), e, ns, Key("k"))) == INVALID


def test_t5_a_thief_signed_block_is_refused(chain45):
    world, a, e, f, ns, tok, parent = chain45
    t = world.peer("T")
    assert _code(world.decide(world.delegate(parent, e, t, signer=t), t, ns,
                              Key("task/42/x"))) == HOLDER


def test_more_than_ten_blocks_is_refused_and_ten_is_not(base):
    world, a, ns, tok = base
    cap = _cap()
    assert cap.MAX_BLOCKS == 10
    ten = tok
    for i in range(9):
        ten = world.restrict(ten, 'check if action("read") or action("write");')
    assert ten.block_count() == 10
    assert world.decide(ten, a, ns, Key("task/42/x"))[0] == "ALLOW"
    eleven = world.restrict(ten, 'check if action("read") or action("write");')
    assert _code(world.decide(eleven, a, ns, Key("task/42/x"))) == INVALID


# ============================================================================ T10: injection

INJECTION = 'x"); right("{ns}", "", "write"); allow if true; resource("{ns}", "'


def test_t10_terms_round_trip_as_single_terms(world):
    """A namespace and a key holding `"`, `)`, `;` and Datalog keywords are ONE term each: the
    host facts are parameters, never spliced."""
    a = world.peer("A")
    ns = world.claim(a, f'captest/"); allow if true; check if true {a.id[:8]}')
    key = INJECTION.format(ns=ns)
    tok = world.mint(issuer=a, subject=a, ns=ns, rights=((key, "read"),))
    assert world.decide(tok, a, ns, Key(key))[0] == "ALLOW"                    # control
    assert world.decide(tok, a, ns, Key(key + "more"))[0] == "ALLOW"
    assert _code(world.decide(tok, a, ns, Key(key), action="write")) == DENIED
    assert _code(world.decide(tok, a, ns, Key(key[:-1]))) == DENIED
    assert _code(world.decide(tok, a, ns, Key("x"))) == DENIED
    assert _code(world.decide(tok, a, ns + '"', Key(key))) == DENIED


def test_t10_the_caller_and_operation_are_parameters_too(base):
    world, a, ns, tok = base
    evil_op = 'memory.read"); allow if true; operation("memory.read'
    assert _code(world.decide(tok, a, ns, Key("task/42/x"), operation=evil_op)) == NOT_ACCEPTED
    assert _code(world.decide(tok, 'x"); allow if true; caller("x', ns,
                              Key("task/42/x"))) == HOLDER


def test_the_decision_refuses_a_resource_that_is_not_exactly_a_string(base):
    """Carried from slice 2's review: a str subclass could redefine startswith and equality."""
    world, a, ns, tok = base

    class Sneaky(str):
        def startswith(self, *a):
            return True

    for bad in (Key(Sneaky("task/42/x")), Key(42), Prefix(None), Prefix(Sneaky("task/42/"))):
        assert _code(world.decide(tok, a, ns, bad)) == DENIED, bad
    assert _code(world.decide(tok, a, Sneaky(ns), Key("task/42/x"))) == DENIED


def test_a_range_prefix_holding_a_backslash_is_refused(base):
    """Carried from slice 2's review (F5): memory.query and memory.list escape % and _ but not
    \\, LIKE's escape character, so `task/42/\\x` would match rows outside the prefix the token
    allowed. Until the handlers match literally, the decision refuses such a range."""
    world, a, ns, tok = base
    assert _code(world.decide(tok, a, ns, Prefix("task/42/a\\b"),
                              operation="memory.query")) == DENIED
    assert world.decide(tok, a, ns, Key("task/42/a\\b"))[0] == "ALLOW", (
        "a POINT key is compared with =, not LIKE: the backslash is only a range's problem")
    assert world.decide(tok, a, ns, Prefix("task/42/ab"),
                        operation="memory.query")[0] == "ALLOW"                   # control


# ============================================================================ T13: the fact bomb

BOMB = "wide($a, $b) <- n($a), n($b);"


def _bomb_block(world, tok, n: int):
    code = "".join(f"n({{v{i}}});\n" for i in range(n)) + BOMB
    return world.restrict(tok, code, {f"v{i}": i for i in range(n)})


def test_t13_a_fact_bomb_is_refused_by_the_deterministic_bound_and_counted(base):
    """A holder appends a keyless block whose rule derives n*n facts. 40 -> 1600 facts, past
    1000: refused and counted. 30 -> 900: allowed (the control, the same shape below the
    bound), so the refusal is the FACT bound, not the clock."""
    world, a, ns, tok = base
    cap = _cap()
    before = cap.run_limit_denials()
    assert world.decide(_bomb_block(world, tok, 30), a, ns, Key("task/42/x"))[0] == "ALLOW"
    assert cap.run_limit_denials() == before
    assert _code(world.decide(_bomb_block(world, tok, 40), a, ns, Key("task/42/x"))) == DENIED
    assert cap.run_limit_denials() == before + 1


def test_t13_a_bomb_in_a_root_signed_authority_is_refused_too(world):
    """The bomb signed by the real root."""
    a = world.peer("A")
    ns = world.claim(a)
    code = "".join(f"n({{v{i}}});" for i in range(40)) + BOMB
    bomb = world.mint(issuer=a, subject=a, ns=ns, extra=code,
                      extra_params={f"v{i}": i for i in range(40)})
    assert _code(world.decide(bomb, a, ns, Key("k"))) == DENIED


def test_every_authorizer_the_decision_builds_has_the_explicit_limits(base, monkeypatch):
    """1000 facts, 100 iterations, 100 ms, set BEFORE build on every authorizer, the holder
    walk's included (1.3 row 20: the 1 ms default trips a legitimate decision under load).
    Every AuthorizerBuilder the module makes is recorded through the library's own name."""
    world, a, ns, tok = base
    cap = _cap()
    import biscuit_auth
    real = biscuit_auth.AuthorizerBuilder
    made = []

    class Recorder:
        def __init__(self, *args, **kwargs):
            self._real = real(*args, **kwargs)
            self.limits_at_build = None
            made.append(self)

        def build(self, token):
            lim = self._real.limits()
            self.limits_at_build = (lim.max_facts, lim.max_iterations, lim.max_time)
            return self._real.build(token)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(biscuit_auth, "AuthorizerBuilder", Recorder)
    e = world.peer("E")
    ch = world.delegate(tok, a, e)
    assert world.decide(ch, e, ns, Key("task/42/x"))[0] == "ALLOW"
    assert len(made) >= 2, "control: the walk and the decision each build an authorizer"
    want = (1000, 100, _dt.timedelta(milliseconds=100))
    assert [m.limits_at_build for m in made] == [want] * len(made), [
        m.limits_at_build for m in made]
    assert (cap.LIMIT_FACTS, cap.LIMIT_ITERATIONS, cap.LIMIT_TIME) == want


# ======================================================================== T15: lifted and legacy

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _read_varint(buf: bytes, i: int):
    shift = n = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def pb_decode(buf: bytes) -> list:
    """[(field, int | bytes)]: the outer container of biscuit's schema.proto (Biscuit,
    SignedBlock, ExternalSignature, PublicKey, Proof). Block payloads stay opaque."""
    out, i = [], 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(buf, i)
        elif wire == 2:
            length, i = _read_varint(buf, i)
            value, i = bytes(buf[i:i + length]), i + length
        else:
            raise ValueError(f"wire type {wire}")
        out.append((field, value))
    return out


def pb_encode(fields) -> bytes:
    out = bytearray()
    for field, value in fields:
        if isinstance(value, int):
            out += _varint(field << 3) + _varint(value)
        else:
            out += _varint(field << 3 | 2) + _varint(len(value)) + value
    return bytes(out)


def _get(fields, n):
    return [v for f, v in fields if f == n]


def _signed_block(raw: bytes) -> dict:
    f = pb_decode(raw)
    return {"data": _get(f, 1)[0], "next": _get(pb_decode(_get(f, 2)[0]), 2)[0],
            "sig": _get(f, 3)[0], "ext": _get(f, 4), "version": _get(f, 5)}


def _sign(private: bytes, message: bytes) -> bytes:
    from nacl.signing import SigningKey
    return SigningKey(private).sign(message).signature


def _public(private: bytes) -> bytes:
    from nacl.signing import SigningKey
    return bytes(SigningKey(private).verify_key)


def _verify(public: bytes, message: bytes, signature: bytes) -> bool:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    try:
        VerifyKey(public).verify(message, signature)
        return True
    except BadSignatureError:
        return False


def _v1_external(data: bytes, prev_sig: bytes) -> bytes:
    """The domain-separated v1 external-signature payload."""
    return (b"\0EXTERNAL\0\0VERSION\0" + struct.pack("<I", 1) + b"\0PAYLOAD\0" + data
            + b"\0PREVSIG\0" + prev_sig)


def _append_by_hand(tok, data: bytes, ext_sig: bytes, ext_public: bytes, *, legacy: bool):
    """Append a third-party block to `tok` BY HAND, signing with the token's own next secret:
    what anyone holding a token can do. `legacy` builds the v0 format (no version; the block
    signature over data || ext_sig || alg || next key) instead of v1. Returns the new token's
    base64 and the payloads each signature covers."""
    top = pb_decode(bytes(tok.to_bytes()))
    auth = _signed_block(_get(top, 2)[0])
    blocks = _get(top, 3)
    prev = _signed_block(blocks[-1]) if blocks else auth
    secret = _get(pb_decode(_get(top, 4)[0]), 1)[0]
    assert _public(secret) == prev["next"], "control: the proof's secret is the next key's"
    nxt_secret = os.urandom(32)
    nxt = _public(nxt_secret)
    if legacy:
        payload = data + ext_sig + struct.pack("<i", 0) + nxt
    else:
        payload = (b"\0BLOCK\0\0VERSION\0" + struct.pack("<I", 1) + b"\0PAYLOAD\0" + data
                   + b"\0ALGORITHM\0" + struct.pack("<i", 0) + b"\0NEXTKEY\0" + nxt
                   + b"\0PREVSIG\0" + prev["sig"] + b"\0EXTERNALSIG\0" + ext_sig)
    block_sig = _sign(secret, payload)
    signed = [(1, data), (2, pb_encode([(1, 0), (2, nxt)])), (3, block_sig),
              (4, pb_encode([(1, ext_sig), (2, pb_encode([(1, 0), (2, ext_public)]))]))]
    if not legacy:
        signed.append((5, 1))
    out = ([(f, v) for f, v in top if f in (1, 2)] + [(3, b) for b in blocks]
           + [(3, pb_encode(signed)), (4, pb_encode([(1, nxt_secret)]))])
    raw = pb_encode(out)
    return base64.urlsafe_b64encode(raw).decode("ascii"), {
        "prev": prev, "block_payload": payload, "block_sig": block_sig, "secret": secret}


def test_t15_a_hand_built_v1_block_is_accepted_so_the_builder_is_sound(base):
    """CONTROL for the two refusals below: the same hand construction, in the v1 format with
    an external signature made for THIS token, verifies and walks to its delegate."""
    world, a, ns, tok = base
    e = world.peer("E")
    lib = world.delegate(tok, a, e)                     # the library's own block, for its data
    data = _signed_block(_get(pb_decode(bytes(lib.to_bytes())), 3)[0])["data"]
    auth_sig = _signed_block(_get(pb_decode(bytes(tok.to_bytes())), 2)[0])["sig"]
    private = bytes(a.kp.private_key.to_bytes())
    b64, _ = _append_by_hand(tok, data, _sign(private, _v1_external(data, auth_sig)),
                             a.public_bytes, legacy=False)
    assert world.decide(b64, e, ns, Key("task/42/x"))[0] == "ALLOW"


def test_t15_a_delegation_block_lifted_onto_another_token_is_refused(base):
    """A's block delegating to E, cut from token X and appended to token Y, which has the
    same holder A. Its external signature covers X's previous signature, so it cannot verify
    on Y. CONTROL: the same bytes re-signed by A for Y are accepted."""
    world, a, ns, tok = base
    e = world.peer("E")
    x = world.mint(issuer=a, subject=a, ns=ns, depth_cap=4,
                   rights=(("task/42/", "read"),))
    xe = world.delegate(x, a, e)
    lifted = _signed_block(_get(pb_decode(bytes(xe.to_bytes())), 3)[0])
    lifted_sig = _get(pb_decode(lifted["ext"][0]), 1)[0]
    b64, _ = _append_by_hand(tok, lifted["data"], lifted_sig, a.public_bytes, legacy=False)
    assert _code(world.decide(b64, e, ns, Key("task/42/x"))) == INVALID
    auth_sig = _signed_block(_get(pb_decode(bytes(tok.to_bytes())), 2)[0])["sig"]
    private = bytes(a.kp.private_key.to_bytes())
    ok, _ = _append_by_hand(tok, lifted["data"],
                            _sign(private, _v1_external(lifted["data"], auth_sig)),
                            a.public_bytes, legacy=False)
    assert world.decide(ok, e, ns, Key("task/42/x"))[0] == "ALLOW"


def test_t15_a_legacy_v0_third_party_block_is_refused(base):
    """Inference 7, settled: `Biscuit.from_base64` uses PreviousSignatureHashing rather than
    the legacy unsafe parser. A v0 block, whose external signature covers only its data and the
    previous key and so could be
    lifted, is refused. CONTROL: every signature in it verifies by the v0 rules, so the
    refusal is of the format, not of a broken construction."""
    world, a, ns, tok = base
    e = world.peer("E")
    lib = world.delegate(tok, a, e)
    data = _signed_block(_get(pb_decode(bytes(lib.to_bytes())), 3)[0])["data"]
    auth = _signed_block(_get(pb_decode(bytes(tok.to_bytes())), 2)[0])
    private = bytes(a.kp.private_key.to_bytes())
    v0_external_payload = data + struct.pack("<i", 0) + auth["next"]
    ext_sig = _sign(private, v0_external_payload)
    b64, parts = _append_by_hand(tok, data, ext_sig, a.public_bytes, legacy=True)
    assert _verify(a.public_bytes, v0_external_payload, ext_sig)
    assert _verify(_public(parts["secret"]), parts["block_payload"], parts["block_sig"])
    assert _code(world.decide(b64, e, ns, Key("task/42/x"))) == INVALID


# ============================================================================ T19: the register

def test_t19_a_root_signed_token_never_registered_is_refused(world):
    """Critique C3: a root-key thief can sign any token, naming the namespace's REAL owner as
    issuer. Only the register (2.5 (c)) tells it from one this server minted."""
    owner = world.peer("O")
    ns = world.claim(owner)
    forged = world.mint(issuer=owner, subject=owner, ns=ns, rights=(("", "write"),),
                        register=False)
    code, message = world.decide(forged, owner, ns, Key("k"), action="write")
    assert code == DENIED and "issued" in message, (code, message)
    stranger = world.peer("S")
    forged2 = world.mint(issuer=owner, subject=stranger, ns=ns, register=False)
    assert _code(world.decide(forged2, stranger, ns, Key("k"))) == DENIED
    # Once the test inserts the register row, the same bytes are allowed.
    world.register(forged, issuer=owner.id, subject=owner.id, ns=ns,
                   rights=(("", "write"),), depth_cap=2,
                   expires_at=utcnow() + _dt.timedelta(minutes=30), kid=ROOT_KID)
    assert world.decide(forged, owner, ns, Key("k"), action="write")[0] == "ALLOW"


def test_t19_the_registers_expiry_ends_a_token_whose_own_time_check_runs_longer(world):
    """Review F2, 040's "Decided here" 4: a token minted without a short time check still
    dies with its register row. The root signs a 48-hour check (past Q3's 24); the register
    row expires in 30 minutes. At +10 minutes it is allowed (the control); at +1 hour the
    register clause refuses it, although the token's own check would still pass. Beside the
    mint (slice 5), this clause is the only place the verifier holds a token to Q3's bound."""
    owner = world.peer("O")
    ns = world.claim(owner)
    tok = world.mint(issuer=owner, subject=owner, ns=ns, ttl=48 * 3600, register=False)
    now = utcnow()
    world.register(tok, issuer=owner.id, subject=owner.id, ns=ns, rights=(("", "read"),),
                   depth_cap=2, expires_at=now + _dt.timedelta(minutes=30), kid=ROOT_KID)
    assert world.decide(tok, owner, ns, Key("k"),
                        now=now + _dt.timedelta(minutes=10))[0] == "ALLOW"
    assert world.decide(tok, owner, ns, Key("k"), now=now + _dt.timedelta(hours=1)) == (
        DENIED, _cap().CLAUSE_MESSAGES["register"])


# ============================================================================ cap_version

def test_a_token_without_cap_version_1_is_never_allowed(world):
    a = world.peer("A")
    ns = world.claim(a)
    ok = world.mint(issuer=a, subject=a, ns=ns)
    assert world.decide(ok, a, ns, Key("k"))[0] == "ALLOW"                      # control
    for version in (None, 2, 0):
        tok = world.mint(issuer=a, subject=a, ns=ns, cap_version=version)
        assert _code(world.decide(tok, a, ns, Key("k"))) == DENIED, version
    # A block cannot supply it: block facts are invisible to the allow policy.
    missing = world.mint(issuer=a, subject=a, ns=ns, cap_version=None)
    patched = world.restrict(missing, "cap_version(1);")
    assert _code(world.decide(patched, a, ns, Key("k"))) == DENIED


# ============================================================================ the cache

def test_the_cache_holds_the_crypto_result_only(base, monkeypatch):
    """A second decision on the same token does not parse it again, and still runs the
    liveness statement and the authorizer: a revocation after the first use refuses the
    second. No negative caching: an invalid token is parsed every time."""
    world, a, ns, tok = base
    import biscuit_auth
    parses = []
    real = biscuit_auth.Biscuit.from_base64
    monkeypatch.setattr(biscuit_auth.Biscuit, "from_base64",
                        lambda *x, **k: (parses.append(1), real(*x, **k))[1])
    assert world.decide(tok, a, ns, Key("task/42/x"))[0] == "ALLOW"
    assert world.decide(tok, a, ns, Key("task/42/x"))[0] == "ALLOW"
    assert len(parses) == 1, parses
    assert _code(world.decide(tok, a, ns, Key("task/7/x"))) == DENIED          # authorizer ran
    world.revoke(tok.revocation_ids[0], a)
    assert _code(world.decide(tok, a, ns, Key("task/42/x"))) == REVOKED        # liveness ran
    bad = tok.to_base64()[:-4] + "AAAA"
    for _ in range(3):
        assert _code(world.decide(bad, a, ns, Key("task/42/x"))) == INVALID
    assert len(parses) == 4, parses
    cap = _cap()
    assert cap.CACHE_SIZE == 1024 and cap.CACHE_SECONDS == 60


def test_the_cache_is_dropped_with_the_root_key_store(base):
    """A token cached under one verify set is not honoured under another: the entry belongs
    to the store it was verified with."""
    world, a, ns, tok = base
    from apps.memoryskills import capability_keys
    assert world.decide(tok, a, ns, Key("task/42/x"))[0] == "ALLOW"
    other = world.b.KeyPair()
    store = capability_keys.RootKeyStore(bytes(other.private_key.to_bytes()), ROOT_KID,
                                         {ROOT_KID: bytes(other.public_key.to_bytes())})
    capability_keys.install(store)
    assert _code(world.decide(tok, a, ns, Key("task/42/x"))) == INVALID
    capability_keys.install(world.store)
    assert world.decide(tok, a, ns, Key("task/42/x"))[0] == "ALLOW"


# ============================================================================ check_permission

def test_admin_is_never_accepted_with_a_principal_even_from_a_root_signed_admin_right(world):
    """`admin` is never in a token (2.1), and a request that needs it never takes one (4.3):
    E_CAPABILITY_NOT_ACCEPTED, even for a registered, root-signed right(ns, "", "admin")
    that the Datalog would otherwise allow."""
    a = world.peer("A")
    ns = world.claim(a)
    tok = world.mint(issuer=a, subject=a, ns=ns, rights=(("", "admin"), ("", "read")),
                     register=False)
    world.register(tok, issuer=a.id, subject=a.id, ns=ns, rights=(("", "read"),),
                   depth_cap=2, expires_at=utcnow() + _dt.timedelta(minutes=30),
                   kid=ROOT_KID)
    assert _code(world.check(tok, a, ns, "admin", NAMESPACE_WIDE)) == NOT_ACCEPTED
    assert _code(world.check(tok, a, ns, "admin", Key("k"),
                             operation="memory.read")) == NOT_ACCEPTED
    assert _code(world.decide(tok, a, ns, Key("k"), action="admin",
                              operation="memory.read")) == NOT_ACCEPTED
    assert world.check(tok, a, ns, "read", Key("k"))[0] == "ALLOW"             # control


def test_a_principal_is_refused_where_v1_takes_no_token(base):
    """NAMESPACE_WIDE (grant, prune-all, the mesh routes), an operation 4.3 does not list, an
    operation that does not match the need, a range where a point is asked and a point where
    a range is: each E_CAPABILITY_NOT_ACCEPTED, never decided and never ignored."""
    world, a, ns, tok = base
    for need, resource, op in (("read", NAMESPACE_WIDE, "memory.read"),
                               ("read", Key("task/42/x"), "mesh.read"),
                               ("read", Key("task/42/x"), "memory.write"),
                               ("write", Key("task/42/x"), "memory.read"),
                               ("read", Prefix("task/42/"), "memory.read"),
                               ("read", Key("task/42/x"), "memory.query")):
        assert _code(world.check(tok, a, ns, need, resource, operation=op)) == NOT_ACCEPTED, (
            need, resource, op)
    assert world.check(tok, a, ns, "read", Prefix("task/42/"),
                       operation="memory.list")[0] == "ALLOW"                     # control


def test_with_no_key_store_a_principal_is_unavailable_and_never_falls_through(base):
    """Tokens off (no store loaded): a principal is E_CAPABILITY_UNAVAILABLE, not a grant
    decision. A is the namespace's owner, so the database path would have allowed it."""
    world, a, ns, tok = base
    from apps.memoryskills import capability_keys
    previous = capability_keys.install(None)
    try:
        assert _code(world.check(tok, a, ns, "read", Key("task/42/x"))) == UNAVAILABLE
    finally:
        capability_keys.install(previous)
    assert world.check_without(a, ns, "read", Key("task/42/x"))[0] == "ALLOW"   # control


def test_the_principal_never_shows_its_token():
    cap = _cap()
    token = "EnoKEAoIdGVzdC10b2tlbhIDAQIDGgA" * 3
    p = cap.PresentedCapability(token, "memory.read")
    assert p.sha256 == __import__("hashlib").sha256(token.encode()).hexdigest()
    for text in (repr(p), str(p)):
        assert token[:16] not in text and token not in text, text
