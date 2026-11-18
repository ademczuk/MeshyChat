"""A node reads the messages it is sent: `read`, `listen --read` and `inbox --bodies` against
the REAL server, and the context anchor's body beside memory.read's answer.

THE LIVE FINDING (2026-09-23, the node-test server at c5457ac, a real remote node on HQB and
a probe node on HQD): a node could not read a message it was sent. The frame carries key,
from_peer_id, to and created_at only, by design; an inbox hit is an addressed_to edge with
no body; the context route's `anchor` had no body; and the client had no way to
call the memory.read skill the server implements. Right after a message arrived, context
also answered `thread_absent: anchor_not_projected` until the edges job ran.

So this file brings up an install the way README.md "Fresh install" does (the harness is
tests/test_fresh_install_runbook.py's: a fresh database, the real ASGI application on a real
socket, the operator's commands) with three nodes: A and B hold write on shared/mesh-chat,
C holds nothing. Every message is sent by the client and read back by the client, so each
read crosses the real invoke pipeline, the real handler and the real result signature.
Models are the deterministic fake; nothing here needs a GPU. Synthetic text only.

EVERY REFUSAL SITS BESIDE ITS CONTROL (docs/V1-DESIGN-RULES.md C1): C's refused read beside
B's successful one, an unknown key beside a known one, a version that does not exist beside
one that does.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_read.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import socket
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from django.test import override_settings  # noqa: E402

from apps.authapi import ratelimit  # noqa: E402
from tests.fixtures.meshsem_fake import FakeModelHost  # noqa: E402
from tests.test_fresh_install_runbook import (Runbook, _empty_database,  # noqa: E402
                                              _jwt_pair, _serve)
from tools.meshy_node import node  # noqa: E402

NS = "shared/mesh-chat"
WIKI = "shared/wiki"
DENIED = "peer {peer} has no active grant on namespace 'shared/mesh-chat'"


def _settle(rb: Runbook) -> None:
    """One scheduler tick of the edges and embed jobs, now: `--run-now` makes each due (it
    moves the schedule and bypasses nothing), then the tick runs them, as in the runbook."""
    for job in ("mesh_edges_catch_up", "mesh_embed_catch_up"):
        rb.manage("run_jobs", "--job", job, "--run-now", control=True)
    out, _ = rb.manage("run_jobs", control=True)
    assert "ok    mesh_edges_catch_up" in out, out


@pytest.fixture(scope="module")
def mesh(tmp_path_factory):
    """An install with an operator and three minted nodes: A and B granted write, C not.
    For the anchor-body tests (the review of fin/read): K, granted write, whose key a test
    revokes; and D, minted under a SECOND owner in another trust group (`bootstrap_owner
    --additional-owner`), holding a bounded read grant on the namespace and no skill
    grant, which is exactly what memory.read's invoke refuses and the namespace allows."""
    tmp_path = tmp_path_factory.mktemp("read")
    fake = FakeModelHost(dims=768)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    ratelimit.reset_all()
    try:
        with pytest.MonkeyPatch.context() as mp, _empty_database("read") as dsn:
            rb = Runbook(f"http://127.0.0.1:{sock.getsockname()[1]}")
            code, out = rb.migrate(dsn)
            assert code == 0, out
            jwt_priv, jwt_pub = _jwt_pair(tmp_path)
            group, group2 = str(uuid.uuid4()), str(uuid.uuid4())
            op, op_pub = tmp_path / "operator.json", tmp_path / "operator.pub"
            code, _ = rb.node("keygen", op, "--public-key-out", op_pub)
            assert code == 0
            op2, op2_pub = tmp_path / "operator2.json", tmp_path / "operator2.pub"
            code, _ = rb.node("keygen", op2, "--public-key-out", op2_pub, control=True)
            assert code == 0
            mp.setenv("MESHY_DSN", dsn)
            with override_settings(MESHY_DSN=dsn):
                out, _ = rb.manage("bootstrap_owner", "--email", "ops@example.test",
                                   "--public-key-file", str(op_pub), "--plane", "dev",
                                   "--trust-group", group)
                owner = json.loads(out)
                rb.manage("bootstrap_memory_peer", "--trust-group", group,
                          "--owner-email", "memory@example.test")
                out, _ = rb.manage("bootstrap_owner", "--email", "other@example.test",
                                   "--public-key-file", str(op2_pub), "--plane", "dev",
                                   "--trust-group", group2, "--additional-owner",
                                   "--peer-name", "operator-2",   # divergence 065: names are addresses
                                   control=True)
                owner2 = json.loads(out)
            env = {
                "MESHY_DSN": dsn, "MESHYCHAT_JWT_ISSUER": "meshychat-read-test",
                "MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": str(jwt_priv),
                "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": str(jwt_pub),
                "MESHYCHAT_STATE_SECRET": os.urandom(32).hex(), "MESHY_ALLOWED_HOSTS": "127.0.0.1",
                "MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID": owner["peer_id"],
                "MESHY_EMBED_FORBIDDEN_ROWS": "true",
                "MESHY_MODEL_PROVIDERS": json.dumps([
                    {"id": "embed-fake", "kind": "embed", "base_url": fake.base_url,
                     "model": "embeddinggemma-300m",
                     "space": "embeddinggemma-300m/768/retrieval-v1"},
                    {"id": "rerank-fake", "kind": "rerank", "base_url": fake.base_url,
                     "model": "bge-reranker-v2-m3"}]),
            }
            with _serve(env, mp, sock):
                rb.manage("run_jobs")
                code, bound = rb.node("bind", op, "--peer-id", owner["peer_id"])
                assert code == 0, bound
                code, bound = rb.node("bind", op2, "--peer-id", owner2["peer_id"],
                                      control=True)
                assert code == 0, bound

                # BEFORE the claim: the operator's read and its context are refused alike.
                unclaimed = {
                    "read": rb.node("read", op, "--key", "msg/1-nobody-000000"),
                    "context": rb.node("context", op, "--key", "msg/1-nobody-000000")}

                code, claimed = rb.node("claim", op, "--namespace", NS)
                assert code == 0 and node.succeeded(claimed), claimed
                code, paired = rb.node("pair", op, "--count", "4")
                assert code == 0, paired
                states, peer = {}, {}
                for name, row in zip("ABCK", paired["codes"]):
                    states[name] = tmp_path / f"node{name}.json"
                    code, minted = rb.node("mint", states[name], "--code", row["code"],
                                           "--device-label", f"read-{name.lower()}")
                    assert code == 0, minted
                    peer[name] = minted["peer_id"]
                for name in "ABK":
                    code, granted = rb.node("grant", op, "--namespace", NS, "--grantee",
                                            peer[name], "--permission", "write")
                    assert code == 0 and node.succeeded(granted), granted
                # read --namespace (2026-09-28): A writes shared/wiki, B reads it, C holds
                # nothing there.
                code, claimed = rb.node("claim", op, "--namespace", WIKI)
                assert code == 0 and node.succeeded(claimed), claimed
                for name, permission in (("A", "write"), ("B", "read")):
                    code, granted = rb.node("grant", op, "--namespace", WIKI, "--grantee",
                                            peer[name], "--permission", permission)
                    assert code == 0 and node.succeeded(granted), granted
                code, paired2 = rb.node("pair", op2, "--count", "1", control=True)
                assert code == 0, paired2
                states["D"] = tmp_path / "nodeD.json"
                code, minted = rb.node("mint", states["D"], "--code",
                                       paired2["codes"][0]["code"], "--device-label",
                                       "read-d", control=True)
                assert code == 0, minted
                peer["D"] = minted["peer_id"]
                # Cross-tenant, so bounded (I1): the real memory.grant path, as an operator
                # would issue it.
                expires = (_dt.datetime.now(_dt.timezone.utc)
                           + _dt.timedelta(days=1)).isoformat()
                code, granted = rb.node("grant", op, "--namespace", NS, "--grantee",
                                        peer["D"], "--permission", "read", "--expires-at",
                                        expires, control=True)
                assert code == 0 and node.succeeded(granted), granted
                labels = {}
                for name in "AB":
                    code, box = rb.node("inbox", states[name])
                    assert code == 0, box
                    labels[name] = box["label"]
                yield types.SimpleNamespace(rb=rb, dsn=dsn, states=states, peer=peer,
                                            labels=labels, unclaimed=unclaimed, fake=fake,
                                            owner2=owner2)
    finally:
        ratelimit.reset_all()
        sock.close()
        fake.close()


