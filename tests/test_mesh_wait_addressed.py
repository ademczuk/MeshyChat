"""`GET /v1/mesh/wait?addressed=1`: the long-poll answers with the push's own set (divergence 031).

WHY THIS EXISTS. `listen --monitor` (tools/meshy_node/monitor.py) catches up through
/v1/mesh/wait on every connect, then goes live on the socket. The two paths had different
recipient filters, measured here in source before the change:

  * the live frame (apps/wsgw/mesh_notify.py, divergence 020) goes to a peer only when the
    row is in shared/mesh-chat AND addresses it by `to` OR `cc` OR is a broadcast (`all`,
    `both`, no `to`), with the peer's server-known labels, lower-cased;
  * the long-poll (apps/mesh/wait.py, divergence 018 (c)) is ADDRESSING-AGNOSTIC: every
    head `msg/` row the caller did not write, in EVERY namespace it can read. Its body
    carries `to` but never `cc`, so a client cannot recompute the push's filter from it.

A monitor catching up through the plain route would emit every message on the mesh after a
re-arm and only its own while live. The union of the two sets is the plain route's set,
and the addressed set is what a seat is for, so the route gains ONE opt-in parameter that
applies the push's own predicate (`mesh_notify.addressing_of`, `peer_labels`,
`role_for`) and answers with the push's own frame. Nothing about the plain route
changes; the last test pins that.

Every negative sits beside its control (C1): the plain route over the same cursor returns
the rows the addressed one leaves out, a denied caller beside the same caller granted, a
parked wait not woken by a row for someone else beside the same wait woken by one for it.

Run:  pytest tests/test_mesh_wait_addressed.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

ISSUER = "meshy-mesh-wait-addressed-issuer"
_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption())
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.core import rls  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS  # noqa: E402
from apps.wsgw import mesh_notify  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

WAIT = importlib.import_module("apps.mesh.wait")
#: The push's frame fields include the recipient's `role`, `cc`, and the two truncation marks
#: (divergence 033).
FRAME_FIELDS = {"type", "key", "from_peer_id", "to", "created_at", "role", "cc",
                "to_truncated", "cc_truncated", "requires_ack"}


# --------------------------------------------------------------------------- harness
# The shapes of tests/test_mesh_graphread.py's helpers, copied so this file does not depend
# on another test module's fixtures (an imported autouse fixture would apply here too).

@pytest.fixture(autouse=True)
def mesh_settings():
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM, MESHY_JWT_ISSUER=ISSUER,
                           MESHY_DSN=DSN):
        yield


@pytest.fixture(autouse=True)
def no_roster():
    handlers.prime_roster_labels(b"{}")
    yield
    handlers.prime_roster_labels(b"{}")


@pytest.fixture()
def client():
    return Client()


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _auth(peer_id, ttl=300) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    token = pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + ttl,
                          "aud": "peer", "scopes": []}, JWT_PRIVATE_PEM, algorithm="EdDSA")
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    name = f"wa-{tag}-{peer_id.hex[:8]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"wa-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, name, f"wa {tag}"))
    return {"peer_id": peer_id, "owner_id": owner_id, "name": name, "label": name}


def _claim(conn, ns: str, peer) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                    " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                    " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                    (ns, peer["peer_id"], peer["owner_id"]))


def _mesh_owner(conn) -> dict:
    """Whoever owns shared/mesh-chat in this run's database, claiming it if nobody has."""
    _claim(conn, NS, _peer(conn, "claimant"))
    with conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace"
                    " WHERE namespace = %s", (NS,))
        peer_id, owner_id = cur.fetchone()
    return {"peer_id": peer_id, "owner_id": owner_id}


def _grant(conn, ns: str, grantee, granter, *, permission="read") -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id,"
                    " revoked) VALUES (%s,%s,%s,%s,%s,%s,FALSE)",
                    (ns, grantee["peer_id"], grantee["owner_id"], permission,
                     granter["peer_id"], granter["owner_id"]))


def _row(conn, peer, value: dict, *, ns: str = NS) -> dict:
    """A committed head msg/ row written as `peer`, created NOW (so it sorts after every
    row a sibling test left in the shared namespace)."""
    rid = uuid.uuid4()
    when = _dt.datetime.now(_dt.timezone.utc)
    key = f"msg/{int(when.timestamp() * 1000)}-{rid.hex}"
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without).
    sr.insert(conn, sr.row(peer["peer_id"], peer["owner_id"], ns, key, value, rid=rid,
                           call_id=f"call-{rid}", author_label=peer["label"], created_at=when))
    created = conn.execute("SELECT created_at FROM shared_memory WHERE id = %s",
                           (rid,)).fetchone()[0]
    return {"key": key, "id": rid, "created_at": created, "author": peer["peer_id"]}


def _wait(client, peer_id, *, since=None, timeout=None, addressed="1", seq="1", extra=""):
    """GET /v1/mesh/wait as a node would. `seq`: the addressed mode's opt-in to the commit
    order's cursor (STATUS E13), sent with `addressed` only, as the monitor sends it since
    E13; None for a client from before it, which never sends it."""
    q = []
    if since is not None:
        q.append(f"since={since}")
    if timeout is not None:
        q.append(f"timeout={timeout}")
    if addressed is not None:
        q.append(f"addressed={addressed}")
        if seq is not None:
            q.append(f"seq={seq}")
    url = "/v1/mesh/wait" + ("?" + "&".join(q) if q else "") + extra
    started = time.monotonic()
    r = client.get(url, **_auth(peer_id))
    return r, time.monotonic() - started


