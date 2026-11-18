"""The invoke pipeline, proved against a real database.

THE REVIEW RULE THIS FILE IS WRITTEN TO. Every negative assertion here is paired with a
positive control IN THE SAME TEST, because "rejected" and "the fixture never worked" are
indistinguishable from the outside. A test that only asserts a rejection passes just as
happily when the key is wrong, the grant was never seeded, or the skill does not exist -
it proves the pipeline is broken, not that it is safe. So each test below builds TWO
calls that differ in exactly ONE property (a tampered byte, a reused nonce, a missing
grant, a malleable scalar), asserts the control is ACCEPTED, and only then asserts the
variant is REJECTED. The delta is the finding.

Run:  .venv/Scripts/python.exe -m pytest tests/test_invoke.py -q
Needs MESHY_TEST_DSN (defaults to the dev stack database).
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.calls import capability, nonce_ledger, pipeline, repository  # noqa: E402
from apps.calls import route as routing  # noqa: E402
from apps.calls.authorize import (build_grant_request, find_active_grant,  # noqa: E402
                                  version_pin_permits)
from apps.calls.envelope import SignedEnvelope  # noqa: E402
from apps.calls.errors import (AttachmentsNotPermitted, E_FORBIDDEN, E_SIG_BAD, NotAuthorized,  # noqa: E402
                               SignatureFailed)
from apps.core import rls  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

SKILL_NAME = "invoke.test.skill"
SKILL_VERSION = "1.0.0"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def owner_conn():
    """Owner role: BYPASSES RLS. Used only to seed and to read back independently of
    the pipeline's own visibility - a fixture that read through the same context the
    code under test uses could not tell "row absent" from "row invisible"."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """meshychat_app + autocommit: the connection a request actually runs on.

    Both properties are load-bearing. Without SET ROLE the policies do not apply at
    all (the role would bypass them); without autocommit `SET LOCAL` binds to psycopg's
    implicit outer transaction instead of a real one and the context leaks past its
    block. apps/core/rls.py refuses the second case rather than documenting it.
    """
    with psycopg.connect(DSN, autocommit=True) as conn:
        rls.set_role(conn)
        yield conn


@pytest.fixture()
def ctx(app_conn):
    # Nonce enforcement ON: gate 8 requires the flag enabled to exercise the ledger.
    return pipeline.CallsContext(conn=app_conn, envelope_nonce_enforce=True,
                                 memory_peer_id=routing.DEFAULT_MEMORY_PEER_ID)


@pytest.fixture(scope="module", autouse=True)
def _no_file_outlives_this_module():
    """No file_artifacts row of an owner this module made is left for a later module.

    A row left `pending` is swept by the table-wide files janitor an upload window (an hour)
    later, inside whatever test runs then: issue #164 was such a row crossing its window
    during tests/test_retention_sweeps.py's cap test, whose 99-row control swept 100. Owners
    here are `invoke-*@example.test` (`_owner`)."""
    yield
    with psycopg.connect(DSN, autocommit=True) as conn:
        left = conn.execute(
            "SELECT f.id::text, f.status FROM file_artifacts f "
            "JOIN dashboard_users u ON u.id = f.owner_id "
            "WHERE u.email LIKE %s", ("invoke-%@example.test",)).fetchall()
    assert left == [], f"tests/test_invoke.py left file_artifacts rows: {left}"


def _owner(conn, tag: str, plane: str = "product") -> uuid.UUID:
    """dashboard_users, not `owners`. The live CHECK ties the two columns together -
    `(plane='dev') = (trust_group_id IS NOT NULL)` - so a product owner MUST have a
    NULL trust group and a dev owner MUST have one."""
    owner_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,%s,%s)",
                    (owner_id, f"invoke-{tag}-{owner_id.hex[:8]}@example.test", plane,
                     uuid.uuid4() if plane == "dev" else None))
    conn.commit()
    return owner_id


def _peer(conn, owner_id: uuid.UUID, tag: str) -> uuid.UUID:
    peer_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                    "VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"invoke-{tag}-{peer_id.hex[:8]}", tag))
    conn.commit()
    return peer_id


def _publish_key(conn, peer_id: uuid.UUID) -> tuple[bytes, str]:
    """Register an ACTIVE Ed25519 key. `public_key` is stored as base64url with NO PAD."""
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    conn.commit()
    return seed, kid


def _skill(conn, peer_id: uuid.UUID, *, mode: str = "sync") -> uuid.UUID:
    skill_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
            "input_schema, is_current) VALUES (%s,%s,%s,%s,%s,'grants_only','{}',TRUE)",
            (skill_id, peer_id, SKILL_NAME, SKILL_VERSION, mode))
    conn.commit()
    return skill_id


def _grant(conn, *, grantor_owner, grantee_owner, granter_peer,
           skill_name: str = SKILL_NAME) -> uuid.UUID:
    """A DIRECT grant. The live trigger `skill_grants_granter_ownership_guard` refuses
    a non-delegated grant whose granter_peer is not owned by the grantor, so the
    direction of these four columns is enforced by the database, not by convention."""
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
    """Two SEPARATE product-plane tenants, so `meshy_same_tenant_or_devgroup` is FALSE
    and the grant gate is actually exercised. Same-tenant peers would be allowed
    grant-free and every authorization assertion below would pass vacuously."""
    caller_owner = _owner(owner_conn, "caller")
    target_owner = _owner(owner_conn, "target")
    caller_peer = _peer(owner_conn, caller_owner, "caller")
    target_peer = _peer(owner_conn, target_owner, "target")
    seed, kid = _publish_key(owner_conn, caller_peer)
    skill_id = _skill(owner_conn, target_peer)
    return {"caller_owner": caller_owner, "target_owner": target_owner,
            "caller_peer": caller_peer, "target_peer": target_peer,
            "seed": seed, "kid": kid, "skill_id": skill_id}


# --------------------------------------------------------------------------
# signing helper - the CALLER side of the wire
# --------------------------------------------------------------------------
def sign_call(world, *, args=None, call_id=None, nonce=None, target_peer=None,
              skill_name=SKILL_NAME, issued_at=None, seed=None, kid=None):
    """Build and sign a `skill.call` envelope.

    The signed field set includes `nonce`, the TASK-33 envelope replay nonce. The signature
    covers the JCS bytes of the payload and NOTHING ELSE: there is no domain prefix - `kind`
    inside the object is the domain separator.
    """
    payload = {
        "v": 1,
        "kind": "skill.call",
        "call_id": call_id or str(uuid.uuid4()),
        "caller_peer_id": str(world["caller_peer"]),
        "target_peer_id": str(target_peer or world["target_peer"]),
        "skill_name": skill_name,
        "skill_version": SKILL_VERSION,
        "args": {"text": "hello"} if args is None else args,
        "args_attachments": [],
        "issued_at": (issued_at or _dt.datetime.now(_dt.timezone.utc)).isoformat(),
        "nonce": nonce or f"n-{uuid.uuid4()}",
    }
    signature = P.sign(seed or world["seed"], P.jcs(payload))
    return payload, SignedEnvelope(payload=payload, signature=P.b64u(signature),
                                   signing_kid=kid or world["kid"])


def make_input(world, envelope, payload, **kw):
    return pipeline.StartCallInput(
        caller_peer_id=world["caller_peer"],
        target_peer_id=uuid.UUID(payload["target_peer_id"]),
        skill_name=payload["skill_name"],
        envelope=envelope,
        call_id=payload["call_id"],
        transport=pipeline.TRANSPORT_HTTP,
        **kw)


def _row(owner_conn, call_id: str):
    """Read the row as the OWNER (RLS bypassed), so absence means the row is not there
    rather than merely invisible to whatever context the pipeline last used."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT call_id, caller_peer_id, caller_owner_id, target_peer_id, "
            "       target_owner_id, skill_id, skill_name, skill_version, mode, "
            "       transport, status, args, args_signature, args_signing_kid, "
            "       args_attachments, grant_id, deadline_at "
            "FROM skill_calls WHERE call_id = %s", (call_id,))
        row = cur.fetchone()
    owner_conn.commit()
    if row is None:
        return None
    keys = ("call_id", "caller_peer_id", "caller_owner_id", "target_peer_id",
            "target_owner_id", "skill_id", "skill_name", "skill_version", "mode",
            "transport", "status", "args", "args_signature", "args_signing_kid",
            "args_attachments", "grant_id", "deadline_at")
    return dict(zip(keys, row))


