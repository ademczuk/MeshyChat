"""Writes three_batch_dialogue.jsonl: a synthetic Codex dialogue in three batches for the context.v1 acceptance test.

Same record shapes as codex-kxxx-cli's context_acceptance.py. Its expected items (EXPECTED.md) were written by hand
before any parser ran on it. Run from the repository root: python tests/fixtures/context_v1/make_three_batch.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRET = "-----BEGIN PRIVATE KEY-----"


def at(second):
    return "2026-10-19T17:%02d:%02d.000Z" % (second // 60, second % 60)


def record(kind, payload, second):
    row = {"type": kind, "payload": payload}
    if second is not None:
        row["timestamp"] = at(second)
    return row


def user(identity, text, second):
    return record("response_item", {"type": "message", "id": identity, "role": "user",
                                    "content": [{"type": "input_text", "text": text}]}, second)


def assistant(identity, text, second):
    return record("response_item", {"type": "message", "id": identity, "role": "assistant", "phase": "final_answer",
                                    "content": [{"type": "output_text", "text": text}]}, second)


def plan(call, steps, second):
    return record("response_item", {"type": "function_call", "call_id": call, "name": "update_plan",
                                    "arguments": json.dumps({"plan": [{"step": s, "status": st} for s, st in steps]})},
                  second)


ROWS = [
    record("session_meta", {"id": "synthetic-three-batch", "originator": "codex-tui", "source": "cli"}, 0),
    # batch 1
    user("u1", "Please summarise the plan for the scratch server.", 10),
    assistant("a1", "The plan: rebase both branches, gate them, then redeploy the scratch server.", 20),
    plan("p1", [("Rebase", "pending"), ("Gate", "pending")], 30),
    # batch 2: a secret the screen refuses, and a record with no usable time (the deliberate gap)
    user("u2", "Here is the deploy key, use it:\n" + SECRET + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n", 40),
    assistant("a2-undated", "This record has no timestamp, so it is skipped and takes no ordinal.", None),
    assistant("a3", "I will not store that key. The rebase is done.", 60),
    plan("p2", [("Rebase", "completed"), ("Gate", "pending")], 70),
    # batch 3
    user("u3", "Thanks. Continue.", 80),
    assistant("a4", "The gate passed. The redeploy is next.", 90),
    plan("p3", [("Rebase", "completed"), ("Gate", "completed")], 100),
]

if __name__ == "__main__":
    out = HERE / "three_batch_dialogue.jsonl"
    out.write_bytes(b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8") for r in ROWS))
    print(out)
