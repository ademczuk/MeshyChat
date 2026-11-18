"""Mint and revoke object-capability tokens (B07 slice 5, docs/DIVERGENCES.md 040).

The two routes of design docs/design/biscuit-capabilities.md 3.1, `POST /v1/memory/capabilities`
and `POST /v1/memory/capabilities/revoke`, are served by apps/memoryskills/views.py, which
imports this module only when MESHY_CAPABILITIES is on and a root key store is loaded. This is
the only code that writes `capability_event` and `capability_revocation` (sql_meshychat/019).

THE MINT (design 2.2, 2.3, 3.1; Q2, Q3; critique C2, C3, C9):

  * THE ISSUER IS THE AUTHENTICATED CALLER (act.sub, else sub: apps/peers/auth.py), and it must
    hold `admin` on the ONE namespace by `meshy_namespace_permission` AND be `active`, read in
    ONE statement inside the transaction that writes the register. Authentication already
    refused an inactive caller; a suspension that commits between the two is refused here
    (critique C6: the predicate reads no peer status).
  * THE REQUEST states the subject, exactly one namespace, the rights as (prefix, action) with
    action `read` or `write` (`admin` is never mintable, and `write` does not imply `read`), a
    depth cap of 0 to 4 and an expiry at most 24 hours after the mint's own clock (whole
    seconds; a fractional expiry is truncated to its second, never rounded up). Any second
    namespace is refused, however it is asked for. A namespace or prefix holding NUL or a lone
    surrogate is refused: neither the register nor the Biscuit builder can hold it.
  * THE SUBJECT must be an active peer; on a `shared/*` namespace its owner must be dev-plane,
    else E_PRODUCT_NO_SHARED_GRANT with the tenancy_denials row memory.grant writes (I2's grant
    half at the mint, critique C2; the verifier re-checks it at every use). The admin check
    comes first, so a non-admin learns nothing about the subject.
  * THE AUTHORITY BLOCK is 2.3's, built entirely from PARAMETERS: cap_version(1); issuer;
    subject (lowercase hyphenated uuid strings); one right per (prefix, action); depth_cap;
    `check if time($t), $t < expiry`; the active key's kid. Signed with the active root key.
  * THE REGISTER ROW (2.5 (c)) is written in the same transaction as the checks: issuer,
    subject, namespace, rights, depth cap, expiry, kid and the authority revocation id, its
    time the mint's clock. NEVER THE TOKEN.
  * THE TOKEN IS RETURNED ONCE, in the 201. It is not persisted and not logged, and nothing
    answers a later mint from an earlier one: a retried mint mints and registers a second token.

THE REVOKE (design 2.5 (a) "who may revoke" and (c), 3.1):

  * TWO FORMS. The token in the `Meshychat-Capability` header, the one channel a token is presented
    in (3.1), with `{"block": i, "reason"}`: the server parses it under the root keys (the
    verifier's step 1) and computes block i's revocation id itself. Or no header and
    `{"authority_id", "reason"}`, for the issuer or an admin, who may revoke a token they do
    not hold by the id the register holds. A body of the other form is refused.
  * THE REGISTER FINDS THE TOKEN (2.5 (c)): its issue row gives the issuer, the namespace and
    the expiry. No row, or a row naming another issuer or subject than the presented token's,
    is 404: this server did not issue it.
  * WHO: the issuer; any LIVE admin of the token's namespace, by `meshy_namespace_permission`
    and `active`, read in the statement that reads the register; or a peer that SIGNED a
    delegation block of the presented token, for that block and the blocks after it, never
    one before it (capability.blocks_signed_by: its live key signed the block AND it held the
    token there). A signer's standing is the presented token's, so it ends when the token does:
    from the register's expiry on, only the issuer or a live admin revokes it (a holder could
    otherwise fork a dead token to no one and write a permanent row per fork). Anyone else is
    403 E_PERMISSION_DENIED.
  * WHAT IS WRITTEN, in one transaction: the capability_revocation row (the block id, the
    caller as revoked_by_peer, the reason, not_after the token's own expiry from the register)
    and the revoke event (actor, the token's authority id, the block id, the reason). A block
    already revoked is 409 and writes nothing. The reason is at most 200 characters on one
    line (no control character, no line or paragraph separator), has a UTF-8 form, and never
    holds a window of the presented token: these rows are never pruned (Q8).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import re
import unicodedata
import uuid

from apps.core import dbconn
from apps.peers.errors import ApiError

from . import capability
from .errors import HandlerError
from .permissions import UNCLAIMED, answers_admitting, audit_tenancy_denial, is_dev_plane

log = logging.getLogger("meshychat.capability")

#: Q3: an authority lives at most this long after the mint.
MAX_TTL = _dt.timedelta(hours=24)
#: 2.3: depth_cap(n), n in 0..4, limits delegation hops.
MAX_DEPTH_CAP = capability.MAX_HOPS
#: 2.1: the actions a token carries. `admin` is never one.
ACTIONS = capability.ACTIONS
#: A bound on the request, so a token stays well inside the verifier's 8 KiB (3.1).
MAX_RIGHTS = 16
MAX_TEXT = 512

MINT_FIELDS = ("subject", "namespace", "rights", "depth_cap", "expires_at")
RIGHT_FIELDS = ("prefix", "action")
ONE_NAMESPACE = ("a capability names exactly one namespace (design 2.3, critique C9): send "
                 "`namespace` as one string; a peer that needs two holds two tokens")

VALIDATION = "E_VALIDATION"


def current_time() -> _dt.datetime:
    """The mint's clock: UTC, whole seconds (the verifier's decision clock is the same)."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _invalid(message: str) -> ApiError:
    return ApiError(422, VALIDATION, message)


