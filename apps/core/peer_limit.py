"""The per-peer rate limit (B12, docs/DIVERGENCES.md 003): one token bucket per peer, taken by
every transport's entry through ONE function, `admit`.

WHY IT EXISTS. Divergence 003 dropped quota with billing and named what is still needed: the
callers are agents, agents loop by construction, and a 1,000,000-call poller turned up while
diagnosing an unmetered transport. Its instruction is a per-peer token bucket, no tiers, no
plans, in ONE service function every transport calls. The per-transport metering had two
defects this shape is built against: the WebSocket transport was unmetered while HTTP reserved
(#206), so here the bucket is keyed on the PEER and never on the transport; and a daily limit
was enforced monthly (#593/#765), so here the only window is the refill rate, named per second.

WHERE IT IS TAKEN (each a call to `admit`):

  * `apps/calls/pipeline.start_call`, first: every call on every transport (HTTP invoke, A2A
    `message:send`, a WS `skill_call`, the dashboard's test call). Every memory skill is a call
    (step 9 executes it), so this is the memory transport too.
  * `apps/wsgw/consumer`, the socket's own frames, in the peer's FRAMES bucket (below).
  * `apps/wsgw/frame_handlers._target_frame`: a target frame that is REFUSED takes a FRAMES
    token, and one that arrives with FRAMES empty is refused before its statement runs
    (`check`). A target frame that lands is free: it answers a call charged to its caller, and
    a real stream's chunks are never cut by the target's limit.
  * `apps/memoryskills/views` (capability mint and revoke) and `apps/board/views` (the board's
    write door, a memory write that does not pass the pipeline), after authentication.
  * the mesh transport, after authentication: the graph routes' wrapper and the wait
    (`apps/mesh/views`), search and context (`apps/meshsem/views`), the summaries
    (`apps/meshsum/views`). The CALLS bucket: one bucket for the work a peer asks of the
    server, whichever transport asks.

`check` reads a bucket without taking from it. It is for a refusal that must come before
work whose charge is taken later (a WS skill_call, which start_call charges, is checked on the
event loop before a pipeline thread opens a connection for it), or taken only on refusal (a
target frame). A race between the check and the take is one extra refused request, not a
bypass: the take still refuses.

TWO BUCKETS PER PEER, SAME SIZE. `CALLS` is the work a peer asks of the server. `FRAMES` is its
socket's control traffic: ping, subscribe, unsubscribe, skill_cancel. They are separate so a
peer that has spent its calls still gets its pong and its presence refresh: one bucket would
turn a busy caller into a peer the hub reports offline, and its calls would queue.

THE KEY IS THE AUTHENTICATED PEER. Every call site passes the identity its transport proved (the
JWT subject, or `act.sub`), never a value from a body. A capability token's bearer is an
authenticated peer too, so a token cannot spend another peer's bucket.

THE STATED LIMIT: IN-PROCESS. The buckets live in this process. README's runbook serves one
daphne process, where that is exact. Several serving processes would each hold a bucket per peer,
so the effective limit is that many times the configured one; still a bound, not the configured
bound. Sharing it across processes needs a table (a migration) or Redis, and is not built.

CONFIGURATION. `MESHY_PEER_RATE_BURST` (tokens, integer >= 1) and `MESHY_PEER_RATE_PER_SEC`
(refill, finite > 0), from the Django setting or the environment (apps/core/env.py). There is
no off switch: a value that cannot be parsed raises ImproperlyConfigured at the first request
rather than admitting unmetered. Read per request, so `override_settings` works and a changed
value takes effect with fresh buckets.

B8: the table is bounded. A bucket idle for burst/refill seconds is full again, so evicting it
is lossless; past the ceiling with none idle, a NEW key is refused rather than admitted
unmetered (the shape of apps/authapi/ratelimit.py, whose idle window is a fixed 600 s).
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid

from apps.core import env

log = logging.getLogger("meshychat.peer_limit")

#: A burst a working node does not meet (paging a namespace, a hivemind turn's reads), and a
#: sustained rate far above the fleet's: production ran 175,859 memory.query in seven days
#: (divergence 014), about 0.3 per second for every peer together. A peer looping flat out is
#: held to 86,400 calls a day per process.
DEFAULT_BURST = 120
DEFAULT_REFILL_PER_SEC = 1.0
#: More peers than the fleet has (1,871 peer rows on the substrate, 2026-08-18).
MAX_TRACKED = 20_000

CALLS = "calls"
FRAMES = "frames"
BUCKETS = (CALLS, FRAMES)

#: The HTTP code (apps/authapi/errors.py already answers sign-in with it) and the WebSocket
#: error frame's code (the hub's codes are bare upper-case, apps/wsgw/envelope.py).
E_RATE_LIMITED = "E_RATE_LIMITED"
WS_RATE_LIMITED = "RATE_LIMITED"
HTTP_STATUS = 429
#: A refused peer is logged at most once per this many seconds, so a loop cannot flood the log.
LOG_EVERY_SECS = 60.0


class PeerRateLimited(Exception):
    """The peer's bucket is empty. `retry_after` is whole seconds until one token is back."""

    code = E_RATE_LIMITED
    http_status = HTTP_STATUS

    def __init__(self, peer: str, transport: str, bucket: str, retry_after: int):
        self.peer = peer
        self.transport = transport
        self.bucket = bucket
        self.retry_after = retry_after
        self.message = (f"this peer's {bucket} are rate limited; retry after "
                        f"{retry_after} seconds")
        super().__init__(self.message)