# --------------------------------------------------------------------------
# 1. the happy path
# --------------------------------------------------------------------------
def test_valid_signed_call_is_accepted_and_lands_in_skill_calls(ctx, owner_conn, world):
    grant_id = _grant(owner_conn, grantor_owner=world["target_owner"],
                      grantee_owner=world["caller_owner"],
                      granter_peer=world["target_peer"])
    payload, envelope = sign_call(world)

    result = pipeline.start_call(ctx, make_input(world, envelope, payload))

    assert result.replayed is False
    assert result.call_id == payload["call_id"]
    assert result.skill_version == SKILL_VERSION

    row = _row(owner_conn, payload["call_id"])
    assert row is not None, "an accepted call must be recorded"
    assert row["caller_peer_id"] == world["caller_peer"]
    assert row["caller_owner_id"] == world["caller_owner"]
    assert row["target_peer_id"] == world["target_peer"]
    assert row["target_owner_id"] == world["target_owner"]
    assert row["skill_id"] == world["skill_id"]
    assert row["skill_name"] == SKILL_NAME
    assert row["skill_version"] == SKILL_VERSION
    assert row["mode"] == "sync"
    assert row["transport"] == "http"
    # Step 9 (divergence 017, and 041 S5 for a remote target). This target is not the
    # memory authority and holds no socket anywhere, so the call is QUEUED for it until its
    # deadline (Q1), never left pending and never executed here. The routing itself is
    # proved in tests/test_call_routing.py and tests/test_call_delivery.py.
    assert row["status"] == "queued"
    assert result.status == "queued"
    # The grant that authorized the call is recorded ON the call: this is the audit
    # link, and a NULL here would mean "grant-free allow", a different decision.
    assert row["grant_id"] == grant_id
    # args holds the WHOLE SIGNED PAYLOAD, not payload["args"] - idempotency re-canonicalizes
    # this column and compares it
    # against the canonical envelope bytes.
    assert row["args"] == payload
    assert row["args"] != payload["args"], \
        "storing only the user args would break the idempotency hash"
    assert row["args_signature"] == envelope.signature
    assert row["args_signing_kid"] == world["kid"]
    assert row["deadline_at"] > _dt.datetime.now(_dt.timezone.utc)

    # The row is real AND it is under RLS: visible to a party to the call, invisible
    # without a context. The second assertion is the negative; the first is its
    # control, and without it "invisible" could just mean "never written".
    with rls.peer_context(ctx.conn, peer_id=world["caller_peer"]):
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM skill_calls WHERE call_id=%s",
                        (payload["call_id"],))
            as_caller = cur.fetchone()[0]
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_calls WHERE call_id=%s",
                    (payload["call_id"],))
        no_context = cur.fetchone()[0]
    assert as_caller == 1, "the caller must be able to see its own call"
    assert no_context == 0, "skill_calls must not be readable without a peer context"

    # And the 'created' event was written in the same transaction as the row, then
    # step 9's 'queued' in the same transaction as the status change (041 S5).
    with owner_conn.cursor() as cur:
        cur.execute("SELECT event_type, source FROM skill_call_events WHERE call_id=%s "
                    "ORDER BY id", (payload["call_id"],))
        events = cur.fetchall()
    owner_conn.commit()
    assert events == [("created", "meshy"), ("queued", "meshy")]


