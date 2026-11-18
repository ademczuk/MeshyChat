"""Files that travel WITH a mesh message, through the server's existing file service (divergence 079).

THE PROBLEM, measured: ordinary bodies are tiny (median 99 bytes, p90 about 1.5 KB), but every serial log (189 to
243 KB) went as 40 to 50 KB body parts, and `send` now refuses those (sendguard: 16 KiB a body, 6 messages a recipient
in 30 minutes). A log, a kit or a report is ONE file, so it goes as one, and the message cites it: `send --attach FILE`.

THE WIRE is apps/files/views.py and service.py exactly as they are; nothing on the server changed for this:

  POST   /v1/files {filename, content_type, size_bytes_declared}   201 {id, upload_url, upload_headers, expires_at}
  PUT    <upload_url>, the bytes, upload_headers verbatim          fs: <MESHY_PUBLIC_BASE_URL>/v1/files/raw/<token>;
                                                                   r2: the bucket's presigned URL
  POST   /v1/files/<id>/finalize {sha256}                          200, the artifact: the server re-hashes what it
                                                                   stored and refuses a mismatch itself (422)
  GET    /v1/files/<id>/url                                        200 {url, expires_at}: a presigned GET
  DELETE /v1/files/<id>                                            204, the owner only

The POSTs and the DELETE carry a DPoP proof: apps/peers/auth requires one on every write method. A presigned URL gets
neither the bearer nor a proof (its token IS its authority, and this node's JWT goes only to the server it was minted
on), and a redirect from one is not followed: the bytes go to, and come from, the URL the server named.

WHAT IS CHECKED. `upload` hashes the file as it reads it, sends exactly that many bytes, and after finalize compares the
artifact the server answers (its id, `ready`, its size and its sha256) with what it computed: a server that finalized
other bytes is refused, not cited. `download` compares what it received with the size and sha256 IN THE MESSAGE, never
with what the file service says of itself, so a file replaced under the same id is caught. That sha256 is the WRITER's
word only when `read` verified the writer's signature (`signature_verified`), which `fetch` prints beside its result.

WHO MAY DOWNLOAD, TODAY (apps/files/service.authorize_read): a peer of the file's owner (the uploader's OWNER), and the
caller or the target of a skill call whose args_attachments or result_attachments reference the file; anyone else is
answered 404 E_FILE_NOT_FOUND. Being named in the message is not part of the rule: every peer of the sender's owner
can download the file whether or not the message names it (recipient-scoped access is issue #118). So nothing private
is attached: no firmware, no calibration, no keys.

Errors a caller acts on are `AttachFailed` (its `error` is the typed record) or `node.Refused` (the client would not
send: a path that is not a regular file, a name the schema refuses). `cli.py` maps them.
"""
from __future__ import annotations

import errno
import functools
import hashlib
import re
import http.client
import json
import mimetypes
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import errors, filenames, node, wire

# Read at call time by the filename helpers; tests can model a Windows receiver
# without changing os.name (which also changes pathlib's concrete path class).
_IS_WINDOWS = os.name == "nt"

#: Read and hashed at a time; the server's finalize streams the same size (service.FINALIZE_BUF_SIZE).
CHUNK = 64 * 1024
#: The content type a name mimetypes cannot place (or places only inside a compression) is declared as. The file
#: service stores it and serves every raw download as application/octet-stream regardless (views.raw), so it is a
#: label, not a decision.
DEFAULT_CONTENT_TYPE = "application/octet-stream"
FILES_ROUTE = "/v1/files"
#: A download is written under this prefix in the out directory and takes its real name only once verified.
TEMP_PREFIX = ".meshy-fetch-"
TEMP_SUFFIX = ".part"
#: How much of a refusal's body is kept in the typed record: the file service answers a small JSON error.
MAX_ERROR_BODY = 4096
#: The flag that makes a rename fail with EEXIST instead of replacing its destination: renamex_np(2)'s RENAME_EXCL
#: (macOS <stdio.h>) and renameat2(2)'s RENAME_NOREPLACE (Linux <linux/fs.h>, its paths taken from the working
#: directory by AT_FDCWD). See `_rename_noreplace`.
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
_AT_FDCWD = -100

#: The typed errors. Every record is {"error": <one of these>, "step": <where>, ...}; `step` is create, upload,
#: finalize, presign, download, check, place or cleanup.
E_ATTACH_HTTP = "E_ATTACH_HTTP"                # a files route or a presigned URL answered, and not with the success
                                               # the step needs (its status and body are in the record)
