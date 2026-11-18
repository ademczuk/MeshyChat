"""The six memory.* skills (plus namespace.claim).

TRANSACTIONS. The connection runs as `meshychat_app` under FORCE RLS, so each handler body
sits inside ONE `rls.server_internal(conn)` block. That context manager sets the flag the
three memory-table policies compare against and owns commit/rollback, keeping each handler
in one transaction. See db.py for the measured policies.

`primitives.jcs` uses RFC 8785 canonical JSON; see the validation.py module docstring.

INTERFACE
---------
    handle_x(verified_payload, caller_peer_id, conn, *, principal=None) -> dict

  * `verified_payload` is `{payload, signature, signing_kid, verified_at?}`, the
    VERBATIM envelope plus a verification timestamp.
  * `caller_peer_id` is the AUTHENTICATED peer. It is authoritative; a client
    scalar naming a peer or an owner is NEVER trusted (MUST-2).
  * a domain error is `HandlerError(code, message)`; anything else is an internal
    fault that maps to E_INTERNAL. The namespace-claim plane trigger propagates as a raw
    psycopg.errors.RaiseException - see handle_namespace_claim.
  * `principal` (docs/DIVERGENCES.md 040, slice 6; design B07 3.3, 4.2, 4.3) is the OBJECT
    capability the call presented (`capability.PresentedCapability`), or None. None is
    today's behaviour, byte for byte. Present, it is the WHOLE authority for the request:
    memory.write (not a pre_discipline one), memory.read, memory.query and memory.list with
    a namespace pass it to `check_permission` (sites 2 to 5), whose refusal never falls
    through to the caller's own grants; memory.grant, memory.prune, namespace.claim, a
    pre_discipline write and memory.list with no namespace refuse it
    (E_CAPABILITY_NOT_ACCEPTED). When a token decided, the result carries `authz`, the
    decision record, and a write's provenance carries `capability`, the same record (Q6).
    The token itself is never in a result, a provenance or a message.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

import psycopg
import psycopg.errors
from psycopg.rows import dict_row

from apps.calls.envelope import KIND_SKILL_CALL
from apps.cards import validation as card_rules
from apps.context import validation as context_rules
from apps.core import mesh_unversioned, rls
from apps.core.env import lookup
from apps.wsgw.revocation import notify as notify_revocation

from . import legacy_verdict
from .errors import E_CAPABILITY_NOT_ACCEPTED, HandlerError
from .permissions import (
    NAMESPACE_WIDE,
    Key,
    Prefix,
    answers_admitting,
    OWNER,
    audit_tenancy_denial,
    check_permission,
    derive_owner_id,
    fetch_caller_pubkey,
    is_dev_plane,
    namespace_owner_id,
    same_tenant_or_devgroup,
)
from .signed_bytes import check_arg_types, refuse_unsafe_numbers
from .validation import (
    ABSTRACT_CONTENT_FIELDS,
    ALL_STATUSES,
    CLOSURE_STATUSES,
    MESH_CHAT_NAMESPACE,
    NEW_KEY_FORBIDDEN_STATUSES,
    PERM_LEVEL,
    RETENTION_EPHEMERAL,
    RETENTION_HIGH_CHURN,
    RETENTION_KEEP_ALL,
    STATUS_PROPOSED_FIX,
    abstract_of,
    args_of,
    claims_mesh_sender,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    fields_mode_of,
    is_mesh_message,
    norm_tags,
    now_iso,
    payload_of,
    require,
    structure_of,
    validate_mesh_key_sender,
    validate_mesh_key_ts,
    validate_mesh_message_v1,
    validate_operator_words,
    validate_directive,
    validate_urgent,
    URGENT_RATE_PER_HOUR,
    validate_window_end,
    is_roster_seat,
    seat_directives_enabled,
    DIRECTIVE_RATE_PER_HOUR,
    DIRECTIVE_KEY_SKEW_MS,
    check_directive_key_time,
    check_directive_not_expired,
    check_window_not_past,
    validate_retention_policy,
    validate_status,
)

# --------------------------------------------------------------------------- #
# Bootstrap authority (MUST-1, anti-squat)
# --------------------------------------------------------------------------- #
# There is NO hardcoded peer-id default. An unset authority fails CLOSED - no caller can
# claim a namespace - which prevents squatting.
BOOTSTRAP_AUTHORITY_PEER_ID = lookup("MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID", "").strip()


def set_bootstrap_authority(peer_id) -> None:
    """Single source of truth for MUST-1, set at startup."""
    global BOOTSTRAP_AUTHORITY_PEER_ID
    BOOTSTRAP_AUTHORITY_PEER_ID = str(peer_id or "").strip()


# --------------------------------------------------------------------------- #
# Fleet roster labels (attribution, never invention)
# --------------------------------------------------------------------------- #
# FAILS OPEN by design: with no roster the cache is empty, author_label falls back to the
# peer-id prefix, and the #775 authenticated
# self-confirm rule becomes inapplicable rather than violated. Priming from bytes
# already read (rather than a path) is what closes the window in which an edit to
# the roster between boot and the first write silently changes attribution.
_ROSTER_LABELS: dict[str, str] = {}


def prime_roster_labels(raw: bytes) -> int:
    """Fill the roster cache from bytes already read; return the count. Absent or
    unparseable bytes leave it empty - this must never become a way for startup to
    fail."""
    global _ROSTER_LABELS
    try:
        peers = json.loads(raw.decode("utf-8")).get("peers") or {}
    except Exception:
        peers = {}
    _ROSTER_LABELS = {str(k): str(v) for k, v in peers.items()} if isinstance(peers, dict) else {}
    return len(_ROSTER_LABELS)


def set_roster_labels(labels: dict[str, str]) -> int:
    """Replace the roster cache with a map ALREADY validated by apps/memoryskills/roster.py,
    the one the served app loads at startup (divergence 034); return the count. Unlike
    prime_roster_labels (fail-open), nothing here can be malformed."""
    global _ROSTER_LABELS
    _ROSTER_LABELS = {str(k): str(v) for k, v in labels.items()}
    return len(_ROSTER_LABELS)


def roster_label_of(peer_id):
    label = _ROSTER_LABELS.get(str(peer_id))
    return str(label) if label else None


def roster_labels() -> dict[str, str]:
    """A copy of the loaded roster, peer id to label; empty with no roster (divergence 081:
    the fleet backlog counts every roster peer)."""
    return dict(_ROSTER_LABELS)


# --------------------------------------------------------------------------- #
# shared/wiki confirmer discipline (#693 / #775)
# --------------------------------------------------------------------------- #

def _norm_seat(label):
    """Seat label for provenance comparison: case- and suffix-insensitive. A '-cc'
    seat is the SAME provenance, so it is stripped."""
    seat = str(label or "").strip().lower()
    while seat.endswith("-cc"):
        seat = seat[:-3]
    return seat


def _confirmer_seats(value):
    """confirmed_by normalised to a list. Accepts a scalar or a list because BOTH
    shapes are live in the corpus."""
    if not isinstance(value, dict):
        return []
    raw = value.get("confirmed_by")
    items = raw if isinstance(raw, list) else [raw]
    return [s for s in (_norm_seat(x) for x in items) if s]


def _chain_root_author(conn, namespace, key):
    """author_label of version 1, or "". A confirm is performed by writing a NEW
    version, so the HEAD's author_label is the confirmer, not the author - version 1
    is the author's own write. This is what makes the self-confirm rule enforceable
    on heads carrying no value.author."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT author_label FROM shared_memory "
            "WHERE namespace = %s AND key = %s ORDER BY version ASC LIMIT 1",
            (namespace, key),
        )
        row = cur.fetchone()
    return _norm_seat(row[0]) if row else ""


def _wiki_author_of(value, conn, namespace, key, head_status):
    """(author_seat, source). Declared value.author wins; otherwise version 1's
    author_label, but only when a head exists - a brand-new key has no v1 yet.
    Shared by both rules so they decide "who is the author" identically."""
    author = _norm_seat(value.get("author") if isinstance(value, dict) else None)
    source = "value.author"
    if not author and conn is not None and key is not None and head_status is not None:
        author = _chain_root_author(conn, namespace, key)
        source = "version 1 author_label"
    return author, source


def _enforce_wiki_confirmer(namespace, pre_discipline, status, value, head_status,
                            conn=None, key=None):
    """#693. The two rules are scoped DIFFERENTLY and
    that asymmetry is the whole design:

      * NO CONFIRMER fires only on the TRANSITION into CONFIRMED. Live heads exist
        with no confirmed_by at any version; enforcing on every write would make
        them uneditable by anyone, punishing the next editor for an old defect.
      * SELF-CONFIRM fires on EVERY CONFIRMED write, transition or not, because a
        head naming its own author as confirmer is the exact condition #693 forbids
        and carrying it forward re-asserts it.

    A missing value.author makes rule 2 INAPPLICABLE, not violated.
    """
    if namespace != "shared/wiki" or pre_discipline or status != "CONFIRMED":
        return
    confirmers = _confirmer_seats(value)
    if not confirmers and head_status != "CONFIRMED":
        raise HandlerError(
            "E_WIKI_NO_CONFIRMER",
            "cannot set CONFIRMED on shared/wiki without value.confirmed_by: "
            "CONFIRMED asserts that a differing-provenance peer checked this entry, "
            "and an absent confirmer records no such check (#693)",
        )
    author, source = _wiki_author_of(value, conn, namespace, key, head_status)
    if author and author in confirmers:
        raise HandlerError(
            "E_WIKI_SELF_CONFIRM",
            f"value.confirmed_by names the entry's own author ({author!r}, read from "
            f"{source}); CONFIRMED requires a confirmer of differing provenance, and a "
            "'-cc' seat is the same provenance. Write this entry OPEN and let a "
            "different seat confirm it (#693)",
        )


def _enforce_wiki_confirmer_authenticated(namespace, pre_discipline, status, value,
                                          head_status, caller_peer_id, conn=None, key=None):
    """#775. #693 compares two client-TYPED strings, so a
    writer that types a different confirmed_by launders its own confirmation past it.
    This keys on the SIGNER instead.

    Deliberately narrow, and each edge is a measurement, not a preference:
      * the comparison is the peer's ROSTER LABEL, not peer equality - one peer
        hosts several seats, so peer equality would refuse honest cross-seat
        confirmations;
      * TRANSITION only, because editing an already-CONFIRMED entry is not the act
        of confirming;
      * a caller with NO roster label fails OPEN - there is nothing authenticated to
        compare, and refusing would punish a peer for a roster gap it did not cause.
    """
    if namespace != "shared/wiki" or pre_discipline or status != "CONFIRMED":
        return
    if head_status == "CONFIRMED":
        return
    caller = _norm_seat(roster_label_of(caller_peer_id))
    if not caller:
        return
    author, source = _wiki_author_of(value, conn, namespace, key, head_status)
    if author and author == caller:
        raise HandlerError(
            "E_WIKI_SELF_CONFIRM",
            f"the authenticated caller ({caller!r}, the fleet roster label of the peer "
            f"that signed this write) is this entry's own author (read from {source}); "
            "value.confirmed_by is not consulted because a typed name is not the "
            "signer. CONFIRMED requires a confirmer of differing provenance: write "
            "this entry OPEN and let a different seat confirm it (#775)",
        )


