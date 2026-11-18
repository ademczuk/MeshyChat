"""The embed job against a real database: the gate, the provider record, idempotence, bounds,
ON DELETE CASCADE and RLS. Divergence 019.

Providers are the deterministic fake in tests/fixtures/meshsem_fake.py, served over real
HTTP on 127.0.0.1. No live model is touched here.

ISOLATION. The namespace is shared by every test in the run, so each test gets its OWN
embedding space (tests/fixtures/meshsem_db.make_space): its provider record, watermark and
vectors are keyed by that space and no other test can see them. Every test first DRAINS
the walk, so the rows it then inserts are the only fresh ones, and every assertion about
content is scoped to the ids the test created.

Run:  pytest tests/test_meshsem_embedjob.py -q
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import uuid
from pathlib import Path

import numpy as np
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.core import dbconn, rls  # noqa: E402
from apps.jobs import registry, runner  # noqa: E402
from apps.meshsem import embedjob, lexical, spaces, store  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures.meshsem_fake import FakeModelHost  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        db.ensure_namespace(c)
        yield c


@pytest.fixture()
def app():
    """The role the job actually runs as (dbconn.connect: SET ROLE meshychat_app, verified)."""
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


class Env:
    """One test's space, fake hosts and settings. `settings(...)` re-declares them."""

    def __init__(self, space, hosts):
        self.space, self.hosts = space, hosts
        self._ctx = None

    def settings(self, providers, *, gate=None):
        if self._ctx is not None:
            self._ctx.disable()
        kw = {"MESHY_SEMANTIC_SPACE": self.space.name, "MESHY_MODEL_PROVIDERS": providers,
              "MESHY_EMBED_FORBIDDEN_ROWS": gate}
        self._ctx = override_settings(**kw)
        self._ctx.enable()

    def embed(self, pid, host):
        return {"id": pid, "kind": "embed", "base_url": host.base_url,
                "space": self.space.name}

    def close(self):
        if self._ctx is not None:
            self._ctx.disable()


@pytest.fixture()
def env(monkeypatch):
    space = db.make_space("job")
    monkeypatch.setitem(spaces.SPACES, space.name, space)
    hosts: dict[str, FakeModelHost] = {m: FakeModelHost(dims=db.DIMS, mode=m)
                                       for m in ("same", "near", "other", "down")}
    e = Env(space, hosts)
    e.settings([e.embed("same", hosts["same"])])
    yield e
    e.close()
    for h in hosts.values():
        h.close()


def _drain(conn, **kw) -> list:
    reports = []
    for _ in range(200):
        r = embedjob.catch_up(conn, **kw)
        reports.append(r)
        if not r.more or r.rows_processed == 0:
            break
    return reports


def _vectors(owner, space, ids) -> dict:
    with owner.cursor() as cur:
        cur.execute("SELECT src_id, text_sha256, provider_id, embedded_at FROM mesh_embeddings "
                    "WHERE space = %s AND src_id = ANY(%s::uuid[])",
                    (space.name, [str(i) for i in ids]))
        return {str(r[0]): r[1:] for r in cur.fetchall()}


def _state(owner, space) -> dict:
    with owner.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT * FROM mesh_embeddings_projection WHERE space = %s", (space.name,))
        return cur.fetchone()


# =============================================================== the embed_policy gate

def test_the_gate_is_closed_by_default_and_forbidden_text_never_leaves(owner, env):
    """sql/028 MUST-8's marker, honoured. Every production writer stores 'forbidden'; with
    the gate unset the job embeds none of those rows and SENDS none of their text.
    Positive control in the same test: an 'allowed' row is embedded and its text sent."""
    p = db.peer(owner, "gate")
    _drain(owner)
    secret = f"forbidden secret {uuid.uuid4().hex}"
    default = f"default writer text {uuid.uuid4().hex}"
    f = db.row(owner, p, {"body": secret}, embed_policy="forbidden")
    d = db.row(owner, p, {"body": default})                      # the column default
    a = db.row(owner, p, {"body": "allowed beta words"}, embed_policy="allowed")

    r = embedjob.catch_up(owner)
    assert (r.gate_open, r.embedded, r.skipped_forbidden) == (False, 1, 2), r
    sent = env.hosts["same"].embed_inputs()
    assert any("allowed beta words" in s for s in sent), "positive control: A was sent"
    assert not any(secret in s or default in s for s in sent), "forbidden text was sent"
    got = _vectors(owner, env.space, [f["id"], d["id"], a["id"]])
    assert set(got) == {str(a["id"])}


def test_the_gate_opens_only_on_an_explicit_true_and_closing_purges(owner, env):
    p = db.peer(owner, "gate2")
    _drain(owner)
    secret = f"forbidden but opened {uuid.uuid4().hex}"
    f = db.row(owner, p, {"body": secret}, embed_policy="forbidden")
    a = db.row(owner, p, {"body": "allowed gamma"}, embed_policy="allowed")
    providers = [env.embed("same", env.hosts["same"])]

    # A value that is not an explicit true reads CLOSED (apps/core/env.env_flag).
    env.settings(providers, gate="yes please")
    _drain(owner)
    assert set(_vectors(owner, env.space, [f["id"], a["id"]])) == {str(a["id"])}

    env.settings(providers, gate=True)
    reports = _drain(owner)
    assert reports[0].gate_changed and reports[0].gate_open
    assert set(_vectors(owner, env.space, [f["id"], a["id"]])) == {str(f["id"]), str(a["id"])}
    assert any(secret in s for s in env.hosts["same"].embed_inputs())

    env.settings(providers, gate=False)
    closing = embedjob.catch_up(owner)
    assert closing.gate_changed and not closing.gate_open and closing.purged >= 1
    assert set(_vectors(owner, env.space, [f["id"], a["id"]])) == {str(a["id"])}, (
        "closing the gate must delete the forbidden row's vector and keep the allowed one")