def _seq(conn, row) -> int:
    """The row's place in the commit order (sql_meshychat/023), which the addressed wait's
    cursor names since STATUS E13."""
    with conn.cursor() as cur:
        cur.execute("SELECT seq FROM mesh_commit_order WHERE memory_id = %s", (row["id"],))
        return int(cur.fetchone()[0])


def _plain_before(row) -> str:
    """A PLAIN-route cursor just before `row` (its created_at less 1 us, the nil uuid). Since
    STATUS E13 the addressed cursor is a seq cursor, which the plain route reads as "no
    cursor", so a plain control needs a plain cursor of its own."""
    return WAIT.encode_cursor(WAIT.micros_of(row["created_at"]) - 1, uuid.UUID(int=0))


def _anchor(client, peer) -> str:
    """The addressed route's own answer to "where am I now": a 204 names its position."""
    r, _ = _wait(client, peer["peer_id"], timeout=0)
    assert r.status_code in (200, 204), (r.status_code, r.content[:300])
    if r.status_code == 200:
        return json.loads(r.content)["cursor"]
    assert r.get("X-Meshy-Cursor"), dict(r.items())
    return r["X-Meshy-Cursor"]


def _pair(owner) -> tuple[dict, dict, dict]:
    """(mesh owner, a sender that can write the mesh, a receiver that can read it)."""
    mesh = _mesh_owner(owner)
    sender, receiver = _peer(owner, "send"), _peer(owner, "recv")
    _grant(owner, NS, sender, mesh, permission="write")
    _grant(owner, NS, receiver, mesh, permission="read")
    return mesh, sender, receiver


def _push(owner, row, peer_id) -> tuple[dict | None, bool]:
    """What the LIVE push builds for this row and `peer_id`, and whether it sends it: the
    bridge's own functions on its own read of the row (`_pushed_frame`, below)."""
    frame = _pushed_frame(owner, row, peer_id)
    return frame, frame is not None


# =============================================================================== the set

def test_addressed_wait_answers_exactly_the_rows_the_push_would_nudge_and_plain_answers_all(
        client, owner):
    """One sequence over every branch of the push's filter. Addressed: `to` by peers.name,
    `to` in another case, `to` by `peer-<id8>`, `cc` only, `all`, and no `to` at all.
    Not addressed: `to` someone else, the receiver's own broadcast, and a row addressed to
    it in ANOTHER namespace it can read (the push never fires outside shared/mesh-chat).
    CONTROL, same cursor: the plain route returns every row the receiver did not write, in
    every namespace it reads, which is the difference this parameter exists for."""
    mesh, sender, receiver = _pair(owner)
    ns2 = f"shared/mesh-chat-wa-{uuid.uuid4().hex[:8]}"
    _claim(owner, ns2, sender)
    _grant(owner, ns2, receiver, sender)
    since = _anchor(client, receiver)

    name, fallback = receiver["name"], f"peer-{str(receiver['peer_id'])[:8]}"
    rows = {
        "to_name": _row(owner, sender, {"to": name}),
        "other": _row(owner, sender, {"to": "somebody-else"}),
        "cc_only": _row(owner, sender, {"to": "somebody-else", "cc": [name]}),
        "all": _row(owner, sender, {"to": "all"}),
        "no_to": _row(owner, sender, {"body": "no recipient field"}),
        "upper": _row(owner, sender, {"to": name.upper()}),
        "fallback": _row(owner, sender, {"to": [fallback]}),
        "own": _row(owner, receiver, {"to": "all"}),
        "other_ns": _row(owner, sender, {"to": name}, ns=ns2),
    }
    mine = {r["key"]: tag for tag, r in rows.items()}
    want = ["to_name", "cc_only", "all", "no_to", "upper", "fallback"]

    r, _ = _wait(client, receiver["peer_id"], since=since, timeout=1)
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert r["X-Meshy-Addressed"] == "1"
    body = json.loads(r.content)
    got = [m for m in body["messages"] if m["key"] in mine]
    assert [mine[m["key"]] for m in got] == want, [mine[m["key"]] for m in got]

    # Each item IS the push's frame for that row, built by the push's own function, and the
    # push's own recipient resolution agrees row by row (it is the one predicate).
    for m in got:
        tag = mine[m["key"]]
        frame, _ = _push(owner, rows[tag], receiver["peer_id"])
        assert m == frame, (tag, m, frame)
        assert set(m) <= FRAME_FIELDS and m["type"] == "mesh_message"
    for tag in ("to_name", "other", "cc_only", "all", "no_to", "upper", "fallback", "own"):
        _, pushed = _push(owner, rows[tag], receiver["peer_id"])
        assert pushed == (tag in want), (tag, pushed)

    # CONTROL: the plain route over the same span (a plain cursor just before the first row:
    # the addressed cursor is a seq cursor since E13). Addressing-agnostic, every readable
    # namespace.
    r, _ = _wait(client, receiver["peer_id"], since=_plain_before(rows["to_name"]), timeout=1,
                 addressed=None)
    assert r.status_code == 200 and "X-Meshy-Addressed" not in r
    plain = [mine[m["key"]] for m in json.loads(r.content)["messages"] if m["key"] in mine]
    assert set(plain) - set(want) == {"other", "other_ns"}, plain
    assert "own" not in plain, "neither path answers a peer its own row"


