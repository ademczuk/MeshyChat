"""The one chokepoint every summary passes before any work reaches HQB (design 5.3, E3).

Divergence 037. TWO STAGES, each one short transaction under a transaction-scoped
advisory lock, counted in Postgres (`mesh_summary_requests`, sql_meshychat/017), because
a per-process limiter multiplies by the number of processes:

  1. REQUEST admission, before the rows are read and before anything reaches HQB, the
     tokenizer included: the per-peer request count. It inserts the request's ledger row
     as `admitted`. A request it refuses writes NO row.
  2. MODEL admission, after a cache miss: the fleet in-flight cap, then the model-call
     budgets per peer (hourly, and five-minute), per owner (except the bootstrap owner)
     and fleet-wide. It marks the row `calling` with `lease_until = now() + LEASE_S`.

WHY THE BOOTSTRAP OWNER IS EXCEPTED from the per-owner budget (critique C12, measured on
the test server 2026-09-24: 11 peers under 2 owners, one holding 10 and the author of all
163 messages). The owner limit exists against an owner minting nodes to multiply a
per-peer budget; the operator's own fleet is not that case, and the fleet limit still
bounds it. The bootstrap owner is the owner of MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID's peer.

THE IN-FLIGHT CAP COUNTS HQB'S WORK. It counts `calling` rows whose lease has not passed.
A call the client abandons at its budget is not abandoned by HQB, which keeps generating
to max_tokens, so `settle(..., keep_lease=True)` records the outcome and leaves the row
`calling` until the lease passes (critique C15). A crashed process's row stops counting
when its lease passes.

THE LOCK is `pg_advisory_xact_lock(k1, k2)`, the TWO-integer form, whose key space does
not overlap the single-bigint keys this code base already takes (apps/calls/pipeline.py
`hashtext(call_id)`, apps/authapi/bootstrap.py) (critique C21). It lasts its transaction
only, never a model call.

No queue: a queue in front of a busy GPU turns a burst into requests that time out after
waiting (docs/MODEL-CAPABILITIES.md, rule 3).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import uuid

from apps.core import dbconn

log = logging.getLogger("meshychat.meshsum.admission")

#: Design 5.3, proposed values (Q6: re-set from the ledger after two weeks of use).
LIMITS = {
    "inflight": 2,                    # at once; leaves 4 of HQB's 6 slots
    "peer_calls_per_hour": 12,
    "peer_calls_per_5min": 3,
    "owner_calls_per_hour": 30,       # every owner except the bootstrap owner
    "fleet_calls_per_hour": 120,
    "peer_requests_per_hour": 60,     # cache hits and refusals after admission included
}
#: How long a model call counts against the in-flight cap after it is admitted.
LEASE_S = 60
#: The advisory lock, two-integer form: 'msum' and the one key this module takes.
LOCK_K1 = 0x6D73756D
LOCK_K2 = 1
#: Days the ledger keeps a row (sql_meshychat/017, E7).
RETENTION_DAYS = 30
RETENTION_BATCH = 5000

OUTCOMES = ("generated", "cache_hit", "rejected", "unavailable", "busy", "rate_limited",
            "model_changed", "withheld", "no_text", "nothing_new", "embed_forbidden",
            "not_projected", "not_found", "error")

#: Ledger write failures in this process (never reset; monitoring reads deltas).
WRITE_FAILURES = 0


class Refused(Exception):
    """An admission refusal, shaped for the wire: 429 E_RATE_LIMITED or 503
    E_SUMMARY_BUSY, a Retry-After in seconds, and the window its limit is counted over
    (E4: the name is the period)."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int,
                 window: str, outcome: str):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message
        self.retry_after, self.window, self.outcome = max(1, int(retry_after)), window, outcome

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, "window": self.window,
                "retry_after": self.retry_after}


@dataclasses.dataclass(frozen=True)
class Admitted:
    request_id: uuid.UUID
    owner_id: uuid.UUID


def _lock(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_K1, LOCK_K2))


def _bootstrap_authority_peer_id() -> str:
    from apps.memoryskills import handlers
    return (handlers.BOOTSTRAP_AUTHORITY_PEER_ID or "").strip()


def _retry(cur, sql: str, params: tuple, period_s: int) -> int:
    """Seconds until the oldest counted row leaves its window."""
    cur.execute(sql, params)
    (secs,) = cur.fetchone()
    return int(period_s - (secs or 0)) + 1