# =============================================================== a redaction position (divergence 053)

def test_a_redaction_position_is_walked_once_as_the_write_and_never_counted_again(owner, env):
    """A row redacted after the walk read it is a tombstone at a SECOND position of the commit order (kind 'redaction',
    sql_meshychat/027). The walk counts every row it reads and moves its watermark past it, so a second visit would count the tombstone
    again (`skipped_empty`, `rows_seen`) for a row it had already counted. CONTROL: the write position IS walked, once, and the vector
    is stored; the redaction position is not read at all."""
    from apps.memoryskills import redact

    p = db.peer(owner, "redact")
    _drain(owner)
    a = db.row(owner, p, {"body": "alpha redaction words"}, embed_policy="allowed")
    first = embedjob.catch_up(owner)
    assert (first.embedded, first.rows_processed) == (1, 1), first
    assert str(a["id"]) in _vectors(owner, env.space, [a["id"]]), "control: the write was embedded"
    seen = _state(owner, env.space)["rows_seen"]

    with owner.transaction():
        cur = owner.cursor(row_factory=psycopg.rows.dict_row)
        cur.execute("SELECT id, version, value, signed_bytes, payload_jcs_sha256, created_by_peer_id FROM shared_memory WHERE id = %s",
                    (a["id"],))
        redact._redact_one(cur, cur.fetchone(), embedjob.MESH_CHAT_NAMESPACE, a["key"], p["peer_id"], p["owner_id"], "client_name",
                           False, b"the request as signed", f"call-{uuid.uuid4()}", [])
    second = embedjob.catch_up(owner)
    assert (second.rows_read, second.rows_processed, second.skipped_empty) == (0, 0, 0), second
    assert _state(owner, env.space)["rows_seen"] == seen, "the walk counted the tombstone again"
    assert str(a["id"]) not in _vectors(owner, env.space, [a["id"]]), "the redaction left the row's vector (the handler deletes it)"


# =========================================================== provider record

def test_the_first_admitted_provider_records_a_write_once_reference(owner, env):
    env.settings([env.embed("hqb-down", env.hosts["down"]),
                  env.embed("local", env.hosts["same"])])
    r = embedjob.catch_up(owner, max_batch=1)
    assert r.reference_recorded
    st = _state(owner, env.space)
    assert st["reference_provider"] == "local" and st["dims"] == db.DIMS
    assert st["canary_sha256"] == env.space.canary_sha256()

    # Write-once below every writer: even the bootstrap role cannot rewrite it...
    with pytest.raises(psycopg.errors.CheckViolation):
        with owner.cursor() as cur:
            cur.execute("UPDATE mesh_embeddings_projection SET reference_provider = 'x' "
                        "WHERE space = %s", (env.space.name,))
    # ...while the rest of the row stays writable (positive control: the trigger protects
    # the provider record, not a blanket UPDATE ban).
    with owner.cursor() as cur:
        cur.execute("UPDATE mesh_embeddings_projection SET updated_at = now() "
                    "WHERE space = %s", (env.space.name,))
        assert cur.rowcount == 1
    assert embedjob.catch_up(owner, max_batch=1).reference_recorded is False


def test_a_different_model_is_refused_and_failover_stays_inside_the_space(owner, env):
    p = db.peer(owner, "space")
    _drain(owner)                                   # provider record set by "same"
    a = db.row(owner, p, {"body": "delta epsilon"}, embed_policy="allowed")
    wm_before = _state(owner, env.space)["watermark_id"]

    env.settings([env.embed("other-768", env.hosts["other"])])
    r = embedjob.catch_up(owner)
    assert r.stopped == "provider_unavailable" and r.embedded == 0
    assert [x["verdict"] for x in r.attempts] == ["not_admitted"]
    assert _state(owner, env.space)["watermark_id"] == wm_before, (
        "the walk moved past a row it could not embed")
    job = registry.by_name("mesh_embed_catch_up")
    with pytest.raises(embedjob.EmbedJobFailed):
        embedjob.run_catch_up_job(owner, job)

    env.settings([env.embed("other-768", env.hosts["other"]),
                  env.embed("same-model-other-gpu", env.hosts["near"])])
    r = embedjob.catch_up(owner)
    assert r.embedded == 1 and r.providers_used == ("same-model-other-gpu",)
    assert _vectors(owner, env.space, [a["id"]])[str(a["id"])][1] == "same-model-other-gpu"


def test_an_edited_canary_set_is_refused_rather_than_compared(owner, env, monkeypatch):
    """A recorded reference (the canary vectors and provider) answers ONE canary set
    (canary_sha256 beside it). Editing the
    sentences without naming a new space must refuse, on the job and on a read, rather
    than compare vectors of different sentences and call them one space."""
    _drain(owner)                                            # reference recorded
    edited = spaces.Space(**{**env.space.__dict__,
                             "canaries": env.space.canaries[:-1] + (("query", "edited"),)})
    monkeypatch.setitem(spaces.SPACES, env.space.name, edited)
    with pytest.raises(store.ReferenceMismatch):
        embedjob.catch_up(owner, space=edited)
    with rls.server_internal(owner):
        with pytest.raises(store.ReferenceMismatch):
            store.reference_of(store.read_state(owner.cursor(), edited), edited)
        assert store.reference_of(store.read_state(owner.cursor(), env.space),
                                  env.space) is not None, "positive control: original"


