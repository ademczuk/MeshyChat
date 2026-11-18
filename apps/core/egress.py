"""SSRF-safe egress: the one way this server sends a request to a URL a PEER controls.

WHY IT EXISTS (docs/DIVERGENCES.md 041 S8; design docs/design/skill-frames.md 4.11 on
fin/design-skill-frames). S9's invocation doorbell and its proof-of-control challenge POST to
a URL a peer registered, and push configs may later. Unguarded, a peer points that URL at
127.0.0.1, at 10.x, or at the cloud metadata address 169.254.169.254, and this server makes
the request from inside the trust boundary. This module guards those sends. Its one caller is
S9's doorbell sender (apps/calls/webhooks.py).

NOT apps/modelhost/locality.py. MUST-8 admits ONLY loopback and RFC1918 for mesh content; an
SSRF guard admits ONLY public addresses for peer-controlled URLs. The policies are opposites,
and one shared helper would be wrong for one of them.

THE RULES, each enforced here:

  * The URL: https, port 443, no userinfo, no fragment, at most 2048 characters, each of
    them printable ASCII, and an ASCII host name or an IP literal. http.client writes the
    request line as ASCII only once the connection is made, so a path or query past 0x7E
    would otherwise fail after a dial, outside both exception kinds.
  * An address is BLOCKED if EITHER of two independent checks blocks it:
      `in_blocklist`  unwraps an IPv4 carried inside a v6 address: mapped, compatible,
                      NAT64, SIIT and 6to4; it then checks that address against the v4 list
      `not_global`    Python's `ipaddress` does not call it `is_global`
    Neither is enough alone. Measured on this interpreter (3.14.3): `is_global` calls
    fec0::1, ::7f00:1, 64:ff9b::7f00:1, ::ffff:0:7f00:1 and 192.0.0.9 global, and the
    blocklist admits 2002:808:808:: (the 6to4 form of a public v4) and
    198.18.0.1. The stricter answer wins.
  * Resolve ONCE: `getaddrinfo` in a thread, capped at 5 s. Refused if ANY returned address
    is blocked, or none is returned. An
    IP literal is not resolved at all.
  * Connect to what was validated: the FIRST validated address is dialed, and TLS
    `server_hostname` and `Host:` are the NAME, so SNI and certificate verification are for
    the name. No later DNS answer reaches a socket, which prevents DNS rebinding without a
    new dependency.
  * No proxy, no redirect. `http.client` reads no proxy variable and follows no Location;
    a 3xx is a failure. A proxy would resolve only its own name and then resolve the target
    past the pin. The tests prove both constraints here
    against recording servers rather than trusting the library.
  * 15 s in total, held by a watchdog that shuts the connection down at
    the deadline. A per-read timeout alone would not: `http.client` reads the headers a line
    at a time, so a server sending one byte per read holds the call open.
  * The response BODY is read to at most 64 KiB: 64 KiB and one byte are asked for, so
    `truncated` can be told, and the rest is never asked for. http.client reads through a
    buffered reader (io.DEFAULT_BUFFER_SIZE, 128 KiB on 3.14.3), so up to one buffer more can
    come off the socket before the close, unread. What comes before the body is capped by
    http.client's own limits (a line of 65536 bytes, 100 headers), and the 100 Continue
    responses it skips in a loop by the 15 s alone: a peer can make one send download for its
    whole budget, not hold more than those limits in memory. The body read is returned,
    because S9's proof of control compares the echoed challenge; a doorbell ignores it.

THE TEST HATCH, `MESHY_EGRESS_ALLOW_LOOPBACK`. It opens LOOPBACK only: 127.0.0.0/8 and ::1 as addresses, not the v6
forms that embed 127.x, which stay blocked. For a destination whose every address is
loopback it also permits http and any port, so a test can reach a local recorder. Everything
else stays blocked with it on. meshychat/settings.py parses it once (`allow_loopback_from_env`)
and refuses to load a production process, one with DEBUG off, that sets it: the deploy check.
`loopback_allowed` is the one reader, and it answers False unless DEBUG is on too.

EXCEPTIONS. `EgressRefused`: nothing was dialed; a policy verdict or a failed resolution.
`EgressFailed`: a connection was attempted and the exchange did not end in a 2xx. Each
carries a stable `reason` for S9 to record.

The pieces a test replaces are module attributes read at call time: `resolve_host`, `dial`
and `tls_context`. They are the production implementations, not test seams with defaults of
their own.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from apps.core.capability_flag import parse_flag

#: The only port a peer-controlled URL may name (the hatch aside).
HTTPS_PORT = 443
MAX_URL_LENGTH = 2048
#: The resolver's cap.
RESOLVE_TIMEOUT_S = 5.0
#: The whole exchange: resolution, connect, TLS, request and response.
TOTAL_TIMEOUT_S = 15.0
MAX_RESPONSE_BYTES = 64 * 1024

ENV_VAR = "MESHY_EGRESS_ALLOW_LOOPBACK"

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


# --------------------------------------------------------------------------- errors

class EgressError(Exception):
    def __init__(self, reason: str, detail: str, *, status: int | None = None):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.status = status


class EgressRefused(EgressError):
    """Nothing was dialed. Reasons: bad_url, scheme, port, userinfo, blocked_address,
    no_addresses, resolve_failed, resolve_timeout, timeout."""


class EgressFailed(EgressError):
    """A connection was attempted and did not end in a 2xx. Reasons: connect, tls,
    timeout, protocol, redirect (a 3xx, never followed; `status` set), status (any other
    non-2xx; `status` set)."""


class EgressConfigError(ImproperlyConfigured):
    """The hatch is set to something that is not a boolean, or set in a production process."""


# --------------------------------------------------------------------------- the blocklist

#: Networks: loopback, RFC1918, link-local (with the metadata
#: address), unspecified and "this network", broadcast and 240/4, documentation, CGNAT and
#: the IETF protocol block. 0/8 covers 0.0.0.0 and 240/4 covers 255.255.255.255, so those
#: two addresses need no network of their own.
_V4_BLOCKED = tuple((ipaddress.IPv4Network(net), why) for net, why in (
    ("0.0.0.0/8", "this network, and 0.0.0.0 unspecified"),
    ("10.0.0.0/8", "RFC1918"),
    ("100.64.0.0/10", "carrier-grade NAT"),
    ("127.0.0.0/8", "loopback"),
    ("169.254.0.0/16", "link-local, and the cloud metadata address 169.254.169.254"),
    ("172.16.0.0/12", "RFC1918"),
    ("192.0.0.0/24", "IETF protocol assignments"),
    ("192.0.2.0/24", "documentation, TEST-NET-1"),
    ("192.168.0.0/16", "RFC1918"),
    ("198.51.100.0/24", "documentation, TEST-NET-2"),
    ("203.0.113.0/24", "documentation, TEST-NET-3"),
    ("240.0.0.0/4", "reserved, and 255.255.255.255 broadcast"),
))

#: IPv6 ranges that cannot be egress destinations.
_V6_BLOCKED = tuple((ipaddress.IPv6Network(net), why) for net, why in (
    ("::1/128", "loopback"),
    ("::/128", "unspecified"),
    ("fc00::/7", "unique local"),
    ("fe80::/10", "link-local"),
    ("fec0::/10", "site-local, deprecated by RFC 3879 and still routed internally"),
    ("2001:db8::/32", "documentation"),
    ("2001::/32", "Teredo: its embedded v4 is obfuscated, so the range is blocked"),
))

#: The v6 forms that carry an IPv4, and where it sits (the
#: shift of its 32 bits). The embedded v4 is checked against the v4 arms, so an internal v4
#: cannot be smuggled inside a v6 literal. SIIT is ::ffff:0:0:0/96 (segments 0-3 zero, 4
#: ffff, 5 zero); the v4-MAPPED range is ::ffff:0:0/96.
_V6_EMBEDDING = tuple((ipaddress.IPv6Network(net), shift, why) for net, shift, why in (
    ("::ffff:0:0/96", 0, "v4-mapped ::ffff:a.b.c.d"),
    ("::/96", 0, "v4-compatible ::a.b.c.d (:: and ::1 excepted)"),
    ("64:ff9b::/96", 0, "NAT64"),
    ("::ffff:0:0:0/96", 0, "SIIT ::ffff:0:a.b.c.d"),
    ("2002::/16", 80, "6to4 2002:AABB:CCDD::"),
))

_V4_LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")
_V6_LOOPBACK = ipaddress.IPv6Address("::1")


def _address(value) -> IPAddress:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return value
    return ipaddress.ip_address(value)


def embedded_ipv4s(v6: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 a transition or compatibility form of `v6` carries."""
    n = int(v6)
    out = []
    for net, shift, _why in _V6_EMBEDDING:
        if v6 not in net:
            continue
        if net.prefixlen == 96 and int(net.network_address) == 0 and n <= 1:
            continue        # :: and ::1 are the v6 unspecified and loopback arms' own
        out.append(ipaddress.IPv4Address((n >> shift) & 0xFFFFFFFF))
    return out


