"""Authorization - spec 7.2 step 5.

THE INVARIANT THIS FILE EXISTS TO PRESERVE:

    The GrantRequest is built from the AUTHENTICATED identity - grantee = the caller
    owner resolved from the authenticated caller peer, grantor + granter_peer = the
    call's target - and the capability token is CHECKED AGAINST it. The token is
    NEVER used to BUILD it.

    A token that supplies its own (grantor, grantee, peer, skill) and is then checked
    against itself verifies vacuously: it says "I authorize what I say I authorize"
    and every signature check passes. `build_grant_request` below takes ONLY authenticated
    values as parameters; it does not accept a token at all, which makes the invariant
    a property of the signature rather than of reviewer attention.

THE GATE ORDER:

    0. same-tenant / dev-group  -> ALLOW with NO grant. Checked FIRST, and it
       short-circuits, so a trust-group call never needs a grant row. Moving this
       after the grant lookup would not be "stricter", it would be wrong: there is no
       grant to find and every intra-tenant call would 403.
    1. capability token (biscuit) -> offline verify, cheap crypto BEFORE the DB read.
       Deploy-dark: see capability.py.
    2. find_active_grant -> the sole gate whenever gate 1 is dormant.

FAIL-CLOSED: `same_tenant_or_devgroup` denies on NULL or error.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from apps.calls.errors import NotAuthorized


@dataclass(frozen=True)
class GrantRequest:
    """The four-field lookup key of find_active_grant.

    Every field here must originate from the authenticated channel or the routed
    request - never from a caller-supplied credential. See the module docstring.
    """
    grantor_owner_id: uuid.UUID
    grantee_owner_id: uuid.UUID
    granter_peer_id: uuid.UUID
    skill_name: str


@dataclass(frozen=True)
class SkillGrant:
    id: uuid.UUID
    grantor_owner_id: uuid.UUID
    grantee_owner_id: uuid.UUID
    granter_peer_id: uuid.UUID
    skill_name: str
    version_pin: str | None
    scopes: object | None


@dataclass(frozen=True)
class AuthorizeResult:
    """The resolved authorization result.

    `grant_id is None` is NOT "unauthorized" - it is the same-tenant / dev-group
    ALLOW, which has no grant row. The pipeline writes that None straight into
    skill_calls.grant_id, so a NULL there means "grant-free allow", never "we forgot".
    """
    grant_id: uuid.UUID | None
    same_tenant: bool


def build_grant_request(*, caller_owner_id, target_owner_id, target_peer_id,
                        skill_name: str) -> GrantRequest:
    """Build the request from AUTHENTICATED identity only.

    Deliberately takes no token, no envelope and no request body: there is nothing to
    pass that could smuggle an attacker-chosen grantor/grantee in. The direction is
    fixed and easy to get backwards - the TARGET owner is the GRANTOR (it is their
    skill being lent out) and the CALLER owner is the GRANTEE.
    """
    return GrantRequest(
        grantor_owner_id=uuid.UUID(str(target_owner_id)),
        grantee_owner_id=uuid.UUID(str(caller_owner_id)),
        granter_peer_id=uuid.UUID(str(target_peer_id)),
        skill_name=skill_name,
    )


def lookup_owner_for_peer(conn, peer_id) -> uuid.UUID:
    """`status = 'active'` is part of the predicate: a
    suspended peer resolves to no owner and the call is denied, rather than resolving
    to an owner and being authorized."""
    with conn.cursor() as cur:
        cur.execute("SELECT owner_id FROM peers WHERE id = %s AND status = 'active'",
                    (str(peer_id),))
        row = cur.fetchone()
    if row is None:
        raise NotAuthorized(f"peer {peer_id} is not active")
    return row[0]


def same_tenant_or_devgroup(conn, caller_owner_id, target_owner_id) -> bool:
    """Call the SQL function `meshychat_same_tenant_or_devgroup` (migration 039)
    rather than re-expressing the predicate in Python.

    That indirection is the point: the same function backs the invoke gate, the
    memory read gate, the dashboard visibility CTE and the daemon write gate. A Python
    implementation here would be a fifth predicate free to drift from the other four.
    Fail-closed on NULL.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT meshychat_same_tenant_or_devgroup(%s, %s)",
                    (str(caller_owner_id), str(target_owner_id)))
        allowed = cur.fetchone()[0]
    return bool(allowed) if allowed is not None else False


def _scope_set(scopes) -> set[str]:
    """Fail-closed: anything that is not a list of strings or a
    bare string maps to the EMPTY set (least authority), never to "all"."""
    if isinstance(scopes, list):
        return {s for s in scopes if isinstance(s, str)}
    if isinstance(scopes, str):
        return {scopes}
    return set()


