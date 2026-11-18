"""Real-file, synthetic-only checks for bounded raw incremental input.

No parsing, upload, native history or integrated recovery claim. The large-file
case writes and drains 778 MiB in a temporary directory; it is opt-in.
"""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import native_window as windows


class ReadMeter:
    """Intercept real file reads without using the reader's own counters."""
    def __init__(self):
        self.requested = 0
        self.returned = 0
        self.calls = 0
        self.open_stream = os.fdopen

    def wrap(self, *args, **kwargs):
        stream, meter = self.open_stream(*args, **kwargs), self
        class MeasuredStream:
            def __enter__(self):
                stream.__enter__()
                return self
            def __exit__(self, *args):
                return stream.__exit__(*args)
            def fileno(self):
                return stream.fileno()
            def seek(self, *args):
                return stream.seek(*args)
            def read(self, size):
                if type(size) is not int or size < 0:
                    raise AssertionError("unbounded source read")
                meter.requested += size
                meter.calls += 1
                result = stream.read(size)
                meter.returned += len(result)
                return result
        return MeasuredStream()


class NativeWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "rollout-synthetic.jsonl"
        self.path.write_bytes(b"")
        self.budget = windows.Budget(read_bytes=32, record_bytes=16, records=3)

    def read(self, offset=0, target=None, identity=None, budget=None):
        return windows.read_window(self.path,
            identity or windows.Identity.from_stat(self.path.stat()), offset,
            self.path.stat().st_size if target is None else target, budget or self.budget)

    def test_crlf_original_offsets_and_partial_tail_are_preserved(self):
        self.path.write_bytes(b'{"n":1}\r\n{"n":2}\n{"n":')
        first = self.read()
        self.assertEqual([(row.offset, row.data) for row in first.records],
                         [(0, b'{"n":1}\r\n'), (9, b'{"n":2}\n')])
        self.assertEqual((first.next_offset, first.reason), (17, "partial_tail"))
        self.assertFalse(first.reached_target)
        with self.path.open("ab") as stream:
            stream.write(b"3}\n")
        second = self.read(offset=first.next_offset)
        self.assertEqual([(row.offset, row.data) for row in second.records], [(17, b'{"n":3}\n')])
        self.assertTrue(second.reached_target)

    def test_record_budget_replays_unreturned_records_without_advance(self):
        self.path.write_bytes(b"a\nb\nc\nd\n")
        first = self.read()
        self.assertEqual((first.reason, first.next_offset), ("record_budget", 6))
        second = self.read(offset=first.next_offset)
        self.assertEqual([(row.offset, row.data) for row in second.records], [(6, b"d\n")])

    def test_byte_budget_keeps_a_fragment_for_next_pass(self):
        self.path.write_bytes(b"123456789\n" * 5)
        first = self.read(budget=replace(self.budget, records=10))
        self.assertEqual((first.reason, first.next_offset, first.observed_bytes), ("byte_budget", 30, 32))
        second = self.read(offset=30)
        self.assertEqual([row.offset for row in second.records], [30, 40])
        self.assertTrue(second.reached_target)

    def test_oversized_complete_and_unterminated_record_remain_pending(self):
        for suffix in (b"\n", b""):
            with self.subTest(suffix=suffix):
                self.path.write_bytes(b"ok\n" + b"x" * 17 + suffix)
                first = self.read()
                self.assertEqual((first.reason, first.next_offset), ("oversized_record", 3))
                self.assertFalse(first.reached_target)
                second = self.read(offset=3)
                self.assertEqual((second.reason, second.next_offset, second.records),
                                 ("oversized_record", 3, ()))

    def test_captured_target_does_not_consume_later_append(self):
        self.path.write_bytes(b"first\n")
        target = self.path.stat().st_size
        identity = windows.Identity.from_stat(self.path.stat())
        with self.path.open("ab") as stream:
            stream.write(b"later\n")
        first = self.read(target=target, identity=identity)
        self.assertEqual([row.data for row in first.records], [b"first\n"])
        self.assertTrue(first.reached_target)

    def test_no_dialect_interpretation_or_private_text_logging(self):
        self.path.write_bytes(b"not JSON\n")
        first = self.read()
        self.assertEqual(first.records[0].data, b"not JSON\n")
        self.assertTrue(first.reached_target)
        self.assertNotIn("not JSON", first.reason)
        self.assertNotIn("not JSON", repr(first))
        self.assertNotIn("not JSON", repr(first.records[0]))

    def test_io_diagnostic_omits_native_path(self):
        self.path.unlink()
        with self.assertRaises(windows.SourceIOError) as caught:
            windows.read_window(self.path, windows.Identity(1, 2), 0, 0)
        self.assertNotIn(str(self.path), str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_actual_read_calls_are_bounded_and_match_counter(self):
        self.path.write_bytes(b"a\n" * 100)
        meter = ReadMeter()
        with mock.patch.object(windows.os, "fdopen", side_effect=meter.wrap):
            part = self.read(offset=2)
        self.assertLessEqual(meter.requested, 2 * self.budget.read_bytes + 2)
        self.assertEqual((meter.returned, meter.requested, meter.calls),
                         (part.bytes_read, part.bytes_read, 4))

    def test_replacement_truncation_and_nonboundary_offset_fail_closed(self):
        self.path.write_bytes(b"first\nsecond\n")
        identity = windows.Identity.from_stat(self.path.stat())
        target = self.path.stat().st_size
        self.path.rename(self.root / "old-source")
        self.path.write_bytes(b"first\nsecond\n")
        with self.assertRaises(windows.SourceChanged):
            self.read(identity=identity, target=target)
        self.path.write_bytes(b"first\n")
        with self.assertRaises(windows.SourceChanged):
            self.read(target=target)
        with self.assertRaises(windows.SourceChanged):
            self.read(offset=2)

    def test_direct_symlink_is_rejected(self):
        indirect = self.root / "indirect.jsonl"
        try:
            indirect.symlink_to(self.path)
        except OSError:
            self.skipTest("host does not permit creating synthetic symlinks")
        with self.assertRaises(windows.SourceChanged):
            windows.read_window(indirect, windows.Identity.from_stat(self.path.stat()), 0, 0)

    def test_window_and_prior_boundary_rewrites_during_read_are_rejected(self):
        self.path.write_bytes(b"a\nb\n")
        actual = windows.os.fdopen
        for boundary in (False, True):
            with self.subTest(boundary=boundary):
                self.path.write_bytes(b"a\nb\n")
                class MutatingStream:
                    def __init__(inner, stream):
                        inner.stream = stream
                        inner.calls = 0
                    def __enter__(inner):
                        return inner
                    def __exit__(inner, *args):
                        inner.stream.close()
                    def __getattr__(inner, name):
                        return getattr(inner.stream, name)
                    def read(inner, size):
                        data = inner.stream.read(size)
                        inner.calls += 1
                        if inner.calls == 2:
                            with self.path.open("r+b", buffering=0) as writer:
                                writer.seek(1 if boundary else 2)
                                writer.write(b"X")
                        return data
                with mock.patch.object(windows.os, "fdopen", side_effect=lambda *a, **k: MutatingStream(actual(*a, **k))):
                    with self.assertRaises(windows.SourceChanged):
                        self.read(offset=2)

    def test_fresh_process_continues_at_original_offset(self):
        self.path.write_bytes(b"a\nb\nc\nd\n")
        first = self.read()
        code = """import json,sys
from pathlib import Path
import native_window as w
p=Path(sys.argv[1]); s=p.stat()
r=w.read_window(p,w.Identity.from_stat(s),int(sys.argv[2]),s.st_size,w.Budget(32,16,3))
print(json.dumps({'offset':r.next_offset,'rows':[x.offset for x in r.records],'reason':r.reason}))
"""
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        child = subprocess.run([getattr(sys, "_base_executable", sys.executable), "-c", code,
                                str(self.path), str(first.next_offset)], cwd=Path(__file__).parent,
                               capture_output=True, text=True, timeout=10, **options)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), '{"offset": 8, "rows": [6], "reason": "target"}')

    def test_empty_target_and_invalid_budgets(self):
        first = self.read()
        self.assertEqual((first.records, first.bytes_read, first.reason), ((), 0, "target"))
        self.assertTrue(first.reached_target)
        for options in ({"read_bytes": True}, {"record_bytes": 0}, {"records": -1},
                        {"read_bytes": 16, "record_bytes": 16}, {"seconds": float("nan")},
                        {"seconds": float("inf")}):
            with self.subTest(options=options), self.assertRaises(windows.WindowError):
                windows.Budget(**options)
        with self.assertRaises(windows.WindowError):
            windows.read_window(self.path, windows.Identity.from_stat(self.path.stat()), 0, 0, False)
        with mock.patch.object(windows.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaises(windows.WindowDeadline):
                self.read()

    @unittest.skipUnless(os.environ.get("MESHY_SYNTHETIC_LARGE_WINDOW") == "1",
                         "opt-in 778 MiB synthetic read-bound acceptance")
    def test_778_mib_source_drains_with_bounded_reads_and_small_source_interleaving(self):
        line = b'{"synthetic":"' + b"x" * (4096 - 17) + b'"}\n'
        self.assertEqual(len(line), 4096)
        block = line * 256
        expected = hashlib.sha256()
        with self.path.open("wb") as stream:
            for _ in range(778):
                stream.write(block)
                expected.update(block)
        target = self.path.stat().st_size
        self.assertEqual(target, 778 * 1024 * 1024)
        identity = windows.Identity.from_stat(self.path.stat())
        small = self.root / "small.jsonl"
        small.write_bytes(b'{"small":true}\n')
        small_identity = windows.Identity.from_stat(small.stat())
        budget = windows.Budget(record_bytes=8192)
        offset, passes, maximum_read, maximum_observed = 0, 0, 0, 0
        found = hashlib.sha256()
        while offset < target:
            meter = ReadMeter()
            with mock.patch.object(windows.os, "fdopen", side_effect=meter.wrap):
                part = windows.read_window(self.path, identity, offset, target, budget)
            self.assertGreater(part.next_offset, offset)
            for row in part.records:
                self.assertEqual(row.offset, offset)
                found.update(row.data)
                offset += len(row.data)
            self.assertEqual(offset, part.next_offset)
            self.assertLessEqual(part.bytes_read, 2 * budget.read_bytes + 2)
            self.assertEqual(meter.returned, part.bytes_read)
            self.assertLessEqual(meter.requested, 2 * budget.read_bytes + 2)
            maximum_read = max(maximum_read, part.bytes_read)
            maximum_observed = max(maximum_observed, part.observed_bytes)
            passes += 1
            if passes in (1, 100, 400, 778):
                independent = windows.read_window(small, small_identity, 0, small.stat().st_size)
                self.assertTrue(independent.reached_target)
        self.assertEqual(passes, 778)
        self.assertEqual(found.hexdigest(), expected.hexdigest())
        print("SYNTHETIC_WINDOW_BOUND", {"source_bytes": target, "passes": passes,
              "max_read_bytes_per_pass": maximum_read, "max_observed_bytes": maximum_observed,
              "raw_bytes_written_by_reader": 0, "queue_fairness_proved": False,
              "parser_checkpoint_proved": False, "graph_recovery_proved": False})


if __name__ == "__main__":
    unittest.main()
