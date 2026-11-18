"""Make a fresh database able to route to the memory authority. docs/DIVERGENCES.md 017.

Step 9 (apps/calls/route.py) executes a call in-process when its target is
`settings.MESHY_MEMORY_PEER_ID`. Steps 3 and 5 run first, and both read rows that a fresh
database does not have:

  * step 3 resolves `skills WHERE peer_id = <target> AND name = <skill> AND is_current`,
    so without skill rows every memory call is 404;
  * step 5 resolves the target's OWNER through `peers WHERE id = <target> AND
    status = 'active'`, so without the peer row every memory call is 403.

The memory daemon normally signs in as its own peer and POSTs its manifest to
`/v1/peers/me/skills/sync` on every connect. With no daemon, this module applies that
manifest once for an operator: `manage.py bootstrap_memory_peer`.

WHAT IT REGISTERS is the daemon's manifest, not this server's dispatch table:
the manifest lists six skills (this build adds a seventh, `memory.redact`, see
MANIFEST_SKILLS), and `memory.prune`,
which apps/memoryskills also implements, is NOT among them. Registering it here would
expose over the wire a capability absent from the manifest. Mode, share mode, version
and runtime are the manifest's common block.

WHAT IT REFUSES rather than repairs: a peer that exists under a different owner than the
one asked for, and a peer that exists but is not `active`. Either is an operator decision
(who owns the memory authority; whether a suspended authority comes back), and a bootstrap
that silently re-owned or re-activated it would make that decision for them.

THE DEFAULT OWNER FAILS CLOSED. With no `--owner-id`, a dedicated dev-plane owner is
created in its OWN new trust group. Nobody else is in that group, so step 5's
same-tenant-or-dev-group allow matches no caller, and every caller needs an explicit skill
grant (or the operator names the fleet's trust group with `--trust-group`). The namespace
grants each handler checks (apps/memoryskills/permissions.py) are a second, independent
gate that this module does not touch.

Runs as the application role, not the owner: `peers`, `dashboard_users` and `skills` carry
no row-level security and the application role `meshychat_app` holds INSERT on all three (measured, as
`meshy_app` before P2, off information_schema.role_table_grants). One transaction, so a failure leaves nothing half
made.
"""
from __future__ import annotations

import dataclasses
import json
import uuid

#: Manifest skills, in their declared order. NOT apps.memoryskills.handlers.DISPATCH.
#: PLUS TWO OF THIS BUILD'S OWN, the last two: `memory.redact` (divergence 053) and `memory.edit` (divergence 059).
#: They are exposed because a seat must be able to withdraw or correct its own row; `memory.prune`
#: stays unexposed, as above. A database bootstrapped before 053 or 059 gets the row by running
#: `manage.py bootstrap_memory_peer` again, which creates only what is missing.
MANIFEST_SKILLS = ("memory.write", "memory.read", "memory.query", "memory.list",
                   "memory.grant", "namespace.claim", "memory.redact", "memory.edit")

#: Manifest version and common runtime settings.
SKILL_VERSION = "0.2.0"
SKILL_MODE = "sync"
SKILL_SHARE_MODE = "grants_only"
SKILL_MAX_RUNTIME_MS = 30_000

#: The compatibility roster label node clients resolve the memory daemon by
#: (mem_client.DAEMON_ROSTER_LABEL).
PEER_NAME = "meshy-mem-daemon"


class BootstrapRefused(RuntimeError):
    """The database holds a memory-authority peer this command must not alter."""


@dataclasses.dataclass(frozen=True)
class BootstrapReport:
    peer_id: uuid.UUID
    owner_id: uuid.UUID
    owner_created: bool
    peer_created: bool
    skills_created: tuple[str, ...]
    skills_present: tuple[str, ...]
    #: The owner's plane and trust group, READ BACK from the row. Printed because
    #: `bootstrap_owner --plane dev --trust-group` needs this group when the memory
    #: authority already exists, and before this line the only place to find it was
    #: dashboard_users, by SQL (divergence 025).
    plane: str = ""
    trust_group: uuid.UUID | None = None

    def lines(self) -> list[str]:
        out = [
            f"memory authority peer {self.peer_id}: "
            + ("CREATED" if self.peer_created else "already present"),
            f"  owner {self.owner_id}: " + ("CREATED" if self.owner_created else "existing")
            + f" (plane {self.plane or '?'}, trust group {self.trust_group or 'none'})",
        ]
        for name in self.skills_created:
            out.append(f"  skill {name} v{SKILL_VERSION}: CREATED")
        for name in self.skills_present:
            out.append(f"  skill {name}: already current")
        return out


