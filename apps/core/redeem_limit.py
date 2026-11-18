"""The per-client-IP limit on invitation redemption (B16, docs/DIVERGENCES.md 016).

WHY IT EXISTS. `POST /v1/auth/redeem-invitation` takes no credential but the code in its body: a
redeem creates an owner from nothing else. B12's limit (apps/core/peer_limit) is keyed on the
AUTHENTICATED peer, and on this route there is no peer yet, so before this nothing bounded how
fast one host could guess codes.

PAIRING REDEMPTION (`POST /v1/onboard/peers`) TOOK THIS BUDGET UNTIL DIVERGENCE 062, which removed
it on the operator's instruction: no cap or limit on minting nodes. A pairing code is 24 random
bytes (onboard.generate_pairing_code), so no rate of guessing finds one.

ONE BUDGET PER CLIENT. The bucket key is the client address alone. A legitimate client redeems
once, so a small burst and a slow refill cost it nothing.

TAKEN FIRST. The route calls `admit` before it reads its body and before any database work, so
a refused guess never reaches the code lookup, and a 429 carries no information about any code.

THE KEY IS THE SOCKET ADDRESS. The caller resolves it with apps/authapi/ratelimit
`resolve_client_ip`: REMOTE_ADDR, and proxy headers only when the operator asserted
MESHYCHAT_BEHIND_TRUSTED_PROXY. A key read from a header would let a caller pick a fresh bucket per
request. An IPv6 client is keyed on its /48, one site's allocation (RFC 6177): keying the full
address would hand every host 2^64 buckets, and keying the /64 handed every holder of one free
tunnel-broker /48 65,536 of them, enough to fill the table (review MEDIUM-1, below). Clients
inside one /48 share a budget, as clients behind one IPv4 NAT address do. An IPv4-mapped address
is its IPv4 address; an address that is absent or unparseable is ONE shared bucket (kept with
IPv4), never a skipped check.

BOUNDED: LRU + TTL, ONE TABLE PER ADDRESS FAMILY. Buckets are kept in least-recently-used
order. A bucket idle for its TTL (burst / refill: the time to refill completely) is full again,
so dropping it is lossless, and those are dropped first, oldest first. Past `max_tracked` with
no such bucket, a NEW key is refused rather than admitted by evicting a live one: evicting a
partly spent bucket would hand its client a fresh burst, which is exactly what a client cycling
addresses wants. That refusal's wait is the time until the oldest bucket's TTL runs out, the
earliest any slot can free (a lower bound: a flood that touches it again keeps it).

THE STATED RESIDUAL (review MEDIUM-1). An attacker who holds `max_tracked` distinct keys of one
family and touches each within the TTL (at the defaults: 10,000 keys, each every < 300 s, about
34 requests a second) refuses every NEW client of that family for as long as it keeps that up.
Clients already in the table are unaffected, and the other family's table is untouched: IPv6
keys are cheap, so without the split an IPv6 flood would refuse every new IPv4 client too. With
IPv6 keyed on its /48 the IPv6 flood needs 10,000 /48s (an LIR-scale allocation, not a free
tunnel); the IPv4 flood needs 10,000 IPv4 addresses. Evicting instead would turn the same flood
into unlimited guessing; this module chooses refusal, as sig_auth_ip_limiter does.

THE STATED LIMIT: IN-PROCESS, as peer_limit's (several serving processes each hold a bucket).

CONFIGURATION. `MESHY_REDEEM_RATE_BURST` (tokens, integer >= 1, default 10) and
`MESHY_REDEEM_RATE_PER_SEC` (refill, finite > 0, default one token per 30 s), from the Django
setting or the environment; validated as peer_limit's are, so a bad value raises
ImproperlyConfigured at the first request instead of admitting unmetered. No off switch.

ONE CLOCK: `_clock` (time.monotonic). Tests replace it; nothing here reads another.
"""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import OrderedDict

from apps.core import env
from apps.core.peer_limit import retry_after_seconds, stale

#: A person who mistypes a code a few times is never refused; a host guessing codes gets 10,
#: then one every 30 s (2,880 a day, per address, per process). Pairing codes are 192 random
#: bits (apps/authapi/onboard.generate_pairing_code), so this is depth, not the only defence;
#: invitation codes are written by whoever mints them, and this is what bounds a weak one.
DEFAULT_BURST = 10
DEFAULT_REFILL_PER_SEC = 1.0 / 30.0
#: Distinct client keys tracked at once, PER ADDRESS FAMILY (IPv4 addresses; IPv6 /48s).
MAX_TRACKED = 10_000
#: The IPv6 prefix a client is keyed on.
IPV6_PREFIX = 48

