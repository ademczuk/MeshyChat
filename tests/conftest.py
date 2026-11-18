"""Give every test RUN its own database, cloned from the migrated template.

WHY: with ~560 tests sharing one database, failures became order-dependent. Two examples
measured here, both of which passed in isolation and failed in a full run:

  * `test_server_internal_is_a_separate_mechanism` — `shared_memory.payload_jcs_sha256` is
    UNIQUE across the whole table (the substrate deduplicates by content digest), so a test
    writing fixed content succeeded once and collided on every later run.
  * `test_a_deleted_peer_keeps_a_valid_jwt_but_loses_access` and
    `test_a_hash_covered_update_nulls_a_now_stale_signature` — green alone, red together.

This is how it happens: in another suite, eleven test files defaulted to a shared dev DSN and
issued 46 DELETE/TRUNCATE statements against it. A suite that pollutes its own database
reports failures that are about the ORDER, not the code, and people learn to re-run rather
than read.

WHAT THIS DOES: at session start it creates `meshy_test_<pid>`, applies the 104 migrations
AND the divergences into it, points `MESHY_TEST_DSN` there, and drops it at the
end. Cross-RUN pollution is then impossible, and every run exercises the real migration
path. (Cloning with TEMPLATE would be faster but needs EXCLUSIVE access to the template,
which fails whenever anything else is connected - during a parallel build, always.)

WHAT THIS DOES NOT DO: isolate tests from EACH OTHER inside one run. That needs either
per-test transactions (awkward: the RLS context is transaction-bound by design) or unique
fixture data per test. Where an intra-run collision shows up, the test must scope its own
query rather than assume an empty table. Say so rather than papering over it.

Opt out with MESHY_NO_TEST_DB=1 to run against the shared database on purpose.

EVERY RUN NAMES ITS SERVER: MESHY_TEMPLATE_DSN, with no default (operator decision 41,
2026-09-26). The run's database is created and dropped on the server it names, so a run
without it is REFUSED here, before any connection, MESHY_NO_TEST_DB=1 included. It used to
default to 127.0.0.1:5434, which is this project's dev Postgres on HQD and ANOTHER project's
Postgres on HQB: a plain `pytest` there created and dropped databases on a server nobody
chose. Only the server part is used (the run's database gets its own name), so
`postgresql://USER:PW@127.0.0.1:5434/postgres` is the usual form on HQD.
tests/test_template_dsn_required.py holds the proof.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest


_NO_TEMPLATE_DSN = (
    "REFUSED: MESHY_TEMPLATE_DSN is not set. Every pytest run must name the Postgres server "
    "its database is created and dropped on (operator decision 41, 2026-09-26); there is no "
    "default, because 127.0.0.1:5434 is this project's dev database on one host and another "
    "project's Postgres on another, and a default would make and drop databases there. Set "
    "it, e.g. MESHY_TEMPLATE_DSN=postgresql://USER:PW@127.0.0.1:5434/postgres (only the "
    "server part is used), and run again. MESHY_NO_TEST_DB=1 does not lift this. Nothing "
    "was connected to.")


def _template_dsn_named_or_refuse() -> str:
    """MESHY_TEMPLATE_DSN as the runner set it, or a UsageError BEFORE any connection: this
    runs at import, ahead of the live-run refusal and of `_provision`, which is the first
    thing here that connects. Blank counts as unset."""
    named = (os.environ.get("MESHY_TEMPLATE_DSN") or "").strip()
    if not named:
        raise pytest.UsageError(_NO_TEMPLATE_DSN)
    return named


TEMPLATE_DSN = _template_dsn_named_or_refuse()


def _admin_dsn(dsn: str) -> str:
    """Same server, `postgres` database — you cannot drop a database you are connected to."""
    head, _, _ = dsn.rpartition("/")
    return head + "/postgres"


def _template_name(dsn: str) -> str:
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def _publish(dsn: str) -> str:
    """Point EVERY resolver at the run's database, on every path out of _provision().

    THIS EXISTS BECAUSE THE TWO PATHS DIVERGED AND NOTHING NOTICED. The clone fast path
    returned early, publishing only MESHY_TEST_DSN, while the migrate path published
    MESHY_DSN and corrected the frozen setting as well. The suite was green either way -
    until the template happened to be current, the clone path was taken, and
    tests/test_dsn_resolvers_agree.py went red with four failures saying routes were
    reading the SHARED database.

    That is a semantic merge conflict: two correct edits to one function, no textual
    overlap, so git merged them cleanly into something neither author wrote. The fix is
    structural rather than another assignment - one exit, so a third path cannot forget a
    fourth variable.
    """
    os.environ["MESHY_TEST_DSN"] = dsn
    os.environ["MESHY_DSN"] = dsn
    _correct_a_setting_frozen_before_us(dsn)
    _refuse_a_hijacked_application_dsn(dsn)
    return dsn


def _record_the_run_database(name: str) -> None:
    """Append the run's database name to the file MESHY_TEST_DB_RECORD names, BEFORE the database exists.

    For a runner that may have to kill this process: tools/mutants/run.py SIGKILLs a pytest that ignores its
    interrupt, and then pytest_sessionfinish below never drops the database, so the runner drops what this file
    names. A pytest started by a test inherits the variable and appends its own name. Unset (every ordinary run):
    nothing is written and nothing else changes."""
    record = os.environ.get("MESHY_TEST_DB_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as f:
            f.write(name + "\n")


def _provision() -> str | None:
    """Create and migrate the run's database AT IMPORT TIME.

    ORDERING IS THE WHOLE POINT. pytest imports conftest BEFORE it imports test modules,
    and the test modules read `MESHY_TEST_DSN` at module level. A session fixture runs
    AFTER collection, so it would set the variable too late and every module would keep
    the shared database - while the fixture printed a reassuring line saying otherwise.

    I proved that on myself: my first check imported a test module INSIDE the test
    function, so it saw the new value and passed. It was verifying the fixture's ordering
    with a probe that did not share it.
    """
    if os.environ.get("MESHY_NO_TEST_DB") == "1":
        return None
    name = f"meshy_test_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    _record_the_run_database(name)
    admin = _admin_dsn(TEMPLATE_DSN)
    dsn = TEMPLATE_DSN.rsplit("/", 1)[0] + "/" + name

    # CLONE A DEDICATED TEMPLATE IF ONE EXISTS. Applying 104 migrations costs ~15 s, and a
    # mutation campaign runs the suite once per mutation - dozens of times - so that cost
    # lands on every run. `meshy_template` is migrated once and nothing connects to it, so
    # CREATE DATABASE ... TEMPLATE (a file copy) takes about a second.
    # Falls back to migrating when the template is absent or busy: a clone that cannot
    # happen must not become a skipped suite.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from apps.core import schema_sets

    template = os.environ.get("MESHY_CLONE_TEMPLATE", "meshy_template")
    with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (template,))
        have_template = cur.fetchone() is not None

    # A STALE TEMPLATE IS WORSE THAN NO TEMPLATE, and this bit me: the template was built
    # before `sql_meshychat/002_background_job_leases.sql` existed, so every clone came up
    # missing `background_job_leases` and 40 tests failed with UndefinedTable. The clone
    # path skips migration entirely - that is the whole point of it - which means nothing
    # on that path ever asks whether the thing being copied is current.
    #
    # That is the same shape as the incident apps/core/schema_sets.py was written about: a
    # set nobody enumerates is a check that reports success. So ask the DECLARATION, not a
    # hardcoded directory list, and fall back to migrating when the answer is "stale".
    if have_template:
        try:
            with psycopg.connect(TEMPLATE_DSN.rsplit("/", 1)[0] + "/" + template) as tconn:
                stale = {n: v for n, v in schema_sets.pending(tconn).items() if v}
        except (psycopg.Error, schema_sets.SchemaIncomplete) as exc:
            stale = {"(unreadable)": [str(exc)[:80]]}
        if stale:
            print(f"conftest: NOT cloning {template} - it is missing {stale}. "
                  "Migrating a fresh database instead; re-create the template to get the "
                  "fast path back.", flush=True)
            have_template = False

    with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
        if have_template:
            try:
                cur.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
                return _publish(dsn)
            except psycopg.Error:
                pass                        # busy or unusable: migrate instead
        cur.execute(f'CREATE DATABASE "{name}"')

    with psycopg.connect(dsn) as conn:
        schema_sets.apply_all(conn, verbose=False)
        # The declaration is the authority on completeness, so ASK IT rather than trusting
        # that apply_all did what it said. This is the one place a wrong answer is cheap to
        # catch and expensive to miss: every test in the run inherits this database.
        schema_sets.assert_schema_complete(conn)

    # POINT EVERY RESOLVER AT THE CLONE, not just the fixtures.
    #
    # There are THREE dsn resolvers and they do not agree on where to look:
    #   apps/peers/db.dsn()       MESHY_DSN, then MESHY_TEST_DSN, then the default
    #   apps/authapi/config       MESHY_DSN, then MESHY_TEST_DSN only when testing
    #   apps/core/dbconn.dsn()    settings.MESHY_DSN, then MESHY_DSN, then
    #                             DATABASE_URL, then the default. NEVER MESHY_TEST_DSN.
    #
    # Measured before this line existed: with MESHY_DSN unset, which is the invocation
    # everyone uses and the one that produces a green suite, MESHY_TEST_DSN named the
    # per-run clone while dbconn.dsn() fell through to the default and returned the
    # SHARED database. Any route resolving through dbconn without an explicit
    # override_settings therefore read and wrote shared state during the run, on a box
    # where more than one checkout runs this suite at once.
    #
    # Setting MESHY_DSN here makes all three agree without changing any resolver, which
    # matters because apps/authapi/config.py records a review finding where reordering
    # this exact precedence left the auth surface running as an RLS-bypassing superuser.
    # The guard below still runs, and now passes because the two values match.
    return _publish(dsn)


def _correct_a_setting_frozen_before_us(test_dsn: str) -> None:
    """Fix settings.MESHY_DSN when Django was configured before this file ran.

    Setting the environment variable above is only enough while nothing has configured
    Django yet, and today nothing does: no ini declares DJANGO_SETTINGS_MODULE, so
    pytest-django forces no early setup and each test module calls django.setup() itself
    at collection. Measured: settings.configured is False when this function runs.

    That is ORDERING LUCK, not a guarantee, and one ordinary change removes it. With
    DJANGO_SETTINGS_MODULE exported, which is exactly what adding a pytest.ini would do,
    settings freeze BEFORE this file is imported. meshychat/settings.py:128 resolves
    MESHY_DSN as env, then DATABASE_URL, then the dev default, so with MESHY_DSN unset at
    freeze time the setting becomes the SHARED database and never changes afterwards.
    dbconn.dsn() prefers the setting over the environment, so the environment variable
    set above would be ignored and the isolation would be silently gone.

    Verified, not theorised: running the suite with DJANGO_SETTINGS_MODULE exported and
    MESHY_DSN unset turned tests/test_dsn_resolvers_agree.py red, 3 of 4, before this
    function existed.

    Assigning the attribute is deliberate and narrow. It corrects the ONE value dbconn
    reads. DATABASES was already built from the stale value at freeze time and is left
    alone, because this project serves requests through raw psycopg rather than the ORM,
    and rebuilding it here would change more than the seam this is about.
    """
    try:
        from django.conf import settings
        if settings.configured and getattr(settings, "MESHY_DSN", None) != test_dsn:
            settings.MESHY_DSN = test_dsn
    except Exception:
        # Django absent or unconfigurable is not a test-harness failure: the environment
        # variable above already covers every resolver that reads the environment.
        pass


def _refuse_a_hijacked_application_dsn(test_dsn: str) -> None:
    """Refuse when MESHY_DSN points somewhere other than the database we just built.

    THE INCIDENT, 2026-09-21, and it cost most of an afternoon plus a repair made on a
    wrong premise. A run was invoked with BOTH variables exported:

        MESHY_TEST_DSN=...meshy_django MESHY_DSN=...meshy_django pytest tests/

    The fixtures seed into the per-run clone this function creates, but the application
    under test resolves its connection through apps/core/dbconn.dsn(), which consults
    MESHY_DSN and has never heard of MESHY_TEST_DSN. So every request ran against a
    DIFFERENT database from the one the test had just seeded, every authenticated route
    looked up a peer that database had never seen, and the suite produced 125 failures
    that all read as `403 Forbidden`.

    Nothing about that looks like a misconfiguration. It looks like an authorization bug,
    which is exactly where four hours went: a checksum drift, a missing pytest plugin, a
    stale clone template, import-order contamination and a missing package were all
    investigated and eliminated first. The single-variable control that settled it was
    running the same module with and without MESHY_DSN: 59 passed against 44 failed.

    So this refuses rather than warns. docs/V1-DESIGN-RULES.md E1 names the class: "a
    failure and a pass looked identical". A warning scrolls past in a 700-test run, and
    the failure it precedes is indistinguishable from a real defect.

    MESHY_NO_TEST_DB=1 remains the way to aim at the shared database ON PURPOSE. This
    fires only when an isolated database was built AND something else is redirecting the
    application away from it, which is never intentional.
    """
    NL = chr(10)   # built rather than escaped: this message crossed a shell
                   # heredoc during authoring and the escapes collapsed.
    hijack = os.environ.get("MESHY_DSN")
    if not hijack or hijack == test_dsn:
        return
    raise RuntimeError(
        f"REFUSING TO RUN: MESHY_DSN is set and does not point at this run's"
        f" isolated test database.{NL}"
        f"  MESHY_DSN      {hijack}{NL}"
        f"  test database  {test_dsn}{NL}"
        f"The fixtures would seed the test database while the application under"
        f" test connected to the other one, because apps/core/dbconn.dsn() reads"
        f" MESHY_DSN and never reads MESHY_TEST_DSN. Every authenticated route"
        f" would then 403 on a peer it cannot see, and the suite would look like"
        f" an authorization failure rather than a misconfiguration.{NL}"
        f"Unset MESHY_DSN, or set MESHY_NO_TEST_DB=1 to target the shared"
        f" database deliberately."
    )


def _harness_runs_dpop_off_unless_asked() -> None:
    """Run the suite with DPoP enforcement OFF unless the runner chose a value.

    WHY. Production defaults it ON (divergence 015), and under
    enforcement every peer WRITE needs a cnf-bound token and a DPoP proof. The route tests
    across this suite mint plain bearer tokens for their writes because they test the
    route, not the proof. The harness sets `MESHYCHAT_DPOP_ENFORCE=false`, and DPoP tests opt
    in.

    THIS DOES NOT LEAVE ENFORCEMENT UNTESTED. tests/test_jwt_seam.py turns it on
    explicitly in-process, and its subprocess test boots the real settings with
    MESHYCHAT_DPOP_ENFORCE REMOVED from the environment, which is how it proves the production
    default is ON rather than assuming it.

    Only when unset, so `MESHYCHAT_DPOP_ENFORCE=true pytest tests/` runs the whole suite
    enforced. The settings correction covers the case `_correct_a_setting_frozen_before_us`
    documents: settings already configured before this file ran.
    """
    if "MESHYCHAT_DPOP_ENFORCE" in os.environ:
        return
    os.environ["MESHYCHAT_DPOP_ENFORCE"] = "false"
    try:
        from django.conf import settings
        if settings.configured:
            settings.MESHY_DPOP_ENFORCE = False
    except Exception:
        # Django absent or unconfigurable: the environment variable already covers the
        # settings module that has not been read yet, as in the DSN correction above.
        pass


_harness_runs_dpop_off_unless_asked()


def _harness_mounts_every_surface_unless_asked() -> None:
    """Run the suite with every OPTIONAL surface mounted unless the runner chose a value.

    WHY. Production mounts none of them unless MESHY_SURFACES names it (divergence 054). Their
    route tests go through the project urlconf, so the harness turns them all on, as it turns
    DPoP enforcement off: the tests are about the routes, not the switch.

    THIS DOES NOT LEAVE THE DEFAULT UNTESTED. tests/test_surfaces.py builds the patterns for
    the empty set and for each surface alone, and its subprocess test loads the real settings
    with MESHY_SURFACES REMOVED from the environment and asks the real urlconf.
    """
    if "MESHY_SURFACES" in os.environ:
        return
    os.environ["MESHY_SURFACES"] = "all"
    try:
        from django.conf import settings
        if settings.configured:
            from apps.core import surfaces
            settings.MESHY_SURFACES = frozenset(surfaces.OPTIONAL)
    except Exception:
        pass


_harness_mounts_every_surface_unless_asked()


def _refuse_a_live_run_on_a_server_nobody_named() -> None:
    """A live run (MESHY_LIVE_MODELS=1) writes rows, so it makes its clone only on the server
    the operator NAMED, and is refused here, before the first connection, otherwise
    (tests/live_env.py; vxxxxxxxxxx 2026-09-26: the default 127.0.0.1:5434 is another
    project's live Postgres on HQB, and CREATE DATABASE there was this file's write).

    It also records what the operator set in MESHY_TEST_DSN before `_publish` replaces it
    with the clone: after that nothing can tell the two apart, and a live test must know
    whether its database was named or merely defaulted. A value already recorded (a child
    run started by a test of this suite) is kept."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests import live_env
    os.environ.setdefault(live_env.EXPLICIT_VAR, os.environ.get("MESHY_TEST_DSN", ""))
    why = live_env.refusal_for_provisioning(TEMPLATE_DSN)
    if why:
        raise pytest.UsageError(why)