def test_an_addressed_wait_scans_past_rows_for_others_and_its_cursor_says_where_it_got(
        client, owner):
    """A page of rows none of which is for the caller must not stall it. 55 rows to someone
    else (more than one 50-row page), then one to the caller: ONE call returns that row,
    and its cursor is at or past it. A later call from that cursor never returns it again.
    Then an idle call over rows for others only: 204, and `X-Meshy-Cursor` has moved past
    them (control: the plain route over the same span returns them all)."""
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    for i in range(55):
        _row(owner, sender, {"to": f"not-you-{i}"})
    target = _row(owner, sender, {"to": receiver["name"]})

    r, _ = _wait(client, receiver["peer_id"], since=since, timeout=1)
    assert r.status_code == 200, (r.status_code, r.content[:300])
    body = json.loads(r.content)
    assert [m["key"] for m in body["messages"]] == [target["key"]]
    at = WAIT.decode_seq_cursor(body["cursor"])
    assert at is not None and at.seq >= _seq(owner, target), body["cursor"]

    r, _ = _wait(client, receiver["peer_id"], since=body["cursor"], timeout=0)
    assert r.status_code == 204, r.content[:300]
    moved_from = r["X-Meshy-Cursor"]

    others = [_row(owner, sender, {"to": f"still-not-you-{i}"}) for i in range(3)]
    r, _ = _wait(client, receiver["peer_id"], since=moved_from, timeout=0)
    assert r.status_code == 204 and r["X-Meshy-Addressed"] == "1", r.content[:300]
    moved_to = WAIT.decode_seq_cursor(r["X-Meshy-Cursor"])
    assert moved_to == WAIT.SeqCursor(_seq(owner, others[-1]))

    r, _ = _wait(client, receiver["peer_id"], since=_plain_before(others[0]), timeout=1,
                 addressed=None)
    assert r.status_code == 200, "CONTROL: the rows the addressed wait skipped exist"
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [o["key"] for o in others]


def test_a_read_that_spends_its_budget_on_rows_for_others_answers_200_and_does_not_park(
        client, owner, monkeypatch):
    """The scan budget made tiny (5-row pages, 2 pages a read): 12 rows for others, then
    one for the caller. The first call answers AT ONCE (timeout 5, no park) with 200, no
    messages, and a cursor exactly 10 rows on: "go on from here". A 204 there would read as
    "nothing left" and the caller would stop short of its row. The second call returns it."""
    monkeypatch.setattr(WAIT, "ROW_LIMIT", 5)
    monkeypatch.setattr(WAIT, "ADDRESSED_SCAN_PAGES", 2)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    others = [_row(owner, sender, {"to": f"not-you-{i}"}) for i in range(12)]
    target = _row(owner, sender, {"to": receiver["name"]})
    r, took = _wait(client, receiver["peer_id"], since=since, timeout=5)
    assert r.status_code == 200 and r["X-Meshy-Addressed"] == "1", (r.status_code, took)
    assert took < 3.0, f"it parked for {took:.2f}s with rows left to scan"
    body = json.loads(r.content)
    assert body["messages"] == []
    assert WAIT.decode_seq_cursor(body["cursor"]) == WAIT.SeqCursor(_seq(owner, others[9]))
    r, _ = _wait(client, receiver["peer_id"], since=body["cursor"], timeout=0)
    assert r.status_code == 200, r.content[:300]
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [target["key"]]


def test_addressed_timeout_zero_reads_and_answers_at_once(client, owner, monkeypatch):
    """The plain route answers `timeout=0` with 204 before reading anything, so it cannot be
    used to read without waiting. The addressed
    route reads and answers at once: 200 with the rows, or 204 with its position. The
    control is the plain route answering the same request 204 with a row waiting."""
    # "At once" against a 30 s poll: a request that waited even one poll takes 30 s, so the 10 s bound still tells
    # them apart. The old bound, 1.5 s against the 2 s default poll, failed at 2.9 s on a loaded gate box (main's
    # gate 675924fb, 29 Oct); the focused rerun on a quiet box passed (mg446f, mg446g).
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    row = _row(owner, sender, {"to": receiver["name"]})
    r, took = _wait(client, receiver["peer_id"], since=since, timeout=0)
    assert r.status_code == 200 and took < 10.0, (r.status_code, took)
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [row["key"]]
    r, _ = _wait(client, receiver["peer_id"], since=since, timeout=0, addressed=None)
    assert r.status_code == 204, "CONTROL: plain timeout=0 never reads"


def _row_in_open_transaction(conn, peer, value: dict) -> str:
    """A head msg/ row INSERTED but not committed, `created_at` left to its default: NOW(),
    the writing transaction's START, exactly as the real write path stamps a row
    (sql/028: `created_at DEFAULT NOW()`; handlers.handle_write never sets it)."""
    rid = uuid.uuid4()
    key = f"msg/{int(time.time() * 1000)}-{rid.hex}"
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without).
    sr.insert(conn, sr.row(peer["peer_id"], peer["owner_id"], NS, key, value, rid=rid,
                           call_id=f"call-{rid}", author_label=peer["label"]))
    return key


