"""`manage.py bootstrap_memory_peer`: create the memory-authority peer in a fresh database.

See apps/calls/bootstrap.py for what it creates, what it refuses and why the default owner
fails closed. docs/DIVERGENCES.md 017.

EXIT CODES, because an operator's provisioning script reads nothing else:
    0  the peer, its owner and its six manifest skills exist (created or already present)
    1  refused: the peer exists under another owner, or is not active, or --owner-id
       names nobody. Nothing was changed.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.calls import bootstrap
from apps.core import dbconn


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise CommandError(f"not a UUID: {raw!r}") from exc


class Command(BaseCommand):
    help = ("Ensure the memory-authority peer (settings.MESHY_MEMORY_PEER_ID), its owner and "
            "its memory.* skill rows exist, so signed memory calls can be routed.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--peer-id", default=None,
            help="the peer to create; defaults to settings.MESHY_MEMORY_PEER_ID, which is "
                 "the id step 9 routes on. Anything else creates a peer nothing routes to.")
        parser.add_argument("--owner-id", default=None,
                            help="attach the peer to this EXISTING dashboard_users row")
        parser.add_argument("--owner-email", default=None,
                            help="email for the dev-plane owner created when --owner-id "
                                 "is not given")
        parser.add_argument("--trust-group", default=None,
                            help="trust group for the created owner. Default: a NEW group, "
                                 "so no caller is same-group and every caller needs a "
                                 "skill grant (fail closed)")

    def handle(self, *args, **opts):
        peer_id = _uuid(opts["peer_id"] or str(settings.MESHY_MEMORY_PEER_ID))
        owner_id = _uuid(opts["owner_id"]) if opts["owner_id"] else None
        trust_group = _uuid(opts["trust_group"]) if opts["trust_group"] else None
        if str(peer_id) != str(uuid.UUID(str(settings.MESHY_MEMORY_PEER_ID))):
            # Not refused: a staging database may want the row before the setting flips.
            # Said out loud, because a peer nothing routes to looks exactly like success.
            self.stderr.write(self.style.WARNING(
                f"  NOTE: {peer_id} is not settings.MESHY_MEMORY_PEER_ID "
                f"({settings.MESHY_MEMORY_PEER_ID}); calls to it will NOT execute until "
                "the setting names it."))

        conn = dbconn.connect()
        try:
            report = bootstrap.bootstrap_memory_peer(
                conn, peer_id=peer_id, owner_id=owner_id,
                owner_email=opts["owner_email"], trust_group=trust_group)
        except (bootstrap.BootstrapRefused, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        finally:
            conn.close()

        for line in report.lines():
            self.stdout.write(line)
