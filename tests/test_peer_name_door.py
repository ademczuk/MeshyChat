"""Divergence 065: a peer's name is a mail address, so a new peer may not take one already in use.

Each property has a case and a control beside it; a mutant that would reopen the hole is noted where
it is killed. The review that found the hole (#42, F1) proved it by creating a second peer under
ANOTHER owner with a seat's name; that exact shape is the first test.

Run:  .venv/Scripts/python.exe -m pytest tests/test_peer_name_door.py -q -p no:cacheprovider
"""
from __future__ import annotations

import threading
import time
import types
import uuid

import psycopg
import pytest

from tests.test_onboard import (  # noqa: F401 - fixtures are used by name; sets up Django
    DSN,
    _fresh_limiters,
    csrf_for,
    outcome,
    owner_conn,
    post,
    redeem,
    server,
    signed_in_owner,
)
from apps.authapi import onboard  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402
from apps.peers import names  # noqa: E402

PEERS = "/v1/peers"
PAIRING = "/v1/dashboard/pairing/generate"
CODE = "E_PEER_NAME_UNAVAILABLE"


def _fresh(tag: str) -> str:
    return f"door-{tag}-{uuid.uuid4().hex[:8]}"


def _create(client, name: str):
    return post(client, PEERS, {"name": name}, HTTP_X_CSRF_TOKEN=csrf_for(client))


def _count(conn, folded_like: str) -> int:
    return conn.execute("SELECT count(*) FROM peers WHERE lower(name) = lower(%s)",
                        (folded_like,)).fetchone()[0]


def _usage(conn, owner_id) -> int:
    row = conn.execute("SELECT coalesce(sum(counter), 0) FROM owner_usage WHERE owner_id = %s "
                       "AND capability = 'peers_per_owner'", (owner_id,)).fetchone()
    return row[0]


def test_another_owners_peer_cannot_take_a_name_that_is_in_use(server, owner_conn):
    """The F1 shape: owner B names a peer exactly as owner A's peer is named. Refused 409, nothing
    written (no peer, no quota unit), and the message names no owner. Control: a fresh name for B.
    (Mutant: drop the `names.claim` call in POST /v1/peers: this fails.)"""
    a_signer, a = signed_in_owner(owner_conn, "door-a")
    b_signer, b = signed_in_owner(owner_conn, "door-b")
    name = _fresh("seat")
    assert _create(a, name).status_code == 201
    used = _usage(owner_conn, b_signer["owner_id"])
    r = _create(b, name)
    assert outcome(r) == (409, CODE), r.content
    msg = r.json()["message"]
    assert str(a_signer["owner_id"]) not in msg and "owner" not in msg.lower(), msg
    assert _count(owner_conn, name) == 1, "the refused create wrote a peer"
    assert _usage(owner_conn, b_signer["owner_id"]) == used, "the refused create took a quota unit"
    assert _create(b, _fresh("other")).status_code == 201                       # control


def test_the_comparison_is_case_and_compatibility_folded(server, owner_conn):
    """`Kxxx` and `kxxx` are one address to a reader; so is the full-width form. Control: a name
    that differs by a real character is free."""
    _, a = signed_in_owner(owner_conn, "door-fold-a")
    _, b = signed_in_owner(owner_conn, "door-fold-b")
    name = _fresh("fold")
    assert _create(a, name).status_code == 201
    fullwidth = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in name.upper())
    for variant in (name.upper(), name.capitalize(), fullwidth):
        assert outcome(_create(b, variant)) == (409, CODE), variant
    assert _create(b, name + "x").status_code == 201                            # control