E_RATE_LIMITED = "E_RATE_LIMITED"
HTTP_STATUS = 429
UNKNOWN_CLIENT = "unknown"

_clock = time.monotonic


class IpBuckets:
    """`take(key)` takes one token, refilling lazily from elapsed time; it answers 0.0 when a
    token was taken and otherwise the seconds until one will be there."""

    def __init__(self, burst: int, refill_per_sec: float, *, max_tracked: int = MAX_TRACKED):
        self.burst = float(burst)
        self.refill_per_sec = float(refill_per_sec)
        self.max_tracked = max_tracked
        self.ttl = self.burst / self.refill_per_sec
        self._buckets: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._buckets)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._buckets)

    def take(self, key: str, *, now: float | None = None) -> float:
        now = _clock() if now is None else now
        with self._lock:
            if key not in self._buckets:
                while len(self._buckets) >= self.max_tracked:
                    _, (_, last) = next(iter(self._buckets.items()))
                    if now - last < self.ttl:
                        # The least recently used bucket is still live: refuse the new key,
                        # until that bucket's TTL runs out (the earliest a slot can free).
                        return self.ttl - (now - last)
                    self._buckets.popitem(last=False)
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + max(0.0, now - last) * self.refill_per_sec)
            taken = tokens >= 1.0
            self._buckets[key] = [tokens - 1.0 if taken else tokens, now]
            self._buckets.move_to_end(key)
            return 0.0 if taken else (1.0 - tokens) / self.refill_per_sec


def ip_key(ip: str | None) -> str:
    """The bucket key for a client address: IPv4 as is, IPv6 by its /48, IPv4-mapped IPv6 as
    its IPv4 address, anything else the one shared UNKNOWN_CLIENT bucket."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return UNKNOWN_CLIENT
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{addr}/{IPV6_PREFIX}", strict=False))
    return str(addr)


def config() -> tuple[int, float]:
    """(burst, refill per second), from the setting or the environment, validated as
    peer_limit's are (apps/core/env.py `rate_config`)."""
    return env.rate_config("MESHY_REDEEM_RATE_BURST", "MESHY_REDEEM_RATE_PER_SEC",
                           DEFAULT_BURST, DEFAULT_REFILL_PER_SEC)


_state_lock = threading.Lock()
#: One table per address family, so abundance in one cannot fill the other's (MEDIUM-1).
_limiter4: IpBuckets | None = None
_limiter6: IpBuckets | None = None


def _current(key: str) -> IpBuckets:
    """The table for `key`'s family: an IPv6 key is a network, with a colon in it; an IPv4
    key and UNKNOWN_CLIENT have none."""
    global _limiter4, _limiter6
    burst, rate = config()
    with _state_lock:
        if ":" in key:
            if stale(_limiter6, burst, rate):
                _limiter6 = IpBuckets(burst, rate, max_tracked=MAX_TRACKED)
            return _limiter6
        if stale(_limiter4, burst, rate):
            _limiter4 = IpBuckets(burst, rate, max_tracked=MAX_TRACKED)
        return _limiter4


def admit(client_ip: str | None):
    """Take one token from this client's redeem bucket. None when admitted; otherwise the 429
    to return: `E_RATE_LIMITED` and `Retry-After` in whole seconds, rounded UP (a rounded-down
    wait sends the client back while its bucket is still empty)."""
    key = ip_key(client_ip)
    wait = _current(key).take(key)
    if wait <= 0.0:
        return None
    from django.http import JsonResponse
    retry_after = retry_after_seconds(wait)
    return JsonResponse({"error": E_RATE_LIMITED,
                         "message": f"too many code redeem attempts; retry after "
                                    f"{retry_after} seconds"},
                        status=HTTP_STATUS, headers={"Retry-After": str(retry_after)})


def reset() -> None:
    """Empty every bucket (tests; a process restart does the same)."""
    global _limiter4, _limiter6
    with _state_lock:
        _limiter4 = None
        _limiter6 = None


__all__ = ["admit", "config", "reset", "ip_key", "IpBuckets", "DEFAULT_BURST",
           "DEFAULT_REFILL_PER_SEC", "E_RATE_LIMITED"]