# --------------------------------------------------------------------------- #
# Mesh message authorship (divergence 028)
# --------------------------------------------------------------------------- #
# Before these rules, the shared key-value semantics let any writer append a version of any
# key and name any author, and only created_by_peer_id was
# bound to the signer. Measured on the live test server on 2026-09-23: a node replaced
# another node's message (version 2 became the head, body "TAMPERED ..."), stamped it with
# the victim's label as author_label, and a remote node sent as another node's address in
# value.from and in the key. Search, context, inbox and the graph's authored_by edge all
# read those fields, so readers saw the forgery.
#
# SCOPE: shared/mesh-chat, keys under msg/ (validation.is_mesh_message), for all five
# rules; plus, for R2 and R3 only, a msg/ row in ANY namespace that names a sender
# (validation.claims_mesh_sender), because /v1/mesh/wait reports that `from` from every
# readable namespace. shared/wiki's CONFIRM is a different peer writing a new version of
# someone else's entry, and every other namespace and key keeps the plain shared key-value
# semantics: any key, any author, any writer.
#
# "THE CALLER'S LABELS" is apps.mesh.views.labels_for, the set the graph routes answer a
# caller for (roster label, peers.name, peer-<id8>), less one shape: a label that LOOKS
# like the server's own fallback for another peer. One function decides who a peer is
# on both sides of the mesh; imported at call time because apps.mesh.views imports this
# module at load. The served app loads a roster only when MESHY_FLEET_ROSTER_PATH is set
# (divergence 034, apps/memoryskills/roster.py); without one the set is peers.name and
# peer-<id8>.

#: The label handle_write stamps for a peer with no roster label: `peer-` and the first
#: eight characters of its id, a lower-case uuid string. `fallback_label` writes it and this
#: recognises it; roster.py refuses a roster label of the shape for another peer.
FALLBACK_LABEL_RE = re.compile(r"^peer-[0-9a-f]{8}$")


def fallback_label(peer_id) -> str:
    """The server's own name for a peer with no roster label (FALLBACK_LABEL_RE's shape)."""
    return f"peer-{str(peer_id)[:8]}"


def stamped_label(peer_id) -> str:
    """The author_label a row gets when the caller supplied none: the roster label first;
    the peer-id prefix is the honest fallback for an unrostered peer (attribution, never
    invention). memory.write's default and memory.edit's editor are this one label."""
    return roster_label_of(peer_id) or fallback_label(peer_id)


def _caller_labels(conn, caller_peer_id) -> list[str]:
    """labels_for's labels, WITHOUT a roster label or peers.name shaped like another peer's
    fallback. `peer-<id8>` is the server's own name for exactly one peer; a free-text name
    of that shape (peers.name has no UNIQUE constraint, and bootstrap_owner --peer-name
    takes anything) sent measurement 2's exact forgery through R1 to R3 (review of 7084e88,
    probe b3). The caller's own fallback is always kept."""
    from apps.mesh.views import labels_for
    own = fallback_label(caller_peer_id)
    return [x["label"] for x in labels_for(conn, caller_peer_id)
            if x["label"] == own or not FALLBACK_LABEL_RE.match(x["label"])]


def _enforce_mesh_sender(conn, args, value, caller_peer_id, *, check_author: bool) -> None:
    """R1 and R2. A supplied author (either spelling handle_write reads) and value.from
    must each be one of the caller's labels. An ABSENT author is not refused: the
    caller-derived default in handle_write stands. R1 runs for a mesh message only
    (`check_author`): outside shared/mesh-chat the author is a free-form label,
    which shared/wiki's CONFIRM relies on. R3, the key's embedded label, is
    validation.validate_mesh_key_sender, which needs no database and runs first."""
    labels = _caller_labels(conn, caller_peer_id)
    for name in (("author", "author_label") if check_author else ()):
        supplied = args.get(name)
        # Truthiness, as handle_write's own `args.get("author") or args.get("author_label")`:
        # a falsy spelling is not the one it stores, and the other one is checked.
        if supplied and supplied not in labels:
            raise HandlerError(
                "E_AUTHOR_MISMATCH",
                f"args.{name} {supplied!r} is not one of the caller's labels {labels}: a "
                "mesh message is attributed to the peer that signed it. Omit it for the "
                "default, or name one of those labels (divergence 028)")
    sender = value.get("from") if isinstance(value, dict) else None
    if sender not in labels:
        raise HandlerError(
            "E_FROM_MISMATCH",
            f"value.from {sender!r} is not one of the caller's labels {labels}: a node "
            "sends as itself (divergence 028)")


def _enforce_single_writer(conn, namespace, key, caller_peer_id) -> None:
    """R4. A later version of an existing mesh message is written only by the peer that
    created its version 1. Called with the head already found, BEFORE the head is retired,
    so a refusal writes nothing.

    "Version 1" is the chain's lowest surviving version. memory.prune never deletes it for
    a mesh message (handle_prune), so on any chain this rule has seen it IS version 1, and
    while this rule has held every version of a message has the same creator. A chain
    written before it can carry a second writer; its version-1 author still decides, and
    the chain is not rewritten. sql_meshychat/011 holds the same rule in the database for a
    second writer APPENDING by any path that does not come through here."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_by_peer_id, version FROM shared_memory "
            "WHERE namespace = %s AND key = %s ORDER BY version ASC LIMIT 1",
            (namespace, key),
        )
        root = cur.fetchone()
    if root is not None and uuid.UUID(str(root[0])) != uuid.UUID(str(caller_peer_id)):
        raise HandlerError(
            "E_NOT_MESSAGE_AUTHOR",
            f"{namespace}/{key} was created (version {root[1]}) by peer {root[0]}; only that "
            "peer may write a later version of a mesh message. Send a new message with "
            "in_reply_to naming this key instead (divergence 028); a seat of the owner that wrote it "
            "edits it with memory.edit (divergence 059)")


# --------------------------------------------------------------------------- #
# Row shaping
# --------------------------------------------------------------------------- #

def _memo_cols(*, include_provenance=False):
    cols = ("id, namespace, key, version, is_head, parent_id, value, status, tags, "
            "author_label, pre_discipline, claimed_status, "
            "created_by_peer_id, created_by_owner_id, created_at")
    if include_provenance:
        # Divergence 039 (design Q11): the bytes the writer signed, and for a row without
        # them its current legacy verdict. Both queries select FROM shared_memory unaliased,
        # which the verdict subquery correlates on.
        cols += ", provenance_jsonb, signed_bytes, " + legacy_verdict.VERDICT_COLUMN_SQL
    return cols


def _row_to_memo(row, *, include_provenance=False):
    """Build the response representation for one memory row."""
    memo = {
        "id": str(row["id"]),
        "namespace": row["namespace"],
        "key": row["key"],
        "version": int(row["version"]),
        "is_head": row["is_head"],
        "parent_id": str(row["parent_id"]) if row.get("parent_id") else None,
        "value": row["value"],
        "status": row["status"],
        "tags": list(row["tags"]) if row.get("tags") is not None else [],
        "author_label": row["author_label"],
        "pre_discipline": row["pre_discipline"],
        "claimed_status": row["claimed_status"],
        "created_by_peer_id": str(row["created_by_peer_id"]),
        "created_by_owner_id": str(row["created_by_owner_id"]),
        "created_at": str(row["created_at"]),
    }
    if include_provenance:
        memo["provenance"] = row.get("provenance_jsonb")
        memo.update(legacy_verdict.status_basis(row.get("signed_bytes"),
                                                row.get("legacy_verdict"), memo["provenance"]))
    return memo


# --------------------------------------------------------------------------- #
# Object capabilities (divergence 040, slice 6)
# --------------------------------------------------------------------------- #

def _refuse_capability(principal, what: str, why: str) -> None:
    """Design 4.3: "A token presented where the table says no is E_CAPABILITY_NOT_ACCEPTED,
    never silently ignored." A no-op without a principal."""
    if principal is not None:
        raise HandlerError(E_CAPABILITY_NOT_ACCEPTED,
                           f"{what} never takes a capability: {why} (design B07 4.3)")


def _with_authz(result: dict, decision) -> dict:
    """Q6: the decision record as `authz`, ONLY when a token decided (`decision` is not None);
    every other result is returned exactly as it was built."""
    if decision is not None:
        result["authz"] = decision
    return result


# --------------------------------------------------------------------------- #
# namespace.claim - MUST-1
# --------------------------------------------------------------------------- #

def handle_namespace_claim(verified_payload, caller_peer_id, conn, *, principal=None):
    """Claim a namespace. Only the configured bootstrap authority may claim.

    Idempotent: re-claiming with the same owner is a no-op returning the EXISTING policy
    (retention_policy is set once at claim and
    frozen thereafter by shared_memory_namespace_freeze_retention_policy_trg);
    claiming one already owned by a DIFFERENT peer is E_NAMESPACE_ALREADY_CLAIMED.

    THE PLANE RULE IS THE DATABASE'S, NOT THIS FUNCTION'S. Trigger
    `ns_dev_only_trigger` runs `enforce_ns_dev_only()` BEFORE INSERT OR UPDATE and
    raises unless the owner's dashboard_users.plane is 'dev'. Read from the live
    function body: the check has NO namespace-prefix condition despite its message
    naming shared/*, so a product-plane owner cannot claim ANY namespace, not just a
    shared/* one. That exception surfaces here as psycopg.errors.RaiseException and
    is deliberately NOT wrapped: the core contract is that a non-HandlerError is an
    internal fault. Inventing an error code here would invent a contract the wire does
    not have.

    A capability is refused (design 4.3): a claim is bootstrap authority (MUST-1).
    """
    _refuse_capability(principal, "namespace.claim", "a claim is bootstrap authority")
    payload = payload_of(verified_payload)
    args = args_of(payload)
    namespace = require(args, "namespace")

    # An UNSET authority must refuse outright rather than compare against "". The string
    # comparison alone reads as fail-closed only because a real peer id is never empty -
    # a property of the callers, not of this check. State the precondition.
    if not BOOTSTRAP_AUTHORITY_PEER_ID:
        raise HandlerError(
            "E_NOT_BOOTSTRAP_AUTHORITY",
            "no bootstrap authority is configured; namespace claims are refused",
        )
    if str(caller_peer_id) != str(BOOTSTRAP_AUTHORITY_PEER_ID):
        raise HandlerError(
            "E_NOT_BOOTSTRAP_AUTHORITY",
            "only the configured bootstrap-authority peer may claim namespaces",
        )

    owner_peer_id = str(args.get("owner_peer_id") or caller_peer_id)
    retention_policy = validate_retention_policy(args.get("retention_policy"))

    with rls.server_internal(conn):
        owner_owner_id = derive_owner_id(conn, owner_peer_id)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shared_memory_namespace "
                "(namespace, owner_peer_id, owner_owner_id, retention_policy) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (namespace) DO NOTHING "
                "RETURNING namespace, owner_peer_id, owner_owner_id, retention_policy, created_at",
                (namespace, owner_peer_id, owner_owner_id, retention_policy),
            )
            row = cur.fetchone()
            idempotent = False
            if row is None:
                cur.execute(
                    "SELECT namespace, owner_peer_id, owner_owner_id, retention_policy, "
                    "created_at FROM shared_memory_namespace WHERE namespace = %s",
                    (namespace,),
                )
                row = cur.fetchone()
                if str(row[1]) != owner_peer_id:
                    raise HandlerError(
                        "E_NAMESPACE_ALREADY_CLAIMED",
                        f"namespace {namespace!r} already claimed by {row[1]}",
                    )
                idempotent = True

    return {
        "op": "namespace.claim",
        "namespace": row[0],
        "owner_peer_id": str(row[1]),
        "owner_owner_id": str(row[2]),
        "retention_policy": str(row[3]),
        # Back-compat boolean derived from the policy, for callers that still read
        # the mig-045 flag.
        "ephemeral": str(row[3]) == RETENTION_EPHEMERAL,
        "created_at": str(row[4]),
        "idempotent": idempotent,
    }


# --------------------------------------------------------------------------- #
# memory.grant
# --------------------------------------------------------------------------- #

