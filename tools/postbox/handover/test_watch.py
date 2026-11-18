"""Pure phase B fixtures: no installed files, network, sender or scheduler."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import watch as w
from .protocol import Hold, atomic_json, read_json

TARGET = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
NOW = 1793888400.0


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "private"
        self.config, self.bank, self.policy = (self.base / n for n in ("config.json", "bank.json", "policy.json"))
        self.sessions = self.base / "sessions"
        self.policy_value = {"schema": 1, "seat": w.SEAT, "source_commit": "a" * 40,
            "runtime_epoch": "unmeasured-runtime-1", "dispatch_enabled": True,
            "limit": 245000, "scope": "total", "warn": 183750, "request": 196000}
        atomic_json(self.policy, self.policy_value)
        self.install(TARGET)
        self.calls = []

    def install(self, target):
        atomic_json(self.config, {"identities": [{"name": w.IDENTITY, "enabled": True, "target": target}]})
        atomic_json(self.bank, {"schema_version": 1, "identity": w.IDENTITY, "consumer": "codex:" + target})
        path = self.sessions / "2026" / "11" / "05" / ("rollout-fixture-" + target + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "timestamp": "2026-11-05T14:19:00Z",
                                   "payload": {"id": target}}) + "\n", encoding="utf-8")
        self.rollout = path

    def observed(self, *args, **kwargs):
        self.assertIsNone(kwargs["accounting"].qualified_epoch)
        self.assertIsNone(kwargs["accounting"].limit)
        self.assertEqual((kwargs["max_scans"], kwargs["max_seconds"]), (64, 2.0))
        return {"action": "REQUEST", "notices": [{"action": "REQUEST",
                "reason": "same-task-compaction-rate"}], "fault_notices": []}

    def send(self, entry, path):
        self.calls.append(copy.deepcopy(entry))
        self.assertEqual(path.read_text(encoding="utf-8"), entry["body"])
        return self.evidence(entry)

    def evidence(self, entry):
        key = "msg/123-codex-kxxx-cli-fixture"
        client = {"status": "completed", "state": "succeeded", "result_signature_verified": True,
                  "op_id": entry["op_id"], "key": key,
                  "storage_receipt": {"key": key, "version": 1, "proven_by": "call"},
                  "value": {"from": w.SEAT, "to": ["kxxx"], "cc": [], "thread_id": w.NOTICE_THREAD,
                            "body": entry["body"], "type": "note"}}
        return {**{k: client[k] for k in ("status", "state", "result_signature_verified", "op_id", "key")},
                "client_result": client}

    def run_watch(self, **changes):
        args = dict(config=self.config, bank=self.bank, sessions=self.sessions,
                    clock=lambda: NOW, sender=self.send, observer=self.observed)
        args.update(changes)
        return w.watch_once(self.root, self.policy, **args)

    def journal(self):
        return read_json(self.root / "notice-journal.json")

    def test_one_operation_across_repeated_observation(self):
        first, second = self.run_watch(), self.run_watch()
        self.assertEqual((first["delivered"], second["delivered"], len(self.calls)), (1, 0, 1))
        entry = self.journal()["entries"][0]
        self.assertEqual(entry["state"], "delivered")
        self.assertEqual(entry["op_id"], "hb1." + w.digest([w.SEAT, TARGET, 1, "REQUEST"]))
        body = json.loads(entry["body"])
        self.assertEqual(body["accounting"], "UNQUALIFIED")
        self.assertEqual((body["limit"], body["warn"], body["request"]), (245000, 183750, 196000))
        self.assertFalse(body["effects_enabled"])
        self.assertEqual(body["phase_c"], "HOLD")

    def test_crash_after_remote_delivery_retries_identical_operation(self):
        def crash(entry, path):
            self.send(entry, path)
            raise KeyboardInterrupt("crash before receipt commit")
        with self.assertRaises(KeyboardInterrupt):
            self.run_watch(sender=crash)
        self.assertEqual(self.journal()["entries"][0]["state"], "intent")
        self.run_watch()
        self.assertEqual(len(self.calls), 2)
        for key in ("op_id", "body", "to", "thread"):
            self.assertEqual(self.calls[0][key], self.calls[1][key])
        self.assertEqual(self.journal()["entries"][0]["state"], "delivered")

    def test_crash_before_send_leaves_retryable_intent(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_watch(sender=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(self.journal()["entries"][0]["state"], "intent")
        self.run_watch()
        self.assertEqual(len(self.calls), 1)

    def test_receipt_commit_failure_retries_same_operation(self):
        real = w.atomic_json
        def commit(path, value):
            if path.name == "notice-journal.json" and any(e["state"] == "delivered" for e in value["entries"]):
                raise OSError("disk full")
            return real(path, value)
        with patch.object(w, "atomic_json", side_effect=commit), self.assertRaises(OSError):
            self.run_watch()
        self.assertEqual(self.journal()["entries"][0]["state"], "intent")
        self.run_watch()
        self.assertEqual(self.calls[0]["op_id"], self.calls[1]["op_id"])

    def test_delivery_evidence_rejects_each_wrong_binding(self):
        self.run_watch()
        entry = self.calls[0]
        changes = [((), "result_signature_verified", 1), ((), "state", "uncertain"),
            ((), "op_id", "different"), (("client_result",), "result_signature_verified", False),
            (("client_result", "value"), "from", "kxxx"),
            (("client_result", "value"), "to", ["pxx"]),
            (("client_result", "value"), "cc", ["pxx"]),
            (("client_result", "value"), "thread_id", "other"),
            (("client_result", "value"), "body", "different"),
            (("client_result", "value"), "requires_ack", True),
            (("client_result", "storage_receipt"), "key", "msg/wrong"),
            (("client_result", "storage_receipt"), "version", True)]
        for parents, key, value in changes:
            with self.subTest(parents=parents, key=key):
                evidence = self.evidence(entry)
                at = evidence
                for parent in parents:
                    at = at[parent]
                at[key] = value
                with self.assertRaises(Hold):
                    w.delivery_key(evidence, entry)

    def test_wrong_receipt_never_marks_delivered(self):
        def wrong(entry, path):
            result = self.send(entry, path)
            result["client_result"]["value"]["to"] = ["pxx"]
            return result
        with self.assertRaises(Hold):
            self.run_watch(sender=wrong)
        self.assertEqual(self.journal()["entries"][0]["state"], "intent")

    def test_route_bank_mismatch_allocates_nothing(self):
        atomic_json(self.bank, {"schema_version": 1, "identity": w.IDENTITY, "consumer": "codex:" + OTHER})
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertFalse((self.root / "binding.json").exists())
        self.assertEqual(self.calls, [])

    def test_two_rollouts_refused_even_if_one_is_newest(self):
        second = self.rollout.with_name("rollout-newest-" + TARGET + ".jsonl")
        second.write_bytes(self.rollout.read_bytes())
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertFalse((self.root / "binding.json").exists())

    def test_session_header_must_match_filename(self):
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": OTHER}}) + "\n")
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertFalse((self.root / "binding.json").exists())

    def test_route_change_during_discovery_allocates_nothing(self):
        def resolver(pair):
            result = w.resolve_rollout(pair)
            self.install(OTHER)
            return result
        with self.assertRaises(Hold):
            self.run_watch(resolver=resolver)
        self.assertFalse((self.root / "binding.json").exists())

    def test_route_change_after_observation_stops_dispatch(self):
        def observer(*args, **kwargs):
            result = self.observed(*args, **kwargs)
            self.install(OTHER)
            return result
        with self.assertRaises(Hold):
            self.run_watch(observer=observer)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.journal()["entries"][0]["state"], "prepared")

    def test_new_target_gets_new_generation_preserves_prior(self):
        self.run_watch()
        before = (self.root / "notice-journal.json").read_bytes()
        self.install(OTHER)
        result = self.run_watch()
        self.assertEqual(result["generation"], 2)
        self.assertEqual(len(self.journal()["entries"]), 2)
        self.assertEqual(self.calls[0]["notice_key"][1], TARGET)
        self.assertEqual(self.calls[1]["notice_key"][1], OTHER)
        self.assertIn(json.loads(before)["entries"][0], self.journal()["entries"])

    def test_unresolved_old_generation_requires_reconciliation(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_watch(sender=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.install(OTHER)
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.journal()["entries"][0]["state"], "intent")

    def test_expired_operation_does_not_mint_or_send(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_watch(sender=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        original = self.journal()["entries"][0]["op_id"]
        with self.assertRaises(Hold):
            self.run_watch(clock=lambda: NOW + w.RETRY_AGE)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.journal()["entries"][0]["op_id"], original)

    def test_dispatch_enable_drains_same_generation(self):
        self.policy_value["dispatch_enabled"] = False
        atomic_json(self.policy, self.policy_value)
        result = self.run_watch()
        self.assertEqual((result["pending"], len(self.calls)), (1, 0))
        original = self.journal()["entries"][0]["op_id"]
        self.policy_value["dispatch_enabled"] = True
        atomic_json(self.policy, self.policy_value)
        result = self.run_watch()
        self.assertEqual((result["generation"], len(self.calls)), (1, 1))
        self.assertEqual(self.calls[0]["op_id"], original)

    def test_single_owner_lock_not_stolen(self):
        self.root.mkdir()
        lock = self.root / "runner.lock"
        lock.write_text("0")
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertEqual(lock.read_text(), "0")
        self.assertEqual(self.calls, [])

    def test_discovery_budget_exhaustion_never_assumes_unique(self):
        pair = w.route_pair(self.config, self.bank, self.sessions)
        with self.assertRaises(Hold):
            w.resolve_rollout(pair, max_entries=1)

    def test_policy_cannot_claim_qualification(self):
        self.policy_value["qualified_epoch"] = "yes"
        atomic_json(self.policy, self.policy_value)
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertEqual(self.calls, [])

    def test_duplicate_json_rejected(self):
        self.policy.write_text('{"schema":1,"schema":1}')
        with self.assertRaises(Hold):
            self.run_watch()

    def test_corrupt_journal_preserved_without_send(self):
        self.run_watch()
        journal = self.journal()
        journal["entries"][0]["body"] = "transcript must never escape"
        atomic_json(self.root / "notice-journal.json", journal)
        before = (self.root / "notice-journal.json").read_bytes()
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertEqual((self.root / "notice-journal.json").read_bytes(), before)
        self.assertEqual(len(self.calls), 1)

    def test_real_observer_stays_unqualified_with_high_token_floor(self):
        # High tokens cannot qualify the accounting epoch by themselves.
        from datetime import datetime, timezone
        rows = []
        for index in range(2):
            for kind, payload in (("context_compacted", {}), ("token_count", {
                    "info": {"last_token_usage": {"input_tokens": 244999}}})):
                rows.append({"type": "event_msg", "timestamp": datetime.fromtimestamp(
                    NOW - 4 + index * 2 + (kind == "token_count"), timezone.utc).isoformat(),
                    "payload": {"type": kind, **payload}})
        with self.rollout.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        result = self.run_watch(observer=w.observe_bounded)
        self.assertEqual(result["accounting"], "UNQUALIFIED")
        self.assertEqual(self.calls, [])
        metrics = read_json(self.root / "g000001" / "metrics.json")["metrics"]
        self.assertEqual(len(metrics["floors"]), 2)
        self.assertEqual(metrics["degraded"], [])

    def test_real_compaction_rate_requests_without_qualified_accounting(self):
        from datetime import datetime, timezone
        rows = [("task_started", {"turn_id": "one-task"}),
                ("context_compacted", {}), ("context_compacted", {}), ("context_compacted", {})]
        with self.rollout.open("a") as stream:
            for index, (kind, payload) in enumerate(rows):
                stream.write(json.dumps({"type": "event_msg", "timestamp": datetime.fromtimestamp(
                    NOW - 5 + index, timezone.utc).isoformat(),
                    "payload": {"type": kind, **payload}}) + "\n")
        result = self.run_watch(observer=w.observe_bounded)
        self.assertEqual((result["action"], result["delivered"]), ("REQUEST", 1))
        body = json.loads(self.calls[0]["body"])
        self.assertEqual(body["reason"], "same-task-compaction-rate")
        self.assertEqual(body["observed_evidence"]["total_compactions"], 3)

    def test_two_fault_reasons_are_separate_operations_and_one_send_per_run(self):
        def faults(*args, **kwargs):
            return {"action": "HOLD", "notices": [], "fault_notices": [
                {"action": "HOLD", "reason": reason,
                 "binding": [TARGET, 1, kwargs["epoch"], reason]}
                for reason in ("missing-telemetry", "observer-error")]}
        first = self.run_watch(observer=faults)
        second = self.run_watch(observer=faults)
        self.assertEqual((first["delivered"], first["pending"], second["pending"]), (1, 1, 0))
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[0]["op_id"], self.calls[1]["op_id"])

    def test_journal_commit_failure_before_intent_never_sends(self):
        real = w.atomic_json
        def broken(path, value):
            if path.name == "notice-journal.json":
                raise OSError("disk full")
            return real(path, value)
        with patch.object(w, "atomic_json", side_effect=broken), self.assertRaises(OSError):
            self.run_watch()
        self.assertEqual(self.calls, [])

    def test_same_target_second_identity_is_ambiguous(self):
        config = read_json(self.config)
        config["identities"].append({"name": "another-seat", "enabled": True, "target": TARGET})
        atomic_json(self.config, config)
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertFalse((self.root / "binding.json").exists())

    def test_policy_epoch_creates_fresh_metrics_without_reusing_old_floors(self):
        self.run_watch(observer=w.observe_bounded)
        self.policy_value["runtime_epoch"] = "unmeasured-runtime-2"
        atomic_json(self.policy, self.policy_value)
        result = self.run_watch(observer=w.observe_bounded)
        self.assertEqual(result["generation"], 2)
        self.assertTrue((self.root / "g000001" / "metrics.json").exists())
        fresh = read_json(self.root / "g000002" / "metrics.json")["metrics"]
        self.assertEqual(fresh["floors"], [])

    def test_raw_private_metrics_are_not_copied_into_notice(self):
        def observed(*args, **kwargs):
            result = self.observed(*args, **kwargs)
            result["metrics"] = {"partial": "private-transcript-secret", "large": {"raw": "secret"},
                                 "current_input": 17, "file_bytes": 200, "total_compactions": 3}
            return result
        self.run_watch(observer=observed)
        self.assertNotIn("secret", self.calls[0]["body"])
        self.assertEqual(json.loads(self.calls[0]["body"])["observed_evidence"],
                         {"current_input": 17, "file_bytes": 200, "total_compactions": 3})

    def test_retry_keeps_first_evidence_when_metrics_advance(self):
        def observed(*args, **kwargs):
            result = self.observed(*args, **kwargs)
            result["metrics"] = {"file_bytes": 99999}
            return result
        with self.assertRaises(KeyboardInterrupt):
            self.run_watch(sender=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        frozen = self.journal()["entries"][0]["body"]
        self.run_watch(observer=observed)
        self.assertEqual(self.calls[0]["body"], frozen)

    def test_backward_clock_does_not_send_pending_operation(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_watch(sender=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(Hold):
            self.run_watch(clock=lambda: NOW - 1)
        self.assertEqual(self.calls, [])

    def test_private_root_must_not_overlap_installed_evidence(self):
        with self.assertRaises(Hold):
            w.watch_once(self.base, self.policy, config=self.config,
                         bank=self.bank, sessions=self.sessions)
        self.assertFalse((self.base / "binding.json").exists())

    def test_policy_must_not_alias_generated_state(self):
        self.root.mkdir()
        aliased = self.root / "local-status.json"
        atomic_json(aliased, self.policy_value)
        with self.assertRaises(Hold):
            w.watch_once(self.root, aliased, config=self.config, bank=self.bank, sessions=self.sessions)
        self.assertEqual(read_json(aliased), self.policy_value)

    def test_reparse_point_is_refused_when_platform_allows_fixture(self):
        linked = self.base / "linked"
        try:
            linked.symlink_to(self.sessions, target_is_directory=True)
        except OSError as exc:
            self.skipTest("symlink fixture unavailable: " + str(exc.errno))
        with self.assertRaises(Hold):
            w.safe_path(linked / "2026")

    def test_malformed_saved_binding_is_refused(self):
        self.run_watch()
        binding = read_json(self.root / "binding.json")
        binding["target"] = OTHER
        atomic_json(self.root / "binding.json", binding)
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertEqual(len(self.calls), 1)

    def test_sender_uses_gated_wrapper_and_frozen_operation_without_ack(self):
        self.run_watch()
        entry = self.calls[0]
        class Process:
            def wait(inner, timeout):
                self.assertEqual(timeout, 130)
                return 0
        def launch(command, **kwargs):
            self.assertEqual(command[:2], [str(w.PYTHON), str(w.SENDER)])
            self.assertIn("--no-tag", command)
            self.assertNotIn("--requires-ack", command)
            self.assertEqual(command[command.index("--op-id") + 1], entry["op_id"])
            self.assertEqual(command[command.index("--to") + 1], "kxxx")
            self.assertEqual(command[command.index("--thread") + 1], w.NOTICE_THREAD)
            kwargs["stdout"].write(json.dumps(self.evidence(entry)).encode())
            return Process()
        with patch.object(w.subprocess, "Popen", side_effect=launch):
            result = w.send_notice(entry, self.base / "body.txt")
        self.assertEqual(w.delivery_key(result, entry), "msg/123-codex-kxxx-cli-fixture")

    def test_route_round_trip_cannot_replay_first_generation_intent(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_watch(sender=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.policy_value["dispatch_enabled"] = False
        atomic_json(self.policy, self.policy_value)
        self.install(OTHER)
        self.assertEqual(self.run_watch()["generation"], 2)
        self.install(TARGET)
        self.policy_value["dispatch_enabled"] = True
        atomic_json(self.policy, self.policy_value)
        with self.assertRaises(Hold):
            self.run_watch()
        self.assertEqual(read_json(self.root / "binding.json")["generation"], 3)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.journal()["entries"][0]["state"], "intent")

    def test_long_degraded_set_does_not_starve_fault_dispatch(self):
        reason = "degraded:" + ",".join(sorted([
            "ambiguous-thread", "clock-rollback", "invalid-partial-state",
            "rollout-replaced-or-truncated", "runtime-model-changed-requalification-required",
            "unknown-runtime-model"]))
        self.assertGreater(len(reason), 160)
        def fault(*args, **kwargs):
            return {"action": "HOLD", "notices": [], "fault_notices": [{
                "action": "HOLD", "reason": reason,
                "binding": [TARGET, 1, kwargs["epoch"], reason]}]}
        try:
            result = self.run_watch(observer=fault)
        except Hold:
            self.fail("long degraded set must remain dispatchable")
        self.assertEqual((result["delivered"], len(self.calls)), (1, 1))
        body = json.loads(self.calls[0]["body"])
        self.assertLessEqual(len(body["reason"]), 160)
        self.assertEqual(self.journal()["entries"][0]["notice_key"][-2], reason)
        self.run_watch(observer=fault)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
