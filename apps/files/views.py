"""HTTP surface for `/v1/files/*`.

Route paths, methods and status codes:

  POST   /v1/files                  -> 201 Created
  GET    /v1/files/{id}             -> 200
  DELETE /v1/files/{id}             -> 204 No Content
  POST   /v1/files/{id}/finalize    -> 200
  GET    /v1/files/{id}/url         -> 200
  PUT    /v1/files/raw/{token}      -> 200 [FS backend]
  GET    /v1/files/raw/{token}      -> 200 [FS backend]

Create replies 201, not the 200 its body shape suggests.

KEY ORDER IS PART OF THE WIRE: clients may hash or diff response bytes.
`FileArtifactView` is emitted in the order written and the JSON is compact, with no space
after `:` or `,`. `meshychat/urls.py` applies the same requirement to the health body.

AUTHENTICATION. Every `/v1/files/*` route is authenticated as a peer. `/v1/files/raw/{token}`
is NOT: its authorisation IS the HMAC token, which is the whole point of a
presigned URL - the client that follows it holds no JWT.
"""
from __future__ import annotations

import functools
import json
import uuid

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.peers.auth import authenticate_peer
from apps.peers.timefmt import format_rfc3339

from . import blob as blobmod
from . import config as cfgmod
from . import db, errors, quota, service

#: A ceiling above the default MESHYCHAT_MAX_FILE_BYTES so a malicious
#: client cannot exhaust memory before the per-token check runs.
BODY_HARD_CAP = 256 * 1024 * 1024


# ------------------------------------------------------------------- plumbing

def _json(payload, status=200) -> JsonResponse:
    """Compact separators keep every byte after the first key stable.
    `sort_keys` is NOT used: the fields are emitted in insertion order."""
    return JsonResponse(payload, status=status, json_dumps_params={"separators": (",", ":")})


def _error_response(exc: errors.ApiError) -> JsonResponse:
    return _json(exc.body(), status=exc.status)


def _route(handler, *, with_db=True):
    """One guarded `meshychat_app` autocommit connection per request, and one place where an
    `ApiError` becomes a response. Mirrors `apps/peers/views.py::_route`.

    `with_db=False` (`_route_no_db`) is for the raw-token routes: they touch the
    filesystem, never the database. Opening a connection for them would make a blob read
    depend on Postgres being up."""
    def wrapper(request, *args, **kwargs):
        try:
            if not with_db:
                return handler(request, *args, **kwargs)
            with db.connection() as conn:
                return handler(request, conn, *args, **kwargs)
        except errors.ApiError as exc:
            return _error_response(exc)
    wrapper.__name__ = getattr(handler, "__name__", "route")
    return csrf_exempt(wrapper)


_route_no_db = functools.partial(_route, with_db=False)


def _method(request, *allowed):
    if request.method not in allowed:
        return HttpResponse(status=405, headers={"Allow": ", ".join(allowed)})
    return None


