"""Stacked wake/mode regressions using real local CLI, SQLite and synthetic files."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import capture_queue as queue
import incremental_queue as iq


class WakeModeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.path = self.source / 'session.jsonl'
        self.path.write_bytes(self.record('first', 'Keep the cache local to this project.'))
        self.spool = self.root / 'spool'
        self.key = queue.enqueue(self.spool, {'kind': 'claude', 'seat': 'codex-kxxx-cli',
            'root': str(self.source), 'files': [self.path.name]})['job']

    @staticmethod
    def record(identity, text):
        return (json.dumps({'type': 'user', 'uuid': identity, 'sessionId': 'synthetic-mode-test',
            'timestamp': '2026-10-10T09:00:00Z',
            'message': {'role': 'user', 'content': text}}) + '\n').encode()

    def cli(self, command, *args):
        return subprocess.run([getattr(sys, '_base_executable', sys.executable), '-B',
            str(Path(queue.__file__)), command, '--spool', str(self.spool), *args],
            capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    def successful(self, command, *args):
        result = self.cli(command, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def dump(self):
        with closing(sqlite3.connect(self.spool / 'capture.sqlite')) as db:
            return '\n'.join(db.iterdump())

    def test_unbound_wake_does_not_choose_or_create_capture_mode(self):
        result = self.successful('wake', '--job', self.key)
        self.assertEqual(result['requested'], 2)
        with closing(sqlite3.connect(self.spool / 'capture.sqlite')) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='capture_mode'").fetchone())
        self.assertEqual(queue.status(self.spool)[0]['staged'], 0)

    def check_mode(self, mode):
        correct, wrong = ('stage', 'incremental') if mode == 'snapshot' else ('incremental', 'stage')
        self.assertEqual(self.successful(correct)[0]['phase'], 'staged')
        with self.path.open('ab') as stream:
            stream.write(self.record('second', 'Keep this second operator request as well.'))
        # Synthetic persisted witnesses make retry/observation resets and loss of
        # pending diagnostics visible, even when the initial stage was clean.
        with closing(sqlite3.connect(self.spool / 'capture.sqlite')) as db, db:
            db.execute("UPDATE jobs SET attempts=7,scan_attempts=11,observed=? WHERE id=?",
                       ('synthetic-observed', self.key))
            if mode == 'incremental':
                db.execute('INSERT INTO incremental_pending VALUES(?,?,?)',
                           (self.key, iq.opaque([self.key, self.path.name]),
                            json.dumps({'reason': 'synthetic_pending_fixture'})))
        before_wake = self.dump()
        with closing(sqlite3.connect(':memory:')) as expected:
            expected.executescript(before_wake)
            expected.execute('UPDATE jobs SET requested=requested+1 WHERE id=?', (self.key,))
            expected.commit()
            after_requested_only = '\n'.join(expected.iterdump())
        result = self.successful('wake', '--job', self.key)
        self.assertEqual(result['requested'], 2)
        self.assertEqual(self.dump(), after_requested_only,
                         'wake changed persisted state other than this job requested')
        state = queue.status(self.spool)[0]
        self.assertEqual((state['requested'], state['staged'], state['error']), (2, 1, None))
        self.assertTrue(state['pending'])
        before = self.dump()
        refused = self.cli(wrong)
        self.assertEqual(refused.returncode, 1)
        self.assertIn(b'CaptureLimitError', refused.stderr)
        self.assertEqual(self.dump(), before, 'wrong worker changed persisted queue state')
        self.assertEqual(self.successful(correct)[0]['phase'], 'staged')
        state = queue.status(self.spool)[0]
        self.assertEqual((state['requested'], state['staged']), (2, 2))
        self.assertFalse(state['pending'])
        with closing(sqlite3.connect(self.spool / 'capture.sqlite')) as db:
            self.assertEqual(db.execute('SELECT mode FROM capture_mode WHERE id=1').fetchone(), (mode,))
            batch = db.execute('SELECT batch FROM jobs WHERE id=?', (self.key,)).fetchone()[0]
        if mode == 'incremental':
            self.assertEqual(batch, 'incremental-store')
        else:
            self.assertTrue((Path(batch) / 'manifest.json').is_file())

    def test_wake_incremental_spool_only_incremental_worker_can_complete(self):
        self.check_mode('incremental')

    def test_wake_snapshot_spool_only_snapshot_worker_can_complete(self):
        self.check_mode('snapshot')

    def test_wake_rejects_new_window_override_without_reading_or_queuing(self):
        result = self.cli('wake', '--job', self.key, '--window', 'private-untrusted-window')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b'private-untrusted-window', result.stdout + result.stderr)
        self.assertEqual(queue.status(self.spool)[0]['requested'], 1)


if __name__ == '__main__':
    unittest.main()