def handle_grant(verified_payload, caller_peer_id, conn, *, principal=None):
    """Grant or revoke a namespace permission.

    Only a namespace ADMIN may call this. One ACTIVE grant per
    (namespace, grantee) is an invariant enforced in two places that must agree:
    this handler retires the existing active grant before inserting, and the partial
    unique index `shared_memory_grant_active_unique (namespace, grantee_peer_id)
    WHERE revoked = false` refuses a second one outright.

    A capability is refused (design 4.3): a token must never mint DURABLE authority, and a
    300 s delegate turning itself into a permanent grant row is an escalation.
    """
    _refuse_capability(principal, "memory.grant", "a token must never mint durable authority")
    payload = payload_of(verified_payload)
    args = args_of(payload)
    namespace = require(args, "namespace")
    grantee_peer_id = str(require(args, "grantee_peer_id"))
    revoke = bool(args.get("revoke", False))

    ctx = {"granted_by_owner_id": None, "grantee_owner_id": None}
    try:
        with rls.server_internal(conn):
            check_permission(namespace, caller_peer_id, "admin", conn,
                             resource=NAMESPACE_WIDE)

            if revoke:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shared_memory_grant "
                        "SET revoked = TRUE, revoked_at = NOW(), updated_at = NOW() "
                        "WHERE namespace = %s AND grantee_peer_id = %s AND revoked = FALSE",
                        (namespace, grantee_peer_id),
                    )
                    revoked_count = cur.rowcount
                if revoked_count:
                    # STATUS B15: IN this transaction, so the grantee's open sockets are
                    # re-judged on commit and never for a revoke that rolled back. The
                    # bridge decides from the tables, not from this payload.
                    notify_revocation(conn, grantee_peer_id, namespace=namespace)
                return {
                    "op": "memory.grant",
                    "namespace": namespace,
                    "grantee_peer_id": grantee_peer_id,
                    "revoked": revoked_count,
                }

            permission = require(args, "permission")
            if permission not in PERM_LEVEL:
                raise HandlerError(
                    "E_BAD_REQUEST", f"permission must be one of {sorted(PERM_LEVEL)}")
            expires_at = args.get("expires_at")
            if expires_at is not None:
                # Validated UP FRONT so garbage is E_BAD_REQUEST rather than an
                # E_INTERNAL from the TIMESTAMPTZ cast, and a PAST date is a loud
                # error rather than a silent born-dead grant. That second half is
                # load-bearing: a past expires_at would otherwise satisfy the I1
                # cross-subtree bound below while granting nothing.
                try:
                    _exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    raise HandlerError("E_BAD_REQUEST", "expires_at must be an ISO-8601 timestamp")
                if _exp.tzinfo is None:
                    _exp = _exp.replace(tzinfo=timezone.utc)
                if _exp <= datetime.now(timezone.utc):
                    raise HandlerError("E_BAD_REQUEST", "expires_at must be strictly in the future")

            grantee_owner_id = ctx["grantee_owner_id"] = derive_owner_id(conn, grantee_peer_id)
            granted_by_owner_id = ctx["granted_by_owner_id"] = derive_owner_id(conn, caller_peer_id)
            ns_owner = namespace_owner_id(conn, namespace)

            # I2: no product tenant holds a shared/* grant. Asserted with the DIRECT
            # plane read - see permissions.is_dev_plane for why the keystone cannot
            # answer this on a self-owned namespace.
            if namespace.startswith("shared/") and not is_dev_plane(conn, grantee_owner_id):
                raise HandlerError(
                    "E_PRODUCT_NO_SHARED_GRANT",
                    f"only dev-plane owners may be granted on a shared/* namespace "
                    f"({namespace!r})",
                )
            # I1: a cross-subtree grant must be BOUNDED. Within one tenant or dev
            # group it is unrestricted; revocability is structural (the revoked column).
            if not same_tenant_or_devgroup(conn, grantee_owner_id, ns_owner) and not expires_at:
                raise HandlerError(
                    "E_UNBOUNDED_CROSS_GRANT",
                    "a cross-subtree grant must carry a non-NULL expires_at (I1)",
                )

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE shared_memory_grant "
                    "SET revoked = TRUE, revoked_at = NOW(), updated_at = NOW() "
                    "WHERE namespace = %s AND grantee_peer_id = %s AND revoked = FALSE",
                    (namespace, grantee_peer_id),
                )
                cur.execute(
                    "INSERT INTO shared_memory_grant "
                    "(namespace, grantee_peer_id, grantee_owner_id, permission, "
                    " granted_by_peer_id, granted_by_owner_id, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz) "
                    "RETURNING id, namespace, grantee_peer_id, permission, expires_at, created_at",
                    (namespace, grantee_peer_id, grantee_owner_id, permission,
                     str(caller_peer_id), granted_by_owner_id, expires_at),
                )
                row = cur.fetchone()
    except HandlerError as e:
        # The transaction is already rolled back by the context manager, so the audit
        # insert runs as its own committed statement and SURVIVES the denial.
        if e.code == "E_PRODUCT_NO_SHARED_GRANT":
            audit_tenancy_denial(
                conn, e.code, ctx["granted_by_owner_id"], caller_peer_id, namespace,
                detail="product-plane grantee denied shared/* grant (I2)",
                target_owner_id=ctx["grantee_owner_id"],
            )
        raise

    return {
        "op": "memory.grant",
        "grant_id": str(row[0]),
        "namespace": row[1],
        "grantee_peer_id": str(row[2]),
        "permission": row[3],
        "expires_at": str(row[4]) if row[4] is not None else None,
        "created_at": str(row[5]),
    }


# --------------------------------------------------------------------------- #
# memory.write
# --------------------------------------------------------------------------- #

def _proposed_fix_owners(conn, namespace, key):
    """Every owner that authored a PROPOSED_FIX anywhere on the chain.

    ALL proposals, not just the most recent: taking only the latest lets a later
    different-owner proposal launder an earlier
    same-owner proposer's self-close.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT created_by_owner_id FROM shared_memory "
            "WHERE namespace = %s AND key = %s AND status = %s",
            (namespace, key, STATUS_PROPOSED_FIX),
        )
        rows = cur.fetchall()
    return {str(r[0]) for r in rows}


def _existing_by_dedup(conn, jcs_sha, call_id):
    """A row already written with this payload hash or call_id (replay no-op).

    `provenance_call_id` is a GENERATED column
    (confirmed against information_schema: is_generated=ALWAYS, expression
    `provenance_jsonb -> 'payload' ->> 'call_id'`) carrying a UNIQUE index, so the
    replay key is derived from the SIGNED envelope and cannot be forged by a client
    scalar.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, namespace, key, version, is_head, status, pre_discipline, "
            "       claimed_status, author_label, tags, created_at, created_by_peer_id, "
            "       payload_jcs_sha256, (value ? 'directive') AS stored_directive "
            "FROM shared_memory "
            "WHERE payload_jcs_sha256 = %s OR provenance_call_id = %s LIMIT 1",
            (jcs_sha, call_id),
        )
        return cur.fetchone()