def _canonical_peer(value, field: str) -> str:
    if type(value) is not str:
        raise _invalid(f"`{field}` must be a peer id, a string")
    try:
        text = str(uuid.UUID(value))
    except ValueError:
        text = None
    if text != value:
        raise _invalid(f"`{field}` must be a peer id in lowercase hyphenated form")
    return text


def _text(value, field: str, *, empty: bool) -> str:
    if type(value) is not str:
        raise _invalid(f"`{field}` must be a string")
    if (not value and not empty) or len(value) > MAX_TEXT:
        raise _invalid(f"`{field}` must be {'' if empty else 'a non-empty string '}"
                       f"at most {MAX_TEXT} characters")
    if chr(0) in value:
        # Postgres text and jsonb hold no NUL: the register could not record it (a 500).
        raise _invalid(f"`{field}` must not hold NUL")
    if not _has_utf8(value):
        # A JSON escape can spell a lone surrogate; neither Postgres nor the Biscuit builder
        # can encode it (a 500).
        raise _invalid(f"`{field}` must not hold a lone surrogate (it has no UTF-8 form)")
    return value


def _has_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# --------------------------------------------------------------------------- the request

@dataclasses.dataclass(frozen=True, slots=True)
class MintRequest:
    subject: str
    namespace: str
    rights: tuple            # ((prefix, action), ...), in the order asked
    depth_cap: int
    expires_at: _dt.datetime


def parse_mint(body, now: _dt.datetime) -> MintRequest:
    """The request, or 422 E_VALIDATION naming what is wrong. Nothing here reads the
    database, so a refusal costs nothing and signs nothing."""
    if not isinstance(body, dict):
        raise _invalid("the body must be a JSON object")
    if "namespaces" in body or isinstance(body.get("namespace"), list):
        raise _invalid(ONE_NAMESPACE)
    unknown = sorted(set(body) - set(MINT_FIELDS))
    if unknown:
        raise _invalid(f"unknown field(s): {', '.join(unknown)[:200]}; a mint states "
                       f"{', '.join(MINT_FIELDS)}")
    missing = [f for f in MINT_FIELDS if f not in body]
    if missing:
        raise _invalid(f"missing field(s): {', '.join(missing)}")
    subject = _canonical_peer(body["subject"], "subject")
    namespace = _text(body["namespace"], "namespace", empty=False)
    rights = _parse_rights(body["rights"])
    depth_cap = body["depth_cap"]
    if type(depth_cap) is not int or not 0 <= depth_cap <= MAX_DEPTH_CAP:
        raise _invalid(f"`depth_cap` must be an integer from 0 to {MAX_DEPTH_CAP}")
    expires_at = _parse_expiry(body["expires_at"], now)
    return MintRequest(subject, namespace, rights, depth_cap, expires_at)