# --------------------------------------------------------------------------
# 2. a tampered argument
# --------------------------------------------------------------------------
def test_tampered_arg_is_rejected(ctx, owner_conn, world):
    """The whole point of signing the args. One byte of `args` is changed AFTER
    signing; everything else - key, kid, grant, nonce, timestamps - is identical to the
    control, so the rejection can only be the signature."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])

    # POSITIVE CONTROL: the untampered envelope is accepted.
    good_payload, good_envelope = sign_call(world, args={"amount": 1})
    ok = pipeline.start_call(ctx, make_input(world, good_envelope, good_payload))
    assert ok.replayed is False
    assert _row(owner_conn, good_payload["call_id"]) is not None

    # NEGATIVE: same signature, mutated args.
    bad_payload, bad_envelope = sign_call(world, args={"amount": 1})
    bad_payload["args"]["amount"] = 1_000_000

    with pytest.raises(SignatureFailed) as exc:
        pipeline.start_call(ctx, make_input(world, bad_envelope, bad_payload))
    assert exc.value.code == E_SIG_BAD
    assert exc.value.http_status == 401
    assert _row(owner_conn, bad_payload["call_id"]) is None, \
        "a rejected call must leave no skill_calls row"


# --------------------------------------------------------------------------
# 3. a replayed nonce
# --------------------------------------------------------------------------
def test_replayed_nonce_is_rejected(ctx, owner_conn, world):
    """Gate 8. The replayed envelope is byte-identical to one that WAS accepted - same
    signature, same live key, same grant, inside the 120s window. Nothing but the
    ledger can distinguish them, which is exactly why the ledger exists."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])

    nonce = f"replay-{uuid.uuid4()}"
    payload, envelope = sign_call(world, nonce=nonce)

    # POSITIVE CONTROL 1: the first presentation is accepted.
    first = pipeline.start_call(ctx, make_input(world, envelope, payload))
    assert first.replayed is False

    # NEGATIVE: the SAME nonce re-presented under a fresh call_id. A fresh call_id is
    # what makes this a replay test rather than an idempotency test - idempotency
    # would return the existing row, gate 8 must reject outright.
    replay_payload, replay_envelope = sign_call(world, nonce=nonce)
    with pytest.raises(NotAuthorized) as exc:
        pipeline.start_call(ctx, make_input(world, replay_envelope, replay_payload))
    assert exc.value.code == E_FORBIDDEN
    assert "replay" in str(exc.value)
    assert _row(owner_conn, replay_payload["call_id"]) is None

    # POSITIVE CONTROL 2: a DIFFERENT nonce from the SAME peer is not a replay. Without
    # this the test would also pass if the ledger rejected the peer's every second call.
    fresh_payload, fresh_envelope = sign_call(world)
    assert pipeline.start_call(ctx, make_input(world, fresh_envelope,
                                               fresh_payload)).replayed is False

    # POSITIVE CONTROL 3: the ledger keys on (peer, nonce), not nonce alone - the same
    # string from a second peer is fresh (sql/049:20-22).
    other_peer = _peer(owner_conn, world["caller_owner"], "other")
    assert nonce_ledger.record_nonce(ctx.conn, other_peer, nonce,
                                     _dt.datetime.now(_dt.timezone.utc)) is True


