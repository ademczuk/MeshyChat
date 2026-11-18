"""The serving process's boot and request bounds: status items E14, E15 and F17.

E14. `docs/V1-DESIGN-RULES.md` E6: "the deploy gate asserts schema is a superset of code, not
     'the process started'". Only `run_jobs` and `run_call_worker` asked
     `schema_sets.assert_schema_complete`; the served entrypoint (`meshychat.asgi`, what daphne
     and uvicorn import) booted on a database missing a migration and served it. The boot now
     asks, and a database it cannot ask refuses the boot too.
E15. A request connection carried no `statement_timeout`. Upstream's pool installs one on every
     connection (crates/meshy-db/src/pool.rs `build_pool`: `MESHYCHAT_DB_STATEMENT_TIMEOUT_MS`,
     default 30_000, 0 disables); `dbconn.connect` now does the same when the caller names none.
F17. The /v1/health deadline read `'PROBES' in globals()`, which is always false (the executor
     is `_PROBES`), so the whole-set bound was always three probe timeouts (6 s) where the
     route states, and upstream's `tokio::join!` of three 2 s timeouts gives, about 2 s.

Every negative here has its positive control in the same test (C1). The boot tests run the
served entrypoint in a fresh interpreter, as daphne does: an in-process import is cached and
would not boot again.

Run:  pytest tests/test_serving_boot.py -v
Needs the run's database (tests/conftest.py builds it).
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_DSN", DSN)

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

from apps.core import dbconn, schema_sets  # noqa: E402

DIVERGENCE_SET = "meshychat"
DIVERGENCE_LEDGER = next(s.ledger for s in schema_sets.SETS if s.name == DIVERGENCE_SET)
TIMEOUT_ENV = "MESHYCHAT_DB_STATEMENT_TIMEOUT_MS"

BOOT = "import meshychat.asgi\nprint('BOOTED', flush=True)\n"


# ============================================================================ E14, the boot

def _boot(dsn: str) -> subprocess.CompletedProcess:
    """A FRESH interpreter importing the served entrypoint against `dsn`, as daphne does."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update({"DJANGO_SETTINGS_MODULE": "meshychat.settings", "MESHY_DSN": dsn,
                "PYTHONPATH": str(ROOT)})
    return subprocess.run([sys.executable, "-c", BOOT], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=180)


@contextlib.contextmanager
def _one_divergence_unrecorded():
    """Commit the run database into "one divergence migration not applied", then put it back.

    Committed, not rolled back, because the booting process is another session. Only on the
    run's OWN database (tests/conftest.py names it meshy_test_<pid>_...): never the shared one.
    The row is restored as it was in `finally`.
    """
    name = DSN.rsplit("/", 1)[-1].split("?", 1)[0]
    if not name.startswith("meshy_test_"):
        pytest.skip(f"refusing to edit the ledger of {name!r}: not this run's own database")
    cols = "version, description, installed_on, success, checksum, execution_time"
    with psycopg.connect(DSN, autocommit=True) as owner:
        row = owner.execute(
            f"DELETE FROM {DIVERGENCE_LEDGER} WHERE version = "
            f"(SELECT max(version) FROM {DIVERGENCE_LEDGER}) RETURNING {cols}").fetchone()
        assert row is not None, "positive control: the divergence ledger has rows to remove"
        try:
            yield row[0]
        finally:
            owner.execute(f"INSERT INTO {DIVERGENCE_LEDGER} ({cols}) "
                          "VALUES (%s, %s, %s, %s, %s, %s)", row)


def test_the_served_entrypoint_refuses_a_database_missing_one_migration():
    """THE ITEM. Control first: the same boot on the complete database serves."""
    booted = _boot(DSN)
    assert booted.returncode == 0, booted.stderr[-1500:]
    assert "BOOTED" in booted.stdout

    with _one_divergence_unrecorded() as version:
        refused = _boot(DSN)
    assert refused.returncode != 0, (
        f"meshychat.asgi booted on a database missing {DIVERGENCE_SET} {version}: "
        + refused.stdout[-400:])
    assert "BOOTED" not in refused.stdout
    assert "SchemaIncomplete" in refused.stderr, refused.stderr[-1500:]
    assert f"{DIVERGENCE_SET}: 1 unapplied [{version}]" in refused.stderr, refused.stderr[-1500:]

    # And the restore held: the next boot serves again.
    again = _boot(DSN)
    assert again.returncode == 0, again.stderr[-1500:]


