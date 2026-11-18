"""Synthetic bounded projection/crash proofs; no native host or graph claim."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import card_projection as cp
import incremental_store as ist

PARSER, CID = "synthetic:1", "a" * 32
SOURCE, GENERATION = "1" * 64, "2" * 64


def card(session="one", text="first", stamp="2026-10-10T10:00:00Z", seat="nxxxxx", cid=CID):
    return {"schema": "card.v1", "id": cid, "seat": seat, "time": stamp, "text": text,
            "source": {"source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                       "sessions": [session], "files": [session + "/wire.jsonl"]}}


def output(value):
    return cp.encoded(value) + b"\n"


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "private"
        self.store = ist.Store(self.folder)
        self.projection = cp.Projection("nxxxxx", PARSER)
        self.initial = self.store.enroll(SOURCE, GENERATION, PARSER, {}, {"offset": 0})

    def commit(self, value, source=SOURCE, projection=None, drops=b"", boundary=None):
        current = self.store.resume(source, GENERATION, PARSER)
        return self.store.commit(current, current.offset + 1,
                                 b"" if value is None else output(value), drops,
                                 {"offset": current.offset + 1}, boundary=boundary,
                                 projection=projection or self.projection)

    def read(self, cid=CID):
        return self.projection.read(ist.Store(self.folder), cid)

    def second(self):
        source = "3" * 64
        self.store.enroll(source, GENERATION, PARSER, {}, {"offset": 0})
        return source

    def test_variants_merge_deterministically_in_both_orders(self):
        a, b = card(), card("two", "later", "2026-10-10T11:00:00Z")
        self.assertEqual(cp.merge(a, b), cp.merge(b, a))
        self.commit(b)
        self.commit(a, self.second())
        result = self.read()["card"]
        self.assertEqual(result["text"], "first")
        self.assertEqual(result["source"]["sessions"], ["one", "two"])
        self.assertEqual(result["source"]["files"], ["one/wire.jsonl", "two/wire.jsonl"])
        self.assertEqual(result["source"]["other_content"], [b["source"]["source_sha256"]])

    def test_replay_is_idempotent_and_never_drops_provenance(self):
        value = card()
        first = self.commit(value)
        self.commit(card("two"), self.second())
        before = self.read()
        replay = self.store.commit(self.initial, 1, output(value), b"", {"offset": 1},
                                   projection=self.projection)
        self.assertEqual(replay, first)
        self.assertEqual(self.read(), before)

    def test_malformed_merge_fields_explain_refusal_and_rollback(self):
        self.commit(card())
        prior = self.store.resume(SOURCE, GENERATION, PARSER)
        retained = self.read()
        variants = []
        value = card("two"); del value["time"]; variants.append(value)
        value = card("two"); del value["source"]["source_sha256"]; variants.append(value)
        value = card("two"); value["source"] = []; variants.append(value)
        value = card("two"); value["source"]["sessions"] = [None]; variants.append(value)
        value = card("two"); value["source"]["other_content"] = 5; variants.append(value)
        value = card("two"); value["time"] = 42; variants.append(value)
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                with self.assertRaisesRegex(ist.StoreError, "projection card"):
                    self.commit(value)
                self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), prior)
                self.assertEqual(self.read(), retained)

    def test_malformed_first_card_refuses_before_progress(self):
        value = card()
        del value["source"]["source_sha256"]
        with self.assertRaisesRegex(ist.StoreError, "projection card requires source digest"):
            cp.merge(value, card())
        with self.assertRaisesRegex(ist.StoreError, "projection card"):
            self.commit(value)
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), self.initial)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)

    def test_projection_failure_rolls_back_batch_and_offset(self):
        self.commit(card())
        second = self.second()
        prior = self.store.resume(second, GENERATION, PARSER)
        limits = replace(cp.Limits(), card_bytes=len(cp.encoded(card())))
        bounded = cp.Projection("nxxxxx", PARSER, limits)
        with self.assertRaisesRegex(ist.StoreError, "provenance"):
            self.commit(card("two"), second, projection=bounded)
        self.assertEqual(self.store.resume(second, GENERATION, PARSER), prior)
        self.assertEqual(self.read()["card"]["source"]["sessions"], ["one"])

    def test_card_count_and_row_limits_refuse_before_progress(self):
        self.commit(card())
        prior = self.store.resume(SOURCE, GENERATION, PARSER)
        bounded = cp.Projection("nxxxxx", PARSER, replace(cp.Limits(), cards=1))
        with self.assertRaisesRegex(ist.StoreError, "count"):
            self.commit(card(cid="b" * 32), projection=bounded)
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), prior)
        bounded = cp.Projection("nxxxxx", PARSER, replace(cp.Limits(), rows_per_batch=1))
        with self.assertRaisesRegex(ist.StoreError, "row"):
            self.store.commit(prior, 2, output(card()) * 2, b"", {"offset": 2}, projection=bounded)
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), prior)

    def test_capacity_counts_all_seats_warns_before_full_and_allows_existing_id_updates(self):
        bounded = cp.Projection("nxxxxx", PARSER, replace(cp.Limits(), cards=10))
        self.assertEqual(bounded.capacity(self.store)["remaining_identities"], 10)
        for digit in "12345678":
            self.commit(card(cid=digit * 32), projection=bounded)
        self.assertEqual(bounded.capacity(self.store)["capacity_state"], "available")
        second = self.second()
        other = cp.Projection("codex-oxxxxxx-cli", PARSER, bounded.limits)
        self.commit(card(cid="9" * 32, seat=other.seat), second, projection=other)
        self.assertEqual(bounded.capacity(self.store)["capacity_state"], "near_limit")
        self.commit(card(), projection=bounded)
        full = bounded.capacity(self.store)
        self.assertEqual((full["identities"], full["remaining_identities"], full["capacity_state"]), (10, 0, "full"))
        self.commit(card("resumed"), projection=bounded)
        self.assertEqual(self.read()["card"]["source"]["sessions"], ["one", "resumed"])
        before = self.store.resume(SOURCE, GENERATION, PARSER)
        with self.assertRaises(ist.StoreError):
            self.commit(card(cid="b" * 32), projection=bounded)
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), before)
        self.assertEqual(bounded.capacity(self.store), full)

    def test_capacity_preserves_blocked_cleanup_gap_across_reopen(self):
        self.commit(card())
        drop = {"id": CID, "drop": "synthetic_detector"}
        self.commit(None, drops=output(drop))
        status = self.projection.capacity(ist.Store(self.folder))
        self.assertEqual((status["blocked_identities"], status["blocked_server_cleanup_unverified"]), (1, 1))
        self.assertEqual((status["retention"], status["server_cleanup"]), ("not_implemented", "unverified"))
        self.assertNotIn("synthetic_detector", json.dumps(status))
        self.assertNotIn("first", json.dumps(status))

    def test_drop_blocks_whole_id_and_later_source_cannot_restore(self):
        self.commit(card())
        drop = {"id": CID, "detectors": ["synthetic-private-marker"]}
        self.commit(None, drops=output(drop))
        self.assertIsNone(self.read()["card"])
        self.assertEqual(self.read()["drop"], drop)
        self.assertNotIn("text", self.read()["drop"])
        retained = self.read()
        self.commit(card("two"), self.second())
        self.assertTrue(self.read()["blocked"])
        self.assertEqual(self.read(), retained)

    def test_legacy_batch_refuses_implicit_backfill(self):
        self.store.commit(self.initial, 1, output(card()), b"", {"offset": 1})
        prior = self.store.resume(SOURCE, GENERATION, PARSER)
        with self.assertRaisesRegex(ist.StoreConflict, "migration"):
            self.commit(card())
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), prior)
        with self.assertRaisesRegex(ist.StoreConflict, "migration"):
            self.store.commit(self.initial, 1, output(card()), b"", {"offset": 1}, projection=self.projection)
        with self.assertRaisesRegex(ist.StoreConflict, "migration"):
            self.projection.verify(self.store)

    def test_substituted_coverage_key_refuses_even_with_equal_counts(self):
        self.commit(card())
        with closing(sqlite3.connect(self.store.path)) as db:
            with db:
                db.execute("UPDATE card_projection_batches SET key=?", ("f" * 64,))
        with self.assertRaisesRegex(ist.StoreConflict, "coverage"):
            self.read()
        prior = self.store.resume(SOURCE, GENERATION, PARSER)
        with self.assertRaisesRegex(ist.StoreConflict, "coverage"):
            self.commit(card("two"))
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), prior)

    def test_corrupt_digest_refuses_read_and_update(self):
        self.commit(card())
        with closing(sqlite3.connect(self.store.path)) as db:
            with db:
                db.execute("UPDATE card_projection SET digest=?", ("f" * 64,))
        with self.assertRaisesRegex(ist.StoreConflict, "digest"):
            self.read()
        prior = self.store.resume(SOURCE, GENERATION, PARSER)
        with self.assertRaisesRegex(ist.StoreConflict, "digest"):
            self.commit(card("two"))
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), prior)

    def test_seat_namespace_cannot_replace_other_seat(self):
        self.commit(card())
        other = cp.Projection("other", PARSER)
        self.commit(card("two", seat="other"), self.second(), projection=other)
        self.assertEqual(self.read()["card"]["seat"], "nxxxxx")
        self.assertEqual(other.read(self.store, CID)["card"]["source"]["sessions"], ["two"])

    def test_fresh_process_crash_rolls_back_all_three_and_retry_commits(self):
        self.commit(card())
        source = self.second()
        prior = self.store.resume(source, GENERATION, PARSER)
        args = [sys.executable, "-B", str(Path(__file__)), "--crash", str(self.folder), source]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(args, capture_output=True, timeout=20, creationflags=flags)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(self.store.resume(source, GENERATION, PARSER), prior)
        self.assertEqual(self.read()["card"]["source"]["sessions"], ["one"])
        self.commit(card("two"), source)
        self.assertEqual(self.read()["card"]["source"]["sessions"], ["one", "two"])

    def test_fresh_process_exit_after_commit_preserves_output_card_and_offset(self):
        self.commit(card())
        source = self.second()
        args = [sys.executable, "-B", str(Path(__file__)), "--crash", str(self.folder), source, "after_commit"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(args, capture_output=True, timeout=20, creationflags=flags)
        self.assertEqual(result.returncode, 73, result.stderr)
        restored = ist.Store(self.folder)
        snap = restored.resume(source, GENERATION, PARSER)
        self.assertEqual(snap.offset, 1)
        self.assertEqual(json.loads(restored.committed(snap.batch)[1])["source"]["sessions"], ["two"])
        self.assertEqual(self.projection.read(restored, CID)["card"]["source"]["sessions"], ["one", "two"])

    def test_parser_mismatch_refuses_without_progress(self):
        with self.assertRaisesRegex(ist.StoreConflict, "parser"):
            self.commit(card(), projection=cp.Projection("nxxxxx", "other:1"))
        self.assertEqual(self.store.resume(SOURCE, GENERATION, PARSER), self.initial)

    def test_invalid_projection_limits_refuse_at_api_boundary(self):
        with self.assertRaises(ist.StoreError):
            cp.Projection('nxxxxx', PARSER, limits={'cards': 1})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--crash":
        store = ist.Store(sys.argv[2])
        snap = store.resume(sys.argv[3], GENERATION, PARSER)
        def crash(point):
            if point == (sys.argv[4] if len(sys.argv) > 4 else "after_projection"):
                os._exit(73)
        store.commit(snap, 1, output(card("two")), b"", {"offset": 1}, boundary=crash,
                     projection=cp.Projection("nxxxxx", PARSER))
    else:
        unittest.main()
