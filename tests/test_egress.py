"""SSRF-safe egress (divergence 041 S8): the tests E1-E9 and R8 of the design's section 6.

WHAT IS UNDER TEST. `apps/core/egress.py`, the one way this server will POST to a URL a PEER
controls (S9's doorbell and its proof-of-control challenge; push configs later). It enforces
the egress rules of design 4.11 and 2.12.12. No caller exists in this slice.

HOW THE NETWORK IS FAKED, and what is real.
  * `egress.resolve_host` (the system resolver) is replaced by a scripted resolver that records
    every name it is asked for, so "resolved once" and "never resolved" are counted, not
    assumed. One test keeps the real resolver (`localhost`).
  * `egress.dial` (the one socket this module opens) is replaced by a recorder that notes the
    (ip, port) it was asked to dial and then connects to a LOCAL server, so a pinned public
    address is visible in the record while the bytes go to 127.0.0.1. The hatch tests keep the
    real `dial`.
  * TLS is real: a CA and leaf certificates are made here, and SSL_CERT_FILE makes the
    PRODUCTION `tls_context()` trust that CA (measured on this box: OpenSSL reads the variable
    at context creation), so its verification settings are the ones under test.
  * Every test data list is written HERE, never read from the module: a table the tests read
    from the code under test would shrink with the code, and a removed blocklist arm would
    remove its own test case.

The literal tables cover URL, unit, and embedded IPv4/IPv6 forms. HTTP is refused before any
address check (4.11: HTTPS only), so the URL forms are tested with HTTPS and HTTP separately.

Run:  .venv/Scripts/python.exe -m pytest tests/test_egress.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime
import http.server
import importlib
import ipaddress
import os
import socket
import ssl
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

PUBLIC = "93.184.216.34"
NAME = "hook.test"


@pytest.fixture(scope="module")
def eg():
    """The module, imported per test module so each test reports its own failure while the
    module is absent (the RED run), instead of one collection error."""
    return importlib.import_module("apps.core.egress")


# ------------------------------------------------------------------ blocklist battery

#: The hosts of 13 URLs that must be refused.
BLOCKED_URL_HOSTS = [
    "127.0.0.1", "169.254.169.254", "10.0.0.5", "172.16.3.4", "192.168.1.1", "::1",
    "fd00::1", "fe80::1", "::ffff:127.0.0.1", "0.0.0.0", "100.64.0.1", "fec0::1",
    "::ffff:0:7f00:1",
]
#: Literal addresses that must be blocked.
BLOCKED_UNIT_IPS = [
    "127.0.0.1", "10.1.2.3", "172.20.0.1", "192.168.7.1", "169.254.169.254", "0.0.0.0",
    "::1", "fd12::1", "fe80::abcd", "fec0::1", "::ffff:0:7f00:1", "::ffff:0:a9fe:a9fe",
]
#: IPv6 forms embedding blocked IPv4 addresses.
BLOCKED_EMBEDDED_IPS = [
    "::7f00:1", "::a9fe:a9fe", "64:ff9b::7f00:1", "64:ff9b::a00:1", "2002:7f00:1::",
    "2002:a9fe:a9fe::", "2001::1234", "::ffff:0:7f00:1", "::ffff:0:a9fe:a9fe",
]
#: URL hosts with embedded IPv4 forms that must be blocked.
BLOCKED_EMBEDDED_URL_HOSTS = ["64:ff9b::7f00:1", "2002:7f00:1::", "::a9fe:a9fe"]
BLOCKED_HOSTS = list(dict.fromkeys(
    BLOCKED_URL_HOSTS + BLOCKED_UNIT_IPS + BLOCKED_EMBEDDED_IPS
    + BLOCKED_EMBEDDED_URL_HOSTS))

#: Public addresses and transition forms that embed a public IPv4 address: the guard blocks
#: the embedded target, not the mechanism.
ALLOWED_PUBLIC_HOSTS = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111",
                    "64:ff9b::808:808", "::808:808", "::ffff:0:808:808"]

#: One witness per blocklist arm, blocked by that arm alone, so a removed arm unblocks its
#: witness (the blocklist is tested apart from is_global, which would
#: otherwise block most of these on its own).
ARM_WITNESSES = [
    ("0.0.0.0/8", "0.1.2.3"),
    ("10.0.0.0/8", "10.200.0.1"),
    ("100.64.0.0/10", "100.127.255.254"),
    ("127.0.0.0/8", "127.8.9.10"),
    ("169.254.0.0/16", "169.254.1.1"),
    ("172.16.0.0/12", "172.31.255.1"),
    ("192.0.0.0/24", "192.0.0.9"),
    ("192.0.2.0/24", "192.0.2.7"),
    ("192.168.0.0/16", "192.168.77.1"),
    ("198.51.100.0/24", "198.51.100.7"),
    ("203.0.113.0/24", "203.0.113.7"),
    ("240.0.0.0/4", "255.255.255.255"),
    ("::1/128", "::1"),
    ("::/128", "::"),
    ("fc00::/7", "fc00::1"),
    ("fe80::/10", "febf::1"),
    ("fec0::/10", "fec0::1"),
    ("2001:db8::/32", "2001:db8::1"),
    ("2001::/32 Teredo", "2001:0:4136:e378::1"),
    ("v4-mapped ::ffff:0:0/96", "::ffff:10.0.0.1"),
    ("v4-compatible ::/96", "::a00:1"),
    ("NAT64 64:ff9b::/96", "64:ff9b::a9fe:a9fe"),
    ("SIIT ::ffff:0:0:0/96", "::ffff:0:c0a8:1"),
    ("6to4 2002::/16", "2002:c0a8:101::"),
]

#: The blocklist blocks these and ipaddress calls them GLOBAL on this interpreter (design 4.11,
#: measured again below as the test's own control): is_global alone would admit them.
ONLY_THE_BLOCKLIST_BLOCKS = ["fec0::1", "::7f00:1", "64:ff9b::7f00:1", "::ffff:0:7f00:1",
                        "::a9fe:a9fe", "192.0.0.9"]
#: ipaddress refuses these and the blocklist admits them: the blocklist alone would admit them.
#: The first is the 6to4 form of a public IPv4 address; the stricter answer wins.
ONLY_IS_GLOBAL_BLOCKS = ["2002:808:808::", "198.18.0.1", "64:ff9b:1::808:808"]


def _url(host: str, scheme: str = "https", path: str = "/x") -> str:
    return f"{scheme}://[{host}]{path}" if ":" in host else f"{scheme}://{host}{path}"


# ------------------------------------------------------------------ the fake network

class Net:
    """A scripted resolver and a recording dial. `answers[i]` is the i-th resolution's
    answer (the last repeats); an exception instance is raised instead. `route` is where a
    dial really connects (a local server), or None for a refused connection."""

    def __init__(self, answers=(), route=None, delay=0.0):
        self.answers = list(answers)
        self.route = route
        self.delay = delay
        self.resolved: list[str] = []
        self.dialed: list[tuple] = []
        #: The module's own `dial`, kept before the recorder replaces it, for a test that must
        #: run the production socket code.
        self.production_dial = None

    def resolve(self, host):
        self.resolved.append(host)
        if self.delay:
            time.sleep(self.delay)
        answer = self.answers[min(len(self.resolved), len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)

    def dial(self, address, timeout):
        self.dialed.append((address[0], address[1]))
        if self.route is None:
            raise ConnectionRefusedError("the test network routes nothing")
        return socket.create_connection(self.route, timeout=timeout)


@pytest.fixture
def net(eg, monkeypatch):
    """Install a Net; the test sets its answers and route."""
    n = Net()
    n.production_dial = eg.dial
    monkeypatch.setattr(eg, "resolve_host", n.resolve)
    monkeypatch.setattr(eg, "dial", n.dial)
    return n


@pytest.fixture
def hatch():
    """The test hatch as a test or dev box has it: the setting on AND DEBUG on."""
    with override_settings(DEBUG=True, MESHY_EGRESS_ALLOW_LOOPBACK=True):
        yield


@pytest.fixture
def no_hatch():
    with override_settings(DEBUG=False, MESHY_EGRESS_ALLOW_LOOPBACK=False):
        yield


# ------------------------------------------------------------------ local servers

@dataclass
class Seen:
    method: str
    path: str
    headers: dict
    body: bytes


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.requests.append(Seen(
            self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        status, headers, payload = self.server.respond(self.path)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass     # the client read its 64 KiB and hung up: expected

    do_POST = do_GET = _handle


class Recorder(http.server.ThreadingHTTPServer):
    """A local HTTP(S) server that records each request and answers from `respond(path)`."""

    daemon_threads = True

    def __init__(self, respond, tls: ssl.SSLContext | None = None):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.respond = respond
        self.requests: list[Seen] = []
        self.sni: list = []
        if tls is not None:
            tls.sni_callback = lambda sock, name, ctx: self.sni.append(name)
            self.socket = tls.wrap_socket(self.socket, server_side=True)
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()

    @property
    def address(self):
        return self.server_address[:2]

    def handle_error(self, request, client_address):
        pass

    def stop(self):
        self.shutdown()
        self.server_close()


class TcpRecorder:
    """A bare TCP listener that counts the connections it accepts (the proxy). With `drip`,
    it answers a status line and then one header byte every 0.1 s, forever. With `stall`, it
    answers a 200 whose Content-Length is `length`, sends only `stall` bytes of that body, and
    then holds the connection open, sending nothing more or, with `body_drip`, one more body
    byte every 0.1 s. With `tls`, each connection it accepts is TLS first, server side, with a
    2 s handshake."""

    def __init__(self, drip: bool = False, tls: ssl.SSLContext | None = None,
                 stall: int | None = None, length: int = 10 * 1024 * 1024,
                 body_drip: bool = False):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.settimeout(0.05)
        self.connections = 0
        self.first_bytes: list[bytes] = []
        self.drip = drip
        self.tls = tls
        self.stall = stall
        self.length = length
        self.body_drip = body_drip
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def address(self):
        return self.sock.getsockname()[:2]

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(2)
        try:
            if self.tls is not None:
                conn = self.tls.wrap_socket(conn, server_side=True)
            self.first_bytes.append(conn.recv(4096))
            if self.stall is not None:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % self.length
                             + b"z" * self.stall)
                while not self._stop.wait(0.1):
                    if self.body_drip:
                        conn.sendall(b"z")
                return
            if not self.drip:
                return
            conn.sendall(b"HTTP/1.1 200 OK\r\nX-Drip: ")
            while not self._stop.is_set():
                conn.sendall(b"a")
                time.sleep(0.1)
        except OSError:
            return
        finally:
            conn.close()

    def stop(self):
        self._stop.set()
        self.sock.close()


class DripRelay:
    """A TCP relay to `upstream` that passes the client's bytes through at once and drips the
    server's back one byte every 20 ms: a peer that holds the TLS handshake open."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.settimeout(0.05)
        self.connections = 0
        self.dripped = 0
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def address(self):
        return self.sock.getsockname()[:2]

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn):
        try:
            up = socket.create_connection(self.upstream, timeout=2)
        except OSError:
            conn.close()
            return

        def client_to_server():
            try:
                while data := conn.recv(4096):
                    up.sendall(data)
            except OSError:
                pass

        threading.Thread(target=client_to_server, daemon=True).start()
        try:
            while not self._stop.is_set() and (data := up.recv(4096)):
                for i in range(len(data)):
                    if self._stop.is_set():
                        return
                    conn.sendall(data[i:i + 1])
                    self.dripped += 1
                    time.sleep(0.02)
        except OSError:
            pass
        finally:
            conn.close()
            up.close()

    def stop(self):
        self._stop.set()
        self.sock.close()


