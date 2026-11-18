"""Object-capability tokens: the verifier, dark (B07 slice 4, docs/DIVERGENCES.md 040).

Design docs/design/biscuit-capabilities.md 1.3, 2.3-2.5, 3.2, 3.3 (the principal branch), 4.1,
4.2 and 5.1. check_permission calls `decide` when a caller passes a `principal`: since slice 6
the memory handlers do (sites 2 to 5), with the token the envelope path's views read from
`Meshychat-Capability` and pre-checked with `precheck` (3.2 steps 1 and 3) before the call started.
With MESHY_CAPABILITIES off no key store is loaded, a principal is E_CAPABILITY_UNAVAILABLE, and
this module never imports biscuit_auth.

`authorize_capability` decides in 3.2's order, each step a typed refusal whose text never holds
the token or any part of it (a handler's refusal is persisted in skill_calls since slice 6):

  1. SIZE AND PARSE. At most 8 KiB (3.1); `Biscuit.from_base64` with the key store's kid
     provider, so a foreign root, an unknown kid, a token with no kid and any tampering fail
     here. biscuit-auth 6.0.0 parses third-party blocks in its PreviousSignatureHashing mode:
     a block lifted from another token, and a legacy (v0) third-party block, are refused
     (tests/test_capability_verifier.py T15 settles the design's inference 7).
     E_CAPABILITY_INVALID.
  2. THE CACHE, keyed by sha256(token), holds the crypto result only: the verified token and
     what its signed blocks say (subject, issuer, depth cap, each third-party block's signer
     and its one delegate pair). LRU of 1024, an entry lives min(60 s, the token's earliest
     expiry), and belongs to the key store it was verified with. No negative caching. The
     holder walk's key check, the liveness statement and the authorizer run on every use.
  3. THE HOLDER WALK (1.3, 2.4). Every third-party block must be signed by a LIVE key of the
     peer holding the token at that point, by the grace-aware rule of fetch_caller_pubkey
     (active, or revoked with the grace window still open), read from peer_keys.public_key
      (base64url TEXT) and compared as bytes; the eight small-order points are refused (Q7).
      Exactly one delegate / delegation_expires pair `trusting` that key;
     a hop's expiry must be in the future (checked HERE, because the delegator writes its own
     block and may leave the time check out) and at most 300 s away; no peer twice; hops at
     most min(depth_cap, 4); at most 10 blocks. The caller must be the final holder.
     E_CAPABILITY_INVALID for a malformed chain, E_CAPABILITY_HOLDER for a block the holder
     did not sign and for a caller that is not the holder, E_CAPABILITY_DENIED for an expired
     hop.
  4. LIVENESS, ONE SQL statement with 2.5 (b)'s six clauses, never cached: no presented
     revocation id revoked (E_CAPABILITY_REVOKED); the authority id in the issuance register
     and not past the register's expiry, on a row that names the token's own issuer and
     subject and the REQUEST's namespace (review F3: clause 3 reads the issuer's standing on
     the request's namespace before the Datalog compares it with the token's one namespace,
     so without this a holder learned where its issuer is admin, and a foreign shared/*
     namespace wrote a plane audit per request); the issuer holding admin on the namespace by
     meshy_namespace_permission AND active (critique C6: check_permission alone reads no peer
     status); every chain peer active; every delegation key live, a key with NO row DEAD (not
     030's key_states reading); on shared/*, every chain peer's owner dev-plane, else
     E_PRODUCT_NO_SHARED_GRANT with the audit_tenancy_denial row I2's grant half writes
     (critique C2). The others are E_CAPABILITY_DENIED, each with its own message.
  5. DATALOG (4.1). The host facts time, caller, operation, resource and action are
     PARAMETERS, never spliced. The allow policy requires cap_version(1) (critique C1: a token
     minted for another purpose never matches) and a right covering the request; every
     block's checks must hold; block facts are invisible to the policy. Every authorizer this
     module builds, the walk's included, carries explicit limits: 1000 facts, 100 iterations,
      100 ms (the 1 ms default run time limit trips a legitimate decision under load, 1.3 row
      20). A run-limit error is a denial, and is
     counted. E_CAPABILITY_DENIED.
  6. THE DECISION RECORD, {"via": "capability", "cap_id", "issuer", "holder_chain", "hops"}.

A PRESENTED TOKEN IS THE WHOLE AUTHORITY FOR THE REQUEST (4.2). check_permission never falls
through to the caller's own grants when a principal is present, and a request that needs
`admin` never takes a token (E_CAPABILITY_NOT_ACCEPTED): `admin` is never in a token (2.1).

Run under rls.server_internal, as check_permission is: the register and revocation tables, and
the namespace predicate, are server_internal only. Outside it the register looks empty and
every token is refused, which is the fail-closed direction.
"""
from __future__ import annotations