def in_blocklist(value) -> bool:
    """Whether an address is blocked without the loopback hatch."""
    ip = _address(value)
    if ip.version == 4:
        return any(ip in net for net, _why in _V4_BLOCKED)
    if any(in_blocklist(v4) for v4 in embedded_ipv4s(ip)):
        return True
    return any(ip in net for net, _why in _V6_BLOCKED)


def not_global(value) -> bool:
    """The second, independent check: anything `ipaddress` does not call global."""
    return not _address(value).is_global


def is_plain_loopback(value) -> bool:
    """127.0.0.0/8 or ::1, as written. Not ::ffff:127.0.0.1, which `ipaddress` calls
    loopback on this interpreter: the hatch opens the plain forms only."""
    ip = _address(value)
    if ip.version == 4:
        return ip in _V4_LOOPBACK
    return int(ip) == int(_V6_LOOPBACK)


def is_blocked(value, *, allow_loopback: bool = False) -> bool:
    """Blocked if either check blocks it. `allow_loopback` is the hatch's answer, passed by
    `post` from `loopback_allowed()`; it opens plain loopback and nothing else."""
    ip = _address(value)
    if allow_loopback and is_plain_loopback(ip):
        return False
    return in_blocklist(ip) or not_global(ip)


# --------------------------------------------------------------------------- the hatch