def test_a_concurrent_walker_makes_the_tick_write_nothing(owner, env, monkeypatch):
    """The lease keeps two job ticks apart; the CLI is not leased. If the watermark moves
    while this tick is at the model, this tick must write nothing at all."""
    p = db.peer(owner, "race")
    _drain(owner)
    a = db.row(owner, p, {"body": "race alpha"}, embed_policy="allowed")
    real = embedjob.providers.embed_in_space

    def racing(*args, **kw):
        with owner.cursor() as cur:
            cur.execute("UPDATE mesh_embeddings_projection SET watermark_at = now(), "
                        "watermark_id = %s WHERE space = %s", (a["id"], env.space.name))
        return real(*args, **kw)

    monkeypatch.setattr(embedjob.providers, "embed_in_space", racing)
    with pytest.raises(embedjob.ConcurrentWalk):
        embedjob.catch_up(owner)
    assert _vectors(owner, env.space, [a["id"]]) == {}, "the losing tick wrote vectors"

    monkeypatch.setattr(embedjob.providers, "embed_in_space", real)   # positive control
    with owner.cursor() as cur:
        cur.execute("UPDATE mesh_embeddings_projection SET watermark_seq = NULL, "
                    "watermark_at = NULL, watermark_id = NULL WHERE space = %s",
                    (env.space.name,))
    _drain(owner)
    assert str(a["id"]) in _vectors(owner, env.space, [a["id"]])


# =============================================================== idempotence, bounds

def test_the_job_is_idempotent_and_re_embeds_only_changed_text(owner, env, monkeypatch):
    p = db.peer(owner, "idem")
    _drain(owner)
    short = db.row(owner, p, {"body": "short zeta"}, embed_policy="allowed")
    long_ = db.row(owner, p, {"body": "long eta " + "theta " * 40}, embed_policy="allowed")
    assert embedjob.catch_up(owner).embedded == 2
    first = _vectors(owner, env.space, [short["id"], long_["id"]])

    assert embedjob.catch_up(owner).rows_read == 0, "a second tick re-read rows"

    def rewind():
        with owner.cursor() as cur:
            cur.execute("UPDATE mesh_embeddings_projection SET watermark_seq = NULL, "
                        "watermark_at = NULL, watermark_id = NULL WHERE space = %s",
                        (env.space.name,))

    rewind()
    calls = len(env.hosts["same"].embed_inputs())
    again = _drain(owner)
    assert sum(r.embedded for r in again) == 0 and sum(r.unchanged for r in again) >= 2
    assert not any("zeta" in s or "theta" in s
                   for s in env.hosts["same"].embed_inputs()[calls:]), (
        "an unchanged row was sent to the model again")
    assert _vectors(owner, env.space, [short["id"], long_["id"]]) == first

    monkeypatch.setattr(embedjob, "MAX_CHARS", 40)   # the derivation changes
    rewind()
    _drain(owner)
    after = _vectors(owner, env.space, [short["id"], long_["id"]])
    assert after[str(short["id"])] == first[str(short["id"])], "short text did not change"
    assert after[str(long_["id"])][0] != first[str(long_["id"])][0], "truncation changed"
    assert after[str(long_["id"])][2] > first[str(long_["id"])][2], "embedded_at moved"


def test_the_job_error_names_a_misdeclared_embed_entry_and_both_spaces(owner, env):
    """E20. A registry whose only embed entry declares another space used to fail every
    tick with "no embed provider declared", which is false and names nothing to fix. The
    error must name the entry, the space it declares and the configured space. Positive
    control: with NO embed entry at all the job still says exactly that."""
    p = db.peer(owner, "misdeclared")
    _drain(owner)
    db.row(owner, p, {"body": "misdeclared lambda"}, embed_policy="allowed")
    job = registry.by_name("mesh_embed_catch_up")
    wrong = "embeddinggemma-300m/768/raw-v1-not-this-one"

    env.settings([{"id": "embed-hqb", "kind": "embed",
                   "base_url": env.hosts["same"].base_url, "space": wrong}])
    before = len(env.hosts["same"].requests)
    with pytest.raises(embedjob.EmbedJobFailed) as exc:
        embedjob.run_catch_up_job(owner, job)
    msg = str(exc.value)
    assert "no embed provider declared" not in msg, msg
    for needle in ("embed-hqb", "space_mismatch", repr(wrong), repr(env.space.name),
                   "MESHY_SEMANTIC_SPACE"):
        assert needle in msg, (needle, msg)
    assert len(env.hosts["same"].requests) == before, "the misdeclared entry was asked"

    env.settings([])
    with pytest.raises(embedjob.EmbedJobFailed) as exc:
        embedjob.run_catch_up_job(owner, job)
    assert "no embed provider declared" in str(exc.value), str(exc.value)
    assert "embed-hqb" not in str(exc.value)

    env.settings([env.embed("same", env.hosts["same"])])
    _drain(owner)


