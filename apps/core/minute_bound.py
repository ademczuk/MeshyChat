"""A per-peer calls-per-minute bound for a route that takes no CALLS token (divergence 091).

Some reads run BESIDE a send and must not take the token that send needs: the sender's recipient
check (divergence 083, apps/mesh/views._recipients_admit, which keeps its own copy of this logic)
and the card list (divergence 091). Such a route still has to bound itself, or a peer could use it
as an unmetered scan. This is that bound, as one small class so the next such route does not grow
a third copy.

The read of a peer's history, the check and the write are ONE critical section: unlocked,
concurrent requests each saw room and all got in (Codex on #145 measured 8 extra admissions from
29). At most `tracked_max` peers are tracked; when full, peers idle for a minute are dropped, and if
it is still full a NEW peer is refused for a minute. The table is never cleared: that would forget
the peers being refused right now.
"""
from __future__ import annotations

import threading
import time

from . import peer_limit


class MinuteBound:
    def __init__(self, what: str, per_minute: int, tracked_max: int = 4096):
        self.what = what
        self.per_minute = per_minute
        self.tracked_max = tracked_max
        self._seen: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def admit(self, peer_id) -> None:
        """Refuse (PeerRateLimited, a 429) a peer's per_minute+1th call within 60 s."""
        now = time.monotonic()
        key = str(peer_id)
        with self._lock:
            if key not in self._seen and len(self._seen) >= self.tracked_max:
                for k in [k for k, ts in self._seen.items() if not ts or now - ts[-1] >= 60.0]:
                    del self._seen[k]
                if len(self._seen) >= self.tracked_max:
                    raise peer_limit.PeerRateLimited(key, "mesh", self.what, 60)   # a NEW peer waits
            seen = [t for t in self._seen.get(key, ()) if now - t < 60.0]
            if len(seen) >= self.per_minute:
                self._seen[key] = seen
                retry = max(1, int(60.0 - (now - seen[0])) + 1)
            else:
                seen.append(now)
                self._seen[key] = seen
                return
        raise peer_limit.PeerRateLimited(key, "mesh", self.what, retry)

    def forget(self) -> None:
        """Tests only: start from an empty table."""
        with self._lock:
            self._seen.clear()


__all__ = ["MinuteBound"]