def allow_loopback_from_env(env, *, debug: bool) -> bool:
    """`MESHY_EGRESS_ALLOW_LOOPBACK`, parsed; meshychat/settings.py calls it once, at load.

    Unset, blank or a false spelling: off. A true spelling: on, and only when `debug`,
    because a process with DEBUG off is a production process and the hatch lets a peer's URL
    reach loopback there (the deploy check). Anything else refuses to load.
    The spellings are capability_flag's (MESHYCHAT_DPOP_ENFORCE's), so one
    spelling means one thing across the project.
    """
    raw = env.get(ENV_VAR)
    value = parse_flag(raw)
    if value is None:
        raise EgressConfigError(
            f"{ENV_VAR}={raw!r} is not a boolean. It is a test and development hatch, off "
            f"unless set to 1.")
    if not value:
        return False
    if not debug:
        raise EgressConfigError(
            f"{ENV_VAR} is on and DEBUG is off. A process with DEBUG off is a production "
            f"process, and this hatch would let a peer-controlled URL reach loopback from it. "
            f"Unset {ENV_VAR}, or set MESHY_DEBUG=1 on a test or development box "
            f"(docs/DIVERGENCES.md 041 S8).")
    return True


def loopback_allowed() -> bool:
    """The ONE reader of settings.MESHY_EGRESS_ALLOW_LOOPBACK, read per send.

    No default: settings.py always defines the name. Only a real bool: the string "false" is
    truthy. And DEBUG must be on as well, so an override that sets the hatch alone in a
    production process opens nothing.
    """
    from django.conf import settings
    value = settings.MESHY_EGRESS_ALLOW_LOOPBACK
    if not isinstance(value, bool):
        raise EgressConfigError(
            f"settings.{ENV_VAR} must be a bool, got {type(value).__name__} {value!r}")
    return value and settings.DEBUG is True


# --------------------------------------------------------------------------- the URL

#: LDH labels, lower case (urlsplit lowercases the host), an optional trailing dot.
_HOST_NAME = re.compile(
    r"(?=[a-z0-9.-]{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?")


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    #: The request target: the path, "/" when empty, and the query.
    path: str
    #: The host as an address when the URL names one; None for a name to resolve.
    literal: IPAddress | None

    @property
    def strict(self) -> bool:
        """https on 443: the only shape outside the hatch."""
        return self.scheme == "https" and self.port == HTTPS_PORT


