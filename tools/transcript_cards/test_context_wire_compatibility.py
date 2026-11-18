"""Offline integration with current and explicitly pinned canonical validators.

Set MESHY_CONTEXT_VALIDATOR_SNAPSHOT to a directory containing the three flattened
snapshot files below. Hashes pin PR #380 at db9e6d9a. Preflight tests additionally
exercise its real privacy screens with synthetic markers and repository
detectors. No server database, grants, signatures or writes are exercised.
"""
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from frozen_outbox import FrozenOutbox, Store
from screened_outbox import AdmissionError, freeze_screened
from test_frozen_outbox import bundle, omission, encoded, SOURCE, GENERATION, PARSER, EVIDENCE


SNAPSHOT = os.environ.get("MESHY_CONTEXT_VALIDATOR_SNAPSHOT")
HASHES = {
    "apps__context__validation.py": "2bf2c39478d0048c688891911bde13a8adab8d2447c30909e06e3d993cd0ffe6",
    "apps__context__screens.py": "03c5c910eea73c65141549b427f25a74f1c1ec33d87123c67abbe5e296c5bdd0",
    "apps__memoryskills__errors.py": "dc5e3053e950ea161c0c6fb90c50ed79a9e60272df5c87d8417a2560cd5aad80",
}


@contextmanager
def scratch_policy():
    from django.test import override_settings
    namespace = "shared/scratch-transcript-context"
    with override_settings(MESHY_CONTEXT_NAMESPACE=namespace), patch("screened_outbox.NAMESPACE", namespace):
        yield


