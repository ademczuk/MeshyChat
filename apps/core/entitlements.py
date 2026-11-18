"""The Accounts entitlements gate, in ONE place, for every surface that resolves a cap.

## Why this is in apps/core rather than in the app that first needed it

The gate lives inside capability resolution because `resolve_cap` is the one chokepoint
every capability decision in the tree passes through. A gate added per-route can be omitted.
Two incidents made that concrete:

The dashboard forgot one: the check had been hoisted to a per-route helper called from
`tier_info` alone, so on a revoked owner, in the same instant, `/api/tier-info` answered 402
while `/api/logs/settings` answered 200. That was fixed by moving the gate back into the
dashboard's own `_resolve_cap`.

**And `apps/files` had the same hole the whole time, one layer down.**
`apps/files/quota.check_product_access` was a documented NO-OP - the seam was correctly
placed, called from inside `quota.resolve_cap`, and simply did nothing. So a revoked owner
could still reserve storage. Two apps, one gate, two independent chokepoints: the honest fix
is one implementation both call, not a second copy in the second app.

`apps/files` importing `apps/dashboard` would have been the wrong direction of dependency -
a storage surface has no business importing a read API - so the check lives here, where both
already depend.

## The order is the policy, and each branch is a decision

    NO ROW      -> PASS. An owner predating the Accounts cutover is not denied.
    stale row   -> DENY. `recheck_at` has passed, so the cached verdict has expired.
    inactive    -> DENY.

Collapsing the stale case into the inactive case would let an EXPIRED cached verdict keep
granting access, which is the failure that looks like nothing is wrong.
"""
from __future__ import annotations

import logging

from psycopg.rows import tuple_row

log = logging.getLogger("meshychat.core.entitlements")

#: One spelling of the question. `stale` is computed in SQL rather than in Python so the
#: comparison uses the DATABASE's clock - a server whose clock has drifted would otherwise
#: decide entitlement freshness against the wrong now().
ENTITLEMENTS_SQL = (
    "SELECT active, recheck_at, recheck_at <= NOW() AS stale "
    "FROM accounts_entitlements WHERE owner_id = %(owner)s"
)


class ProductAccessDenied(Exception):
    """The owner's entitlement row says inactive."""


class EntitlementsStale(Exception):
    """The cached verdict has expired. DENY - do not fall back to the last answer."""


def verdict(conn, owner_id) -> tuple[bool, str]:
    """`(allowed, reason)`. Raises nothing, so a caller can render its own error type.

    Returning a reason rather than a bare bool because "no entitlement row" and "denied"
    are different operational facts, and a caller that logs one as the other sends an
    operator looking in the wrong place.
    """
    # The row is read by position, so the cursor names its own row factory: a caller whose connection
    # defaults to dict rows would otherwise get a KeyError here, which no caller translates.
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(ENTITLEMENTS_SQL, {"owner": str(owner_id)})
        row = cur.fetchone()
        if row is None:
            return True, "no entitlement row (owner predates the Accounts cutover)"
        active, _recheck_at, stale = row[0], row[1], row[2]
    if stale:
        return False, "entitlement verdict is stale"
    if not active:
        return False, "product access denied"
    return True, "active"


def require(conn, owner_id) -> None:
    """Raise unless this owner may resolve a capability. No return value on success.

    Deliberately raises rather than returning False: a gate whose refusal a caller can
    ignore by not checking the return value is a gate that gets ignored.
    """
    allowed, reason = verdict(conn, owner_id)
    if allowed:
        return
    if reason == "entitlement verdict is stale":
        log.warning("accounts entitlements: cached verdict is stale; denying (fail closed) "
                    "owner=%s", owner_id)
        raise EntitlementsStale(reason)
    raise ProductAccessDenied(reason)


__all__ = ["ENTITLEMENTS_SQL", "EntitlementsStale", "ProductAccessDenied", "require",
           "verdict"]