def _parse_rights(raw) -> tuple:
    if not isinstance(raw, list) or not raw:
        raise _invalid("`rights` must be a non-empty list of {prefix, action}")
    if len(raw) > MAX_RIGHTS:
        raise _invalid(f"`rights` holds at most {MAX_RIGHTS} entries")
    out = []
    for n, right in enumerate(raw, start=1):
        if isinstance(right, dict) and "namespace" in right:
            raise _invalid(ONE_NAMESPACE)
        if not isinstance(right, dict) or sorted(right) != sorted(RIGHT_FIELDS):
            raise _invalid(f"right {n} must be exactly {{prefix, action}}")
        prefix = _text(right["prefix"], f"rights[{n}].prefix", empty=True)
        action = right["action"]
        if type(action) is not str or action not in ACTIONS:
            raise _invalid(f"right {n}: a capability carries read and write only; admin is "
                           "never mintable, and write does not imply read (design 2.1)")
        if (prefix, action) in out:
            raise _invalid(f"right {n} repeats an earlier one")
        out.append((prefix, action))
    return tuple(out)


def _parse_expiry(raw, now: _dt.datetime) -> _dt.datetime:
    if type(raw) is not str:
        raise _invalid("`expires_at` must be an ISO-8601 timestamp with a time zone")
    try:
        moment = _dt.datetime.fromisoformat(raw)
    except ValueError:
        raise _invalid("`expires_at` must be an ISO-8601 timestamp with a time zone") from None
    if moment.tzinfo is None:
        raise _invalid("`expires_at` must carry a time zone (Z or an offset)")
    try:
        moment = moment.astimezone(_dt.timezone.utc).replace(microsecond=0)
    except OverflowError:
        # 0001-01-01T00:00:00+05:00 has no UTC form a datetime can hold.
        raise _invalid("`expires_at` is outside the representable range") from None
    if moment <= now:
        raise _invalid("`expires_at` must be in the future")
    if moment - now > MAX_TTL:
        raise _invalid("`expires_at` is at most 24 hours after the mint (Q3); re-mint "
                       "for longer")
    return moment


# --------------------------------------------------------------------------- the mint

#: The issuer's standing and the subject's, in ONE statement inside the register's
#: transaction. The issuer's admin is THE predicate's answer (design 2.2, V1 rule A2).
MINT_SQL = """
SELECT meshychat_namespace_permission(%(ns)s, %(issuer)s::uuid)                  AS standing,
       (SELECT p.status FROM peers p WHERE p.id = %(issuer)s::uuid)          AS issuer_status,
       (SELECT p.owner_id::text FROM peers p WHERE p.id = %(issuer)s::uuid)  AS issuer_owner,
       (SELECT p.status FROM peers p WHERE p.id = %(subject)s::uuid)         AS subject_status,
       (SELECT p.owner_id::text FROM peers p WHERE p.id = %(subject)s::uuid) AS subject_owner
"""

REGISTER_SQL = """
INSERT INTO capability_event (event, at, actor_peer_id, authority_revocation_id,
                              issuer_peer_id, subject_peer_id, namespace, rights, depth_cap,
                              expires_at, kid)
VALUES ('issue', %(at)s, %(issuer)s, %(aid)s, %(issuer)s, %(subject)s, %(ns)s,
        %(rights)s::jsonb, %(depth_cap)s, %(expires_at)s, %(kid)s)
"""


class _PlaneRefusal(Exception):
    """Raised inside the transaction so it rolls back; the audit row is written after."""

    def __init__(self, error: ApiError, audit: dict):
        super().__init__(error.message)
        self.error, self.audit = error, audit


def authority_source(rights) -> tuple[str, dict]:
    """Design 2.3's block as Datalog text whose every term is a PARAMETER. The text holds
    only parameter NAMES; a namespace, prefix or peer id never reaches it."""
    lines = ["cap_version(1);", "issuer({issuer});", "subject({subject});"]
    params = {}
    for n, (prefix, action) in enumerate(rights):
        lines.append(f"right({{ns}}, {{p{n}}}, {{a{n}}});")
        params[f"p{n}"], params[f"a{n}"] = prefix, action
    lines += ["depth_cap({depth_cap});", "check if time($t), $t < {expires_at};"]
    return "\n".join(lines), params