import base64
import collections
import dataclasses
import datetime as _dt
import hashlib
import logging
import re
import threading
import time
import uuid

from . import capability_keys
from .errors import HandlerError
from .permissions import Key, Prefix, answers_admitting

log = logging.getLogger("meshychat.capability")

MAX_TOKEN_BYTES = 8192
MAX_BLOCKS = 10
MAX_HOPS = 4
MAX_HOP_SECONDS = 300
LIMIT_FACTS = 1000
LIMIT_ITERATIONS = 100
LIMIT_TIME = _dt.timedelta(milliseconds=100)
CACHE_SIZE = 1024
CACHE_SECONDS = 60

INVALID = "E_CAPABILITY_INVALID"
HOLDER = "E_CAPABILITY_HOLDER"
REVOKED = "E_CAPABILITY_REVOKED"
DENIED = "E_CAPABILITY_DENIED"
NOT_ACCEPTED = "E_CAPABILITY_NOT_ACCEPTED"
UNAVAILABLE = "E_CAPABILITY_UNAVAILABLE"
PLANE = "E_PRODUCT_NO_SHARED_GRANT"

#: The actions a token can carry (2.1). `admin` is never one.
ACTIONS = ("read", "write")
#: Design 4.3: the operations v1 takes a token for, the action each needs, and the shape of
#: what each touches, a point (Key) or a range (Prefix). Anything else is NOT_ACCEPTED.
OPERATION_ACTION = {"memory.read": "read", "memory.query": "read", "memory.list": "read",
                    "memory.write": "write"}
OPERATION_RESOURCE = {"memory.read": Key, "memory.write": Key, "memory.query": Prefix,
                      "memory.list": Prefix}

#: One message per liveness clause (2.5 (b)), none naming a row or the token.
CLAUSE_MESSAGES = {
    "revoked": "the capability, or a block it was derived from, has been revoked",
    "register": "the capability was not issued by this server for this namespace: no "
                "issuance register row holds its authority with its issuer, subject and this "
                "namespace, or the row's expiry has passed",
    "issuer": "the capability's issuer no longer holds admin on the namespace, or is not active",
    "chain": "a peer in the capability's holder chain is not active",
    "keys": "a delegation in the capability was signed with a key that is no longer live",
    "plane": "a capability on a shared/* namespace is held by, or passes through, a "
             "product-plane peer (I2)",
}

#: 4.1: the host facts, every value a parameter, and the one allow. `cap_version(1)` and
#: `right` are read from the authority block (an authorizer policy trusts the authority and
#: itself, never another block), `resource` and `action` from the facts above it.
POLICY = """
time({now});
caller({caller});
operation({operation});
resource({namespace}, {resource});
action({action});
allow if cap_version(1), right($ns, $p, $a), resource($ns, $r), $r.starts_with($p), action($a);
deny if true;
"""

_DELEGATE_RULE = "d($e, $x) <- delegate($e), delegation_expires($x) trusting {signer}"
_EXPIRY_CHECK = re.compile(r"check if time\(\$t\), \$t < (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ);")
_SPARED = (KeyboardInterrupt, SystemExit, GeneratorExit)


