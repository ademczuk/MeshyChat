"""`python -m tools.backup serve`: HQD serves its backup export folder READ-ONLY over HTTP so HQB
can pull the dumps (E02, the offsite copy; 2026-09-26). There is no credential on this channel,
by operator ruling; tools/backup/serve.py says what stands in for one and why that is enough
for test data only.

THE CONTRACT, agreed with the HQB side:
  * GET /index.json is 200 application/json {"files": [{"name", "bytes", "mtime"}]}, sorted
    by name, of the entries DIRECTLY in DIR that are regular files (no directory, symlink,
    junction or other reparse point) and whose name fully matches
    ^[a-z0-9_]+-\\d{8}T\\d{6}Z\\.(dump|manifest\\.json)$, read again on every request;
  * GET /<name> is 200 and the file's bytes, streamed, with Content-Length, only for a name
    that index lists AT THAT REQUEST;
  * anything else is 404 with a tiny fixed body, judged on the RAW request target;
  * any method but GET, HEAD included, is 405 with Allow: GET;
  * --bind is required, and a wildcard bind is refused before anything listens.

What this file proves, each refusal beside its control (the valid name served, or the valid
input accepted, in the same test):
  * the index lists exactly the two valid files among decoys: a name that does not match, an
    uppercase one, an in-progress .partial, one with non-ASCII digits, a DIRECTORY with a valid
    name, and a valid name one directory down; a file symlink and a junction with valid names
    are neither listed nor served; a stat that says "regular file" and carries the
    reparse-point attribute is not listable;
  * each download is byte-identical (CR, LF, Ctrl-Z and NUL bytes, several chunks) with the
    right Content-Length and content type;
  * every refused target form is 404 over HTTP, AND the raw-target guard refuses each one on
    its own: the listing lookup behind the guard would 404 most of them too, so over HTTP alone
    the guard's removal would not show;
  * a single unlisted name is 404, including forms Windows would open as the valid file
    (another letter case, a trailing dot, an alternate data stream);
  * POST, PUT, DELETE, HEAD, OPTIONS, PATCH and an unknown method are 405 with Allow: GET;
  * a wildcard or non-address --bind is refused with exit 3 and never reaches a listener (the
    listener is replaced here, so a check that let one through is a red test, never a socket on
    every interface of the box running the suite); --bind is required; --dir must be a
    directory;
  * no cache: a file added after startup is on the next index; a file deleted after it was
    listed is 404; a name replaced after the listing is not opened;
  * a download in progress does not stop the file being deleted (what `prune` does);
  * one log line per request (UTC time, client, method, raw target, status, body bytes),
    control characters escaped, never file contents;
  * the Server header names no Python, and the stdlib's own error responses get the fixed body;
  * an unreadable folder is a 500, not a dropped connection;
  * `python -m tools.backup serve` itself starts, serves and logs.

Nothing here uses a database; MESHY_NO_TEST_DB=1 skips the harness's per-run one.
Run:  .venv313/Scripts/python.exe -m pytest tests/test_backup_serve.py -q
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.backup import pgbackup, serve  # noqa: E402

UTC = dt.timezone.utc
DUMP = "meshy_django-20260925T030000Z.dump"
MANIFEST = "meshy_django-20260925T030000Z.manifest.json"
NESTED = "meshy_django-20260922T030000Z.dump"
DUMP_MTIME = dt.datetime(2026, 9, 25, 3, 0, 5, tzinfo=UTC)
MANIFEST_MTIME = dt.datetime(2026, 9, 25, 3, 0, 6, tzinfo=UTC)
# Found in the dump and in the file a link points at: neither may ever appear in a log line or
# in a refusal's body.
MARKER = b"FILE-CONTENTS-MUST-NEVER-BE-LOGGED"
SECRET = b"SECRET-OUTSIDE-THE-EXPORT-FOLDER"
# CR, LF, Ctrl-Z (end of file to a text-mode read on Windows) and NUL, then enough bytes for
# several chunks: a download that is not byte-for-byte, or stops after the first chunk, shows.
DUMP_BYTES = MARKER + b"\r\n\x1a\n\r\x00" * 64 + random.Random(406).randbytes(5 * (1 << 20) // 2)
MANIFEST_BYTES = json.dumps({"dump_file": DUMP, "dump_bytes": len(DUMP_BYTES),
                             "dump_sha256": hashlib.sha256(DUMP_BYTES).hexdigest()},
                            indent=1, sort_keys=True).encode()
DECOYS = {
    "notes.txt": b"not a backup",
    "Meshy_django-20260924T030000Z.dump": b"uppercase",
    "meshy_django-20260924T030000Z.dump.partial": b"a write in progress",
    # Arabic-Indic digits: Python's \d matches them unless the pattern is ASCII-only.
    "meshy_django-\u0662\u0660\u0662\u0666\u0660\u0669\u0662\u0664T030000Z.dump": b"not ASCII",
}
DIR_DECOY = "meshy_django-20260923T030000Z.dump"            # a DIRECTORY with a valid name
LOG_TIME = "%Y-%m-%dT%H:%M:%S.%fZ"


def _unavailable(msg: str):
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(msg)
    pytest.skip(msg)


def _stamp(path: Path, when: dt.datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def _rfc3339(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def export(tmp_path):
    d = tmp_path / "export"
    d.mkdir()
    (d / DUMP).write_bytes(DUMP_BYTES)
    (d / MANIFEST).write_bytes(MANIFEST_BYTES)
    _stamp(d / DUMP, DUMP_MTIME)
    _stamp(d / MANIFEST, MANIFEST_MTIME)
    for name, data in DECOYS.items():
        (d / name).write_bytes(data)
    (d / DIR_DECOY).mkdir()
    (d / DIR_DECOY / "inside.bin").write_bytes(SECRET)
    (d / "sub").mkdir()
    (d / "sub" / NESTED).write_bytes(SECRET)
    return d


@pytest.fixture
def running(export):
    """A server on 127.0.0.1, an ephemeral port, over `export`, in a thread; its log lines
    collect in `.lines` instead of stdout."""
    lines: list[str] = []
    srv = serve.make_server(export, "127.0.0.1", 0, log=lines.append)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=srv.server_address[1], dir=export, lines=lines)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(10)


def _exchange(port: int, request: bytes) -> tuple[int, dict[str, str], bytes]:
    """(status, headers with lower-cased names, body) for request bytes sent EXACTLY as given.
    http.client and urllib would normalise some of the targets this file must send as-is. The
    server closes the connection after each response, so the body is everything to EOF."""
    with socket.create_connection(("127.0.0.1", port), timeout=30) as s:
        s.sendall(request)
        buf = bytearray()
        while chunk := s.recv(1 << 16):
            buf += chunk
    head, sep, body = bytes(buf).partition(b"\r\n\r\n")
    assert sep, f"no end of headers in {bytes(buf[:200])!r}"
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return int(lines[0].split()[1]), headers, body


def _http(port: int, target: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    return _exchange(port, f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                           "Connection: close\r\n\r\n".encode("latin-1"))


def _names(port: int) -> list[str]:
    status, _, body = _http(port, "/index.json")
    assert status == 200
    return [f["name"] for f in json.loads(body)["files"]]


def _served(port: int, name: str = DUMP) -> bool:
    """The control beside a refusal: the valid file, whole, is still served."""
    status, _, body = _http(port, "/" + name)
    return status == 200 and body == {DUMP: DUMP_BYTES, MANIFEST: MANIFEST_BYTES}[name]


# ------------------------------------------------------------------ what the index lists

def test_the_index_lists_exactly_the_valid_backups_among_the_decoys(running):
    present = {p.name for p in running.dir.iterdir()}
    assert present >= set(DECOYS) | {DIR_DECOY, "sub"}, "a decoy is missing: the test is vacuous"
    status, headers, body = _http(running.port, "/index.json")
    assert status == 200 and headers["content-type"] == "application/json"
    assert int(headers["content-length"]) == len(body)
    assert json.loads(body) == {"files": [
        {"name": DUMP, "bytes": len(DUMP_BYTES), "mtime": _rfc3339(DUMP_MTIME)},
        {"name": MANIFEST, "bytes": len(MANIFEST_BYTES), "mtime": _rfc3339(MANIFEST_MTIME)},
    ]}


def test_a_symlink_with_a_valid_name_is_neither_listed_nor_served(running, tmp_path):
    secret = tmp_path / "secret.bin"
    secret.write_bytes(SECRET)
    link = running.dir / "meshy_django-20260921T030000Z.dump"
    try:
        os.symlink(secret, link)
    except OSError as exc:
        # NOT _unavailable: the gate runs non-elevated with CI=true, and creating a symlink on
        # Windows needs elevation or Developer Mode. The branch it tests (a reparse point is
        # never listed or served) still runs in the junction and synthetic-reparse tests
        # below, so this one SKIPS, pinned by name and reason in local_gate's ALLOWED_SKIPS.
        pytest.skip(f"this account cannot create a symlink without elevation ({exc}); "
                    "the junction test still covers a reparse point")
    # The link works: a server that followed it would hand out SECRET.
    assert link.is_symlink() and link.read_bytes() == SECRET
    assert _names(running.port) == [DUMP, MANIFEST]
    status, _, body = _http(running.port, "/" + link.name)
    assert status == 404 and SECRET not in body
    assert _served(running.port)


@pytest.mark.skipif(os.name != "nt", reason="a junction is a Windows reparse point; the "
                                            "symlink test covers POSIX")
def test_a_junction_with_a_valid_name_is_neither_listed_nor_served(running, tmp_path):
    import _winapi
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / DUMP).write_bytes(SECRET)
    junction = running.dir / "meshy_django-20260920T030000Z.dump"
    _winapi.CreateJunction(str(target), str(junction))
    assert os.lstat(junction).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert (junction / DUMP).read_bytes() == SECRET              # it reaches outside the folder
    assert _names(running.port) == [DUMP, MANIFEST]
    for target_path in ("/" + junction.name, f"/{junction.name}/{DUMP}"):
        status, _, body = _http(running.port, target_path)
        assert status == 404 and SECRET not in body, target_path
    assert _served(running.port)


def test_a_regular_file_mode_with_the_reparse_attribute_is_not_listable():
    """CPython's Windows lstat says S_IFLNK only for IO_REPARSE_TAG_SYMLINK; another
    name-surrogate reparse point on a file (a WSL symlink, IO_REPARSE_TAG_LX_SYMLINK) comes back
    S_IFREG with the reparse attribute, so the mode alone is not enough. Such a file cannot be
    made here without WSL, hence a synthetic stat."""
    regular = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0)
    reparse = SimpleNamespace(st_mode=stat.S_IFREG | 0o644,
                              st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
    assert serve.is_listable(regular)                                    # control
    assert not serve.is_listable(reparse)
    assert not serve.is_listable(SimpleNamespace(st_mode=stat.S_IFDIR | 0o755))
    assert not serve.is_listable(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777))
    assert serve.is_listable(SimpleNamespace(st_mode=stat.S_IFREG | 0o600))   # POSIX: no attrs


# ------------------------------------------------------------------ what a download is

def test_each_listed_file_downloads_byte_identical_with_its_content_length(running):
    assert len(DUMP_BYTES) > 2 * serve.CHUNK, "the dump must span several chunks"
    for name, data, ctype in ((DUMP, DUMP_BYTES, "application/octet-stream"),
                              (MANIFEST, MANIFEST_BYTES, "application/json")):
        status, headers, body = _http(running.port, "/" + name)
        assert status == 200 and headers["content-type"] == ctype, name
        assert int(headers["content-length"]) == len(data) == len(body), name
        assert hashlib.sha256(body).hexdigest() == hashlib.sha256(data).hexdigest(), name


# ------------------------------------------------------------------ what is refused

REFUSED = [
    ("a query", f"/{DUMP}?x=1"),
    ("an empty query", f"/{DUMP}?"),
    ("a query on the index", "/index.json?x=1"),
    ("a fragment", f"/{DUMP}#x"),
    ("a percent-encoded letter", f"/%6D{DUMP[1:]}"),
    ("a percent-encoded dot", "/" + DUMP.replace(".dump", "%2Edump")),
    ("encoded traversal", f"/..%2Fexport%2F{DUMP}"),
    ("traversal back into the folder", f"/../export/{DUMP}"),
    ("a lone ..", "/.."),
    ("a dot segment", f"/./{DUMP}"),
    ("a backslash into a subdirectory", "/sub\\" + NESTED),
    ("backslash traversal", f"/..\\export\\{DUMP}"),
    # The stdlib rewrites self.path "//NAME" to "/NAME" (gh-87389): judged on self.path,
    # this one would be served.
    ("a leading double slash", f"//{DUMP}"),
    ("two segments into a subdirectory", f"/sub/{NESTED}"),
    ("a trailing slash", f"/{DUMP}/"),
    ("the absolute form", f"http://127.0.0.1/{DUMP}"),
    ("the asterisk form", "*"),
    ("no leading slash", DUMP),
    ("the bare root", "/"),
]


@pytest.mark.parametrize("target", [t for _, t in REFUSED], ids=[w for w, _ in REFUSED])
def test_a_refused_target_is_404_with_the_fixed_body(running, target):
    status, headers, body = _http(running.port, target)
    assert status == 404 and body == serve.fixed_body(404) and len(body) < 32
    assert int(headers["content-length"]) == len(body)
    assert MARKER not in body and SECRET not in body
    assert _served(running.port)


@pytest.mark.parametrize("target", [t for _, t in REFUSED], ids=[w for w, _ in REFUSED])
def test_the_raw_target_guard_refuses_it_on_its_own(target):
    assert serve.request_name(target) is None
    assert serve.request_name("/" + DUMP) == DUMP                          # control
    assert serve.request_name("/index.json") == "index.json"


UNLISTED = [
    ("a name that does not match", "/notes.txt"),
    ("an uppercase name", "/Meshy_django-20260924T030000Z.dump"),
    ("the valid name in upper case", "/" + DUMP.upper()),
    ("a trailing dot", f"/{DUMP}."),
    ("an alternate data stream", f"/{DUMP}::$DATA"),
    ("a write in progress", "/meshy_django-20260924T030000Z.dump.partial"),
    ("a directory with a valid name", "/" + DIR_DECOY),
    ("a name nobody wrote", "/meshy_django-20250101T000000Z.dump"),
]


@pytest.mark.parametrize("target", [t for _, t in UNLISTED], ids=[w for w, _ in UNLISTED])
def test_a_name_the_index_does_not_list_is_404(running, target):
    """Several of these name the valid file to Windows (the file system ignores case, a
    trailing dot and ::$DATA), so the file opened must be DIR / a LISTED name, never DIR /
    the request's text."""
    status, _, body = _http(running.port, target)
    assert status == 404 and body == serve.fixed_body(404)
    assert MARKER not in body and SECRET not in body
    assert _served(running.port)