def _namespace_retention_policy(conn, namespace):
    """keep_all / high_churn / ephemeral; keep_all is the SAFE default when the
    namespace is unclaimed or the column is absent.

    The savepoint is not defensive decoration. A running service can start before a
    migration lands, and an UndefinedColumn would otherwise abort the caller's whole
    transaction and take EVERY write down. A nested transaction context inside an
    open transaction is a SAVEPOINT.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT retention_policy FROM shared_memory_namespace WHERE namespace = %s",
                    (namespace,),
                )
                row = cur.fetchone()
        return row[0] if (row and row[0]) else RETENTION_KEEP_ALL
    except psycopg.errors.UndefinedColumn:
        return RETENTION_KEEP_ALL


def _replay_of_own_write(existing, caller_peer_id):
    """The replay answer, for the signer who wrote the row and no one else.

    Divergence 039 (adversarial review of fin/b02-a1, finding 4). The dedup finds a row by
    payload hash OR call_id in ANY namespace and must not answer it as the caller's replay
    when another signer wrote it.
    Since the replay result carries the row's status, custody, author label, tags and
    creation time, a signer who knew another's call_id read those fields of a row it could
    not read, and its own write silently did not land. A call_id another signer used is
    refused with E_REPLAY, a durable replay rejection, and
    the refusal names nothing of that row. The same signer re-sending is a replay, as
    before."""
    try:
        own = uuid.UUID(str(existing["created_by_peer_id"])) == uuid.UUID(str(caller_peer_id))
    except (ValueError, TypeError, AttributeError):
        own = False
    if not own:
        raise HandlerError(
            "E_REPLAY", "this call_id is already bound to a write by another signer; "
            "sign the write under a call_id of your own")
    return _replay_result(existing)


def _replay_result(existing):
    """What a replayed write answers: the stored row, in the first write's shape.

    Divergence 039 (design 1.3, G4, Q5). A replay includes the row's status, custody,
    author, and tags so a re-sent write (the board route's retry, or a concurrent duplicate)
    describes the row it replays. Over W1, the server signs this result, so its signature
    attests those fields. Every field is read from the row, never from the replayed args:
    the row is what was written."""
    return {
        "op": "memory.write",
        "namespace": existing["namespace"],
        "key": existing["key"],
        "version": int(existing["version"]),
        "is_head": existing["is_head"],
        "id": str(existing["id"]),
        "status": existing["status"],
        "pre_discipline": existing["pre_discipline"],
        "claimed_status": existing["claimed_status"],
        "author_label": existing["author_label"],
        "tags": list(existing["tags"] or []),
        "created_at": str(existing["created_at"]),
        "replay": True,
    }


def _replay_within(existing, namespace, key, caller_peer_id):
    """A dedup hit is the replay of THIS write only when it is the write's own (namespace, key).

    Divergence 040, slice 4 (design B07 5.2, the dedup row; critique C14). `_existing_by_dedup`
    finds a row by payload hash OR call_id in ANY namespace. A replay must not answer with
    that row's namespace, key, and version while the new write silently does not land. Under
    a key-scoped authority, that would answer with another key's row. A payload-hash hit is
    always the same (namespace, key), since the hash covers the
    args; a call_id reused for another write is refused E_REPLAY, naming nothing of the row it
    found. FOR EVERY WRITE, with a token or without: every write's authorised resource is its
    own Key(key) since slice 2, so there is one rule, not one per transport. Recorded and
    pinned in divergence 040 (tests/test_write_dedup_scope.py).

    Integrated on integ/r11 with divergence 039's signer rule, which runs first and unchanged:
    a call_id another signer used is refused by `_replay_of_own_write`, and only the signer's
    own row reaches the (namespace, key) comparison. Both refusals are E_REPLAY and name
    nothing of the row found."""
    result = _replay_of_own_write(existing, caller_peer_id)
    if (existing["namespace"], existing["key"]) != (namespace, key):
        raise HandlerError(
            "E_REPLAY",
            "this call_id was already used by a write to another key; a replay is answered "
            "only for the key it wrote")
    return result


def _signed_envelope(verified_payload, payload, conn, caller_peer_id):
    """The signed request a new row is written from, VERIFIED: (call_id, signature,
    signing_kid, verified_at, signed_bytes, payload_jcs_sha256). One body for memory.write
    and memory.edit (divergence 059), whose rows carry the same envelope; memory.redact's
    record needs no call_id or verified_at and keeps its own check (redact.py)."""
    call_id = payload.get("call_id")
    if not call_id:
        raise HandlerError("E_BAD_REQUEST", "payload.call_id missing")
    signature = verified_payload.get("signature")
    signing_kid = verified_payload.get("signing_kid")
    if not signature or not signing_kid:
        raise HandlerError(
            "E_BAD_REQUEST", "verified_payload missing signature/signing_kid for provenance")
    verified_at = verified_payload.get("verified_at") or now_iso()

    # VERIFY THE SIGNATURE. Until 2026-09-21 this path checked only that `signature` and
    # `signing_kid` were NON-EMPTY, fetched the caller's public key for provenance, and
    # never verified anything against it - then wrote provenance asserting the write WAS
    # verified. Any caller could supply an arbitrary signature string and the row would
    # carry a verification attestation that was false.
    #
    # This module is reachable in-process, so it verifies its input: an attestation
    # must never be written by code that did not check it.
    #
    # THE BYTES IT VERIFIED ARE THE BYTES STORED (divergence 039). `signed_bytes` is the
    # RFC 8785 serialisation verify_strict just accepted, so it IS the signed message: the
    # server never sees the client's own bytes, only the parsed body, and a signature that
    # verifies over the re-canonicalised bytes was made over those bytes. Its SHA-256 is
    # `payload_jcs_sha256`, the replay key, computed from the same bytes rather than a
    # second serialisation, and the database's agree-trigger (sql_meshychat/014) refuses
    # the row unless every derived column agrees with them.
    signed_bytes = _verify_provenance_signature(conn, caller_peer_id, signing_kid, payload,
                                                signature)
    jcs_sha = hashlib.sha256(signed_bytes).hexdigest()
    return call_id, signature, signing_kid, verified_at, signed_bytes, jcs_sha


def _provenance(payload, signature, signing_kid, caller_owner_id, jcs_sha, pubkey_b64url,
                pubkey_fp, verified_at, pre_discipline) -> dict:
    """A row's `provenance_jsonb`: the VERBATIM envelope plus the exact key bytes used to
    verify, in this key order, for memory.write and memory.edit alike. The key fetch stays
    in each handler, inside its transaction."""
    return {
        "payload": payload,
        "signature": signature,
        "signing_kid": signing_kid,
        "caller_owner_id": caller_owner_id,
        "payload_jcs_sha256": jcs_sha,
        "public_key_b64url": pubkey_b64url,
        "public_key_sha256": pubkey_fp,
        "verified_at": verified_at,
        "pre_discipline": pre_discipline,
    }


def _enforce_directive_rate(conn, caller_peer_id) -> None:
    """At most DIRECTIVE_RATE_PER_HOUR directives per signer in any rolling hour (the directive rule; pxx's review of plan v1,
    P2-4), counted from the stored rows, so a restart or a second worker cannot reset it. Called INSIDE the write's
    own server_internal transaction, after a transaction-scoped advisory lock on the signer: a second concurrent
    directive from the same signer waits for the first to commit or roll back, then counts it, so the last slot is
    taken once (codex-oxxxxxx-admin-cli ef2965, P2-4). The lock ends with the transaction."""
    # B5 (docs/V1-DESIGN-RULES.md; gate mgbatch6): never a per-request scan of the table. A mesh key is
    # msg/<epoch-ms>-..., so the last hour is one RANGE of the (namespace, key) indexes, and the count stops at
    # DIRECTIVE_RATE_PER_HOUR + 1 rows: it reads at most one hour of mesh messages, whatever the history.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Every directive's key is within DIRECTIVE_KEY_SKEW_MS of its write time (validate_directive), so a directive
    # written in the last hour has a key in [now - 1 h - skew, now + skew): the range misses none of them (pxx).
    lo, hi = f"msg/{now_ms - 3_600_000 - DIRECTIVE_KEY_SKEW_MS}", f"msg/{now_ms + DIRECTIVE_KEY_SKEW_MS}"
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1081))", ("directive-rate:" + str(caller_peer_id),))
        cur.execute(
            "SELECT count(*) AS n FROM (SELECT 1 FROM shared_memory WHERE namespace = %s AND key >= %s AND key < %s "
            "AND created_by_peer_id = %s::uuid AND created_at > now() - interval '1 hour' AND value ? 'directive' "
            "LIMIT %s) AS recent",
            (MESH_CHAT_NAMESPACE, lo, hi, str(caller_peer_id), DIRECTIVE_RATE_PER_HOUR + 1))
        row = cur.fetchone()
    n = int(row["n"] if isinstance(row, dict) else row[0])
    if n >= DIRECTIVE_RATE_PER_HOUR:
        raise HandlerError("E_RATE_LIMITED", f"{n} directives from this seat in the last hour; at most "
                                             f"{DIRECTIVE_RATE_PER_HOUR}. Wait, or ask the operator")


def _urgent_count(conn, caller_peer_id) -> int:
    """How many `urgent` messages this signer stored in the last hour, counted like _enforce_directive_rate: one key
    RANGE of the last hour (an urgent message's key is within DIRECTIVE_KEY_SKEW_MS of its write time,
    check_directive_key_time), after a transaction-scoped advisory lock on the signer, LIMIT n + 1.

    A REDACTED row of this signer in the hour counts too: its tombstone keeps no `urgent` (the immutability trigger
    allows only the routing keys, sql_meshychat/040), so without this, send-then-redact would refill the allowance
    (GitHub Codex on e70b2c21; pxx chose no migration). Conservative: a seat that redacts ordinary mail in the
    hour loses that much urgent allowance; it never gains any."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    lo, hi = f"msg/{now_ms - 3_600_000 - DIRECTIVE_KEY_SKEW_MS}", f"msg/{now_ms + DIRECTIVE_KEY_SKEW_MS}"
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1081))", ("urgent-rate:" + str(caller_peer_id),))
        cur.execute(
            "SELECT count(*) AS n FROM (SELECT 1 FROM shared_memory WHERE namespace = %s AND key >= %s AND key < %s "
            "AND created_by_peer_id = %s::uuid AND created_at > now() - interval '1 hour' "
            "AND (value ? 'urgent' OR value ? 'redacted') "
            "LIMIT %s) AS recent",
            (MESH_CHAT_NAMESPACE, lo, hi, str(caller_peer_id), URGENT_RATE_PER_HOUR + 1))
        row = cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def _enforce_urgent(conn, caller_peer_id, value) -> None:
    """Admission for the urgent lane. `urgent`: refused past URGENT_RATE_PER_HOUR in the last hour (E_RATE_LIMITED).
    `urgent_declined` is the SENDER's own label on an ordinary message ("I tried urgent and was over the allowance"):
    it is never refused for the count. Checking it against the count made a correct resend fail whenever a slot freed
    between the refusal and the resend, which dropped parked questions (GitHub Codex on cb0f3db4; pxx P3-1), and
    a false label gains a seat nothing: the message is delivered as ordinary either way."""
    if "urgent" not in value:
        return
    n = _urgent_count(conn, caller_peer_id)
    if "urgent" in value and n >= URGENT_RATE_PER_HOUR:
        raise HandlerError("E_RATE_LIMITED", f"{n} urgent messages from this seat in the last hour; at most "
                                             f"{URGENT_RATE_PER_HOUR}. Send it as ordinary (send --urgent does that "
                                             f"by default, marked urgent_declined)")


