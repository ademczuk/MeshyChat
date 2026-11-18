"""Synthetic cryptographic readback and private receipt-commit regressions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.meshy_node import node, wire
from authenticated_readback import ReadbackPending, reconcile_present
from frozen_outbox import FrozenOutbox, Store, StoreError
from test_frozen_outbox import SOURCE, GENERATION, PARSER, bundle

WRITER = "11111111-1111-4111-8111-111111111111"
MEMORY = "22222222-2222-4222-8222-222222222222"
SEED, SERVER = bytes([7]) * 32, bytes([8]) * 32


def jwks(seed, kid):
    return {"keys": [{"kid": kid, "kty": "OKP", "crv": "Ed25519",
                       "x": wire.b64u(wire.public_key_of(seed))}]}


def sign_call(call):
    payload = {"kind": "skill.result", **{k: deepcopy(call.get(k)) for k in wire.RESULT_BOUND_FIELDS}}
    call.update(result_payload=payload, result_signing_kid="server",
                result_signature=wire.b64u(wire.sign(SERVER, wire.jcs(payload))))


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "private"
        self.store = Store(self.folder)
        snapshot = self.store.enroll(SOURCE, GENERATION, PARSER, {}, {"next": 0})
        self.outbox = FrozenOutbox(self.store)
        self.batch = self.outbox.freeze(snapshot, 100, *bundle(), {"next": 1}).key
        self.action = self.outbox.begin_send(self.batch)
        self.state = {"peer_id": WRITER, "memory_peer_id": MEMORY, "kid": "writer",
                      "base_url": "http://mesh.invalid:1", "signing_seed_b64url": wire.b64u(SEED)}
        self.writer_keys = jwks(SEED, "writer")

    def envelope(self, skill, args):
        return wire.call_envelope(seed=SEED, kid="writer", caller_peer_id=WRITER,
                                  target_peer_id=MEMORY, skill=skill, args=args)

    def result(self):
        args = {"namespace": "shared/transcript-context", "key": self.action.record.key,
                "include_provenance": True}
        request = self.envelope("memory.read", args)
        value = json.loads(self.action.record.value)
        memo = {"namespace": args["namespace"], "key": args["key"], "value": value,
                "version": 1, "is_head": True, "created_by_peer_id": WRITER,
                "provenance": self.envelope("memory.write", {"namespace": args["namespace"],
                                                            "key": args["key"], "value": value})}
        call = {"call_id": request["payload"]["call_id"], "status": "completed",
                "result": {"ok": True, "memo": memo}, "error_code": None, "error_message": None}
        sign_call(call)
        return {"call_id": call["call_id"], "result_signature_verified": True,
                "verification_evidence": {"call": call, "request_envelope": request,
                                          "server_jwks": jwks(SERVER, "server")}}

    def reconcile(self, result, *, keys=None, boundary=None):
        with patch.object(node, "invoke", return_value=result) as invoke, \
             patch.object(node, "author_keys", return_value=(keys or self.writer_keys, None)):
            receipt = reconcile_present(self.outbox, self.batch, state_path=Path("unused"),
                                        state=self.state, seat="synthetic-seat", writer_peer_id=WRITER,
                                        boundary=boundary)
            self.assertEqual("memory.read", invoke.call_args.kwargs["skill"])
            self.assertTrue(invoke.call_args.kwargs["include_verification_evidence"])
            return receipt

    def test_signed_match_and_proof_survive_reopen(self):
        result = self.result()
        result["result_signature_verified"] = False  # Flags do not grant or deny cryptographic proof.
        receipt = self.reconcile(result)
        self.assertEqual("matched", receipt.state)
        reopened = FrozenOutbox(Store(self.folder))
        raw = reopened.evidence(receipt.evidence_sha256)
        self.assertEqual(receipt.evidence_sha256, hashlib.sha256(raw).hexdigest())
        proof = json.loads(raw)
        self.assertEqual(WRITER, proof["writer_peer_id"])
        self.assertNotIn(self.state["signing_seed_b64url"].encode(), raw)
        self.assertEqual("send", reopened.next_action(self.batch).kind)

    def test_signed_different_value_is_durable_conflict(self):
        result = self.result()
        memo = result["verification_evidence"]["call"]["result"]["memo"]
        memo["value"]["text"] = "Different synthetic record"
        memo["value"]["text_sha256"] = hashlib.sha256(memo["value"]["text"].encode()).hexdigest()
        memo["provenance"] = self.envelope("memory.write", {"namespace": memo["namespace"],
                                                          "key": memo["key"], "value": memo["value"]})
        sign_call(result["verification_evidence"]["call"])
        receipt = self.reconcile(result)
        self.assertEqual("conflict", receipt.state)
        self.assertEqual("review_conflict", FrozenOutbox(Store(self.folder)).next_action(self.batch).kind)
        self.assertEqual(bundle()[0][0].value, self.action.record.value)

    def test_invalid_or_unsupported_evidence_stays_pending(self):
        mutations = {
            "version-bool": lambda r, p, c, m: m.update(version=True),
            "version-float": lambda r, p, c, m: m.update(version=1.0),
            "non-head": lambda r, p, c, m: m.update(is_head=False),
            "old-version": lambda r, p, c, m: m.update(version=2),
            "parent": lambda r, p, c, m: m.update(parent_id="previous"),
            "edited": lambda r, p, c, m: m.update(edited_at="synthetic"),
            "redacted": lambda r, p, c, m: m.update(redacted_at="synthetic"),
            "wrong-writer": lambda r, p, c, m: m.update(created_by_peer_id=MEMORY),
            "wrong-key": lambda r, p, c, m: m.update(key="different"),
            "wrong-namespace": lambda r, p, c, m: m.update(namespace="shared/mesh-chat"),
            "wrong-seat": lambda r, p, c, m: m["value"].update(seat="another-seat"),
            "missing-memo": lambda r, p, c, m: c.update(result={"ok": True}),
            "refused": lambda r, p, c, m: c.update(status="failed", error_code="E_DENIED"),
            "replayed-call": lambda r, p, c, m: c.update(call_id="different-call"),
            "wrong-request": lambda r, p, c, m: p["request_envelope"]["payload"]["args"].update(key="other"),
            "wrong-author-signature": lambda r, p, c, m: m["provenance"].update(signature=wire.b64u(bytes(64))),
            "duplicate-server-key": lambda r, p, c, m: p["server_jwks"]["keys"].append(deepcopy(p["server_jwks"]["keys"][0])),
            "oversized-proof": lambda r, p, c, m: p.update(extra="x" * 524288),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                result = self.result()
                proof = result["verification_evidence"]
                call = proof["call"]
                mutate(result, proof, call, call["result"]["memo"])
                sign_call(call)  # Even a genuine server signature cannot authorize these shapes.
                with self.assertRaises(ReadbackPending):
                    self.reconcile(result)
                self.assertEqual("read", self.outbox.next_action(self.batch).kind)
        for malformed in (None, {}, {"result_signature_verified": True}):
            with self.subTest(malformed=malformed), self.assertRaises(ReadbackPending):
                self.reconcile(malformed)

    def test_forged_server_flag_cannot_progress(self):
        result = self.result()
        result["verification_evidence"]["call"]["result_signature"] = wire.b64u(bytes(64))
        with self.assertRaises(ReadbackPending):
            self.reconcile(result)
        self.assertEqual("read", self.outbox.next_action(self.batch).kind)

    def test_strict_server_binding_rejects_bool_integer_alias(self):
        result = self.result()
        call = result["verification_evidence"]["call"]
        call["result"]["memo"]["version"] = True  # Signed version remains integer one.
        with self.assertRaises(ReadbackPending):
            self.reconcile(result)

    def test_rotated_or_duplicate_author_keys_stay_pending(self):
        for keys in ({"keys": []}, jwks(SERVER, "writer"),
                     {"keys": self.writer_keys["keys"] * 2}):
            with self.subTest(keys=keys), self.assertRaises(ReadbackPending):
                self.reconcile(self.result(), keys=keys)

    def test_evidence_and_receipt_rollback_together(self):
        def crash(point):
            if point == "after_evidence_output":
                raise RuntimeError("synthetic crash")
        with self.assertRaises(RuntimeError):
            self.reconcile(self.result(), boundary=crash)
        self.assertEqual("read", self.outbox.next_action(self.batch).kind)
        with self.store._connection() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM outbox_evidence_v1").fetchone()[0])
        self.assertEqual("matched", self.reconcile(self.result()).state)

    def test_artifact_digest_mismatch_cannot_progress(self):
        with self.assertRaises(StoreError):
            self.outbox.observe(self.action.attempt, value=self.action.record.value,
                                evidence_sha256="a" * 64, evidence_artifact=b"synthetic proof")
        self.assertEqual("read", self.outbox.next_action(self.batch).kind)

    def test_invoke_opt_in_keeps_default_output_and_fresh_call_guard(self):
        for opt_in, replay in ((False, False), (True, False), (True, True)):
            with self.subTest(opt_in=opt_in, replay=replay):
                captured = {}
                def http(method, base, path, **kwargs):
                    if method == "POST":
                        captured.update(kwargs["body"])
                        return 202, {"status": "pending"}
                    if path == "/.well-known/jwks.json":
                        return 200, jwks(SERVER, "server")
                    call = {"call_id": "old-call" if replay else captured["call_id"],
                            "status": "completed", "result": {"ok": True},
                            "error_code": None, "error_message": None}
                    sign_call(call)
                    return 200, call
                with patch.object(node, "require_peer"), patch.object(node, "fresh_jwt", return_value="SYNTHETIC-JWT"), \
                     patch.object(node, "_proved_headers", return_value={}), patch.object(node, "http_json", side_effect=http):
                    result = node.invoke(Path("unused"), self.state, skill="memory.read", args={},
                                         include_verification_evidence=opt_in)
                self.assertEqual(not replay, result["result_signature_verified"])
                self.assertEqual(opt_in, "verification_evidence" in result)
                self.assertNotIn("SYNTHETIC-JWT", json.dumps(result))
                self.assertNotIn(self.state["signing_seed_b64url"], json.dumps(result))



    def server_failure(self, code):
        """A failed read in the server's own shape: apps/calls/route._dispatch maps the handler's HandlerError."""
        from apps.calls import route
        from apps.memoryskills import handlers
        from apps.memoryskills.errors import HandlerError
        result = self.result()
        missing = HandlerError(code, f"no memo at shared/transcript-context/{self.action.record.key}")
        with patch.object(handlers, "dispatch", side_effect=missing):
            status, body, error_code, error_message = route._dispatch(None, "memory.read", {}, WRITER)
        call = result["verification_evidence"]["call"]
        call.update(status=status, result=body, error_code=error_code, error_message=error_message)
        sign_call(call)
        return result

    def absent_result(self):
        return self.server_failure("E_UNKNOWN_KEY")

    def test_absence_fixture_is_the_server_shape(self):
        call = self.absent_result()["verification_evidence"]["call"]
        self.assertEqual(call["status"], "failed")
        self.assertEqual(call["result"], {"ok": False, "error_code": "E_UNKNOWN_KEY",
                                          "error_message": call["error_message"]})
        self.assertIsInstance(call["error_message"], str)

    def test_another_server_failure_is_not_absence(self):
        for code in ("E_FORBIDDEN", "E_DENIED", "E_INTERNAL"):
            with self.subTest(code=code):
                with self.assertRaises(ReadbackPending):
                    self.reconcile(self.server_failure(code))
                self.assertEqual(self.outbox.next_action(self.batch).kind, "read")

    def test_authenticated_absence_allows_same_frozen_send(self):
        original_key, original_value = self.action.record.key, self.action.record.value
        receipt = self.reconcile(self.absent_result())
        self.assertEqual(receipt.state, "ready")
        action = self.outbox.next_action(self.batch)
        self.assertEqual(action.kind, "send")
        self.assertEqual((action.record.key, action.record.value),
                         (original_key, original_value))
        retried = self.outbox.begin_send(self.batch)
        self.assertNotEqual(retried.attempt.token, self.action.attempt.token)
        self.assertEqual((retried.record.key, retried.record.value),
                         (original_key, original_value))
        self.assertEqual(self.reconcile(self.result()).state, "matched")

    def test_absence_rejects_unbound_or_ambiguous_proof(self):
        def wrong_code(r):
            r["verification_evidence"]["call"]["error_code"] = "E_FORBIDDEN"
        def completed(r):
            r["verification_evidence"]["call"]["status"] = "completed"
        def empty(r):
            r["verification_evidence"]["call"]["result"] = {}
        def null(r):  # the shape this PR first assumed; the server never sends it
            r["verification_evidence"]["call"]["result"] = None
        def missing_result(r):
            del r["verification_evidence"]["call"]["result"]
        def extra_key(r):
            r["verification_evidence"]["call"]["result"]["memo"] = None
        def ok_true(r):
            r["verification_evidence"]["call"]["result"]["ok"] = True
        def ok_zero(r):  # 0 == False, but only the boolean is the server's shape
            r["verification_evidence"]["call"]["result"]["ok"] = 0
        def body_code(r):
            r["verification_evidence"]["call"]["result"]["error_code"] = "E_FORBIDDEN"
        def body_message(r):
            r["verification_evidence"]["call"]["result"]["error_message"] = "another message"
        def null_message(r):
            call = r["verification_evidence"]["call"]
            call["error_message"] = call["result"]["error_message"] = None
        for mutation in (wrong_code, completed, empty, null, missing_result, extra_key, ok_true, ok_zero,
                         body_code, body_message, null_message):
            with self.subTest(mutation=mutation.__name__):
                result = self.absent_result()
                mutation(result)
                sign_call(result["verification_evidence"]["call"])
                with self.assertRaises(ReadbackPending):
                    self.reconcile(result)
                self.assertEqual(self.outbox.next_action(self.batch).kind, "read")

    def test_absence_requires_all_signed_result_fields(self):
        result = self.absent_result()
        call = result["verification_evidence"]["call"]
        del call["result_payload"]["result"]
        call["result_signature"] = wire.b64u(wire.sign(SERVER, wire.jcs(call["result_payload"])))
        with self.assertRaises(ReadbackPending):
            self.reconcile(result)
        self.assertEqual(self.outbox.next_action(self.batch).kind, "read")

    def test_absence_evidence_commit_is_atomic(self):
        def crash(name):
            if name == "after_evidence_output":
                raise RuntimeError("simulated crash")
        with self.assertRaises(RuntimeError):
            self.reconcile(self.absent_result(), boundary=crash)
        self.assertEqual(self.outbox.next_action(self.batch).kind, "read")
        self.assertEqual(self.reconcile(self.absent_result()).state, "ready")

if __name__ == "__main__":
    unittest.main()
