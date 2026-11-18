"""Serve the backup export folder READ-ONLY over HTTP, so HQB can pull the dumps (E02, the
offsite copy; 2026-09-26).

    python -m tools.backup serve --dir DIR --bind ADDR --port N

    GET /index.json   200 application/json {"files": [{"name", "bytes", "mtime"}]}, by name;
                      mtime is RFC 3339 UTC, e.g. "2026-09-25T03:00:05Z"
    GET /<name>       200 the file's bytes, streamed, for a name that index lists NOW
    anything else     404 with a fixed body; any method but GET (HEAD too) is 405, Allow: GET

TRUST MODEL: THERE IS NO CREDENTIAL ON THIS CHANNEL (operator ruling 2026-09-26). Two things
stand in for one, and both are configured OUTSIDE this code:

  * who can read is the NETWORK SCOPE: `--bind` is HQD's LAN address (a wildcard is refused
    here), and a Windows Firewall rule allows the port from ONE remote address, HQB's;
  * what arrives is checked by the MANIFEST: HQB verifies each dump against the manifest's
    sha256 and size before it calls it a backup, as pull_to_hqb.ps1 does over this channel.

THAT IS ENOUGH FOR TEST DATA ONLY. The sha256 proves the bytes survived the trip, not who sent
them: the manifest arrives over the same channel, so anything that can answer on this address
can serve a dump and a manifest that agree. Production data needs every request signed by
HQB's node key and verified here before a byte is sent; until then, serve test data only.
This channel does not sign requests yet. Until it does, who can read rests on the
network scope alone, which is not a production pattern; signing each request, as
above, is the planned change.

WHAT IS SERVED is decided on every request, with no cache, so a backup written after startup
is listed and a pruned one is gone: the entries DIRECTLY in DIR that os.lstat says are regular
files (not a directory, a symlink, a junction or any other reparse point) and whose name
fully matches NAME below, which is what `backup` writes. A consequence the HQB side named
(bxxxxx, 2026-09-26): a database name with an upper-case letter or a hyphen never matches, so
its dumps are never listed and HQB cannot know they exist - the freshness alarm cannot name
them. It does not bite meshy_nodetest; tests/test_backup_offsite.py pins the HQB half. The request target is judged exactly
as it arrived, never decoded and never joined to DIR: the file opened is DIR / a name taken
from that listing, so a request can only pick from the list, whatever Windows would make of
its text (another letter case, a trailing dot and "::$DATA" all name the same file there).

Logs one line per request to stdout (UTC time, client address, method, target, status, body
bytes sent), never file contents. Runs until Ctrl+C. Refusing to start is exit 3.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import re
import socket
import socketserver
import stat
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.backup.pgbackup import Refused

INDEX = "index.json"
# re.ASCII: without it \d also matches other scripts' digits, and a name no backup of ours
# has would be listed.
NAME = re.compile(r"[a-z0-9_]+-\d{8}T\d{6}Z\.(dump|manifest\.json)", re.ASCII)
CHUNK = 1 << 20                  # a dump is streamed 1 MiB at a time, never read whole
_LOG_LOCK = threading.Lock()

# CreateFile flags, for opening a file without following a link and without blocking prune.
_SHARE_ALL = 0x1 | 0x2 | 0x4     # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
_OPEN_REPARSE_POINT = 0x00200000
_SEQUENTIAL_SCAN = 0x08000000


def fixed_body(code: int) -> bytes:
    """The whole body of every response that is not a listed file or the index."""
    return f"{code} {HTTPStatus(code).phrase}\n".encode("ascii")


def request_name(target: str) -> str | None:
    """The one name a raw request target asks for, or None when it is refused as written.

    Nothing here decodes. A target that would need decoding, or could mean something else
    once decoded or joined to a path, is refused rather than interpreted: a query or a fragment
    ("?", "#"), percent-encoding ("%"), a backslash (a separator to Windows), "..", a leading
    "//", more than one segment, "/" itself, and anything not starting with "/" ("*", an
    absolute URL). The listing lookup after this would 404 most of these too; this refuses
    them before anything is looked up, so a bug in the lookup is not the only thing between a
    request and the disk.
    """
    if not target.startswith("/") or target.startswith("//"):
        return None
    name = target[1:]
    if not name or ".." in name or any(c in name for c in "/\\?#%"):
        return None
    return name


def is_listable(st) -> bool:
    """A regular file and not a reparse point. The mode alone is not enough on Windows:
    CPython's lstat says S_IFLNK only for a Win32 symlink, and another name-surrogate reparse
    point on a file (a WSL symlink) comes back S_IFREG with the reparse attribute."""
    return (stat.S_ISREG(st.st_mode)
            and not getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def listing(folder: Path) -> dict[str, os.stat_result]:
    """{name: lstat} of what the index lists, read now."""
    out = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if NAME.fullmatch(entry.name) is None:
                continue
            try:
                # os.lstat, not entry.stat(): on Windows a DirEntry's stat has no st_ino,
                # which open_listed compares.
                st = os.lstat(os.path.join(folder, entry.name))
            except OSError:
                continue                  # removed between the scan and the lstat
            if is_listable(st):
                out[entry.name] = st
    return out


def _open_no_follow(path: Path):
    if os.name == "nt":
        import _winapi
        import msvcrt
        handle = _winapi.CreateFile(str(path), _winapi.GENERIC_READ, _SHARE_ALL, 0,
                                    _winapi.OPEN_EXISTING,
                                    _OPEN_REPARSE_POINT | _SEQUENTIAL_SCAN, 0)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)   # binary: no _O_TEXT
        except OSError:
            _winapi.CloseHandle(handle)
            raise
        return open(fd, "rb")
    return open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")


def open_listed(path: Path, listed: os.stat_result):
    """`path` open for reading, or None when it is gone or is not the file `listed` describes.

    The listing's lstat and this open are two calls, and the name can be replaced in between
    (by a link, say). So the open does not follow a link (FILE_FLAG_OPEN_REPARSE_POINT,
    O_NOFOLLOW), and the handle must be a regular file with the listed (st_dev, st_ino).

    On Windows the handle also shares DELETE. `prune` deletes old backups from this folder,
    and a file Python's open() holds cannot be deleted there (WinError 32): a pull in progress
    would make the daily prune fail. A file deleted mid-download is still read to the end
    through the handle.
    """
    try:
        f = _open_no_follow(path)
    except OSError:
        return None
    try:
        st = os.fstat(f.fileno())
    except OSError:
        st = None
    if st is None or not is_listable(st) or \
            (st.st_dev, st.st_ino) != (listed.st_dev, listed.st_ino):
        f.close()
        return None
    return f


def _rfc3339(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _printable(text: str) -> str:
    # The method and target are the client's bytes: escaped, a control character in them
    # cannot rewrite the terminal or forge a log line. Neither contains whitespace (the
    # stdlib splits the request line on it), so the line stays six fields.
    return text.encode("unicode_escape").decode("ascii")


def _stdout_line(line: str) -> None:
    with _LOG_LOCK:
        print(line, file=sys.stdout, flush=True)


class _Handler(BaseHTTPRequestHandler):
    server_version = "backup-serve"
    timeout = 60                      # a stalled client releases its thread
    # The stdlib answers a request line it cannot parse as HTTP/0.9 would be answered: no
    # status line and no headers, a bare body. Every response here has a status line.
    default_request_version = "HTTP/1.0"

    def version_string(self) -> str:
        # The stdlib's default names the Python version ("BaseHTTP/0.6 Python/3.13.12"),
        # which helps nobody pulling a backup and tells a scanner what to try.
        return self.server_version

    def handle_one_request(self) -> None:
        self.raw_target, self.status, self.sent = "-", None, 0
        try:
            super().handle_one_request()
        finally:
            if self.status is not None:           # None: the client sent nothing
                when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
                self.server.log(f"{when[:-3]}Z {self.client_address[0]} "
                                f"{_printable(self.command or '-')} "
                                f"{_printable(self.raw_target)} {self.status} {self.sent}")

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        # THE RAW TARGET, not self.path: the stdlib has already rewritten a leading "//" in
        # self.path to "/" (gh-87389), which would turn the refused "//NAME" into "/NAME".
        self.raw_target = self.requestline.split()[1]
        if self.command != "GET":
            self._fixed(HTTPStatus.METHOD_NOT_ALLOWED, [("Allow", "GET")])
            return False
        return True

    def do_GET(self) -> None:
        name = request_name(self.raw_target)
        if name is None:
            return self._fixed(HTTPStatus.NOT_FOUND)
        try:
            listed = listing(self.server.folder)
        except OSError:
            return self._fixed(HTTPStatus.INTERNAL_SERVER_ERROR)   # the folder is unreadable
        if name == INDEX:
            return self._index(listed)
        if name not in listed:
            return self._fixed(HTTPStatus.NOT_FOUND)
        self._file(name, listed[name])

    def send_error(self, code, message=None, explain=None) -> None:
        # The responses the stdlib makes itself (400, 414, 431, 505) get the same fixed body
        # and log line as ours; its default page repeats the client's request line back.
        self._fixed(code)

    def log_request(self, code="-", size="-") -> None:
        pass                    # one line per request, written after the body with its size

    def _head(self, code: int, content_type: str, length: int, extra=()) -> None:
        self.status = int(code)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()

    def _write(self, data: bytes) -> None:
        self.wfile.write(data)
        self.sent += len(data)

    def _fixed(self, code: int, extra=()) -> None:
        body = fixed_body(code)
        self._head(code, "text/plain; charset=utf-8", len(body), extra)
        if self.command != "HEAD":
            self._write(body)

    def _index(self, listed: dict[str, os.stat_result]) -> None:
        body = json.dumps({"files": [
            {"name": name, "bytes": st.st_size, "mtime": _rfc3339(st.st_mtime)}
            for name, st in sorted(listed.items())]}).encode("ascii")
        self._head(HTTPStatus.OK, "application/json", len(body))
        self._write(body)

    def _file(self, name: str, listed: os.stat_result) -> None:
        f = open_listed(self.server.folder / name, listed)
        if f is None:
            return self._fixed(HTTPStatus.NOT_FOUND)   # gone, or not the file listed, since
        with f:
            remaining = os.fstat(f.fileno()).st_size
            self._head(HTTPStatus.OK, "application/json" if name.endswith(".manifest.json")
                       else "application/octet-stream", remaining)
            try:
                while remaining:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        # Truncated after the headers went: the body falls short of
                        # Content-Length, which the client sees, and the manifest's hash
                        # would refuse it anyway.
                        break
                    self._write(chunk)
                    remaining -= len(chunk)
            except OSError:
                # The client went away mid-body; the log line still says how much it got.
                self.close_connection = True


class _Server(ThreadingHTTPServer):
    # HTTPServer turns SO_REUSEADDR on. On Windows that lets another socket bind the SAME
    # address and port and take connections meant for this one; server_bind asks for
    # exclusive use instead.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], folder: Path, log):
        self.folder, self.log = folder, log
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # TCPServer's bind, not HTTPServer's: that one reverse-resolves the address
        # (socket.getfqdn), which waits on DNS for a name nothing here uses.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def check_bind(bind: str) -> str:
    """`bind` when it is ONE address; Refused(3) for a wildcard or anything that is not an IP.

    The listening address is half of this channel's access control (the module docstring), so
    it must be one interface: 0.0.0.0, :: and "" listen on every one, including those the
    firewall rule was not written for. A host name is refused too: it is resolved at bind
    time to whatever it resolves to then, and "0" resolves to 0.0.0.0.
    """
    fix = "pass the one address HQB reaches, e.g. --bind 192.168.50.20"
    if not bind.strip():
        raise Refused(3, f"--bind is empty, which listens on every interface; {fix}")
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        raise Refused(3, f"--bind {bind!r} is not an IP address (a name is resolved at bind "
                         f"time, and '0' resolves to every interface); {fix}") from None
    mapped = getattr(address, "ipv4_mapped", None)
    if address.is_unspecified or (mapped is not None and mapped.is_unspecified):
        raise Refused(3, f"--bind {bind} listens on every interface; {fix}, and scope the "
                         "firewall rule to HQB's address")
    return str(address)


def make_server(folder, bind: str, port: int, log=None) -> _Server:
    """The listening server, not yet serving; Refused(3) for anything it will not start on."""
    folder = Path(folder)
    if not folder.is_dir():
        raise Refused(3, f"--dir {folder} is not a directory; pass the export folder that "
                         "`backup --out` writes to")
    address = check_bind(bind)
    if not 0 <= port <= 65535:
        raise Refused(3, f"--port {port} is not a TCP port (0-65535)")
    try:
        return _Server((address, port), folder.absolute(), log or _stdout_line)
    except OSError as exc:
        raise Refused(3, f"cannot listen on {address} port {port}: {exc}; the address must be "
                         "one of this host's and the port free") from exc


def serve(folder, bind: str, port: int) -> dict:
    server = make_server(folder, bind, port)
    host, bound = server.server_address[:2]
    url_host = f"[{host}]" if ":" in host else host
    print(f"serving {server.folder} read-only on http://{url_host}:{bound}/ "
          "(GET /index.json; no credential, test data only; Ctrl+C stops)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"ok": True, "dir": str(server.folder), "bind": host, "port": bound,
            "stopped": "interrupted"}