@pytest.fixture(autouse=True)
def _fresh_limiters():
    ratelimit.reset_all()
    yield


def _send(mesh, who: str, to: str, thread: str, body: str, *extra: str) -> dict:
    code, sent = mesh.rb.node("send", mesh.states[who], "--to", mesh.labels[to], "--thread",
                              thread, "--type", "request", "--author-family", "claude",
                              "--body", body, *extra)
    assert code == 0 and node.succeeded(sent), sent
    return sent


def _read(mesh, who: str, key: str, *extra: str) -> tuple[int, dict]:
    return mesh.rb.node("read", mesh.states[who], "--key", key, *extra)


def test_before_the_claim_read_and_context_refuse_alike(mesh):
    """An unclaimed namespace refuses memory.read (E_NAMESPACE_UNCLAIMED) and the context
    route with the SAME code and message: the route runs memory.read's own predicate."""
    code, read = mesh.unclaimed["read"]
    assert code == 1 and read["error"] == "E_READ_REFUSED", read
    assert read["error_code"] == "E_NAMESPACE_UNCLAIMED"
    code, ctx = mesh.unclaimed["context"]
    assert code == 1 and ctx["http_status"] == 403, ctx
    assert ctx["body"] == {"error": "E_NAMESPACE_UNCLAIMED",
                           "message": read["error_message"]}, (ctx, read)