class PeerBuckets:
    """`take(key)` takes one token, refilling lazily from elapsed time; it answers 0.0 when a
    token was taken and otherwise the seconds until one will be there."""

    def __init__(self, burst: int, refill_per_sec: float, *, max_tracked: int = MAX_TRACKED):
        self.burst = float(burst)
        self.refill_per_sec = float(refill_per_sec)
        self.max_tracked = max_tracked
        self.idle_full_secs = self.burst / self.refill_per_sec
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._buckets)

    def take(self, key: str, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            if key not in self._buckets and len(self._buckets) >= self.max_tracked:
                for k in [k for k, (_, last) in self._buckets.items()
                          if now - last >= self.idle_full_secs]:
                    del self._buckets[k]
                if len(self._buckets) >= self.max_tracked:
                    return 1.0 / self.refill_per_sec
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + max(0.0, now - last) * self.refill_per_sec)
            if tokens < 1.0:
                self._buckets[key] = [tokens, now]
                return (1.0 - tokens) / self.refill_per_sec
            self._buckets[key] = [tokens - 1.0, now]
            return 0.0

    def peek(self, key: str, *, now: float | None = None) -> float:
        """What `take` would answer, taking nothing and tracking nothing: 0.0 when a token is
        there (an untracked key is a full bucket), else the seconds until one will be."""
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._buckets.get(key)
            if entry is None:
                return 0.0
            tokens, last = entry
            tokens = min(self.burst, tokens + max(0.0, now - last) * self.refill_per_sec)
            return 0.0 if tokens >= 1.0 else (1.0 - tokens) / self.refill_per_sec


def config() -> tuple[int, float]:
    """(burst, refill per second), from the setting or the environment, validated
    (apps/core/env.py `rate_config`, which apps/core/redeem_limit.py reads through too)."""
    return env.rate_config("MESHY_PEER_RATE_BURST", "MESHY_PEER_RATE_PER_SEC",
                           DEFAULT_BURST, DEFAULT_REFILL_PER_SEC)