def _bx():
    """biscuit_auth, imported only when a token is being decided: never with tokens off."""
    import biscuit_auth
    return biscuit_auth


# --------------------------------------------------------------------------- the principal

@dataclasses.dataclass(frozen=True, slots=True, repr=False)
class PresentedCapability:
    """A token as a transport presents it, and the operation it was presented for (the
    dispatched skill). Only its sha256 is ever shown: the repr carries no part of the token."""

    token: str
    operation: str
    sha256: str = dataclasses.field(init=False)

    def __post_init__(self):
        if type(self.token) is not str or type(self.operation) is not str:
            raise TypeError("PresentedCapability(token, operation) takes two strings")
        digest = hashlib.sha256(self.token.encode("utf-8", "surrogatepass")).hexdigest()
        object.__setattr__(self, "sha256", digest)

    def __repr__(self) -> str:
        return f"PresentedCapability(operation={self.operation!r}, sha256={self.sha256[:12]}...)"

    __str__ = __repr__


# --------------------------------------------------------------------------- refusals

_RUN_LIMIT_DENIALS = 0


def run_limit_denials() -> int:
    """How many decisions a Datalog run limit has denied in this process (3.2 step 5). A
    legitimate decision should never reach one; the count is what monitoring reads."""
    return _RUN_LIMIT_DENIALS


def _refuse(code: str, message: str, digest: str | None = None) -> HandlerError:
    log.info("capability refused: %s: %s (cap sha256 %s)", code, message,
             digest[:12] if digest else "-")
    return HandlerError(code, message)


def _library_failure(exc: BaseException, code: str, message: str, digest) -> HandlerError:
    """A biscuit call failed: a run-limit error is a denial and is counted; anything else is
    `code`. Never the library's own text, which could quote a term."""
    global _RUN_LIMIT_DENIALS
    if isinstance(exc, _SPARED):
        raise exc
    if "execution limits" in str(exc):
        _RUN_LIMIT_DENIALS += 1
        log.warning("capability denied by a Datalog run limit (count %d, cap sha256 %s)",
                    _RUN_LIMIT_DENIALS, digest[:12] if digest else "-")
        return _refuse(DENIED, "the capability's Datalog exceeded the verifier's limits",
                       digest)
    return _refuse(code, message, digest)


def _limited(builder):
    """The explicit limits, on every authorizer, before it is built."""
    lim = builder.limits()
    lim.max_facts, lim.max_iterations, lim.max_time = LIMIT_FACTS, LIMIT_ITERATIONS, LIMIT_TIME
    builder.set_limits(lim)
    return builder


def _canonical_uuid(value) -> str | None:
    """`value` as a lowercase hyphenated uuid string, when it is exactly one (2.3)."""
    try:
        text = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None
    return text if text == str(value).lower() else None


def _delegation_key_bytes(raw: bytes) -> bytes:
    """A third-party block's signer, as the 32 key bytes the walk compares. The eight
    small-order points are refused here whatever biscuit's own verify_strict does (Q7)."""
    from apps.crypto import primitives
    raw = bytes(raw)
    if len(raw) != 32 or raw in primitives._SMALL_ORDER:
        raise _refuse(INVALID, "a delegation block is signed by a key that is not a usable "
                               "Ed25519 public key")
    return raw


def _decoded(text) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- steps 1 and 2

@dataclasses.dataclass(frozen=True, slots=True)
class _Hop:
    index: int
    signer: bytes
    delegate: str
    expires: _dt.datetime


@dataclasses.dataclass(frozen=True, slots=True, repr=False)
class _Verified:
    """What the signatures vouch for: the crypto result the cache holds."""
    biscuit: object
    digest: str
    authority_id: str
    revocation_ids: tuple
    issuer: str
    subject: str
    depth_cap: int
    hops: tuple
    blocks: int
    earliest_expiry: _dt.datetime | None


