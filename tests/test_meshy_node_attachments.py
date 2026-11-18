"""`send --attach` and `fetch` (docs/DIVERGENCES.md 079): a file travels beside a mesh message through the file service.

The client (tools/meshy_node/files.py) is driven against a LOOPBACK fake of the file service that answers in the shapes
apps/files/views.py does (POST /v1/files 201 {id, upload_url, upload_headers, expires_at}; PUT and GET on
/v1/files/raw/<token>; POST /v1/files/<id>/finalize 200 with the artifact, 422 E_FILE_HASH_MISMATCH when the stored
bytes do not hash to the sha256 sent; GET /v1/files/<id>/url 200 {url, expires_at}; DELETE /v1/files/<id> 204), and
records every request. No database and no real server: `node.send` and `node.read` are replaced where a message is
sent or read, as tests/test_meshy_node_send_guard.py does.

Each refusal sits beside its control (docs/V1-DESIGN-RULES.md C1):

  * UPLOAD: the bytes, their size and sha256 arrive as computed; the POSTs carry a DPoP proof for exactly their path
    and the presigned PUT carries neither bearer nor proof. A finalize answer that is not THIS artifact at this size
    and hash is refused and the artifact deleted again, as is a 201 that names an id but nothing to PUT to (or a URL
    the transfer cannot follow), and a 200 that names an id; a server-side hash refusal is typed.
  * SEND --ATTACH: every file is uploaded before the message is sent, and any failure sends NOTHING, types the
    failure, and deletes what this command uploaded before it, also when the refusal's own body breaks off, when the
    file's own disk fails after the create, and when an upload raises something no step types. Local refusals cost
    no upload.
  * FETCH: the bytes are checked against the size and sha256 IN THE MESSAGE; a mismatch, an extra byte, an existing
    file and an unchecked row's path-like name are refused, and no temporary file is left behind unnamed: one the
    filesystem will not delete is a typed E_ATTACH_CLEANUP, and fetch exits 1. Where the filesystem has no hard
    links, a file another process puts under the name at the last moment is neither replaced nor deleted; and a disk
    that refuses the last flush, at the close, a refusal whose body breaks off, or a presigned URL the transfer
    cannot follow, is a typed failure of that one attachment.

All file contents are synthetic.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_attachments.py -q -p no:cacheprovider
"""
from __future__ import annotations

import errno
import hashlib
import http.server
import io
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, files, node, sendguard, wire  # noqa: E402

LOG = b"".join(b"[%06d] boot: synthetic serial line\n" % i for i in range(6000))      # ~210 KB, a serial log's size
JWT = "h.p.s"


# =============================================================================== a loopback file service

class FakeFiles:
    """The file service's routes, in apps/files/views.py's shapes, over a real socket. `fault` changes one answer."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.tokens: dict[str, tuple[str, str]] = {}
        self.seen: list[dict] = []
        self.fault: dict = {}
        self.creates = self.puts = self.raw_gets = self.presigns = 0
        self.release = threading.Event()
        world = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=None, raw=None):
                data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if raw is None else "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _refuse_broken(self, how):
                """A 500 whose body never arrives whole: `short` promises 100 bytes and sends 10, `chunked` cuts its
                one chunk short, `stalled` sends the headers and then nothing until the test ends."""
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                if how == "chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"64\r\n" + b'{"error":"')
                else:
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    if how == "short":
                        self.wfile.write(b'{"error":"')
                    else:
                        world.release.wait(30)
                self.close_connection = True

            def _record(self, body=None):
                world.seen.append({"method": self.command, "path": self.path, "body": body,
                                   "headers": {k.lower(): v for k, v in self.headers.items()}})

            def _json(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_POST(self):  # noqa: N802
                body = self._json()
                self._record(body)
                if self.path == "/v1/files":
                    world.creates += 1
                    if world.fault.get("create_status_on") == world.creates:
                        return self._reply(413, {"error": "E_FILE_SIZE_EXCEEDED", "message": "size exceeded: declared=1, max=0"})
                    fid, token = str(uuid.uuid4()), secrets.token_urlsafe(16)
                    world.objects[fid] = {"data": None, "status": "pending", "filename": body.get("filename"),
                                          "content_type": body.get("content_type"),
                                          "declared": body.get("size_bytes_declared")}
                    world.tokens[token] = ("PUT", fid)
                    created = {"id": fid, "upload_url": f"{world.base}/v1/files/raw/{token}",
                               "upload_headers": [["Content-Type", body.get("content_type")]],
                               "expires_at": "2099-01-01T00:00:00Z"}
                    created.update(world.fault.get("create_says", {}))
                    return self._reply(world.fault.get("create_status", 201), created)
                m = re.fullmatch(r"/v1/files/([0-9a-f-]{36})/finalize", self.path)
                if m and m.group(1) in world.objects:
                    obj = world.objects[m.group(1)]
                    actual = hashlib.sha256(obj["data"] or b"").hexdigest()
                    if body.get("sha256") != actual:
                        obj["status"] = "failed"
                        return self._reply(422, {"error": "E_FILE_HASH_MISMATCH", "message": "hash mismatch"})
                    obj["status"] = "ready"
                    view = {"id": m.group(1), "owner_id": str(uuid.UUID(int=1)), "uploaded_by_peer_id": None,
                            "sha256": actual, "size_bytes": len(obj["data"]), "content_type": obj["content_type"],
                            "filename": obj["filename"], "status": "ready", "created_at": "2026-10-05T12:00:00Z",
                            "finalized_at": "2026-10-05T12:00:01Z"}
                    view.update(world.fault.get("finalize_says", {}))
                    return self._reply(200, view)
                return self._reply(404, {"error": "E_FILE_NOT_FOUND", "message": "file not found"})

            def do_PUT(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n)
                self._record(len(data))
                token = self.path.rsplit("/", 1)[-1]
                op, fid = world.tokens.get(token, (None, None))
                if op != "PUT":
                    return self._reply(401, {"error": "E_AUTH_INVALID", "message": "token invalid"})
                world.puts += 1
                broken = world.fault.get("put_refused_on")
                if broken and broken[0] == world.puts:
                    return self._refuse_broken(broken[1])
                world.objects[fid]["data"] = world.fault.get("store_instead", data)
                return self._reply(200)

            def do_GET(self):  # noqa: N802
                self._record()
                m = re.fullmatch(r"/v1/files/([0-9a-f-]{36})/url", self.path)
                if m:
                    obj = world.objects.get(m.group(1))
                    if obj is None or world.fault.get("presign_404"):
                        return self._reply(404, {"error": "E_FILE_NOT_FOUND", "message": "file not found"})
                    world.presigns += 1
                    named = world.fault.get("presign_url_on")
                    if named and named[0] == world.presigns:
                        return self._reply(200, {"url": named[1], "expires_at": "2099-01-01T00:00:00Z"})
                    token = secrets.token_urlsafe(16)
                    world.tokens[token] = ("GET", m.group(1))
                    return self._reply(200, {"url": f"{world.base}/v1/files/raw/{token}",
                                             "expires_at": "2099-01-01T00:00:00Z"})
                token = self.path.rsplit("/", 1)[-1] if self.path.startswith("/v1/files/raw/") else ""
                op, fid = world.tokens.get(token, (None, None))
                if op != "GET":
                    return self._reply(401, {"error": "E_AUTH_INVALID", "message": "token invalid"})
                world.raw_gets += 1
                broken = world.fault.get("get_refused_on")
                if broken and broken[0] == world.raw_gets:
                    return self._refuse_broken(broken[1])
                data = world.objects[fid]["data"]
                return self._reply(200, raw=world.fault.get("serve", data))

            def do_DELETE(self):  # noqa: N802
                self._record()
                if world.fault.get("delete_refused"):
                    return self._reply(503, {"error": "E_UNAVAILABLE", "message": "synthetic"})
                m = re.fullmatch(r"/v1/files/([0-9a-f-]{36})", self.path)
                if m and world.objects.pop(m.group(1), None) is not None:
                    self.send_response(204)
                    self.end_headers()
                    return None
                return self._reply(404, {"error": "E_FILE_NOT_FOUND", "message": "file not found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def routes(self) -> list[tuple[str, str]]:
        return [(r["method"], re.sub(r"/raw/[^/]+$", "/raw/<token>", r["path"])) for r in self.seen]

    def put(self, data: bytes) -> dict:
        """An artifact uploaded and finalized out of band: a message reference."""
        fid = str(uuid.uuid4())
        self.objects[fid] = {"data": data, "status": "ready", "filename": None, "content_type": None, "declared": None}
        return {"file_id": fid, "name": "serial.log", "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}


@pytest.fixture()
def service():
    world = FakeFiles()
    try:
        yield world
    finally:
        world.release.set()
        world.server.shutdown()
        world.server.server_close()


def _state(tmp_path: Path, base_url: str) -> tuple[Path, dict]:
    """A minted node's state file for `base_url`, its token valid for decades, so nothing signs in."""
    seed = wire.new_seed()
    state = {"schema": node.STATE_SCHEMA, "base_url": base_url, "status": node.STATUS_MINTED,
             "signing_seed_b64url": wire.b64u(seed), "public_key_b64url": wire.b64u(wire.public_key_of(seed)),
             "device_label": "test", "memory_peer_id": node.DEFAULT_MEMORY_PEER_ID,
             "peer_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4()), "kid": "test-kid",
             "jwt": JWT, "jwt_expires_at": "2099-01-01T00:00:00Z", "label": "desktop-a"}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path, state