def test_the_served_entrypoint_refuses_a_database_it_cannot_ask():
    """An unreachable database is not a verified schema. Control: the reachable one boots."""
    assert _boot(DSN).returncode == 0
    dead = _boot("postgresql://meshychat_owner:change-me@127.0.0.1:1/meshy_nowhere")
    assert dead.returncode != 0, dead.stdout[-400:]
    assert "BOOTED" not in dead.stdout
    assert "SchemaUnverified" in dead.stderr, dead.stderr[-1500:]


def test_require_complete_schema_raises_the_distinct_type_and_passes_a_complete_one():
    """In-process, on a rolled-back connection: the check itself, without a boot."""
    import meshychat.asgi as asgi

    with psycopg.connect(DSN) as conn:
        conn.autocommit = False

        @contextlib.contextmanager
        def opener():
            yield conn

        assert asgi.require_complete_schema(opener) is None      # control: complete
        conn.execute(f"DELETE FROM {DIVERGENCE_LEDGER}")
        with pytest.raises(schema_sets.SchemaIncomplete, match=DIVERGENCE_SET):
            asgi.require_complete_schema(opener)
        conn.rollback()

    @contextlib.contextmanager
    def unreachable():
        raise psycopg.OperationalError("connection refused")
        yield  # pragma: no cover

    with pytest.raises(asgi.SchemaUnverified) as exc:
        asgi.require_complete_schema(unreachable)
    assert not isinstance(exc.value, schema_sets.SchemaIncomplete), (
        "E1: an unreachable database must not read as a schema hole, nor the reverse")


# ============================================================ E15, the request connection

def _timeout_of(conn) -> str:
    return conn.execute("SHOW statement_timeout").fetchone()[0]


def test_a_request_connection_carries_the_configured_statement_timeout(monkeypatch):
    """Every path a request takes to the database: connect(), serving_connection() and the
    middleware's default factory. Control: an explicit bound still wins."""
    from apps.core.middleware import DatabaseConnectionMiddleware

    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    with contextlib.closing(dbconn.connect(DSN)) as conn:
        assert _timeout_of(conn) == "30s"
    with dbconn.serving_connection(DSN) as conn:
        assert _timeout_of(conn) == "30s"
    with override_settings(MESHY_DSN=DSN):
        mw = DatabaseConnectionMiddleware(lambda r: None)
        with contextlib.closing(mw._factory()) as conn:
            assert _timeout_of(conn) == "30s"
    with contextlib.closing(dbconn.connect(DSN, statement_timeout_ms=1500)) as conn:
        assert _timeout_of(conn) == "1500ms"                     # control: explicit wins


def test_a_slow_request_query_is_cancelled_and_a_fast_one_is_not(monkeypatch):
    """THE ITEM: the server cancels the statement. Control: under the bound it answers."""
    monkeypatch.setenv(TIMEOUT_ENV, "300")
    with dbconn.serving_connection(DSN) as conn:
        assert conn.execute("SELECT pg_sleep(0.05), 1").fetchone()[1] == 1  # control
        started = time.monotonic()
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(3)")
        assert time.monotonic() - started < 2.5
        # The session survives a cancelled statement (autocommit), and still carries the bound.
        assert _timeout_of(conn) == "300ms"


def test_zero_disables_and_garbage_falls_back_to_the_default(monkeypatch):
    """Upstream's `env_timeout_ms`: 0 disables, a value that does not parse is the default."""
    monkeypatch.setenv(TIMEOUT_ENV, "0")
    with contextlib.closing(dbconn.connect(DSN)) as conn:
        assert _timeout_of(conn) == "0"
        assert conn.execute("SELECT pg_sleep(0.4), 1").fetchone()[1] == 1
    for garbage in ("-1", "thirty", "1.5", ""):
        monkeypatch.setenv(TIMEOUT_ENV, garbage)
        assert dbconn.default_statement_timeout_ms() == dbconn.DEFAULT_STATEMENT_TIMEOUT_MS
    monkeypatch.setenv(TIMEOUT_ENV, " 1234 ")
    assert dbconn.default_statement_timeout_ms() == 1234        # control: a value is read


