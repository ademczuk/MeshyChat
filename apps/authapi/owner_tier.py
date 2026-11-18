"""An owner's tier and per-capability overrides, from a shell. Divergence 028 (T).

THE GAP. `manage.py bootstrap_owner` creates the operator's owner at the dashboard_users.tier
column default, and sql/013 seeds free `peers_per_owner` = 5. Minting
a node reserves one `peers_per_owner` unit (apps/authapi/onboard.py, through
apps/files/quota.py), so a self-hosted mesh stopped at five nodes per owner, and the only way
past it was SQL: no billing service changes a tier here, and until this module there was no
command for it.

WHAT THIS IS. Three writes, each validated against `tier_capabilities` (the table the caps are
read from, so a name it does not hold is refused rather than written as a row nothing reads):

  * `set_tier`        dashboard_users.tier
  * `set_override`    one owner_tier_overrides row per capability (upsert; -1 is unlimited)
  * `clear_override`  delete that row, so the tier's cap applies again

and `effective_caps`, which reads the result BACK through the dashboard's own cap statement
(`apps.dashboard.queries.CAPS_SNAPSHOT_SQL`, the one spelling of override-then-tier
resolution on that surface) rather than restating what was asked.

WHAT A CAP DOES HERE, measured by grep on 2026-09-23 (positive controls: each enforced
capability's name is found at its call site): `peers_per_owner` is reserved at mint
(apps/authapi/onboard.py) and shown on the dashboard; `file_bytes` is reserved at file
finalize (apps/files); `log_retention_days_max` bounds the dashboard's log retention.
`calls_per_day`, `calls_per_cycle`, `concurrent_calls`, `local_memory_keys` and
`meshy_memory_keys` are stored and settable here but do not limit any code. `calls_per_cycle`
is deliberately not reserved around invoke (see apps/calls/views.py and apps/a2a/views.py).
Setting them changes what the dashboard reports and nothing else.

The entitlements gate (apps/core/entitlements) is not consulted here: this shows the
configured caps, and a revoked owner is still refused at the route that reserves.
"""
from __future__ import annotations

import re
import uuid

from apps.dashboard.queries import CAPS_SNAPSHOT_SQL
from apps.files import quota

#: -1 is the unlimited value (sql/013, apps/files/quota.UNLIMITED). Any other negative
#: value is refused to keep one spelling (quota treats any negative as unlimited).
UNLIMITED = -1

#: owner_tier_overrides.value is BIGINT (sql/013). A larger value used to reach the column and
#: escape as an untyped database error (a traceback, not the command's "refused").
BIGINT_MAX = 2 ** 63 - 1

#: One spelling: an optional minus and ASCII digits. `int()` alone also takes a `+`, `_`
#: separators and any Unicode decimal digit (fullwidth, Arabic-Indic), each a second way to
#: write a cap that the operator reading it back would not have typed.
_VALUE_RE = re.compile(r"-?[0-9]+", re.ASCII)

NOT_ENFORCED = ("calls_per_day", "calls_per_cycle", "concurrent_calls", "local_memory_keys",
                "meshy_memory_keys")


class TierRefused(Exception):
    """A refusal the operator must act on. Nothing was written."""


