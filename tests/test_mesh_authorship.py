"""Who may write a shared/mesh-chat message, and under what name. Divergence 028.

MEASURED ON THE LIVE TEST SERVER, 2026-09-23 (c5457ac, kxxx's nodes plus one real remote):

  1. CROSS-NODE OVERWRITE. Node P (peer 1baad537) invoked memory.write on the key of a
     message node Q (peer 4db41686) had written. The server accepted and signed it: version
     2 became the head with its body replaced; version 1 stayed immutable.
  2. FORGED AUTHOR. The same write passed args.author = "peer-4db41686" (Q's label) and
     version 2's author_label said so, while created_by_peer_id was P's.
   3. SPOOFED FROM AND KEY LABEL. A remote node used its
      `--from desktop-5e6e5b2b` option (another node's address): value.from AND the key's embedded
     label both carried the claimed name.

Only created_by_peer_id and the live frame's from_peer_id were bound to the authenticated
caller. Readers see author_label, value.from and the head, so they saw the forgery.

THE RULES, for namespace shared/mesh-chat and keys under msg/ ONLY. "The caller's labels"
is exactly `apps.mesh.views.labels_for`: roster label, peers.name, peer-<id8>.

  R1  a supplied args.author / args.author_label is one of the caller's labels
      (E_AUTHOR_MISMATCH); absent, the caller-derived default stands.
  R2  value.from is one of the caller's labels (E_FROM_MISMATCH).
  R3  the label embedded in the msg/ key is value.from (E_KEY_LABEL_MISMATCH).
  R4  a later version of an existing msg/ key is written only by the peer that created its
      version 1 (E_NOT_MESSAGE_AUTHOR), and nothing is written on refusal.
  R5  the database refuses the same thing (sql_meshychat/011), for a path that bypasses
      the handler.

Everything outside msg/ keeps the ordinary shared-namespace semantics: shared/wiki's CONFIRM
is a different peer writing a new version of someone else's key, and it is pinned here as a
control.

Every refusal sits beside the control that differs from it in the one variable under test
(docs/V1-DESIGN-RULES.md C1). All message text is synthetic.

Run:  .venv/Scripts/python.exe -m pytest tests/test_mesh_authorship.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import psycopg.errors
import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import rls  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import db, handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
MESH = "shared/mesh-chat"
WIKI = "shared/wiki"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def owner_conn():
    """Owner role, AUTOCOMMIT: seeds and observes. Bypasses RLS; never a request's role."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """The role a request runs as: meshychat_app under FORCE RLS (db.connect asserts it)."""
    conn = db.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def roster():
    """Load a fleet roster for one test and put the previous one back afterwards: the
    cache is process-global, and every other test in the run expects it empty."""
    before = dict(handlers._ROSTER_LABELS)

    def load(mapping: dict) -> None:
        handlers.prime_roster_labels(json.dumps(
            {"peers": {str(k): v for k, v in mapping.items()}}).encode("utf-8"))
    yield load
    handlers._ROSTER_LABELS = before


def _share_label(monkeypatch, label: str, *peers) -> None:
    """Two peers under ONE label: the state R4 exists for. Since divergence 066 a `peers.name` two
    peers hold is nobody's label, and the roster FILE refuses one label for two peers
    (apps/memoryskills/roster.py), so the label cache itself is the only place the state can still
    be made. It stands for what a roster reload leaves behind when the operator moves a label from
    one peer to another: the new holder sends as the label the old holder's messages carry."""
    monkeypatch.setattr(handlers, "_ROSTER_LABELS",
                        {**handlers._ROSTER_LABELS, **{str(x["peer_id"]): label for x in peers}})


@pytest.fixture()
def mesh_policy(owner_conn):
    """Set shared/mesh-chat's retention policy for ONE test and put the claim row back
    afterwards. The run's database holds a single mesh claim (keep_all, as the runbook
    claims it) that every other test writes under, and sql/063 freezes the column, so the
    flip and the restore each run with triggers off for their own UPDATE only."""
    saved = {}

    def flip(row_policy: str) -> None:
        with owner_conn.transaction():
            owner_conn.execute("SET LOCAL session_replication_role = replica")
            row = owner_conn.execute(
                "SELECT retention_policy, ephemeral FROM shared_memory_namespace "
                "WHERE namespace = %s", (MESH,)).fetchone()
            assert row is not None, "claim the namespace (_peers) before flipping it"
            saved.setdefault("row", row)
            owner_conn.execute("UPDATE shared_memory_namespace SET retention_policy = %s, "
                               "ephemeral = %s WHERE namespace = %s",
                               (row_policy, row_policy == "ephemeral", MESH))
    yield flip
    if "row" in saved:
        with owner_conn.transaction():
            owner_conn.execute("SET LOCAL session_replication_role = replica")
            owner_conn.execute("UPDATE shared_memory_namespace SET retention_policy = %s, "
                               "ephemeral = %s WHERE namespace = %s", (*saved["row"], MESH))