def test_the_bound_keeps_options_the_dsn_already_carries(monkeypatch):
    """`options=` in the DSN is not clobbered by the bound. Control: the bound is there too."""
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    sep = "&" if "?" in DSN else "?"
    with_opts = f"{DSN}{sep}options=-c%20application_name%3Dboot_probe"
    with contextlib.closing(dbconn.connect(with_opts)) as conn:
        assert conn.execute("SHOW application_name").fetchone()[0] == "boot_probe"
        assert _timeout_of(conn) == "30s"
    with contextlib.closing(dbconn.connect(DSN)) as conn:
        assert conn.execute("SHOW application_name").fetchone()[0] != "boot_probe"


def test_every_connection_reads_time_in_utc_whatever_the_server_zone(monkeypatch):
    """A session zone with a clock change makes Python misread intervals across it (the
    25 Oct 2026 grace-window failure), so connect() pins TimeZone=UTC. Control: psycopg
    alone, under the same PGOPTIONS, does get Europe/Vienna, so the pin is what decides."""
    import datetime as _dt
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    monkeypatch.setenv("PGOPTIONS", "-c TimeZone=Europe/Vienna")
    with contextlib.closing(psycopg.connect(DSN)) as raw:
        assert raw.execute("SHOW TimeZone").fetchone()[0] == "Europe/Vienna"   # control
    with contextlib.closing(dbconn.connect(DSN)) as conn:
        assert conn.execute("SHOW TimeZone").fetchone()[0] == "UTC"
        now = conn.execute("SELECT now()").fetchone()[0]
        assert now.utcoffset() == _dt.timedelta(0)
        assert _timeout_of(conn) == "30s"                       # the bound is still there
    with dbconn.serving_connection(DSN) as conn:
        assert conn.execute("SHOW TimeZone").fetchone()[0] == "UTC"


# ============================================================== F17, the health deadline

def _stub_probes(monkeypatch, redis):
    import meshychat.urls as u
    monkeypatch.setattr(u, "probe_db", lambda *a, **k: True)
    monkeypatch.setattr(u, "probe_blob", lambda *a, **k: True)
    monkeypatch.setattr(u, "probe_redis", redis)
    return u


def test_health_answers_within_one_probe_timeout_with_a_stalled_probe(monkeypatch):
    """THE ITEM. The three probes run concurrently, each under PROBE_TIMEOUT, so the route
    answers in about PROBE_TIMEOUT (health.rs:19-21), not three of them.

    Control, first: a probe that answers inside the bound is honoured, so the deadline is not
    passing by reporting everything false."""
    def answers_in_half_a_second(*_a, **_k):
        time.sleep(0.5)
        return True

    u = _stub_probes(monkeypatch, answers_in_half_a_second)
    resp = Client().get("/v1/health")
    assert resp.status_code == 200, resp.content
    assert resp.json()["redis"] is True

    release = threading.Event()

    def stalled(*_a, **_k):
        release.wait(30)
        return True

    _stub_probes(monkeypatch, stalled)
    try:
        started = time.monotonic()
        resp = Client().get("/v1/health")
        elapsed = time.monotonic() - started
    finally:
        release.set()                                  # free the pool's worker
    assert resp.status_code == 503
    assert resp.json()["redis"] is False and resp.json()["db"] is True
    assert elapsed < u.PROBE_TIMEOUT + 0.5, (
        f"/v1/health took {elapsed:.1f}s with one stalled probe; the probes are concurrent, "
        f"so the bound is one probe timeout ({u.PROBE_TIMEOUT}s)")
    assert elapsed >= u.PROBE_TIMEOUT - 0.1, "control: the stalled probe was waited for"


# ======================================================= review of fin/w1-boot, 2026-09-26