def known_tiers(cur) -> list[str]:
    cur.execute("SELECT DISTINCT tier FROM tier_capabilities ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def known_capabilities(cur) -> list[str]:
    cur.execute("SELECT DISTINCT capability FROM tier_capabilities ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def check_tier(cur, tier: str) -> str:
    tiers = known_tiers(cur)
    if tier not in tiers:
        raise TierRefused(f"--tier must be one of {tiers} (the tiers tier_capabilities "
                          f"defines caps for), not {tier!r}")
    return tier


def check_capability(cur, capability: str) -> str:
    caps = known_capabilities(cur)
    if capability not in caps:
        raise TierRefused(f"capability {capability!r} is not one tier_capabilities defines; "
                          f"known: {caps}")
    return capability


def parse_override(text: str) -> tuple[str, int]:
    """`capability=value`. The value is an integer in ASCII decimal digits, -1 (unlimited)
    or a cap >= 0 that fits the BIGINT column. Surrounding whitespace is ignored."""
    capability, sep, raw = str(text).partition("=")
    capability = capability.strip()
    if not sep or not capability:
        raise TierRefused(f"--override takes capability=value, not {text!r}")
    if not _VALUE_RE.fullmatch(raw.strip()):
        raise TierRefused(f"--override value for {capability!r} must be an integer in ASCII "
                          f"digits (-1, 0, 5, ...), not {raw!r}")
    value = int(raw.strip())
    if value > BIGINT_MAX:
        raise TierRefused(f"--override value for {capability!r} must be at most {BIGINT_MAX} "
                          f"(the column is BIGINT; -1 is unlimited), not {value}")
    if value < UNLIMITED:
        raise TierRefused(f"--override value for {capability!r} must be -1 (unlimited) or "
                          f">= 0, not {value}")
    return capability, value


def _owner_row(cur, owner_id) -> uuid.UUID:
    try:
        oid = uuid.UUID(str(owner_id))
    except ValueError:
        raise TierRefused(f"--owner must be an owner id (a UUID), not {owner_id!r}") from None
    cur.execute("SELECT id FROM dashboard_users WHERE id = %s FOR UPDATE", (oid,))
    if cur.fetchone() is None:
        raise TierRefused(f"there is no owner {oid}; nothing was changed")
    return oid


def set_tier(conn, owner_id, tier: str) -> None:
    with conn.transaction(), conn.cursor() as cur:
        check_tier(cur, tier)
        oid = _owner_row(cur, owner_id)
        cur.execute("UPDATE dashboard_users SET tier = %s WHERE id = %s", (tier, oid))


def set_override(conn, owner_id, capability: str, value: int, note: str | None = None) -> None:
    with conn.transaction(), conn.cursor() as cur:
        check_capability(cur, capability)
        oid = _owner_row(cur, owner_id)
        cur.execute(
            "INSERT INTO owner_tier_overrides (owner_id, capability, value, note) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (owner_id, capability) DO UPDATE "
            "SET value = EXCLUDED.value, note = EXCLUDED.note",
            (oid, capability, int(value), note))


def clear_override(conn, owner_id, capability: str) -> None:
    with conn.transaction(), conn.cursor() as cur:
        check_capability(cur, capability)
        oid = _owner_row(cur, owner_id)
        cur.execute("DELETE FROM owner_tier_overrides WHERE owner_id = %s AND capability = %s "
                    "RETURNING capability", (oid, capability))
        if cur.fetchone() is None:
            raise TierRefused(f"owner {oid} has no override for {capability!r}; nothing was "
                              "changed")


def effective_caps(conn, owner_id) -> dict:
    """What the database now says, read in one transaction: the tier, and per capability the
    resolved cap, where it came from, and this cycle's usage."""
    oid = uuid.UUID(str(owner_id))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT tier FROM dashboard_users WHERE id = %s", (oid,))
        tier = cur.fetchone()[0]
        cur.execute("SELECT capability FROM owner_tier_overrides WHERE owner_id = %s", (oid,))
        overridden = {r[0] for r in cur.fetchall()}
        # The cycle the usage counter is written under (UTC, or the live subscription's period).
        cur.execute(CAPS_SNAPSHOT_SQL, {"owner": oid, "tier": tier,
                                        "cycle": quota._cycle_start(conn, oid)})
        rows = cur.fetchall()
    caps = {}
    for capability, value, current in rows:
        source = "override" if capability in overridden else ("tier" if value is not None
                                                              else None)
        caps[capability] = {"value": None if value is None else int(value), "source": source,
                            "current": int(current)}
    return {"owner_id": str(oid), "tier": tier, "caps": caps,
            "not_enforced": [c for c in NOT_ENFORCED if c in caps]}