_refuse_a_live_run_on_a_server_nobody_named()

RUN_DSN = _provision()


@pytest.fixture(autouse=True)
def call_bridge_stopped_and_presence_cleared():
    """Design section 6: "Autouse fixtures clear MANAGER, the new CallRegistry and
    hub_connections after each test" (divergence 041 S3).

    The production CallBridge (apps/wsgw/call_notify.CALL_BRIDGE) starts with the first socket
    any test opens on `meshychat.asgi.application`, is bound to that test's event loop, and
    writes a `hub_connections` row per socket and a beat. So after a test that touched it
    (its counters moved), it is stopped, and joined BEFORE the table is cleared, or its last
    beat could write after the clear. A test that never touched it pays nothing: no
    database round trip. `tests/fixtures/hub_presence_db.py` refuses to start a presence test
    on rows an earlier test left, which is what makes this clearing observable.
    """
    module = sys.modules.get("apps.wsgw.call_notify")
    before = module.CALL_BRIDGE.stats() if module is not None else None
    yield
    module = sys.modules.get("apps.wsgw.call_notify")
    if module is None:
        return
    bridge = module.CALL_BRIDGE
    bridge.stop()
    if bridge.stats() == (before or {}):
        return
    dsn = os.environ.get("MESHY_TEST_DSN", TEMPLATE_DSN)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM hub_connections")