def handle_write(verified_payload, caller_peer_id, conn, *, principal=None):
    """Append a new signed version of (namespace, key).

    APPEND-ONLY IS ENFORCED IN TWO PLACES AND THEY MUST AGREE. This handler retires
    the prior head (`is_head` TRUE -> FALSE, the only legal UPDATE) and inserts
    version+1 linked by parent_id. The database independently refuses anything else:
    trigger `shared_memory_immutability` raises 'shared_memory rows are immutable;
    only head retirement (is_head TRUE->FALSE) is permitted' on any content or
    identity column change, and refuses is_head FALSE -> TRUE outright. Trigger
    `shared_memory_root_parent_trg` holds version=1 <=> parent_id IS NULL. An
    implementation that "optimised" this into an UPDATE would be refused by the database, which is
    the point.

    Gates, in the order they fire:
      (before the database: the mesh v1 shape, then divergence 028 R3, the key's
      embedded sender is value.from. The v1 shape is skipped for an object with NO
      schema_version while the transitional MESHY_MESH_ACCEPT_UNVERSIONED is on, and only
      it: divergence 011, 2026-09-27, counted once the row is written)
      default-deny authorisation (admin for a custody write, write otherwise)
      -> replay dedup -> live owner derivation (MUST-2) -> the I2 plane ban
      -> divergence 028 R1/R2 (a mesh message's author and from are the caller's labels)
      -> retention policy -> head lookup
      -> divergence 028 R4 (a later version of a mesh message is its author's)
      -> optimistic concurrency
      -> MUST-3 (a new key cannot be born PROPOSED_FIX/FIXED)
      -> MUST-7 (no closure directly off a pre_discipline head)
      -> MUST-2 closure independence -> the shared/wiki confirmer rules.

    With a capability (`principal`, divergence 040 slice 6) the default-deny step is the
    token's decision for `Key(key)`, and a pre_discipline custody write refuses it
    (E_CAPABILITY_NOT_ACCEPTED): a custody write is administrative (design 4.3). Every gate
    after it is unchanged: the plane ban reads the caller's own owner, and 028's R1 to R4
    bind a mesh message to its signer, so a token lets its holder write as itself only
    (design 4.2, "authorship is not delegable").
    """
    payload = payload_of(verified_payload)
    # Divergence 039, design Q4: a number no stored copy of this payload could be
    # re-canonicalised from is refused before anything else reads it.
    refuse_unsafe_numbers(payload)
    args = args_of(payload)
    # Divergence 039, design Q3: the args the database derives columns from must have the
    # JSON types its rules read. Refused here with a typed code; the agree-trigger refuses
    # the same rows (untyped) if a path ever skips this.
    check_arg_types(args)
    namespace = require(args, "namespace")
    key = require(args, "key")
    validate_mesh_key_ts(key)  # reject a non-epoch-ms mesh key AT THE WRITE SEAM
    value = args.get("value", args.get("body"))
    if value is None:
        raise HandlerError("E_BAD_REQUEST", "missing required arg 'value'")
    # Divergence 011, 2026-09-27: True when the transitional flag let an unversioned mesh
    # message past the v1 field rules. Every other gate below still runs.
    unversioned = validate_mesh_message_v1(
        namespace, key, value, accept_unversioned=mesh_unversioned.accept_from_settings())
    validate_mesh_key_sender(namespace, key, value)  # divergence 028 R3
    validate_operator_words(namespace, key, value, caller_peer_id)  # threat_model.md amendment, 28 Oct 2026
    validate_directive(namespace, key, value, caller_peer_id)       # the directive rule; threat_model.md amendment, 2 Nov 2026
    validate_urgent(namespace, key, value)                          # the urgent lane, kxxx / oxxxxxx
    validate_window_end(namespace, key, value)  # kxxx, a recorded decision Nov 2026
    # Divergence 091: a row of the card namespace is a card, checked in full (apps/cards).
    card = card_rules.validate_card(namespace, key, value)
    # docs/design/context-v1.md: a row of the context namespace is an item or a manifest, shape-checked here with NO
    # screens (they run in enforce_context, after the permission check, so no caller can probe the denylist).
    context = context_rules.validate_context(namespace, key, value)
    status = validate_status(args.get("status"))
    tags = norm_tags(args.get("tags"))
    author_label = args.get("author") or args.get("author_label")
    if context:
        # As for a card: no second status channel, and the author is the signer.
        if status is not None or args.get("claimed_status") is not None:
            raise HandlerError("E_CONTEXT_SCHEMA", "a context row takes no row status")
        # Nor tags: they are stored beside the value, outside the screens and the budget (a reviewer, #346 review).
        if args.get("tags") is not None:
            raise HandlerError("E_CONTEXT_SCHEMA", "a context row takes no tags")
        if author_label and author_label != stamped_label(caller_peer_id):
            raise HandlerError("E_CONTEXT_SEAT_MISMATCH", f"args.author {author_label!r} is not the caller's label: "
                                                          "a context row's author is its signer. Omit it")
    if card:
        # ONE status and ONE author for a card. The row-level `status` argument (OPEN, WONTFIX, ...) is
        # a second channel memory.query and memory.list would honour while /v1/cards derives another;
        # and a supplied author would let a card read as its seat's in /v1/cards and as someone else's
        # to every generic reader of the row. The author is stamped below from the signer, which is
        # the label the card's seat must equal.
        if status is not None or args.get("claimed_status") is not None:
            raise HandlerError("E_CARD_SCHEMA", "a card takes no row status: superseded is derived from the "
                                                "cards that name it (apps/cards/store.py)")
        if author_label and author_label != stamped_label(caller_peer_id):
            raise HandlerError("E_CARD_SEAT_MISMATCH", f"args.author {author_label!r} is not the caller's label: "
                                                       "a card's author is its signer. Omit it")
    if not author_label:
        # A missing --author blanked author_label UNCONDITIONALLY at this seam and
        # cost real attribution. The asymmetry was the tell: status gets defaulted
        # below because the handler cannot infer it, while identity - which the
        # handler CAN infer from the verified caller it holds right here - had
        # no defence. Roster label first; the peer-id prefix is the honest fallback
        # for an unrostered peer (attribution, never invention).
        author_label = stamped_label(caller_peer_id)

    pre_discipline = bool(args.get("pre_discipline", False))
    claimed_status = args.get("claimed_status") if pre_discipline else None

    # shared/wiki ontology shape. Off-vocab tags are NOT blocked - the vocabulary
    # grows by proposal then CONFIRM - but an UNTAGGED entry falls out of the graph
    # buckets entirely and the handler cannot infer a topic.
    if namespace == "shared/wiki" and not pre_discipline:
        if status is None:
            status = "OPEN"
        if not tags:
            raise HandlerError(
                "E_WIKI_NO_TAGS",
                "a shared/wiki entry must carry at least one tag (the daemon cannot "
                "infer topic)",
            )

    if pre_discipline and status is not None:
        raise HandlerError(
            "E_BAD_REQUEST",
            "a pre_discipline custody write must not set the discipline 'status' column "
            "(carry the v0.1 value in claimed_status instead)",
        )

    expected_head_version = args.get("expected_head_version")
    if expected_head_version is not None:
        try:
            expected_head_version = int(expected_head_version)
        except (TypeError, ValueError):
            raise HandlerError("E_BAD_REQUEST", "expected_head_version must be an integer")

    call_id, signature, signing_kid, verified_at, signed_bytes, jcs_sha = _signed_envelope(
        verified_payload, payload, conn, caller_peer_id)

    ctx = {"caller_owner_id": None, "authz": None}
    try:
        with rls.server_internal(conn):
            # 1) default-deny. A custody/backfill write is an administrative op, and never
            #    takes a capability (design 4.3). With one, the token decides (Q6: `authz`).
            if pre_discipline:
                _refuse_capability(principal, "a pre_discipline write",
                                   "a custody write is administrative")
            decision = check_permission(namespace, caller_peer_id,
                                        "admin" if pre_discipline else "write", conn,
                                        resource=Key(key), principal=principal)
            if principal is not None:
                ctx["authz"] = decision

            # 2) replay dedup (read-before-write), held to this write's (namespace, key).
            existing = _existing_by_dedup(conn, jcs_sha, call_id)
            if existing is not None:
                # the directive rule: a directive's replay is the IDENTICAL signed payload only. The dedup also matches on
                # call_id, so a changed directive (body, target, target_peer, expiry) re-sent under the same call_id
                # would get the stored write back and skip the admission below (pxx, codex-pxx-cli R2).
                if (((isinstance(value, dict) and "directive" in value) or bool(existing.get("stored_directive")))
                        and str(existing.get("payload_jcs_sha256") or "") != str(jcs_sha)):
                    raise HandlerError("E_REPLAY", "this call_id was already used for a different message; a "
                                                   "directive is replayed only byte for byte")
                return _with_authz(
                    _replay_within(existing, namespace, key, caller_peer_id), ctx["authz"])

            # the directive rule: a NEW directive's admission, after the replay check, so an identical committed send replayed
            # (a lost reply) returns its stored result even once the hour's allowance is spent or the label has moved
            # (codex-oxxxxxx-admin-cli bc9a07, P2-2). The per-signer allowance is serialized inside this transaction
            # (see the helper), and the target peer must hold the target label now.
            if isinstance(value, dict) and "directive" in value:
                check_directive_key_time(key)
                check_directive_not_expired(value)
                _enforce_directive_rate(conn, caller_peer_id)
                d = value["directive"]
                if d["target"] not in set(_caller_labels(conn, d["target_peer"])):
                    raise HandlerError("E_INVALID_ARGS", "directive.target_peer does not hold directive.target now")
            # The urgent lane's allowance (kxxx), after the replay return like a directive's: the key carries the
            # current time, so the hour's count is one key range.
            if isinstance(value, dict) and ("urgent" in value or "urgent_declined" in value):
                check_directive_key_time(key)
                _enforce_urgent(conn, caller_peer_id, value)
            # A window that has already closed is refused at write, for a directive and a relay alike (kxxx),
            # after the replay return so an identical late replay still gets its stored result.
            check_window_not_past(value)

            # 3) owner derived LIVE (MUST-2).
            caller_owner_id = ctx["caller_owner_id"] = derive_owner_id(conn, caller_peer_id)

            # 4) I2 write side - the actual leak path. A product tenant must never be
            #    created_by_owner_id on a shared/* row. Defence in depth BELOW
            #    check_permission, which already needs a grant the I2 grant-ban denies.
            if namespace.startswith("shared/") and not is_dev_plane(conn, caller_owner_id):
                raise HandlerError(
                    "E_PRODUCT_NO_SHARED_WRITE",
                    f"only dev-plane owners may write a shared/* namespace ({namespace!r})",
                )

            # 4b) divergence 028 R1/R2: a mesh message is sent as the caller, under the
            #     caller's name; a msg/ row in any other namespace that names a sender
            #     names the caller (R2). After the plane ban, so a product tenant still
            #     gets its audited refusal rather than this one.
            mesh_message = is_mesh_message(namespace, key)
            if claims_mesh_sender(namespace, key, value):
                _enforce_mesh_sender(conn, args, value, caller_peer_id,
                                     check_author=mesh_message)

            # 4c) divergence 091: a card is its seat's own, under the one label this handler
            #     stamps for the caller; a later version is its first writer's; and every card
            #     it supersedes is an earlier head card. After the plane ban, as the mail's
            #     sender rules are, and before anything is written.
            if card:
                card_rules.enforce_card(conn, namespace, key, value, caller_peer_id,
                                        caller_label=stamped_label(caller_peer_id))

            # 4d) context.v1: the seat's own key, written once, both screens on the whole text, and a manifest's
            #     items and order. After check_permission and the plane ban: a screen code reaches only a caller
            #     who may write here (docs/design/context-v1.md 7).
            if context:
                context_rules.enforce_context(conn, namespace, key, value, context, caller_peer_id,
                                              caller_label=stamped_label(caller_peer_id))

            ephemeral = _namespace_retention_policy(conn, namespace) == RETENTION_EPHEMERAL

            # 5) current head.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, version, status, pre_discipline FROM shared_memory "
                    "WHERE namespace = %s AND key = %s AND is_head = TRUE LIMIT 1",
                    (namespace, key),
                )
                head = cur.fetchone()
            # 5a) context.v1: write-once again, on THIS read, the one the version below is taken from: a racer that
            #     committed after enforce_context's check is refused here, never written as version 2.
            if context:
                context_rules.refuse_existing_head(key, head)

            # 5b) divergence 028 R4: one writer per message. Before anything is retired or
            #     deleted - the ephemeral path DELETEs the chain, so this is its only guard.
            if mesh_message and head is not None:
                _enforce_single_writer(conn, namespace, key, caller_peer_id)
                # Divergence 059: THE AUTHOR TOO. A later version through memory.write was an unguarded edit path (nothing
                # froze to or cc on it, and no seat was told). Every later version of a mail key now goes through
                # memory.edit, with its checks and its event; sql_meshychat/029 holds the same refusal in the database.
                raise HandlerError(
                    "E_USE_MEMORY_EDIT",
                    f"{namespace}/{key} already has a version; a later version of a mail key is written only by "
                    "memory.edit (its body only, and the seats that held it are told), never by memory.write "
                    "(divergence 059)")

            if head is None:
                if expected_head_version not in (None, 0):
                    raise HandlerError(
                        "E_WRITE_CONFLICT",
                        f"expected head version {expected_head_version} but "
                        f"{namespace}/{key} has no head",
                    )
                if status in NEW_KEY_FORBIDDEN_STATUSES:
                    raise HandlerError(
                        "E_NEW_KEY_BAD_STATUS", f"new key cannot start at status {status!r}")
                new_version, parent_id, head_status = 1, None, None
            else:
                head_id, head_version = head[0], int(head[1])
                head_status, head_pre = head[2], bool(head[3])

                if expected_head_version is not None and expected_head_version != head_version:
                    raise HandlerError(
                        "E_WRITE_CONFLICT",
                        f"expected head version {expected_head_version}, actual {head_version}")

                # MUST-7: a pre_discipline head cannot jump straight to a closure.
                if head_pre and status in CLOSURE_STATUSES:
                    raise HandlerError(
                        "E_PRE_DISCIPLINE_TRANSITION",
                        f"cannot set {status!r} directly on a pre_discipline head; "
                        "write a fresh PROPOSED_FIX first",
                    )

                # MUST-2: a closure needs a PROPOSED_FIX on the chain authored by a
                # DIFFERENT owner. Applied to WONTFIX too, which closes the
                # dismiss-path bypass.
                if status in CLOSURE_STATUSES:
                    proposer_owners = _proposed_fix_owners(conn, namespace, key)
                    if not proposer_owners:
                        raise HandlerError(
                            "E_NO_PROPOSED_FIX",
                            f"cannot set {status} on {namespace}/{key}: no PROPOSED_FIX in chain")
                    if caller_owner_id in proposer_owners:
                        raise HandlerError(
                            "E_DISCIPLINE_VIOLATION",
                            "fix-author and close-author share owner_id "
                            f"({caller_owner_id}); a different owner must set {status}")

                new_version, parent_id = head_version + 1, head_id

            # 6) confirmer discipline. AFTER the head is resolved, because the
            #    no-confirmer rule fires on the TRANSITION into CONFIRMED and a
            #    transition cannot be told from a carry-forward without head_status.
            _enforce_wiki_confirmer(namespace, pre_discipline, status, value, head_status,
                                    conn=conn, key=key)
            _enforce_wiki_confirmer_authenticated(
                namespace, pre_discipline, status, value, head_status, caller_peer_id,
                conn=conn, key=key)

            # 7) provenance: the VERBATIM envelope plus the exact key bytes used to
            #    verify. No embedding (MUST-8).
            pubkey_b64url, pubkey_fp = fetch_caller_pubkey(conn, caller_peer_id, signing_kid)
            provenance = _provenance(payload, signature, signing_kid, caller_owner_id, jcs_sha,
                                     pubkey_b64url, pubkey_fp, verified_at, pre_discipline)
            if ctx["authz"] is not None:
                # Q6: how this write was authorised, ONLY when a token decided it: the
                # decision record, never the token (the signed payload above carries only
                # its hash, capability_sha256).
                provenance["capability"] = ctx["authz"]

            # 8) write the new head.
            #    ephemeral: latest-only, so the prior rows are DELETEd and a single
            #    version-1 head is inserted with no chain.
            #    otherwise: APPEND - retire the prior head, insert version+1.
            #    provenance_call_id is GENERATED and is therefore NOT in the column list.
            with conn.cursor() as cur:
                if ephemeral:
                    cur.execute(
                        "DELETE FROM shared_memory WHERE namespace = %s AND key = %s",
                        (namespace, key),
                    )
                    ins_version, ins_parent = 1, None
                else:
                    if parent_id is not None:
                        cur.execute(
                            "UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (parent_id,))
                    ins_version, ins_parent = new_version, parent_id
                cur.execute(
                    "INSERT INTO shared_memory "
                    "(namespace, key, version, is_head, parent_id, value, status, tags, "
                    " author_label, pre_discipline, claimed_status, embed_policy, "
                    " provenance_jsonb, payload_jcs_sha256, signed_bytes, "
                    " created_by_peer_id, created_by_owner_id) "
                    "VALUES (%s, %s, %s, TRUE, %s, %s::jsonb, %s, %s::text[], %s, %s, %s, "
                    " 'forbidden', %s::jsonb, %s, %s, %s, %s) "
                    "RETURNING id, version, created_at",
                    (namespace, key, ins_version, ins_parent, json.dumps(value), status, tags,
                     author_label, pre_discipline, claimed_status,
                     json.dumps(provenance), jcs_sha, signed_bytes, str(caller_peer_id),
                     caller_owner_id),
                )
                ins = cur.fetchone()

            # 9) issue #118: a mail message that attaches files lets the peers it addresses download them (rule 3 of
            #    apps/files/service.readable_row). Recorded HERE, in the write's own transaction, so the message and
            #    its grants commit together; only for files the sender may already read (no minting). Recipients and
            #    attachments never change after this (059: later versions go through memory.edit, body only).
            if is_mesh_message(namespace, key) and isinstance(value, dict) and value.get("attachments"):
                from apps.files import grants as file_grants
                file_grants.record_message_grants(
                    conn, namespace=namespace, key=key, value=value,
                    sender_peer_id=caller_peer_id, sender_owner_id=caller_owner_id)

    except HandlerError as e:
        if e.code == "E_PRODUCT_NO_SHARED_WRITE":
            audit_tenancy_denial(
                conn, e.code, ctx["caller_owner_id"], caller_peer_id, namespace,
                detail="product-plane caller denied shared/* write (I2)",
            )
        raise
    except psycopg.errors.UniqueViolation:
        # A concurrent write: either a replay (same payload/call_id) or a
        # version/head race. Re-check the dedup keys to disambiguate rather than
        # guessing - the two are indistinguishable from the constraint name alone.
        with rls.server_internal(conn):
            existing = _existing_by_dedup(conn, jcs_sha, call_id)
        if existing is not None:
            if (((isinstance(value, dict) and "directive" in value) or bool(existing.get("stored_directive")))
                    and str(existing.get("payload_jcs_sha256") or "") != str(jcs_sha)):
                raise HandlerError("E_REPLAY", "this call_id was already used for a different message; a "
                                               "directive is replayed only byte for byte")
            return _with_authz(
                _replay_within(existing, namespace, key, caller_peer_id), ctx["authz"])
        raise HandlerError("E_WRITE_CONFLICT", f"concurrent write to {namespace}/{key}; retry")

    if unversioned:
        # Counted only here, where the row is written: a refusal raised above and a replay
        # returned above are not acceptances. The caller's stamped label, never the value.
        mesh_unversioned.note_accepted(author_label)

    return _with_authz({
        "op": "memory.write",
        "namespace": namespace,
        "key": key,
        "version": int(ins[1]),
        "is_head": True,
        "id": str(ins[0]),
        "status": status,
        "pre_discipline": pre_discipline,
        "claimed_status": claimed_status,
        # Divergence 039 (G4, Q5): the server's own stamp and its normalisation, attested.
        # Over W1 this dict is the `result` the server signs (apps/calls/route.py).
        "author_label": author_label,
        "tags": list(tags),
        "created_at": str(ins[2]),
        "replay": False,
    }, ctx["authz"])


# --------------------------------------------------------------------------- #
# memory.read
# --------------------------------------------------------------------------- #

def handle_read(verified_payload, caller_peer_id, conn, *, principal=None):
    """Read the head of (namespace, key), or an explicit ?version=N. Default-deny.
    With a capability, the token decides for `Key(key)`
    (site 3; divergence 040 slice 6), and the result carries `authz` (Q6)."""
    payload = payload_of(verified_payload)
    args = args_of(payload)
    namespace = require(args, "namespace")
    key = require(args, "key")
    version = args.get("version")
    include_provenance = bool(args.get("include_provenance", False))
    fields_mode = fields_mode_of(args)

    with rls.server_internal(conn):
        decision = check_permission(namespace, caller_peer_id, "read", conn,
                                    resource=Key(key), principal=principal)
        cols = _memo_cols(include_provenance=include_provenance)
        with conn.cursor(row_factory=dict_row) as cur:
            if version is not None:
                try:
                    version = int(version)
                except (TypeError, ValueError):
                    raise HandlerError("E_BAD_REQUEST", "version must be an integer")
                cur.execute(
                    f"SELECT {cols} FROM shared_memory "
                    "WHERE namespace = %s AND key = %s AND version = %s",
                    (namespace, key, version),
                )
            else:
                cur.execute(
                    f"SELECT {cols} FROM shared_memory "
                    "WHERE namespace = %s AND key = %s AND is_head = TRUE",
                    (namespace, key),
                )
            row = cur.fetchone()
            # A version an EDIT wrote says so, from the record and never from `value` (divergence 059,
            # docs/design/edit.md section 2): `from` stays the original sender, so without this a reader
            # credits that sender with words the editor wrote. Only a version above 1 can have a record.
            edited_at = None
            if row is not None and int(row["version"]) > 1:
                cur.execute("SELECT to_char(edited_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS edited_at "
                            "FROM shared_memory_edit WHERE memory_id = %s", (row["id"],))
                found = cur.fetchone()
                edited_at = found["edited_at"] if found else None

    if row is None:
        raise HandlerError(
            "E_UNKNOWN_KEY",
            f"no memo at {namespace}/{key}" + (f"@v{version}" if version is not None else ""),
        )
    memo = _row_to_memo(row, include_provenance=include_provenance)
    if edited_at is not None:
        memo["edited_by"] = memo["author_label"]
        memo["edited_at"] = edited_at
    if fields_mode in ("abstract", "structure"):
        project = abstract_of if fields_mode == "abstract" else structure_of
        memo["value"] = project(memo.get("value"))
        # MARKED at the row level so a reduced value cannot be mistaken for the full
        # one; never marked INSIDE the value, where it would collide with an
        # author-owned key.
        memo["value_fields"] = fields_mode
    # threat_model.md, amendment of 28 Oct 2026: whether an operator relay is addressed to THIS caller is the server's
    # word, inside the signed result, never the client's match against labels an unsigned GET returned (pxx's review
    # of #447, F2). Only on a mesh message that carries operator_words.
    value = memo.get("value")
    if (namespace == MESH_CHAT_NAMESPACE and str(key).startswith("msg/") and isinstance(value, dict)
            and value.get("operator_words") is not None):
        raw_to = value.get("to")       # a list of labels; a string or dict would iterate as letters or keys (codex-pxx-cli)
        to = {x for x in raw_to if isinstance(x, str)} if isinstance(raw_to, list) else set()
        memo["addressed_to_caller"] = bool(to & set(_caller_labels(conn, caller_peer_id)))
    # the directive rule: a directive is for its ONE target. Its own signed bit, never the relay's (codex-pxx-cli's pass on plan
    # v1, P2-3: one bit for two fields lets the to line of one decide the other). False whenever the switch is off, so
    # turning it off stops every stored directive at the next read, not only new writes.
    if (namespace == MESH_CHAT_NAMESPACE and str(key).startswith("msg/") and isinstance(value, dict)
            and isinstance(value.get("directive"), dict)):
        target = value["directive"].get("target")
        on = seat_directives_enabled()
        memo["seat_directives"] = "on" if on else "off"
        # The signer must be on the CURRENT roster at read time, not only when it wrote: removing a seat from
        # SEAT_DIRECTIVE_PEERS stops its stored directives at the next read, whatever roster an old client pins
        # (codex-oxxxxxx-admin-cli ef2965, P2-3).
        on_roster = is_roster_seat(memo.get("created_by_peer_id"))
        memo["directive_signer_on_roster"] = on_roster
        # The peer fixed at write, never the label now (pxx): a moved label addresses nobody new.
        target_peer = value["directive"].get("target_peer")
        memo["directive_addressed_to_caller"] = bool(
            on and on_roster and isinstance(target, str) and isinstance(target_peer, str)
            and target_peer == str(caller_peer_id))
    out = {"op": "memory.read", "memo": memo}
    if fields_mode in ("abstract", "structure"):
        out["fields"] = fields_mode
    return _with_authz(out, decision if principal is not None else None)


# --------------------------------------------------------------------------- #
# memory.query
# --------------------------------------------------------------------------- #

#: A windowed history (`include_history` with `since`) reports, per key, how many versions
#: `since` withheld. That count reads at most this many versions + 1 of each key (E16,
#: docs/V1-DESIGN-RULES.md B5): exact up to it, and past it a lower bound that says so.
HISTORY_WINDOW_COUNT_CAP = 100

#: The count is bounded per chain (E16). `COUNT(*) ... GROUP BY key` would read every
#: version of every key before `since`, which shape B5 forbids at the scale of the longest
#: chain. This reads each key's LOWEST
#: `cap + 1` versions through the (namespace, key, version) unique index and counts those
#: before `since`. Versions are appended in time order, so the withheld versions are the
#: lowest ones: when the prefix holds a version at or after `since`, every withheld version is
#: in it and the count is exact; when all `cap + 1` are before `since`, it is a lower bound.
#: The one inexactness, stated: two concurrent writers of one key can commit a version whose
#: created_at (its transaction's start) is earlier than the version below it; that inversion
#: moves a row across `since` only when `since` falls inside it, milliseconds wide.
_BEFORE_SINCE_SQL = (
    "SELECT k.key, p.before, p.read FROM unnest(%s::text[]) AS k(key) "
    "CROSS JOIN LATERAL ("
    " SELECT count(*) FILTER (WHERE s.created_at < %s::timestamptz) AS before,"
    " count(*) AS read"
    " FROM (SELECT created_at FROM shared_memory"
    " WHERE namespace = %s AND key = k.key ORDER BY version ASC LIMIT %s) s"
    ") p"
)


def _before_since(cur, namespace: str, keys: list, since_iso: str) -> dict:
    """key -> (versions before `since`, whether that is a lower bound). See _BEFORE_SINCE_SQL."""
    cap = HISTORY_WINDOW_COUNT_CAP
    cur.execute(_BEFORE_SINCE_SQL, [keys, since_iso, namespace, cap + 1])
    out = {}
    for r in cur.fetchall():
        before, read = int(r["before"]), int(r["read"])
        out[r["key"]] = (before, before > cap and before == read)
    return out


def handle_query(verified_payload, caller_peer_id, conn, *, principal=None):
    """Filter heads in ONE namespace.

    There are NO embeddings and no semantic path: this is exact/filter only
    (MUST-8). The namespace is REQUIRED and the filter is namespace-scoped, so a
    query can never sweep across namespaces. `tags` is contains-all (@>).
    `include_history` adds non-head versions.

    With a capability, the token decides for `Prefix(key_prefix or "")` (site 4; divergence
    040 slice 6): the range must lie inside a granted prefix, and `contains`, `tags`, `since`
    and the rest filter INSIDE it (design 4.3). The result carries `authz` (Q6).
    """
    payload = payload_of(verified_payload)
    args = args_of(payload)
    namespace = require(args, "namespace")
    key_prefix = args.get("key_prefix")
    status = args.get("status")
    if status is not None:
        validate_status(status)
    tags = norm_tags(args.get("tags")) if args.get("tags") is not None else None
    author = args.get("author") or args.get("author_label")
    contains = args.get("contains")
    if contains is not None and not isinstance(contains, str):
        raise HandlerError("E_BAD_REQUEST", "contains must be a string")
    include_history = bool(args.get("include_history", False))
    include_provenance = bool(args.get("include_provenance", False))
    fields_mode = fields_mode_of(args)
    since = args.get("since")
    since_dt = None
    if since is not None:
        try:
            since_text = str(since)
            if since_text.endswith(("Z", "z")):
                since_text = since_text[:-1] + "+00:00"
            since_dt = datetime.fromisoformat(since_text)
            # A bare date has no timezone and no time-of-day, so it would silently
            # mean local midnight. Both halves are required.
            if "t" not in str(since).lower() or since_dt.tzinfo is None:
                raise ValueError("timezone required")
        except (TypeError, ValueError):
            raise HandlerError(
                "E_INVALID_SINCE", "since must be an RFC3339 timestamp with a timezone")
    limit = clamp_limit(args.get("limit"))
    cursor = args.get("cursor")

    where = ["namespace = %s"]
    params: list = [namespace]
    if not include_history:
        where.append("is_head = TRUE")
    if key_prefix:
        # LIKE metacharacters in the caller's prefix are escaped so a prefix cannot
        # become a wildcard sweep of the namespace.
        where.append("key LIKE %s")
        params.append(key_prefix.replace("%", r"\%").replace("_", r"\_") + "%")
    if status is not None:
        where.append("status = %s")
        params.append(status)
    if tags:
        where.append("tags @> %s::text[]")
        params.append(tags)
    if author:
        where.append("author_label = %s")
        params.append(author)
    if since is not None:
        where.append("created_at >= %s::timestamptz")
        params.append(since_dt.isoformat())
    if contains:
        # Case-insensitive containment over the content-class fields. HONEST CAVEAT
        # ABSTRACT_CONTENT_FIELDS names
        # four fields; other fleet tooling reads a wider six-name class. The gap was
        # measured at one row on the corpus at the time - a measurement, not a
        # guarantee, and it drifts the moment a seventh convention lands.
        escaped = contains.replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped}%"
        content_preds = []
        for field in ABSTRACT_CONTENT_FIELDS:
            content_preds.append("value->>%s ILIKE %s")
            params.append(field)
            params.append(pattern)
        where.append("(" + " OR ".join(content_preds) + ")")
    if cursor:
        # Keyset over (created_at DESC, id DESC). id breaks created_at ties so the
        # order is TOTAL - without it, rows sharing a created_at can be skipped or
        # repeated across pages, which is precisely the bug OFFSET has.
        c_created, c_id = decode_cursor(cursor, ("c", "i"))
        where.append("(created_at, id) < (%s::timestamptz, %s::uuid)")
        params.append(c_created)
        params.append(c_id)

    cols = _memo_cols(include_provenance=include_provenance)
    sql = (f"SELECT {cols} FROM shared_memory WHERE " + " AND ".join(where)
           + " ORDER BY created_at DESC, id DESC LIMIT %s")
    params.append(limit + 1)  # over-fetch one to detect a further page

    windowed_history = include_history and since is not None
    before_since: dict = {}

    with rls.server_internal(conn):
        decision = check_permission(namespace, caller_peer_id, "read", conn,
                                    resource=Prefix(key_prefix or ""), principal=principal)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            if windowed_history and rows:
                # `since` filters VERSION rows, so a key created before the bound
                # arrives WITHOUT its earlier versions. Count what the bound withheld
                # per key, so a partial history cannot pass for a full one; bounded per
                # chain (E16), see _BEFORE_SINCE_SQL.
                keys = sorted({r["key"] for r in rows[:limit]})
                before_since = _before_since(cur, namespace, keys, since_dt.isoformat())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor({"c": last["created_at"].isoformat(), "i": str(last["id"])})

    results = [_row_to_memo(r, include_provenance=include_provenance) for r in rows]
    if windowed_history:
        for memo in results:
            n, lower_bound = before_since.get(memo["key"], (0, False))
            memo["versions_before_since"] = n
            if lower_bound:
                # E16: at least this many; the count stopped at the cap.
                memo["versions_before_since_is_lower_bound"] = True
    if fields_mode in ("abstract", "structure"):
        project = abstract_of if fields_mode == "abstract" else structure_of
        for memo in results:
            memo["value"] = project(memo.get("value"))
            memo["value_fields"] = fields_mode
    out = {
        "op": "memory.query",
        "namespace": namespace,
        "count": len(rows),
        "results": results,
        "next_cursor": next_cursor,
    }
    if fields_mode in ("abstract", "structure"):
        out["fields"] = fields_mode
    if windowed_history:
        withheld = sum(1 for n, _ in before_since.values() if n)
        out["history_window"] = {
            "since": str(since),
            "keys_with_versions_before_since": withheld,
            "complete": withheld == 0,
            "note": "since filters version rows; a memo with versions_before_since > 0 "
                    "is missing that many earlier versions of its key, so its earliest "
                    "returned row is not version 1 and not the author",
        }
        if any(lower for _, lower in before_since.values()):
            out["history_window"]["count_cap"] = HISTORY_WINDOW_COUNT_CAP
    return _with_authz(out, decision if principal is not None else None)


