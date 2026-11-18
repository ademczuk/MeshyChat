"""The mesh graph over HTTP, through the REAL urlconf, with a REAL peer bearer.

Same three proofs as tests/test_board_routes.py: wiring through `meshychat.urls`, a
genuine EdDSA bearer, and that the response carries coverage so an empty inbox cannot be
read as "no mail" when it means "not projected".

Run:  pytest tests/test_mesh_routes.py -v
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
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

ISSUER = "meshy-mesh-test-issuer"
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

from apps.memoryskills import handlers  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS  # noqa: E402
from apps.core import rls  # noqa: E402
from apps.mesh import projection as P  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402


@pytest.fixture(autouse=True)
def mesh_settings():
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM, MESHY_JWT_ISSUER=ISSUER,
                           MESHY_DSN=DSN):
        yield


@pytest.fixture()
def client():
    return Client()


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


#: How many times _sqlmigrate runs the same command again after Postgres picked it as a deadlock victim. A down
#: script or a re-applied migration takes ACCESS EXCLUSIVE locks with no lock_timeout of its own (046 has none), and
#: the test cluster runs autovacuum, so the two can deadlock. On HQB on 25 Oct 2026 (gate mg385i) 046's re-apply lost
#: to autovacuum on shared_memory, the half-restored database failed the next 95 tests, and the gate was void. Each
#: down step and each migration commits with its ledger row, so the same command carries on from where it stopped.
#: One retry; a second deadlock fails the test as before.
_SQLMIGRATE_RETRIES = 1


def _sqlmigrate(*args: str, _run=subprocess.run, _sleep=None) -> subprocess.CompletedProcess:
    """`apps/core/sqlmigrate.py --dsn DSN *args` from the repo root, run again once if it died as a deadlock victim."""
    import time
    sleep = _sleep or time.sleep
    command = [sys.executable, "apps/core/sqlmigrate.py", "--dsn", DSN, *args]
    for attempt in range(_SQLMIGRATE_RETRIES + 1):
        done = _run(command, cwd=Path(__file__).resolve().parent.parent,
                    capture_output=True, text=True, timeout=120)
        lost = done.returncode != 0 and ("DeadlockDetected" in (done.stderr or "")
                                         or "deadlock detected" in (done.stderr or ""))
        if not lost or attempt == _SQLMIGRATE_RETRIES:
            return done
        sleep(1)
    return done  # pragma: no cover (the loop always returns)


def _fake_sqlmigrate_runs(*results):
    """For the _sqlmigrate tests: a subprocess.run stand-in that answers each call with the next (returncode, stderr)."""
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        rc, err = results[len(calls) - 1]
        return subprocess.CompletedProcess(command, rc, stdout="", stderr=err)
    return run, calls


_DEADLOCK = "psycopg.errors.DeadlockDetected: deadlock detected\nDETAIL:  Process 1 waits for AccessExclusiveLock"


def test_sqlmigrate_runs_again_once_after_a_deadlock():
    """MUTANT: no retry (_SQLMIGRATE_RETRIES = 0); the deadlock reaches the test and poisons the shared database."""
    run, calls = _fake_sqlmigrate_runs((1, _DEADLOCK), (0, ""))
    done = _sqlmigrate("--rollback-to", "meshychat:36", _run=run, _sleep=lambda s: None)
    assert done.returncode == 0 and len(calls) == 2
    assert calls[0] == calls[1] and calls[0][-2:] == ["--rollback-to", "meshychat:36"]


def test_sqlmigrate_does_not_retry_any_other_failure():
    """CONTROL: a refused gate or a broken migration fails at once, so a retry can't hide it."""
    run, calls = _fake_sqlmigrate_runs((1, "REFUSED, nothing applied. no way back"), (0, ""))
    assert _sqlmigrate(_run=run, _sleep=lambda s: None).returncode == 1 and len(calls) == 1


def test_sqlmigrate_gives_up_after_a_second_deadlock():
    """A deadlock that repeats is a real one: the second failure goes to the test, with no third run."""
    run, calls = _fake_sqlmigrate_runs((1, _DEADLOCK), (1, _DEADLOCK), (0, ""))
    done = _sqlmigrate(_run=run, _sleep=lambda s: None)
    assert done.returncode == 1 and "DeadlockDetected" in done.stderr and len(calls) == 2


def _mint(peer_id, ttl=300) -> str:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + ttl,
                         "aud": "peer", "scopes": []}, JWT_PRIVATE_PEM, algorithm="EdDSA")


def _auth(peer_id) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {_mint(peer_id)}"}


def _peer(conn, tag: str, *, grant: bool = True, name: str | None = None) -> dict:
    """A dev-plane peer. It claims the namespace if nobody has yet, and otherwise is
    GRANTED read on it by the claimant: since divergence 018 every graph route requires
    read on shared/mesh-chat, and these tests are about the routes' answers, not the
    gate (tests/test_mesh_graphread.py is about the gate). `grant=False` makes the
    peer the gate would refuse."""
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    name = name if name is not None else f"msh-{tag}-{peer_id.hex[:8]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"msh-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, name, f"msh {tag}"))
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                    " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                    " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                    (NS, peer_id, owner_id))
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace"
                    " WHERE namespace = %s", (NS,))
        ns_peer, ns_owner = cur.fetchone()
        if grant and ns_peer != peer_id:
            cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                        " grantee_owner_id, permission, granted_by_peer_id,"
                        " granted_by_owner_id) VALUES (%s,%s,%s,'read',%s,%s)",
                        (NS, peer_id, owner_id, ns_peer, ns_owner))
    return {"peer_id": peer_id, "owner_id": owner_id, "label": tag, "name": name}


def _row(conn, peer, value: dict) -> str:
    rid = uuid.uuid4()
    key = f"msg/{int(_dt.datetime.now().timestamp() * 1000)}-{rid.hex}"
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without).
    sr.insert(conn, sr.row(peer["peer_id"], peer["owner_id"], NS, key, value, rid=rid,
                           call_id=f"call-{rid}", author_label=peer["label"]))
    return key


