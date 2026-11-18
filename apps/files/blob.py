"""Storage abstraction: the FS backend, the backend registry, and the one store the
process uses.

The local-filesystem backend and the `from_config` factory are defined here. The
object-storage backend (`MESHYCHAT_BLOB_BACKEND=r2`) is `apps/files/r2.py` (F25, 2026-09-25);
until then it was a declared seam that raised. `register_backend` still replaces either
backend without editing this file.

ONE STORE PER PROCESS (B11, 2026-09-25). `activate(cfg)` builds the configured store at
startup (apps/files/apps.py) and `active()` hands the routes that store and the config it
was built from. A store that cannot be built is a refusal to start, not a 503 on every
request.

## HMAC TOKEN WIRE FORMAT (CANONICAL: signed bytes must remain stable)

  1. payload = {key, op: "PUT"|"GET", exp_unix: int, max_size_bytes: int|absent}
  2. canonicalize with RFC 8785 JCS
  3. tag = HMAC-SHA256(jcs_bytes, token_secret)          # 32 bytes
  4. token = base64url_nopad(jcs_bytes || tag)

`max_size_bytes` is OMITTED (not null) when absent: the wire format fixes the signed
bytes, so a null would produce tokens outside the specified format. Verification splits
the LAST 32 bytes as the tag and compares in constant time before parsing, so a forged
payload is never deserialized.

The secret is the RAW BYTES of the hex decode, not the hex text. Feeding the hex string to
HMAC would produce tokens outside the specified format while signing and verification
still agreed with each other.
"""
from __future__ import annotations

import binascii
import datetime as _dt
import hashlib
import hmac
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path, PurePath

from apps.crypto import primitives as P

from . import config as cfgmod

TOKEN_OP_PUT = "PUT"
TOKEN_OP_GET = "GET"

#: HMAC-SHA256 tag length. The verifier splits on it, so it is named.
TAG_LEN = 32


class BlobError(Exception):
    """Storage error. `kind` distinguishes the one case callers branch on."""

    def __init__(self, message: str, kind: str = "backend"):
        super().__init__(message)
        self.kind = kind


class BlobKeyRejected(BlobError):
    """The key itself was refused: empty, absolute, or escaping blob_dir.

    ITS OWN CLASS BECAUSE THE CALLER MUST BRANCH ON IT, and branching on message text is
    how three of the four rejection classes ended up as 500s. `raw()` routed only the
    message containing "escapes" to a refusal, so a signed token for `C:/Windows/win.ini`
    (rejected as "must be relative") returned 500 rather than a clean 401 - measured.
    A refusal that arrives as an internal error reads as a server bug and gets retried.

    The distinction that matters to a caller is key-vs-backend: a rejected key is the
    holder's fault and never a server fault, so it must never become a 5xx."""

    def __init__(self, message: str):
        super().__init__(message, kind="key_rejected")


class BlobNotFound(BlobError):
    """Missing storage object. `finalize` maps it to 409 E_FILE_NOT_READY while every
    other backend error is 503."""

    def __init__(self, message: str = "not found"):
        super().__init__(message, kind="not_found")


@dataclass(frozen=True)
class ObjectMeta:
    """SHA-256 is deliberately absent: R2's ETag is MD5 for single-part
    uploads, so the hash check lives in the service layer's finalize, not here."""
    size_bytes: int
    etag: str | None
    content_type: str


@dataclass(frozen=True)
class PresignedUrl:
    """`headers` are headers the client MUST send verbatim."""
    url: str
    headers: list[tuple[str, str]]
    expires_at: _dt.datetime


@dataclass(frozen=True)
class TokenPayload:
    key: str
    op: str
    exp_unix: int
    max_size_bytes: int | None = None

    def to_json(self) -> dict:
        body: dict = {"key": self.key, "op": self.op, "exp_unix": self.exp_unix}
        if self.max_size_bytes is not None:      # Omit absent size from signed JSON.
            body["max_size_bytes"] = self.max_size_bytes
        return body