def test_nonce_enforcement_off_accepts_a_nonceless_envelope(app_conn, owner_conn, world):
    """The disabled half of gate 8: with the flag OFF the gate is
    NOT CALLED, so a pre-nonce signer still works. Its control is the flag ON, where
    the very same envelope is refused as `missing envelope nonce` - one delta, the
    flag, proving the flag is what decides."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])

    def nonceless():
        payload = {
            "v": 1, "kind": "skill.call", "call_id": str(uuid.uuid4()),
            "caller_peer_id": str(world["caller_peer"]),
            "target_peer_id": str(world["target_peer"]),
            "skill_name": SKILL_NAME, "skill_version": SKILL_VERSION,
            "args": {"text": "hi"}, "args_attachments": [],
            "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        sig = P.sign(world["seed"], P.jcs(payload))
        return payload, SignedEnvelope(payload=payload, signature=P.b64u(sig),
                                       signing_kid=world["kid"])

    off = pipeline.CallsContext(conn=app_conn, envelope_nonce_enforce=False,
                                memory_peer_id=routing.DEFAULT_MEMORY_PEER_ID)
    p_off, e_off = nonceless()
    assert pipeline.start_call(off, make_input(world, e_off, p_off)).replayed is False

    on = pipeline.CallsContext(conn=app_conn, envelope_nonce_enforce=True,
                               memory_peer_id=routing.DEFAULT_MEMORY_PEER_ID)
    p_on, e_on = nonceless()
    with pytest.raises(NotAuthorized) as exc:
        pipeline.start_call(on, make_input(world, e_on, p_on))
    assert "missing envelope nonce" in str(exc.value)


# --------------------------------------------------------------------------
# 4. no grant
# --------------------------------------------------------------------------
def test_cross_tenant_call_without_a_grant_is_rejected(ctx, owner_conn, world):
    """Two product-plane tenants: `meshy_same_tenant_or_devgroup` is false, so the grant
    is the ONLY thing that can authorize. The control seeds that grant and nothing
    else, so acceptance is attributable to the grant row alone."""
    # NEGATIVE first, so the control cannot have left state that explains it.
    payload, envelope = sign_call(world)
    with pytest.raises(NotAuthorized) as exc:
        pipeline.start_call(ctx, make_input(world, envelope, payload))
    assert exc.value.code == E_FORBIDDEN
    assert exc.value.http_status == 403
    assert _row(owner_conn, payload["call_id"]) is None

    # The envelope itself was fine - prove the denial was authorization, not signing.
    req = build_grant_request(caller_owner_id=world["caller_owner"],
                              target_owner_id=world["target_owner"],
                              target_peer_id=world["target_peer"],
                              skill_name=SKILL_NAME)
    assert find_active_grant(ctx.conn, req) is None

    # POSITIVE CONTROL: seed the grant, change nothing else, re-sign, accepted.
    grant_id = _grant(owner_conn, grantor_owner=world["target_owner"],
                      grantee_owner=world["caller_owner"],
                      granter_peer=world["target_peer"])
    assert find_active_grant(ctx.conn, req).id == grant_id
    ok_payload, ok_envelope = sign_call(world)
    assert pipeline.start_call(ctx, make_input(world, ok_envelope,
                                               ok_payload)).grant_id == grant_id

    # A REVOKED grant is not an active grant: liveness is part of the lookup
    # predicate, not a filter applied afterwards.
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_grants SET revoked_at = now() WHERE id = %s",
                    (grant_id,))
    owner_conn.commit()
    assert find_active_grant(ctx.conn, req) is None
    revoked_payload, revoked_envelope = sign_call(world)
    with pytest.raises(NotAuthorized):
        pipeline.start_call(ctx, make_input(world, revoked_envelope, revoked_payload))


# --------------------------------------------------------------------------
# 5. signature malleability
# --------------------------------------------------------------------------
def test_malleable_signature_is_rejected(ctx, owner_conn, world):
    """A SECOND encoding of an accepted signature must NOT also be accepted.

    Ed25519 signatures are (R, s) with s a scalar mod the group order L. `s + L` is a
    different 64-byte string carrying the SAME scalar mod L - the textbook malleable
    second encoding. A system that treats signature bytes as a unique identifier can
    then be handed two distinct "valid" signatures for one message. A strict Ed25519 verifier
    rejects any s >= L; primitives.verify_strict performs that check explicitly rather than
    relying on the linked libsodium (apps/crypto/primitives.py:34-36, 82-89).

    MEASURED, and it corrects the obvious assumption: the PyNaCl/libsodium in this
    venv ALSO rejects s+L (modern libsodium checks scalar canonicality), so this
    mutation does not slip past the backend. That does not make the explicit check
    redundant - it makes the rejection a property of THIS code rather than of whichever
    libsodium happens to be linked, which is the whole point of writing it out. The
    assertion below records what was measured instead of what was assumed.

    The control is the SAME signature with s left alone: accepted. The only difference
    between the two calls is 32 bytes of scalar encoding.
    """
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])
    L = 2 ** 252 + 27742317777372353535851937790883648493

    # POSITIVE CONTROL: the canonical signature is accepted.
    good_payload, good_envelope = sign_call(world)
    assert pipeline.start_call(ctx, make_input(world, good_envelope,
                                               good_payload)).replayed is False

    # NEGATIVE: same key, same payload, s -> s + L.
    bad_payload, bad_envelope = sign_call(world)
    raw = P.b64u_decode(bad_envelope.signature)
    s = int.from_bytes(raw[32:], "little")
    assert s < L, "a freshly produced signature is canonical by construction"
    malleable = raw[:32] + (s + L).to_bytes(32, "little")
    assert malleable != raw and len(malleable) == 64

    # It really is the SAME scalar mod L - a second ENCODING, not garbage. Without
    # this the test would also pass if the mutation had produced random bytes, which
    # would prove nothing about malleability.
    assert (s + L) % L == s

    # The rejection is attributable to the scalar check and to nothing else: the
    # instrument accepts the canonical bytes and refuses only the re-encoded ones,
    # over the identical message and key.
    public = P.public_key_of(world["seed"])
    message = P.jcs(bad_payload)
    assert P.verify_strict(public, message, raw) is True
    assert P.verify_strict(public, message, malleable) is False

    # Measured backend behaviour (see the docstring): libsodium refuses it too.
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    VerifyKey(public).verify(message, raw)          # control: the backend is working
    with pytest.raises(BadSignatureError):
        VerifyKey(public).verify(message, malleable)

    tampered = SignedEnvelope(payload=bad_payload, signature=P.b64u(malleable),
                              signing_kid=bad_envelope.signing_kid)
    with pytest.raises(SignatureFailed) as exc:
        pipeline.start_call(ctx, make_input(world, tampered, bad_payload))
    assert exc.value.code == E_SIG_BAD
    assert _row(owner_conn, bad_payload["call_id"]) is None


# --------------------------------------------------------------------------
# 6. confused deputy: a valid envelope rebound to a different route
# --------------------------------------------------------------------------
def test_signed_envelope_cannot_be_rebound_to_another_skill(ctx, owner_conn, world):
    """The signature is valid and the caller is who they say they are; what is wrong is that
    the ROUTE says one thing and the SIGNED payload says
    another. HTTP takes the target and skill from the path, so without this check a
    captured envelope can be replayed against a different skill the caller happens to
    also hold a grant for."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])
    other = "invoke.test.other"
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"],
           skill_name=other)
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO skills (peer_id, name, version, mode, share_mode, "
                    "input_schema) VALUES (%s,%s,%s,'sync','grants_only','{}')",
                    (world["target_peer"], other, SKILL_VERSION))
    owner_conn.commit()

    # POSITIVE CONTROL: routed to the skill it was signed for -> accepted.
    p_ok, e_ok = sign_call(world, skill_name=other)
    assert pipeline.start_call(ctx, make_input(world, e_ok, p_ok)).replayed is False

    # NEGATIVE: signed for `other`, routed at SKILL_NAME. Both grants exist, so the
    # denial cannot be "no grant" - it is the payload/route binding alone.
    p_bad, e_bad = sign_call(world, skill_name=other)
    bad_input = pipeline.StartCallInput(
        caller_peer_id=world["caller_peer"], target_peer_id=world["target_peer"],
        skill_name=SKILL_NAME, envelope=e_bad, call_id=p_bad["call_id"])
    with pytest.raises(NotAuthorized) as exc:
        pipeline.start_call(ctx, bad_input)
    assert "skill_name does not match" in str(exc.value)
    assert _row(owner_conn, p_bad["call_id"]) is None