def _peer(conn, tag: str, *, name: str | None = None,
          same_owner_as: dict | None = None) -> dict:
    """A dev-plane owner of its own (or `same_owner_as`'s owner: a second node of one
    operator), a peer named `name` (a mesh label), an active key."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    name = name or f"au-{tag}-{peer_id.hex[:8]}"
    if same_owner_as is not None:
        owner_id = same_owner_as["owner_id"]
    else:
        conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                     "VALUES (%s,%s,'dev',%s)",
                     (owner_id, f"au-{tag}-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
    conn.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                 (peer_id, owner_id, name, f"au {tag}"))
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(pub)))
    return {"owner_id": owner_id, "peer_id": peer_id, "seed": seed, "kid": kid,
            "name": name, "fallback": f"peer-{str(peer_id)[:8]}"}


def _admit(conn, namespace: str, peer: dict, permission: str = "write") -> None:
    """Access for `peer`: the claim when nobody holds the namespace yet, else a grant
    (`write`, or `admin` for a custody write or a prune) from whoever does. Seeded as the
    database owner, as tests/test_board_declare.py does: the run's database is shared, and
    the holder may be a peer whose seed this process never had."""
    row = conn.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace "
                       "WHERE namespace = %s", (namespace,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                     "owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                     "VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                     (namespace, peer["peer_id"], peer["owner_id"]))
    elif str(row[0]) != str(peer["peer_id"]):
        conn.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                     "grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                     "VALUES (%s,%s,%s,%s,%s,%s)",
                     (namespace, peer["peer_id"], peer["owner_id"], permission, row[0], row[1]))


def _peers(conn, *tags, namespace=MESH, **kw) -> list[dict]:
    out = [_peer(conn, t, **kw) for t in tags]
    for p in out:
        _admit(conn, namespace, p)
    return out


def _key(label: str, tail: str | None = "hex") -> str:
    """The message-key grammar is `msg/<epoch-ms>-<sender>-<6 hex>`
    (tools/meshy_node/wire.py mesh_key). tail=None gives the legacy `msg/<ms>-<sender>`."""
    ms = int(time.time() * 1000) + secrets.randbelow(1000)
    if tail is None:
        return f"msg/{ms}-{label}"
    return f"msg/{ms}-{label}-{secrets.token_hex(3) if tail == 'hex' else tail}"


def _value(sender: str, body: str = "synthetic authorship probe") -> dict:
    return {"schema_version": 1, "to": ["all"], "from": sender, "type": "status",
            "thread_id": "authorship/probe", "ts": "2026-09-23T00:00:00Z", "body": body,
            "author_family": "claude"}


def _envelope(peer: dict, skill: str, args: dict) -> dict:
    payload = {"v": 1, "kind": "skill.call", "call_id": f"call-{uuid.uuid4()}",
               "caller_peer_id": str(peer["peer_id"]), "target_peer_id": str(peer["peer_id"]),
               "skill_name": skill, "skill_version": "1", "args": args,
               "args_attachments": [],
               "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    return {"payload": payload, "signature": P.b64u(P.sign(peer["seed"], P.jcs(payload))),
            "signing_kid": peer["kid"]}


def _write(conn, peer: dict, args: dict) -> dict:
    return handlers.dispatch("memory.write", _envelope(peer, "memory.write", args),
                             peer["peer_id"], conn)


def _refused(conn, peer: dict, args: dict) -> HandlerError:
    with pytest.raises(HandlerError) as exc:
        _write(conn, peer, args)
    return exc.value


def _rows(conn, namespace: str, key: str) -> list[tuple]:
    """(version, is_head, created_by_peer_id, author_label, from, body), oldest first."""
    return [(r[0], r[1], str(r[2]), r[3], r[4], r[5]) for r in conn.execute(
        "SELECT version, is_head, created_by_peer_id, author_label, value->>'from', "
        "value->>'body' FROM shared_memory WHERE namespace = %s AND key = %s "
        "ORDER BY version", (namespace, key)).fetchall()]


def _msg(namespace: str, key: str, value: dict, *, author: str | None = None,
         author_label: str | None = None, **extra) -> dict:
    args = {"namespace": namespace, "key": key, "value": value, **extra}
    if author is not None:
        args["author"] = author
    if author_label is not None:
        args["author_label"] = author_label
    return args


# --------------------------------------------------------------------------- #
# R1: a supplied author is one of the caller's labels
# --------------------------------------------------------------------------- #

def test_r1_an_author_naming_another_peer_is_refused_and_nothing_is_written(app_conn,
                                                                             owner_conn):
    """Measurement 2: args.author named the victim's label and author_label said so."""
    p, q = _peers(owner_conn, "r1p", "r1q")
    key = _key(p["name"])
    err = _refused(app_conn, p, _msg(MESH, key, _value(p["name"]), author=q["fallback"]))
    assert err.code == "E_AUTHOR_MISMATCH", err
    assert q["fallback"] in err.message and p["name"] in err.message, err.message
    assert _rows(owner_conn, MESH, key) == []
    # Control, one variable apart: the caller's own label as author.
    _write(app_conn, p, _msg(MESH, key, _value(p["name"]), author=p["name"]))
    assert _rows(owner_conn, MESH, key) == [
        (1, True, str(p["peer_id"]), p["name"], p["name"], "synthetic authorship probe")]


def test_r1_the_author_label_spelling_is_checked_as_well(app_conn, owner_conn):
    """handle_write reads `author` OR `author_label`; each supplied spelling is checked, so
    the alias is not a way round the rule, and a valid `author` does not launder a forged
    `author_label` beside it."""
    p, q = _peers(owner_conn, "r1ap", "r1aq")
    key = _key(p["name"])
    err = _refused(app_conn, p, _msg(MESH, key, _value(p["name"]), author_label=q["name"]))
    assert err.code == "E_AUTHOR_MISMATCH" and q["name"] in err.message, err
    err = _refused(app_conn, p, _msg(MESH, key, _value(p["name"]), author=p["name"],
                                     author_label=q["name"]))
    assert err.code == "E_AUTHOR_MISMATCH", err
    assert _rows(owner_conn, MESH, key) == []
    _write(app_conn, p, _msg(MESH, key, _value(p["name"]), author_label=p["fallback"]))
    assert [r[3] for r in _rows(owner_conn, MESH, key)] == [p["fallback"]]


def test_r1_with_no_author_the_caller_derived_default_stands(app_conn, owner_conn):
    (p,) = _peers(owner_conn, "r1d")
    key = _key(p["name"])
    _write(app_conn, p, _msg(MESH, key, _value(p["name"])))
    # No roster is loaded in this run, so the default is the peer-id prefix
    # (apps/memoryskills/handlers.py, "attribution, never invention").
    assert [r[3] for r in _rows(owner_conn, MESH, key)] == [p["fallback"]]


# --------------------------------------------------------------------------- #
# R2: value.from is one of the caller's labels
# --------------------------------------------------------------------------- #