# --------------------------------------------------------------------------- #
# memory.list
# --------------------------------------------------------------------------- #

#: memory.list's enumeration (divergence 040 slice 3, design 3.5): the namespaces the
#: predicate's set form, `meshy_namespace_permission(peer)` (sql_meshychat/015), answers
#: with something that admits a read. The query used to carry its own copy of the
#: owner-or-live-grant predicate. The planner inlines the set form, so this reads the two
#: tables as the copy did rather than calling a function once per claimed namespace.
_VISIBLE_NAMESPACES_SQL = (
    "SELECT n.namespace, n.created_at, a.permission AS answer "
    "FROM meshychat_namespace_permission(%(peer)s) a "
    "JOIN shared_memory_namespace n ON n.namespace = a.namespace "
    "WHERE a.permission = ANY(%(readable)s) "
    "ORDER BY n.namespace ASC"
)


def _visible_namespaces_params(caller_peer_id) -> dict:
    return {"peer": str(caller_peer_id), "readable": list(answers_admitting("read"))}


def _list_visible_namespaces(caller_peer_id, conn):
    """The claimed namespaces the caller OWNS or holds an active grant on.
    Default-deny: a namespace the caller has no relationship with is not returned,
    so `list` cannot be used to enumerate the store.

    "May read" is `check_permission`'s own decision, the database function's
    (`_VISIBLE_NAMESPACES_SQL`), and `is_owner` is its answer 'owner'."""
    with rls.server_internal(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_VISIBLE_NAMESPACES_SQL, _visible_namespaces_params(caller_peer_id))
            rows = cur.fetchall()
    items = [
        {"namespace": r["namespace"],
         "is_owner": r["answer"] == OWNER,
         "created_at": str(r["created_at"])}
        for r in rows
    ]
    return {"op": "memory.list", "scope": "namespaces", "count": len(items), "namespaces": items}


