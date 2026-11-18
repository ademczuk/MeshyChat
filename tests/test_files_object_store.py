"""The object-storage backend (`MESHYCHAT_BLOB_BACKEND=r2`, F25), proved without an object store.

The R2 backend uses R2's S3-compatible API: region "auto", endpoint
`https://{account_id}.r2.cloudflarestorage.com` unless MESHYCHAT_R2_ENDPOINT overrides it,
presigned PUT and GET, and HEAD / GET / DELETE as signed requests. `apps/files/r2.py` uses
the standard library and AWS Signature Version 4 on the wire. AWS Signature Version 4 is all
a client library adds on the wire, so a signer that matches AWS's published examples is enough.

WHAT PROVES IT, and what cannot:
  * THE SIGNER is checked against AWS's own published examples (the S3 API reference,
    "Authenticating Requests: Using Query Parameters" and "... Using the Authorization
    Header"): same inputs, the exact signature AWS prints. A signer that agrees only with
    itself would pass everything else here.
  * THE PLUMBING runs through the REAL routes - create, the client's PUT to the presigned
    URL, finalize (HEAD + streamed GET), the download presign and the client's GET, DELETE
    - against a local S3-shaped server that VERIFIES every signature with an independent
    implementation written in this file, refuses a tampered or expired one, and can be
    told to fail.
  * NOT PROVED: that Cloudflare R2 accepts these requests. No credentials or bucket exist
    for this project; that is the operator question F25 is built up to.

Run:  pytest tests/test_files_object_store.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, urlsplit

import pytest
from django.test import Client, override_settings

from tests.test_files import (DSN, ISSUER, JWT_PUBLIC_PEM, _auth, _seed_peer,  # noqa: E402
                              owner_conn)  # noqa: F401 - a fixture, used by name

from apps.files import blob as blobmod  # noqa: E402
from apps.files import config as cfgmod  # noqa: E402

ACCESS_KEY = "AKIDTESTR2EXAMPLE"
SECRET_KEY = "NEVER-PRINT-THIS-R2-SECRET"
BUCKET = "meshy-bucket"


def _r2():
    """Imported per test, so a tree without the module fails each test by name."""
    from apps.files import r2
    return r2


# ====================================================== the signer, against AWS's examples

AWS_AK = "AKIAIOSFODNN7EXAMPLE"
AWS_SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_WHEN = _dt.datetime(2013, 5, 24, 0, 0, 0, tzinfo=_dt.timezone.utc)


def test_the_signer_reproduces_awss_presigned_url_example():
    """S3 API reference, "Authenticating Requests: Using Query Parameters (AWS Signature
    Version 4)": GET examplebucket/test.txt, us-east-1, 86400 s, signed header `host`."""
    url = _r2().presign("GET", "https", "examplebucket.s3.amazonaws.com", "/test.txt",
                        region="us-east-1", access_key=AWS_AK, secret_key=AWS_SK,
                        when=AWS_WHEN, expires=86400,
                        headers={"host": "examplebucket.s3.amazonaws.com"})
    assert url == (
        "https://examplebucket.s3.amazonaws.com/test.txt"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature="
        "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def test_the_signer_reproduces_awss_authorization_header_example():
    """S3 API reference, "Authenticating Requests: Using the Authorization Header", the GET
    Object example: a Range header, an empty payload."""
    empty = hashlib.sha256(b"").hexdigest()
    auth = _r2().authorization("GET", "/test.txt", "", region="us-east-1",
                               access_key=AWS_AK, secret_key=AWS_SK, when=AWS_WHEN,
                               headers={"host": "examplebucket.s3.amazonaws.com",
                                        "range": "bytes=0-9",
                                        "x-amz-content-sha256": empty,
                                        "x-amz-date": "20130524T000000Z"},
                               payload_hash=empty)
    assert auth == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/"
        "aws4_request,SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_a_key_is_encoded_per_segment_and_the_slash_kept():
    """S3 does not normalise a key: `/` separates segments and is sent as-is, anything
    outside the unreserved set is percent-encoded as UTF-8 bytes (upper-case hex)."""
    enc = _r2().encode_key
    assert enc("o/f") == "o/f"
    assert enc("a b/c+d") == "a%20b/c%2Bd"
    assert enc("café/x~y_z.-") == "caf%C3%A9/x~y_z.-"


# =============================================================== endpoint addressing rules

def _cfg(**over) -> cfgmod.R2Config:
    base = dict(account_id="acct123", bucket=BUCKET, access_key_id=ACCESS_KEY,
                secret_access_key=SECRET_KEY, endpoint=None)
    base.update(over)
    return cfgmod.R2Config(**base)


def test_the_default_endpoint_is_the_accounts_r2_host_virtual_hosted():
    """The default endpoint is `https://{account_id}.r2.cloudflarestorage.com`, region
    "auto". A DNS-compatible bucket is a sub-domain of the endpoint."""
    store = _r2().R2BlobStore(_cfg())
    got = store.presign_get("owner/file", 60)
    parts = urlsplit(got.url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https", f"{BUCKET}.acct123.r2.cloudflarestorage.com", "/owner/file")
    q = dict(parse_qsl(parts.query))
    assert q["X-Amz-Credential"].split("/")[2:] == ["auto", "s3", "aws4_request"]
    assert q["X-Amz-Expires"] == "60" and q["X-Amz-SignedHeaders"] == "host"
    assert got.headers == []


def test_an_ip_endpoint_or_an_unhostable_bucket_is_path_style():
    """An IP endpoint and a bucket that is not a DNS label use path style; the control is the
    hostable bucket on a named host, above."""
    ip = _r2().R2BlobStore(_cfg(endpoint="http://127.0.0.1:9000")).presign_get("o/f", 60)
    assert urlsplit(ip.url)[:3] == ("http", "127.0.0.1:9000", f"/{BUCKET}/o/f")
    odd = _r2().R2BlobStore(_cfg(bucket="Not_A_Label",
                                 endpoint="https://r2.example.test")).presign_get("o/f", 60)
    assert urlsplit(odd.url)[:3] == ("https", "r2.example.test", "/Not_A_Label/o/f")


def test_the_put_presign_signs_the_type_and_the_declared_length():
    """Content type and declared length are signed headers handed to the client verbatim."""
    got = _r2().R2BlobStore(_cfg()).presign_put("o/f", "text/plain", 42, 900)
    q = dict(parse_qsl(urlsplit(got.url).query))
    assert q["X-Amz-SignedHeaders"] == "content-length;content-type;host"
    assert got.headers == [("content-length", "42"), ("content-type", "text/plain")]
    now = _dt.datetime.now(_dt.timezone.utc)
    assert abs((got.expires_at - now).total_seconds() - 900) < 5


def test_a_presign_past_seven_days_is_refused_and_the_secret_never_renders():
    """Presigning refuses more than one week with `BlobError("presign failed")`.
    Control: exactly one week presigns."""
    store = _r2().R2BlobStore(_cfg())
    assert store.presign_get("o/f", 7 * 24 * 3600).url
    with pytest.raises(blobmod.BlobError, match="presign failed"):
        store.presign_get("o/f", 7 * 24 * 3600 + 1)
    put = store.presign_put("o/f", "text/plain", 1, 60)
    rendered = put.url + json.dumps(put.headers) + repr(store) + repr(_cfg())
    assert SECRET_KEY not in rendered and quote(SECRET_KEY, safe="") not in rendered
    assert ACCESS_KEY in put.url, "control: the ACCESS key id is in the credential scope"


# ==================================================== a local S3 that checks signatures

def _hm(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _independent_signature(method, raw_path, query_pairs, headers, signed, payload_hash,
                           amz_date, scope) -> str:
    """SigV4 again, written separately from apps/files/r2.py so a shared mistake is not
    a shared blind spot: canonical query by quote(), headers from the request received."""
    cq = "&".join(sorted(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
                         for k, v in query_pairs))
    ch = "".join(f"{h}:{' '.join(headers.get(h, '').split())}\n" for h in signed)
    creq = "\n".join([method, raw_path, cq, ch, ";".join(signed), payload_hash])
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                     hashlib.sha256(creq.encode()).hexdigest()])
    day, region, service, term = scope.split("/")
    k = _hm(("AWS4" + SECRET_KEY).encode(), day)
    for part in (region, service, term):
        k = _hm(k, part)
    return hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()


class FakeS3(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail: dict[str, int | tuple[int, str]] = {}   # method -> status[, code]
        self.log: list[tuple[str, str, int]] = []

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, status, body=b"", headers=()):
        self._drain_body()
        self.server.log.append((self.command, self.path.split("?")[0], status))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if self.command != "HEAD":
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _drain_body(self):
        """Read the request body this handler has not used, before an early answer. On Windows, closing a socket that
        still holds unread received data sends a reset, and a reset that beats the client's read loses the answer it
        was sent: #526's HQB gate read a 403 as ConnectionAbortedError 10053 (memory-bxxxxx 268f45). It reads at most
        1 MiB; a client that declared more than it sent would still hold the handler, and no test here does that."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 0:
            self.rfile.read(min(n, 1 << 20))

    def _error(self, status, code):
        self._drain_body()
        self._reply(status, f"<Error><Code>{code}</Code></Error>".encode(),
                    [("Content-Type", "application/xml")])

    def _verify(self) -> str | None:
        """None when the signature and the expiry check; else an S3 error code."""
        parts = urlsplit(self.path)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        headers = {k.lower(): v for k, v in self.headers.items()}
        q = dict(pairs)
        if "X-Amz-Signature" in q:
            signed = q["X-Amz-SignedHeaders"].split(";")
            ak, scope = q["X-Amz-Credential"].split("/", 1)
            amz_date, given = q["X-Amz-Date"], q["X-Amz-Signature"]
            when = _dt.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=_dt.timezone.utc)
            if _dt.datetime.now(_dt.timezone.utc) > when + _dt.timedelta(
                    seconds=int(q["X-Amz-Expires"])):
                return "AccessDenied"
            rest = [(k, v) for k, v in pairs if k != "X-Amz-Signature"]
            payload = "UNSIGNED-PAYLOAD"
        else:
            m = re.fullmatch(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\S+?),\s*"
                             r"SignedHeaders=(\S+?),\s*Signature=([0-9a-f]{64})",
                             headers.get("authorization", ""))
            if not m:
                return "AccessDenied"
            ak, scope, signed_s, given = m.groups()
            signed = signed_s.split(";")
            amz_date = headers.get("x-amz-date", "")
            rest = pairs
            payload = headers.get("x-amz-content-sha256", "")
        if ak != ACCESS_KEY or scope.split("/")[1:] != ["auto", "s3", "aws4_request"]:
            return "InvalidAccessKeyId"
        want = _independent_signature(self.command, parts.path, rest, headers, signed,
                                      payload, amz_date, scope)
        return None if hmac.compare_digest(want, given) else "SignatureDoesNotMatch"

    def _key(self) -> str:
        return urlsplit(self.path).path

    def _handle(self):
        self._body_read = False
        if self.command in self.server.fail:
            fail = self.server.fail[self.command]
            status, code = fail if isinstance(fail, tuple) else (fail, "InternalError")
            return self._error(status, code)
        bad = self._verify()
        if bad:
            return self._error(403, bad)
        key = self._key()
        if self.command == "PUT":
            n = int(self.headers.get("Content-Length", "0"))
            self._body_read = True
            self.server.objects[key] = (self.rfile.read(n),
                                        self.headers.get("Content-Type", ""))
            return self._reply(200, headers=[("ETag", '"e1"')])
        if self.command == "DELETE":
            self.server.objects.pop(key, None)
            return self._reply(204)
        if key not in self.server.objects:
            if self.command == "HEAD":
                return self._reply(404)
            return self._error(404, "NoSuchKey")
        data, ctype = self.server.objects[key]
        if self.command == "HEAD":
            self._drain_body()
            self.server.log.append(("HEAD", key, 200))
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("ETag", '"e1"')
            self.end_headers()
            return None
        return self._reply(200, data, [("Content-Type", ctype)])

    do_GET = do_PUT = do_HEAD = do_DELETE = _handle


@pytest.fixture()
def s3():
    server = FakeS3()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def r2_env(s3, monkeypatch):
    """MESHYCHAT_BLOB_BACKEND=r2 pointed at the local server, installed the way startup
    installs it (`blob.activate`), and the previous installation restored afterwards."""
    for k, v in {"MESHYCHAT_BLOB_BACKEND": "r2", "MESHYCHAT_R2_ACCOUNT_ID": "acct123",
                 "MESHYCHAT_R2_BUCKET": BUCKET, "MESHYCHAT_R2_ACCESS_KEY_ID": ACCESS_KEY,
                 "MESHYCHAT_R2_SECRET_ACCESS_KEY": SECRET_KEY,
                 "MESHYCHAT_R2_ENDPOINT": s3.endpoint, "MESHY_DSN": DSN}.items():
        monkeypatch.setenv(k, v)
    for k in ("MESHYCHAT_BLOB_DIR", "MESHYCHAT_BLOB_TOKEN_SECRET", "MESHYCHAT_PUBLIC_BASE_URL",
              "MESHYCHAT_MAX_FILE_BYTES", "MESHYCHAT_PRESIGN_TTL_SECS", "MESHYCHAT_UPLOAD_WINDOW_SECS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(blobmod, "_ACTIVE", blobmod._ACTIVE)
    blobmod.activate(cfgmod.build_blob_config())
    with override_settings(ALLOWED_HOSTS=["*"], MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
                           MESHY_JWT_ISSUER=ISSUER, MESHY_DSN=DSN):
        yield s3


def _http(method, url, data=None, headers=()) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_the_fake_store_refuses_a_tampered_or_expired_presign(r2_env):
    """THE CONTROL FOR THE VERIFIER. If it accepted anything, every route test below
    would prove nothing about the signatures."""
    store = blobmod.active()[1]
    put = store.presign_put("o/f", "text/plain", 3, 60)
    assert _http("PUT", put.url, b"abc", put.headers)[0] == 200
    got = store.presign_get("o/f", 60)
    assert _http("GET", got.url) == (200, b"abc")

    sig = got.url.rsplit("=", 1)[1]
    tampered = got.url[: -len(sig)] + ("0" if sig[0] != "0" else "1") + sig[1:]
    assert _http("GET", tampered)[0] == 403
    other_key = got.url.replace("/o/f?", "/o/g?")
    assert _http("GET", other_key)[0] == 403
    # The declared length is signed: a different body length is refused.
    assert _http("PUT", put.url, b"abcd",
                 [("content-length", "4"), ("content-type", "text/plain")])[0] == 403
    old = _r2().presign("GET", "http", urlsplit(got.url).netloc, f"/{BUCKET}/o/f",
                        region="auto", access_key=ACCESS_KEY, secret_key=SECRET_KEY,
                        when=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2),
                        expires=60, headers={"host": urlsplit(got.url).netloc})
    assert _http("GET", old)[0] == 403


def test_the_whole_file_flow_runs_on_the_object_store(owner_conn, r2_env):
    """create -> the client's PUT to the presigned URL -> finalize (HEAD + streamed GET)
    -> presigned download -> DELETE, every step through the real routes, every store
    request signature-checked by the fake."""
    s3 = r2_env
    client = Client()
    p = _seed_peer(owner_conn, "r2flow")
    payload = b"bytes that live in an object store"

    # Finalize before the bytes land: HEAD 404 is the 409 not-ready, not a 500.
    r = client.post("/v1/files", data=json.dumps({"filename": "n.txt",
                                                  "content_type": "text/plain",
                                                  "size_bytes_declared": len(payload)}),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 201, r.content
    created = r.json()
    assert created["upload_url"].startswith(f"{s3.endpoint}/{BUCKET}/{p['owner_id']}/")
    assert created["upload_headers"] == [["content-length", str(len(payload))],
                                         ["content-type", "text/plain"]]
    sha = hashlib.sha256(payload).hexdigest()
    early = client.post(f"/v1/files/{created['id']}/finalize",
                        data=json.dumps({"sha256": sha}),
                        content_type="application/json", **_auth(p["peer_id"]))
    assert early.status_code == 409, early.content
    assert early.json()["error"] == "E_FILE_NOT_READY"

    status, _ = _http("PUT", created["upload_url"], payload,
                      [tuple(h) for h in created["upload_headers"]])
    assert status == 200
    key = f"/{BUCKET}/{p['owner_id']}/{created['id']}"
    assert s3.objects[key][0] == payload

    fin = client.post(f"/v1/files/{created['id']}/finalize",
                      data=json.dumps({"sha256": sha}),
                      content_type="application/json", **_auth(p["peer_id"]))
    assert fin.status_code == 200, fin.content
    assert fin.json()["status"] == "ready" and fin.json()["size_bytes"] == len(payload)
    assert ("HEAD", key, 200) in s3.log and ("GET", key, 200) in s3.log

    url = client.get(f"/v1/files/{created['id']}/url", **_auth(p["peer_id"]))
    assert url.status_code == 200, url.content
    assert _http("GET", url.json()["url"]) == (200, payload)

    gone = client.delete(f"/v1/files/{created['id']}", **_auth(p["peer_id"]))
    assert gone.status_code == 204, gone.content
    assert key not in s3.objects and ("DELETE", key, 204) in s3.log
    # A DELETE of an object already gone is success (S3 answers 204 either way).
    blobmod.active()[1].delete(f"{p['owner_id']}/{created['id']}")


def test_a_failing_object_store_is_503_and_names_nothing(owner_conn, r2_env):
    """The body is the constant "blob backend error". A 500 from the store on HEAD must not
    leak the endpoint, the bucket or a credential. Control: the
    same finalize succeeds once the store stops failing."""
    s3 = r2_env
    client = Client()
    p = _seed_peer(owner_conn, "r2fail")
    payload = b"x" * 5
    r = client.post("/v1/files", data=json.dumps({"content_type": "text/plain",
                                                  "size_bytes_declared": 5}),
                    content_type="application/json", **_auth(p["peer_id"]))
    created = r.json()
    assert _http("PUT", created["upload_url"], payload,
                 [tuple(h) for h in created["upload_headers"]])[0] == 200
    sha = hashlib.sha256(payload).hexdigest()

    s3.fail["HEAD"] = 500
    bad = client.post(f"/v1/files/{created['id']}/finalize",
                      data=json.dumps({"sha256": sha}),
                      content_type="application/json", **_auth(p["peer_id"]))
    assert bad.status_code >= 500, bad.content
    text = bad.content.decode()
    for secret_or_detail in (SECRET_KEY, ACCESS_KEY, BUCKET, "127.0.0.1"):
        assert secret_or_detail not in text, text

    s3.fail.clear()
    ok = client.post(f"/v1/files/{created['id']}/finalize",
                     data=json.dumps({"sha256": sha}),
                     content_type="application/json", **_auth(p["peer_id"]))
    assert ok.status_code == 200, ok.content


def test_a_missing_object_read_is_not_found_and_a_store_fault_is_a_backend_error(r2_env):
    """GET NoSuchKey and HEAD 404 raise BlobNotFound; every other failure raises BlobError
    with a constant message, never the store's own text."""
    s3 = r2_env
    store = blobmod.active()[1]
    with pytest.raises(blobmod.BlobNotFound):
        store.head("no/such")
    with pytest.raises(blobmod.BlobNotFound):
        store.get_stream("no/such")
    s3.fail["GET"] = 503
    with pytest.raises(blobmod.BlobError) as exc:
        store.get_stream("no/such")
    assert not isinstance(exc.value, blobmod.BlobNotFound)
    assert str(exc.value) == "object read failed"
    s3.fail["DELETE"] = 500
    with pytest.raises(blobmod.BlobError, match="delete failed"):
        store.delete("no/such")


def test_the_r2_backend_is_the_registered_default_and_the_seam_still_overrides(r2_env):
    """F25 closes the seam the way it was declared: `MESHYCHAT_BLOB_BACKEND=r2` builds the
    object-store backend, and `register_backend` still replaces it without editing
    blob.py (the old test's control, kept)."""
    cfg = cfgmod.build_blob_config()
    assert type(blobmod.from_config(cfg)).__name__ == "R2BlobStore"

    class Fake(blobmod.BlobStore):
        def __init__(self, r2cfg):
            self.bucket = r2cfg.bucket

    real = blobmod._BACKENDS["r2"]
    try:
        blobmod.register_backend("r2", Fake)
        assert isinstance(blobmod.from_config(cfg), Fake)
    finally:
        blobmod.register_backend("r2", real)
    assert type(blobmod.from_config(cfg)).__name__ == "R2BlobStore"


# ============================================ review of c007c45: redirects, and four gaps

class _Recording(ThreadingHTTPServer):
    """A server that records every request it receives and answers `status`, optionally
    with a Location. Used as the host a redirect points at and as a proxy."""
    daemon_threads = True

    def __init__(self, status=200, location=None):
        self.seen: list[tuple[str, str, dict]] = []
        self.status, self.location = status, location

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                srv = self.server
                srv.seen.append((self.command, self.path, dict(self.headers)))
                n = int(self.headers.get("Content-Length", "0") or 0)
                if n > 0:
                    self.rfile.read(min(n, 1 << 20))
                if self.path.startswith(f"/{BUCKET}/direct/"):
                    status, location = 200, None
                else:
                    status, location = srv.status, srv.location
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "7" if self.command == "HEAD" else "0")
                self.end_headers()

            do_GET = do_HEAD = do_DELETE = do_PUT = _any

        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_from_the_store_is_an_error_and_the_signature_never_leaves(status):
    """A 3xx is an error. urllib's default opener follows redirects, re-sending the signed
    Authorization header and x-amz-date to
    whatever host the Location names, and would then read that host's answer as the
    object (review of c007c45, LOW: HEAD returned the other host's size, and the other
    host received `AWS4-HMAC-SHA256 Credential=...`). Every operation answers a redirect
    as a BlobError with its constant message, and the other host receives NOTHING.
    Control: the same store, same origin, on a path the origin answers 200 directly."""
    other = _Recording(200)
    origin = _Recording(status, location=f"{other.endpoint}/elsewhere")
    try:
        store = _r2().R2BlobStore(_cfg(endpoint=origin.endpoint))
        assert store.head("direct/x").size_bytes == 7, "control: a 2xx is still read"
        with pytest.raises(blobmod.BlobError, match="^head failed$") as exc:
            store.head("owner/file")
        assert not isinstance(exc.value, blobmod.BlobNotFound)
        with pytest.raises(blobmod.BlobError, match="^object read failed$"):
            store.get_stream("owner/file")
        with pytest.raises(blobmod.BlobError, match="^delete failed$"):
            store.delete("owner/file")
        assert [m for m, _p, _h in origin.seen] == ["HEAD", "HEAD", "GET", "DELETE"], \
            origin.seen
        assert other.seen == [], f"the redirect was followed: {other.seen}"
    finally:
        other.stop()
        origin.stop()