def test_the_refusal_names_a_command_that_runs():
    """Finding 1. SchemaIncomplete is now the serving process's crash message, so the command
    it tells the operator to run must exist. It said `sqlmigrate.py --dsn DSN --all`, and
    sqlmigrate has no --all (argparse exit 2). Run the command the refusal names, with the
    run's database for DSN: it applies every set (nothing pending here) and exits 0.

    Control: the same harness with an unknown flag appended exits 2, so a bad command is caught
    rather than passing because the harness never ran anything."""
    import shlex

    with psycopg.connect(DSN) as conn:
        conn.autocommit = False
        conn.execute(f"DELETE FROM {DIVERGENCE_LEDGER}")
        with pytest.raises(schema_sets.SchemaIncomplete) as refusal:
            schema_sets.assert_schema_complete(conn)
        conn.rollback()
    marker = "Apply every set with: "
    assert marker in str(refusal.value), str(refusal.value)
    named = shlex.split(str(refusal.value).rsplit(marker, 1)[1].strip().rstrip("."))
    assert named[0] == "python" and "DSN" in named, named
    argv = [sys.executable] + [DSN if a == "DSN" else a for a in named[1:]]

    def run(args):
        return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=300)

    ran = run(argv)
    assert ran.returncode == 0, (named, ran.stdout[-800:], ran.stderr[-800:])
    assert f"across {len(schema_sets.SETS)} sets" in ran.stdout, ran.stdout[-800:]
    bogus = run(argv + ["--no-such-flag"])                          # control
    assert bogus.returncode == 2 and "unrecognized arguments" in bogus.stderr, bogus.stderr


def _divergence_set_from(sql_dir: Path) -> tuple:
    return tuple(schema_sets.MigrationSet(name=s.name, sql_dir=sql_dir, ledger=s.ledger, why=s.why)
                 if s.name == DIVERGENCE_SET else s for s in schema_sets.SETS)


def test_a_local_file_refusal_is_not_labelled_an_unreachable_database(monkeypatch, tmp_path):
    """Finding 2. A CRLF migration on disk makes `discover()` raise ValueError while the
    database answers normally; the boot said "the database did not answer". It must surface
    as the file refusal it is. Control: the same copied directory, LF, passes the check."""
    import shutil

    import meshychat.asgi as asgi

    real = next(s.sql_dir for s in schema_sets.SETS if s.name == DIVERGENCE_SET)
    copy = tmp_path / real.name
    shutil.copytree(real, copy)
    monkeypatch.setattr(schema_sets, "SETS", _divergence_set_from(copy))

    @contextlib.contextmanager
    def opener():
        with psycopg.connect(DSN) as conn:
            yield conn

    assert asgi.require_complete_schema(opener) is None             # control: LF copy passes
    newest = sorted(copy.glob("*.sql"))[-1]
    newest.write_bytes(newest.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match="CRLF") as exc:
        asgi.require_complete_schema(opener)
    assert not isinstance(exc.value, asgi.SchemaUnverified), (
        "a file on disk refused by discover() was reported as an unreachable database")


def test_a_permission_denial_is_not_labelled_an_unreachable_database():
    """Finding 2. A role that cannot read the ledger reaches the database and is REFUSED by
    it; that is a grant to fix, not a database to start. Control: the owner's read of the
    same ledgers on the same connection answers."""
    import meshychat.asgi as asgi

    with psycopg.connect(DSN) as conn:
        conn.autocommit = False

        @contextlib.contextmanager
        def opener():
            yield conn

        assert asgi.require_complete_schema(opener) is None           # control
        conn.execute("CREATE ROLE meshy_boot_noread NOLOGIN")          # rolled back below
        conn.execute("SET ROLE meshy_boot_noread")
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            asgi.require_complete_schema(opener)
        assert not isinstance(exc.value, asgi.SchemaUnverified)
        conn.rollback()


def test_the_boot_gives_up_on_a_silent_database_within_its_connect_bound(monkeypatch):
    """Review M-C. The boot's own connect bound, not psycopg's default (none), ends the wait
    on a database that accepts TCP and never answers. A local listener that never accepts is
    that database. Control: the wait really happened (the refusal was not instantaneous)."""
    import socket

    import meshychat.asgi as asgi

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    silent = f"postgresql://meshychat_owner:unused@127.0.0.1:{port}/meshy_silent"
    monkeypatch.setattr(asgi, "BOOT_CONNECT_TIMEOUT_S", 2)
    outcome: dict = {}

    def boot():
        started = time.monotonic()
        try:
            asgi.require_complete_schema()
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted on below
            outcome["exc"] = exc
        outcome["elapsed"] = time.monotonic() - started

    # settings.MESHY_DSN wins over the environment in dbconn.dsn(), so the override is there.
    with override_settings(MESHY_DSN=silent):
        assert dbconn.dsn() == silent                  # control: the boot will ask this one
        worker = threading.Thread(target=boot, daemon=True)
        worker.start()
        worker.join(asgi.BOOT_CONNECT_TIMEOUT_S + 6)
        hung = worker.is_alive()
        listener.close()                               # frees a hung connect either way
        worker.join(10)
    assert not hung, "the boot waited past its connect bound on a silent database"
    assert isinstance(outcome.get("exc"), asgi.SchemaUnverified), outcome
    assert outcome["elapsed"] >= asgi.BOOT_CONNECT_TIMEOUT_S - 0.5, (
        "control: the silent database was waited on, not refused outright", outcome)