def test_a_node_reads_a_message_it_was_sent_before_any_job_has_run(mesh):
    """A sends to B; B reads it AT ONCE, before the edges job has projected anything: the
    signed read needs no projection. The printed message is the one A sent, field for
    field. The context route's anchor carries the same body in the same moment, beside
    `thread_absent: anchor_not_projected` (the live finding's second half)."""
    thread = f"ops/read-{uuid.uuid4().hex[:6]}"
    body = "synthetic: the compaction job keeps losing its scheduler lease"
    sent = _send(mesh, "A", "B", thread, body)
    key = sent["key"]

    code, got = _read(mesh, "B", key)
    assert code == 0, got
    value = sent["value"]
    assert {k: got[k] for k in ("key", "version", "from", "to", "cc", "thread_id", "type",
                                "in_reply_to", "body")} == {
        "key": key, "version": 1, "from": value["from"], "to": value["to"], "cc": None,
        "thread_id": thread, "type": "request", "in_reply_to": None, "body": body}
    # `from` is what A claimed; `author` is what the server stamped for A's verified
    # caller (no roster here, so the peer-id fallback), which is why both are printed.
    assert got["author"] == f"peer-{mesh.peer['A'][:8]}" and got["recorded_at"], got

    code, ctx = mesh.rb.node("context", mesh.states["B"], "--key", key)
    assert code == 0, ctx
    assert ctx["thread_absent"] == "anchor_not_projected", ctx["thread_absent"]
    assert ctx["anchor"]["edges_projected"] is False
    assert ctx["anchor"]["body"] == body and ctx["anchor"]["body_truncated"] is False

    # --version names a row: the one that exists reads, the one that does not is refused.
    code, v1 = _read(mesh, "B", key, "--version", "1")
    assert code == 0 and v1 == got, v1
    code, v2 = _read(mesh, "B", key, "--version", "2")
    assert code == 1 and v2["error_code"] == "E_UNKNOWN_KEY", v2
    assert v2["error_message"] == f"no memo at {NS}/{key}@v2"


def test_read_shows_the_ask_the_sender_signed_and_null_for_a_plain_message(mesh):
    """Roadmap item 8.0. `read` is the command every notice tells a seat to run, and until
    2026-10-04 it printed neither `requires_ack` nor `done_when`, so a reader was never told a
    reply was expected. Both come from the signed value, with no projection in between.

    CONTROL: a message sent without the flag prints null for both, so the first half cannot
    pass on a client that prints a constant.
    MUTANT: drop `requires_ack` from node.READ_VALUE_FIELDS; the first assert has a KeyError.
    MUTANT: drop `done_when` from node.READ_VALUE_FIELDS; likewise.
    """
    thread = f"ops/ask-{uuid.uuid4().hex[:6]}"
    ask = _send(mesh, "A", "B", thread, "synthetic: can you confirm the lease window?",
                "--expects-reply", "--done-when", "the window is confirmed in this thread")
    code, got = _read(mesh, "B", ask["key"])
    assert code == 0, got
    assert got["requires_ack"] is True, got
    assert got["done_when"] == "the window is confirmed in this thread", got
    assert got["signature_verified"] is True, got

    plain = _send(mesh, "A", "B", thread, "synthetic: a note that asks for nothing")
    code, got = _read(mesh, "B", plain["key"])
    assert code == 0, got
    assert got["requires_ack"] is None and got["done_when"] is None, got


def test_a_refused_read_exits_1_with_the_servers_error_and_no_body(mesh):
    """C holds no grant: its read is refused with the handler's own code and message, and
    the message text appears nowhere in what it prints. The context route refuses C with
    the SAME message. Controls: B reads the same key; an unknown key is E_UNKNOWN_KEY."""
    body = f"synthetic: only granted peers may read this {uuid.uuid4().hex[:8]}"
    key = _send(mesh, "A", "B", "ops/refusal", body)["key"]

    code, refused = _read(mesh, "C", key)
    assert code == 1 and refused["error"] == "E_READ_REFUSED", refused
    assert refused["error_code"] == "E_PERMISSION_DENIED"
    assert refused["error_message"] == DENIED.format(peer=mesh.peer["C"])
    assert body not in json.dumps(refused)
    code, ctx = mesh.rb.node("context", mesh.states["C"], "--key", key)
    assert code == 1 and ctx == {
        "http_status": 403,
        "body": {"error": "E_PERMISSION_DENIED", "message": refused["error_message"]},
        "code": "E_HTTP", "retryable": False,
    }, ctx

    code, got = _read(mesh, "B", key)
    assert code == 0 and got["body"] == body, "control: the granted reader"
    missing = f"msg/{int(time.time() * 1000)}-nobody-abcdef"
    code, unknown = _read(mesh, "B", missing)
    assert code == 1 and unknown["error_code"] == "E_UNKNOWN_KEY", unknown
    assert unknown["error_message"] == f"no memo at {NS}/{missing}"