def _proof_binds(proof: str) -> tuple[str, str]:
    """(method, target) a DPoP proof is bound to (wire.dpop_proof's layout)."""
    raw = wire.b64u_decode(proof)
    (ml,) = struct.unpack(">H", raw[:2])
    (tl,) = struct.unpack(">H", raw[2 + ml:4 + ml])
    return raw[2:2 + ml].decode(), raw[4 + ml:4 + ml + tl].decode()


def _dead_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Os:
    """files.py's `os` with some calls replaced (keyword arguments); everything else is the real module. Set as
    `files.os`, so nothing outside files.py sees the change."""

    def __init__(self, **replaced):
        self.__dict__.update(replaced)

    def __getattr__(self, name):
        return getattr(os, name)


#: Whether this platform has a rename that refuses to replace (files._rename_noreplace): Windows' own rename, or the
#: C library's renamex_np (macOS) or renameat2 (Linux, glibc 2.28 on).
NO_REPLACE_RENAME = os.name == "nt" or files._libc_rename_noreplace() is not None


# =============================================================================== upload

def test_upload_sends_the_bytes_and_returns_a_reference_the_server_accepts(service, tmp_path):
    """The happy path, measured with instruments other than the one under test: hashlib over the file, the fake's
    record of what arrived, and the SERVER's validator over that reference."""
    path, state = _state(tmp_path, service.base)
    log = tmp_path / "serial-2026-10-05.log"
    log.write_bytes(LOG)

    got = files.upload(path, state, log)

    assert got == {"file_id": got["file_id"], "name": "serial-2026-10-05.log", "size_bytes": len(LOG),
                   "sha256": hashlib.sha256(LOG).hexdigest()}
    assert str(uuid.UUID(got["file_id"])) == got["file_id"]
    assert service.objects[got["file_id"]]["data"] == LOG and service.objects[got["file_id"]]["status"] == "ready"
    assert service.routes() == [("POST", "/v1/files"), ("PUT", "/v1/files/raw/<token>"),
                                ("POST", f"/v1/files/{got['file_id']}/finalize")]
    create, put, finalize = service.seen
    # The content type is the platform's mimetypes guess for `.log` (a label: the raw route serves octet-stream).
    assert set(create["body"]) == {"filename", "content_type", "size_bytes_declared"}
    assert create["body"]["filename"] == "serial-2026-10-05.log" and create["body"]["content_type"]
    assert create["body"]["size_bytes_declared"] == len(LOG)
    for post in (create, finalize):
        assert post["headers"]["authorization"] == f"Bearer {JWT}"
        assert _proof_binds(post["headers"]["dpop"]) == ("POST", post["path"]), "a proof for exactly this POST"
    assert finalize["body"] == {"sha256": hashlib.sha256(LOG).hexdigest()}
    assert "authorization" not in put["headers"] and "dpop" not in put["headers"], \
        "the presigned URL is its own authority: the JWT goes only to the server"
    assert put["headers"]["content-type"] == create["body"]["content_type"], "upload_headers sent verbatim"
    assert put["headers"]["content-length"] == str(len(LOG)) and put["body"] == len(LOG)
    # Only these schema assertions need the server package's database/crypto imports.
    from apps.memoryskills import validation

    validation.validate_mesh_message_v1(validation.MESH_CHAT_NAMESPACE, "msg/1760000000000-desktop-a-abcdef", {
        "schema_version": 1, "to": ["desktop-b"], "from": "desktop-a", "type": "note", "thread_id": "ops/serial",
        "ts": "2026-10-05T12:00:00Z", "body": "the boot log", "author_family": "claude", "attachments": [got]})


@pytest.mark.parametrize("lie", [{"size_bytes": len(LOG) + 1}, {"sha256": "f" * 64}, {"status": "pending"},
                                 {"id": str(uuid.UUID(int=7))}], ids=["size", "sha256", "status", "id"])
def test_a_finalize_answer_that_is_not_this_artifact_is_refused_and_the_artifact_deleted(service, tmp_path, lie):
    """The server answers 200 for an artifact that is not the one uploaded (another size, hash, status or id). The
    client refuses to cite it, names the field, and deletes the artifact again. CONTROL: the happy path above, the
    same answer without the lie."""
    path, state = _state(tmp_path, service.base)
    log = tmp_path / "serial.log"
    log.write_bytes(LOG)
    service.fault["finalize_says"] = lie
    with pytest.raises(files.AttachFailed) as failed:
        files.upload(path, state, log)
    err = failed.value.error
    assert err["error"] == files.E_ATTACH_MISMATCH and err["step"] == "finalize", err
    assert err["fields"] == sorted(lie), err
    assert err["discarded"] is True and service.routes()[-1] == ("DELETE", f"/v1/files/{err['file_id']}")
    assert err["file_id"] not in service.objects


def test_the_servers_own_hash_refusal_is_typed(service, tmp_path):
    """The stored bytes are not the ones sent (the fake keeps other bytes): finalize answers 422 E_FILE_HASH_MISMATCH,
    and the client reports it as the finalize step's HTTP refusal with the server's body verbatim."""
    path, state = _state(tmp_path, service.base)
    log = tmp_path / "serial.log"
    log.write_bytes(LOG)
    service.fault["store_instead"] = LOG[:-1] + b"!"
    with pytest.raises(files.AttachFailed) as failed:
        files.upload(path, state, log)
    err = failed.value.error
    assert (err["error"], err["step"], err["http_status"]) == (files.E_ATTACH_HTTP, "finalize", 422), err
    assert err["body"]["error"] == "E_FILE_HASH_MISMATCH"


def test_local_refusals_send_nothing(service, tmp_path):
    path, state = _state(tmp_path, service.base)
    for bad in (tmp_path, tmp_path / "missing.log"):
        with pytest.raises(node.Refused, match="not a regular file"):
            files.upload(path, state, bad)
    assert service.seen == []
    assert wire.attachment_name_problem("a\\b") and wire.attachment_name_problem("..")
    assert wire.attachment_name_problem("serial.log") is None, "control"


WINDOWS_UNSAFE_NAMES = [
    *(f"fw{ch}.bin" for ch in ':<>"|?*'),
    "CON", "con.txt", "CON .txt", "Prn.tar.gz", "aux", "NUL.bin", "COM0", "com9.bin",
    "CONIN$", "conin$.txt", "CONOUT$", "conout$.txt",
    "LPT0.bin", "lpt9.txt", "COM\u00b9", "lpt\u00b2.bin", "COM\u00b3.txt",
    "fw.", "fw.bin.", "fw ", "fw.bin ",
    # 132 characters (within the wire's 255) but 260 UTF-16 code units: too long for one Windows name.
    "\U0001F600" * 128 + ".txt",
]


def test_windows_name_length_counts_utf16_code_units():
    """255 UTF-16 code units is the most one Windows name holds; a supplementary character counts twice."""
    from tools.meshy_node import filenames

    assert filenames.windows_name_problem("a" * 255) is None
    assert filenames.windows_name_problem("\U0001F600" * 125 + "abcde") is None  # 250 + 5 = 255 units
    assert "256 UTF-16 code units" in filenames.windows_name_problem("\U0001F600" * 125 + "abcdef")
    assert "Windows" in filenames.windows_name_problem("\U0001F600" * 128 + ".txt")
    # A lone surrogate (surrogateescape from an undecodable POSIX name) is counted, never encoded.
    assert filenames.windows_name_problem("\udcff" * 255) is None
    assert filenames.windows_name_problem("\udcff" * 256) is not None


def test_posix_name_is_counted_as_the_filesystem_encoding_writes_it():
    """255 bytes is the most one POSIX name holds, counted as UTF-8 with surrogateescape: an undecodable byte
    (U+DC80..U+DCFF) is that one byte on disk. Any other lone surrogate cannot be written at all, so it is refused at
    any length, and nothing here raises (Codex r1 on a05d949)."""
    from tools.meshy_node import filenames

    assert filenames.utf8_name_problem("a" * 255) is None
    assert filenames.utf8_name_problem("a" * 256) == "the name is longer than 255 UTF-8 bytes"
    assert filenames.utf8_name_problem("\udcff" * 255) is None
    assert filenames.utf8_name_problem("\udcff" * 254 + "\U0001F600") == "the name is longer than 255 UTF-8 bytes"
    for name in ("\ud800", "a\udbff.log", "\udc7f"):                                # outside U+DC80..U+DCFF
        assert filenames.utf8_name_problem(name) == "the name holds a lone surrogate, which no file name can hold"
    # A signed message carries no surrogate at all, escaped byte or not.
    for name in ("\udcff.log", "\ud800", "a\udc80"):
        assert "cannot carry" in filenames.surrogate_name_problem(name)
    assert filenames.surrogate_name_problem("\U0001F600.log") is None


