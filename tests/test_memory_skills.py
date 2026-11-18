"""The six memory.* skills, proved against the real database.

RULES THIS FILE HOLDS ITSELF TO
-------------------------------
* EVERY negative assertion is paired with a POSITIVE CONTROL in the same test.
  "raises E_PERMISSION_DENIED" and "the whole connection is misconfigured" look
  identical from here, and so do "the trigger refused the UPDATE" and "the row was
  never visible to this connection in the first place". Each negative below is
  therefore preceded or followed by the same operation performed the LEGAL way, so
  a vacuous pass is detectable.
* No database mocks. Every row is written through the handlers or seeded with real
  SQL against the migrated schema.
* The contracts come from the live schema and documented memory-skills rules, never from
  the handler under test or from a guess. Where a test encodes a rule that surprised me,
  a comment states the rule.

Run:  .venv/Scripts/python.exe -m pytest tests/test_memory_skills.py -q
Needs MESHY_TEST_DSN (defaults to the dev stack database built by sqlmigrate.py).
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import psycopg.errors
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.core import rls  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import db, handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def owner_conn():
    """Owner role: BYPASSES RLS. Used only to seed peers/owners and to observe."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """The connection a request actually runs under: autocommit + SET ROLE meshychat_app.

    db.connect() runs rls.assert_rls_active() itself, so merely obtaining this
    fixture already proves the handlers are not silently running as a bypassing
    role - the failure mode that would make every isolation assertion below vacuous.
    """
    conn = db.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


def _owner(conn, tag: str, plane: str = "dev", trust_group=None) -> uuid.UUID:
    """One dashboard_users row.

    CHECK dashboard_users_plane_group_ck is `(plane='dev') = (trust_group_id IS NOT
    NULL)`: the plane and the trust group are ONE decision, so a dev owner without a
    group is rejected and a product owner with one is too. Read off pg_constraint.
    """
    owner_id = uuid.uuid4()
    if plane == "dev" and trust_group is None:
        trust_group = uuid.uuid4()
    if plane != "dev":
        trust_group = None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dashboard_users (id, email, plane, trust_group_id) VALUES (%s,%s,%s,%s)",
            (owner_id, f"mem-{tag}-{owner_id.hex[:8]}@example.test", plane, trust_group))
    conn.commit()
    return owner_id


def _peer(conn, tag: str, plane: str = "dev", trust_group=None, owner_id=None) -> dict:
    """An owner + peer + an ACTIVE signing key.

    The key is real: handle_write looks the kid up in peer_keys and puts the exact
    public-key bytes it resolved into provenance (E_PROVENANCE_KEY if it cannot), so
    a fake kid would fail the write rather than quietly writing unverifiable
    provenance.
    """
    if owner_id is None:
        owner_id = _owner(conn, tag, plane=plane, trust_group=trust_group)
    peer_id = uuid.uuid4()
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"mem-{tag}-{peer_id.hex[:8]}", f"mem {tag}"))
        cur.execute(
            "INSERT INTO peer_keys (peer_id, kid, public_key, active) VALUES (%s,%s,%s,TRUE)",
            (peer_id, kid, P.b64u(pub)))
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id, "seed": seed, "kid": kid, "pub": pub}


def _envelope(peer: dict, skill: str, args: dict, *, call_id: str | None = None) -> dict:
    """A genuinely signed verified_payload.

    The envelope contains the VERBATIM payload plus a
    verification timestamp. The signature is over the JCS bytes of the payload,
    which is also what payload_jcs_sha256 hashes, so two writes differing only in
    call_id produce different hashes and do NOT collide on the UNIQUE index.
    """
    payload = {
        "v": 1,
        "kind": "skill.call",
        "call_id": call_id or f"call-{uuid.uuid4()}",
        "caller_peer_id": str(peer["peer_id"]),
        "target_peer_id": str(peer["peer_id"]),
        "skill_name": skill,
        "skill_version": "1",
        "args": args,
        "args_attachments": [],
        "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return {
        "payload": payload,
        "signature": P.b64u(P.sign(peer["seed"], P.jcs(payload))),
        "signing_kid": peer["kid"],
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _call(conn, peer: dict, skill: str, args: dict, *, call_id: str | None = None):
    env = _envelope(peer, skill, args, call_id=call_id)
    return handlers.dispatch(skill, env, peer["peer_id"], conn)


def _claim(conn, peer: dict, namespace: str, retention_policy: str | None = None,
           owner_peer_id=None):
    """Claim a namespace as `peer`, which must therefore BE the bootstrap authority
    (MUST-1). Setting it per call gives the test one bootstrap authority source."""
    handlers.set_bootstrap_authority(peer["peer_id"])
    args = {"namespace": namespace}
    if retention_policy is not None:
        args["retention_policy"] = retention_policy
    if owner_peer_id is not None:
        args["owner_peer_id"] = str(owner_peer_id)
    return _call(conn, peer, "namespace.claim", args)


def _ns(tag: str) -> str:
    return f"memtest/{tag}-{uuid.uuid4().hex[:12]}"


def _shared_ns(tag: str) -> str:
    return f"shared/memtest-{tag}-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def wiki_ns(app_conn, owner_conn):
    """A freshly owned `shared/wiki`.

    The wiki rules are keyed on the LITERAL namespace string, so they cannot be exercised under a
    generated name. Any prior claim is cleared first, because a claim left by an
    earlier run belongs to a peer whose signing seed this process does not have.
    """
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE namespace = 'shared/wiki'")
            cur.execute("DELETE FROM shared_memory_grant WHERE namespace = 'shared/wiki'")
            cur.execute("DELETE FROM shared_memory_namespace WHERE namespace = 'shared/wiki'")
    peer = _peer(owner_conn, "wiki")
    _claim(app_conn, peer, "shared/wiki")
    return peer


# --------------------------------------------------------------------------- #
# Positive control for the entire file
# --------------------------------------------------------------------------- #

def test_the_handler_connection_is_actually_subject_to_rls(app_conn, owner_conn):
    """Without this, every isolation assertion below could pass for the wrong reason.

    Two independent things must hold and EITHER ONE ALONE reads as healthy: the role
    does not bypass RLS, and the memory tables force it.
    """
    rls.assert_rls_active(app_conn, "shared_memory")
    assert rls.current_role(app_conn) == "meshychat_app"

    # The paired negative: the seeding connection DOES bypass, and is detected.
    with pytest.raises(RuntimeError, match="BYPASSES"):
        rls.assert_rls_active(owner_conn, "shared_memory")

    # And the server_internal gate is doing work: shared_memory is invisible without it.
    with app_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory")
        assert cur.fetchone()[0] == 0, (
            "shared_memory was readable with no server_internal context; the policy "
            "is not in force and every handler test below is measuring nothing")
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shared_memory_namespace")
            assert cur.fetchone()[0] >= 0  # the context can at least reach the table


# --------------------------------------------------------------------------- #
# write / read
# --------------------------------------------------------------------------- #

def test_write_then_read_returns_the_value(app_conn, owner_conn):
    peer = _peer(owner_conn, "wr")
    ns, key = _ns("wr"), "note/alpha"
    _claim(app_conn, peer, ns)

    # Negative FIRST, so the positive below cannot be explained by a pre-existing row.
    with pytest.raises(HandlerError) as missing:
        _call(app_conn, peer, "memory.read", {"namespace": ns, "key": key})
    assert missing.value.code == "E_UNKNOWN_KEY"

    value = {"title": "alpha", "body_md": "# alpha\nhello", "n": 1}
    w = _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value})
    assert w["version"] == 1 and w["is_head"] is True and w["replay"] is False

    r = _call(app_conn, peer, "memory.read", {"namespace": ns, "key": key})
    assert r["memo"]["value"] == value, "the value read back is not the value written"
    assert r["memo"]["version"] == 1
    assert r["memo"]["id"] == w["id"]
    # author_label is INFERRED from the verified caller when no --author is given:
    # attribution, never invention.
    assert r["memo"]["author_label"] == f"peer-{str(peer['peer_id'])[:8]}"


def test_second_write_creates_version_two_and_the_head_moves(app_conn, owner_conn):
    peer = _peer(owner_conn, "v2")
    ns, key = _ns("v2"), "note/chain"
    _claim(app_conn, peer, ns)

    w1 = _call(app_conn, peer, "memory.write",
               {"namespace": ns, "key": key, "value": {"body": "first"}})
    w2 = _call(app_conn, peer, "memory.write",
               {"namespace": ns, "key": key, "value": {"body": "second"}})

    assert w1["version"] == 1 and w2["version"] == 2
    assert w2["id"] != w1["id"], "a second write reused the same row - that is an in-place update"

    head = _call(app_conn, peer, "memory.read", {"namespace": ns, "key": key})["memo"]
    assert head["version"] == 2 and head["value"] == {"body": "second"}
    assert head["parent_id"] == w1["id"], "version 2 is not linked to version 1"

    # Positive control for "the head MOVED": v1 still exists, is readable by explicit
    # version, and carries the ORIGINAL value - so this is an append, not an overwrite.
    old = _call(app_conn, peer, "memory.read",
                {"namespace": ns, "key": key, "version": 1})["memo"]
    assert old["value"] == {"body": "first"}, "version 1's value changed - the write was in place"
    assert old["is_head"] is False, "version 1 is still flagged as head; two heads exist"
    assert head["is_head"] is True


def test_an_update_in_place_is_refused_by_the_immutability_trigger(app_conn, owner_conn):
    """The database refuses what the handler never does.

    Trigger `shared_memory_immutability` (function
    shared_memory_enforce_immutability) raises on ANY content/identity column change
    and permits exactly one mutation: is_head TRUE -> FALSE. Read from the live
    function body via pg_get_functiondef.
    """
    peer = _peer(owner_conn, "imm")
    ns, key = _ns("imm"), "note/frozen"
    _claim(app_conn, peer, ns)
    w = _call(app_conn, peer, "memory.write",
              {"namespace": ns, "key": key, "value": {"body": "original"}})
    row_id = w["id"]

    # NEGATIVE: mutate the value in place.
    with pytest.raises(psycopg.errors.RaiseException) as err:
        with rls.server_internal(app_conn):
            with app_conn.cursor() as cur:
                cur.execute("UPDATE shared_memory SET value = %s::jsonb WHERE id = %s",
                            ('{"body": "tampered"}', row_id))
    assert "immutable" in str(err.value)

    # POSITIVE CONTROL 1 - the row is untouched, so the refusal happened before any write.
    assert _call(app_conn, peer, "memory.read",
                 {"namespace": ns, "key": key})["memo"]["value"] == {"body": "original"}

    # POSITIVE CONTROL 2 - this very connection CAN update this very row the legal
    # way. Without it, the refusal above is equally explained by "meshychat_app cannot
    # UPDATE shared_memory at all", which would make the negative vacuous.
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (row_id,))
            assert cur.rowcount == 1, (
                "the legal head-retirement UPDATE affected no row: the connection cannot "
                "update this table, so the refusal above proves nothing about the trigger")

    # NEGATIVE 2 - a retired head may not be resurrected.
    with pytest.raises(psycopg.errors.RaiseException) as res:
        with rls.server_internal(app_conn):
            with app_conn.cursor() as cur:
                cur.execute("UPDATE shared_memory SET is_head = TRUE WHERE id = %s", (row_id,))
    assert "resurrect" in str(res.value)


def test_replaying_the_same_signed_envelope_is_idempotent(app_conn, owner_conn):
    """Dedup keys on payload_jcs_sha256 (UNIQUE) and the GENERATED provenance_call_id
    (UNIQUE), both derived from the SIGNED envelope - so a replay cannot be forged by
    a client scalar."""
    peer = _peer(owner_conn, "replay")
    ns, key = _ns("replay"), "note/once"
    _claim(app_conn, peer, ns)

    env = _envelope(peer, "memory.write",
                    {"namespace": ns, "key": key, "value": {"body": "once"}})
    first = handlers.handle_write(env, peer["peer_id"], app_conn)
    second = handlers.handle_write(env, peer["peer_id"], app_conn)

    assert first["replay"] is False and second["replay"] is True
    assert second["version"] == 1 and second["id"] == first["id"]

    # Positive control: a DIFFERENT envelope for the same key does append, so the
    # no-op above is replay detection and not a write path that is simply broken.
    third = _call(app_conn, peer, "memory.write",
                  {"namespace": ns, "key": key, "value": {"body": "twice"}})
    assert third["replay"] is False and third["version"] == 2


def test_expected_head_version_guards_a_concurrent_write(app_conn, owner_conn):
    peer = _peer(owner_conn, "occ")
    ns, key = _ns("occ"), "note/occ"
    _claim(app_conn, peer, ns)
    _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": {"n": 1}})

    with pytest.raises(HandlerError) as conflict:
        _call(app_conn, peer, "memory.write",
              {"namespace": ns, "key": key, "value": {"n": 2}, "expected_head_version": 7})
    assert conflict.value.code == "E_WRITE_CONFLICT"

    # Control: the CORRECT anchor writes. Also proves the head did not move on the
    # refused attempt.
    ok = _call(app_conn, peer, "memory.write",
               {"namespace": ns, "key": key, "value": {"n": 2}, "expected_head_version": 1})
    assert ok["version"] == 2


# --------------------------------------------------------------------------- #
# query / list
# --------------------------------------------------------------------------- #

def test_query_filters_by_namespace(app_conn, owner_conn):
    peer = _peer(owner_conn, "qns")
    ns_a, ns_b = _ns("qa"), _ns("qb")
    _claim(app_conn, peer, ns_a)
    _claim(app_conn, peer, ns_b)
    _call(app_conn, peer, "memory.write", {"namespace": ns_a, "key": "k/a", "value": {"x": "a"}})
    _call(app_conn, peer, "memory.write", {"namespace": ns_b, "key": "k/b", "value": {"x": "b"}})

    in_a = _call(app_conn, peer, "memory.query", {"namespace": ns_a})
    in_b = _call(app_conn, peer, "memory.query", {"namespace": ns_b})

    keys_a = {m["key"] for m in in_a["results"]}
    keys_b = {m["key"] for m in in_b["results"]}
    # Positive control first: each namespace really does contain its own row, so an
    # empty cross-namespace result is scoping and not an empty store.
    assert keys_a == {"k/a"}, f"namespace A did not return its own row: {keys_a}"
    assert keys_b == {"k/b"}, f"namespace B did not return its own row: {keys_b}"
    assert "k/b" not in keys_a and "k/a" not in keys_b, "query leaked across namespaces"


def test_query_filters_by_tag(app_conn, owner_conn):
    peer = _peer(owner_conn, "qtag")
    ns = _ns("qtag")
    _claim(app_conn, peer, ns)
    _call(app_conn, peer, "memory.write",
          {"namespace": ns, "key": "k/tagged", "value": {"x": 1}, "tags": ["alpha", "shared"]})
    _call(app_conn, peer, "memory.write",
          {"namespace": ns, "key": "k/other", "value": {"x": 2}, "tags": ["beta", "shared"]})

    # Positive control: unfiltered, the namespace holds BOTH rows.
    everything = _call(app_conn, peer, "memory.query", {"namespace": ns})
    assert {m["key"] for m in everything["results"]} == {"k/tagged", "k/other"}

    only_alpha = _call(app_conn, peer, "memory.query", {"namespace": ns, "tags": ["alpha"]})
    assert {m["key"] for m in only_alpha["results"]} == {"k/tagged"}

    # `tags` is contains-ALL (@>), not contains-any: both tags must be present.
    both = _call(app_conn, peer, "memory.query",
                 {"namespace": ns, "tags": ["alpha", "shared"]})
    assert {m["key"] for m in both["results"]} == {"k/tagged"}
    neither = _call(app_conn, peer, "memory.query",
                    {"namespace": ns, "tags": ["alpha", "beta"]})
    assert neither["results"] == [], (
        "tags matched a row carrying only ONE of the requested tags; the filter is "
        "contains-any, not contains-all")


def test_list_paginates_with_an_opaque_cursor(app_conn, owner_conn):
    peer = _peer(owner_conn, "page")
    ns = _ns("page")
    _claim(app_conn, peer, ns)
    for i in range(5):
        _call(app_conn, peer, "memory.write",
              {"namespace": ns, "key": f"k/{i:02d}", "value": {"i": i}})

    # Positive control: an unpaged list returns every key and NO cursor.
    whole = _call(app_conn, peer, "memory.list", {"namespace": ns})
    assert [i["key"] for i in whole["items"]] == [f"k/{i:02d}" for i in range(5)]
    assert whole["next_cursor"] is None, "a complete page still advertised a next page"

    page1 = _call(app_conn, peer, "memory.list", {"namespace": ns, "limit": 2})
    assert [i["key"] for i in page1["items"]] == ["k/00", "k/01"]
    assert page1["next_cursor"] is not None
    page2 = _call(app_conn, peer, "memory.list",
                  {"namespace": ns, "limit": 2, "cursor": page1["next_cursor"]})
    assert [i["key"] for i in page2["items"]] == ["k/02", "k/03"]

    # A cursor whose field is the wrong type must be refused, not interpolated.
    from apps.memoryskills.validation import encode_cursor
    with pytest.raises(HandlerError) as bad:
        _call(app_conn, peer, "memory.list",
              {"namespace": ns, "cursor": encode_cursor({"k": 17})})
    assert bad.value.code == "E_BAD_REQUEST"


def test_list_without_a_namespace_shows_only_what_the_caller_may_see(app_conn, owner_conn):
    mine = _peer(owner_conn, "lsmine")
    stranger = _peer(owner_conn, "lsstr")
    ns_mine, ns_theirs = _ns("lsmine"), _ns("lstheirs")
    _claim(app_conn, mine, ns_mine)
    _claim(app_conn, stranger, ns_theirs)

    seen = _call(app_conn, mine, "memory.list", {})
    names = {n["namespace"] for n in seen["namespaces"]}
    assert ns_mine in names, "the caller could not see its OWN namespace - the negative is vacuous"
    assert ns_theirs not in names, "a namespace the caller has no relationship with was listed"


# --------------------------------------------------------------------------- #
# Namespace claim + the plane trigger
# --------------------------------------------------------------------------- #

def test_a_product_plane_owner_cannot_claim_a_shared_namespace(app_conn, owner_conn):
    """The rule lives in the DATABASE, not in this package.

    Trigger `ns_dev_only_trigger` runs enforce_ns_dev_only() BEFORE INSERT OR UPDATE
    on shared_memory_namespace and raises unless the owner's plane is 'dev'. The
    handler does not pre-check it and does not wrap the exception.
    """
    product = _peer(owner_conn, "prodclaim", plane="product")
    dev = _peer(owner_conn, "devclaim", plane="dev")
    ns = _shared_ns("claim")

    with pytest.raises(psycopg.errors.RaiseException) as denied:
        _claim(app_conn, product, ns)
    assert "product tenants cannot claim" in str(denied.value)

    # The claim really did not land.
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shared_memory_namespace WHERE namespace = %s", (ns,))
            assert cur.fetchone()[0] == 0

    # POSITIVE CONTROL: the SAME namespace name, claimed by a dev-plane owner,
    # succeeds. So the refusal above is the plane, not the name, not the handler,
    # not the connection.
    ok = _claim(app_conn, dev, ns)
    assert ok["namespace"] == ns and ok["owner_peer_id"] == str(dev["peer_id"])
    assert ok["retention_policy"] == "keep_all", "an unmarked namespace must default to keep_all"


def test_only_the_bootstrap_authority_may_claim(app_conn, owner_conn):
    authority = _peer(owner_conn, "auth")
    squatter = _peer(owner_conn, "squat")
    ns = _ns("squat")

    handlers.set_bootstrap_authority(authority["peer_id"])
    with pytest.raises(HandlerError) as denied:
        _call(app_conn, squatter, "namespace.claim", {"namespace": ns})
    assert denied.value.code == "E_NOT_BOOTSTRAP_AUTHORITY"

    # Control: the authority claims the same namespace.
    assert _call(app_conn, authority, "namespace.claim", {"namespace": ns})["namespace"] == ns

    # And a re-claim by the same owner is an idempotent no-op, not a second claim.
    again = _call(app_conn, authority, "namespace.claim", {"namespace": ns})
    assert again["idempotent"] is True


def test_an_unclaimed_namespace_denies_every_op(app_conn, owner_conn):
    """Default-deny is on READ too, not just write - that is what makes claim the
    anti-squat gate."""
    peer = _peer(owner_conn, "unclaimed")
    ns = _ns("unclaimed")

    for skill, args in (("memory.read", {"namespace": ns, "key": "k"}),
                        ("memory.query", {"namespace": ns}),
                        ("memory.list", {"namespace": ns}),
                        ("memory.write", {"namespace": ns, "key": "k", "value": {"x": 1}})):
        with pytest.raises(HandlerError) as denied:
            _call(app_conn, peer, skill, args)
        assert denied.value.code == "E_NAMESPACE_UNCLAIMED", f"{skill} did not default-deny"

    # POSITIVE CONTROL: after the claim the identical calls work, so the denials
    # above are the claim gate and not a broken peer, key or connection.
    _claim(app_conn, peer, ns)
    _call(app_conn, peer, "memory.write", {"namespace": ns, "key": "k", "value": {"x": 1}})
    assert _call(app_conn, peer, "memory.read", {"namespace": ns, "key": "k"})["memo"]["value"] \
        == {"x": 1}
    assert _call(app_conn, peer, "memory.query", {"namespace": ns})["count"] == 1


# --------------------------------------------------------------------------- #
# Grants
# --------------------------------------------------------------------------- #

def test_a_grant_is_what_opens_a_namespace_to_another_peer(app_conn, owner_conn):
    group = uuid.uuid4()  # one trust group, so I1 does not require an expires_at
    ownerp = _peer(owner_conn, "gowner", trust_group=group)
    guest = _peer(owner_conn, "gguest", trust_group=group)
    ns = _ns("grant")
    _claim(app_conn, ownerp, ns)
    _call(app_conn, ownerp, "memory.write", {"namespace": ns, "key": "k/1", "value": {"x": 1}})

    with pytest.raises(HandlerError) as denied:
        _call(app_conn, guest, "memory.read", {"namespace": ns, "key": "k/1"})
    assert denied.value.code == "E_PERMISSION_DENIED"

    _call(app_conn, ownerp, "memory.grant",
          {"namespace": ns, "grantee_peer_id": str(guest["peer_id"]), "permission": "read"})
    assert _call(app_conn, guest, "memory.read",
                 {"namespace": ns, "key": "k/1"})["memo"]["value"] == {"x": 1}

    # A read grant is NOT a write grant: the lattice is ordered read < write < admin.
    with pytest.raises(HandlerError) as too_low:
        _call(app_conn, guest, "memory.write", {"namespace": ns, "key": "k/2", "value": {"x": 2}})
    assert too_low.value.code == "E_PERMISSION_DENIED"

    # Control: upgrade the grant and the same write lands.
    _call(app_conn, ownerp, "memory.grant",
          {"namespace": ns, "grantee_peer_id": str(guest["peer_id"]), "permission": "write"})
    assert _call(app_conn, guest, "memory.write",
                 {"namespace": ns, "key": "k/2", "value": {"x": 2}})["version"] == 1

    # Revocation removes the capability again.
    revoked = _call(app_conn, ownerp, "memory.grant",
                    {"namespace": ns, "grantee_peer_id": str(guest["peer_id"]), "revoke": True})
    assert revoked["revoked"] == 1
    with pytest.raises(HandlerError) as gone:
        _call(app_conn, guest, "memory.read", {"namespace": ns, "key": "k/1"})
    assert gone.value.code == "E_PERMISSION_DENIED"


def test_an_expired_grant_is_history_not_capability(app_conn, owner_conn):
    """The grant predicate is `revoked = FALSE AND (expires_at IS NULL OR expires_at
    > NOW())`. Dropping the second half counts history."""
    group = uuid.uuid4()
    ownerp = _peer(owner_conn, "expown", trust_group=group)
    guest = _peer(owner_conn, "expguest", trust_group=group)
    ns = _ns("expiry")
    _claim(app_conn, ownerp, ns)
    _call(app_conn, ownerp, "memory.write", {"namespace": ns, "key": "k", "value": {"x": 1}})

    # A PAST expires_at is refused up front rather than written born-dead, so the expired row is
    # seeded directly.
    _call(app_conn, ownerp, "memory.grant",
          {"namespace": ns, "grantee_peer_id": str(guest["peer_id"]), "permission": "read"})
    assert _call(app_conn, guest, "memory.read", {"namespace": ns, "key": "k"})["memo"]["version"] == 1

    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute(
                "UPDATE shared_memory_grant SET expires_at = NOW() - interval '1 hour' "
                "WHERE namespace = %s AND grantee_peer_id = %s AND revoked = FALSE",
                (ns, guest["peer_id"]))
            assert cur.rowcount == 1, "no active grant row to expire - the control never existed"

    with pytest.raises(HandlerError) as expired:
        _call(app_conn, guest, "memory.read", {"namespace": ns, "key": "k"})
    assert expired.value.code == "E_PERMISSION_DENIED"

    # Positive control that the row is still THERE (revoked = FALSE), so the denial
    # comes from the expiry clause and not from the row vanishing.
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM shared_memory_grant "
                "WHERE namespace = %s AND grantee_peer_id = %s AND revoked = FALSE",
                (ns, guest["peer_id"]))
            assert cur.fetchone()[0] == 1


def test_a_cross_subtree_grant_must_be_bounded(app_conn, owner_conn):
    """I1: outside one tenant or dev group, a grant needs a non-NULL expires_at."""
    ownerp = _peer(owner_conn, "i1own")                      # its own trust group
    outsider = _peer(owner_conn, "i1out")                    # a different one
    ns = _shared_ns("i1")
    _claim(app_conn, ownerp, ns)

    with pytest.raises(HandlerError) as unbounded:
        _call(app_conn, ownerp, "memory.grant",
              {"namespace": ns, "grantee_peer_id": str(outsider["peer_id"]),
               "permission": "read"})
    assert unbounded.value.code == "E_UNBOUNDED_CROSS_GRANT"

    # Control 1: the SAME grant with a future bound is accepted.
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    ok = _call(app_conn, ownerp, "memory.grant",
               {"namespace": ns, "grantee_peer_id": str(outsider["peer_id"]),
                "permission": "read", "expires_at": future})
    assert ok["permission"] == "read" and ok["expires_at"] is not None

    # Control 2: a grantee INSIDE the group needs no bound at all, so the rule is
    # cross-subtree-scoped rather than a blanket expires_at requirement.
    insider = _peer(owner_conn, "i1in", trust_group=None,
                    owner_id=_owner(owner_conn, "i1in-owner", plane="dev",
                                    trust_group=_group_of(owner_conn, ownerp["owner_id"])))
    assert _call(app_conn, ownerp, "memory.grant",
                 {"namespace": ns, "grantee_peer_id": str(insider["peer_id"]),
                  "permission": "read"})["permission"] == "read"


def _group_of(conn, owner_id):
    with conn.cursor() as cur:
        cur.execute("SELECT trust_group_id FROM dashboard_users WHERE id = %s", (owner_id,))
        return cur.fetchone()[0]


# --------------------------------------------------------------------------- #
# Dual-plane tenancy (I2)
# --------------------------------------------------------------------------- #

def test_a_product_plane_owner_cannot_be_granted_on_a_shared_namespace(app_conn, owner_conn):
    ownerp = _peer(owner_conn, "i2gown")
    product = _peer(owner_conn, "i2gprod", plane="product")
    dev = _peer(owner_conn, "i2gdev")
    ns = _shared_ns("i2grant")
    _claim(app_conn, ownerp, ns)

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tenancy_denials WHERE namespace = %s", (ns,))
        before = cur.fetchone()[0]
    owner_conn.commit()

    with pytest.raises(HandlerError) as denied:
        _call(app_conn, ownerp, "memory.grant",
              {"namespace": ns, "grantee_peer_id": str(product["peer_id"]),
               "permission": "read"})
    assert denied.value.code == "E_PRODUCT_NO_SHARED_GRANT"

    # The denial is AUDITED, and the audit survives the handler's rollback.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT denial_class, op, caller_owner_id, target_owner_id "
                    "FROM tenancy_denials WHERE namespace = %s", (ns,))
        rows = cur.fetchall()
    owner_conn.commit()
    assert len(rows) == before + 1, "the tenancy denial was not recorded"
    assert rows[-1][0] == "product_no_shared_grant" and rows[-1][1] == "memory.grant"
    assert str(rows[-1][2]) == str(ownerp["owner_id"]), "caller_owner_id is not the ACTOR"
    assert str(rows[-1][3]) == str(product["owner_id"]), "target_owner_id is not the GRANTEE"

    # POSITIVE CONTROL: a DEV-plane grantee on the same namespace is granted. So the
    # refusal is the plane, not the namespace, the handler or the audit path.
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    assert _call(app_conn, ownerp, "memory.grant",
                 {"namespace": ns, "grantee_peer_id": str(dev["peer_id"]),
                  "permission": "read", "expires_at": future})["permission"] == "read"


def test_a_product_plane_caller_cannot_write_a_shared_namespace(app_conn, owner_conn):
    """The I2 write-ban, exercised through the only route this schema leaves open.

    There are two obvious ways to reach this gate and NEITHER is available here as written:

      * an owner flipped dev -> product. MEASURED IMPOSSIBLE here: trigger
        `dashboard_users_freeze_plane_group` raises 'dashboard_users.plane is
        immutable (was dev, attempted product)' with ERRCODE check_violation. My
        first draft of this test did exactly that and the database refused it.
      * a claim by a product owner. Refused by `ns_dev_only_trigger` (see
        test_a_product_plane_owner_cannot_claim_a_shared_namespace).

    What is left is a LEGACY grant row written before the I2 grant-ban existed -
    which is precisely the case a defence-in-depth check below check_permission is
    for. So the grant is seeded with direct SQL rather than through handle_grant,
    which would (correctly) refuse to create it.
    """
    from apps.memoryskills import Key, check_permission

    ownerp = _peer(owner_conn, "i2w")
    product = _peer(owner_conn, "i2wprod", plane="product")
    dev = _peer(owner_conn, "i2wdev")
    ns = _shared_ns("i2write")
    _claim(app_conn, ownerp, ns)

    def _seed_legacy_grant(grantee):
        with rls.server_internal(app_conn):
            with app_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                    "grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                    "VALUES (%s, %s, %s, 'write', %s, %s)",
                    (ns, grantee["peer_id"], grantee["owner_id"],
                     ownerp["peer_id"], ownerp["owner_id"]))

    _seed_legacy_grant(product)
    _seed_legacy_grant(dev)

    # POSITIVE CONTROL 1: the product peer PASSES the namespace layer. Without this,
    # the denial below is equally explained by "it had no grant", which would make
    # the test prove nothing about the plane gate.
    with rls.server_internal(app_conn):
        allowed = check_permission(ns, product["peer_id"], "write", app_conn,
                                   resource=Key("k/2"))
    assert allowed["via"] == "grant" and allowed["permission"] == "write"

    with pytest.raises(HandlerError) as denied:
        _call(app_conn, product, "memory.write",
              {"namespace": ns, "key": "k/2", "value": {"x": 2}})
    assert denied.value.code == "E_PRODUCT_NO_SHARED_WRITE"

    with owner_conn.cursor() as cur:
        cur.execute("SELECT denial_class, op, caller_owner_id, target_owner_id "
                    "FROM tenancy_denials WHERE namespace = %s", (ns,))
        rows = cur.fetchall()
    owner_conn.commit()
    assert len(rows) == 1 and rows[0][0] == "product_no_shared_write"
    assert rows[0][1] == "memory.write"
    # For a write-ban the caller IS the subject, so target stays NULL (mig 043 column map).
    assert str(rows[0][2]) == str(product["owner_id"]) and rows[0][3] is None

    # The refused write left nothing behind.
    with pytest.raises(HandlerError) as missing:
        _call(app_conn, product, "memory.read", {"namespace": ns, "key": "k/2"})
    assert missing.value.code == "E_UNKNOWN_KEY"

    # POSITIVE CONTROL 2: a DEV-plane peer holding the IDENTICAL seeded grant writes
    # the same namespace and key. So the refusal is the plane and nothing else.
    assert _call(app_conn, dev, "memory.write",
                 {"namespace": ns, "key": "k/2", "value": {"x": 2}})["version"] == 1


# --------------------------------------------------------------------------- #
# Key shape + mesh schema
# --------------------------------------------------------------------------- #

def test_a_date_style_mesh_key_is_refused_at_the_write_seam(app_conn, owner_conn):
    """A 14-digit date-style ts parses ABOVE MAX_EPOCH_MS and sorts lexically over
    every real 13-digit key, poisoning the poll cursor. The check is namespace-
    AGNOSTIC - it keys on the `msg/` key shape."""
    peer = _peer(owner_conn, "meshkey")
    ns = _ns("meshkey")
    _claim(app_conn, peer, ns)

    for bad in ("msg/20260620123638-nxxxxx", "msg/1234-x", "msg/notanumber-x"):
        with pytest.raises(HandlerError) as err:
            _call(app_conn, peer, "memory.write",
                  {"namespace": ns, "key": bad, "value": {"body": "x"}})
        assert err.value.code == "E_BAD_KEY_TS", f"{bad!r} was not refused"

    # Control 1: a real epoch-ms mesh key in the same namespace is accepted.
    good = f"msg/{int(datetime.now(timezone.utc).timestamp() * 1000)}-tester"
    assert _call(app_conn, peer, "memory.write",
                 {"namespace": ns, "key": good, "value": {"body": "x"}})["version"] == 1
    # Control 2: a NON-msg/ key carrying the same digits passes untouched, so the
    # gate is the key SHAPE and not a digit filter.
    assert _call(app_conn, peer, "memory.write",
                 {"namespace": ns, "key": "wiki/20260620123638-nxxxxx",
                  "value": {"body": "x"}})["version"] == 1


def test_a_mesh_message_is_validated_and_a_legacy_one_is_refused(app_conn, owner_conn):
    """schema_version is REQUIRED for a mesh write (divergence 011). A legacy value is
    refused with a message that names the fix, and the well-formed message is the positive
    control in the same test."""
    peer = _peer(owner_conn, "meshv1")
    # The rules key on the literal namespace shared/mesh-chat, so it must be claimed
    # under that exact name.
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE namespace = 'shared/mesh-chat'")
            cur.execute("DELETE FROM shared_memory_grant WHERE namespace = 'shared/mesh-chat'")
            cur.execute("DELETE FROM shared_memory_namespace WHERE namespace = 'shared/mesh-chat'")
    _claim(app_conn, peer, "shared/mesh-chat")

    # The sender is the caller's own `peer-<id8>` label and the key embeds it: divergence
    # 028 binds both to the signer, and this test is about the v1 SHAPE.
    sender = f"peer-{str(peer['peer_id'])[:8]}"

    def _key():
        ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return f"msg/{ms}-{sender}-{uuid.uuid4().hex[:6]}"

    base = {
        "schema_version": 1, "to": ["someone"], "from": sender, "type": "status",
        "thread_id": "t/1", "ts": "2026-09-21T12:00:00Z", "body": "hello",
        "author_family": "claude",
    }
    # POSITIVE CONTROL: the well-formed v1 message is accepted.
    assert _call(app_conn, peer, "memory.write",
                 {"namespace": "shared/mesh-chat", "key": _key(), "value": dict(base)}
                 )["version"] == 1

    # An unknown field fails closed.
    with pytest.raises(HandlerError) as unknown:
        _call(app_conn, peer, "memory.write",
              {"namespace": "shared/mesh-chat", "key": _key(),
               "value": dict(base, surprise="x")})
    assert unknown.value.code == "E_MESH_SCHEMA" and "unknown fields" in unknown.value.message

    # schema_version must be the INTEGER 1: True == 1 in Python, so a bool must not
    # sneak through (`type(...) is not int`, memory_handlers.py:473).
    with pytest.raises(HandlerError) as boolver:
        _call(app_conn, peer, "memory.write",
              {"namespace": "shared/mesh-chat", "key": _key(),
               "value": dict(base, schema_version=True)})
    assert boolver.value.code == "E_MESH_SCHEMA"

    # A LEGACY value with no schema_version is REFUSED, so unvalidated fields are not stored.
    with pytest.raises(HandlerError) as legacy:
        _call(app_conn, peer, "memory.write",
              {"namespace": "shared/mesh-chat", "key": _key(),
               "value": {"body": "legacy", "whatever": True}})
    assert legacy.value.code == "E_MESH_SCHEMA"
    assert "schema_version" in legacy.value.message and "MESH_SCHEMA_V1=1" in legacy.value.message
    # The rule is scoped to msg/ keys in shared/mesh-chat: the same shapeless value on a
    # non-msg key in the same namespace is not a message and is not refused.
    assert _call(app_conn, peer, "memory.write",
                 {"namespace": "shared/mesh-chat", "key": f"roster/{uuid.uuid4().hex[:6]}",
                  "value": {"body": "legacy", "whatever": True}})["version"] == 1


def test_done_when_is_a_bounded_ask_condition_at_the_mesh_write_seam(app_conn, owner_conn):
    """A completion condition belongs only to a real ask and is saved when it is valid.

    MUTANT: remove the `requires_ack` gate; the first refusal writes. MUTANT: remove the
    control-character predicate; the newline value writes. MUTANT: remove the 280 cap;
    the oversized value writes.
    """
    peer = _peer(owner_conn, "donewhen")
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE namespace = 'shared/mesh-chat'")
            cur.execute("DELETE FROM shared_memory_grant WHERE namespace = 'shared/mesh-chat'")
            cur.execute("DELETE FROM shared_memory_namespace WHERE namespace = 'shared/mesh-chat'")
    _claim(app_conn, peer, "shared/mesh-chat")
    sender = f"peer-{str(peer['peer_id'])[:8]}"

    def message(**extra):
        return {"schema_version": 1, "to": ["someone"], "from": sender, "type": "request",
                "thread_id": "done/when", "ts": "2026-10-03T12:00:00Z", "body": "please answer",
                "author_family": "claude", **extra}

    def write(value):
        key = f"msg/{int(datetime.now(timezone.utc).timestamp() * 1000)}-{sender}-{uuid.uuid4().hex[:6]}"
        return _call(app_conn, peer, "memory.write",
                     {"namespace": "shared/mesh-chat", "key": key, "value": value})

    with pytest.raises(HandlerError) as no_ask:
        write(message(done_when="reply with the receipt"))
    assert no_ask.value.code == "E_MESH_SCHEMA"
    for bad in (" leading", "line\nbreak", "x" * 281):
        with pytest.raises(HandlerError) as malformed:
            write(message(requires_ack=True, done_when=bad))
        assert malformed.value.code == "E_MESH_SCHEMA", bad
    assert write(message(requires_ack=True, done_when="reply with the receipt"))["version"] == 1


def test_closes_is_a_small_distinct_set_of_real_mesh_message_keys_at_the_write_seam(app_conn, owner_conn):
    """`closes` is explicit settlement evidence, never an arbitrary body pointer.

    MUTANT: remove the msg/ guard; `wiki/not-an-ask` writes. MUTANT: remove the timestamp
    validation; a date-style key writes. MUTANT: remove the duplicate predicate; repeated
    settlement evidence writes. MUTANT: weaken the 16 cap; the oversized list writes.
    """
    peer = _peer(owner_conn, "closes")
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE namespace = 'shared/mesh-chat'")
            cur.execute("DELETE FROM shared_memory_grant WHERE namespace = 'shared/mesh-chat'")
            cur.execute("DELETE FROM shared_memory_namespace WHERE namespace = 'shared/mesh-chat'")
    _claim(app_conn, peer, "shared/mesh-chat")
    sender = f"peer-{str(peer['peer_id'])[:8]}"

    def message(closes):
        return {"schema_version": 1, "to": ["someone"], "from": sender, "type": "note",
                "thread_id": "closes/check", "ts": "2026-10-03T12:00:00Z", "body": "settled",
                "author_family": "claude", "closes": closes}

    def write(value):
        key = f"msg/{int(datetime.now(timezone.utc).timestamp() * 1000)}-{sender}-{uuid.uuid4().hex[:6]}"
        return _call(app_conn, peer, "memory.write",
                     {"namespace": "shared/mesh-chat", "key": key, "value": value})

    valid = "msg/1760000000000-a"
    for bad in ([], ["wiki/not-an-ask"], ["msg/20260620123638-b"], ["msg/1760000000000-" + "x" * 6000], [valid, valid],
                [f"msg/1760000000{i:03d}-a" for i in range(17)]):
        with pytest.raises(HandlerError) as malformed:
            write(message(bad))
        assert malformed.value.code == "E_MESH_SCHEMA", bad
    assert write(message([valid, "msg/1760000000001-b"]))["version"] == 1


# --------------------------------------------------------------------------- #
# Discipline (MUST-2 / MUST-3 / MUST-7) and the wiki shape
# --------------------------------------------------------------------------- #

def test_a_new_key_cannot_be_born_closed(app_conn, owner_conn):
    peer = _peer(owner_conn, "must3")
    ns = _ns("must3")
    _claim(app_conn, peer, ns)

    for status in ("PROPOSED_FIX", "FIXED"):
        with pytest.raises(HandlerError) as err:
            _call(app_conn, peer, "memory.write",
                  {"namespace": ns, "key": f"k/{status}", "value": {"x": 1}, "status": status})
        assert err.value.code == "E_NEW_KEY_BAD_STATUS"

    # Control 1: a new key CAN be born OPEN, so the refusals are the status and not
    # the write path.
    assert _call(app_conn, peer, "memory.write",
                 {"namespace": ns, "key": "k/open", "value": {"x": 1},
                  "status": "OPEN"})["status"] == "OPEN"
    # Control 2: PROPOSED_FIX is legal on an EXISTING chain, so it is the "born"
    # part that is forbidden, not the value.
    assert _call(app_conn, peer, "memory.write",
                 {"namespace": ns, "key": "k/open", "value": {"x": 2},
                  "status": "PROPOSED_FIX"})["version"] == 2
    # And an off-vocabulary status is refused outright.
    with pytest.raises(HandlerError) as bad:
        _call(app_conn, peer, "memory.write",
              {"namespace": ns, "key": "k/open", "value": {"x": 3}, "status": "SHIPPED"})
    assert bad.value.code == "E_INVALID_STATUS"


def test_a_closure_needs_a_proposed_fix_authored_by_a_different_owner(app_conn, owner_conn):
    """MUST-2. Applied to WONTFIX as well as FIXED, which closes the dismiss-path
    bypass."""
    group = uuid.uuid4()
    author = _peer(owner_conn, "must2a", trust_group=group)
    reviewer = _peer(owner_conn, "must2b", trust_group=group)
    ns, key = _ns("must2"), "k/finding"
    _claim(app_conn, author, ns)
    _call(app_conn, author, "memory.grant",
          {"namespace": ns, "grantee_peer_id": str(reviewer["peer_id"]), "permission": "write"})

    _call(app_conn, author, "memory.write",
          {"namespace": ns, "key": key, "value": {"x": 1}, "status": "OPEN"})

    # No PROPOSED_FIX on the chain yet.
    with pytest.raises(HandlerError) as none_yet:
        _call(app_conn, author, "memory.write",
              {"namespace": ns, "key": key, "value": {"x": 2}, "status": "FIXED"})
    assert none_yet.value.code == "E_NO_PROPOSED_FIX"

    _call(app_conn, author, "memory.write",
          {"namespace": ns, "key": key, "value": {"x": 3}, "status": "PROPOSED_FIX"})

    # The proposer may not close its own proposal - for FIXED or for WONTFIX.
    for status in ("FIXED", "WONTFIX"):
        with pytest.raises(HandlerError) as self_close:
            _call(app_conn, author, "memory.write",
                  {"namespace": ns, "key": key, "value": {"x": 4}, "status": status})
        assert self_close.value.code == "E_DISCIPLINE_VIOLATION", (
            f"{status} let the proposer close its own proposal")

    # POSITIVE CONTROL: a DIFFERENT owner closes it. Without this the two refusals
    # above are equally explained by "closure never works".
    closed = _call(app_conn, reviewer, "memory.write",
                   {"namespace": ns, "key": key, "value": {"x": 5}, "status": "FIXED"})
    # v1 OPEN, v2 PROPOSED_FIX, v3 FIXED: the three refused writes appended nothing,
    # which is itself the assertion that a denied write does not consume a version.
    assert closed["status"] == "FIXED" and closed["version"] == 3


def test_a_shared_wiki_entry_must_carry_a_tag(app_conn, wiki_ns):
    """The handler cannot infer an entry's topic, so an untagged entry falls out of
    the graph buckets. Off-vocabulary tags are deliberately NOT blocked."""
    peer = wiki_ns
    with pytest.raises(HandlerError) as untagged:
        _call(app_conn, peer, "memory.write",
              {"namespace": "shared/wiki", "key": "wiki/untagged", "value": {"body_md": "x"}})
    assert untagged.value.code == "E_WIKI_NO_TAGS"

    # Control 1: the identical write WITH a tag lands, and status defaults to OPEN
    # (the just-written, pre-review state).
    ok = _call(app_conn, peer, "memory.write",
               {"namespace": "shared/wiki", "key": "wiki/tagged",
                "value": {"body_md": "x"}, "tags": ["process"]})
    assert ok["status"] == "OPEN"
    # Control 2: an off-vocabulary tag is accepted, so the rule is "at least one
    # tag", not a vocabulary gate.
    assert _call(app_conn, peer, "memory.write",
                 {"namespace": "shared/wiki", "key": "wiki/offvocab",
                  "value": {"body_md": "x"}, "tags": ["a-tag-no-ontology-has"]})["version"] == 1


def test_a_shared_wiki_entry_cannot_confirm_itself(app_conn, wiki_ns, owner_conn):
    """#693. The no-confirmer rule fires on the TRANSITION into CONFIRMED; the
    self-confirm rule fires on EVERY CONFIRMED write."""
    peer = wiki_ns
    base = {"namespace": "shared/wiki", "tags": ["process"]}

    _call(app_conn, peer, "memory.write",
          dict(base, key="wiki/e1", value={"body_md": "x", "author": "seat-a"}))

    with pytest.raises(HandlerError) as no_conf:
        _call(app_conn, peer, "memory.write",
              dict(base, key="wiki/e1", value={"body_md": "x", "author": "seat-a"},
                   status="CONFIRMED"))
    assert no_conf.value.code == "E_WIKI_NO_CONFIRMER"

    with pytest.raises(HandlerError) as self_conf:
        _call(app_conn, peer, "memory.write",
              dict(base, key="wiki/e1",
                   value={"body_md": "x", "author": "seat-a", "confirmed_by": "seat-a"},
                   status="CONFIRMED"))
    assert self_conf.value.code == "E_WIKI_SELF_CONFIRM"

    # A '-cc' seat is the SAME provenance, so it is stripped before comparison.
    with pytest.raises(HandlerError) as cc_conf:
        _call(app_conn, peer, "memory.write",
              dict(base, key="wiki/e1",
                   value={"body_md": "x", "author": "seat-a", "confirmed_by": "Seat-A-cc"},
                   status="CONFIRMED"))
    assert cc_conf.value.code == "E_WIKI_SELF_CONFIRM"

    # POSITIVE CONTROL: a DIFFERENT seat confirms the same entry and it lands.
    ok = _call(app_conn, peer, "memory.write",
               dict(base, key="wiki/e1",
                    value={"body_md": "x", "author": "seat-a", "confirmed_by": ["seat-b"]},
                    status="CONFIRMED"))
    assert ok["status"] == "CONFIRMED"


def test_the_authenticated_signer_cannot_launder_a_self_confirm(app_conn, wiki_ns):
    """#775: value.confirmed_by is a client-TYPED string, so a writer that types a
    different confirmer launders #693. This rule compares the SIGNER's roster label.

    The roster cache fails OPEN when empty, so the control below is what proves the
    rule fires at all rather than being permanently inapplicable.
    """
    peer = wiki_ns
    base = {"namespace": "shared/wiki", "tags": ["process"]}
    _call(app_conn, peer, "memory.write",
          dict(base, key="wiki/laundered", value={"body_md": "x", "author": "seat-signer"}))

    # Roster-less: #693 sees confirmed_by=seat-other != author, and #775 has no
    # label to compare, so the write lands. This is the fail-open baseline.
    handlers.prime_roster_labels(b"{}")
    laundered = dict(base, key="wiki/laundered", status="CONFIRMED",
                     value={"body_md": "x", "author": "seat-signer",
                            "confirmed_by": ["seat-other"]})
    assert _call(app_conn, peer, "memory.write", laundered)["status"] == "CONFIRMED"

    # Now the same peer IS on the roster, as the entry's author. The identical
    # payload must be refused - the difference is only who the signer is known to be.
    _call(app_conn, peer, "memory.write",
          dict(base, key="wiki/laundered2", value={"body_md": "x", "author": "seat-signer"}))
    try:
        handlers.prime_roster_labels(
            ('{"peers": {"%s": "seat-signer"}}' % peer["peer_id"]).encode())
        with pytest.raises(HandlerError) as caught:
            _call(app_conn, peer, "memory.write",
                  dict(base, key="wiki/laundered2", status="CONFIRMED",
                       value={"body_md": "x", "author": "seat-signer",
                              "confirmed_by": ["seat-other"]}))
        assert caught.value.code == "E_WIKI_SELF_CONFIRM"
        assert "not consulted" in caught.value.message, "this was #693 firing, not #775"

        # Control: a signer who is NOT the author confirms the same entry fine.
        handlers.prime_roster_labels(
            ('{"peers": {"%s": "seat-reviewer"}}' % peer["peer_id"]).encode())
        assert _call(app_conn, peer, "memory.write",
                     dict(base, key="wiki/laundered2", status="CONFIRMED",
                          value={"body_md": "x", "author": "seat-signer",
                                 "confirmed_by": ["seat-other"]}))["status"] == "CONFIRMED"
    finally:
        handlers.prime_roster_labels(b"{}")


# --------------------------------------------------------------------------- #
# Retention: ephemeral + prune
# --------------------------------------------------------------------------- #

def test_an_ephemeral_namespace_keeps_only_the_latest(app_conn, owner_conn):
    peer = _peer(owner_conn, "eph")
    ns, key = _ns("eph"), "k/latest"
    claim = _claim(app_conn, peer, ns, retention_policy="ephemeral")
    assert claim["retention_policy"] == "ephemeral" and claim["ephemeral"] is True

    a = _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": {"n": 1}})
    b = _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": {"n": 2}})
    assert a["version"] == 1 and b["version"] == 1, "an ephemeral write built a version chain"

    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND key = %s",
                        (ns, key))
            assert cur.fetchone()[0] == 1, "the ephemeral key holds more than one row"
    assert _call(app_conn, peer, "memory.read",
                 {"namespace": ns, "key": key})["memo"]["value"] == {"n": 2}

    # POSITIVE CONTROL: the DEFAULT policy on an otherwise identical namespace keeps
    # the chain, so the single row above is the policy and not a broken write path.
    keep_ns = _ns("keep")
    _claim(app_conn, peer, keep_ns)
    _call(app_conn, peer, "memory.write", {"namespace": keep_ns, "key": key, "value": {"n": 1}})
    _call(app_conn, peer, "memory.write", {"namespace": keep_ns, "key": key, "value": {"n": 2}})
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND key = %s",
                        (keep_ns, key))
            assert cur.fetchone()[0] == 2


def test_prune_compacts_only_high_churn_and_never_touches_a_head_or_a_status_row(
        app_conn, owner_conn):
    peer = _peer(owner_conn, "prune")
    hc_ns = _ns("hc")
    _claim(app_conn, peer, hc_ns, retention_policy="high_churn")

    # A plain chain of four versions, plus one key whose middle version carries a
    # status (which prune must preserve whatever the keep window says).
    for i in range(4):
        _call(app_conn, peer, "memory.write",
              {"namespace": hc_ns, "key": "k/churn", "value": {"i": i}})
    _call(app_conn, peer, "memory.write",
          {"namespace": hc_ns, "key": "k/status", "value": {"i": 0}})
    _call(app_conn, peer, "memory.write",
          {"namespace": hc_ns, "key": "k/status", "value": {"i": 1}, "status": "OPEN"})
    _call(app_conn, peer, "memory.write",
          {"namespace": hc_ns, "key": "k/status", "value": {"i": 2}})

    out = _call(app_conn, peer, "memory.prune", {"namespace": hc_ns})
    removed = {(r["key"], r["version"]) for r in out["removed"]}

    # It removed SOMETHING (otherwise every assertion below is vacuous)...
    assert removed, "prune removed nothing at all; the protections below prove nothing"
    assert removed == {("k/churn", 1), ("k/churn", 2), ("k/churn", 3), ("k/status", 1)}, removed

    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT key, version, is_head, status FROM shared_memory "
                        "WHERE namespace = %s ORDER BY key, version", (hc_ns,))
            left = cur.fetchall()
    # ... and what survived is exactly the heads plus the status-bearing version.
    assert left == [("k/churn", 4, True, None),
                    ("k/status", 2, False, "OPEN"),
                    ("k/status", 3, True, None)], left

    # Heads still read.
    assert _call(app_conn, peer, "memory.read",
                 {"namespace": hc_ns, "key": "k/churn"})["memo"]["version"] == 4

    # NEGATIVE: the same call against a keep_all namespace is refused. Paired with
    # the successful prune above, so "refused" is the policy and not a broken verb.
    ka_ns = _ns("ka")
    _claim(app_conn, peer, ka_ns)
    _call(app_conn, peer, "memory.write", {"namespace": ka_ns, "key": "k", "value": {"i": 0}})
    _call(app_conn, peer, "memory.write", {"namespace": ka_ns, "key": "k", "value": {"i": 1}})
    with pytest.raises(HandlerError) as refused:
        _call(app_conn, peer, "memory.prune", {"namespace": ka_ns})
    assert refused.value.code == "E_BAD_REQUEST" and "high_churn" in refused.value.message
    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s", (ka_ns,))
            assert cur.fetchone()[0] == 2, "the keep_all chain was compacted anyway"


def test_prune_is_admin_gated(app_conn, owner_conn):
    group = uuid.uuid4()
    ownerp = _peer(owner_conn, "pown", trust_group=group)
    writer = _peer(owner_conn, "pwriter", trust_group=group)
    ns = _ns("padmin")
    _claim(app_conn, ownerp, ns, retention_policy="high_churn")
    _call(app_conn, ownerp, "memory.grant",
          {"namespace": ns, "grantee_peer_id": str(writer["peer_id"]), "permission": "write"})
    _call(app_conn, ownerp, "memory.write", {"namespace": ns, "key": "k", "value": {"i": 0}})
    _call(app_conn, ownerp, "memory.write", {"namespace": ns, "key": "k", "value": {"i": 1}})

    with pytest.raises(HandlerError) as denied:
        _call(app_conn, writer, "memory.prune", {"namespace": ns})
    assert denied.value.code == "E_PERMISSION_DENIED"

    # Control: the writer CAN write (so the grant is live) and the owner CAN prune
    # (so the verb works) - the denial is specifically the admin level.
    assert _call(app_conn, writer, "memory.write",
                 {"namespace": ns, "key": "k", "value": {"i": 2}})["version"] == 3
    assert _call(app_conn, ownerp, "memory.prune", {"namespace": ns})["total_removed"] == 2


# --------------------------------------------------------------------------- #
# Argument handling
# --------------------------------------------------------------------------- #

def test_limits_clamp_and_bad_arguments_are_refused(app_conn, owner_conn):
    from apps.memoryskills.validation import MAX_LIMIT, clamp_limit

    assert clamp_limit(None) == 50 and clamp_limit(0) == 50 and clamp_limit(-3) == 50
    assert clamp_limit(10) == 10 and clamp_limit(10_000) == MAX_LIMIT
    with pytest.raises(HandlerError):
        clamp_limit("not-a-number")

    peer = _peer(owner_conn, "args")
    ns = _ns("args")
    _claim(app_conn, peer, ns)

    with pytest.raises(HandlerError) as no_key:
        _call(app_conn, peer, "memory.write", {"namespace": ns, "value": {"x": 1}})
    assert no_key.value.code == "E_BAD_REQUEST"

    with pytest.raises(HandlerError) as no_value:
        _call(app_conn, peer, "memory.write", {"namespace": ns, "key": "k"})
    assert no_value.value.code == "E_BAD_REQUEST"

    with pytest.raises(HandlerError) as bad_fields:
        _call(app_conn, peer, "memory.read", {"namespace": ns, "key": "k", "fields": "brief"})
    assert bad_fields.value.code == "E_INVALID_FIELDS"

    with pytest.raises(HandlerError) as bad_since:
        _call(app_conn, peer, "memory.query", {"namespace": ns, "since": "2026-09-21"})
    assert bad_since.value.code == "E_INVALID_SINCE"

    with pytest.raises(HandlerError) as unknown_skill:
        handlers.dispatch("memory.teleport", _envelope(peer, "memory.teleport", {}),
                          peer["peer_id"], app_conn)
    assert unknown_skill.value.code == "E_UNKNOWN_SKILL"

    # POSITIVE CONTROL for the whole block: the well-formed versions all work, so
    # the refusals above are the specific arguments and not a dead call path.
    _call(app_conn, peer, "memory.write",
          {"namespace": ns, "key": "k", "value": {"body_md": "hello world"}})
    assert _call(app_conn, peer, "memory.read",
                 {"namespace": ns, "key": "k", "fields": "abstract"}
                 )["memo"]["value"]["abstract"] == "hello world"
    assert _call(app_conn, peer, "memory.query",
                 {"namespace": ns, "since": "2000-01-01T00:00:00Z"})["count"] == 1


def test_tag_normalisation_accepts_a_comma_string_and_a_list(app_conn, owner_conn):
    """norm_tags is the WHOLE of tag normalisation: split-and-strip, or coerce each
    element to str. Deliberately no lowercasing and no dedupe - either would rewrite
    tags the corpus already carries."""
    from apps.memoryskills.validation import norm_tags

    assert norm_tags(None) == []
    assert norm_tags(" a , b ,, c ") == ["a", "b", "c"]
    assert norm_tags(["A", "b"]) == ["A", "b"], "tags were case-folded; that is not the contract"
    with pytest.raises(HandlerError):
        norm_tags({"not": "a list"})

    peer = _peer(owner_conn, "tags")
    ns = _ns("tags")
    _claim(app_conn, peer, ns)
    _call(app_conn, peer, "memory.write",
          {"namespace": ns, "key": "k", "value": {"x": 1}, "tags": "one, two"})
    assert _call(app_conn, peer, "memory.read",
                 {"namespace": ns, "key": "k"})["memo"]["tags"] == ["one", "two"]
    # Control: querying by one of the normalised tags finds it, so the stored array
    # really is the split form and not the raw string.
    assert _call(app_conn, peer, "memory.query",
                 {"namespace": ns, "tags": ["two"]})["count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
