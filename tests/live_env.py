"""What a live suite must be TOLD before it runs: the database it writes to and every model
URL it calls. Nothing here has a default, on purpose.

WHY NO DEFAULTS (vxxxxxxxxxx, 2026-09-26). The live suites defaulted `MESHY_TEST_DSN` to
`127.0.0.1:5434`, which is this project's dev database on HQD and a DIFFERENT project's
live Postgres on HQB, and they write rows. They defaulted the embedders to HQB plus a HQD
embedder that no longer exists. A default names one host's layout and is wrong on every
other host, silently: the suite either writes somewhere nobody chose or fails for a reason
that is not about the code. So a live run names everything, and a run that has not is
REFUSED, loudly, before it touches anything.

Two callers:

  * tests/conftest.py, at import, BEFORE it makes the run's clone
    (`refusal_for_provisioning`): with MESHY_LIVE_MODELS=1 the clone must be made on the
    server the operator named, so an unnamed server is never written to, not even by
    CREATE DATABASE. It also records what the operator set (`EXPLICIT_VAR`) before it
    overwrites MESHY_TEST_DSN with the clone, because after that the two are
    indistinguishable.
  * each live test, FIRST, before any connection (`dsn`, `urls`, `url`).

A refusal inside a test is a FAILURE, not a skip: MESHY_LIVE_MODELS=1 says the operator
asked for the live run, and a skipped live run reads as a quiet pass. The
one skip is a claim that needs more than was named (two embedders for the one-space
check); its reason says what was not run and what to name.
"""
from __future__ import annotations

import os

import pytest

REFUSED = "REFUSED (live suite)"

#: The value of MESHY_TEST_DSN the OPERATOR set ("" for none), recorded by tests/conftest.py
#: before it publishes the run's clone under MESHY_TEST_DSN.
EXPLICIT_VAR = "MESHY_TEST_DSN_EXPLICIT"


def live() -> bool:
    return os.environ.get("MESHY_LIVE_MODELS") == "1"


def operator_dsn(environ=None) -> str | None:
    """The database the operator named, or None. Read from conftest's record when there is
    one, since conftest has replaced MESHY_TEST_DSN by then."""
    environ = os.environ if environ is None else environ
    if EXPLICIT_VAR in environ:
        return (environ[EXPLICIT_VAR] or "").strip() or None
    return (environ.get("MESHY_TEST_DSN") or "").strip() or None


def server_of(dsn: str) -> tuple[str, str]:
    """(host, port) of a DSN, as libpq would read them; no credentials. Hostnames are
    compared as written: `localhost` and `127.0.0.1` are different names and are refused as
    different, which can refuse a run that was safe but never admits one that was not."""
    from psycopg.conninfo import conninfo_to_dict
    parts = conninfo_to_dict(dsn)
    return (str(parts.get("host") or "localhost"), str(parts.get("port") or "5432"))


def _named(dsn: str) -> str:
    host, port = server_of(dsn)
    return f"{host}:{port}"


def refusal_for_provisioning(template_dsn: str, environ=None) -> str | None:
    """Why conftest must not make the run's clone, or None. Only a live run is judged."""
    environ = os.environ if environ is None else environ
    if environ.get("MESHY_LIVE_MODELS") != "1":
        return None
    named = operator_dsn(environ)
    if not named:
        return (f"{REFUSED}: MESHY_LIVE_MODELS=1 and MESHY_TEST_DSN is not set. A live suite "
                "writes rows; name the database it may write to (and MESHY_TEMPLATE_DSN for "
                "the server the run's clone is made on, when that is not the same server). "
                "There is no default: 127.0.0.1:5434 is this project's dev database on one "
                "host and another project's live Postgres on another.")
    if environ.get("MESHY_NO_TEST_DB") == "1":
        return None
    if server_of(named) != server_of(template_dsn):
        return (f"{REFUSED}: MESHY_TEST_DSN names {_named(named)}, but the run's clone would be "
                f"made on {_named(template_dsn)} (MESHY_TEMPLATE_DSN). Set "
                "MESHY_TEMPLATE_DSN to the server you named, or MESHY_NO_TEST_DB=1 to use "
                "the database you named directly.")
    return None


def dsn() -> str:
    """The database this live test may write to: the run's clone when conftest made one on
    the server the operator named, else the operator's own. Fails REFUSED otherwise."""
    named = operator_dsn()
    if not named:
        pytest.fail(f"{REFUSED}: MESHY_TEST_DSN is not set; name the database this live test "
                    "may write to. There is no default.", pytrace=False)
    effective = (os.environ.get("MESHY_TEST_DSN") or "").strip() or named
    if server_of(effective) != server_of(named):
        pytest.fail(f"{REFUSED}: this run's database is on {_named(effective)}, but "
                    f"MESHY_TEST_DSN named {_named(named)}.", pytrace=False)
    return effective


def urls(var: str, what: str) -> list[str]:
    """The comma-separated base URLs in `var`, in the operator's order. Fails REFUSED when
    `var` names none: there is no default model host."""
    got = [u.strip() for u in (os.environ.get(var) or "").split(",") if u.strip()]
    if not got:
        pytest.fail(f"{REFUSED}: {var} is not set; name the {what} base URL(s) this live test "
                    "calls (comma-separated). There is no default: a default names one "
                    "host's models and is wrong on every other host.", pytrace=False)
    return got


def url(var: str, what: str) -> str:
    """Exactly ONE base URL in `var`. More than one is refused rather than narrowed to the
    first, because the test pins one model and would otherwise test a model nobody chose."""
    got = urls(var, what)
    if len(got) != 1:
        pytest.fail(f"{REFUSED}: {var} names {len(got)} URLs; this test pins ONE {what}. "
                    f"Name exactly one for this run.", pytrace=False)
    return got[0]


__all__ = ["EXPLICIT_VAR", "REFUSED", "dsn", "live", "operator_dsn",
           "refusal_for_provisioning", "server_of", "url", "urls"]