@pytest.mark.parametrize("target", ["/index.json", "/" + DUMP])
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH", "BREW"])
def test_every_method_but_get_is_405_with_allow_get(running, method, target):
    status, headers, body = _http(running.port, target, method)
    assert (status, headers.get("allow")) == (405, "GET")
    assert body == (b"" if method == "HEAD" else serve.fixed_body(405))
    assert MARKER not in body
    assert _http(running.port, target)[0] == 200                          # control: GET


# ------------------------------------------------------------------ what it refuses to start on

@pytest.fixture
def listener(monkeypatch):
    """Replaces the listener: records each address it would have bound and never opens a
    socket; serving ends as Ctrl+C ends it."""
    bound: list = []

    class NeverListens:
        def __init__(self, address, folder, log):
            bound.append(address)
            self.server_address, self.folder = address, folder

        def serve_forever(self, *args, **kwargs):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(serve, "_Server", NeverListens)
    return bound


WILDCARDS = [("ipv4 any", "0.0.0.0"), ("ipv6 any", "::"), ("empty", ""), ("blank", " "),
             ("ipv6 any, long form", "0:0:0:0:0:0:0:0"), ("ipv4-mapped any", "::ffff:0.0.0.0"),
             ("'0', which resolves to 0.0.0.0", "0"), ("a host name", "localhost")]


