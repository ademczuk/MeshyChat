"""The in-process summary cache: the MODEL'S PART only, single-flight (design 2.7, 5.6).

Divergence 037.

WHAT IS CACHED (critique C3). The validated sentences (with the model's own citation
numbers, which the prompt digest fixes to one covered set), the drop list, the number of
sentences the answer had, and the model's provenance. NEVER `facts` (whose
`owed_by_you` is the caller's own acks), `scope` (the backlog's roles), `window`,
`covered` or `excluded`: those are computed for each caller on each request. A cache that
stored the whole response would hand one reader's owed acks to the next reader of the
same thread.

THE KEY is (scope kind, prompt digest, summary_version, model pin, provider id). The
prompt digest is the sha256 of the exact messages sent to the model, so anything that
would change the question changes the key: a row the caller cannot read, a deleted row, a
superseded version, a gate change or a renamed author label gives a different prompt. The
cache is consulted only AFTER the route gate, the body gate and the per-row rules have
produced the covered set, so a caller can hit only an entry whose every source it can read
now. The prompt carries nothing about the requester, so two callers with one prompt are
asking one question. Accepted (2.7): a hit answers faster than a miss, so a caller can
tell somebody asked about the same prompt in the last 15 minutes; everybody who could
have asked could read every message in it.

BOUNDED (B8): at most MAX_ENTRIES, least recently used first out, each for TTL_S. Per
process and not shared: whether a shared, durable cache would pay is the ledger's
question (design 6.4, Q8).

SINGLE-FLIGHT. Concurrent requests for one key wait for the first one's call instead of
each making it. A waiter takes only the model's part and then runs its OWN re-check
(the route: its own body gate, its own `source_changed`), so a first caller refused
after its call does not refuse the waiter, and a waiter refused after it does not see the
answer. A leader's failure is handed to its waiters (`LeaderFailed`) and never cached;
only a VALIDATED answer is. A failure `retry_on` names (the leader's OWN budget refused
it) is not the waiters' failure: the first waiter to see it makes the call itself.
"""
from __future__ import annotations

import collections
import dataclasses
import threading
import time

MAX_ENTRIES = 256
TTL_S = 900


@dataclasses.dataclass(frozen=True)
class ModelPart:
    kept: tuple            # of validate.Sentence
    dropped: tuple         # of validate.Dropped
    total: int
    provenance: dict


class LeaderFailed(RuntimeError):
    """The request this one waited on failed; `cause` is its exception."""

    def __init__(self, cause: BaseException):
        super().__init__(f"the request this one waited on failed: {cause}")
        self.cause = cause


class WaitTimedOut(RuntimeError):
    """The request this one waited on did not finish within this one's budget."""


def cache_key(*, kind: str, prompt_digest: str, summary_version: str, model_pin: str,
              provider_id: str) -> tuple:
    return (kind, prompt_digest, summary_version, model_pin, provider_id)


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.part: ModelPart | None = None
        self.error: BaseException | None = None
        #: Requests parked on this flight (read by `waiting`).
        self.waiters = 0


class SummaryCache:
    def __init__(self, *, max_entries: int = MAX_ENTRIES, ttl_s: float = TTL_S,
                 clock=time.monotonic):
        self.max_entries, self.ttl_s, self.clock = max_entries, ttl_s, clock
        self._entries: "collections.OrderedDict[tuple, tuple[float, ModelPart]]" = \
            collections.OrderedDict()
        self._flights: dict[tuple, _Flight] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _fresh(self, key) -> ModelPart | None:
        hit = self._entries.get(key)
        if hit is None:
            return None
        at, part = hit
        if self.clock() - at >= self.ttl_s:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return part

    def get(self, key) -> ModelPart | None:
        with self._lock:
            return self._fresh(key)

    def put(self, key, part: ModelPart) -> None:
        with self._lock:
            self._entries[key] = (self.clock(), part)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def waiting(self) -> int:
        """How many requests are parked on another's call right now. An observation for
        tests and operators; nothing decides by it."""
        with self._lock:
            return sum(f.waiters for f in self._flights.values())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def single_flight(self, key, compute, *, wait_s: float, retry_on=None
                      ) -> tuple[ModelPart, str]:
        """(the model's part, how): "hit" from the cache, "miss" when this request made
        the call, "shared" when it waited on another's. `compute` returns a VALIDATED
        ModelPart or raises; its result is cached, its exception is not."""
        deadline = time.monotonic() + wait_s
        while True:
            with self._lock:
                part = self._fresh(key)
                if part is not None:
                    return part, "hit"
                flight = self._flights.get(key)
                leader = flight is None
                if leader:
                    flight = self._flights[key] = _Flight()
            if leader:
                try:
                    part = compute()
                except BaseException as exc:
                    flight.error = exc
                    raise
                else:
                    flight.part = part
                    self.put(key, part)
                    return part, "miss"
                finally:
                    with self._lock:
                        self._flights.pop(key, None)
                    flight.done.set()
            with self._lock:
                flight.waiters += 1
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0 or not flight.done.wait(remaining):
                    raise WaitTimedOut("the request this one waited on did not finish in "
                                       "time")
            finally:
                with self._lock:
                    flight.waiters -= 1
            if flight.part is not None:
                return flight.part, "shared"
            if retry_on is not None and retry_on(flight.error):
                continue                     # the leader's own refusal: try as a leader
            raise LeaderFailed(flight.error)


#: The process's one cache.
CACHE = SummaryCache()


__all__ = ["CACHE", "LeaderFailed", "MAX_ENTRIES", "ModelPart", "SummaryCache", "TTL_S",
           "WaitTimedOut", "cache_key"]
