"""`manage.py set_owner_tier` - change an owner's tier, or one capability's cap, with no SQL.

    python manage.py set_owner_tier --owner <owner_id> --tier enterprise
    python manage.py set_owner_tier --owner <owner_id> --override peers_per_owner=50 --note "site mesh"
    python manage.py set_owner_tier --owner <owner_id> --clear-override peers_per_owner

Exactly one of --tier, --override, --clear-override. Tier and capability names are checked
against `tier_capabilities`; an override value is -1 (unlimited) or a cap >= 0. Prints one JSON
object on stdout: the owner's tier and each capability's effective cap, its source (override or
tier) and this cycle's usage, READ BACK from the database after the change.

WHY IT EXISTS (divergence 028, T). `bootstrap_owner` creates the operator on the
`free` tier, where `peers_per_owner` is 5, so a self-hosted mesh stops at five nodes per owner
and only SQL could lift it. For a self-hosted mesh, `--tier enterprise` (peers_per_owner -1,
unlimited) or `--override peers_per_owner=N`. A new install can instead pass
`bootstrap_owner --tier enterprise` and never run this.

NOT ENFORCED HERE: `calls_per_day`, `calls_per_cycle`, `concurrent_calls`,
`local_memory_keys`, `meshy_memory_keys` are stored and can be set here, and no code reads them
as a limit (apps/authapi/owner_tier.py says how that was measured). The JSON names them.

EXIT CODES: 0 changed; 1 refused (CommandError), and nothing was written. WHICH DATABASE:
`--dsn`, else MESHY_DSN, else DATABASE_URL, with NO fallback, as bootstrap_owner.
"""
from __future__ import annotations

import json
import os
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.authapi import owner_tier
from apps.core import dbconn


class Command(BaseCommand):
    help = ("Set an owner's tier (free|pro|enterprise) or one capability override, and print "
            "the effective caps read back from the database.")

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True, help="the owner id (dashboard_users.id)")
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--tier", help="free, pro or enterprise (as tier_capabilities "
                                           "defines them)")
        action.add_argument("--override", metavar="CAPABILITY=VALUE",
                            help="set one capability's cap for this owner; -1 is unlimited")
        action.add_argument("--clear-override", metavar="CAPABILITY",
                            help="remove this owner's override, so the tier's cap applies")
        parser.add_argument("--note", default=None, help="with --override: why, stored on the row")
        parser.add_argument("--dsn", default=None,
                            help="database DSN (default: MESHY_DSN, then DATABASE_URL)")

    def handle(self, *args, **opts):
        target = opts["dsn"] or os.environ.get("MESHY_DSN") or os.environ.get("DATABASE_URL")
        if not target:
            raise CommandError("no database named: pass --dsn or set MESHY_DSN. There is "
                               "deliberately no default for this command.")
        if opts["note"] is not None and opts["override"] is None:
            raise CommandError("--note goes with --override only")
        # dbconn.connect: the RLS-bound application role, verified. The three tables written
        # here (dashboard_users, owner_tier_overrides) and read (tier_capabilities,
        # owner_usage) carry no RLS and meshychat_app holds the privileges; the command needs no
        # more than the routes that read them.
        conn = dbconn.connect(target)
        try:
            try:
                if opts["tier"] is not None:
                    owner_tier.set_tier(conn, opts["owner"], opts["tier"])
                elif opts["override"] is not None:
                    capability, value = owner_tier.parse_override(opts["override"])
                    owner_tier.set_override(conn, opts["owner"], capability, value,
                                            opts["note"])
                else:
                    owner_tier.clear_override(conn, opts["owner"], opts["clear_override"])
            except owner_tier.TierRefused as exc:
                raise CommandError(f"refused: {exc}") from exc
            shown = owner_tier.effective_caps(conn, opts["owner"])
        finally:
            conn.close()
        self.stdout.write(json.dumps(shown))
        sys.stderr.write(
            "caps enforced by this server for peers_per_owner (mint), file_bytes (file "
            "finalize) and log_retention_days_max (dashboard); "
            f"{', '.join(shown['not_enforced'])} are stored and settable, nothing enforces "
            "them\n")
