"""The invocation doorbell's signing key: one dedicated Ed25519 key per host (divergence 041 S9).

Design `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6) 4.11 "Signing",
the approved Q3, critique F3.

NOT THE TOKEN KEY. `result_signing.server_result_signer()` signs with the key that mints peer
tokens (`get_config().jwt_signing_key_pem`), and the one JWT verifier checks neither `typ` nor
`aud` (apps/authapi/tokens.py): under that key a doorbell would be a valid token that only
the extractors' `aud == "peer"` rule refuses. Under a key of its own, no verifier configured
with the token key accepts it at all. So this module holds a separate key, and REFUSES one
whose public half is the token key's (current, signing or history): a key file pointed at the
token key by mistake stops the doorbells instead of signing them with it.

CUSTODY (4.11). The key lives in the file `settings.MESHY_INVOKE_WEBHOOK_KEY_PATH` names, as
a PKCS#8 PEM, on each host. `run_call_worker` creates it at its first start (`ensure`): the
PEM is written to a temporary file in the same directory and published by `os.link`, which
fails when the name exists, so the create is exclusive AND a racing reader never sees a
half-written file. It is never overwritten: an existing file is loaded, and one that is not
an Ed25519 private key refuses (`WebhookKeyError`) and is left as it is. It is never copied
between hosts, and it is rotated by deleting it: the next start creates a new one. A doorbell
lives 120 s and a receiver resolves the kid from the doorbell JWKS, so no key has to survive
a host move, and nothing is added to the secrets copied at a cutover.

UNSET, DOORBELLS ARE OFF. With no path configured the worker runs the deadline sweep alone,
nothing is verified or rung, and the doorbell JWKS is empty. Setting the path is
how S9 is enabled on a host (5.5: production counts are re-measured before that).

PUBLISHED ALONE by `/.well-known/meshy-invoke-jwks.json` (JWKS_PATH;
apps/authapi/views.invoke_webhook_jwks), never by the token key set `/.well-known/jwks.json`
(operator decision 2026-09-25, divergence 041's dated paragraph: S9 published it there as a
second entry), under its own server-rule kid (16 lowercase hex of sha256(raw)[..8],
`primitives.server_kid`), read from the same file on every request, so a rotation is published
as soon as the file changes. The serving process reads the private key file to derive the public half; it
already holds the token private key, so this widens nothing it can read.

THE JWS is assembled here, as `tokens.sign` assembles a token (no PyJWT under apps/ but the one
verifier): header `{"alg":"EdDSA","kid":...,"typ":"meshy-invoke+jwt"}` in that order, the
claims as given, compact serialization.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.crypto import primitives as P

log = logging.getLogger("meshychat.calls.webhook_key")

#: The setting (and the environment variable meshychat/settings.py reads it from).
SETTING = "MESHY_INVOKE_WEBHOOK_KEY_PATH"
ALGORITHM = "EdDSA"
#: The JWS `typ`, so a receiver can tell a doorbell from any other JWT this server signs.
TYP = "meshy-invoke+jwt"
#: A doorbell's lifetime: exp = iat + 120 (4.11).
TTL_SECS = 120
#: Where the key is published, alone (operator decision 2026-09-25): never the token JWKS.
JWKS_PATH = "/.well-known/meshy-invoke-jwks.json"


class WebhookKeyError(RuntimeError):
    """The webhook key cannot be used: absent, unreadable, not an Ed25519 private key, or the
    token key. Doorbells stop; nothing is signed with anything else."""


@dataclasses.dataclass(frozen=True)
class WebhookKey:
    private_key: Ed25519PrivateKey
    #: The server kid rule, derived from the key itself.
    kid: str

    def public_raw(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)

    def public_pem(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign_jwt(self, claims: dict) -> str:
        """The compact JWS of `claims` under this key, header alg, kid, typ."""
        header = json.dumps({"alg": ALGORITHM, "kid": self.kid, "typ": TYP},
                            separators=(",", ":")).encode("ascii")
        payload = json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        signing_input = f"{P.b64u(header)}.{P.b64u(payload)}".encode("ascii")
        return f"{signing_input.decode('ascii')}.{P.b64u(self.private_key.sign(signing_input))}"


def _of(private_key: Ed25519PrivateKey) -> WebhookKey:
    raw = private_key.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                                format=serialization.PublicFormat.Raw)
    return WebhookKey(private_key=private_key, kid=P.server_kid(raw))


def _parse(pem: bytes, path: Path) -> WebhookKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:  # noqa: BLE001 - any parse failure is one refusal
        raise WebhookKeyError(f"{path} is not a PKCS#8 PEM private key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise WebhookKeyError(f"{path} holds a {type(key).__name__}, not an Ed25519 key")
    return _of(key)


def load(path) -> WebhookKey:
    """The key the file holds. Raises WebhookKeyError when it is absent or not a key."""
    path = Path(path)
    try:
        pem = path.read_bytes()
    except FileNotFoundError:
        raise WebhookKeyError(f"no webhook key at {path}; run_call_worker creates it at its "
                              f"first start") from None
    except OSError as exc:
        raise WebhookKeyError(f"cannot read the webhook key at {path}: {exc}") from exc
    return _parse(pem, path)


def ensure(path) -> WebhookKey:
    """Load the key at `path`, or create it there if the name is free: never overwritten.

    The new PEM is written to a temporary file beside it and published with `os.link`, which
    refuses an existing name: of several first starts racing, one publishes and the others
    load what it published, whole. The directory must exist."""
    path = Path(path)
    if path.exists():
        return load(path)
    if not path.parent.is_dir():
        raise WebhookKeyError(f"the directory of {path} does not exist")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(encoding=serialization.Encoding.PEM,
                            format=serialization.PrivateFormat.PKCS8,
                            encryption_algorithm=serialization.NoEncryption())
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(pem)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return load(path)
        log.warning("created the invocation webhook key %s (kid %s)", path, _of(key).kid)
        return _of(key)
    except OSError as exc:
        raise WebhookKeyError(f"cannot create the webhook key at {path}: {exc}") from exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def configured_path() -> Path | None:
    """The ONE reader of settings.MESHY_INVOKE_WEBHOOK_KEY_PATH. None: doorbells are off."""
    from django.conf import settings
    value = getattr(settings, SETTING, None)
    if value is None or not str(value).strip():
        return None
    return Path(str(value).strip())


def _token_public_raws() -> list[bytes]:
    """Every public key the token side uses: the verifiers' setting, and the minter's signing
    key, current public key and history."""
    from django.conf import settings

    from apps.authapi import tokens
    from apps.authapi.config import get_config

    raws: list[bytes] = []
    pems: list[bytes] = []
    configured = getattr(settings, "MESHY_JWT_PUBLIC_KEY_PEM", None)
    if configured:
        pems.append(configured.encode("ascii") if isinstance(configured, str) else configured)
    cfg = get_config()
    if cfg.jwt_public_key_pem:
        pems.append(cfg.jwt_public_key_pem)
    pems.extend(cfg.jwt_public_key_history_pem or ())
    for pem in pems:
        try:
            raws.append(tokens.public_key_raw(pem))
        except Exception:  # noqa: BLE001 - an unparseable token key cannot equal this one
            continue
    if cfg.jwt_signing_key_pem:
        try:
            signing = tokens.load_signing_key(cfg.jwt_signing_key_pem)
            raws.append(signing.public_key().public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw))
        except Exception:  # noqa: BLE001
            pass
    return raws


def refuse_token_key(key: WebhookKey) -> WebhookKey:
    """F3: the doorbell key is never a token key. Raises when its public half is one."""
    if key.public_raw() in _token_public_raws():
        raise WebhookKeyError(
            "the invocation webhook key is the token key: a doorbell signed with it would be a "
            f"valid token to every verifier but for its aud. Point {SETTING} at a file of its "
            "own (docs/DIVERGENCES.md 041 S9)")
    return key


def ensure_configured() -> WebhookKey | None:
    """run_call_worker's first start: create or load the configured key, never the token key.
    None when no path is configured (doorbells off)."""
    path = configured_path()
    if path is None:
        return None
    return refuse_token_key(ensure(path))


def load_for_signing() -> WebhookKey:
    """The key a pass signs with, read from the file each pass, so what is signed is what the
    JWKS publishes. Raises WebhookKeyError when doorbells are off or the key is unusable."""
    path = configured_path()
    if path is None:
        raise WebhookKeyError(f"{SETTING} is not set: doorbells are off")
    return refuse_token_key(load(path))


def published_public_pem() -> bytes | None:
    """The public PEM JWKS_PATH publishes, or None: no path, no file yet, a file that is not
    a key, or the token key itself (which is never published as a doorbell key). Never
    raises: a broken webhook key must not take the doorbell key set down with a 500."""
    path = configured_path()
    if path is None:
        return None
    try:
        key = refuse_token_key(load(path))
    except WebhookKeyError as exc:
        if path.exists():
            log.warning("the JWKS does not publish the invocation webhook key: %s", exc)
        return None
    return key.public_pem()


__all__ = ["ALGORITHM", "JWKS_PATH", "SETTING", "TTL_SECS", "TYP", "WebhookKey", "WebhookKeyError",
           "configured_path", "ensure", "ensure_configured", "load", "load_for_signing",
           "published_public_pem", "refuse_token_key"]