def test_the_job_error_names_each_attempt_once_when_a_later_sub_batch_fails(
        owner, env, monkeypatch):
    """Review LOW-3 of fin/w3-ops. The report's `attempts` gets every embed_in_space call's
    records, and each call names the misdeclared entry again; when one sub-batch is
    embedded and a later one fails, the job error named the same `space_mismatch` once per
    call. It names each distinct attempt once. The report's own record is left whole
    (control: it holds the mismatch twice, so the repetition this test removes was there),
    and the failure that stopped the tick is still in the message."""
    p = db.peer(owner, "repeats")
    _drain(owner)
    for i in range(3):
        db.row(owner, p, {"body": f"repeats row {i}"}, embed_policy="allowed")
    same = env.hosts["same"]
    wrong = "embeddinggemma-300m/768/raw-v1-not-this-one"
    env.settings([{"id": "typo", "kind": "embed", "base_url": same.base_url, "space": wrong},
                  env.embed("same", same)])
    monkeypatch.setattr(embedjob, "SUB_BATCH", 1)
    real_embed, real_catch_up = embedjob.providers.embed_in_space, embedjob.catch_up
    reports = []

    def first_chunk_then_down(space, texts, reference, **kw):
        got = real_embed(space, texts, reference, **kw)
        if texts:
            same.mode = "down"                 # the next sub-batch finds the host down
        return got

    def recording(conn, **kw):
        reports.append(real_catch_up(conn, **kw))
        return reports[-1]

    monkeypatch.setattr(embedjob.providers, "embed_in_space", first_chunk_then_down)
    monkeypatch.setattr(embedjob, "catch_up", recording)
    job = registry.by_name("mesh_embed_catch_up")
    try:
        with pytest.raises(embedjob.EmbedJobFailed) as exc:
            embedjob.run_catch_up_job(owner, job)
    finally:
        same.mode = "same"
    msg = str(exc.value)
    record = [(a.get("id"), a.get("verdict")) for a in reports[-1].attempts]
    assert record.count(("typo", "space_mismatch")) == 2, record
    assert ("same", "admitted") in record and record[-1][0] == "same", record
    assert record[-1][1] != "admitted", record
    assert "1 row(s) processed" in msg, msg
    assert msg.count("typo: space_mismatch") == 1, msg
    assert f"same: {record[-1][1]}" in msg and repr(wrong) in msg, msg

    monkeypatch.setattr(embedjob, "catch_up", real_catch_up)
    monkeypatch.setattr(embedjob.providers, "embed_in_space", real_embed)
    env.settings([env.embed("same", same)])
    _drain(owner)


def test_the_walk_is_bounded_and_never_passes_an_unembedded_row(owner, env):
    p = db.peer(owner, "bound")
    _drain(owner)
    rows = [db.row(owner, p, {"body": f"row {i} iota"}, embed_policy="allowed")
            for i in range(5)]

    r = embedjob.catch_up(owner, max_batch=3, embed_per_tick=10)
    assert (r.rows_read, r.embedded, r.more) == (3, 3, True)

    r = embedjob.catch_up(owner, embed_per_tick=1)
    assert (r.embedded, r.stopped, r.more) == (1, "embed_bound", True)
    assert _state(owner, env.space)["watermark_id"] == rows[3]["id"]

    env.settings([env.embed("down", env.hosts["down"])])
    r = embedjob.catch_up(owner)
    assert (r.rows_processed, r.stopped) == (0, "provider_unavailable")
    assert _state(owner, env.space)["watermark_id"] == rows[3]["id"]

    env.settings([env.embed("same", env.hosts["same"])])
    r = embedjob.catch_up(owner)
    assert r.embedded == 1 and r.stopped is None
    assert set(_vectors(owner, env.space, [x["id"] for x in rows])) == {
        str(x["id"]) for x in rows}


def test_the_registered_job_runs_through_the_runner_as_the_app_role(owner, env):
    """End to end through apps/jobs: the runner's own connection factory (meshychat_app,
    statement_timeout), the lease, and an ERROR outcome when no provider is admitted."""
    p = db.peer(owner, "runner")
    _drain(owner)
    db.row(owner, p, {"body": "runner kappa"}, embed_policy="allowed")
    base = registry.by_name("mesh_embed_catch_up")
    job = registry.Job(name="test_embed_" + uuid.uuid4().hex[:10],
                       interval_secs=base.interval_secs, timeout_secs=base.timeout_secs,
                       jitter_frac=base.jitter_frac, max_batch=base.max_batch, run=base.run)

    def connect(**kw):
        return dbconn.connect(DSN, **kw)

    try:
        env.settings([env.embed("down", env.hosts["down"])])
        out = runner.run_due(connect, jobs=(job,), holder="t")[0]
        assert out.result == "error" and "EmbedJobFailed" in out.error, out
        with owner.cursor() as cur:
            cur.execute("UPDATE background_job_leases SET next_run_at = now() "
                        "WHERE job_name = %s", (job.name,))
        env.settings([env.embed("same", env.hosts["same"])])
        out = runner.run_due(connect, jobs=(job,), holder="t")[0]
        assert out.result == "ok" and out.rows >= 1, out
    finally:
        with owner.cursor() as cur:
            cur.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job.name,))


# =============================================================== storage properties

def test_on_delete_cascade_removes_the_vector_with_its_message(owner, env):
    """Why the vectors are in Postgres: retention deletes a message and its derivative
    goes in the same transaction."""
    p = db.peer(owner, "cascade")
    _drain(owner)
    a = db.row(owner, p, {"body": "lambda mu"}, embed_policy="allowed")
    keep = db.row(owner, p, {"body": "nu xi"}, embed_policy="allowed")
    embedjob.catch_up(owner)
    assert set(_vectors(owner, env.space, [a["id"], keep["id"]])) == {
        str(a["id"]), str(keep["id"])}, "positive control: both were embedded"
    with owner.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (a["id"],))
    assert set(_vectors(owner, env.space, [a["id"], keep["id"]])) == {str(keep["id"])}