def test_the_resume_point_stays_behind_a_write_that_commits_after_a_later_one(
        client, owner):
    """REVIEW F1, as STATUS E13 answers it. `created_at` is the writing transaction's START,
    so a row can commit AFTER a row stamped later than it has been read; a walk keyed on
    `created_at` then lies past it, and a caller that resumes there never sees it: that is
    how the monitor lost a message across a re-arm. Until E13 the resume point was held
    ADDRESSED_SETTLE_SECS behind the database clock. The addressed wait now walks the commit
    order, so the late row's seq is drawn at ITS commit, after the row the read passed.
    Here a write addressed to the receiver is begun and held open, a later write to someone
    else commits, and the addressed read scans past the later one (a 204 naming its seq).
    From that position the held row, once committed, IS answered, and the resume point is
    the position itself: nothing is held back. (Before E13, CONTROL of this same test: from
    `X-Meshy-Cursor` the held row was never answered.)"""
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    with psycopg.connect(DSN) as slow:                        # a transaction, not autocommit
        held = _row_in_open_transaction(slow, sender, {"to": receiver["name"]})
        time.sleep(0.05)
        later = _row(owner, sender, {"to": "somebody-else"})
        r, _ = _wait(client, receiver["peer_id"], since=since, timeout=0)
        assert r.status_code == 204, (r.status_code, r.content[:300])
        position, resume = r["X-Meshy-Cursor"], r["X-Meshy-Resume-Cursor"]
        slow.commit()
    assert WAIT.decode_seq_cursor(position) == WAIT.SeqCursor(_seq(owner, later))
    assert resume == position, "the resume point is the scan position"

    ours = {held, later["key"]}
    # Page by page from each answer's `cursor`, as the monitor reads. (A broadcast a sibling
    # test wrote in the last seconds is addressed to this receiver too, so it may come
    # first; only this test's two rows are compared.)
    got, at = [], position
    for _ in range(50):
        r, _ = _wait(client, receiver["peer_id"], since=at, timeout=0)
        if r.status_code == 204:
            break
        assert r.status_code == 200, (r.status_code, r.content[:300])
        body = json.loads(r.content)
        assert r["X-Meshy-Resume-Cursor"] == body["cursor"], "a 200 resumes where it read to"
        got += [m["key"] for m in body["messages"]]
        at = body["cursor"]
    assert [k for k in got if k in ours] == [held], got


def test_a_parked_addressed_wait_is_woken_by_a_row_for_the_caller_not_by_one_for_another(
        client, owner, monkeypatch):
    monkeypatch.setattr(WAIT, "POLL_SECS", 0.1)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    result = {}

    def park():
        result["r"], result["took"] = _wait(Client(), receiver["peer_id"], since=since,
                                            timeout=10)

    t = threading.Thread(target=park)
    t.start()
    time.sleep(0.5)
    _row(owner, sender, {"to": "not-you"})
    time.sleep(1.0)
    mine = _row(owner, sender, {"cc": [receiver["name"]], "to": "not-you"})
    t.join(timeout=15)
    assert not t.is_alive(), "the waiter never returned"
    r = result["r"]
    assert r.status_code == 200, r.content[:300]
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [mine["key"]]
    assert 1.3 <= result["took"] < 9.0, f"woken at {result['took']:.2f}s"


# =============================================================================== the gate

def test_the_addressed_wait_refuses_a_peer_that_cannot_read_the_mesh(client, owner):
    """The addressed set is shared/mesh-chat's, so the addressed route is gated like the
    four graph routes: memory.read's own check, a typed 403. The plain route returns 204 with
    zero visible namespaces for the same peer. CONTROL: the peer, granted read, is served."""
    mesh = _mesh_owner(owner)
    p = _peer(owner, "nogrant")
    r, _ = _wait(client, p["peer_id"], timeout=0)
    assert r.status_code == 403, (r.status_code, r.content[:300])
    assert json.loads(r.content)["error"] == "E_PERMISSION_DENIED"
    r, _ = _wait(client, p["peer_id"], timeout=1, addressed=None)
    assert r.status_code == 204 and r["X-Meshy-Visible-Namespaces"] == "0"
    _grant(owner, NS, p, mesh)
    r, _ = _wait(client, p["peer_id"], timeout=0)
    assert r.status_code in (200, 204) and r["X-Meshy-Addressed"] == "1", r.content[:300]


# =============================================================================== the plain route