@pytest.fixture
def servers():
    made = []

    def make(kind, *args, **kwargs):
        s = kind(*args, **kwargs)
        made.append(s)
        return s

    yield make
    for s in reversed(made):     # a relay stops before the server behind it
        s.stop()


def ok(body: bytes = b"ok"):
    return lambda path: (200, {"Content-Type": "text/plain"}, body)


# ------------------------------------------------------------------ certificates

@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """A test CA, and leaves for `hook.test` and `other.test` it signed, as PEM files."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    d = tmp_path_factory.mktemp("egress_pki")
    now = datetime.datetime.now(datetime.timezone.utc)
    day = datetime.timedelta(days=1)

    def usage(ca: bool):
        return x509.KeyUsage(digital_signature=True, content_commitment=False,
                             key_encipherment=False, data_encipherment=False,
                             key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                             encipher_only=False, decipher_only=False)

    def cn(text):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)])

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(cn("egress test CA")).issuer_name(cn("egress test CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - day).not_valid_after(now + day)
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(usage(True), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    (d / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    out = {"ca": str(d / "ca.pem")}
    for name in ("hook.test", "other.test"):
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder().subject_name(cn(name)).issuer_name(ca.subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - day).not_valid_after(now + day)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(usage(False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                               critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        (d / f"{name}.key").write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        (d / f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        out[name] = (str(d / f"{name}.pem"), str(d / f"{name}.key"))
    return out


def server_tls(pki, name: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*pki[name])
    return ctx


@pytest.fixture
def trust_test_ca(pki, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", pki["ca"])


@pytest.fixture
def tls_hook(servers, pki, trust_test_ca):
    """An HTTPS server holding a certificate for hook.test, answering 200 `ok`."""
    return servers(Recorder, ok(), server_tls(pki, NAME))


# ================================================================== E1: the battery

@pytest.mark.parametrize("literal", BLOCKED_HOSTS)
def test_e1_every_fixture_literal_is_blocked(eg, literal, net, no_hatch):
    """Both the direct blocklist check and the guard refuse this URL host before any
    resolution or dial."""
    assert eg.in_blocklist(literal) is True, f"the port of is_blocked_ip admits {literal}"
    assert eg.is_blocked(literal) is True
    with pytest.raises(eg.EgressRefused) as exc:
        eg.post(_url(literal), b"{}")
    assert exc.value.reason == "blocked_address", exc.value
    assert net.resolved == [] and net.dialed == []


@pytest.mark.parametrize("arm,witness", ARM_WITNESSES, ids=[a for a, _ in ARM_WITNESSES])
def test_e1_each_blocklist_arm_blocks_its_witness(eg, arm, witness):
    assert eg.in_blocklist(witness) is True, f"the {arm} arm does not block {witness}"
    assert eg.is_blocked(witness) is True


def test_e1_the_fixture_urls_with_their_own_scheme_are_refused_too(eg, net, no_hatch):
    """The HTTP spelling is refused on its scheme before address resolution."""
    for host in BLOCKED_URL_HOSTS + BLOCKED_EMBEDDED_URL_HOSTS:
        with pytest.raises(eg.EgressRefused):
            eg.post(_url(host, "http"), b"{}")
    assert net.resolved == [] and net.dialed == []


# ================================================================== E2: public passes

@pytest.mark.parametrize("literal", ALLOWED_PUBLIC_HOSTS)
def test_e2_public_literals_pass_and_are_dialed_as_written(eg, literal, net, no_hatch):
    assert eg.in_blocklist(literal) is False
    assert eg.is_blocked(literal) is False
    target = eg.check_url(_url(literal, path="/hook"))
    assert str(target.literal) == str(ipaddress.ip_address(literal))
    # Admitted all the way to the socket: the dial is asked for the literal on 443, and no
    # name was resolved. The test network refuses the connection.
    with pytest.raises(eg.EgressFailed) as exc:
        eg.post(_url(literal, path="/hook"), b"{}")
    assert exc.value.reason == "connect"
    assert net.resolved == []
    assert net.dialed == [(str(ipaddress.ip_address(literal)), 443)]


def test_e2_the_two_checks_are_independent_and_either_one_blocks(eg):
    for literal in ONLY_THE_BLOCKLIST_BLOCKS:
        # The measured premise (design 4.11), re-measured: is_global admits it.
        assert ipaddress.ip_address(literal).is_global is True, literal
        assert eg.in_blocklist(literal) is True, literal
        assert eg.is_blocked(literal) is True, f"is_global alone guards {literal}"
    for literal in ONLY_IS_GLOBAL_BLOCKS:
        assert ipaddress.ip_address(literal).is_global is False, literal
        assert eg.in_blocklist(literal) is False, f"control: the port admits {literal}"
        assert eg.is_blocked(literal) is True, f"the port alone guards {literal}"


# ================================================================== E3 and R8: the pin

def test_e3_a_rebinding_answer_never_reaches_a_socket(eg, net, tls_hook, no_hatch):
    """The resolver answers public, then loopback. The request goes to the validated public
    address, with TLS and Host for the NAME; the name was resolved once. A second send
    resolves afresh and is refused on the loopback answer."""
    net.answers = [[PUBLIC], ["127.0.0.1"]]
    net.route = tls_hook.address
    r = eg.post(f"https://{NAME}/doorbell", b'{"call_id":"c1"}',
                {"Content-Type": "application/json"})
    assert r.status == 200 and r.body == b"ok" and r.truncated is False
    assert r.address == PUBLIC
    assert net.resolved == [NAME]
    assert net.dialed == [(PUBLIC, 443)]
    assert tls_hook.sni == [NAME]
    [seen] = tls_hook.requests
    assert (seen.method, seen.path, seen.headers["host"], seen.body) == (
        "POST", "/doorbell", NAME, b'{"call_id":"c1"}')
    assert seen.headers["content-type"] == "application/json"

    with pytest.raises(eg.EgressRefused) as exc:
        eg.post(f"https://{NAME}/doorbell", b"{}")
    assert exc.value.reason == "blocked_address"
    assert net.resolved == [NAME, NAME] and net.dialed == [(PUBLIC, 443)]
    assert len(tls_hook.requests) == 1


def test_r8_tls_to_the_pinned_ip_verifies_the_name(eg, net, servers, pki, trust_test_ca,
                                                  monkeypatch, no_hatch):
    """R8: a pinned IP with SNI and verification for the NAME. The positive case is E3's;
    here the two ways it must fail: a certificate for another name, and a certificate from a
    CA the context does not trust. Neither request reaches the server's handler."""
    net.answers = [[PUBLIC]]
    wrong_name = servers(Recorder, ok(), server_tls(pki, "other.test"))
    net.route = wrong_name.address
    with pytest.raises(eg.EgressFailed) as exc:
        eg.post(f"https://{NAME}/x", b"{}")
    assert exc.value.reason == "tls", exc.value
    assert wrong_name.requests == [] and wrong_name.sni == [NAME]

    right_name = servers(Recorder, ok(), server_tls(pki, NAME))
    net.route = right_name.address
    monkeypatch.delenv("SSL_CERT_FILE")
    with pytest.raises(eg.EgressFailed) as exc:
        eg.post(f"https://{NAME}/x", b"{}")
    assert exc.value.reason == "tls", exc.value
    assert right_name.requests == []
    # Control: the same server, the CA trusted again, answers.
    monkeypatch.setenv("SSL_CERT_FILE", pki["ca"])
    assert eg.post(f"https://{NAME}/x", b"{}").status == 200
    assert len(right_name.requests) == 1


