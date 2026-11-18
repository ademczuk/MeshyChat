"""`file_bytes` quota accounting for finalize - and the seam where the billing app takes
it over.

`POST /v1/files/{id}/finalize` calls `try_reserve`, `refund`, and `bump_usage`, plus cap
and cycle resolution.

WHY IT IS HERE AT ALL. 402 `E_QUOTA_EXCEEDED` is a status code on a route this package
owns. Leaving it out would let an over-quota owner get 200, and the cap would be advisory.
So the reserve is real.

WHY IT IS A SEAM. `apps/billing` is another agent's package and the full tier service is
its surface. `set_quota_service()` replaces the implementation wholesale, so when that
lands this module becomes one import and nothing in `service.py` changes.

`resolve_cap` runs `check_product_access`, the Platform Accounts product-access gate that can
deny with `ProductAccessDenied` / `EntitlementsStale`, also 402. The gate reads the
`accounts_entitlements` cache owned by the auth/billing surface. `check_product_access`
below is the named hook.

MEASURED SCHEMA (information_schema + pg_constraint on meshy_django):
    owner_usage(owner_id uuid, capability text, cycle_start date, counter bigint
                default 0, updated_at timestamptz) PK (owner_id, capability, cycle_start),
                owner_id -> dashboard_users(id) ON DELETE CASCADE
    tier_capabilities(tier text, capability text, value bigint, updated_at)
                seeded file_bytes: free=1073741824, pro=26843545600, enterprise=-1
    owner_tier_overrides(owner_id, capability, value bigint, note, ...)
    owner_subscriptions(owner_id, ..., status, current_period_start, ...)
    dashboard_users.tier CHECK IN ('free','pro','enterprise')
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from apps.core import entitlements

#: A cap below zero means UNLIMITED and never produces QuotaExceeded.
UNLIMITED = -1

CAPABILITY_FILE_BYTES = "file_bytes"


class QuotaExceeded(Exception):
    """Carries the three fields used to format the quota error."""

    def __init__(self, capability: str, actual: int, cap: int):
        super().__init__(f"quota exceeded for {capability}: {actual} would exceed cap {cap}")
        self.capability = capability
        self.actual = actual
        self.cap = cap


class ProductAccessDenied(Exception):
    """The owner's Accounts entitlement denies product access, or its cached verdict has
    gone stale. Raised from `check_product_access`, which `resolve_cap` runs FIRST - so a
    denied owner never reaches a cap value at all."""


class UnknownCapability(Exception):
    """400 E_VALIDATION. Raised
    when neither an override nor a `(tier, capability)` row exists - NOT defaulted to
    unlimited, which is the failure direction that gives away the product."""


@dataclass(frozen=True)
class Reservation:
    """What `finalize` must be able to hand back to `refund` on a failure path."""
    owner_id: str
    capability: str
    amount: int


def _cycle_start(conn, owner_id) -> _dt.date:
    """Pro/Enterprise: the most recent `current_period_start` of an
    active/past_due/trialing subscription. Free, missing, or mid-webhook-race: the first
    day of the current calendar month, UTC."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_period_start FROM owner_subscriptions "
                    " WHERE owner_id = %s AND status IN ('active','past_due','trialing') "
                    "   AND current_period_start IS NOT NULL", (str(owner_id),))
        rec = cur.fetchone()
    if rec is not None and rec[0] is not None:
        return rec[0].date()
    today = _dt.datetime.now(_dt.timezone.utc).date()
    return today.replace(day=1)


def check_product_access(conn, owner_id) -> None:
    """Run INSIDE `resolve_cap`, the chokepoint.

    THIS WAS A DOCUMENTED NO-OP and that is the interesting part: the seam was correctly
    placed and correctly called, and did nothing. A revoked owner could still reserve
    storage, and no test could see it, because a gate that returns None is indistinguishable
    from a gate that passed.

    The same hole existed one layer up on the dashboard, where the check had been hoisted to
    a per-route helper and one route was missed - measured on a revoked owner in the same
    instant: `/api/tier-info` 402, `/api/logs/settings` 200. Two apps, one gate, so the
    implementation is in `apps/core/entitlements` and both call it. A second copy here would
    be the third spelling of one policy.

    `apps.core` rather than importing the dashboard: a storage surface has no business
    importing a read API, and that dependency would have pointed the wrong way.

    The error TYPES are translated at this boundary. `apps/core/entitlements` raises its own
    exceptions so it owes nothing to any app's error module; `apps/files` answers 402
    E_QUOTA_EXCEEDED for a denied product, which is the status this surface already uses for
    "you may not consume more", and `service.reserve_file_bytes` maps it.
    """
    try:
        entitlements.require(conn, owner_id)
    except entitlements.EntitlementsStale as exc:
        raise ProductAccessDenied(str(exc)) from exc
    except entitlements.ProductAccessDenied as exc:
        raise ProductAccessDenied(str(exc)) from exc