E_ATTACH_UNREACHABLE = "E_ATTACH_UNREACHABLE"  # it could not be reached, or what came back was not a complete response
E_ATTACH_MISMATCH = "E_ATTACH_MISMATCH"        # the bytes are not the ones computed (upload) or named (download)
E_ATTACH_EXISTS = "E_ATTACH_EXISTS"            # fetch: the out directory already holds that name; nothing is replaced
E_ATTACH_INVALID = "E_ATTACH_INVALID"          # fetch: the message's reference is not the v1 shape, so nothing named
                                               # by it is used (a row the server stored unchecked, divergence 011)
E_ATTACH_LOCAL = "E_ATTACH_LOCAL"              # this disk failed. upload: a read of the file after its artifact was
                                               # created (the rewind, the bytes for the PUT, the close); fetch: the
                                               # write or its close, or a filesystem that cannot give the name without
                                               # risking a replace (`_place`)
E_ATTACH_CLEANUP = "E_ATTACH_CLEANUP"          # fetch: the filesystem refused to delete the temporary file, which
                                               # stays at `temp_path` (`_remove_temp`)
E_ATTACH_NONE = "E_ATTACH_NONE"                # fetch: the message cites no attachment


class AttachFailed(Exception):
    """An attachment was not uploaded, or not fetched. `error` is the typed record."""

    def __init__(self, error: dict):
        self.error = error
        super().__init__(json.dumps(error))


class CleanupFailed(AttachFailed):
    """`download` verified the attachment and gave it its name, at `path`, but the filesystem refused to delete the
    temporary file (E_ATTACH_CLEANUP; the record's `temp_path` names it). The file at `path` is the verified one."""

    def __init__(self, error: dict, path: Path):
        self.path = path
        super().__init__(error)


class _Changed(Exception):
    """The local file ended before the size it was hashed at: it changed while it was uploaded. Not an OSError on
    purpose, so urllib does not fold it into a URLError that would read as an unreachable server."""


class _ReadFailed(Exception):
    """A read of the local file failed while urllib was sending it; the OSError is its `__cause__`. Not an OSError,
    for `_Changed`'s reason: a disk's EIO is E_ATTACH_LOCAL, not an unreachable server."""


def _failed(code: str, step: str, **fields) -> AttachFailed:
    return AttachFailed({"error": code, "step": step, **fields})


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _canonical_id(raw) -> str | None:
    """The file id in the one spelling the schema accepts, or None when it is not a UUID."""
    try:
        return str(uuid.UUID(str(raw)))
    except ValueError:
        return None


def _call(state_file, state: dict, method: str, route: str, *, step: str, name: str, body=None,
          prove: bool = True):
    """(status, parsed body) of one files route with this node's bearer, and with `prove` a DPoP proof for exactly
    this method and path. The sign-in `fresh_jwt` runs when the token is due is part of the step: its refusal and an
    unreachable server are typed here, so `send --attach` reports every failure the same way."""
    try:
        jwt = node.fresh_jwt(state_file, state)
        headers = node._proved_headers(state, jwt, method, route) if prove else node._bearer(jwt)
        return node.http_json(method, state["base_url"], route, body=body, headers=headers)
    except node.ServerSaid as exc:
        raise _failed(E_ATTACH_HTTP, step, name=name, http_status=exc.status, body=exc.body,
                      detail="the sign-in for a fresh token was refused")
    except OSError as exc:
        raise _failed(E_ATTACH_UNREACHABLE, step, name=name, detail=f"{type(exc).__name__}: {exc}")


def _presigned(state: dict, url, *, step: str, **record) -> str:
    """The URL a presign named. The server builds both kinds absolute (fs: MESHY_PUBLIC_BASE_URL plus
    /v1/files/raw/<token>; r2: the bucket's). A bare path is taken under the base URL, as every route is; anything
    that is neither is not followed.

    The URL is checked as the transfer would read it, so one it cannot follow is this step's E_ATTACH_HTTP, never a
    ValueError: `urlsplit` raises one itself (`http://[`), as does a port out of range or not a number, a host label
    the IDNA encoding the socket layer applies refuses (empty, over 63 characters), and any character that is not
    ASCII (http.client encodes the request line in ASCII and the Host header in Latin-1). Codex's probe of the earlier
    code: `http://[` from a presign escaped `download` and `fetch` raw, so the attachments after it were never tried
    and no summary was printed."""
    if isinstance(url, str) and url.startswith("/") and not url.startswith("//"):
        url = state["base_url"] + url
    problem = _url_problem(url) if isinstance(url, str) else "the server named no http(s) URL for the bytes"
    if problem:
        raise _failed(E_ATTACH_HTTP, step, **record, detail=problem)
    return url


