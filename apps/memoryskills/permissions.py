"""Identity, authorisation and the dual-plane tenancy gates.

Every function here takes an OPEN connection that is
already inside `rls.server_internal()` - see handlers.py for why that context is
the one the memory tables key on.

THE RULE THAT IS EASY TO GET BACKWARDS: there are two independent authorisation
layers and they deny for different reasons.

  * check_permission  - the NAMESPACE layer (shared_memory_namespace ownership,
    then shared_memory_grant). Denies E_NAMESPACE_UNCLAIMED / E_PERMISSION_DENIED.
  * the plane gates    - the TENANCY layer (dashboard_users.plane). Deny
    E_PRODUCT_NO_SHARED_WRITE / E_PRODUCT_NO_SHARED_GRANT / E_UNBOUNDED_CROSS_GRANT.

A grant can never fix a plane denial and a plane can never fix a missing grant.
They are checked in that order in handle_write/handle_grant, and the plane gate is
defence in depth BELOW the grant gate, not a substitute for it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging

from .errors import HandlerError
from .validation import PERM_LEVEL, b64url_decode

log = logging.getLogger("meshychat.memoryskills")


# --------------------------------------------------------------------------- #
# Live derivations - NEVER trust a client scalar (MUST-2)
# --------------------------------------------------------------------------- #

def derive_owner_id(conn, peer_id, *, require_active=True):
    """Resolve peers.owner_id LIVE.

    `peers.status` is CHECK-constrained to active/suspended/deleted (read off
    pg_constraint: peers_status_check), so this is a real gate, not decoration.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT owner_id, status FROM peers WHERE id = %s", (str(peer_id),))
        row = cur.fetchone()
    if row is None:
        raise HandlerError("E_UNKNOWN_PEER", f"peer {peer_id} not found")
    if require_active and row[1] != "active":
        raise HandlerError("E_UNKNOWN_PEER", f"peer {peer_id} is not active (status={row[1]})")
    return str(row[0])