# --------------------------------------------------------------------------
# 7. the gates that are deliberately absent, and the pure predicates
# --------------------------------------------------------------------------
def test_capability_gate_is_a_loud_seam_not_a_silent_allow():
    """Gate 1 is dormant: authorize calls it only when a capability_token is presented, and
    the application callers pass None. A stub that returned True would silently authorize any
    token the day one is sent; the seam must FAIL CLOSED and say so."""
    with pytest.raises(capability.BiscuitMintingNotConfigured):
        capability.verify_grant_biscuit("any-token", object())
    with pytest.raises(capability.BiscuitMintingNotConfigured):
        capability.mint_grant_biscuit()


def test_a_presented_capability_token_denies_rather_than_falling_through(ctx, owner_conn,
                                                                        world):
    """The seam must not be a BYPASS. A caller who presents a token gets an error, not
    a quiet skip to gate 2 - even though gate 2 would have allowed this exact call.
    The control is the identical call with no token: accepted."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])

    p_ok, e_ok = sign_call(world)
    assert pipeline.start_call(ctx, make_input(world, e_ok, p_ok)).replayed is False

    p_tok, e_tok = sign_call(world)
    with pytest.raises(capability.BiscuitMintingNotConfigured):
        pipeline.start_call(ctx, make_input(world, e_tok, p_tok,
                                            capability_token="ANY"))
    assert _row(owner_conn, p_tok["call_id"]) is None


def test_version_pin_predicate():
    """TASK-19. An unpinned grant permits any version; a pinned one must equal the
    RESOLVED version; a range prefix denies rather than being approximated."""
    assert version_pin_permits(None, "9.9.9") is True          # control
    assert version_pin_permits("1.0.0", "1.0.0") is True       # control
    assert version_pin_permits("=1.0.0", "1.0.0") is True      # control
    assert version_pin_permits("1.0.0", "1.0.1") is False
    assert version_pin_permits("^1.0.0", "1.0.0") is False


def test_call_id_validation_rejects_the_log_injection_shapes():
    """Validate the call ID BEFORE the value is logged or persisted."""
    assert repository.validate_call_id("call-abc") == "call-abc"       # control
    assert repository.validate_call_id("x" * 200) == "x" * 200         # control (at cap)
    for bad in ("", "x" * 201, "line\ninjected", "bell\x07"):
        with pytest.raises(ValueError):
            repository.validate_call_id(bad)


def test_idempotent_retry_replays_and_a_changed_payload_conflicts(ctx, owner_conn,
                                                                  world):
    """A retry of the SAME call_id with the SAME payload must replay the existing row
    rather than re-authorizing and re-inserting. Note the retry carries the same nonce
    too, and must still replay - idempotency is checked at step 2, BEFORE gate 8, so a
    genuine retry is not mistaken for an attack."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])
    payload, envelope = sign_call(world)

    first = pipeline.start_call(ctx, make_input(world, envelope, payload))
    assert first.replayed is False                                      # control
    again = pipeline.start_call(ctx, make_input(world, envelope, payload))
    assert again.replayed is True
    assert again.call_id == first.call_id

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_calls WHERE call_id=%s",
                    (payload["call_id"],))
        assert cur.fetchone()[0] == 1, "a replay must not insert a second row"
    owner_conn.commit()

    # A DIFFERENT payload under the same call_id is a 409, not a replay.
    other_payload, other_envelope = sign_call(world, args={"text": "different"},
                                              call_id=payload["call_id"])
    from apps.calls.errors import IdempotencyConflict
    with pytest.raises(IdempotencyConflict) as exc:
        pipeline.start_call(ctx, make_input(world, other_envelope, other_payload))
    assert exc.value.http_status == 409


