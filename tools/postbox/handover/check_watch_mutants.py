"""Serial pure-fixture mutation checks. Run only under an approved low-priority launcher.

Each case copies this package to a temporary tree, changes one safety property,
and requires a unittest assertion failure (never an import/runtime error). No
installed runner, credentials, production route or sender is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request


CASES = [
    ("delivered-replay", 'entry["state"] == "delivered" or not policy["dispatch_enabled"]',
     'False or not policy["dispatch_enabled"]', "test_one_operation_across_repeated_observation"),
    ("wrong-session", 'meta["payload"].get("id") != pair["target"]', 'False',
     "test_session_header_must_match_filename"),
    ("route-allocation-race", 'if route_pair(config, bank, sessions) != pair or policy_read(policy_path) != policy:',
     'if False:', "test_route_change_during_discovery_allocates_nothing"),
    ("unverified-result", 'result.get("result_signature_verified") is not True', 'False',
     "test_delivery_evidence_rejects_each_wrong_binding"),
    ("wrong-recipient", 'value.get("to") != ["kxxx"]', 'False',
     "test_delivery_evidence_rejects_each_wrong_binding"),
    ("unearned-accounting", 'accounting=Accounting(), max_scans',
     'accounting=Accounting(limit=245000, scope="total", qualified_epoch=binding["epoch"]), max_scans',
     "test_real_observer_stays_unqualified_with_high_token_floor"),
    ("expired-retry", 'if not 0 <= now - entry["created_at"] < RETRY_AGE:', 'if False:',
     "test_expired_operation_does_not_mint_or_send"),
    ("disabled-dispatch", 'entry["state"] == "delivered" or not policy["dispatch_enabled"]',
     'entry["state"] == "delivered" or False', "test_dispatch_enable_drains_same_generation"),
    ("missing-storage-receipt", 'receipt.get("key") != key', 'False',
     "test_delivery_evidence_rejects_each_wrong_binding"),
    ("old-generation-replay", 'if (entry.get("route_fingerprint") != fingerprint\n                    or entry["generation"] != binding["generation"]):', 'if False:',
     "test_unresolved_old_generation_requires_reconciliation"),
    ("route-round-trip-generation", 'or entry["generation"] != binding["generation"]', 'or False',
     "test_route_round_trip_cannot_replay_first_generation_intent"),
    ("long-degraded-starvation", 'reason = "degraded-set:" + digest(reason)', 'reason = reason',
     "test_long_degraded_set_does_not_starve_fault_dispatch"),
]


def health():
    # Explicit opt-in for HQD's production constraint; never a persistent poller.
    with urllib.request.urlopen("http://192.168.50.20:6310/v1/livez", timeout=5) as response:
        if response.status != 200 or json.load(response).get("status") != "ok":
            raise RuntimeError("production livez faltered; stop")


def main():
    check_health = sys.argv[1:] == ["--check-hqd-livez"]
    if sys.argv[1:] and not check_health:
        raise SystemExit("only --check-hqd-livez is supported")
    here = Path(__file__).resolve().parent
    source = (here / "watch.py").read_text(encoding="utf-8")
    for name, before, after, test in CASES:
        if check_health:
            health()
        if before not in source:
            raise RuntimeError("mutation anchor absent: " + name)
        with tempfile.TemporaryDirectory(prefix="handover-mutant-") as directory:
            tree = Path(directory) / "tools" / "postbox" / "handover"
            shutil.copytree(here, tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (tree / "watch.py").write_text(source.replace(before, after, 1), encoding="utf-8")
            command = [sys.executable, "-m", "unittest",
                       "tools.postbox.handover.test_watch.WatchTests." + test, "-q"]
            result = subprocess.run(command, cwd=directory, capture_output=True, text=True,
                timeout=30, creationflags=0x08000000 if os.name == "nt" else 0)
            output = result.stdout + result.stderr
            # An error is not evidence that an assertion detected the defect.
            detected = (result.returncode == 1 and "FAIL:" in output
                        and "AssertionError" in output and "ERROR:" not in output
                        and "FAILED (failures=" in output and "errors=" not in output)
            print(json.dumps({"mutant": name, "assertion_detected": detected}), flush=True)
            if not detected:
                print(output)
                return 1
        if check_health:
            health()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