@pytest.mark.parametrize("name", WINDOWS_UNSAFE_NAMES, ids=repr)
def test_windows_unsafe_attachment_is_refused_before_upload(service, tmp_path, sent, monkeypatch, capsys, name):
    """A bad second attachment must not upload the good first one, on any host OS."""
    from types import SimpleNamespace

    state_path, state = _state(tmp_path, service.base)
    good = tmp_path / "boot.log"
    good.write_bytes(b"good")
    # Windows cannot create these fixtures. Model the sender's path without touching its filesystem.
    bad = SimpleNamespace(name=name, is_file=lambda: True, open=lambda mode: io.BytesIO(b"bad"))
    real_path = files.Path
    monkeypatch.setattr(files, "Path", lambda p: bad if p is bad or str(p) == str(tmp_path / name) else real_path(p))
    assert _cli_send(state_path, service.base, "--attach", str(good), "--attach", str(tmp_path / name)) == 2
    assert "Windows" in json.loads(capsys.readouterr().err)["refused"]
    with pytest.raises(node.Refused, match="Windows"):
        files.upload(state_path, state, str(tmp_path / name))
    assert sent == [] and service.seen == []


@pytest.mark.parametrize("name, problem", [
    ("\U0001F600" * 70, "the name is longer than 255 UTF-8 bytes"),
    ("\udcff.log", "which a signed message cannot carry"),     # an undecodable byte from a POSIX disk (Codex r1)
    ("\ud800.log", "which a signed message cannot carry"),     # an unpaired UTF-16 unit from a Windows disk
], ids=["emoji", "undecodable-byte", "unpaired-surrogate"])
def test_utf8_name_is_refused_before_upload(tmp_path, sent, monkeypatch, capsys, name, problem):
    """70 emoji fit the wire and Windows limits, but exceed a 255-byte filesystem's name limit. A surrogate passes
    both, but the signed message could not carry it, after the upload. Each is refused before anything is sent."""
    from types import SimpleNamespace

    state_path, state = tmp_path / "state.json", {"status": node.STATUS_MINTED, "peer_id": str(uuid.UUID(int=3))}
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: state)
    monkeypatch.setattr(sendguard, "admit", lambda *a, **kw: (kw["body"], kw["subject"]))

    def no_request(*a, **kw):
        pytest.fail("a refused name must not upload")
    monkeypatch.setattr(files, "_call", no_request)
    good = tmp_path / "boot.log"
    good.write_bytes(b"good")
    bad = SimpleNamespace(name=name, is_file=lambda: True, open=lambda mode: io.BytesIO(b"bad"))
    real_path = files.Path
    monkeypatch.setattr(files, "Path", lambda p: bad if p is bad or str(p) == str(tmp_path / name) else real_path(p))
    assert _cli_send(state_path, "http://unused", "--attach", str(good), "--attach", str(tmp_path / name)) == 2
    assert problem in json.loads(capsys.readouterr().err)["refused"]
    with pytest.raises(node.Refused, match=problem):
        files.upload(state_path, state, str(tmp_path / name))
    assert sent == []


@pytest.mark.parametrize("name, problem", [
    ("\U0001F600" * 70, "the name is longer than 255 UTF-8 bytes"),
    ("\ud800", "the name holds a lone surrogate, which no file name can hold"),   # short: placement raised (Codex r1)
], ids=["emoji", "lone-surrogate"])
def test_posix_utf8_name_is_refused_before_presign_or_download(tmp_path, monkeypatch, name, problem):
    state_path, state = tmp_path / "state.json", {"status": node.STATUS_MINTED, "peer_id": str(uuid.UUID(int=3))}
    out = tmp_path / "out"
    out.mkdir()
    reference = {"file_id": str(uuid.UUID(int=4)), "name": name,
                 "size_bytes": len(LOG), "sha256": hashlib.sha256(LOG).hexdigest()}

    def no_request(*a, **kw):
        pytest.fail("a refused name must not presign or download")
    monkeypatch.setattr(files, "_call", no_request)
    monkeypatch.setattr(files, "_receive", no_request)
    assert wire.attachment_problem(reference) is None, "server schema remains unchanged"
    with pytest.raises(files.AttachFailed) as failed:
        files.download(state_path, state, reference, out, windows=False)
    assert (failed.value.error["error"], failed.value.error["step"]) == (files.E_ATTACH_INVALID, "check")
    assert failed.value.error["detail"] == problem
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("name", ["a" * 255, "\U0001F600" * 63 + "abc"], ids=["ascii", "emoji"])
def test_255_byte_attachment_name_passes_send_and_posix_admission(tmp_path, monkeypatch, name):
    from types import SimpleNamespace

    assert len(name.encode("utf-8")) == 255
    source = SimpleNamespace(name=name, is_file=lambda: True)
    real_path = files.Path
    monkeypatch.setattr(files, "Path", lambda p: source if p is source else real_path(p))
    assert files.local_problem(source) is None
    reference = {"file_id": str(uuid.UUID(int=4)), "name": name,
                 "size_bytes": len(LOG), "sha256": hashlib.sha256(LOG).hexdigest()}
    calls = []

    def presign(*a, **kw):
        calls.append(kw["step"])
        return 404, {"error": "E_FILE_NOT_FOUND"}
    monkeypatch.setattr(files, "_call", presign)
    with pytest.raises(files.AttachFailed) as failed:
        files.download(tmp_path / "state.json", {"status": node.STATUS_MINTED, "peer_id": str(uuid.UUID(int=3))},
                       reference, tmp_path, windows=False)
    assert (failed.value.error["error"], failed.value.error["step"]) == (files.E_ATTACH_HTTP, "presign")
    assert calls == ["presign"]


@pytest.mark.parametrize("name", WINDOWS_UNSAFE_NAMES, ids=repr)
def test_windows_unsafe_attachment_is_refused_before_presign_or_download(service, tmp_path, name):
    from pathlib import PurePosixPath, PureWindowsPath

    state_path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    reference = {**service.put(LOG), "name": name}
    assert wire.attachment_problem(reference) is None, "server schema remains unchanged"
    with pytest.raises(files.AttachFailed) as failed:
        files.download(state_path, state, reference, out, windows=True)
    assert (failed.value.error["error"], failed.value.error["step"]) == (files.E_ATTACH_INVALID, "check")
    assert "Windows" in failed.value.error["detail"]
    assert service.seen == [] and list(out.iterdir()) == []
    assert files.named_in(PureWindowsPath("out"), name, windows=True) is None
    assert files.named_in(PurePosixPath("out"), name, windows=False) == PurePosixPath("out", name)


@pytest.mark.parametrize("name", [name for name in WINDOWS_UNSAFE_NAMES if len(name.encode("utf-8")) <= 255], ids=repr)
def test_posix_receiver_keeps_access_to_older_names_before_presign(service, tmp_path, name):
    """POSIX admission reaches the presign for Windows-unsafe names within 255 UTF-8 bytes, on any host whose
    filesystem lets it ask. Some Windows builds report a device name (CON, aux, NUL.bin) as present in every folder
    (HQD, 14 Oct 2026); others do not (HQB, Windows 11 26200). Only a row the host reports so is skipped."""
    state_path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    if os.name == "nt" and os.path.lexists(out / name):
        pytest.skip("this Windows build reports a device name as present in an empty folder")
    reference = {**service.put(LOG), "name": name}
    service.fault["presign_404"] = True
    with pytest.raises(files.AttachFailed) as failed:
        files.download(state_path, state, reference, out, windows=False)
    assert (failed.value.error["error"], failed.value.error["step"]) == (files.E_ATTACH_HTTP, "presign")
    assert service.routes() == [("GET", f"/v1/files/{reference['file_id']}/url")]
    assert service.raw_gets == 0


@pytest.mark.skipif(os.name == "nt", reason="these legacy names require a POSIX filesystem")
@pytest.mark.parametrize("name", ["report:v2.txt", "CON", "draft."], ids=repr)
def test_posix_receiver_downloads_older_windows_unsafe_names(service, tmp_path, name):
    state_path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    reference = {**service.put(LOG), "name": name}
    assert files.download(state_path, state, reference, out).read_bytes() == LOG


@pytest.mark.parametrize("name", ["CONSOLE.txt", "CONINPUT.txt", "CONOUT-report.txt", "com10.bin", "auxiliary",
                                 "nul-report.json", "fw .bin", "fw v2.bin", "a" * 255, "\U0001F600" * 63 + "abc"],
                         ids=repr)
def test_portable_attachment_name_still_uploads_and_downloads(service, tmp_path, name):
    state_path, state = _state(tmp_path, service.base)
    source = tmp_path / name
    source.write_bytes(b"portable control")
    reference = files.upload(state_path, state, source)
    out = tmp_path / "out"
    out.mkdir()
    assert files.download(state_path, state, reference, out).read_bytes() == b"portable control"