def test_r2_a_from_naming_another_node_is_refused_the_live_spoof(app_conn, owner_conn):
    """Measurement 3, exactly: `--from <another node's address>`, with the key embedding
    the claimed label (so R3 alone would pass it) and no author (so R1 does not fire)."""
    p, q = _peers(owner_conn, "r2p", "r2q")
    key = _key(q["name"])
    err = _refused(app_conn, p, _msg(MESH, key, _value(q["name"])))
    assert err.code == "E_FROM_MISMATCH", err
    assert q["name"] in err.message and p["name"] in err.message, err.message
    assert _rows(owner_conn, MESH, key) == []
    # Control: the caller's own address, in the value and the key.
    own = _key(p["name"])
    _write(app_conn, p, _msg(MESH, own, _value(p["name"])))
    assert [(r[2], r[4]) for r in _rows(owner_conn, MESH, own)] == [(str(p["peer_id"]),
                                                                      p["name"])]


def test_r2_every_label_the_graph_routes_answer_for_is_accepted(app_conn, owner_conn, roster):
    """The set is labels_for's: roster label, peers.name, peer-<id8>. Each is accepted as
    `from` (and as author), and another peer's ROSTER label is not."""
    p, q = _peers(owner_conn, "r2lp", "r2lq")
    seat_p, seat_q = f"seat-{p['peer_id'].hex[:6]}", f"seat-{q['peer_id'].hex[:6]}"
    roster({p["peer_id"]: seat_p, q["peer_id"]: seat_q})
    for label in (seat_p, p["name"], p["fallback"]):
        key = _key(label)
        _write(app_conn, p, _msg(MESH, key, _value(label), author=label))
        assert [(r[3], r[4]) for r in _rows(owner_conn, MESH, key)] == [(label, label)]
    key = _key(seat_q)
    err = _refused(app_conn, p, _msg(MESH, key, _value(seat_q)))
    assert err.code == "E_FROM_MISMATCH", err
    assert _rows(owner_conn, MESH, key) == []


def test_control_a_legacy_node_whose_roster_label_is_its_from_and_author(app_conn, owner_conn,
                                                                        roster):
    """WITH A ROSTER LOADED: fleet clients pass --author <roster label> and send from
    the same label, with the legacy key shape `msg/<ms>-<label>` (no random tail). Accepted.
    The served app loads a roster only when MESHY_FLEET_ROSTER_PATH is set (divergence
    034, whose startup tests/test_fleet_roster_startup.py runs); the state without one is
    pinned by test_without_a_roster_the_label_set_is_peers_name_and_the_fallback."""
    (p,) = _peers(owner_conn, "legacy")
    seat = f"legacy-seat-{p['peer_id'].hex[:6]}"
    roster({p["peer_id"]: seat})
    key = _key(seat, tail=None)
    _write(app_conn, p, _msg(MESH, key, _value(seat), author=seat, status="OPEN",
                             tags=["mesh-chat"]))
    assert [(r[3], r[4]) for r in _rows(owner_conn, MESH, key)] == [(seat, seat)]


# --------------------------------------------------------------------------- #
# R3: the key's embedded label is value.from
# --------------------------------------------------------------------------- #

def test_r3_the_key_must_embed_the_sender_it_claims(app_conn, owner_conn):
    """Grammar: `msg/<epoch-ms>-<from>` optionally followed by `-<tail>`. No such equality
    check existed before this rule: the pre-change tree accepts every refusal below."""
    p, q = _peers(owner_conn, "r3p", "r3q")
    refusals = {
        "another node's label": _key(q["name"]),
        "no label at all": f"msg/{int(time.time() * 1000)}",
        "a label that only shares a prefix": _key(p["name"] + "x"),
        # The sender is the segment right after the epoch, not any segment: a key that
        # names another node first and the caller second still reads as the other node's.
        "the caller's label after another's": _key(f"{q['name']}-{p['name']}"),
    }
    for why, key in refusals.items():
        err = _refused(app_conn, p, _msg(MESH, key, _value(p["name"])))
        assert err.code == "E_KEY_LABEL_MISMATCH", (why, err)
        assert p["name"] in err.message, (why, err.message)
        assert _rows(owner_conn, MESH, key) == [], why
    # Controls: the same value under the two shapes the grammar admits.
    for key in (_key(p["name"]), _key(p["name"], tail=None)):
        _write(app_conn, p, _msg(MESH, key, _value(p["name"])))
        assert len(_rows(owner_conn, MESH, key)) == 1, key


# --------------------------------------------------------------------------- #
# R4: one writer per message
# --------------------------------------------------------------------------- #

def test_r4_only_the_messages_author_writes_a_later_version(app_conn, owner_conn, monkeypatch):
    """Measurement 1. Isolated from R1-R3: P legitimately sends as the SAME label Q used, in
    the value, the key and the author (`_share_label` says how that state still arises since
    divergence 066). Only authorship of version 1 separates them."""
    shared = f"r4-op-{secrets.token_hex(4)}"
    q = _peer(owner_conn, "r4q")
    p = _peer(owner_conn, "r4p")
    _share_label(monkeypatch, shared, q, p)
    for x in (q, p):
        _admit(owner_conn, MESH, x)
    key = _key(shared)
    _write(app_conn, q, _msg(MESH, key, _value(shared, "original synthetic text")))

    err = _refused(app_conn, p, _msg(MESH, key, _value(shared, "TAMPERED synthetic text"),
                                     author=shared))
    assert err.code == "E_NOT_MESSAGE_AUTHOR", err
    assert str(q["peer_id"]) in err.message and key in err.message, err.message
    assert _rows(owner_conn, MESH, key) == [
        (1, True, str(q["peer_id"]), shared, shared, "original synthetic text")]

    # Divergence 059: the author's own later version through memory.write is CLOSED too. It was an unguarded edit path (nothing
    # froze to or cc on it, and no seat was told); every later version of a mail key is memory.edit's now
    # (tests/test_edit.py). The refusal names the door, and the chain is unchanged.
    err = _refused(app_conn, q, _msg(MESH, key, _value(shared, "edited synthetic text")))
    assert err.code == "E_USE_MEMORY_EDIT" and "memory.edit" in err.message, err
    assert _rows(owner_conn, MESH, key) == [
        (1, True, str(q["peer_id"]), shared, shared, "original synthetic text")]
    # ...and the other peer is still refused, with 028's own words.
    err = _refused(app_conn, p, _msg(MESH, key, _value(shared, "TAMPERED again")))
    assert err.code == "E_NOT_MESSAGE_AUTHOR", err
    assert len(_rows(owner_conn, MESH, key)) == 1