def check_url(url, *, allow_loopback: bool = False) -> Target:
    """The checks that need no network, in order; `EgressRefused` on the first that fails.

    With `allow_loopback` (the hatch), http and a port other than 443 pass here and are
    refused later unless every address of the destination is loopback.
    """
    if not isinstance(url, str) or not url:
        raise EgressRefused("bad_url", "the URL is not a non-empty string")
    if len(url) > MAX_URL_LENGTH:
        raise EgressRefused("bad_url", f"the URL is {len(url)} characters; at most {MAX_URL_LENGTH}")
    if any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in url):
        # Printable ASCII only: http.client encodes the request line as ASCII after the dial.
        raise EgressRefused("bad_url", "the URL carries whitespace, a control character or a "
                                       "character that is not ASCII")
    try:
        parts = urlsplit(url)
        explicit_port = parts.port
    except ValueError as exc:
        raise EgressRefused("bad_url", f"the URL does not parse: {exc}") from exc
    scheme = parts.scheme
    if scheme not in ("https", "http"):
        raise EgressRefused("scheme", f"scheme {scheme!r} is not https")
    if scheme == "http" and not allow_loopback:
        raise EgressRefused("scheme", "http is refused: https only")
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    if scheme == "https" and port != HTTPS_PORT and not allow_loopback:
        raise EgressRefused("port", f"port {port} is refused: 443 only")
    host = parts.hostname
    if not host:
        raise EgressRefused("bad_url", "the URL has no host")
    if "#" in url:
        raise EgressRefused("bad_url", "the URL carries a fragment")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is None and not _HOST_NAME.fullmatch(host):
        raise EgressRefused("bad_url", f"host {host!r} is not an ASCII host name or an address")
    if literal is not None and is_blocked(literal, allow_loopback=allow_loopback):
        raise EgressRefused("blocked_address", f"{literal} is a blocked address")
    if "@" in parts.netloc:
        raise EgressRefused("userinfo", "the URL carries userinfo")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return Target(scheme=scheme, host=host, port=port, path=path, literal=literal)


# --------------------------------------------------------------------------- the network

def resolve_host(host: str) -> list[str]:
    """The system resolver's addresses for `host`, in its order, duplicates dropped. Uncapped:
    `post` runs it in a thread under RESOLVE_TIMEOUT_S."""
    infos = socket.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM,
                               proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for *_rest, sockaddr in infos:
        if sockaddr[0] not in out:
            out.append(sockaddr[0])
    return out


def dial(address: tuple[str, int], timeout: float) -> socket.socket:
    """The one socket this module opens: TCP to an (ip, port) that was validated."""
    return socket.create_connection(address, timeout=timeout)