def test_listen_read_prints_each_frame_with_the_message_it_points_at(mesh):
    """B listens with read on; A sends two messages. Each frame arrives on one line with
    the message read through the signed path: its body is what A sent, its `from` is A."""
    b_state = node.load_state(mesh.states["B"], mesh.rb.base_url)
    out, err = io.StringIO(), io.StringIO()
    listener = threading.Thread(target=node.listen, args=(mesh.states["B"], b_state),
                                kwargs={"count": 2, "duration": 60, "out": out, "err": err,
                                        "read": True}, daemon=True)
    listener.start()
    deadline = time.monotonic() + 20
    while '"welcome"' not in err.getvalue():
        assert time.monotonic() < deadline, err.getvalue()
        time.sleep(0.05)
    bodies = {}
    for n in (1, 2):
        text = f"synthetic: live read number {n} {uuid.uuid4().hex[:6]}"
        bodies[_send(mesh, "A", "B", "ops/live-read", text)["key"]] = text
    listener.join(60)
    assert not listener.is_alive(), err.getvalue()
    lines = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert sorted(ln["frame"]["key"] for ln in lines) == sorted(bodies), (lines,
                                                                         err.getvalue())
    for ln in lines:
        assert "read_error" not in ln, ln
        assert ln["read"]["key"] == ln["frame"]["key"]
        assert ln["read"]["body"] == bodies[ln["frame"]["key"]]
        assert ln["read"]["from"] == mesh.labels["A"]
        assert ln["read"]["signature_verified"] is True, ln["read"]
        assert ln["read"]["status_verified"] is True, ln["read"]   # B02 phase 0
        assert ln["read"]["author_signed"] is False, "send signs no author"


def test_inbox_bodies_attaches_every_hits_message(mesh):
    """After the edges job runs, B's inbox lists what A sent it; with --bodies each hit
    carries the message read at the hit's version. Control: C, with no grant, gets no
    inbox at all (403), so --bodies adds no way around the namespace grant."""
    sent = {}
    for n in (1, 2):
        text = f"synthetic: inbox body {n} {uuid.uuid4().hex[:6]}"
        sent[_send(mesh, "A", "B", "ops/inbox-bodies", text)["key"]] = text
    _settle(mesh.rb)
    code, box = mesh.rb.node("inbox", mesh.states["B"], "--bodies", "--limit", "200")
    assert code == 0, box
    by_key = {h["key"]: h for h in box["hits"]}
    assert set(sent) <= set(by_key), (sorted(sent), sorted(by_key))
    for hit in box["hits"]:
        assert "read_error" not in hit, hit
        assert hit["read"]["key"] == hit["key"] and hit["read"]["version"] == hit["version"]
    for key, text in sent.items():
        assert by_key[key]["read"]["body"] == text
        assert by_key[key]["read"]["signature_verified"] is True, by_key[key]["read"]

    code, plain = mesh.rb.node("inbox", mesh.states["B"], "--limit", "200")
    assert code == 0 and all("read" not in h for h in plain["hits"]), "control: no flag"
    code, refused = mesh.rb.node("inbox", mesh.states["C"], "--bodies")
    assert code == 1 and refused["http_status"] == 403, refused


def _tamper(mesh, key: str, path: str, text: str) -> None:
    """Edit one stored message the way only the database's owner can: a jsonb path in the
    head row set to `text`, triggers off for this one UPDATE (shared_memory_immutability
    refuses it otherwise). The server then serves, and signs, the edited row."""
    import psycopg
    column, _, rest = path.partition(".")
    with psycopg.connect(mesh.dsn, autocommit=True) as c, c.transaction():
        c.execute("SET LOCAL session_replication_role = replica")
        n = c.execute(f"UPDATE shared_memory SET {column} = jsonb_set({column}, %s, "
                      "to_jsonb(%s::text)) WHERE namespace = %s AND key = %s AND is_head",
                      ("{" + rest.replace(".", ",") + "}", text, NS, key)).rowcount
    assert n == 1, (key, path)


def test_read_verifies_the_authors_signature_and_a_tampered_row_reads_false(mesh):
    """On the real server: B reads what A sent and the client verifies A's signature over
    the write, with the key A publishes (`signature_verified: true`). Then the stored row
    is edited in the database, as a compromised or buggy store would: the server still
    serves it under its own result signature, so `read` exits 0 and prints the edited
    body, and `signature_verified` is false (`E_SIGNATURE_NOT_THE_MESSAGE`: the value is
    not the one A signed). Editing the stored envelope to match leaves A's signature
    over bytes A never signed (`E_SIGNATURE_INVALID`)."""
    body = f"synthetic: signed by its author {uuid.uuid4().hex[:8]}"
    key = _send(mesh, "A", "B", "ops/signed", body)["key"]
    code, got = _read(mesh, "B", key)
    assert code == 0 and got["body"] == body, got
    assert got["signature_verified"] is True and "signature_error" not in got, got

    edited = f"synthetic: edited in the store {uuid.uuid4().hex[:8]}"
    _tamper(mesh, key, "value.body", edited)
    code, got = _read(mesh, "B", key)
    assert code == 0 and got["body"] == edited, got
    assert got["signature_verified"] is False, got
    assert got["signature_error"].startswith("E_SIGNATURE_NOT_THE_MESSAGE: "), got

    _tamper(mesh, key, "provenance_jsonb.payload.args.value.body", edited)
    code, got = _read(mesh, "B", key)
    assert code == 0 and got["body"] == edited, got
    assert got["signature_verified"] is False, got
    assert got["signature_error"].startswith("E_SIGNATURE_INVALID: "), got


