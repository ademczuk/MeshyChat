"""The statement timeout of a FULL in-process index load (search.VectorIndex, lexical.LexicalIndex, and through them
graphsearch.ChunkIndex and related.py, which reads search.index_for).

A full load is one SELECT of the newest `ceiling` rows: the first use after a restart, or a refill past the ceiling.
On 7 Nov 2026 the mail index's full load (57,740 vectors, about 177 MB) hit the request's 30 s statement timeout
three times in 15 minutes on a HQD short of memory (QueryCanceled on /v1/mesh/search at 10:31 and (time withheld), and
on /v1/mesh/context at 10:43). The index stays empty after a failed load, so every search started it again, and mail
search and context stayed down from the 09:27 restart onwards. One long statement once, after a restart, is better
than a load that never finishes.

So that ONE statement runs under FULL_LOAD_TIMEOUT_MS as a SET LOCAL, and the transaction's own timeout is put back
straight after it. The incremental and reconcile reads keep the request's timeout. If the load itself fails, the
transaction is aborted and the SET LOCAL ends with it.
"""
from __future__ import annotations

import logging
import time

from apps.core import dbconn, env

log = logging.getLogger("meshychat.meshsem.fullload")

#: Milliseconds the full-load statement may run. MESHY_SEMANTIC_FULL_LOAD_TIMEOUT_MS overrides it. A value that is not a
#: whole number from 1,000 to dbconn.MAX_STATEMENT_TIMEOUT_MS keeps this.
FULL_LOAD_TIMEOUT_MS = 180_000


def timeout_ms() -> int:
    raw = env.setting("MESHY_SEMANTIC_FULL_LOAD_TIMEOUT_MS", "MESHY_SEMANTIC_FULL_LOAD_TIMEOUT_MS", default=None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return FULL_LOAD_TIMEOUT_MS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 0
    if not 1_000 <= n <= dbconn.MAX_STATEMENT_TIMEOUT_MS:
        log.warning("fullload: MESHY_SEMANTIC_FULL_LOAD_TIMEOUT_MS=%r is not a whole number of ms from 1000 to %d; "
                    "it stays %d", raw, dbconn.MAX_STATEMENT_TIMEOUT_MS, FULL_LOAD_TIMEOUT_MS)
        return FULL_LOAD_TIMEOUT_MS
    return n


def fetch_all(cur, sql: str, params) -> list:
    """Run one full-load SELECT under timeout_ms(), then restore the transaction's timeout. `cur` must be a dict_row
    cursor inside a transaction (server_internal is one), so that SET LOCAL applies to the transaction only."""
    cur.execute("SELECT current_setting('statement_timeout') AS t")
    prev = cur.fetchone()["t"]
    budget = timeout_ms()
    cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{budget}ms",))
    t = time.monotonic()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.execute("SELECT set_config('statement_timeout', %s, true)", (prev,))
    # One line per full load, so the server log says whether a cold index loaded and how close it came to the budget.
    log.info("fullload: %d rows in %.1f s (budget %d ms, request timeout %s)", len(rows), time.monotonic() - t,
             budget, prev)
    return rows


__all__ = ["FULL_LOAD_TIMEOUT_MS", "fetch_all", "timeout_ms"]