def _row_at(conn, peer, value: dict, created_at, *, key: str | None = None,
            version: int = 1, is_head: bool = True, parent_id=None, rid=None) -> tuple[str, uuid.UUID]:
    """A signed mesh row at a chosen time, including an edit-version shape when requested."""
    rid = rid or uuid.uuid4()
    key = key or f"msg/{int(created_at.timestamp() * 1000)}-{rid.hex}"
    sr.insert(conn, sr.row(peer["peer_id"], peer["owner_id"], NS, key, value, rid=rid,
                           call_id=f"call-{rid}", author_label=peer["label"],
                           created_at=created_at, version=version, is_head=is_head,
                           parent_id=parent_id))
    return key, rid


def _edit_row_at(conn, peer, key: str, value: dict, prior_id, created_at) -> uuid.UUID:
    """Insert a real edit-shaped version with the database's edit-path mark."""
    with conn.transaction():
        conn.execute("SELECT set_config('app.server_internal', '1', true)")
        conn.execute("SELECT set_config('app.mesh_edit_owner', %s, true)", (str(peer["owner_id"]),))
        conn.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (prior_id,))
        payload = sr.payload(peer["peer_id"], NS, key, value, skill_name="memory.edit",
                             extra_args={"expected_head_version": 1})
        return sr.insert(conn, sr.columns(payload, peer_id=peer["peer_id"], owner_id=peer["owner_id"],
                                          version=2, parent_id=prior_id, author_label=peer["label"],
                                          created_at=created_at))


def test_routes_refuse_without_a_bearer(client):
    for url in ("/v1/mesh/inbox", "/v1/mesh/owed", "/v1/mesh/drift", "/v1/mesh/threads/t",
                "/v1/mesh/wait?timeout=1"):
        assert client.get(url).status_code == 401, url


def test_inbox_answers_for_the_roster_label_and_names_its_source(client, owner):
    """The caller's label comes from the roster, the same source the daemon stamps
    author_label from. The response says whether it was the roster or the fallback."""
    p = _peer(owner, "me")
    me = f"me-{uuid.uuid4().hex[:6]}"
    handlers.prime_roster_labels(json.dumps({"peers": {str(p["peer_id"]): me}}).encode())
    try:
        k1 = _row(owner, p, {"to": me, "body": "string"})
        k2 = _row(owner, p, {"to": ["x", me], "body": "array"})
        _row(owner, p, {"to": ["x"], "body": "not mine"})
        P.rebuild(owner)

        r = client.get("/v1/mesh/inbox", **_auth(p["peer_id"]))
        assert r.status_code == 200, r.content[:300]
        body = r.json()
        assert body["label"] == me and body["label_source"] == "roster"
        assert {h["key"] for h in body["hits"]} == {k1, k2}
        assert body["coverage"]["total"] >= body["coverage"]["projected"] > 0
    finally:
        handlers.prime_roster_labels(b"{}")


def test_inbox_under_a_fallback_label_says_so(client, owner):
    """With no roster entry AND a blank peers.name the label is peer-<id8>, and the
    response must say 'fallback' so an empty inbox is not read as empty mail. (With a
    usable peers.name the label comes from there since divergence 018; that case is
    tests/test_mesh_graphread.py::test_the_label_comes_from_peers_name_when_no_roster.)"""
    p = _peer(owner, "nobody", name=" ")
    handlers.prime_roster_labels(b"{}")
    P.rebuild(owner)
    r = client.get("/v1/mesh/inbox", **_auth(p["peer_id"]))
    assert r.status_code == 200
    body = r.json()
    assert body["label_source"] == "fallback"
    assert body["label"] == f"peer-{str(p['peer_id'])[:8]}"
    assert "coverage" in body


def test_thread_and_drift_routes_answer(client, owner):
    p = _peer(owner, "t")
    t = f"t-{uuid.uuid4().hex[:6]}"
    k = _row(owner, p, {"to": ["x"], "thread": t})
    P.rebuild(owner)
    r = client.get(f"/v1/mesh/threads/{t}", **_auth(p["peer_id"]))
    assert r.status_code == 200 and [h["key"] for h in r.json()["hits"]] == [k]
    d = client.get("/v1/mesh/drift", **_auth(p["peer_id"]))
    assert d.status_code == 200
    assert d.json()["drift"]["in_thread"].get("thread", 0) >= 1
    assert "projection" in d.json()


def test_read_routes_answer_post_with_405(client, owner):
    p = _peer(owner, "post")
    r = client.post("/v1/mesh/inbox", **_auth(p["peer_id"]))
    assert r.status_code == 405 and r.headers.get("Allow") == "GET"


def test_owed_preserves_its_unfiltered_shape_and_echoes_validated_filters(client, owner):
    """The absent-filter response keeps every pre-filter field and value; supplied bounds
    are echoed only after the route validates them, so a client can show the applied window.

    MUTANT: always add `filters`; the no-parameter root shape changes. MUTANT: remove the
    state predicate from graph.owed_acks; an answered row leaks into the `open` response.
    """
    receiver, asker = _peer(owner, "owed-receiver"), _peer(owner, "owed-asker")
    ask = _row(owner, asker, {"to": [receiver["name"]], "thread_id": f"t-{uuid.uuid4().hex[:6]}",
                              "requires_ack": True, "done_when": "send the signed receipt"})
    answered_thread = f"t-{uuid.uuid4().hex[:6]}"
    answered = _row(owner, asker, {"to": [receiver["name"]], "thread_id": answered_thread,
                                   "requires_ack": True})
    _row(owner, receiver | {"label": receiver["name"]}, {"to": [asker["name"]], "thread_id": answered_thread,
                           "in_reply_to": answered})
    P.rebuild(owner)
    plain = client.get("/v1/mesh/owed", **_auth(receiver["peer_id"]))
    assert plain.status_code == 200, plain.content
    body = plain.json()
    assert set(body) == {"label", "label_source", "labels", "owed", "coverage", "truncated"}
    assert body["truncated"] is False
    item = next(row for row in body["owed"] if row["hit"]["key"] == ask)
    assert {key: item[key] for key in ("hit", "thread_id", "answered_in_thread")} == {
        "hit": item["hit"], "thread_id": item["thread_id"], "answered_in_thread": False}
    assert item["done_when"] == "send the signed receipt"
    filtered = client.get("/v1/mesh/owed?state=open&since=2000-01-01T00:00:00Z",
                          **_auth(receiver["peer_id"]))
    assert filtered.status_code == 200, filtered.content
    assert filtered.json()["filters"] == {"state": ["open"], "since": "2000-01-01T00:00:00Z"}
    assert ask in {row["hit"]["key"] for row in filtered.json()["owed"]}
    assert answered not in {row["hit"]["key"] for row in filtered.json()["owed"]}
    assert client.get("/v1/mesh/owed?state=closed", **_auth(receiver["peer_id"])).status_code == 200


