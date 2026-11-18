"""Back up the database, verify a backup, restore it into an empty database (E02, #406).

    python -m tools.backup backup  --dsn DSN --out DIR [--copy-to DIR ...]
    python -m tools.backup verify  --manifest DIR/NAME.manifest.json
    python -m tools.backup restore --manifest DIR/NAME.manifest.json --dsn EMPTY_DSN
    python -m tools.backup prune   --dir DIR [--dir DIR ...] [--dry-run]
    python -m tools.backup rehearse --manifest DIR/NAME.manifest.json --role meshychat_app --role OWNER
    python -m tools.backup serve   --dir DIR --bind ADDR --port N

`backup` writes two files: NAME.dump (`pg_dump --format=custom`) and NAME.manifest.json, which
records the dump's sha256 and size, and, read in the SAME snapshot the dump reads, every
table's exact row count and every migration ledger (apps/core/backup_manifest.py). `restore`
refuses a dump whose bytes do not match, refuses a target that is not empty, restores with
`pg_restore --exit-on-error --single-transaction`, and then compares the restored database
with the manifest; any difference is a non-zero exit naming the table or ledger.

The offsite copy (operator decision 2026-09-25): HQD writes the backups into an export
folder and `prune`s it; HQB PULLS that folder over the LAN with tools/backup/pull_to_hqb.ps1,
which verifies every copy and applies the same retention (tools/backup/retention.py: 14 daily
+ 4 weekly). `--copy-to DIR` still writes a second copy to any directory and re-hashes it
there. README "Offsite copy" says what to install on each machine; nothing here schedules
anything.

`serve` (2026-09-26) is an HTTP channel for HQB's pull: HQD serves the export folder
READ-ONLY, GET /index.json lists the backups and GET /NAME streams one (tools/backup/serve.py
has the contract). There is NO credential on that channel, by operator ruling. Who can read
is the network scope, a bind to HQD's LAN address (a wildcard is refused) plus a Windows
Firewall rule allowing one remote address, HQB's, both set up outside this code; what arrives
is checked at HQB against the manifest's sha256. That is for TEST DATA ONLY: the manifest
crosses the same channel, so its hash proves the transfer, not the sender. Production needs
each request signed by HQB's node key.
This channel does not sign requests yet. Until it does, who can read rests on the
network scope alone, which is not a production pattern; signing each request is
the planned change.

The role that runs `backup` must own the tables, be a superuser or have BYPASSRLS (counts are
read with row_security off, and a filtered role gets an error, not a smaller count).
pg_dump does not carry roles: a restore on a new host needs meshychat_app and the owner role
created first, which `rehearse` does in a new scratch cluster (tools/backup/rehearse.py).

pg_dump and pg_restore are found in `--pg-bin DIR`, then `MESHY_PG_BIN`, then PATH. They must
be the server's major version or newer.
"""