def test_stale_envelope_is_rejected_by_the_replay_window(ctx, owner_conn, world):
    """The 120s window is the FIRST replay defence; gate 8 is the
    second. A signature that is still cryptographically perfect is refused once it
    ages out, and the code is E_SIG_REPLAY, not E_SIG_BAD - the distinction tells a
    client to fix its clock rather than its key."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])
    now = _dt.datetime.now(_dt.timezone.utc)

    fresh_payload, fresh_envelope = sign_call(world, issued_at=now)
    assert pipeline.start_call(ctx, make_input(world, fresh_envelope,
                                               fresh_payload)).replayed is False

    stale = now - _dt.timedelta(seconds=121)
    stale_payload, stale_envelope = sign_call(world, issued_at=stale)
    with pytest.raises(SignatureFailed) as exc:
        pipeline.start_call(ctx, make_input(world, stale_envelope, stale_payload))
    assert exc.value.code == "E_SIG_REPLAY"
    assert _row(owner_conn, stale_payload["call_id"]) is None


def test_an_envelope_signed_by_another_peers_key_is_rejected(ctx, owner_conn, world):
    """Authentication, not authorization: the payload claims `caller_peer_id` = the
    authenticated peer, but the bytes were signed by a key belonging to somebody else.
    The kid resolves under the WRONG peer, so the lookup finds nothing and the result
    is E_SIG_BAD, which does not leak E_KEY_NOT_FOUND."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])
    intruder_peer = _peer(owner_conn, world["target_owner"], "intruder")
    intruder_seed, intruder_kid = _publish_key(owner_conn, intruder_peer)

    p_ok, e_ok = sign_call(world)
    assert pipeline.start_call(ctx, make_input(world, e_ok, p_ok)).replayed is False

    p_bad, e_bad = sign_call(world, seed=intruder_seed, kid=intruder_kid)
    with pytest.raises(SignatureFailed) as exc:
        pipeline.start_call(ctx, make_input(world, e_bad, p_bad))
    assert exc.value.code == E_SIG_BAD
    assert _row(owner_conn, p_bad["call_id"]) is None



