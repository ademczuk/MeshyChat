"""`/v1/health` wire behavior, and the refusal that keeps a request off an unsafe connection.

Two things are under test and they fail in opposite directions:

  1. the health route must not claim health it has not measured. A route that always returns
     200 even with db:false misstates availability, so the tests below pin the 200-only-
     when-all-pass rule, the exact key set and order, and the degraded path.
  2. `dbconn` must refuse to serve a request on a connection that bypasses RLS.

Every negative here is paired with a positive control in the same test, because
"reports false" and "the probe is broken" look identical, and so do "refused" and
"raises on every connection". The Redis and blob controls run against a real socket and
a real directory - no mock stands in for the thing being measured, and there is no
database mock anywhere.

Run:  pytest tests/test_health.py -q
Needs the dev stack database (MESHY_TEST_DSN / MESHY_DSN, default port 5434).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import socket
import sys
import threading
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

# Settings read the environment at import time, so these must be set BEFORE
# django.setup(). Setting MESHY_ALLOWED_HOSTS here is also the positive control that
# ALLOWED_HOSTS really is env-driven: the Django test client sends Host: testserver,
# and with DEBUG off a settings module that ignored the variable would 400 every
# request below.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ["MESHY_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
os.environ.setdefault("MESHY_DSN", DSN)

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402

from apps.core import dbconn, rls  # noqa: E402
from apps.core.middleware import (  # noqa: E402
    DatabaseConnectionMiddleware,
    Identity,
    RequestDatabase,
    Unauthenticated,
)

#: Response fields, in declaration order - serialization preserves that order,
#: so this is the byte order of the response, not just its contents.
HEALTH_FIELDS = ["status", "version", "git_sha", "git_dirty", "db", "redis", "blob"]

#: Appended AFTER the base fields (rule B9, operator decision 2026-09-25, divergence 041's
#: dated paragraph): the serving code's hash, the call worker's recorded hash, and whether
#: they differ. The three fields follow `blob` on the wire.
B9_FIELDS = ["code_sha256", "worker_code_sha256", "worker_code_stale"]


# --------------------------------------------------------------------------- helpers
class FakeRedis:
    """A real TCP server speaking just enough RESP to answer DBSIZE.

    This is the positive control for the Redis probe: without it, "redis is reported
    false when unconfigured" would also pass if the probe were hard-wired to False.
    It is a socket, not a patch of the probe - the probe's socket code is the thing
    being tested.
    """

    def __init__(self, reply: bytes = b":0\r\n"):
        self.reply = reply
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(2)
                conn.recv(4096)          # the DBSIZE command frame
                conn.sendall(self.reply)
            except OSError:
                pass
            finally:
                conn.close()

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}"

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture()
def fake_redis():
    r = FakeRedis()
    yield r
    r.close()


def _activate_fs(monkeypatch, blob_dir) -> None:
    """Build and install an fs store the way startup does (apps/files/apps.py ->
    blob.activate). The blob probe asks THAT store, never the environment (B11), so
    setting MESHYCHAT_BLOB_* here alone would change nothing; the previous store is restored
    afterwards by monkeypatch.

    The directory is then given a file for every ready row in the run's database
    (tests/blob_ready_files.py): since divergence 047 a healthy fs store is one that holds
    them, and earlier modules leave ready rows behind whose files are elsewhere."""
    from apps.files import blob as blobmod, config as cfgmod
    from tests.blob_ready_files import place_ready_files
    for k, v in {"MESHYCHAT_BLOB_BACKEND": "fs", "MESHYCHAT_BLOB_DIR": str(blob_dir),
                 "MESHYCHAT_BLOB_TOKEN_SECRET": "00" * 32,
                 "MESHYCHAT_PUBLIC_BASE_URL": "https://meshy.test"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(blobmod, "_ACTIVE", blobmod._ACTIVE)
    blobmod.activate(cfgmod.build_blob_config())
    place_ready_files(blob_dir)


def _no_store(monkeypatch) -> None:
    """A process that started with MESHYCHAT_BLOB_BACKEND unset: no store."""
    from apps.files import blob as blobmod
    monkeypatch.setattr(blobmod, "_ACTIVE", None)


@pytest.fixture()
def healthy_env(monkeypatch, fake_redis, tmp_path):
    """Every dependency configured and genuinely reachable: the all-green control."""
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    monkeypatch.setenv("MESHY_REDIS_URL", fake_redis.url)
    _activate_fs(monkeypatch, blob_dir)
    return {"blob_dir": blob_dir, "redis_url": fake_redis.url}


@pytest.fixture()
def unconfigured_env(monkeypatch):
    """Redis and blob absent; the database still real."""
    for var in ("MESHY_REDIS_URL", "MESHYCHAT_BLOB_BACKEND", "MESHYCHAT_BLOB_DIR"):
        monkeypatch.delenv(var, raising=False)
    _no_store(monkeypatch)


@pytest.fixture()
def owner_conn():
    """The bootstrap `meshy` role: rolbypassrls = true. Must never serve a request."""
    conn = psycopg.connect(DSN, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture()
def app_conn():
    """What a request is supposed to run on: autocommit + SET ROLE meshychat_app."""
    conn = dbconn.connect(DSN)
    yield conn
    conn.close()


def _get_health(client=None):
    resp = (client or Client()).get("/v1/health")
    return resp, json.loads(resp.content.decode())


# --------------------------------------------------------------------------- shape
def test_health_body_has_the_expected_fields_in_the_expected_order(healthy_env):
    resp, body = _get_health()
    assert list(body.keys()) == HEALTH_FIELDS + B9_FIELDS, (
        "health response fields differ from the documented order, including B9's three "
        f"appended fields. got={list(body.keys())}")
    assert resp["Content-Type"] == "application/json"
    assert isinstance(body["status"], str) and isinstance(body["version"], str)
    assert isinstance(body["git_sha"], str)
    for flag in ("git_dirty", "db", "redis", "blob"):
        assert isinstance(body[flag], bool), f"{flag} must be a JSON bool, got {body[flag]!r}"


def test_health_body_is_compact_json(healthy_env):
    raw = _get_health()[0].content.decode()
    assert raw.startswith('{"status":"ok"'), raw[:60]
    assert ", " not in raw and '": ' not in raw, (
        "the health response must use compact JSON separators")


# ------------------------------------------------------------ B9: the code it runs
def _set_worker_code(owner_conn, sha) -> None:
    owner_conn.execute("INSERT INTO background_job_leases (job_name) "
                       "VALUES ('call_deadline_sweep') ON CONFLICT (job_name) DO NOTHING")
    owner_conn.execute("UPDATE background_job_leases SET worker_code_sha256 = %s, "
                       "worker_code_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END "
                       "WHERE job_name = 'call_deadline_sweep'", (sha, sha))


def test_health_reports_the_serving_code_and_a_stale_call_worker(healthy_env, owner_conn):
    """Rule B9 (operator decision 2026-09-25): /v1/health reports the hash of the code this
    process serves and the hash `run_call_worker` last recorded, and says when they differ.
    A stale worker does not make the node `degraded`: the web process serves requests
    whatever the worker runs, and a load balancer must not evict it for that. CONTROLS: the
    worker on the serving code is not stale; a hash never recorded is null, not stale."""
    from apps.core import code_fingerprint
    serving = code_fingerprint.running()
    try:
        _set_worker_code(owner_conn, serving)
        resp, body = _get_health()
        assert (body["code_sha256"], body["worker_code_sha256"], body["worker_code_stale"]) \
            == (serving, serving, False), body
        assert (resp.status_code, body["status"]) == (200, "ok"), body

        _set_worker_code(owner_conn, "0" * 64)
        resp, body = _get_health()
        assert (body["worker_code_sha256"], body["worker_code_stale"]) == ("0" * 64, True), body
        assert (resp.status_code, body["status"]) == (200, "ok"), body

        _set_worker_code(owner_conn, None)
        resp, body = _get_health()
        assert (body["code_sha256"], body["worker_code_sha256"], body["worker_code_stale"]) \
            == (serving, None, None), body
    finally:
        _set_worker_code(owner_conn, serving)


# ------------------------------------------------------------------- status coupling
def test_200_and_ok_only_when_every_dependency_passes(healthy_env):
    """Positive control for the degraded cases below."""
    resp, body = _get_health()
    assert (body["db"], body["redis"], body["blob"]) == (True, True, True), body
    assert body["status"] == "ok"
    assert resp.status_code == 200


def test_503_and_degraded_when_a_dependency_fails(unconfigured_env):
    resp, body = _get_health()
    assert body["db"] is True, ("the database probe failed, so this test would pass for "
                                "the wrong reason - is the dev stack up on 5434?")
    assert (body["redis"], body["blob"]) == (False, False)
    assert body["status"] == "degraded"
    assert resp.status_code == 503, (
        "a health response with unavailable dependencies must return 503")


# --------------------------------------------------------------------------- redis
def test_unconfigured_redis_is_reported_false_not_healthy(monkeypatch, fake_redis):
    from meshychat import urls

    monkeypatch.delenv("MESHY_REDIS_URL", raising=False)
    absent = urls.probe_redis()

    monkeypatch.setenv("MESHY_REDIS_URL", fake_redis.url)      # positive control
    present = urls.probe_redis()

    assert present is True, ("the probe could not verify a Redis that IS answering "
                             "DBSIZE, so the negative below proves nothing")
    assert absent is False, "an unconfigured Redis was reported healthy"


def test_redis_probe_fails_on_a_dead_port_and_on_an_error_reply(monkeypatch, fake_redis):
    from meshychat import urls

    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()                                   # nothing listening there now

    monkeypatch.setenv("MESHY_REDIS_URL", f"redis://127.0.0.1:{dead_port}")
    refused = urls.probe_redis(timeout=1)

    err = FakeRedis(reply=b"-ERR unknown command\r\n")
    try:
        monkeypatch.setenv("MESHY_REDIS_URL", err.url)
        errored = urls.probe_redis(timeout=1)
    finally:
        err.close()

    monkeypatch.setenv("MESHY_REDIS_URL", fake_redis.url)      # positive control
    assert urls.probe_redis() is True, "the probe cannot see a working Redis at all"
    assert refused is False, "a closed port was reported healthy"
    assert errored is False, ("a RESP error reply was reported healthy - the probe is "
                              "checking that bytes came back, not that DBSIZE succeeded")


# --------------------------------------------------------------------------- blob
def test_blob_backend_probe_matches_each_configured_health_check(monkeypatch, tmp_path):
    """The probe is the startup store's `health_check` (B11; review of c007c45), so each
    case installs a store the way startup does."""
    from apps.files import blob as blobmod, config as cfgmod
    from meshychat import urls

    _no_store(monkeypatch)
    unset = urls.probe_blob()

    (tmp_path / "blobs").mkdir()
    _activate_fs(monkeypatch, tmp_path / "blobs")
    real_dir = urls.probe_blob()                              # positive control
    shutil.rmtree(tmp_path / "blobs")         # it holds the ready rows' files (047)
    missing_dir = urls.probe_blob()           # the store's blob_dir is gone

    # r2: construction is the liveness signal; construction requires the four variables.
    # Without one, the store is never built: the
    # process refuses to start (tests/test_files_startup.py), so there is no probe.
    monkeypatch.setenv("MESHYCHAT_BLOB_BACKEND", "r2")
    for var in ("MESHYCHAT_R2_ACCOUNT_ID", "MESHYCHAT_R2_BUCKET",
                "MESHYCHAT_R2_ACCESS_KEY_ID", "MESHYCHAT_R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("MESHYCHAT_R2_ENDPOINT", raising=False)
    blobmod.activate(cfgmod.build_blob_config())
    r2_present = urls.probe_blob()                            # positive control
    monkeypatch.delenv("MESHYCHAT_R2_ACCOUNT_ID")
    with pytest.raises(cfgmod.ConfigError, match="MESHYCHAT_R2_ACCOUNT_ID"):
        cfgmod.build_blob_config()

    assert real_dir is True, "an existing blob dir was reported unhealthy"
    assert r2_present is True, "a constructed r2 backend was reported unhealthy"
    assert unset is False, "a process with no blob store was reported healthy"
    assert missing_dir is False, "a blob_dir that is not a directory was reported healthy"


# ----------------------------------------- blob: the ready rows' files (OI-48, 047)
class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def health_log():
    """Every record `meshychat.health` emits. `meshychat` does not propagate to the root
    (settings.LOGGING), so caplog alone would see nothing."""
    lg = logging.getLogger("meshychat.health")
    h = _Records()
    lg.addHandler(h)
    try:
        yield h.records
    finally:
        lg.removeHandler(h)


@pytest.fixture()
def ready_rows(owner_conn):
    """Two ready `file_artifacts` rows of a fresh owner, `[older, newer]`, both newer than
    every row already in the run's database, so a read of the newest N reaches them first.
    Keyed `{owner_id}/{id}`, as `FileService.create` keys them. Deleted afterwards with
    their owner (file_artifacts.owner_id is ON DELETE CASCADE), so no later test inherits a
    ready row whose file is gone."""
    owner = uuid.uuid4()
    owner_conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                       "VALUES (%s, %s, 'product', NULL)",
                       (owner, f"oi48-{owner.hex[:10]}@example.test"))
    try:
        base = owner_conn.execute(
            "SELECT greatest(now(), max(created_at)) FROM file_artifacts").fetchone()[0].astimezone(_dt.timezone.utc)
        keys = []
        for step in (1, 2):
            fid = uuid.uuid4()
            at = base + _dt.timedelta(seconds=step)
            owner_conn.execute(
                "INSERT INTO file_artifacts (id, owner_id, r2_key, sha256, size_bytes, "
                " content_type, status, created_at, finalized_at) "
                "VALUES (%s, %s, %s, %s, 1, 'text/plain', 'ready', %s, %s)",
                (fid, owner, f"{owner}/{fid}", "0" * 64, at, at))
            keys.append(f"{owner}/{fid}")
        yield keys
    finally:
        owner_conn.execute("DELETE FROM dashboard_users WHERE id = %s", (owner,))


def test_blob_is_false_when_a_ready_rows_file_is_missing(healthy_env, ready_rows, health_log):
    """ASDLC OI-48, divergence 047. Production answered `blob: true` for six hours while
    both of its stored files were missing (OI-07): the health check asked only whether
    blob_dir is a directory, and the files had never been placed in it.

    CONTROL: both ready rows' files present, 200 "ok", `blob` true. The refusal differs in
    one variable, one of the two files removed: `blob` false, 503 "degraded", `db` and
    `redis` unchanged, the key set unchanged (the field is still a JSON bool). The number
    goes to the log, never a key: the warning's arguments are the missing count, how many
    rows were read, and the cap, all ints."""
    from apps.files import blob as blobmod
    from meshychat import urls

    _cfg, store = blobmod.active()
    for key in ready_rows:
        store.put_bytes(key, b"x")
    resp, body = _get_health()
    assert (resp.status_code, body["status"], body["blob"]) == (200, "ok", True), body

    os.remove(store.safe_path(ready_rows[0]))
    resp, body = _get_health()
    assert (resp.status_code, body["status"], body["blob"]) == (503, "degraded", False), body
    assert (body["db"], body["redis"]) == (True, True), body
    assert list(body) == HEALTH_FIELDS + B9_FIELDS, list(body)
    counts = [r.args for r in health_log if r.msg == urls.BLOB_FILES_MISSING_LOG]
    assert len(counts) == 1, [r.getMessage() for r in health_log]
    missing, read, cap = counts[0]
    assert (missing, cap) == (1, urls.BLOB_READY_ROWS_CAP) and 2 <= read <= cap, counts
    assert all(type(a) is int for a in counts[0]), counts


def test_blob_is_false_when_the_ready_rows_cannot_be_read(healthy_env):
    """E1: a check that cannot read is not a pass. The database unreachable, the probe
    cannot compare rows with files, and `blob` is false (the route's `db` is false with it,
    so the status it answers does not change). CONTROL, the same store with the database
    reachable: true."""
    from django.test import override_settings

    from meshychat import urls

    reachable = urls.probe_blob()
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    dead_dsn = f"postgresql://meshychat_owner:change-me@127.0.0.1:{dead_port}/x"
    with override_settings(MESHY_DSN=dead_dsn):
        unreachable = urls.probe_blob()
    assert reachable is True, "the probe cannot pass a healthy store; the negative is vacuous"
    assert unreachable is False, "the rows could not be read and blob was reported healthy"


def test_a_row_deleted_while_its_file_is_checked_is_not_counted(healthy_env, ready_rows,
                                                                 owner_conn, monkeypatch):
    """Every delete removes the ROW first and the file second (FileService.soft_delete,
    the grace sweep), so a delete landing between the probe's read of the rows and its stat
    of the files leaves a stat that misses, for a row already gone. The probe counts only
    the missing keys that are STILL ready when it re-reads them, so that delete is not
    reported. CONTROL, differing in one variable: the same row's file removed at the same
    instant with the row left ready is reported."""
    from apps.files import blob as blobmod
    from meshychat import urls

    _cfg, store = blobmod.active()
    for key in ready_rows:
        store.put_bytes(key, b"x")
    real = store.missing_keys

    def removing(victim, *, delete_row):
        def missing_keys(keys):
            assert victim in keys, "the rows were read before this delete, as in the race"
            if delete_row:
                owner_conn.execute("DELETE FROM file_artifacts WHERE r2_key = %s", (victim,))
            os.remove(store.safe_path(victim))
            return real(keys)
        return missing_keys

    monkeypatch.setattr(store, "missing_keys", removing(ready_rows[0], delete_row=False))
    kept = urls.probe_blob()
    store.put_bytes(ready_rows[0], b"x")
    monkeypatch.setattr(store, "missing_keys", removing(ready_rows[0], delete_row=True))
    deleted = urls.probe_blob()
    assert kept is False, "a ready row whose file vanished was not reported"
    assert deleted is True, "a row deleted mid-check was reported as a missing file"


def test_the_check_reads_the_newest_rows_up_to_its_cap_and_no_further(healthy_env, ready_rows,
                                                                      monkeypatch):
    """What the bound costs, stated: only the newest BLOB_READY_ROWS_CAP ready rows are
    read, so a file missing from an OLDER row is not seen. The older of the two rows loses
    its file: with the cap at 1 only the newer is read and `blob` stays true. CONTROL, the
    cap at 2: false."""
    from apps.files import blob as blobmod
    from meshychat import urls

    _cfg, store = blobmod.active()
    store.put_bytes(ready_rows[1], b"x")               # the newer; the older has none
    store.safe_path(ready_rows[0]).unlink(missing_ok=True)
    monkeypatch.setattr(urls, "BLOB_READY_ROWS_CAP", 1)
    capped = urls.probe_blob()
    monkeypatch.setattr(urls, "BLOB_READY_ROWS_CAP", 2)
    reached = urls.probe_blob()
    assert reached is False, "the older row's missing file was not seen within the cap"
    assert capped is True, "a row past the cap was read"


def test_fs_missing_keys_is_one_stat_per_key_and_names_what_cannot_be_served(tmp_path,
                                                                              monkeypatch):
    """`FsBlobStore.missing_keys`: a key naming a regular file is present. Missing: a key
    with nothing at it, a key naming a DIRECTORY (`get_stream` could not open it), and a
    key `safe_path` refuses (it can never be served). CONTROL: the present key alone, none
    missing. And no file is opened: the check is a stat, never a read (CONTROL for that
    recorder: `get_stream` of the same key is seen to open it)."""
    import builtins

    from apps.files import blob as blobmod, config as cfgmod

    store = blobmod.FsBlobStore(cfgmod.FsConfig(
        blob_dir=str(tmp_path), token_secret="00" * 32, public_base_url="https://meshy.test"))
    store.put_bytes("o/present", b"x")
    (tmp_path / "o" / "a-directory").mkdir()
    opened = []
    real_open = builtins.open

    def recording_open(*a, **k):
        opened.append(a[0] if a else k.get("file"))
        return real_open(*a, **k)

    monkeypatch.setattr(builtins, "open", recording_open)
    missing = store.missing_keys(["o/present", "o/absent", "o/a-directory", "../escapes",
                                  "/abs"])
    present = store.missing_keys(["o/present"])
    opened_by_the_check = list(opened)
    with store.get_stream("o/present"):
        pass
    monkeypatch.undo()
    assert present == [], "a key with its file was reported missing"
    assert missing == ["o/absent", "o/a-directory", "../escapes", "/abs"], missing
    assert len(opened) == 1, f"the recorder did not see get_stream open the file: {opened}"
    assert opened_by_the_check == [], f"the check opened files: {opened_by_the_check}"


# --------------------------------------------------------------------------- db probe
def test_db_probe_is_false_when_postgres_is_unreachable():
    from django.test import override_settings

    from meshychat import urls

    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()

    dead_dsn = f"postgresql://meshychat_owner:change-me@127.0.0.1:{dead_port}/x"
    with override_settings(MESHY_DSN=dead_dsn):
        unreachable = urls.probe_db(timeout=2)

    reachable = urls.probe_db()                               # positive control

    assert reachable is True, "the probe cannot see the real database; the negative is vacuous"
    assert unreachable is False, "an unreachable database was reported healthy"


def test_configured_settings_win_over_the_environment_for_the_dsn(monkeypatch):
    """Precedence is settings > env > default, and it is load-bearing.

    An operator who exports MESHY_DSN gets it because settings.py reads the env at
    startup. But once Django is configured, settings are the single source of truth: a
    process that mutates os.environ mid-flight must NOT silently move the database out
    from under the request path.
    """
    from django.test import override_settings

    monkeypatch.setenv("MESHY_DSN", "postgresql://nobody@127.0.0.1:1/never")
    assert dbconn.dsn() == DSN, "an env mutation overrode configured settings"

    # Positive control: the resolver is not just returning a constant.
    with override_settings(MESHY_DSN="postgresql://elsewhere@127.0.0.1:2/db"):
        assert dbconn.dsn() == "postgresql://elsewhere@127.0.0.1:2/db"


# ------------------------------------------------------- the RLS-bypass refusal
def test_serving_connection_refuses_a_connection_that_bypasses_rls(owner_conn, app_conn):
    """The core refusal. `meshychat_owner` has rolbypassrls; `meshychat_app` does not."""
    with pytest.raises(dbconn.RlsBypassError, match="BYPASSES"):
        dbconn.assert_serving_connection(owner_conn)

    # Positive control: the guard is not simply raising on everything.
    assert dbconn.assert_serving_connection(app_conn) is None
    assert rls.current_role(app_conn) == "meshychat_app"


def test_request_helpers_run_the_same_refusal(owner_conn, app_conn):
    """A view cannot route around the guard by asking for a context directly."""
    with pytest.raises(dbconn.RlsBypassError):
        with dbconn.request_peer_context(owner_conn, peer_id="00000000-0000-0000-0000-000000000001"):
            pass
    with pytest.raises(dbconn.RlsBypassError):
        with dbconn.request_server_internal(owner_conn):
            pass

    # Positive control: both contexts work, and actually bind, on a correct connection.
    peer = "11111111-1111-1111-1111-111111111111"
    with dbconn.request_peer_context(app_conn, peer_id=peer):
        assert rls.context_values(app_conn)["peer_id"] == peer
    with dbconn.request_server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.server_internal', true)")
            assert cur.fetchone()[0] == "1"


def test_serving_connection_refuses_a_non_autocommit_connection():
    """SET LOCAL on a non-autocommit connection binds to a savepoint and leaks."""
    conn = psycopg.connect(DSN, autocommit=False)
    try:
        rls.set_role(conn)                     # correct role, wrong transaction mode
        with pytest.raises(dbconn.NonAutocommitError):
            dbconn.assert_serving_connection(conn)

        # Positive control: the SAME connection, same role, passes once autocommit is on.
        # commit(), not rollback(): SET ROLE is transactional, so a rollback here would
        # put the connection back on `meshy` and the control would fail as a BYPASS -
        # passing the test for a reason that has nothing to do with autocommit.
        conn.commit()
        conn.autocommit = True
        assert dbconn.assert_serving_connection(conn) is None
    finally:
        conn.close()


def test_dbconn_connect_gives_a_connection_that_passes_the_guard():
    """dbconn.connect is the production path; it must produce a safe connection."""
    with dbconn.serving_connection(DSN) as conn:
        assert conn.autocommit is True
        assert rls.current_role(conn) == "meshychat_app"
        rls.assert_rls_active(conn)


# --------------------------------------------------------------------------- middleware
class _Req:
    """A bare request object. The middleware sets `.db` and `.identity` on it itself;
    nothing here stands in for the database - the connections below are real."""

    def __init__(self):
        self.path = "/v1/whatever"


def _run_middleware(capture):
    """Run one request and record the DATABASE's view of the context, or None if the
    request never opened a connection."""

    def get_response(request):
        capture.append(rls.context_values(request.db.connection)
                       if request.db.opened else None)
        return "response"

    return DatabaseConnectionMiddleware(get_response)(_Req())


def test_middleware_binds_the_peer_context_from_the_identity(monkeypatch):
    from apps.core import middleware as mw

    peer = "0447bf92-25fb-5230-b531-5a0d9c00d8c7"
    owner = "33333333-3333-3333-3333-333333333333"

    bound: list = []
    monkeypatch.setattr(mw, "_resolver",
                        lambda request: Identity(peer_id=peer, owner_id=owner))
    assert _run_middleware(bound) == "response"

    anon: list = []
    monkeypatch.setattr(mw, "_resolver", mw.anonymous_resolver)
    assert _run_middleware(anon) == "response"

    # Positive control first: an identity really reaches current_setting inside the view.
    assert bound[0] == {"peer_id": peer, "owner_id": owner, "role": "meshychat_app"}, bound
    # Negative: no identity binds nothing - and opens no connection at all, which is why
    # /v1/health can still answer while Postgres is down.
    assert anon[0] is None, f"an anonymous request bound a context: {anon}"


def test_middleware_closes_the_connection_even_when_the_view_raises(monkeypatch):
    from apps.core import middleware as mw

    monkeypatch.setattr(mw, "_resolver", lambda request: Identity(peer_id=None, owner_id=None))
    holder: list = []

    def get_response(request):
        holder.append(request.db)
        request.db.connection                 # force the lazy open
        raise ValueError("view blew up")

    middleware = DatabaseConnectionMiddleware(get_response)
    with pytest.raises(ValueError):
        middleware(_Req())
    assert holder[0].opened is False, "the request connection outlived the request"


def test_middleware_refuses_to_serve_on_an_rls_bypassing_connection():
    """The guard is on the REQUEST path, not only in dbconn's own constructor."""
    leaked: list = []

    def bypassing_factory():
        conn = psycopg.connect(DSN, autocommit=True)     # role `meshy`: bypasses RLS
        leaked.append(conn)
        return conn

    def get_response(request):
        return request.db.connection

    mw = DatabaseConnectionMiddleware(get_response)
    mw.connection_factory = bypassing_factory
    with pytest.raises(dbconn.RlsBypassError):
        mw(_Req())
    assert leaked and leaked[0].closed, "the refused connection was left open"

    # Positive control: the default factory serves the same request fine.
    ok = DatabaseConnectionMiddleware(lambda request: rls.current_role(request.db.connection))
    assert ok(_Req()) == "meshychat_app"