def test_control_non_message_keys_in_the_mesh_namespace_keep_shared_semantics(app_conn,
                                                                            owner_conn):
    """The rules are for msg/ keys. Any writer can append a version to a coordination key in
    the same namespace, such as a cursor or lease."""
    q, p = _peers(owner_conn, "nkq", "nkp")
    key = f"cursor/authorship-{secrets.token_hex(4)}"
    _write(app_conn, q, _msg(MESH, key, {"cursor": 1}))
    _write(app_conn, p, _msg(MESH, key, {"cursor": 2}, author="any-free-label"))
    assert [(r[0], r[2], r[3]) for r in _rows(owner_conn, MESH, key)] == [
        (1, str(q["peer_id"]), q["fallback"]), (2, str(p["peer_id"]), "any-free-label")]


def test_control_a_wiki_confirm_by_a_different_peer_still_writes_a_new_version(app_conn,
                                                                             owner_conn):
    """shared/wiki's CONFIRM lets a confirmer write a NEW VERSION of another peer's entry,
    including a free-form author label."""
    a, b = _peers(owner_conn, "wka", "wkb", namespace=WIKI)
    key = f"authorship-confirm-{secrets.token_hex(4)}"
    _write(app_conn, a, _msg(WIKI, key, {"title": "synthetic entry", "body": "synthetic"},
                             tags=["test"]))
    out = _write(app_conn, b, _msg(WIKI, key, {"title": "synthetic entry", "body": "synthetic",
                                               "confirmed_by": [b["name"]]},
                                   status="CONFIRMED", tags=["test"], author="a-free-seat"))
    assert out["version"] == 2
    assert [(r[0], r[1], r[2], r[3]) for r in _rows(owner_conn, WIKI, key)] == [
        (1, False, str(a["peer_id"]), a["fallback"]),
        (2, True, str(b["peer_id"]), "a-free-seat")]


# --------------------------------------------------------------------------- #
# R5: the database holds R4 for a path that bypasses the handler
# --------------------------------------------------------------------------- #

def _direct_version(conn, head: dict, *, peer: dict, body: str) -> None:
    """Retire `head` and insert the next version, as raw SQL on a meshychat_app connection in
    the server_internal context the handler itself uses: what a future code path that
    skipped handle_write would do. Since divergence 039 such a path must also carry the
    signed bytes its columns derive from (sql_meshychat/014 refuses NULL from meshychat_app), so
    the row is built from a payload naming `peer` as its signer, which is the identity
    011's single-writer rule then compares."""
    value = dict(head["value"], body=body)
    p = sr.payload(peer["peer_id"], head["namespace"], head["key"], value)
    cols = sr.columns(p, peer_id=peer["peer_id"], owner_id=peer["owner_id"],
                      version=head["version"] + 1, parent_id=head["id"],
                      author_label=peer["fallback"])
    with rls.server_internal(conn), conn.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (head["id"],))
        sr.insert(conn, cols)


def _head(conn, namespace: str, key: str) -> dict:
    r = conn.execute("SELECT id, namespace, key, version, value FROM shared_memory "
                     "WHERE namespace = %s AND key = %s AND is_head", (namespace, key)).fetchone()
    return {"id": r[0], "namespace": r[1], "key": r[2], "version": r[3], "value": r[4]}


def test_r5_the_database_refuses_a_second_writer_on_a_mesh_message(app_conn, owner_conn):
    q, p = _peers(owner_conn, "r5q", "r5p")
    key = _key(q["name"])
    _write(app_conn, q, _msg(MESH, key, _value(q["name"], "original synthetic text")))

    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR"):
        _direct_version(app_conn, _head(owner_conn, MESH, key), peer=p, body="TAMPERED")
    assert [(r[0], r[1], r[2]) for r in _rows(owner_conn, MESH, key)] == [
        (1, True, str(q["peer_id"]))], "the refusal must roll back the head retirement too"

    # Divergence 059: the author's own later version is refused by the database too, without the edit path's mark, with the
    # refusal that names memory.edit. (The edit path's own insert, marked, is tests/test_edit_db_rule.py's control.)
    with pytest.raises(psycopg.errors.CheckViolation, match="E_USE_MEMORY_EDIT"):
        _direct_version(app_conn, _head(owner_conn, MESH, key), peer=q, body="edited")
    assert [(r[0], r[1], r[2]) for r in _rows(owner_conn, MESH, key)] == [
        (1, True, str(q["peer_id"]))], "the refusal must roll back the head retirement too"


def test_r5_is_scoped_to_mesh_messages(app_conn, owner_conn):
    """The trigger's WHEN clause leaves shared/wiki and non-msg/ keys multi-writer at the
    database, too."""
    q, p = _peers(owner_conn, "r5sq", "r5sp")
    for x in (q, p):
        _admit(owner_conn, WIKI, x)
    wiki_key = f"authorship-direct-{secrets.token_hex(4)}"
    _write(app_conn, q, _msg(WIKI, wiki_key, {"title": "synthetic", "body": "v1"},
                             tags=["test"]))
    _direct_version(app_conn, _head(owner_conn, WIKI, wiki_key), peer=p, body="v2")
    cursor_key = f"lease/authorship-{secrets.token_hex(4)}"
    _write(app_conn, q, _msg(MESH, cursor_key, {"holder": "q", "body": "v1"}))
    _direct_version(app_conn, _head(owner_conn, MESH, cursor_key), peer=p, body="v2")
    for ns, key in ((WIKI, wiki_key), (MESH, cursor_key)):
        assert [r[2] for r in _rows(owner_conn, ns, key)] == [str(q["peer_id"]),
                                                              str(p["peer_id"])], key