@pytest.mark.parametrize("bind", [b for _, b in WILDCARDS], ids=[w for w, _ in WILDCARDS])
def test_a_wildcard_or_non_address_bind_is_refused_before_anything_listens(
        export, listener, capsys, bind):
    code = pgbackup.main(["serve", "--dir", str(export), "--bind", bind, "--port", "0"])
    out, err = capsys.readouterr()
    assert code == 3 and listener == []
    last = json.loads(out.strip().splitlines()[-1])
    assert last["ok"] is False and "--bind" in last["error"]
    assert "192.168.50.20" in err                        # the message names the fix
    # control: one specific address reaches the listener
    assert pgbackup.main(["serve", "--dir", str(export), "--bind", "127.0.0.1",
                          "--port", "0"]) == 0
    assert listener == [("127.0.0.1", 0)]


def test_bind_is_required(export, listener, capsys):
    with pytest.raises(SystemExit) as exc:
        pgbackup.main(["serve", "--dir", str(export), "--port", "0"])
    assert exc.value.code == 2 and "--bind" in capsys.readouterr().err
    assert listener == []


@pytest.mark.parametrize("which", ["missing", "a file"])
def test_dir_must_be_an_existing_directory(tmp_path, listener, capsys, which):
    target = tmp_path / "nowhere"
    if which == "a file":
        target.write_bytes(b"x")
    assert pgbackup.main(["serve", "--dir", str(target), "--bind", "127.0.0.1",
                          "--port", "0"]) == 3
    assert listener == [] and "--dir" in capsys.readouterr().err
    assert pgbackup.main(["serve", "--dir", str(tmp_path), "--bind", "127.0.0.1",
                          "--port", "0"]) == 0                                # control
    assert listener == [("127.0.0.1", 0)]