def test_a_file_that_shrinks_while_it_is_uploaded_is_refused_not_sent_short():
    """The request declares the hashed size; a file that ends sooner raises rather than leave the server waiting,
    and one that grew sends exactly the hashed bytes (the server's hash check covers a change of the same size)."""
    exact = files._Exactly(io.BytesIO(b"abcdef"), 4)
    assert exact.read(3) + exact.read(3) + exact.read(3) == b"abcd"
    short = files._Exactly(io.BytesIO(b"ab"), 4)
    assert short.read(2) == b"ab"
    with pytest.raises(files._Changed):
        short.read(2)


#: Presigned URLs the transfer cannot follow, each a ValueError somewhere in urllib before `_presigned` checked for it:
#: from urlsplit itself (Codex's probe), from the port, from the IDNA encoding of a host label, and from the request
#: line's ASCII encoding (a bare path, taken under the base URL).
UNFOLLOWABLE_URLS = {"unparseable": "http://[", "port-out-of-range": "http://127.0.0.1:99999/x",
                     "empty-label": "http://a..b/x", "not-ascii": "/v1/files/raw/☃"}


#: Upload headers that are [name, value] pairs of strings but not valid HTTP fields; each made urllib raise a raw
#: ValueError at the PUT before `_upload_target` checked their syntax (GitHub Codex review of #123, P2).
BAD_HEADERS = {"empty-name": [["", "x"]], "name-with-space": [["X Bad", "x"]],
               "value-with-newline": [["X-A", "a\nb"]], "value-not-latin1": [["X-A", "\u2603"]]}


@pytest.mark.parametrize("says", [{"upload_url": "ftp://127.0.0.1/x"}, {"upload_url": None},
                                  {"upload_headers": "Content-Type: text/plain"},
                                  *({"upload_url": url} for url in UNFOLLOWABLE_URLS.values()),
                                  *({"upload_headers": h} for h in BAD_HEADERS.values())],
                         ids=["ftp-url", "no-url", "headers-not-pairs", *(f"{k}-url" for k in UNFOLLOWABLE_URLS),
                              *(f"{k}-header" for k in BAD_HEADERS)])
def test_a_create_answer_with_an_id_but_nothing_to_put_to_deletes_that_artifact(service, tmp_path, says):
    """The 201 names a valid artifact id, so the artifact exists, but no URL for the bytes that the transfer can
    follow, or no [name, value] headers. Codex's probe of the earlier code: refused inside `_create`, before the
    cleanup began, so that artifact was never deleted; and an unfollowable URL (UNFOLLOWABLE_URLS) escaped as a raw
    ValueError. Now the refusal is typed, names the artifact and deletes it, and no bytes go anywhere. CONTROL: the
    happy path above, the same 201 with both."""
    path, state = _state(tmp_path, service.base)
    log = tmp_path / "serial.log"
    log.write_bytes(LOG)
    service.fault["create_says"] = says
    with pytest.raises(files.AttachFailed) as failed:
        files.upload(path, state, log)
    err = failed.value.error
    assert (err["error"], err["step"], err["discarded"]) == (files.E_ATTACH_HTTP, "create", True), err
    assert service.routes() == [("POST", "/v1/files"), ("DELETE", f"/v1/files/{err['file_id']}")]
    assert service.creates == 1 and service.objects == {} and service.puts == 0


@pytest.mark.parametrize("delete_refused", [False, True], ids=["deleted", "delete-refused"])
def test_a_create_answer_with_an_id_and_another_success_status_deletes_that_artifact(service, tmp_path,
                                                                                     delete_refused):
    """The create answers 200, not 201, with an otherwise valid answer: the artifact it names exists. Codex's probe
    of the earlier code: refused inside `_create` before `upload` held the id, so no DELETE was even attempted. Now
    the refusal names the artifact and deletes it, and when the server refuses the DELETE too, the record says so
    (`discarded` false) and the artifact is the one named. The answer's body, whose upload URL is a live capability,
    is not in the record. CONTROL: the happy path above, the same answer as a 201."""
    path, state = _state(tmp_path, service.base)
    log = tmp_path / "serial.log"
    log.write_bytes(LOG)
    service.fault.update({"create_status": 200, "delete_refused": delete_refused})
    with pytest.raises(files.AttachFailed) as failed:
        files.upload(path, state, log)
    err = failed.value.error
    assert (err["error"], err["step"], err["http_status"]) == (files.E_ATTACH_HTTP, "create", 200), err
    assert err["discarded"] is not delete_refused and "body" not in err, err
    assert service.routes() == [("POST", "/v1/files"), ("DELETE", f"/v1/files/{err['file_id']}")]
    assert service.creates == 1 and service.puts == 0
    assert list(service.objects) == ([err["file_id"]] if delete_refused else [])


# =============================================================================== send --attach

def _cli_send(state_file: Path, base: str, *extra: str) -> int:
    # --no-recipient-check: these tests count every request the file service sees; the advisory recipient
    # check (divergence 083) is tested in tests/test_meshy_node_recipientwarn.py. --no-card-check for the same
    # reason: the read of the thread's cards (divergence 091) is tested in tests/test_meshy_node_cards.py.
    return cli.main(["send", "--base-url", base, "--state-file", str(state_file), "--to", "desktop-b",
                     "--thread", "ops/serial", "--body", "the boot logs, attached", "--no-recipient-check",
                     "--no-card-check", *extra])


@pytest.fixture()
def sent(monkeypatch):
    """node.send replaced: what it was called with, and how many requests the file service had seen by then."""
    calls: list[dict] = []
    monkeypatch.setattr(node, "send", lambda *a, **kw: calls.append(kw) or {"status": "completed", "key": "msg/9"})
    monkeypatch.setattr(node, "succeeded", lambda result: True)
    return calls


def test_send_attach_uploads_every_file_before_the_message(service, tmp_path, sent, monkeypatch):
    path, _state_ = _state(tmp_path, service.base)
    seen_at_send = []
    real = node.send
    monkeypatch.setattr(node, "send", lambda *a, **kw: seen_at_send.append(len(service.seen)) or real(*a, **kw))
    one, two = tmp_path / "boot-1.log", tmp_path / "boot-2.log"
    one.write_bytes(LOG)
    two.write_bytes(LOG[::-1])

    assert _cli_send(path, service.base, "--attach", str(one), "--attach", str(two)) == 0

    (call,) = sent
    assert [(a["name"], a["size_bytes"], a["sha256"]) for a in call["attachments"]] == [
        ("boot-1.log", len(LOG), hashlib.sha256(LOG).hexdigest()),
        ("boot-2.log", len(LOG), hashlib.sha256(LOG[::-1]).hexdigest())]
    assert seen_at_send == [6], "both uploads (create, put, finalize each) were done before the message was sent"
    assert all(service.objects[a["file_id"]]["status"] == "ready" for a in call["attachments"])


def test_send_without_attach_carries_no_attachments(service, tmp_path, sent):
    path, _state_ = _state(tmp_path, service.base)
    assert _cli_send(path, service.base) == 0
    assert sent[0]["attachments"] is None and service.seen == []


def test_send_attach_refuses_when_an_upload_fails_and_sends_nothing(service, tmp_path, sent, capsys):
    """The SECOND upload is refused at create (413). Nothing is sent, the failure is typed with `sent: false`, and
    the first file, which nothing will cite, is deleted again."""
    path, _state_ = _state(tmp_path, service.base)
    one, two = tmp_path / "boot-1.log", tmp_path / "boot-2.log"
    one.write_bytes(LOG)
    two.write_bytes(LOG)
    service.fault["create_status_on"] = 2

    assert _cli_send(path, service.base, "--attach", str(one), "--attach", str(two)) == 1

    assert sent == [], "a failed upload must stop the message"
    out = json.loads(capsys.readouterr().out)
    assert (out["error"], out["step"], out["http_status"], out["sent"]) == (files.E_ATTACH_HTTP, "create", 413, False)
    assert out["name"] == "boot-2.log" and out["body"]["error"] == "E_FILE_SIZE_EXCEEDED"
    (first,) = out["earlier_uploads_discarded"]
    assert out["earlier_uploads_left"] == [] and first not in service.objects
    assert ("DELETE", f"/v1/files/{first}") in service.routes()
    assert not sendguard.ledger_path(path).exists(), "a send that never happened is not counted"


#: How a refusal's body fails to arrive (FakeFiles._refuse_broken), and the presigned URL's timeout that ends a stall.
BROKEN_BODIES = ["short", "chunked", "stalled"]
BROKEN_TIMEOUT_S = 0.5


def _assert_broken_refusal(err: dict, how: str) -> None:
    """The refusal is typed with its status, whatever became of its body: a stall or a cut chunk leaves `body` null
    and says why; a body cut short is what arrived of it, or null where this Python's bounded read refuses it too."""
    assert err["http_status"] == 500, err
    if how == "short" and err["body"] is not None:
        assert err["body"] == '{"error":"', err
        return
    assert err["body"] is None and "could not be read" in err["detail"], err
    assert how != "chunked" or "IncompleteRead" in err["detail"], err


