"""T21: site 11 stays tokenless (B07 slice 6, docs/DIVERGENCES.md 040; design
docs/design/biscuit-capabilities.md 3.4, "Site 11 is a SIMULATION of memory.read", critique C5).

027's anchor gate (`apps/meshsem/search.py` `_anchor_body_withheld`, exported as `body_withheld`)
and 033's preview (`apps/wsgw/mesh_notify.py` `body_withheld`, `preview_gate`) show a body only
as memory.read would. Since slice 6 memory.read takes a token, and a reader whose ONLY authority
is a token can read a key the gate, which has no token, withholds. That is the fail-closed
direction, kept by decision (design 3.4: site 11 accepts no token in v1), and the docstrings and
divergences 027 and 033 now say "memory.read without a capability", or they state something
false.

  * The reader F has no grant on shared/mesh-chat and holds a read token covering the anchor's
    key. memory.read with the token shows the body (the control that the token is good), and
    the anchor gate and the preview gate withhold it, `namespace_permission`. Control for the
    gates: a reader G with a DB read grant is shown the body by both.
  * The docstrings of the gate functions say "memory.read without a capability", and no longer
    say "memory.read would" without it; so do divergences 027 and 033.

Run:  pytest tests/test_capability_site11_tokenless.py -q
"""
from __future__ import annotations

import inspect
import os
import re
import secrets
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

from tests.capability_invoke import invoke_context, outcome  # noqa: E402
from tests.capability_routes import admin_on  # noqa: E402

MESH = "shared/mesh-chat"
QUALIFIED = "memory.read without a capability"
DIVERGENCES = ROOT / "docs" / "DIVERGENCES.md"


def test_t21_a_reader_whose_only_authority_is_a_token_is_withheld_the_body_and_the_preview():
    from apps.core import rls
    from apps.meshsem import search
    from apps.meshsem.search import Reader
    from apps.wsgw import mesh_notify

    with invoke_context() as inv:
        issuer, writer, f, g = (inv.peer(t) for t in ("I", "W", "F", "G"))
        admin_on(inv.w, issuer, MESH)
        admin_on(inv.w, writer, MESH)
        inv.w.sql("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                  "grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                  "SELECT %s, %s, %s, 'read', owner_peer_id, owner_owner_id "
                  "FROM shared_memory_namespace WHERE namespace = %s",
                  (MESH, g.id, g.owner_id, MESH))
        label = f"peer-{writer.id[:8]}"
        key = f"msg/{int(time.time() * 1000)}-{label}-{secrets.token_hex(3)}"
        body = "T21 synthetic anchor body"
        wrote, _ = inv.call(writer, "memory.write", {
            "namespace": MESH, "key": key,
            "value": {"schema_version": 1, "to": ["all"], "from": label, "type": "status",
                      "thread_id": "cap6/t21", "ts": "2026-09-25T00:00:00Z", "body": body,
                      "author_family": "claude"}})
        assert outcome(wrote) == (200, "completed", None), wrote
        tok = inv.w.mint(issuer=issuer, subject=f, ns=MESH, rights=((key, "read"),),
                         depth_cap=0)

        read, _ = inv.call(f, "memory.read", {"namespace": MESH, "key": key}, token=tok)
        assert outcome(read) == (200, "completed", None), read
        assert read.body["result"]["memo"]["value"]["body"] == body

        def gates(peer):
            reader = Reader(peer_id=peer.id, possession=True, key=peer.public_bytes)
            with rls.server_internal(inv.w.app):
                return (search.body_withheld(inv.w.app, reader, key),
                        mesh_notify.body_withheld(inv.w.app, reader, key),
                        mesh_notify.preview_gate(inv.w.app, reader, key),
                        mesh_notify.preview_gates(inv.w.app, reader, [key])[key])

        assert gates(f) == (search.WITHHELD_NAMESPACE,) * 4, gates(f)
        assert gates(g) == (None,) * 4, "control: a reader by grant is shown the body"
        no_token, _ = inv.call(f, "memory.read", {"namespace": MESH, "key": key})
        assert outcome(no_token) == (200, "failed", "E_PERMISSION_DENIED"), (
            "the gate answers as memory.read WITHOUT a capability does", no_token)


def _gate_functions():
    from apps.meshsem import search
    from apps.wsgw import mesh_notify
    return {
        "search._anchor_body_withheld": search._anchor_body_withheld,
        "search.body_withheld_many": search.body_withheld_many,
        "search.context_for": search.context_for,
        "mesh_notify.body_withheld": mesh_notify.body_withheld,
        "mesh_notify.body_withheld_many": mesh_notify.body_withheld_many,
        "mesh_notify.preview_gate": mesh_notify.preview_gate,
        "mesh_notify.preview_gates": mesh_notify.preview_gates,
    }


def _unqualified(text: str) -> list[str]:
    """Every "memory.read would" that is not "memory.read without a capability would"."""
    flat = re.sub(r"\s+", " ", text)
    return re.findall(r"memory\.read would[^.]{0,40}", flat)


def test_t21_the_gates_docstrings_say_without_a_capability():
    """Each gate function's docstring states the rule as it now is. The absent phrase is paired
    with the present one, so a docstring corrected in place passes and one that only gained a
    sentence beside the old claim does not."""
    problems = {}
    for name, fn in _gate_functions().items():
        doc = inspect.getdoc(fn) or ""
        flat = re.sub(r"\s+", " ", doc)
        if QUALIFIED not in flat or _unqualified(doc):
            problems[name] = _unqualified(doc) or "no 'memory.read without a capability'"
    assert problems == {}, problems
    # The checker's controls: it flags the old wording and passes the new.
    assert _unqualified("None when memory.read would answer this reader") != []
    assert _unqualified("None when memory.read without a capability would answer") == []


def _section(number: str) -> str:
    text = DIVERGENCES.read_text(encoding="utf-8")
    start = text.index(f"\n## {number},")
    end = text.index("\n## ", start + 1)
    return text[start:end]


@pytest.mark.parametrize("number", ["027", "033"])
def test_t21_divergences_027_and_033_say_without_a_capability(number):
    """Design 3.4: "the docstrings and divergences 027 and 033 must then say 'exactly when
    memory.read without a capability would', or they state something false"."""
    flat = re.sub(r"\s+", " ", _section(number))
    assert QUALIFIED in flat, number
    assert "slice 6" in flat and "040" in flat, number


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
