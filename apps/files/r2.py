"""The object-storage backend: Cloudflare R2 through its S3-compatible API (F25).

This backend uses region "auto", static credentials, and `endpoint_url` =
MESHYCHAT_R2_ENDPOINT or `https://{account_id}.r2.cloudflarestorage.com`. It implements
AWS Signature Version 4 with the standard library instead of an AWS SDK; the signer is
small enough to check line by line against AWS's published examples
(tests/test_files_object_store.py reproduces two of them exactly).

BEHAVIOR
  * presign_put signs `content-type` and `content-length` (= the declared size) and hands
    both to the client to send verbatim; presign_get signs `host` only and hands back no
    headers. Both refuse a lifetime above one week with the message "presign failed".
  * HEAD 404 and GET NoSuchKey are `BlobNotFound`; every other failure is a `BlobError`
    with the constant message ("head failed", "object read failed", "delete
    failed"); the detail goes to the log, never to a caller. DELETE of a missing key is
    success (S3 answers 204).
  * Addressing follows S3 endpoint rules: a DNS-compatible bucket is a
    sub-domain of the endpoint (virtual-hosted); an IP-address endpoint, or a bucket that
    is not a DNS label, is path-style.
  * health_check treats a constructed store as healthy.
  * A redirect is an error. urllib's default opener follows a 3xx,
    re-sending the signed Authorization header to whatever host the Location names and
    reading that host's answer as the object (review of c007c45). The opener here refuses
    every redirect, so a 3xx is an HTTPError and then the operation's BlobError.

DIFFERENCES FROM THE AWS SDK (docs/DIVERGENCES.md 044)
  * No retries. The AWS SDK's standard retry mode retries throttling and transient 5xx up to
    three attempts; here one attempt, and a failure is the caller's 503.
  * The presigned URL carries only the SigV4 parameters. The AWS SDK adds `x-id=PutObject` /
    `x-id=GetObject`, which R2 ignores; the URL bytes differ, the request does not.
  * Header names in `upload_headers` are lower-case and sorted; their order is not a
    contract either side relies on.
  * One read timeout (60 s) on every store request; the AWS SDK sets none by default.

NOT PROVED: that R2 itself accepts these requests. No bucket or credentials exist for this
project, so the backend is proved against AWS's examples and a local S3-shaped server
that verifies every signature. Pointing it at a real bucket is the operator step.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import ipaddress
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import config as cfgmod
from .blob import BlobError, BlobNotFound, BlobStore, ObjectMeta, PresignedUrl

log = logging.getLogger("apps.files.r2")

REGION = "auto"                         # R2 uses the auto region.
SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"   # what a presigned URL signs for its body
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_PRESIGN_SECS = 7 * 24 * 3600        # S3 limits a presigned lifetime to 7 days.
REQUEST_TIMEOUT_SECS = 60

_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")
_DNS_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")


# ------------------------------------------------------------------- SigV4

def _encode(text: str, keep_slash: bool) -> str:
    """RFC 3986 percent-encoding over the UTF-8 BYTES, upper-case hex, the unreserved set
    left alone - SigV4's UriEncode. `str.isalnum()` is deliberately not used: it is true
    for non-ASCII letters, which must be encoded."""
    out = []
    for byte in text.encode("utf-8"):
        if byte in _UNRESERVED or (keep_slash and byte == 0x2F):
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def encode_key(key: str) -> str:
    """An object key as a URI path: each segment encoded, `/` kept. S3 does not
    normalise the path, so the canonical URI is exactly this string."""
    return _encode(key, keep_slash=True)


def _stamp(when: _dt.datetime) -> tuple[str, str]:
    when = when.astimezone(_dt.timezone.utc)
    return when.strftime("%Y%m%dT%H%M%SZ"), when.strftime("%Y%m%d")


def _signing_key(secret_key: str, day: str, region: str) -> bytes:
    key = ("AWS4" + secret_key).encode("utf-8")
    for part in (day, region, SERVICE, "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    return key


def _canonical_query(params: dict[str, str]) -> str:
    return "&".join(f"{_encode(k, False)}={_encode(v, False)}"
                    for k, v in sorted(params.items()))


def _canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    names = sorted(h.lower() for h in headers)
    lowered = {k.lower(): v for k, v in headers.items()}
    block = "".join(f"{n}:{' '.join(str(lowered[n]).split())}\n" for n in names)
    return block, ";".join(names)


def _signature(secret_key, region, day, amz_date, canonical_request) -> tuple[str, str]:
    scope = f"{day}/{region}/{SERVICE}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope,
                         hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    sig = hmac.new(_signing_key(secret_key, day, region), to_sign.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return sig, scope


def presign(method: str, scheme: str, host: str, path: str, *, region: str,
            access_key: str, secret_key: str, when: _dt.datetime, expires: int,
            headers: dict[str, str]) -> str:
    """A SigV4 query-string presigned URL. `path` is already encoded; `headers` are the
    signed headers and must include `host`."""
    amz_date, day = _stamp(when)
    header_block, signed = _canonical_headers(headers)
    params = {"X-Amz-Algorithm": ALGORITHM,
              "X-Amz-Credential": f"{access_key}/{day}/{region}/{SERVICE}/aws4_request",
              "X-Amz-Date": amz_date,
              "X-Amz-Expires": str(int(expires)),
              "X-Amz-SignedHeaders": signed}
    query = _canonical_query(params)
    creq = "\n".join([method, path, query, header_block, signed, UNSIGNED_PAYLOAD])
    sig, _scope = _signature(secret_key, region, day, amz_date, creq)
    return f"{scheme}://{host}{path}?{query}&X-Amz-Signature={sig}"


def authorization(method: str, path: str, query: str, *, region: str, access_key: str,
                  secret_key: str, when: _dt.datetime, headers: dict[str, str],
                  payload_hash: str) -> str:
    """The `Authorization` header for a SigV4 header-signed request. `headers` are the
    signed headers (host, x-amz-date, x-amz-content-sha256, ...)."""
    amz_date, day = _stamp(when)
    header_block, signed = _canonical_headers(headers)
    creq = "\n".join([method, path, query, header_block, signed, payload_hash])
    sig, scope = _signature(secret_key, region, day, amz_date, creq)
    return f"{ALGORITHM} Credential={access_key}/{scope},SignedHeaders={signed},Signature={sig}"


# ------------------------------------------------------------------- the store

def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _virtual_hostable(bucket: str) -> bool:
    """A single DNS label, 3-63 characters,
    lower-case letters, digits and hyphens, and not shaped like an IP address."""
    return bool(_DNS_LABEL.fullmatch(bucket)) and not _is_ip(bucket)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Replaces urllib's default HTTPRedirectHandler in the opener. Returning None from
    redirect_request leaves the 3xx unhandled, and HTTPDefaultErrorHandler raises it as
    an HTTPError, which the store treats as a failed request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class R2BlobStore(BlobStore):
    """R2 blob store, constructed once at startup by apps/files/apps.py."""

    def __init__(self, cfg: cfgmod.R2Config):
        endpoint = cfg.endpoint or f"https://{cfg.account_id}.r2.cloudflarestorage.com"
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BlobError("MESHYCHAT_R2_ENDPOINT must be an http(s) URL with a host")
        self.bucket = cfg.bucket
        self._access_key = cfg.access_key_id
        self._secret_key = cfg.secret_access_key
        self._scheme = parts.scheme
        base = parts.path.rstrip("/")
        if _virtual_hostable(cfg.bucket) and not _is_ip(parts.hostname):
            self._host = f"{cfg.bucket}.{parts.netloc}"
            self._prefix = base
        else:
            self._host = parts.netloc
            self._prefix = f"{base}/{encode_key(cfg.bucket)}"
        # No proxy from the environment: signed requests must not be exposed to a proxy,
        # and a proxy would see every signed request. No redirects: see _NoRedirect.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                   _NoRedirect())

    def __repr__(self) -> str:   # The secret never renders.
        return (f"R2BlobStore(host={self._host!r}, bucket={self.bucket!r}, "
                f"access_key_id={self._access_key!r}, secret_access_key=<redacted>)")

    def _path(self, key: str) -> str:
        return f"{self._prefix}/{encode_key(key)}"

    def _url(self, key: str) -> str:
        return f"{self._scheme}://{self._host}{self._path(key)}"

    # -- presign ---------------------------------------------------------------

    def _presign(self, method, key, ttl_secs, headers, op) -> PresignedUrl:
        ttl = int(ttl_secs)
        if ttl < 0 or ttl > MAX_PRESIGN_SECS:
            log.error("R2 backend error op=%s bucket=%s: presign lifetime %ss is outside "
                      "0..%s", op, self.bucket, ttl, MAX_PRESIGN_SECS)
            raise BlobError("presign failed")
        now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
        signed = {"host": self._host, **headers}
        url = presign(method, self._scheme, self._host, self._path(key), region=REGION,
                      access_key=self._access_key, secret_key=self._secret_key,
                      when=now, expires=ttl, headers=signed)
        return PresignedUrl(url=url, headers=sorted(headers.items()),
                            expires_at=now + _dt.timedelta(seconds=ttl))

    def presign_put(self, key, content_type, max_size_bytes, ttl_secs) -> PresignedUrl:
        """Signs the content type and the declared content length."""
        return self._presign("PUT", key, ttl_secs,
                             {"content-length": str(int(max_size_bytes)),
                              "content-type": content_type}, "presign_put")

    def presign_get(self, key, ttl_secs) -> PresignedUrl:
        return self._presign("GET", key, ttl_secs, {}, "presign_get")

    # -- signed requests ------------------------------------------------------

    def _send(self, method: str, key: str):
        """One header-signed request with an empty body. Returns the open response for
        a 2xx; raises `urllib.error.HTTPError` (a response too) for anything else."""
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date, _day = _stamp(now)
        headers = {"host": self._host, "x-amz-content-sha256": EMPTY_SHA256,
                   "x-amz-date": amz_date}
        headers["authorization"] = authorization(
            method, self._path(key), "", region=REGION, access_key=self._access_key,
            secret_key=self._secret_key, when=now,
            headers={k: v for k, v in headers.items()}, payload_hash=EMPTY_SHA256)
        req = urllib.request.Request(self._url(key), method=method,
                                     headers={("Host" if k == "host" else k): v
                                              for k, v in headers.items()})
        return self._opener.open(req, timeout=REQUEST_TIMEOUT_SECS)

    def _fail(self, op: str, message: str, exc: Exception) -> BlobError:
        detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else \
            type(exc).__name__
        log.error("R2 backend error op=%s bucket=%s: %s", op, self.bucket, detail)
        return BlobError(message)

    def head(self, key: str) -> ObjectMeta:
        try:
            with self._send("HEAD", key) as resp:
                length = resp.headers.get("Content-Length")
                return ObjectMeta(
                    size_bytes=int(length) if length else 0,   # Missing Content-Length is zero.
                    etag=resp.headers.get("ETag"),
                    content_type=resp.headers.get("Content-Type")
                    or "application/octet-stream")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:                                # HEAD returned 404.
                raise BlobNotFound()
            raise self._fail("head_object", "head failed", exc)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise self._fail("head_object", "head failed", exc)

    def get_stream(self, key: str):
        """An open response: a context manager with `read(n)`, which is what finalize
        streams the hash from. The caller closes it."""
        try:
            return self._send("GET", key)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(4096)
            except OSError:
                body = b""
            if exc.code == 404 and b"<Code>NoSuchKey</Code>" in body:
                raise BlobNotFound()                           # GET returned NoSuchKey.
            raise self._fail("get_object", "object read failed", exc)
        except (urllib.error.URLError, OSError) as exc:
            raise self._fail("get_object", "object read failed", exc)

    def delete(self, key: str) -> None:
        try:
            with self._send("DELETE", key):
                return None
        except (urllib.error.URLError, OSError) as exc:
            raise self._fail("delete_object", "delete failed", exc)