@pytest.mark.parametrize("how", BROKEN_BODIES)
def test_a_refused_upload_whose_body_breaks_is_typed_and_cleans_up(service, tmp_path, sent, capsys, monkeypatch,
                                                                    how):
    """The SECOND file's PUT is refused (500), and reading the refusal's body fails: cut short, a cut chunk
    (http.client.IncompleteRead, not an OSError) or a stall to the timeout. Codex's probe of the earlier code: that
    read raised inside the HTTPError handler, the raw exception escaped `upload` and `cli._attach`, and nothing was
    deleted. Now it is the upload step's E_ATTACH_HTTP, nothing is sent, and both artifacts are deleted again.
    CONTROL: the 413 test above, whose refusal body arrives whole."""
    monkeypatch.setattr(node, "HTTP_TIMEOUT_S", BROKEN_TIMEOUT_S)
    path, _state_ = _state(tmp_path, service.base)
    one, two = tmp_path / "boot-1.log", tmp_path / "boot-2.log"
    one.write_bytes(LOG)
    two.write_bytes(LOG)
    service.fault["put_refused_on"] = (2, how)

    assert _cli_send(path, service.base, "--attach", str(one), "--attach", str(two)) == 1

    assert sent == [], "a failed upload must stop the message"
    out = json.loads(capsys.readouterr().out)
    assert (out["error"], out["step"], out["name"], out["sent"]) == (files.E_ATTACH_HTTP, "upload", "boot-2.log",
                                                                      False), out
    _assert_broken_refusal(out, how)
    (first,) = out["earlier_uploads_discarded"]
    assert out["discarded"] is True and out["earlier_uploads_left"] == [], out
    assert {first, out["file_id"]}.isdisjoint(service.objects), "both artifacts are deleted again"
    assert {("DELETE", f"/v1/files/{first}"), ("DELETE", f"/v1/files/{out['file_id']}")} <= set(service.routes())


class _SourceFails:
    """The file `upload` opens, whose disk fails (EIO) once its artifact exists: at `rewind` (the seek back to the
    start after hashing), at `transfer` (a read for the PUT, after that rewind) or at `close`. The hashing reads
    before the rewind work. Everything else is the real file."""

    def __init__(self, fh, at: str):
        self._fh, self._at, self._rewound = fh, at, False

    @staticmethod
    def _eio():
        raise OSError(errno.EIO, "Input/output error")

    def seek(self, *args):
        if self._at == "rewind":
            self._eio()
        self._rewound = True
        return self._fh.seek(*args)

    def read(self, *args):
        if self._at == "transfer" and self._rewound:
            self._eio()
        return self._fh.read(*args)

    def close(self):
        was_open = not self._fh.closed
        self._fh.close()
        if self._at == "close" and was_open:
            self._eio()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __getattr__(self, name):
        return getattr(self._fh, name)


@pytest.mark.parametrize("at", ["rewind", "transfer", "close"])
def test_a_local_failure_after_the_create_is_typed_and_deletes_every_upload(service, tmp_path, sent, capsys,
                                                                            monkeypatch, at):
    """The SECOND file's disk fails (EIO) after its artifact was created: at the rewind after hashing, at a read for
    the PUT, or at the close. Codex's probe of the earlier code, EIO at the rewind: a raw OSError escaped `upload` and
    `cli._attach`, nothing was deleted, and the command said the server was unreachable (exit 2); a read for the PUT
    was folded by urllib into that same "unreachable", and a failed close came after the finalize, orphaning a ready
    artifact. Now each is the upload step's E_ATTACH_LOCAL (exit 1: the server was reached), nothing is sent, and
    both artifacts are deleted again. CONTROL: test_send_attach_uploads_every_file_before_the_message, no failure."""
    path, _state_ = _state(tmp_path, service.base)
    one, two = tmp_path / "boot-1.log", tmp_path / "boot-2.log"
    one.write_bytes(LOG)
    two.write_bytes(LOG)
    real = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **kw: (_SourceFails(real(self, *a, **kw), at)
                                                               if self.name == "boot-2.log" else real(self, *a, **kw)))

    assert _cli_send(path, service.base, "--attach", str(one), "--attach", str(two)) == 1

    assert sent == [], "a failed upload must stop the message"
    out = json.loads(capsys.readouterr().out)
    assert (out["error"], out["step"], out["name"], out["sent"]) == (files.E_ATTACH_LOCAL, "upload", "boot-2.log",
                                                                      False), out
    assert "Input/output error" in out["detail"], out
    (first,) = out["earlier_uploads_discarded"]
    assert out["discarded"] is True and out["earlier_uploads_left"] == [], out
    assert service.creates == 2 and service.objects == {}, "both artifacts are deleted again"
    assert {("DELETE", f"/v1/files/{first}"), ("DELETE", f"/v1/files/{out['file_id']}")} <= set(service.routes())
    assert not [r for r in service.seen if r["path"] == f"/v1/files/{out['file_id']}/finalize"], \
        "a file whose read failed is never finalized"


@pytest.mark.parametrize("delete_refused", [False, True], ids=["deleted", "delete-refused"])
def test_any_failure_of_a_later_upload_deletes_the_earlier_ones_and_keeps_that_failure(
        service, tmp_path, sent, capsys, monkeypatch, delete_refused):
    """The SECOND upload raises an exception no step types (a stand-in for a bug, or an interrupt). Before,
    `cli._attach` deleted the earlier uploads only for AttachFailed and node.Refused, so the first file stayed and
    nothing named it. Now it is deleted, that exception is the one that propagates, and a stderr line says which
    went and, when the server would not delete one, which stayed. CONTROL: the 413 test above, a typed failure."""
    path, _state_ = _state(tmp_path, service.base)
    one, two = tmp_path / "boot-1.log", tmp_path / "boot-2.log"
    one.write_bytes(LOG)
    two.write_bytes(LOG)
    real = files.upload

    def upload(state_file, state, p):
        if Path(p).name == "boot-2.log":
            service.fault["delete_refused"] = delete_refused
            raise RuntimeError("synthetic")
        return real(state_file, state, p)
    monkeypatch.setattr(files, "upload", upload)

    with pytest.raises(RuntimeError, match="synthetic"):
        _cli_send(path, service.base, "--attach", str(one), "--attach", str(two))

    assert sent == []
    (first,) = [r["path"].split("/")[3] for r in service.seen if r["method"] == "DELETE"]
    (line,) = [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.strip()]
    gone, left = ([], [first]) if delete_refused else ([first], [])
    assert line == {"warning": "attach_failed", "failure": "RuntimeError: synthetic", "sent": False,
                    "earlier_uploads_discarded": gone, "earlier_uploads_left": left}, line
    assert (first in service.objects) is delete_refused


def test_send_attach_to_an_unreachable_server_exits_2_and_sends_nothing(tmp_path, sent, capsys):
    base = f"http://127.0.0.1:{_dead_port()}"
    path, _state_ = _state(tmp_path, base)
    log = tmp_path / "boot.log"
    log.write_bytes(LOG)
    assert _cli_send(path, base, "--attach", str(log)) == 2
    assert sent == []
    out = json.loads(capsys.readouterr().out)
    assert (out["error"], out["step"], out["sent"]) == (files.E_ATTACH_UNREACHABLE, "create", False)


@pytest.mark.parametrize("failure", ["refused", 429, 503])
@pytest.mark.parametrize("attached", [False, True])
def test_a_send_that_fails_after_the_uploads_names_the_files_and_keeps_them(
        service, tmp_path, monkeypatch, capsys, failure, attached):
    """Completed uploads make a later refusal unsafe to repeat; retained files are named in a warning."""
    path, _state_ = _state(tmp_path, service.base)
    log = tmp_path / "boot.log"
    log.write_bytes(LOG)

    calls = []

    def refused(*_a, **_kw):
        calls.append(_kw)
        if failure == "refused":
            raise ConnectionRefusedError("invoke connection refused")
        raise node.ServerSaid(failure, {"error": "E_UNAVAILABLE", "message": "synthetic"})
    monkeypatch.setattr(node, "invoke", refused)

    extra = ("--attach", str(log)) if attached else ()
    assert _cli_send(path, service.base, *extra) == (2 if failure == "refused" else 1)
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    payload = lines[-1] if failure == "refused" else json.loads(captured.out)
    assert payload["retryable"] is (not attached)
    assert payload.get("outcome") == ("unknown" if attached else None)
    assert len(calls) == 1
    assert service.creates == int(attached)
    if not attached:
        assert service.objects == {}
        return
    (warning,) = [line for line in lines if line.get("warning") == "attachments_uploaded_send_unconfirmed"]
    (file_id,) = warning["file_ids"]
    assert service.objects[file_id]["status"] == "ready" and not [r for r in service.seen if r["method"] == "DELETE"]


