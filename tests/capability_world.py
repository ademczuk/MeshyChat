"""Test helpers for the capability verifier (B07 slice 4, divergence 040). Not a test module.

A `World` is one test's setting: a root key store installed for the test (and the previous one
put back after it), the bootstrap-role connection that seeds rows, and a meshychat_app connection
the verifier runs on under `rls.server_internal`, as check_permission is called. Tokens are
built with biscuit_auth in the design's shapes, EVERY term a parameter, never spliced into the
Datalog text:

  authority (2.3)    cap_version(1); issuer(..); subject(..); right(ns, prefix, action)...;
                     depth_cap(n); check if time($t), $t < expiry
  delegation (2.4)   a THIRD-PARTY block signed by the current holder's registered peer key:
                     delegate(peer); delegation_expires(x); check if time($t), $t < x; plus
                     any restriction checks
  restriction (2.4)  a first-party (keyless) block of checks

The issuance register row a mint route would write (slice 5) is inserted by the test, in the
shape sql_meshychat/019 requires. Peers are uuid strings, lowercase and hyphenated (2.3).
"""
from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import json
import os
import uuid

import psycopg

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
ROOT_KID = 7


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Peer:
    def __init__(self, peer_id: str, owner_id: str, keypair, kid: str | None, tag: str):
        self.id, self.owner_id, self.kp, self.kid, self.tag = peer_id, owner_id, keypair, kid, tag

    @property
    def public_bytes(self) -> bytes:
        return bytes(self.kp.public_key.to_bytes())

    def __repr__(self) -> str:
        return f"Peer({self.tag})"