def _url_problem(url: str) -> str | None:
    """Why the transfer could not follow `url` (`_presigned`), or None. The URL itself is never in the record: it is a
    capability."""
    if not url.isascii():
        return "the server named a URL for the bytes that is not ASCII"
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "the server named no http(s) URL for the bytes"
        _ = parts.port                                 # read for its ValueError: out of range, or not a number
        parts.hostname.encode("idna")                  # the socket layer's encoding: an empty or too long label
    except ValueError as exc:                          # UnicodeError is one
        return f"the server named a URL for the bytes that cannot be followed ({_why(exc)})"
    return None


def _error_body(raw: bytes):
    text = raw[:MAX_ERROR_BODY].decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _refusal(exc: urllib.error.HTTPError) -> dict:
    """The record's fields for a presigned URL's refusal: its `http_status` and `body`. At most MAX_ERROR_BODY bytes
    of the body are read, and the read may fail as any read can: cut short (http.client.IncompleteRead, which is not
    an OSError), a timeout, a reset. Then `body` is null and `detail` says why, and the refusal is still typed. Raised
    from inside the HTTPError handler, that failure escaped its sibling handlers raw, so `upload` deleted nothing and
    `cli._attach` left the earlier uploads in place. The refusal's connection is closed on every path."""
    fields = {"http_status": exc.code}
    try:
        raw = exc.read(MAX_ERROR_BODY)
    except (OSError, http.client.HTTPException, ValueError) as failure:
        fields.update({"body": None,
                       "detail": f"the refusal's body could not be read ({type(failure).__name__}: {failure})"})
    else:
        fields["body"] = _error_body(raw)
    finally:
        try:
            exc.close()
        except (OSError, http.client.HTTPException):
            pass
    return fields


class _Exactly:
    """A file object that yields exactly `size` bytes, then EOF. The request declares that Content-Length (an r2
    presigned PUT signs it), so a file that grew since it was hashed sends the bytes that were hashed and no more, and
    one that shrank raises `_Changed` rather than leaving the server waiting for the rest. A read that fails raises
    `_ReadFailed`."""

    def __init__(self, fh, size: int):
        self._fh, self._left = fh, size

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        want = self._left if n is None or n < 0 else min(n, self._left)
        try:
            chunk = self._fh.read(want)
        except OSError as exc:
            raise _ReadFailed() from exc
        if not chunk:
            raise _Changed()
        self._left -= len(chunk)
        return chunk


# --------------------------------------------------------------------------- upload

def _digest(fh) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    while True:
        chunk = fh.read(CHUNK)
        if not chunk:
            return size, digest.hexdigest()
        digest.update(chunk)
        size += len(chunk)


def local_problem(path) -> str | None:
    """Why `send --attach` refuses this path before anything is uploaded, or None: not a regular file, or a name the
    schema would refuse (wire.attachment_name_problem), Windows cannot save (filenames.windows_name_problem),
    a signed message cannot carry (filenames.surrogate_name_problem) or a 255-byte filesystem cannot save
    (filenames.utf8_name_problem).
    The Windows rules apply on every platform. `cli` asks it of every path first, so a bad second path never costs
    an upload of the first. The server's schema is unchanged."""
    path = Path(path)
    if not path.is_file():
        return f"--attach {path}: not a regular file"
    problem = (wire.attachment_name_problem(path.name) or filenames.windows_name_problem(path.name)
               or filenames.surrogate_name_problem(path.name) or filenames.utf8_name_problem(path.name))
    if problem:
        return f"--attach {path}: {problem}"
    return None


