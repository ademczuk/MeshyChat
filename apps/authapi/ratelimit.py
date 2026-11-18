"""Token-bucket limiter and client-IP resolution.

The two key classes matter as much as the numbers: one bucket
per source IP (loose — every client behind one NAT or the Cloudflare proxy shares it)
and one per REQUESTED peer_id (strict). Both are taken BEFORE any database work, and
both are taken uniformly for peers that exist and peers that do not — so a 429 carries
no existence information and the limiter adds no oracle of its own.
"""
from __future__ import annotations

import ipaddress
import threading
import time

#: Per-peer signature-authentication bucket.
SIG_AUTH_BURST = 10
SIG_AUTH_REFILL_PER_SEC = 0.2
#: Per-source-IP signature-authentication bucket.
SIG_AUTH_IP_BURST = 30
SIG_AUTH_IP_REFILL_PER_SEC = 1.0
#: Bucket cache bound and idle eviction interval.
MAX_TRACKED_KEYS = 10_000
IDLE_EVICT_SECS = 600

CF_CONNECTING_IP = "HTTP_CF_CONNECTING_IP"
X_FORWARDED_FOR = "HTTP_X_FORWARDED_FOR"
X_REAL_IP = "HTTP_X_REAL_IP"


class TokenBucketLimiter:
    """`allow(key)` is the whole API: take one token, refilling lazily from elapsed
    time."""

    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = float(capacity)
        self.refill_per_sec = refill_per_sec
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._buckets) >= MAX_TRACKED_KEYS and key not in self._buckets:
                # Evict idle buckets first; a bucket idle this long is at full
                # capacity anyway, so dropping it is lossless.
                for k in [k for k, (_, last) in self._buckets.items()
                          if now - last > IDLE_EVICT_SECS]:
                    del self._buckets[k]
                if len(self._buckets) >= MAX_TRACKED_KEYS:
                    return False
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_sec)
            if tokens < 1.0:
                self._buckets[key] = [tokens, now]
                return False
            self._buckets[key] = [tokens - 1.0, now]
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


sig_auth_peer_limiter = TokenBucketLimiter(SIG_AUTH_BURST, SIG_AUTH_REFILL_PER_SEC)
sig_auth_ip_limiter = TokenBucketLimiter(SIG_AUTH_IP_BURST, SIG_AUTH_IP_REFILL_PER_SEC)


def reset_all() -> None:
    sig_auth_peer_limiter.reset()
    sig_auth_ip_limiter.reset()


def _parse_ip(value: str | None):
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def client_ip_from_headers(meta) -> str | None:
    """CF-Connecting-IP, then the rightmost parseable entry of
    X-Forwarded-For (anything further left is client-supplied and forgeable), then
    X-Real-IP."""
    ip = _parse_ip(meta.get(CF_CONNECTING_IP))
    if ip is not None:
        return str(ip)
    forwarded = meta.get(X_FORWARDED_FOR)
    if forwarded:
        for hop in reversed([h.strip() for h in forwarded.split(",")]):
            ip = _parse_ip(hop)
            if ip is not None:
                return str(ip)
    ip = _parse_ip(meta.get(X_REAL_IP))
    return str(ip) if ip is not None else None


def resolve_client_ip(request, *, behind_trusted_proxy: bool) -> str | None:
    """Proxy headers are consulted only when the operator has
    asserted this deployment is behind a trusted proxy; the socket address is the
    fallback in every other case. Without that gate a caller forges X-Forwarded-For,
    picks a fresh limiter key per request, and the IP bucket stops existing."""
    if behind_trusted_proxy:
        from_headers = client_ip_from_headers(request.META)
        if from_headers is not None:
            return from_headers
    return _parse_ip(request.META.get("REMOTE_ADDR")) and request.META["REMOTE_ADDR"].strip()


def sig_auth_rate_check(route: str, ip: str | None, peer_id: str) -> bool:
    """Return False when the request must be refused with 429.

    The IP bucket is skipped when the address is unknown, and the
    peer bucket is ALWAYS taken — the peer_id comes from the request body, so it is
    available whether or not the peer exists.
    """
    if ip is not None and not sig_auth_ip_limiter.allow(f"{route}:ip:{ip}"):
        return False
    return sig_auth_peer_limiter.allow(f"{route}:peer:{peer_id}")


def redeem_throttle(request):
    """B16: the per-client-IP budget of invitation redemption (apps/core/redeem_limit;
    pairing redemption left it in divergence 062). Called FIRST in the view, before the body
    is read and before any database work. None when admitted, else the 429 to return. The address is the
    socket's unless the operator asserted a trusted proxy (`resolve_client_ip`)."""
    from apps.authapi.config import get_config
    from apps.core import redeem_limit
    ip = resolve_client_ip(request, behind_trusted_proxy=get_config().behind_trusted_proxy)
    return redeem_limit.admit(ip)