@pytest.fixture(autouse=True)
def peer_rate_buckets_full():
    """B12: the per-peer buckets (apps/core/peer_limit) live in the process, so each test starts
    with them full, as a fresh server process does; otherwise a peer a module shares across its
    tests would spend a later test's limit, and that test would fail for its ORDER. A test that
    never imported the module pays nothing. A test that needs an empty bucket empties it."""
    module = sys.modules.get("apps.core.peer_limit")
    if module is not None:
        module.reset()
    # B16: the per-client-IP redeem buckets (apps/core/redeem_limit) likewise. Every test
    # client is 127.0.0.1, so without this a module's redeems would spend a later test's
    # budget.
    module = sys.modules.get("apps.core.redeem_limit")
    if module is not None:
        module.reset()
    yield


@pytest.fixture(autouse=True)
def coverage_pairs_forgotten():
    """The coverage pairs live in the process: apps/mesh/graph holds its pair and
    apps/meshsem/related_reads its figures, each COVERAGE_TTL_S per key (divergence 107), and
    one database serves every test of a run. So each test starts without a pair an earlier test
    left, as a fresh server process does. A test that never imported the modules pays nothing."""
    module = sys.modules.get("apps.mesh.graph")
    if module is not None:
        module.forget_coverage()
    module = sys.modules.get("apps.meshsem.related_reads")
    if module is not None:
        module.forget()
    yield