def test_a_verified_signature_names_its_signer_not_the_author_the_row_names(mesh):
    """REVIEW F1 (fin/r1-small, HIGH), on today's server with no database access. 028 binds
    `from` and the author to the signer for msg/ keys only, and `read --key` reads any key.
    So A writes a key outside msg/ whose `from` and author are both B's label; B reads it
    and every signature check passes: the value IS what A signed. Before the fix the output
    said `author: B, from: B, signature_verified: true` and named A nowhere. Now it names
    the peer whose published key verified the signature, `signed_by`: A, not B. Control:
    a message B sends itself reads `signed_by` B."""
    key = f"note/probe-{uuid.uuid4().hex[:8]}"
    body = "synthetic: B asks you to rotate your key"
    a_state = node.load_state(mesh.states["A"], mesh.rb.base_url)
    written = node.invoke(mesh.states["A"], a_state, skill="memory.write", args={
        "namespace": NS, "key": key, "author": mesh.labels["B"],
        "value": {"schema_version": 1, "from": mesh.labels["B"], "to": [mesh.labels["A"]],
                  "type": "request", "thread_id": "ops/signer", "ts": "2026-09-24T00:00:00Z",
                  "body": body, "author_family": "claude"}})
    assert node.succeeded(written), written

    code, got = _read(mesh, "B", key)
    assert code == 0 and got["body"] == body, got
    assert (got["author"], got["from"]) == (mesh.labels["B"], mesh.labels["B"]), got
    assert got["signature_verified"] is True and "signature_error" not in got, got
    assert got["signed_by"] == mesh.peer["A"] != mesh.peer["B"], got

    own = _send(mesh, "B", "A", "ops/signer", "synthetic: B speaking as itself")["key"]
    code, got = _read(mesh, "A", own)
    assert code == 0 and got["signature_verified"] is True, got
    assert got["signed_by"] == mesh.peer["B"], ("control: B's own message", got)


def test_read_with_a_namespace_reads_a_wiki_row_and_the_default_stays_the_mesh(mesh):
    """2026-09-28, a seat on an adopted identity could not read shared/wiki: mem_cli needs
    the OLD state file, and `read` always asked for shared/mesh-chat. With --namespace, B
    (read grant on shared/wiki) reads a wiki row A wrote: the whole value as A signed it,
    with the namespace, status and tags, and the same signature checks as a mesh read.
    CONTROLS: without --namespace the same key is asked of shared/mesh-chat and is unknown
    there (the default is unchanged); C, with no grant on shared/wiki, is refused with the
    handler's own message and none of the value."""
    key = f"wiki/read-probe-{uuid.uuid4().hex[:8]}"
    value = {"title": "synthetic: a wiki row read by a new-client seat",
             "body_md": f"synthetic body {uuid.uuid4().hex[:8]}"}
    a_state = node.load_state(mesh.states["A"], mesh.rb.base_url)
    written = node.invoke(mesh.states["A"], a_state, skill="memory.write",
                          args={"namespace": WIKI, "key": key, "value": value,
                                "tags": ["agent-harness"]})   # the server requires one
    assert node.succeeded(written), written

    code, got = _read(mesh, "B", key, "--namespace", WIKI)
    assert code == 0, got
    assert (got["key"], got["version"], got["namespace"]) == (key, 1, WIKI), got
    assert got["value"] == value, got
    assert got["tags"] == ["agent-harness"] and "status" in got and "body" not in got, got
    assert got["signature_verified"] is True and got["signed_by"] == mesh.peer["A"], got

    code, default = _read(mesh, "B", key)
    assert code == 1 and default["error_code"] == "E_UNKNOWN_KEY", default
    assert default["error_message"] == f"no memo at {NS}/{key}", default

    code, refused = _read(mesh, "C", key, "--namespace", WIKI)
    assert code == 1 and refused["error"] == "E_READ_REFUSED", refused
    assert refused["error_code"] == "E_PERMISSION_DENIED", refused
    assert refused["error_message"] == (
        f"peer {mesh.peer['C']} has no active grant on namespace '{WIKI}'"), refused
    assert value["body_md"] not in json.dumps(refused)


# ================================================ B02 phase 0: the row's status fields
#
# Design docs/design/signed-bytes-status.md (fin/design-signed-bytes), 4.1 phase 0, Q13,
# test 18, probe P4: on the real server, a row whose status, tags or author label is not
# what its writer signed read `signature_verified: true` and nothing else. The client now
# derives those fields from the args it verified and prints `status_verified`.


def _tamper_column(mesh, key: str, column: str, value, cast: str) -> None:
    """Set one plain column of the head row, as only the database's owner can: triggers
    off for this one UPDATE (shared_memory_immutability refuses it otherwise). The server
    then serves, and signs, the edited row."""
    import psycopg
    assert column in ("status", "tags", "author_label"), column
    with psycopg.connect(mesh.dsn, autocommit=True) as c, c.transaction():
        c.execute("SET LOCAL session_replication_role = replica")
        n = c.execute(f"UPDATE shared_memory SET {column} = %s::{cast} WHERE namespace = %s "
                      "AND key = %s AND is_head", (value, NS, key)).rowcount
    assert n == 1, (key, column)