class _Cache:
    """sha256(token) -> _Verified, LRU, each entry with its own deadline and its key store."""

    def __init__(self, size: int):
        self.size = size
        self._entries: collections.OrderedDict = collections.OrderedDict()
        self._lock = threading.Lock()

    def get(self, digest: str, store):
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                return None
            verified, entry_store, deadline = entry
            if entry_store is not store or time.monotonic() >= deadline:
                del self._entries[digest]
                return None
            self._entries.move_to_end(digest)
            return verified

    def put(self, digest: str, verified, store, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._entries[digest] = (verified, store, time.monotonic() + seconds)
            self._entries.move_to_end(digest)
            while len(self._entries) > self.size:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_CACHE = _Cache(CACHE_SIZE)


def clear_cache() -> None:
    _CACHE.clear()


def _query(authorizer, rule, digest):
    try:
        return authorizer.query(rule)
    except BaseException as exc:                 # noqa: BLE001 - see _library_failure
        raise _library_failure(exc, INVALID, "the capability's blocks could not be read",
                               digest) from None


def _one(authorizer, source: str, kind: type, name: str, digest):
    rows = _query(authorizer, _bx().Rule(source), digest)
    if len(rows) != 1 or len(rows[0].terms) != 1 or type(rows[0].terms[0]) is not kind:
        raise _refuse(INVALID, f"the capability's authority block must state exactly one "
                               f"{name}", digest)
    return rows[0].terms[0]


def _authority_expiry(biscuit) -> _dt.datetime | None:
    """The authority block's `check if time($t), $t < X`, as 2.3 writes it: only the cache's
    entry life reads it. The authorizer enforces the check itself on every use."""
    try:
        found = _EXPIRY_CHECK.findall(biscuit.block_source(0))
    except BaseException as exc:                 # noqa: BLE001
        if isinstance(exc, _SPARED):
            raise
        return None
    if len(found) != 1:
        return None
    return _dt.datetime.fromisoformat(found[0].replace("Z", "+00:00"))


def _extract(biscuit, digest: str) -> _Verified:
    b = _bx()
    blocks = biscuit.block_count()
    if blocks > MAX_BLOCKS:
        raise _refuse(INVALID, f"the capability has {blocks} blocks; at most {MAX_BLOCKS} are "
                               "accepted", digest)
    ids = tuple(biscuit.revocation_ids)
    try:
        walker = _limited(b.AuthorizerBuilder()).build(biscuit)
    except BaseException as exc:                 # noqa: BLE001
        raise _library_failure(exc, INVALID, "the capability's blocks could not be read",
                               digest) from None
    subject = _one(walker, "s($s) <- subject($s)", str, "subject", digest)
    issuer = _one(walker, "s($s) <- issuer($s)", str, "issuer", digest)
    depth_cap = _one(walker, "s($n) <- depth_cap($n)", int, "depth_cap", digest)
    if _canonical_uuid(subject) != subject or _canonical_uuid(issuer) != issuer:
        raise _refuse(INVALID, "the capability's subject and issuer must be peer ids", digest)
    if depth_cap < 0:
        raise _refuse(INVALID, "the capability's depth cap is negative", digest)
    hops = []
    for index in range(1, blocks):
        external = biscuit.block_external_key(index)
        if external is None:
            continue                             # a first-party block: checks only
        signer = _delegation_key_bytes(bytes(external.to_bytes()))
        rows = _query(walker, b.Rule(_DELEGATE_RULE, {}, {"signer": external}), digest)
        if len(rows) != 1:
            raise _refuse(INVALID, f"delegation block {index} must state exactly one delegate "
                                   "and one delegation_expires under its signer's key", digest)
        delegate, expires = rows[0].terms
        if (type(delegate) is not str or _canonical_uuid(delegate) != delegate
                or not isinstance(expires, _dt.datetime)):
            raise _refuse(INVALID, f"delegation block {index} must name a peer id and an "
                                   "expiry", digest)
        hops.append(_Hop(index, signer, delegate, expires))
    expiries = [h.expires for h in hops]
    authority = _authority_expiry(biscuit)
    if authority is not None:
        expiries.append(authority)
    return _Verified(biscuit, digest, ids[0], ids, issuer, subject, depth_cap, tuple(hops),
                     blocks, min(expiries) if expiries else None)


def _verify(token, store, now: _dt.datetime) -> _Verified:
    if type(token) is not str:
        raise _refuse(INVALID, "the capability token is not a string")
    raw = token.encode("utf-8", "surrogatepass")
    if len(raw) > MAX_TOKEN_BYTES:
        raise _refuse(INVALID, f"the capability token is larger than {MAX_TOKEN_BYTES} bytes")
    digest = hashlib.sha256(raw).hexdigest()
    hit = _CACHE.get(digest, store)
    if hit is not None:
        return hit
    try:
        biscuit = _bx().Biscuit.from_base64(token, store.provider)
    except BaseException as exc:                 # noqa: BLE001 - a panic is BaseException
        raise _library_failure(exc, INVALID, "the capability token could not be parsed, or a "
                                             "signature in it does not verify under this "
                                             "server's root keys", digest) from None
    verified = _extract(biscuit, digest)
    life = CACHE_SECONDS
    if verified.earliest_expiry is not None:
        life = min(life, (verified.earliest_expiry - now).total_seconds())
    _CACHE.put(digest, verified, store, life)
    return verified


# --------------------------------------------------------------------------- step 3

@dataclasses.dataclass(frozen=True, slots=True)
class _Chain:
    peers: tuple
    delegation_keys: tuple          # ((signer peer id, peer_keys.public_key), ...)
    hops: int


def _live_keys(conn, peer_ids, now) -> list:
    """(peer id, public_key text) for every LIVE key of `peer_ids`: active, or revoked with
    the grace window still open (fetch_caller_pubkey's rule, permissions.py)."""
    with conn.cursor() as cur:
        cur.execute("SELECT peer_id::text, public_key FROM peer_keys "
                    "WHERE peer_id = ANY(%s::uuid[]) "
                    "AND (active = TRUE OR (revoked_at IS NOT NULL AND revoked_at > %s))",
                    (list(peer_ids), now))
        return cur.fetchall()


def _walk(conn, v: _Verified, caller: str | None, now: _dt.datetime) -> _Chain:
    peers = (v.subject,) + tuple(h.delegate for h in v.hops)
    if len(set(peers)) != len(peers):
        raise _refuse(INVALID, "a peer appears twice in the capability's holder chain",
                      v.digest)
    cap = min(v.depth_cap, MAX_HOPS)
    if len(v.hops) > cap:
        raise _refuse(INVALID, f"the capability has {len(v.hops)} delegation hops; its depth "
                               f"cap allows {cap}", v.digest)
    for h in v.hops:
        if h.expires <= now:
            raise _refuse(DENIED, "a delegation in the capability has expired", v.digest)
        if (h.expires - now).total_seconds() > MAX_HOP_SECONDS:
            raise _refuse(INVALID, f"a delegation in the capability has more than "
                                   f"{MAX_HOP_SECONDS} s of life left", v.digest)
    live = _live_keys(conn, peers[:-1], now) if v.hops else []
    holder, keys = v.subject, []
    for h in v.hops:
        signed = [text for peer, text in live if peer == holder and _decoded(text) == h.signer]
        if not signed:
            raise _refuse(HOLDER, f"delegation block {h.index} is not signed by a live key of "
                                  "the peer holding the capability", v.digest)
        keys.append((holder, signed[0]))
        holder = h.delegate
    if caller is None or caller != holder:
        raise _refuse(HOLDER, "the caller is not the capability's holder", v.digest)
    return _Chain(peers, tuple(keys), len(v.hops))


# --------------------------------------------------------------------------- step 4

#: 2.5 (b), the six clauses in ONE statement, run on every use and never cached.
LIVENESS_SQL = """
WITH chain AS (
    SELECT c.peer_id, p.status, p.owner_id, u.plane
      FROM unnest(%(chain)s::uuid[]) AS c(peer_id)
      LEFT JOIN peers p ON p.id = c.peer_id
      LEFT JOIN dashboard_users u ON u.id = p.owner_id
), dkeys AS (
    SELECT * FROM unnest(%(dpeers)s::uuid[], %(dkeys)s::text[]) AS d(peer_id, public_key)
)
SELECT
    EXISTS (SELECT 1 FROM capability_revocation r
             WHERE r.revocation_id = ANY(%(rids)s::bytea[]))                AS revoked,
    EXISTS (SELECT 1 FROM capability_event e
             WHERE e.event = 'issue' AND e.authority_revocation_id = %(aid)s
               AND e.namespace = %(ns)s
               AND e.issuer_peer_id = %(issuer)s::uuid
               AND e.subject_peer_id = %(subject)s::uuid
               AND e.expires_at > %(now)s)                                  AS registered,
    (coalesce(meshychat_namespace_permission(%(ns)s, %(issuer)s::uuid) = ANY(%(admin)s::text[]),
              FALSE)
     AND EXISTS (SELECT 1 FROM peers p
                  WHERE p.id = %(issuer)s::uuid AND p.status = 'active'))   AS issuer_ok,
    NOT EXISTS (SELECT 1 FROM chain WHERE status IS DISTINCT FROM 'active') AS chain_ok,
    NOT EXISTS (SELECT 1 FROM dkeys d
                 WHERE NOT EXISTS (
                     SELECT 1 FROM peer_keys k
                      WHERE k.peer_id = d.peer_id AND k.public_key = d.public_key
                        AND (k.active = TRUE
                             OR (k.revoked_at IS NOT NULL AND k.revoked_at > %(now)s))))
                                                                            AS keys_ok,
    (NOT starts_with(%(ns)s, 'shared/')
     OR NOT EXISTS (SELECT 1 FROM chain WHERE plane IS DISTINCT FROM 'dev')) AS plane_ok,
    ARRAY(SELECT DISTINCT owner_id::text FROM chain
           WHERE plane IS DISTINCT FROM 'dev' AND owner_id IS NOT NULL)     AS product_owners,
    (SELECT owner_id::text FROM peers WHERE id = %(caller)s::uuid)          AS caller_owner
"""


def _liveness(conn, v: _Verified, chain: _Chain, namespace: str, caller: str,
              now: _dt.datetime) -> None:
    params = {"chain": list(chain.peers),
              "dpeers": [peer for peer, _ in chain.delegation_keys],
              "dkeys": [text for _, text in chain.delegation_keys],
              "rids": [bytes.fromhex(rid) for rid in v.revocation_ids],
              "aid": bytes.fromhex(v.authority_id), "now": now, "ns": namespace,
              "issuer": v.issuer, "subject": v.subject,
              "admin": list(answers_admitting("admin")), "caller": caller}
    with conn.cursor() as cur:
        cur.execute(LIVENESS_SQL, params)
        (revoked, registered, issuer_ok, chain_ok, keys_ok, plane_ok, product_owners,
         caller_owner) = cur.fetchone()
    if revoked:
        raise _refuse(REVOKED, CLAUSE_MESSAGES["revoked"], v.digest)
    for ok, clause in ((registered, "register"), (issuer_ok, "issuer"), (chain_ok, "chain"),
                       (keys_ok, "keys")):
        if not ok:
            raise _refuse(DENIED, CLAUSE_MESSAGES[clause], v.digest)
    if not plane_ok:
        exc = _refuse(PLANE, CLAUSE_MESSAGES["plane"], v.digest)
        # Written by handlers.dispatch after the handler's transaction has rolled back, as
        # handle_grant writes its own (permissions.audit_tenancy_denial).
        exc.deferred_audit = {
            "code": PLANE, "caller_owner_id": caller_owner, "caller_peer_id": caller,
            "namespace": namespace, "target_owner_id": (product_owners or [None])[0],
            "detail": "product-plane peer in a capability's holder chain on shared/* "
                      "(I2 grant half, at use)"}
        raise exc


# --------------------------------------------------------------------------- step 5

def _authorize(v: _Verified, caller: str, namespace: str, resource: str, action: str,
               operation: str, now: _dt.datetime) -> None:
    b = _bx()
    params = {"now": now, "caller": caller, "operation": operation, "namespace": namespace,
              "resource": resource, "action": action}
    try:
        _limited(b.AuthorizerBuilder(POLICY, params)).build(v.biscuit).authorize()
    except BaseException as exc:                 # noqa: BLE001 - see _library_failure
        raise _library_failure(exc, DENIED, "the capability does not allow this request",
                               v.digest) from None


# --------------------------------------------------------------------------- the decision

def _resource_term(resource) -> str:
    """The key (a point) or the prefix (a range) the `resource` fact names."""
    if type(resource) is Key:
        value, is_range = resource.k, False
    elif type(resource) is Prefix:
        value, is_range = resource.p, True
    else:
        raise _refuse(NOT_ACCEPTED, "a capability authorises a key or a key range, never the "
                                    "namespace itself")
    if type(value) is not str:
        raise _refuse(DENIED, "the key a capability is asked about is not a string")
    if is_range and "\\" in value:
        # memory.query and memory.list escape % and _ but not \, LIKE's escape character, so
        # their SQL would not match this prefix literally (divergence 040, slice 2's review F5).
        raise _refuse(DENIED, "a key range holding a backslash cannot be decided by a "
                              "capability")
    return value


def _whole_seconds(now: _dt.datetime) -> _dt.datetime:
    if not isinstance(now, _dt.datetime) or now.tzinfo is None:
        raise TypeError("now must be a timezone-aware datetime")
    return now.astimezone(_dt.timezone.utc).replace(microsecond=0)


def authorize_capability(conn, *, token, caller_peer_id, namespace, resource, action,
                         operation, now) -> dict:
    """3.2, in order. Returns the decision record or raises HandlerError (module docstring)."""
    store = capability_keys.current()
    if store is None:
        raise _refuse(UNAVAILABLE, "capability tokens are not available on this server")
    if action not in ACTIONS:
        raise _refuse(NOT_ACCEPTED, "a capability carries read and write only; a request "
                                    "that needs admin never takes one")
    if operation not in OPERATION_ACTION:
        raise _refuse(NOT_ACCEPTED, "this operation does not take a capability in v1")
    term = _resource_term(resource)
    if type(namespace) is not str:
        raise _refuse(DENIED, "the namespace a capability is asked about is not a string")
    now = _whole_seconds(now)
    caller = _canonical_uuid(caller_peer_id)
    verified = _verify(token, store, now)
    chain = _walk(conn, verified, caller, now)
    _liveness(conn, verified, chain, namespace, caller, now)
    _authorize(verified, caller, namespace, term, action, operation, now)
    return {"via": "capability", "cap_id": verified.authority_id, "issuer": verified.issuer,
            "holder_chain": list(chain.peers), "hops": chain.hops}


# --------------------------------------------------------------------------- the view's pre-check

def precheck(conn, principal, caller_peer_id, now=None) -> None:
    """3.2 steps 1 and 3, run by the envelope path's views BEFORE `start_call` (design 3.2,
    "Where each refusal surfaces", critique C10; slice 6): size and parse under the kid
    provider (every signature), then the holder walk (every delegation signed by a live key of
    the peer holding the token there, the caller the final holder). A refusal here is the
    view's HTTP 403 with its code, E_CAPABILITY_INVALID, E_CAPABILITY_HOLDER, or
    E_CAPABILITY_DENIED for a hop past its expiry (decision 1), and no call starts: no
    skill_calls row, no nonce checked or burned. Steps 4 and 5 (liveness, the per-object
    decision) need the request's namespace and key, which only the handler reads from the
    signed args; they run in `check_permission` and surface as a signed failed result.

    Returns nothing: the view keeps only the parsed result, in the cache `decide` reads again
    (step 2), and passes the handler the `PresentedCapability`. Run under server_internal, as
    the walk reads peer_keys."""
    if not isinstance(principal, PresentedCapability):
        raise TypeError("principal must be a PresentedCapability")
    store = capability_keys.current()
    if store is None:
        raise _refuse(UNAVAILABLE, "capability tokens are not available on this server",
                      principal.sha256)
    now = _whole_seconds(now or _dt.datetime.now(_dt.timezone.utc))
    verified = _verify(principal.token, store, now)
    _walk(conn, verified, _canonical_uuid(caller_peer_id), now)


# --------------------------------------------------------------------------- the revoke route

def presented(token, store, now: _dt.datetime):
    """A token presented to the revoke route (slice 5), read as 3.2 step 1 reads it: at most
    8 KiB, parsed under the kid provider, every signature checked, its signed blocks read (the
    same cache). Raises E_CAPABILITY_INVALID. No holder walk and no liveness: the revoker need
    not hold the token, and a dead token may still be revoked by its issuer or a live admin (the
    route ends a signer's standing at the token's expiry). The result's `authority_id`,
    `revocation_ids`, `issuer`, `subject` and `blocks` are what the route reads."""
    return _verify(token, store, _whole_seconds(now))


def blocks_signed_by(conn, verified, caller_peer_id, now: _dt.datetime) -> list[int]:
    """The indexes of the delegation blocks of `verified` that the caller SIGNED (design 2.5
    (a): a signer revokes its own block and those after it). Two facts, never one: the block's
    external key is a LIVE key of the caller (fetch_caller_pubkey's grace rule, as the walk
    reads it), AND the caller held the token at that block, being the subject for the first
    delegation or the delegate the previous one named. The key alone would let a peer that
    registered another's public key revoke that peer's blocks; the position alone would name
    as signer the holder whose key did not sign the block. Run under server_internal."""
    caller = _canonical_uuid(caller_peer_id)
    if caller is None or not verified.hops:
        return []
    keys = {_decoded(text) for _peer, text in _live_keys(conn, [caller], _whole_seconds(now))}
    holder, signed = verified.subject, []
    for hop in verified.hops:
        if holder == caller and hop.signer in keys:
            signed.append(hop.index)
        holder = hop.delegate
    return signed


def decide(conn, *, principal, caller_peer_id, namespace, resource, need, now=None) -> dict:
    """check_permission's principal branch (3.3, 4.2): the token decides, and a refusal is
    final. Only read and write are accepted; the operation must be one 4.3 lists, need its
    action and touch its shape of resource. Anything else is E_CAPABILITY_NOT_ACCEPTED."""
    if not isinstance(principal, PresentedCapability):
        raise TypeError("principal must be a PresentedCapability")
    if capability_keys.current() is None:
        raise _refuse(UNAVAILABLE, "capability tokens are not available on this server",
                      principal.sha256)
    if need not in ACTIONS:
        raise _refuse(NOT_ACCEPTED, "a request that needs admin never takes a capability",
                      principal.sha256)
    op = principal.operation
    if OPERATION_ACTION.get(op) != need or type(resource) is not OPERATION_RESOURCE.get(op):
        raise _refuse(NOT_ACCEPTED, "this operation does not take a capability for this "
                                    "request in v1", principal.sha256)
    return authorize_capability(
        conn, token=principal.token, caller_peer_id=caller_peer_id, namespace=namespace,
        resource=resource, action=need, operation=op,
        now=now or _dt.datetime.now(_dt.timezone.utc))


__all__ = ["CLAUSE_MESSAGES", "LIVENESS_SQL", "MAX_BLOCKS", "MAX_HOPS", "MAX_HOP_SECONDS",
           "MAX_TOKEN_BYTES", "PresentedCapability", "authorize_capability", "blocks_signed_by",
           "clear_cache", "decide", "precheck", "presented", "run_limit_denials"]
