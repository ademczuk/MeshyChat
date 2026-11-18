"""The tenancy visibility seam - ONE place that answers "whose data may this request
see".

WHY THIS IS A MODULE AND NOT A `WHERE` CLAUSE COPIED INTO EACH VIEW
-------------------------------------------------------------------
Changing who may see what is a ONE-FUNCTION edit here, not an edit to each
co-author-broadened endpoint. This is the SEAM, not the filter. Eight handlers thread
it. A per-view re-expression is how one of them ends up one clause behind the others,
and a visibility predicate that is right in seven places and wrong in the eighth is a
leak, not a bug.

So: nothing outside this module writes `shared_memory_grant`, `owner_owner_id` or
`meshychat_same_tenant_or_devgroup`. Views call `scope.vis_cte` or `scope.content_vis_cte`
and then reference the two CTEs those define:

    vis        namespaces the caller may read: ones it OWNS, UNION ones with an
               active, non-revoked, unexpired grant to it, intersected with the
               caller's tenant via `meshychat_same_tenant_or_devgroup`.
    vis_peers  peers that co-authored a `vis` namespace AND are in the caller's
               tenant. Closes the other leak direction: a foreign co-author drops out.

A caller with no shared_memory footprint gets EMPTY `vis`/`vis_peers`. That is the
intended fail-closed behaviour, and it is also why every view that reads through these
CTEs must distinguish "empty because you may see nothing" from "empty because the
context never bound" - see `apps/dashboard/repo.py`, which refuses to return a bare
list from a read whose privileged context was not proven active.

TWO CTEs, NOT ONE, AND THE DIFFERENCE IS LOAD-BEARING
-----------------------------------------------------
`vis_cte()` broadens for a dashboard ADMIN: `dashboard_users.is_admin` adds the
namespaces and co-authors active in the last 30 days, so an operator sees the live
mesh. `content_vis_cte()` deliberately does NOT: handlers that project shared_memory
CONTENT (memory bodies, message bodies, notifications) stay inside owned-or-granted
namespaces even for an admin. Using `vis_cte()` on a content route would hand every
admin every tenant's message bodies. The admin arms are written `WHERE <admin-bool>`,
so for a non-admin caller they evaluate to `WHERE false` and contribute zero rows.

PLACEHOLDERS: psycopg named style `%(owner)s`, repeated freely. Positional `%s` would need
the same value passed five to seven times per CTE in the right order, which is exactly the
kind of edit that silently
mis-binds one arm. Literal `%` in any SQL built from these constants must be written
`%%` (see `queries.py`, where the tag filters and `LIKE 'msg/%%'` do).
"""
from __future__ import annotations

import uuid
from apps.core.env import lookup

#: Default OFF. When ON a dev-plane caller sees the whole
#: fleet's shared_memory content/peers; a product caller is unaffected because the
#: inner `plane='dev'` gate yields zero fleet rows for it.
DEV_FLEET_ENV = "MESHYCHAT_DEV_FLEET_MODE"


def dev_fleet_mode() -> bool:
    """Accept only `1`, `true`, and `TRUE`.

    Deliberately NOT a general truthiness parse: "yes", "on" and "TrUe" are false.
    Accepting more values would turn a typo into a fleet-wide
    visibility change.
    """
    return lookup(DEV_FLEET_ENV) in ("1", "true", "TRUE")


# The two CONTENT CTEs define `vis` only. They used to carry a `vis_peers` too, copied from
# the metadata CTEs, but no content query reads it: every query that joins `vis_peers`
# (peers, dispatch nodes, stats, tools, models) is formatted with `vis_cte()`. The dead
# copy hid a tenant gate that no test could reach (review queue M01, the `:84` half).
DEFAULT_CONTENT_VIS_CTE = """
WITH vis AS (
    SELECT namespace
    FROM shared_memory_namespace
    WHERE owner_owner_id = %(owner)s
    UNION
    SELECT DISTINCT g.namespace
    FROM shared_memory_grant g
    JOIN shared_memory_namespace n ON n.namespace = g.namespace
    WHERE g.grantee_owner_id = %(owner)s
      AND g.revoked = FALSE
      AND (g.expires_at IS NULL OR g.expires_at > NOW())
      AND meshychat_same_tenant_or_devgroup(%(owner)s, n.owner_owner_id)
)
"""

DEV_FLEET_CONTENT_VIS_CTE = """
WITH caller_dev AS (
    SELECT COALESCE((SELECT du.plane='dev' FROM dashboard_users du WHERE du.id = %(owner)s), false) AS is_dev
),
vis AS (
    SELECT DISTINCT sm.namespace
    FROM shared_memory sm
    WHERE (SELECT is_dev FROM caller_dev)
    UNION
    SELECT namespace
    FROM shared_memory_namespace
    WHERE owner_owner_id = %(owner)s
    UNION
    SELECT DISTINCT g.namespace
    FROM shared_memory_grant g
    JOIN shared_memory_namespace n ON n.namespace = g.namespace
    WHERE g.grantee_owner_id = %(owner)s
      AND g.revoked = FALSE
      AND (g.expires_at IS NULL OR g.expires_at > NOW())
      AND meshychat_same_tenant_or_devgroup(%(owner)s, n.owner_owner_id)
)
"""