def _sign(store, issuer: str, req: MintRequest):
    import biscuit_auth
    source, params = authority_source(req.rights)
    params.update({"issuer": issuer, "subject": req.subject, "ns": req.namespace,
                   "depth_cap": req.depth_cap, "expires_at": req.expires_at})
    builder = biscuit_auth.BiscuitBuilder(source, params)
    builder.set_root_key_id(store.active_kid)
    return builder.build(store.root_private_key())


def mint(conn, *, issuer: str, body, store, now: _dt.datetime | None = None) -> dict:
    """Validate, check, sign, register, in that order, and return the response body: the
    token and what it says. Raises ApiError for every refusal; a refusal writes no register
    row (the transaction rolls back), and the plane refusal writes its audit row after."""
    now = now or current_time()
    req = parse_mint(body, now)
    try:
        with dbconn.request_server_internal(conn) as c:
            with c.cursor() as cur:
                cur.execute(MINT_SQL, {"ns": req.namespace, "issuer": issuer,
                                       "subject": req.subject})
                standing, issuer_status, issuer_owner, subject_status, subject_owner = (
                    cur.fetchone())
            if issuer_status != "active":
                raise ApiError(403, "E_FORBIDDEN", "the issuer is not active: only an active "
                               "admin of the namespace mints")
            if standing == UNCLAIMED:
                raise ApiError(403, "E_NAMESPACE_UNCLAIMED",
                               f"namespace {req.namespace!r} is not claimed")
            if standing not in answers_admitting("admin"):
                raise ApiError(403, "E_PERMISSION_DENIED",
                               "only an admin of the namespace mints a capability on it (Q2)")
            if subject_status != "active":
                raise ApiError(422, "E_UNKNOWN_PEER", "the subject is not an active peer")
            if req.namespace.startswith("shared/") and not is_dev_plane(c, subject_owner):
                raise _PlaneRefusal(
                    ApiError(403, "E_PRODUCT_NO_SHARED_GRANT",
                             f"only dev-plane peers may hold a capability on a shared/* "
                             f"namespace ({req.namespace!r}; I2)"),
                    {"caller_owner_id": issuer_owner, "target_owner_id": subject_owner})
            token = _sign(store, issuer, req)
            encoded = token.to_base64()
            if len(encoded.encode("ascii")) > capability.MAX_TOKEN_BYTES:
                raise _invalid(f"the capability would be larger than "
                               f"{capability.MAX_TOKEN_BYTES} bytes; ask for fewer rights")
            authority_id = token.revocation_ids[0]
            rights = [{"prefix": p, "action": a} for p, a in req.rights]
            with c.cursor() as cur:
                cur.execute(REGISTER_SQL, {
                    "at": now, "issuer": issuer, "aid": bytes.fromhex(authority_id),
                    "subject": req.subject, "ns": req.namespace, "rights": json.dumps(rights),
                    "depth_cap": req.depth_cap, "expires_at": req.expires_at,
                    "kid": store.active_kid})
    except _PlaneRefusal as refusal:
        # The transaction has rolled back; the audit row is its own statement and survives,
        # as memory.grant's does (handlers.handle_grant).
        audit_tenancy_denial(conn, refusal.error.code, refusal.audit["caller_owner_id"],
                             issuer, req.namespace,
                             detail="product-plane subject denied a shared/* capability at "
                                    "the mint (I2)",
                             target_owner_id=refusal.audit["target_owner_id"])
        raise refusal.error from None
    log.info("capability minted: cap %s... by %s for %s on %r, %d right(s), depth %d, "
             "expires %s, kid %d", authority_id[:16], issuer, req.subject, req.namespace,
             len(rights), req.depth_cap, _iso(req.expires_at), store.active_kid)
    return {"token": encoded, "cap_id": authority_id, "issuer": issuer,
            "subject": req.subject, "namespace": req.namespace, "rights": rights,
            "depth_cap": req.depth_cap, "expires_at": _iso(req.expires_at),
            "kid": store.active_kid}


