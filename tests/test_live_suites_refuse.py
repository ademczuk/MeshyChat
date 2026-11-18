"""Every live suite refuses to run until the operator has NAMED what it touches.

WHY (node-reported hazard, vxxxxxxxxxx 2026-09-26, and their F19 the same day). The four live
suites defaulted what they talk to:

  * `tests/test_meshaudit_live.py` and `tests/test_meshsem_live.py` defaulted
    `MESHY_TEST_DSN` to `127.0.0.1:5434`. On HQB that port is a live Postgres database outside
    this test environment, and the suites write rows. tests/conftest.py makes the per-run clone on
    `MESHY_TEMPLATE_DSN`, which defaulted to the same port, so under pytest the first write
    to that server (CREATE DATABASE) happened at conftest import, before any test body.
    Since operator decision 41 (2026-09-26) that variable has no default for ANY run
    (tests/test_template_dsn_required.py), so every child here names it.
  * `tests/test_meshsem_live.py` defaulted `MESHY_LIVE_EMBED` to HQB plus
    `http://127.0.0.1:8103`, a HQD embedder that no longer exists (HQD runs no model
    workloads), so the one-space test failed as shipped; and with one embedder it would
    have compared that embedder with ITSELF and passed, a two-embedder claim silently
    narrowed to one.

THE RULE these tests hold. With `MESHY_LIVE_MODELS=1`:

  1. no `MESHY_TEST_DSN` named by the operator: REFUSED before any database connection,
     conftest's included (scenarios A and A2, the second on the real default);
  2. a clone that would be made on a server other than the one the operator named:
     REFUSED before any connection (scenario B);
  3. a model URL the operator did not name: the test that needs it fails REFUSED, naming
     the variable, before it opens a database connection (scenario D);
  4. one embedder named: the one-space check is SKIPPED, loudly, and the end-to-end test
     still runs (scenario C); two named: the one-space check runs (scenario E).

Positive controls sit beside each refusal: in C every live test RUNS and fails on the
listener it was pointed at (which hangs up at once: how a run that got past the refusal
looks without real models), so a refusal that fired unconditionally would fail C. Every
scenario runs the live files in a child pytest with a controlled environment and reads its
JUnit report; listeners count connections, so "refused before connecting" is observed, not
inferred from a message.

No scenario reaches a model or a real database: every URL and every database a child may
connect to is a listener. Scenario A2 (the real default) connects to nothing on a fixed
tree; on the tree before the fix it made and dropped a clone on the dev server, which is
the defect.
"""
from __future__ import annotations

import ast
import os
import re
import socket
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIVE = ["tests/test_meshsem_live.py", "tests/test_meshsem_related_live.py",
        "tests/test_meshaudit_live.py", "tests/test_meshsum_live.py"]
URL_VARS = ("MESHY_LIVE_EMBED", "MESHY_LIVE_RERANK", "MESHY_LIVE_CHAT", "MESHY_LIVE_CHAT_URL")
#: Everything that could name a database or a model for the child: stripped, then set per
#: scenario, so the parent's per-run clone never leaks in as if an operator had named it.
STRIPPED = ("MESHY_TEST_DSN", "MESHY_TEST_DSN_EXPLICIT", "MESHY_DSN", "DATABASE_URL",
            "MESHY_TEMPLATE_DSN", "MESHY_NO_TEST_DB", "MESHY_LIVE_MODELS",
            "MESHY_CLONE_TEMPLATE") + URL_VARS