# --------------------------------------------------------------------------
# attachment admission (issue #122, divergence 087)
# --------------------------------------------------------------------------
@pytest.fixture()
def plant_file(owner_conn):
    """`plant_file(owner_id, uploader)`: a file_artifacts row of `owner_id`, written as the
    owner (no RLS on that table) and deleted at teardown. It stays `pending` as
    insert_with_id writes it: admission reads the row's owner and the calls that reference
    it (files.service.readable_row), never its status."""
    from apps.files import repository as files_repo
    planted: list[uuid.UUID] = []

    def plant(owner_id: uuid.UUID, uploader: uuid.UUID) -> uuid.UUID:
        file_id = uuid.uuid4()
        files_repo.insert_with_id(owner_conn, file_id, owner_id, uploader,
                                  f"{owner_id}/{file_id}", "text/plain", "note.txt")
        owner_conn.commit()
        planted.append(file_id)
        return file_id

    yield plant
    owner_conn.rollback()           # a test that failed mid-transaction still cleans up
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM file_artifacts WHERE id = ANY(%s)", (planted,))
    owner_conn.commit()


def _attachments_on(conn, skill_id: uuid.UUID) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE skills SET attachments_allowed = TRUE WHERE id = %s", (skill_id,))
    conn.commit()


def test_attachments_on_a_skill_that_declared_none_are_refused(ctx, owner_conn, world, plant_file):
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])
    own = plant_file(world["caller_owner"], world["caller_peer"])
    payload, envelope = sign_call(world)
    with pytest.raises(AttachmentsNotPermitted) as exc:
        pipeline.start_call(ctx, make_input(world, envelope, payload, args_attachments=[own]))
    assert "attachments_allowed=false" in exc.value.message
    assert _row(owner_conn, payload["call_id"]) is None


