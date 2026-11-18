"""Standalone stdlib tests: temporary fixtures only, no Django/DB/mesh/CLI.

Run exactly this file with unittest. Fake capability proofs are not qualification.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from . import observe as observe_module
from .adapters import InstalledAdapter, PLATFORMS, REQUIRED
from .cutover import plan_route, reconcile_snapshot
from .metrics import Accounting, Metrics, Scanner, decision
from .observe import observe, observe_bounded
from .protocol import (Hold, Journal, PHASES, SECTIONS, checkpoint,
                       digest, exclusive, read_json, save_checkpoint)

NOW = datetime.fromisoformat("2026-11-03T09:00:59+00:00").timestamp()


def event(kind, second=0, **payload):
    # Synthetic timestamp, no transcript bodies or real identities.
    return {"type": "event_msg", "timestamp": f"2026-11-03T09:00:{second:02d}Z",
            "payload": {"type": kind, **payload}}


def token(n, second=0, body=None):
    return event("token_count", second, info={"last_token_usage": {
        "input_tokens": n, "accounted_body_tokens": body}})


def lines(*events):
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


def meta():
    return {"type": "session_meta", "timestamp": "2026-11-03T09:00:00Z", "payload": {"id": "thread"}}


class TempTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class MetricsTests(TempTest):
    def setUp(self):
        super().setUp()
        self.path = self.root / "rollout.jsonl"
        self.s = Metrics("thread", "epoch")
        self.reader = Scanner(self.s)

    def append(self, *events):
        fresh = not self.path.exists()
        with self.path.open("ab") as f:
            if fresh:
                f.write(lines(meta()))
            f.write(lines(*events))
        self.reader.scan(self.path, "epoch")

    def policy(self, scope="total", limit=64000, **kw):
        return decision(self.s, Accounting(limit, scope, "epoch"), now=NOW, **kw)

    def floors(self, value, body=None):
        self.append(event("context_compacted", 1), token(value, 2, body),
                    event("context_compacted", 3), token(value, 4, body))

    def test_partial_restart_and_public_redaction(self):
        raw = lines(meta(), event("context_compacted", 1), token(50000, 2))
        self.path.write_bytes(raw[:-7])
        status = self.reader.scan(self.path, "epoch")
        self.assertTrue(self.s.floor_pending)
        self.assertEqual(self.policy()[0], "UNKNOWN")
        self.assertNotIn("partial", status)
        # Serialized metrics survives controller restart including partial bytes.
        from dataclasses import asdict
        self.s = Metrics(**json.loads(json.dumps(asdict(self.s))))
        self.reader = Scanner(self.s)
        with self.path.open("ab") as f:
            f.write(raw[-7:])
        self.reader.scan(self.path, "epoch")
        self.assertEqual(self.s.floors[0]["input"], 50000)

    def test_floor_unknown_until_first_positive(self):
        self.append(event("context_compacted", 1), token(0, 2))
        self.assertTrue(self.s.floor_pending)
        self.assertEqual(self.s.floors, [])
        self.append(token(32000, 3), token(90000, 4))
        self.assertEqual(self.s.floors[0]["input"], 32000)
        self.assertEqual(self.s.current_input, 90000)

    def test_thresholds(self):
        for n, action in ((44799, "OK"), (44800, "WARN"), (47999, "WARN"), (48000, "REQUEST")):
            with self.subTest(n=n):
                self.s.floors = [{"input": n, "body": None}] * 2
                self.s.last_event = NOW
                self.s.source_verified = True
                self.assertEqual(self.policy()[0], action)
        self.assertEqual(self.policy(limit=16000)[0], "HOLD")

    def test_combined_triggers_preserve_strongest_action(self):
        # H495-2: uncertain task provenance weakens only the rate inference.
        for floor, task, size, action, reason in (
            (50000, None, 0, "REQUEST", "floor-reserve"),
            (50000, "task", 0, "REQUEST", "floor-reserve"),
            (45000, None, 0, "WARN", "floor-warning"),
            (45000, "task", 0, "REQUEST", "same-task-compaction-rate"),
            (10000, None, 150000000, "REQUEST", "rollout-size-maintenance"),
            (10000, "task", 150000000, "REQUEST", "same-task-compaction-rate"),
            (50000, None, 150000000, "REQUEST", "floor-reserve"),
            (50000, "task", 150000000, "REQUEST", "floor-reserve"),
        ):
            with self.subTest(floor=floor, task=task, size=size):
                self.s = Metrics("thread", "epoch", source_verified=True, last_event=NOW,
                                 floors=[{"input": floor, "body": None}] * 2,
                                 task=task, compactions=[NOW - 3, NOW - 2, NOW - 1],
                                 file_bytes=size, offset=size)
                self.assertEqual(self.policy(), (action, reason))
                self.assertEqual(self.policy(cooldown=True), ("HOLD", "trigger-during-cooldown"))
                self.assertEqual(self.policy(limit=16000), ("HOLD", "unusable-budget"))

    def test_unknown_limit_scope_epoch_and_body_accounting(self):
        self.floors(100000)
        self.assertEqual(self.policy(scope="UNKNOWN")[0], "OK")
        self.assertEqual(self.policy(limit=None)[0], "OK")
        self.assertEqual(self.policy(scope="body_after_prefix")[0], "OK")
        self.s.floors = [{"input": 100000, "body": 48000}] * 2
        self.assertEqual(self.policy(scope="body_after_prefix")[0], "REQUEST")
        self.assertEqual(decision(self.s, Accounting(64000, "total", "wrong"), now=NOW)[0], "OK")

    def test_unresolved_second_floor_never_zero(self):
        self.append(event("context_compacted", 1), token(50000, 2), event("context_compacted", 3))
        self.assertEqual(self.policy()[0], "OK")
        self.assertTrue(self.s.floor_pending)

    def test_missing_floor_breaks_consecutive_sequence_across_restart(self):
        from dataclasses import asdict
        self.append(event("context_compacted", 1), token(50000, 2),
                    event("context_compacted", 3))
        self.s = Metrics(**json.loads(json.dumps(asdict(self.s))))
        self.reader = Scanner(self.s)
        self.append(event("context_compacted", 4), token(50000, 5))
        self.assertEqual(self.policy(), ("WARN", "unknown-task-rate"))
        self.assertEqual(len(self.s.floors), 1)
        self.append(event("context_compacted", 6), token(50000, 7))
        self.assertEqual(self.policy(), ("REQUEST", "floor-reserve"))

    def test_duplicate_forms_and_repeated_scans(self):
        compact = event("context_compacted", 1)
        equivalent = {"type": "compacted", "timestamp": compact["timestamp"], "payload": {}}
        self.append(compact, equivalent, token(50000, 2), token(50000, 2))
        self.reader.scan(self.path, "epoch")
        self.assertEqual(self.s.total_compactions, 1)
        self.assertEqual(len(self.s.floors), 1)

    def test_rate_requires_same_known_task_and_no_completed_turn(self):
        self.append(event("task_started", 0, turn_id="t"), event("context_compacted", 1),
                    event("context_compacted", 2), event("context_compacted", 3))
        self.assertEqual(self.policy()[1], "same-task-compaction-rate")
        self.append(event("task_complete", 4))
        self.assertEqual(self.policy()[0], "OK")
        self.append(event("context_compacted", 5), event("context_compacted", 6), event("context_compacted", 7))
        self.assertEqual(self.policy()[1], "unknown-task-rate")

    def test_model_epoch_clears_floor_and_rate_not_warning_history(self):
        self.floors(50000)
        self.s.warning_sent = True
        self.reader.scan(self.path, "other-epoch")
        self.assertFalse(self.s.floors)
        self.assertFalse(self.s.compactions)
        self.assertTrue(self.s.warning_sent)

    def test_rotation_truncation_and_rewrite_skip_history(self):
        for mode in ("rotation", "truncate", "rewrite"):
            with self.subTest(mode=mode):
                self.path.unlink(missing_ok=True)
                self.s = Metrics("thread", "epoch")
                self.reader = Scanner(self.s)
                self.append(event("context_compacted", 1), token(50000, 2))
                self.s.warning_sent = True
                before = self.s.total_compactions
                if mode == "rotation":
                    self.path.rename(self.root / "old")
                    self.path.write_bytes(lines(event("context_compacted", 3)))
                elif mode == "truncate":
                    self.path.write_bytes(b"")
                else:
                    self.path.write_bytes(lines(event("context_compacted", 3), token(99000, 4)) + b" " * 200)
                self.reader.scan(self.path, "epoch")
                self.assertEqual(self.s.total_compactions, before)
                self.assertTrue(self.s.warning_sent)
                self.assertIn("rollout-replaced-or-truncated", self.s.degraded)
                self.assertFalse(self.s.floors)

    def test_large_compaction_across_scan_and_durable_restart(self):
        # Ordinary carried compaction text can exceed the old 64 KiB cap.
        # Force a scan boundary inside it and restore through the actual state reader.
        compact = {"type": "compacted", "timestamp": "2026-11-03T09:00:01Z",
                   "payload": {"message": "x" * 234000}}
        padding = event("item_completed", 0, text="x" * 80000)
        self.path.write_bytes(lines(meta(), padding, compact, token(50000, 2)))
        state = self.root / "metrics.json"
        first = observe(self.path, state, thread="thread", epoch="epoch", now=NOW)
        self.assertEqual(first["metrics"]["offset"], 262144)
        self.assertTrue(read_json(state)["metrics"]["partial"])
        self.assertNotIn("partial", first["metrics"])
        self.assertLess(state.stat().st_size, 1048576)
        second = observe(self.path, state, thread="thread", epoch="epoch", now=NOW)
        self.assertEqual(second["metrics"]["degraded"], [])
        self.assertEqual(second["metrics"]["total_compactions"], 1)
        self.assertEqual(second["metrics"]["floors"][0]["input"], 50000)
        self.assertEqual(second["metrics"]["offset"], self.path.stat().st_size)
        self.assertFalse(second["effects_enabled"])

    def test_streaming_transition_preserves_newline_boundary(self):
        compact = {"type": "compacted", "timestamp": "2026-11-03T09:00:01Z",
                   "payload": {"message": ""}}
        for extra in (0, 1):
            with self.subTest(extra=extra):
                compact["payload"]["message"] = ""
                compact["payload"]["message"] = "x" * (262144 - len(lines(compact)) + extra)
                self.path.write_bytes(lines(meta(), compact, token(50000, 2)))
                self.s = Metrics("thread", "epoch")
                reader = Scanner(self.s)
                while self.s.offset < self.path.stat().st_size:
                    before = self.s.offset
                    reader.scan(self.path, "epoch")
                    self.assertLessEqual(self.s.offset - before, 262144)
                self.assertEqual(self.s.degraded, [])
                self.assertEqual(self.s.total_compactions, 1)

    def test_stream_validator_survives_bytewise_utf8_and_escapes(self):
        from .large_record import feed
        values = [None, True, False, 123, -3.5e12, [], {},
                  {"text": "\u00e9\u2603\\\"\n", "nested": [True, None, {"a": 1}]}]
        for value in values:
            raw = (json.dumps({"payload": value}, ensure_ascii=False) + "\n").encode()
            state = {}
            for index, byte in enumerate(raw):
                feed(state, bytes([byte]), final=index == len(raw) - 1)
                state = json.loads(json.dumps(state))
            self.assertTrue(state["done"])
        for raw in (b'{"a":01}\n', b'{"a":[1,]}\n', b'{"a":"\\u12x4"}\n',
                    b'{"a":true false}\n', b'{"a":1,}\n'):
            with self.assertRaises(ValueError):
                feed({}, raw, final=True)

    def test_multimegabyte_records_survive_every_observer_restart(self):
        # Both ignored output and compaction text can grow beyond any full-record cap.
        compact = {"timestamp": "2026-11-03T09:00:01Z", "type": "compacted",
                   "payload": {"message": "x" * 2100000 + "é\\\""}}
        ignored = {"timestamp": "2026-11-03T09:00:00Z", "type": "response_item",
                   "payload": {"text": "x" * 550000}}
        self.path.write_bytes(lines(meta(), event("task_started", 0, turn_id="t"), ignored,
            compact, {**compact, "timestamp": "2026-11-03T09:00:02Z"},
            {**compact, "timestamp": "2026-11-03T09:00:03Z"}, token(50000, 4)))
        state = self.root / "metrics.json"
        prior, scans = 0, 0
        while prior < self.path.stat().st_size:
            result = observe(self.path, state, thread="thread", epoch="epoch", now=NOW)
            offset = result["metrics"]["offset"]
            self.assertLessEqual(offset - prior, 262144)
            self.assertLess(state.stat().st_size, 400000)
            self.assertNotIn("large", result["metrics"])
            prior, scans = offset, scans + 1
        self.assertGreater(scans, 20)
        self.assertEqual(result["metrics"]["degraded"], [])
        self.assertEqual(result["metrics"]["total_compactions"], 3)
        self.assertEqual((result["action"], result["reason"]), ("REQUEST", "same-task-compaction-rate"))
        self.assertEqual(len(result["notices"]), 1)
        again = observe(self.path, state, thread="thread", epoch="epoch", now=NOW)
        self.assertEqual(again["metrics"]["total_compactions"], 3)
        self.assertEqual(again["notices"], result["notices"])

    def test_large_ignored_event_keeps_compaction_rate_visible_across_restarts(self):
        ignored = event("item_completed", 0, text="x" * 300000)
        compact = {"type": "compacted", "timestamp": "2026-11-03T09:00:01Z",
                   "payload": {"message": "x" * 400000}}
        self.path.write_bytes(lines(meta(), event("task_started", 0, turn_id="t"), ignored,
            compact, {**compact, "timestamp": "2026-11-03T09:00:02Z"},
            {**compact, "timestamp": "2026-11-03T09:00:03Z"}, token(50000, 4)))
        state, prior = self.root / "metrics.json", 0
        while prior < self.path.stat().st_size:
            result = observe(self.path, state, thread="thread", epoch="epoch", now=NOW)
            offset = result["metrics"]["offset"]
            self.assertLessEqual(offset - prior, 262144)
            self.assertLess(state.stat().st_size, 400000)
            prior = offset
        self.assertEqual(result["metrics"]["degraded"], [])
        self.assertEqual(result["metrics"]["total_compactions"], 3)
        self.assertEqual((result["action"], result["reason"]),
                         ("REQUEST", "same-task-compaction-rate"))

    def test_large_relevant_or_ambiguous_event_stays_unknown(self):
        from .large_record import RELEVANT_TYPES
        rows = [lines(event(kind, 1, text="x" * 300000)) for kind in RELEVANT_TYPES]
        ignored = lines(event("item_completed", 1, text="x" * 300000))
        rows.extend([
            ignored[:-3] + b',"type":"task_complete"}}\n',
            ignored[:-3] + b',"t\\u0079pe":"task_complete"}}\n',
            lines({"type": "event_msg", "timestamp": "2026-11-03T09:00:01Z",
                   "payload": {"text": "x" * 300000, "type": "item_completed"}}),
        ])
        for index, raw in enumerate(rows):
            with self.subTest(index=index):
                self.path.write_bytes(lines(meta()) + raw)
                state = self.root / f"metrics-{index}.json"
                faults = self.root / f"faults-{index}.json"
                result = observe_bounded(self.path, state, faults, max_seconds=10,
                    thread="thread", epoch="epoch", now=NOW)
                self.assertEqual(result["action"], "UNKNOWN")
                self.assertIn("oversized-record", result["metrics"]["degraded"])
                self.assertEqual(len(result["fault_notices"]), 1)

    def test_ignored_event_key_guard_survives_bytewise_restarts(self):
        from .large_record import feed
        raw = lines(event("item_completed", 1, nested={"type": "task_complete"},
                          **{'key"with\\escapes': "ignored"}))
        state = {"guard_payload_type": True}
        for index, byte in enumerate(raw):
            feed(state, bytes([byte]), final=index == len(raw) - 1)
            state = json.loads(json.dumps(state))
        self.assertTrue(state["done"])
        self.assertTrue(state["payload_type_seen"])

    def test_large_record_fail_closed_and_waits_for_newline(self):
        good = lines({"type": "compacted", "timestamp": "2026-11-03T09:00:01Z",
                      "payload": {"message": "x" * 550000}})
        bad_rows = [good[:-3] + b'!}\n', good[:-1] + b',"type":"compacted"}\n',
                    good.replace(b'xxx', b'\xffxx', 1),
                    lines({"payload": {"message": "x" * 550000}, "type": "compacted",
                           "timestamp": "2026-11-03T09:00:01Z"})]
        for raw in bad_rows:
            with self.subTest(length=len(raw)):
                self.path.write_bytes(lines(meta()) + raw)
                s = Metrics("thread", "epoch")
                while s.offset < self.path.stat().st_size:
                    Scanner(s).scan(self.path, "epoch")
                self.assertIn("oversized-record", s.degraded)
                self.assertEqual(s.total_compactions, 0)
        self.path.write_bytes(lines(meta()) + good[:-1])
        s = Metrics("thread", "epoch")
        while s.offset < self.path.stat().st_size:
            Scanner(s).scan(self.path, "epoch")
        self.assertEqual(s.total_compactions, 0)
        self.assertEqual(decision(s, Accounting(), now=NOW)[1], "incomplete-record")
        with self.path.open("ab") as stream:
            stream.write(b'\n')
        Scanner(s).scan(self.path, "epoch")
        self.assertEqual(s.total_compactions, 1)

    def test_bounded_caller_catchup_fault_outbox_and_idle(self):
        state, faults = self.root / "metrics.json", self.root / "faults.json"
        args = dict(thread="thread", epoch="epoch", now=NOW)
        self.assertEqual(observe_bounded(self.path, state, faults, **args)["fault_notices"], [])
        self.assertFalse(faults.exists())
        self.path.write_bytes(lines(meta(), {"type": "compacted", "timestamp": "2026-11-03T09:00:01Z",
                                             "payload": {"message": "x" * 550000}}))
        first = observe_bounded(self.path, state, faults, max_scans=1, **args)
        self.assertEqual(first["reason"], "scan-backlog")
        result = observe_bounded(self.path, state, faults, max_seconds=10, **args)
        self.assertEqual(result["metrics"]["total_compactions"], 1)
        self.assertEqual(result["fault_notices"], [])
        lock = state.with_suffix(".json.lock")
        lock.write_text("owned")
        for _ in range(2):
            result = observe_bounded(self.path, state, faults, **args)
            self.assertEqual(len(result["fault_notices"]), 1)
            self.assertEqual(result["fault_notices"][0]["reason"], "observer-error")
            self.assertEqual(lock.read_text(), "owned")
        lock.unlink()
        with self.path.open("ab") as stream:
            stream.write(b'not-json\n')
        for _ in range(2):
            result = observe_bounded(self.path, state, faults, **args)
            self.assertEqual(len(result["fault_notices"]), 2)
            self.assertEqual(result["fault_notices"][-1]["reason"], "degraded:malformed-record")
        with patch.object(observe_module, "atomic_json", side_effect=OSError):
            with self.assertRaises(OSError):
                observe_bounded(self.path, state, self.root / "cannot-commit.json", **args)

    def test_missing_rollout_preserves_pending_fault_notices(self):
        state, faults = self.root / "metrics.json", self.root / "faults.json"
        args = dict(thread="thread", epoch="epoch", now=NOW)
        self.path.write_bytes(lines(meta()))
        lock = state.with_suffix(".json.lock")
        lock.write_text("owned")
        first = observe_bounded(self.path, state, faults, **args)
        self.assertEqual(len(first["fault_notices"]), 1)
        before = faults.read_bytes()
        self.path.unlink()
        for _ in range(2):
            result = observe_bounded(self.path, state, faults, **args)
            self.assertEqual(result["action"], "UNKNOWN")
            self.assertEqual(result["fault_notices"], first["fault_notices"])
            self.assertEqual(faults.read_bytes(), before)
        self.assertEqual(lock.read_text(), "owned")

    def test_disappeared_bound_rollout_persists_one_missing_fault(self):
        state, faults = self.root / "metrics.json", self.root / "faults.json"
        args = dict(thread="thread", epoch="epoch", now=NOW)
        self.path.write_bytes(lines(meta()))
        observe_bounded(self.path, state, faults, **args)
        self.assertTrue(state.exists())
        before = state.read_bytes()
        self.path.unlink()
        first = observe_bounded(self.path, state, faults, **args)
        self.assertEqual(first["action"], "UNKNOWN")
        self.assertEqual(first["scans"], 0)
        self.assertEqual(len(first["fault_notices"]), 1)
        self.assertEqual(first["fault_notices"][0]["reason"], "missing-telemetry")
        saved = faults.read_bytes()
        again = observe_bounded(self.path, state, faults, **args)
        self.assertEqual(again["fault_notices"], first["fault_notices"])
        self.assertEqual(faults.read_bytes(), saved)
        self.assertEqual(state.read_bytes(), before)
        with patch.object(observe_module, "atomic_json", side_effect=OSError):
            with self.assertRaises(OSError):
                observe_bounded(self.path, state, self.root / "cannot-commit-missing.json", **args)

    def test_invalid_metrics_root_persists_observer_fault(self):
        state, faults = self.root / "metrics.json", self.root / "faults.json"
        args = dict(thread="thread", epoch="epoch", now=NOW)
        self.path.write_bytes(lines(meta()))
        for malformed in ([], None, 1, "bad-root"):
            with self.subTest(malformed=malformed):
                state.write_text(json.dumps(malformed), encoding="utf-8")
                before = state.read_bytes()
                for _ in range(2):
                    result = observe_bounded(self.path, state, faults, **args)
                    self.assertEqual(result["action"], "HOLD")
                    self.assertEqual(result["reason"], "observer-error")
                    self.assertEqual(len(result["fault_notices"]), 1)
                    self.assertEqual(state.read_bytes(), before)

    def test_malformed_metrics_field_persists_observer_fault(self):
        state, faults = self.root / "metrics.json", self.root / "faults.json"
        args = dict(thread="thread", epoch="epoch", now=NOW)
        self.path.write_bytes(lines(meta()))
        observe(self.path, state, **args)
        data = read_json(state)
        data["metrics"]["floors"] = "x"
        state.write_text(json.dumps(data), encoding="utf-8")
        before = state.read_bytes()
        for _ in range(2):
            result = observe_bounded(self.path, state, faults, **{**args, "epoch": "new-epoch"})
            self.assertEqual((result["action"], result["reason"]), ("HOLD", "observer-error"))
            self.assertEqual(len(result["fault_notices"]), 1)
            self.assertEqual(result["fault_notices"][0]["reason"], "observer-error")
            self.assertEqual(state.read_bytes(), before)
        for error in (KeyboardInterrupt, SystemExit):
            with patch.object(observe_module, "observe", side_effect=error):
                with self.assertRaises(error):
                    observe_bounded(self.path, state, faults, **args)
        with patch.object(observe_module, "atomic_json", side_effect=OSError):
            with self.assertRaises(OSError):
                observe_bounded(self.path, state, self.root / "cannot-commit-field.json",
                                **{**args, "epoch": "new-epoch"})

    def test_malformed_oversized_and_bounded_scan(self):
        self.path.write_bytes(b"x" * 120 + b"\n" + b"not-json\n")
        reader = Scanner(self.s, scan_bytes=64, record_bytes=32)
        for _ in range(3):
            prior = self.s.offset
            reader.scan(self.path, "epoch")
            self.assertLessEqual(self.s.offset - prior, 64)
        self.assertIn("oversized-record", self.s.degraded)
        self.assertIn("malformed-record", self.s.degraded)
        self.assertLessEqual(len(self.s.partial), 44)

    def test_clock_rollback_missing_stale_and_size(self):
        self.append(event("context_compacted", 9), event("context_compacted", 1))
        self.assertIn("clock-rollback", self.s.degraded)
        self.s = Metrics("t", "e", last_event=NOW - 301, source_verified=True)
        self.assertEqual(decision(self.s, Accounting(), now=NOW)[0], "UNKNOWN")
        self.s.last_event = NOW
        self.s.file_bytes = self.s.offset = 150000000
        self.assertEqual(decision(self.s, Accounting(), now=NOW)[1], "rollout-size-maintenance")
        self.assertEqual(decision(self.s, Accounting(), now=NOW, cooldown=True)[0], "HOLD")

    def test_durable_notices_survive_restart_and_no_repeated_emission(self):
        self.path.write_bytes(lines(meta(), event("task_started", 0, turn_id="t"),
            event("context_compacted", 1), event("context_compacted", 2), event("context_compacted", 3)))
        args = dict(thread="thread", epoch="epoch", now=NOW)
        state = self.root / "metrics.json"
        first = observe(self.path, state, **args)
        second = observe(self.path, state, **args)
        self.assertEqual(first["action"], "REQUEST")
        self.assertEqual(second["action"], "OK")
        self.assertEqual(first["notices"], second["notices"])
        with self.assertRaises(Hold):
            observe(self.path, state, **{**args, "thread": "other"})

    def test_deep_record_is_durable_unknown_across_observer_restart(self):
        # H495-1: a byte-bounded record can still exhaust the JSON decoder stack.
        deep = b'{"type":"unrelated","unused":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}\n'
        self.assertLess(len(deep), 65536)
        for existing in (False, True):
            with self.subTest(existing=existing):
                state = self.root / f"metrics-{existing}.json"
                self.path.write_bytes(lines(meta()))
                args = dict(thread="thread", epoch="epoch", now=NOW,
                            accounting=Accounting(64000, "total", "epoch"))
                notices = []
                if existing:
                    with self.path.open("ab") as stream:
                        stream.write(lines(event("context_compacted", 1), token(50000, 2),
                                           event("context_compacted", 3), token(50000, 4)))
                    first = observe(self.path, state, **args)
                    self.assertEqual(first["action"], "REQUEST")
                    notices = first["notices"]
                with self.path.open("ab") as stream:
                    stream.write(deep)
                for _ in range(2):
                    result = observe(self.path, state, **args)
                    self.assertEqual((result["action"], result["reason"]), ("UNKNOWN", "malformed-record"))
                    saved = read_json(state)
                    self.assertEqual(saved["metrics"]["offset"], self.path.stat().st_size)
                    self.assertEqual(saved["metrics"]["partial"], "")
                    self.assertEqual(saved["metrics"]["request_sent"], existing)
                    self.assertEqual(saved["metrics"]["warning_sent"], existing)
                    self.assertEqual(saved["notices"], notices)
                with self.path.open("ab") as stream:
                    stream.write(lines(token(12345, 5)))
                result = observe(self.path, state, **args)
                self.assertEqual(result["action"], "UNKNOWN")  # epoch remains degraded
                self.assertEqual(result["metrics"]["current_input"], 12345)
                self.assertEqual(result["notices"], notices)

    def test_json_resource_errors_are_contained_without_swallowing_termination(self):
        for error in (RecursionError, MemoryError):
            with self.subTest(error=error.__name__):
                state = Metrics("thread", "epoch", warning_sent=True)
                with patch("tools.postbox.handover.metrics.json.loads", side_effect=error):
                    Scanner(state).record(b"{}\n")
                self.assertEqual(state.degraded, ["malformed-record"])
                self.assertTrue(state.warning_sent)
                path = self.root / "state.json"
                path.write_bytes(b"{}")
                with patch("tools.postbox.handover.protocol.json.loads", side_effect=error):
                    with self.assertRaises(Hold):
                        read_json(path)
                self.assertEqual(path.read_bytes(), b"{}")
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error=error.__name__):
                with patch("tools.postbox.handover.metrics.json.loads", side_effect=error):
                    with self.assertRaises(error):
                        Scanner(Metrics("thread", "epoch")).record(b"{}\n")

    def test_deep_state_is_held_and_preserved(self):
        path = self.root / "state.json"
        raw = b'{"unused":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}'
        path.write_bytes(raw)
        self.path.write_bytes(lines(meta()))
        with self.assertRaises(Hold):
            observe(self.path, path, thread="thread", epoch="epoch", now=NOW)
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(path.with_suffix(".json.lock").exists())


def manifest():
    row = {key: [] for key in SECTIONS}
    row.update(schema=1, ready=True, seat="fake", home="/fake-home", old_thread="old", generation=1,
               goal={"state": "paused"}, operator={"objective": "fixture", "restrictions": ["no live effects"]},
               paths={"state_path": "/fake/identity.json"},
               messages=[{"key": "msg/fake", "version": 2, "result": "not handled"}],
               open_work=[{"path": "/fake/canonical-ledger", "sha256": "a" * 64, "count": 190}],
               resume_text={"bootstrap_sha256": hashlib.sha256(b"bootstrap").hexdigest(),
                            "selected_sha256": hashlib.sha256(b"selected").hexdigest()})
    row["completeness"] = {key: {"count": len(row[key]), "sha256": digest(row[key])} for key in SECTIONS}
    return row


class CheckpointTests(TempTest):
    def test_immutable_content_addressed_checkpoint_and_paused_goal(self):
        row = manifest()
        path, sha = save_checkpoint(self.root, row, "bootstrap", "selected")
        self.assertEqual(path.stem, sha)
        self.assertEqual(read_json(path)["manifest"]["goal"]["state"], "paused")
        self.assertEqual(save_checkpoint(self.root, row, "bootstrap", "selected")[1], sha)

    def test_all_sections_and_indexes_required(self):
        for key in SECTIONS:
            row = manifest()
            del row[key]
            with self.subTest(key=key), self.assertRaises(Hold):
                checkpoint(row, "bootstrap", "selected")
        row = manifest()
        row["open_work"] = []  # silent backlog drop
        with self.assertRaises(Hold):
            checkpoint(row, "bootstrap", "selected")

    def test_message_version_and_bounded_bootstrap(self):
        row = manifest()
        del row["messages"][0]["version"]
        row["completeness"]["messages"]["sha256"] = digest(row["messages"])
        with self.assertRaises(Hold):
            checkpoint(row, "bootstrap", "selected")
        for a, b in (("x" * 6001, ""), ("x" * 6000, "y" * 18001)):
            with self.assertRaises(Hold):
                checkpoint(manifest(), a, b)


class SimulatedCrash(BaseException):
    pass


class FakeAdapter:
    """Trusted test verifier; never a production adapter or qualification record."""
    def __init__(self):
        self.effects = []
        self.crash_at = None
        self.mutate = lambda phase, receipt: None
        self.ordinary_old = True
        self.ordinary_new = False
        self.queue = {("msg/old-native-only", 1): "pending", ("msg/banked", 2): "held"}
        self.offset = 17
        self.route = "old"

    def missing_capabilities(self, row):
        return []

    def plan(self, row, phase):
        return {"ownership": row["transaction"], "phase": phase, "shared_daemon_stop": False}

    def perform(self, row, phase):
        self.effects.append(phase)
        # Mail and recall can arrive at EVERY boundary and must survive.
        self.queue[("msg/arrival/" + phase, 1)] = "pending"
        self.queue[("msg/banked", 2)] = "recalled"
        if phase == "OLD_QUIESCED":
            self.ordinary_old = False
        if phase == "ROUTE_COMMITTED":
            self.route = "new"
        if phase == "SUCCESSOR_ACTIVE":
            self.ordinary_new = True
        if phase == self.crash_at:
            raise SimulatedCrash()
        receipt = {"transaction": row["transaction"], "generation": row["generation"],
                   "seat": row["seat"], "home": row["home"], "old_thread": row["old_thread"],
                   "checkpoint": row["checkpoint"], "new_thread": row["new_thread"] or "new"}
        keys = {
            "CHECKPOINTED": "manifest_complete authority_current resume_dependency_qualified bootstrap_budget_qualified",
            "OLD_QUIESCED": "old_tools_fenced old_turns_fenced old_mail_fenced children_settled external_operations_settled unknown_sends_resolved",
            "SUCCESSOR_STANDBY": "fresh_thread same_policy standby_enforced owned_process",
            "ROUTE_COMMITTED": "intake_stopped_verified old_route_cas all_consumers_inventoried native_queue_reconciled queue_versions_preserved offsets_preserved held_preserved one_intake_running loaded_route_verified",
            "DELIVERY_PROVEN": "sender_distinct_controlled authenticated_reply routing_verified new_rollout_consumed ack_ledger_recorded nonce_not_replayed deadline_valid response_contract_verified",
            "SUCCESSOR_ACTIVE": "old_fence_rechecked route_rechecked proof_rechecked lease_cas native_queue_reconciled ordinary_work_released",
            "OLD_RETIRED": "owned_old_only shared_daemon_preserved receipt_archived",
        }
        receipt.update({k: True for k in keys[phase].split()})
        receipt.update(digest="a" * 64, route_digest="b" * 64, nonce=row["nonce"],
                       linked_reply_key="msg/fake-reply", request_key="msg/fake-request")
        self.mutate(phase, receipt)
        return receipt

    def verify(self, row, phase, receipt):
        # Only this fixture's owned effects qualify; a caller's forged flags do not.
        return self.effects[-1] == phase and (not self.ordinary_new or not self.ordinary_old)


class JournalTests(TempTest):
    def begin(self, root=None):
        journal = Journal(root or self.root)
        journal.begin(seat="fake", home="/fake-home", old_thread="old", generation=1,
                      expected_route="old-hash", queue_inventory={"native": "must-inventory"},
                      now=NOW, boot_id="boot", monotonic=10)
        return journal

    def step(self, journal, fake, **kwargs):
        return journal.step(fake, **dict(expected_generation=1, boot_id="boot", monotonic=20, now=NOW, **kwargs))

    def test_happy_path_mail_recall_native_only_and_shared_daemon(self):
        j, fake = self.begin(), FakeAdapter()
        for phase in PHASES[1:]:
            row = self.step(j, fake)
            self.assertEqual(row["phase"], phase)
            self.assertFalse(fake.ordinary_old and fake.ordinary_new)
            self.assertEqual(fake.offset, 17)
        self.assertEqual(len(fake.queue), 9)
        self.assertIn(("msg/old-native-only", 1), fake.queue)
        self.assertEqual(fake.queue[("msg/banked", 2)], "recalled")
        self.assertEqual(row["cooldown_until"], NOW + 1800)
        self.assertTrue(row["receipts"]["OLD_RETIRED"]["shared_daemon_preserved"])
        self.step(j, fake)
        self.assertEqual(len(fake.effects), 7)  # terminal replay has no effects

    def test_crash_after_every_effect_never_relaunches_or_reactivates(self):
        for boundary in PHASES[1:]:
            with self.subTest(boundary=boundary):
                j, fake = self.begin(self.root / boundary), FakeAdapter()
                fake.crash_at = boundary
                with self.assertRaises(SimulatedCrash):
                    for _ in range(7):
                        self.step(j, fake)
                before = list(fake.effects)
                self.assertEqual(j.load()["inflight"]["phase"], boundary)
                with self.assertRaises(Hold):
                    self.step(j, fake)
                self.assertEqual(fake.effects, before)
                self.assertLessEqual(before.count("SUCCESSOR_STANDBY"), 1)
                self.assertFalse(fake.ordinary_old and fake.ordinary_new)

    def test_bad_proofs_cannot_advance(self):
        cases = [
            ("OLD_QUIESCED", "children_settled", False),
            ("OLD_QUIESCED", "external_operations_settled", False),
            ("OLD_QUIESCED", "unknown_sends_resolved", False),
            ("CHECKPOINTED", "resume_dependency_qualified", False),
            ("SUCCESSOR_STANDBY", "standby_enforced", False),
            ("SUCCESSOR_STANDBY", "new_thread", "old"),
            ("ROUTE_COMMITTED", "loaded_route_verified", False),
            ("ROUTE_COMMITTED", "native_queue_reconciled", False),
            ("ROUTE_COMMITTED", "queue_versions_preserved", False),
            ("DELIVERY_PROVEN", "new_rollout_consumed", False),  # queue rc0 insufficient
            ("DELIVERY_PROVEN", "sender_distinct_controlled", False),
            ("DELIVERY_PROVEN", "authenticated_reply", False),
            ("DELIVERY_PROVEN", "authenticated_reply", "true"),
            ("DELIVERY_PROVEN", "linked_reply_key", True),
            ("DELIVERY_PROVEN", "nonce_not_replayed", False),
            ("DELIVERY_PROVEN", "deadline_valid", False),
            ("DELIVERY_PROVEN", "nonce", "forged"),
            ("DELIVERY_PROVEN", "checkpoint", "stale"),
            ("SUCCESSOR_ACTIVE", "lease_cas", False),
            ("OLD_RETIRED", "shared_daemon_preserved", False),
        ]
        for n, (phase, key, value) in enumerate(cases):
            with self.subTest(phase=phase, key=key):
                j, fake = self.begin(self.root / str(n)), FakeAdapter()
                fake.mutate = lambda p, receipt: receipt.update({key: value}) if p == phase else None
                with self.assertRaises(Hold):
                    for _ in range(7):
                        self.step(j, fake)
                self.assertEqual(j.load()["phase"], "HOLD")
                self.assertEqual(j.load()["inflight"]["phase"], phase)

    def test_generation_duplicate_lock_clock_deadline_and_cooldown(self):
        j, fake = self.begin(), FakeAdapter()
        with self.assertRaises(Hold):
            j.begin(seat="fake", home="h", old_thread="old", generation=2, expected_route="x",
                    queue_inventory={}, now=NOW, boot_id="boot", monotonic=10)
        with exclusive(self.root / ".controller.lock"), self.assertRaises(Hold):
            self.step(j, fake)
        with self.assertRaises(Hold):
            j.step(fake, expected_generation=2, boot_id="boot", monotonic=20, now=NOW)
        with self.assertRaises(Hold):
            j.step(fake, expected_generation=1, boot_id="new-boot", monotonic=20, now=NOW)
        j = self.begin(self.root / "deadline")
        with self.assertRaises(Hold):
            j.step(fake, expected_generation=1, boot_id="boot", monotonic=311, now=NOW)
        with self.assertRaises(Hold):
            Journal(self.root / "cooldown").begin(seat="s", home="h", old_thread="o", generation=2,
                expected_route="x", queue_inventory={}, now=NOW, boot_id="boot", monotonic=1,
                cooldown_until=NOW + 1)
        self.assertFalse(fake.effects)

    def test_ready_deadline_includes_checkpoint_verification_time(self):
        j, fake = self.begin(), FakeAdapter()
        with patch("tools.postbox.handover.protocol.time.monotonic", side_effect=[10, 301]):
            with self.assertRaises(Hold):
                self.step(j, fake)
        self.assertEqual(j.load()["phase"], "HOLD")
        self.assertIsNone(j.load()["checkpoint"])
        self.assertEqual(j.load()["inflight"]["phase"], "CHECKPOINTED")

    def test_all_live_platforms_fail_closed_without_effects(self):
        for name in PLATFORMS:
            adapter = InstalledAdapter(name)
            self.assertEqual(adapter.missing_capabilities(), list(REQUIRED))
            self.assertFalse(adapter.status()["effects_enabled"])
            j = self.begin(self.root / name)
            with self.assertRaises(Hold):
                self.step(j, adapter)
            self.assertIsNone(j.load()["inflight"])
        self.assertNotIn("bank.consumer", PLATFORMS["hz-builder"]["route_fields"])


class RoutePlanTests(unittest.TestCase):
    def snapshot(self, platform):
        names = set(PLATFORMS[platform]["route_fields"]) - {"other-inventoried-consumers"}
        return {"inventory_complete": True, "thread": "old", "generation": 1,
                "routes": {name: {"thread": "old", "unknown-setting": "retained"} for name in names},
                "offsets": {"spool": 17}, "unknown-setting": {"keep": True},
                "queues": {"native": [{"key": "native-only", "version": 1, "status": "pending"}],
                           "installed": [{"key": "banked", "version": 2, "status": "held"}]}}

    def test_five_platforms_preserve_all_queue_and_unknown_fields(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                before = self.snapshot(platform)
                original = deepcopy(before)
                plan = plan_route(platform, before, expected_digest=digest(before), new_thread="new")
                after = reconcile_snapshot(plan, before)
                self.assertEqual(before, original)
                self.assertEqual(after["offsets"], before["offsets"])
                self.assertEqual(after["queues"], before["queues"])
                self.assertEqual(after["unknown-setting"], before["unknown-setting"])
                self.assertEqual(after["generation"], 2)
                self.assertTrue(all(v["thread"] == "new" for v in after["routes"].values()))
                self.assertFalse(plan["effects_enabled"])

    def test_cas_arrivals_recalls_missing_consumer_and_bank_mismatch_hold(self):
        before = self.snapshot("hqd")
        plan = plan_route("hqd", before, expected_digest=digest(before), new_thread="new")
        for change in ("arrival", "recall", "offset", "consumer"):
            changed = deepcopy(before)
            if change == "arrival":
                changed["queues"]["native"].append({"key": "new", "version": 1})
            elif change == "recall":
                changed["queues"]["installed"][0]["status"] = "recalled"
            elif change == "offset":
                changed["offsets"]["spool"] += 1
            else:
                changed["routes"]["bank.consumer"]["thread"] = "different"
            with self.subTest(change=change), self.assertRaises(Hold):
                reconcile_snapshot(plan, changed)
        del before["routes"]["default-launch-pointer"]
        with self.assertRaises(Hold):
            plan_route("hqd", before, expected_digest=digest(before), new_thread="new")
        before = self.snapshot("hz-builder")
        before["routes"]["bank.consumer"] = {"thread": "old"}
        with self.assertRaises(Hold):
            plan_route("hz-builder", before, expected_digest=digest(before), new_thread="new")


if __name__ == "__main__":
    unittest.main()