def admit_request(conn, *, peer_id, scope_kind: str, summary_version: str,
                  limits: dict = LIMITS) -> Admitted:
    """Stage 1. Raises Refused (429) past the peer's request budget, writing nothing."""
    with dbconn.request_server_internal(conn) as c:
        with c.cursor() as cur:
            _lock(cur)
            cur.execute("SELECT owner_id FROM peers WHERE id = %s", (str(peer_id),))
            row = cur.fetchone()
            if row is None:
                raise Refused(429, "E_RATE_LIMITED", "unknown peer", retry_after=3600,
                              window="per hour", outcome="rate_limited")
            owner_id = row[0]
            cur.execute("SELECT count(*) FROM mesh_summary_requests WHERE peer_id = %s "
                        "AND requested_at > now() - interval '1 hour'", (str(peer_id),))
            (n,) = cur.fetchone()
            if n >= limits["peer_requests_per_hour"]:
                retry = _retry(cur, "SELECT extract(epoch FROM now() - min(requested_at)) "
                                    "FROM mesh_summary_requests WHERE peer_id = %s AND "
                                    "requested_at > now() - interval '1 hour'",
                               (str(peer_id),), 3600)
                raise Refused(429, "E_RATE_LIMITED",
                              f"this peer's summary requests are limited to "
                              f"{limits['peer_requests_per_hour']} per hour",
                              retry_after=retry, window="per hour", outcome="rate_limited")
            cur.execute("INSERT INTO mesh_summary_requests (peer_id, owner_id, scope_kind, "
                        "summary_version) VALUES (%s, %s, %s, %s) RETURNING id",
                        (str(peer_id), owner_id, scope_kind, summary_version))
            (rid,) = cur.fetchone()
    return Admitted(request_id=rid, owner_id=owner_id)


def admit_model_call(conn, request_id, *, peer_id, owner_id, limits: dict = LIMITS) -> None:
    """Stage 2. Marks the row `calling` under a lease, or raises Refused: 503
    E_SUMMARY_BUSY when the fleet is full (in flight, or its hourly budget), 429
    E_RATE_LIMITED for the peer's or its owner's budget. A refusal settles the row."""
    refusal: Refused | None = None
    with dbconn.request_server_internal(conn) as c:
        with c.cursor() as cur:
            _lock(cur)
            cur.execute("SELECT count(*), min(lease_until) FROM mesh_summary_requests "
                        "WHERE state = 'calling' AND lease_until > now()")
            inflight, first_free = cur.fetchone()
            if inflight >= limits["inflight"]:
                cur.execute("SELECT extract(epoch FROM %s::timestamptz - now())",
                            (first_free,))
                (secs,) = cur.fetchone()
                refusal = Refused(503, "E_SUMMARY_BUSY",
                                  f"{inflight} summaries are being generated, the most "
                                  f"allowed at once; no queue", retry_after=int(secs or 0) + 1,
                                  window="at once", outcome="busy")
            if refusal is None:
                refusal = _budgets(cur, peer_id, owner_id, limits)
            if refusal is None:
                cur.execute("UPDATE mesh_summary_requests SET state = 'calling', "
                            "called_at = now(), lease_until = now() + make_interval("
                            "secs => %s) WHERE id = %s", (LEASE_S, str(request_id)))
                return
            cur.execute("UPDATE mesh_summary_requests SET state = 'done', outcome = %s "
                        "WHERE id = %s", (refusal.outcome, str(request_id)))
    raise refusal


def _budgets(cur, peer_id, owner_id, limits: dict) -> Refused | None:
    # Every count is a range on an index (B5): requested_at bounds the per-peer and
    # per-owner scans (a call is admitted within seconds of its request), called_at the
    # fleet's.
    cur.execute(
        "SELECT count(*) FILTER (WHERE called_at > now() - interval '5 minutes'), "
        "       count(*) FILTER (WHERE called_at > now() - interval '1 hour'), "
        "       extract(epoch FROM now() - min(called_at) FILTER (WHERE called_at > now() - "
        "         interval '5 minutes')), "
        "       extract(epoch FROM now() - min(called_at)) "
        "FROM mesh_summary_requests WHERE peer_id = %s "
        "  AND requested_at > now() - interval '61 minutes' "
        "  AND called_at > now() - interval '1 hour'", (str(peer_id),))
    five, hour, age5, age60 = cur.fetchone()
    if five >= limits["peer_calls_per_5min"]:
        return Refused(429, "E_RATE_LIMITED",
                       f"this peer's summaries are limited to "
                       f"{limits['peer_calls_per_5min']} per 5 minutes",
                       retry_after=300 - int(age5 or 0) + 1, window="per 5 minutes",
                       outcome="rate_limited")
    if hour >= limits["peer_calls_per_hour"]:
        return Refused(429, "E_RATE_LIMITED",
                       f"this peer's summaries are limited to "
                       f"{limits['peer_calls_per_hour']} per hour",
                       retry_after=3600 - int(age60 or 0) + 1, window="per hour",
                       outcome="rate_limited")
    if not _is_bootstrap_owner(cur, owner_id):
        cur.execute("SELECT count(*), extract(epoch FROM now() - min(called_at)) "
                    "FROM mesh_summary_requests WHERE owner_id = %s "
                    "  AND requested_at > now() - interval '61 minutes' "
                    "  AND called_at > now() - interval '1 hour'", (str(owner_id),))
        n, age = cur.fetchone()
        if n >= limits["owner_calls_per_hour"]:
            return Refused(429, "E_RATE_LIMITED",
                           f"this peer's owner's summaries are limited to "
                           f"{limits['owner_calls_per_hour']} per hour across its peers",
                           retry_after=3600 - int(age or 0) + 1, window="per hour",
                           outcome="rate_limited")
    cur.execute("SELECT count(*), extract(epoch FROM now() - min(called_at)) "
                "FROM mesh_summary_requests WHERE called_at IS NOT NULL "
                "  AND called_at > now() - interval '1 hour'")
    n, age = cur.fetchone()
    if n >= limits["fleet_calls_per_hour"]:
        return Refused(503, "E_SUMMARY_BUSY",
                       f"the fleet's summaries are limited to "
                       f"{limits['fleet_calls_per_hour']} per hour",
                       retry_after=3600 - int(age or 0) + 1, window="per hour",
                       outcome="busy")
    return None