def grant_self_scope_consistent(skill_name: str, scopes) -> bool:
    """Skill-family CONDITIONAL.

    A grant that names ANY `meshy:skill:*` scope must name its OWN skill, or it is
    incoherent and self-denies. A grant with no scopes, or only non-skill scopes
    (`read`, `meshy:flow:*`, `meshy:delegate:*`), is not skill-scoped and stays UNGATED -
    denying those would break every pre-scope grant.
    """
    scope_set = _scope_set(scopes)
    has_skill_scope = any(s.startswith("meshy:skill:") for s in scope_set)
    return (not has_skill_scope) or (f"meshy:skill:{skill_name}" in scope_set)


def find_active_grant(conn, req: GrantRequest) -> SkillGrant | None:
    """Find a live grant that matches all four request fields.

    LIVENESS IS PART OF THE LOOKUP, not a later filter: `revoked_at IS NULL AND
    (expires_at IS NULL OR expires_at > NOW())`. Selecting the row first and checking
    liveness in Python would be the "counting history, not capability" bug - and
    `expires_at IS NULL` means "never expires", so it must be permitted explicitly or
    every unexpiring grant silently stops matching.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, grantor_owner_id, grantee_owner_id, granter_peer_id, "
            "       skill_name, version_pin, scopes "
            "FROM skill_grants "
            "WHERE grantor_owner_id = %s "
            "  AND grantee_owner_id = %s "
            "  AND granter_peer_id = %s "
            "  AND skill_name = %s "
            "  AND revoked_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > NOW())",
            (str(req.grantor_owner_id), str(req.grantee_owner_id),
             str(req.granter_peer_id), req.skill_name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    grant = SkillGrant(id=row[0], grantor_owner_id=row[1], grantee_owner_id=row[2],
                       granter_peer_id=row[3], skill_name=row[4], version_pin=row[5],
                       scopes=row[6])
    # The self-scope filter is applied to the RESULT, so an
    # incoherent grant reads back as "no grant" and denies.
    if not grant_self_scope_consistent(grant.skill_name, grant.scopes):
        return None
    return grant


def version_pin_permits(version_pin: str | None, resolved_version: str) -> bool:
    """TASK-19: an UNPINNED grant permits any
    version; a pinned one must equal the RESOLVED version. `=1.2.3` is the same exact
    match as `1.2.3`; a range prefix is unsupported and denies rather than guessing."""
    if version_pin is None:
        return True
    pin = version_pin.strip()
    if pin[:1] in {"^", "~", ">", "<"}:
        return False
    return pin.lstrip("=") == resolved_version


def authorize(conn, *, caller_peer_id, target_owner_id, target_peer_id, skill,
              capability_token: str | None = None) -> AuthorizeResult:
    """The gate sequence. Returns an AuthorizeResult or raises NotAuthorized.

    ORDER IS THE CONTRACT (see module docstring). The caller's OWNER is resolved here
    from the AUTHENTICATED caller peer - the pipeline never passes an owner in, so
    there is no parameter through which a caller could nominate their own tenancy.
    """
    caller_owner_id = lookup_owner_for_peer(conn, caller_peer_id)

    # Gate 0: same tenant or dev trust group -> allowed WITHOUT a grant.
    # A capability token is only meaningful cross-tenant, so it is not consulted here.
    if same_tenant_or_devgroup(conn, caller_owner_id, target_owner_id):
        return AuthorizeResult(grant_id=None, same_tenant=True)

    # The request is built from authenticated identity BEFORE either gate sees it.
    req = build_grant_request(caller_owner_id=caller_owner_id,
                              target_owner_id=target_owner_id,
                              target_peer_id=target_peer_id,
                              skill_name=skill["name"])

    # Gate 1: offline capability-token verify, checked AGAINST `req`.
    if capability_token is not None:
        from apps.calls import capability
        if not capability.verify_grant_biscuit(capability_token, req):
            raise NotAuthorized("capability token does not authorize this call")

    # Gate 2: the live grant lookup. Cross-tenant requires an active grant REGARDLESS
    # of share_mode - a 'public' skill is public WITHIN the
    # tenant only, so share_mode is deliberately NOT consulted here.
    grant = find_active_grant(conn, req)
    if grant is None:
        raise NotAuthorized(
            f"no active grant for skill {skill['name']} from owner {target_owner_id}")

    # TASK-19: the pin is checked against the RESOLVED version, so a grant pinned to
    # one version cannot ride along when the target publishes a new current version.
    if not version_pin_permits(grant.version_pin, skill["version"]):
        raise NotAuthorized(
            f"grant is pinned to version {grant.version_pin}, call resolved "
            f"{skill['version']}")

    return AuthorizeResult(grant_id=grant.id, same_tenant=False)