def test_the_plain_route_is_unchanged_and_the_parameter_is_strict(client, owner):
    """No new header on a plain answer, `addressed=0` IS the plain route, and anything else
    is refused before authentication like the route's other parameters (400)."""
    mesh, sender, receiver = _pair(owner)
    row = _row(owner, sender, {"to": "somebody-else"})
    since = _plain_before(row)
    for flag in (None, "0"):
        r, _ = _wait(client, receiver["peer_id"], since=since, timeout=1, addressed=flag)
        assert r.status_code == 200, (flag, r.content[:300])
        assert "X-Meshy-Addressed" not in r and "X-Meshy-Cursor" not in r, (flag, dict(r.items()))
        assert "X-Meshy-Resume-Cursor" not in r, (flag, dict(r.items()))
        body = json.loads(r.content)
        assert [m["key"] for m in body["messages"]] == [row["key"]]
        assert set(body["messages"][0]) == {"key", "from", "to", "created_at"}
        # The plain 204 as well (`timeout=0` answers before reading a row): a marker there
        # would tell a monitor the plain route had applied a filter it never applies.
        r, _ = _wait(client, receiver["peer_id"], timeout=0, addressed=flag)
        assert r.status_code == 204, (flag, r.status_code, r.content[:300])
        assert "X-Meshy-Addressed" not in r and "X-Meshy-Cursor" not in r, (flag, dict(r.items()))
        assert "X-Meshy-Resume-Cursor" not in r, (flag, dict(r.items()))
    for bad, extra in (("yes", ""), ("1", "&addressed=1")):
        r, _ = _wait(client, receiver["peer_id"], timeout=0, addressed=bad, extra=extra)
        assert r.status_code == 400, (bad, extra, r.status_code, r.content[:200])


# =============================================================================== the role
#
# Divergence 033. MEASURED BEFORE THE CHANGE (round 1, txxxx, live, 2026-09-23): a
# `listen --monitor` line carried the frame's `to` only, so a cc'd peer could not tell it
# had been cc'd; and `to` arrived as a JSON-ENCODED STRING ("[\"desktop-5e6e5b2b\"]").

def _drain(client, peer, since) -> list[dict]:
    """Every addressed frame after `since`, page by page from each answer's `cursor`, as
    the monitor reads (a broadcast a sibling test wrote may be among them)."""
    out, at = [], since
    for _ in range(50):
        r, _ = _wait(client, peer["peer_id"], since=at, timeout=0)
        if r.status_code == 204:
            return out
        assert r.status_code == 200, (r.status_code, r.content[:300])
        body = json.loads(r.content)
        out += body["messages"]
        at = body["cursor"]
    raise AssertionError("the addressed wait never answered 204")


def _pushed_frame(owner, row, peer_id):
    """The frame the LIVE push sends this peer for this row (None when it sends none), by
    the bridge's own functions on its own read of the row, for a socket bound to no key."""
    with rls.server_internal(owner):
        nudge = mesh_notify.load_nudge(owner, row["key"], row["author"])
        recipients, _ = mesh_notify.resolve_recipients(owner, nudge, [peer_id])
        roles = dict(recipients)
        if peer_id not in roles:
            return None
        frame, _why = mesh_notify.push_frame(owner, nudge, peer_id, roles[peer_id], None)
    return frame


def test_the_addressed_wait_tells_the_caller_the_role_the_push_does(
        client, owner, memory_authority):
    """`role` for THIS caller, from the one function the push decides by
    (`mesh_notify.role_for`), so the two paths cannot disagree: to, cc, broadcast, both
    (to wins), a broadcast that also names the caller in cc (cc), no `to` at all
    (broadcast), and the measured legacy shape (a JSON array held in a string), whose `to`
    now arrives as a list. Each answer is then compared, whole, with the frame the push
    builds for the same row and the same peer.

    The caller is one memory.read would show the rows to, so its frames carry `cc`. Review
    F3 of 44640f2: `cc` is shown to no other reader. The outsider (another trust group, a
    key, read, no skill grant) catches up the broadcast that cc's the caller with its role
    and `to` and no `cc`, the caller's cc'd label nowhere in its answer, and that answer is
    the push's frame for it too."""
    from tests.fixtures import meshsem_db as db

    mesh = _mesh_owner(owner)
    sender = _peer(owner, "send")
    receiver = _member(owner, "recv", memory_authority["group"])
    outsider = _peer(owner, "outsider")
    outsider.update(db.add_key(owner, outsider["peer_id"]))
    _grant(owner, NS, sender, mesh, permission="write")
    for p in (receiver, outsider):
        _grant(owner, NS, p, mesh)
    since = _anchor(client, receiver)
    since_outsider = _anchor(client, outsider)
    name = receiver["name"]
    cases = {
        "to": ({"to": [name]}, "to"),
        "cc": ({"to": ["somebody-else"], "cc": [name]}, "cc"),
        "all": ({"to": ["all"]}, "broadcast"),
        "both": ({"to": [name], "cc": [name]}, "to"),
        "all_cc_me": ({"to": ["all"], "cc": [name.upper()]}, "cc"),
        "no_to": ({"body": "no recipient field"}, "broadcast"),
        "legacy": ({"to": json.dumps([name])}, "to"),
    }
    rows = {tag: _row(owner, sender, value) for tag, (value, _) in cases.items()}
    mine = {r["key"]: tag for tag, r in rows.items()}
    got = {mine[m["key"]]: m for m in _drain(client, receiver, since) if m["key"] in mine}
    assert sorted(got) == sorted(cases), sorted(got)
    assert ({tag: m.get("role") for tag, m in got.items()}
            == {tag: role for tag, (_, role) in cases.items()}), got
    assert got["legacy"]["to"] == [name], got["legacy"]
    assert got["both"]["to"] == [name] and got["both"]["cc"] == [name], got["both"]
    assert got["all_cc_me"]["to"] == ["all"] and got["all_cc_me"]["cc"] == [name.upper()]
    assert "to" not in got["no_to"] and "cc" not in got["no_to"], got["no_to"]
    for tag, m in got.items():
        assert m == _pushed_frame(owner, rows[tag], receiver["peer_id"]), (tag, m)

    theirs = {mine[m["key"]]: m for m in _drain(client, outsider, since_outsider)
              if m["key"] in mine}
    assert sorted(theirs) == ["all", "all_cc_me", "no_to"], sorted(theirs)
    assert {tag: m.get("role") for tag, m in theirs.items()} == {
        "all": "broadcast", "all_cc_me": "broadcast", "no_to": "broadcast"}, theirs
    assert theirs["all_cc_me"]["to"] == ["all"], theirs["all_cc_me"]
    for tag, m in theirs.items():
        assert "cc" not in m and "cc_truncated" not in m, (tag, m)
        assert m == _pushed_frame(owner, rows[tag], outsider["peer_id"]), (tag, m)
    assert name.upper() not in json.dumps(theirs), "cc reached a reader without read"