def test_the_tables_are_unreadable_and_unwritable_without_server_internal(owner, app, env):
    """RLS tripwire on the new tables (mesh_lexical since divergence 026). The app role
    with no context sees and writes nothing; the same role under server_internal sees the
    rows (the positive control that the refusal is the policy and not an empty table)."""
    p = db.peer(owner, "rls")
    _drain(owner)
    other = db.row(owner, p, {"body": "not embedded"})
    db.row(owner, p, {"body": "omicron pi"}, embed_policy="allowed")
    embedjob.catch_up(owner)

    with app.cursor() as cur:
        for table in ("mesh_embeddings", "mesh_embeddings_projection", "mesh_lexical"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE space = %s", (env.space.name,))
            assert cur.fetchone()[0] == 0, f"{table} leaked to a contextless connection"
    # The inserts are otherwise VALID (a real src_id, the right widths), so the only thing
    # that can refuse them is the policy. InsufficientPrivilege is the RLS refusal; a
    # constraint violation is a different class and would fail this test. The first
    # draft inserted SELECT ... FROM shared_memory, which RLS hid, so it inserted zero
    # rows, raised nothing, and failed for a reason that proved nothing.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app.cursor() as cur:
            cur.execute("INSERT INTO mesh_embeddings_projection (space) VALUES (%s)",
                        (env.space.name + "-x",))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app.cursor() as cur:
            cur.execute("INSERT INTO mesh_embeddings (src_id, src_key, src_version, space, "
                        "dims, text_sha256, vec, provider_id, recorded_at) VALUES "
                        "(%s, %s, 1, %s, 1, repeat('a', 64), decode('00000000', 'hex'), "
                        "'x', now())", (other["id"], other["key"], env.space.name))
    with rls.server_internal(app):
        with app.cursor() as cur:
            for table in ("mesh_embeddings", "mesh_embeddings_projection", "mesh_lexical"):
                cur.execute(f"SELECT count(*) FROM {table} WHERE space = %s",
                            (env.space.name,))
                assert cur.fetchone()[0] >= 1, f"positive control: {table} readable"
    for table in ("mesh_embeddings", "mesh_embeddings_projection", "mesh_lexical"):
        rls.assert_rls_active(app, table=table)


def test_the_job_runs_as_the_app_role_not_just_the_owner(owner, app, env):
    """apps/mesh learned this the hard way (TRUNCATE is a privilege meshychat_app lacks): the
    job's own role must be the one the test uses. Control first: this IS meshychat_app."""
    with app.cursor() as cur:
        cur.execute("SELECT current_user, (SELECT rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user)")
        role, bypasses = cur.fetchone()
    assert role == rls.APP_ROLE and not bypasses
    p = db.peer(owner, "approle")
    _drain(app)
    a = db.row(owner, p, {"body": "rho sigma"}, embed_policy="allowed")
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    r = embedjob.catch_up(app)
    assert r.embedded == 1
    env.settings([env.embed("same", env.hosts["same"])], gate=True)
    closing = _drain(app)                          # opening restarts the walk
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    closing = embedjob.catch_up(app)               # closing purges, as meshychat_app
    assert closing.gate_changed
    assert str(a["id"]) in _vectors(owner, env.space, [a["id"]])


def test_the_cli_reports_status_as_a_module_entry_point(owner):
    """`python -m apps.meshsem status` in a fresh process: Django settings configured by
    the entry point itself, the app role, the real space, a coverage line. The first
    draft read the gate through an unconfigured settings object and would have raised."""
    import subprocess

    env_ = {k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "DJANGO_SETTINGS_MODULE", "MESHY_DSN")}
    out = subprocess.run([sys.executable, "-m", "apps.meshsem", "--dsn", DSN, "status"],
                         cwd=str(Path(__file__).resolve().parent.parent),
                         capture_output=True, text=True, env=env_, timeout=120)
    assert out.returncode == 0, f"rc={out.returncode} stderr={out.stderr[-600:]}"
    assert f"space {spaces.DEFAULT_SPACE}" in out.stdout
    assert "coverage: {" in out.stdout and "gate (MESHY_EMBED_FORBIDDEN_ROWS) open: False" \
        in out.stdout, out.stdout


def _partition(cov) -> int:
    """embedded + every skip + pending: the five counts that must sum to total."""
    return (cov.embedded + cov.skipped_forbidden + cov.skipped_no_schema_version
            + cov.skipped_empty + cov.pending)


def test_coverage_counts_what_the_gate_keeps_out(owner, env):
    p = db.peer(owner, "cov")
    _drain(owner)
    db.row(owner, p, {"body": "tau"}, embed_policy="allowed")
    db.row(owner, p, {"body": "upsilon"})
    _drain(owner)
    with rls.server_internal(owner):
        closed = store.coverage(owner, env.space, gate_open=False)
        opened = store.coverage(owner, env.space, gate_open=True)
    assert closed.embedded >= 1 and closed.skipped_forbidden >= 1
    # The partition is FIVE counts since divergence 023. This test used to check the old
    # three-way sum, which held only while no other test had left a body-less row in the
    # shared namespace: in the full run (1,160 tests) 24 such rows made the open-gate sum
    # 355 of 379, while every file passed on its own.
    assert _partition(closed) == closed.total
    assert opened.skipped_forbidden == 0 and opened.total == closed.total
    assert opened.pending >= 1, "open, the forbidden row is pending rather than skipped"
    assert _partition(opened) == closed.total

    # Now give the forbidden rows vectors (gate open), and ask the CLOSED question again
    # without a job tick: vectors the gate hides are not searchable, so they must not be
    # counted as embedded. The first draft never had a forbidden row WITH a vector, so
    # counting every vector (mutant E6) survived it.
    env.settings([env.embed("same", env.hosts["same"])], gate=True)
    _drain(owner)
    with rls.server_internal(owner):
        after = store.coverage(owner, env.space, gate_open=False)
        reopened = store.coverage(owner, env.space, gate_open=True)
    assert reopened.embedded > opened.embedded, "positive control: forbidden rows embedded"
    assert after.embedded == closed.embedded, "a hidden vector was counted as embedded"
    assert after.skipped_forbidden == closed.skipped_forbidden