def test_a_call_may_attach_only_files_its_caller_could_already_read(ctx, owner_conn, world,
                                                                   plant_file):
    """#122: a reference in a call is rule 2 of the download check, so admitting any file id
    would MINT read access for the caller and the target. Positive control first: the
    caller's own file is admitted and recorded."""
    from apps.files import service as files_service
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])
    _attachments_on(owner_conn, world["skill_id"])
    own = plant_file(world["caller_owner"], world["caller_peer"])
    foreign = plant_file(world["target_owner"], world["target_peer"])

    p_ok, e_ok = sign_call(world)
    assert pipeline.start_call(ctx, make_input(world, e_ok, p_ok, args_attachments=[own])).replayed is False
    assert _row(owner_conn, p_ok["call_id"])["args_attachments"] == [own]

    # another tenant's file: refused, nothing recorded, and the caller still cannot read it
    p_bad, e_bad = sign_call(world)
    with pytest.raises(AttachmentsNotPermitted) as exc:
        pipeline.start_call(ctx, make_input(world, e_bad, p_bad, args_attachments=[own, foreign]))
    assert exc.value.message == "args_attachments[1] is not a file the caller may read"
    assert _row(owner_conn, p_bad["call_id"]) is None
    assert files_service.readable_row(ctx.conn, foreign, world["caller_peer"], world["caller_owner"]) is None

    # a file that does not exist gets the very same refusal: nothing about existence leaks
    p_gone, e_gone = sign_call(world)
    with pytest.raises(AttachmentsNotPermitted) as gone:
        pipeline.start_call(ctx, make_input(world, e_gone, p_gone, args_attachments=[uuid.uuid4()]))
    assert gone.value.message == "args_attachments[0] is not a file the caller may read"
    assert _row(owner_conn, p_gone["call_id"]) is None


def test_a_file_the_caller_received_in_an_earlier_call_may_be_passed_on(ctx, owner_conn, world,
                                                                       plant_file):
    """The transitivity #122 left to decide: a file a third tenant SENT the caller in a call is
    one the caller may already download (rule 2), so it may attach it again; the caller could
    re-upload the bytes anyway. Control: before that earlier call exists, the same file is refused."""
    _grant(owner_conn, grantor_owner=world["target_owner"],
           grantee_owner=world["caller_owner"], granter_peer=world["target_peer"])
    _attachments_on(owner_conn, world["skill_id"])
    third_owner = _owner(owner_conn, "third")
    third_peer = _peer(owner_conn, third_owner, "third")
    theirs = plant_file(third_owner, third_peer)

    p_before, e_before = sign_call(world)
    with pytest.raises(AttachmentsNotPermitted):
        pipeline.start_call(ctx, make_input(world, e_before, p_before, args_attachments=[theirs]))

    with owner_conn.cursor() as cur:   # the third tenant's own call to the caller, carrying its file
        cur.execute(
            "INSERT INTO skill_calls (id, call_id, caller_peer_id, caller_owner_id, target_peer_id, "
            " target_owner_id, skill_id, skill_name, skill_version, mode, transport, status, args, "
            " args_attachments, deadline_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'s','1.0.0','sync','http','completed','{}'::jsonb,%s,"
            "        NOW() + interval '1 hour')",
            (uuid.uuid4(), f"call-{uuid.uuid4().hex}", third_peer, third_owner, world["caller_peer"],
             world["caller_owner"], world["skill_id"], [theirs]))
    owner_conn.commit()

    p_ok, e_ok = sign_call(world)
    assert pipeline.start_call(ctx, make_input(world, e_ok, p_ok, args_attachments=[theirs])).replayed is False
    assert _row(owner_conn, p_ok["call_id"])["args_attachments"] == [theirs]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
