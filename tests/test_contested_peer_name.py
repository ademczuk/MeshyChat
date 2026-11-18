"""Divergence 066: a `peers.name` that more than one peer holds is NOBODY's label.

065's door stops a NEW peer from taking a name that is already an address. This is the other half: a
duplicate that already exists, or one written by a path that does not take the door (these tests write
it the way such a path would, a direct INSERT). `peers.name` is one of a peer's labels wherever labels
are read, so two holders of one name would each collect the other's mail and each write as it. The name
alone cannot say which is rightful, so neither is answered for it; the roster's own holder keeps a
label through the roster.

Each case has its control beside it: a name only ONE peer holds still works everywhere. The mutant that
would reopen each read site is noted where it is killed.

Run:  .venv/Scripts/python.exe -m pytest tests/test_contested_peer_name.py -q -p no:cacheprovider
"""
from __future__ import annotations

import uuid

import pytest

from tests.test_mesh_wait_addressed import (  # noqa: F401 - fixtures are used by name; sets up Django
    NS,
    _grant,
    _mesh_owner,
    _peer,
    _pushed_frame,
    _row,
    mesh_settings,
    no_roster,
    owner,
)
from apps.memoryskills import handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from apps.mesh import views as mesh_views  # noqa: E402
from apps.mesh import wait as mesh_wait  # noqa: E402
from apps.meshsum import window as sum_window  # noqa: E402
from apps.peers import names  # noqa: E402


def _named(conn, tag: str, name: str) -> dict:
    """A peer whose `peers.name` is exactly `name`, written as a path that skips the door would."""
    peer = _peer(conn, tag)
    conn.execute("UPDATE peers SET name = %s WHERE id = %s", (name, peer["peer_id"]))
    return {**peer, "name": name, "label": name}


def _fresh(tag: str) -> str:
    return f"c66-{tag}-{uuid.uuid4().hex[:8]}"


def _labels(conn, peer) -> list[str]:
    return [x["label"] for x in mesh_views.labels_for(conn, peer["peer_id"])]


def _fallback(peer) -> str:
    return f"peer-{str(peer['peer_id'])[:8]}"


# --------------------------------------------------------------------------- the rule itself

def test_a_name_two_peers_hold_is_given_to_neither_and_a_sole_holder_keeps_its_own(owner):
    dup, solo = _fresh("dup"), _fresh("solo")
    a, b, c = _named(owner, "a", dup), _named(owner, "b", dup), _named(owner, "c", solo)
    got = names.uncontested(owner, {a["peer_id"]: dup, b["peer_id"]: dup, c["peer_id"]: solo})
    assert got == {a["peer_id"]: None, b["peer_id"]: None, c["peer_id"]: solo}


def test_names_that_differ_only_by_case_or_compatibility_form_or_outer_space_are_one_name(owner):
    """The door's canonical form, so the two halves agree on what "the same name" is. Control: the
    exact name of a sole holder is returned unchanged, not canonicalised."""
    base = _fresh("Case")
    a, b = _named(owner, "a", base), _named(owner, "b", base.upper())
    wide = _fresh("wide")
    c, d = _named(owner, "c", wide), _named(owner, "d", wide.replace("w", "ｗ", 1))   # FULLWIDTH w
    spaced = _fresh("sp")
    e, f = _named(owner, "e", spaced), _named(owner, "f", spaced + " ")
    solo = _fresh("MiXed")
    g = _named(owner, "g", solo)
    got = names.uncontested(owner, {p["peer_id"]: p["name"] for p in (a, b, c, d, e, f, g)})
    assert [got[p["peer_id"]] for p in (a, b, c, d, e, f)] == [None] * 6
    assert got[g["peer_id"]] == solo


def test_a_deleted_peers_name_still_contests_it(owner):
    """As at the door: stored mail carries labels as strings, so a deleted holder's name is not free."""
    dup = _fresh("gone")
    a, b = _named(owner, "a", dup), _named(owner, "b", dup)
    owner.execute("UPDATE peers SET status = 'deleted' WHERE id = %s", (a["peer_id"],))
    assert names.uncontested(owner, {b["peer_id"]: dup}) == {b["peer_id"]: None}


def test_a_roster_label_of_another_peer_contests_a_name_and_its_own_holder_keeps_it(owner, monkeypatch):
    """The review's shape (#42, F1): a peer NAMED like a seat. The seat is the roster's; the squatter
    loses the name, the seat keeps the label through the roster. Control: with the roster empty the
    same peer, now the name's only holder, is answered for it."""
    seat_label = _fresh("seat")
    seat, squatter = _peer(owner, "seat"), _named(owner, "squat", seat_label)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(seat["peer_id"]): seat_label})
    assert names.uncontested(owner, {squatter["peer_id"]: seat_label}) == {squatter["peer_id"]: None}
    assert seat_label not in _labels(owner, squatter)
    assert _labels(owner, seat)[0] == seat_label                                    # primary, from the roster
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    assert seat_label in _labels(owner, squatter)                                    # control


def test_a_peer_named_as_its_own_roster_label_is_not_contested_by_itself(owner, monkeypatch):
    label = _fresh("own")
    peer = _named(owner, "own", label)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(peer["peer_id"]): label})
    assert names.uncontested(owner, {peer["peer_id"]: label}) == {peer["peer_id"]: label}


def test_blank_and_missing_names_stay_none(owner):
    a = _peer(owner, "blank")
    assert names.uncontested(owner, {a["peer_id"]: None, "x": "   ", "y": ""}) == {a["peer_id"]: None, "x": None, "y": None}