def test_owed_exposes_the_head_message_type_for_question_and_request(client, owner):
    receiver, asker = _peer(owner, "owed-type-receiver"), _peer(owner, "owed-type-asker")
    question = _row(owner, asker, {"to": [receiver["name"]], "thread_id": "owed-type-question",
                                    "requires_ack": True, "type": "question"})
    request = _row(owner, asker, {"to": [receiver["name"]], "thread_id": "owed-type-request",
                                   "requires_ack": True, "type": "request"})
    P.rebuild(owner)
    rows = client.get("/v1/mesh/owed?state=open", **_auth(receiver["peer_id"])).json()["owed"]
    assert {row["hit"]["key"]: row["msg_type"] for row in rows if row["hit"]["key"] in (question, request)} == {
        question: "question", request: "request"}


def test_owed_thread_narrows_in_sql_before_limit_and_echoes_as_sent(client, owner):
    """#155 (carried forward from #165 by a reviewer): `thread` lists one thread's asks, filtered before LIMIT, so
    an older ask in the thread is not lost behind a newer one elsewhere, and is echoed in the caller's spelling (it
    is queried stripped, as the projection stores a thread). `thread_id` is not an alias: it is neither applied nor
    echoed, so a caller that checks the echo refuses that answer (pxx's review of #165).

    MUTANT: drop thread_id=thread from the graph.owed_acks call; the thread lists the other thread's ask too.
    MUTANT: drop applied["thread"]; the echo is missing. MUTANT: echo the stripped value; the padded echo differs.
    MUTANT: query the raw value; the padded thread matches nothing.
    MUTANT: read `thread_id` too (the alias); the alias call answers one row and echoes a filter.
    """
    receiver, asker = _peer(owner, "owed-thr-receiver"), _peer(owner, "owed-thr-asker")
    now = _dt.datetime.now(_dt.timezone.utc)
    old_thread, new_thread = f"t-{uuid.uuid4().hex[:6]}", f"t-{uuid.uuid4().hex[:6]}"
    older, _ = _row_at(owner, asker, {"to": [receiver["name"]], "thread_id": old_thread, "requires_ack": True},
                       now - _dt.timedelta(hours=2))
    newer, _ = _row_at(owner, asker, {"to": [receiver["name"]], "thread_id": new_thread, "requires_ack": True},
                       now - _dt.timedelta(hours=1))
    P.rebuild(owner)

    def owed(query):
        response = client.get(f"/v1/mesh/owed?{query}", **_auth(receiver["peer_id"]))
        assert response.status_code == 200, response.content
        return response.json().get("filters"), [row["hit"]["key"] for row in response.json()["owed"]]

    assert owed("limit=1") == (None, [newer]), "CONTROL: unfiltered, the newer ask fills the page"
    assert owed(f"thread={old_thread}&limit=1") == ({"thread": old_thread}, [older])
    assert owed(f"thread={new_thread}&state=open") == ({"state": ["open"], "thread": new_thread}, [newer])
    assert owed(f"thread=%20{old_thread}%20") == ({"thread": f" {old_thread} "}, [older])
    assert owed("thread=t-no-such-thread") == ({"thread": "t-no-such-thread"}, [])
    assert owed(f"thread_id={old_thread}") == (None, [newer, older])


def test_owed_thread_is_capped_at_the_write_paths_thread_id_bound(client, owner):
    """pxx's review of #165: `thread` had no bound. It takes what a message's thread_id may be written with, the
    write path's MESH_THREAD_RE, probed here rather than copied, and refuses one character more.

    MUTANT: OWED_THREAD_MAX 255 or 257; the probe disagrees. MUTANT: `>=` for `>`; the longest thread is refused.
    MUTANT: drop the length check; the longer one answers 200.
    The cap is on the value as queried, stripped (memory-bxxxxx's review of #212): the longest thread with a space
    either side answers 200 and is echoed as sent; one character more, padded the same way, is still refused.
    MUTANT: cap the raw value (as #212 did); the padded longest thread is refused.
    """
    from apps.memoryskills.validation import MESH_THREAD_RE
    from apps.mesh import views
    longest = max(n for n in range(1, 1024) if MESH_THREAD_RE.fullmatch("a" * n))
    assert views.OWED_THREAD_MAX == longest
    p = _peer(owner, "owed-thr-cap")
    ok = client.get(f"/v1/mesh/owed?thread={'a' * longest}", **_auth(p["peer_id"]))
    assert ok.status_code == 200 and ok.json()["filters"] == {"thread": "a" * longest}, ok.content
    over = client.get(f"/v1/mesh/owed?thread={'a' * (longest + 1)}", **_auth(p["peer_id"]))
    assert over.status_code == 422 and over.json()["error"] == "E_VALIDATION", over.content
    padded = client.get(f"/v1/mesh/owed?thread=%20{'a' * longest}%20", **_auth(p["peer_id"]))
    assert padded.status_code == 200, padded.content
    assert padded.json()["filters"] == {"thread": f" {'a' * longest} "}, padded.content
    padded_over = client.get(f"/v1/mesh/owed?thread=%20{'a' * (longest + 1)}%20", **_auth(p["peer_id"]))
    assert padded_over.status_code == 422 and padded_over.json()["error"] == "E_VALIDATION", padded_over.content