def upload(state_file, state: dict, path) -> dict:
    """Upload one file and return its reference, {file_id, name, size_bytes, sha256}, the shape `node.send`'s
    `attachments` takes. The file is hashed as it is read, created with its exact size, PUT to the URL the server named
    with the headers it named, and finalized with that hash; the artifact the server answers must be this id, `ready`,
    this size and this sha256.

    Raises node.Refused before anything is sent (`local_problem`, an unreadable file), and AttachFailed after. From
    the moment the create answers with an artifact id, every way out but success deletes that artifact again, best
    effort (`discard`): the rest of that answer refused, a local read that fails (the rewind after hashing, a read for
    the PUT, the close: E_ATTACH_LOCAL), the transfer, the finalize, and anything raised that no step types. That
    moment is any success that names a valid id, a 200 as much as the 201 the step needs (`_create`). An
    AttachFailed's record names the artifact (`file_id`) and says whether it went (`discarded`); any other exception
    carries both as a note."""
    node.require_peer(state_file, state)
    path = Path(path)
    problem = local_problem(path)
    if problem:
        raise node.Refused(problem)
    name = path.name
    try:
        fh = path.open("rb")
    except OSError as exc:
        raise node.Refused(f"--attach {path}: cannot be read ({_why(exc)})")
    file_id = None                                     # once set, the artifact is this call's to delete
    try:
        try:
            size, sha256 = _digest(fh)
        except OSError as exc:
            raise node.Refused(f"--attach {path}: cannot be read ({_why(exc)})")
        if size > wire.MAX_ATTACHMENT_SIZE_BYTES:
            raise node.Refused(f"--attach {path}: {size} bytes is over what a reference can carry")
        guessed, encoding = mimetypes.guess_type(name)
        # `x.tar.gz` guesses application/x-tar with encoding gzip: the type of what is INSIDE, so not the label.
        content_type = guessed if guessed and not encoding else DEFAULT_CONTENT_TYPE
        file_id, status, created = _create(state_file, state, name, content_type, size)
        url, headers = _upload_target(state, status, created, name=name, file_id=file_id)
        try:
            fh.seek(0)
        except OSError as exc:
            raise _failed(E_ATTACH_LOCAL, "upload", name=name, file_id=file_id,
                          detail=f"the rewind after hashing failed ({_why(exc)})")
        _put(url, headers, _Exactly(fh, size), size, name=name, file_id=file_id)
        try:
            fh.close()
        except OSError as exc:
            raise _failed(E_ATTACH_LOCAL, "upload", name=name, file_id=file_id,
                          detail=f"the file's close failed ({_why(exc)})")
        _finalize(state_file, state, file_id, name=name, size=size, sha256=sha256)
    except AttachFailed as exc:
        if file_id is not None:
            exc.error.setdefault("file_id", file_id)
            exc.error["discarded"] = discard(state_file, state, file_id)
        raise
    except BaseException as exc:
        if file_id is not None:
            exc.add_note(json.dumps({"file_id": file_id, "discarded": discard(state_file, state, file_id)}))
        raise
    finally:
        try:
            fh.close()                                 # a no-op after the close above
        except OSError:
            pass                                       # the failure being raised is the one to report
    # A later send failure must not invite a repeat that uploads another ready artifact.
    errors.note_call_accepted()
    return {"file_id": file_id, "name": name, "size_bytes": size, "sha256": sha256}


def _create(state_file, state: dict, name: str, content_type: str, size: int) -> tuple[str, int, dict]:
    """POST /v1/files (views.create): the id of the pending artifact the server created, the status it answered, and
    its whole answer. Only the id is read here, from ANY success (2xx) that carries one: from that id on the artifact
    is the caller's to delete, so `_upload_target` checks the rest of the answer, the 201 itself included, under
    `upload`'s cleanup. Codex's probe of the earlier code: a 200 with a valid id was refused here, before `upload`
    held the id, so the artifact it named was never deleted."""
    status, body = _call(state_file, state, "POST", FILES_ROUTE, step="create", name=name,
                         body={"filename": name, "content_type": content_type, "size_bytes_declared": size})
    if not 200 <= status < 300 or not isinstance(body, dict):
        raise _failed(E_ATTACH_HTTP, "create", name=name, http_status=status, body=body)
    file_id = _canonical_id(body.get("id"))
    if file_id is None:
        raise _failed(E_ATTACH_HTTP, "create", name=name, http_status=status,
                      detail=f"the {status} carries no file id")
    return file_id, status, body


#: RFC 9110 field-name (a `token`) and a field value urllib can put on the wire: Latin-1, no CR, LF or NUL. A pair
#: that is not one makes urllib raise a raw ValueError at the PUT (GitHub Codex review of #123, P2), so it is refused
#: here, typed, inside the cleanup.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _header_ok(name: str, value: str) -> bool:
    return (_HEADER_NAME.fullmatch(name) is not None
            and not any(ch in value for ch in "\r\n\x00")
            and all(ord(ch) < 256 for ch in value))