@pytest.mark.parametrize("status", ["deleted", "suspended"])
def test_a_deleted_or_suspended_peers_name_is_never_reused(server, owner_conn, status):
    """Stored mail carries labels as strings, so a reused name would inherit another peer's history.
    Control: the same owner can still create a fresh name."""
    signer, owner = signed_in_owner(owner_conn, f"door-{status}")
    name = _fresh(status)
    owner_conn.execute("INSERT INTO peers (id, owner_id, name, status) VALUES (%s, %s, %s, %s)",
                       (uuid.uuid4(), signer["owner_id"], name, status))
    assert outcome(_create(owner, name)) == (409, CODE)
    assert _create(owner, _fresh("live")).status_code == 201                     # control


def test_a_roster_label_is_refused_in_any_case(server, owner_conn, monkeypatch):
    """A roster label names a seat even when no peer row holds it as a name. Control: the same name
    is free once the roster does not hold it. (Mutant: drop the roster check: this fails.)"""
    _, owner = signed_in_owner(owner_conn, "door-roster")
    label = _fresh("roster")
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(uuid.uuid4()): label})
    assert outcome(_create(owner, label.upper())) == (409, CODE)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    assert _create(owner, label.upper()).status_code == 201                     # control


def test_a_stored_name_or_roster_label_with_outer_whitespace_still_contests(server, owner_conn,
                                                                           monkeypatch):
    """ASDLC OI-71 (a). Both creators strip the NEW name, but a stored `peers.name` or a roster label
    with outer whitespace is the same address once a recipient resolves it (`uncontested` strips), so
    the door compares both sides stripped. No such row exists on production (0 of 1880, 2026-10-03);
    this keeps an old or hand-written one from slipping past. Control: a different name is free.
    (Mutant: drop the `.strip()` on the stored side or on the roster side: this fails.)"""
    signer, owner = signed_in_owner(owner_conn, "door-ws")
    stored = _fresh("ws")
    owner_conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s, %s, %s)",
                       (uuid.uuid4(), signer["owner_id"], f"  {stored}\t"))
    assert outcome(_create(owner, stored.upper())) == (409, CODE)
    label = _fresh("wsroster")
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(uuid.uuid4()): f" {label} "})
    assert outcome(_create(owner, label)) == (409, CODE)
    assert _create(owner, _fresh("wsfree")).status_code == 201                  # control


def test_the_door_strips_the_new_name_too(server, owner_conn):
    """The third side of OI-71 (a) (uxxx's review of #72): both creators strip the new name before calling
    the door, so only a direct call shows the door strips it itself. A padded new name against a stored
    bare one is refused, and its lock key is the stripped canonical form. Control: a free padded name passes.
    (Mutant: drop the `.strip()` on the new name in names.claim: this fails.)"""
    signer, _ = signed_in_owner(owner_conn, "door-new-ws")
    stored = _fresh("newws")
    owner_conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s, %s, %s)",
                       (uuid.uuid4(), signer["owner_id"], stored))
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        with pytest.raises(names.NameUnavailable):
            names.claim(cur, f"  {stored.upper()}\t")
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        names.claim(cur, f" {_fresh('newwsfree')} ")                               # control


@pytest.mark.parametrize("name", ["peer-1234abcd", "desktop-1234abcd", "mentee-123456789abc",
                                  "meshy-mem-daemon", "all", "BOTH", "Desktop-ABCDEF12"])
def test_a_name_the_server_generates_or_a_broadcast_address_is_reserved(server, owner_conn, name):
    """A user may not pre-claim a name the server draws later, nor a broadcast address."""
    _, owner = signed_in_owner(owner_conn, f"door-shape-{uuid.uuid4().hex[:6]}")
    assert outcome(_create(owner, name)) == (409, CODE), name


def test_near_misses_of_the_reserved_shapes_are_free(server, owner_conn):
    """Control for the test above: the shapes are exact (length and hex), not prefixes."""
    _, owner = signed_in_owner(owner_conn, "door-near")
    tail = uuid.uuid4().hex
    for name in (f"peer-{tail[:7]}", f"desktop-{tail[:9]}", f"mentee-{tail[:11]}",
                 f"desktop-{tail[:7]}g", f"all-{tail[:4]}"):
        assert _create(owner, name).status_code == 201, name