def test_awaiting_neither_applies_nor_echoes_a_thread(client, owner):
    """`thread` is owed's filter only (#155): graph.awaiting takes no thread, so awaiting must not echo one it did
    not apply, and a client that checks the echo refuses its answer.

    MUTANT: parse the thread in awaiting too (with_thread=True); it echoes a filter it never applied.
    """
    asker, owing = _peer(owner, "await-thr-asker"), _peer(owner, "await-thr-owing")
    one, other = f"t-{uuid.uuid4().hex[:6]}", f"t-{uuid.uuid4().hex[:6]}"
    keys = {_row(owner, dict(asker, label=asker["name"]), {"to": [owing["name"]], "thread_id": t,
                                                         "requires_ack": True}) for t in (one, other)}
    P.rebuild(owner)
    response = client.get(f"/v1/mesh/awaiting?thread={one}", **_auth(asker["peer_id"]))
    assert response.status_code == 200, response.content
    assert "filters" not in response.json()
    assert {row["hit"]["key"] for row in response.json()["awaiting"]} == keys


def test_owed_and_awaiting_report_when_their_default_page_is_truncated(client, owner):
    """MUTANT: fetch only 100 rows in either view; its root `truncated` lies false.

    A graph read is a bounded prefix, never evidence that older asks do not exist.
    """
    asker, owing = _peer(owner, "trunc-asker"), _peer(owner, "trunc-owing")
    for n in range(101):
        _row(owner, dict(asker, label=asker["name"]), {"to": [owing["name"]], "thread_id": f"t-trunc-{n}",
                             "requires_ack": True})
    from apps.mesh import projection as P
    P.rebuild(owner)
    owed = client.get("/v1/mesh/owed", **_auth(owing["peer_id"]))
    awaiting = client.get("/v1/mesh/awaiting", **_auth(asker["peer_id"]))
    assert owed.status_code == awaiting.status_code == 200
    assert len(owed.json()["owed"]) == len(awaiting.json()["awaiting"]) == 100
    assert owed.json()["truncated"] is True
    assert awaiting.json()["truncated"] is True


def test_the_mentions_route_reads_the_embed_gate_now(client, owner, monkeypatch):
    """Codex on #89 (P1): the route passes the gate as it is at the request.
    MUTANT: the view passes no gate; with the gate open a forbidden row's mention is still hidden."""
    p = _peer(owner, "mention-gate-route")
    key = _row(owner, p, {"body": "see PR #7320"})   # production default: forbidden
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    P.rebuild(owner)

    def keys():
        r = client.get("/v1/mesh/mentions?ref=%237320", **_auth(p["peer_id"]))
        assert r.status_code == 200, r.content
        return [h["hit"]["key"] for h in r.json()["hits"]]
    assert keys() == [key]
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: False)
    assert keys() == []


def test_mentions_by_peer_answers_holders_and_refuses_another_grouping(client, owner, monkeypatch):
    """Divergence 090. MUTANT: ignore `by`; the route answers hits, not holders. MUTANT: accept any `by`; a typo
    silently reads as the plain list."""
    p = _peer(owner, "custody-route")
    digest = "160a" + "f00d" * 15
    built = _row(owner, p, {"body": f"built sha256 {digest}", "type": "status"})
    held = _row(owner, dict(p, label=f"{p['label']}-alias"), {"body": f"holding {digest[:8]}...", "type": "answer"})
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    P.rebuild(owner)
    r = client.get(f"/v1/mesh/mentions?ref=hash:{digest}&by=peer", **_auth(p["peer_id"]))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["by"] == "peer" and body["hits_read"] == 2 and body["truncated"] is False
    (holder,) = body["holders"]
    assert holder["peer_id"] == str(p["peer_id"]) and holder["labels"] == sorted([p["label"], f"{p['label']}-alias"])
    assert holder["mentions"] == 2 and holder["ambiguous"] == 1 and set(holder["keys"]) == {built, held}
    assert holder["types"] == {"answer": 1, "status": 1}
    plain = client.get(f"/v1/mesh/mentions?ref=hash:{digest}", **_auth(p["peer_id"]))
    assert "holders" not in plain.json() and len(plain.json()["hits"]) == 2
    bad = client.get(f"/v1/mesh/mentions?ref=hash:{digest}&by=label", **_auth(p["peer_id"]))
    assert bad.status_code == 422 and bad.json()["error"] == "E_VALIDATION"



def test_brief_owed_excludes_answered_asks_and_counts_open_and_active(client, owner):
    """Two open asks and one active one, so a count that swaps the two states cannot pass.

    MUTANT: remove brief's open/active state predicate; the answered ask appears.
    MUTANT (uxxx T3): swap 'open' and 'active' in owed_counts' state CASE; counts read 1 and 2."""
    owing, asker = _peer(owner, "brief-owing"), _peer(owner, "brief-asker")
    owing = dict(owing, label=owing["name"])
    now = _dt.datetime.now(_dt.timezone.utc)
    open_key, _ = _row_at(owner, asker, {"to": [owing["name"]], "requires_ack": True,
                                          "thread_id": f"brief-open-{uuid.uuid4().hex[:6]}"}, now)
    open_key_2, _ = _row_at(owner, asker, {"to": [owing["name"]], "requires_ack": True,
                                            "thread_id": f"brief-open2-{uuid.uuid4().hex[:6]}"},
                            now + _dt.timedelta(milliseconds=500))
    active_thread = f"brief-active-{uuid.uuid4().hex[:6]}"
    active_key, _ = _row_at(owner, asker, {"to": [owing["name"]], "requires_ack": True,
                                            "thread_id": active_thread}, now + _dt.timedelta(seconds=1))
    _row_at(owner, owing, {"to": [asker["name"]], "thread_id": active_thread},
            now + _dt.timedelta(seconds=2))
    answered_thread = f"brief-answered-{uuid.uuid4().hex[:6]}"
    answered_key, _ = _row_at(owner, asker, {"to": [owing["name"]], "requires_ack": True,
                                              "thread_id": answered_thread}, now + _dt.timedelta(seconds=3))
    _row_at(owner, owing, {"to": [asker["name"]], "thread_id": answered_thread,
                            "in_reply_to": answered_key}, now + _dt.timedelta(seconds=4))
    P.rebuild(owner)
    body = client.get("/v1/mesh/brief", **_auth(owing["peer_id"])).json()
    states = {item["hit"]["key"]: item["state"] for item in body["owed"]["items"]}
    assert states == {open_key: "open", open_key_2: "open", active_key: "active"}
    assert answered_key not in states
    assert body["owed"]["counts"] == {"open": 2, "active": 1}