class Listener:
    """A TCP port that counts the connections made to it and hangs up on each at once."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.accepted += 1
            conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self.sock.close()


@pytest.fixture
def listener():
    """The DATABASE server a refused run must never reach."""
    lst = Listener()
    yield lst
    lst.close()


@pytest.fixture
def sink():
    """Where every model URL (and, in the positive controls, the database) points. It hangs
    up at once, so a test that got past the refusal fails in milliseconds; a closed port
    costs about two seconds per attempt on Windows, and the providers retry."""
    lst = Listener()
    yield lst
    lst.close()


def _dsn(port: int, db: str = "meshy_live_probe") -> str:
    return f"postgresql://meshychat_owner:change-me@127.0.0.1:{port}/{db}"


def _all_urls(port: int, embeds: int = 1) -> dict:
    url = f"http://127.0.0.1:{port}"
    return {"MESHY_LIVE_EMBED": ",".join([url] * embeds), "MESHY_LIVE_RERANK": url,
            "MESHY_LIVE_CHAT": url, "MESHY_LIVE_CHAT_URL": url}


def _run(tmp_path, files, **env_set) -> tuple[subprocess.CompletedProcess, dict]:
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED}
    env["MESHY_LIVE_MODELS"] = "1"
    env.update(env_set)
    junit = tmp_path / f"live-{len(list(tmp_path.iterdir()))}.xml"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *files, "-q", "-rs", "-p", "no:cacheprovider",
         f"--junitxml={junit}"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    return proc, _cases(junit)


def _cases(junit: Path) -> dict:
    """{test name: (outcome, message)} from the child's JUnit report ({} when none). The
    message is the report's `message` attribute ONLY (a refusal's or a skip's reason, or the
    exception's one line), never the traceback text: that quotes source lines, and the
    words these tests look for are in the source it would quote."""
    if not junit.exists():
        return {}
    out = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        outcome, message = "passed", ""
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                outcome, message = child.tag, child.get("message") or ""
        out[case.get("name")] = (outcome, message)
    return out


def _say(proc) -> str:
    return (f"exit {proc.returncode}\n--- stdout\n{proc.stdout[-3000:]}"
            f"\n--- stderr\n{proc.stderr[-3000:]}")


# ------------------------------------------------------------------- refusals

def test_A_no_database_named_is_refused_before_any_connection(tmp_path, listener, sink):
    """The operator named no MESHY_TEST_DSN. The clone's server (the listener) must not be
    reached, not even by conftest, and the run must not look like a pass."""
    proc, _ = _run(tmp_path, LIVE, MESHY_TEMPLATE_DSN=_dsn(listener.port, "meshy_django"),
                   **_all_urls(sink.port))
    assert listener.accepted == 0, f"connected {listener.accepted}x before refusing\n{_say(proc)}"
    assert proc.returncode != 0, _say(proc)
    said = proc.stdout + proc.stderr
    assert "REFUSED" in said and "MESHY_TEST_DSN" in said, _say(proc)


def test_A2_the_real_default_is_refused_too(tmp_path, sink):
    """The same with no database named at all: conftest's old default was the HQB hazard.
    Since decision 41 there is no default to fall back on, and the run is refused for the
    missing MESHY_TEMPLATE_DSN before the live refusal is reached; either way nothing runs."""
    proc, cases = _run(tmp_path, LIVE, **_all_urls(sink.port))
    assert proc.returncode != 0, _say(proc)
    said = proc.stdout + proc.stderr
    assert "REFUSED" in said and "MESHY_TEMPLATE_DSN" in said, _say(proc)
    assert not cases, f"tests ran: {sorted(cases)}"


def test_B_a_clone_on_a_server_other_than_the_one_named_is_refused(tmp_path, listener, sink):
    proc, _ = _run(tmp_path, LIVE, MESHY_TEST_DSN=_dsn(sink.port),
                   MESHY_TEMPLATE_DSN=_dsn(listener.port, "meshy_django"),
                   **_all_urls(sink.port))
    assert listener.accepted == 0, f"connected {listener.accepted}x before refusing\n{_say(proc)}"
    assert proc.returncode != 0, _say(proc)
    assert "REFUSED" in proc.stdout + proc.stderr, _say(proc)


def test_D_an_unnamed_model_url_fails_the_test_that_needs_it_before_the_database(tmp_path,
                                                                                listener):
    proc, cases = _run(tmp_path, LIVE, MESHY_NO_TEST_DB="1",
                       MESHY_TEMPLATE_DSN=_dsn(listener.port, "postgres"),
                       MESHY_TEST_DSN=_dsn(listener.port))
    assert cases, _say(proc)
    for name, (outcome, message) in cases.items():
        assert outcome == "failure", (name, outcome, message[:400])
        assert "REFUSED" in message and "MESHY_LIVE_" in message, (name, message[:400])
    assert listener.accepted == 0, f"connected {listener.accepted}x before refusing"


# ------------------------------------------------------- positive controls

def test_C_named_everything_so_every_live_test_runs_and_one_embedder_skips_loudly(tmp_path,
                                                                                   sink):
    """POSITIVE CONTROL for every refusal above: all named, so nothing is refused. Each
    test runs and fails on the listener it was pointed at; only the one-space check is
    skipped, because one embedder cannot show that two are one space."""
    proc, cases = _run(tmp_path, LIVE, MESHY_NO_TEST_DB="1",
                       MESHY_TEMPLATE_DSN=_dsn(sink.port, "postgres"),
                       MESHY_TEST_DSN=_dsn(sink.port), **_all_urls(sink.port))
    assert len(cases) >= 7, f"expected every live test, got {sorted(cases)}\n{_say(proc)}"
    skipped = {n: m for n, (o, m) in cases.items() if o == "skipped"}
    assert list(skipped) == ["test_the_named_embedders_are_one_space"], (skipped, _say(proc))
    reason = skipped["test_the_named_embedders_are_one_space"]
    assert "ONE-SPACE CHECK NOT RUN" in reason and "MESHY_LIVE_EMBED" in reason, reason
    for name, (outcome, message) in cases.items():
        if name in skipped:
            continue
        assert "REFUSED" not in message, (name, message[:400])
        assert outcome in ("failure", "error"), (name, outcome)
    assert sink.accepted > 0, "no live test reached a model or the database"


def test_E_two_embedders_named_so_the_one_space_check_runs(tmp_path, sink):
    proc, cases = _run(tmp_path, ["tests/test_meshsem_live.py"], MESHY_NO_TEST_DB="1",
                       MESHY_TEMPLATE_DSN=_dsn(sink.port, "postgres"),
                       MESHY_TEST_DSN=_dsn(sink.port), **_all_urls(sink.port, embeds=2))
    outcome, message = cases.get("test_the_named_embedders_are_one_space", (None, _say(proc)))
    assert outcome == "failure", (outcome, message[:600])
    assert "REFUSED" not in message and "ONE-SPACE CHECK NOT RUN" not in message, message[:600]
    assert sink.accepted > 0, "the one-space check reached no embedder"


# ------------------------------------------- tests/live_env.py's guards on their own
#
# The scenarios above go through conftest, whose refusal fires first, so the guards INSIDE
# the live tests were never exercised alone (review of e983a9a, LOW 4: mutants MH, MI and ML
# all passed). Under `pytest --noconftest`, or a live test imported by another harness,
# live_env.dsn() is the only guard. These call it directly, each refusal beside the same
# call with the missing name supplied.

sys.path.insert(0, str(ROOT))
from tests import live_env  # noqa: E402

REFUSAL = pytest.fail.Exception


@pytest.fixture
def bare_env(monkeypatch):
    """No database named and no record from conftest: what a run under --noconftest sees."""
    for var in (live_env.EXPLICIT_VAR, "MESHY_TEST_DSN"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_dsn_refuses_when_no_database_is_named(bare_env):
    """Mutant ML (a 127.0.0.1:5434 default back inside dsn()) returns that default here."""
    with pytest.raises(REFUSAL, match=r"REFUSED .*MESHY_TEST_DSN is not set"):
        live_env.dsn()
    named = "postgresql://meshychat_owner:pw@db.example:6000/meshy_live"
    bare_env.setenv("MESHY_TEST_DSN", named)                      # control: named, returned
    assert live_env.dsn() == named


def test_dsn_refuses_a_published_clone_when_the_operator_named_nothing(bare_env):
    """conftest recorded "" (nothing named) and then published its clone as MESHY_TEST_DSN:
    the clone must not pass for a database the operator chose."""
    bare_env.setenv(live_env.EXPLICIT_VAR, "")
    bare_env.setenv("MESHY_TEST_DSN", "postgresql://meshychat_owner:pw@127.0.0.1:5434/meshy_test_abc")
    with pytest.raises(REFUSAL, match=r"REFUSED .*MESHY_TEST_DSN is not set"):
        live_env.dsn()
    bare_env.setenv(live_env.EXPLICIT_VAR,                        # control: it was named
                    "postgresql://meshychat_owner:pw@127.0.0.1:5434/meshy_django")
    assert live_env.dsn() == "postgresql://meshychat_owner:pw@127.0.0.1:5434/meshy_test_abc"


def test_dsn_refuses_a_database_on_a_server_other_than_the_one_named(bare_env):
    """Mutant MI (no server comparison in dsn()) returns the other server's database here."""
    bare_env.setenv(live_env.EXPLICIT_VAR, "postgresql://meshychat_owner:pw@db.example:6000/meshy_live")
    bare_env.setenv("MESHY_TEST_DSN", "postgresql://meshychat_owner:pw@127.0.0.1:5434/meshy_test_abc")
    with pytest.raises(REFUSAL, match=r"REFUSED .*127\.0\.0\.1:5434.*db\.example:6000"):
        live_env.dsn()
    clone = "postgresql://meshychat_owner:pw@db.example:6000/meshy_test_abc"  # control: same server
    bare_env.setenv("MESHY_TEST_DSN", clone)
    assert live_env.dsn() == clone