@pytest.mark.parametrize("spelling", ["same", "upper"])
def test_a_concurrent_create_of_the_same_name_waits_and_is_refused(server, owner_conn, spelling):
    """Two creates of one name: the second waits on the first's lock, then sees its committed row.
    Control: when the first ROLLS BACK instead, the waiting create gets the name. `upper`: the second
    create spells it differently, so only a lock keyed on the CANONICAL name makes it wait (kxxx's
    review of #43, F1).
    (Mutants: drop the advisory lock: both spellings fail. Key the lock on the raw name: `upper` fails,
    the two creates take different locks and both commit.)"""
    signer, owner = signed_in_owner(owner_conn, f"door-race-{spelling}")
    other = (lambda n: n) if spelling == "same" else str.upper

    def race(finish: str):
        name = _fresh("race")
        result = {}
        with psycopg.connect(DSN) as held:                    # a transaction, not autocommit
            with held.cursor() as cur:
                names.claim(cur, name)
                cur.execute("INSERT INTO peers (owner_id, name) VALUES (%s, %s)",
                            (signer["owner_id"], name))
            t = threading.Thread(target=lambda: result.update(r=_create(owner, other(name))))
            t.start()
            time.sleep(1.5)
            assert t.is_alive(), "the second create did not wait for the first one's lock"
            getattr(held, finish)()
        t.join(30)
        assert not t.is_alive()
        return result["r"], name

    refused, name = race("commit")
    assert outcome(refused) == (409, CODE), refused.content
    assert _count(owner_conn, name) == 1
    allowed, name = race("rollback")                                          # control
    assert allowed.status_code == 201, allowed.content
    assert _count(owner_conn, name) == 1


def test_bootstrap_owner_refuses_a_taken_peer_name_and_writes_nothing(owner_conn):
    """The admin path: a clash is a BootstrapRefused naming --peer-name, with no owner written.
    Control: a fresh --peer-name creates the owner. (Mutant: drop the claim in bootstrap: this fails.)"""
    import os

    from apps.authapi import bootstrap
    from apps.crypto import primitives as P

    taken = _fresh("boot")
    owner_conn.execute("INSERT INTO dashboard_users (id, email) VALUES (%s, %s)",
                       (uuid.uuid4(), f"{taken}@example.test"))
    holder = owner_conn.execute("SELECT id FROM dashboard_users WHERE email = %s",
                                (f"{taken}@example.test",)).fetchone()[0]
    owner_conn.execute("INSERT INTO peers (owner_id, name) VALUES (%s, %s)", (holder, taken))

    def run(peer_name, email):
        with psycopg.connect(DSN) as conn:
            return bootstrap.bootstrap_owner(conn, email=email, peer_name=peer_name,
                                             additional_owner=True,
                                             public_key_raw=P.public_key_of(os.urandom(32)))

    email = f"{_fresh('boot-owner')}@example.test"
    with pytest.raises(bootstrap.BootstrapRefused, match="--peer-name"):
        run(taken.upper(), email)
    assert owner_conn.execute("SELECT count(*) FROM dashboard_users WHERE email = %s",
                              (email,)).fetchone()[0] == 0, "the refusal wrote an owner"
    assert run(_fresh("boot-fresh"), email) is not None                     # control


def test_draw_takes_the_next_name_when_one_is_taken_and_gives_up_after_eight(owner_conn):
    """The server-generated paths: a clash is drawn again, never shared; a generator that only ever
    clashes fails the create rather than looping."""
    signer_owner = uuid.uuid4()
    owner_conn.execute("INSERT INTO dashboard_users (id, email) VALUES (%s, %s)",
                       (signer_owner, f"draw-{signer_owner.hex[:8]}@example.test"))
    taken = f"desktop-{uuid.uuid4().hex[:8]}"
    owner_conn.execute("INSERT INTO peers (owner_id, name) VALUES (%s, %s)", (signer_owner, taken))
    fresh = f"desktop-{uuid.uuid4().hex[:8]}"
    seq = iter([taken, fresh])
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        assert names.draw(cur, lambda: next(seq)) == fresh
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        with pytest.raises(names.NameUnavailable):
            names.draw(cur, lambda: taken)


