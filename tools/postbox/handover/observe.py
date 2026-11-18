"""Shared seat-skill / opt-in postbox detector interface.

The caller owns the state directory, clock and accounting provenance. No mesh
reads, process operations or machine configuration writes happen here.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import time

from .metrics import Accounting, Metrics, Scanner, decision
from .protocol import Hold, atomic_json, exclusive, read_json


def observe(rollout: Path, state_path: Path, *, thread: str, epoch: str, now: float,
            accounting: Accounting = Accounting(), generation=0, cooldown=False):
    if rollout.resolve() == state_path.resolve():
        raise Hold("telemetry state must not overwrite rollout")
    with exclusive(state_path.with_suffix(state_path.suffix + ".lock")):
        data = {}
        if state_path.exists():
            data = read_json(state_path)
            if not isinstance(data, dict) or data.get("schema") != 1:
                raise Hold("unsupported metrics schema")
            s = Metrics(**data["metrics"])
            if s.thread != thread or s.generation != generation:
                raise Hold("metrics binding mismatch; preserve prior generation")
        else:
            s = Metrics(thread=thread, epoch=epoch, generation=generation)
        Scanner(s).scan(rollout, epoch)
        action, reason = decision(s, accounting, now=now, cooldown=cooldown)
        if action == "WARN":
            s.warning_sent = True
        elif action == "REQUEST":
            s.warning_sent = s.request_sent = True
        # Durable outbox: a crash between write and caller delivery cannot erase
        # the request. Caller deduplicates (thread,generation,kind), then records
        # its consumption separately. Repeated status is not a second emission.
        notices = data.get("notices", [])
        if action in {"WARN", "REQUEST", "HOLD"}:
            key = f"{thread}:{generation}:{action}"
            if not any(n["key"] == key for n in notices):
                notices.append({"key": key, "action": action, "reason": reason,
                                "detected_at": now, "provenance": "local-detector"})
        atomic_json(state_path, {"schema": 1, "metrics": asdict(s), "notices": notices})
        return {"action": action, "reason": reason, "metrics": s.public(), "notices": notices,
                "effects_enabled": False}


def observe_bounded(rollout: Path, state_path: Path, fault_path: Path, *,
                    max_scans=64, max_seconds=2.0, **kwargs):
    """Opt-in scheduled caller: bounded catch-up plus a durable fault outbox.

    The owner delivers notices and records authenticated delivery separately.
    This function never sends mail or enables a scheduler. Outbox failure raises;
    it must not be represented as healthy or silently swallowed by its caller.
    """
    paths = [p.resolve() for p in (rollout, state_path, fault_path)]
    protected = paths + [p.with_suffix(p.suffix + ".lock") for p in paths[1:]]
    if len(set(protected)) != 5 or not 1 <= max_scans <= 256 or not 0 < max_seconds <= 10:
        raise Hold("invalid bounded observer paths or budget")
    start = time.monotonic()
    fault = None
    scans = 0
    if not rollout.exists():
        result = {"action": "UNKNOWN", "reason": "stale-or-missing-telemetry",
                  "effects_enabled": False}
        if state_path.exists():
            fault = "missing-telemetry"
        elif not fault_path.exists():
            return {**result, "fault_notices": [], "scans": 0}
    else:
        for index in range(max_scans):
            scans = index + 1
            try:
                result = observe(rollout, state_path, **kwargs)
            except Exception:
                # Malformed saved fields and unexpected observer failures must
                # still reach the independent durable outbox. BaseException
                # (including KeyboardInterrupt/SystemExit) remains uncaught;
                # outbox commit failures below also propagate unchanged.
                result = {"action": "HOLD", "reason": "observer-error", "effects_enabled": False}
                fault = "observer-error"
                break
            degraded = sorted(set(result["metrics"]["degraded"]))
            if degraded:
                fault = "degraded:" + ",".join(degraded)
                break
            if result["reason"] != "scan-backlog" or time.monotonic() - start >= max_seconds:
                break
    # A separate lock permits reporting a stuck metrics lock without stealing it.
    with exclusive(fault_path.with_suffix(fault_path.suffix + ".lock")):
        data = read_json(fault_path) if fault_path.exists() else {"schema": 1, "notices": []}
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise Hold("unsupported fault outbox schema")
        if fault:
            key = (kwargs["thread"], kwargs.get("generation", 0), kwargs["epoch"], fault)
            if not any(n["binding"] == list(key) for n in data["notices"]):
                data["notices"].append({"binding": list(key), "action": "HOLD",
                    "reason": fault, "detected_at": kwargs["now"], "provenance": "local-detector"})
                atomic_json(fault_path, data)
        result["fault_notices"] = data["notices"]
    result["scans"] = scans
    return result
