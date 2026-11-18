"""Write invocation frames with the previous frame's hash as the link per chain,
the root's hash as `parent_hash`, the hash over the canonical payload, and optionally an
Ed25519 signature over the domain-separated hash with its server kid.

The hash is `apps.calls.frame_audit.compute_frame_hash`, which tests/test_frame_audit.py pins
against captured known-answer bytes. Rows go in on the owner connection (`meshychat_app` may INSERT frames but has
no UPDATE or DELETE on the table, sql/062): the tests tamper as the owner does.
"""
from __future__ import annotations

import uuid

from apps.calls import frame_audit as FA
from apps.crypto import primitives as P

#: A test-only server key: the seed, its raw public key, and its server kid.
SEED = bytes(range(1, 33))
PUBLIC = P.public_key_of(SEED)
KID = P.server_kid(PUBLIC)
KEYS = {KID: PUBLIC}


def base_row(**over) -> dict:
    row = {
        "chain_id": None, "capability_ref": None, "delegation_depth": 0,
        "invocation_nonce": None, "decision": "allow", "deny_reason": None,
        "call_id": f"fa-{uuid.uuid4().hex[:12]}",
        "caller_owner_id": uuid.uuid4(), "caller_peer_id": uuid.uuid4(),
        "target_owner_id": uuid.uuid4(), "target_peer_id": uuid.uuid4(),
        "skill_name": "memory.query", "mandate_hash": None, "flow_guard_mode": None,
        "flow_root_kind": None, "continuation_nonce": None,
    }
    row.update(over)
    return row


def insert(conn, row: dict, *, previous_frame_hash=None, parent_hash=None, sign: bool = False,
           frame_id: int | None = None, frame_hash: str | None = None) -> dict:
    """Insert one frame. Returns the row as stored (id, frame_hash included)."""
    fh = frame_hash or FA.compute_frame_hash(row, previous_frame_hash)
    sig, kid = (P.sign(SEED, FA.signature_material(fh)), KID) if sign else (None, None)
    cols = ["chain_id", "capability_ref", "parent_hash", "delegation_depth", "invocation_nonce",
            "previous_frame_hash", "frame_hash", "decision", "deny_reason", "call_id",
            "caller_owner_id", "caller_peer_id", "target_owner_id", "target_peer_id",
            "skill_name", "mandate_hash", "flow_guard_mode", "flow_root_kind",
            "continuation_nonce", "frame_signature", "signing_kid"]
    vals = [row["chain_id"], row["capability_ref"], parent_hash, row["delegation_depth"],
            row["invocation_nonce"], previous_frame_hash, fh, row["decision"],
            row["deny_reason"], row["call_id"], row["caller_owner_id"], row["caller_peer_id"],
            row["target_owner_id"], row["target_peer_id"], row["skill_name"],
            row["mandate_hash"], row["flow_guard_mode"], row["flow_root_kind"],
            row["continuation_nonce"], sig, kid]
    if frame_id is not None:
        cols.insert(0, "id")
        vals.insert(0, frame_id)
    got = conn.execute(f"INSERT INTO invocation_frames ({', '.join(cols)}) VALUES "
                       f"({', '.join(['%s'] * len(vals))}) RETURNING id", vals).fetchone()
    return {**row, "id": got[0], "frame_hash": fh, "previous_frame_hash": previous_frame_hash,
            "parent_hash": parent_hash}


def chain(conn, n: int, *, chain_id=None, sign: bool = False, **over) -> list[dict]:
    """A new chain of `n` frames, linked and anchored as the writer links them."""
    chain_id = chain_id or uuid.uuid4()
    out: list[dict] = []
    for _ in range(n):
        out.append(extend(conn, out, chain_id=chain_id, sign=sign, **over))
    return out


def extend(conn, frames: list[dict], *, chain_id=None, sign: bool = False,
           frame_id: int | None = None, **over) -> dict:
    """The next frame of the chain `frames` (a root when it is empty)."""
    chain_id = chain_id or frames[0]["chain_id"]
    prev = frames[-1]["frame_hash"] if frames else None
    parent = frames[0]["frame_hash"] if frames else None
    row = base_row(chain_id=chain_id, capability_ref=uuid.uuid4(), **over)
    return insert(conn, row, previous_frame_hash=prev, parent_hash=parent, sign=sign,
                  frame_id=frame_id)


def standalone(conn, *, sign: bool = False, **over) -> dict:
    return insert(conn, base_row(**over), sign=sign)


def code_strings(module) -> list[str]:
    """Every string literal in `module`'s source except docstrings (which quote constructs
    such as `COUNT(*)` to explain their absence): the text a rule-ii check must scan."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]