def test_request_database_is_lazy():
    """Nothing connects until something asks - the reason /v1/health survives a dead DB."""
    calls: list = []

    def factory():
        calls.append(1)
        return dbconn.connect(DSN)

    rd = RequestDatabase(factory)
    assert calls == [] and rd.opened is False
    try:
        assert rd.connection is not None            # positive control: it does connect
        assert calls == [1] and rd.opened is True
    finally:
        rd.close()


def test_a_resolver_can_reject_the_request_with_the_json_error_envelope(monkeypatch):
    from apps.core import middleware as mw

    def reject(request):
        raise Unauthenticated()

    monkeypatch.setattr(mw, "_resolver", reject)
    resp = DatabaseConnectionMiddleware(lambda r: "should not be reached")(_Req())
    assert resp.status_code == 401
    # The error body has `error` and `message`; E_AUTH_MISSING identifies this refusal.
    assert json.loads(resp.content.decode()) == {"error": "E_AUTH_MISSING",
                                                 "message": "missing auth"}

    # Positive control: a resolver that does NOT reject reaches the view.
    monkeypatch.setattr(mw, "_resolver", mw.anonymous_resolver)
    assert DatabaseConnectionMiddleware(lambda r: "reached")(_Req()) == "reached"


def test_settings_defaults_match_the_documented_behavior():
    from django.conf import settings

    assert settings.DEBUG is False, "DEBUG must default off"
    assert settings.APPEND_SLASH is False, (
        "APPEND_SLASH would redirect /v1/health/ instead of returning 404")
    assert settings.ASGI_APPLICATION == "meshychat.asgi.application"
    assert settings.CHANNEL_LAYERS["default"]["BACKEND"].endswith("InMemoryChannelLayer")
    assert "testserver" in settings.ALLOWED_HOSTS, "ALLOWED_HOSTS is not env-driven"
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"


def test_trailing_slash_is_not_redirected(healthy_env):
    resp = Client().get("/v1/health/")
    assert resp.status_code == 404, (
        "a trailing slash must return 404 without a redirect")
    assert Client().get("/v1/health").status_code == 200   # positive control


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