def test_a_bucket_that_is_not_one_lower_case_dns_label_is_path_style():
    """aws.isVirtualHostableS3Bucket(bucket, allowSubDomains=false): upper case and dots
    both fall back to path style (review INFO: a regex accepting either survived every
    test). Control: the lower-case label is virtual-hosted on the same endpoint."""
    for bucket in ("My-Bucket", "my.bucket", "MY.BUCKET"):
        got = _r2().R2BlobStore(_cfg(bucket=bucket, endpoint="https://r2.example.test"))
        assert urlsplit(got.presign_get("o/f", 60).url)[1:3] == (
            "r2.example.test", f"/{bucket}/o/f"), bucket
    hosted = _r2().R2BlobStore(_cfg(bucket="my-bucket", endpoint="https://r2.example.test"))
    assert urlsplit(hosted.presign_get("o/f", 60).url)[1:3] == (
        "my-bucket.r2.example.test", "/o/f")


def test_a_content_type_with_inner_runs_of_spaces_still_verifies(r2_env):
    """SigV4 canonical headers collapse runs of spaces in a value; S3 and R2 do the same
    when they check the signature. A signer that does not collapse them signs a different
    string from the one the server rebuilds, and the client's PUT is refused (review
    INFO: removing the collapse survived). Control: the single-space type."""
    store = blobmod.active()[1]
    for ctype in ("text/plain; charset=utf-8", "text/plain;  charset=utf-8"):
        put = store.presign_put("o/ws", ctype, 3, 60)
        assert _http("PUT", put.url, b"abc", put.headers)[0] == 200, ctype