# ------------------------------------------------------------------ no cache

def test_a_file_added_after_startup_is_on_the_next_index(running):
    assert _names(running.port) == [DUMP, MANIFEST]
    late = {"aaa-20260926T030000Z.dump": b"sorted first",
            "meshy_django-20260926T030000Z.manifest.json": b"{}"}
    for name, data in late.items():
        (running.dir / name).write_bytes(data)
    assert _names(running.port) == sorted([DUMP, MANIFEST, *late])
    for name, data in late.items():
        status, _, body = _http(running.port, "/" + name)
        assert status == 200 and body == data


def test_a_file_deleted_after_it_was_listed_is_404(running):
    assert DUMP in _names(running.port)
    os.remove(running.dir / DUMP)
    status, _, body = _http(running.port, "/" + DUMP)
    assert status == 404 and body == serve.fixed_body(404)
    assert _names(running.port) == [MANIFEST]
    assert _served(running.port, MANIFEST)                                   # control


def test_a_name_replaced_after_the_listing_is_not_opened(export):
    listed = serve.listing(export)[DUMP]
    replacement = export / "replacement.tmp"
    replacement.write_bytes(b"not the file that was listed")
    os.replace(replacement, export / DUMP)
    assert serve.open_listed(export / DUMP, listed) is None
    f = serve.open_listed(export / DUMP, serve.listing(export)[DUMP])     # control
    assert f is not None
    with f:
        assert f.read() == b"not the file that was listed"


