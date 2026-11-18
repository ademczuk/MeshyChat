"""Opt-in phase B observation and notice delivery; phase C stays fenced.

No import-time effects. Installed route files are read only. Scheduling and
accounting qualification are deliberately outside this runner.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .metrics import Accounting
from .observe import observe_bounded
from .protocol import Hold, MAX_STATE_BYTES, atomic_json, canonical, digest, exclusive

SEAT = "codex-kxxx-cli"
IDENTITY = "codex-kxxx-prod"
NOTICE_THREAD = "codex-handover-example"
PYTHON = Path("C:/_SampleProjects/meshychat-django/.venv313/Scripts/python.exe")
SENDER = Path("C:/Users/user/meshychat/watchers/codex_kxxx_cli_send.py")
CONFIG = Path("C:/Users/user/.meshychat/postbox/config.json")
BANK = Path("C:/Users/user/.meshychat/postbox/spool/codex-kxxx-prod.log.codex.offset.bank.json")
SESSIONS = Path("C:/Users/user/.codex/sessions")
# Leave a full day's grace inside the client's seven-day operation retention.
RETRY_AGE = 6 * 86400
REASON = re.compile(r"[a-z0-9,._:-]{1,160}\Z")


def safe_path(path: Path):
    """Reject links/junctions in every existing component, including parents."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Hold("link or reparse point")
    return path


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise Hold("duplicate JSON field")
        out[key] = value
    return out


def local_json(path: Path):
    path = safe_path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise Hold("expected regular local file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise Hold("local file changed during open")
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise Hold("local file exceeds bound")
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Hold("nonfinite JSON")))


def policy_read(path: Path):
    value = local_json(path)
    if (not isinstance(value, dict) or set(value) != {
            "schema", "seat", "source_commit", "runtime_epoch", "dispatch_enabled",
            "limit", "scope", "warn", "request"}
            or value["schema"] != 1 or value["seat"] != SEAT
            or type(value["dispatch_enabled"]) is not bool
            or not re.fullmatch(r"[0-9a-f]{40}", value["source_commit"])
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value["runtime_epoch"])
            or any(type(value[k]) is not int for k in ("limit", "warn", "request"))
            or (value["limit"], value["scope"], value["warn"], value["request"])
               != (245000, "total", 183750, 196000)):
        raise Hold("unsupported phase B policy")
    return value


def route_pair(config=CONFIG, bank=BANK, sessions=SESSIONS):
    data = local_json(config)
    if not isinstance(data, dict):
        raise Hold("invalid route config")
    rows = data["identities"]
    if not isinstance(rows, list):
        raise Hold("invalid identity inventory")
    matches = [r for r in rows if isinstance(r, dict) and r.get("name") == IDENTITY]
    if len(matches) != 1 or matches[0].get("enabled", True) is not True:
        raise Hold("missing or ambiguous installed identity")
    row = matches[0]
    target = row["target"]
    if not isinstance(target, str) or str(uuid.UUID(target)) != target:
        raise Hold("target is not canonical UUID")
    if sum(isinstance(r, dict) and r.get("target") == target for r in rows) != 1:
        raise Hold("session has multiple routes")
    saved = local_json(bank)
    if (not isinstance(saved, dict) or saved.get("schema_version") not in (1, 2) or saved.get("identity") != IDENTITY
            or saved.get("consumer") != "codex:" + target):
        raise Hold("route and bank consumer disagree")
    tree = safe_path(Path(row.get("codex_sessions_dir") or sessions))
    return {"target": target, "consumer": saved["consumer"], "sessions": str(tree)}