def test_r5_fires_on_insert_only_so_no_existing_row_is_ever_rejected(owner_conn):
    """A BEFORE INSERT row trigger cannot reject a row that already exists, and cannot
    touch the head retirement (an UPDATE) the immutability trigger governs. Read off
    pg_trigger rather than assumed; the two existing triggers are still bound."""
    rows = dict(owner_conn.execute(
        "SELECT tgname, tgtype FROM pg_trigger WHERE tgrelid = 'shared_memory'::regclass "
        "AND NOT tgisinternal").fetchall())
    tgtype = rows.get("shared_memory_mesh_single_writer_trg")
    assert tgtype is not None, sorted(rows)
    # pg_trigger.tgtype bits: 1 ROW, 2 BEFORE, 4 INSERT, 8 DELETE, 16 UPDATE, 32 TRUNCATE.
    assert tgtype & 1 and tgtype & 2 and tgtype & 4, tgtype
    assert not tgtype & (8 | 16 | 32), tgtype
    assert {"shared_memory_immutability", "shared_memory_root_parent_trg"} <= set(rows)


def _seed_two_writer_chain(conn, key: str, *, author: dict, tamperer: dict) -> None:
    """Measurement 1's shape, as a row written before 011 is: `author` wrote version 1 of
    its own message, `tamperer` the version-2 head. Triggers are off for these two inserts
    only (the state a pre-migration row is in)."""
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        for rid, ver, parent, head, who, body in ((v1, 1, None, False, author, "original"),
                                                  (v2, 2, v1, True, tamperer, "TAMPERED")):
            conn.execute(
                "INSERT INTO shared_memory (id, namespace, key, version, is_head, parent_id, "
                " value, author_label, provenance_jsonb, payload_jcs_sha256, "
                " created_by_peer_id, created_by_owner_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (rid, MESH, key, ver, head, parent, Jsonb(_value(author["name"], body)),
                 author["fallback"], Jsonb({"payload": {"call_id": f"hist-{uuid.uuid4()}"}}),
                 secrets.token_hex(32), who["peer_id"], who["owner_id"]))


def test_r5_historical_multi_writer_rows_are_not_rewritten_and_the_root_author_decides(
        app_conn, owner_conn):
    """A chain written BEFORE this change can already carry a second writer (measurement
    1 left one on the live server). It is not rewritten and nothing refuses it; the
    author of version 1 can still write the next version, and the peer that wrote the
    historical head cannot. The seed disables triggers for its own two inserts only, which
    is exactly the state a pre-migration row is in."""
    q, p = _peers(owner_conn, "histq", "histp")
    key = _key(q["name"])
    _seed_two_writer_chain(owner_conn, key, author=q, tamperer=p)
    before = _rows(owner_conn, MESH, key)
    assert [r[2] for r in before] == [str(q["peer_id"]), str(p["peer_id"])]

    # Through the handler p cannot even address this key under q's label (R2 and R3), so
    # the second writer is tried at the database, where p's peer id is the only variable.
    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR"):
        _direct_version(app_conn, _head(owner_conn, MESH, key), peer=p, body="third")
    # Divergence 059: the root author's own v3 is memory.edit's now (its owner wrote version 1, so it may edit); memory.write
    # refuses it with the door named, and the historical chain is not touched by either refusal.
    err = _refused(app_conn, q, _msg(MESH, key, _value(q["name"], "the author's own v3")))
    assert err.code == "E_USE_MEMORY_EDIT", err
    assert _rows(owner_conn, MESH, key) == before


# --------------------------------------------------------------------------- #
# Review findings on 7084e88 (two reviews, 2026-09-23). Each test here either failed on
# that head or kills a mutant the whole suite let live there; the docstring says which.
# --------------------------------------------------------------------------- #

def test_r4_holds_when_a_roster_reload_moves_a_label_to_another_peer(app_conn, owner_conn, roster):
    """The state `_share_label` stands for, made only with states the roster loader accepts (memory-bxxxxx's
    review of #48): the roster names Q, Q writes, a reload gives the SAME label to P and no longer to Q. P
    now legitimately sends as the label Q's message carries, in value, key and author, so R1 to R3 pass
    for it, and R4 is what refuses its rewrite. Control: P's own new message under the label lands."""
    label = f"r4-move-{secrets.token_hex(4)}"
    q = _peer(owner_conn, "r4mq")
    p = _peer(owner_conn, "r4mp")
    for x in (q, p):
        _admit(owner_conn, MESH, x)
    roster({q["peer_id"]: label})
    key = _key(label)
    _write(app_conn, q, _msg(MESH, key, _value(label, "original synthetic text")))

    roster({p["peer_id"]: label})                                    # the reload: the label moved
    err = _refused(app_conn, p, _msg(MESH, key, _value(label, "TAMPERED synthetic text"), author=label))
    assert err.code == "E_NOT_MESSAGE_AUTHOR", err
    assert [(r[0], r[2], r[5]) for r in _rows(owner_conn, MESH, key)] == [
        (1, str(q["peer_id"]), "original synthetic text")]
    own = _key(label)
    _write(app_conn, p, _msg(MESH, own, _value(label, "P's own synthetic text"), author=label))   # control
    assert [(r[0], r[2]) for r in _rows(owner_conn, MESH, own)] == [(1, str(p["peer_id"]))]


def test_r4_a_second_node_of_the_same_operator_is_a_different_writer(app_conn, owner_conn, monkeypatch):
    """R4 and R5 bind the PEER, not the owner: one operator's two nodes (one owner, and
    here one label, `_share_label`) are two writers. Every other R4/R5 test gives each peer its own
    owner, so a handler rule or a trigger that compared OWNERS passed them all (review
    mutants M1b and M2 survived the full suite)."""
    shared = f"r4o-op-{secrets.token_hex(4)}"
    q = _peer(owner_conn, "r4oq")
    p = _peer(owner_conn, "r4op", same_owner_as=q)
    _share_label(monkeypatch, shared, q, p)
    for x in (q, p):
        _admit(owner_conn, MESH, x)
    key = _key(shared)
    _write(app_conn, q, _msg(MESH, key, _value(shared, "original synthetic text")))

    err = _refused(app_conn, p, _msg(MESH, key, _value(shared, "TAMPERED synthetic text")))
    assert err.code == "E_NOT_MESSAGE_AUTHOR", err
    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR"):
        _direct_version(app_conn, _head(owner_conn, MESH, key), peer=p, body="TAMPERED")
    assert [(r[0], r[2], r[5]) for r in _rows(owner_conn, MESH, key)] == [
        (1, str(q["peer_id"]), "original synthetic text")]
    # Divergence 059: the author's own later version is memory.edit's too, so memory.write refuses it with the door named.
    err = _refused(app_conn, q, _msg(MESH, key, _value(shared, "edited synthetic text")))
    assert err.code == "E_USE_MEMORY_EDIT", err
    assert [r[2] for r in _rows(owner_conn, MESH, key)] == [str(q["peer_id"])]