def test_read_checks_the_status_tags_and_author_the_writer_signed(mesh):
    """Probe P4 on the real server. A writes three rows, each signing status OPEN, tags
    [ops, lease] and its own label as author; B reads each: `status_verified` and
    `author_signed` true (the control). Then ONE column of each row is edited in the store:
    status, tags, author_label. B's read of the edited row still reads
    `signature_verified: true` (the value is what A signed: P4's finding) and now
    `status_verified: false`, E_STATUS_CONTRADICTS_SIGNATURE, naming the column.
    Kills: the check not wired into `read`; any of the three fields not compared."""
    a_state = node.load_state(mesh.states["A"], mesh.rb.base_url)
    edits = {"status": ("CONFIRMED", "text"), "tags": (["ops"], "text[]"),
             "author_label": (mesh.labels["B"], "text")}
    keys = {}
    for field in edits:
        keys[field] = f"note/status-{field.replace('_', '-')}-{uuid.uuid4().hex[:8]}"
        written = node.invoke(mesh.states["A"], a_state, skill="memory.write", args={
            "namespace": NS, "key": keys[field], "status": "OPEN", "tags": ["ops", "lease"],
            "author": mesh.labels["A"],
            "value": {"schema_version": 1, "from": mesh.labels["A"], "to": [mesh.labels["B"]],
                      "type": "note", "thread_id": "ops/status", "ts": "2026-09-24T00:00:00Z",
                      "body": f"synthetic: status probe for {field}",
                      "author_family": "claude"}})
        assert node.succeeded(written), written

    for field, (changed, cast) in edits.items():
        code, got = _read(mesh, "B", keys[field])
        assert code == 0 and got["signature_verified"] is True, got
        assert got["status_verified"] is True and got["author_signed"] is True, (
            "control: the row as A signed it", got)
        assert "status_error" not in got and got["author"] == mesh.labels["A"], got

        _tamper_column(mesh, keys[field], field, changed, cast)
        code, got = _read(mesh, "B", keys[field])
        assert code == 0 and got["signature_verified"] is True, ("P4: still true", got)
        assert got["status_verified"] is False, got
        assert got["author_signed"] is (field != "author_label"), got
        assert got["status_error"].startswith(
            f"E_STATUS_CONTRADICTS_SIGNATURE: {field}: "), got["status_error"]


def test_a_status_nobody_signed_reads_as_a_default_that_differs(mesh):
    """A `send` signs namespace, key and value only, so the row's status is memory.write's
    default (null outside shared/wiki) and its author label the server's stamp. B's read:
    `status_verified: true`, `author_signed: false`. The status column edited to CONFIRMED:
    false, E_STATUS_DEFAULT_DIFFERS (the writer signed no status, so it is reported as a
    default, never as a contradiction of A's signature), in `read` and in `inbox --bodies`
    alike. Kills: every disagreement reported as a contradiction; the check skipped for a
    field the writer did not sign."""
    key = _send(mesh, "A", "B", "ops/default-status",
                f"synthetic: nobody signed a status {uuid.uuid4().hex[:8]}")["key"]
    code, got = _read(mesh, "B", key)
    assert code == 0 and got["status_verified"] is True, ("control", got)
    assert got["author_signed"] is False, got

    _tamper_column(mesh, key, "status", "CONFIRMED", "text")
    code, got = _read(mesh, "B", key)
    assert code == 0 and got["signature_verified"] is True, got
    assert got["status_verified"] is False, got
    assert got["status_error"].startswith("E_STATUS_DEFAULT_DIFFERS: status: "), got

    _settle(mesh.rb)
    code, box = mesh.rb.node("inbox", mesh.states["B"], "--bodies", "--limit", "200")
    assert code == 0, box
    (hit,) = [h for h in box["hits"] if h["key"] == key]
    assert hit["read"]["status_verified"] is False, hit
    assert hit["read"]["status_error"].startswith("E_STATUS_DEFAULT_DIFFERS: status: ")


# ================================================ the anchor's body beside the REAL read
#
# THE FINDING (review of fin/read, adversarial, on a fresh install like this one): context
# showed the anchor's body to callers memory.read refused, because memory.read is reached
# through the invoke pipeline and context applied only handle_read's namespace predicate.
# Here the oracle is the client's real `read` (the invoke pipeline, the handler, the
# signed result) for the same caller and key; the anchor must show the body exactly when
# it succeeds. DPoP is ENFORCED on this install (the runbook environment leaves
# MESHYCHAT_DPOP_ENFORCE unset, and its default is on).


def _context(mesh, who: str, key: str) -> tuple[int, dict]:
    return mesh.rb.node("context", mesh.states[who], "--key", key, "--no-rerank")