# ================================================================== E4: no redirect

@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_e4_a_redirect_is_a_failure_and_is_not_followed(eg, code, servers, hatch):
    rec = servers(Recorder, lambda path: (
        (code, {"Location": "/second"}, b"") if path == "/first" else (200, {}, b"followed")))
    base = f"http://127.0.0.1:{rec.address[1]}"
    with pytest.raises(eg.EgressFailed) as exc:
        eg.post(f"{base}/first", b"{}")
    assert (exc.value.reason, exc.value.status) == ("redirect", code)
    assert [s.path for s in rec.requests] == ["/first"]


def test_e4_a_redirect_on_the_pinned_https_path_is_not_followed(eg, net, servers, pki,
                                                               trust_test_ca, no_hatch):
    rec = servers(Recorder, lambda path: (
        (302, {"Location": f"https://{NAME}/second"}, b"") if path == "/first"
        else (200, {}, b"followed")), server_tls(pki, NAME))
    net.answers = [[PUBLIC]]
    net.route = rec.address
    with pytest.raises(eg.EgressFailed) as exc:
        eg.post(f"https://{NAME}/first", b"{}")
    assert (exc.value.reason, exc.value.status) == ("redirect", 302)
    assert [s.path for s in rec.requests] == ["/first"]
    assert net.resolved == [NAME] and net.dialed == [(PUBLIC, 443)]