def _iso(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- the revoke

MAX_REASON = 200
#: A reason must not hold this many consecutive characters of the presented token.
TOKEN_WINDOW = 16
#: A reason is one line: no control character (Cc), no line or paragraph separator (Zl, Zp:
#: U+2028 and U+2029 end a line as a newline does).
NOT_IN_A_REASON = frozenset({"Cc", "Zl", "Zp"})
TOKEN_FORM = ("block", "reason")
ID_FORM = ("authority_id", "reason")
_AUTHORITY_ID = re.compile(r"[0-9a-f]{128}")


@dataclasses.dataclass(frozen=True, slots=True)
class RevokeRequest:
    block: int | None           # the token form: the block's index, 0 the authority
    authority_id: str | None    # the id form: the authority id, 128 lowercase hex digits
    reason: str


def _holds_window(text: str, token: str) -> bool:
    return any(text[i:i + TOKEN_WINDOW] in token for i in range(len(text) - TOKEN_WINDOW + 1))


def parse_revoke(body, presented: str | None) -> RevokeRequest:
    """The request, or 422 E_VALIDATION. `presented` is the header's value, or None."""
    if not isinstance(body, dict):
        raise _invalid("the body must be a JSON object")
    if presented is not None and "authority_id" in body:
        raise _invalid("one form or the other: with the token in Meshychat-Capability, name its "
                       "`block`; without it, name the `authority_id`")
    if presented is None and "block" in body:
        raise _invalid("`block` names a block of the token presented in Meshychat-Capability; "
                       "present the token there, or revoke by `authority_id`")
    fields = TOKEN_FORM if presented is not None else ID_FORM
    unknown = sorted(set(body) - set(fields))
    if unknown:
        raise _invalid(f"unknown field(s): {', '.join(unknown)[:200]}; this revoke states "
                       f"{', '.join(fields)}")
    missing = [f for f in fields if f not in body]
    if missing:
        raise _invalid(f"missing field(s): {', '.join(missing)}")
    reason = body["reason"]
    if type(reason) is not str or not reason or len(reason) > MAX_REASON:
        raise _invalid(f"`reason` must be a string of 1 to {MAX_REASON} characters")
    if any(unicodedata.category(ch) in NOT_IN_A_REASON for ch in reason):
        raise _invalid("`reason` must be one line with no control characters")
    if not _has_utf8(reason):
        raise _invalid("`reason` must not hold a lone surrogate (it has no UTF-8 form)")
    if presented is not None and _holds_window(reason, presented):
        raise _invalid("`reason` holds part of the token; the revocation rows are kept "
                       "forever and never hold it")
    if presented is not None:
        block = body["block"]
        if type(block) is not int or block < 0:
            raise _invalid("`block` must be a block's index, an integer from 0")
        return RevokeRequest(block, None, reason)
    authority_id = body["authority_id"]
    if type(authority_id) is not str or not _AUTHORITY_ID.fullmatch(authority_id):
        raise _invalid("`authority_id` must be 128 lowercase hex digits, the `cap_id` the "
                       "mint returned")
    return RevokeRequest(None, authority_id, reason)


#: The register's issue row for the token, and whether the caller is a LIVE admin of its
#: namespace by THE predicate and `active` (2.5 (a)), in one statement.
REVOKE_SQL = """
SELECT e.issuer_peer_id::text, e.subject_peer_id::text, e.namespace, e.expires_at,
       (coalesce(meshychat_namespace_permission(e.namespace, %(caller)s::uuid)
                 = ANY(%(admin)s::text[]), FALSE)
        AND EXISTS (SELECT 1 FROM peers p
                     WHERE p.id = %(caller)s::uuid AND p.status = 'active')) AS caller_admin
  FROM capability_event e
 WHERE e.event = 'issue' AND e.authority_revocation_id = %(aid)s
"""

REVOCATION_SQL = """
INSERT INTO capability_revocation (revocation_id, revoked_by_peer, reason, not_after)
VALUES (%(rid)s, %(caller)s, %(reason)s, %(not_after)s)
ON CONFLICT (revocation_id) DO NOTHING
RETURNING revoked_at
"""

REVOKE_EVENT_SQL = """
INSERT INTO capability_event (event, actor_peer_id, authority_revocation_id, revocation_id,
                              reason)
VALUES ('revoke', %(actor)s, %(aid)s, %(rid)s, %(reason)s)
"""


def write_revoke_event(cur, *, actor: str, authority_id: str, revocation_id: str,
                       reason: str) -> None:
    """The append-only record of who revoked which block and why (2.5 (a)), on the cursor of
    the revocation's own transaction."""
    cur.execute(REVOKE_EVENT_SQL, {"actor": actor, "aid": bytes.fromhex(authority_id),
                                   "rid": bytes.fromhex(revocation_id), "reason": reason})


def revoke(conn, *, caller: str, body, presented: str | None, store,
           now: _dt.datetime | None = None) -> dict:
    """Validate, read the token, find its issue row, decide who the caller is to it, write
    the revocation and its event in one transaction. Raises ApiError for every refusal, which
    writes nothing."""
    now = now or current_time()
    req = parse_revoke(body, presented)
    token = None
    if presented is not None:
        try:
            token = capability.presented(presented, store, now)
        except HandlerError as exc:
            raise ApiError(403, exc.code, exc.message) from None
        if req.block >= token.blocks:
            raise _invalid(f"`block` must be 0 to {token.blocks - 1}: the presented token has "
                           f"{token.blocks} block(s)")
        authority_id, block = token.authority_id, req.block
        target = token.revocation_ids[block]
    else:
        authority_id, block, target = req.authority_id, 0, req.authority_id
    with dbconn.request_server_internal(conn) as c:
        with c.cursor() as cur:
            cur.execute(REVOKE_SQL, {"aid": bytes.fromhex(authority_id), "caller": caller,
                                     "admin": list(answers_admitting("admin"))})
            row = cur.fetchone()
        if row is None or (token is not None and (row[0], row[1]) != (token.issuer,
                                                                       token.subject)):
            raise ApiError(404, "E_NOT_FOUND",
                           "no capability with this authority id was issued by this server")
        issuer, _subject, namespace, expires_at, caller_admin = row
        if caller == issuer:
            standing = "issuer"
        elif caller_admin:
            standing = "admin"
        else:
            signed = capability.blocks_signed_by(c, token, caller, now) if token else []
            if not signed:
                raise ApiError(403, "E_PERMISSION_DENIED",
                               "only the capability's issuer, a live admin of its namespace, "
                               "or a peer that signed a delegation block in it (for its own "
                               "block and those after it) revokes it")
            if now >= expires_at:
                # The authority's time check already refuses every use; a signer's standing is
                # the token's, and a revoke now would only add a row that is never pruned.
                raise ApiError(403, "E_PERMISSION_DENIED",
                               "the capability has expired: a signer's standing ends with it; "
                               "its issuer or a live admin of its namespace may still revoke it")
            if block < min(signed):
                raise ApiError(403, "E_PERMISSION_DENIED",
                               f"a signer revokes its own block ({min(signed)}) and the blocks "
                               f"after it, never block {block}")
            standing = "signer"
        with c.cursor() as cur:
            cur.execute(REVOCATION_SQL, {"rid": bytes.fromhex(target), "caller": caller,
                                         "reason": req.reason, "not_after": expires_at})
            inserted = cur.fetchone()
            if inserted is None:
                raise ApiError(409, "E_CONFLICT",
                               f"block {block} of this capability is already revoked")
            write_revoke_event(cur, actor=caller, authority_id=authority_id,
                               revocation_id=target, reason=req.reason)
    log.info("capability revoked: block %d of cap %s... by %s (%s) on %r", block,
             authority_id[:16], caller, standing, namespace)
    return {"revoked": {"revocation_id": target, "block": block, "authority_id": authority_id,
                        "namespace": namespace, "not_after": _iso(expires_at),
                        "revoked_at": _iso(inserted[0])},
            "standing": standing}


__all__ = ["MAX_TTL", "MINT_SQL", "MintRequest", "REVOCATION_SQL", "REVOKE_SQL",
           "RevokeRequest", "authority_source", "current_time", "mint", "parse_mint",
           "parse_revoke", "revoke", "write_revoke_event"]