def resolve_rollout(pair, *, max_entries=20000, seconds=2.0):
    """Inspect the entire bounded date tree, never select the newest match.

    An exhausted search is UNKNOWN even if a candidate was already found.
    Only YYYY/MM/DD directories are admissible; links fail closed.
    """
    root = safe_path(Path(pair["sessions"]))
    start, count, found = time.monotonic(), 0, []

    def walk(path, depth):
        nonlocal count
        with os.scandir(path) as entries:
            for entry in entries:
                count += 1
                if count > max_entries or time.monotonic() - start > seconds:
                    raise Hold("rollout discovery budget exhausted")
                if depth < 3:
                    if entry.name.isdigit() and len(entry.name) == (4 if depth == 0 else 2):
                        child = safe_path(Path(entry.path))
                        if not child.is_dir():
                            raise Hold("invalid session directory")
                        walk(child, depth + 1)
                elif entry.name.startswith("rollout-") and entry.name.endswith("-" + pair["target"] + ".jsonl"):
                    found.append(safe_path(Path(entry.path)))
                    if len(found) > 1:
                        raise Hold("ambiguous rollout")
    walk(root, 0)
    if len(found) != 1:
        raise Hold("missing rollout")
    path = found[0]
    with path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Hold("rollout is not regular")
        header = stream.readline(262145)
    if not header.endswith(b"\n") or len(header) > 262144:
        raise Hold("unbounded or incomplete session metadata")
    meta = json.loads(header, object_pairs_hook=_pairs)
    if (not isinstance(meta, dict) or meta.get("type") != "session_meta"
            or not isinstance(meta.get("payload"), dict) or meta["payload"].get("id") != pair["target"]):
        raise Hold("rollout metadata does not bind target")
    return path


def _notice(key, action, reason, binding, policy, evidence=None):
    # Keep the complete degraded set in the private operation key, but bound
    # its public representation so accumulated faults cannot starve the outbox.
    if (isinstance(reason, str) and reason.startswith("degraded:")
            and 160 < len(reason) <= 2048 and re.fullmatch(r"[a-z0-9,._:-]+", reason)):
        reason = "degraded-set:" + digest(reason)
    if action not in {"WARN", "REQUEST", "HOLD"} or not REASON.fullmatch(reason):
        raise Hold("unsupported notice")
    op_id = "hb1." + digest(key)
    # Explicit allowlist: neither private metrics nor transcript fields escape.
    body = canonical({"phase": "B", "seat": SEAT, "thread": binding["target"],
        "generation": binding["generation"], "epoch": binding["epoch"],
        "source_commit": policy["source_commit"], "operation": op_id,
        "source_provenance": "owner-declared-unverified",
        "action": action, "reason": reason, "accounting": "UNQUALIFIED",
        "observed_evidence": evidence or {},
        "limit": 245000, "scope": "total", "warn": 183750, "request": 196000,
        "effects_enabled": False, "phase_c": "HOLD"}).decode() + "\n"
    return {"notice_key": key, "op_id": op_id, "body": body,
            "body_sha256": digest(body), "to": ["kxxx"], "cc": [], "thread": NOTICE_THREAD}


def send_notice(entry, body_file):
    """One hidden, bounded gated invocation; operation identity survives timeout."""
    with tempfile.TemporaryFile() as output:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([str(PYTHON), str(SENDER), "--to", "kxxx",
                "--thread", NOTICE_THREAD, "--type", "note", "--no-tag",
                "--body-file", str(body_file), "--op-id", entry["op_id"]],
                stdout=output, stderr=errors,
                creationflags=0x08000000 if os.name == "nt" else 0)
            try:
                returncode = process.wait(timeout=130)
            except subprocess.TimeoutExpired as exc:
                if os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=5, creationflags=0x08000000)
                    finally:
                        if process.poll() is None:
                            process.kill()
                else:
                    process.kill()
                process.wait(timeout=5)
                raise Hold("sender timeout; retain operation") from exc
            output.seek(0)
            raw = output.read(MAX_STATE_BYTES + 1)
    if returncode != 0 or len(raw) > MAX_STATE_BYTES:
        raise Hold("sender incomplete; retain operation")
    return json.loads(raw, object_pairs_hook=_pairs)


