"""The first owner, created from a shell: `manage.py bootstrap_owner`.

THE CHICKEN-AND-EGG. Every route that creates a peer needs an owner (`POST /v1/auth/onetime`
is owner-authenticated), and the only credential an owner can present headlessly is
`POST /v1/auth/owner/sign-in` - a signature by a PEER key that the owner has registered in
`owner_human_keys`. Registering one needs an owner session. Normally the owner is created
by an SSO callback in a browser, which registers a key from that session. This deployment
has no SSO service, so nothing can create the first owner at all.

This module is the one place that can, and it is a CLI on purpose. There is no HTTP route
to it, because an unauthenticated "create an owner" route is exactly the loop being broken,
left open. Whoever runs it already holds the database; it grants nothing they did not have.

WHAT IT WRITES, IN ONE TRANSACTION (docs/DIVERGENCES.md 016):

  dashboard_users   the owner (role at its column default, as an SSO-born owner gets it;
                    the PLANE is the operator's choice, see below, and so is the TIER when
                    `--tier` is given, else the column default `free`)
  peers             the owner's SIGNER - the peer whose key owner sign-in verifies. Owner
                    sign-in is keyed on a peer_id, so a key with
                    no peer cannot sign in at all.
  peer_keys         that peer's active key: the operator's public key
  owner_human_keys  the SAME public key, registered for owner sign-in - the allowlist row
                    the browser would otherwise have added

The private key never touches this process. It reads a PUBLIC key file and refuses one
that holds a private key, so the command cannot become a way to put signing material into
a shell history, a log or a database.

"FIRST" IS ENFORCED, not described: with any owner already present it refuses unless the
operator says `--additional-owner`. A duplicate email is refused either way, so a re-run
never re-keys an owner that exists. `manage.py bootstrap_memory_peer` also creates an owner
(the memory authority's, a service owner with no sign-in key), so run THIS command first on
a fresh install (README.md "Fresh install"); when that service owner is the only one
present, the refusal says so and names `--additional-owner` as the correct way on
(divergence 025). The check itself is unchanged: any owner still counts.

THE PLANE IS CHOSEN HERE OR NEVER (divergence 024). An owner is `product` (the column
default, what an SSO-born owner gets) or `dev` in a trust group, and sql/040 freezes both
after insert. Every shared/* namespace refuses a product tenant as a claimant, a grantee
and a writer, so an install whose only owner is product-plane can mint nodes that read
the mesh and can never write to it: the end-to-end run of 2026-09-23 met exactly that,
and got past it only with operator SQL. `plane="dev"` with the memory authority's trust
group gives the operator's nodes the same group as the memory authority (so
`meshy_same_tenant_or_devgroup` authorizes their calls without a cross-owner skill grant,
apps/calls/authorize.py) and a plane shared/* admits. The default stays `product`, so an
existing provisioning script creates exactly what it did before.

THE TIER, divergence 028 (T). The column default is the `free` tier, where sql/013
seeds `peers_per_owner` = 5, and minting reserves one unit per node, so a self-hosted mesh
stopped at five nodes per owner with SQL as the only way on. `tier` (validated against
`tier_capabilities`, apps/authapi/owner_tier.py) sets it at creation; left out, the column
default applies exactly as before. `manage.py set_owner_tier` changes it afterwards.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from apps.authapi import owner_tier
from apps.authapi.didkey import to_did_key
from apps.authapi.onboard import decode_pubkey, is_edwards_point
from apps.crypto import primitives as P
from apps.peers import names as peer_names

#: Serialises two bootstraps racing on an empty install, so "no owner exists yet" cannot
#: be true for both. Transaction-scoped, released on commit or rollback.
_ADVISORY_LOCK_KEY = 0x6D657368_626F6F74   # "meshboot"

HUMAN_KEY_LABEL = "bootstrap"

_B64URL_KEY = re.compile(r"[A-Za-z0-9_-]{43}")


class BootstrapRefused(Exception):
    """A refusal the operator must act on. Nothing was written."""


@dataclass(frozen=True)
class BootstrappedOwner:
    owner_id: uuid.UUID
    peer_id: uuid.UUID
    peer_kid: str
    human_key_kid: str
    public_key: str
    #: dashboard_users.tier as the database stored it (RETURNING), not as it was asked.
    tier: str = "free"

    def as_dict(self) -> dict:
        return {"owner_id": str(self.owner_id), "peer_id": str(self.peer_id),
                "peer_kid": self.peer_kid, "human_key_kid": self.human_key_kid,
                "public_key": self.public_key, "tier": self.tier}


def read_public_key(data: bytes) -> bytes:
    """The raw 32 bytes of an Ed25519 PUBLIC key, from a file's bytes.

    Two forms: a PEM `PUBLIC KEY` (what `openssl pkey -pubout` writes), or one line of
    unpadded base64url - the `public_key_b64url` form the node client keeps in its state
    file and sends to `/v1/onboard/peers`. Anything holding a PRIVATE key is refused.
    """
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BootstrapRefused("the key file is not ASCII text; expected a PEM PUBLIC KEY "
                               "or one line of base64url") from exc
    if "PRIVATE KEY" in text:
        raise BootstrapRefused(
            "this file holds a PRIVATE key. Pass the public half only "
            "(openssl pkey -in <private.pem> -pubout -out <public.pem>).")
    if "-----BEGIN PUBLIC KEY-----" in text:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            key = serialization.load_pem_public_key(data)
        except ValueError as exc:
            raise BootstrapRefused(f"the PEM public key does not parse: {exc}") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise BootstrapRefused("the PEM public key is not an Ed25519 key")
        raw = key.public_bytes(encoding=serialization.Encoding.Raw,
                               format=serialization.PublicFormat.Raw)
    else:
        token = text.strip()
        raw = decode_pubkey(token) if _B64URL_KEY.fullmatch(token) else None
        if raw is None or len(raw) != 32:
            raise BootstrapRefused(
                "expected a PEM PUBLIC KEY or one line of unpadded base64url encoding "
                "32 bytes")
    return check_public_key(raw)


def check_public_key(raw: bytes) -> bytes:
    """Refuse a key that could never pass `verify_strict` at sign-in, here, loudly, rather
    than creating an owner who can never sign in. Not a curve point, or small order."""
    if len(raw) != 32 or not is_edwards_point(raw):
        raise BootstrapRefused("the public key is not a valid Ed25519 point")
    if raw in P._SMALL_ORDER:  # noqa: SLF001 - the one list verify_strict refuses with
        raise BootstrapRefused("the public key is a small-order point and can verify nothing")
    return raw


PLANES = ("product", "dev")


def check_plane(plane: str, trust_group) -> tuple[str, uuid.UUID | None]:
    """The plane and the trust group are ONE decision (CHECK dashboard_users_plane_group_ck:
    `(plane = 'dev') = (trust_group_id IS NOT NULL)`), so they are validated together and
    refused with the reason before anything is written."""
    if plane not in PLANES:
        raise BootstrapRefused(f"--plane must be one of {list(PLANES)}, not {plane!r}")
    if plane == "product":
        if trust_group is not None:
            raise BootstrapRefused("--trust-group is only for --plane dev: a product-plane "
                                   "owner has no trust group")
        return plane, None
    if trust_group is None:
        raise BootstrapRefused(
            "--plane dev needs --trust-group: the group decides which owners' calls are "
            "authorized without a skill grant. On a fresh install choose a new UUID and "
            "pass the SAME value to `manage.py bootstrap_memory_peer --trust-group` after "
            "this command; if the memory authority already exists, name the trust group "
            "bootstrap_memory_peer prints for its owner.")
    try:
        return plane, uuid.UUID(str(trust_group))
    except ValueError:
        raise BootstrapRefused(f"--trust-group must be a UUID, not {trust_group!r}") from None


#: apps/calls/bootstrap.PEER_NAME, restated rather than imported: this module is the auth
#: app's and must not depend on the calls app to explain a refusal.
MEMORY_AUTHORITY_PEER_NAME = "meshy-mem-daemon"


def _not_first_reason(cur) -> str:
    """Why an existing owner blocks a first-owner bootstrap, in the one case an operator
    following the runbook's old order meets: every owner present is a memory-authority
    SERVICE owner (it owns the `meshy-mem-daemon` peer and holds no owner sign-in key), which
    `bootstrap_memory_peer` creates. The refusal stands either way; only the reason
    differs, because the generic one sent the 2026-09-23 run looking for a human owner that
    did not exist."""
    cur.execute(
        "SELECT count(*) FILTER (WHERE NOT svc), count(*) FILTER (WHERE svc) FROM ("
        "  SELECT EXISTS(SELECT 1 FROM peers p WHERE p.owner_id = u.id AND p.name = %s)"
        "     AND NOT EXISTS(SELECT 1 FROM owner_human_keys k WHERE k.owner_id = u.id)"
        "     AND NOT EXISTS(SELECT 1 FROM peers p WHERE p.owner_id = u.id AND p.name <> %s)"
        "     AS svc FROM dashboard_users u) owners",
        (MEMORY_AUTHORITY_PEER_NAME, MEMORY_AUTHORITY_PEER_NAME))
    others, service = cur.fetchone()
    if service and not others:
        # Name the group too: that owner's group is frozen (sql/040), so a --plane dev
        # owner created with any OTHER group can never reach the memory authority without
        # a skill grant, and bootstrap_memory_peer cannot move it afterwards.
        cur.execute("SELECT DISTINCT u.trust_group_id::text FROM dashboard_users u "
                    "JOIN peers p ON p.owner_id = u.id AND p.name = %s "
                    "WHERE u.trust_group_id IS NOT NULL ORDER BY 1",
                    (MEMORY_AUTHORITY_PEER_NAME,))
        groups = ", ".join(r[0] for r in cur.fetchall()) or "none"
        return ("the only owner in this database is the memory authority's service owner "
                "(manage.py bootstrap_memory_peer creates it; it has no sign-in key), so no "
                "human owner exists yet. Pass --additional-owner: that is the correct way on "
                f"here, and with --plane dev pass --trust-group {groups}, that owner's group, "
                "which cannot be changed. (The runbook in README.md runs bootstrap_owner "
                "FIRST, then bootstrap_memory_peer with the same --trust-group.)")
    return ("an owner already exists, so this is not the first owner. Pass "
            "--additional-owner to create another one deliberately.")


def bootstrap_owner(conn, *, email: str, public_key_raw: bytes,
                    display_name: str | None = None, peer_name: str = "operator",
                    additional_owner: bool = False, plane: str = "product",
                    trust_group=None, tier: str | None = None) -> BootstrappedOwner:
    """Create the owner, its signer peer, the peer's key and the owner-sign-in allowlist
    row, atomically. Raises `BootstrapRefused` having written nothing."""
    plane, group = check_plane(plane, trust_group)
    email = (email or "").strip()
    if not email:
        raise BootstrapRefused("--email is required")
    peer_name = (peer_name or "").strip()
    if not peer_name:
        raise BootstrapRefused("--peer-name must not be empty")
    raw = check_public_key(public_key_raw)
    public_key = P.b64u(raw)
    peer_kid = P.kid_for_peer_key(raw)     # the peer-key rule key-publish uses
    human_kid = to_did_key(raw)            # sql/066: "kid is the did:key / did:wba id"

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
        if tier is not None:
            try:
                owner_tier.check_tier(cur, tier)
            except owner_tier.TierRefused as exc:
                raise BootstrapRefused(str(exc)) from None
        if not additional_owner:
            cur.execute("SELECT EXISTS(SELECT 1 FROM dashboard_users)")
            if cur.fetchone()[0]:
                raise BootstrapRefused(_not_first_reason(cur))
        cur.execute("SELECT EXISTS(SELECT 1 FROM dashboard_users WHERE email = %s)", (email,))
        if cur.fetchone()[0]:
            raise BootstrapRefused(f"an owner with email {email!r} already exists; "
                                   "nothing was changed")
        # plane/trust_group and tier written only when chosen, so an owner still gets the
        # column DEFAULTS exactly as before rather than literals that could drift from them.
        cols, vals = ["email", "display_name"], [email, display_name]
        if plane == "dev":
            cols += ["plane", "trust_group_id"]
            vals += ["dev", group]
        if tier is not None:
            cols.append("tier")
            vals.append(tier)
        cur.execute(f"INSERT INTO dashboard_users ({', '.join(cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id, tier", vals)
        owner_id, stored_tier = cur.fetchone()
        try:
            peer_names.claim(cur, peer_name)                  # divergence 065
        except peer_names.NameUnavailable as exc:
            raise BootstrapRefused(f"--peer-name {peer_name!r}: {exc}. A peer name is a mail "
                                   "address, so every owner's peers need distinct names; "
                                   "pass a different --peer-name") from None
        cur.execute("INSERT INTO peers (owner_id, name, display_name) VALUES (%s, %s, %s) "
                    "RETURNING id", (owner_id, peer_name, display_name or peer_name))
        peer_id = cur.fetchone()[0]
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s, %s, %s, TRUE)", (peer_id, peer_kid, public_key))
        cur.execute("INSERT INTO owner_human_keys (owner_id, kid, public_key, label) "
                    "VALUES (%s, %s, %s, %s)", (owner_id, human_kid, public_key,
                                                HUMAN_KEY_LABEL))
    return BootstrappedOwner(owner_id=owner_id, peer_id=peer_id, peer_kid=peer_kid,
                             human_key_kid=human_kid, public_key=public_key,
                             tier=stored_tier)