# --------------------------------------------------------------------------- every place a name is read as a label

def test_the_graph_routes_do_not_answer_a_contested_name(owner):
    """`labels_for` is what inbox, owed and context answer a caller for. The fallback stays.
    (Mutant: `_peer_name` returns the name without asking `uncontested`: this fails.)"""
    dup, solo = _fresh("dup"), _fresh("solo")
    a, b, c = _named(owner, "a", dup), _named(owner, "b", dup), _named(owner, "c", solo)
    for twin in (a, b):
        assert _labels(owner, twin) == [_fallback(twin)]
    assert _labels(owner, c) == [solo, _fallback(c)]                                 # control


def test_a_peer_cannot_write_as_a_contested_name(owner):
    """028's R2, the write side. The twin sends `value.from` = the shared name and is refused; it may
    still send as its own `peer-<id8>`. Control: the sole holder of a name sends as it."""
    dup, solo = _fresh("dup"), _fresh("solo")
    a, _b, c = _named(owner, "a", dup), _named(owner, "b", dup), _named(owner, "c", solo)
    with pytest.raises(HandlerError) as refused:
        handlers._enforce_mesh_sender(owner, {}, {"from": dup}, a["peer_id"], check_author=True)
    assert refused.value.code == "E_FROM_MISMATCH", refused.value
    with pytest.raises(HandlerError) as refused:
        handlers._enforce_mesh_sender(owner, {"author": dup}, {"from": _fallback(a)}, a["peer_id"], check_author=True)
    assert refused.value.code == "E_AUTHOR_MISMATCH", refused.value
    handlers._enforce_mesh_sender(owner, {}, {"from": _fallback(a)}, a["peer_id"], check_author=True)
    handlers._enforce_mesh_sender(owner, {"author": solo}, {"from": solo}, c["peer_id"], check_author=True)   # control


def test_the_addressed_wait_does_not_answer_a_contested_name(owner):
    """(Mutant: `caller_labels` passes the name straight to `peer_labels`: this fails.)"""
    dup, solo = _fresh("dup"), _fresh("solo")
    a, _b, c = _named(owner, "a", dup), _named(owner, "b", dup), _named(owner, "c", solo)
    assert mesh_wait.caller_labels(owner, a["peer_id"]) == frozenset({_fallback(a)})
    assert mesh_wait.caller_labels(owner, c["peer_id"]) == frozenset({solo.lower(), _fallback(c)})   # control


def test_the_graph_routes_and_the_addressed_wait_read_one_peer_name(owner, monkeypatch):
    """`labels_for` and `caller_labels` take `peers.name` from ONE read, `names.name_of`: as
    written for a sole holder, None for a twin, a blank name or an unknown peer. (Mutant:
    either site keeps its own SELECT: the patched name below does not reach it.)"""
    dup, solo = _fresh("dup"), _fresh("solo")
    a, _b, c = _named(owner, "a", dup), _named(owner, "b", dup), _named(owner, "c", solo)
    blank = _named(owner, "blank", "   ")
    assert names.name_of(owner, c["peer_id"]) == solo                                # control
    assert names.name_of(owner, a["peer_id"]) is None
    assert names.name_of(owner, blank["peer_id"]) is None
    assert names.name_of(owner, uuid.uuid4()) is None
    monkeypatch.setattr(names, "name_of", lambda conn, peer_id: "Planted-Name")
    assert _labels(owner, a) == ["Planted-Name", _fallback(a)]
    assert mesh_wait.caller_labels(owner, a["peer_id"]) == frozenset({"planted-name", _fallback(a)})


def test_the_push_sends_a_message_addressed_to_a_contested_name_to_neither_holder(owner):
    """End to end through the bridge's own functions. A message `to` the shared name reaches neither
    twin; the same author's message `to` a sole holder's name reaches that peer, and a twin is still
    reached by its own `peer-<id8>`. (Mutant: `_candidate_rows` returns the names as read: this fails.)"""
    dup, solo = _fresh("dup"), _fresh("solo")
    mesh = _mesh_owner(owner)
    author = _peer(owner, "author")
    a, b, c = _named(owner, "a", dup), _named(owner, "b", dup), _named(owner, "c", solo)
    for peer in (author, a, b, c):
        _grant(owner, NS, peer, mesh, permission="write")
    to_dup = _row(owner, author, {"from": author["label"], "to": [dup], "body": "for the shared name"})
    assert _pushed_frame(owner, to_dup, a["peer_id"]) is None
    assert _pushed_frame(owner, to_dup, b["peer_id"]) is None
    to_solo = _row(owner, author, {"from": author["label"], "to": [solo], "body": "for the sole holder"})
    frame = _pushed_frame(owner, to_solo, c["peer_id"])                              # control
    assert frame is not None and frame["key"] == to_solo["key"], frame
    to_fallback = _row(owner, author, {"from": author["label"], "to": [_fallback(a)], "body": "by id"})
    assert _pushed_frame(owner, to_fallback, a["peer_id"]) is not None


def test_a_summarys_author_aliases_leave_a_contested_name_out(owner):
    """(Mutant: `_names` returns the names as read: this fails.)"""
    dup, solo = _fresh("dup"), _fresh("solo")
    a, _b, c = _named(owner, "a", dup), _named(owner, "b", dup), _named(owner, "c", solo)
    got = sum_window._names(owner, [a["peer_id"], c["peer_id"]])
    assert got == {str(a["peer_id"]): None, str(c["peer_id"]): solo}