def test_r4_guards_the_ephemeral_rewrite_the_database_cannot_see(app_conn, owner_conn,
                                                                 mesh_policy, monkeypatch):
    """Under `ephemeral` a write DELETEs the key's chain and inserts a fresh version 1,
    which the R5 trigger (version > 1 only) never sees, so R4 in the handler is that
    path's only guard. Review mutant M4 skipped R4 there and the suite stayed green."""
    shared = f"eph-op-{secrets.token_hex(4)}"
    q = _peer(owner_conn, "ephq")
    p = _peer(owner_conn, "ephp")
    _share_label(monkeypatch, shared, q, p)
    for x in (q, p):
        _admit(owner_conn, MESH, x)
    mesh_policy("ephemeral")
    key = _key(shared)
    _write(app_conn, q, _msg(MESH, key, _value(shared, "original synthetic text")))

    err = _refused(app_conn, p, _msg(MESH, key, _value(shared, "TAMPERED synthetic text")))
    assert err.code == "E_NOT_MESSAGE_AUTHOR", err
    assert [(r[0], r[1], r[2], r[5]) for r in _rows(owner_conn, MESH, key)] == [
        (1, True, str(q["peer_id"]), "original synthetic text")]
    # Divergence 059: the AUTHOR's own rewrite is refused too. Under `ephemeral` it deleted the
    # chain and stored a fresh version 1, silently changing what recipients had read and erasing
    # history; every later state of a mail key is memory.edit's now, with its checks, event and
    # kept history.
    err = _refused(app_conn, q, _msg(MESH, key, _value(shared, "rewritten synthetic text")))
    assert err.code == "E_USE_MEMORY_EDIT", err
    assert [(r[0], r[1], r[2], r[5]) for r in _rows(owner_conn, MESH, key)] == [
        (1, True, str(q["peer_id"]), "original synthetic text")]


def test_r5_refuses_a_later_version_whose_key_has_no_earlier_version(app_conn, owner_conn):
    """A version 2 whose parent is ANOTHER key's row passes the root-parent trigger (it only
    requires a parent) and the FK (the row exists). The single-writer trigger then finds
    no earlier version of THIS key and refuses rather than waving the row through. Review
    mutant M5 failed open here and the suite stayed green. Same peer throughout, so the
    missing chain is the only variable; the author's own chained version 2 is the control
    in the R5 refusal test."""
    (q,) = _peers(owner_conn, "r5nq")
    other = _key(q["name"])
    _write(app_conn, q, _msg(MESH, other, _value(q["name"])))
    orphan = _key(q["name"])
    with pytest.raises(psycopg.errors.CheckViolation, match="no earlier version"):
        _direct_version(app_conn, dict(_head(owner_conn, MESH, other), key=orphan), peer=q,
                        body="orphan version 2")
    assert _rows(owner_conn, MESH, orphan) == []
    assert [r[1] for r in _rows(owner_conn, MESH, other)] == [True], "retirement rolled back"


def test_a_custody_write_is_bound_to_its_signer_like_any_other(app_conn, owner_conn):
    """A pre_discipline custody write needs admin and is still a mesh message: an admin
    cannot backfill a row under another node's name (review finding 8, a lockout 028 now
    states). Review mutant HZ exempted custody writes from R1/R2 and the suite stayed
    green. Control: the admin's backfill under its own label lands."""
    q = _peer(owner_conn, "cuq")
    _admit(owner_conn, MESH, q)
    a = _peer(owner_conn, "cua")
    _admit(owner_conn, MESH, a, permission="admin")
    key = _key(q["name"])
    err = _refused(app_conn, a, _msg(MESH, key, _value(q["name"], "historical synthetic"),
                                     pre_discipline=True))
    assert err.code == "E_FROM_MISMATCH", err
    err = _refused(app_conn, a, _msg(MESH, key, _value(q["name"], "historical synthetic"),
                                     author=q["name"], pre_discipline=True))
    assert err.code == "E_AUTHOR_MISMATCH", err
    assert _rows(owner_conn, MESH, key) == []
    own = _key(a["name"])
    _write(app_conn, a, _msg(MESH, own, _value(a["name"], "historical synthetic"),
                             pre_discipline=True))
    assert [(r[2], r[4]) for r in _rows(owner_conn, MESH, own)] == [(str(a["peer_id"]),
                                                                      a["name"])]