def test_only_no_such_key_is_not_found_on_a_read(r2_env):
    """Only NoSuchKey raises BlobNotFound. A 404 carrying NoSuchBucket is a
    misconfigured store; a 503 from the object store is not "this file is not there
    yet", which finalize would answer 409 (review INFO: mapping every 404 survived).
    Control: NoSuchKey on the same route is BlobNotFound."""
    s3 = r2_env
    store = blobmod.active()[1]
    s3.fail["GET"] = (404, "NoSuchKey")
    with pytest.raises(blobmod.BlobNotFound):
        store.get_stream("o/missing")
    s3.fail["GET"] = (404, "NoSuchBucket")
    with pytest.raises(blobmod.BlobError, match="^object read failed$") as exc:
        store.get_stream("o/missing")
    assert not isinstance(exc.value, blobmod.BlobNotFound)


def test_a_proxy_in_the_environment_is_not_used(r2_env, monkeypatch):
    """Every store request is signed; a proxy would see all of them, and the HTTP client
    reads no proxy from the environment (review INFO: dropping ProxyHandler({})
    survived). With HTTP_PROXY and HTTPS_PROXY naming a recording proxy, the store still
    talks to the store directly and the proxy receives nothing. Control: an opener that
    DOES read the environment reaches the proxy, so the variables are really in force."""
    proxy = _Recording(502)
    try:
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(var, proxy.endpoint)
        # Built AFTER the variables are set, as a server started with them would be: an
        # opener reads the environment when it is built, so the store r2_env built
        # earlier could not see them and would pass either way (measured: it did).
        blobmod.activate(cfgmod.build_blob_config())
        store = blobmod.active()[1]
        put = store.presign_put("o/px", "text/plain", 3, 60)
        assert _http("PUT", put.url, b"abc", put.headers)[0] == 200
        assert store.head("o/px").size_bytes == 3
        assert proxy.seen == [], f"a store request went through the proxy: {proxy.seen}"

        env_opener = urllib.request.build_opener()        # reads the environment
        try:
            env_opener.open(urllib.request.Request(f"{r2_env.endpoint}/{BUCKET}/o/px",
                                                   method="HEAD"), timeout=10)
        except urllib.error.HTTPError:
            pass
        assert proxy.seen, "control: an environment-reading opener did not use the proxy"
    finally:
        proxy.stop()