@pytest.fixture(scope="module", autouse=True)
def auth_config_restored_after_each_module():
    """Put `apps.authapi.config`'s cached config back after every test module.

    `get_config()` caches the auth config, DSN included, in a module global on first
    use. Several module fixtures (test_meshy_node_read, test_mesh_authorship,
    test_meshy_node_monitor) point MESHY_DSN at a database they create and DROP, and run
    the server in this process, so the first auth request inside them caches that DSN.
    Every later module then reached `/v1/auth/me` on a database that no longer existed:
    `pytest tests/test_meshy_node_read.py tests/test_owner_session_is_one_session.py`
    failed 11 while the second file alone passed (kxxx, STATUS F20), and the other two
    fixtures did the same to it (11 and 12 failures). One guard here rather than one per
    fixture, so the next fixture of that shape is covered without anyone remembering.

    Module scope and autouse, so it is set up before the module's own fixtures and torn
    down after theirs. The import is inside, because this file runs before Django is set up.
    """
    from apps.authapi import config as authconfig
    previous = authconfig._config
    yield
    authconfig._config = previous


def pytest_report_header(config):
    if RUN_DSN:
        return f"meshychat: isolated test database {RUN_DSN.rsplit('/', 1)[-1]}"
    return "meshychat: MESHY_NO_TEST_DB=1, using the shared database"


def pytest_sessionfinish(session, exitstatus):
    if not RUN_DSN:
        return
    admin = _admin_dsn(TEMPLATE_DSN)
    name = RUN_DSN.rsplit("/", 1)[-1]
    try:
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except psycopg.Error:
        pass          # a leftover test database is untidy, not a test failure


# --- Public cut (build_cut.py): deselect the tests listed in public_cut_deselect.txt. That file gives the reason
# for each group: internal-only files this repository leaves out, a commit of the original history, known failures.
def pytest_collection_modifyitems(config, items):
    import pathlib
    listed = pathlib.Path(__file__).with_name("public_cut_deselect.txt")
    if not listed.exists():
        return
    ids = {ln.strip() for ln in listed.read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.startswith("#")}
    keep, drop = [], []
    for item in items:
        (drop if item.nodeid.split("[")[0] in ids else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
