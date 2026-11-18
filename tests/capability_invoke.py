"""Test helpers for tokens on the envelope path (B07 slice 6, divergence 040). Not a test module.

`invoke_context()` is one test's setting: a `World` (tests/capability_world.py: a root key store
installed for the test, the bootstrap-role connection that seeds rows, a meshychat_app connection),
a memory authority made by THE COMMAND (`manage.py bootstrap_memory_peer`, as
tests/test_call_routing.py makes it) in a dev trust group every peer made here joins, so the
pipeline's step 5 allows each call by tenancy and the namespace decision is the handler's, and
the settings a call needs: MESHY_CAPABILITIES on, the memory peer id, the JWT verification key
these helpers sign with, the SAME key as the server's result signer, any host. DPoP stays as
the suite runs it (off, tests/conftest.py) unless a test turns it on; the replay ledger is at its
shipped default, ON (divergence 036).

Every request goes through django.test.Client and the PROJECT urlconf and middleware, so the
header middleware, the view's pre-check, the pipeline and the handler are what the test sees.
Every response is kept (`Invoker.seen`), so T14 can scan every body and header for a token.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import io
import json
import uuid

from django.core.management import call_command
from django.test import Client, override_settings

from tests.capability_routes import JWT_ISSUER, JWT_PRIVATE_PEM, JWT_PUBLIC_PEM, Seen, peer_jwt
from tests.capability_world import Peer, World, world_context

#: A payload that names no capability, and one that names the token it is sent with.
OMIT = object()
AUTO = object()

MEMORY_SKILL_VERSION = "0.2.0"


def sha256_hex(text: str) -> str:
    """The hex sha256 of the exact header string (design 3.1), as a client computes it."""
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def seed_of(peer: Peer) -> bytes:
    """A World peer's Ed25519 seed: its biscuit keypair's private half, which also signs its
    envelopes (the key peer_keys holds for it)."""
    return bytes(peer.kp.private_key.to_bytes())


class Invoker:
    def __init__(self, world: World, memory_peer: str, trust_group: str):
        self.w = world
        self.memory_peer = memory_peer
        self.trust_group = trust_group
        self.client = Client()
        self.seen: list[Seen] = []

    # ------------------------------------------------------------------ peers
    def peer(self, tag: str, **kw) -> Peer:
        """A peer in the memory authority's trust group (step 5 allows it by tenancy)."""
        kw.setdefault("trust_group", self.trust_group)
        return self.w.peer(tag, **kw)

    # ------------------------------------------------------------------ envelopes
    def envelope(self, peer: Peer, skill: str, args: dict, *, token=None,
                 capability_sha256=AUTO, target: str | None = None,
                 call_id: str | None = None, extra: dict | None = None) -> dict:
        """A skill.call envelope as a node signs it (tests/test_call_routing.py `signed`),
        with `capability_sha256` the sha256 of `token` when one is given (AUTO), left out
        (OMIT, or AUTO with no token), or any value a test chooses."""
        from apps.crypto import primitives as P
        payload = {
            "v": 1, "kind": "skill.call", "call_id": call_id or str(uuid.uuid4()),
            "caller_peer_id": peer.id, "target_peer_id": str(target or self.memory_peer),
            "skill_name": skill, "skill_version": MEMORY_SKILL_VERSION, "args": args,
            "args_attachments": [],
            "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "nonce": uuid.uuid4().hex,
        }
        if capability_sha256 is AUTO:
            if token is not None:
                payload["capability_sha256"] = sha256_hex(_text(token))
        elif capability_sha256 is not OMIT:
            payload["capability_sha256"] = capability_sha256
        payload.update(extra or {})
        return {"payload": payload,
                "signature": P.b64u(P.sign(seed_of(peer), P.jcs(payload))),
                "signing_kid": peer.kid}

    # ------------------------------------------------------------------ requests
    def invoke(self, peer: Peer, skill: str, envelope: dict, *, token=None,
               headers: dict | None = None, target: str | None = None,
               mode: str = "sync") -> Seen:
        """POST /v1/agents/{target}/skills/{skill}/invoke, as mem_client posts it, with
        `token` in Meshychat-Capability when given. Sync: the terminal body comes back inline."""
        h = {"authorization": f"Bearer {peer_jwt(peer.id)}", **(headers or {})}
        if token is not None:
            h["Meshychat-Capability"] = _text(token)
        body = {"envelope": envelope, "mode_override": mode,
                "call_id": envelope["payload"]["call_id"]}
        path = f"/v1/agents/{target or self.memory_peer}/skills/{skill}/invoke"
        seen = Seen(path, self.client.post(path, data=json.dumps(body),
                                           content_type="application/json", headers=h))
        self.seen.append(seen)
        return seen

    def call(self, peer: Peer, skill: str, args: dict, *, token=None, **kw):
        """Sign and invoke in one: (response, envelope)."""
        sig = {k: kw.pop(k) for k in ("capability_sha256", "call_id", "extra") if k in kw}
        envelope = self.envelope(peer, skill, args, token=token, target=kw.get("target"),
                                 **sig)
        return self.invoke(peer, skill, envelope, token=token, **kw), envelope

    def a2a(self, peer: Peer, skill: str, args: dict, *, token=None,
            target: str | None = None, **sig) -> tuple:
        """POST /a2a/agents/{target}/skills/{skill}/message:send: the same signed call, its
        args carried again as the A2A message's one data part. The target is the memory
        authority unless one is given."""
        envelope = self.envelope(peer, skill, args, token=token, target=target, **sig)
        h = {"authorization": f"Bearer {peer_jwt(peer.id)}"}
        if token is not None:
            h["Meshychat-Capability"] = _text(token)
        body = {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                            "parts": [{"data": args}]},
                "envelope": envelope, "call_id": envelope["payload"]["call_id"]}
        path = f"/a2a/agents/{target or self.memory_peer}/skills/{skill}/message:send"
        seen = Seen(path, self.client.post(path, data=json.dumps(body),
                                           content_type="application/json", headers=h))
        self.seen.append(seen)
        return seen, envelope

    # ------------------------------------------------------------------ rows
    def _rows(self, sql: str, args=()) -> list[dict]:
        with self.w.owner.cursor() as cur:
            cur.execute(sql, args)
            names = [d.name for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def call_row(self, call_id: str) -> dict | None:
        rows = self._rows("SELECT call_id, status, args, result, error_code, error_message, "
                          "result_payload FROM skill_calls WHERE call_id = %s", (call_id,))
        return rows[0] if rows else None

    def nonce_rows(self, envelope: dict) -> list[dict]:
        payload = envelope["payload"]
        return self._rows("SELECT peer_id, nonce FROM envelope_nonce_seen "
                          "WHERE peer_id = %s AND nonce = %s",
                          (payload["caller_peer_id"], payload["nonce"]))

    def memory_rows(self, ns: str, key: str) -> list[dict]:
        return self._rows("SELECT key, version, created_by_peer_id::text AS writer, value, "
                          "provenance_jsonb FROM shared_memory WHERE namespace = %s AND key = %s "
                          "ORDER BY version", (ns, key))


def _text(token) -> str:
    return token if isinstance(token, str) else token.to_base64()


def outcome(seen: Seen) -> tuple:
    """(http status, call status or error code): 200 and the terminal status and error code of
    a call that ran; the HTTP status and error of a request refused before one."""
    body = seen.body if isinstance(seen.body, dict) else {}
    if seen.status == 200 and "status" in body:
        return 200, body["status"], body.get("error_code")
    return seen.status, body.get("error"), None


@contextlib.contextmanager
def invoke_context(**settings):
    """A World, a memory authority, and the settings a signed call needs (module docstring)."""
    from apps.authapi import config as authconfig

    trust_group = str(uuid.uuid4())
    memory_peer = str(uuid.uuid4())
    with world_context() as w, override_settings(
            ALLOWED_HOSTS=["*"], MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
            MESHY_JWT_ISSUER=JWT_ISSUER, MESHY_CAPABILITIES=True,
            MESHY_MEMORY_PEER_ID=memory_peer, MESHY_SYNC_TIMEOUT_SECONDS=0.0,
            **settings), authconfig.override_config(
            jwt_signing_key_pem=JWT_PRIVATE_PEM.encode("ascii"),
            jwt_public_key_pem=JWT_PUBLIC_PEM.encode("ascii"), jwt_issuer=JWT_ISSUER):
        out = io.StringIO()
        call_command("bootstrap_memory_peer", "--trust-group", trust_group,
                     "--owner-email", f"cap6-mem-{memory_peer[:8]}@example.test",
                     stdout=out, stderr=io.StringIO())
        assert "CREATED" in out.getvalue(), out.getvalue()
        yield Invoker(w, memory_peer, trust_group)