def test_send_attach_local_refusals_cost_no_upload(service, tmp_path, sent, capsys):
    """Each is exit 2 with nothing uploaded and nothing sent: a bad LAST path is found before the first upload."""
    path, _state_ = _state(tmp_path, service.base)
    good = tmp_path / "boot.log"
    good.write_bytes(b"x")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "same.log").write_bytes(b"1")
    (tmp_path / "b" / "same.log").write_bytes(b"2")
    nine = []
    for i in range(9):
        p = tmp_path / f"part{i}.log"
        p.write_bytes(b"x")
        nine += ["--attach", str(p)]
    for extra, needle in ((["--attach", str(good), "--attach", str(tmp_path / "missing.log")], "not a regular file"),
                          (["--attach", str(tmp_path / "a" / "same.log"), "--attach", str(tmp_path / "b" / "same.log")],
                           "more than once"),
                          (nine, "at most 8")):
        assert _cli_send(path, service.base, *extra) == 2
        assert needle in json.loads(capsys.readouterr().err)["refused"]
    assert sent == [] and service.seen == []
    # CONTROL: eight is allowed.
    assert _cli_send(path, service.base, *nine[:16]) == 0 and len(sent[0]["attachments"]) == 8


def test_the_size_refusal_names_attach():
    """sendguard's SIZE refusal says where a large text artifact goes. CONTROL: it still names share and the
    override."""
    why = sendguard.refusals(Path("unused-state.json"), to=None, cc=None, body="x" * (sendguard.MAX_BODY_BYTES + 1))
    assert len(why) == 1 and "--attach" in why[0] and "share" in why[0] and str(sendguard.MAX_BODY_BYTES) in why[0]


# =============================================================================== fetch

def test_download_verifies_and_leaves_only_the_file(service, tmp_path):
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    reference = service.put(LOG)
    got = files.download(path, state, reference, out)
    assert got == out / "serial.log" and got.read_bytes() == LOG
    assert sorted(p.name for p in out.iterdir()) == ["serial.log"], "no temporary file is left"
    presign = next(r for r in service.seen if r["path"].endswith("/url"))
    raw = next(r for r in service.seen if r["path"].startswith("/v1/files/raw/"))
    assert presign["headers"]["authorization"] == f"Bearer {JWT}"
    assert "authorization" not in raw["headers"], "the presigned URL gets no bearer"


@pytest.mark.parametrize("served,needle", [
    (LOG[:-1] + b"?", "sha256"),                 # same size, other bytes
    (LOG[:-10], "size_bytes"),                   # short
    (LOG + b"extra", "more than"),               # long: stopped at the first byte past the size
], ids=["tampered", "short", "long"])
def test_download_refuses_bytes_that_are_not_the_messages(service, tmp_path, served, needle):
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    reference = service.put(LOG)
    service.fault["serve"] = served
    with pytest.raises(files.AttachFailed) as failed:
        files.download(path, state, reference, out)
    err = failed.value.error
    assert err["error"] == files.E_ATTACH_MISMATCH and err["step"] == "download", err
    assert needle in json.dumps(err)
    assert list(out.iterdir()) == [], "neither the file nor its temporary copy is left"


def test_download_never_replaces_a_file(service, tmp_path):
    """Refused before anything is asked of the server, and the file there is untouched. And the place step itself
    refuses a file that appeared between the check and the rename (`_place`), keeping that file."""
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    (out / "serial.log").write_bytes(b"mine")
    reference = service.put(LOG)
    with pytest.raises(files.AttachFailed) as failed:
        files.download(path, state, reference, out)
    assert failed.value.error["error"] == files.E_ATTACH_EXISTS and failed.value.error["step"] == "check"
    assert service.seen == [] and (out / "serial.log").read_bytes() == b"mine"

    tmp = out / ".meshy-fetch-x.part"
    tmp.write_bytes(LOG)
    with pytest.raises(files.AttachFailed) as raced:
        files._place(str(tmp), out / "serial.log", {"name": "serial.log"})
    assert raced.value.error["error"] == files.E_ATTACH_EXISTS and (out / "serial.log").read_bytes() == b"mine"


class _NoLinksAndARival:
    """A filesystem without hard links (FAT, some network shares), and another process beside this one that puts
    ITS file under the destination name, atomically, as a rename does, at the worst moment: immediately before the
    call that publishes the download, whichever call that is (a replacing `os.replace` or `os.rename`, or
    `_rename_noreplace`). With `publish_fails` that call then fails. Codex's interleaving of the earlier code: the
    name reserved with O_EXCL, the reservation closed, the rival's file renamed over it, then `os.replace` over the
    rival (or, after a failed replace, `os.unlink` of the rival)."""

    def __init__(self, monkeypatch, final: Path, theirs: Path, publish_fails: bool):
        self.final, self.theirs, self.publish_fails, self.struck = final, theirs, publish_fails, False
        real = files._rename_noreplace
        monkeypatch.setattr(files, "os", _Os(link=self.link, replace=self.publishing(os.replace),
                                             rename=self.publishing(os.rename)))
        monkeypatch.setattr(files, "_rename_noreplace", self.publishing(real))

    @staticmethod
    def link(src, dst):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(src), None, str(dst))

    def publishing(self, call):
        def wrapped(src, dst):
            if not self.struck:
                self.struck = True
                os.replace(self.theirs, self.final)
            if self.publish_fails:
                raise OSError(errno.EIO, "Input/output error")
            return call(src, dst)
        return wrapped


@pytest.mark.parametrize("publish_fails", [False, True], ids=["publishes", "publish-fails"])
def test_without_hard_links_a_file_that_appears_at_the_last_moment_is_neither_replaced_nor_deleted(
        service, tmp_path, monkeypatch, publish_fails):
    """The rival's file lands under the name right before the publishing call. It must survive: the download is
    refused (E_ATTACH_EXISTS from a rename that refuses to replace; E_ATTACH_LOCAL where the platform has none, or
    when the publishing call fails), nothing at the name is replaced or removed, and the temporary file is gone.
    CONTROL: the next test, the same filesystem with no rival, publishes."""
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    theirs = tmp_path / "theirs.log"
    theirs.write_bytes(b"the other process's file")
    race = _NoLinksAndARival(monkeypatch, out / "serial.log", theirs, publish_fails)

    with pytest.raises(files.AttachFailed) as failed:
        files.download(path, state, service.put(LOG), out)

    err = failed.value.error
    exists = NO_REPLACE_RENAME and not publish_fails
    assert (err["error"], err["step"]) == (files.E_ATTACH_EXISTS if exists else files.E_ATTACH_LOCAL, "place"), err
    assert race.struck, "the rival's file landed before the publishing call"
    assert (out / "serial.log").read_bytes() == b"the other process's file", "replaced or deleted"
    assert sorted(p.name for p in out.iterdir()) == ["serial.log"], "no temporary file is left"


def test_without_hard_links_a_rename_that_cannot_replace_publishes(service, tmp_path, monkeypatch):
    """CONTROL for the test above: no hard links and no rival. The verified file takes its name by the platform's
    no-replace rename (or, where there is none, the download is refused and nothing is left)."""
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(files, "os", _Os(link=_NoLinksAndARival.link))
    reference = service.put(LOG)
    if not NO_REPLACE_RENAME:
        with pytest.raises(files.AttachFailed) as failed:
            files.download(path, state, reference, out)
        assert failed.value.error["error"] == files.E_ATTACH_LOCAL and list(out.iterdir()) == []
        return
    assert files.download(path, state, reference, out) == out / "serial.log"
    assert (out / "serial.log").read_bytes() == LOG
    assert sorted(p.name for p in out.iterdir()) == ["serial.log"], "no temporary file is left"


@pytest.mark.skipif(not NO_REPLACE_RENAME, reason="this platform has no rename that refuses to replace")
def test_the_no_replace_rename_refuses_an_existing_name(tmp_path):
    """The platform call itself (renamex_np, renameat2 or Windows' rename), on a real directory: an existing
    destination is refused and both files are untouched. CONTROL: a free name is renamed to."""
    src, dst = tmp_path / "src.part", tmp_path / "dst.log"
    src.write_bytes(b"verified")
    dst.write_bytes(b"theirs")
    with pytest.raises(FileExistsError):
        files._rename_noreplace(str(src), dst)
    assert src.read_bytes() == b"verified" and dst.read_bytes() == b"theirs"
    dst.unlink()
    files._rename_noreplace(str(src), dst)
    assert dst.read_bytes() == b"verified" and not src.exists()


def test_a_name_from_an_unchecked_row_is_never_used_as_a_path(service, tmp_path):
    """A row stored unchecked (divergence 011) can carry any `attachments`. A name that is a path is refused before
    any request; nothing is written outside, or inside, the out directory."""
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    for name in ("../escape.log", "..", "a/b.log"):
        with pytest.raises(files.AttachFailed) as failed:
            files.download(path, state, {**service.put(LOG), "name": name}, out)
        assert failed.value.error["error"] == files.E_ATTACH_INVALID
    assert not (tmp_path / "escape.log").exists() and list(out.iterdir()) == []
    assert not [r for r in service.seen if r["path"].endswith("/url")]


