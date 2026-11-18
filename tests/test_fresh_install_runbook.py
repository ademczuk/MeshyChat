"""README.md "Fresh install", run in-process: an EMPTY database to two nodes chatting, no SQL.

THE REGRESSION THIS EXISTS FOR (divergence 025). On 2026-09-23 the documented fresh-install
path could not produce a mesh a node could write. Attempt 0 of the end-to-end run executed
what the repository then documented, literally:

  * `bootstrap_memory_peer` first, as bootstrap_owner's help implied ("name the memory
    authority owner's group ... its trust_group_id is on dashboard_users", a SQL read), and
    then `bootstrap_owner --plane dev` REFUSED: "an owner already exists", because the
    memory authority's owner is an owner;
  * in the other order, the install came up, a node was minted, and then bootstrap_owner's
    step 4, "signed by this key, call namespace.claim for shared/mesh-chat and memory.grant
    permission=write to each node", had no command to do it: `python -m tools.meshy_node
    claim` was "invalid choice", manage.py had nothing, and the node's inbox and send both
    answered E_NAMESPACE_UNCLAIMED. The first end-to-end run got past exactly that point
    with operator SQL.

So this file runs the runbook's command sequence end to end, as its commands: the migrator's
`main`, `call_command` for the management commands, `tools.meshy_node`'s `cli.main` for the
operator and the nodes, against the real ASGI application served on a real socket
(uvicorn, in a thread of this process) and a fresh database. Models are the deterministic
fake (tests/fixtures/meshsem_fake.py) over real HTTP; nothing here needs a GPU. The live
record against HQB's models is outside the suite.

ZERO SQL WRITES. Every row is written by a command or by a route a command calls. The test
reads the database only through `_read`, whose session is `default_transaction_read_only`,
so a write from this file cannot happen by accident. (Creating and dropping the empty
database is the DBA's prerequisite, the runbook's `createdb`, not a write to the install.)

THE SERVER'S ENVIRONMENT IS THE RUNBOOK'S. meshychat/settings.py reads the JWT variables at
import, so the served process cannot be re-imported here; `_serve` feeds the runbook's
environment through the SAME parsers settings.py and the minter call
(`jwt_material_from_env`, `AuthConfig.from_env`) and exports the rest, which are read at
call time. `MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID` is read by apps/memoryskills/handlers at
import, so it goes through that module's own setter.

EVERY REFUSAL SITS BESIDE ITS CONTROL (docs/V1-DESIGN-RULES.md C1): a bind to the wrong
peer beside the right one; a node that tries to claim beside the operator's claim; a node's
send before its grant beside the same send after it.

Run:  .venv/Scripts/python.exe -m pytest tests/test_fresh_install_runbook.py -q -p no:cacheprovider
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.test import override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.authapi import ratelimit  # noqa: E402
from apps.calls import nonce_ledger  # noqa: E402
from apps.core import sqlmigrate  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402
from tests.fixtures.meshsem_fake import FakeModelHost  # noqa: E402
from tools.meshy_node import cli, node, sendguard  # noqa: E402

TEST_DSN = os.environ.get("MESHY_TEST_DSN",
                          "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
MEMORY_PEER = node.DEFAULT_MEMORY_PEER_ID
T1, T2 = "ops/lease-renewal", "ops/embed-throughput"

#: The runbook's commands, in the order their FIRST use appears in README.md "Fresh
#: install". The end-to-end test records what it runs and must match this; the README test
#: reads the README and must match it too. Change the runbook and one of them goes red.
RUNBOOK = (
    "python apps/core/sqlmigrate.py",
    "python apps/core/sqlmigrate.py --check",
    "python -m tools.meshy_node keygen",
    "python manage.py bootstrap_owner",
    "python manage.py bootstrap_memory_peer",
    "python manage.py run_jobs",
    "python manage.py run_call_worker",
    "python -m tools.meshy_node bind",
    "python -m tools.meshy_node claim",
    "python -m tools.meshy_node pair",
    "python -m tools.meshy_node mint",
    "python -m tools.meshy_node grant",
    "python -m tools.meshy_node inbox",
    "python -m tools.meshy_node listen",
    "python -m tools.meshy_node send",
    "python -m tools.meshy_node search",
    "python -m tools.meshy_node context",
    "python -m tools.meshy_node neighbours",
    "python -m tools.meshy_node thread",
    "python -m tools.meshy_node read",
)


# =============================================================================== harness

def _admin_dsn() -> str:
    return TEST_DSN.rsplit("/", 1)[0] + "/postgres"


@contextlib.contextmanager
def _empty_database(tag: str):
    """The runbook's prerequisite, `createdb`: an EMPTY database, dropped afterwards."""
    name = f"meshy_fresh_{tag}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield TEST_DSN.rsplit("/", 1)[0] + "/" + name
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _read(dsn: str, sql: str, params=()) -> list:
    """The ONLY way this file touches the database, and it cannot write."""
    with psycopg.connect(dsn, autocommit=True,
                         options="-c default_transaction_read_only=on") as conn:
        return conn.execute(sql, params).fetchall()


