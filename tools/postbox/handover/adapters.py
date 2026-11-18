"""Explicit platform inventory and non-effecting adapter boundaries.

Nothing here launches Codex, edits machine configuration, stops postbox, reads
identity secrets or claims an installed adapter has been qualified.
"""
from __future__ import annotations

from .protocol import Hold

REQUIRED = (
    "durable_replace", "exclusive_controller", "execution_fence", "settled_operation_discovery",
    "owned_fresh_launcher", "enforced_standby", "persistent_idle_intake", "same_home_policy",
    "resume_dependency", "bounded_bootstrap", "native_queue_inventory", "service_pause_drain",
    "route_cas", "running_loaded_route", "distinct_sender_probe", "active_lease_cas", "owned_thread_retirement",
)

PLATFORMS = {
    "hqd": {
        "service": "Windows Task Scheduler: MeshyChat-Postbox (shared; never broad-kill)",
        "route_fields": ["identity.target", "bank.consumer", "default-launch-pointer", "other-inventoried-consumers"],
        "queue_fields": ["spool.offset", "bank.key/version/status", "native-Codex-queue", "handled-key/version"],
        "constraints": ["owner Kxxx changes machine configuration", "pinned client remains unchanged",
                        "Windows replacement durability needs disposable crash qualification"],
    },
    "hqb": {
        "service": "Windows Task Scheduler; discover exact shared service ownership",
        "route_fields": ["identity.target", "bank.consumer", "default-launch-pointer", "raw-transcript-consumer"],
        "queue_fields": ["SQLite/WAL native queue", "spool.offset", "bank.key/version/status", "handled-key/version"],
        "constraints": ["launcher rejects second live profile", "retire old execution only after quiescence",
                        "memory-bxxxxx runs gates"],
    },
    "hz-admin": {
        "service": "launchd; discover actual installed domain/label, do not assume GUI",
        "route_fields": ["identity.target", "bank.consumer", "default-launch-pointer", "other-inventoried-consumers"],
        "queue_fields": ["same-home native queue", "spool.offset", "bank.key/version/status", "held-key/version"],
        "constraints": ["headless persistent launcher unqualified", "Admin GO plus Bxxxxx yes before install"],
    },
    "hz-builder": {
        "service": "system-domain launchd KeepAlive; do not assume sudo or GUI domain",
        "route_fields": ["identity.target", "default-launch-pointer", "other-inventoried-consumers"],
        "queue_fields": ["same-home native queue", "installed spool-offset semantics", "handled-key/version"],
        "constraints": ["no installed bank-consumer field: never invent one",
                        "no resume-brief/owed: reviewed signed bounded fallback required",
                        "Admin GO plus Bxxxxx yes before install"],
    },
    "pxx": {
        "service": "launchd; discover actual installed domain/label",
        "route_fields": ["identity.target", "bank.consumer", "hardcoded-default-resume-UUID", "other-inventoried-consumers"],
        "queue_fields": ["same-home native queue", "spool.offset", "bank.key/version/status", "held-key/version"],
        "constraints": ["Terminal/AppleScript restart not qualified headless",
                        "never stop a shared app-server to retire one thread"],
    },
}


class InstalledAdapter:
    """Intentionally unsupported until platform implementation AND pilot review.

    There is no --force or environment variable that promotes this inventory to
    evidence. Implementations must verify observed OS/process/queue facts and
    authenticated messages, not accept a JSON bag of true flags from the agent.
    """
    def __init__(self, platform):
        if platform not in PLATFORMS:
            raise Hold("unknown installed platform")
        self.platform = platform

    def missing_capabilities(self, row=None):
        return list(REQUIRED)

    def status(self):
        return {"platform": self.platform, "state": "HOLD", "effects_enabled": False,
                "missing": self.missing_capabilities(), "inventory": PLATFORMS[self.platform]}

    def plan(self, row, phase):
        raise Hold("live platform adapter has not been implemented/qualified")

    def perform(self, row, phase):
        raise Hold("production effects disabled")

    def verify(self, row, phase, receipt):
        return False