def _upload_target(state: dict, status: int, created: dict, *, name: str, file_id: str) -> tuple[str, list]:
    """Where the create's 201 says to PUT the bytes: the URL (`_presigned`) and its [name, value] headers. Any other
    success is refused here, after `_create` handed its id to the cleanup. The answer's body is not in that record:
    its upload URL is a live capability for the pending artifact."""
    if status != 201:
        raise _failed(E_ATTACH_HTTP, "create", name=name, file_id=file_id, http_status=status,
                      detail=f"the create answered {status} with an artifact id, not 201 Created")
    headers = created.get("upload_headers")
    pairs = (isinstance(headers, list)
             and all(isinstance(h, list) and len(h) == 2 and all(isinstance(x, str) for x in h)
                     for h in headers))
    if not pairs:
        raise _failed(E_ATTACH_HTTP, "create", name=name, file_id=file_id, http_status=201,
                      detail="the 201 carries no [name, value] upload headers")
    if not all(_header_ok(k, v) for k, v in headers):
        raise _failed(E_ATTACH_HTTP, "create", name=name, file_id=file_id, http_status=201,
                      detail="the 201 carries an upload header that is not a valid HTTP field")
    url = _presigned(state, created.get("upload_url"), step="create", name=name, file_id=file_id)
    return url, [(k, v) for k, v in headers]


def _put(url: str, headers: list, data: _Exactly, size: int, *, name: str, file_id: str) -> None:
    """The bytes, to the presigned URL, with its headers verbatim and the exact Content-Length."""
    request = urllib.request.Request(url, data=data, method="PUT")
    for header, value in headers:
        request.add_header(header, value)
    request.add_header("Content-Length", str(size))
    try:
        with node._opener(follow_redirects=False).open(request, timeout=node.HTTP_TIMEOUT_S) as response:
            status = response.status
            response.read()
    except urllib.error.HTTPError as exc:
        raise _failed(E_ATTACH_HTTP, "upload", name=name, file_id=file_id, **_refusal(exc))
    except _Changed:
        raise _failed(E_ATTACH_MISMATCH, "upload", name=name, file_id=file_id,
                      detail="the file is shorter than when it was hashed: it changed while it was uploaded")
    except _ReadFailed as exc:
        raise _failed(E_ATTACH_LOCAL, "upload", name=name, file_id=file_id,
                      detail=f"a read of the file failed ({_why(exc.__cause__)})")
    except (OSError, http.client.HTTPException) as exc:
        raise _failed(E_ATTACH_UNREACHABLE, "upload", name=name, file_id=file_id,
                      detail=f"{type(exc).__name__}: {exc}")
    except ValueError as exc:                          # urllib refusing what `_upload_target` let through
        raise _failed(E_ATTACH_HTTP, "upload", name=name, file_id=file_id,
                      detail=f"the request could not be sent ({type(exc).__name__})")
    if not 200 <= status < 300:
        raise _failed(E_ATTACH_HTTP, "upload", name=name, file_id=file_id, http_status=status)


def _finalize(state_file, state: dict, file_id: str, *, name: str, size: int, sha256: str) -> None:
    """POST /v1/files/<id>/finalize (views.finalize) with the hash computed here. The server refuses bytes that do
    not hash to it (422) on its own; this refuses an answer that is not THIS artifact, ready, at this size and hash."""
    route = f"{FILES_ROUTE}/{file_id}/finalize"
    status, body = _call(state_file, state, "POST", route, step="finalize", name=name, body={"sha256": sha256})
    if status != 200 or not isinstance(body, dict):
        raise _failed(E_ATTACH_HTTP, "finalize", name=name, file_id=file_id, http_status=status, body=body)
    got = {"id": _canonical_id(body.get("id")), "status": body.get("status"),
           "size_bytes": body.get("size_bytes") if type(body.get("size_bytes")) is int else None,
           "sha256": body.get("sha256")}
    want = {"id": file_id, "status": "ready", "size_bytes": size, "sha256": sha256}
    differ = sorted(field for field in want if got[field] != want[field])
    if differ:
        raise _failed(E_ATTACH_MISMATCH, "finalize", name=name, file_id=file_id, fields=differ,
                      expected={f: want[f] for f in differ}, answered={f: body.get(f) for f in differ})


def discard(state_file, state: dict, file_id: str) -> bool:
    """DELETE /v1/files/<id>, best effort: an artifact this command created and will not cite. True when the server
    answered 204. Never raises: it runs while another failure is being reported, and must not replace it. A pending
    artifact left behind is swept by the server after MESHYCHAT_UPLOAD_WINDOW_SECS; a ready one stays until deleted."""
    try:
        status, _ = _call(state_file, state, "DELETE", f"{FILES_ROUTE}/{file_id}", step="discard", name="")
    except Exception:                                  # noqa: BLE001 - see the docstring
        return False
    return status == 204