def _jwt_pair(tmp_path: Path) -> tuple[Path, Path]:
    """The runbook's `openssl genpkey -algorithm ed25519` and `openssl pkey -pubout`: the
    same PKCS8 / SubjectPublicKeyInfo PEMs, written with the library the suite already has."""
    sk = Ed25519PrivateKey.generate()
    priv, pub = tmp_path / "jwt.pem", tmp_path / "jwt.pub.pem"
    priv.write_bytes(sk.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    pub.write_bytes(sk.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv, pub


class Runbook:
    """Runs one runbook command and records its stem, so the test can prove it ran the
    README's sequence and nothing it left out. `control=True` runs a command the runbook
    does not contain (a deliberate refusal) without recording it."""

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url
        self.stems: list[str] = []

    def _note(self, stem: str, control: bool) -> None:
        if not control and stem not in self.stems:
            self.stems.append(stem)

    def migrate(self, dsn: str, *extra: str) -> tuple[int, str]:
        self._note(" ".join(["python apps/core/sqlmigrate.py", *extra]), False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sqlmigrate.main(["--dsn", dsn, *extra])
        return code, out.getvalue()

    def manage(self, name: str, *args: str, control: bool = False) -> tuple[str, str]:
        self._note(f"python manage.py {name}", control)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                call_command(name, *args, stdout=out, stderr=err)
            except SystemExit as exc:          # run_jobs exits with its tick's code
                assert exc.code == 0, (name, exc.code, out.getvalue(), err.getvalue())
        return out.getvalue(), err.getvalue()

    def node(self, sub: str, state: Path, *args, control: bool = False) -> tuple[int, object]:
        self._note(f"python -m tools.meshy_node {sub}", control)
        if sub == "send":
            # The client refuses a seventh message to one recipient in 30 minutes (divergence 068). These suites send in
            # bursts to test delivery, not volume, so each send starts from an empty ledger. The guard itself has no
            # bypass and is tested in tests/test_meshy_node_send_guard.py.
            sendguard.reset_ledgers(Path(state))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([sub, "--base-url", self.base_url, "--state-file", str(state),
                             *map(str, args)])
        text = out.getvalue().strip()
        try:
            body = json.loads(text) if text else json.loads(err.getvalue().strip() or "null")
        except ValueError:
            body = text or err.getvalue()
        return code, body


@contextlib.contextmanager
def _serve(env: dict, monkeypatch, sock: socket.socket):
    """The ASGI application on `sock`, configured by the runbook's environment."""
    import uvicorn

    from apps.mesh import graph as mesh_graph
    from apps.wsgw import mesh_notify
    from meshychat.asgi import application

    # Step 6 reads the inbox before the edges job ticks and again right after it, and expects
    # the coverage pair to move from projected 0 to 7. Production holds the pair COVERAGE_TTL_S
    # (divergence 107); this server, and only this one, holds nothing, so those reads stay exact.
    monkeypatch.setattr(mesh_graph, "COVERAGE_TTL_S", 0)
    jwt = authconfig.jwt_material_from_env(env)
    for name in ("MESHY_DSN", "MESHYCHAT_STATE_SECRET", "MESHY_MODEL_PROVIDERS",
                 "MESHY_EMBED_FORBIDDEN_ROWS", "MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID"):
        monkeypatch.setenv(name, env[name])
    settings_cm = override_settings(
        MESHY_DSN=env["MESHY_DSN"], MESHY_JWT_ISSUER=jwt.issuer,
        MESHY_JWT_PUBLIC_KEY_PEM=jwt.public_key_pem, MESHY_DPOP_ENFORCE=jwt.dpop_enforce,
        # The parser settings.py runs, over the runbook's environment rather than this
        # process's: the replay ledger is whatever the runbook leaves it (divergence 036).
        MESHY_ENVELOPE_NONCE_ENFORCE=nonce_ledger.enforce_from_env(env),
        ALLOWED_HOSTS=env["MESHY_ALLOWED_HOSTS"].split(","), DEBUG=False)
    before_config = authconfig.get_config()
    before_authority = handlers.BOOTSTRAP_AUTHORITY_PEER_ID
    # log_config=None: uvicorn must not reconfigure this process's logging for the tests
    # that run after this one.
    server = uvicorn.Server(uvicorn.Config(application, log_config=None, lifespan="off",
                                           access_log=False))
    server.install_signal_handlers = lambda: None
    with settings_cm:
        authconfig.set_config(authconfig.AuthConfig.from_env(env))
        handlers.set_bootstrap_authority(env["MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID"])
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while not server.started:
                assert time.monotonic() < deadline, "uvicorn did not start"
                time.sleep(0.05)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(15)
            mesh_notify.BRIDGE.stop()
            authconfig.set_config(before_config)
            handlers.set_bootstrap_authority(before_authority)


@pytest.fixture(autouse=True)
def _fresh_limiters():
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


def _ok(result) -> bool:
    return isinstance(result, dict) and node.succeeded(result)


# =============================================================================== the runbook

def test_the_fresh_install_runbook_takes_an_empty_database_to_two_nodes_chatting(
        tmp_path, monkeypatch):
    fake = FakeModelHost(dims=768)
    # The runbook fixes BASE (where the server WILL listen) before keygen records it, so the
    # port is reserved first and handed to the server in step 4.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        with _empty_database("runbook") as dsn:
            _walk_the_runbook(dsn, tmp_path, monkeypatch, fake, sock)
    finally:
        sock.close()
        fake.close()


def _walk_the_runbook(dsn: str, tmp_path: Path, monkeypatch, fake: FakeModelHost,
                      sock: socket.socket) -> None:
    rb = Runbook(f"http://127.0.0.1:{sock.getsockname()[1]}")
    assert _read(dsn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == [(0,)]

    # ---- 1. schema
    code, out = rb.migrate(dsn)
    assert code == 0 and out.strip().splitlines()[-1].startswith("OK:"), out
    code, out = rb.migrate(dsn, "--check")
    assert code == 0 and out.count(" 0 missing") == 2, out

    # ---- 2. the server's JWT key pair
    jwt_priv, jwt_pub = _jwt_pair(tmp_path)

    # ---- 3. operator key, FIRST owner (dev plane), memory authority in the same group
    group = str(uuid.uuid4())
    op, op_pub = tmp_path / "operator.json", tmp_path / "operator.pub"
    code, keygen = rb.node("keygen", op, "--public-key-out", op_pub)
    assert code == 0 and op_pub.read_text(encoding="ascii").strip() == keygen["public_key"]
    monkeypatch.setenv("MESHY_DSN", dsn)
    with override_settings(MESHY_DSN=dsn):
        out, err = rb.manage("bootstrap_owner", "--email", "ops@example.test",
                             "--public-key-file", str(op_pub), "--plane", "dev",
                             "--trust-group", group, "--tier", "enterprise")
        owner = json.loads(out)
        assert owner["tier"] == "enterprise", owner
        assert owner["peer_kid"] == keygen["peer_kid"], "bootstrap_owner registered this key"
        assert "tools.meshy_node claim" in err and "tools.meshy_node grant" in err, err
        out, _ = rb.manage("bootstrap_memory_peer", "--trust-group", group,
                           "--owner-email", "memory@example.test")
        assert f"trust group {group}" in out, out
    owners = _read(dsn, "SELECT email, plane, trust_group_id::text, tier FROM dashboard_users "
                        "ORDER BY created_at")
    # The operator's owner is enterprise (divergence 028, T: no five-node cap on a
    # self-hosted mesh); the memory authority's service owner keeps the column default.
    assert owners == [("ops@example.test", "dev", group, "enterprise"),
                      ("memory@example.test", "dev", group, "free")], owners

    # ---- 4. the server, with the runbook's environment
    env = {
        "MESHY_DSN": dsn,
        "MESHYCHAT_JWT_ISSUER": "meshychat-runbook-test",
        "MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": str(jwt_priv),
        "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": str(jwt_pub),
        "MESHYCHAT_STATE_SECRET": os.urandom(32).hex(),
        "MESHY_ALLOWED_HOSTS": "127.0.0.1",
        "MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID": owner["peer_id"],
        "MESHY_EMBED_FORBIDDEN_ROWS": "true",
        "MESHY_MODEL_PROVIDERS": json.dumps([
            {"id": "embed-fake", "kind": "embed", "base_url": fake.base_url,
             "model": "embeddinggemma-300m", "space": "embeddinggemma-300m/768/retrieval-v1"},
            {"id": "rerank-fake", "kind": "rerank", "base_url": fake.base_url,
             "model": "bge-reranker-v2-m3"}]),
    }
    assert "MESHYCHAT_DPOP_ENFORCE" not in env, "the runbook leaves DPoP at its default: ON"
    assert "MESHYCHAT_ENVELOPE_NONCE_ENFORCE" not in env, (
        "the runbook leaves the envelope replay ledger at its default: ON")
    with _serve(env, monkeypatch, sock) as base_url:
        assert base_url == rb.base_url
        _operate(rb, dsn, tmp_path, owner, fake)
    assert rb.stems == list(RUNBOOK), rb.stems


def _operate(rb: Runbook, dsn: str, tmp_path: Path, owner: dict, fake: FakeModelHost) -> None:
    op = tmp_path / "operator.json"

    # ---- 5. schedule the jobs: one tick (the cron form of the runbook's step 5)
    out, _ = rb.manage("run_jobs")
    assert "FAIL" not in out, out
    # ... and the call worker beside it (divergence 041 S5): one tick, exit 0, healthy.
    out, _ = rb.manage("run_call_worker", "--once")
    assert "call_deadline_sweep" in out and "FAIL" not in out and "ALERT" not in out, out

    # ---- 6. bind (refusal beside control), claim the mesh and the wiki
    code, body = rb.node("bind", op, "--peer-id", MEMORY_PEER, control=True)
    assert code == 1 and body["http_status"] == 401, body
    assert json.loads(op.read_text(encoding="utf-8"))["status"] == node.STATUS_KEY_ONLY, \
        "a refused bind must write nothing"
    code, bound = rb.node("bind", op, "--peer-id", owner["peer_id"])
    assert code == 0 and bound["status"] == "bound" and bound["kid"] == owner["peer_kid"]
    for ns in ("shared/mesh-chat", "shared/wiki"):
        code, claimed = rb.node("claim", op, "--namespace", ns)
        assert code == 0 and _ok(claimed), claimed
        assert claimed["result"]["owner_peer_id"] == owner["peer_id"]
    # Both claims were signed invokes and the ledger is ON by default, so each one's nonce
    # is on record: the gate ran, and signed envelopes passed it
    # (divergence 036). The bind before them signs in and invokes nothing.
    assert _read(dsn, "SELECT count(*) FROM envelope_nonce_seen WHERE peer_id = %s",
                 (owner["peer_id"],)) == [(2,)]
    assert _read(dsn, "SELECT namespace, owner_peer_id::text, claimant_plane FROM "
                      "shared_memory_namespace ORDER BY namespace") == [
        ("shared/mesh-chat", owner["peer_id"], "dev"), ("shared/wiki", owner["peer_id"], "dev")]

    # ---- 7. pair, mint two nodes, grant each (refusals before the grant)
    code, paired = rb.node("pair", op, "--count", "2")
    assert code == 0 and len(paired["codes"]) == 2 and paired["owner_id"] == owner["owner_id"]
    states = {"A": tmp_path / "nodeA.json", "B": tmp_path / "nodeB.json"}
    peer = {}
    for (name, path), code_row in zip(states.items(), paired["codes"]):
        code, minted = rb.node("mint", path, "--code", code_row["code"],
                               "--device-label", f"runbook-{name.lower()}")
        assert code == 0 and minted["owner_id"] == owner["owner_id"], minted
        peer[name] = minted["peer_id"]

    code, refused = rb.node("claim", states["A"], "--namespace", "shared/squat", control=True)
    assert code == 1 and refused["error_code"] == "E_NOT_BOOTSTRAP_AUTHORITY", refused
    code, refused = rb.node("send", states["A"], "--to", "anyone", "--thread", T1, "--body",
                            "before the grant", control=True)
    assert code == 1 and refused["error_code"] == "E_PERMISSION_DENIED", refused
    code, refused = rb.node("inbox", states["A"], control=True)
    assert code == 1 and refused["http_status"] == 403, refused

    for name in ("A", "B"):
        code, granted = rb.node("grant", op, "--namespace", "shared/mesh-chat",
                                "--grantee", peer[name], "--permission", "write")
        assert code == 0 and _ok(granted), granted
    assert sorted(_read(dsn, "SELECT grantee_peer_id::text, permission, "
                             "granted_by_peer_id::text FROM shared_memory_grant "
                             "WHERE NOT revoked")) == sorted(
        (peer[n], "write", owner["peer_id"]) for n in ("A", "B"))

    # ---- 8. chat
    code, inbox_a = rb.node("inbox", states["A"])
    assert code == 0
    code, inbox_b = rb.node("inbox", states["B"])
    assert code == 0 and inbox_b["hits"] == [] and "coverage" in inbox_b
    a_label, b_label = inbox_a["label"], inbox_b["label"]

    frames, listen_err = io.StringIO(), io.StringIO()
    rb._note("python -m tools.meshy_node listen", False)
    b_state = node.load_state(states["B"], rb.base_url)
    listener = threading.Thread(target=node.listen, args=(states["B"], b_state),
                                kwargs={"count": 6, "duration": 60, "out": frames,
                                        "err": listen_err}, daemon=True)
    listener.start()
    deadline = time.monotonic() + 20
    while '"welcome"' not in listen_err.getvalue():
        assert time.monotonic() < deadline, listen_err.getvalue()
        time.sleep(0.05)

    sent: dict[str, dict] = {}

    def send(tag, who, to, thread, body, reply_to=None):
        args = ["--to", to, "--thread", thread, "--type", "status", "--author-family",
                "claude", "--body", body]
        if reply_to:
            args += ["--in-reply-to", sent[reply_to]["key"]]
        code, result = rb.node("send", states[who], *args)
        assert code == 0 and _ok(result), (tag, result)
        sent[tag] = result

    # The control that the positive above is the grant: the same send, one variable apart.
    send("a1", "A", b_label, T1, "the nightly compaction job keeps losing its scheduler lease")
    send("a2", "A", b_label, T1, "compaction ran 140 seconds so the lease lapses and a second "
                                 "worker starts the same job", reply_to="a1")
    send("b1", "B", a_label, T1, "agreed renew the lease from inside the job loop",
         reply_to="a2")
    send("a3", "A", b_label, T1, "the renewal patch runs on its own connection", reply_to="b1")
    send("a4", "A", b_label, T2, "the embedding catch up pushes 40 rows per second")
    send("a5", "A", b_label, T2, "batch size 64 lifted embedding throughput to 310 rows per "
                                 "second", reply_to="a4")
    send("a6", "A", b_label, T2, "rerun the throughput check after the reranker restart",
         reply_to="a5")
    to_b = ["a1", "a2", "a3", "a4", "a5", "a6"]
    listener.join(30)
    got = [json.loads(line)["frame"]["key"] for line in frames.getvalue().splitlines()]
    assert sorted(got) == sorted(sent[t]["key"] for t in to_b), (got, listen_err.getvalue())
    assert sent["b1"]["key"] not in got, "B was framed its own message"

    # The inbox is empty until the edges job ticks: that is why the runbook schedules it.
    code, before = rb.node("inbox", states["B"])
    assert code == 0 and before["hits"] == [] and before["coverage"]["projected"] == 0, before
    # `--run-now` stands in for the minute the scheduler would wait (both jobs ran at the
    # step-5 tick and are next due in ~60 s); it moves the schedule and bypasses nothing.
    for job in ("mesh_edges_catch_up", "mesh_embed_catch_up"):
        rb.manage("run_jobs", "--job", job, "--run-now", control=True)
    out, _ = rb.manage("run_jobs")
    assert "ok    mesh_edges_catch_up rows=7" in out and \
        "ok    mesh_embed_catch_up rows=7" in out, out
    code, inbox = rb.node("inbox", states["B"])
    assert code == 0 and sorted(h["key"] for h in inbox["hits"]) == sorted(
        sent[t]["key"] for t in to_b), inbox
    assert inbox["coverage"] == {"projected": 7, "total": 7}, inbox["coverage"]

    tag_of = {v["key"]: t for t, v in sent.items()}
    # The default ranking (divergence 026, as amended): both arms fused, then the
    # configured reranker, because the client reranks unless told not to, as the server
    # does.
    reranks = lambda: sum(p.endswith("/rerank") for p, _ in fake.requests)  # noqa: E731
    before = reranks()
    code, found = rb.node("search", states["B"], "--q",
                          "compaction lease lapses second worker", "--k", "3")
    assert code == 0 and tag_of.get(found["hits"][0]["key"]) == "a2", found
    assert found["method"] == "rrf(lexical,embed)+rerank" and found["degraded"] is None, found
    assert found["providers"] == {"embed": "embed-fake", "rerank": "rerank-fake"}
    assert found["reranked"] is True and found["rerank_requested"] is True
    assert reranks() == before + 1, "the default search did not rerank"
    cov = found["coverage"]
    assert cov["embedded"] == cov["total"] == 7 and cov["pending"] == 0, cov
    # Control, one variable apart: `--no-rerank` turns it off, and no reranker is called.
    code, found = rb.node("search", states["B"], "--q",
                          "compaction lease lapses second worker", "--k", "3", "--no-rerank")
    assert code == 0 and tag_of.get(found["hits"][0]["key"]) == "a2", found
    assert found["method"] == "rrf(lexical,embed)", found
    assert found["providers"] == {"embed": "embed-fake", "rerank": None}
    assert found["reranked"] is False and reranks() == before + 1
    assert all(p.endswith(("/v1/embeddings", "/rerank")) for p, _ in fake.requests)

    code, ctx = rb.node("context", states["B"], "--key", sent["a3"]["key"], "--k", "3")
    assert code == 0, ctx
    assert sorted(tag_of.get(h["key"]) for h in ctx["thread"]["hits"]) == ["a1", "a2", "a3",
                                                                          "b1"]
    assert [tag_of.get(a["key"]) for a in ctx["reply_chain"]["ancestors"]] == ["b1", "a2", "a1"]
    assert ctx["reply_chain"]["ended"] == "root"
    assert ctx["related"]["hits"] and all(h["thread_id"] == T2 for h in ctx["related"]["hits"])
    assert ctx["anchor"]["body"] == sent["a3"]["value"]["body"], ctx["anchor"]

    # One hop from the same message (divergence 032): its declared reply parent is the
    # context's first ancestor; the inferred related_to edges are asked for and answered
    # (none here: this runbook declares no reranker, so the related job writes none).
    code, nb = rb.node("neighbours", states["B"], "--key", sent["a3"]["key"],
                       "--include-inferred")
    assert code == 0, nb
    assert nb["anchor"]["key"] == sent["a3"]["key"] and nb["anchor"]["thread_id"] == T1, nb
    assert tag_of.get(nb["declared"]["replies_to"]["key"]) == "b1", nb["declared"]
    assert nb["inferred"]["included"] is True, nb["inferred"]

    code, thread_hits = rb.node("thread", states["B"], "--id", T1)
    assert code == 0 and [tag_of.get(hit["key"]) for hit in thread_hits["hits"]] == [
        "a1", "a2", "b1", "a3"]

    # The frame and the inbox hit are pointers: the message itself is read through the
    # signed path, memory.read, its result signature checked against the server's JWKS.
    code, got = rb.node("read", states["B"], "--key", sent["a1"]["key"])
    assert code == 0 and got["body"] == sent["a1"]["value"]["body"], got
    assert got["from"] == a_label and got["thread_id"] == T1, got

    rows = _read(dsn, "SELECT count(*), count(DISTINCT created_by_peer_id) FROM shared_memory "
                      "WHERE namespace = 'shared/mesh-chat'")
    assert rows == [(7, 2)], rows


# =============================================================================== the order

def test_the_readme_runbook_is_the_sequence_the_test_runs():
    """The README is the runbook. Its bash blocks, in order, must name the same commands the
    end-to-end test above runs; and the order that bit the 2026-09-23 run must stay fixed:
    bootstrap_owner before bootstrap_memory_peer, the server's bootstrap authority after
    bootstrap_owner."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    head = "## Fresh install"
    assert head in text, "README.md has no fresh-install runbook"
    section = text.split(head, 1)[1].split("\n## ", 1)[0]
    lines = []
    for block in re.findall(r"```bash\n(.*?)```", section, flags=re.S):
        lines += [ln.strip() for ln in block.splitlines() if ln.strip().startswith("python ")]
    stems = []
    for ln in lines:
        words = ln.split()
        if words[1] == "-c":
            continue
        if words[1] == "-m":
            stem = " ".join(words[:4])
        elif words[1] == "manage.py":
            stem = " ".join(words[:3])
        else:
            stem = " ".join(words[:2]) + (" --check" if "--check" in words else "")
        if stem not in stems:
            stems.append(stem)
    assert stems == list(RUNBOOK), stems
    assert section.index("bootstrap_owner --email") < section.index("bootstrap_memory_peer "
                                                                     "--trust-group")
    assert section.index("bootstrap_owner --email") < section.index(
        "export MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID")
    for needle in ("--plane dev", '--trust-group "$G"', "--permission write",
                   "--namespace shared/mesh-chat", "run_jobs --loop", "--tier enterprise",
                   "set_owner_tier --owner"):
        assert needle in section, needle
    # Control: the settings module's own fresh-install list is in the runbook's table.
    from tests.test_settings_documents_the_fresh_install import FRESH_INSTALL
    missing = [name for name in FRESH_INSTALL if f"`{name}`" not in section]
    assert not missing, missing


def test_the_memory_authority_first_is_refused_with_its_reason_and_the_way_on(tmp_path):
    """The old order: bootstrap_memory_peer, then bootstrap_owner. The first-owner check
    still REFUSES (it is unchanged: any owner counts), and now says why, naming the only
    owner present as the memory authority's service owner and `--additional-owner` as the
    way on. Control, one variable apart: a HUMAN owner present gets the generic refusal."""
    seed = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    pub = tmp_path / "op.pub"
    pub.write_bytes(seed)
    group = str(uuid.uuid4())
    with _empty_database("order") as dsn:
        assert sqlmigrate.main(["--dsn", dsn]) == 0
        with override_settings(MESHY_DSN=dsn):
            call_command("bootstrap_memory_peer", "--trust-group", group,
                         stdout=io.StringIO(), stderr=io.StringIO())
            with pytest.raises(CommandError) as refused:
                call_command("bootstrap_owner", email="ops@example.test",
                             public_key_file=str(pub), plane="dev", trust_group=group,
                             dsn=dsn, stdout=io.StringIO())
            why = str(refused.value)
            assert "memory authority's service owner" in why and "--additional-owner" in why
            assert f"--trust-group {group}" in why, "the way on must name the frozen group"
            assert _read(dsn, "SELECT count(*) FROM dashboard_users") == [(1,)], \
                "the refusal wrote something"
            out = io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()):
                call_command("bootstrap_owner", email="ops@example.test",
                             public_key_file=str(pub), plane="dev", trust_group=group,
                             dsn=dsn, additional_owner=True, stdout=out)
            assert json.loads(out.getvalue())["peer_id"]
            # CONTROL: now a human owner exists, and the refusal is the generic one.
            with pytest.raises(CommandError) as generic:
                call_command("bootstrap_owner", email="second@example.test",
                             public_key_file=str(pub), dsn=dsn, stdout=io.StringIO())
            assert "not the first owner" in str(generic.value)
            assert "service owner" not in str(generic.value)


def test_bootstrap_memory_peer_prints_its_owners_trust_group(tmp_path):
    """bootstrap_owner --plane dev needs the memory authority's trust group when that
    authority already exists; the only place it could be read was dashboard_users. Now the
    command prints it, created or existing."""
    peer_id, group = uuid.uuid4(), uuid.uuid4()
    with override_settings(MESHY_MEMORY_PEER_ID=str(peer_id)):
        first, again = io.StringIO(), io.StringIO()
        call_command("bootstrap_memory_peer", "--peer-id", str(peer_id), "--trust-group",
                     str(group), stdout=first, stderr=io.StringIO())
        call_command("bootstrap_memory_peer", "--peer-id", str(peer_id), stdout=again,
                     stderr=io.StringIO())
    assert f"(plane dev, trust group {group})" in first.getvalue(), first.getvalue()
    assert f"(plane dev, trust group {group})" in again.getvalue(), \
        "an EXISTING owner's group is read back too"
    stored = _read(TEST_DSN, "SELECT u.trust_group_id::text FROM peers p JOIN dashboard_users u"
                             " ON u.id = p.owner_id WHERE p.id = %s", (peer_id,))
    assert stored == [(str(group),)], "the printed group is the stored one"


def test_bootstrap_memory_peer_refuses_a_trust_group_it_cannot_apply(tmp_path):
    """The runbook passes `--trust-group G` to bootstrap_memory_peer so the operator's nodes
    and the memory authority share one group. When the peer ALREADY exists (the old order,
    or a re-run) no owner is created, so G cannot be applied: its owner's group is frozen
    (sql/040). Exiting 0 then would be a silent success that leaves the operator's nodes
    denied with no_active_grant. It is refused, naming both groups, and nothing changes.
    Control, one variable apart: the SAME group again is an idempotent re-run."""
    peer_id, group, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with override_settings(MESHY_MEMORY_PEER_ID=str(peer_id)):
        call_command("bootstrap_memory_peer", "--peer-id", str(peer_id), "--trust-group",
                     str(group), stdout=io.StringIO(), stderr=io.StringIO())
        again = io.StringIO()
        call_command("bootstrap_memory_peer", "--peer-id", str(peer_id), "--trust-group",
                     str(group), stdout=again, stderr=io.StringIO())
        assert f"trust group {group}" in again.getvalue(), "control: same group re-runs"
        with pytest.raises(CommandError) as refused:
            call_command("bootstrap_memory_peer", "--peer-id", str(peer_id),
                         "--trust-group", str(other), stdout=io.StringIO(),
                         stderr=io.StringIO())
    why = str(refused.value)
    assert str(group) in why and str(other) in why, why
    stored = _read(TEST_DSN, "SELECT u.trust_group_id::text FROM peers p JOIN dashboard_users u"
                             " ON u.id = p.owner_id WHERE p.id = %s", (peer_id,))
    assert stored == [(str(group),)], "the refusal changed the owner's group"
