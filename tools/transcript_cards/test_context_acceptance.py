"""Adversarial checks for the independent synthetic recovery oracle."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import context_acceptance as acceptance
import context_parser


def bridge(case):
    rows = []
    for item in case["items"]:
        # Deliberately split inside words and use multi-byte characters; the
        # oracle must reconstruct exact approved text rather than search tails.
        chunks = [item["text"][n:n + 400] for n in range(0, len(item["text"]), 400)]
        rows += [{"id": item["id"], "role": item["role"], "part": n + 1, "parts": len(chunks), "text": text,
                  "text_sha256": item["text_sha256"], "omitted_bytes": 0}
                 for n, text in enumerate(chunks)]
    return {"case": case["case"], "namespace": case["namespace"], "items": rows, "plan_states": copy.deepcopy(case["plan_states"])}


class ContextAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.cases = acceptance.corpus()
        self.visible = self.cases[0]
        self.observed = bridge(self.visible)

    def check(self, changed, error):
        self.assertIn(error, acceptance.verify(self.visible, changed))

    def test_all_explicit_expected_outcomes_pass_oracle_only(self):
        for case in self.cases:
            with self.subTest(case=case["case"]):
                self.assertEqual(acceptance.verify(case, bridge(case)), [])

    def test_assistant_copy_cannot_replace_missing_operator_turn(self):
        case = next(row for row in self.cases if row["case"] == "codex-user-and-assistant-authority")
        observed = bridge(case)
        observed["items"] = [row for row in observed["items"] if row["role"] != "user"]
        self.assertIn("missing_item", acceptance.verify(case, observed))

    def test_identical_text_cannot_exchange_human_and_assistant_authority(self):
        case = next(row for row in self.cases if row["case"] == "codex-user-and-assistant-authority")
        observed = bridge(case)
        for row in observed["items"]:
            row["role"] = "assistant" if row["role"] == "user" else "user"
        self.assertIn("invalid_parts_digest_or_omission", acceptance.verify(case, observed))

    def test_every_chunk_keeps_its_role(self):
        case = next(row for row in self.cases if row["case"] == "codex-user-and-assistant-authority")
        observed = bridge(case)
        observed["items"][1]["role"] = "assistant"
        self.assertIn("invalid_parts_digest_or_omission", acceptance.verify(case, observed))

    def test_parts_can_arrive_out_of_order(self):
        self.observed["items"].reverse()
        self.assertEqual(acceptance.verify(self.visible, self.observed), [])

    def test_missing_tail_fails_even_if_earlier_text_is_valid(self):
        self.observed["items"].pop()
        self.check(self.observed, "incomplete_parts")

    def test_duplicate_part_cannot_replace_missing_part(self):
        self.observed["items"][-1] = copy.deepcopy(self.observed["items"][-2])
        self.check(self.observed, "invalid_parts_digest_or_omission")

    def test_truncation_with_expected_digest_still_fails(self):
        self.observed["items"][-1]["text"] = ""
        self.check(self.observed, "approved_text_mismatch")

    def test_changed_digest_fails(self):
        self.observed["items"][-1]["text_sha256"] = "0" * 64
        self.check(self.observed, "invalid_parts_digest_or_omission")

    def test_explicit_omission_is_not_full_recovery(self):
        self.observed["items"][-1]["omitted_bytes"] = 12
        self.check(self.observed, "invalid_parts_digest_or_omission")

    def test_event_copy_is_not_a_second_visible_item(self):
        duplicate = copy.deepcopy(self.observed["items"][-1])
        duplicate["id"] = "event-agent-message-copy"
        self.observed["items"].append(duplicate)
        self.check(self.observed, "unexpected_item")

    def test_private_analysis_is_not_visible_output(self):
        self.observed["items"].append({"id": "private-analysis", "text": acceptance.PRIVATE})
        self.check(self.observed, "unexpected_item")

    def test_boolean_part_cannot_masquerade_as_integer(self):
        self.observed["items"][0]["part"] = True
        self.check(self.observed, "invalid_parts_digest_or_omission")

    def test_boolean_omission_cannot_masquerade_as_zero(self):
        self.observed["items"][0]["omitted_bytes"] = False
        self.check(self.observed, "invalid_parts_digest_or_omission")

    def test_whole_blocked_item_must_drop_not_just_the_tail(self):
        for case in acceptance.corpus(namespace="shared/screened-fixture")[3:]:
            observed = bridge(case)
            observed["items"] = [{"id": "blocked-prefix", "text": "Clean early prefix"}]
            self.assertIn("unexpected_item", acceptance.verify(case, observed))

    def test_plan_reorder_cannot_emit_a_completion(self):
        case = self.cases[1]
        observed = bridge(case)
        observed["plan_states"][2]["new_completions"] = 1
        self.assertIn("plan_state_mismatch", acceptance.verify(case, observed))

    def test_boolean_cannot_replace_plan_completion_count(self):
        case = self.cases[1]
        observed = bridge(case)
        observed["plan_states"][1]["new_completions"] = True
        self.assertIn("plan_state_mismatch", acceptance.verify(case, observed))

    def test_unbounded_parts_refuse_before_reassembly(self):
        self.observed["items"][0]["parts"] = 10 ** 9
        self.check(self.observed, "incomplete_parts")

    def test_wrong_case_does_not_borrow_another_fixture(self):
        self.observed["case"] = "another-case"
        self.check(self.observed, "case_mismatch")

    def test_removal_cannot_silently_close_unfinished_work(self):
        case = self.cases[1]
        observed = bridge(case)
        observed["plan_states"][3]["unresolved_steps"] = []
        self.assertIn("plan_state_mismatch", acceptance.verify(case, observed))

    def test_ambiguous_completed_and_removed_is_not_success(self):
        case = self.cases[2]
        observed = bridge(case)
        observed["plan_states"][-1]["new_completions"] = 1
        self.assertIn("plan_state_mismatch", acceptance.verify(case, observed))

    def test_generated_byte_boundaries_are_real_and_tail_is_over_8k(self):
        self.assertGreater(len(acceptance.VISIBLE[:acceptance.VISIBLE.index(acceptance.TAIL)].encode()), 8192)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "corpus"
            manifest = acceptance.write_corpus(output)
            self.assertFalse(manifest["graph_recovery_proved"])
            for case in manifest["cases"]:
                raw = (output / case["source_file"]).read_bytes()
                self.assertEqual(len(raw), case["source_bytes"])
                for start, end in zip(case["record_offsets"], case["record_ends"]):
                    self.assertTrue(raw[start:end].endswith(b"\n"))
                    json.loads(raw[start:end])
            with self.assertRaises(FileExistsError):
                acceptance.write_corpus(output)

    def test_namespace_cannot_silently_switch_recovery_policy(self):
        self.observed['namespace'] = 'shared/screened-fixture'
        self.check(self.observed, 'namespace_mismatch')

    def test_current_parser_keeps_unscreened_text_and_role_independently(self):
        # A bridge of REAL current-parser outputs for every dialogue-only case.
        # Chunking here is a test observation format, not a production parser.
        for case in self.cases:
            if case['plan_states'] or 'subagent' in case['case']:
                continue
            with self.subTest(case=case['case']):
                parser = context_parser.ContextParser('codex-kxxx-cli', 'codex',
                    namespace=acceptance.NAMESPACE, denylist=[acceptance.CLIENT])
                for record in case['records']:
                    parser.feed(record)
                ids = {context_parser._hex32(item['id']): item['id'] for item in case['items']}
                rows = []
                for actual in parser.items:
                    self.assertIn(actual.get('native_ref'), ids)
                    self.assertIn(actual['role'], ('user', 'assistant'))
                    self.assertEqual(actual['text_sha256'], acceptance.digest(actual['text']))
                    chunks = [actual['text'][n:n+400] for n in range(0, len(actual['text']), 400)]
                    rows.extend({'id': ids[actual['native_ref']], 'role': actual['role'],
                        'part': n+1, 'parts': len(chunks), 'text': text,
                        'text_sha256': actual['text_sha256'], 'omitted_bytes': 0}
                        for n, text in enumerate(chunks))
                observed = {'case': case['case'], 'namespace': acceptance.NAMESPACE,
                            'items': rows, 'plan_states': []}
                self.assertEqual(acceptance.verify(case, observed), [])


if __name__ == "__main__":
    unittest.main()
