"""Where a provider lives, as a fact about its base_url: loopback, RFC1918, or neither.

WHY THIS EXISTS. sql/028:35-37 records MUST-8 for the shared memory: "no Gemini key /
no embedding", which is to say no third-party key and no mesh content to an external
API. The modelhost registry is configuration, and configuration can name any URL. So
before any mesh content is sent to a declared provider (apps/meshsem/providers.py is
the only place that does), the provider's host must be proven to be THIS machine or a
machine on the private LAN. docs/DIVERGENCES.md 019.

WHAT COUNTS, and it is deliberately narrow:
  loopback   127.0.0.0/8 and ::1
  RFC1918    10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
Nothing else. Not `ipaddress.is_private`, which also admits 100.64.0.0/10 carrier NAT,
169.254.0.0/16 link-local and fc00::/7, none of which is "this LAN" in the sense MUST-8
means. Not a hostname either, `localhost` included: a name is an answer from a resolver
that can change between this check and the connection, so only an IP literal is a
statement about where the bytes go. An operator writes 127.0.0.1, not localhost.

FAIL CLOSED: anything unparseable, any scheme but http/https, and a URL carrying
credentials are all refused, each with its own reason so the refusal is diagnosable.

This module is pure (no network, no Django, no database) so it can be tested
exhaustively and imported from anywhere without side effects.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

#: The only networks MUST-8 content may reach, besides loopback.
RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


@dataclass(frozen=True)
class Locality:
    #: True only for an IP-literal loopback or RFC1918 host over http(s).
    content_ok: bool
    #: "loopback", "rfc1918", or "refused".
    kind: str
    reason: str


def classify(base_url: str) -> Locality:
    """The verdict for one base_url. Never raises."""
    if not isinstance(base_url, str) or not base_url.strip():
        return Locality(False, "refused", "base_url is empty")
    try:
        parts = urlsplit(base_url.strip())
        host = parts.hostname
        _ = parts.port            # raises ValueError on a malformed port
    except ValueError as exc:
        return Locality(False, "refused", f"base_url does not parse: {exc}")
    if parts.scheme not in ("http", "https"):
        return Locality(False, "refused", f"scheme {parts.scheme!r} is not http or https")
    if parts.username is not None or parts.password is not None:
        # A credential in the URL is a key travelling with the content, which is the
        # other half of MUST-8. Refused whatever the host.
        return Locality(False, "refused", "base_url carries credentials")
    if not host:
        return Locality(False, "refused", "base_url has no host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return Locality(False, "refused",
                        f"host {host!r} is a name, not an IP literal; a name is resolved "
                        "after this check and can point anywhere")
    if ip.is_loopback:
        return Locality(True, "loopback", f"{ip} is loopback")
    if ip.version == 4 and any(ip in net for net in RFC1918):
        return Locality(True, "rfc1918", f"{ip} is RFC1918")
    return Locality(False, "refused", f"{ip} is neither loopback nor RFC1918")


__all__ = ["Locality", "RFC1918", "classify"]
