"""Durable existing-enrollment wakes; local synthetic SQLite and fresh CLI."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import capture_queue as queue


class EnrolledWakeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.path = self.source / 'rollout-fixture.jsonl'
        self.path.write_bytes(b'synthetic\n')
        self.spool = self.root / 'spool'
        self.key = queue.enqueue(self.spool, {'seat': 'codex-kxxx-cli', 'kind': 'codex',
            'root': str(self.source), 'files': [self.path.name]})['job']

    def command(self, *extra):
        return [getattr(sys, '_base_executable', sys.executable), '-B', str(Path(queue.__file__)),
                'wake', '--spool', str(self.spool), '--job', self.key, *extra]

    def run_cli(self, *extra):
        return subprocess.run(self.command(*extra), capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    def test_wake_does_not_read_source_or_clear_error_and_review_state(self):
        with closing(queue.database(self.spool)) as db, db:
            db.execute("UPDATE jobs SET error='ReviewRequired' WHERE id=?", (self.key,))
        self.path.rename(self.source / 'preserved.jsonl')
        with mock.patch.object(Path, 'open', side_effect=AssertionError('no transcript IO')):
            result = queue.wake_enrolled(self.spool, self.key)
        self.assertEqual(result['requested'], 2)
        state = queue.status(self.spool)[0]
        self.assertEqual((state['staged'], state['error']), (0, 'ReviewRequired'))
        self.assertTrue(state['pending'])

    def test_concurrent_processes_increment_without_lost_wakes(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        children = []
        try:
            for _ in range(4):
                children.append(subprocess.Popen(self.command(), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags))
            generations = []
            for child in children:
                out, err = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, err)
                generations.append(json.loads(out)['requested'])
            self.assertEqual(sorted(generations), [2, 3, 4, 5])
            self.assertEqual(queue.status(self.spool)[0]['requested'], 5)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_transaction_failure_produces_no_success_receipt(self):
        with closing(queue.database(self.spool)) as db, db:
            db.execute("CREATE TRIGGER fail_wake BEFORE UPDATE OF requested ON jobs BEGIN SELECT RAISE(ABORT,'private detail'); END")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(json.loads(result.stderr), {'phase': 'pending', 'error': 'IntegrityError'})
        self.assertEqual(queue.status(self.spool)[0]['requested'], 1)

    def test_unknown_job_and_missing_database_never_create_enrollment(self):
        with self.assertRaises(ValueError):
            queue.wake_enrolled(self.spool, 'f' * 64)
        missing = self.root / 'missing-spool'
        with self.assertRaises(FileNotFoundError):
            queue.wake_enrolled(missing, self.key)
        self.assertFalse(missing.exists())

    def test_substituted_source_and_counter_overflow_refuse(self):
        with closing(queue.database(self.spool)) as db, db:
            original = db.execute('SELECT source FROM jobs WHERE id=?', (self.key,)).fetchone()[0]
            db.execute("UPDATE jobs SET source='changed' WHERE id=?", (self.key,))
        with self.assertRaises(ValueError):
            queue.wake_enrolled(self.spool, self.key)
        with closing(queue.database(self.spool)) as db, db:
            db.execute('UPDATE jobs SET source=?,requested=? WHERE id=?', (original, 2**63-1, self.key))
        with self.assertRaises(queue.CaptureLimitError):
            queue.wake_enrolled(self.spool, self.key)

    def test_source_override_refuses_without_echoing_its_path(self):
        result = self.run_cli('--source', 'private-untrusted-path')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b'private-untrusted-path', result.stdout + result.stderr)
        self.assertEqual(queue.status(self.spool)[0]['requested'], 1)


if __name__ == '__main__':
    unittest.main()
