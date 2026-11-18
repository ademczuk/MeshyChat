"""Synthetic fresh-process crash and replay proofs; no native history or graph."""
from dataclasses import replace
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import incremental_store as stores

SOURCE, GENERATION, PARSER = "1" * 64, "2" * 64, "synthetic.parser:1"
META = {"fixture": "synthetic-only"}
INITIAL = {"pending": "synthetic question", "task": "OPEN"}
FINAL = {"pending": None, "task": "OPEN", "next": 19}
OUTPUT, DROPS = b'{"approved":"synthetic answer"}\n', b'[]\n'


def child(mode, folder, argument):
    store = stores.Store(folder)
    expected = store.resume(SOURCE, GENERATION, PARSER)
    if mode == "list":
        page = store.batches(SOURCE, GENERATION, PARSER, after_revision=int(argument), limit=1)
        print(json.dumps({"keys": [item[0].key for item in page.items],
                          "digests": [item[0].output_sha256 for item in page.items],
                          "next_revision": page.next_revision, "has_more": page.has_more,
                          "checkpoint_revision": page.snapshot.revision}))
        return 0
    if mode == "race":
        ready = Path(folder) / ("ready-" + argument)
        ready.write_text("ready")
        deadline = time.monotonic() + 20
        while not (Path(folder) / "go").exists():
            if time.monotonic() > deadline:
                return 74
            time.sleep(.02)
    def boundary(point):
        if mode == "crash" and point == argument:
            os._exit(73)
    store.commit(expected, 19, OUTPUT, DROPS, FINAL, boundary=boundary)
    return 0


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "private"
        self.store = stores.Store(self.directory)
        self.initial = self.store.enroll(SOURCE, GENERATION, PARSER, META, INITIAL)

    def count_batches(self):
        with self.database() as db:
            return db.execute("SELECT count(*) FROM batches").fetchone()[0]

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.store.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def command(self, mode, argument):
        return [sys.executable, str(Path(__file__).absolute()), "--child", mode,
                str(self.directory), argument]

    def capture_three(self):
        state, batches = self.initial, []
        for revision in range(1, 4):
            batches.append(self.store.commit(state, revision * 19, OUTPUT, DROPS,
                                             {"revision": revision, "task": "OPEN"}))
            state = self.store.resume(SOURCE, GENERATION, PARSER)
        return batches, state

    def test_fresh_process_replays_all_batches_without_advancing_state(self):
        batches, state = self.capture_three()
        for after, expected in enumerate(batches):
            result = subprocess.run(self.command("list", str(after)), timeout=30,
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            page = json.loads(result.stdout)
            self.assertEqual(page, {"keys": [expected.key],
                                    "digests": [expected.output_sha256],
                                    "next_revision": after + 1, "has_more": after < 2,
                                    "checkpoint_revision": 3})
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), state)
        self.assertEqual(self.count_batches(), 3)
        replay = self.store.batches(SOURCE, GENERATION, PARSER, limit=1)
        self.assertEqual(replay.items[0], (batches[0], OUTPUT, DROPS))
        empty = self.store.batches(SOURCE, GENERATION, PARSER, after_revision=3)
        self.assertEqual((empty.items, empty.next_revision, empty.has_more), ((), 3, False))

    def test_replay_byte_budget_keeps_next_batch_pending(self):
        batches, _ = self.capture_three()
        size = len(OUTPUT) + len(DROPS)
        page = self.store.batches(SOURCE, GENERATION, PARSER, byte_limit=size + 1)
        self.assertEqual([item[0] for item in page.items], [batches[0]])
        self.assertEqual((page.next_revision, page.has_more), (1, True))
        with self.assertRaises(stores.StoreError):
            self.store.batches(SOURCE, GENERATION, PARSER, byte_limit=size - 1)

    def test_replay_refuses_old_corruption_despite_valid_latest_checkpoint(self):
        batches, state = self.capture_three()
        with self.database() as db:
            db.execute("UPDATE batches SET output=? WHERE key=?", (b"damaged", batches[0].key))
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), state)
        with self.assertRaises(stores.StoreConflict):
            self.store.batches(SOURCE, GENERATION, PARSER)

    def test_replay_refuses_missing_old_batch_instead_of_false_drain(self):
        batches, state = self.capture_three()
        with self.database() as db:
            db.execute("DELETE FROM batches WHERE key=?", (batches[1].key,))
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), state)
        with self.assertRaises(stores.StoreConflict):
            self.store.batches(SOURCE, GENERATION, PARSER)
        with self.assertRaises(stores.StoreConflict):
            self.store.batches(SOURCE, GENERATION, PARSER, after_revision=1, limit=1)

    def test_replay_enrollment_and_position_are_checked_even_when_empty(self):
        empty = self.store.batches(SOURCE, GENERATION, PARSER)
        self.assertEqual((empty.items, empty.has_more), ((), False))
        for generation, parser, after in (("3" * 64, PARSER, 0),
                                          (GENERATION, "synthetic.parser:2", 0),
                                          (GENERATION, PARSER, 1)):
            with self.assertRaises(stores.StoreConflict):
                self.store.batches(SOURCE, generation, parser, after_revision=after)
        for kwargs in ({"after_revision": True}, {"limit": 0}, {"limit": 1025},
                       {"byte_limit": self.store.limits.output_bytes + 1}):
            with self.assertRaises(stores.StoreError):
                self.store.batches(SOURCE, GENERATION, PARSER, **kwargs)

    def test_real_process_exit_each_transaction_boundary(self):
        for point in ("before_output", "after_output", "after_state", "after_commit"):
            with self.subTest(point=point):
                result = subprocess.run(self.command("crash", point), timeout=30,
                                        capture_output=True)
                self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))
                reopened = stores.Store(self.directory)
                state = reopened.resume(SOURCE, GENERATION, PARSER)
                if point != "after_commit":
                    self.assertEqual(state, self.initial)
                    self.assertEqual(self.count_batches(), 0)
                else:
                    self.assertEqual((state.offset, state.revision, state.parser_state()),
                                     (19, 1, FINAL))
                    self.assertEqual(reopened.committed(state.batch)[1:], (OUTPUT, DROPS))
                    batch = reopened.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
                    self.assertEqual(batch.key, state.batch)
                    self.assertEqual(self.count_batches(), 1)

    def test_two_fresh_writers_same_snapshot_replay_one_batch(self):
        options = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        processes = [subprocess.Popen(self.command("race", str(n)), **options) for n in range(2)]
        try:
            deadline = time.monotonic() + 20
            while not all((self.directory / ("ready-" + str(n))).exists() for n in range(2)):
                if time.monotonic() > deadline:
                    self.fail("fresh writers did not reach the bounded test barrier")
                time.sleep(.02)
            (self.directory / "go").write_text("go")
            for process in processes:
                _, errors = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, errors.decode(errors="replace"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
        self.assertEqual(self.count_batches(), 1)
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER).revision, 1)

    def test_reader_sees_old_state_and_no_output_during_uncommitted_write(self):
        observed = []
        def boundary(point):
            if point not in ("after_output", "after_state"):
                return
            with self.database() as db:
                observed.append((db.execute("SELECT offset,revision FROM sources").fetchone(),
                                 db.execute("SELECT count(*) FROM batches").fetchone()[0]))
        self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL, boundary=boundary)
        self.assertEqual(observed, [((0, 0), 0), ((0, 0), 0)])

    def test_stale_different_output_refused_and_enrollment_preserves_progress(self):
        batch = self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
        with self.assertRaises(stores.StoreConflict):
            self.store.commit(self.initial, 19, b"different", DROPS, FINAL)
        current = self.store.enroll(SOURCE, GENERATION, PARSER, META, INITIAL)
        self.assertEqual((current.batch, current.parser_state()), (batch.key, FINAL))
        for generation, parser, metadata in (("3" * 64, PARSER, META),
                                              (GENERATION, "parser:2", META),
                                              (GENERATION, PARSER, {"changed": True})):
            with self.assertRaises(stores.StoreConflict):
                self.store.enroll(SOURCE, generation, parser, metadata, INITIAL)
        self.assertEqual(self.count_batches(), 1)

    def test_same_offset_pending_state_is_durable_but_empty_and_backward_refused(self):
        with self.assertRaises(stores.StoreError):
            self.store.commit(self.initial, 0, b"", b"", INITIAL)
        updated = {"pending": "synthetic follow-up", "task": "OPEN"}
        self.store.commit(self.initial, 0, b"", b"", updated)
        current = stores.Store(self.directory).resume(SOURCE, GENERATION, PARSER)
        self.assertEqual((current.offset, current.revision, current.parser_state()), (0, 1, updated))
        with self.assertRaises(stores.StoreError):
            self.store.commit(current, -1, OUTPUT, b"", FINAL)

    def test_sqlite_update_failure_rolls_back_output_and_suppresses_context(self):
        with self.database() as db:
            db.execute("CREATE TRIGGER fail_state BEFORE UPDATE ON sources BEGIN "
                       "SELECT RAISE(ABORT,'synthetic-private-message'); END")
        with self.assertRaises(stores.StoreIOError) as caught:
            self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("synthetic-private-message", str(caught.exception))
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), self.initial)
        self.assertEqual(self.count_batches(), 0)

    def test_real_lock_is_retryable_without_advancing_and_retry_commits_once(self):
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertRaises(stores.StoreBusy) as caught:
                self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 8, "SQLite contention must have a bounded wait")
            self.assertIsNone(caught.exception.__context__)
            self.assertIsNone(caught.exception.__cause__)
            self.assertNotIn(str(self.directory), str(caught.exception))
            self.assertEqual(db.execute("SELECT offset,revision FROM sources").fetchone(), (0, 0))
            self.assertEqual(db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)
        reopened = stores.Store(self.directory)
        self.assertEqual(reopened.resume(SOURCE, GENERATION, PARSER), self.initial)
        first = reopened.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
        self.assertEqual(reopened.commit(self.initial, 19, OUTPUT, DROPS, FINAL), first)
        self.assertEqual(self.count_batches(), 1)

    def test_corrupt_database_is_not_retryable_and_diagnostics_are_private(self):
        self.store.path.write_bytes(b"synthetic-private-corrupt-database")
        with self.assertRaises(stores.StoreIOError) as caught:
            self.store.resume(SOURCE, GENERATION, PARSER)
        self.assertNotIsInstance(caught.exception, stores.StoreBusy)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertEqual(self.store.path.read_bytes(), b"synthetic-private-corrupt-database")

    def test_limits_do_not_advance_or_destroy_old_output(self):
        limited = stores.Store(self.directory, replace(stores.Limits(), output_bytes=1))
        with self.assertRaises(stores.StoreError):
            limited.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
        self.assertEqual(self.count_batches(), 0)
        batch = self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
        with self.assertRaises(stores.StoreError):
            limited.committed(batch.key)
        small_state = stores.Store(self.directory, replace(stores.Limits(), checkpoint_bytes=1))
        with self.assertRaises(stores.StoreError):
            small_state.resume(SOURCE, GENERATION, PARSER)
        one_batch = stores.Store(self.directory, replace(stores.Limits(), batches=1))
        current = self.store.resume(SOURCE, GENERATION, PARSER)
        with self.assertRaises(stores.StoreError):
            one_batch.commit(current, 20, b"x", b"", FINAL)
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), current)
        self.assertEqual(self.store.committed(batch.key)[1:], (OUTPUT, DROPS))

    def test_invalid_pending_state_and_source_count_do_not_change_progress(self):
        for state in ({"bad": float("nan")}, {"bad": object()}, ["not an object"]):
            with self.assertRaises(stores.StoreError):
                self.store.commit(self.initial, 19, OUTPUT, DROPS, state)
        limited = stores.Store(self.directory, replace(stores.Limits(), sources=1))
        with self.assertRaises(stores.StoreError):
            limited.enroll("4" * 64, GENERATION, PARSER, META, INITIAL)
        limited = stores.Store(self.directory, replace(stores.Limits(), checkpoint_bytes=80))
        with self.assertRaises(stores.StoreError):
            limited.commit(self.initial, 19, OUTPUT, DROPS, {"pending": "x" * 81})
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), self.initial)
        self.assertEqual(self.count_batches(), 0)

    def test_database_full_never_advances_checkpoint(self):
        with self.database() as db:
            pages, size = db.execute("PRAGMA page_count").fetchone()[0], db.execute("PRAGMA page_size").fetchone()[0]
        tight = stores.Store(self.directory, replace(stores.Limits(), database_bytes=pages * size))
        with self.assertRaises(stores.StoreIOError) as caught:
            tight.commit(self.initial, 19, b"x" * 100000, b"", FINAL)
        self.assertIsInstance(caught.exception, stores.StoreFull)
        self.assertNotIsInstance(caught.exception, stores.StoreBusy)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertEqual(tight.resume(SOURCE, GENERATION, PARSER), self.initial)
        self.assertEqual(self.count_batches(), 0)

    def test_enospc_maps_to_store_full_while_other_os_errors_stay_store_io_error(self):
        import errno as _errno

        @stores._private_errors
        def raise_os(code):
            raise OSError(code, "synthetic os failure", str(self.directory))

        with self.assertRaises(stores.StoreFull) as full_caught:
            raise_os(_errno.ENOSPC)
        self.assertIsNone(full_caught.exception.__context__)
        self.assertIsNone(full_caught.exception.__cause__)
        self.assertNotIn(str(self.directory), str(full_caught.exception))

        with self.assertRaises(stores.StoreIOError) as io_caught:
            raise_os(_errno.EIO)
        self.assertNotIsInstance(io_caught.exception, stores.StoreFull)
        self.assertNotIsInstance(io_caught.exception, stores.StoreBusy)
        self.assertNotIn(str(self.directory), str(io_caught.exception))

    def test_corrupt_or_missing_batch_cannot_pass_replay(self):
        batch = self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
        progressed = self.store.resume(SOURCE, GENERATION, PARSER)
        with self.database() as db:
            db.execute("UPDATE batches SET output=? WHERE key=?", (b"tampered", batch.key))
        for operation in (lambda: self.store.committed(batch.key),
                          lambda: self.store.resume(SOURCE, GENERATION, PARSER),
                          lambda: self.store.enroll(SOURCE, GENERATION, PARSER, META, INITIAL),
                          lambda: self.store.commit(progressed, 20, b"next", b"", FINAL),
                          lambda: self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL)):
            with self.assertRaises(stores.StoreConflict):
                operation()
        with self.database() as db:
            db.execute("DELETE FROM batches WHERE key=?", (batch.key,))
        for operation in (lambda: self.store.resume(SOURCE, GENERATION, PARSER),
                          lambda: self.store.enroll(SOURCE, GENERATION, PARSER, META, INITIAL),
                          lambda: self.store.commit(progressed, 20, b"next", b"", FINAL),
                          lambda: self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL)):
            with self.assertRaises(stores.StoreConflict):
                operation()

    def test_checkpoint_and_batch_link_disagreement_refuses_resume(self):
        self.store.commit(self.initial, 19, OUTPUT, DROPS, FINAL)
        for column, value in (("checkpoint", b'{"task":"CLOSED"}'),
                              ("offset", 20), ("revision", 2)):
            with self.subTest(column=column):
                with self.database() as db:
                    original = db.execute(f"SELECT {column} FROM sources").fetchone()[0]
                    db.execute(f"UPDATE sources SET {column}=?", (value,))
                try:
                    with self.assertRaises(stores.StoreConflict):
                        self.store.resume(SOURCE, GENERATION, PARSER)
                finally:
                    with self.database() as db:
                        db.execute(f"UPDATE sources SET {column}=?", (original,))

    def test_format_mismatch_is_preserved_and_private_repr_excludes_state(self):
        self.assertNotIn(INITIAL["pending"], repr(self.initial))
        self.assertNotIn("synthetic-only", repr(self.initial))
        with self.database() as db:
            db.execute("UPDATE format SET schema='unsupported.synthetic' ")
        with self.assertRaises(stores.StoreError):
            stores.Store(self.directory)
        with self.database() as db:
            self.assertEqual(db.execute("SELECT schema FROM format").fetchone()[0], "unsupported.synthetic")
            self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0], 1)

    def test_private_directory_before_database_creation(self):
        directory = self.root / "fresh"
        actual = stores.private_scratch
        def guarded(path):
            self.assertFalse((path / "incremental.sqlite").exists())
            actual(path)
        with mock.patch.object(stores, "private_scratch", side_effect=guarded):
            stores.Store(directory)

    @unittest.skipUnless(os.name == "nt", "Windows ACL proof")
    def test_actual_windows_private_directory_and_database_acl(self):
        directory = str(self.directory).replace("'", "''")
        script = "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        script += "$paths=@('" + directory + "','" + directory + "/incremental.sqlite'); "
        script += "$r=@(foreach($p in $paths){$a=Get-Acl -LiteralPath $p; "
        script += "$ids=@($a.Access | ForEach-Object {$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value}); "
        script += "[pscustomobject]@{Protected=$a.AreAccessRulesProtected;Ids=$ids;Current=$sid}}); ConvertTo-Json -Compress -InputObject $r"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        rows = json.loads(result.stdout)
        self.assertTrue(rows[0]["Protected"])
        for row in rows:
            self.assertEqual(set(row["Ids"]), {row["Current"], "S-1-5-18"})

    def test_indirect_directory_database_and_recovery_journal_rejected(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit synthetic symlink creation")
        with self.assertRaises(stores.StoreError):
            stores.Store(link)
        foreign = self.root / "foreign"
        foreign.write_bytes(b"not SQLite")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.store.path) + suffix)
            sidecar.symlink_to(foreign)
            try:
                with self.assertRaises(stores.StoreError):
                    stores.Store(self.directory)
                self.assertEqual(foreign.read_bytes(), b"not SQLite")
            finally:
                sidecar.unlink()
        self.store.path.unlink()
        self.store.path.symlink_to(foreign)
        with self.assertRaises(stores.StoreError):
            stores.Store(self.directory)
        self.assertEqual(foreign.read_bytes(), b"not SQLite")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        sys.exit(child(*sys.argv[2:]))
    unittest.main()