# =============================================================================== the preview
#
# Divergence 033: an addressed frame to a caller memory.read would show the body to carries
# its first 160 code points as `preview_unverified`, with `preview_truncated`. Divergence
# 027's gate decides (`search._anchor_body_withheld`), in the transaction that reads the rows.

ASTRAL = "\U0001F600"


@pytest.fixture()
def memory_authority(owner):
    """A memory authority made by the bootstrap command's own function, and
    MESHY_MEMORY_PEER_ID pointed at it for the test."""
    from tests.fixtures import meshsem_db as db
    mem = db.memory_authority(owner)
    with override_settings(MESHY_MEMORY_PEER_ID=str(mem["peer_id"])):
        yield mem


def _member(conn, tag: str, group, *, key: bool = True) -> dict:
    """`_peer`'s shape, its owner in the trust group `group` (a trust group is fixed when
    the owner row is written), with an active signing key unless `key` is False: a caller
    memory.read's invoke path admits."""
    from tests.fixtures import meshsem_db as db
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    name = f"wa-{tag}-{peer_id.hex[:8]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"wa-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, name, f"wa {tag}"))
    p = {"peer_id": peer_id, "owner_id": owner_id, "name": name, "label": name}
    if key:
        p.update(db.add_key(conn, peer_id))
    return p


def _previews(client, peer, since, **headers) -> dict:
    """key -> (preview_unverified or None, preview_truncated or None), every addressed frame
    after `since`, with extra request headers."""
    out, at = {}, since
    for _ in range(50):
        q = f"/v1/mesh/wait?since={at}&timeout=0&addressed=1"
        r = client.get(q, **{**_auth(peer["peer_id"]), **headers})
        if r.status_code == 204:
            return out
        assert r.status_code == 200, (r.status_code, r.content[:300])
        body = json.loads(r.content)
        for m in body["messages"]:
            out[m["key"]] = (m.get("preview_unverified"), m.get("preview_truncated"))
        at = body["cursor"]
    raise AssertionError("the addressed wait never answered 204")


def test_the_addressed_wait_previews_160_whole_code_points_as_the_push_does(
        client, owner, memory_authority):
    """The bound through the database: bodies of 159, 160 and 161 characters, an astral
    character on each side of the cut, a body that is not a string, no body at all, and
    an empty body. `nudge_columns` reads the head of the body in SQL (`left()` counts
    characters, not bytes), and each answer equals the push's own frame for the same row
    and peer."""
    mesh = _mesh_owner(owner)
    sender = _peer(owner, "send")
    receiver = _member(owner, "recv", memory_authority["group"])
    _grant(owner, NS, sender, mesh, permission="write")
    _grant(owner, NS, receiver, mesh)
    since = _anchor(client, receiver)
    name = receiver["name"]
    cases = {
        "159": ("a" * 159, ("a" * 159, False)),
        "160": ("a" * 160, ("a" * 160, False)),
        "161": ("a" * 161, ("a" * 160, True)),
        "astral_at_160": ("a" * 159 + ASTRAL, ("a" * 159 + ASTRAL, False)),
        "astral_kept": ("a" * 159 + ASTRAL + "b", ("a" * 159 + ASTRAL, True)),
        "astral_after": ("a" * 160 + ASTRAL, ("a" * 160, True)),
        "all_astral": (ASTRAL * 400, (ASTRAL * 160, True)),
        "empty": ("", ("", False)),
        "object": ({"text": "not a string"}, (None, None)),
        "absent": (None, (None, None)),
    }
    rows = {}
    for tag, (body, _) in cases.items():
        value = {"to": [name]}
        if body is not None:
            value["body"] = body
        rows[tag] = _row(owner, sender, value)
    got = _previews(client, receiver, since)
    for tag, (_, want) in cases.items():
        assert got.get(rows[tag]["key"]) == want, (tag, got.get(rows[tag]["key"]), want)
        pushed = _pushed_frame(owner, rows[tag], receiver["peer_id"])
        assert (pushed.get("preview_unverified"), pushed.get("preview_truncated")) == want, tag