# --------------------------------------------------------------------------- download

def named_in(out_dir, name: str, *, windows: bool | None = None):
    """`out_dir / name` when this platform reads that as a file called `name` directly in `out_dir`, else None. The
    schema's rules (no separator, not `.` or `..`) make that so on POSIX; on Windows a name the schema accepts can
    still be more than a name: `C:x` joined to `out` is `C:x`, a path on drive C:. Pure, so a test can hand it a
    PureWindowsPath on any platform. Windows receivers also refuse Windows-unsafe names before the transfer;
    POSIX receivers retain access to older attachments with names their filesystem can save."""
    windows = _IS_WINDOWS if windows is None else windows
    if windows and filenames.windows_name_problem(name):
        return None
    final = out_dir / name
    return final if final.name == name and final.parent == out_dir else None


def download(state_file, state: dict, attachment, out_dir, *, windows: bool | None = None) -> Path:
    """Download one attachment into `out_dir` under its name and return that path.

    Attachment metadata is checked first (wire.attachment_problem: a received name is used on this disk only when it is a
    plain name). Windows receivers refuse Windows-unsafe names; POSIX receivers refuse a name the filesystem
    encoding cannot write or that is over 255 bytes.
    Then a file of that name already there is refused before anything is asked of the server. The bytes
    are streamed into a temporary file in `out_dir`, hashed and counted as they arrive (more than `size_bytes` stops
    the transfer), that file is closed with its close checked (`_receive_file`), the bytes are compared with the
    attachment's size and sha256, and only then given the name, by a call that fails rather than overwrite a file that
    appeared meanwhile (`_place`). Every failure this disk causes is E_ATTACH_LOCAL, never a raw OSError, so `fetch`
    goes on to the next attachment and prints its summary.

    The temporary file is removed on every path out, best effort: a filesystem can refuse the deletion (EACCES, EIO
    on a share), and then the file stays and is NAMED, never left silently (`_remove_temp`, E_ATTACH_CLEANUP with
    its `temp_path`). After a failed download that record is the failure's `cleanup`, and the failure is still the
    one reported; after a published one `CleanupFailed` is raised, whose `path` is the verified file, which stays.
    Raises AttachFailed (CleanupFailed included), or node.Refused when `out_dir` is not a directory."""
    node.require_peer(state_file, state)
    windows = _IS_WINDOWS if windows is None else windows
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise node.Refused(f"{out_dir} is not a directory")
    problem = wire.attachment_problem(attachment)
    if problem:
        raise _failed(E_ATTACH_INVALID, "check", detail=problem)
    file_id, name = attachment["file_id"], attachment["name"]
    size, sha256 = attachment["size_bytes"], attachment["sha256"]
    record = {"name": name, "file_id": file_id}
    problem = filenames.windows_name_problem(name) if windows else filenames.utf8_name_problem(name)
    if problem:
        raise _failed(E_ATTACH_INVALID, "check", **record, detail=problem)
    final = named_in(out_dir, name, windows=windows)
    if final is None:
        raise _failed(E_ATTACH_INVALID, "check", **record,
                      detail="the name is not a plain file name on this platform")
    if os.path.lexists(final):
        raise _failed(E_ATTACH_EXISTS, "check", **record, path=str(final))

    status, body = _call(state_file, state, "GET", f"{FILES_ROUTE}/{file_id}/url", step="presign", name=name,
                         prove=False)
    if status != 200 or not isinstance(body, dict):
        raise _failed(E_ATTACH_HTTP, "presign", **record, http_status=status, body=body)
    url = _presigned(state, body.get("url"), step="presign", **record)

    try:
        fd, tmp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=str(out_dir))
    except OSError as exc:
        raise _failed(E_ATTACH_LOCAL, "download", **record, detail=f"{type(exc).__name__}: {exc}")
    try:
        got_size, got_sha = _receive_file(fd, url, size, record)
        if got_size != size or got_sha != sha256:
            raise _failed(E_ATTACH_MISMATCH, "download", **record,
                          expected={"size_bytes": size, "sha256": sha256},
                          received={"size_bytes": got_size, "sha256": got_sha})
        _place(tmp, final, record)
    except BaseException as exc:
        left = _remove_temp(tmp)
        if left is not None:                           # the failure raised stays the one reported
            if isinstance(exc, AttachFailed):
                exc.error["cleanup"] = left
            else:
                exc.add_note(json.dumps(left))
        raise
    left = _remove_temp(tmp)          # after a link the name holds the bytes; after a rename this is already gone
    if left is not None:
        raise CleanupFailed({**left, **record, "path": str(final)}, final)
    return final


