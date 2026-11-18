"""B02 phase 0, differential: what memory.write STORES is what the client DERIVES.

Review of fin/b02-phase0 at 0b08f4e, finding 1. Four one-line regressions in
`wire.derive_status_fields` (list tags deduplicated; list tags stripped; `author_label` read
before `author`; a comma string's parts stripped of spaces only, where norm_tags uses
Python's str.strip) each read an HONEST write as E_STATUS_CONTRADICTS_SIGNATURE, and the
whole client suite passed on every one of them: the pure tests restate the rules, and the
real-server tests sign ["ops", "lease"] and one author. The only oracle for "these are
memory.write's rules" is memory.write.

So each shape below is WRITTEN through the real handler (`handlers.dispatch` memory.write:
a genuinely signed envelope, the migrated schema, RLS on), READ back through memory.read
with include_provenance, and CHECKED as `read` checks it: `wire.author_signature_ok` under
the writer's key (the positive control that the comparison runs on a verified payload),
then `wire.status_check` with that verdict. Every shape the handler accepts must read
`status_verified: true`. One variable away, the same memo with its tags edited reads false,
naming tags: a status_check that always answered true would pass the first half alone.

Two shapes phase 0 listed as honest, a number in `claimed_status` and in `author`, are no
longer writable: B02 A1 (divergence 039, item 5, strict write-seam types) refuses them
E_INVALID_ARGS, and REFUSED_SHAPES holds that refusal instead.

It also covers what the build report left to the pure function: the shared/wiki rules
(the OPEN default, and a custody write's null status) on rows the handler stored.

Synthetic data only, in the run's own database (conftest).

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_status_differential.py -q -p no:cacheprovider
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from tests.test_memory_skills import (  # noqa: E402
    _call, _claim, _ns, _peer, app_conn, owner_conn, wiki_ns,  # noqa: F401
)
from tools.meshy_node import wire  # noqa: E402

VALUE = {"body": "synthetic: B02 phase 0 differential"}

#: id, the args signed beside namespace, key and value, whether the stored author label
#: is one the writer signed, and fields the stored row must hold (so the fixture is known
#: to be the shape it names, not one memory.write rewrote into an easier one).
SHAPES = [
    ("plain", {}, False, {"status": None, "tags": [], "pre_discipline": False}),
    ("explicit-nulls", {"status": None, "tags": None, "pre_discipline": None,
                        "claimed_status": None, "author": None}, False,
     {"status": None, "tags": [], "pre_discipline": False, "claimed_status": None}),
    ("status-signed", {"status": "OPEN"}, False, {"status": "OPEN"}),
    ("pre-false-signed", {"pre_discipline": False}, False, {"pre_discipline": False}),
    ("custody", {"pre_discipline": True}, False,
     {"pre_discipline": True, "status": None, "claimed_status": None}),
    ("custody-claimed", {"pre_discipline": True, "claimed_status": "OPEN"}, False,
     {"pre_discipline": True, "status": None, "claimed_status": "OPEN"}),
    ("claimed-off-custody", {"claimed_status": "OPEN"}, False,
     {"pre_discipline": False, "claimed_status": None}),
    ("tags-list", {"tags": ["ops", "lease"]}, False, {"tags": ["ops", "lease"]}),
    ("tags-list-duplicates", {"tags": ["ops", "ops"]}, False, {"tags": ["ops", "ops"]}),
    ("tags-list-padded", {"tags": [" ops ", "lease"]}, False, {"tags": [" ops ", "lease"]}),
    ("tags-empty-list", {"tags": []}, False, {"tags": []}),
    ("tags-comma", {"tags": " ops, lease ,,x"}, False, {"tags": ["ops", "lease", "x"]}),
    ("tags-comma-tab", {"tags": "ops,\tlease"}, False, {"tags": ["ops", "lease"]}),
    ("tags-comma-nbsp", {"tags": "ops,\u00a0lease"}, False, {"tags": ["ops", "lease"]}),
    ("tags-empty-string", {"tags": ""}, False, {"tags": []}),
    ("author", {"author": "desktop-a"}, True, {"author_label": "desktop-a"}),
    ("author-label-arg", {"author_label": "desktop-a"}, True, {"author_label": "desktop-a"}),
    ("author-and-label", {"author": "desktop-a", "author_label": "desktop-b"}, True,
     {"author_label": "desktop-a"}),
    ("author-empty-falls-back", {"author": "", "author_label": "desktop-a"}, True,
     {"author_label": "desktop-a"}),
]

#: Shapes phase 0 listed above as honest writes the handler stored, which B02 A1 (divergence
#: 039, item 5, the operator's Q3 "strict write-seam types") made unwritable: a number where
#: memory.write reads a string. `signed_bytes.check_arg_types` refuses each E_INVALID_ARGS
#: before anything is stored, so there is no row for `status_verified` to be computed on.
#: (id, the offending signed args, the field the refusal names, the same write made honest.)
REFUSED_SHAPES = [
    ("claimed-off-custody-number", {"claimed_status": 5}, "claimed_status",
     {"claimed_status": "OPEN"}),
    ("author-zero", {"author": 0}, "author", {"author": "desktop-a"}),
]

#: The same, in shared/wiki, whose rules key on the literal namespace.
WIKI_SHAPES = [
    ("wiki-default-open", {"tags": ["ops"]}, False, {"status": "OPEN"}),
    ("wiki-null-status", {"status": None, "tags": "ops, lease"}, False,
     {"status": "OPEN", "tags": ["ops", "lease"]}),
    ("wiki-status-signed", {"status": "OPEN", "tags": ["ops"], "author": "desktop-a"}, True,
     {"status": "OPEN", "author_label": "desktop-a"}),
    ("wiki-custody", {"pre_discipline": True, "claimed_status": "CONFIRMED"}, False,
     {"pre_discipline": True, "status": None, "claimed_status": "CONFIRMED", "tags": []}),
]


def _jwks(peer: dict):
    """The writer's published keys, as GET /agents/{peer}/jwks.json answers them."""
    jwk = {"kty": "OKP", "crv": "Ed25519", "kid": peer["kid"], "x": P.b64u(peer["pub"])}
    return lambda _peer_id: ({"keys": [jwk]}, None)