def delivery_key(result, entry):
    """Authenticated call completion is delivery, never an agent acknowledgement."""
    if not isinstance(result, dict) or not isinstance(result.get("client_result"), dict):
        raise Hold("missing delivery evidence")
    client = result.get("client_result", {})
    value = client.get("value", {})
    receipt = client.get("storage_receipt", {})
    if not isinstance(value, dict) or not isinstance(receipt, dict):
        raise Hold("malformed delivery evidence")
    key = result.get("key")
    if (result.get("status") != "completed" or result.get("state") != "succeeded"
            or result.get("result_signature_verified") is not True
            or result.get("op_id") != entry["op_id"] or client.get("op_id") != entry["op_id"]
            or client.get("result_signature_verified") is not True
            or client.get("state") != "succeeded" or client.get("status") != "completed"
            or not isinstance(key, str) or not re.fullmatch(r"msg/[A-Za-z0-9._-]+", key)
            or client.get("key") != key or value.get("from") != SEAT
            or value.get("to") != ["kxxx"] or value.get("cc", []) != []
            or value.get("thread_id") != NOTICE_THREAD or value.get("body") != entry["body"]
            or value.get("type") != "note" or value.get("requires_ack", False) is not False
            or receipt.get("key") != key or type(receipt.get("version")) is not int
            or receipt["version"] < 1 or receipt.get("proven_by") != "call"):
        raise Hold("delivery evidence does not match frozen notice")
    return key


@contextmanager
def record_failure(root):
    try:
        yield
    except Exception:
        # No input values or exception strings in status: installed files may
        # contain credentials. Commit failure still propagates to the caller.
        atomic_json(safe_path(root / "local-status.json"), {
            "state": "HOLD", "reason": "local-evidence-or-delivery-incomplete",
            "effects_enabled": False, "phase_c": "HOLD", "accounting": "UNQUALIFIED"})
        raise