def handle_list(verified_payload, caller_peer_id, conn, *, principal=None):
    """List heads in a namespace, or - with `namespace` omitted - the namespaces the
    caller may see.

    With a capability (divergence 040 slice 6): a namespace given, the token decides for
    `Prefix(prefix or "")` (site 5) and the result carries `authz` (Q6); no namespace, it is
    refused (E_CAPABILITY_NOT_ACCEPTED), because the enumeration reads the caller's DB
    relationships and a token authorises objects, not enumeration (design 3.5, 4.3)."""
    payload = payload_of(verified_payload)
    args = args_of(payload)
    namespace = args.get("namespace")
    if not namespace:
        _refuse_capability(principal, "memory.list without a namespace",
                           "it enumerates the caller's namespaces, and a token authorises "
                           "objects, not enumeration")
        return _list_visible_namespaces(caller_peer_id, conn)
    prefix = args.get("prefix")
    since = args.get("since")
    limit = clamp_limit(args.get("limit"))
    cursor = args.get("cursor")

    where = ["namespace = %s", "is_head = TRUE"]
    params: list = [namespace]
    if prefix:
        where.append("key LIKE %s")
        params.append(prefix.replace("%", r"\%").replace("_", r"\_") + "%")
    if since:
        where.append("created_at >= %s::timestamptz")
        params.append(since)
    if cursor:
        # Heads are unique per (namespace, key) - enforced by the partial unique
        # index shared_memory_head_unique (namespace, key) WHERE is_head - so `key`
        # alone is a TOTAL order under ORDER BY key ASC.
        (c_key,) = decode_cursor(cursor, ("k",))
        where.append("key > %s")
        params.append(c_key)

    sql = (
        "SELECT key, version, status, author_label, created_at, "
        # Prefer the author-written abstract as the preview: the raw
        # LEFT(value::text, 160) fallback opens mid-JSON and is useless for
        # discovery. `->>` on a non-object jsonb returns NULL, so scalar values fall
        # through to the truncation unchanged.
        "COALESCE(NULLIF(LEFT(value->>'body_abstract', 160), ''), "
        "LEFT(value::text, 160)) AS value_preview "
        "FROM shared_memory WHERE " + " AND ".join(where) + " ORDER BY key ASC LIMIT %s"
    )
    params.append(limit + 1)

    with rls.server_internal(conn):
        decision = check_permission(namespace, caller_peer_id, "read", conn,
                                    resource=Prefix(prefix or ""), principal=principal)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor({"k": rows[-1]["key"]})

    items = [
        {"key": r["key"], "version": int(r["version"]), "status": r["status"],
         "author_label": r["author_label"], "created_at": str(r["created_at"]),
         "value_preview": r["value_preview"]}
        for r in rows
    ]
    return _with_authz({
        "op": "memory.list",
        "namespace": namespace,
        "count": len(items),
        "items": items,
        "next_cursor": next_cursor,
    }, decision if principal is not None else None)


# --------------------------------------------------------------------------- #
# memory.prune
# --------------------------------------------------------------------------- #

