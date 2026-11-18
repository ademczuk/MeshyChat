"""The write half of the board: declare an act as cheaply as sending a message.

WHY THIS EXISTS. The read half (`projection.py`) is correct on constructed ground truth
and answers nothing on the live mesh, because almost nobody declares. Re-measured on the
300 most recent production rows on 2026-09-21 (28 hours, 10 authors): 3 rows carried a
declared act, 85 had opted into schema v1, and 208 carried an anchor a reader could point
at anyway (175 a commit sha, 68 a PR or issue number, 71 a repository path). The raw
material is in the traffic; the DECLARATION is not, because the correct path costs more
than the incorrect one. On the fleet's sender a declared act is four environment
variables typed by hand on every send; on this backend there was no write door at all.

So this module makes the correct path the cheap one, and it does so WITHOUT inventing a
second write semantics. A declared act is an ordinary `memory.write` of an ordinary mesh
message v1 row, signed by the actor, going through `apps.memoryskills.handlers.handle_write`
with every gate that handler already has (default-deny grant, signature verification,
replay dedup, the I2 plane ban, append-only chain). What is added is:

  * a BUILDER that produces the whole row from three fields, and RESTATES the act in
    prose in the body, because the board spec says an attribution carried only in a
    structured field is absent to a reader whose reading is the prose;
  * a GATE in front of the handler that refuses a bare message on this door and names
    the missing field, so the act door cannot be used to quietly file messages;
  * `ref` derived from the checkout the actor is standing in, or refused. Never guessed.

WHAT IS DELIBERATELY NOT HERE. The server does not sign on the actor's behalf and does
not fill in `subject`, `type` or `ref` from the body. An act the actor did not declare is
an inference, and an inference filed as a declaration is the exact failure the board was
built to remove (nine `reviewed` edges attributed to the seat REPORTING a review). If an
inferred layer is ever added it goes in as a distinct verb class by a distinct actor, and
the projection renders it as such; see `derive.py` when it exists.

THE ACTOR IS THE AUTHENTICATED CALLER. `handlers.dispatch` binds `payload.caller_peer_id`
to the authenticated peer and `handle_write` verifies the signature against that peer's
registered key. `value['from']`, the author label and the sender label in the key are
each bound to that caller by `handle_write` (divergence 028): they must be one of the labels
the mesh routes answer the caller for, so `--label` names one of them, never another seat.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import secrets
import subprocess
import uuid

from apps.calls.envelope import KIND_SKILL_CALL, check_claims
from apps.calls.errors import SignatureFailed
from apps.crypto import primitives as P
from apps.crypto.ijson import untextable_string_path
from apps.memoryskills import handlers
from apps.memoryskills.errors import HandlerError
from apps.memoryskills.validation import (
    MESH_CHAT_NAMESPACE,
    MESH_MESSAGE_V1,
    validate_mesh_key_sender,
    validate_mesh_key_ts,
    validate_mesh_message_v1,
)

#: The skill an act is written through. There is no `board.declare` skill on purpose.
SKILL = "memory.write"
DEFAULT_THREAD = "board"
DEFAULT_TO = ("all",)
DEFAULT_AUTHOR_FAMILY = "claude"
#: The value of `--ref` that means "the HEAD of the checkout I am standing in".
REF_AUTO = "auto"


class NotADeclaredAct(HandlerError):
    """This door is for acts. The message names what is missing and where it goes."""

    def __init__(self, message: str):
        super().__init__("E_MESH_SCHEMA", message)


class NoCheckout(Exception):
    """`ref=auto` was asked for outside a git checkout. Refused rather than defaulted:
    a version that came from anywhere other than the checkout the actor is standing in
    is a guess, and a guessed version is what the field exists to remove."""


# --------------------------------------------------------------------------- #
# Building the row
# --------------------------------------------------------------------------- #

def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def mint_key(sender: str, *, now_ms: int | None = None, nonce: str | None = None) -> str:
    """`msg/<epoch-ms>-<sender>-<nonce>`, the reference client's key shape. The sender is
    REQUIRED: the write seam refuses a mesh key that does not embed its value.from
    (divergence 028 R3), and the old `msg/<ms>-<nonce>` embedded none. Checked against the
    same epoch bound the seam checks (`validate_mesh_key_ts`), so a key this mints cannot be
    one the daemon then refuses after the act was composed."""
    ms = int(now_utc().timestamp() * 1000) if now_ms is None else int(now_ms)
    key = f"msg/{ms}-{sender}-{nonce or secrets.token_hex(4)}"
    validate_mesh_key_ts(key)
    return key


def claimed_ts(now: _dt.datetime | None = None) -> str:
    """The actor's own clock in the one shape MESH_TIMESTAMP_RE admits. Reported by the
    projection beside the server clock, never used for ordering."""
    return (now or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def restate(actor_label: str, verb: str, subject: str, ref: str) -> str:
    """The prose restatement of the act. This is the body when the actor gives none."""
    return f"{actor_label} {verb} {subject} at {ref}"


def act_value(*, subject: str, verb: str, ref: str, actor_label: str,
              body: str | None = None, to=DEFAULT_TO, cc=(), thread_id: str = DEFAULT_THREAD,
              author_family: str = DEFAULT_AUTHOR_FAMILY, ts: str | None = None,
              in_reply_to: str | None = None) -> dict:
    """A complete mesh message v1 value carrying the act.

    Every field the validator requires is supplied here so the actor supplies three.
    `body` defaults to the prose restatement; an actor who writes their own body keeps
    it, and the structured fields still carry the act.
    """
    value = {
        "schema_version": MESH_MESSAGE_V1,
        "to": list(to),
        "from": actor_label,
        "type": verb,
        "thread_id": thread_id,
        "ts": ts or claimed_ts(),
        "body": body if body is not None else restate(actor_label, verb, subject, ref),
        "author_family": author_family,
        "subject": subject,
        "ref": ref,
    }
    if cc:
        value["cc"] = list(cc)
    if in_reply_to:
        value["in_reply_to"] = in_reply_to
    return value


def write_args(*, key: str, value: dict, actor_label: str) -> dict:
    """The `memory.write` args for one act, validated HERE with the daemon's own
    validator so a refusal happens before a key is spent and a signature is made.
    `expected_head_version` 0 is the no-clobber guard: a minted key must be new."""
    validate_mesh_message_v1(MESH_CHAT_NAMESPACE, key, value)
    validate_mesh_key_sender(MESH_CHAT_NAMESPACE, key, value)
    return {
        "namespace": MESH_CHAT_NAMESPACE,
        "key": key,
        "value": value,
        "author": actor_label,
        "expected_head_version": 0,
    }


def ref_from_checkout(path: str | None = None) -> str:
    """The full HEAD sha of the checkout at `path` (default: the current directory)."""
    try:
        out = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=path or None,
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NoCheckout(f"git could not run to resolve HEAD: {exc}")
    if out.returncode != 0:
        where = path or os.getcwd()
        raise NoCheckout(
            f"{where} is not inside a git checkout with a HEAD, so ref={REF_AUTO!r} has "
            f"nothing to point at. Pass --ref explicitly. git said: {out.stderr.strip()!r}")
    return out.stdout.strip()


def resolve_ref(ref: str, *, checkout: str | None = None) -> str:
    return ref_from_checkout(checkout) if ref == REF_AUTO else ref


# --------------------------------------------------------------------------- #
# The gate in front of the handler
# --------------------------------------------------------------------------- #

def require_declared_act(args: dict) -> tuple[str, str, str]:
    """Refuse anything on this door that is not a declared act, naming what is missing.

    The validator downstream treats `subject` and `ref` as OPTIONAL, because on the mesh
    a message without them is legal. On the board's door they are the point. A row that
    passed the validator without them would land as a message and the caller would read
    the 201 as a declaration that never happened.
    """
    ns = args.get("namespace")
    if ns != MESH_CHAT_NAMESPACE:
        raise NotADeclaredAct(
            f"an act is declared in {MESH_CHAT_NAMESPACE!r}; the envelope names {ns!r}")
    key = args.get("key")
    if not isinstance(key, str) or not key.startswith("msg/"):
        raise NotADeclaredAct("key must be a mesh key beginning 'msg/' (mint one with mint_key)")
    value = args.get("value")
    if not isinstance(value, dict):
        raise NotADeclaredAct("value must be an object")
    sv = value.get("schema_version")
    if type(sv) is not int or sv != MESH_MESSAGE_V1:
        raise NotADeclaredAct(
            "a declared act is a mesh message v1 (schema_version 1). Without it the daemon "
            "stores subject and ref UNCHECKED and a later reader cannot tell a validated "
            "act from an unvalidated one")
    missing = [f for f in ("subject", "type", "ref")
               if not isinstance(value.get(f), str) or not value.get(f)]
    if missing:
        raise NotADeclaredAct(
            "a declared act carries subject (the object, e.g. pr/933), type (the verb, e.g. "
            "reviewed) and ref (the version it was performed at, e.g. a commit sha); "
            f"missing: {', '.join(missing)}. Build one with act_value(...)")
    return value["subject"], value["type"], value["ref"]


# --------------------------------------------------------------------------- #
# Identity and signing (client side)
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class Identity:
    """What an actor needs to sign: its peer id, the kid of an ACTIVE key, and the seed.

    Reads the fleet's `.agentmeshy-<node>-state.json` shape unchanged, so a seat that can
    already send on the mesh can declare here with the file it already has.
    """

    peer_id: str
    kid: str
    seed: bytes
    label: str | None = None

    @classmethod
    def from_state_file(cls, path: str, *, label: str | None = None) -> "Identity":
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        try:
            seed = P.b64u_decode(d["signing_key_b64url"])
            peer_id, kid = str(d["peer_id"]), str(d["kid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: not a state file with peer_id, kid and "
                             f"signing_key_b64url: {exc}")
        if len(seed) != 32:
            raise ValueError(f"{path}: signing_key_b64url is not a 32-byte seed")
        return cls(peer_id=peer_id, kid=kid, seed=seed, label=label)

    @property
    def fallback_label(self) -> str:
        return self.label or f"peer-{self.peer_id[:8]}"


def signed_envelope(identity: Identity, args: dict, *, call_id: str | None = None,
                    target_peer_id: str | None = None, skill: str = SKILL) -> dict:
    """A `skill.call` payload over `args`, signed over its JCS bytes.

    Exactly the envelope `handle_write` verifies and stores verbatim as provenance
    (MUST-8). `verified_at` is NOT set here: it is the server's attestation of when it
    verified, and a client cannot make it.
    """
    payload = {
        "v": 1,
        "kind": "skill.call",
        "call_id": call_id or f"call-{uuid.uuid4()}",
        "caller_peer_id": identity.peer_id,
        "target_peer_id": target_peer_id or identity.peer_id,
        "skill_name": skill,
        "skill_version": "1",
        "args": args,
        "args_attachments": [],
        "issued_at": now_utc().isoformat().replace("+00:00", "Z"),
    }
    return {
        "payload": payload,
        "signature": P.b64u(P.sign(identity.seed, P.jcs(payload))),
        "signing_kid": identity.kid,
    }


# --------------------------------------------------------------------------- #
# Declaring (server side)
# --------------------------------------------------------------------------- #

def declare(conn, envelope: dict, caller_peer_id) -> dict:
    """Write one declared act through `memory.write`, gate first.

    `conn` MUST be the base serving connection with no transaction open:
    `handle_write` enters `rls.server_internal` itself, and that refuses an open
    transaction. Wrapping this call in `request_server_internal` is the mistake to make
    here, and it fails loudly rather than silently, which is the right direction.

    Anything on the envelope other than payload, signature and signing_kid is DROPPED.
    In particular a client-supplied `verified_at` is not carried: the handler stamps its
    own when the field is absent, and that stamp must be the server's.

    THE ENVELOPE CHECKS FIRST (divergence 038, design B02 Q9). Checks 1-4 of
    `verify_payload` (version, kind, the 120 s window, the payload naming this caller) run
    here, before the act gate and before dispatch, so the HTTP door and the in-process path
    (`declare_in_process`, which builds a complete payload itself) share one check. They
    raise `apps.calls.errors.SignatureFailed` with invoke's code and status. Checks 5-7
    (the key and the signature) are `handle_write`'s, as before. No nonce ledger: see 038.

    A PAYLOAD THAT NAMES NO SKILL is refused here too, 400 E_SIG_BAD as malformed, as a
    missing caller is. The binding in `dispatch` compares `skill_name` only when present
    (a wrong one is its 400 E_BAD_ENVELOPE), and since A1 (divergence 039) the database's
    agree-trigger requires `memory.write`, so an act naming none reached it and was an
    untyped 500 E_INTERNAL. The door has no call to take the skill from, so the payload
    must name it.
    """
    if not isinstance(envelope, dict):
        raise HandlerError("E_BAD_REQUEST", "envelope must be an object")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise HandlerError("E_BAD_REQUEST", "envelope.payload missing or not an object")
    check_claims(payload, caller_peer_id, KIND_SKILL_CALL)
    if payload.get("skill_name") is None:
        raise SignatureFailed.malformed("payload.skill_name missing")
    args = payload.get("args")
    if not isinstance(args, dict):
        raise HandlerError("E_BAD_REQUEST", "envelope.payload.args missing or not an object")
    # #252 (memory-bxxxxx's #275 review): this door dispatches in process, so the call
    # pipeline's step 1a never sees it. A validly signed U+0000 in free text, or a kid holding
    # U+0000 or a lone surrogate (a Postgres parameter), answered 500. Neither is echoed.
    path = untextable_string_path(payload, "$.payload")
    if path is not None:
        raise HandlerError("E_BAD_REQUEST", f"{path} holds U+0000 or a lone surrogate, "
                           "which no stored copy can carry (I-JSON, RFC 7493 section 2.1)")
    kid = envelope.get("signing_kid")
    if kid is not None and not isinstance(kid, str):
        # #277: a non-string kid reached the key lookup and answered 500; the invoke route
        # refuses the same input with 422 in envelope_from.
        raise HandlerError("E_BAD_REQUEST", "envelope.signing_kid must be a string")
    if isinstance(kid, str) and untextable_string_path(kid) is not None:
        raise HandlerError("E_BAD_REQUEST",
                           "envelope.signing_kid must not hold U+0000 or a lone surrogate")
    subject, verb, ref = require_declared_act(args)
    verified = {
        "payload": payload,
        "signature": envelope.get("signature"),
        "signing_kid": envelope.get("signing_kid"),
    }
    result = handlers.dispatch(SKILL, verified, caller_peer_id, conn)
    return {"subject": subject, "verb": verb, "ref": ref, **result}


def declare_in_process(conn, identity: Identity, *, subject: str, verb: str, ref: str,
                       body: str | None = None, to=DEFAULT_TO, cc=(),
                       thread_id: str = DEFAULT_THREAD, actor_label: str | None = None,
                       checkout: str | None = None, in_reply_to: str | None = None) -> dict:
    """The whole cheap path in one call: mint, build, sign, gate, write.

    Three required fields. `ref` may be `auto`, resolved against `checkout` (default:
    the current directory) and refused outside a repository. `in_reply_to` names the row
    this act is about, which is how an inferred act (`derive.py`) points at its source.
    """
    label = (actor_label or identity.label or handlers.roster_label_of(identity.peer_id)
             or identity.fallback_label)
    resolved = resolve_ref(ref, checkout=checkout)
    key = mint_key(label)
    value = act_value(subject=subject, verb=verb, ref=resolved, actor_label=label,
                      body=body, to=to, cc=cc, thread_id=thread_id, in_reply_to=in_reply_to)
    args = write_args(key=key, value=value, actor_label=label)
    return declare(conn, signed_envelope(identity, args), identity.peer_id)
