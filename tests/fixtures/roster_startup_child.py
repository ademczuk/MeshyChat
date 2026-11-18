"""The served app's startup, for tests/test_fleet_roster_startup.py. Run ONLY as a subprocess.

It boots the way daphne and uvicorn do: `import meshychat.asgi`, whose
get_asgi_application() runs django.setup(), with whatever MESHY_FLEET_ROSTER_PATH the parent
sets (divergence 034). A refused startup is this process exiting non-zero with the error on
stderr, as a server's would. Otherwise it prints one line, `ROSTER_RESULT <json>` (prefixed:
meshychat/settings.py logs to stdout, and the parent counts those log lines too):

  labels         {peer_id: handlers.roster_label_of(peer_id)} for ROSTER_CHILD_PEERS
  after_rewrite  the same, after this process overwrote the roster file
                 (ROSTER_CHILD_REWRITE=1): the bytes read at startup are not read again
  writes         for ROSTER_CHILD_WRITER (a JSON file: peer_id, seed_hex, kid, label), each
                 mesh write's outcome through the real handler on a meshychat_app connection:
                 "ok" or the HandlerError code
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import meshychat.asgi  # noqa: E402,F401  - the served entrypoint; django.setup() runs here

from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import db, handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402

MESH = "shared/mesh-chat"


def _envelope(w: dict, args: dict) -> dict:
    payload = {"v": 1, "kind": "skill.call", "call_id": f"call-{uuid.uuid4()}",
               "caller_peer_id": w["peer_id"], "target_peer_id": w["peer_id"],
               "skill_name": "memory.write", "skill_version": "1", "args": args,
               "args_attachments": [],
               "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    seed = bytes.fromhex(w["seed_hex"])
    return {"payload": payload, "signature": P.b64u(P.sign(seed, P.jcs(payload))),
            "signing_kid": w["kid"]}


def _write(conn, w: dict, args: dict) -> str:
    try:
        handlers.dispatch("memory.write", _envelope(w, args), w["peer_id"], conn)
    except HandlerError as exc:
        return exc.code
    return "ok"


def _message(sender: str) -> dict:
    return {"schema_version": 1, "to": ["all"], "from": sender, "type": "status",
            "thread_id": "roster/probe", "ts": "2026-09-24T00:00:00Z",
            "body": "synthetic roster probe", "author_family": "claude"}


def main() -> dict:
    out: dict = {}
    peers = [p for p in os.environ.get("ROSTER_CHILD_PEERS", "").split(",") if p]
    out["labels"] = {p: handlers.roster_label_of(p) for p in peers}
    if os.environ.get("ROSTER_CHILD_REWRITE"):
        Path(os.environ["MESHY_FLEET_ROSTER_PATH"]).write_text(
            json.dumps({"peers": {p: "rewritten-after-startup" for p in peers}}),
            encoding="utf-8")
        out["after_rewrite"] = {p: handlers.roster_label_of(p) for p in peers}
    writer = os.environ.get("ROSTER_CHILD_WRITER")
    if writer:
        w = json.loads(Path(writer).read_text(encoding="utf-8"))
        label, ms = w["label"], int(time.time() * 1000)
        conn = db.connect(os.environ["MESHY_DSN"])
        try:
            def key(n: int) -> str:
                return f"msg/{ms + n}-{label}-{secrets.token_hex(3)}"
            out["writes"] = {
                # The legacy AgentMeshy node: from AND --author are its roster label.
                "from_and_author": _write(conn, w, {"namespace": MESH, "key": key(0),
                                                    "value": _message(label),
                                                    "author": label}),
                # from alone; the author defaults to the caller-derived label.
                "from_only": _write(conn, w, {"namespace": MESH, "key": key(1),
                                              "value": _message(label)}),
                # The AgentMeshy sender's default author, <from>-cc: NOT an alias of the
                # roster label (operator ruling, divergence 028).
                "author_cc": _write(conn, w, {"namespace": MESH, "key": key(2),
                                              "value": _message(label),
                                              "author": f"{label}-cc"}),
            }
        finally:
            conn.close()
    return out


if __name__ == "__main__":
    print("ROSTER_RESULT " + json.dumps(main()), flush=True)
