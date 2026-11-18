"""Seed a skill call and its parties straight into the tables (divergence 041 S3).

The S3 tests prove the two NOTIFY triggers and the CallBridge skeleton, which act on rows
whatever wrote them, so the rows are written here by the bootstrap role (it bypasses RLS)
rather than through `pipeline.start_call`: step 9 does not route a remote call before S5.
`args` carries a marker string so a test can show the marker never rides a notification.
"""
from __future__ import annotations

import datetime as _dt
import uuid

from psycopg.types.json import Jsonb

#: Planted in every seeded call's args, result and chunk content. A notification payload
#: that carried any of them would carry caller- or target-controlled data.
ARGS_MARKER = "s3-args-marker-must-never-ride-a-notification"


def seed_peer(conn, tag: str) -> dict:
    """A product-plane owner and one peer under it. The caller commits."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    conn.execute("INSERT INTO dashboard_users (id, email, plane) VALUES (%s,%s,'product')",
                 (owner_id, f"s3-{tag}-{owner_id.hex[:8]}@example.test"))
    conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                 (peer_id, owner_id, f"s3-{tag}-{peer_id.hex[:8]}"))
    return {"peer_id": peer_id, "owner_id": owner_id}


def seed_skill(conn, target: dict) -> uuid.UUID:
    skill_id = uuid.uuid4()
    conn.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
                 "input_schema) VALUES (%s,%s,%s,'1','sync','private','{}')",
                 (skill_id, target["peer_id"], f"s3.skill{skill_id.hex[:6]}"))
    return skill_id


def seed_call(conn, caller: dict, target: dict, skill_id: uuid.UUID, *,
              call_id: str | None = None, status: str = "pending",
              deadline: _dt.datetime | None = None) -> str:
    """One call row from `caller` to `target`. The caller commits."""
    call_id = call_id or f"s3-{uuid.uuid4()}"
    deadline = deadline or _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)
    conn.execute(
        "INSERT INTO skill_calls (call_id, caller_peer_id, caller_owner_id, target_peer_id, "
        "  target_owner_id, skill_id, skill_name, skill_version, mode, transport, status, "
        "  args, args_signature, args_signing_kid, deadline_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,'s3.skill','1','sync','ws',%s,%s,%s,%s,%s)",
        (call_id, caller["peer_id"], caller["owner_id"], target["peer_id"],
         target["owner_id"], skill_id, status,
         Jsonb({"payload": {"call_id": call_id, "note": ARGS_MARKER}}),
         f"sig-{ARGS_MARKER}", "kid-s3", deadline))
    return call_id


def call_row(conn, call_id: str):
    """Every column of the row, for a before/after comparison. The caller's transaction."""
    return conn.execute("SELECT * FROM skill_calls WHERE call_id = %s", (call_id,)).fetchone()