def sign_token(secret: bytes, payload: TokenPayload) -> str:
    """Sign a canonical token payload with HMAC-SHA256."""
    payload_bytes = P.jcs(payload.to_json())
    tag = hmac.new(secret, payload_bytes, hashlib.sha256).digest()
    return P.b64u(payload_bytes + tag)


def verify_token(secret: bytes, token: str) -> TokenPayload:
    """Compare in constant time BEFORE parsing. Expiry is the CALLER's check so the route
    owns the clock and this function stays a pure signature check."""
    try:
        raw = P.b64u_decode(token)
    except (binascii.Error, ValueError) as exc:
        raise BlobError(f"token verify: base64 decode failed: {exc}")
    if len(raw) < TAG_LEN:
        raise BlobError("token verify: encoded value too short to contain HMAC tag")
    payload_bytes, tag = raw[:-TAG_LEN], raw[-TAG_LEN:]
    expected = hmac.new(secret, payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise BlobError("token verify: HMAC mismatch")
    try:
        body = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BlobError(f"token verify: payload parse failed: {exc}")
    if not isinstance(body, dict) or not isinstance(body.get("key"), str) \
            or body.get("op") not in (TOKEN_OP_PUT, TOKEN_OP_GET) \
            or not isinstance(body.get("exp_unix"), int):
        raise BlobError("token verify: payload parse failed: wrong shape")
    max_size = body.get("max_size_bytes")
    if max_size is not None and not isinstance(max_size, int):
        raise BlobError("token verify: payload parse failed: max_size_bytes not an int")
    return TokenPayload(key=body["key"], op=body["op"], exp_unix=body["exp_unix"],
                        max_size_bytes=max_size)


def _now_unix() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


def _from_unix(exp_unix: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(exp_unix, _dt.timezone.utc)


class BlobStore:
    """The methods a backend must supply."""

    def presign_put(self, key: str, content_type: str, max_size_bytes: int,
                    ttl_secs: int) -> PresignedUrl:
        raise NotImplementedError

    def presign_get(self, key: str, ttl_secs: int) -> PresignedUrl:
        raise NotImplementedError

    def get_stream(self, key: str):
        """A context-managed binary file object. Raises `BlobNotFound`."""
        raise NotImplementedError

    def head(self, key: str) -> ObjectMeta:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def health_check(self) -> None:
        """Default reports healthy when an object-store client was constructed with
        credentials at boot. FS overrides this with a filesystem check."""
        return None


class FsBlobStore(BlobStore):
    """Filesystem-backed blob store."""

    def __init__(self, cfg: cfgmod.FsConfig):
        try:
            self.token_secret = bytes.fromhex(cfg.token_secret)
        except ValueError:
            raise BlobError("MESHYCHAT_BLOB_TOKEN_SECRET must be hex-encoded")
        self.blob_dir = Path(cfg.blob_dir)
        self.public_base_url = cfg.public_base_url

    # -- path safety ------------------------------------------------------------

    def safe_path(self, key: str) -> Path:
        """SYNTACTIC only - no canonicalize, because a presign target does
        not exist yet and canonicalize would fail on it. Rejects absolute paths and any
        `..` segment. On Windows a drive prefix (`C:`) is also absolute, and `PurePath`
        classifies it, which is why the check is not a `startswith('/')`."""
        if not key:
            raise BlobKeyRejected("blob key is empty")
        p = PurePath(key)
        if p.is_absolute() or p.drive or p.root:
            raise BlobKeyRejected("blob key must be relative")
        if any(part == ".." for part in p.parts):
            raise BlobKeyRejected("blob key escapes blob_dir")
        return self.blob_dir / p

    def _resolved_existing_path(self, key: str) -> Path:
        """`safe_path` is syntactic, so a SYMLINK under blob_dir could
        still point out of it. Resolve both and re-assert containment. Missing object ->
        `BlobNotFound`, which is what every caller special-cases."""
        path = self.safe_path(key)
        try:
            canon_root = self.blob_dir.resolve(strict=True)
        except OSError as exc:
            raise BlobError(f"blob_dir is not resolvable: {exc}")
        try:
            canon = path.resolve(strict=True)
        except FileNotFoundError:
            raise BlobNotFound()
        except OSError as exc:
            raise BlobError(f"resolve: {exc}")
        if canon_root not in canon.parents and canon != canon_root:
            raise BlobKeyRejected("blob key escapes blob_dir")
        return canon

    # -- BlobStore --------------------------------------------------------------

    def _presign(self, key, op, ttl_secs, headers, max_size_bytes=None) -> PresignedUrl:
        """The sign-and-build-URL body both presigns share. The key is validated BEFORE a
        URL is signed for it. A PUT always signs `int(max_size_bytes)`, coerced after the
        key and the TTL; a GET signs none, and the absent size is OMITTED from the signed
        JCS, not written as null."""
        self.safe_path(key)
        exp_unix = _now_unix() + int(ttl_secs)
        token = sign_token(self.token_secret, TokenPayload(
            key=key, op=op, exp_unix=exp_unix,
            max_size_bytes=int(max_size_bytes) if op == TOKEN_OP_PUT else None))
        return PresignedUrl(
            url=f"{self.public_base_url.rstrip('/')}/v1/files/raw/{token}",
            headers=headers,
            expires_at=_from_unix(exp_unix),
        )

    def presign_put(self, key, content_type, max_size_bytes, ttl_secs) -> PresignedUrl:
        """Presign a write with a content-type request header."""
        return self._presign(key, TOKEN_OP_PUT, ttl_secs,
                             [("Content-Type", content_type)],   # Signed request header.
                             max_size_bytes)

    def presign_get(self, key, ttl_secs) -> PresignedUrl:
        """No size is signed into a GET, and `headers` is EMPTY
        - not `[("Content-Type", ...)]`, which would tell the client to send a request
        header on a read."""
        return self._presign(key, TOKEN_OP_GET, ttl_secs, [])

    def get_stream(self, key: str):
        """Returns an open binary file; the caller closes it."""
        path = self._resolved_existing_path(key)
        try:
            return open(path, "rb")
        except FileNotFoundError:
            raise BlobNotFound()
        except OSError as exc:
            raise BlobError(f"open: {exc}")

    def head(self, key: str) -> ObjectMeta:
        """content_type is MIME-GUESSED from the path and is not authoritative. The service
        layer reads the database row, never this field."""
        path = self._resolved_existing_path(key)
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            raise BlobNotFound()
        except OSError as exc:
            raise BlobError(f"stat: {exc}")
        guessed = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return ObjectMeta(size_bytes=size, etag=None, content_type=guessed)

    def delete(self, key: str) -> None:
        """IDEMPOTENT: a missing object is success (spec 11.1)."""
        try:
            path = self._resolved_existing_path(key)
        except BlobNotFound:
            return None
        try:
            os.remove(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BlobError(f"remove: {exc}")

    def put_bytes(self, key: str, data: bytes) -> None:
        """The write half of `/v1/files/raw/{token}`. It lives on
        the store rather than in the view so the view never builds a filesystem path."""
        path = self.safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def health_check(self) -> None:
        """One stat on the blob root.

        /v1/health ALSO looks for the
        newest ready rows' files (`missing_keys`, called from `meshychat.urls.probe_blob`;
        divergence 047, ASDLC OI-48): that half needs the database, and this store holds
        no connection."""
        if not self.blob_dir.is_dir():
            raise BlobError("blob_dir is not a directory")

    def missing_keys(self, keys) -> list[str]:
        """The keys in `keys` with no regular file under blob_dir, in their order: the files
        half of /v1/health's blob probe (divergence 047, ASDLC OI-48). Production reported
        `blob: true` for six hours while both of its stored files were missing, because
        `health_check` asks only whether the directory exists.

        ONE STAT PER KEY, NO READ: `os.path.isfile(safe_path(key))`. No file is opened, so
        the cost is the number of keys, which the caller caps
        (`meshychat.urls.BLOB_READY_ROWS_CAP`).
          * `isfile`, not `exists`: a DIRECTORY at a key is no more servable than nothing
            (`get_stream` cannot open it).
          * A key `safe_path` refuses is missing: it can never be served.
          * A stat that fails (permissions, a vanished share) is missing too: `isfile`
            answers False on an OSError, so a store that cannot be read fails the check
            rather than passing it.

        NOT CHECKED, by the same bound: a symlink under blob_dir that resolves outside it
        counts as present here, though `_resolved_existing_path` refuses to serve it.
        Resolving every key costs more than a stat (on Windows `resolve` opens each path),
        and nothing this server writes is a link (`put_bytes` writes regular files)."""
        missing = []
        for key in keys:
            try:
                path = self.safe_path(key)
            except BlobKeyRejected:
                missing.append(key)
                continue
            if not os.path.isfile(path):
                missing.append(key)
        return missing


def _r2_backend(cfg: cfgmod.R2Config) -> BlobStore:
    """Imported on use: apps/files/r2.py imports this module for the base class."""
    from .r2 import R2BlobStore
    return R2BlobStore(cfg)


_BACKENDS: dict[str, object] = {
    cfgmod.BACKEND_FS: FsBlobStore,
    cfgmod.BACKEND_R2: _r2_backend,
}


def register_backend(name: str, factory) -> None:
    """Install a backend for `MESHYCHAT_BLOB_BACKEND=<name>`. `factory` takes the matching
    config dataclass and returns a `BlobStore`.

    It changes only stores built AFTER it: the routes' store is built once, in
    apps.files' ready() (`activate`). apps.files is last in INSTALLED_APPS, so a call from
    another app's ready() - which Django runs in list order - reaches it only if that app
    is listed BEFORE apps.files; listed after, the registration is silently too late."""
    _BACKENDS[name] = factory


def from_config(cfg: cfgmod.BlobConfig) -> BlobStore:
    """Dispatches on the configured backend; a config block
    missing for the selected backend is an error, never a default."""
    factory = _BACKENDS.get(cfg.backend)
    if factory is None:
        raise BlobError(f"unknown blob backend: {cfg.backend!r}")
    if cfg.backend == cfgmod.BACKEND_FS:
        if cfg.fs is None:
            raise BlobError("MESHYCHAT_BLOB_BACKEND=fs but FS config block missing")
        return factory(cfg.fs)
    if cfg.backend == cfgmod.BACKEND_R2:
        if cfg.r2 is None:
            raise BlobError("MESHYCHAT_BLOB_BACKEND=r2 but R2 config block missing")
        return factory(cfg.r2)
    return factory(cfg)


#: (config, store) built at startup by `activate`; None when MESHYCHAT_BLOB_BACKEND was unset.
_ACTIVE: tuple[cfgmod.BlobConfig, BlobStore] | None = None


def activate(cfg: cfgmod.BlobConfig | None) -> None:
    """Build the store for `cfg` and make it THE store the routes use. None means no blob
    backend (MESHYCHAT_BLOB_BACKEND unset at startup). Raises `BlobError` when the store cannot
    be built - the caller at startup turns that into a refusal to start."""
    global _ACTIVE
    _ACTIVE = None if cfg is None else (cfg, from_config(cfg))


def active() -> tuple[cfgmod.BlobConfig, BlobStore]:
    """The (config, store) built at startup. Raises `BlobError` when there is none, which
    the routes answer as 503 E_BLOB_BACKEND_UNAVAILABLE (the detail is logged, never
    sent)."""
    if _ACTIVE is None:
        raise BlobError("no blob backend: MESHYCHAT_BLOB_BACKEND was unset at startup")
    return _ACTIVE


__all__ = [
    "BlobError", "BlobNotFound", "BlobStore", "FsBlobStore", "ObjectMeta",
    "PresignedUrl", "TokenPayload", "TOKEN_OP_GET", "TOKEN_OP_PUT", "activate", "active",
    "from_config", "register_backend", "sign_token", "verify_token",
]