def test_brief_applies_window_and_limit_before_reporting_truncation_and_counts(client, owner):
    """MUTANT: apply limit before since; an old ask hides a recent one."""
    owing, asker = _peer(owner, "brief-window-owing"), _peer(owner, "brief-window-asker")
    now = _dt.datetime.now(_dt.timezone.utc)
    peer = dict(asker, label=asker["name"])
    _row_at(owner, peer, {"to": [owing["name"]], "requires_ack": True,
                          "thread_id": f"brief-old-{uuid.uuid4().hex[:6]}"}, now - _dt.timedelta(days=2))
    recent = []
    for seconds in (30, 60):
        key, _ = _row_at(owner, peer, {"to": [owing["name"]], "requires_ack": True,
                                       "thread_id": f"brief-recent-{uuid.uuid4().hex[:6]}"},
                         now - _dt.timedelta(minutes=5, seconds=seconds))
        recent.append(key)
    P.rebuild(owner)
    body = client.get("/v1/mesh/brief?days=1&limit=1", **_auth(owing["peer_id"])).json()
    assert body["filters"]["days"] == 1 and len(body["owed"]["items"]) == 1
    assert body["owed"]["items"][0]["hit"]["key"] in set(recent)
    assert body["owed"]["truncated"] is True
    assert body["owed"]["counts"] == {"open": 2, "active": 0}


def test_brief_help_wanted_never_includes_direct_mail(client, owner):
    """MUTANT: drop the broadcast-label predicate; a named recipient becomes help wanted."""
    reader, asker = _peer(owner, "brief-help-reader"), _peer(owner, "brief-help-asker")
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=5)
    direct, _ = _row_at(owner, dict(asker, label=asker["name"]),
                        {"to": [reader["name"]], "requires_ack": True,
                         "thread_id": f"brief-direct-{uuid.uuid4().hex[:6]}"}, now)
    broadcast, _ = _row_at(owner, dict(asker, label=asker["name"]),
                           {"to": ["all"], "requires_ack": True,
                            "thread_id": f"brief-broadcast-{uuid.uuid4().hex[:6]}"}, now)
    P.rebuild(owner)
    items = client.get("/v1/mesh/brief", **_auth(reader["peer_id"])).json()["help_wanted"]["items"]
    assert {item["key"] for item in items} == {broadcast}
    assert direct not in {item["key"] for item in items}


def test_brief_help_wanted_uses_only_head_replies(client, owner):
    """MUTANT: remove the reply head join; a superseded reply incorrectly settles an ask."""
    reader, asker, replier = (_peer(owner, tag) for tag in ("brief-reply-reader", "brief-reply-asker", "brief-replier"))
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=5)
    answered, _ = _row_at(owner, dict(asker, label=asker["name"]),
                           {"to": ["all"], "requires_ack": True, "thread_id": f"brief-answer-{uuid.uuid4().hex[:6]}"}, now)
    superseded, _ = _row_at(owner, dict(asker, label=asker["name"]),
                             {"to": ["all"], "requires_ack": True, "thread_id": f"brief-sup-{uuid.uuid4().hex[:6]}"}, now)
    _row_at(owner, dict(replier, label=replier["name"]),
            {"to": [asker["name"]], "thread_id": "reply", "in_reply_to": answered}, now + _dt.timedelta(minutes=1))
    reply_key, reply_id = _row_at(owner, dict(replier, label=replier["name"]),
            {"to": [asker["name"]], "thread_id": "reply", "in_reply_to": superseded},
            now + _dt.timedelta(minutes=2))
    P.rebuild(owner)
    owner.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (reply_id,))
    keys = {item["key"] for item in client.get("/v1/mesh/brief", **_auth(reader["peer_id"])).json()["help_wanted"]["items"]}
    assert answered not in keys and superseded in keys


def test_brief_help_wanted_excludes_closed_broadcast_asks(client, owner):
    """MUTANT: remove the closes predicate; a closed broadcast ask remains offered."""
    reader, asker, closer = (_peer(owner, tag) for tag in ("brief-close-reader", "brief-close-asker", "brief-closer"))
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=5)
    ask, _ = _row_at(owner, dict(asker, label=asker["name"]),
                     {"to": ["both"], "requires_ack": True, "thread_id": f"brief-close-{uuid.uuid4().hex[:6]}"}, now)
    _row_at(owner, dict(closer, label=closer["name"]),
            {"to": [asker["name"]], "thread_id": "close", "closes": [ask]}, now + _dt.timedelta(minutes=1))
    P.rebuild(owner)
    keys = {item["key"] for item in client.get("/v1/mesh/brief", **_auth(reader["peer_id"])).json()["help_wanted"]["items"]}
    assert ask not in keys


def test_brief_help_wanted_ignores_the_askers_own_follow_up(client, owner):
    """An asker's own reply to its broadcast ask ("any takers?") is not an answer; another seat's is.

    CONTROL: the second ask, answered by another seat, leaves help_wanted.
    MUTANT: drop `sr.created_by_peer_id <> sm.created_by_peer_id` in graph.help_wanted; the follow-up hides the first ask."""
    reader, asker, other = (_peer(owner, tag) for tag in ("brief-bump-reader", "brief-bump-asker", "brief-bump-other"))
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=5)
    thread = f"brief-bump-{uuid.uuid4().hex[:6]}"
    bumped, _ = _row_at(owner, dict(asker, label=asker["name"]),
                        {"to": ["all"], "requires_ack": True, "thread_id": thread}, now)
    _row_at(owner, dict(asker, label=asker["name"]),
            {"to": ["all"], "thread_id": thread, "in_reply_to": bumped}, now + _dt.timedelta(minutes=1))
    answered, _ = _row_at(owner, dict(asker, label=asker["name"]),
                          {"to": ["all"], "requires_ack": True, "thread_id": f"brief-ans-{uuid.uuid4().hex[:6]}"}, now)
    _row_at(owner, dict(other, label=other["name"]),
            {"to": [asker["name"]], "thread_id": "reply", "in_reply_to": answered}, now + _dt.timedelta(minutes=2))
    P.rebuild(owner)
    keys = {item["key"] for item in client.get("/v1/mesh/brief", **_auth(reader["peer_id"])).json()["help_wanted"]["items"]}
    assert bumped in keys and answered not in keys