def test_an_out_of_range_timeout_falls_back_instead_of_breaking_every_connection(
        monkeypatch, caplog):
    """Finding 3. statement_timeout is a Postgres int: 3000000000 made every connect() fail
    with "Value exceeds integer range", the outcome the fallback exists to prevent. It is
    now the default, logged. Control: the largest value Postgres accepts is read and applied,
    and logs nothing."""
    import logging

    log = logging.getLogger("meshychat.db")          # settings: meshychat does not propagate
    log.addHandler(caplog.handler)
    try:
        monkeypatch.setenv(TIMEOUT_ENV, "3000000000")
        assert dbconn.default_statement_timeout_ms() == dbconn.DEFAULT_STATEMENT_TIMEOUT_MS
        assert "3000000000" in caplog.text, "the fallback is logged, so the typo is visible"
        with contextlib.closing(dbconn.connect(DSN)) as conn:
            assert _timeout_of(conn) == "30s"
        caplog.clear()
        monkeypatch.setenv(TIMEOUT_ENV, "2147483647")
        assert dbconn.default_statement_timeout_ms() == 2147483647
        assert caplog.text == ""                                         # control
    finally:
        log.removeHandler(caplog.handler)
    with contextlib.closing(dbconn.connect(DSN)) as conn:
        assert conn.execute("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
                            ).fetchone()[0] == "2147483647"


def test_the_bound_keeps_pgoptions(monkeypatch):
    """Review M-D. `options=` replaces PGOPTIONS in libpq, so the bound must carry PGOPTIONS
    forward. Control: without PGOPTIONS the probe setting is absent on a new connection."""
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    probe = "SELECT current_setting('meshy.boot_probe', true)"
    monkeypatch.setenv("PGOPTIONS", "-c meshy.boot_probe=pgoptions")
    with contextlib.closing(dbconn.connect(DSN)) as conn:
        assert conn.execute(probe).fetchone()[0] == "pgoptions"
        assert _timeout_of(conn) == "30s"
    monkeypatch.delenv("PGOPTIONS")
    with contextlib.closing(dbconn.connect(DSN)) as conn:
        assert conn.execute(probe).fetchone()[0] in (None, "")          # control


def test_the_bound_overrides_a_statement_timeout_the_dsn_or_pgoptions_carries(monkeypatch):
    """Review M-H and finding 4, pinned as documented behaviour (DIVERGENCES, E15 paragraph).
    A `statement_timeout` in the DSN's options or in PGOPTIONS is overridden: by an explicit
    bound (the docstring's claim) and by the 30 s default alike, as upstream's after_connect
    SET overrides it. Only MESHYCHAT_DB_STATEMENT_TIMEOUT_MS=0 lifts the default.
    Control: psycopg alone, on the same DSN, does carry the DSN's 0."""
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    monkeypatch.delenv("PGOPTIONS", raising=False)
    sep = "&" if "?" in DSN else "?"
    lifted = f"{DSN}{sep}options=-c%20statement_timeout%3D0"
    with contextlib.closing(psycopg.connect(lifted)) as raw:
        assert _timeout_of(raw) == "0"                                  # control
    with contextlib.closing(dbconn.connect(lifted, statement_timeout_ms=1500)) as conn:
        assert _timeout_of(conn) == "1500ms"
    with contextlib.closing(dbconn.connect(lifted)) as conn:
        assert _timeout_of(conn) == "30s"
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=0")
    with contextlib.closing(dbconn.connect(DSN)) as conn:
        assert _timeout_of(conn) == "30s"
    monkeypatch.setenv(TIMEOUT_ENV, "0")
    with contextlib.closing(dbconn.connect(lifted)) as conn:
        assert _timeout_of(conn) == "0"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