class World:
    def __init__(self, owner, app, store, root_keypair):
        import biscuit_auth
        self.b = biscuit_auth
        self.owner, self.app, self.store, self.root = owner, app, store, root_keypair

    # ------------------------------------------------------------------ rows
    def peer(self, tag: str, *, plane: str = "dev", status: str = "active",
             key: bool = True, trust_group: str | None = None) -> Peer:
        """A peer of an owner of its own. `trust_group`: that dev-plane owner's group (slice 6:
        the memory authority's, so the pipeline's step 5 allows the call by tenancy); by
        default a new group of its own."""
        owner_id, peer_id = str(uuid.uuid4()), str(uuid.uuid4())
        kp = self.b.KeyPair()
        with self.owner.cursor() as cur:
            cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                        "VALUES (%s,%s,%s,%s)",
                        (owner_id, f"cap-{tag}-{owner_id[:8]}@example.test", plane,
                         (str(trust_group) if trust_group else str(uuid.uuid4()))
                         if plane == "dev" else None))
            cur.execute("INSERT INTO peers (id, owner_id, name, status) VALUES (%s,%s,%s,%s)",
                        (peer_id, owner_id, f"cap-{tag}-{peer_id[:8]}", status))
            kid = None
            if key:
                kid = f"k-{uuid.uuid4().hex[:8]}"
                cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                            "VALUES (%s,%s,%s,TRUE)",
                            (peer_id, kid, b64u(bytes(kp.public_key.to_bytes()))))
        return Peer(peer_id, owner_id, kp, kid, tag)

    def claim(self, p: Peer, ns: str | None = None) -> str:
        ns = ns or f"captest/{uuid.uuid4().hex[:12]}"
        plane = self.plane_of(p)
        with self.owner.cursor() as cur:
            cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                        "owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                        "VALUES (%s,%s,%s,%s,FALSE,'keep_all')",
                        (ns, p.id, p.owner_id, plane))
        return ns

    def plane_of(self, p: Peer) -> str:
        with self.owner.cursor() as cur:
            cur.execute("SELECT plane FROM dashboard_users WHERE id = %s", (p.owner_id,))
            return cur.fetchone()[0]

    def grant(self, ns: str, grantee: Peer, granter: Peer, permission: str) -> None:
        with self.owner.cursor() as cur:
            cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                        "grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (ns, grantee.id, grantee.owner_id, permission, granter.id,
                         granter.owner_id))

    def sql(self, statement: str, args=()) -> int:
        with self.owner.cursor() as cur:
            cur.execute(statement, args)
            return cur.rowcount

    # ------------------------------------------------------------------ tokens
    def mint(self, *, issuer: Peer | str, subject: Peer | str, ns: str,
             rights=(("", "read"),), depth_cap: int = 2, ttl: int = 3600,
             cap_version: int | None = 1, extra: str = "", extra_params=None,
             register: bool = True, kid: int | None = ROOT_KID, root=None):
        """A root-signed authority block in 2.3's shape, registered unless told not to."""
        issuer_id = issuer.id if isinstance(issuer, Peer) else issuer
        subject_id = subject.id if isinstance(subject, Peer) else subject
        expires = (utcnow() + _dt.timedelta(seconds=ttl)).replace(microsecond=0)
        params = {"issuer": issuer_id, "subject": subject_id, "ns": ns, "n": depth_cap,
                  "exp": expires}
        lines = []
        if cap_version is not None:
            lines.append("cap_version({v});")
            params["v"] = cap_version
        lines += ["issuer({issuer});", "subject({subject});"]
        for i, (prefix, action) in enumerate(rights):
            lines.append(f"right({{ns}}, {{p{i}}}, {{a{i}}});")
            params[f"p{i}"], params[f"a{i}"] = prefix, action
        lines += ["depth_cap({n});", "check if time($t), $t < {exp};"]
        if extra:
            lines.append(extra)
            params.update(extra_params or {})
        builder = self.b.BiscuitBuilder("\n".join(lines), params)
        if kid is not None:
            builder.set_root_key_id(kid)
        tok = builder.build((root or self.root).private_key)
        if register:
            self.register(tok, issuer=issuer_id, subject=subject_id, ns=ns, rights=rights,
                          depth_cap=depth_cap, expires_at=expires, kid=kid or 0)
        return tok

    def register(self, tok, *, issuer: str, subject: str, ns: str, rights, depth_cap: int,
                 expires_at, kid: int) -> None:
        """The issue row the mint route writes (slice 5), inserted by the test."""
        with self.owner.cursor() as cur:
            cur.execute(
                "INSERT INTO capability_event (event, actor_peer_id, authority_revocation_id, "
                "issuer_peer_id, subject_peer_id, namespace, rights, depth_cap, expires_at, kid)"
                " VALUES ('issue',%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
                (issuer, bytes.fromhex(tok.revocation_ids[0]), issuer, subject, ns,
                 json.dumps([{"prefix": p, "action": a} for p, a in rights]), depth_cap,
                 expires_at, kid))

    def delegate(self, tok, frm: Peer, to: Peer | str, *, ttl: int = 200, checks: str = "",
                 params=None, time_check: bool = True, signer=None):
        """A third-party delegation block signed by `signer` (default: `frm`'s peer key)."""
        to_id = to.id if isinstance(to, Peer) else to
        expires = (utcnow() + _dt.timedelta(seconds=ttl)).replace(microsecond=0)
        code = "delegate({e});\ndelegation_expires({x});\n"
        if time_check:
            code += "check if time($t), $t < {x};\n"
        code += checks
        block = self.b.BlockBuilder(code, {"e": to_id, "x": expires, **(params or {})})
        kp = (signer or frm).kp
        signed = tok.third_party_request().create_block(kp.private_key, block)
        return tok.append_third_party(kp.public_key, signed)

    def restrict(self, tok, code: str, params=None):
        """A first-party (keyless) block: anyone holding the token may append one."""
        return tok.append(self.b.BlockBuilder(code, params or {}))

    def revoke(self, rid_hex: str, by: Peer) -> None:
        with self.owner.cursor() as cur:
            cur.execute("INSERT INTO capability_revocation (revocation_id, revoked_by_peer, "
                        "reason, not_after) VALUES (%s,%s,'test', now() + interval '1 day')",
                        (bytes.fromhex(rid_hex), by.id))

    # ------------------------------------------------------------------ deciding
    def decide(self, tok, caller: Peer | str, ns: str, resource, *, action: str = "read",
               operation: str | None = None, now=None):
        """authorize_capability as check_permission calls it: on the app role, under
        server_internal. ("ALLOW", record) or (code, message)."""
        from apps.core import rls
        from apps.memoryskills import capability
        from apps.memoryskills.errors import HandlerError

        token = tok.to_base64() if hasattr(tok, "to_base64") else tok
        caller_id = caller.id if isinstance(caller, Peer) else caller
        op = operation or {"read": "memory.read", "write": "memory.write"}.get(action, action)
        try:
            with rls.server_internal(self.app):
                record = capability.authorize_capability(
                    self.app, token=token, caller_peer_id=caller_id, namespace=ns,
                    resource=resource, action=action, operation=op, now=now or utcnow())
        except HandlerError as exc:
            return exc.code, exc.message
        return "ALLOW", record

    def check(self, tok, caller: Peer, ns: str, need: str, resource, *, operation=None):
        """check_permission with a principal, under server_internal as a handler asks it."""
        from apps.core import rls
        from apps.memoryskills import capability
        from apps.memoryskills.errors import HandlerError
        from apps.memoryskills.permissions import check_permission

        token = tok.to_base64() if hasattr(tok, "to_base64") else tok
        op = operation or {"read": "memory.read", "write": "memory.write",
                           "admin": "memory.grant"}[need]
        try:
            with rls.server_internal(self.app):
                got = check_permission(ns, caller.id, need, self.app, resource=resource,
                                       principal=capability.PresentedCapability(token, op))
        except HandlerError as exc:
            return exc.code, exc.message
        return "ALLOW", got

    def check_without(self, caller: Peer, ns: str, need: str, resource):
        """check_permission with NO principal: the database grants decide, as today."""
        from apps.core import rls
        from apps.memoryskills.errors import HandlerError
        from apps.memoryskills.permissions import check_permission
        try:
            with rls.server_internal(self.app):
                return "ALLOW", check_permission(ns, caller.id, need, self.app,
                                                 resource=resource)
        except HandlerError as exc:
            return exc.code, exc.message


@contextlib.contextmanager
def world_context():
    """Install a fresh root key store for the test and put the previous one back after."""
    import biscuit_auth
    from apps.core import dbconn
    from apps.memoryskills import capability, capability_keys

    root = biscuit_auth.KeyPair()
    private = bytes(root.private_key.to_bytes())
    store = capability_keys.RootKeyStore(private, ROOT_KID,
                                         {ROOT_KID: bytes(root.public_key.to_bytes())})
    previous = capability_keys.install(store)
    capability.clear_cache()
    owner = psycopg.connect(DSN, autocommit=True)
    app = dbconn.connect(DSN)
    try:
        yield World(owner, app, store, root)
    finally:
        capability_keys.install(previous)
        capability.clear_cache()
        app.close()
        owner.close()