def resolve_cap(conn, owner_id, capability: str) -> int:
    """Override first, then the owner's tier default. `UnknownCapability`
    when neither has a row."""
    check_product_access(conn, owner_id)
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM owner_tier_overrides "
                    " WHERE owner_id = %s AND capability = %s", (str(owner_id), capability))
        rec = cur.fetchone()
        if rec is not None:
            return int(rec[0])
        # The tier is a column on dashboard_users.
        cur.execute("SELECT tier FROM dashboard_users WHERE id = %s", (str(owner_id),))
        rec = cur.fetchone()
        if rec is None:
            raise UnknownCapability(f"no dashboard_users row for owner {owner_id}")
        tier = rec[0]
        cur.execute("SELECT value FROM tier_capabilities WHERE tier = %s AND capability = %s",
                    (tier, capability))
        rec = cur.fetchone()
    if rec is None:
        raise UnknownCapability(capability)
    return int(rec[0])


def current_usage(conn, owner_id, capability: str, cycle_start) -> int:
    """A missing row is 0 usage - that is the ONE place an absent
    row legitimately means zero, because the counter's identity is (owner, capability,
    cycle) and the first write in a cycle creates it."""
    with conn.cursor() as cur:
        cur.execute("SELECT counter FROM owner_usage "
                    " WHERE owner_id=%s AND capability=%s AND cycle_start=%s",
                    (str(owner_id), capability, cycle_start))
        rec = cur.fetchone()
    return int(rec[0]) if rec is not None else 0


def _bump(conn, owner_id, capability, cycle_start, delta: int) -> int:
    """Floors at 0 on BOTH branches. The UPDATE branch references
    the raw delta rather than EXCLUDED.counter: EXCLUDED mirrors the already-floored
    insert value, so reusing it would re-floor a legitimate refund against an existing
    positive counter (counter=10, delta=-3 must be 7, not 10)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO owner_usage (owner_id, capability, cycle_start, counter) "
            "VALUES (%s, %s, %s, GREATEST(0::bigint, %s::bigint)) "
            "ON CONFLICT (owner_id, capability, cycle_start) DO UPDATE "
            "   SET counter = GREATEST(0::bigint, owner_usage.counter + %s::bigint) "
            "RETURNING counter",
            (str(owner_id), capability, cycle_start, int(delta), int(delta)))
        return int(cur.fetchone()[0])


def _bump_checked(conn, owner_id, capability, cycle_start, delta: int, cap: int):
    """ATOMIC reserve-then-act, not check-then-act: the cap
    predicate is inside the same statement as the increment, so two concurrent finalizes
    for one owner cannot both pass a pre-check and push usage past the cap. Returns None
    when the statement admitted nothing (over cap)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO owner_usage (owner_id, capability, cycle_start, counter) "
            "SELECT %s, %s, %s, %s::bigint WHERE %s::bigint <= %s::bigint "
            "ON CONFLICT (owner_id, capability, cycle_start) DO UPDATE "
            "   SET counter = owner_usage.counter + EXCLUDED.counter "
            "   WHERE owner_usage.counter + EXCLUDED.counter <= %s::bigint "
            "RETURNING counter",
            (str(owner_id), capability, cycle_start, int(delta), int(delta), int(cap),
             int(cap)))
        rec = cur.fetchone()
    return None if rec is None else int(rec[0])


class TierQuota:
    """The default implementation. Stateless; `conn` is passed per call."""

    def try_reserve(self, conn, owner_id, capability: str, amount: int) -> Reservation:
        """Raises `QuotaExceeded` when the reservation is refused."""
        cap = resolve_cap(conn, owner_id, capability)
        cycle = _cycle_start(conn, owner_id)
        if cap < 0:                                   # unlimited: count, never refuse
            _bump(conn, owner_id, capability, cycle, amount)
            return Reservation(str(owner_id), capability, amount)
        if _bump_checked(conn, owner_id, capability, cycle, amount, cap) is not None:
            return Reservation(str(owner_id), capability, amount)
        actual = current_usage(conn, owner_id, capability, cycle) + amount
        raise QuotaExceeded(capability, actual, cap)

    def refund(self, conn, reservation: Reservation) -> None:
        """Best effort at the call site: a failed refund over-counts,
        never under-counts, so it must not mask the error that triggered it."""
        cycle = _cycle_start(conn, reservation.owner_id)
        _bump(conn, reservation.owner_id, reservation.capability, cycle,
              -reservation.amount)

    def bump(self, conn, owner_id, capability: str, delta: int) -> None:
        """The post-finalize true-up when the verified size differs
        from the reserved one. Never cap-gated: admission was already decided."""
        cycle = _cycle_start(conn, owner_id)
        _bump(conn, owner_id, capability, cycle, delta)


_service = TierQuota()


def set_quota_service(service) -> None:
    """Install a quota service (e.g. apps/billing's tier service). Must provide
    `try_reserve(conn, owner_id, capability, amount)`, `refund(conn, reservation)` and
    `bump(conn, owner_id, capability, delta)`."""
    global _service
    _service = service


def get_quota_service():
    return _service