DEFAULT_VIS_CTE = """
WITH caller_admin AS (
    SELECT COALESCE((SELECT du.is_admin FROM dashboard_users du WHERE du.id = %(owner)s), false) AS is_admin
),
vis AS (
    SELECT DISTINCT sm.namespace
    FROM shared_memory sm
    WHERE (SELECT is_admin FROM caller_admin)
      AND sm.created_at > now() - interval '30 days'
    UNION
    SELECT namespace FROM shared_memory_namespace WHERE owner_owner_id = %(owner)s
    UNION
    SELECT DISTINCT g.namespace
    FROM shared_memory_grant g
    JOIN shared_memory_namespace n ON n.namespace = g.namespace
    WHERE g.grantee_owner_id = %(owner)s
      AND g.revoked = FALSE
      AND (g.expires_at IS NULL OR g.expires_at > NOW())
      AND meshychat_same_tenant_or_devgroup(%(owner)s, n.owner_owner_id)
),
vis_peers AS (
    SELECT DISTINCT sm.created_by_peer_id
    FROM shared_memory sm
    WHERE (SELECT is_admin FROM caller_admin)
      AND sm.created_at > now() - interval '30 days'
      AND sm.created_by_peer_id IS NOT NULL
    UNION
    SELECT DISTINCT sm.created_by_peer_id
    FROM shared_memory sm
    JOIN peers cp ON cp.id = sm.created_by_peer_id
    WHERE sm.namespace IN (SELECT namespace FROM vis)
      AND meshychat_same_tenant_or_devgroup(%(owner)s, cp.owner_id)
)
"""

DEV_FLEET_VIS_CTE = """
WITH caller_admin AS (
    SELECT COALESCE((SELECT du.is_admin FROM dashboard_users du WHERE du.id = %(owner)s), false) AS is_admin
),
caller_dev AS (
    SELECT COALESCE((SELECT du.plane='dev' FROM dashboard_users du WHERE du.id = %(owner)s), false) AS is_dev
),
vis AS (
    SELECT DISTINCT sm.namespace
    FROM shared_memory sm
    WHERE (SELECT is_admin FROM caller_admin)
      AND sm.created_at > now() - interval '30 days'
    UNION
    SELECT DISTINCT sm.namespace
    FROM shared_memory sm
    WHERE (SELECT is_dev FROM caller_dev)
    UNION
    SELECT namespace FROM shared_memory_namespace WHERE owner_owner_id = %(owner)s
    UNION
    SELECT DISTINCT g.namespace
    FROM shared_memory_grant g
    JOIN shared_memory_namespace n ON n.namespace = g.namespace
    WHERE g.grantee_owner_id = %(owner)s
      AND g.revoked = FALSE
      AND (g.expires_at IS NULL OR g.expires_at > NOW())
      AND meshychat_same_tenant_or_devgroup(%(owner)s, n.owner_owner_id)
),
vis_peers AS (
    SELECT DISTINCT sm.created_by_peer_id
    FROM shared_memory sm
    WHERE (SELECT is_admin FROM caller_admin)
      AND sm.created_at > now() - interval '30 days'
      AND sm.created_by_peer_id IS NOT NULL
    UNION
    SELECT DISTINCT sm.created_by_peer_id
    FROM shared_memory sm
    WHERE (SELECT is_dev FROM caller_dev)
      AND sm.created_by_peer_id IS NOT NULL
    UNION
    SELECT DISTINCT sm.created_by_peer_id
    FROM shared_memory sm
    JOIN peers cp ON cp.id = sm.created_by_peer_id
    WHERE sm.namespace IN (SELECT namespace FROM vis)
      AND meshychat_same_tenant_or_devgroup(%(owner)s, cp.owner_id)
)
"""


class TenantScope:
    """Opaque scope for ONE dashboard read. Resolve once per request, thread it.

    Opaque on purpose: the only things a view may ask of it are the two CTE texts and
    `params()`. It does not expose the owner id as a formattable value, so no view can
    interpolate it into SQL - the id reaches the database only as a bound parameter.
    """

    __slots__ = ("_owner",)

    def __init__(self, owner_id):
        # Parsed here so a malformed id fails at scope resolution rather than as an
        # opaque 500 from the first query that binds it.
        self._owner = owner_id if isinstance(owner_id, uuid.UUID) else uuid.UUID(str(owner_id))

    @classmethod
    def resolve(cls, auth) -> "TenantScope":
        """Resolve the authenticated owner id."""
        return cls(auth.owner_id)

    @property
    def owner(self) -> uuid.UUID:
        """For the owner-scoped routes that do NOT use the CTEs (agents, docs,
        tier-info, logs): they bind the caller's own id directly and are not
        co-author-broadened."""
        return self._owner

    def params(self, **extra) -> dict:
        """The parameter dict for any SQL built from these CTEs."""
        return {"owner": self._owner, **extra}

    def content_vis_cte(self) -> str:
        """NO admin broadening - see the module docstring."""
        return DEV_FLEET_CONTENT_VIS_CTE if dev_fleet_mode() else DEFAULT_CONTENT_VIS_CTE

    def vis_cte(self) -> str:
        """Admin-broadened metadata visibility."""
        return DEV_FLEET_VIS_CTE if dev_fleet_mode() else DEFAULT_VIS_CTE
