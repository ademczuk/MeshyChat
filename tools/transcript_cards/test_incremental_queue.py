"""Synthetic queue integration; stdlib runner, no server/real transcripts."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import capture_queue as queue
import incremental_queue as iq
import incremental_store as ist
import native_window as nw


def line(value):
    return (json.dumps(value) + "\n").encode()


def meta():
    return line({"type": "session_meta", "payload": {"id": "synthetic-session", "source": "cli",
                                                     "originator": "codex-tui"}})


def pair(text="keep the cache local", stamp="2026-10-10T09:00:00Z"):
    return [line({"timestamp": stamp, "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}),
            line({"timestamp": stamp, "type": "event_msg", "payload": {
                "type": "user_message", "message": text}})]


class IncrementalQueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.spool = self.root / "spool"
        self.path = self.source / "rollout-fixture.jsonl"
        self.path.write_bytes(meta() + b"".join(pair()))
        self.config = {"kind": "codex", "seat": "codex-kxxx-cli", "root": str(self.source),
                       "files": [self.path.name]}
        self.budget = nw.Budget(read_bytes=2048, record_bytes=1024, records=2, seconds=5)

    def enqueue(self, config=None):
        return queue.enqueue(self.spool, config or self.config)["job"]

    def cli(self, command, *, fail=False, limits=None):
        args = [sys.executable, "-B", str(Path(queue.__file__)), command, "--spool", str(self.spool)]
        if command == "incremental":
            settings = self.root / "window.json"
            settings.write_text(json.dumps({"read_bytes": 2048, "record_bytes": 1024, "records": 2, "seconds": 5}))
            args += ["--window", str(settings)]
        if limits:
            settings = self.root / "limits.json"
            settings.write_text(json.dumps(limits))
            args += ["--limits", str(settings)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 1 if fail else 0, result.stderr)
        return json.loads(result.stderr if fail else result.stdout)

    def store_state(self, job):
        info = self.path.stat()
        store = ist.Store(self.spool / "incremental-store")
        snap = store.resume(iq.opaque([job, self.path.name]), iq.opaque([info.st_dev, info.st_ino]), iq.parser_id())
        return store, snap

    def test_actual_cli_fresh_processes_finish_held_pair_once(self):
        job = self.enqueue()
        first = self.cli("incremental")[0]
        self.assertEqual(first["phase"], "pending")
        self.assertEqual(first["safe_offset"], len(meta()))
        self.assertEqual(self.cli("incremental")[0]["phase"], "staged")
        self.assertEqual(self.cli("incremental"), [])
        self.assertFalse(self.cli("status")[0]["pending"])
        store, snap = self.store_state(job)
        page = store.batches(snap.source, snap.generation, snap.parser)
        ids = {json.loads(row)["id"] for _, output, _ in page.items for row in output.splitlines()}
        self.assertEqual(len(ids), 1)
        self.assertEqual(snap.offset, self.path.stat().st_size)

    def test_machine_context_matches_snapshot_filter_and_keeps_operator_text(self):
        injected = '  <codex_internal_context source="synthetic">harness context</codex_internal_context>'
        typed = 'Please explain <codex_internal_context source in this pasted example'
        self.path.write_bytes(meta() + b"".join(pair(injected)) + b"".join(pair(typed)))
        snapshot = self.root / "snapshot"
        # Real owner parser/filter, same synthetic source, no snapshot-loader double.
        report = iq.loader.run(self.source, self.config["seat"], snapshot,
                               codex_dir=self.source)
        self.assertEqual(report["dropped_by_class"]["machine_prefix"], 1)
        expected = (snapshot / "cards.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(expected), 1)
        job = self.enqueue()
        # Exercise held item/event replay through fresh CLI invocations.
        for _ in range(5):
            self.cli("incremental")
        store, snap = self.store_state(job)
        page = store.batches(snap.source, snap.generation, snap.parser)
        actual = [json.loads(row) for _, output, _ in page.items for row in output.splitlines()]
        self.assertEqual([card["id"] for card in actual], [json.loads(expected[0])["id"]])
        self.assertEqual(snap.offset, self.path.stat().st_size)
        self.assertFalse(queue.status(self.spool)[0]["pending"])

    def test_appending_large_history_never_calls_full_loader_or_file_inventory(self):
        # Larger than old whole-file/source caps, while each window stays tiny.
        self.path.write_bytes(meta() + b"".join(pair()) * 100)
        self.enqueue()
        limits = replace(queue.Limits(), file_bytes=8, source_bytes=8)
        with mock.patch.object(queue, "source_inventory", side_effect=AssertionError("whole scan")), \
                mock.patch.object(queue, "run_bounded_loader", side_effect=AssertionError("whole loader")):
            result = iq.stage_incremental(self.spool, budget=self.budget, limits=limits)[0]
        self.assertEqual(result["phase"], "pending")
        self.assertLess(result["safe_offset"], 2048)

    def test_partial_tail_waits_for_new_wake_and_extends_same_checkpoint(self):
        self.path.write_bytes(meta() + pair()[0] + pair()[1][:-4])
        job = self.enqueue()
        for _ in range(3):
            result = iq.stage_incremental(self.spool, budget=self.budget)[0]
            self.assertEqual(result["phase"], "pending")
        _, prior = self.store_state(job)
        iq.stage_incremental(self.spool, budget=self.budget)
        self.assertEqual(self.store_state(job)[1].revision, prior.revision)
        with self.path.open("ab") as stream:
            stream.write(pair()[1][-4:])
        self.enqueue()
        result = iq.stage_incremental(self.spool, budget=self.budget)[0]
        self.assertEqual(result["phase"], "staged")
        self.assertEqual(queue.status(self.spool)[0]["staged"], 2)

    def test_wake_during_commit_remains_pending_for_later_capture(self):
        self.enqueue()
        for _ in range(2):
            def wake(point):
                if point == "before_queue_commit":
                    self.enqueue()
            iq.stage_incremental(self.spool, budget=self.budget, boundary=wake)
        state = queue.status(self.spool)[0]
        self.assertTrue(state["pending"])
        self.assertEqual(state["requested"], 3)
        self.assertEqual(state["staged"], 2)

    def test_crash_after_store_before_queue_commit_recovers_in_fresh_cli(self):
        job = self.enqueue()
        # Unbounded records count only for this small fixture so first step reaches EOF.
        crash_script = self.root / "crash.py"
        crash_script.write_text("import sys,os\nsys.path.insert(0, " + repr(str(Path(queue.__file__).parent)) + ")\n"
            "import incremental_queue as iq\n"
            "def crash(point):\n    if point=='before_queue_commit': os._exit(37)\n"
            "iq.stage_incremental(" + repr(str(self.spool)) + ", boundary=crash)\n")
        crashed = subprocess.run([sys.executable, "-B", str(crash_script)], capture_output=True, timeout=20,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(crashed.returncode, 37, crashed.stderr)
        _, snap = self.store_state(job)
        self.assertEqual(snap.revision, 1)
        self.assertTrue(queue.status(self.spool)[0]["pending"])
        self.assertEqual(self.cli("incremental")[0]["phase"], "staged")
        self.assertEqual(self.store_state(job)[1].revision, 1)

    def test_oversized_job_does_not_starve_other_job_in_bounded_passes(self):
        self.enqueue()
        bad = self.source / "rollout-large.jsonl"
        bad.write_bytes(meta() + b"x" * 4000 + b"\n")
        self.enqueue({**self.config, "files": [bad.name]})
        for _ in range(6):
            self.cli("incremental", limits={"jobs_per_pass": 1})
        states = queue.status(self.spool)
        self.assertEqual(sum(row["pending"] for row in states), 1)

    def test_one_job_multiple_files_rotates_even_when_first_file_stalls(self):
        other = self.source / "rollout-other.jsonl"
        other.write_bytes(self.path.read_bytes())
        self.path.write_bytes(meta() + b"x" * 4000 + b"\n")
        job = self.enqueue({**self.config, "files": [self.path.name, other.name]})
        for _ in range(5):
            iq.stage_incremental(self.spool, budget=self.budget)
        store = ist.Store(self.spool / "incremental-store")
        info = other.stat()
        snap = store.resume(iq.opaque([job, other.name]), iq.opaque([info.st_dev, info.st_ino]), iq.parser_id())
        self.assertEqual(snap.offset, info.st_size)
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_replacement_and_parser_upgrade_refuse_without_reset(self):
        job = self.enqueue()
        iq.stage_incremental(self.spool, budget=self.budget)
        _, snap = self.store_state(job)
        with mock.patch.object(iq, "parser_id", return_value="future-parser"):
            with self.assertRaises(ist.StoreConflict):
                iq.stage_incremental(self.spool, budget=self.budget)
        self.assertEqual(self.store_state(job)[1], snap)
        other = self.source / "replacement.jsonl"
        other.write_bytes(self.path.read_bytes())
        os.replace(other, self.path)
        self.assertEqual(self.cli("incremental", fail=True)["error"], "SourceChanged")
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_mode_switch_cannot_complete_incremental_wakes_as_snapshot(self):
        self.enqueue()
        iq.stage_incremental(self.spool, budget=self.budget)
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool)
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_claude_actual_cli_resumes_and_retains_approved_human_card(self):
        records = [line({"type": "user", "uuid": "synthetic-user-" + str(n),
                         "sessionId": "synthetic-claude", "timestamp": "2026-10-10T09:00:00Z",
                         "message": {"role": "user", "content": "keep the cache local " + str(n)}})
                   for n in range(3)]
        self.path.write_bytes(b"".join(records))
        job = self.enqueue({**self.config, "kind": "claude"})
        self.assertEqual(self.cli("incremental")[0]["phase"], "pending")
        self.assertEqual(self.cli("incremental")[0]["phase"], "staged")
        store, snap = self.store_state(job)
        page = store.batches(snap.source, snap.generation, snap.parser)
        self.assertEqual(sum(len(output.splitlines()) for _, output, _ in page.items), 3)
        self.assertEqual(snap.offset, sum(map(len, records)))

    def test_malformed_record_does_not_commit_partial_window(self):
        self.path.write_bytes(meta() + b"not-json\n")
        job = self.enqueue()
        self.assertEqual(self.cli("incremental", fail=True)["error"], "CaptureLimitError")
        store, snap = self.store_state(job)
        self.assertEqual((snap.offset, snap.revision), (0, 0))
        self.assertEqual(store.batches(snap.source, snap.generation, snap.parser).items, ())
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_exec_rollout_is_not_enrolled_as_interactive_context(self):
        self.path.write_bytes(meta().replace(b'"cli"', b'"exec"') + b"".join(pair()))
        self.enqueue()
        self.assertEqual(self.cli("incremental", fail=True)["error"], "CaptureLimitError")
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_invalid_operator_settings_emit_only_error_class(self):
        self.enqueue()
        self.assertEqual(self.cli("incremental", limits={"private-native-path": "private-value"},
                                  fail=True), {"phase": "pending", "error": "TypeError"})

    def test_secret_only_present_in_private_pending_state_never_output_or_cli(self):
        self.path.write_bytes(meta() + b"".join(pair("-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 60)))
        job = self.enqueue()
        for _ in range(2):
            result = self.cli("incremental")
            self.assertNotIn("BEGIN RSA", json.dumps(result))
        store, snap = self.store_state(job)
        page = store.batches(snap.source, snap.generation, snap.parser)
        self.assertFalse(any(b"BEGIN RSA" in output + rejected for _, output, rejected in page.items))
        self.assertTrue(any(rejected for _, _, rejected in page.items))

    def test_dialect_defaults_capture_large_first_and_operator_records(self):
        # Unknown native fields enlarge whole records; approved human text stays
        # short. Both enrollment and capture must use the dialect budget.
        padding = "x" * (300 * 1024)
        for kind in ("codex", "claude"):
            with self.subTest(kind=kind):
                spool = self.root / ("spool-" + kind)
                if kind == "codex":
                    records = [json.loads(meta())] + [json.loads(r) for r in pair()]
                else:
                    records = [{"type": "user", "uuid": "synthetic-large-user", "sessionId": "synthetic",
                                "timestamp": "2026-10-10T09:00:00Z",
                                "message": {"role": "user", "content": "keep the cache local"}}]
                for record in records:
                    record["synthetic_padding"] = padding
                self.path.write_bytes(b"".join(line(r) for r in records))
                job = queue.enqueue(spool, {**self.config, "kind": kind})["job"]
                self.assertEqual(iq.stage_incremental(spool)[0]["phase"], "staged")
                info = self.path.stat()
                store = ist.Store(spool / "incremental-store")
                snap = store.resume(iq.opaque([job, self.path.name]), iq.opaque([info.st_dev, info.st_ino]), iq.parser_id())
                outputs = store.batches(snap.source, snap.generation, snap.parser).items
                self.assertEqual(sum(len(out.splitlines()) for _, out, _ in outputs), 1)
                self.assertEqual(snap.offset, info.st_size)

    def test_complete_oversize_persists_across_cli_then_reviewed_retry(self):
        large = json.loads(meta())
        large["synthetic_padding"] = "x" * 1200
        record = line(large)
        self.path.write_bytes(record + b"".join(pair()))
        job = self.enqueue()
        result = self.cli("incremental")[0]
        self.assertEqual((result["phase"], result["safe_offset"], result["reason"]),
                         ("pending", 0, "oversized_record"))
        blocked = self.cli("status")[0]["blocked_records"][0]
        self.assertEqual((blocked["scope"], blocked["length"], blocked["offset"]),
                         ("complete_record", len(record), 0))
        self.assertEqual(blocked["sha256"], hashlib.sha256(record).hexdigest())
        self.assertIsNone(queue.status(self.spool)[0]["error"])
        raised = nw.Budget(read_bytes=8192, record_bytes=4096)
        self.assertEqual(iq.stage_incremental(self.spool, budget=raised)[0]["phase"], "staged")
        self.assertEqual(queue.status(self.spool)[0]["blocked_records"], [])
        self.assertEqual(self.store_state(job)[1].offset, self.path.stat().st_size)

    def test_prefix_oversize_never_claims_full_record_digest(self):
        large = json.loads(meta())
        large["synthetic_padding"] = "x" * 6000
        record = line(large)
        self.path.write_bytes(record + b"".join(pair()))
        self.enqueue()
        self.cli("incremental")
        blocked = self.cli("status")[0]["blocked_records"][0]
        self.assertFalse(blocked["record_complete"])
        self.assertEqual(blocked["scope"], "observed_prefix")
        self.assertEqual(blocked["length"], self.budget.read_bytes)
        self.assertEqual(blocked["sha256"], hashlib.sha256(record[:self.budget.read_bytes]).hexdigest())
        self.assertNotEqual(blocked["sha256"], hashlib.sha256(record).hexdigest())

    def test_unfinished_first_record_just_under_limit_waits_for_writer(self):
        large = json.loads(meta())
        large["synthetic_padding"] = "x" * 1200
        record = line(large)
        self.path.write_bytes(record[:self.budget.record_bytes - 1])
        self.enqueue()
        first = iq.stage_incremental(self.spool, budget=self.budget)[0]
        self.assertEqual((first["phase"], first["reason"], first["safe_offset"]),
                         ("pending", "partial_tail", 0))
        self.assertEqual(queue.status(self.spool)[0]["blocked_records"], [])
        self.assertIsNone(queue.status(self.spool)[0]["error"])
        with self.path.open("ab") as stream:
            stream.write(record[self.budget.record_bytes - 1:])
        self.enqueue()
        second = iq.stage_incremental(self.spool, budget=self.budget)[0]
        self.assertEqual((second["phase"], second["reason"], second["safe_offset"]),
                         ("pending", "oversized_record", 0))
        self.assertEqual(len(queue.status(self.spool)[0]["blocked_records"]), 1)

    def test_equal_budget_prefix_rewrite_refuses_and_preserves_observation(self):
        large = json.loads(meta())
        large["synthetic_padding"] = "x" * 6000
        record = line(large)
        self.path.write_bytes(record)
        self.enqueue()
        iq.stage_incremental(self.spool, budget=self.budget)
        prior = queue.status(self.spool)[0]["blocked_records"]
        with self.path.open("r+b") as stream:
            stream.seek(self.budget.record_bytes - 1)
            stream.write(b"y")
        with self.assertRaises(nw.SourceChanged):
            iq.stage_incremental(self.spool, budget=self.budget)
        state = queue.status(self.spool)[0]
        self.assertEqual(state["blocked_records"], prior)
        self.assertEqual(state["error"], "SourceChanged")
        self.assertTrue(state["pending"])

    def test_another_file_cannot_clear_durable_blocked_record(self):
        other = self.source / "rollout-other.jsonl"
        other.write_bytes(self.path.read_bytes())
        self.path.write_bytes(meta() + b"x" * 4000 + b"\n")
        self.enqueue({**self.config, "files": [self.path.name, other.name]})
        first = iq.stage_incremental(self.spool, budget=self.budget)[0]
        self.assertEqual(first["reason"], "oversized_record")
        blocked = queue.status(self.spool)[0]["blocked_records"]
        # The next dispatch handles only the other selected file. A later
        # revisit of the blocked file may observe a larger verified prefix as
        # its preceding complete records have now committed.
        iq.stage_incremental(self.spool, budget=self.budget)
        self.assertEqual(queue.status(self.spool)[0]["blocked_records"], blocked)

    def test_default_limits_are_distinct_and_operator_override_is_preserved(self):
        self.assertEqual((iq.dialect_budget("claude").record_bytes, iq.dialect_budget("claude").read_bytes),
                         (4 * 1024 * 1024, 8 * 1024 * 1024))
        self.assertEqual((iq.dialect_budget("codex").record_bytes, iq.dialect_budget("codex").read_bytes),
                         (16 * 1024 * 1024, 32 * 1024 * 1024))
        # A small operator override still blocks instead of silently applying
        # larger defaults; the other tests exercise the retry path.
        large = json.loads(meta())
        large["synthetic_padding"] = "x" * 1200
        self.path.write_bytes(line(large))
        self.enqueue()
        self.assertEqual(iq.stage_incremental(self.spool, budget=self.budget)[0]["reason"], "oversized_record")

    def test_selected_hardlink_aliases_refuse_before_parser_enrollment(self):
        alias = self.source / "rollout-alias.jsonl"
        os.link(self.path, alias)
        self.enqueue({**self.config, "files": [self.path.name, alias.name]})
        with mock.patch.object(iq, "enroll", side_effect=AssertionError("must not parse aliases")):
            with self.assertRaises(queue.CaptureLimitError):
                iq.stage_incremental(self.spool, budget=self.budget)
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_truncated_enrolled_file_does_not_reset_committed_offset(self):
        job = self.enqueue()
        iq.stage_incremental(self.spool, budget=self.budget)
        _, prior = self.store_state(job)
        self.path.write_bytes(meta())
        self.enqueue()
        with self.assertRaises(nw.SourceChanged):
            iq.stage_incremental(self.spool, budget=self.budget)
        self.assertEqual(self.store_state(job)[1], prior)
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_same_timestamp_idless_prompts_match_single_window_identity(self):
        records = [meta(), *pair('First distinct synthetic prompt.'), *pair('Second distinct synthetic prompt.')]
        self.path.write_bytes(b''.join(records))
        job = self.enqueue()
        offset, reference = 0, []
        for record in records:
            reference.append((offset, record))
            offset += len(record)
        cp = iq.loader.new_checkpoint('codex', {'session': 'synthetic-session'})
        raws, _, _ = iq.loader.read_file(reference, cp, seat=self.config['seat'])
        expected, _ = iq.loader.approved_cards(raws, self.config['seat'], self.path.name)
        self.assertEqual(len(expected), 2)
        for _ in range(5):
            self.cli('incremental')
        store, snap = self.store_state(job)
        page = store.batches(snap.source, snap.generation, snap.parser)
        actual = {json.loads(row)['id']: json.loads(row) for _, out, _ in page.items for row in out.splitlines()}
        self.assertEqual(actual, expected)

    def test_held_item_replay_does_not_reset_or_double_increment_fallback_counter(self):
        self.path.write_bytes(meta() + b''.join(pair('First distinct synthetic prompt.')) + pair('Second distinct synthetic prompt.')[0])
        job = self.enqueue()
        for _ in range(4):
            iq.stage_incremental(self.spool, budget=self.budget)
        store, prior = self.store_state(job)
        with self.path.open('ab') as stream:
            stream.write(pair('Second distinct synthetic prompt.')[1])
        self.enqueue()
        self.cli('incremental')
        store, snap = self.store_state(job)
        page = store.batches(snap.source, snap.generation, snap.parser)
        ids = {json.loads(row)['id'] for _, out, _ in page.items for row in out.splitlines()}
        self.assertEqual(len(ids), 2)
        self.assertEqual(snap.parser_state()['codex_fallback_last'][1], 2)

    def test_codex_pair_incompatible_overrides_refuse_before_enrollment(self):
        self.enqueue()
        for budget in (nw.Budget(read_bytes=2048, record_bytes=1024, records=1),
                       nw.Budget(read_bytes=2047, record_bytes=1024, records=2)):
            with self.subTest(budget=budget):
                with self.assertRaisesRegex(queue.CaptureLimitError, 'Codex.*records.*read_bytes'):
                    iq.stage_incremental(self.spool, budget=budget)
                self.assertTrue(queue.status(self.spool)[0]['pending'])


if __name__ == "__main__":
    unittest.main()