def test_a_row_skipped_for_a_missing_schema_version_is_counted_never_pending(owner, env):
    """Divergence 023. A head msg/ row with NO schema_version and no string body (a legacy
    row the v1 validator never saw: cursors, state rows) has nothing to embed, and the job
    walks past it. Before, that skip was counted in the state row but NOT in coverage, so
    the row sat in `pending` forever: reported as work to come, never done, with no word
    for why. Now it is its own denominator, `skipped_no_schema_version`, and the invariant
    embedded + every skip + pending == total holds.

    Controls, each differing in one variable: a legacy row WITH a body is embedded (the
    skip is about having no text, not about being legacy), and a row that DOES declare
    schema_version with a blank body is counted `skipped_empty`, not as legacy."""
    p = db.peer(owner, "nosv")
    _drain(owner)
    with rls.server_internal(owner):
        before = store.coverage(owner, env.space, gate_open=False)
    # One row per drain, so each tick's report is about ONE row and a swapped label shows.
    blank_legacy = db.row(owner, p, {"cursor": "c-17"}, embed_policy="allowed")
    first = _drain(owner)
    assert sum(r.skipped_no_schema_version for r in first) == 1
    assert sum(r.skipped_empty for r in first) == 0
    text_legacy = db.row(owner, p, {"body": "legacy phi chi"}, embed_policy="allowed")
    blank_v1 = db.row(owner, p, {"schema_version": 1, "body": "   "}, embed_policy="allowed")
    reports = first + _drain(owner)
    with rls.server_internal(owner):
        after = store.coverage(owner, env.space, gate_open=False)

    assert after.total - before.total == 3
    assert after.skipped_no_schema_version - before.skipped_no_schema_version == 1
    assert after.skipped_empty - before.skipped_empty == 1
    assert after.embedded - before.embedded == 1, "control: a legacy row WITH text embeds"
    assert after.pending == before.pending, "a skipped row was reported as pending"
    assert _partition(after) == after.total
    cov = after.as_json()
    assert cov["skipped_no_schema_version"] == after.skipped_no_schema_version
    assert cov["skipped_empty"] == after.skipped_empty

    # The job's own report and its state row count the same rows.
    assert sum(r.skipped_no_schema_version for r in reports) == 1
    assert sum(r.skipped_empty for r in reports) == 1
    assert set(_vectors(owner, env.space, [blank_legacy["id"], text_legacy["id"],
                                           blank_v1["id"]])) == {str(text_legacy["id"])}
    assert _state(owner, env.space)["rows_skipped_no_schema_version"] >= 1


# =============================================================== the lexical arm (026)

def _lexical(owner, space, ids) -> dict:
    """src_id -> (derivation, text_sha256, lexemes, tfs, indexed_at), read on the OWNER
    connection: the truth, not a view of it."""
    with owner.cursor() as cur:
        cur.execute("SELECT src_id, derivation, text_sha256, lexemes, tfs, indexed_at "
                    "FROM mesh_lexical WHERE space = %s AND src_id = ANY(%s::uuid[])",
                    (space.name, [str(i) for i in ids]))
        return {str(r[0]): r[1:] for r in cur.fetchall()}


def test_the_job_lexes_exactly_the_rows_it_embeds(owner, env):
    """Divergence 026. The lexical arm covers the rows the vector arm covers: same gate,
    same text, same walk. A forbidden row gets neither (and its words are nowhere in the
    table); an allowed row gets both, tokenized by lexical.terms (lowercased, NOT
    stemmed, one-character words dropped, snake_case kept whole), digested over the text
    the job embedded; coverage counts it."""
    p = db.peer(owner, "lex")
    _drain(owner)
    with rls.server_internal(owner):
        before = store.coverage(owner, env.space, gate_open=False)
    word = f"zl{uuid.uuid4().hex[:8]}"
    a = db.row(owner, p, {"body": f"  Running {word} Tests a mesh_job mesh  "},
               embed_policy="allowed")
    f = db.row(owner, p, {"body": f"secret {word} words"}, embed_policy="forbidden")
    punct = db.row(owner, p, {"body": "-> ! ?"}, embed_policy="allowed")
    r = embedjob.catch_up(owner)
    assert (r.embedded, r.lexed, r.skipped_forbidden) == (2, 2, 1), r

    got = _lexical(owner, env.space, [a["id"], f["id"], punct["id"]])
    assert set(got) == {str(a["id"]), str(punct["id"])}, "a forbidden row was lexed"
    assert got[str(punct["id"])][2:4] == ([], []), (
        "text with no term is indexed with empty arrays, and never matched")
    derivation, sha, lexemes, tfs, _ = got[str(a["id"])]
    assert derivation == lexical.DERIVATION == "word2-lower/v1"
    assert list(zip(lexemes, tfs)) == sorted([("running", 1), (word, 1), ("tests", 1),
                                              ("mesh_job", 1), ("mesh", 1)])
    assert sha == embedjob.text_sha256(f"Running {word} Tests a mesh_job mesh")
    with rls.server_internal(owner):
        after = store.coverage(owner, env.space, gate_open=False)
    assert after.lexical_indexed - before.lexical_indexed == 2
    assert after.as_json()["lexical_indexed"] == after.lexical_indexed