def test_onboarding_draws_its_desktop_name_through_the_door(server, owner_conn, monkeypatch):
    """A node redeeming a pairing code gets a desktop-<8 hex> name; if the first draw is already some
    peer's, it gets the next one instead of a duplicate. (Mutant: onboarding inserts its first draw
    without `draw`: the new peer shares the taken name, and this fails.)"""
    signer, owner = signed_in_owner(owner_conn, "door-onboard")
    clash = uuid.uuid4()
    taken = f"desktop-{clash.hex[:8]}"
    owner_conn.execute("INSERT INTO peers (owner_id, name) VALUES (%s, %s)",
                       (signer["owner_id"], taken))
    real = uuid.uuid4
    scripted = []                                   # the kid's draw, then the name's first draw

    def uuid4():
        return scripted.pop(0) if scripted else real()

    shim = types.SimpleNamespace(**{k: getattr(uuid, k) for k in dir(uuid) if not k.startswith("__")})
    shim.uuid4 = uuid4
    monkeypatch.setattr(onboard, "uuid", shim)
    code = post(owner, PAIRING, HTTP_X_CSRF_TOKEN=csrf_for(owner)).json()["code"]
    scripted[:] = [real(), clash]
    r, _ = redeem(code)
    assert r.status_code == 201, r.content
    got = owner_conn.execute("SELECT name FROM peers WHERE id = %s", (r.json()["peer_id"],)).fetchone()[0]
    assert got != taken and got.startswith("desktop-") and len(got) == len(taken), got
    assert not scripted, "the scripted draws were not the ones onboarding used"
    assert _count(owner_conn, taken) == 1


def test_invitation_redemption_draws_its_mentee_name_through_the_door(server, owner_conn, monkeypatch):
    """Invitation redemption names the new peer `mentee-<first 12 hex of the new owner id>`; if that
    name is already some peer's, the owner id is drawn again rather than the name shared (codex-kxxx-cli's
    review of #43: the onboarding redraw had a test, this path did not).
    (Mutant: redemption inserts its first draw without `draw`: the new peer shares the taken name, and
    this fails.)"""
    from apps.authapi import views as authviews
    from tests.test_owner_routes import _invitation, _redeem_invite
    from tests.test_onboard import seed_signer

    mentor = seed_signer(owner_conn, "door-invite-mentor")
    clash = uuid.uuid4()
    taken = f"mentee-{clash.hex[:12]}"
    owner_conn.execute("INSERT INTO peers (owner_id, name) VALUES (%s, %s)",
                       (mentor["owner_id"], taken))
    real = uuid.uuid4
    scripted = []

    def uuid4():
        return scripted.pop(0) if scripted else real()

    shim = types.SimpleNamespace(**{k: getattr(uuid, k) for k in dir(uuid) if not k.startswith("__")})
    shim.uuid4 = uuid4
    monkeypatch.setattr(authviews, "uuid", shim)
    code = _invitation(owner_conn, mentor["peer_id"])
    scripted[:] = [clash]
    r = _redeem_invite(code)
    assert r.status_code == 201, r.content
    body = r.json()
    assert not scripted, "the scripted draw was not the one redemption used"
    assert body["owner_id"] != str(clash), "redemption kept the owner id whose name was taken"
    got = owner_conn.execute("SELECT name FROM peers WHERE id = %s", (body["peer_id"],)).fetchone()[0]
    assert got == f"mentee-{uuid.UUID(body['owner_id']).hex[:12]}" and got != taken, got
    assert _count(owner_conn, taken) == 1