def handle_prune(verified_payload, caller_peer_id, conn, *, principal=None):
    """Compact a high_churn namespace.

    THE SAFETY INVARIANTS, none of which is optional: never deletes a head, never a
    row carrying ANY status (live or discipline), never a pre_discipline custody
    row, and ONLY in a high_churn namespace - keep_all keeps the whole append-only
    chain and ephemeral is latest-only at write time with nothing to prune. Admin-
    gated, because a destructive compaction is an administrative op.

    DIVERGENCE 028 ADDS ONE: never the lowest version of a mesh message (shared/mesh-chat,
    msg/). It is the row the single-writer rule reads the author from (R4 here, R5 in
    sql_meshychat/011). Deleting it on a chain written before 011 with a second writer made
    the tamperer the author and locked the real one out (review of 7084e88, finding 5).
    Later non-head versions still go, so a message keeps at most its root, keep_versions-1
    more, and its head.

    `keep_versions` counts versions retained per key INCLUDING the head, so the
    default 1 means head-only and the SQL keeps keep_versions-1 non-head candidates.

    The parent_id sever in step 2 is the ONE mutation the immutability trigger
    exempts, and only for high_churn, only non-NULL -> NULL, and only when no other
    column moves - which is exactly the policy already gated above.

    A capability is refused (design 4.3): a prune is destructive and bulk.
    """
    _refuse_capability(principal, "memory.prune", "a prune is destructive and bulk")
    payload = payload_of(verified_payload)
    args = args_of(payload)
    namespace = require(args, "namespace")
    key = args.get("key")  # None compacts every key in the namespace
    keep_versions = args.get("keep_versions", 1)
    try:
        keep_versions = int(keep_versions)
    except (TypeError, ValueError):
        raise HandlerError("E_BAD_REQUEST", "keep_versions must be an integer")
    keep_count = max(0, keep_versions - 1)

    statuses = sorted(ALL_STATUSES)

    with rls.server_internal(conn):
        # One key when a key is given, the empty string included (the SQL below compares
        # `key = %s`); every key only when none is (`%s::text IS NULL`).
        check_permission(namespace, caller_peer_id, "admin", conn,
                         resource=Key(key) if key is not None else NAMESPACE_WIDE)

        policy = _namespace_retention_policy(conn, namespace)
        if policy != RETENTION_HIGH_CHURN:
            raise HandlerError(
                "E_BAD_REQUEST",
                f"memory.prune only compacts high_churn namespaces; {namespace!r} is {policy!r}",
            )
        # Divergence 091: never the card namespace, whatever its policy. A card's older versions are
        # the only record of who changed a supersession and when, and version 1 is the row the
        # single-writer rule reads the author from. The namespace is claimed keep_all; this holds if
        # it is ever claimed otherwise.
        if card_rules.is_card_namespace(namespace):
            raise HandlerError(
                "E_BAD_REQUEST",
                f"memory.prune never compacts the card namespace {namespace!r}: a card's versions are its record",
            )
        # Nor the context namespace: its retention is the operator's open decision (docs/design/context-v1.md 10).
        if context_rules.is_context_namespace(namespace):
            raise HandlerError(
                "E_BAD_REQUEST",
                f"memory.prune never compacts the context namespace {namespace!r}: its retention is not decided",
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT id, namespace, key, version,
                           row_number() OVER (PARTITION BY namespace, key
                                              ORDER BY version DESC) AS rn
                    FROM shared_memory
                    WHERE namespace = %s
                      AND (%s::text IS NULL OR key = %s)
                      AND is_head = FALSE
                      AND (status IS NULL OR status <> ALL(%s::text[]))
                      AND pre_discipline = FALSE
                      AND NOT (namespace = %s AND key LIKE 'msg/%%'
                               AND version = (SELECT min(r.version) FROM shared_memory r
                                              WHERE r.namespace = shared_memory.namespace
                                                AND r.key = shared_memory.key))
                )
                SELECT id, namespace, key, version
                FROM candidates
                WHERE namespace = %s
                  AND (%s::text IS NULL OR key = %s)
                  AND rn > %s
                """,
                (namespace, key, key, statuses, MESH_CHAT_NAMESPACE,
                 namespace, key, key, keep_count),
            )
            selected = cur.fetchall()
            to_delete_ids = [r[0] for r in selected]

            if to_delete_ids:
                # Sever inbound parent_id links into the delete set so the RESTRICT FK
                # does not block the DELETE.
                cur.execute(
                    "UPDATE shared_memory SET parent_id = NULL WHERE parent_id = ANY(%s::uuid[])",
                    (to_delete_ids,),
                )
                cur.execute(
                    "DELETE FROM shared_memory WHERE id = ANY(%s::uuid[]) "
                    "RETURNING namespace, key, version",
                    (to_delete_ids,),
                )
                rows = cur.fetchall()
            else:
                rows = []

    removed = [{"namespace": r[0], "key": r[1], "version": int(r[2])} for r in rows]
    return {
        "op": "memory.prune",
        "namespace": namespace,
        "retention_policy": policy,
        "keep_versions": keep_versions,
        "total_removed": len(removed),
        "removed": removed,
    }


# --------------------------------------------------------------------------- #
# Dispatch - keyed on the VERIFIED payload's skill name
# --------------------------------------------------------------------------- #

DISPATCH = {
    "memory.write": handle_write,
    "memory.read": handle_read,
    "memory.query": handle_query,
    "memory.list": handle_list,
    "memory.grant": handle_grant,
    "memory.prune": handle_prune,
    "memory.redact": None,   # bound below: apps/memoryskills/redact.py imports this module
    "memory.edit": None,     # bound below: apps/memoryskills/edit.py imports this module
    "namespace.claim": handle_namespace_claim,
}


def _handle_redact(verified_payload, caller_peer_id, conn, *, principal=None):
    """memory.redact (divergence 053). The body lives in redact.py, which needs this
    module's signature check; importing it here at call time keeps the import one-way."""
    from .redact import handle_redact
    return handle_redact(verified_payload, caller_peer_id, conn, principal=principal)


DISPATCH["memory.redact"] = _handle_redact


def _handle_edit(verified_payload, caller_peer_id, conn, *, principal=None):
    """memory.edit (divergence 059). The body lives in edit.py, for the reason redact.py's does."""
    from .edit import handle_edit
    return handle_edit(verified_payload, caller_peer_id, conn, principal=principal)


DISPATCH["memory.edit"] = _handle_edit


def _bind_envelope_to_the_call(skill_name, verified_payload, caller_peer_id, principal=None):
    """The signed payload must describe THIS call, made by THIS caller.

    Without this, the envelope is only proof that SOMETHING was signed. Two confused-deputy
    shapes were open here:

      * `payload.caller_peer_id` was never compared to the AUTHENTICATED caller, so a
        payload naming a different caller could be presented and the provenance row would
        record a caller the signature does not support.
      * `payload.skill_name` was never compared to the DISPATCHED skill, so an envelope
        signed for `memory.read` could be replayed into `memory.write`.

    The authenticated identity wins; the payload is checked AGAINST it and never used to
    build it. Grants follow the same invariant.

    CODE CHOICE: E_BAD_ENVELOPE is in the registered vocabulary of errors.py. An invented
    code such as E_SIG_BAD is silently coerced to E_INTERNAL by errors.py, so the caller
    would see "internal error" for a signature failure.

    REQUIRED, NOT "COMPARED WHEN PRESENT" (divergence 038, design B02 Q9). Until 038 a
    payload that named no caller passed, and so did one with no `kind` or a `kind` of
    `skill.result`: the row was attributed to the bearer while its signed bytes named
    nobody, and nothing on the board door or an in-process call compared the domain
    separator. HTTP invoke never showed it because `verify_payload` refuses both first.
    This is the handler's own copy of checks 2 and 4, so every caller of `dispatch` gets
    them. Freshness (check 3) needs a clock and stays with the transport in front of
    `dispatch`: `verify_payload` on invoke, and the board door since 038.

    THE CAPABILITY IS BOUND BY ITS HASH (divergence 040, slice 6; design B07 3.1). A call
    that presents an object capability (`principal`) must carry `capability_sha256` in its
    SIGNED payload, the hex sha256 of the exact `Meshychat-Capability` string, and a payload that
    names one must come with it: a mismatch in either direction is refused, the field with
    no principal, the principal with no field, or any other value (compared exactly, lower
    case). The token itself is never in the payload, which is stored verbatim in
    skill_calls.args and shared_memory.provenance_jsonb; its hash binds it to one signed call
    without persisting it. Every caller of `dispatch` passes through here, so the board's
    in-process path (apps/board/declare.py), which passes no principal, refuses a payload
    that names a capability.
    """
    payload = verified_payload.get("payload") if isinstance(verified_payload, dict) else None
    if not isinstance(payload, dict):
        raise HandlerError("E_BAD_REQUEST", "verified_payload.payload missing or not an object")

    kind = payload.get("kind")
    if kind != KIND_SKILL_CALL:
        raise HandlerError(
            "E_BAD_ENVELOPE",
            f"payload.kind must be {KIND_SKILL_CALL!r}, got {kind!r}")

    claimed_caller = payload.get("caller_peer_id")
    if claimed_caller is None:
        raise HandlerError("E_BAD_ENVELOPE", "payload.caller_peer_id missing")
    if str(claimed_caller) != str(caller_peer_id):
        raise HandlerError(
            "E_BAD_ENVELOPE",
            "payload.caller_peer_id does not match the authenticated caller")

    claimed_skill = payload.get("skill_name")
    if claimed_skill is not None and str(claimed_skill) != str(skill_name):
        raise HandlerError(
            "E_BAD_ENVELOPE",
            f"payload.skill_name {claimed_skill!r} does not match the dispatched skill "
            f"{skill_name!r}")

    if principal is None:
        if "capability_sha256" in payload:
            raise HandlerError(
                "E_BAD_ENVELOPE",
                "payload.capability_sha256 names a capability, and this call presents none")
    elif "capability_sha256" not in payload:
        raise HandlerError(
            "E_BAD_ENVELOPE",
            "this call presents a capability, and its signed payload does not bind it "
            "(payload.capability_sha256 is missing)")
    elif payload["capability_sha256"] != principal.sha256:
        raise HandlerError(
            "E_BAD_ENVELOPE",
            "payload.capability_sha256 is not the sha256 of the capability this call presents")


def dispatch(skill_name, verified_payload, caller_peer_id, conn, *, principal=None):
    """Route to a handler. An unknown skill is E_UNKNOWN_SKILL, never a KeyError.

    A refusal that carries a DEFERRED AUDIT (`exc.deferred_audit`, set by the capability
    decision's shared/* plane clause, divergence 040 slice 4) has it written here: the
    handler raised inside its server_internal transaction, which has rolled back by now, so
    the audit row survives the denial, as handle_grant's and handle_write's own do.

    `principal` (divergence 040, slice 6) is the call's object capability or None: bound to
    the signed payload by `_bind_envelope_to_the_call`, then handed to the handler as its
    `principal`. Without one the handler is called exactly as before."""
    handler = DISPATCH.get(skill_name)
    if handler is None:
        raise HandlerError("E_UNKNOWN_SKILL", f"unknown skill {skill_name!r}")
    _bind_envelope_to_the_call(skill_name, verified_payload, caller_peer_id, principal)
    try:
        if principal is None:
            return handler(verified_payload, caller_peer_id, conn)
        return handler(verified_payload, caller_peer_id, conn, principal=principal)
    except HandlerError as exc:
        deferred = getattr(exc, "deferred_audit", None)
        if deferred is not None:
            audit_tenancy_denial(conn, deferred["code"], deferred["caller_owner_id"],
                                 deferred["caller_peer_id"], deferred["namespace"],
                                 detail=deferred["detail"],
                                 target_owner_id=deferred["target_owner_id"])
        raise


def _verify_provenance_signature(conn, caller_peer_id, signing_kid, payload,
                                 signature_b64) -> bytes:
    """Ed25519 verify_strict over JCS(payload) with the caller's key for `signing_kid`.
    Returns the bytes it verified, which handle_write stores as `signed_bytes`.

    Strict, not libsodium-default: small-order public keys and non-canonical (malleable)
    scalars are rejected. libsodium's default verification does NOT do this.
    """
    from apps.crypto import primitives as _p

    with rls.server_internal(conn):
        row = fetch_caller_pubkey(conn, caller_peer_id, signing_kid)
    pubkey_b64url = row[0] if isinstance(row, (tuple, list)) else row
    if not pubkey_b64url:
        raise HandlerError("E_BAD_ENVELOPE", "no resolvable key for signing_kid")
    try:
        pubkey = _p.b64u_decode(str(pubkey_b64url))
        signature = _p.b64u_decode(str(signature_b64))
    except (ValueError, TypeError) as exc:
        raise HandlerError("E_BAD_ENVELOPE", f"signature or key is not base64url: {exc}")
    message = _p.jcs(payload)
    if not _p.verify_strict(pubkey, message, signature):
        raise HandlerError("E_BAD_ENVELOPE", "envelope signature does not verify")
    return message