def test_an_embedder_outage_does_not_stall_the_lexical_rows(owner, env):
    """Divergence 026. With the embedder down the tick fails (provider_unavailable) and the
    watermark stays before the first row it could not embed, exactly as before; but the
    rows it READ are lexed anyway, because a tokenizer needs no model and lexical-only is
    what search serves in exactly this outage. The gate still applies: the forbidden row
    is not lexed. Controls: a second failing tick rewrites nothing, and when the embedder
    is back the same rows are embedded and not lexed again (their digest is stored)."""
    p = db.peer(owner, "lexdown")
    _drain(owner)
    wm = _state(owner, env.space)["watermark_id"]
    word = f"zo{uuid.uuid4().hex[:8]}"
    a = db.row(owner, p, {"body": f"{word} outage alpha"}, embed_policy="allowed")
    b = db.row(owner, p, {"body": f"{word} outage beta"}, embed_policy="allowed")
    f = db.row(owner, p, {"body": f"{word} outage secret"}, embed_policy="forbidden")
    ids = [a["id"], b["id"], f["id"]]

    env.settings([env.embed("down", env.hosts["down"])])
    r = embedjob.catch_up(owner)
    assert (r.rows_processed, r.embedded, r.stopped) == (0, 0, "provider_unavailable"), r
    assert _state(owner, env.space)["watermark_id"] == wm, "walked past an unembedded row"
    assert not _vectors(owner, env.space, ids)
    assert r.lexed == 2, r
    assert set(_lexical(owner, env.space, ids)) == {str(a["id"]), str(b["id"])}, (
        "lexed despite the outage, and never the forbidden row")
    assert embedjob.catch_up(owner).lexed == 0, "a stored lexical digest is not rewritten"

    env.settings([env.embed("same", env.hosts["same"])])
    r = embedjob.catch_up(owner)
    assert (r.embedded, r.lexed, r.stopped) == (2, 0, None), r
    assert set(_vectors(owner, env.space, ids)) == {str(a["id"]), str(b["id"])}


def test_closing_the_gate_purges_the_lexemes_of_forbidden_rows(owner, env):
    p = db.peer(owner, "lexgate")
    _drain(owner)
    f = db.row(owner, p, {"body": "forbidden lexeme omega"}, embed_policy="forbidden")
    a = db.row(owner, p, {"body": "allowed lexeme alpha"}, embed_policy="allowed")
    providers = [env.embed("same", env.hosts["same"])]
    env.settings(providers, gate=True)
    _drain(owner)
    assert set(_lexical(owner, env.space, [f["id"], a["id"]])) == {str(f["id"]),
                                                                  str(a["id"])}, "control"
    env.settings(providers, gate=False)
    closing = embedjob.catch_up(owner)
    assert closing.gate_changed and not closing.gate_open
    assert set(_lexical(owner, env.space, [f["id"], a["id"]])) == {str(a["id"])}, (
        "closing the gate must delete the forbidden row's lexemes and keep the allowed")


def test_a_new_lexical_derivation_restarts_the_walk_without_re_embedding(owner, env):
    """A state row from before divergence 026 (lexical_derivation NULL), or one written
    under another derivation, restarts the walk: the old derivation's rows go, every row
    is lexed again, and no vector is re-embedded (unchanged rows cost a digest compare)."""
    p = db.peer(owner, "lexderiv")
    _drain(owner)
    a = db.row(owner, p, {"body": "derivation restart kappa"}, embed_policy="allowed")
    # A row the restarted walk will NEVER revisit (it reads head rows only): its terms go
    # only if the restart DELETES the old derivation. Every row the walk does revisit is
    # rewritten by the upsert anyway, so without this row the DELETE is untested.
    old = db.row(owner, p, {"body": "derivation superseded lambda"}, embed_policy="allowed")
    _drain(owner)
    vec_before = _vectors(owner, env.space, [a["id"]])
    assert embedjob.catch_up(owner).derivation_changed is False, "control: settled"
    with owner.cursor() as cur:                 # what an older walk would have left
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (old["id"],))
        cur.execute("UPDATE mesh_lexical SET derivation = 'word2-lower/v0' "
                    "WHERE space = %s", (env.space.name,))
        cur.execute("UPDATE mesh_embeddings_projection SET lexical_derivation = NULL "
                    "WHERE space = %s", (env.space.name,))
    assert _lexical(owner, env.space, [a["id"]])[str(a["id"])][0] == "word2-lower/v0"
    assert _lexical(owner, env.space, [old["id"]])[str(old["id"])][0] == "word2-lower/v0", (
        "control: the superseded row holds an old-derivation row before the restart")
    calls = len(env.hosts["same"].embed_inputs())

    reports = _drain(owner)
    assert reports[0].derivation_changed and not reports[0].gate_changed
    assert sum(r.embedded for r in reports) == 0 and sum(r.unchanged for r in reports) >= 1
    assert not any("kappa" in s for s in env.hosts["same"].embed_inputs()[calls:])
    assert _lexical(owner, env.space, [a["id"]])[str(a["id"])][0] == lexical.DERIVATION
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM mesh_lexical WHERE space = %s AND derivation <> %s",
                    (env.space.name, lexical.DERIVATION))
        assert cur.fetchone()[0] == 0, "an old derivation's row survived the restart"
    assert _lexical(owner, env.space, [old["id"]]) == {}, (
        "the superseded row's old-derivation terms survived: the restart did not delete them")
    assert _state(owner, env.space)["lexical_derivation"] == lexical.DERIVATION
    assert _vectors(owner, env.space, [a["id"]]) == vec_before


def test_on_delete_cascade_removes_the_lexemes_with_their_message(owner, env):
    p = db.peer(owner, "lexcascade")
    _drain(owner)
    a = db.row(owner, p, {"body": "cascade lexeme one"}, embed_policy="allowed")
    keep = db.row(owner, p, {"body": "cascade lexeme two"}, embed_policy="allowed")
    embedjob.catch_up(owner)
    assert set(_lexical(owner, env.space, [a["id"], keep["id"]])) == {
        str(a["id"]), str(keep["id"])}, "positive control: both were lexed"
    with owner.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (a["id"],))
    assert set(_lexical(owner, env.space, [a["id"], keep["id"]])) == {str(keep["id"])}


# =============================================================== D19, the commit order