def tls_context() -> ssl.SSLContext:
    """Certificate and hostname verification on, the system trust store, TLS 1.2 at least."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _resolve_capped(host: str, timeout: float) -> list:
    box: dict = {}
    done = threading.Event()

    def work():
        try:
            box["addresses"] = list(resolve_host(host))
        except BaseException as exc:      # noqa: BLE001 - carried to the caller's thread
            box["error"] = exc
        finally:
            done.set()

    # A daemon thread: a resolver that never answers must not hold the process at exit.
    threading.Thread(target=work, name="egress-resolve", daemon=True).start()
    if not done.wait(timeout):
        raise EgressRefused("resolve_timeout", f"{host}: no answer in {timeout:g} s")
    if "error" in box:
        raise EgressRefused("resolve_failed", f"{host}: {box['error']}")
    return box["addresses"]


def _validated(host: str, answer, allow_loopback: bool) -> list[IPAddress]:
    """One blocked address fails the whole name, and an empty answer is refused,
    never a silent allow."""
    addresses: list[IPAddress] = []
    for raw in answer:
        try:
            ip = _address(raw)
        except ValueError:
            raise EgressRefused("blocked_address", f"{host} resolved to {raw!r}, not an address") from None
        if is_blocked(ip, allow_loopback=allow_loopback):
            raise EgressRefused("blocked_address", f"{host} resolves to a blocked address {ip}")
        addresses.append(ip)
    if not addresses:
        raise EgressRefused("no_addresses", f"{host} resolved to no addresses")
    return addresses


class _Watchdog:
    """Shuts the connection down at the deadline, whatever phase it is in. It holds its own
    duplicate of the socket, so the shutdown reaches the connection before, during and after
    the TLS handshake (a shutdown applies to the connection, not to one handle of it)."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.fired = False
        self._sock: socket.socket | None = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def arm(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock.dup()
            self._timer = threading.Timer(max(0.0, self.deadline - time.monotonic()), self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._sock is None:
                return
            self.fired = True
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def disarm(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            if self._sock is not None:
                self._sock.close()
                self._sock = None


class _DialFailed(Exception):
    pass


def _dial_pinned(ip: str, port: int, timeout, watchdog: _Watchdog) -> socket.socket:
    try:
        sock = dial((ip, port), timeout)
    except OSError as exc:
        raise _DialFailed(exc) from exc
    watchdog.arm(sock)
    return sock


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Dials the validated address; SNI, certificate verification and Host are the name."""

    def __init__(self, host: str, port: int, *, pinned: str, timeout: float, watchdog: _Watchdog):
        super().__init__(host, port, timeout=timeout, context=tls_context())
        self._pinned = pinned
        self._watchdog = watchdog

    def set_tunnel(self, *args, **kwargs):
        raise RuntimeError("egress never tunnels through a proxy")

    def connect(self):
        sock = _dial_pinned(self._pinned, self.port, self.timeout, self._watchdog)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain http: reachable only through the hatch, to loopback."""

    def __init__(self, host: str, port: int, *, pinned: str, timeout: float, watchdog: _Watchdog):
        super().__init__(host, port, timeout=timeout)
        self._pinned = pinned
        self._watchdog = watchdog

    def set_tunnel(self, *args, **kwargs):
        raise RuntimeError("egress never tunnels through a proxy")

    def connect(self):
        self.sock = _dial_pinned(self._pinned, self.port, self.timeout, self._watchdog)


# --------------------------------------------------------------------------- the send

@dataclass(frozen=True)
class EgressResponse:
    status: int
    #: At most MAX_RESPONSE_BYTES of the body; the rest was never asked for.
    body: bytes
    #: True when the body was longer than MAX_RESPONSE_BYTES.
    truncated: bool
    #: The validated address the connection was made to.
    address: str


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def post(url: str, body: bytes, headers: Mapping[str, str] | None = None) -> EgressResponse:
    """POST `body` to a peer-controlled `url` under every rule in this module's docstring.

    Returns the 2xx response; raises `EgressRefused` when nothing was dialed and
    `EgressFailed` when the exchange did not end in a 2xx.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("the body is bytes")
    headers = dict(headers or {})
    if any(name.strip().lower() == "host" for name in headers):
        raise ValueError("Host is the URL's name, never a caller's")
    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    hatch = loopback_allowed()
    target = check_url(url, allow_loopback=hatch)
    if target.literal is not None:
        addresses = [target.literal]
    else:
        answer = _resolve_capped(target.host, min(RESOLVE_TIMEOUT_S, max(0.0, _remaining(deadline))))
        addresses = _validated(target.host, answer, hatch)
    if not target.strict and not all(is_plain_loopback(a) for a in addresses):
        # Only the hatch lets a non-strict URL this far, and only for loopback.
        if target.scheme != "https":
            raise EgressRefused("scheme", "http is refused: https only")
        raise EgressRefused("port", f"port {target.port} is refused: 443 only")
    return _exchange(target, str(addresses[0]), bytes(body), headers, deadline)


def _exchange(target: Target, pinned: str, body: bytes, headers: dict,
              deadline: float) -> EgressResponse:
    remaining = _remaining(deadline)
    if remaining <= 0:
        raise EgressRefused("timeout", f"the {TOTAL_TIMEOUT_S:g} s budget ran out before the connection")
    watchdog = _Watchdog(deadline)
    kind = _PinnedHTTPSConnection if target.scheme == "https" else _PinnedHTTPConnection
    conn = kind(target.host, target.port, pinned=pinned, timeout=remaining, watchdog=watchdog)
    data = b""
    try:
        try:
            conn.connect()
        except _DialFailed as exc:
            cause = exc.__cause__
            reason = "timeout" if isinstance(cause, TimeoutError) else "connect"
            raise EgressFailed(reason, f"{pinned}:{target.port}: {cause}") from cause
        except ssl.SSLError as exc:
            raise EgressFailed("timeout" if watchdog.fired else "tls", f"{target.host}: {exc}") from exc
        except OSError as exc:
            reason = "timeout" if watchdog.fired or isinstance(exc, TimeoutError) else "tls"
            raise EgressFailed(reason, f"{target.host}: {exc}") from exc
        try:
            conn.request("POST", target.path, body=body, headers=headers)
            response = conn.getresponse()
            status = response.status
            if 200 <= status < 300:
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            reason = "timeout" if watchdog.fired or isinstance(exc, TimeoutError) else "protocol"
            raise EgressFailed(reason, f"{target.host}: {exc}") from exc
    finally:
        watchdog.disarm()
        conn.close()
    if watchdog.fired:
        raise EgressFailed("timeout", f"{target.host}: the {TOTAL_TIMEOUT_S:g} s budget ran out")
    if 300 <= status < 400:
        raise EgressFailed("redirect", f"{target.host} answered {status}; a redirect is never "
                                       f"followed", status=status)
    if not 200 <= status < 300:
        raise EgressFailed("status", f"{target.host} answered {status}", status=status)
    return EgressResponse(status=status, body=data[:MAX_RESPONSE_BYTES],
                          truncated=len(data) > MAX_RESPONSE_BYTES, address=pinned)


__all__ = [
    "EgressConfigError", "EgressError", "EgressFailed", "EgressRefused", "EgressResponse",
    "Target", "allow_loopback_from_env", "check_url", "dial", "embedded_ipv4s", "in_blocklist",
    "is_blocked", "is_plain_loopback", "loopback_allowed", "not_global", "post",
    "resolve_host", "tls_context",
]