def _skill_grant(mesh, grantee_owner_id: str) -> str:
    """An active memory.read skill grant from the memory authority's owner: what the
    invoke path asks of a caller outside its tenant and trust group."""
    import psycopg
    with psycopg.connect(mesh.dsn, autocommit=True) as c:
        (mem_peer, mem_owner), = c.execute(
            "SELECT p.id, p.owner_id FROM skills s JOIN peers p ON p.id = s.peer_id "
            "WHERE s.name = 'memory.read' AND s.is_current AND s.deleted_at IS NULL"
        ).fetchall()
        (grant_id,), = c.execute(
            "INSERT INTO skill_grants (grantor_owner_id, grantee_owner_id, granter_peer_id,"
            " skill_name, source) VALUES (%s,%s,%s,'memory.read','direct') RETURNING id",
            (str(mem_owner), grantee_owner_id, str(mem_peer))).fetchall()
    return str(grant_id)


def test_a_peer_memory_read_refuses_by_tenancy_gets_no_anchor_body(mesh):
    """D (another owner, another trust group, a bounded read grant on the namespace, no
    skill grant): its `read` is refused by the invoke path's tenancy gate (403), and its
    context withholds the anchor's body (`not_authorized`), the text nowhere in what it
    prints. Control, one variable apart: with a memory.read skill grant to D's owner, D's
    `read` succeeds and the anchor shows the same body.
    Kills: the authorize() step removed from pipeline.refusal_without_envelope."""
    body = f"synthetic: cross tenant probe {uuid.uuid4().hex[:8]}"
    key = _send(mesh, "A", "B", "ops/tenancy", body)["key"]

    code, refused = _read(mesh, "D", key)
    assert code == 1 and refused == {
        "http_status": 403,
        "body": {"error": "E_FORBIDDEN", "message": "forbidden"},
        "code": "E_HTTP", "retryable": False,
    }, refused
    code, ctx = _context(mesh, "D", key)
    assert code == 0, ctx
    assert ctx["anchor"]["body"] is None and ctx["anchor"]["body_truncated"] is None
    assert ctx["anchor"]["body_withheld"] == "not_authorized", ctx["anchor"]
    assert body not in json.dumps(ctx)

    grant_id = _skill_grant(mesh, mesh.owner2["owner_id"])
    try:
        code, got = _read(mesh, "D", key)
        assert code == 0 and got["body"] == body, ("control: the skill grant", got)
        code, ctx = _context(mesh, "D", key)
        assert code == 0 and ctx["anchor"]["body"] == body, ctx["anchor"]
        assert ctx["anchor"]["body_withheld"] is None
    finally:
        import psycopg
        with psycopg.connect(mesh.dsn, autocommit=True) as c:
            c.execute("UPDATE skill_grants SET revoked_at = now() WHERE id = %s",
                      (grant_id,))


def test_a_bearer_token_without_its_key_reads_no_anchor_body(mesh):
    """memory.read is a DPoP-bound POST: B's bearer token alone, with no proof, is refused
    at the invoke route (403 E_FORBIDDEN). So is the anchor's body on a context GET that
    carries no proof (`no_proof_of_possession`), although the route still answers 200.
    Control: B's `context` command, which proves possession, shows it; so does `read`.
    Kills: the view's possession check removed, and the client's proof dropped."""
    body = f"synthetic: key holders only {uuid.uuid4().hex[:8]}"
    key = _send(mesh, "A", "B", "ops/possession", body)["key"]
    b_state = node.load_state(mesh.states["B"], mesh.rb.base_url)

    # The oracle: memory.read's POST with a valid signed envelope and NO DPoP proof.
    envelope = node.wire.call_envelope(
        seed=node._seed(b_state), kid=b_state["kid"], caller_peer_id=b_state["peer_id"],
        target_peer_id=b_state["memory_peer_id"], skill="memory.read",
        args={"namespace": NS, "key": key})
    status, answer = node.http_json(
        "POST", mesh.rb.base_url,
        f"/v1/agents/{b_state['memory_peer_id']}/skills/memory.read/invoke",
        headers=node._bearer(node.fresh_jwt(mesh.states["B"], b_state)),
        body={"envelope": envelope, "mode_override": "async",
              "call_id": envelope["payload"]["call_id"]})
    assert (status, answer) == (403, {"error": "E_FORBIDDEN", "message": "forbidden"}), \
        (status, answer)

    bare = node._get(mesh.states["B"], b_state, "/v1/mesh/context",
                     {"key": key, "k": 3, "rerank": 0})
    assert bare["anchor"]["body"] is None, bare["anchor"]
    assert bare["anchor"]["body_withheld"] == "no_proof_of_possession"
    assert body not in json.dumps(bare)

    code, ctx = _context(mesh, "B", key)
    assert code == 0 and ctx["anchor"]["body"] == body, ("control: proven", ctx["anchor"])
    code, got = _read(mesh, "B", key)
    assert code == 0 and got["body"] == body, "control: read"