def _remove_temp(tmp: str) -> dict | None:
    """Delete a download's temporary file: None once it is gone, or the E_ATTACH_CLEANUP record naming it when the
    filesystem refused. Only a path that is already missing is quiet (after `_place` renamed it, the name moved);
    every other error (EACCES, EPERM, EIO) means the file is still there, and saying nothing would hide it."""
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"error": E_ATTACH_CLEANUP, "step": "cleanup", "temp_path": tmp,
                "detail": f"the temporary file could not be deleted ({_why(exc)}); delete it by hand"}
    return None


def _receive_file(fd: int, url: str, size: int, record: dict) -> tuple[int, str]:
    """`_receive` into the temporary file open on `fd`, which is closed here on every path out. The close is checked
    as a write is: the file object is buffered, so a small download, or the last chunk of a large one, is accepted by
    `write` and reaches the disk only at the flush the close does, where ENOSPC or an I/O error (on a network share,
    a delayed write's) surfaces. Raised raw, that error would escape `fetch`'s handling of one attachment, skip the
    ones after it and lose the summary, and `cli` would call a full disk unreachable; here it is E_ATTACH_LOCAL. A
    close that fails while another failure is already on its way out does not replace that failure."""
    try:
        out = os.fdopen(fd, "wb")
    except OSError as exc:
        try:
            os.close(fd)              # as tempfile.NamedTemporaryFile does when the same open fails
        except OSError:
            pass
        raise _failed(E_ATTACH_LOCAL, "download", **record, detail=f"{type(exc).__name__}: {exc}")
    try:
        received = _receive(url, out, size, record)
    except BaseException:
        try:
            out.close()
        except OSError:
            pass                      # the failure being raised is the one to report
        raise
    try:
        out.close()
    except OSError as exc:
        raise _failed(E_ATTACH_LOCAL, "download", **record, detail=f"{type(exc).__name__}: {exc}")
    return received