def test_a_name_the_schema_accepts_is_still_refused_where_the_platform_reads_it_as_a_path():
    """`C:x` has no separator, so the schema accepts it; on Windows, joined to the out directory it is a path on drive
    C:. Windows receivers refuse it; POSIX receivers retain access to older names.
    CONTROL: a plain name on Windows is still a name."""
    from pathlib import PurePosixPath, PureWindowsPath
    from apps.memoryskills import validation
    assert files.named_in(PureWindowsPath("out"), "C:x", windows=True) is None
    assert files.named_in(PureWindowsPath("out"), "serial.log", windows=True) == PureWindowsPath("out", "serial.log")
    assert files.named_in(PurePosixPath("out"), "C:x", windows=False) == PurePosixPath("out", "C:x")
    assert validation._is_plain_file_name("C:x") and wire.attachment_name_problem("C:x") is None


def test_a_file_this_reader_may_not_read_is_typed_and_leaves_nothing(service, tmp_path):
    """A file this reader may not read (apps/files/service.authorize_read: not of its owner, and referenced by no
    skill call it is the caller or the target of) answers 404 E_FILE_NOT_FOUND at the presign."""
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    service.fault["presign_404"] = True
    with pytest.raises(files.AttachFailed) as failed:
        files.download(path, state, service.put(LOG), out)
    err = failed.value.error
    assert (err["error"], err["step"], err["http_status"]) == (files.E_ATTACH_HTTP, "presign", 404)
    assert list(out.iterdir()) == []


def _cli_fetch(monkeypatch, state_file: Path, base: str, message: dict, out: Path) -> int:
    monkeypatch.setattr(node, "read", lambda *a, **kw: message)
    return cli.main(["fetch", "--base-url", base, "--state-file", str(state_file), "--key", message["key"],
                     "--out", str(out)])


def _message(attachments) -> dict:
    value = {"key": "msg/1760000000000-desktop-a-abcdef", "version": 1, "from": "desktop-a",
             "signature_verified": True, "signed_by": str(uuid.UUID(int=3)), "body": "the boot logs"}
    if attachments is not None:
        value["attachments"] = attachments
    return value


def test_fetch_downloads_every_attachment_and_prints_one_summary(service, tmp_path, monkeypatch, capsys):
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    one = service.put(LOG)
    two = {**service.put(LOG[::-1]), "name": "serial-2.log"}
    assert _cli_fetch(monkeypatch, path, service.base, _message([one, two]), out) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True and summary["signature_verified"] is True
    assert [(a["name"], a["path"]) for a in summary["attachments"]] == [
        ("serial.log", str(out / "serial.log")), ("serial-2.log", str(out / "serial-2.log"))]
    assert (out / "serial-2.log").read_bytes() == LOG[::-1]


def test_fetch_refuses_windows_unsafe_name_without_contacting_it_and_fetches_the_next(service, tmp_path,
                                                                                  monkeypatch, capsys):
    monkeypatch.setattr(files, "_IS_WINDOWS", True)
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    bad = {**service.put(LOG), "name": "fw?.bin"}
    good = service.put(LOG[::-1])
    assert _cli_fetch(monkeypatch, path, service.base, _message([bad, good]), out) == 1
    summary = json.loads(capsys.readouterr().out)
    refused, accepted = summary["attachments"]
    assert (refused["error"]["error"], refused["error"]["step"]) == (files.E_ATTACH_INVALID, "check")
    assert accepted["path"] == str(out / "serial.log")
    assert (out / "serial.log").read_bytes() == LOG[::-1]
    assert service.presigns == service.raw_gets == 1
    assert sorted(p.name for p in out.iterdir()) == ["serial.log"]


def test_fetch_windows_default_refuses_before_presign(service, tmp_path, monkeypatch, capsys):
    """The real fetch path supplies no platform keyword: refuse before contacting the file service."""
    monkeypatch.setattr(files, "_IS_WINDOWS", True)
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    bad = {**service.put(LOG), "name": "fw?.bin"}
    assert _cli_fetch(monkeypatch, path, service.base, _message([bad]), out) == 1
    failure = json.loads(capsys.readouterr().out)["attachments"][0]["error"]
    assert (failure["error"], failure["step"]) == (files.E_ATTACH_INVALID, "check")
    assert service.seen == [] and list(out.iterdir()) == []


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
@pytest.mark.parametrize("name", ["fw?.bin", "CON", "draft."], ids=repr)
def test_named_in_defaults_to_the_receiver_platform(monkeypatch, windows, name):
    from pathlib import PurePosixPath, PureWindowsPath

    monkeypatch.setattr(files, "_IS_WINDOWS", windows)
    out = PureWindowsPath("out") if windows else PurePosixPath("out")
    assert files.named_in(out, name) == (None if windows else out / name)
    assert files.named_in(out, "ok.txt") == out / "ok.txt"


def test_fetch_exits_1_when_one_attachment_fails_and_keeps_the_verified_one(service, tmp_path, monkeypatch, capsys):
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    good = service.put(LOG)
    bad = {**service.put(LOG), "name": "lied.log", "sha256": "0" * 64}
    assert _cli_fetch(monkeypatch, path, service.base, _message([good, bad]), out) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is False
    assert summary["attachments"][0]["path"] == str(out / "serial.log")
    assert summary["attachments"][1]["error"]["error"] == files.E_ATTACH_MISMATCH
    assert sorted(p.name for p in out.iterdir()) == ["serial.log"]


@pytest.mark.parametrize("how", BROKEN_BODIES)
def test_a_refused_download_whose_body_breaks_is_typed_and_fetch_goes_on(service, tmp_path, monkeypatch, capsys,
                                                                         how):
    """The FIRST attachment's presigned GET is refused (500), and reading the refusal's body fails, as in the upload
    test above. That attachment is the download step's E_ATTACH_HTTP, the second is still fetched, one summary is
    printed, the exit is 1, and no temporary file is left. Before, the raw exception escaped `fetch`: no summary, and
    the second attachment never fetched."""
    monkeypatch.setattr(node, "HTTP_TIMEOUT_S", BROKEN_TIMEOUT_S)
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    refused, good = service.put(LOG), {**service.put(LOG[::-1]), "name": "serial-2.log"}
    service.fault["get_refused_on"] = (1, how)

    assert _cli_fetch(monkeypatch, path, service.base, _message([refused, good]), out) == 1

    summary = json.loads(capsys.readouterr().out)
    first, second = summary["attachments"]
    err = first["error"]
    assert (err["error"], err["step"], err["name"]) == (files.E_ATTACH_HTTP, "download", "serial.log"), err
    _assert_broken_refusal(err, how)
    assert second["path"] == str(out / "serial-2.log") and (out / "serial-2.log").read_bytes() == LOG[::-1]
    assert sorted(p.name for p in out.iterdir()) == ["serial-2.log"], "no temporary file is left"


@pytest.mark.parametrize("url", list(UNFOLLOWABLE_URLS.values()), ids=list(UNFOLLOWABLE_URLS))
def test_a_presigned_url_that_cannot_be_followed_is_typed_and_fetch_goes_on(service, tmp_path, monkeypatch, capsys,
                                                                            url):
    """Three attachments; the SECOND one's presign answers 200 with a URL the transfer cannot follow. Codex's probe
    of the earlier code, `http://[`: urlsplit's ValueError escaped `download`, `fetch` and the CLI, so the third
    attachment was never tried and no summary was printed, though the first was already in place (the other URLs
    raised theirs at the request, or, the bad port, read as an unreachable server). Now that one is the presign
    step's E_ATTACH_HTTP and nothing is requested from it, the first stays, the third is fetched, one summary is
    printed and the exit is 1. CONTROL: test_fetch_downloads_every_attachment_and_prints_one_summary."""
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    one, two, three = ({**service.put(data), "name": f"serial-{i}.log"}
                       for i, data in enumerate((LOG, LOG[::-1], LOG[1:]), start=1))
    service.fault["presign_url_on"] = (2, url)

    assert _cli_fetch(monkeypatch, path, service.base, _message([one, two, three]), out) == 1

    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is False and service.presigns == 3 and service.raw_gets == 2
    first, second, third = summary["attachments"]
    assert first["path"] == str(out / "serial-1.log") and third["path"] == str(out / "serial-3.log")
    assert "error" not in first and "error" not in third and "path" not in second
    err = second["error"]
    assert (err["error"], err["step"], err["name"], err["file_id"]) == (files.E_ATTACH_HTTP, "presign",
                                                                        "serial-2.log", two["file_id"]), err
    assert "URL for the bytes" in err["detail"], err
    assert sorted(p.name for p in out.iterdir()) == ["serial-1.log", "serial-3.log"]
    assert (out / "serial-1.log").read_bytes() == LOG and (out / "serial-3.log").read_bytes() == LOG[1:]