def test_the_addressed_wait_previews_only_what_memory_read_would_show_the_caller(
        client, owner, memory_authority):
    """Divergence 027's gate, per caller, each beside its control: another trust group
    without a skill grant (not_authorized; a skill grant then shows it) and no signing key
    (no_usable_signing_key; a key then shows it). The frames themselves are the same set
    either way: the preview is withheld, never the message."""
    from tests.fixtures import meshsem_db as db
    mesh = _mesh_owner(owner)
    sender = _peer(owner, "send")
    outsider = _peer(owner, "outsider")
    outsider.update(db.add_key(owner, outsider["peer_id"]))
    keyless = _member(owner, "keyless", memory_authority["group"], key=False)
    _grant(owner, NS, sender, mesh, permission="write")
    for p in (outsider, keyless):
        _grant(owner, NS, p, mesh)
    since = {p["name"]: _anchor(client, p) for p in (outsider, keyless)}
    row = _row(owner, sender, {"to": [outsider["name"], keyless["name"]], "body": "for readers"})

    for p in (outsider, keyless):
        got = _previews(client, p, since[p["name"]])
        assert got.get(row["key"]) == (None, None), (p["name"], got.get(row["key"]))
    db.skill_grant(owner, memory_authority, outsider)
    db.add_key(owner, keyless["peer_id"])
    for p in (outsider, keyless):
        got = _previews(client, p, since[p["name"]])
        assert got.get(row["key"]) == ("for readers", False), ("control", p["name"])


def test_a_read_revoked_inside_the_scan_withholds_the_preview_not_the_frame(
        client, owner, memory_authority, monkeypatch):
    """The route checks read in its own transaction, then scans in another. A grant revoked
    after the scan read its rows must not see the body: the gate re-checks the namespace
    grant in the transaction that read them (namespace_permission), as handle_read checks
    and reads in one. The revoke is placed there by wrapping `_addressed_seq_page`, the
    scan's row read (`_addressed_page` until STATUS E13). The frame itself still comes back (its set was read before the revoke).
    Control: the same read with no revoke shows the preview."""
    mesh = _mesh_owner(owner)
    sender = _peer(owner, "send")
    reader = _member(owner, "revoked", memory_authority["group"])
    _grant(owner, NS, sender, mesh, permission="write")
    _grant(owner, NS, reader, mesh)
    since = _anchor(client, reader)
    row = _row(owner, sender, {"to": [reader["name"]], "body": "read before the revoke"})
    assert _previews(client, reader, since).get(row["key"]) == (
        "read before the revoke", False), "control"

    real = WAIT._addressed_seq_page

    def page_then_revoke(conn, peer_id, since):
        rows = real(conn, peer_id, since)
        with owner.cursor() as cur:
            cur.execute("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = now() "
                        "WHERE grantee_peer_id = %s", (reader["peer_id"],))
        return rows

    monkeypatch.setattr(WAIT, "_addressed_seq_page", page_then_revoke)
    r, _ = _wait(client, reader["peer_id"], since=since, timeout=0)
    assert r.status_code == 200, (r.status_code, r.content[:300])
    mine = [m for m in json.loads(r.content)["messages"] if m["key"] == row["key"]]
    assert len(mine) == 1, "the frame itself must still come back"
    assert "preview_unverified" not in mine[0] and "preview_truncated" not in mine[0], mine
    assert b"read before the revoke" not in r.content