def test_a_download_in_progress_does_not_stop_the_file_being_deleted(running):
    """`prune` deletes old backups from this folder. On Windows a file that Python's open()
    holds cannot be deleted (WinError 32), so a pull in progress would make the daily prune
    fail. The server opens the file before it sends the headers; 32 MiB does not fit in the two
    sockets' buffers (the receive buffer is fixed at 64 KiB, which also stops Windows growing
    the window), so once the headers are here the server holds the file mid-body."""
    big = running.dir / "meshy_django-20260926T030000Z.dump"
    data = random.Random(26).randbytes(32 << 20)
    big.write_bytes(data)
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 16)
        s.settimeout(30)
        s.connect(("127.0.0.1", running.port))
        s.sendall(f"GET /{big.name} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            assert chunk, "the connection closed before the headers"
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        assert head.split(b"\r\n")[0].split()[1] == b"200"
        os.remove(big)                                     # what retention.prune does
        while chunk := s.recv(1 << 16):
            body += chunk
    assert len(body) == len(data)
    assert hashlib.sha256(body).digest() == hashlib.sha256(data).digest()
    assert _http(running.port, "/" + big.name)[0] == 404
    assert _served(running.port)                                             # control


# ------------------------------------------------------------------ the log and the headers

def test_each_request_is_one_log_line_and_never_the_contents(running):
    before = dt.datetime.now(UTC).replace(tzinfo=None) - dt.timedelta(seconds=1)
    sent = [("GET", "/index.json"), ("GET", "/" + DUMP), ("GET", "//" + DUMP),
            ("GET", "/nope"), ("POST", "/index.json"), ("HEAD", "/" + DUMP),
            ("GET", "/\x1b[2J")]
    results = [_http(running.port, target, method) for method, target in sent]
    after = dt.datetime.now(UTC).replace(tzinfo=None) + dt.timedelta(seconds=1)
    assert len(running.lines) == len(sent), running.lines
    for (method, target), (status, _, body), line in zip(sent, results, running.lines):
        when, ip, logged_method, logged_target, logged_status, sent_bytes = line.split(" ")
        assert before <= dt.datetime.strptime(when, LOG_TIME) <= after, line
        logged = target.replace("\x1b", "\\x1b")                     # escaped, never raw
        assert (ip, logged_method, logged_target, int(logged_status), int(sent_bytes)) == \
            ("127.0.0.1", method, logged, status, len(body)), line
        assert MARKER.decode() not in line and "\x1b" not in line
    assert [int(line.split(" ")[4]) for line in running.lines] == [200, 200, 404, 404, 405, 405,
                                                                   404]


def test_the_server_header_names_no_python_and_the_stdlibs_errors_get_the_fixed_body(running):
    for target in ("/index.json", "/" + DUMP, "/nope"):
        server = _http(running.port, target)[1]["server"]
        assert server, target                                              # control: present
        assert "python" not in server.lower() and platform.python_version() not in server
    # A request line the stdlib answers itself (400); its default page repeats the line back.
    status, headers, body = _exchange(running.port, b"NONSENSE-REQUEST-LINE\r\n\r\n")
    assert status == 400 and body == serve.fixed_body(400)
    assert b"NONSENSE" not in body and "python" not in headers["server"].lower()


def test_an_unreadable_folder_is_a_500_not_a_dropped_connection(running):
    assert _names(running.port) == [DUMP, MANIFEST]                          # control
    shutil.rmtree(running.dir)
    for target in ("/index.json", "/" + DUMP):
        status, _, body = _http(running.port, target)
        assert status == 500 and body == serve.fixed_body(500), target


# ------------------------------------------------------------------ the command itself

def test_the_cli_serves_the_folder_and_logs_each_request(export):
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.backup", "serve", "--dir", str(export),
         "--bind", "127.0.0.1", "--port", "0"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    try:
        first = proc.stdout.readline()
        found = re.search(r"http://127\.0\.0\.1:(\d+)/", first)
        assert found, f"no address in {first!r}"
        port = int(found.group(1))
        assert _served(port)
        line = proc.stdout.readline().split(" ")
        assert line[1:] == ["127.0.0.1", "GET", "/" + DUMP, "200", f"{len(DUMP_BYTES)}\n"]
        assert _http(port, "/notes.txt")[0] == 404
        assert proc.stdout.readline().split(" ")[4] == "404"
    finally:
        watchdog.cancel()
        proc.kill()
        proc.communicate(timeout=30)