def bootstrap_memory_peer(conn, *, peer_id: uuid.UUID, owner_id: uuid.UUID | None = None,
                          owner_email: str | None = None,
                          trust_group: uuid.UUID | None = None) -> BootstrapReport:
    """Ensure the memory-authority peer, its owner and its manifest skills exist.

    Idempotent: a second run creates nothing and reports what it found. `conn` must be an
    autocommit connection (apps/core/dbconn.connect); the work runs in one transaction.
    """
    peer_id = uuid.UUID(str(peer_id))
    if owner_id is not None and (owner_email is not None or trust_group is not None):
        raise ValueError("--owner-id names an existing owner; --owner-email and "
                         "--trust-group describe one to create. Pass one or the other.")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT owner_id, status FROM peers WHERE id = %s FOR UPDATE",
                        (str(peer_id),))
            existing = cur.fetchone()
            owner_created = peer_created = False

            if existing is not None:
                found_owner, status = uuid.UUID(str(existing[0])), existing[1]
                if owner_id is not None and uuid.UUID(str(owner_id)) != found_owner:
                    raise BootstrapRefused(
                        f"peer {peer_id} already exists under owner {found_owner}, not "
                        f"{owner_id}. Refusing to re-own the memory authority.")
                if status != "active":
                    raise BootstrapRefused(
                        f"peer {peer_id} exists with status {status!r}. Step 5 treats a "
                        "non-active peer as having no owner, so memory calls are refused; "
                        "reactivating it is an operator decision, not a bootstrap.")
                if trust_group is not None:
                    # --trust-group describes an owner to CREATE, and none is created on
                    # this path. Ignoring it and exiting 0 would leave the operator's nodes
                    # outside the memory authority's group with nothing said (the runbook
                    # passes the same G to bootstrap_owner and to this command).
                    cur.execute("SELECT trust_group_id FROM dashboard_users WHERE id = %s",
                                (str(found_owner),))
                    held = cur.fetchone()
                    held = held[0] if held else None
                    if held is None or uuid.UUID(str(held)) != uuid.UUID(str(trust_group)):
                        raise BootstrapRefused(
                            f"peer {peer_id} already exists under owner {found_owner}, "
                            f"whose trust group is {held or 'none'}, not {trust_group}. "
                            "--trust-group only describes an owner to create, and an "
                            "owner's group cannot be changed (sql/040). Pass "
                            f"{held or 'that owner group'} to bootstrap_owner --trust-group "
                            "instead, or omit --trust-group here.")
                resolved_owner = found_owner
            else:
                if owner_id is not None:
                    cur.execute("SELECT id FROM dashboard_users WHERE id = %s",
                                (str(owner_id),))
                    if cur.fetchone() is None:
                        raise BootstrapRefused(f"owner {owner_id} does not exist")
                    resolved_owner = uuid.UUID(str(owner_id))
                else:
                    resolved_owner = uuid.uuid4()
                    email = owner_email or \
                        f"memory-authority+{peer_id.hex[:12]}@meshychat.invalid"
                    # A dev-plane owner MUST carry a trust group and a product one must
                    # not (CHECK dashboard_users_plane_group_ck); a namespace owner MUST be
                    # dev-plane (trigger ns_dev_only_trigger, sql/038), and the memory
                    # authority is the natural namespace owner.
                    cur.execute(
                        "INSERT INTO dashboard_users (id, email, display_name, plane, "
                        "  trust_group_id) VALUES (%s, %s, %s, 'dev', %s)",
                        (str(resolved_owner), email, "memory authority (in-process)",
                         str(trust_group or uuid.uuid4())))
                    owner_created = True
                cur.execute(
                    "INSERT INTO peers (id, owner_id, name, display_name) "
                    "VALUES (%s, %s, %s, %s)",
                    (str(peer_id), str(resolved_owner), PEER_NAME,
                     "memory authority (in-process, docs/DIVERGENCES.md 017)"))
                peer_created = True

            created, present = [], []
            for name in MANIFEST_SKILLS:
                cur.execute(
                    "SELECT 1 FROM skills WHERE peer_id = %s AND name = %s "
                    "  AND is_current = TRUE AND deleted_at IS NULL",
                    (str(peer_id), name))
                if cur.fetchone() is not None:
                    present.append(name)
                    continue
                cur.execute(
                    "INSERT INTO skills (peer_id, name, version, description, mode, "
                    "  share_mode, input_schema, max_runtime_ms, tags, is_current) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::text[], TRUE)",
                    (str(peer_id), name, SKILL_VERSION,
                     f"{name}, executed in-process by this server (docs/DIVERGENCES.md 017)",
                     SKILL_MODE, SKILL_SHARE_MODE, json.dumps({"type": "object"}),
                     SKILL_MAX_RUNTIME_MS, ["memory", "in-process"]))
                created.append(name)

            cur.execute("SELECT plane, trust_group_id FROM dashboard_users WHERE id = %s",
                        (str(resolved_owner),))
            plane, group = cur.fetchone()

    return BootstrapReport(peer_id=peer_id, owner_id=resolved_owner,
                           owner_created=owner_created, peer_created=peer_created,
                           skills_created=tuple(created), skills_present=tuple(present),
                           plane=str(plane),
                           trust_group=uuid.UUID(str(group)) if group else None)


__all__ = ["BootstrapRefused", "BootstrapReport", "MANIFEST_SKILLS", "PEER_NAME",
           "SKILL_VERSION", "bootstrap_memory_peer"]