def test_a_high_churn_prune_keeps_a_messages_root_so_its_author_stays_its_author(
        app_conn, owner_conn, mesh_policy):
    """Review findings A5-1 and 5. R4 and R5 read a message's author off its chain's
    LOWEST surviving version, and memory.prune deleted every non-head row, version 1
    included. On a chain written before 011 with a second writer (measurement 1's shape)
    a prune handed the message to the tamperer and locked its author out. The prune now
    keeps a mesh message's lowest version; later non-head versions still go, and a
    non-message key in the same namespace prunes exactly as before (the control)."""
    q = _peer(owner_conn, "prq")
    _admit(owner_conn, MESH, q, permission="admin")
    p = _peer(owner_conn, "prp")
    _admit(owner_conn, MESH, p)
    mesh_policy("high_churn")
    key = _key(q["name"])
    _seed_two_writer_chain(owner_conn, key, author=q, tamperer=p)
    ctl = f"cursor/prune-{secrets.token_hex(4)}"
    for n in (1, 2):
        _write(app_conn, q, _msg(MESH, ctl, {"cursor": n}))

    def prune(k):
        return handlers.dispatch("memory.prune",
                                 _envelope(q, "memory.prune", {"namespace": MESH, "key": k}),
                                 q["peer_id"], app_conn)["removed"]

    assert prune(key) == []
    assert prune(ctl) == [{"namespace": MESH, "key": ctl, "version": 1}]
    assert [(r[0], r[2]) for r in _rows(owner_conn, MESH, key)] == [
        (1, str(q["peer_id"])), (2, str(p["peer_id"]))]

    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR"):
        _direct_version(app_conn, _head(owner_conn, MESH, key), peer=p, body="tamperer's v3")
    # Divergence 059: the author's v3 is an EDIT now (its owner wrote version 1), by memory.edit, on the head as it stands.
    head = _head(owner_conn, MESH, key)
    handlers.dispatch("memory.edit", _envelope(q, "memory.edit", {
        "namespace": MESH, "key": key, "value": dict(head["value"], body="the author's own v3"),
        "expected_head_version": 2}), q["peer_id"], app_conn)
    # The prune still compacts a message: the tamperer's non-head v2 goes, the root stays.
    assert prune(key) == [{"namespace": MESH, "key": key, "version": 2}]
    assert [(r[0], r[1], r[2]) for r in _rows(owner_conn, MESH, key)] == [
        (1, False, str(q["peer_id"])), (3, True, str(q["peer_id"]))]


def test_a_from_claim_on_a_msg_key_is_bound_in_every_namespace_the_wait_route_reads(
        app_conn, owner_conn):
    """Review finding 1. GET /v1/mesh/wait reports `value->>'from'` for the head `msg/` rows
    of EVERY namespace the caller can read, so a `from` forged in shared/wiki was served as
    the sender while the same write to shared/mesh-chat was refused. A `msg/` row that CLAIMS
    a sender (a non-null `from`) is now bound in any namespace, R2 and R3, for the same reason
    validate_mesh_key_ts is namespace-agnostic: the wait route serves `msg/` rows from every
    readable namespace. A `msg/` row that claims no sender remains an unrestricted control
    (any key, any writer)."""
    p, q = _peers(owner_conn, "xnp", "xnq", namespace=WIKI)
    entry = {"title": "synthetic", "body": "synthetic authorship probe"}
    spoof = _key(q["name"])
    err = _refused(app_conn, p, _msg(WIKI, spoof, {**entry, "from": q["name"]}, tags=["test"]))
    assert err.code == "E_FROM_MISMATCH", err
    mislabeled = _key(q["name"])
    err = _refused(app_conn, p, _msg(WIKI, mislabeled, {**entry, "from": p["name"]},
                                     tags=["test"]))
    assert err.code == "E_KEY_LABEL_MISMATCH", err
    assert _rows(owner_conn, WIKI, spoof) == [] and _rows(owner_conn, WIKI, mislabeled) == []

    own = _key(p["name"])
    _write(app_conn, p, _msg(WIKI, own, {**entry, "from": p["name"]}, tags=["test"]))
    assert [r[4] for r in _rows(owner_conn, WIKI, own)] == [p["name"]]
    bare = _key(q["name"])
    _write(app_conn, p, _msg(WIKI, bare, entry, tags=["test"]))
    _write(app_conn, q, _msg(WIKI, bare, {**entry, "body": "v2"}, tags=["test"]))
    assert [r[2] for r in _rows(owner_conn, WIKI, bare)] == [str(p["peer_id"]),
                                                             str(q["peer_id"])]


def test_a_label_shaped_like_another_peers_fallback_is_not_the_callers(app_conn, owner_conn,
                                                                       roster):
    """Review findings A1-1 and 6. `peer-<id8>` is the label this server itself stamps for
    a peer (handle_write's default author), so it names exactly one peer. A peers.name or
    roster label of that shape carrying ANOTHER peer's id prefix passed R1 to R3 with the
    exact string forged in measurement 2 (probe b3). Such a label is not the caller's.
    Controls: the peer that label names, and a peer NAMED its own fallback."""
    q = _peer(owner_conn, "fbq")
    p = _peer(owner_conn, "fbp", name=q["fallback"])
    r = _peer(owner_conn, "fbr")
    s = _peer(owner_conn, "fbs")
    owner_conn.execute("UPDATE peers SET name = %s WHERE id = %s", (s["fallback"], s["peer_id"]))
    for x in (q, p, r, s):
        _admit(owner_conn, MESH, x)
    roster({r["peer_id"]: q["fallback"]})
    key = _key(q["fallback"])
    err = _refused(app_conn, p, _msg(MESH, key, _value(q["fallback"]), author=q["fallback"]))
    assert err.code == "E_AUTHOR_MISMATCH", err
    for who in (p, r):
        err = _refused(app_conn, who, _msg(MESH, key, _value(q["fallback"])))
        assert err.code == "E_FROM_MISMATCH", (who["name"], err)
    assert _rows(owner_conn, MESH, key) == []

    _write(app_conn, q, _msg(MESH, key, _value(q["fallback"]), author=q["fallback"]))
    assert [(row[2], row[3]) for row in _rows(owner_conn, MESH, key)] == [
        (str(q["peer_id"]), q["fallback"])]
    own = _key(s["fallback"])
    _write(app_conn, s, _msg(MESH, own, _value(s["fallback"]), author=s["fallback"]))
    assert len(_rows(owner_conn, MESH, own)) == 1


