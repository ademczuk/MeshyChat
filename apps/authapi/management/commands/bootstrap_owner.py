"""`manage.py bootstrap_owner` - create the FIRST owner and register the operator's key.

    python manage.py bootstrap_owner --email ops@example.org --public-key-file owner.pub
    python manage.py bootstrap_owner --email ops@example.org --public-key-file owner.pub \
        --plane dev --trust-group <G, the SAME group bootstrap_memory_peer is given> \
        --tier enterprise

`--plane dev` is the install whose nodes can WRITE the mesh: shared/* refuses a
product-plane owner as claimant, grantee and writer, and the plane cannot be changed after
the owner exists (divergence 024). The default stays `product`. README.md "Fresh install"
is the whole runbook this command is step 3 of: `python -m tools.meshy_node keygen` makes
the key file, and this command runs BEFORE `bootstrap_memory_peer`, which creates an owner
of its own (divergence 025).

`--tier` sets the owner's tier (divergence 028, T). Left out, the owner gets the column
default, the `free` tier, whose `peers_per_owner` cap is 5: a self-hosted mesh wants
`--tier enterprise` (unlimited nodes per owner). `manage.py set_owner_tier` changes the tier or
one capability afterwards.

Run once, by an operator, on a new install. There is no HTTP equivalent and there must
never be one (apps/authapi/bootstrap.py says why). Prints one JSON object on stdout -
owner_id, peer_id, peer_kid, human_key_kid, public_key - and the next steps on stderr.

EXIT CODES: 0 created; 1 refused (CommandError: an owner exists, the email exists, the key
file is a private key or not an Ed25519 public key, no DSN). Nothing is written on 1.

WHICH DATABASE. `--dsn`, else MESHY_DSN, else DATABASE_URL. There is NO fallback: the
settings module falls back to the local dev database, and a bootstrap that quietly created
the production operator's owner on a developer's laptop would report success.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.authapi import bootstrap
from apps.core import dbconn


class Command(BaseCommand):
    help = "Create the first owner and register the operator's Ed25519 public key for owner sign-in."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="the owner's email (unique)")
        parser.add_argument("--public-key-file", required=True,
                            help="PEM PUBLIC KEY, or one line of unpadded base64url. "
                                 "A private key is refused.")
        parser.add_argument("--display-name", default=None)
        parser.add_argument("--peer-name", default="operator",
                            help="name of the signer peer that holds this key "
                                 "(default: operator). A name is a mail address, so one "
                                 "already held by any peer is refused (divergence 065)")
        parser.add_argument("--additional-owner", action="store_true",
                            help="create an owner even though one already exists")
        parser.add_argument("--plane", choices=("product", "dev"), default="product",
                            help="the owner's plane, FIXED once created (sql/040). 'dev' is "
                                 "required for this owner's nodes to write any shared/* "
                                 "namespace, the mesh included (default: product)")
        parser.add_argument("--trust-group", default=None,
                            help="with --plane dev, REQUIRED: the trust group UUID. Pass the "
                                 "SAME value to bootstrap_memory_peer --trust-group, so this "
                                 "owner's nodes are authorized to call the memory authority "
                                 "without a skill grant")
        parser.add_argument("--tier", default=None,
                            help="the owner's tier: free, pro or enterprise (default: the "
                                 "column default, free, whose peers_per_owner cap is 5). A "
                                 "self-hosted mesh wants enterprise; `set_owner_tier` "
                                 "changes it later")
        parser.add_argument("--dsn", default=None,
                            help="database DSN (default: MESHY_DSN, then DATABASE_URL)")

    def handle(self, *args, **opts):
        target = opts["dsn"] or os.environ.get("MESHY_DSN") or os.environ.get("DATABASE_URL")
        if not target:
            raise CommandError("no database named: pass --dsn or set MESHY_DSN. There is "
                               "deliberately no default for this command.")
        try:
            data = Path(opts["public_key_file"]).read_bytes()
        except OSError as exc:
            raise CommandError(f"cannot read --public-key-file: {exc}") from exc
        try:
            raw = bootstrap.read_public_key(data)
            # dbconn.connect: SET ROLE to the RLS-bound application role and VERIFY it.
            # Every table written here is one meshychat_app may write; the command needs no
            # more privilege than the routes that later read these rows.
            conn = dbconn.connect(target)
            try:
                created = bootstrap.bootstrap_owner(
                    conn, email=opts["email"], public_key_raw=raw,
                    display_name=opts["display_name"], peer_name=opts["peer_name"],
                    additional_owner=opts["additional_owner"], plane=opts["plane"],
                    trust_group=opts["trust_group"], tier=opts["tier"])
            finally:
                conn.close()
        except bootstrap.BootstrapRefused as exc:
            raise CommandError(f"refused: {exc}") from exc

        self.stdout.write(json.dumps(created.as_dict()))
        # The next steps name COMMANDS (README.md "Fresh install"), not wire formats: the
        # previous text said "sign JCS ... and POST" and "call namespace.claim signed by
        # this key", which no documented tool could do (divergence 025).
        client = "python -m tools.meshy_node"
        sys.stderr.write(
            "owner created. On the machine holding the PRIVATE key (the keygen state file):\n"
            f"  {client} bind --base-url <server> --state-file <operator.json> "
            f"--peer-id {created.peer_id}\n"
            f"  {client} pair --base-url <server> --state-file <operator.json>   "
            "(pairing codes; a node redeems one with `mint`)\n")
        if opts["plane"] == "dev":
            sys.stderr.write(
                "For this owner's nodes to WRITE the mesh (dev plane, divergence 024):\n"
                f"  start the server with MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID={created.peer_id}\n"
                f"  {client} claim ... --namespace shared/mesh-chat\n"
                f"  {client} grant ... --namespace shared/mesh-chat --grantee <node peer_id> "
                "--permission write   (once per node)\n")
        else:
            sys.stderr.write(
                "NOTE: a product-plane owner's nodes can never be granted or WRITE a shared/*\n"
                "namespace, the mesh included (I2). For a mesh-writing install, re-run on an\n"
                "empty database with --plane dev --trust-group <group> (divergence 024).\n")
