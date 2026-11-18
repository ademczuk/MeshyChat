"""Synthetic durability checks; stdlib runner deliberately requires no database server."""
import hashlib
from contextlib import closing
from dataclasses import replace
import json
import os
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import capture_queue as queue


class CaptureQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "rollout-fixture.jsonl").write_bytes(b"")
        self.spool = self.root / "spool"
        self.config = {"seat": "codex-kxxx-cli", "kind": "codex", "root": str(self.source),
                       "files": ["rollout-fixture.jsonl"]}

    def fake_loader(self, project, seat, out, **options):
        self.assertTrue(options["rebuild"])
        path = Path(out)
        path.mkdir(parents=True, exist_ok=True)
        (path / "cards.jsonl").write_text('{"synthetic":true}\n', encoding="utf-8")
        return {"bad_lines": 0, "files": 1}

    def test_scan_catches_missed_wake_after_fresh_process_and_ignores_foreign_files(self):
        queue.enqueue(self.spool, self.config)
        queue.scan(self.spool)
        queue.stage(self.spool, self.fake_loader)
        self.assertEqual(queue.scan(self.spool)[0]["phase"], "unchanged")
        (self.source / "rollout-foreign.jsonl").write_bytes(b"not enrolled\n")
        self.assertEqual(queue.scan(self.spool)[0]["phase"], "unchanged")
        with (self.source / "rollout-fixture.jsonl").open("ab") as stream:
            stream.write(b"missed hook\n")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run([getattr(sys, "_base_executable", sys.executable),
                                 str(Path(queue.__file__)), "scan", "--spool", str(self.spool)],
                                capture_output=True, timeout=10, **options)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(json.loads(result.stdout)[0]["changed"])
        self.assertEqual((queue.status(self.spool)[0]["requested"],
                          queue.status(self.spool)[0]["staged"]), (3, 2))
        self.assertFalse(queue.scan(self.spool)[0]["changed"])

    def test_scan_only_stats_enrolled_files_and_detects_same_size_replacement(self):
        path = self.source / "rollout-fixture.jsonl"
        path.write_bytes(b"old\n")
        queue.enqueue(self.spool, self.config)
        with mock.patch.object(Path, "open", side_effect=AssertionError("scan read source bytes")):
            self.assertTrue(queue.scan(self.spool)[0]["changed"])
        prior = path.stat()
        path.rename(self.source / "preserved-original.jsonl")
        path.write_bytes(b"new\n")
        os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        self.assertTrue(queue.scan(self.spool)[0]["changed"])
        self.assertFalse(queue.scan(self.spool)[0]["changed"])

    def test_scan_observation_and_wake_roll_back_together_on_database_failure(self):
        key = queue.enqueue(self.spool, self.config)["job"]
        with closing(queue.database(self.spool)) as db, db:
            db.execute("""CREATE TRIGGER reject_wake BEFORE UPDATE OF requested ON jobs
                          BEGIN SELECT RAISE(ABORT, 'synthetic interrupted commit'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            queue.scan(self.spool)
        with closing(queue.database(self.spool)) as db, db:
            self.assertEqual(db.execute("SELECT observed,requested FROM jobs WHERE id=?", (key,)).fetchone(),
                             (None, 1))
            db.execute("DROP TRIGGER reject_wake")
        self.assertTrue(queue.scan(self.spool)[0]["changed"])
        self.assertEqual(queue.status(self.spool)[0]["requested"], 2)

    def test_missing_source_does_not_starve_other_scans_across_bounded_passes(self):
        for seat in ("codex-kxxx-cli", "codex-oxxxxxx-cli", "codex-oxxxxxx-admin-cli"):
            config = {**self.config, "seat": seat}
            if seat == "codex-kxxx-cli":
                (self.source / "rollout-missing.jsonl").write_bytes(b"")
                config["files"] = ["rollout-missing.jsonl"]
            queue.enqueue(self.spool, config)
        (self.source / "rollout-missing.jsonl").unlink()
        errors = 0
        for _ in range(3):
            try:
                queue.scan(self.spool, limits=replace(queue.Limits(), jobs_per_pass=1))
            except FileNotFoundError:
                errors += 1
        self.assertEqual(errors, 1)
        states = queue.status(self.spool)
        self.assertEqual(sorted(row["requested"] for row in states), [1, 2, 2])
        self.assertEqual(sum(row["error"] == "CaptureScanError" for row in states), 1)

    def test_scan_during_capture_leaves_changed_source_pending(self):
        path = self.source / "rollout-fixture.jsonl"
        path.write_bytes(b"first\n")
        queue.enqueue(self.spool, self.config)
        queue.scan(self.spool)
        def parser(*args, **options):
            with path.open("ab") as stream:
                stream.write(b"second\n")
            self.assertTrue(queue.scan(self.spool)[0]["changed"])
            return self.fake_loader(*args, **options)
        queue.stage(self.spool, parser)
        self.assertEqual((queue.status(self.spool)[0]["requested"],
                          queue.status(self.spool)[0]["staged"]), (3, 2))
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_overlapping_fresh_process_scans_queue_one_observation(self):
        queue.enqueue(self.spool, self.config)
        ready = [self.root / "ready-1", self.root / "ready-2"]
        release = self.root / "release"
        code = """import sys,time
from pathlib import Path
import capture_queue as q
Path(sys.argv[2]).touch()
deadline=time.monotonic()+8
while not Path(sys.argv[3]).exists():
 if time.monotonic()>deadline: raise RuntimeError('synthetic barrier timed out')
 time.sleep(.01)
q.scan(sys.argv[1])
"""
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        children = []
        try:
            for path in ready:
                children.append(subprocess.Popen([getattr(sys, "_base_executable", sys.executable),
                                                  "-c", code, str(self.spool), str(path), str(release)],
                                                 cwd=Path(queue.__file__).parent,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **options))
            deadline = time.monotonic() + 5
            while not all(path.exists() for path in ready) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(all(path.exists() for path in ready))
            release.touch()
            for child in children:
                _, error = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, error.decode())
            self.assertEqual(queue.status(self.spool)[0]["requested"], 2)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_killed_worker_raw_scratch_is_private_and_recovered_without_deleting_other_data(self):
        original = b'{"synthetic":"private raw dialogue"}\n'
        source = self.source / "rollout-fixture.jsonl"
        source.write_bytes(original)
        queue.enqueue(self.spool, self.config)
        legacy = self.spool / ".source-unrecorded"
        legacy.mkdir()
        (legacy / "keep.txt").write_text("operator review required")
        ready = self.root / "ready"
        code = """import sys,time
from pathlib import Path
import capture_queue as q
def loader(project,seat,out,**kw):
 Path(sys.argv[2]).write_text(kw['codex_dir'])
 time.sleep(30)
q.stage(sys.argv[1],run_loader=loader)
"""
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", code, str(self.spool), str(ready)],
                              cwd=Path(queue.__file__).parent, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **options) as child:
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "worker did not reach the frozen-source parser")
                scratch = Path(ready.read_text())
                self.assertEqual((scratch / source.name).read_bytes(), original)
                if os.name == "nt":
                    # Independently inspect the resulting DACL, including the raw
                    # file's inherited permissions, rather than trusting icacls exit.
                    script = """$own=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rows=@(@($env:TRANSCRIPT_TEST_SCRATCH,$env:TRANSCRIPT_TEST_RAW) | ForEach-Object {
 $acl=Get-Acl -LiteralPath $_
 [pscustomobject]@{protected=$acl.AreAccessRulesProtected; own=$own;
 sids=@($acl.Access | ForEach-Object {$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value})}
})
ConvertTo-Json -InputObject $rows -Compress -Depth 4"""
                    result = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", script],
                                                     timeout=5, env={**os.environ,
                                                     "TRANSCRIPT_TEST_SCRATCH": str(scratch),
                                                     "TRANSCRIPT_TEST_RAW": str(scratch / source.name)}, **options)
                    acls = json.loads(result)
                    self.assertTrue(acls[0]["protected"])
                    for acl in acls:
                        self.assertEqual(set(acl["sids"]), {acl["own"], "S-1-5-18"})
                else:
                    self.assertEqual(scratch.stat().st_mode & 0o777, 0o700)
            finally:
                child.kill()
                child.wait(timeout=5)
        with closing(queue.database(self.spool)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scratch").fetchone()[0], 1)
        self.assertTrue(queue.status(self.spool)[0]["pending"])
        queue.stage(self.spool, self.fake_loader)
        self.assertFalse(scratch.exists())
        self.assertTrue((legacy / "keep.txt").exists())
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(queue.status(self.spool)[0]["pending"])
        with closing(queue.database(self.spool)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scratch").fetchone()[0], 0)

    def test_replaced_scratch_identity_is_preserved_for_review(self):
        queue.enqueue(self.spool, self.config)
        scratch = self.spool / ".source-recorded"
        scratch.mkdir()
        info = scratch.stat()
        with closing(queue.database(self.spool)) as db, db:
            db.execute("INSERT INTO scratch(name,device,inode) VALUES(?,?,?)",
                       (scratch.name, str(info.st_dev), str(info.st_ino)))
        scratch.rename(self.spool / "preserved-original")
        scratch.mkdir()
        (scratch / "keep.txt").write_text("replacement is not owned")
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, self.fake_loader)
        self.assertTrue((scratch / "keep.txt").exists())
        self.assertTrue(queue.status(self.spool)[0]["pending"])
        with closing(queue.database(self.spool)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scratch").fetchone()[0], 1)

    def test_invalid_scratch_record_cannot_delete_source(self):
        queue.enqueue(self.spool, self.config)
        info = self.source.stat()
        with closing(queue.database(self.spool)) as db, db:
            db.execute("INSERT INTO scratch(name,device,inode) VALUES(?,?,?)",
                       ("../source", str(info.st_dev), str(info.st_ino)))
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, self.fake_loader)
        self.assertTrue((self.source / "rollout-fixture.jsonl").exists())
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_permission_failure_prevents_raw_copy_and_preserves_pending_wake(self):
        (self.source / "rollout-fixture.jsonl").write_bytes(b'"private synthetic"\n')
        queue.enqueue(self.spool, self.config)
        with mock.patch.object(queue, "private_scratch", side_effect=queue.CaptureLimitError("denied")):
            with self.assertRaises(queue.CaptureLimitError):
                queue.stage(self.spool, self.fake_loader)
        self.assertFalse(list(self.spool.glob(".source-*")))
        self.assertTrue(queue.status(self.spool)[0]["pending"])

    def record_suspect_scratch(self, job, name=".source-suspect"):
        scratch = self.spool / name
        scratch.mkdir()
        (scratch / "keep.txt").write_bytes(b"synthetic retained bytes")
        info = scratch.stat()
        with closing(queue.database(self.spool)) as db, db:
            db.execute("INSERT INTO scratch(name,device,inode,job) VALUES(?,?,?,?)",
                       (name, str(info.st_dev), str(info.st_ino + 1), job))
        return scratch

    def test_quarantined_owner_does_not_starve_healthy_job_or_clear_on_next_wake(self):
        affected = queue.enqueue(self.spool, self.config)["job"]
        healthy_config = {**self.config, "seat": "codex-oxxxxxx-cli"}
        healthy = queue.enqueue(self.spool, healthy_config)["job"]
        scratch = self.record_suspect_scratch(affected)
        for generation in (1, 2):
            if generation == 2:
                queue.enqueue(self.spool, healthy_config)
                queue.enqueue(self.spool, self.config)
            seen = []
            def loader(project, seat, out, **options):
                seen.append(seat)
                return self.fake_loader(project, seat, out, **options)
            with self.assertRaises(queue.CaptureLimitError):
                queue.stage(self.spool, loader)
            self.assertEqual(seen, ["codex-oxxxxxx-cli"])
            states = {row["job"]: row for row in queue.status(self.spool)}
            self.assertEqual(states[healthy]["staged"], generation)
            self.assertFalse(states[healthy]["pending"])
            self.assertEqual(states[affected]["staged"], 0)
            self.assertTrue(states[affected]["pending"])
            self.assertEqual(states[affected]["scratch_review_count"], 1)
            self.assertEqual(states[affected]["error"], "ScratchReviewRequired")
            self.assertEqual((scratch / "keep.txt").read_bytes(), b"synthetic retained bytes")
        # Closing/reopening the queue and even repairing the recorded identity
        # must not silently lift the operator review state.
        with closing(queue.database(self.spool)) as db, db:
            db.execute("UPDATE scratch SET inode=?", (str(scratch.stat().st_ino),))
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, self.fake_loader)
        self.assertTrue(scratch.exists())
        self.assertTrue(next(row for row in queue.status(self.spool) if row["job"] == affected)["pending"])

    def test_unknown_scratch_owner_blocks_global_capture_without_deletion(self):
        queue.enqueue(self.spool, self.config)
        scratch = self.record_suspect_scratch("a" * 64)
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, self.fake_loader)
        self.assertTrue((scratch / "keep.txt").exists())
        self.assertEqual(queue.status(self.spool)[0]["staged"], 0)

    def test_snapshot_records_real_job_before_cleanup_failure_and_next_wake_isolates_it(self):
        affected = queue.enqueue(self.spool, self.config)["job"]
        observed = []
        def parser(project, seat, out, **options):
            with closing(queue.database(self.spool)) as db, db:
                row = db.execute("SELECT name,job FROM scratch").fetchone()
                self.assertEqual(row[1], affected)
                scratch = self.spool / row[0]
                # The active worker already holds the original identity in
                # memory. Replace the actual directory to exercise cleanup,
                # preserving its raw fixture bytes rather than altering SQL.
                held = self.spool / ".source-retained"
                scratch.rename(held)
                scratch.mkdir()
                (scratch / "keep.txt").write_bytes(b"synthetic replacement")
                observed.append(scratch)
            return self.fake_loader(project, seat, out, **options)
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, parser)
        healthy = queue.enqueue(self.spool, {**self.config, "seat": "codex-oxxxxxx-cli"})["job"]
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, self.fake_loader)
        states = {row["job"]: row for row in queue.status(self.spool)}
        self.assertEqual(states[affected]["staged"], 0)
        self.assertEqual(states[affected]["scratch_review_count"], 1)
        self.assertEqual(states[healthy]["staged"], 1)
        self.assertTrue(observed[0].exists())

    def test_quarantine_does_not_bypass_retained_spool_size_guards(self):
        affected = queue.enqueue(self.spool, self.config)["job"]
        queue.enqueue(self.spool, {**self.config, "seat": "codex-oxxxxxx-cli"})
        scratch = self.record_suspect_scratch(affected)
        (scratch / "large.txt").write_bytes(b"x" * 4096)
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, self.fake_loader, limits=replace(queue.Limits(), spool_bytes=1024))
        self.assertTrue(all(row["staged"] == 0 for row in queue.status(self.spool)))
        self.assertEqual((scratch / "large.txt").stat().st_size, 4096)

    def test_review_survives_when_no_capture_generation_is_pending(self):
        affected = queue.enqueue(self.spool, self.config)["job"]
        queue.stage(self.spool, self.fake_loader)
        scratch = self.record_suspect_scratch(affected)
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, self.fake_loader)
        state = queue.status(self.spool)[0]
        self.assertFalse(state["pending"])
        self.assertEqual(state["scratch_review_count"], 1)
        self.assertTrue(scratch.exists())

    def test_legacy_scratch_migration_preserves_identity_and_unknown_ownership(self):
        self.spool.mkdir()
        with closing(sqlite3.connect(self.spool / "capture.sqlite")) as db, db:
            db.execute("CREATE TABLE scratch(name TEXT PRIMARY KEY,device TEXT NOT NULL,inode TEXT NOT NULL)")
            db.execute("INSERT INTO scratch VALUES('.source-legacy','12','34')")
        with closing(queue.database(self.spool)) as db:
            self.assertEqual(db.execute("SELECT name,device,inode,job,review FROM scratch").fetchone(),
                             (".source-legacy", "12", "34", None, 0))

    def test_existing_queue_migration_preserves_pending_and_staged_state(self):
        self.spool.mkdir()
        key = hashlib.sha256(queue.canonical(self.config).encode()).hexdigest()
        with closing(sqlite3.connect(self.spool / "capture.sqlite")) as db, db:
            db.execute("""CREATE TABLE jobs(id TEXT PRIMARY KEY, source TEXT NOT NULL,
                requested INTEGER NOT NULL, staged INTEGER NOT NULL DEFAULT 0,
                batch TEXT, error TEXT)""")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                       (key, queue.canonical(self.config), 3, 2, "previous-batch", "OSError"))
        state = queue.status(self.spool)[0]
        self.assertEqual((state["requested"], state["staged"], state["batch"], state["error"]),
                         (3, 2, "previous-batch", "OSError"))
        queue.stage(self.spool, self.fake_loader)
        state = queue.status(self.spool)[0]
        self.assertEqual((state["requested"], state["staged"], state["error"]), (3, 3, None))
        with closing(sqlite3.connect(self.spool / "capture.sqlite")) as db:
            self.assertEqual(db.execute("SELECT attempts FROM jobs WHERE id=?", (key,)).fetchone(), (1,))

    def test_crash_after_durable_batch_before_checkpoint_replays(self):
        first = queue.enqueue(self.spool, self.config)
        # A killed child releases its kernel lock without a fabricated lease reset.
        code = """import json,os,sys
from pathlib import Path
import capture_queue as q
def loader(project,seat,out,**kw):
 p=Path(out);p.mkdir(parents=True,exist_ok=True)
 (p/'cards.jsonl').write_text('{"synthetic":true}\\n')
 return {'bad_lines':0,'files':1}
q.stage(sys.argv[1],run_loader=loader,before_commit=lambda *_:os._exit(23))
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.spool)],
                               cwd=Path(queue.__file__).parent, capture_output=True)
        self.assertEqual(child.returncode, 23, child.stderr.decode())
        self.assertTrue(queue.status(self.spool)[0]["pending"])
        queue.stage(self.spool, self.fake_loader)
        state = queue.status(self.spool)[0]
        self.assertEqual(state["staged"], first["requested"])
        self.assertFalse(state["pending"])
        batch = Path(state["batch"])
        manifest = json.loads((batch / "manifest.json").read_text())
        self.assertEqual(manifest["cards_sha256"], hashlib.sha256((batch / "cards.jsonl").read_bytes()).hexdigest())
        self.assertEqual(manifest["server_persistence"], "unverified")

    def test_wake_during_capture_is_not_lost(self):
        queue.enqueue(self.spool, self.config)
        queue.stage(self.spool, self.fake_loader,
                    before_commit=lambda *_: queue.enqueue(self.spool, self.config))
        state = queue.status(self.spool)[0]
        self.assertEqual((state["requested"], state["staged"]), (2, 1))
        self.assertTrue(state["pending"])
        queue.stage(self.spool, self.fake_loader)
        self.assertFalse(queue.status(self.spool)[0]["pending"])

    def test_worker_exclusion_and_rejection_leave_pending(self):
        queue.enqueue(self.spool, self.config)
        with queue.worker_lock(self.spool):
            with self.assertRaises(OSError):
                queue.stage(self.spool, self.fake_loader)
        self.assertTrue(queue.status(self.spool)[0]["pending"])
        def broken(*args, **kwargs):
            raise OSError("simulated unavailable source")
        with self.assertRaises(OSError):
            queue.stage(self.spool, broken)
        self.assertEqual(queue.status(self.spool)[0]["error"], "OSError")
        queue.stage(self.spool, self.fake_loader)
        self.assertIsNone(queue.status(self.spool)[0]["error"])

    def test_native_partial_tail_restart_and_stable_card_ids(self):
        path = self.source / "rollout-fixture.jsonl"
        stamp = "2026-10-10T05:00:00.000Z"
        meta = {"type": "session_meta", "timestamp": stamp,
                "payload": {"id": "synthetic-session", "source": "cli", "originator": "codex_cli_rs"}}
        event = {"type": "event_msg", "timestamp": stamp,
                 "payload": {"type": "user_message", "message": "Keep the synthetic deployment task open.",
                             "images": [], "local_images": [], "text_elements": []}}
        path.write_text(json.dumps(meta) + "\n" + json.dumps(event))
        queue.enqueue(self.spool, self.config)
        queue.stage(self.spool)
        batch = Path(queue.status(self.spool)[0]["batch"])
        self.assertEqual((batch / "cards.jsonl").read_text(), "")
        with path.open("a") as stream:
            stream.write("\n")
        queue.enqueue(self.spool, self.config)
        queue.stage(self.spool)
        batch = Path(queue.status(self.spool)[0]["batch"])
        captured = (batch / "cards.jsonl").read_bytes()
        self.assertEqual(len(captured.splitlines()), 1)
        queue.enqueue(self.spool, self.config)
        queue.stage(self.spool)
        replay = Path(queue.status(self.spool)[0]["batch"])
        self.assertEqual(captured, (replay / "cards.jsonl").read_bytes())

    def test_malformed_complete_record_never_marks_capture_done(self):
        path = self.source / "rollout-fixture.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "synthetic", "source": "cli"}})
                        + "\nnot json\n")
        queue.enqueue(self.spool, self.config)
        for _ in range(2):
            with self.assertRaises(ValueError):
                queue.stage(self.spool)
            self.assertTrue(queue.status(self.spool)[0]["pending"])

    def test_missing_supported_source_never_marks_capture_done(self):
        queue.enqueue(self.spool, self.config)
        (self.source / "rollout-fixture.jsonl").unlink()
        with self.assertRaises(FileNotFoundError):
            queue.stage(self.spool)
        state = queue.status(self.spool)[0]
        self.assertEqual(state["staged"], 0)
        self.assertTrue(state["pending"])

    def test_source_limits_reject_before_parser_without_dropping_wake(self):
        path = self.source / "rollout-large.jsonl"
        path.write_bytes(b"x" * 40)
        (self.source / "rollout-second.jsonl").write_bytes(b"y" * 40)
        config = {**self.config, "files": ["rollout-large.jsonl", "rollout-second.jsonl"]}
        queue.enqueue(self.spool, config)
        for limits in (replace(queue.Limits(), file_bytes=39),
                       replace(queue.Limits(), source_bytes=79),
                       replace(queue.Limits(), source_entries=1)):
            with self.subTest(limits=limits):
                def forbidden_parser(*args, **kwargs):
                    self.fail("source guard must run before parsing")
                with self.assertRaises(queue.CaptureLimitError):
                    queue.stage(self.spool, forbidden_parser, limits=limits)
                self.assertTrue(queue.status(self.spool)[0]["pending"])
                self.assertIsNone(queue.status(self.spool)[0]["batch"])
        queue.stage(self.spool, self.fake_loader)
        self.assertFalse(queue.status(self.spool)[0]["pending"])

    def test_rewritten_complete_prefix_during_parser_remains_pending(self):
        path = self.source / "rollout-fixture.jsonl"
        path.write_bytes(b"before\n")
        queue.enqueue(self.spool, self.config)
        def rewriting_parser(*args, **kwargs):
            result = self.fake_loader(*args, **kwargs)
            path.write_bytes(b"edited\n")
            return result
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, rewriting_parser)
        state = queue.status(self.spool)[0]
        self.assertTrue(state["pending"])
        self.assertIsNone(state["batch"])
        queue.stage(self.spool, self.fake_loader)
        self.assertFalse(queue.status(self.spool)[0]["pending"])

    def test_failure_does_not_starve_other_jobs_in_same_pass(self):
        for seat in ("codex-kxxx-cli", "codex-oxxxxxx-cli", "codex-oxxxxxx-admin-cli"):
            queue.enqueue(self.spool, {**self.config, "seat": seat})
        first = queue.status(self.spool)[0]["job"]
        with queue.contextlib.closing(queue.database(self.spool)) as db:
            failing_seat = json.loads(db.execute("SELECT source FROM jobs WHERE id=?", (first,)).fetchone()[0])["seat"]
        def parser(project, seat, out, **options):
            if seat == failing_seat:
                raise OSError("synthetic persistent failure")
            return self.fake_loader(project, seat, out, **options)
        for _ in range(3):
            with self.assertRaises(OSError):
                queue.stage(self.spool, parser)
        states = queue.status(self.spool)
        self.assertEqual(sum(row["pending"] for row in states), 1)
        self.assertTrue(next(row for row in states if row["job"] == first)["pending"])
        self.assertEqual(sum(row["staged"] == 1 for row in states), 2)

    def test_failed_job_rotates_across_bounded_passes_and_restarts(self):
        for seat in ("codex-kxxx-cli", "codex-oxxxxxx-cli", "codex-oxxxxxx-admin-cli"):
            queue.enqueue(self.spool, {**self.config, "seat": seat})
        first = queue.status(self.spool)[0]["job"]
        with queue.contextlib.closing(queue.database(self.spool)) as db:
            failing_seat = json.loads(db.execute("SELECT source FROM jobs WHERE id=?", (first,)).fetchone()[0])["seat"]
        def parser(project, seat, out, **options):
            if seat == failing_seat:
                raise OSError("synthetic persistent failure")
            return self.fake_loader(project, seat, out, **options)
        with self.assertRaises(OSError):
            queue.stage(self.spool, parser, limits=replace(queue.Limits(), jobs_per_pass=1))
        for _ in range(2):
            queue.stage(self.spool, parser, limits=replace(queue.Limits(), jobs_per_pass=1))
        self.assertEqual(sum(row["pending"] for row in queue.status(self.spool)), 1)

    def test_unselected_oversized_and_growing_file_does_not_affect_capture(self):
        foreign = self.source / "rollout-other-seat.jsonl"
        foreign.write_bytes(b"x" * 100)
        def parser(*args, **options):
            with foreign.open("ab") as stream:
                stream.write(b"more")
            self.assertEqual([Path(p).name for p in options["files"]], ["rollout-fixture.jsonl"])
            self.assertFalse((Path(options["codex_dir"]) / foreign.name).exists())
            return self.fake_loader(*args, **options)
        queue.enqueue(self.spool, self.config)
        queue.stage(self.spool, parser, limits=replace(queue.Limits(), file_bytes=1, source_bytes=1))
        self.assertFalse(queue.status(self.spool)[0]["pending"])

    def test_append_while_parsing_keeps_snapshot_and_next_wake(self):
        source = self.source / "rollout-fixture.jsonl"
        source.write_bytes(b"first\npartial")
        def parser(*args, **options):
            self.assertEqual(Path(options["files"][0]).read_bytes(), b"first\n")
            with source.open("ab") as stream:
                stream.write(b"-completed\nnext\n")
            queue.enqueue(self.spool, self.config)
            return self.fake_loader(*args, **options)
        queue.enqueue(self.spool, self.config)
        queue.stage(self.spool, parser)
        state = queue.status(self.spool)[0]
        self.assertEqual((state["requested"], state["staged"]), (2, 1))
        self.assertTrue(state["pending"])
        def next_parser(*args, **options):
            self.assertEqual(Path(options["files"][0]).read_bytes(), b"first\npartial-completed\nnext\n")
            return self.fake_loader(*args, **options)
        queue.stage(self.spool, next_parser)
        self.assertFalse(queue.status(self.spool)[0]["pending"])
        self.assertFalse(list(self.spool.glob(".source-*")))

    def test_unselected_legacy_configuration_fails_closed(self):
        legacy = {key: value for key, value in self.config.items() if key != "files"}
        with self.assertRaises(ValueError):
            queue.enqueue(self.spool, legacy)

    def test_selected_path_escape_or_duplicate_basename_rejected(self):
        for names in (["../rollout-foreign.jsonl"], ["C:/rollout-foreign.jsonl"],
                      ["C:rollout-foreign.jsonl"], [r"C:\rollout-foreign.jsonl"],
                      [r"\\server\share\rollout-foreign.jsonl"],
                      ["a/rollout-same.jsonl", "b/rollout-same.jsonl"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                queue.enqueue(self.spool, {**self.config, "files": names})

    def test_windows_parent_escape_rejected_with_posix_path_semantics(self):
        name = r"..\rollout-foreign.jsonl"
        def path_factory(value):
            return PurePosixPath(value) if value == name else Path(value)
        with mock.patch.object(queue, "Path", side_effect=path_factory):
            with self.assertRaises(ValueError):
                queue.source_config({**self.config, "kind": "claude", "files": [name]})

    def test_selected_path_escape_matrix_under_both_path_parsers(self):
        escapes = (
            "/x.jsonl", "//x.jsonl", "///x.jsonl", r"\x.jsonl",
            r"\a\x.jsonl", "/a/x.jsonl", r"\\server\share\x.jsonl",
            "//server/share/x.jsonl", r"\\?\C:\x.jsonl", r"\\.\C:\x.jsonl",
            "C:x.jsonl", "C:/x.jsonl", r"C:\x.jsonl", "c:x.jsonl",
            "../x.jsonl", "a/../x.jsonl", "a/../../x.jsonl",
            r"..\x.jsonl", r"a\..\x.jsonl", r"a\..\..\x.jsonl",
            r"a/..\x.jsonl", r"a\../x.jsonl",
        )
        refused = {name: "relative descendants" for name in escapes}
        refused.update({name: "colon" for name in (
            "a/D:/x.jsonl", r"a\D:\x.jsonl", "a.jsonl:b.jsonl",
            "dir:stream/x.jsonl", r"dir:stream\x.jsonl",
            "dir/a.jsonl:b.jsonl", "a/C:/x.jsonl", "a/C:x.jsonl", "a/D:x.jsonl",
        )})
        refused.update({name: "trailing dot or space" for name in (
            "a./x.jsonl", "a /x.jsonl", r"a.\x.jsonl", r"a \x.jsonl",
            "x.jsonl.", "x.jsonl ", "a/./x.jsonl", r"a\.\x.jsonl",
            ".. /x.jsonl", r".. \\x.jsonl", ".. ./x.jsonl", r".. .\\x.jsonl",
            ".../x.jsonl", r"...\\x.jsonl",
        )})
        devices = ("CON", "PRN", "AUX", "NUL",
                   *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10)))
        for device in devices:
            for name in (f"{device}.jsonl", f"a/{device.lower()}.jsonl",
                         f"{device}/x.jsonl", rf"a\{device.lower()}.extra\x.jsonl"):
                refused[name] = "reserved device name"
        controls = ("x.jsonl", "a/x.jsonl")
        for parser in (PurePosixPath, PureWindowsPath):
            def path_factory(value):
                return parser(value) if value in (*refused, *controls) else Path(value)
            with mock.patch.object(queue, "Path", side_effect=path_factory):
                for name, reason in refused.items():
                    # Claude avoids filename-prefix checks masking a path escape.
                    with self.subTest(parser=parser.__name__, name=name):
                        with mock.patch.object(Path, "stat") as source_stat:
                            with mock.patch.object(Path, "resolve") as source_resolve:
                                with self.assertRaisesRegex(ValueError, reason):
                                    queue.source_config(
                                        {**self.config, "kind": "claude", "files": [name]})
                                source_resolve.assert_not_called()
                            source_stat.assert_not_called()
                        for control in controls:
                            result = queue.source_config(
                                {**self.config, "kind": "claude", "files": [control]})
                            self.assertEqual(result["files"], [control])

    def test_device_spellings_refused_before_filesystem_or_queue_work(self):
        devices = ("CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                   *(f"COM{i}" for i in "0123456789\u00b9\u00b2\u00b3"),
                   *(f"LPT{i}" for i in "0123456789\u00b9\u00b2\u00b3"))
        refused = set()
        for device in devices:
            refused.update((device, f"{device.lower()}.jsonl",
                            f"a/{device}/x.jsonl", rf"a\{device.lower()}.extra\x.jsonl",
                            f"{device} .jsonl", f"a/{device.lower()}  .extra/x.jsonl",
                            rf"a\{device} .extra\x.jsonl"))
        for parser in (PurePosixPath, PureWindowsPath):
            def path_factory(value):
                return parser(value) if value in refused else Path(value)
            with mock.patch.object(queue, "Path", side_effect=path_factory):
                for name in sorted(refused):
                    with self.subTest(parser=parser.__name__, name=name):
                        with (mock.patch.object(Path, "stat") as source_stat,
                              mock.patch.object(Path, "resolve") as source_resolve,
                              mock.patch.object(queue, "database") as database,
                              mock.patch.object(queue.tempfile, "mkdtemp") as allocate):
                            with self.assertRaisesRegex(ValueError, "reserved device name"):
                                queue.enqueue(self.spool,
                                              {**self.config, "kind": "claude", "files": [name]})
                            source_stat.assert_not_called()
                            source_resolve.assert_not_called()
                            database.assert_not_called()
                            allocate.assert_not_called()

    def test_ordinary_device_lookalikes_capture_without_changing_source_bytes(self):
        names = ("COM10.jsonl", "LPT10.jsonl", "COM0notes.jsonl", "LPT\u00b9notes.jsonl",
                 "CONIN.jsonl", "CONOUT.jsonl", "console.jsonl", "report .jsonl")
        original = b'{"synthetic":true}\n{"incomplete":'
        for name in names:
            (self.source / name).write_bytes(original)
        for parser in (PurePosixPath, PureWindowsPath):
            def path_factory(value):
                return parser(value) if value in names else Path(value)
            with mock.patch.object(queue, "Path", side_effect=path_factory):
                result = queue.source_config({**self.config, "kind": "claude", "files": list(names)})
                self.assertEqual(result["files"], sorted(names))
        queue.enqueue(self.spool, {**self.config, "kind": "claude", "files": list(names)})
        queue.stage(self.spool, self.fake_loader)
        self.assertFalse(queue.status(self.spool)[0]["pending"])
        for name in names:
            self.assertEqual((self.source / name).read_bytes(), original)

    def test_selected_path_normalization_policy_under_both_path_parsers(self):
        # A raw dot component is refused by the trailing-dot rule, even when
        # pathlib would normalize it away. Repeated separators remain accepted.
        names = ("./x.jsonl", "a//x.jsonl")
        for parser in (PurePosixPath, PureWindowsPath):
            def path_factory(value):
                return parser(value) if value in names else Path(value)
            with mock.patch.object(queue, "Path", side_effect=path_factory):
                with self.subTest(parser=parser.__name__, name="./x.jsonl"):
                    with mock.patch.object(Path, "stat") as source_stat:
                        with mock.patch.object(Path, "resolve") as source_resolve:
                            with self.assertRaisesRegex(ValueError, "trailing dot or space"):
                                queue.source_config(
                                    {**self.config, "kind": "claude", "files": [names[0]]})
                            source_resolve.assert_not_called()
                        source_stat.assert_not_called()
                with self.subTest(parser=parser.__name__, name="a//x.jsonl"):
                    result = queue.source_config(
                        {**self.config, "kind": "claude", "files": [names[1]]})
                    self.assertEqual(result["files"], ["a/x.jsonl"])

    def test_hard_link_alias_rejected_before_snapshot_or_parser(self):
        source = self.source / "rollout-fixture.jsonl"
        original = b'{"synthetic":true}\n{"incomplete":'
        source.write_bytes(original)
        alias = self.source / "rollout-alias.jsonl"
        os.link(source, alias)
        config = {**self.config, "files": [source.name, alias.name]}
        queue.enqueue(self.spool, config)
        parser = mock.Mock(side_effect=AssertionError("alias reached parser"))
        with mock.patch.object(queue.tempfile, "mkdtemp") as allocate:
            with self.assertRaisesRegex(queue.CaptureLimitError, "same source file"):
                queue.stage(self.spool, parser)
            allocate.assert_not_called()
        parser.assert_not_called()
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(alias.read_bytes(), original)
        self.assertTrue(queue.status(self.spool)[0]["pending"])
        self.assertFalse(list(self.spool.glob(".source-*")))

    def test_native_case_alias_rejected_by_inventory(self):
        source = self.source / "rollout-fixture.jsonl"
        alias = self.source / "rollout-FIXTURE.jsonl"
        if not alias.exists() or not source.samefile(alias):
            self.skipTest("source filesystem has distinct case-sensitive names")
        with self.assertRaisesRegex(queue.CaptureLimitError, "same source file"):
            queue.source_inventory({**self.config, "files": [source.name, alias.name]},
                                   self.spool, queue.Limits())

    def test_native_unicode_alias_rejected_by_inventory(self):
        source = self.source / "rollout-\u00e9.jsonl"
        alias = self.source / "rollout-e\u0301.jsonl"
        source.write_bytes(b"synthetic\n")
        if not alias.exists() or not source.samefile(alias):
            self.skipTest("source filesystem has distinct Unicode normalization names")
        config = queue.source_config({**self.config, "files": [source.name, alias.name]})
        with self.assertRaisesRegex(queue.CaptureLimitError, "same source file"):
            queue.source_inventory(config, self.spool, queue.Limits())

    def test_distinct_selected_files_count_once_each(self):
        source = self.source / "rollout-fixture.jsonl"
        other = self.source / "rollout-other.jsonl"
        source.write_bytes(b"first\n")
        other.write_bytes(b"second\n")
        config = queue.source_config({**self.config, "files": [source.name, other.name]})
        total, fingerprints = queue.source_inventory(config, self.spool, queue.Limits())
        self.assertEqual(total, len(b"first\nsecond\n"))
        self.assertEqual(set(fingerprints), {source.name, other.name})

    def test_output_limit_leaves_generation_pending_without_batch(self):
        queue.enqueue(self.spool, self.config)
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, self.fake_loader, limits=replace(queue.Limits(), cards_bytes=1))
        state = queue.status(self.spool)[0]
        self.assertTrue(state["pending"])
        self.assertIsNone(state["batch"])
        self.assertFalse((self.spool / "batches").exists())
        queue.stage(self.spool, self.fake_loader)
        manifest = json.loads((Path(queue.status(self.spool)[0]["batch"]) / "manifest.json").read_text())
        self.assertEqual(manifest["limits"]["cards_bytes"], queue.Limits().cards_bytes)

    def test_exhausted_spool_preserves_previous_batch_and_pending_wake(self):
        queue.enqueue(self.spool, self.config)
        queue.stage(self.spool, self.fake_loader)
        first = queue.status(self.spool)[0]
        batch = Path(first["batch"])
        before = {p.name: p.read_bytes() for p in batch.iterdir()}
        queue.enqueue(self.spool, self.config)
        def forbidden_parser(*args, **kwargs):
            self.fail("exhausted spool must reject before parsing")
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(self.spool, forbidden_parser, limits=replace(queue.Limits(), spool_bytes=1))
        state = queue.status(self.spool)[0]
        self.assertTrue(state["pending"])
        self.assertEqual(state["staged"], first["staged"])
        self.assertEqual(state["batch"], first["batch"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in batch.iterdir()})

    def test_source_and_spool_overlap_requires_review(self):
        overlapping = self.source / "capture"
        queue.enqueue(overlapping, self.config)
        with self.assertRaises(queue.CaptureLimitError):
            queue.stage(overlapping, self.fake_loader)
        self.assertTrue(queue.status(overlapping)[0]["pending"])

    def test_invalid_limits_cannot_disable_guards(self):
        for value in (0, -1, True, 1.5, "unlimited"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                queue.Limits(source_bytes=value)

    def test_timeout_kills_and_reaps_local_loader(self):
        started = self.root / "started"
        finished = self.root / "finished"
        code = "from pathlib import Path; import time,sys; Path(sys.argv[1]).touch(); time.sleep(30); Path(sys.argv[2]).touch()"
        begin = time.monotonic()
        with self.assertRaises(queue.CaptureLimitError):
            queue.run_process([getattr(sys, "_base_executable", sys.executable), "-c", code, str(started), str(finished)], 1)
        self.assertTrue(started.exists())
        self.assertFalse(finished.exists())
        self.assertLess(time.monotonic() - begin, 5)

    def test_jobs_per_pass_keeps_remaining_jobs_pending(self):
        queue.enqueue(self.spool, self.config)
        second = {**self.config, "seat": "codex-oxxxxxx-cli"}
        queue.enqueue(self.spool, second)
        result = queue.stage(self.spool, self.fake_loader, limits=replace(queue.Limits(), jobs_per_pass=1))
        self.assertEqual(len(result), 1)
        self.assertEqual(sum(row["pending"] for row in queue.status(self.spool)), 1)
        queue.stage(self.spool, self.fake_loader)
        self.assertFalse(any(row["pending"] for row in queue.status(self.spool)))


if __name__ == "__main__":
    unittest.main()