def test_d19_a_row_that_commits_after_a_later_stamped_row_was_walked_is_embedded(owner, env):
    """STATUS D19, the embed walk's half. Transaction A starts; B writes a row and commits;
    the embed tick walks B; only then does A write its row, carrying A's EARLIER start as
    `created_at`, and commit. The next tick must embed A's row. CONTROL: B is embedded by
    the first tick, which could not see A (not yet committed).

    ONE CLOCK, as in tests/test_mesh_projection.py's twin: B's created_at is 1 ms after A's
    transaction's now(), on the database's clock, not db.row's default (this process's
    clock), which let the VM's clock running ahead make A's key the greater one. CONTROL:
    the stored created_at values are in the order this test is about."""
    p = db.peer(owner, "late")
    _drain(owner)
    early = psycopg.connect(DSN)
    try:
        started = early.execute("SELECT now()").fetchone()[0].astimezone(_dt.timezone.utc)
        b = db.row(owner, p, {"body": "late commit beta"}, embed_policy="allowed",
                   created_at=started + _dt.timedelta(milliseconds=1))
        first = embedjob.catch_up(owner)
        assert first.embedded >= 1
        assert str(b["id"]) in _vectors(owner, env.space, [b["id"]]), "control: B embedded"
        a = db.row(early, p, {"body": "late commit alpha"}, embed_policy="allowed",
                   created_at=started)
        early.commit()
    finally:
        early.close()
    with owner.cursor() as cur:
        cur.execute("SELECT id, created_at FROM shared_memory WHERE id = ANY(%s)",
                    ([a["id"], b["id"]],))
        at = dict(cur.fetchall())
    assert at[a["id"]] < at[b["id"]], f"control: A's created_at is the earlier one: {at}"
    wm_after_b = (_state(owner, env.space)["watermark_at"],
                  _state(owner, env.space)["watermark_id"])

    _drain(owner)
    assert str(a["id"]) in _vectors(owner, env.space, [a["id"]]), (
        "a row that committed after a later-stamped row was walked was never embedded")
    wm_now = (_state(owner, env.space)["watermark_at"], _state(owner, env.space)["watermark_id"])
    assert wm_now == wm_after_b, (
        f"the reader-facing key moved from {wm_after_b} to {wm_now} when only an older row "
        "was walked; it must only move forward")


def test_d21_a_tick_that_read_an_older_watermark_cannot_set_it_back(owner, env, monkeypatch):
    """STATUS D21, the embed walk's half, with TWO connections (the test above moves the
    state row by hand on the tick's own connection). The lease keeps two job ticks apart;
    `python -m apps.meshsem catch-up` takes no lease. Tick A reads one row (max_batch=1);
    while A is at the model, tick B on a second connection reads both rows from the same
    watermark, embeds them and commits its position. A writes second, with the OLDER
    position. The saved position must not go back, nor be counted twice: this walk compares
    the position it read under FOR UPDATE before writing (`ConcurrentWalk`), which is what
    this test pins. Deterministic, one thread: B runs inside A's first embed call. ONE
    CLOCK: created_at from the database. CONTROLS: A read exactly the older row, B read both
    and committed before A wrote, and a later tick still moves the position forward."""
    p = db.peer(owner, "d21")
    _drain(owner)
    db_now = owner.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(_dt.timezone.utc)
    r1 = db.row(owner, p, {"body": "d21 first"}, embed_policy="allowed", created_at=db_now)
    r2 = db.row(owner, p, {"body": "d21 second"}, embed_policy="allowed",
                created_at=db_now + _dt.timedelta(milliseconds=1))

    def seq_of(rid):
        with owner.cursor() as cur:
            cur.execute("SELECT seq FROM mesh_commit_order WHERE memory_id = %s", (rid,))
            return cur.fetchone()[0]

    seq_1, seq_2 = seq_of(r1["id"]), seq_of(r2["id"])
    assert seq_1 < seq_2, "control: the rows are walked in this order"
    before = _state(owner, env.space)

    b_conn = psycopg.connect(DSN, autocommit=True, options="-c lock_timeout=5s")
    real = embedjob.providers.embed_in_space
    seen: dict = {}

    def interleave(space, texts, *args, **kw):
        if texts and "b" not in seen:
            seen["b"] = "running"
            seen["a_texts"] = len(texts)
            seen["b"] = embedjob.catch_up(b_conn)          # the LATER tick, committed first
            seen["after_b"] = _state(owner, env.space)
        return real(space, texts, *args, **kw)

    monkeypatch.setattr(embedjob.providers, "embed_in_space", interleave)
    refused = None
    try:
        try:
            embedjob.catch_up(owner, max_batch=1)          # the EARLIER tick, writes second
        except RuntimeError as exc:
            refused = exc
    finally:
        b_conn.close()
    monkeypatch.setattr(embedjob.providers, "embed_in_space", real)

    assert seen["a_texts"] == 1, f"control: A read the older row only: {seen}"
    assert seen["b"].rows_read == 2 and seen["b"].embedded == 2, "control: B walked both"
    assert seen["after_b"]["watermark_seq"] == seq_2, "control: B committed before A wrote"
    after = _state(owner, env.space)
    assert after["watermark_seq"] == seq_2, (
        f"the watermark went BACK from {seq_2} to {after['watermark_seq']}")
    assert ((after["watermark_at"], after["watermark_id"])
            == (seen["after_b"]["watermark_at"], seen["after_b"]["watermark_id"]))
    assert after["rows_seen"] - before["rows_seen"] == 2, (
        f"rows_seen moved by {after['rows_seen'] - before['rows_seen']} for 2 rows")
    assert isinstance(refused, embedjob.ConcurrentWalk), f"not refused: {refused!r}"

    # POSITIVE CONTROL: only a stale position is refused; a later tick still moves it.
    assert embedjob.catch_up(owner).rows_read == 0, "nothing lost, nothing re-walked"
    r3 = db.row(owner, p, {"body": "d21 third"}, embed_policy="allowed",
                created_at=db_now + _dt.timedelta(milliseconds=2))
    assert embedjob.catch_up(owner).embedded == 1
    assert _state(owner, env.space)["watermark_seq"] == seq_of(r3["id"])