def test_conftest_recorded_what_the_operator_named_before_publishing_its_clone():
    """Mutant MH (conftest's `setdefault(EXPLICIT_VAR, ...)` dropped) leaves no record, and
    the clone conftest publishes as MESHY_TEST_DSN then reads as the operator's choice, in
    this run and in any child run a test starts with this environment."""
    assert live_env.EXPLICIT_VAR in os.environ, \
        "conftest did not record the operator's MESHY_TEST_DSN before replacing it"
    if os.environ[live_env.EXPLICIT_VAR] == "":
        assert live_env.operator_dsn() is None, live_env.operator_dsn()
    else:                                  # the operator named one: it is what is recorded
        assert live_env.operator_dsn() == os.environ[live_env.EXPLICIT_VAR].strip()


@pytest.mark.parametrize("value,count", [("", 0), (" , ", 0), ("http://a:1,http://b:2", 2)])
def test_url_takes_exactly_one_named_url(monkeypatch, value, count):
    monkeypatch.setenv("MESHY_LIVE_PROBE", value)
    with pytest.raises(REFUSAL, match=r"REFUSED .*MESHY_LIVE_PROBE"):
        live_env.url("MESHY_LIVE_PROBE", "probe")
    if count:
        assert live_env.urls("MESHY_LIVE_PROBE", "probe") == ["http://a:1", "http://b:2"]
    monkeypatch.setenv("MESHY_LIVE_PROBE", " http://a:1 ")       # control: one, returned
    assert live_env.url("MESHY_LIVE_PROBE", "probe") == "http://a:1"


# ----------------------------------------------------------- structural

_HOSTISH = re.compile(r"(?i)\b(?:https?|postgres(?:ql)?)://|\b\d{1,3}(?:\.\d{1,3}){3}\b|localhost")


def _code_literals(source: str) -> list[tuple[int, str]]:
    """String constants in CODE: every docstring (module, class, function) is left out, so
    prose may say where the models usually live, and code may not default to it."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings and _HOSTISH.search(n.value)]


def test_the_scanner_catches_a_planted_default_and_passes_a_docstring():
    """The scanner's own controls: a default in code is found; the same text as prose is not."""
    planted = ('"""Usually http://192.168.50.10:7875."""\n'
               'import os\nURL = os.environ.get("X", "http://192.168.50.10:7875")\n')
    assert [ln for ln, _ in _code_literals(planted)] == [3]
    assert _code_literals('"""postgresql://meshychat_owner@127.0.0.1:5434/x"""\n') == []


@pytest.mark.parametrize("path", LIVE)
def test_no_live_suite_carries_a_host_in_code(path):
    found = _code_literals((ROOT / path).read_text(encoding="utf-8"))
    assert found == [], f"{path}: host-specific literal(s) in code: {found}"