def fetch_caller_pubkey(conn, peer_id, signing_kid):
    """Return the EXACT key bytes used to verify: active OR inside the revocation grace
    window.

    Absence here is a RACE (the key was revoked between verification and write), so
    it refuses rather than writing provenance that names no resolvable key.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public_key, revoked_at FROM peer_keys "
            "WHERE peer_id = %s AND kid = %s "
            "AND (active = TRUE OR (revoked_at IS NOT NULL AND revoked_at > NOW())) "
            "LIMIT 1",
            (str(peer_id), signing_kid),
        )
        row = cur.fetchone()
    if row is None:
        raise HandlerError(
            "E_PROVENANCE_KEY", f"no resolvable key for peer={peer_id} kid={signing_kid}")
    pk_b64url = row[0]
    fingerprint = hashlib.sha256(b64url_decode(pk_b64url)).hexdigest()
    return pk_b64url, fingerprint


# --------------------------------------------------------------------------- #
# What a check is about: the resource (design B07 3.3, divergence 040 slice 2)
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True, slots=True)
class Key:
    """ONE key: a point operation (memory.write, memory.read, a mesh row's preview).

    `k` is the key exactly as the handler received it. Its shape is the handler's to
    validate, as it was before this type existed, so a malformed key is refused where it
    always was and in the same order. The token decision (slice 4) reads it and must
    refuse a non-string before it builds a fact from it."""
    k: str


@dataclasses.dataclass(frozen=True, slots=True)
class Prefix:
    """Every key that starts with `p`: a range operation (memory.query, memory.list).
    `Prefix("")` is every key in the namespace, and says so as a RANGE, which is what a
    query or list without a prefix is; it is not NAMESPACE_WIDE."""
    p: str


class _NamespaceWide:
    """The namespace itself rather than any key in it: memory.grant, a prune of every
    key, and the mesh routes' gate on shared/mesh-chat. One instance, NAMESPACE_WIDE;
    constructing the type again returns it."""

    __slots__ = ()
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NAMESPACE_WIDE"

    def __reduce__(self):
        return "NAMESPACE_WIDE"


NAMESPACE_WIDE = _NamespaceWide()

#: The only types `resource` may have, compared EXACTLY (a subclass is refused: it could
#: redefine equality, which is what a later decision compares by).
_RESOURCE_TYPES = (Key, Prefix, _NamespaceWide)


def _require_resource(resource) -> None:
    if type(resource) not in _RESOURCE_TYPES:
        raise TypeError(
            "check_permission: resource must be Key(k), Prefix(p) or NAMESPACE_WIDE, "
            f"got {type(resource).__qualname__}")


# --------------------------------------------------------------------------- #
# Namespace authorisation - default-deny on EVERY op
# --------------------------------------------------------------------------- #

#: What `meshy_namespace_permission` answers besides a grant's permission (sql_meshychat/015,
#: divergence 040 slice 3). From the one-namespace form, NULL (None here) is "claimed, and no
#: live grant row"; the set form has no row for such a namespace, and none for an unclaimed
#: one.
UNCLAIMED = "unclaimed"
OWNER = "owner"


def answers_admitting(need: str) -> tuple[str, ...]:
    """The answers of `meshy_namespace_permission` that admit `need`: the owner's, and every
    grant permission at or above `need` in PERM_LEVEL. The one place the function's answers
    meet the level order, for `check_permission` and for the SQL that asks the function
    about many namespaces at once (`memory.list` with no namespace, `/v1/mesh/wait`'s
    visibility), so an answer it does not list admits nothing anywhere."""
    floor = PERM_LEVEL[need]
    return (OWNER,) + tuple(p for p in sorted(PERM_LEVEL, key=PERM_LEVEL.get)
                            if PERM_LEVEL[p] >= floor)


def check_permission(namespace, peer_id, need, conn, *, resource, principal=None):
    """Default-deny. An UNCLAIMED namespace denies read/query/list too, not just
    write - that is what makes namespace claim the anti-squat gate rather than a
    write-side formality. The namespace owner holds implicit admin; everyone else
    needs an active, non-revoked, non-expired grant at or above `need`.

    The grant predicate is `revoked = FALSE AND (expires_at IS NULL OR
    expires_at > NOW())`. Dropping either half counts HISTORY instead of
    CAPABILITY.

    ONE PREDICATE (docs/V1-DESIGN-RULES.md A2; divergence 040 slice 3): the decision is
    the database function `meshy_namespace_permission` (sql_meshychat/015). Its
    one-namespace form, asked here, answers 'unclaimed', 'owner', the NEWEST live grant
    row's permission, or NULL, reading the set form that `memory.list`'s enumeration and
    `/v1/mesh/wait`'s visibility read; this function turns the answer into the same codes,
    messages and dict it returned before the move.

    `resource` (design B07 3.3, divergence 040 slice 2): what the call touches,
    `Key(k)` for a point op, `Prefix(p)` for a range op, `NAMESPACE_WIDE` for one about
    the namespace itself. REQUIRED and keyword-only, and its type is checked before
    anything else, so a call site that does not say what it touches is a TypeError and
    no caller can be left behind silently. The DB grants do NOT read it: they decide by
    the namespace alone, exactly as before. The capability decision (slice 4) is its
    first reader.

    `principal` (design B07 3.3 and 4.2, divergence 040 slice 4): None, and the DB grants
    decide exactly as above. A `capability.PresentedCapability`, and the token decides
    (`capability.decide`): its refusal is FINAL and never falls through to the caller's own
    grants, which would be the confused deputy a capability exists to prevent. Only read
    and write are accepted with a principal; a request that needs admin is
    E_CAPABILITY_NOT_ACCEPTED. With tokens off (no key store loaded) a principal is
    E_CAPABILITY_UNAVAILABLE. The decision record it returns is
    {"via": "capability", "cap_id", "issuer", "holder_chain", "hops"}. Since slice 6 the
    memory handlers pass one (sites 2 to 5: memory.write, memory.read, memory.query and
    memory.list with a namespace), the token the envelope path's views read from
    `Meshychat-Capability` (apps/calls/object_capability.py).
    """
    _require_resource(resource)
    if need not in PERM_LEVEL:
        raise HandlerError("E_BAD_REQUEST", f"unknown permission level {need!r}")
    if principal is not None:
        from . import capability     # imported only when a token is presented
        return capability.decide(conn, principal=principal, caller_peer_id=peer_id,
                                 namespace=namespace, resource=resource, need=need)
    peer_id = str(peer_id)

    with conn.cursor() as cur:
        cur.execute("SELECT meshychat_namespace_permission(%s, %s)", (namespace, peer_id))
        have = cur.fetchone()[0]
    if have == UNCLAIMED:
        raise HandlerError("E_NAMESPACE_UNCLAIMED", f"namespace {namespace!r} is not claimed")
    if have == OWNER:
        return {"namespace": namespace, "peer_id": peer_id, "permission": "admin", "via": "owner"}
    if have is None:
        raise HandlerError(
            "E_PERMISSION_DENIED",
            f"peer {peer_id} has no active grant on namespace {namespace!r}",
        )
    if have not in answers_admitting(need):
        raise HandlerError(
            "E_PERMISSION_DENIED", f"peer {peer_id} has {have!r} on {namespace!r}, needs {need!r}")
    return {"namespace": namespace, "peer_id": peer_id, "permission": have, "via": "grant"}


# --------------------------------------------------------------------------- #
# Dual-mode tenancy (I1/I2)
# --------------------------------------------------------------------------- #

def namespace_owner_id(conn, namespace):
    """owner_owner_id of the claim row, or None if unclaimed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner_owner_id FROM shared_memory_namespace WHERE namespace = %s",
            (namespace,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def same_tenant_or_devgroup(conn, a, b):
    """Delegate the tenant predicate to the migration-039 keystone SQL function so
    the memory access gate, the invoke gate, the `meshy_memory` RLS policy and the dashboard
    vis_cte all share ONE definition and cannot drift.

    Verified present in this database: pg_proc has
    meshy_same_tenant_or_devgroup(caller uuid, target uuid).
    """
    if a is None or b is None:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT meshychat_same_tenant_or_devgroup(%s, %s)", (a, b))
        return bool(cur.fetchone()[0])


def is_dev_plane(conn, owner_id):
    """A DIRECT plane read, deliberately not the keystone.

    The keystone short-circuits caller==target to TRUE, so on a SELF-OWNED namespace
    it says yes regardless of plane (DG1). This read closes that; the keystone stays
    for the cross-tenant I1 test.

    MEASURED NOTE ON THIS SCHEMA: an owner cannot flip dev -> product because trigger
    `dashboard_users_freeze_plane_group` raises 'dashboard_users.plane is immutable' with
    ERRCODE check_violation. The gate is still load-bearing for a product-plane grantee
    holding a LEGACY shared/* grant row written before the I2 grant-ban. check_permission
    passes that caller, so this is the only thing standing. See
    tests/test_memory_skills.py::test_a_product_plane_caller_cannot_write_a_shared_namespace.
    """
    if owner_id is None:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT plane = 'dev' FROM dashboard_users WHERE id = %s", (owner_id,))
        row = cur.fetchone()
    return bool(row and row[0])


# --------------------------------------------------------------------------- #
# Tenancy-denial audit (migration 043 vocabulary)
# --------------------------------------------------------------------------- #

# The denial_class vocabulary is FIXED by the migration; E_UNBOUNDED_CROSS_GRANT and E_WIKI_*
# are not tenancy denials in it and
# are deliberately absent.
TENANCY_DENIAL_AUDIT = {
    "E_PRODUCT_NO_SHARED_WRITE": ("memory.write", "product_no_shared_write"),
    "E_PRODUCT_NO_SHARED_GRANT": ("memory.grant", "product_no_shared_grant"),
}

# A SILENTLY failing audit insert hides a permanently-dead security trail, which
# reads as clean. Never reset in-process; monitoring reads deltas.
AUDIT_FAILURES = 0


def audit_tenancy_denial(conn, code, caller_owner_id, caller_peer_id, namespace,
                         detail=None, target_owner_id=None):
    """Best-effort, fail-OPEN insert of an I2 denial into tenancy_denials.

    Called AFTER the handler's transaction has rolled back, so the audit row survives the
    denial. `tenancy_denials` carries no RLS
    policy (measured: pg_class.relrowsecurity = false) and meshychat_app holds INSERT, so
    this runs on the plain autocommit connection outside any server_internal block.

    caller_owner_id is the ACTOR. target_owner_id is who the denial is ABOUT: for a
    grant-ban the actor is the granting admin and the target is the grantee, so they
    differ; for a write-ban the caller IS the subject and target stays NULL.

    An audit failure must never upgrade a policy deny to a 500, but it must be
    OBSERVABLE - hence the counter and the ERROR log rather than a bare pass.
    """
    global AUDIT_FAILURES
    mapped = TENANCY_DENIAL_AUDIT.get(code)
    if mapped is None:
        return
    op, denial_class = mapped
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenancy_denials "
                "(denial_class, op, caller_owner_id, caller_peer_id, target_owner_id, "
                " namespace, detail) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (denial_class, op,
                 str(caller_owner_id) if caller_owner_id is not None else None,
                 str(caller_peer_id) if caller_peer_id is not None else None,
                 str(target_owner_id) if target_owner_id is not None else None,
                 namespace, detail),
            )
    except Exception as exc:
        AUDIT_FAILURES += 1
        log.error(
            "tenancy_denials audit insert FAILED (denial still enforced; audit trail NOT "
            "recorded) code=%s namespace=%s failures_total=%d: %s",
            code, namespace, AUDIT_FAILURES, exc, exc_info=True,
        )
