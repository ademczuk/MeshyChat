"""Synthetic local crash/lost-reply tests; no graph or authentication claims."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from frozen_outbox import FrozenOutbox, Record, Store, StoreConflict, StoreError


SOURCE, GENERATION, PARSER = "1" * 64, "2" * 64, "synthetic.outbox:1"
EVIDENCE = "a" * 64  # Synthetic stand-in, NEVER an authenticated receipt.
ARTIFACT = b"synthetic verification evidence"
ARTIFACT_DIGEST = hashlib.sha256(ARTIFACT).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def bundle(batch=1, first=0, count=1):
    coordinates = dict(seat="synthetic-seat", src="3" * 32, gen=1, batch=batch)
    prefix = f"{coordinates['seat']}/{coordinates['src']}/0001/{batch:06d}"
    items = []
    for ordinal in range(first, first + count):
        text = f"Synthetic café item {ordinal}"
        value = dict(schema="context.v1", kind="item", **coordinates,
                     ordinal=ordinal, item="user_text", role="user",
                     dialect="codex", adapter_version="1.0",
                     time="2026-10-19T00:00:00.000Z", text=text,
                     text_sha256=hashlib.sha256(text.encode()).hexdigest())
        items.append(Record(f"ctx/{prefix}/{ordinal:010d}", encoded(value)))
    manifest = dict(schema="context.v1", kind="manifest", **coordinates,
                    first=first, last=first + count - 1,
                    items=[[json.loads(r.value)["ordinal"], json.loads(r.value)["text_sha256"]] for r in items],
                    counts=dict(written=count, omitted_secret=0, omitted_client_name=0, omitted_too_large=0))
    return tuple(items), Record(f"ctxm/{prefix}", encoded(manifest))


def omission(item, manifest, reason="secret", position=0):
    old = json.loads(item.value)
    value = {k: old[k] for k in ("schema", "kind", "seat", "src", "gen", "batch", "ordinal",
                                "dialect", "adapter_version", "time")}
    value.update(item="omitted", omitted_reason=reason)
    summary = json.loads(manifest.value)
    summary["items"][position][1] = None
    summary["counts"]["written"] -= 1
    summary["counts"]["omitted_" + reason] += 1
    return replace(item, value=encoded(value)), replace(manifest, value=encoded(summary))


def child(folder, operation, point, batch_key):
    store = Store(folder)
    outbox = FrozenOutbox(store)
    def crash(boundary):
        if boundary == point:
            os._exit(73)
    if operation == "freeze":
        snapshot = store.resume(SOURCE, GENERATION, PARSER)
        outbox.freeze(snapshot, 100, *bundle(), {"next": 1}, boundary=crash)
    elif operation == "send":
        outbox.begin_send(batch_key, boundary=crash)
    elif operation == "replace":
        action = outbox.next_action(batch_key)
        item, manifest = omission(action.record, bundle()[1])
        outbox.replace_refused(action.attempt, code="E_CONTEXT_SECRET", item=item, manifest=manifest,
                              evidence_sha256=EVIDENCE, boundary=crash)
    elif operation == "observe-evidence":
        action = outbox.next_action(batch_key)
        outbox.observe(action.attempt, value=action.record.value,
                       evidence_sha256=ARTIFACT_DIGEST, evidence_artifact=ARTIFACT,
                       boundary=crash)
    else:
        action = outbox.next_action(batch_key)
        outbox.observe(action.attempt, value=action.record.value,
                       evidence_sha256=EVIDENCE, boundary=crash)
    return 0


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "private"
        self.store = Store(self.directory)
        self.snapshot = self.store.enroll(SOURCE, GENERATION, PARSER, {}, {"next": 0})
        self.outbox = FrozenOutbox(self.store)

    def freeze(self, *, count=1):
        return self.outbox.freeze(self.snapshot, 100, *bundle(count=count), {"next": count})

    def match_next(self, key):
        action = self.outbox.begin_send(key)
        self.assertEqual("read", self.outbox.next_action(key).kind)
        self.assertEqual("matched", self.outbox.observe(
            action.attempt, value=action.record.value, evidence_sha256=EVIDENCE))
        return action

    def crash(self, operation, point, key="unused"):
        result = subprocess.run([sys.executable, str(Path(__file__).absolute()), "--child",
                                 str(self.directory), operation, point, key],
                                capture_output=True, timeout=25)
        self.assertEqual(73, result.returncode, result.stderr.decode(errors="replace"))
        self.store = Store(self.directory)
        self.outbox = FrozenOutbox(self.store)

    def test_freeze_replay_preserves_every_byte_and_checkpoint(self):
        first = self.freeze()
        self.assertEqual(first, self.freeze())
        action = self.outbox.next_action(first.key)
        self.assertEqual(bundle()[0][0], action.record)
        self.assertIn("café".encode(), action.record.value)
        state = self.store.resume(SOURCE, GENERATION, PARSER)
        self.assertEqual((100, 1, {"next": 1}), (state.offset, state.revision, state.parser_state()))

    def test_freeze_requires_snapshot_without_advancing_source(self):
        with self.assertRaises(StoreError):
            self.outbox.freeze(None, 100, *bundle(), {"next": 1})
        self.assertEqual(self.snapshot, self.store.resume(SOURCE, GENERATION, PARSER))

    def test_wire_numbers_reject_padded_strings_booleans_and_floats(self):
        items, manifest = bundle()
        for field in ("gen", "batch", "ordinal"):
            for bad in (str(json.loads(items[0].value)[field]), True, False, 0.0, 1.0):
                with self.subTest(field=field, bad=bad):
                    value = json.loads(items[0].value)
                    value[field] = bad
                    with self.assertRaises(StoreError):
                        self.outbox.freeze(self.snapshot, 100,
                                           (replace(items[0], value=encoded(value)),), manifest, {})
        for field in ("gen", "batch", "first", "last", "items"):
            for bad in ("0", True, False, 0.0, 1.0):
                with self.subTest(manifest_field=field, bad=bad):
                    value = json.loads(manifest.value)
                    if field == "items":
                        value[field][0][0] = bad
                    else:
                        value[field] = bad
                    with self.assertRaises(StoreError):
                        self.outbox.freeze(self.snapshot, 100, items,
                                           replace(manifest, value=encoded(value)), {})
        self.assertEqual(self.snapshot, self.store.resume(SOURCE, GENERATION, PARSER))

    def test_preexisting_string_wire_records_fail_closed_without_rewriting(self):
        import base64
        from frozen_outbox import FORMAT
        items, manifest = bundle()
        records = []
        for record in items + (manifest,):
            value = json.loads(record.value)
            value.update(gen="0001", batch="000001")
            if value["kind"] == "item":
                value["ordinal"] = "0000000000"
            else:
                value.update(first="0000000000", last="0000000000")
                value["items"][0][0] = "0000000000"
            records.append([record.key, base64.b64encode(encoded(value)).decode("ascii")])
        output = encoded(dict(schema=FORMAT, records=records))
        batch = self.store.commit(self.snapshot, 100, output, b"[]", {})
        before = self.store.committed(batch.key)
        with self.assertRaises(StoreConflict):
            FrozenOutbox(Store(self.directory)).next_action(batch.key)
        self.assertEqual(before, self.store.committed(batch.key))

    def test_freeze_rollback_before_output(self):
        self.crash("freeze", "before_output")
        self.assertEqual(self.snapshot, self.store.resume(SOURCE, GENERATION, PARSER))
        self.assertEqual((), self.store.batches(SOURCE, GENERATION, PARSER).items)

    def test_freeze_rollback_after_output(self):
        self.crash("freeze", "after_output")
        self.assertEqual(self.snapshot, self.store.resume(SOURCE, GENERATION, PARSER))
        self.assertEqual((), self.store.batches(SOURCE, GENERATION, PARSER).items)

    def test_freeze_rollback_after_state(self):
        self.crash("freeze", "after_state")
        self.assertEqual(self.snapshot, self.store.resume(SOURCE, GENERATION, PARSER))
        self.assertEqual((), self.store.batches(SOURCE, GENERATION, PARSER).items)

    def test_freeze_crash_after_commit_is_replayable(self):
        self.crash("freeze", "after_commit")
        state = self.store.resume(SOURCE, GENERATION, PARSER)
        self.assertEqual((100, 1), (state.offset, state.revision))
        self.assertEqual(bundle()[0][0], self.outbox.next_action(state.batch).record)
        self.assertEqual(state.batch, self.freeze().key)

    def test_send_crash_before_commit_has_no_attempt(self):
        batch = self.freeze()
        self.crash("send", "before_attempt_commit", batch.key)
        self.assertEqual("send", self.outbox.next_action(batch.key).kind)

    def test_send_crash_after_commit_requires_read_not_another_send(self):
        batch = self.freeze()
        self.crash("send", "after_attempt_commit", batch.key)
        self.assertEqual("read", self.outbox.next_action(batch.key).kind)
        with self.assertRaises(StoreConflict):
            self.outbox.begin_send(batch.key)

    def test_lost_reply_then_readback_allows_manifest_only_after_items(self):
        batch = self.freeze(count=2)
        sent = self.outbox.begin_send(batch.key)
        resumed = FrozenOutbox(Store(self.directory)).next_action(batch.key)
        self.assertEqual(("read", sent.record, sent.attempt), (resumed.kind, resumed.record, resumed.attempt))
        self.outbox.observe(resumed.attempt, value=resumed.record.value, evidence_sha256=EVIDENCE)
        second = self.match_next(batch.key)
        self.assertTrue(second.record.key.endswith("0000000001"))
        manifest = self.outbox.next_action(batch.key)
        self.assertTrue(manifest.record.key.startswith("ctxm/"))
        self.assertNotEqual("complete", manifest.kind)
        self.match_next(batch.key)
        self.assertEqual("complete", self.outbox.next_action(batch.key).kind)

    def test_missing_read_is_not_receipt_and_retry_uses_same_bytes(self):
        batch = self.freeze()
        first = self.outbox.begin_send(batch.key)
        self.assertEqual("ready", self.outbox.observe(first.attempt, value=None, evidence_sha256=EVIDENCE))
        second = self.outbox.begin_send(batch.key)
        self.assertEqual(first.record, second.record)
        self.assertNotEqual(first.attempt.token, second.attempt.token)
        with self.assertRaises(StoreConflict):
            self.outbox.observe(first.attempt, value=first.record.value, evidence_sha256=EVIDENCE)
        self.assertEqual("read", self.outbox.next_action(batch.key).kind)

    def test_conflict_is_durable_and_never_overwrites(self):
        batch = self.freeze()
        action = self.outbox.begin_send(batch.key)
        self.assertEqual("conflict", self.outbox.observe(action.attempt, value=b"other", evidence_sha256=EVIDENCE))
        self.assertEqual("review_conflict", FrozenOutbox(Store(self.directory)).next_action(batch.key).kind)
        with self.assertRaises(StoreConflict):
            self.outbox.begin_send(batch.key)

    def test_reordered_readback_completes_without_changing_frozen_bytes(self):
        batch = self.freeze(count=2)
        original = self.store.committed(batch.key)
        checkpoint = self.store.resume(SOURCE, GENERATION, PARSER)

        def reorder(value):
            if type(value) is dict:
                return {key: reorder(child) for key, child in reversed(list(value.items()))}
            if type(value) is list:
                return [reorder(child) for child in value]
            return value

        for _ in range(3):
            sent = self.outbox.begin_send(batch.key)
            observed = json.dumps(reorder(json.loads(sent.record.value)),
                                  ensure_ascii=True, indent=2).encode("utf-8")
            self.assertNotEqual(sent.record.value, observed)
            self.assertEqual("matched", self.outbox.observe(
                sent.attempt, value=observed, evidence_sha256=EVIDENCE))
            self.outbox = FrozenOutbox(Store(self.directory))
        self.assertEqual("complete", self.outbox.next_action(batch.key).kind)
        self.assertEqual(original, self.store.committed(batch.key))
        self.assertEqual(checkpoint, self.store.resume(SOURCE, GENERATION, PARSER))

    def assert_readback_conflict(self, observed, *, manifest=False):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "private")
            snapshot = store.enroll(SOURCE, GENERATION, PARSER, {}, {})
            outbox = FrozenOutbox(store)
            batch = outbox.freeze(snapshot, 100, *bundle(count=2), {})
            if manifest:
                for _ in range(2):
                    sent = outbox.begin_send(batch.key)
                    outbox.observe(sent.attempt, value=sent.record.value, evidence_sha256=EVIDENCE)
            sent = outbox.begin_send(batch.key)
            original = store.committed(batch.key)
            checkpoint = store.resume(SOURCE, GENERATION, PARSER)
            self.assertEqual("conflict", outbox.observe(
                sent.attempt, value=observed, evidence_sha256=EVIDENCE))
            resumed = FrozenOutbox(Store(Path(folder) / "private"))
            self.assertEqual("review_conflict", resumed.next_action(batch.key).kind)
            with self.assertRaises(StoreConflict):
                resumed.begin_send(batch.key)
            self.assertEqual(original, store.committed(batch.key))
            self.assertEqual(checkpoint, store.resume(SOURCE, GENERATION, PARSER))

    def test_changed_readback_values_and_numeric_types_stop_durably(self):
        value = json.loads(bundle(count=2)[0][0].value)
        for field, changed in (("text", "changed"), ("text_sha256", "f" * 64),
                               ("gen", True), ("batch", 1.0), ("ordinal", False),
                               ("ordinal", 0.0), ("ordinal", "0"),
                               ("unexpected", None), ("seat", "other-seat")):
            with self.subTest(field=field, changed=changed):
                self.assert_readback_conflict(encoded(dict(value, **{field: changed})))
        del value["role"]
        self.assert_readback_conflict(encoded(value))

    def test_manifest_readback_preserves_array_order_and_nested_types(self):
        original = json.loads(bundle(count=2)[1].value)
        variants = []
        for field in ("first", "last", "gen", "batch"):
            value = json.loads(encoded(original))
            value[field] = float(value[field])
            variants.append(value)
        value = json.loads(encoded(original))
        value["items"].reverse()
        variants.append(value)
        value = json.loads(encoded(original))
        value["items"][0][0] = False
        variants.append(value)
        value = json.loads(encoded(original))
        value["counts"]["omitted_secret"] = False
        variants.append(value)
        for value in variants:
            with self.subTest(value=value):
                self.assert_readback_conflict(encoded(value), manifest=True)

    def test_malformed_ambiguous_or_oversized_readback_stops_durably(self):
        original = bundle(count=2)[0][0].value
        variants = [b"", b"null", b"[]", b"true", b"1", b"{", original + b"{}",
                    original[:-1] + b',"gen":1}',
                    original[:-1] + b',"g\\u0065n":1}',
                    original[:-1] + b',"extra":{"x":1,"x":1}}',
                    original[:-1] + b',"extra":NaN}',
                    original[:-1] + b',"extra":Infinity}',
                    original[:-1] + b',"extra":1e999}',
                    original[:-1] + b',"extra":"\\ud800"}',
                    original[:-1] + b',"\\ud800":1}',
                    b"\xef\xbb\xbf" + original, b"\xff" + original,
                    original.decode("utf-8").encode("utf-16"),
                    b" " * 65537, b"[" * 1500 + b"0" + b"]" * 1500]
        for position, observed in enumerate(variants):
            with self.subTest(position=position):
                self.assert_readback_conflict(observed)

    def test_defensive_redaction_blocks_original_manifest_and_reupload(self):
        # Synthetic defensive coverage: context.v1 refuses server redaction.
        batch = self.freeze()
        action = self.outbox.begin_send(batch.key)
        self.assertEqual("redacted", self.outbox.observe(action.attempt, value=None, redacted=True,
                                                         evidence_sha256=EVIDENCE))
        self.assertEqual("review_redacted", self.outbox.next_action(batch.key).kind)
        with self.assertRaises(StoreConflict):
            self.outbox.begin_send(batch.key)

    def test_observation_crash_before_commit_requires_reconciliation_again(self):
        batch = self.freeze()
        action = self.outbox.begin_send(batch.key)
        self.crash("observe", "before_observation_commit", batch.key)
        self.assertEqual(action.attempt, self.outbox.next_action(batch.key).attempt)

    def test_observation_crash_after_commit_does_not_repeat_item(self):
        batch = self.freeze()
        action = self.outbox.begin_send(batch.key)
        self.crash("observe", "after_observation_commit", batch.key)
        self.assertTrue(self.outbox.next_action(batch.key).record.key.startswith("ctxm/"))
        with self.assertRaises(StoreConflict):
            self.outbox.observe(action.attempt, value=action.record.value, evidence_sha256=EVIDENCE)

    def test_evidence_process_crash_after_insert_rolls_back_proof_and_receipt(self):
        self.assert_evidence_crash_pending("after_evidence_output")

    def test_evidence_process_crash_before_commit_rolls_back_proof_and_receipt(self):
        self.assert_evidence_crash_pending("before_observation_commit")

    def assert_evidence_crash_pending(self, point):
        batch = self.freeze()
        action = self.outbox.begin_send(batch.key)
        self.crash("observe-evidence", point, batch.key)
        self.assertEqual(action.attempt, self.outbox.next_action(batch.key).attempt)
        with self.assertRaises(StoreError):
            self.outbox.evidence(ARTIFACT_DIGEST)

    def test_evidence_process_crash_after_commit_retains_proof_and_receipt(self):
        batch = self.freeze()
        self.outbox.begin_send(batch.key)
        self.crash("observe-evidence", "after_observation_commit", batch.key)
        self.assertTrue(self.outbox.next_action(batch.key).record.key.startswith("ctxm/"))
        self.assertEqual(ARTIFACT, self.outbox.evidence(ARTIFACT_DIGEST))

    def test_successor_waits_for_verified_predecessor_manifest(self):
        first = self.freeze()
        state = self.store.resume(SOURCE, GENERATION, PARSER)
        second = self.outbox.freeze(state, 200, *bundle(batch=2, first=1), {"next": 2})
        self.assertEqual(("wait_predecessor", first.key),
                         (self.outbox.next_action(second.key).kind, self.outbox.next_action(second.key).batch))
        self.match_next(first.key)
        self.assertEqual("wait_predecessor", self.outbox.next_action(second.key).kind)
        self.match_next(first.key)
        self.assertEqual("send", self.outbox.next_action(second.key).kind)

    def test_competing_senders_cannot_start_two_attempts(self):
        batch = self.freeze()
        def begin(_):
            try:
                return self.outbox.begin_send(batch.key).kind
            except StoreConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(["conflict", "send"], sorted(pool.map(begin, range(2))))

    def test_refuses_manifest_digest_drift_and_ordinal_gap_before_commit(self):
        items, manifest = bundle(count=2)
        changed = json.loads(manifest.value)
        changed["items"][0][1] = "f" * 64
        with self.assertRaises(StoreError):
            self.outbox.freeze(self.snapshot, 100, items, replace(manifest, value=encoded(changed)), {})
        with self.assertRaises(StoreError):
            self.outbox.freeze(self.snapshot, 100, tuple(reversed(items)), manifest, {})
        self.assertEqual(self.snapshot, self.store.resume(SOURCE, GENERATION, PARSER))

    def test_refuses_cross_batch_or_generation_discontinuity(self):
        self.freeze()
        state = self.store.resume(SOURCE, GENERATION, PARSER)
        for batch, first in ((3, 1), (2, 0), (2, 5)):
            with self.assertRaises(StoreConflict):
                self.outbox.freeze(state, 200, *bundle(batch=batch, first=first), {})

    def test_frozen_output_corruption_never_becomes_sendable(self):
        batch = self.freeze()
        with self.store._connection() as db:
            db.execute("UPDATE batches SET output=? WHERE key=?", (b"corrupt", batch.key))
            db.commit()
        with self.assertRaises(StoreConflict):
            self.outbox.next_action(batch.key)

    def replace_refusal(self, action, *, manifest=None, reason="secret", **kwargs):
        item, manifest = omission(action.record, manifest or bundle()[1], reason, action.attempt.position)
        return self.outbox.replace_refused(action.attempt, code="E_CONTEXT_" + reason.upper(),
                                          item=item, manifest=manifest, evidence_sha256=EVIDENCE, **kwargs)

    def test_screen_refusal_replacement_preserves_capture_and_checkpoint(self):
        for reason in ("secret", "client_name"):
            with self.subTest(reason=reason):
                # Each reason exercises an independent first attempt.
                with tempfile.TemporaryDirectory() as folder:
                    store = Store(Path(folder) / "private")
                    snapshot = store.enroll(SOURCE, GENERATION, PARSER, {}, {})
                    outbox = FrozenOutbox(store)
                    batch = outbox.freeze(snapshot, 100, *bundle(), {"next": 1})
                    original = store.committed(batch.key)
                    checkpoint = store.resume(SOURCE, GENERATION, PARSER)
                    sent = outbox.begin_send(batch.key)
                    item, manifest = omission(sent.record, bundle()[1], reason)
                    action = outbox.replace_refused(sent.attempt, code="E_CONTEXT_" + reason.upper(),
                                                   item=item, manifest=manifest, evidence_sha256=EVIDENCE)
                    self.assertEqual(("send", item), (action.kind, action.record))
                    self.assertEqual(original, store.committed(batch.key))
                    self.assertEqual(checkpoint, store.resume(SOURCE, GENERATION, PARSER))
                    self.assertEqual(batch, outbox.freeze(snapshot, 100, *bundle(), {"next": 1}))
                    resumed = FrozenOutbox(Store(Path(folder) / "private"))
                    retry = resumed.begin_send(batch.key)
                    self.assertEqual(item, retry.record)
                    resumed.observe(retry.attempt, value=item.value, evidence_sha256=EVIDENCE)
                    self.assertEqual(manifest, resumed.next_action(batch.key).record)

    def test_refusal_replacement_strips_all_optional_metadata(self):
        items, manifest = bundle()
        value = json.loads(items[0].value)
        value.update(item="artifact", native_ref="synthetic.native:1", agent_id="synthetic-agent")
        del value["role"]
        item = replace(items[0], value=encoded(value))
        batch = self.outbox.freeze(self.snapshot, 100, (item,), manifest, {})
        action = self.outbox.begin_send(batch.key)
        result = self.replace_refusal(action)
        data = json.loads(result.record.value)
        for field in ("text", "text_sha256", "native_ref", "agent_id", "role", "omitted_bytes"):
            self.assertNotIn(field, data)
        self.assertEqual(value["adapter_version"], data["adapter_version"])

    def test_replacement_rejects_derived_metadata_identity_and_manifest_drift(self):
        batch = self.freeze()
        sent = self.outbox.begin_send(batch.key)
        item, manifest = omission(sent.record, bundle()[1])
        variants = []
        for field, value in (("text", "retained tail"), ("text_sha256", "b" * 64),
                             ("native_ref", "synthetic-id"), ("role", "user"),
                             ("adapter_version", "changed:2"), ("ordinal", 1)):
            data = json.loads(item.value)
            data[field] = value
            variants.append((replace(item, value=encoded(data)), manifest))
        data = json.loads(manifest.value)
        data["counts"]["omitted_secret"] = 2
        variants.append((item, replace(manifest, value=encoded(data))))
        for proposed_item, proposed_manifest in variants:
            with self.subTest(item=proposed_item.key):
                with self.assertRaises(StoreError):
                    self.outbox.replace_refused(sent.attempt, code="E_CONTEXT_SECRET", item=proposed_item,
                                               manifest=proposed_manifest, evidence_sha256=EVIDENCE)
                self.assertEqual(sent.attempt, self.outbox.next_action(batch.key).attempt)

    def test_refusal_replacement_disallows_unknown_errors_and_stale_attempts(self):
        batch = self.freeze()
        sent = self.outbox.begin_send(batch.key)
        item, manifest = omission(sent.record, bundle()[1])
        for code in ("timeout", "E_CONTEXT_TOO_LARGE", "E_CONTEXT_SYNTHETIC_UNKNOWN", [], None):
            with self.assertRaises(StoreError):
                self.outbox.replace_refused(sent.attempt, code=code, item=item, manifest=manifest,
                                           evidence_sha256=EVIDENCE)
        self.replace_refusal(sent)
        with self.assertRaises(StoreConflict):
            self.replace_refusal(sent)
        with self.assertRaises(StoreConflict):
            self.outbox.observe(sent.attempt, value=sent.record.value, evidence_sha256=EVIDENCE)

    def test_earlier_uncertain_attempt_cannot_be_hidden_by_retry_refusal(self):
        batch = self.freeze()
        first = self.outbox.begin_send(batch.key)
        self.outbox.observe(first.attempt, value=None, evidence_sha256=EVIDENCE)
        retry = self.outbox.begin_send(batch.key)
        with self.assertRaises(StoreConflict):
            self.replace_refusal(retry)
        self.assertEqual(retry.attempt, self.outbox.next_action(batch.key).attempt)

    def test_historical_pending_attempt_without_guard_cannot_replace(self):
        batch = self.freeze()
        sent = self.outbox.begin_send(batch.key)
        with self.store._connection() as db:
            db.execute("DELETE FROM outbox_attempt_guards_v1")
            db.commit()
        with self.assertRaises(StoreConflict):
            self.replace_refusal(sent)

    def test_matched_redacted_conflicting_items_and_manifest_cannot_replace(self):
        # The redacted outcome is defensive input, not a live context.v1 branch.
        for outcome in ("matched", "redacted", "conflict", "manifest"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder) / "private")
                snapshot = store.enroll(SOURCE, GENERATION, PARSER, {}, {})
                outbox = FrozenOutbox(store)
                batch = outbox.freeze(snapshot, 100, *bundle(), {})
                sent = outbox.begin_send(batch.key)
                outbox.observe(sent.attempt, value=(sent.record.value if outcome in ("matched", "manifest") else
                                                   b"different" if outcome == "conflict" else None),
                               redacted=outcome == "redacted", evidence_sha256=EVIDENCE)
                item, manifest = omission(sent.record, bundle()[1])
                attempt = outbox.begin_send(batch.key).attempt if outcome == "manifest" else sent.attempt
                with self.assertRaises(StoreConflict):
                    outbox.replace_refused(attempt, code="E_CONTEXT_SECRET", item=item, manifest=manifest,
                                           evidence_sha256=EVIDENCE)

    def test_replacement_rollback_after_output_keeps_original_attempt(self):
        batch = self.freeze()
        sent = self.outbox.begin_send(batch.key)
        self.crash("replace", "after_replacement_output", batch.key)
        self.assertEqual(sent.record, self.outbox.next_action(batch.key).record)
        self.assertEqual(sent.attempt, self.outbox.next_action(batch.key).attempt)

    def test_replacement_rollback_before_commit_keeps_original_attempt(self):
        batch = self.freeze()
        sent = self.outbox.begin_send(batch.key)
        self.crash("replace", "before_replacement_commit", batch.key)
        self.assertEqual(sent.record, self.outbox.next_action(batch.key).record)
        self.assertEqual(sent.attempt, self.outbox.next_action(batch.key).attempt)

    def test_replacement_crash_after_commit_keeps_item_and_manifest_together(self):
        batch = self.freeze()
        sent = self.outbox.begin_send(batch.key)
        expected_item, expected_manifest = omission(sent.record, bundle()[1])
        self.crash("replace", "after_replacement_commit", batch.key)
        self.assertEqual(("send", expected_item),
                         (self.outbox.next_action(batch.key).kind, self.outbox.next_action(batch.key).record))
        self.match_next(batch.key)
        self.assertEqual(expected_manifest, self.outbox.next_action(batch.key).record)

    def test_multiple_refusals_update_manifest_and_leave_other_items_unchanged(self):
        batch = self.freeze(count=3)
        items, original_manifest = bundle(count=3)
        first = self.outbox.begin_send(batch.key)
        first_item, manifest = omission(first.record, original_manifest)
        self.replace_refusal(first, manifest=original_manifest)
        self.match_next(batch.key)
        second = self.outbox.begin_send(batch.key)
        self.assertEqual(items[1], second.record)
        second_item, final_manifest = omission(second.record, manifest, "client_name", 1)
        self.replace_refusal(second, manifest=manifest, reason="client_name")
        self.match_next(batch.key)
        self.assertEqual(items[2], self.outbox.next_action(batch.key).record)
        self.match_next(batch.key)
        self.assertEqual(final_manifest, self.outbox.next_action(batch.key).record)
        self.match_next(batch.key)
        self.assertEqual("complete", self.outbox.next_action(batch.key).kind)

    def test_competing_replacements_commit_only_one_transition(self):
        batch = self.freeze()
        sent = self.outbox.begin_send(batch.key)
        def attempt(_):
            try:
                return self.replace_refusal(sent).kind
            except StoreConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(["conflict", "send"], sorted(pool.map(attempt, range(2))))

    def test_omission_overlay_corruption_blocks_sending(self):
        batch = self.freeze()
        self.replace_refusal(self.outbox.begin_send(batch.key))
        with self.store._connection() as db:
            db.execute("UPDATE outbox_omissions_v1 SET item=?", (b"corrupt",))
            db.commit()
        with self.assertRaises(StoreConflict):
            self.outbox.next_action(batch.key)

    def test_issue426_draining_a_wide_batch_decodes_it_once(self):
        # #426: every next_action/begin_send/observe re-decoded and re-validated the whole committed batch, so a
        # batch cost items x bytes to drain. Its records are decoded once now, and the drain still sends every
        # record exactly as frozen.
        import frozen_outbox
        batch = self.freeze(count=256)
        decoded, real = [], frozen_outbox._records
        def counting(items, manifest):
            decoded.append(len(items))
            return real(items, manifest)
        frozen_outbox._records = counting
        self.addCleanup(setattr, frozen_outbox, "_records", real)
        sent = []
        while (action := self.outbox.next_action(batch.key)).kind != "complete":
            sent.append(self.match_next(batch.key).record)
        self.assertEqual(list(bundle(count=256)[0]) + [bundle(count=256)[1]], sent)
        self.assertEqual([256], decoded)

    def test_issue426_cached_batch_still_takes_a_later_omission(self):
        batch = self.freeze()
        self.outbox.next_action(batch.key)                      # decoded and cached before the refusal
        sent = self.outbox.begin_send(batch.key)
        item, _ = omission(sent.record, bundle()[1])
        self.replace_refusal(sent)
        action = self.outbox.next_action(batch.key)
        self.assertEqual(("send", item), (action.kind, action.record))
        self.assertEqual(action, FrozenOutbox(self.store).next_action(batch.key))   # an uncached outbox agrees

    def test_issue426_corruption_after_caching_still_never_becomes_sendable(self):
        # The cache saves the decode, never the integrity check: the stored bytes are re-hashed on every load.
        batch = self.freeze()
        self.outbox.next_action(batch.key)
        with self.store._connection() as db:
            db.execute("UPDATE batches SET output=? WHERE key=?", (b"corrupt", batch.key))
            db.commit()
        with self.assertRaises(StoreConflict):
            self.outbox.next_action(batch.key)

    def test_followup_cache_is_bounded_by_committed_output_bytes(self):
        # A long-lived outbox must not retain four limit-sized batches. Charge each entry by its committed
        # output + drops bytes, and evict FIFO until the whole cache fits the store's output budget.
        first = self.freeze()
        self.snapshot = self.store.resume(SOURCE, GENERATION, PARSER)
        second = self.outbox.freeze(self.snapshot, 101, *bundle(batch=2, first=1), {"next": 2})
        with self.store._connection() as db:
            sizes = dict(db.execute("SELECT key,length(output)+length(drops) FROM batches WHERE key IN (?,?)",
                                    (first.key, second.key)).fetchall())
            self.assertEqual({first.key, second.key}, set(sizes))
            cap = max(sizes.values())
            self.store.limits = replace(self.store.limits, output_bytes=cap)
            self.outbox._load(db, first.key)            # direct _load: no predecessor walk to disturb the assertion
            self.assertEqual({first.key}, {key for key, _ in self.outbox._decoded})
            self.outbox._load(db, second.key)
            self.assertEqual({second.key}, {key for key, _ in self.outbox._decoded})
            self.assertLessEqual(self.outbox._decoded_bytes, cap)

    def test_followup_changed_digest_cannot_reuse_cached_batch(self):
        # The existing corruption test changes bytes but not the row's digest. Change both: a cache keyed only
        # by batch key would wrongly reuse these records instead of decoding and rejecting the new bytes.
        batch = self.freeze()
        self.outbox.next_action(batch.key)
        replacement = b"corrupt with a matching digest"
        with self.store._connection() as db:
            db.execute("UPDATE batches SET output=?,output_sha256=? WHERE key=?",
                       (replacement, hashlib.sha256(replacement).hexdigest(), batch.key))
            db.commit()
        with self.assertRaises(StoreConflict):
            self.outbox.next_action(batch.key)

    def test_prune_completed_evidence_preserves_incomplete_shared_and_omission_proofs(self):
        def proof(tag):
            raw = ("proof:" + tag).encode("utf-8")
            return raw, hashlib.sha256(raw).hexdigest()

        b1 = self.outbox.freeze(self.snapshot, 100, *bundle(batch=1, first=0, count=2), {"next": 2})
        shared_raw, shared_sha = proof("shared-b1-b2")
        b1_item1_raw, b1_item1_sha = proof("b1-item1")
        b1_man_raw, b1_man_sha = proof("b1-manifest")

        for raw, sha in ((shared_raw, shared_sha), (b1_item1_raw, b1_item1_sha), (b1_man_raw, b1_man_sha)):
            act = self.outbox.begin_send(b1.key)
            self.assertEqual("matched", self.outbox.observe(
                act.attempt, value=act.record.value, evidence_sha256=sha, evidence_artifact=raw))
        self.assertEqual("complete", self.outbox.next_action(b1.key).kind)

        snap2 = self.store.resume(SOURCE, GENERATION, PARSER)
        b2 = self.outbox.freeze(snap2, 200, *bundle(batch=2, first=2, count=2), {"next": 4})
        # Item 0 of b2 reuses shared_sha; item 1 records an absence proof then starts a new attempt (orphaning it).
        act2_0 = self.outbox.begin_send(b2.key)
        self.assertEqual("matched", self.outbox.observe(
            act2_0.attempt, value=act2_0.record.value, evidence_sha256=shared_sha, evidence_artifact=shared_raw))
        act2_1_first = self.outbox.begin_send(b2.key)
        superseded_raw, superseded_sha = proof("b2-item1-absent-superseded")
        self.assertEqual("ready", self.outbox.observe(
            act2_1_first.attempt, value=None, evidence_sha256=superseded_sha, evidence_artifact=superseded_raw))
        act2_1_retry = self.outbox.begin_send(b2.key)  # immediately drops unreferenced superseded absence proof
        with self.assertRaises(StoreError):
            self.outbox.evidence(superseded_sha)

        # Seed a legacy pre-fix orphan directly in outbox_evidence_v1 to verify full-sweep orphan cleanup.
        orphan_raw, orphan_sha = proof("legacy-unreferenced-orphan")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO outbox_evidence_v1 VALUES(?,?)", (orphan_sha, orphan_raw))
            db.commit()

        # Crash before commit must roll back deletions cleanly.
        with self.assertRaises(RuntimeError):
            self.outbox.prune_completed_evidence(
                b1.key, boundary=lambda p: (_ for _ in ()).throw(RuntimeError("boom")) if p == "before_prune_commit" else None)
        self.assertEqual(b1_item1_raw, self.outbox.evidence(b1_item1_sha))

        # Targeted prune on completed b1 removes b1's exclusive blobs, but keeps shared_sha needed by incomplete b2.
        self.assertEqual(2, self.outbox.prune_completed_evidence(b1.key))
        for gone in (b1_item1_sha, b1_man_sha):
            with self.assertRaises(StoreError):
                self.outbox.evidence(gone)
        self.assertEqual(shared_raw, self.outbox.evidence(shared_sha))
        self.assertEqual(orphan_raw, self.outbox.evidence(orphan_sha))
        self.assertEqual("complete", self.outbox.next_action(b1.key).kind)

        # Pruning an incomplete batch explicitly is a safe no-op.
        self.assertEqual(0, self.outbox.prune_completed_evidence(b2.key))
        self.assertEqual(shared_raw, self.outbox.evidence(shared_sha))

        # Full store sweep removes the unreferenced retry orphan while still protecting incomplete b2's shared_sha.
        self.assertEqual(1, self.outbox.prune_completed_evidence())
        with self.assertRaises(StoreError):
            self.outbox.evidence(orphan_sha)
        self.assertEqual(shared_raw, self.outbox.evidence(shared_sha))

        # Finish b2 -> shared_sha is now referenced only by completed batches and gets pruned too.
        b2_item1_raw, b2_item1_sha = proof("b2-item1-matched")
        b2_man_raw, b2_man_sha = proof("b2-manifest")
        self.assertEqual("matched", self.outbox.observe(
            act2_1_retry.attempt, value=act2_1_retry.record.value,
            evidence_sha256=b2_item1_sha, evidence_artifact=b2_item1_raw))
        act2_man = self.outbox.begin_send(b2.key)
        self.assertEqual("matched", self.outbox.observe(
            act2_man.attempt, value=act2_man.record.value,
            evidence_sha256=b2_man_sha, evidence_artifact=b2_man_raw))
        self.assertEqual(3, self.outbox.prune_completed_evidence(b2.key))
        with self.store._connection() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM outbox_evidence_v1").fetchone()[0])

        # Omission-overlay refusal digest stays protected even after its batch completes.
        snap3 = self.store.resume(SOURCE, GENERATION, PARSER)
        items3, man3 = bundle(batch=3, first=4, count=1)
        b3 = self.outbox.freeze(snap3, 300, items3, man3, {"next": 5})
        om_raw, om_sha = proof("omission-refusal-proof")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO outbox_evidence_v1 VALUES(?,?)", (om_sha, om_raw))
            db.commit()
        sent3 = self.outbox.begin_send(b3.key)
        om_item, om_man = omission(sent3.record, man3, "secret", sent3.attempt.position)
        self.outbox.replace_refused(sent3.attempt, code="E_CONTEXT_SECRET",
                                    item=om_item, manifest=om_man, evidence_sha256=om_sha)
        for tag in ("b3-item", "b3-man"):
            act3 = self.outbox.begin_send(b3.key)
            r3, s3 = proof(tag)
            self.outbox.observe(act3.attempt, value=act3.record.value, evidence_sha256=s3, evidence_artifact=r3)
        self.assertEqual("complete", self.outbox.next_action(b3.key).kind)
        self.assertEqual(2, self.outbox.prune_completed_evidence())
        self.assertEqual(om_raw, self.outbox.evidence(om_sha))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(child(*sys.argv[2:]))
    unittest.main()
