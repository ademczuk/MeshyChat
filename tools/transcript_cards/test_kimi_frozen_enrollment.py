"""Current Kimi dialect through a bounded copy, parser and atomic projection."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import card_projection
import incremental_store as ist
import native_window as nw
import transcript_cards as loader


def line(value):
    return (json.dumps(value) + '\n').encode()


class FrozenKimiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.frozen = self.root / 'first.jsonl'
        self.frozen.write_bytes(line({'type': 'metadata', 'protocol_version': '1.0'}))
        self.original = self.root / 'session_synthetic-session' / 'agents' / 'main' / 'wire.jsonl'

    def test_frozen_first_record_keeps_live_session_without_opening_live_path(self):
        self.assertFalse(self.original.exists())
        dialect, meta = loader.enroll(self.frozen, antigravity=False, original_path=self.original)
        self.assertEqual((dialect, meta), ('kimi', {'session': 'synthetic-session', 'subagent': False}))
        self.assertFalse(self.original.exists())

    def test_original_subagent_mark_is_not_flattened_by_scratch_location(self):
        original = self.original.parent.parent / 'child-1' / 'wire.jsonl'
        dialect, meta = loader.enroll(self.frozen, antigravity=False, original_path=original)
        self.assertEqual((dialect, meta['session'], meta['subagent']), ('kimi', 'synthetic-session', True))

    def test_without_override_existing_identity_behavior_is_preserved(self):
        dialect, meta = loader.enroll(self.frozen, antigravity=False)
        self.assertEqual((dialect, meta), ('kimi', {'session': '', 'subagent': False}))

    def test_path_alone_cannot_enroll_a_non_kimi_first_record(self):
        self.frozen.write_bytes(line({'type': 'user', 'message': {'content': 'synthetic'}}))
        dialect, _ = loader.enroll(self.frozen, antigravity=False, original_path=self.original)
        self.assertEqual(dialect, 'claude')

    def test_read_bounded_copy_and_commit_real_owner_output_and_projection(self):
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(self.frozen.read_bytes() + line({
            'type': 'context.append_message', 'time': 1787064216000,
            'message': {'id': 'synthetic-turn', 'role': 'user', 'origin': {'kind': 'user'},
                        'content': [{'type': 'text', 'text': 'Keep this synthetic cache local.'}]}}))
        info = self.original.stat()
        budget = nw.Budget(read_bytes=4096, record_bytes=2048, records=1)
        first = nw.read_window(self.original, nw.Identity.from_stat(info), 0, info.st_size, budget)
        self.assertEqual(len(first.records), 1)
        self.frozen.write_bytes(first.records[0].data)
        dialect, meta = loader.enroll(self.frozen, antigravity=False, original_path=self.original)
        parser = 'kimi-frozen-test:1'
        source, generation = hashlib.sha256(b'source').hexdigest(), hashlib.sha256(b'generation').hexdigest()
        folder = self.root / 'store'
        store = ist.Store(folder)
        snap = store.enroll(source, generation, parser, meta, loader.new_checkpoint(dialect, meta))
        projection = card_projection.Projection('nxxxxx', parser)
        for _ in range(2):
            window = nw.read_window(self.original, nw.Identity.from_stat(info), snap.offset, info.st_size, budget)
            raws, stats, checkpoint = loader.read_file([(r.offset, r.data) for r in window.records], snap.parser_state(), seat='nxxxxx')
            self.assertEqual(stats['bad_lines'], 0)
            cards, drops = loader.approved_cards(raws, 'nxxxxx', 'session_synthetic-session/agents/main/wire.jsonl')
            output = b''.join(line(cards[k]) for k in sorted(cards))
            rejected = b''.join(line(drops[k]) for k in sorted(drops))
            store.commit(snap, checkpoint['offset'], output, rejected, checkpoint, projection=projection)
            store = ist.Store(folder)  # Reopen real SQLite after each bounded window.
            snap = store.resume(source, generation, parser)
        self.assertEqual(snap.offset, info.st_size)
        self.assertEqual(len(cards), 1)
        card = next(iter(cards.values()))
        retained = projection.read(store, card['id'])
        self.assertEqual(retained['card'], card)
        self.assertEqual(retained['card']['source']['sessions'], ['synthetic-session'])
        self.assertTrue(retained['local_only'])


if __name__ == '__main__':
    unittest.main()
