"""Durable, fail-closed handover protocol. Platform effects belong to adapters.

An adapter is a trusted local verifier, never a source of operator authority.
No shipped live adapter is qualified. Fake adapters exercise the protocol only.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = 1
PHASES = ("DETECTED", "CHECKPOINTED", "OLD_QUIESCED", "SUCCESSOR_STANDBY",
          "ROUTE_COMMITTED", "DELIVERY_PROVEN", "SUCCESSOR_ACTIVE", "OLD_RETIRED")
SECTIONS = ("operator", "goal", "review_budgets", "messages", "open_work", "directives",
            "ack_ledgers", "uncertain_operations", "children", "leases", "worktree",
            "artifacts", "tests", "paths", "standing_instructions")
MAX_STATE_BYTES = 1048576


class Hold(RuntimeError):
    """A required fact is absent; ordinary admission stays fenced."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path: Path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise Hold("state exceeds bound")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError, MemoryError) as exc:
        raise Hold("corrupt state; preserve for recovery") from exc


def atomic_json(path: Path, value):
    """Private same-directory write, fsync, replace; POSIX directory fsync.

    Windows crash durability must additionally be qualified by its platform
    adapter; a successful replace alone is not that qualification.
    """
    data = canonical(value)
    if len(data) > MAX_STATE_BYTES:
        raise Hold("state exceeds bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temp.open("xb") as stream:
            os.chmod(temp, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def exclusive(path: Path):
    """Never steal a lock based on PID age; owner recovery must prove inactivity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise Hold("controller lock exists; prove owner inactive before recovery") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        path.unlink()


def checkpoint(manifest: dict, bootstrap: str, selected: str):
    """Validate explicit completeness indexes, not the truth of agent prose.

    Authenticity, current authority and live operation state are rechecked by the
    qualified adapter at CHECKPOINTED/OLD_QUIESCED, not inferred from this schema.
    """
    if manifest.get("schema") != SCHEMA or manifest.get("ready") is not True:
        raise Hold("checkpoint not ready or schema unsupported")
    for key in ("seat", "home", "old_thread", "generation"):
        if key not in manifest:
            raise Hold("missing checkpoint binding: " + key)
    if len(bootstrap) > 6000 or len(bootstrap + selected) > 24000:
        raise Hold("bootstrap exceeds bounded resume budget")
    if manifest.get("goal", {}).get("state") not in {"active", "paused", "none", "complete"}:
        raise Hold("unknown goal state")
    for name in SECTIONS:
        if name not in manifest:
            raise Hold("missing completeness section: " + name)
        index = manifest.get("completeness", {}).get(name, {})
        value = manifest[name]
        count = len(value) if isinstance(value, (list, dict)) else 1
        if index.get("count") != count or index.get("sha256") != digest(value):
            raise Hold("completeness mismatch: " + name)
    for entry in manifest["messages"]:
        if not entry.get("key") or not isinstance(entry.get("version"), int) or entry["version"] < 1:
            raise Hold("messages require key AND version")
    # Canonical backlog references preserve the complete set, not just prompt text.
    for entry in manifest["open_work"]:
        if not all(entry.get(k) for k in ("path", "sha256")) or not isinstance(entry.get("count"), int):
            raise Hold("open-work canonical ledger reference missing")
    # This section accepts paths only. Do not include credential-bearing state.
    if not isinstance(manifest["paths"], dict) or any(not isinstance(v, str) for v in manifest["paths"].values()):
        raise Hold("identity/home/state/config paths only")
    expected = {"bootstrap_sha256": hashlib.sha256(bootstrap.encode()).hexdigest(),
                "selected_sha256": hashlib.sha256(selected.encode()).hexdigest()}
    if manifest.get("resume_text") != expected:
        raise Hold("resume text binding mismatch")
    return digest(manifest)


def save_checkpoint(root: Path, manifest, bootstrap, selected):
    sha = checkpoint(manifest, bootstrap, selected)
    # Content-addressed immutable bundle, complete before becoming discoverable.
    path = root / (sha + ".json")
    bundle = {"manifest": manifest, "bootstrap": bootstrap, "selected": selected}
    with exclusive(root / ".checkpoint.lock"):
        if path.exists():
            if read_json(path) != bundle:
                raise Hold("immutable checkpoint conflict")
        else:
            atomic_json(path, bundle)
    return path, sha


class Journal:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "transition.json"

    def load(self):
        row = read_json(self.path)
        if row.get("schema") != SCHEMA or row.get("phase") not in PHASES + ("HOLD",):
            raise Hold("unknown journal schema or phase")
        return row

    def begin(self, *, seat, home, old_thread, generation, expected_route, queue_inventory,
              now, boot_id, monotonic, cooldown_until=0):
        with exclusive(self.root / ".controller.lock"):
            if self.path.exists():
                raise Hold("existing transition must be reconciled, never replaced")
            if now < cooldown_until:
                raise Hold("transition during cooldown")
            if not boot_id or not isinstance(generation, int) or generation < 1:
                raise Hold("missing controller clock/generation binding")
            row = dict(schema=SCHEMA, transaction=secrets.token_hex(16), seat=seat, home=home,
                       old_thread=old_thread, new_thread=None, generation=generation,
                       phase="DETECTED", checkpoint=None, expected_route=expected_route,
                       queue_inventory=queue_inventory, nonce=secrets.token_hex(32),
                       boot_id=boot_id, ready_deadline_monotonic=monotonic + 300,
                       started_at=now, launch_attempts=0, effects=[], receipts={}, inflight=None)
            atomic_json(self.path, row)
            return row

    def hold(self, row, reason):
        row["held_from"] = row["phase"]
        row["phase"], row["hold_reason"] = "HOLD", reason
        atomic_json(self.path, row)
        raise Hold(reason)

    def step(self, adapter, *, expected_generation, boot_id, monotonic, now):
        """One effect per locked step. Unknown effects require observed recovery.

        There is no automatic retry/rollback API. A crash after an intent retains
        its exact phase and fences, even if the effect actually completed.
        """
        entered = time.monotonic()
        with exclusive(self.root / ".controller.lock"):
            row = self.load()
            if row["generation"] != expected_generation:
                raise Hold("generation CAS mismatch")
            if row["phase"] == "HOLD":
                raise Hold(row["hold_reason"])
            if row["inflight"]:
                self.hold(row, "uncertain effect: reconcile observed ownership/route; do not replay")
            if boot_id != row["boot_id"]:
                self.hold(row, "clock epoch changed; verify deadlines before recovery")
            phase = row["phase"]
            if phase == "OLD_RETIRED":
                return row
            if phase == "DETECTED" and monotonic > row["ready_deadline_monotonic"]:
                self.hold(row, "checkpoint-ready deadline expired; retain last verified checkpoint")
            missing = adapter.missing_capabilities(row)
            if missing:
                self.hold(row, "unqualified adapter: " + ",".join(missing))
            next_phase = PHASES[PHASES.index(phase) + 1]
            # Journal ownership/command plan before dispatch, not just a PID later.
            intent = adapter.plan(row, next_phase)
            if not intent or not intent.get("ownership"):
                self.hold(row, "missing pre-dispatch ownership plan")
            if next_phase == "SUCCESSOR_STANDBY":
                if row["launch_attempts"]:
                    self.hold(row, "single launch attempt exhausted")
                row["launch_attempts"] = 1
            row["inflight"] = {"phase": next_phase, "intent": intent}
            row["effects"].append(row["inflight"])
            atomic_json(self.path, row)  # MUST precede effect
            try:
                receipt = adapter.perform(row, next_phase)
                self.validate(row, next_phase, receipt)
                if not adapter.verify(row, next_phase, receipt):
                    raise Hold("adapter failed observed proof verification")
                if (next_phase == "CHECKPOINTED" and
                        monotonic + time.monotonic() - entered > row["ready_deadline_monotonic"]):
                    raise Hold("checkpoint verification exceeded ready deadline")
            except Exception:
                # Preserve uncertain intent. Do not roll back based on timeout.
                # The adapter must retain all work fences outside this process.
                self.hold(row, "effect failed or proof missing: " + next_phase)
            row["receipts"][next_phase] = receipt
            if next_phase == "CHECKPOINTED":
                row["checkpoint"] = receipt["digest"]
            if next_phase == "SUCCESSOR_STANDBY":
                row["new_thread"] = receipt["new_thread"]
            row["phase"], row["inflight"] = next_phase, None
            if next_phase == "SUCCESSOR_ACTIVE":
                row["cooldown_until"] = now + 1800
            atomic_json(self.path, row)
            return row

    @staticmethod
    def validate(row, phase, receipt):
        binding = {"transaction": row["transaction"], "generation": row["generation"],
                   "seat": row["seat"], "home": row["home"], "old_thread": row["old_thread"]}
        if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in binding.items()):
            raise Hold("receipt binding mismatch")
        checks = {
            "CHECKPOINTED": ("manifest_complete", "authority_current", "resume_dependency_qualified",
                              "bootstrap_budget_qualified"),
            "OLD_QUIESCED": ("old_tools_fenced", "old_turns_fenced", "old_mail_fenced",
                              "children_settled", "external_operations_settled", "unknown_sends_resolved"),
            "SUCCESSOR_STANDBY": ("fresh_thread", "same_policy", "standby_enforced", "owned_process"),
            "ROUTE_COMMITTED": ("intake_stopped_verified", "old_route_cas", "all_consumers_inventoried",
                                "native_queue_reconciled", "queue_versions_preserved", "offsets_preserved",
                                "held_preserved", "one_intake_running", "loaded_route_verified"),
            "DELIVERY_PROVEN": ("sender_distinct_controlled", "authenticated_reply", "routing_verified",
                                "new_rollout_consumed", "ack_ledger_recorded",
                                "nonce_not_replayed", "deadline_valid", "response_contract_verified"),
            "SUCCESSOR_ACTIVE": ("old_fence_rechecked", "route_rechecked", "proof_rechecked", "lease_cas",
                                 "native_queue_reconciled", "ordinary_work_released"),
            "OLD_RETIRED": ("owned_old_only", "shared_daemon_preserved", "receipt_archived"),
        }
        if any(receipt.get(k) is not True for k in checks[phase]):
            raise Hold("required proof absent: " + phase)
        strings = {"CHECKPOINTED": ("digest",), "SUCCESSOR_STANDBY": ("new_thread",),
                   "ROUTE_COMMITTED": ("route_digest",),
                   "DELIVERY_PROVEN": ("linked_reply_key", "request_key")}.get(phase, ())
        if any(not isinstance(receipt.get(k), str) or not receipt[k] for k in strings):
            raise Hold("required receipt reference absent: " + phase)
        if phase == "SUCCESSOR_STANDBY" and receipt["new_thread"] == row["old_thread"]:
            raise Hold("successor must be fresh")
        if phase not in {"CHECKPOINTED", "OLD_QUIESCED"}:
            if receipt.get("checkpoint") != row["checkpoint"]:
                raise Hold("checkpoint digest mismatch")
        if phase in {"ROUTE_COMMITTED", "DELIVERY_PROVEN", "SUCCESSOR_ACTIVE", "OLD_RETIRED"}:
            if receipt.get("new_thread") != row["new_thread"]:
                raise Hold("successor binding mismatch")
        if phase == "DELIVERY_PROVEN" and receipt.get("nonce") != row["nonce"]:
            raise Hold("nonce mismatch")
