"""The ONE terminal mapping (divergence 041 S6; design 4.7, "One terminal mapping").

A finished call is told to its caller on two surfaces: the WebSocket terminal frame
(`apps/wsgw/caller_frames.terminal_frame`) and the HTTP terminal body
(`apps/calls/views._terminal_body`). Both read the row through `of_row` below and nothing
else, so a WS caller and an HTTP poller cannot be told two outcomes for one row. It applies
divergence 031's one-decision rule (the push and addressed long-poll both call one
`role_for`) to terminals.

WHAT `of_row` DECIDES:

  * `status`: the row's, when it is one of the four terminals; anything else is not terminal
    and gives None for a spurious or early nudge.
  * `signed`: whether the caller gets a signed skill_result. It is decided by the signature,
    not the status: a completed row always does. A failed row does only when it carries BOTH
    its canonical payload and a signature; an
    unsigned failure (the skill_error frame, a server-side failure that ran nothing) does not.
  * `payload` and `payload_reconstructed`: the canonical payload the signature covers,
    `result_payload`. A completed row without one uses its `result` and is MARKED
    reconstructed so a caller can tell a legacy row that never stored a payload from a row
    whose payload was tampered with. This
    server writes `result_payload` (apps/calls/route.py `_finish`,
    apps/wsgw/target_frames.py `result`), so this arm is for rows another writer left.
  * `result`, `signature`, `signing_kid`, `error_code`, `error_message`, `completed_at`: the
    row's own values, unchanged. Each surface then renders them as its wire says: the frame
    writes an absent signature as "" and an absent failure code as "FAILED"; the HTTP body
    writes them as null.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, NamedTuple

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_ABANDONED = "abandoned"

#: The four statuses that end a call (sql/006's CHECK).
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_TIMEOUT,
                               STATUS_ABANDONED})


class Terminal(NamedTuple):
    """What one terminal row tells its caller, whichever surface carries it."""

    call_id: str
    status: str
    signed: bool
    payload: Any
    payload_reconstructed: bool
    result: Any
    signature: str | None
    signing_kid: str | None
    error_code: str | None
    error_message: str | None
    completed_at: _dt.datetime | None


def of_row(row: dict) -> Terminal | None:
    """The outcome a terminal skill_calls row tells, or None when the row is not terminal.

    `row` is a `reads.CALL_COLUMNS` dict (apps/calls/reads.load_call); only the columns
    named above are read."""
    status = row["status"]
    if status not in TERMINAL_STATUSES:
        return None
    payload = row["result_payload"]
    signature = row["result_signature"]
    reconstructed = False
    if status == STATUS_COMPLETED:
        signed = True
        if payload is None:
            payload, reconstructed = row["result"], True
    elif status == STATUS_FAILED:
        signed = payload is not None and signature is not None
    else:
        signed = False
    return Terminal(call_id=row["call_id"], status=status, signed=signed, payload=payload,
                    payload_reconstructed=reconstructed, result=row["result"],
                    signature=signature, signing_kid=row["result_signing_kid"],
                    error_code=row["error_code"], error_message=row["error_message"],
                    completed_at=row["completed_at"])


__all__ = ["TERMINAL_STATUSES", "Terminal", "of_row"]