def watch_once(root: Path, policy_path: Path, *, seat=SEAT, config=CONFIG,
               bank=BANK, sessions=SESSIONS, clock=time.time, sender=send_notice,
               observer=observe_bounded, resolver=resolve_rollout):
    if seat != SEAT:
        raise Hold("unsupported seat")
    root, policy_path = safe_path(root), safe_path(policy_path)
    if root in policy_path.parents and policy_path != root / "policy.json":
        raise Hold("policy overlaps private state names")
    protected = [safe_path(Path(p)) for p in (config, bank, sessions)]
    # Private writes may not overlap installed sources (in either direction).
    if any(root == p or root in p.parents or p in root.parents for p in protected):
        raise Hold("private state overlaps installed sources")
    policy = policy_read(policy_path)
    with exclusive(safe_path(root / "runner.lock")), record_failure(root):
        now = clock()
        if not math.isfinite(now):
            raise Hold("invalid clock")
        pair = route_pair(config, bank, sessions)
        rollout = resolver(pair)
        if root == rollout or root in rollout.parents or rollout in root.parents:
            raise Hold("private state overlaps rollout")
        # Enabling dispatch must drain the already frozen outbox, not create a
        # second generation. It does not qualify accounting or change runtime.
        epoch_policy = {k: v for k, v in policy.items() if k != "dispatch_enabled"}
        fingerprint = digest({"pair": pair, "rollout": str(rollout), "policy": epoch_policy})
        binding_path = safe_path(root / "binding.json")
        prior = local_json(binding_path) if binding_path.exists() else None
        if prior is not None and (not isinstance(prior, dict) or prior.get("schema") != 1
                or type(prior.get("generation")) is not int or prior["generation"] < 1):
            raise Hold("invalid saved binding")
        # The route and bank are independently reread before allocating state.
        if route_pair(config, bank, sessions) != pair or policy_read(policy_path) != policy:
            raise Hold("route or policy changed during discovery")
        if prior is None or prior.get("fingerprint") != fingerprint:
            binding = {"schema": 1, **pair, "fingerprint": fingerprint,
                       "generation": 1 if prior is None else prior["generation"] + 1,
                       "epoch": digest({"policy": epoch_policy, "pair": pair})}
            atomic_json(binding_path, binding)
        else:
            binding = prior
            expected = {"schema": 1, **pair, "fingerprint": fingerprint,
                        "generation": prior["generation"],
                        "epoch": digest({"policy": epoch_policy, "pair": pair})}
            if binding != expected:
                raise Hold("saved binding fields disagree")
        generation = safe_path(root / ("g%06d" % binding["generation"]))
        metrics, faults = safe_path(generation / "metrics.json"), safe_path(generation / "faults.json")
        for path in (metrics, faults):
            safe_path(path.with_suffix(".json.lock"))
        observed = observer(rollout, metrics, faults, thread=pair["target"],
            epoch=binding["epoch"], generation=binding["generation"], now=now,
            accounting=Accounting(), max_scans=64, max_seconds=2.0)
        journal_path = safe_path(root / "notice-journal.json")
        journal = local_json(journal_path) if journal_path.exists() else {"schema": 1, "entries": []}
        if not isinstance(journal, dict) or journal.get("schema") != 1 or not isinstance(journal.get("entries"), list):
            raise Hold("invalid notice journal")
        seen = set()
        for entry in journal["entries"]:
            if (not isinstance(entry, dict) or entry.get("op_id") != "hb1." + digest(entry["notice_key"])
                    or entry.get("body_sha256") != digest(entry["body"])
                    or entry.get("to") != ["kxxx"] or entry.get("cc") != []
                    or entry.get("thread") != NOTICE_THREAD
                    or entry.get("state") not in {"prepared", "intent", "delivered"}
                    or entry["op_id"] in seen
                    or type(entry.get("generation")) is not int or entry["generation"] < 1
                    or type(entry.get("created_at")) not in (int, float)
                    or not math.isfinite(entry["created_at"])):
                raise Hold("corrupt frozen operation")
            seen.add(entry["op_id"])
        notices = [(n, [SEAT, pair["target"], binding["generation"], n["action"]])
                   for n in observed.get("notices", [])]
        notices += [(n, [SEAT, *n["binding"], n["action"]])
                    for n in observed.get("fault_notices", [])]
        evidence = {}
        for field in ("current_input", "file_bytes", "total_compactions"):
            value = observed.get("metrics", {}).get(field)
            if type(value) is int and value >= 0:
                evidence[field] = value
        for notice, key in notices:
            frozen = _notice(key, notice["action"], notice["reason"], binding, policy, evidence)
            existing = next((e for e in journal["entries"] if e["op_id"] == frozen["op_id"]), None)
            # The first observation freezes evidence; later samples must not
            # rewrite the body associated with an already allocated operation.
            if existing is not None and existing["notice_key"] != key:
                raise Hold("frozen notice key changed")
            if existing is None:
                journal["entries"].append({**frozen, "created_at": now,
                    "state": "prepared", "route_fingerprint": fingerprint,
                    "generation": binding["generation"]})
        # Freeze every body and destination before any dispatch.
        atomic_json(journal_path, journal)
        delivered = 0
        for entry in journal["entries"]:
            if entry["state"] == "delivered" or not policy["dispatch_enabled"]:
                continue
            if (entry.get("route_fingerprint") != fingerprint
                    or entry["generation"] != binding["generation"]):
                raise Hold("prior generation operation needs owner reconciliation")
            if not 0 <= now - entry["created_at"] < RETRY_AGE:
                raise Hold("operation outside safe retry lifetime")
            if route_pair(config, bank, sessions) != pair or policy_read(policy_path) != policy:
                raise Hold("route or policy changed before dispatch")
            body_file = safe_path(root / "notices" / (entry["op_id"] + ".txt"))
            body_file.parent.mkdir(parents=True, exist_ok=True)
            # The journal is authoritative: reconstructing this derivative does
            # not mint another operation after a crash.
            fd, temporary = tempfile.mkstemp(prefix=".notice-", dir=body_file.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(entry["body"].encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, body_file)
            finally:
                Path(temporary).unlink(missing_ok=True)
            entry["state"] = "intent"
            atomic_json(journal_path, journal)
            key = delivery_key(sender(entry, body_file), entry)
            entry.update(state="delivered", message_key=key, delivered_at=clock())
            atomic_json(journal_path, journal)
            delivered = 1
            break  # One bounded send per invocation; keep remaining outbox.
        status = {"state": "OBSERVED", "action": observed["action"],
            "generation": binding["generation"], "delivered": delivered,
            "pending": sum(e["state"] != "delivered" for e in journal["entries"]),
            "accounting": "UNQUALIFIED", "effects_enabled": False, "phase_c": "HOLD"}
        atomic_json(safe_path(root / "local-status.json"), status)
        return status
