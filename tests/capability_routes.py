"""Test helpers for the mint and revoke routes (B07 slice 5, divergence 040). Not a test module.

`routes_context()` is one test's setting: a `World` (tests/capability_world.py: a root key store
installed for the test, the bootstrap-role connection that seeds rows, a meshychat_app connection the
verifier runs on), with the settings a route needs layered on top by override_settings:
MESHY_CAPABILITIES on, the JWT verification key these helpers sign with, any host. DPoP stays as
the suite runs it (off, tests/conftest.py) unless a test turns it on.

The requests go through django.test.Client and the PROJECT urlconf and middleware, so a route
that is not mounted, or a middleware that refuses the header, is what the test sees. Every
response a test makes is kept (`Routes.seen`), so T14 can scan every body and header for a
token.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import uuid

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.test import Client, override_settings

from tests.capability_world import World, utcnow, world_context

MINT = "/v1/memory/capabilities"
REVOKE = "/v1/memory/capabilities/revoke"
JWT_ISSUER = "meshy-capability-routes-test"

_jwt_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _jwt_key.private_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()).decode("ascii")
JWT_PUBLIC_PEM = _jwt_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


def peer_jwt(peer_id: str, *, act_for: str | None = None, cnf: str | None = None,
             ttl: int = 300) -> str:
    """A real EdDSA peer JWT the route's verifier accepts. `act_for`: the token's `sub` is that
    owner and `act.sub` the peer, the on-behalf-of shape (the acting peer is act.sub)."""
    now = int(utcnow().timestamp())
    claims = {"sub": act_for or peer_id, "aud": "peer", "iss": JWT_ISSUER, "iat": now,
              "exp": now + ttl, "scopes": []}
    if act_for:
        claims["act"] = {"sub": peer_id}
    if cnf:
        claims["cnf"] = cnf
    return pyjwt.encode(claims, JWT_PRIVATE_PEM, algorithm="EdDSA")


def iso(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Seen:
    """One response, as the client received it."""

    def __init__(self, path: str, response):
        self.path = path
        self.status = response.status_code
        self.raw = response.content.decode("utf-8", "replace")
        try:
            self.body = json.loads(self.raw) if self.raw else None
        except ValueError:
            self.body = None
        self.headers = dict(response.items())

    @property
    def code(self):
        return self.body.get("error") if isinstance(self.body, dict) else None

    def outcome(self):
        return self.status, self.code

    def text(self) -> str:
        """The body and every header, for a scan."""
        return self.raw + "\n" + "\n".join(f"{k}: {v}" for k, v in self.headers.items())

    def __repr__(self) -> str:
        return f"Seen({self.path} {self.status} {self.raw[:300]!r})"


class Routes:
    def __init__(self, world: World):
        self.w = world
        self.client = Client()
        self.seen: list[Seen] = []

    # ------------------------------------------------------------------ requests
    def post(self, path: str, body, *, caller=None, headers=None, raw=None,
             content_type="application/json", token=None) -> Seen:
        h = dict(headers or {})
        if caller is not None and "authorization" not in {k.lower() for k in h}:
            h["authorization"] = f"Bearer {peer_jwt(caller.id)}"
        if token is not None:
            h["Meshychat-Capability"] = token
        data = raw if raw is not None else json.dumps(body)
        seen = Seen(path, self.client.post(path, data=data, content_type=content_type,
                                           headers=h))
        self.seen.append(seen)
        return seen

    def mint_body(self, *, subject, ns, rights=(("", "read"),), depth_cap=2, ttl=3600,
                  expires_at=None) -> dict:
        return {"subject": subject if isinstance(subject, str) else subject.id,
                "namespace": ns,
                "rights": [{"prefix": p, "action": a} for p, a in rights],
                "depth_cap": depth_cap,
                "expires_at": expires_at if expires_at is not None
                else iso(utcnow() + _dt.timedelta(seconds=ttl))}

    def mint(self, caller, **fields) -> Seen:
        body = fields.pop("body", None)
        headers = fields.pop("headers", None)
        return self.post(MINT, body if body is not None else self.mint_body(**fields),
                         caller=caller, headers=headers)

    def minted(self, caller, **fields):
        """Mint, require 201, and return (token string, parsed Biscuit, response)."""
        got = self.mint(caller, **fields)
        assert got.status == 201, got
        token = got.body["token"]
        return token, self.parse(token), got

    def parse(self, token: str):
        return self.w.b.Biscuit.from_base64(token, self.w.store.provider)

    def revoke(self, caller, *, token=None, block=None, authority_id=None,
               reason="rotating the task's access", body=None, headers=None) -> Seen:
        if body is None:
            body = {"reason": reason}
            if block is not None:
                body["block"] = block
            if authority_id is not None:
                body["authority_id"] = authority_id
        if token is not None and not isinstance(token, str):
            token = token.to_base64()
        return self.post(REVOKE, body, caller=caller, headers=headers, token=token)

    # ------------------------------------------------------------------ rows
    def issue_rows(self, authority_id_hex: str | None = None, *, ns=None) -> list[dict]:
        """Issue rows by authority id or by namespace. The table is shared by every test of the
        run, so an unscoped read would see other tests' rows: it is refused."""
        where, args = ["event = 'issue'"], []
        if authority_id_hex is not None:
            where.append("authority_revocation_id = %s")
            args.append(bytes.fromhex(authority_id_hex))
        if ns is not None:
            where.append("namespace = ANY(%s)")
            args.append([ns] if isinstance(ns, str) else list(ns))
        if len(where) == 1:
            raise TypeError("scope the read by authority id or namespace")
        return self._rows(f"SELECT * FROM capability_event WHERE {' AND '.join(where)} "
                          "ORDER BY id", args)

    def events(self, authority_id_hex: str) -> list[dict]:
        return self._rows("SELECT * FROM capability_event WHERE authority_revocation_id = %s "
                          "ORDER BY id", (bytes.fromhex(authority_id_hex),))

    def revocations(self, revocation_id_hex: str | None = None, *, by=None) -> list[dict]:
        """Revocation rows by id or by revoker (a peer or peers); never unscoped (above)."""
        if revocation_id_hex is not None:
            return self._rows("SELECT * FROM capability_revocation WHERE revocation_id = %s",
                              (bytes.fromhex(revocation_id_hex),))
        if by is None:
            raise TypeError("scope the read by revocation id or revoker")
        peers = [by] if not isinstance(by, (list, tuple)) else list(by)
        return self._rows("SELECT * FROM capability_revocation WHERE revoked_by_peer = "
                          "ANY(%s::uuid[]) ORDER BY revoked_at",
                          ([p.id for p in peers],))

    def _rows(self, sql: str, args=()) -> list[dict]:
        with self.w.owner.cursor() as cur:
            cur.execute(sql, args)
            names = [d.name for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def audits(self, peer) -> list[tuple]:
        with self.w.owner.cursor() as cur:
            cur.execute("SELECT denial_class, op, caller_owner_id::text, caller_peer_id::text, "
                        "target_owner_id::text, namespace FROM tenancy_denials "
                        "WHERE caller_peer_id = %s", (peer.id,))
            return cur.fetchall()


@contextlib.contextmanager
def routes_context(**settings):
    with world_context() as w, override_settings(
            ALLOWED_HOSTS=["*"], MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
            MESHY_JWT_ISSUER=JWT_ISSUER, MESHY_CAPABILITIES=True, **settings):
        yield Routes(w)


def random_peer_id() -> str:
    return str(uuid.uuid4())


def admin_on(world, peer, namespace: str) -> str:
    """`peer` holds admin on `namespace`, a namespace the whole run shares (shared/wiki): the
    claim when nobody holds it yet, else an admin GRANT from whoever does, seeded as the
    database owner, as tests/test_mesh_authorship.py `_admit` does. The holder may be a peer
    whose seed this process never had, and test_memory_skills re-claims shared/wiki for itself,
    so nothing here deletes or assumes a prior claim."""
    with world.owner.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace "
                    "WHERE namespace = %s", (namespace,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                        "owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                        "VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                        (namespace, peer.id, peer.owner_id))
        elif str(row[0]) != peer.id:
            cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                        "grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                        "VALUES (%s,%s,%s,'admin',%s,%s)",
                        (namespace, peer.id, peer.owner_id, row[0], row[1]))
    return namespace


def b64u_key(peer) -> str:
    """A World peer's public key as peer_keys.public_key holds it (base64url, no padding)."""
    from tests.capability_world import b64u
    return b64u(peer.public_bytes)


def needles(token: str) -> dict[str, str]:
    """What a scan for `token` looks for: the whole token, and two 24-character windows of its
    TAIL, the last block's signature and the proof (the next block's private key), which are
    random per token. NOT its head or its middle: a biscuit's first bytes are a protobuf header
    every token minted under the same key shares (measured: every token this route mints begins
    `CAcS6QIK_gEKC2Nh`), and two mints of the same request sign the same authority block
    bytes, so a window from either finds ANOTHER token's text (measured: a middle window of one
    token was in the second mint's response)."""
    return {"token": token, "signature 24": token[-72:-48], "last 24": token[-24:]}


def scan(token: str, places: dict[str, str]) -> dict[str, list[str]]:
    """{where: [which needles]} for every place any needle of `token` occurs."""
    found = {}
    for where, text in places.items():
        hits = [name for name, needle in needles(token).items() if needle in text]
        if hits:
            found[where] = hits
    return found
