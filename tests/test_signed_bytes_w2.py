"""B02 phase A1 over W2, A2A `message:send`. Divergence 039, design Q4 and test 5.

W2 reaches the same call pipeline as W1 (apps/a2a/views.py -> pipeline.start_call), so a
memory.write payload holding a number beyond 2^53-1 is refused at step 1b, before any call
row, with the same typed code. Fixtures are tests/test_a2a.py's; its target peer has no
memory.write skill, so the control (1e15, one variable apart) goes on past the seam and is
refused at step 3 instead, which is what shows the seam was the only difference.

Run:  .venv/Scripts/python.exe -m pytest tests/test_signed_bytes_w2.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_a2a import (  # noqa: E402,F401
    a2a_message, a2a_settings, body_of, calls_with_id, client, owner_conn, post_send,
    sign_call, types, world,
)


def test_5_w2_refuses_a_memory_write_number_beyond_2_53_minus_1_before_the_call_row(
        client, owner_conn, world):
    def send(value):
        args = {"namespace": "w2/ns", "key": "k", "value": value}
        payload, envelope = sign_call(world, args=args, skill_name="memory.write")
        r = post_send(client, world, message=a2a_message(parts=[types.part_data(args)]),
                      envelope=envelope, call_id=payload["call_id"],
                      skill_name="memory.write")
        return r, payload["call_id"]

    refused, call_id = send({"n": 1e16})
    assert refused.status_code == 400, refused.content
    assert body_of(refused)["error"] == "E_NUMBER_OUT_OF_RANGE"
    assert calls_with_id(owner_conn, call_id) == 0

    control, _ = send({"n": 1e15})
    assert control.status_code == 404, control.content      # step 3: no such skill there
    assert body_of(control)["error"] != "E_NUMBER_OUT_OF_RANGE"
