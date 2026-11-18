"""Pure platform-specific cutover plans; never read or write installed config.

Installed adapters must extract this normalized inventory from actual services,
configuration and native queues. A plan is review material, not a loaded-route
receipt. Unknown fields are retained. Changed snapshots require reconciliation.
"""
from __future__ import annotations

from copy import deepcopy

from .adapters import PLATFORMS
from .protocol import Hold, digest


def plan_route(platform, snapshot, *, expected_digest, new_thread):
    if platform not in PLATFORMS or digest(snapshot) != expected_digest:
        raise Hold("unknown platform or route snapshot CAS mismatch")
    if snapshot.get("inventory_complete") is not True:
        raise Hold("all route consumers and native queues must be inventoried")
    old = snapshot.get("thread")
    generation = snapshot.get("generation")
    if not isinstance(old, str) or not old or not isinstance(new_thread, str) or not new_thread or old == new_thread:
        raise Hold("fresh successor binding required")
    if type(generation) is not int or generation < 1:
        raise Hold("invalid route generation")
    routes = snapshot.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise Hold("route inventory missing")
    required = set(PLATFORMS[platform]["route_fields"]) - {"other-inventoried-consumers"}
    if not required.issubset(routes):
        raise Hold("platform route consumer omitted")
    if platform == "hz-builder" and "bank.consumer" in routes:
        raise Hold("Builder installed inventory has no bank consumer; requalify changed installation")
    for value in routes.values():
        if not isinstance(value, dict) or value.get("thread") != old:
            raise Hold("consumer binding mismatch")
    if not isinstance(snapshot.get("queues"), dict) or not snapshot["queues"]:
        raise Hold("native and installed queue inventory missing")
    if "native" not in snapshot["queues"] or not isinstance(snapshot.get("offsets"), dict):
        raise Hold("native queue or delivery offsets absent")
    after = deepcopy(snapshot)
    after["thread"], after["generation"] = new_thread, generation + 1
    for value in after["routes"].values():
        value["thread"] = new_thread
    return {"platform": platform, "expected_digest": expected_digest, "after": after,
            "after_digest": digest(after), "effects_enabled": False,
            "requires": ["verified-intake-pause", "durable-multifile-journal",
                         "exactly-one-intake-restart", "running-loaded-route-proof"]}


def reconcile_snapshot(plan, observed_before):
    """Return a detached planned snapshot only while its exact CAS still holds.

    Concurrent mail, recall, consumer or offset changes invalidate the plan. The
    adapter must capture and reconcile those facts; it cannot discard them by
    overwriting an old snapshot. This function performs no installed mutation.
    """
    if digest(observed_before) != plan["expected_digest"]:
        raise Hold("snapshot changed; preserve arrivals and reconcile before routing")
    recomputed = plan_route(plan["platform"], observed_before,
                            expected_digest=plan["expected_digest"], new_thread=plan["after"]["thread"])
    if recomputed != plan:
        raise Hold("cutover plan changed")
    return deepcopy(plan["after"])