def _is_bootstrap_owner(cur, owner_id) -> bool:
    authority = _bootstrap_authority_peer_id()
    if not authority:
        return False
    try:
        uuid.UUID(authority)
    except ValueError:
        return False
    cur.execute("SELECT owner_id FROM peers WHERE id = %s", (authority,))
    row = cur.fetchone()
    return row is not None and str(row[0]) == str(owner_id)


_SETTLE_FIELDS = ("covered_digest", "covered_count", "sentences_kept", "sentences_dropped",
                  "model_pin", "provider_id", "latency_ms", "prompt_tokens",
                  "completion_tokens", "finish_reason")


def settle(conn, request_id, *, outcome: str, keep_lease: bool = False,
           excluded: dict | None = None, drop_reasons: dict | None = None,
           **fields) -> bool:
    """Record how a request ended. `state` becomes `done`, except with `keep_lease` on a
    row that is `calling` (a call abandoned while HQB still generates). Never raises: a
    ledger write that fails is logged and counted, and the answer is still served (the
    apps/mesh/usage.py contract). Returns whether the write landed."""
    global WRITE_FAILURES
    unknown = set(fields) - set(_SETTLE_FIELDS)
    if unknown:
        raise TypeError(f"settle() got unknown fields {sorted(unknown)}")
    sets = ["outcome = %s"]
    params: list = [outcome]
    sets.append("state = CASE WHEN %s AND state = 'calling' THEN 'calling' ELSE 'done' END")
    params.append(bool(keep_lease))
    for k in _SETTLE_FIELDS:
        if k in fields and fields[k] is not None:
            sets.append(f"{k} = %s")
            params.append(fields[k])
    for k, v in (excluded or {}).items():
        if k in ("embed_forbidden", "no_text", "cut_to_fit", "source_changed"):
            sets.append(f"excluded_{k} = %s")
            params.append(int(v))
    if drop_reasons is not None:
        sets.append("drop_reasons = %s::jsonb")
        params.append(json.dumps(drop_reasons, sort_keys=True))
    params.append(str(request_id))
    try:
        with dbconn.request_server_internal(conn) as c:
            with c.cursor() as cur:
                cur.execute(f"UPDATE mesh_summary_requests SET {', '.join(sets)} "
                            "WHERE id = %s", params)
        return True
    except Exception as exc:     # an instrument must not fail the request it records
        WRITE_FAILURES += 1
        log.error("summary ledger settle FAILED (answer still served) request=%s "
                  "outcome=%s failures_total=%d: %s", request_id, outcome,
                  WRITE_FAILURES, exc, exc_info=True)
        return False


def prune(conn, *, keep_days: int = RETENTION_DAYS, limit: int = RETENTION_BATCH) -> int:
    """Delete up to `limit` ledger rows older than `keep_days`: a range on `requested_at`
    (A1: never keyed on a live object's identity). THE ONLY DELETE on the table."""
    keep_days = max(1, int(keep_days))
    with dbconn.request_server_internal(conn) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM mesh_summary_requests WHERE id IN ("
                        "  SELECT id FROM mesh_summary_requests "
                        "  WHERE requested_at < now() - make_interval(days => %s) "
                        "  ORDER BY requested_at LIMIT %s)", (keep_days, int(limit)))
            return cur.rowcount


def run_retention_job(conn, job) -> int:
    """The registered job body (apps/jobs/registry.py): rows deleted, which the runner
    compares with `job.max_batch` to decide whether more work is pending."""
    return prune(conn, limit=job.max_batch)


__all__ = ["Admitted", "LEASE_S", "LIMITS", "OUTCOMES", "RETENTION_DAYS", "Refused",
           "admit_model_call", "admit_request", "prune", "run_retention_job", "settle"]