def test_brief_help_wanted_knows_the_askers_follow_up_under_another_of_its_labels(client, owner):
    """Codex on #91 (major): the asker posts under its name, then bumps the ask under its `peer-<id8>` fallback,
    which is the label this server stamps when no roster is loaded. The bump is the asker's, not an answer.
    CONTROL: a reply signed by another peer, under that peer's fallback label, still settles its ask.
    MUTANT: compare the replies' author labels (`ra.dst IS DISTINCT FROM a.dst`) instead of their signers; the bump
    hides the ask."""
    reader, asker, other = (_peer(owner, tag) for tag in ("brief-label-reader", "brief-label-asker", "brief-label-other"))
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=5)
    bumped, _ = _row_at(owner, dict(asker, label=asker["name"]),
                        {"to": ["all"], "requires_ack": True, "thread_id": f"brief-label-{uuid.uuid4().hex[:6]}"}, now)
    _row_at(owner, dict(asker, label=f"peer-{str(asker['peer_id'])[:8]}"),
            {"to": ["all"], "in_reply_to": bumped, "body": "any takers?"}, now + _dt.timedelta(minutes=1))
    answered, _ = _row_at(owner, dict(asker, label=asker["name"]),
                          {"to": ["all"], "requires_ack": True, "thread_id": f"brief-label-ans-{uuid.uuid4().hex[:6]}"}, now)
    _row_at(owner, dict(other, label=f"peer-{str(other['peer_id'])[:8]}"),
            {"to": [asker["name"]], "in_reply_to": answered}, now + _dt.timedelta(minutes=2))
    P.rebuild(owner)
    keys = {item["key"] for item in client.get("/v1/mesh/brief", **_auth(reader["peer_id"])).json()["help_wanted"]["items"]}
    assert bumped in keys, "the asker's own follow-up under its fallback label settled the ask"
    assert answered not in keys, "control: another peer's reply settles its ask"


def test_help_wanted_lists_an_ask_to_all_and_both_once(owner):
    """Codex on #91 (minor): an ask addressed to `all` and `both` carries an owes_ack edge to each. One ask is one item,
    so with limit 2 the answer is that ask once and `truncated` (a third row) cannot appear.
    CONTROL: two asks in the same window are both listed, so the window and the labels do reach rows.
    MUTANT: drop DISTINCT ON (e.src_id); the one ask fills both slots. The window is a fixed 2001 day no other test
    writes, so rows other tests leave in the shared database cannot answer."""
    from apps.mesh import graph as G
    reader, asker = _peer(owner, "help-once-reader"), _peer(owner, "help-once-asker")
    day = _dt.datetime(2001, 1, 1, 12, 0, tzinfo=_dt.timezone.utc)
    both, _ = _row_at(owner, dict(asker, label=asker["name"]),
                      {"to": ["all", "both"], "requires_ack": True, "thread_id": f"help-once-{uuid.uuid4().hex[:6]}"}, day)
    P.rebuild(owner)
    window = dict(since=day - _dt.timedelta(minutes=1),
                  help_after=_dt.datetime.now(_dt.timezone.utc) - (day + _dt.timedelta(minutes=10)))
    with rls.server_internal(owner):
        got = G.help_wanted(owner, [reader["name"]], limit=2, **window)
    assert [row["key"] for row in got] == [both]
    second, _ = _row_at(owner, dict(asker, label=asker["name"]),
                        {"to": ["all"], "requires_ack": True, "thread_id": f"help-once-2-{uuid.uuid4().hex[:6]}"},
                        day + _dt.timedelta(minutes=1))
    P.rebuild(owner)
    with rls.server_internal(owner):
        got = G.help_wanted(owner, [reader["name"]], limit=2, **window)
    assert [row["key"] for row in got] == [both, second], "control: the window reaches two asks"
    assert set(got[0]) == {"key", "author", "thread_id", "recorded_at", "done_when"}, "the item's fields changed"


def test_brief_does_not_offer_the_callers_broadcast_ask_under_fallback_label(client, owner):
    """MUTANT: compare only the primary label; a fallback-authored own ask leaks back."""
    reader, other = _peer(owner, "brief-own-reader"), _peer(owner, "brief-own-other")
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=5)
    fallback = f"peer-{str(reader['peer_id'])[:8]}"
    own, _ = _row_at(owner, dict(reader, label=fallback),
                     {"to": ["all"], "requires_ack": True, "thread_id": f"brief-own-{uuid.uuid4().hex[:6]}"}, now)
    outside, _ = _row_at(owner, dict(other, label=other["name"]),
                         {"to": ["all"], "requires_ack": True, "thread_id": f"brief-other-{uuid.uuid4().hex[:6]}"}, now)
    P.rebuild(owner)
    keys = {item["key"] for item in client.get("/v1/mesh/brief", **_auth(reader["peer_id"])).json()["help_wanted"]["items"]}
    assert own not in keys and outside in keys


def test_brief_threads_count_only_others_after_last_caller_message_under_any_label(client, owner):
    """MUTANT: use only the primary label; a fallback caller message is not the cutoff."""
    reader, other = _peer(owner, "brief-thread-reader"), _peer(owner, "brief-thread-other")
    now = _dt.datetime.now(_dt.timezone.utc)
    thread = f"brief-thread-{uuid.uuid4().hex[:6]}"
    _row_at(owner, dict(reader, label=reader["name"]), {"to": ["all"], "thread_id": thread}, now)
    _row_at(owner, dict(other, label=other["name"]), {"to": ["all"], "thread_id": thread}, now + _dt.timedelta(seconds=1))
    fallback = f"peer-{str(reader['peer_id'])[:8]}"
    _row_at(owner, dict(reader, label=fallback), {"to": ["all"], "thread_id": thread}, now + _dt.timedelta(seconds=2))
    _row_at(owner, dict(other, label=other["name"]), {"to": ["all"], "thread_id": thread}, now + _dt.timedelta(seconds=3))
    P.rebuild(owner)
    items = client.get("/v1/mesh/brief", **_auth(reader["peer_id"])).json()["threads"]["items"]
    item = next(item for item in items if item["thread_id"] == thread)
    assert item["new"] == 1 and item["last_author"] == other["name"]