def test_a_revoked_signing_key_reads_no_anchor_body(mesh):
    """K's signing key revoked (grace passed) while its JWT is still valid: its `read` is
    refused at the envelope check (401 E_SIG_BAD), and its context withholds the anchor's
    body (`no_usable_signing_key`). Control: both, before the revoke.
    Kills: the usable-key step removed from pipeline.refusal_without_envelope."""
    import psycopg

    body = f"synthetic: key revoked probe {uuid.uuid4().hex[:8]}"
    key = _send(mesh, "A", "B", "ops/revoked-key", body)["key"]
    code, got = _read(mesh, "K", key)
    assert code == 0 and got["body"] == body, "control: before the revoke"
    code, ctx = _context(mesh, "K", key)
    assert code == 0 and ctx["anchor"]["body"] == body, "control: before the revoke"
    # K's OWN message, for the reader's signature check after the revoke (below).
    code, k_sent = mesh.rb.node("send", mesh.states["K"], "--to", mesh.labels["B"],
                                "--thread", "ops/revoked-key", "--type", "note",
                                "--author-family", "claude", "--body", body)
    assert code == 0 and node.succeeded(k_sent), k_sent
    code, got = _read(mesh, "B", k_sent["key"])
    assert code == 0 and got["signature_verified"] is True, ("control: K's key live", got)
    assert got["status_verified"] is True, ("control: K's key live", got)

    with psycopg.connect(mesh.dsn, autocommit=True) as c:
        revoked = c.execute("UPDATE peer_keys SET active = FALSE, revoked_at = now() - "
                            "interval '1 minute' WHERE peer_id = %s",
                            (mesh.peer["K"],)).rowcount
    assert revoked == 1
    code, refused = _read(mesh, "K", key)
    assert code == 1 and refused["http_status"] == 401, refused
    assert refused["body"]["error"] == "E_SIG_BAD" and body not in json.dumps(refused)
    code, ctx = _context(mesh, "K", key)
    assert code == 0 and ctx["anchor"]["body"] is None, ctx["anchor"]
    assert ctx["anchor"]["body_withheld"] == "no_usable_signing_key"
    assert body not in json.dumps(ctx)

    # B reads K's message after the revoke: the server still serves it, and the client can
    # no longer find K's key under that kid among the keys K publishes, so it says it
    # verified nothing rather than trusting the key the row itself names.
    code, got = _read(mesh, "B", k_sent["key"])
    assert code == 0 and got["body"] == body, got
    assert got["signature_verified"] is False, got
    assert got["signature_error"].startswith("E_SIGNATURE_KEY_UNAVAILABLE: "), got
    # B02 phase 0 (design test 18): nothing verified, so no status was compared, and the
    # reason is the signature's own code, not a contradiction.
    assert got["status_verified"] is False and got["author_signed"] is False, got
    assert got["status_error"].startswith("E_SIGNATURE_KEY_UNAVAILABLE: "), got


def test_zz_a_memory_read_skill_that_does_not_resolve_reads_no_anchor_body(mesh):
    """LAST in this file: it soft-deletes the memory authority's memory.read, and puts it
    back. A's `read` is then refused (404, the skill does not resolve) and its context
    withholds the anchor's body (`skill_not_found`). Control: both, before the delete.
    Kills: the resolve step removed from pipeline.refusal_without_envelope."""
    import psycopg

    body = f"synthetic: skill deleted probe {uuid.uuid4().hex[:8]}"
    key = _send(mesh, "A", "B", "ops/skill-deleted", body)["key"]
    code, got = _read(mesh, "A", key)
    assert code == 0 and got["body"] == body, "control"
    code, ctx = _context(mesh, "A", key)
    assert code == 0 and ctx["anchor"]["body"] == body, "control"
    with psycopg.connect(mesh.dsn, autocommit=True) as c:
        deleted = c.execute("UPDATE skills SET deleted_at = now() WHERE name = 'memory.read' "
                            "AND deleted_at IS NULL RETURNING id").fetchall()
    assert len(deleted) == 1
    try:
        code, refused = _read(mesh, "A", key)
        assert code == 1 and refused["http_status"] == 404, refused
        code, ctx = _context(mesh, "A", key)
        assert code == 0 and ctx["anchor"]["body"] is None, ctx["anchor"]
        assert ctx["anchor"]["body_withheld"] == "skill_not_found"
        assert body not in json.dumps(ctx)
    finally:
        with psycopg.connect(mesh.dsn, autocommit=True) as c:
            c.execute("UPDATE skills SET deleted_at = NULL WHERE id = %s", (deleted[0][0],))


def test_the_readme_says_each_error_field_is_absent_when_its_check_is_true():
    """A real node (txxxx) asked, reading `read`'s output: is `signature_error` absent, or
    null, when `signature_verified` is true? The code leaves each error field out when its
    check is true (node.read, wire.status_check; pinned by this file's reads and
    tests/test_meshy_node_status.py). The README's read section says so in so many words."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("**A frame and an inbox hit are pointers.**", 1)[1]
    section = section.split("* `listen --read`", 1)[0]
    flat = " ".join(section.split())
    for field, check in (("signature_error", "signature_verified"),
                         ("status_error", "status_verified")):
        sentence = (f"`{field}` is absent when `{check}` is true: it is present only when "
                    f"`{check}` is false.")
        assert sentence in flat, (sentence, flat)