def _receive(url: str, out, size: int, record: dict) -> tuple[int, str]:
    """GET the presigned URL into `out`: (bytes received, their sha256). Stops at the first byte past `size`."""
    request = urllib.request.Request(url, method="GET")
    try:
        response = node._opener(follow_redirects=False).open(request, timeout=node.HTTP_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        raise _failed(E_ATTACH_HTTP, "download", **record, **_refusal(exc))
    except (OSError, http.client.HTTPException) as exc:
        raise _failed(E_ATTACH_UNREACHABLE, "download", **record, detail=f"{type(exc).__name__}: {exc}")
    digest, total = hashlib.sha256(), 0
    with response:
        if response.status != 200:
            raise _failed(E_ATTACH_HTTP, "download", **record, http_status=response.status)
        while True:
            try:
                chunk = response.read(CHUNK)
            except (OSError, http.client.HTTPException) as exc:
                raise _failed(E_ATTACH_UNREACHABLE, "download", **record, detail=f"{type(exc).__name__}: {exc}")
            if not chunk:
                return total, digest.hexdigest()
            total += len(chunk)
            if total > size:
                raise _failed(E_ATTACH_MISMATCH, "download", **record,
                              detail=f"the server sent more than the {size} bytes the message names")
            digest.update(chunk)
            try:
                out.write(chunk)
            except OSError as exc:
                raise _failed(E_ATTACH_LOCAL, "download", **record, detail=f"{type(exc).__name__}: {exc}")


def _place(tmp: str, final: Path, record: dict) -> None:
    """Give the verified temporary file its name, never replacing a file. Both ways it is tried fail when the name
    exists AT THE MOMENT OF PUBLISHING, decided by the kernel inside the one call, never by a check before it: a hard
    link, and where the filesystem has none (FAT, some network shares) a rename that refuses to replace
    (`_rename_noreplace`). Where neither works the download is refused, E_ATTACH_LOCAL, and not published some other
    way. In particular not by reserving the name (O_EXCL) and renaming over the reservation: once the reservation's
    descriptor is closed, another process can put its own file under that name, and the rename would silently
    replace it, as removing the "reservation" after a failed rename would delete it. Nothing here removes or
    replaces anything at `final`."""
    try:
        os.link(tmp, final)
        return
    except FileExistsError:
        raise _failed(E_ATTACH_EXISTS, "place", **record, path=str(final))
    except OSError as exc:
        linked = f"{type(exc).__name__}: {exc}"
    try:
        _rename_noreplace(tmp, final)
    except FileExistsError:
        raise _failed(E_ATTACH_EXISTS, "place", **record, path=str(final))
    except OSError as exc:
        raise _failed(E_ATTACH_LOCAL, "place", **record,
                      detail=f"this filesystem gave the file its name neither by a hard link ({linked}) nor by a "
                             f"rename that cannot replace a file ({type(exc).__name__}: {exc}); nothing was "
                             "replaced. Fetch into a directory on another filesystem")


def _rename_noreplace(src, dst) -> None:
    """Rename `src` to `dst`, or raise FileExistsError when `dst` exists, decided by the kernel inside the rename,
    never by a check before it. Windows' own rename is that (MoveFileExW without MOVEFILE_REPLACE_EXISTING); macOS
    and Linux have it as renamex_np(RENAME_EXCL) and renameat2(RENAME_NOREPLACE), reached through the C library.
    Raises OSError(ENOTSUP) on a platform with neither, and the call's own error where the filesystem does not
    implement it (ENOTSUP on macOS, EINVAL on Linux): never falls back to a rename that replaces."""
    if os.name == "nt":
        os.rename(src, dst)
        return
    call = _libc_rename_noreplace()
    if call is None:
        raise OSError(errno.ENOTSUP, "this platform has no rename that refuses to replace a file")
    code = call(os.fsencode(src), os.fsencode(dst))
    if code:
        raise OSError(code, os.strerror(code), os.fspath(src), None, os.fspath(dst))


@functools.lru_cache(maxsize=1)
def _libc_rename_noreplace():
    """(src: bytes, dst: bytes) -> 0, or the errno it failed with: the C library's no-replace rename on this
    platform, or None when it has none (another platform, or a C library without the call: glibc has renameat2
    since 2.28)."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            renamex_np = libc.renamex_np
            renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            renamex_np.restype = ctypes.c_int

            def rename(src: bytes, dst: bytes) -> int:
                return renamex_np(src, dst, _RENAME_EXCL)
        elif sys.platform.startswith("linux"):
            renameat2 = libc.renameat2
            renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            renameat2.restype = ctypes.c_int

            def rename(src: bytes, dst: bytes) -> int:
                return renameat2(_AT_FDCWD, src, _AT_FDCWD, dst, _RENAME_NOREPLACE)
        else:
            return None
    except (ImportError, OSError, AttributeError):
        return None

    def call(src: bytes, dst: bytes) -> int:
        return 0 if rename(src, dst) == 0 else (ctypes.get_errno() or errno.EIO)
    return call


def fetch(state_file, state: dict, message: dict, out_dir) -> dict:
    """Every attachment of one message `node.read` returned, downloaded into `out_dir`: the summary `cli fetch`
    prints. One record per attachment: its reference, `path` when the verified file is in place, and its typed
    `error` when anything failed. Both for E_ATTACH_CLEANUP: the file verified and took its name, but its temporary
    file could not be deleted (the error's `temp_path`). One failed download does not stop the next, and a verified
    file already written stays. `ok` is true only when every attachment was fetched with no error, and false for a
    message that cites none (`error` E_ATTACH_NONE) or whose `attachments` is not the v1 shape (E_ATTACH_INVALID:
    then nothing is downloaded).

    `signature_verified` and `signed_by` are `read`'s: whether the sha256 each file was checked against is the
    WRITER's word, or only the server's."""
    summary = {"key": message.get("key"), "version": message.get("version"), "from": message.get("from"),
               "signature_verified": message.get("signature_verified"), "signed_by": message.get("signed_by")}
    if message.get("signature_verified") is not True:
        summary["signature_error"] = message.get("signature_error")
    summary.update({"out_dir": str(out_dir), "attachments": [], "ok": False})
    raw = message.get("attachments")
    if raw is None:
        summary["error"] = E_ATTACH_NONE
        return summary
    problem = wire.attachments_problem(raw)
    if problem:
        summary.update({"error": E_ATTACH_INVALID, "detail": problem})
        return summary
    for item in raw:
        record = {field: item[field] for field in ("file_id", "name", "size_bytes", "sha256")}
        try:
            record["path"] = str(download(state_file, state, item, out_dir))
        except CleanupFailed as exc:
            record.update({"path": str(exc.path), "error": exc.error})
        except AttachFailed as exc:
            record["error"] = exc.error
        summary["attachments"].append(record)
    summary["ok"] = all("path" in record and "error" not in record for record in summary["attachments"])
    return summary