def test_brief_and_neighbours_record_the_notice_usage_dimensions(client, owner):
    """MUTANT: always record the base route; notice traffic cannot be separated."""
    reader = _peer(owner, "brief-usage-reader")
    before = {}
    for route in ("brief", "brief_notice", "neighbours_notice"):
        before[route] = owner.execute("SELECT COALESCE(sum(calls), 0) FROM mesh_route_usage "
                                      "WHERE route = %s AND peer_id = %s AND outcome = 'ok'",
                                      (route, reader["peer_id"])).fetchone()[0]
    key = _row(owner, dict(reader, label=reader["name"]),
               {"to": ["all"], "thread_id": f"brief-usage-{uuid.uuid4().hex[:6]}"})
    P.rebuild(owner)
    auth = _auth(reader["peer_id"])
    assert client.get("/v1/mesh/brief", **auth).status_code == 200
    assert client.get("/v1/mesh/brief?via=notice", **auth).status_code == 200
    assert client.get(f"/v1/mesh/neighbours?key={key}&via=notice", **auth).status_code == 200
    after = {route: owner.execute("SELECT COALESCE(sum(calls), 0) FROM mesh_route_usage "
                                  "WHERE route = %s AND peer_id = %s AND outcome = 'ok'",
                                  (route, reader["peer_id"])).fetchone()[0]
             for route in before}
    assert after["brief"] == before["brief"] + 1
    assert after["brief_notice"] == before["brief_notice"] + 1
    assert after["neighbours_notice"] == before["neighbours_notice"] + 1, after


@pytest.mark.parametrize("query", (
    "days=0", "days=91", "days=bad", "limit=0", "limit=101", "limit=bad",
    "help_after_h=0", "help_after_h=169", "help_after_h=bad", "via=push",
    "since=2026-10-04T10:00:00", "since=2026-10-04T10:00:00Z&days=1",
))
def test_brief_refuses_every_bad_parameter_without_clamping(client, owner, query):
    """MUTANT: clamp one bound; that malformed brief request incorrectly returns 200."""
    reader = _peer(owner, "brief-bad")
    response = client.get(f"/v1/mesh/brief?{query}", **_auth(reader["peer_id"]))
    assert response.status_code == 422 and response.json()["error"] == "E_VALIDATION", response.content


def test_brief_uses_the_same_read_gate_as_owed(client, owner):
    """MUTANT: bypass _route for brief; an ungranted peer receives a 200 response."""
    _peer(owner, "brief-gate-owner")
    denied = _peer(owner, "brief-gate-denied", grant=False)
    response = client.get("/v1/mesh/brief", **_auth(denied["peer_id"]))
    assert response.status_code == 403 and response.json()["error"] == "E_PERMISSION_DENIED"


def test_edited_ask_keeps_its_original_owed_time_and_activity(client, owner):
    """MUTANT: use the head edit time; the edited ask becomes open and jumps ahead."""
    owing, asker = _peer(owner, "brief-edit-owing"), _peer(owner, "brief-edit-asker")
    owing = dict(owing, label=owing["name"])
    now = _dt.datetime.now(_dt.timezone.utc)
    original = now - _dt.timedelta(days=5)
    edited_key = f"msg/{int(original.timestamp() * 1000)}-{uuid.uuid4().hex}"
    value = {"to": [owing["name"]], "requires_ack": True, "thread_id": f"brief-edit-{uuid.uuid4().hex[:6]}", "body": "before"}
    _, first = _row_at(owner, dict(asker, label=asker["name"]), value, original, key=edited_key)
    _row_at(owner, owing, {"to": [asker["name"]], "thread_id": value["thread_id"]},
            original + _dt.timedelta(hours=1))
    _edit_row_at(owner, dict(asker, label=asker["name"]), edited_key,
                 value | {"body": "after"}, first, now)
    newer, _ = _row_at(owner, dict(asker, label=asker["name"]),
                         {"to": [owing["name"]], "requires_ack": True,
                          "thread_id": f"brief-newer-{uuid.uuid4().hex[:6]}"}, now - _dt.timedelta(days=1))
    P.rebuild(owner)
    since = (original - _dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    rows = client.get(f"/v1/mesh/owed?state=active&since={since}", **_auth(owing["peer_id"])).json()["owed"]
    edited = next(item for item in rows if item["hit"]["key"] == edited_key)
    assert edited["edited"] is True and edited["answered_in_thread"] is True
    # The same INSTANT, not the same text: the route renders recorded_at in the database session's zone, so a
    # non-UTC session prints 08:04 for 06:04Z (memory-bxxxxx on #91).
    shown = _dt.datetime.fromisoformat(edited["hit"]["recorded_at"].replace("Z", "+00:00"))
    assert shown.replace(microsecond=0) == original.replace(microsecond=0)
    all_rows = client.get(f"/v1/mesh/owed?since={since}", **_auth(owing["peer_id"])).json()["owed"]
    assert [item["hit"]["key"] for item in all_rows][:2] == [newer, edited_key]


def test_035_rollback_removes_brief_usage_rows_and_restores_034_check(owner):
    """MUTANT: omit a new route from the down delete; the restored CHECK rejects rollback."""
    names = ("brief", "brief_notice", "neighbours_notice")
    peer = _peer(owner, "brief-rollback")
    for route in names:
        owner.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) "
                      "VALUES (%s, current_date, %s, 'ok', 1)", (route, peer["peer_id"]))
    try:
        rolled = _sqlmigrate("--rollback-to", "meshychat:34")
        assert rolled.returncode == 0, rolled.stderr
        left = owner.execute("SELECT count(*) FROM mesh_route_usage WHERE route = ANY(%s)",
                             (list(names),)).fetchone()[0]
        check = owner.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                              "conname = 'mesh_route_usage_route_check'").fetchone()[0]
        assert left == 0 and all(f"'{name}'" not in check for name in names), check
    finally:
        restored = _sqlmigrate()
        assert restored.returncode == 0, restored.stderr