def test_with_dpop_enforced_the_addressed_wait_previews_only_for_a_proof_of_this_get(
        client, owner, memory_authority):
    """memory.read's POST needs a DPoP proof when DPoP is enforced (divergence 015), so the
    preview does too, checked as the context route checks its anchor body (027): no proof,
    and a proof for another path, show the frame without it; a fresh proof for this GET
    shows it. The route stays a bearer read: every answer is 200."""
    from apps.authapi.didkey import to_did_key
    from tools.meshy_node import wire

    mesh = _mesh_owner(owner)
    sender = _peer(owner, "send")
    reader = _member(owner, "dpop", memory_authority["group"])
    _grant(owner, NS, sender, mesh, permission="write")
    _grant(owner, NS, reader, mesh)
    since = _anchor(client, reader)
    row = _row(owner, sender, {"to": [reader["name"]], "body": "for the key holder"})

    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    token = pyjwt.encode({"sub": str(reader["peer_id"]), "iss": ISSUER, "iat": now,
                          "exp": now + 300, "aud": "peer", "scopes": [],
                          "cnf": to_did_key(wire.public_key_of(reader["seed"]))},
                         JWT_PRIVATE_PEM, algorithm="EdDSA")

    def get(proof_target=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if proof_target is not None:
            headers["HTTP_DPOP"] = wire.dpop_proof(reader["seed"], "GET", proof_target, token)
        r = client.get(f"/v1/mesh/wait?since={since}&timeout=0&addressed=1", **headers)
        assert r.status_code == 200, (r.status_code, r.content[:300])
        mine = [m for m in json.loads(r.content)["messages"] if m["key"] == row["key"]]
        assert len(mine) == 1, r.content[:300]
        return mine[0].get("preview_unverified")

    with override_settings(MESHY_DPOP_ENFORCE=True):
        assert get() is None, "no proof"
        assert get("/v1/mesh/context") is None, "a proof for another path"
        assert get("/v1/mesh/wait") == "for the key holder", "control: a proof for this GET"
    assert get() == "for the key holder", "control: DPoP not enforced"


# ===================================================== the review of 44640f2, as applied
#
# F2: when 027's gate FAILED, the push counted `error` and sent the frame without a preview,
# while the addressed wait answered 500 for the whole page. And the wait's reader is built
# by the context route's `_reader`, which RAISES a DPoP-ledger fault (5xx) rather than read
# it as "no proof": right for the context route, whose body is its answer, but here it
# failed a catch-up that never touched the ledger before 033. Both now withhold the preview
# and never the answer, as 033 promises and the push already did.

def _gate_raises(conn, reader, key_or_keys):
    raise RuntimeError("injected: the preview gate failed")


def _gate_database_error(conn, reader, key_or_keys):
    conn.execute("SELECT 1 / 0")          # fails in the database: aborts its transaction


@pytest.mark.parametrize("fault", [_gate_raises, _gate_database_error],
                         ids=["raises", "database_error"])
def test_a_preview_gate_that_fails_withholds_the_preview_never_the_answer(
        client, owner, memory_authority, monkeypatch, caplog, fault):
    """027's gate fails while the addressed wait asks it about a caller it would pass: the
    answer is 200 with the frame, which carries no preview, and it equals the frame the
    push builds for the same row and peer under the same fault (one fail-closed gate, both
    paths). The failure is logged, not silent. A database error inside the gate does not
    abort the scan's transaction (the gate runs in a savepoint). Control: the same wait
    with the gate whole shows the preview."""
    import logging

    from apps.meshsem import search

    mesh = _mesh_owner(owner)
    sender = _peer(owner, "send")
    reader = _member(owner, "gatefault", memory_authority["group"])
    _grant(owner, NS, sender, mesh, permission="write")
    _grant(owner, NS, reader, mesh)
    since = _anchor(client, reader)
    row = _row(owner, sender, {"to": [reader["name"]], "body": "withheld by a fault"})
    assert _previews(client, reader, since).get(row["key"]) == (
        "withheld by a fault", False), "control: the gate whole shows the preview"

    # The push asks 027's gate one key at a time and the wait a page at a time (divergence
    # 040, slice 2 review F2): the same fault in both, so each path meets it.
    monkeypatch.setattr(search, "body_withheld", fault)
    monkeypatch.setattr(search, "body_withheld_many", fault)
    with caplog.at_level(logging.WARNING):
        r, _ = _wait(client, reader["peer_id"], since=since, timeout=0)
    assert r.status_code == 200, (r.status_code, r.content[:300])
    mine = [m for m in json.loads(r.content)["messages"] if m["key"] == row["key"]]
    assert len(mine) == 1, "the frame itself must come back"
    assert "preview_unverified" not in mine[0] and "preview_truncated" not in mine[0], mine
    assert b"withheld by a fault" not in r.content
    assert mine[0] == _pushed_frame(owner, row, reader["peer_id"]), "push and wait differ"
    assert any("preview gate failed" in rec.getMessage() for rec in caplog.records), \
        [rec.getMessage() for rec in caplog.records]


def test_a_dpop_ledger_fault_withholds_the_preview_never_the_answer(
        client, owner, memory_authority, monkeypatch, caplog):
    """DPoP enforced, a valid proof for this GET, and the jti ledger faults (the 500 that
    `require_bound_proof` raises for a database error on `dpop_jti_seen`). The addressed
    wait answers 200 with the frame and no preview, and logs the fault. Control: the same
    request with the ledger whole shows the preview."""
    import logging

    from apps.authapi.didkey import to_did_key
    from apps.meshsem import views as meshsem_views
    from apps.peers import errors
    from tools.meshy_node import wire

    mesh = _mesh_owner(owner)
    sender = _peer(owner, "send")
    reader = _member(owner, "ledger", memory_authority["group"])
    _grant(owner, NS, sender, mesh, permission="write")
    _grant(owner, NS, reader, mesh)
    since = _anchor(client, reader)
    row = _row(owner, sender, {"to": [reader["name"]], "body": "for the key holder"})

    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    token = pyjwt.encode({"sub": str(reader["peer_id"]), "iss": ISSUER, "iat": now,
                          "exp": now + 300, "aud": "peer", "scopes": [],
                          "cnf": to_did_key(wire.public_key_of(reader["seed"]))},
                         JWT_PRIVATE_PEM, algorithm="EdDSA")

    def get():
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}",
                   "HTTP_DPOP": wire.dpop_proof(reader["seed"], "GET", "/v1/mesh/wait", token)}
        r = client.get(f"/v1/mesh/wait?since={since}&timeout=0&addressed=1", **headers)
        assert r.status_code == 200, (r.status_code, r.content[:300])
        mine = [m for m in json.loads(r.content)["messages"] if m["key"] == row["key"]]
        assert len(mine) == 1, r.content[:300]
        return mine[0].get("preview_unverified")

    def ledger_fault(*args, **kwargs):
        raise errors.internal("dpop jti ledger: injected")

    with override_settings(MESHY_DPOP_ENFORCE=True):
        assert get() == "for the key holder", "control: the ledger whole"
        monkeypatch.setattr(meshsem_views, "require_bound_proof", ledger_fault)
        with caplog.at_level(logging.WARNING):
            assert get() is None, "a ledger fault showed the preview"
    assert any("ledger" in rec.getMessage() for rec in caplog.records), \
        [rec.getMessage() for rec in caplog.records]