# ================================================================== E5: no proxy

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def test_e5_proxy_variables_are_not_read(eg, net, servers, tls_hook, monkeypatch, hatch):
    proxy = servers(TcpRecorder)
    for var in PROXY_VARS:
        monkeypatch.setenv(var, f"http://127.0.0.1:{proxy.address[1]}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    net.answers = [[PUBLIC]]
    net.route = tls_hook.address
    assert eg.post(f"https://{NAME}/x", b"{}").status == 200
    assert net.dialed == [(PUBLIC, 443)]
    assert len(tls_hook.requests) == 1

    # The PRODUCTION dial on both schemes (S8 review, finding 5: the recording dial above
    # cannot show a proxy read inside `dial`). https to a name that resolves to loopback,
    # whose port the hatch admits, and http to a loopback literal.
    assert net.production_dial.__module__ == "apps.core.egress", "control: the module's own dial"
    monkeypatch.setattr(eg, "dial", net.production_dial)
    net.answers = [["127.0.0.1"]]
    r = eg.post(f"https://{NAME}:{tls_hook.address[1]}/y", b"{}")
    assert (r.status, r.address) == (200, "127.0.0.1")
    assert [s.path for s in tls_hook.requests] == ["/x", "/y"]
    plain = servers(Recorder, ok())
    assert eg.post(f"http://127.0.0.1:{plain.address[1]}/x", b"{}").status == 200
    assert len(plain.requests) == 1
    time.sleep(0.2)
    assert proxy.connections == 0, proxy.first_bytes

    # Control: the variables are live, and a client that honours them reaches the proxy.
    with pytest.raises(Exception):
        urllib.request.build_opener().open(f"http://{NAME}/x", timeout=5)
    deadline = time.monotonic() + 3
    while proxy.connections == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proxy.connections == 1


# ================================================================== E6: userinfo

def test_e6_userinfo_does_not_smuggle_the_real_host(eg, net, no_hatch):
    """The real host of expected.com@169.254.169.254 is the address."""
    net.answers = [[PUBLIC]]
    with pytest.raises(eg.EgressRefused) as exc:
        eg.post("https://expected.com@169.254.169.254/", b"{}")
    assert exc.value.reason == "blocked_address"
    assert "169.254.169.254" in str(exc.value)
    assert net.resolved == [] and net.dialed == []
    # Userinfo on a name that would resolve public is refused for carrying userinfo.
    for url in (f"https://user@{NAME}/", f"https://user:pw@{NAME}/", f"https://@{NAME}/"):
        with pytest.raises(eg.EgressRefused) as exc:
            eg.post(url, b"{}")
        assert exc.value.reason == "userinfo", url
    assert net.resolved == [] and net.dialed == []


# ================================================================== E7 and E8: resolution

def test_e7_an_empty_resolution_is_refused(eg, net, no_hatch):
    net.answers = [[]]
    with pytest.raises(eg.EgressRefused) as exc:
        eg.post(f"https://{NAME}/x", b"{}")
    assert exc.value.reason == "no_addresses"
    assert net.resolved == [NAME] and net.dialed == []


@pytest.mark.parametrize("answer", [
    [PUBLIC, "10.0.0.5"],
    ["2606:4700::1111", "::ffff:169.254.169.254"],
    ["8.8.8.8", "1.1.1.1", "127.0.0.1"],
    ["8.8.8.8", "2002:808:808::"],
    [PUBLIC, "not-an-address"],
])
def test_e8_a_mixed_resolution_is_refused(eg, net, answer, no_hatch):
    """One blocked address fails the whole name wherever it is in the answer. A public address
    comes first in each answer."""
    net.answers = [answer]
    with pytest.raises(eg.EgressRefused) as exc:
        eg.post(f"https://{NAME}/x", b"{}")
    assert exc.value.reason == "blocked_address"
    assert net.dialed == []


def test_e8_a_fully_public_resolution_dials_its_first_address(eg, net, no_hatch):
    net.answers = [["8.8.8.8", "1.1.1.1"]]
    with pytest.raises(eg.EgressFailed):
        eg.post(f"https://{NAME}/x", b"{}")
    assert net.dialed == [("8.8.8.8", 443)]


def test_a_failed_or_slow_resolution_is_refused_within_the_cap(eg, net, monkeypatch, no_hatch):
    net.answers = [socket.gaierror(11001, "getaddrinfo failed")]
    with pytest.raises(eg.EgressRefused) as exc:
        eg.post(f"https://{NAME}/x", b"{}")
    assert exc.value.reason == "resolve_failed"

    monkeypatch.setattr(eg, "RESOLVE_TIMEOUT_S", 0.2)
    slow = Net([[PUBLIC]], delay=3.0)
    monkeypatch.setattr(eg, "resolve_host", slow.resolve)
    monkeypatch.setattr(eg, "dial", slow.dial)
    t0 = time.monotonic()
    with pytest.raises(eg.EgressRefused) as exc:
        eg.post(f"https://{NAME}/x", b"{}")
    elapsed = time.monotonic() - t0
    assert exc.value.reason == "resolve_timeout"
    assert elapsed < 1.5, f"the resolution cap did not bound the wait: {elapsed:.2f} s"
    assert slow.dialed == []


def test_the_system_resolver_is_used_and_a_name_for_loopback_is_refused(eg, monkeypatch,
                                                                       no_hatch):
    """The real resolver returns loopback for `localhost`, which is refused without dialing."""
    assert {"127.0.0.1"} <= set(eg.resolve_host("localhost")), "control: the name resolves"
    dialed = []
    monkeypatch.setattr(eg, "dial", lambda address, timeout: dialed.append(address))
    with pytest.raises(eg.EgressRefused) as exc:
        eg.post("https://localhost/x", b"{}")
    assert exc.value.reason == "blocked_address"
    assert dialed == []


# ================================================================== E9: https on 443 only

def test_e9_https_on_port_443_only(eg, net, no_hatch):
    net.answers = [[PUBLIC]]
    for url, reason in [
        (f"http://{NAME}/x", "scheme"),
        (f"https://{NAME}:8443/x", "port"),
        (f"https://{NAME}:80/x", "port"),
        ("https://8.8.8.8:8443/x", "port"),
        ("file:///etc/passwd", "scheme"),        # unsupported scheme
        ("gopher://x/y", "scheme"),
        ("ftp://h/f", "scheme"),
    ]:
        with pytest.raises(eg.EgressRefused) as exc:
            eg.post(url, b"{}")
        assert exc.value.reason == reason, url
    assert net.resolved == [] and net.dialed == []
    # Control: 443 written out is the same port.
    with pytest.raises(eg.EgressFailed):
        eg.post(f"https://{NAME}:443/x", b"{}")
    assert net.dialed == [(PUBLIC, 443)]


def test_the_url_shape(eg, no_hatch):
    for url in [f"https://{NAME}/" + "a" * 2048, f"https://{NAME}/x#frag",
                "https://bücher.test/", "https:///x", "https://ho st.test/",
                "https://%31%32%37.0.0.1/", f"https://{NAME}:99999/", 42, ""]:
        with pytest.raises(eg.EgressRefused) as exc:
            eg.check_url(url)
        assert exc.value.reason == "bad_url", url
    t = eg.check_url(f"https://{NAME}/a/b?c=d")
    assert (t.scheme, t.host, t.port, t.path, t.literal) == ("https", NAME, 443, "/a/b?c=d", None)
    assert eg.check_url(f"https://{NAME}").path == "/"


def test_a_url_that_is_not_ascii_is_refused_before_anything_is_dialed(eg, net, tls_hook,
                                                                       no_hatch):
    """http.client writes the request line as ASCII, and only after the connection is made. A
    path or query holding a character past 0x7E would pass the checks, complete TCP and TLS to
    the peer, then raise UnicodeEncodeError: neither EgressRefused nor EgressFailed, so a
    worker catching EgressError would crash on it at every retry. It is refused by check_url,
    so nothing is dialed (S8 review, finding 1). DEL (0x7F), refused before, pins the edge."""
    net.answers = [[PUBLIC]]
    net.route = tls_hook.address
    for url in (f"https://{NAME}/\u00e9", f"https://{NAME}/x?q=\u2603", f"https://{NAME}/\x80",
                f"https://{NAME}/a\u00a0b", f"https://{NAME}/\x7f"):
        with pytest.raises(eg.EgressRefused) as exc:
            eg.post(url, b"{}")
        assert exc.value.reason == "bad_url", ascii(url)
        with pytest.raises(eg.EgressRefused) as exc:
            eg.check_url(url)
        assert exc.value.reason == "bad_url", ascii(url)
    assert net.resolved == [] and net.dialed == [] and tls_hook.sni == []
    # Control: the same characters percent-encoded are ASCII, pass, and are sent as written.
    r = eg.post(f"https://{NAME}/%C3%A9?q=%E2%98%83", b"{}")
    assert r.status == 200
    assert [s.path for s in tls_hook.requests] == ["/%C3%A9?q=%E2%98%83"]


def test_a_caller_cannot_set_host_and_the_body_is_bytes(eg, net, no_hatch):
    net.answers = [[PUBLIC]]
    # Header names are case-insensitive, and http.client sends none of its own Host when the
    # caller's names one in any case (S8 review, finding 8).
    for spelling in ("host", "Host", "HOST", " hOsT "):
        with pytest.raises(ValueError):
            eg.post(f"https://{NAME}/x", b"{}", {spelling: "169.254.169.254"})
    with pytest.raises(TypeError):
        eg.post(f"https://{NAME}/x", "{}")
    assert net.resolved == [] and net.dialed == []


# ================================================================== the hatch

def test_the_hatch_opens_only_loopback(eg, net, servers, monkeypatch, hatch):
    for literal in ("127.0.0.1", "127.9.9.9", "::1"):
        assert eg.is_blocked(literal, allow_loopback=True) is False, literal
        assert eg.is_blocked(literal) is True, f"control: {literal} is blocked without it"
    for literal in ("10.0.0.1", "169.254.169.254", "0.0.0.0", "::", "100.64.0.1", "fd00::1",
                    "::ffff:127.0.0.1", "::7f00:1", "64:ff9b::7f00:1", "2002:7f00:1::",
                    "::ffff:0:7f00:1", "2002:808:808::"):
        assert eg.is_blocked(literal, allow_loopback=True) is True, literal

    # Through post, with the setting on: loopback over http on any port reaches a server,
    # through the real dial.
    monkeypatch.setattr(eg, "dial", lambda address, timeout: socket.create_connection(
        address, timeout=timeout))
    rec = servers(Recorder, ok())
    r = eg.post(f"http://127.0.0.1:{rec.address[1]}/hook", b"{}")
    assert (r.status, r.address, len(rec.requests)) == (200, "127.0.0.1", 1)

    # ...and nothing else opens: private and metadata addresses, an embedded loopback, and
    # http or another port to a public address or to a name that resolves public. One public
    # address in an answer keeps http and other ports shut, first or last, and port 443 does
    # not make http strict (S8 review, finding 6).
    monkeypatch.setattr(eg, "dial", net.dial)
    for url, answer, reason in [
        ("http://10.0.0.5:8080/x", None, "blocked_address"),
        ("https://169.254.169.254/x", None, "blocked_address"),
        ("http://[::ffff:127.0.0.1]:80/x", None, "blocked_address"),
        (f"http://{PUBLIC}/x", None, "scheme"),
        (f"https://{PUBLIC}:8443/x", None, "port"),
        (f"http://{NAME}/x", [PUBLIC], "scheme"),
        (f"https://{NAME}:8443/x", [PUBLIC], "port"),
        (f"http://{NAME}:8080/x", [PUBLIC, "127.0.0.1"], "scheme"),
        (f"https://{NAME}:8443/x", ["127.0.0.1", PUBLIC], "port"),
        (f"http://{PUBLIC}:443/x", None, "scheme"),
        (f"http://{NAME}:443/x", [PUBLIC], "scheme"),
    ]:
        net.answers = [answer or [PUBLIC]]
        with pytest.raises(eg.EgressRefused) as exc:
            eg.post(url, b"{}")
        assert exc.value.reason == reason, url
    assert net.dialed == []


def test_the_hatch_setting_without_debug_opens_nothing(eg, servers, monkeypatch):
    rec = servers(Recorder, ok())
    with override_settings(DEBUG=False, MESHY_EGRESS_ALLOW_LOOPBACK=True):
        with pytest.raises(eg.EgressRefused) as exc:
            eg.post(f"http://127.0.0.1:{rec.address[1]}/x", b"{}")
        assert exc.value.reason == "scheme"
        with pytest.raises(eg.EgressRefused) as exc:
            eg.post("https://127.0.0.1/x", b"{}")
        assert exc.value.reason == "blocked_address"
    assert rec.requests == []


# ================================================================== bounds

def _post_bounded(eg, url, join_s=8.0):
    """`post` in a thread, so a send that the budget fails to stop fails the test instead of
    hanging it. Returns the response or the exception, and the seconds it took."""
    box = {}

    def run():
        t0 = time.monotonic()
        try:
            box["result"] = eg.post(url, b"{}")
        except Exception as exc:          # noqa: BLE001 - recorded, asserted by the caller
            box["result"] = exc
        box["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(join_s)
    assert not t.is_alive(), f"the call was still running {join_s:g} s in"
    return box["result"], box["elapsed"]


def test_the_total_timeout_bounds_a_server_that_drips(eg, servers, monkeypatch, hatch):
    """A per-read timeout alone lets a server that sends one byte per read hold the call
    open (http.client reads the headers a line at a time). The total is enforced."""
    monkeypatch.setattr(eg, "TOTAL_TIMEOUT_S", 1.0)
    drip = servers(TcpRecorder, drip=True)
    result, elapsed = _post_bounded(eg, f"http://127.0.0.1:{drip.address[1]}/x")
    assert isinstance(result, eg.EgressFailed), result
    assert result.reason == "timeout"
    assert elapsed < 3.0, elapsed
    assert drip.connections == 1


def test_the_total_timeout_bounds_the_pinned_https_path_when_the_headers_drip(
        eg, net, servers, pki, trust_test_ca, monkeypatch, no_hatch):
    """The https connection arms the watchdog in its own connect(), apart from the hatch's
    http one, and https is the only path a peer's URL takes (S8 review, finding 2). Budget
    1 s; the server drips a header byte every 0.1 s after the handshake."""
    monkeypatch.setattr(eg, "TOTAL_TIMEOUT_S", 1.0)
    net.answers = [[PUBLIC]]
    headers = servers(TcpRecorder, drip=True, tls=server_tls(pki, NAME))
    net.route = headers.address
    result, elapsed = _post_bounded(eg, f"https://{NAME}/x")
    assert isinstance(result, eg.EgressFailed), result
    assert result.reason == "timeout"
    assert elapsed < 3.0, elapsed
    assert headers.connections == 1 and headers.first_bytes[0].startswith(b"POST /x ")
    assert net.dialed == [(PUBLIC, 443)]


def test_the_total_timeout_bounds_the_pinned_https_path_when_the_handshake_drips(
        eg, net, servers, pki, trust_test_ca, monkeypatch, no_hatch):
    """The same bound in the TLS handshake: a relay passes the client's bytes through and
    drips the server's back one byte every 20 ms (S8 review, finding 2; the S8 entry's limit
    that this was covered by the shutdown and not by a test)."""
    monkeypatch.setattr(eg, "TOTAL_TIMEOUT_S", 1.0)
    net.answers = [[PUBLIC]]
    behind = servers(TcpRecorder, tls=server_tls(pki, NAME))
    relay = servers(DripRelay, behind.address)
    net.route = relay.address
    result, elapsed = _post_bounded(eg, f"https://{NAME}/x")
    assert isinstance(result, eg.EgressFailed), result
    assert result.reason == "timeout"
    assert elapsed < 3.0, elapsed
    assert relay.connections == 1 and 0 < relay.dripped
    assert behind.first_bytes == [], "the handshake finished, so the drip did not hold it"
    assert net.dialed == [(PUBLIC, 443)]


def test_no_more_than_64_kib_of_a_body_is_waited_for(eg, net, servers, pki, trust_test_ca,
                                                     monkeypatch, hatch):
    """A 2xx announces a 10 MiB body, sends 64 KiB and 4 KiB more, then sends nothing and
    holds the connection open. The send returns the first 64 KiB at once: it asked for 64 KiB
    and one byte, never for the rest. A send that read the whole body would wait out the
    budget (S8 review, finding 3). On the pinned https path, and on the hatch's http through
    the production dial."""
    monkeypatch.setattr(eg, "TOTAL_TIMEOUT_S", 3.0)
    net.answers = [[PUBLIC]]
    held = servers(TcpRecorder, tls=server_tls(pki, NAME), stall=65536 + 4096)
    net.route = held.address
    t0 = time.monotonic()
    r = eg.post(f"https://{NAME}/x", b"{}")
    elapsed = time.monotonic() - t0
    assert (r.status, len(r.body), r.truncated, set(r.body)) == (200, 65536, True, {ord("z")})
    assert elapsed < 1.5, elapsed
    assert net.dialed == [(PUBLIC, 443)] and held.connections == 1

    monkeypatch.setattr(eg, "dial", net.production_dial)
    plain = servers(TcpRecorder, stall=65536 + 4096)
    t0 = time.monotonic()
    r = eg.post(f"http://127.0.0.1:{plain.address[1]}/x", b"{}")
    elapsed = time.monotonic() - t0
    assert (r.status, len(r.body), r.truncated) == (200, 65536, True)
    assert elapsed < 1.5, elapsed


def test_a_body_the_watchdog_cuts_off_is_a_timeout_where_the_cut_reads_as_end_of_file(
        eg, servers, monkeypatch, hatch):
    """A server drips the body one byte every 0.1 s, so no per-read timeout ends the send;
    the watchdog does. On this box its shutdown makes the pending read raise (WinError 10053),
    and the exchange fails on that. Where a read after the shutdown returns end of file
    instead ([I] Linux: recv returns 0 after SHUT_RDWR, and http.client's read(amt) returns a
    short body without raising, client.py's read), only the check after the exchange keeps a
    cut-off body from coming back as a 2xx. The dial here hands http.client a socket that
    reads end of file once the watchdog has shut the connection down: that behaviour,
    simulated (S8 review, finding 4)."""
    shut = threading.Event()

    class EofAfterShutdown(socket.socket):
        def shutdown(self, how):
            shut.set()
            super().shutdown(how)

        def recv_into(self, buffer, nbytes=0, flags=0):
            try:
                return super().recv_into(buffer, nbytes, flags)
            except OSError:
                if shut.is_set():
                    return 0
                raise

    def dial(address, timeout):
        plain = socket.create_connection(address, timeout=timeout)
        sock = EofAfterShutdown(plain.family, plain.type, plain.proto, fileno=plain.detach())
        sock.settimeout(timeout)
        return sock

    monkeypatch.setattr(eg, "dial", dial)
    # Control: a whole exchange through this socket answers, and the watchdog never fires.
    whole = servers(Recorder, ok())
    r = eg.post(f"http://127.0.0.1:{whole.address[1]}/x", b"{}")
    assert (r.status, r.body) == (200, b"ok") and not shut.is_set()

    monkeypatch.setattr(eg, "TOTAL_TIMEOUT_S", 1.0)
    cut = servers(TcpRecorder, stall=10, length=1000, body_drip=True)
    result, elapsed = _post_bounded(eg, f"http://127.0.0.1:{cut.address[1]}/x")
    assert shut.is_set(), ("control: the watchdog shut the connection down", result, elapsed)
    assert isinstance(result, eg.EgressFailed), result
    assert result.reason == "timeout"
    assert elapsed < 3.0, elapsed


def test_the_response_is_read_to_64_kib_at_most(eg, servers, hatch):
    big = servers(Recorder, ok(b"x" * (1024 * 1024)))
    r = eg.post(f"http://127.0.0.1:{big.address[1]}/x", b"{}")
    assert (r.status, len(r.body), r.truncated) == (200, 65536, True)
    small = servers(Recorder, ok(b"challenge-echo"))
    r = eg.post(f"http://127.0.0.1:{small.address[1]}/x", b"{}")
    assert (r.body, r.truncated) == (b"challenge-echo", False)
    exact = servers(Recorder, ok(b"y" * 65536))
    r = eg.post(f"http://127.0.0.1:{exact.address[1]}/x", b"{}")
    assert (len(r.body), r.truncated) == (65536, False)


@pytest.mark.parametrize("code", [400, 404, 500, 503])
def test_a_status_outside_2xx_is_a_failure(eg, code, servers, hatch):
    rec = servers(Recorder, lambda path: (code, {}, b"nope"))
    with pytest.raises(eg.EgressFailed) as exc:
        eg.post(f"http://127.0.0.1:{rec.address[1]}/x", b"{}")
    assert (exc.value.reason, exc.value.status) == ("status", code)