def stale(limiter, burst: int, rate: float) -> bool:
    """Whether `limiter` (None, or a bucket table with `burst` and `refill_per_sec`) was built
    for another (burst, rate) than the configured one, so a changed value takes effect with
    fresh buckets. apps/core/redeem_limit.py asks the same of its two tables."""
    return (limiter is None or limiter.burst != float(burst)
            or limiter.refill_per_sec != rate)


def retry_after_seconds(wait: float) -> int:
    """`Retry-After` for a wait of `wait` seconds: whole seconds, rounded UP (a rounded-down
    wait sends the client back while its bucket is still empty), and never below 1. The 1e-9
    keeps a wait that is whole up to floating-point noise from rounding a second up.
    apps/core/redeem_limit.py's 429 rounds through it too."""
    return max(1, math.ceil(wait - 1e-9))


_state_lock = threading.Lock()
_limiter: PeerBuckets | None = None
_last_logged: dict[str, float] = {}


def _current() -> PeerBuckets:
    global _limiter
    burst, rate = config()
    with _state_lock:
        if stale(_limiter, burst, rate):
            _limiter = PeerBuckets(burst, rate)
        return _limiter


def _peer_key(peer_id) -> str:
    try:
        return str(uuid.UUID(str(peer_id)))
    except ValueError:
        return str(peer_id)


def admit(peer_id, *, transport: str, bucket: str = CALLS) -> None:
    """Take one token from the AUTHENTICATED peer's `bucket`, or raise PeerRateLimited.

    `transport` names where the request came in, for the refusal and the log only: it is not
    part of the key, so a peer cannot leave its limit behind by changing transport."""
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {BUCKETS}")
    peer = _peer_key(peer_id)
    wait = _current().take(f"{bucket}:{peer}")
    if wait > 0.0:
        _refuse(peer, transport, bucket, wait)


def check(peer_id, *, transport: str, bucket: str = CALLS) -> None:
    """Raise PeerRateLimited if the peer's `bucket` is empty; take NOTHING. For a refusal that
    must come before work whose token is taken later, or only on refusal (module docstring)."""
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {BUCKETS}")
    peer = _peer_key(peer_id)
    wait = _current().peek(f"{bucket}:{peer}")
    if wait > 0.0:
        _refuse(peer, transport, bucket, wait)


def charge(peer_id, *, transport: str, bucket: str = CALLS) -> None:
    """Take one token if there is one, and never raise: the charge for work already done (a
    refused target frame). An empty bucket stays empty; the NEXT request meets `check`."""
    try:
        admit(peer_id, transport=transport, bucket=bucket)
    except PeerRateLimited:
        pass


def http_response(exc: PeerRateLimited):
    """The HTTP rendering of a refusal: 429, the server's error envelope, `Retry-After`."""
    from django.http import JsonResponse
    return JsonResponse({"error": exc.code, "message": exc.message}, status=exc.http_status,
                        headers={"Retry-After": str(exc.retry_after)})


def _refuse(peer: str, transport: str, bucket: str, wait: float):
    retry_after = retry_after_seconds(wait)
    now = time.monotonic()
    with _state_lock:
        logged = _last_logged.get(peer)
        if logged is None or now - logged >= LOG_EVERY_SECS:
            _last_logged[peer] = now
            if len(_last_logged) > MAX_TRACKED:
                _last_logged.clear()
            log.warning("peer %s rate limited on %s (%s bucket); retry after %ss",
                        peer, transport, bucket, retry_after)
    raise PeerRateLimited(peer, transport, bucket, retry_after)


def reset() -> None:
    """Empty every bucket (tests; a process restart does the same)."""
    global _limiter
    with _state_lock:
        _limiter = None
        _last_logged.clear()


__all__ = ["admit", "check", "charge", "http_response", "config", "reset", "PeerBuckets",
           "PeerRateLimited", "CALLS", "FRAMES", "retry_after_seconds", "stale",
           "E_RATE_LIMITED", "WS_RATE_LIMITED", "DEFAULT_BURST", "DEFAULT_REFILL_PER_SEC"]