@pytest.mark.parametrize("limit", ("0", "101", "bogus"))
def test_mentions_refuses_out_of_range_limits(client, owner, limit):
    """MUTANT: reuse the general graph limit parser; mentions silently changes the request."""
    p = _peer(owner, "mention-limit")
    response = client.get(f"/v1/mesh/mentions?ref=%2373&limit={limit}", **_auth(p["peer_id"]))
    assert response.status_code == 422 and response.json()["error"] == "E_VALIDATION"


@pytest.mark.parametrize("query", ("state=bogus", "state=", "since=2026-10-03T10:00:00",
                                   "thread=", "thread=%20%20", "thread=%00", "thread=t-%00x", "thread=t%0Ax",
                                   "thread=t%7Fx", "thread=t%C2%85x"))
def test_owed_refuses_invalid_state_and_naive_since_with_422(client, owner, query):
    """A malformed bound is refused, never silently broadened to the whole owed list.

    A thread that is blank, or holds a control character (Unicode Cc: C0, DEL, C1), is refused too. A NUL reached
    the `t.dst = %s` comparison and Postgres refuses NUL in text, an uncaught 500 (Codex on #165).

    MUTANT: return an empty filter for an invalid value; this receives 200 instead of 422.
    MUTANT: drop the blank-thread test; `thread=` and `thread=%20%20` answer 200. MUTANT: drop the control
    character test; the NUL cases raise from Postgres and the other three answer 200.
    """
    p = _peer(owner, "bad-owed")
    r = client.get(f"/v1/mesh/owed?{query}", **_auth(p["peer_id"]))
    assert r.status_code == 422 and r.json()["error"] == "E_VALIDATION", r.content


def test_help_wanted_still_lists_an_ask_answered_only_by_a_redacted_reply(owner):
    """pxx's P3-2 on #626: help_wanted's redacted filter had no test. A broadcast ask whose only answer from another
    signer was redacted is still unanswered, so it stays in help_wanted.
    CONTROL: a plain reply from the same signer settles its ask.
    MUTANT: drop the redacted filter from help_wanted's reply check; the first ask disappears.
    The window is a fixed 2002 day no other test writes."""
    from apps.mesh import graph as G
    reader, asker = _peer(owner, "help-red-reader"), _peer(owner, "help-red-asker")
    other = _peer(owner, "help-red-other")
    day = _dt.datetime(2002, 1, 1, 12, 0, tzinfo=_dt.timezone.utc)
    asker_l, other_l = dict(asker, label=asker["name"]), dict(other, label=other["name"])
    redacted_ask, _ = _row_at(owner, asker_l, {"to": ["all"], "requires_ack": True,
                                                "thread_id": f"help-red-{uuid.uuid4().hex[:6]}"}, day)
    _row_at(owner, other_l, {"redacted": True, "reason_code": "test", "redacted_at": day.isoformat(),
                             "to": [asker["name"]], "in_reply_to": redacted_ask}, day + _dt.timedelta(minutes=2))
    plain_ask, _ = _row_at(owner, asker_l, {"to": ["all"], "requires_ack": True,
                                             "thread_id": f"help-red-2-{uuid.uuid4().hex[:6]}"},
                           day + _dt.timedelta(minutes=1))
    _row_at(owner, other_l, {"to": [asker["name"]], "in_reply_to": plain_ask}, day + _dt.timedelta(minutes=3))
    P.rebuild(owner)
    window = dict(since=day - _dt.timedelta(minutes=1),
                  help_after=_dt.datetime.now(_dt.timezone.utc) - (day + _dt.timedelta(minutes=10)))
    with rls.server_internal(owner):
        keys = [row["key"] for row in G.help_wanted(owner, [reader["name"]], limit=5, **window)]
    assert redacted_ask in keys, "an ask answered only by a redacted reply left help_wanted"
    assert plain_ask not in keys, "control: a plain reply from another signer settles its ask"


def test_a_non_boolean_redacted_value_does_not_break_owed_or_awaiting(owner):
    """pxx's P3-1 on #626: '::boolean' raised InvalidTextRepresentation on a reply head whose 'redacted' is not a
    boolean ("abc", "", a list, an object), breaking owed_acks and awaiting. The jsonb form '= true' cannot raise and
    counts only a real tombstone. Reachable only with MESHY_MESH_ACCEPT_UNVERSIONED on ('false' in production).
    MUTANT: back to '::boolean'; this test raises."""
    from apps.mesh import graph as G
    asker, owing = _peer(owner, "red-type-ask"), _peer(owner, "red-type-me")
    me, them = asker["name"] + "-me", asker["name"]
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    asker_l, owing_l = dict(asker, label=them), dict(owing, label=me)
    asks = []
    for i, odd in enumerate(("abc", "", ["x"], {"y": 1})):
        ask, _ = _row_at(owner, asker_l, {"to": [me], "requires_ack": True,
                                          "thread_id": f"red-type-{i}-{uuid.uuid4().hex[:6]}"}, now)
        _row_at(owner, owing_l, {"to": [them], "in_reply_to": ask, "redacted": odd}, now + _dt.timedelta(seconds=5))
        asks.append(ask)
    P.rebuild(owner)
    with rls.server_internal(owner):
        owed = {o.hit.key: o.state for o in G.owed_acks(owner, me, limit=None,
                                                         states=["open", "active", "answered", "closed"])}
        waiting = {w.hit.key: w.state for w in G.awaiting(owner, them, limit=None,
                                                          states=["open", "active", "answered", "closed"])}
    assert all(owed[a] == "answered" and waiting[a] == "answered" for a in asks), "a non-boolean 'redacted' is not a tombstone"
