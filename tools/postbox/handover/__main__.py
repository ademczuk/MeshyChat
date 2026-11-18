"""Read-only platform status, opt-in telemetry file, and offline checkpoint checks."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .adapters import InstalledAdapter, PLATFORMS
from .observe import observe, observe_bounded
from .protocol import Hold, checkpoint, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--platform", choices=PLATFORMS, required=True)
    for command in ("observe", "observe-bounded"):
        scan = commands.add_parser(command)
        for name in ("rollout", "state", "thread", "epoch"):
            scan.add_argument("--" + name, required=True)
        scan.add_argument("--generation", type=int, default=0)
        if command == "observe-bounded":
            scan.add_argument("--fault-state", required=True)
    validate = commands.add_parser("validate-checkpoint")
    validate.add_argument("bundle", type=Path)
    watch = commands.add_parser("watch-once")
    watch.add_argument("--seat", choices=["codex-kxxx-cli"], required=True)
    watch.add_argument("--policy", type=Path, required=True)
    watch.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "status":
            result = InstalledAdapter(args.platform).status()
        elif args.command == "watch-once":
            from .watch import watch_once
            result = watch_once(args.root, args.policy, seat=args.seat)
        elif args.command in {"observe", "observe-bounded"}:
            # CLI never accepts an unverified numeric limit as qualified evidence.
            kwargs = dict(thread=args.thread, epoch=args.epoch,
                          generation=args.generation, now=time.time())
            if args.command == "observe-bounded":
                result = observe_bounded(Path(args.rollout), Path(args.state),
                                         Path(args.fault_state), **kwargs)
            else:
                result = observe(Path(args.rollout), Path(args.state), **kwargs)
        else:
            bundle = read_json(args.bundle)
            result = {"sha256": checkpoint(bundle["manifest"], bundle["bootstrap"], bundle["selected"]),
                      "state": "SCHEMA_VALID", "live_authority_verified": False}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Hold, OSError, ValueError, KeyError, TypeError, RecursionError, MemoryError):
        # Do not echo input values or raw exceptions: they may contain secrets.
        print(json.dumps({"state": "HOLD", "reason": "invalid or unsupported local evidence"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