def _file_uuid(raw):
    """A malformed id is a 400 raised from inside the view, not a 404. The body is this
    app's JSON envelope. Answering 404 would be wrong: it would say the file does not
    exist when the id was never well-formed. Same call the peers app makes for `{peer_id}`."""
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise errors.validation(f"invalid file id: {raw}")


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.validation(f"body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise errors.validation("body must be a JSON object")
    return body


def _active():
    """The configuration and store built ONCE, at startup (apps/files/apps.py, B11).
    Nothing here reads the environment: a set-but-wrong configuration never got this far,
    because the process refused to start. The one way to be here without a store is
    MESHYCHAT_BLOB_BACKEND unset at startup (the divergence stated in config.py), answered with
    the backend-unavailable code; the detail is logged, never sent."""
    try:
        return blobmod.active()
    except blobmod.BlobError as exc:
        raise errors.blob_backend(str(exc))


def _svc(conn):
    cfg, store = _active()
    return service.FileService(conn, store, cfg)


# ------------------------------------------------------------------ wire shapes

def _view(row) -> dict:
    """`FileArtifactView`. Fields are emitted in the order written.

    `r2_key` and `grace_until` are DELIBERATELY ABSENT: they are server-side bookkeeping.
    Emitting the key would hand a client the storage layout.

    File and owner identifiers are serialized as plain UUID strings on the wire."""
    return {
        "id": str(row.id),
        "owner_id": str(row.owner_id),
        "uploaded_by_peer_id": str(row.uploaded_by_peer_id) if row.uploaded_by_peer_id else None,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "content_type": row.content_type,
        "filename": row.filename,
        "status": row.status,
        "created_at": format_rfc3339(row.created_at),
        "finalized_at": format_rfc3339(row.finalized_at) if row.finalized_at else None,
    }


# ----------------------------------------------------------------------- routes

@_route
def create(request, conn):
    """POST /v1/files. 201 Created."""
    bad = _method(request, "POST")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    body = _json_body(request)

    content_type = body.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        raise errors.validation("content_type is required and must be a string")
    filename = body.get("filename")
    if filename is not None and not isinstance(filename, str):
        raise errors.validation("filename must be a string or null")
    declared = body.get("size_bytes_declared")
    # A negative, float, or string is refused here with a 400 before it can reach the
    # database.
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
        raise errors.validation("size_bytes_declared must be a non-negative integer")

    svc = _svc(conn)
    owner_id = svc.resolve_owner_id(auth.peer_id)
    result = svc.create_artifact(owner_id, auth.peer_id, filename, content_type, declared)
    return _json({
        "id": str(result["artifact"].id),
        "upload_url": result["upload_url"],
        # A sequence of string pairs becomes a JSON array of two-element arrays, NOT an object.
        "upload_headers": [[k, v] for k, v in result["upload_headers"]],
        "expires_at": format_rfc3339(result["expires_at"]),
    }, status=201)


@_route
def metadata_or_delete(request, conn, file_id):
    """GET + DELETE /v1/files/{id} share one path."""
    bad = _method(request, "GET", "DELETE")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    fid = _file_uuid(file_id)
    svc = _svc(conn)
    owner_id = svc.resolve_owner_id(auth.peer_id)

    if request.method == "GET":
        row = svc.get_metadata(fid, auth.peer_id, owner_id)
        return _json(_view(row), status=200)

    svc.soft_delete(fid, auth.peer_id, owner_id)
    # 204: no body at all. A JSON body here would change the response.
    return HttpResponse(status=204)


@_route
def finalize(request, conn, file_id):
    """POST /v1/files/{id}/finalize.

    The route does MORE than call the service, and each extra step is load-bearing:

      * the artifact row is fetched HERE first, and a foreign owner is a 404 before any
        blob work happens. The service does not check ownership, so this route-level check
        prevents cross-tenant finalization.
      * the pre-HEAD maps a missing object to 409 E_FILE_NOT_READY, not a 500: finalizing
        before the PUT landed is a normal client race.
      * the quota reservation is taken BEFORE finalize and refunded if finalize fails.
      * the reserved amount is the HEAD-reported size, then trued up to the verified size,
        because `size_bytes` is NULL until mark_ready.
    """
    bad = _method(request, "POST")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    fid = _file_uuid(file_id)
    body = _json_body(request)
    sha256 = body.get("sha256")
    if not isinstance(sha256, str) or not sha256:
        raise errors.validation("sha256 is required and must be a string")

    svc = _svc(conn)
    owner_id = svc.resolve_owner_id(auth.peer_id)

    with conn.cursor() as cur:
        cur.execute("SELECT owner_id, r2_key FROM file_artifacts WHERE id = %s", (str(fid),))
        rec = cur.fetchone()
    if rec is None:
        raise errors.not_found()               # Missing artifact.
    row_owner, r2_key = rec
    if str(row_owner) != str(owner_id):
        raise errors.not_found()               # Foreign owner.

    try:
        meta = svc.store.head(r2_key)
    except blobmod.BlobNotFound:
        raise errors.file_bytes_not_uploaded()   # 409, not 500.
    except blobmod.BlobError as exc:
        raise errors.internal(f"blob head: {exc}")

    declared = int(meta.size_bytes)
    reservation = service.reserve_file_bytes(conn, owner_id, declared)

    try:
        ready = svc.finalize(fid, sha256)
    except Exception:
        try:
            quota.get_quota_service().refund(conn, reservation)
        except Exception:                              # noqa: BLE001 - refund failure is non-fatal.
            errors.log.warning("refund after failed finalize failed; non-fatal",
                               exc_info=True)
        raise

    actual = ready.size_bytes if ready.size_bytes is not None else declared
    if actual != declared:
        try:
            quota.get_quota_service().bump(conn, owner_id, quota.CAPABILITY_FILE_BYTES,
                                           actual - declared)
        except Exception:                              # noqa: BLE001 - usage correction failure is non-fatal.
            errors.log.warning("bump_usage true-up failed; non-fatal", exc_info=True)

    return _json(_view(ready), status=200)


@_route
def presign_url(request, conn, file_id):
    """GET /v1/files/{id}/url."""
    bad = _method(request, "GET")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    fid = _file_uuid(file_id)
    svc = _svc(conn)
    owner_id = svc.resolve_owner_id(auth.peer_id)
    presigned = svc.presign_download(fid, auth.peer_id, owner_id)
    return _json({"url": presigned.url,
                  "expires_at": format_rfc3339(presigned.expires_at)}, status=200)


# ------------------------------------------------- /v1/files/raw/{token} (FS only)

def _fs_store_or_404():
    """Raw routes are available ONLY when the backend is fs.

    ROUTE DECISION: Django resolves URLs from a module imported
    once, so mounting conditionally would make the route table depend on whichever
    environment happened to exist at first import. The path is therefore always mounted
    and answers 404 when the backend is not FS. That 404 carries a JSON error envelope
    rather than an empty body, which is only reachable on an R2 deployment, where no client
    has any reason to construct such a URL."""
    cfg, store = _active()
    if cfg.backend != cfgmod.BACKEND_FS:
        raise errors.not_found()
    return store


def _checked_token(store, token: str, expected_op: str):
    """Any failure is 401 and says nothing about which one, so a token cannot reveal
    storage state. Expiry is checked here, by the route, not inside verify_token, against
    the same clock function `FsBlobStore` stamps `exp_unix` with."""
    try:
        payload = blobmod.verify_token(store.token_secret, token)
    except blobmod.BlobError:
        raise errors.auth_invalid("token invalid")
    now = blobmod._now_unix()
    if payload.exp_unix <= now:
        raise errors.auth_invalid("token expired")
    if payload.op != expected_op:
        raise errors.auth_invalid(f"token op is not {expected_op}")
    return payload


def _read_capped_body(request) -> bytes:
    """Read a body up to `BODY_HARD_CAP`.

    `request.body` is NOT used: Django raises `RequestDataTooBig` on it above
    `DATA_UPLOAD_MAX_MEMORY_SIZE` (2.5 MB by default), which would reject every upload
    larger than that with a 400. `request.read()` streams past
    that check, and the cap is re-imposed here."""
    buf = bytearray()
    while True:
        chunk = request.read(64 * 1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > BODY_HARD_CAP:
            # The cap protects server memory before the token limit is checked, so this
            # server-side limit is a 500, not a client 413.
            raise errors.internal("read body: length limit exceeded")
    return bytes(buf)


@_route_no_db
def raw(request, token):
    """PUT + GET /v1/files/raw/{token}."""
    bad = _method(request, "PUT", "GET")
    if bad:
        return bad
    store = _fs_store_or_404()

    if request.method == "PUT":
        payload = _checked_token(store, token, blobmod.TOKEN_OP_PUT)
        try:
            store.safe_path(payload.key)      # Traversal guard.
        except blobmod.BlobKeyRejected:
            raise errors.auth_invalid("token key escapes blob_dir")
        except blobmod.BlobError as exc:      # a BACKEND fault is not the holder's fault
            raise errors.internal(f"path check: {exc}")
        data = _read_capped_body(request)
        max_bytes = payload.max_size_bytes
        if max_bytes is not None and len(data) > max_bytes:
            raise errors.upload_size_exceeds_token(len(data), max_bytes)
        try:
            store.put_bytes(payload.key, data)
        except (blobmod.BlobError, OSError) as exc:
            raise errors.internal(f"write file: {exc}")
        return HttpResponse(status=200)

    payload = _checked_token(store, token, blobmod.TOKEN_OP_GET)
    try:
        with store.get_stream(payload.key) as fh:
            data = fh.read()
    except blobmod.BlobNotFound:
        raise errors.not_found()
    except blobmod.BlobKeyRejected:
        # BRANCH ON THE TYPE, NEVER ON THE MESSAGE. The previous `"escapes" in str(exc)`
        # routed one of the four rejection classes to a refusal and the other three to 500:
        # a signed token for `C:/Windows/win.ini` is rejected as "must be relative", which
        # does not contain the word, so it surfaced as an internal error - measured.
        raise errors.auth_invalid("token key escapes blob_dir")
    except blobmod.BlobError as exc:
        raise errors.internal(f"read file: {exc}")
    # ALWAYS declare a Content-Type. `nosniff` only protects an
    # already-declared type; with none at all a browser can sniff attacker-controlled
    # bytes as text/html. The real type lives in the DB and is not reachable from a bare
    # token, and blob keys carry no extension, so octet-stream is the honest answer.
    resp = HttpResponse(data, status=200, content_type="application/octet-stream")
    resp["Content-Length"] = str(len(data))
    return resp


# ------------------------------------------------- /v1/files/{id}/grants (audit finding 5)

@_route
def file_grants(request, conn, file_id):
    """POST /v1/files/{id}/grants {"to": "<label>"} - the file's owner lets the peers a label reaches download it,
    as a mail message addressing them would (rule 3). For a file sent to one reader that another must now read: six
    reviewers got 404s that way (audit finding 5). Owner's peers only; anyone else gets the 404 a missing file gets.
    201 with the peers granted; 400 when the label reaches no single reader (a broadcast, an ambiguous or unknown label,
    or only the granter)."""
    bad = _method(request, "POST")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    fid = _file_uuid(file_id)
    body = _json_body(request)
    label = body.get("to")
    if not isinstance(label, str) or not 0 < len(label.strip()) <= 200:
        raise errors.validation("'to' must be one label of 1 to 200 characters")
    svc = _svc(conn)
    owner_id = svc.resolve_owner_id(auth.peer_id)
    granted = svc.grant(fid, label, auth.peer_id, owner_id)
    if not granted:
        raise errors.validation(f"'to' {label.strip()!r} reaches no single reader (a broadcast, an ambiguous or "
                                f"unknown label, or only yourself)")
    return _json({"file_id": str(fid), "to": label.strip(), "granted_peers": granted}, status=201)


# ------------------------------------------------- /v1/files/{id}/downloads (issue #118)

@_route
def downloads(request, conn, file_id):
    """GET /v1/files/{id}/downloads - the pickup receipts of one file, newest 100: one per presigned URL issued,
    with the reader's peer id, the access rule that admitted it (1 owner, 2 call, 3 message) and the hash served.
    Peers of the file's owner only; everyone else gets the same 404 as a missing file. A receipt says a URL was
    issued, not that the bytes arrived."""
    bad = _method(request, "GET")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    fid = _file_uuid(file_id)
    svc = _svc(conn)
    owner_id = svc.resolve_owner_id(auth.peer_id)
    return _json({"file_id": str(fid), "downloads": svc.list_downloads(fid, auth.peer_id, owner_id)}, status=200)