class _DiskFull(io.FileIO):
    """A file on a disk that is full: every write the buffer hands down fails with ENOSPC."""

    def write(self, b):
        raise OSError(errno.ENOSPC, "No space left on device")


def _full_at_close(monkeypatch, which: int) -> list[int]:
    """files.py's `os.fdopen`, with the `which`-th file it opens (1-based) on a full disk behind a buffer larger
    than the download: every `write` is accepted, and ENOSPC surfaces only at the flush its close does."""
    opened: list[int] = []

    def fdopen(fd, mode="r", *args, **kwargs):
        opened.append(fd)
        if len(opened) == which:
            return io.BufferedWriter(_DiskFull(fd, "wb"), buffer_size=4 * len(LOG))
        return os.fdopen(fd, mode, *args, **kwargs)
    monkeypatch.setattr(files, "os", _Os(fdopen=fdopen))
    return opened


def test_fetch_types_a_disk_that_fails_at_the_close_and_goes_on(service, tmp_path, monkeypatch, capsys):
    """Three attachments; the SECOND one's last flush fails with ENOSPC at the close. That one is a typed
    E_ATTACH_LOCAL of the download step, the third is still fetched, the first stays, one summary is printed, and
    the exit is 1 (not 2, "unreachable"). No temporary file is left."""
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    one, two, three = ({**service.put(data), "name": f"serial-{i}.log"}
                       for i, data in enumerate((LOG, LOG[::-1], LOG[1:]), start=1))
    opened = _full_at_close(monkeypatch, 2)

    assert _cli_fetch(monkeypatch, path, service.base, _message([one, two, three]), out) == 1

    summary = json.loads(capsys.readouterr().out)
    assert len(opened) == 3 and summary["ok"] is False
    first, second, third = summary["attachments"]
    assert first["path"] == str(out / "serial-1.log") and third["path"] == str(out / "serial-3.log")
    err = second["error"]
    assert (err["error"], err["step"], err["name"]) == (files.E_ATTACH_LOCAL, "download", "serial-2.log"), err
    assert "ENOSPC" in err["detail"] or "No space" in err["detail"]
    assert sorted(p.name for p in out.iterdir()) == ["serial-1.log", "serial-3.log"]
    assert (out / "serial-3.log").read_bytes() == LOG[1:]


def test_a_close_that_fails_does_not_replace_the_failure_already_raised(service, tmp_path, monkeypatch):
    """The server sends a byte past the size (E_ATTACH_MISMATCH, raised while the file is open) and the close then
    fails too: the mismatch is what is reported. CONTROL: the test above, where the close is the only failure."""
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    reference = service.put(LOG)
    service.fault["serve"] = LOG + b"extra"
    _full_at_close(monkeypatch, 1)
    with pytest.raises(files.AttachFailed) as failed:
        files.download(path, state, reference, out)
    err = failed.value.error
    assert (err["error"], err["step"]) == (files.E_ATTACH_MISMATCH, "download") and "more than" in err["detail"], err
    assert list(out.iterdir()) == []


def _deletion_refused(monkeypatch, **replaced) -> list[str]:
    """files.py's `os.unlink` on a filesystem that refuses to delete an existing file (EACCES: a share whose
    permissions changed), so nothing is removed; a path that does not exist is still FileNotFoundError, as the real
    call says. Other `os` calls may be replaced too. Returns every path it was asked to delete."""
    asked: list[str] = []

    def unlink(target, *args, **kwargs):
        asked.append(str(target))
        if not os.path.lexists(target):
            return os.unlink(target, *args, **kwargs)
        raise PermissionError(errno.EACCES, "Permission denied", str(target))
    monkeypatch.setattr(files, "os", _Os(unlink=unlink, **replaced))
    return asked


def test_a_temporary_file_that_cannot_be_deleted_after_a_failed_download_is_named(service, tmp_path, monkeypatch):
    """The server sends other bytes (E_ATTACH_MISMATCH) and the filesystem refuses to delete the temporary file
    (EACCES). Codex's probe of the earlier code: every OSError from that unlink was swallowed, so the .part file
    stayed and nothing said so. Now the mismatch is still the failure reported, and its `cleanup` is the typed
    E_ATTACH_CLEANUP naming the file left. CONTROL: test_download_refuses_bytes_that_are_not_the_messages, where the
    deletion works and nothing is left."""
    path, state = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    reference = service.put(LOG)
    service.fault["serve"] = LOG[:-1] + b"?"
    asked = _deletion_refused(monkeypatch)
    with pytest.raises(files.AttachFailed) as failed:
        files.download(path, state, reference, out)
    err = failed.value.error
    assert (err["error"], err["step"]) == (files.E_ATTACH_MISMATCH, "download"), err
    (left,) = out.iterdir()
    assert left.name.startswith(files.TEMP_PREFIX) and asked == [str(left)]
    cleanup = err["cleanup"]
    assert (cleanup["error"], cleanup["step"], cleanup["temp_path"]) == (files.E_ATTACH_CLEANUP, "cleanup",
                                                                         str(left)), err
    assert "Permission denied" in cleanup["detail"], err


@pytest.mark.parametrize("linked", [True, False], ids=["hard-link", "no-replace-rename"])
def test_fetch_reports_a_temporary_file_left_beside_a_verified_one_and_exits_1(service, tmp_path, monkeypatch,
                                                                               capsys, linked):
    """The file verified and took its name by a hard link, and the filesystem then refused to delete the temporary
    name (EACCES). Codex's probe of the earlier code: `download` returned success and `fetch` exited 0 with the .part
    file left behind. Now the verified file stays, its record carries both its `path` and the typed
    E_ATTACH_CLEANUP naming the file left, `ok` is false and the exit is 1. CONTROL (no-replace-rename): published by
    a rename, the temporary name is already gone, and only that FileNotFoundError is quiet: exit 0, nothing left."""
    if not linked and not NO_REPLACE_RENAME:
        pytest.skip("this platform has no rename that refuses to replace")
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    _deletion_refused(monkeypatch, **({} if linked else {"link": _NoLinksAndARival.link}))

    assert _cli_fetch(monkeypatch, path, service.base, _message([service.put(LOG)]), out) == (1 if linked else 0)

    summary = json.loads(capsys.readouterr().out)
    (record,) = summary["attachments"]
    assert record["path"] == str(out / "serial.log") and (out / "serial.log").read_bytes() == LOG
    if not linked:
        assert summary["ok"] is True and "error" not in record and sorted(os.listdir(out)) == ["serial.log"]
        return
    assert summary["ok"] is False
    err = record["error"]
    assert (err["error"], err["step"], err["path"]) == (files.E_ATTACH_CLEANUP, "cleanup", str(out / "serial.log"))
    left = Path(err["temp_path"])
    assert left.parent == out and left.name.startswith(files.TEMP_PREFIX) and left.read_bytes() == LOG
    assert sorted(p.name for p in out.iterdir()) == sorted(["serial.log", left.name])


def test_fetch_of_a_message_with_no_attachments_exits_1(service, tmp_path, monkeypatch, capsys):
    path, _state_ = _state(tmp_path, service.base)
    assert _cli_fetch(monkeypatch, path, service.base, _message(None), tmp_path) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["error"] == files.E_ATTACH_NONE and summary["attachments"] == []
    assert service.seen == []


def test_fetch_downloads_nothing_from_a_malformed_attachments_field(service, tmp_path, monkeypatch, capsys):
    path, _state_ = _state(tmp_path, service.base)
    reference = service.put(LOG)
    assert _cli_fetch(monkeypatch, path, service.base, _message([reference, reference]), tmp_path) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["error"] == files.E_ATTACH_INVALID and service.seen == []


def test_fetch_says_when_the_writers_signature_did_not_verify(service, tmp_path, monkeypatch, capsys):
    """The sha256 each file is checked against is the writer's word only when `read` verified the writer's
    signature; the summary carries read's verdict and its error beside the result."""
    path, _state_ = _state(tmp_path, service.base)
    out = tmp_path / "out"
    out.mkdir()
    message = {**_message([service.put(LOG)]), "signature_verified": False, "signed_by": None,
               "signature_error": "E_SIGNATURE_KEY_UNAVAILABLE: synthetic"}
    assert _cli_fetch(monkeypatch, path, service.base, message, out) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["signature_verified"] is False
    assert summary["signature_error"].startswith("E_SIGNATURE_KEY_UNAVAILABLE")


def test_fetch_refuses_an_out_directory_that_does_not_exist(service, tmp_path, monkeypatch, capsys):
    path, _state_ = _state(tmp_path, service.base)
    assert _cli_fetch(monkeypatch, path, service.base, _message([service.put(LOG)]), tmp_path / "nope") == 2
    assert "not a directory" in json.loads(capsys.readouterr().err)["refused"]
    assert service.seen == []


def test_read_prints_attachments_and_null_without():
    """`read` is the command every notice tells a seat to run: it shows the field (null when absent)."""
    assert "attachments" in node.READ_VALUE_FIELDS and node.READ_VALUE_FIELDS[-1] == "body"