@unittest.skipUnless(SNAPSHOT, "explicit pinned validator snapshot required")
class ContextWireCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        cls.addClassCleanup(sys.path.pop, 0)
        from django.conf import settings
        from django.test import override_settings
        if not settings.configured:
            settings.configure()
        config = override_settings(MESHY_CONTEXT_NAMESPACE="shared/transcript-context")
        config.enable()
        cls.addClassCleanup(config.disable)
        modules = patch.dict(sys.modules)
        modules.start()
        cls.addClassCleanup(modules.stop)
        package = types.ModuleType("_pinned_context")
        package.__path__ = []
        sys.modules[package.__name__] = package
        for name, filename in (
                ("apps.memoryskills.errors", "apps__memoryskills__errors.py"),
                ("_pinned_context.screens", "apps__context__screens.py"),
                ("_pinned_context.validation", "apps__context__validation.py")):
            path = Path(SNAPSHOT) / filename
            if hashlib.sha256(path.read_bytes()).hexdigest() != HASHES[filename]:
                raise AssertionError("validator snapshot hash mismatch: " + filename)
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        cls.validator = sys.modules["_pinned_context.validation"]
        cls.refusal = sys.modules["apps.memoryskills.errors"].HandlerError

    def validate(self, record):
        value = json.loads(record.value)
        self.assertEqual(value["kind"], self.validator.validate_context(
            "shared/transcript-context", record.key, value,
            now=datetime(2026, 10, 19, tzinfo=timezone.utc)))

    def test_integer_wire_batches_survive_freeze_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "private")
            state = store.enroll(SOURCE, GENERATION, PARSER, {}, {})
            for number, first, count in ((1, 0, 2), (2, 2, 3)):
                items, manifest = bundle(number, first, count)
                for record in items + (manifest,):
                    self.validate(record)
                outbox = FrozenOutbox(store)
                batch = outbox.freeze(state, number * 100, items, manifest, {})
                reopened = FrozenOutbox(Store(Path(folder) / "private"))
                for expected in items + (manifest,):
                    action = reopened.begin_send(batch.key)
                    self.assertEqual(expected, action.record)
                    self.validate(action.record)
                    reopened.observe(action.attempt, value=expected.value, evidence_sha256=EVIDENCE)
                self.assertEqual("complete", reopened.next_action(batch.key).kind)
                state = store.resume(SOURCE, GENERATION, PARSER)

    def test_full_batch_and_key_numeric_limits_pass(self):
        for items, manifest in (bundle(count=256), bundle(batch=999999, first=9999999999)):
            for record in items + (manifest,):
                self.validate(record)

    def test_both_minimal_refusal_replacements_pass_canonical_validator(self):
        for reason in ("secret", "client_name"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder) / "private")
                state = store.enroll(SOURCE, GENERATION, PARSER, {}, {})
                outbox = FrozenOutbox(store)
                items, manifest = bundle(count=2)
                batch = outbox.freeze(state, 100, items, manifest, {})
                sent = outbox.begin_send(batch.key)
                item, replacement_manifest = omission(sent.record, manifest, reason)
                self.validate(item)
                self.validate(replacement_manifest)
                outbox.replace_refused(sent.attempt, code="E_CONTEXT_" + reason.upper(),
                                       item=item, manifest=replacement_manifest, evidence_sha256=EVIDENCE)
                self.assertEqual(item, FrozenOutbox(Store(Path(folder) / "private")).next_action(batch.key).record)

    def test_canonical_validator_rejects_string_boolean_and_float_numbers(self):
        items, manifest = bundle()
        for record, fields in ((items[0], ("gen", "batch", "ordinal")),
                               (manifest, ("gen", "batch", "first", "last"))):
            for field in fields:
                for bad in ("0", "0001", True, False, 0.0, 1.0):
                    with self.subTest(kind=json.loads(record.value)["kind"], field=field, bad=bad):
                        value = json.loads(record.value)
                        value[field] = bad
                        with self.assertRaises(self.refusal):
                            self.validate(replace(record, value=encoded(value)))

    def setUp(self):
        from django.test import override_settings
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        marker_file = self.folder / "synthetic-markers.json"
        marker_file.write_text('["ZorblaxCorp"]', encoding="utf-8")
        config = override_settings(MESHY_CONTEXT_DENYLIST=str(marker_file))
        config.enable()
        self.addCleanup(config.disable)
        self.store = Store(self.folder / "private")
        self.state = self.store.enroll(SOURCE, GENERATION, PARSER, {}, {"next": 0})
        self.outbox = FrozenOutbox(self.store)

    def freeze_screened(self, items, manifest, **kwargs):
        return freeze_screened(self.outbox, self.state, 100, items, manifest, {"next": len(items)},
                               seat=kwargs.pop("seat", "synthetic-seat"), validator=self.validator,
                               now=datetime(2026, 10, 19, tzinfo=timezone.utc), **kwargs)

    def unchanged(self):
        self.assertEqual(self.state, self.store.resume(SOURCE, GENERATION, PARSER))
        self.assertEqual((), self.store.batches(SOURCE, GENERATION, PARSER).items)

    def changed_item(self, items, manifest, position, **updates):
        value = json.loads(items[position].value)
        value.update(updates)
        if "text" in updates:
            value["text_sha256"] = hashlib.sha256(value["text"].encode()).hexdigest()
        amended = list(items)
        amended[position] = replace(items[position], value=encoded(value))
        summary = json.loads(manifest.value)
        summary["items"][position][1] = value.get("text_sha256")
        return tuple(amended), replace(manifest, value=encoded(summary))

    def test_screened_freeze_reopens_with_exact_bytes_and_checkpoint(self):
        items, manifest = bundle(count=2)
        batch = self.freeze_screened(items, manifest)
        reopened_store = Store(self.folder / "private")
        reopened = FrozenOutbox(reopened_store)
        self.assertEqual(items[0], reopened.next_action(batch.key).record)
        resumed = reopened_store.resume(SOURCE, GENERATION, PARSER)
        self.assertEqual(100, resumed.offset)
        self.assertEqual({"next": 2}, resumed.parser_state())
        for expected in items + (manifest,):
            action = reopened.begin_send(batch.key)
            self.assertEqual(expected, action.record)
            reopened.observe(action.attempt, value=expected.value, evidence_sha256=EVIDENCE)
        self.assertEqual("complete", reopened.next_action(batch.key).kind)

    def test_late_secret_or_normalized_name_refuses_whole_batch(self):
        for text, code in (("sk-" + "a" * 48, "E_CONTEXT_SECRET"),
                           ("Zor blax-Corp", "E_CONTEXT_CLIENT_NAME")):
            with self.subTest(code=code), scratch_policy():
                records = self.changed_item(*bundle(count=2), 1, text=text)
                with self.assertRaises(AdmissionError) as caught:
                    self.freeze_screened(*records)
                self.assertEqual(code, caught.exception.code)
                self.assertNotIn(text, str(caught.exception))
                self.unchanged()

    def test_omission_rejects_free_text_in_version_before_commit(self):
        items, manifest = bundle()
        item, manifest = omission(items[0], manifest)
        value = json.loads(item.value)
        value["adapter_version"] = "ZorblaxCorp:1"
        with self.assertRaises(AdmissionError) as caught:
            self.freeze_screened((replace(item, value=encoded(value)),), manifest)
        self.assertEqual("E_CONTEXT_SCHEMA", caught.exception.code)
        self.unchanged()

    def test_hashed_native_and_agent_ids_survive_freeze(self):
        items, manifest = bundle()
        native = hashlib.sha256(b"toolu_synthetic_record_id").hexdigest()[:32]
        agent = hashlib.sha256(b"synthetic-agent-id").hexdigest()[:32]
        value = json.loads(items[0].value)
        value.update(item="artifact", native_ref=native, agent_id=agent)
        del value["role"]
        item = replace(items[0], value=encoded(value))
        batch = self.freeze_screened((item,), manifest)
        reopened = FrozenOutbox(Store(self.folder / "private"))
        self.assertEqual(item, reopened.next_action(batch.key).record)

    def test_raw_native_ids_and_legacy_versions_are_refused_without_rewrite(self):
        for updates in ({"native_ref": "toolu_synthetic_record_id"},
                        {"adapter_version": "synthetic:1"}):
            with self.subTest(updates=updates):
                items, manifest = self.changed_item(*bundle(), 0, **updates)
                original = items[0].value
                with self.assertRaises(AdmissionError) as caught:
                    self.freeze_screened(items, manifest)
                self.assertEqual("E_CONTEXT_SCHEMA", caught.exception.code)
                self.assertEqual(original, items[0].value)
                self.unchanged()

    def test_missing_denylist_wins_over_secret_and_retry_can_succeed(self):
        from django.test import override_settings
        secret_records = self.changed_item(*bundle(), 0, text="sk-" + "a" * 48)
        with scratch_policy(), override_settings(MESHY_CONTEXT_DENYLIST=""):
            with self.assertRaises(AdmissionError) as caught:
                self.freeze_screened(*secret_records)
        self.assertEqual("E_CONTEXT_SCREEN_UNAVAILABLE", caught.exception.code)
        self.unchanged()
        items, manifest = bundle()
        item, manifest = omission(items[0], manifest)
        batch = self.freeze_screened((item,), manifest)
        self.assertEqual(item, self.outbox.next_action(batch.key).record)

    def test_unexpected_screen_error_is_path_free_and_uncommitted(self):
        with scratch_policy(), patch.object(self.validator.screens, "_detectors", side_effect=OSError("private/path/name")):
            with self.assertRaises(AdmissionError) as caught:
                self.freeze_screened(*bundle())
        self.assertEqual("E_CONTEXT_LOCAL_POLICY", caught.exception.code)
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.unchanged()

    def test_invalid_schema_and_false_manifest_counts_do_not_commit(self):
        items, manifest = bundle(count=2)
        value = json.loads(items[1].value)
        value["unexpected"] = "synthetic"
        invalid = (items[0], replace(items[1], value=encoded(value)))
        with self.assertRaises(AdmissionError) as caught:
            self.freeze_screened(invalid, manifest)
        self.assertEqual("E_CONTEXT_SCHEMA", caught.exception.code)
        summary = json.loads(manifest.value)
        summary["counts"].update(written=1, omitted_secret=1)
        with self.assertRaises(AdmissionError) as caught:
            self.freeze_screened(items, replace(manifest, value=encoded(summary)))
        self.assertEqual("E_CONTEXT_MANIFEST", caught.exception.code)
        self.unchanged()

    def test_trusted_seat_must_match_frozen_identity(self):
        with self.assertRaises(AdmissionError) as caught:
            self.freeze_screened(*bundle(), seat="other-seat")
        self.assertEqual("E_CONTEXT_SEAT_MISMATCH", caught.exception.code)
        self.unchanged()

    def test_nonzero_first_batch_is_final_and_uncommitted(self):
        with self.assertRaises(AdmissionError) as caught:
            self.freeze_screened(*bundle(first=1))
        self.assertEqual("E_CONTEXT_MANIFEST_ORDER", caught.exception.code)
        self.unchanged()

    def test_server_refusals_keep_their_codes_without_committing(self):
        for code in ("E_CONTEXT_WRITE_ONCE", "E_CONTEXT_REDACT_UNAVAILABLE",
                     "E_CONTEXT_MANIFEST_PREDECESSOR", "E_CONTEXT_MANIFEST_ITEM_MISSING"):
            with self.subTest(code=code):
                refusal = self.refusal(code, "synthetic private policy detail")
                with patch.object(self.validator, "validate_context", side_effect=refusal):
                    with self.assertRaises(AdmissionError) as caught:
                        self.freeze_screened(*bundle())
                self.assertEqual(code, caught.exception.code)
                self.assertNotIn("private", str(caught.exception))
                self.unchanged()

    def test_unknown_policy_failures_are_final_and_content_free(self):
        for error in (self.refusal("E_CONTEXT_SYNTHETIC_UNKNOWN", "private detail"),
                      RuntimeError("private/path")):
            with self.subTest(error_type=type(error).__name__):
                with patch.object(self.validator, "validate_context", side_effect=error):
                    with self.assertRaises(AdmissionError) as caught:
                        self.freeze_screened(*bundle())
                self.assertEqual("E_CONTEXT_LOCAL_POLICY", caught.exception.code)
                self.assertNotIn("private", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.unchanged()

    def test_missing_validator_or_namespace_is_not_admission(self):
        with patch("screened_outbox.importlib.import_module", side_effect=ImportError("private/path")):
            with self.assertRaises(AdmissionError) as caught:
                freeze_screened(self.outbox, self.state, 100, *bundle(), {}, seat="synthetic-seat")
        self.assertEqual("E_CONTEXT_SCREEN_UNAVAILABLE", caught.exception.code)
        from django.test import override_settings
        with override_settings(MESHY_CONTEXT_NAMESPACE="other/namespace"):
            with self.assertRaises(AdmissionError) as caught:
                self.freeze_screened(*bundle())
        self.assertEqual("E_CONTEXT_SCREEN_UNAVAILABLE", caught.exception.code)
        self.unchanged()


class CurrentContextScreenPolicy(unittest.TestCase):
    """Exercise the installed namespace policy without a server or database."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root))
        cls.addClassCleanup(sys.path.pop, 0)
        from django.conf import settings
        if not settings.configured:
            settings.configure()
        modules = patch.dict(sys.modules)
        modules.start()
        cls.addClassCleanup(modules.stop)
        package = types.ModuleType("_current_context_policy")
        package.__path__ = []
        sys.modules[package.__name__] = package
        for name in ("screens", "validation"):
            qualified = package.__name__ + "." + name
            spec = importlib.util.spec_from_file_location(qualified, root / "apps/context" / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified] = module
            spec.loader.exec_module(module)
        cls.validator = sys.modules[package.__name__ + ".validation"]

    def setUp(self):
        from django.test import override_settings
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.marker_file = self.folder / "synthetic-markers.json"
        self.marker_file.write_text('["ZorblaxCorp"]', encoding="utf-8")
        config = override_settings(MESHY_CONTEXT_NAMESPACE="shared/transcript-context",
                                   MESHY_CONTEXT_DENYLIST=str(self.marker_file))
        config.enable()
        self.addCleanup(config.disable)
        self.store = Store(self.folder / "private")
        self.state = self.store.enroll(SOURCE, GENERATION, PARSER, {}, {"next": 0})
        self.outbox = FrozenOutbox(self.store)

    freeze_screened = ContextWireCompatibility.freeze_screened
    changed_item = ContextWireCompatibility.changed_item
    unchanged = ContextWireCompatibility.unchanged

    def test_transcript_secret_and_name_freeze_without_loading_either_screen(self):
        items, manifest = self.changed_item(*bundle(count=2), 1, text="sk-" + "a" * 48 + " Zor blax-Corp")
        self.marker_file.unlink()
        with patch.object(self.validator.screens, "_markers", side_effect=AssertionError("denylist read")), \
                patch.object(self.validator.screens, "_detectors", side_effect=AssertionError("detectors loaded")):
            batch = self.freeze_screened(items, manifest)
        reopened_store = Store(self.folder / "private")
        reopened = FrozenOutbox(reopened_store)
        resumed = reopened_store.resume(SOURCE, GENERATION, PARSER)
        self.assertEqual(100, resumed.offset)
        self.assertEqual({"next": 2}, resumed.parser_state())
        for expected in items + (manifest,):
            action = reopened.begin_send(batch.key)
            self.assertEqual(expected, action.record)
            reopened.observe(action.attempt, value=expected.value, evidence_sha256=EVIDENCE)
        self.assertEqual("complete", reopened.next_action(batch.key).kind)

    def test_transcript_invalid_schema_still_refuses_without_screens(self):
        items, manifest = self.changed_item(*bundle(count=2), 1, unexpected="synthetic")
        self.marker_file.unlink()
        with self.assertRaises(AdmissionError) as caught:
            self.freeze_screened(items, manifest)
        self.assertEqual("E_CONTEXT_SCHEMA", caught.exception.code)
        self.unchanged()

    def test_scratch_keeps_both_screens_and_required_denylist(self):
        with scratch_policy():
            for text, code in (("sk-" + "a" * 48, "E_CONTEXT_SECRET"),
                               ("Zor blax-Corp", "E_CONTEXT_CLIENT_NAME")):
                with self.subTest(code=code):
                    items, manifest = self.changed_item(*bundle(count=2), 1, text=text)
                    with self.assertRaises(AdmissionError) as caught:
                        self.freeze_screened(items, manifest)
                    self.assertEqual(code, caught.exception.code)
                    self.unchanged()
            self.marker_file.unlink()
            with self.assertRaises(AdmissionError) as caught:
                self.freeze_screened(*bundle())
            self.assertEqual("E_CONTEXT_SCREEN_UNAVAILABLE", caught.exception.code)
            self.unchanged()

    def test_detector_dependency_failure_keeps_batch_pending(self):
        from apps.memoryskills.errors import HandlerError
        with scratch_policy(), patch.object(self.validator.screens, "_detectors", side_effect=HandlerError(
                "E_CONTEXT_SCREEN_UNAVAILABLE", "synthetic dependency failure")):
            with self.assertRaises(AdmissionError) as caught:
                self.freeze_screened(*bundle())
        self.assertEqual("E_CONTEXT_SCREEN_UNAVAILABLE", caught.exception.code)
        self.unchanged()

    def test_invalid_namespace_policy_fails_closed(self):
        for policy in (None, 0, "false"):
            with self.subTest(policy=policy), patch.object(self.validator, "screens_apply", return_value=policy):
                with self.assertRaises(AdmissionError) as caught:
                    self.freeze_screened(*bundle())
                self.assertEqual("E_CONTEXT_LOCAL_POLICY", caught.exception.code)
                self.unchanged()

    def test_older_validator_without_switch_keeps_both_screens(self):
        legacy = types.SimpleNamespace(validate_context=self.validator.validate_context,
                                       screens=self.validator.screens,
                                       SCREENED_FIELDS=self.validator.SCREENED_FIELDS)
        with patch.object(self, "validator", legacy):
            for text, code in (("sk-" + "a" * 48, "E_CONTEXT_SECRET"),
                               ("Zor blax-Corp", "E_CONTEXT_CLIENT_NAME")):
                with self.subTest(code=code):
                    items, manifest = self.changed_item(*bundle(count=2), 1, text=text)
                    with self.assertRaises(AdmissionError) as caught:
                        self.freeze_screened(items, manifest)
                    self.assertEqual(code, caught.exception.code)
                    self.unchanged()


if __name__ == "__main__":
    unittest.main()