def test_without_a_roster_the_label_set_is_peers_name_and_the_fallback(app_conn, owner_conn):
    """Review findings B2-1 and 3. With MESHY_FLEET_ROSTER_PATH unset the served app loads
    no roster (divergence 034; before it, never: 018 (b)), so there the caller's labels are
    peers.name and peer-<id8> ONLY. Pinned in that state, not under a roster the test
    loads itself: a fleet seat label that is not the node's peers.name is refused as author
    and as `from`; a sender's default author `<from>-cc` is refused; and a node
    whose peers.name IS its seat label sends on the legacy key with --author equal to it."""
    assert handlers._ROSTER_LABELS == {}, "this pins the served state: no roster loaded"
    seat = f"seat-{secrets.token_hex(3)}"
    (p,) = _peers(owner_conn, "noroster")
    key = _key(seat, tail=None)
    err = _refused(app_conn, p, _msg(MESH, key, _value(seat), author=seat))
    assert err.code == "E_AUTHOR_MISMATCH", err
    err = _refused(app_conn, p, _msg(MESH, key, _value(seat)))
    assert err.code == "E_FROM_MISMATCH", err
    mine = _key(p["name"], tail=None)
    err = _refused(app_conn, p, _msg(MESH, mine, _value(p["name"]), author=f"{p['name']}-cc"))
    assert err.code == "E_AUTHOR_MISMATCH", err
    assert _rows(owner_conn, MESH, key) == [] and _rows(owner_conn, MESH, mine) == []
    (n,) = _peers(owner_conn, "seat", name=seat)
    _write(app_conn, n, _msg(MESH, key, _value(seat), author=seat, status="OPEN",
                             tags=["mesh-chat"]))
    assert [(r[3], r[4]) for r in _rows(owner_conn, MESH, key)] == [(seat, seat)]


# --------------------------------------------------------------------------- #
# End to end, through the served app
# --------------------------------------------------------------------------- #

def test_the_command_line_client_sends_and_its_spoofed_from_is_refused_typed(tmp_path,
                                                                              monkeypatch):
    """The live measurement 3 path, literally: `python -m tools.meshy_node send --from
    <another node's label>` against the served application. Its normal send lands; the
    spoof is a typed refusal the client reports, and nothing is written."""
    from apps.authapi import ratelimit
    from tests.fixtures.meshsem_fake import FakeModelHost
    from tests.test_fresh_install_runbook import (Runbook, _empty_database, _jwt_pair, _ok,
                                                  _read, _serve)

    ratelimit.reset_all()
    fake = FakeModelHost(dims=768)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        with _empty_database("authorship") as dsn:
            rb = Runbook(f"http://127.0.0.1:{sock.getsockname()[1]}")
            code, out = rb.migrate(dsn)
            assert code == 0, out
            jwt_priv, jwt_pub = _jwt_pair(tmp_path)
            group = str(uuid.uuid4())
            op, op_pub = tmp_path / "operator.json", tmp_path / "operator.pub"
            code, _ = rb.node("keygen", op, "--public-key-out", op_pub)
            assert code == 0
            monkeypatch.setenv("MESHY_DSN", dsn)
            from django.test import override_settings
            with override_settings(MESHY_DSN=dsn):
                out, _ = rb.manage("bootstrap_owner", "--email", "ops@example.test",
                                   "--public-key-file", str(op_pub), "--plane", "dev",
                                   "--trust-group", group)
                owner = json.loads(out)
                rb.manage("bootstrap_memory_peer", "--trust-group", group,
                          "--owner-email", "memory@example.test")
            env = {"MESHY_DSN": dsn, "MESHYCHAT_JWT_ISSUER": "meshychat-authorship-test",
                   "MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": str(jwt_priv),
                   "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": str(jwt_pub),
                   "MESHYCHAT_STATE_SECRET": os.urandom(32).hex(),
                   "MESHY_ALLOWED_HOSTS": "127.0.0.1",
                   "MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID": owner["peer_id"],
                   "MESHY_EMBED_FORBIDDEN_ROWS": "true",
                   "MESHY_MODEL_PROVIDERS": json.dumps([
                       {"id": "embed-fake", "kind": "embed", "base_url": fake.base_url,
                        "model": "embeddinggemma-300m",
                        "space": "embeddinggemma-300m/768/retrieval-v1"}])}
            with _serve(env, monkeypatch, sock):
                code, _ = rb.node("bind", op, "--peer-id", owner["peer_id"])
                assert code == 0
                code, claimed = rb.node("claim", op, "--namespace", MESH)
                assert code == 0 and _ok(claimed), claimed
                code, paired = rb.node("pair", op, "--count", "2")
                assert code == 0
                states, peer = {"A": tmp_path / "a.json", "B": tmp_path / "b.json"}, {}
                for (name, path), row in zip(states.items(), paired["codes"]):
                    # `--code=`: a pairing code is base64url and can begin with '-',
                    # which argparse would read as an option (measured: a full-suite run
                    # drew one).
                    code, minted = rb.node("mint", path, f"--code={row['code']}",
                                           "--device-label", f"authorship-{name.lower()}")
                    assert code == 0, minted
                    peer[name] = minted["peer_id"]
                    code, granted = rb.node("grant", op, "--namespace", MESH, "--grantee",
                                            minted["peer_id"], "--permission", "write")
                    assert code == 0 and _ok(granted), granted
                labels = {}
                for name in ("A", "B"):
                    code, inbox = rb.node("inbox", states[name])
                    assert code == 0, inbox
                    labels[name] = inbox["label"]

                common = ["--to", labels["B"], "--thread", "authorship/e2e", "--type",
                          "status", "--author-family", "claude"]
                code, sent = rb.node("send", states["A"], *common, "--body",
                                     "synthetic normal send")
                assert code == 0 and _ok(sent), sent
                assert sent["key"].startswith("msg/") and f"-{labels['A']}-" in sent["key"]

                code, spoof = rb.node("send", states["A"], *common, "--from", labels["B"],
                                      "--body", "synthetic spoofed send")
                assert code == 1, spoof
                assert spoof["error_code"] == "E_FROM_MISMATCH", spoof
                assert labels["B"] in spoof["error_message"], spoof

            rows = _read(dsn, "SELECT value->>'from', author_label, created_by_peer_id::text "
                              "FROM shared_memory WHERE namespace = %s AND key LIKE 'msg/%%'",
                         (MESH,))
            assert rows == [(labels["A"], f"peer-{peer['A'][:8]}", peer["A"])], rows
    finally:
        sock.close()
        fake.close()
        ratelimit.reset_all()
