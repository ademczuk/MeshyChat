"""Point a Codex identity's delivery bank at the identity's current target, the supported way.

The bank checkpoint `<log>.codex.offset.bank.json` names its consumer, "codex:" + the target thread. When the target
in the postbox config changes (a seat restarted into a new session), the bank refuses the old checkpoint on every pass
until its consumer matches. On 7-8 Nov 2026 a hand edit made it match but left the file at mode 0644, which the bank
also refuses: the HZ admin's delivery loop crashed every 60 s for 13 hours, 1,610 times (a reviewer's audit, finding
3; oxxxxxx). This command does the same edit through delivery_bank.atomic_json: owner-only from the first byte,
written whole and renamed into place.

    python -I tools/postbox/bank_retarget.py --config <postbox config.json> --identity NAME [--dry-run]

The order is: STOP the postbox, change the identity's `target` in the config, run this, START the postbox. A running
postbox's bank writes its own consumer (the target it read at start) into the checkpoint on every commit, so a
retarget under a running postbox is undone by the next delivery (pxx P2-1). This command therefore takes the
postbox's own lock, `postbox.lock` beside the config, for the whole rewrite, and refuses while a postbox holds it.
It keeps the offset, the delivered tokens and the held mail: held mail goes to the new target, which is where the
seat now is.

It refuses: a checkpoint that is a link, a reparse point or not a regular file; on POSIX, one owned by another account
(a 0644 file of this account's is the case it exists for, and is rewritten 0600); one whose identity is another
identity's; and an identity with no usable target. Exit 0 done or nothing to do, 1 refused, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import time
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import delivery_bank  # noqa: E402
import postbox_os  # noqa: E402

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
#: As the daemon's own acquire_daemon_lock: a prober holds postbox.lock for microseconds, a daemon for its life.
LOCK_TRIES, LOCK_GAP_S = 10, 0.2


def take_postbox_lock(config_path, tries=LOCK_TRIES, gap_s=LOCK_GAP_S, sleep=time.sleep):
    """The open, locked fd of the postbox.lock beside `config_path`, or None when a postbox holds it."""
    fd = os.open(os.path.join(os.path.dirname(os.path.abspath(config_path)), "postbox.lock"), os.O_RDWR | os.O_CREAT)
    for attempt in range(tries):
        if postbox_os.try_lock(fd):
            return fd
        if attempt < tries - 1:
            sleep(gap_s)
    os.close(fd)
    return None


def release_postbox_lock(fd):
    try:
        postbox_os.unlock(fd)
    finally:
        os.close(fd)


def _identity(config: dict, name: str):
    raw = config.get("identities")
    items = list(raw.values()) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    found = [i for i in items if isinstance(i, dict) and i.get("name") == name]
    return found[0] if len(found) == 1 else None


def retarget(ident: dict, *, dry_run: bool = False) -> tuple[int, str]:
    """(exit code, one line saying what was done or why not)."""
    target, log, name = ident.get("target"), ident.get("log"), ident.get("name")
    if not isinstance(target, str) or not target or not isinstance(log, str) or not log:
        return EXIT_REFUSED, "refused: identity %r has no usable target and log" % (name,)
    path = log + ".codex.offset.bank.json"
    if not os.path.lexists(path):
        return EXIT_OK, "nothing to do: no checkpoint at %s (the bank starts fresh)" % path
    try:
        raw = delivery_bank.read_bounded_bytes(path, delivery_bank.MAX_LEDGER, require_private=False,
                                               not_regular_msg="checkpoint is not a regular file")
        info = os.lstat(path)
    except (OSError, ValueError) as exc:
        return EXIT_REFUSED, "refused: %s: %s" % (path, exc)
    if os.name == "posix" and info.st_uid != os.getuid():
        return EXIT_REFUSED, "refused: %s is owned by another account" % path
    try:
        data = json.loads(raw)
    except ValueError:
        return EXIT_REFUSED, "refused: %s is not JSON" % path
    if not isinstance(data, dict) or data.get("schema_version") not in (1, 2):
        return EXIT_REFUSED, "refused: %s is not a delivery checkpoint (schema_version 1 or 2)" % path
    if data.get("identity") != name:
        return EXIT_REFUSED, "refused: %s belongs to identity %r, not %r" % (path, data.get("identity"), name)
    want = "codex:" + target
    mode = stat.S_IMODE(info.st_mode)
    mode_ok = os.name != "posix" or mode == 0o600
    if data.get("consumer") == want and mode_ok:
        return EXIT_OK, "nothing to do: %s already names %s" % (path, want)
    before = data.get("consumer")
    data["consumer"] = want
    what = "consumer %s -> %s" % (before, want) if before != want else "consumer unchanged"
    if not mode_ok:
        what += ", mode %s -> 0o600" % oct(mode)
    if dry_run:
        return EXIT_OK, "dry run: would rewrite %s (%s)" % (path, what)
    try:
        delivery_bank.atomic_json(path, data)
    except (OSError, ValueError) as exc:
        return EXIT_REFUSED, "refused: could not rewrite %s: %s" % (path, exc)
    return EXIT_OK, "rewrote %s (%s)" % (path, what)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Point a Codex identity's delivery bank at its current target.")
    ap.add_argument("--config", required=True, help="the postbox config.json")
    ap.add_argument("--identity", required=True, help="the identity's name in that config")
    ap.add_argument("--dry-run", action="store_true", help="say what would change, write nothing")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        with open(args.config, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError) as exc:
        print("usage: cannot read %s: %s" % (args.config, exc))
        return EXIT_USAGE
    ident = _identity(config if isinstance(config, dict) else {}, args.identity)
    if ident is None:
        print("usage: no single identity named %r in %s" % (args.identity, args.config))
        return EXIT_USAGE
    fd = take_postbox_lock(args.config)
    if fd is None:
        print("refused: a postbox is running (it holds postbox.lock beside %s). Stop it, change the target, run this, "
              "then start it: a running postbox writes its old target back on its next delivery." % args.config)
        return EXIT_REFUSED
    try:
        code, line = retarget(ident, dry_run=args.dry_run)
    finally:
        release_postbox_lock(fd)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