def _named(error: str) -> list[str]:
    """The fields a status_error names, in order: "<CODE>: <field>: <why>; <field>: ..."."""
    return re.findall(r"(?:^E_[A-Z_]+: |; )([a-z_]+): ", error)


def _write_read_check(app_conn, peer: dict, ns: str, signed: dict, author_signed: bool,
                      stored: dict) -> None:
    key = f"note/phase0-diff-{uuid.uuid4().hex[:12]}"
    written = _call(app_conn, peer, "memory.write",
                    {"namespace": ns, "key": key, "value": VALUE, **signed})
    assert written["op"] == "memory.write", written
    memo = _call(app_conn, peer, "memory.read",
                 {"namespace": ns, "key": key, "include_provenance": True})["memo"]
    row = {field: memo.get(field) for field in wire.STATUS_FIELDS}
    assert {field: memo[field] for field in stored} == stored, ("the shape stored", row)

    verified, why = wire.author_signature_ok(memo, _jwks(peer))
    assert (verified, why) == (True, None), ("control: the writer's signature", why)
    got = wire.status_check(memo, verified, why)
    assert got == {"status_verified": True, "author_signed": author_signed}, (row, got)

    edited = {**memo, "tags": [*memo["tags"], "zz-nobody-signed-this"]}
    got = wire.status_check(edited, verified, why)
    assert got["status_verified"] is False and _named(got["status_error"]) == ["tags"], got


@pytest.mark.parametrize("signed,author_signed,stored", [row[1:] for row in SHAPES],
                         ids=[row[0] for row in SHAPES])
def test_every_shape_memory_write_stores_reads_status_verified(app_conn, owner_conn, signed,
                                                               author_signed, stored):
    """An honest write reads true whatever shape its args take, because the client's
    rules are the handler's. Kills (review of 0b08f4e): list tags deduplicated
    (tags-list-duplicates) or stripped (tags-list-padded); comma parts stripped of spaces
    only (tags-comma-tab, tags-comma-nbsp); `author_label` read before `author`
    (author-and-label); and any rule that drifts from handle_write on the other rows."""
    peer = _peer(owner_conn, f"p0d{uuid.uuid4().hex[:6]}")
    ns = _ns("p0diff")
    _claim(app_conn, peer, ns)
    _write_read_check(app_conn, peer, ns, signed, author_signed, stored)


@pytest.mark.parametrize("signed,field,honest", [row[1:] for row in REFUSED_SHAPES],
                         ids=[row[0] for row in REFUSED_SHAPES])
def test_a1_made_a_number_in_a_string_arg_unwritable(app_conn, owner_conn, signed, field,
                                                      honest):
    """A1 (divergence 039, Q3) made these two phase-0 shapes unwritable: memory.write
    refuses a JSON number in `claimed_status` or `author` with E_INVALID_ARGS, naming the
    arg, and no row lands under the key. POSITIVE CONTROL: the same key, written with a
    string in that arg, lands, and reads `status_verified: true`, so the refusal is the
    type and not the key, the namespace or the grant. Kills: `check_arg_types` accepting a
    number for either arg (the write then lands, as phase 0 recorded)."""
    peer = _peer(owner_conn, f"p0r{uuid.uuid4().hex[:6]}")
    ns = _ns("p0refused")
    _claim(app_conn, peer, ns)
    key = f"note/phase0-refused-{uuid.uuid4().hex[:12]}"
    with pytest.raises(HandlerError) as exc:
        _call(app_conn, peer, "memory.write",
              {"namespace": ns, "key": key, "value": VALUE, **signed})
    assert exc.value.code == "E_INVALID_ARGS", (exc.value.code, str(exc.value))
    assert exc.value.message.startswith(f"{field} must be a string"), str(exc.value)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND key = %s",
                    (ns, key))
        assert cur.fetchone()[0] == 0, "refused, and a row landed anyway"
    owner_conn.commit()

    written = _call(app_conn, peer, "memory.write",
                    {"namespace": ns, "key": key, "value": VALUE, **honest})
    assert written["op"] == "memory.write", written
    memo = _call(app_conn, peer, "memory.read",
                 {"namespace": ns, "key": key, "include_provenance": True})["memo"]
    verified, why = wire.author_signature_ok(memo, _jwks(peer))
    assert (verified, why) == (True, None), ("control: the writer's signature", why)
    assert wire.status_check(memo, verified, why)["status_verified"] is True


@pytest.mark.parametrize("signed,author_signed,stored", [row[1:] for row in WIKI_SHAPES],
                         ids=[row[0] for row in WIKI_SHAPES])
def test_the_shared_wiki_rules_hold_on_rows_the_handler_stored(app_conn, wiki_ns, signed,
                                                               author_signed, stored):
    """shared/wiki: no signed status stores OPEN (and a signed null is none), a custody
    write stores no status. Kills: the wiki default dropped, or kept on a custody write."""
    _write_read_check(app_conn, wiki_ns, "shared/wiki", signed, author_signed, stored)
