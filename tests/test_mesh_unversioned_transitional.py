"""MESHY_MESH_ACCEPT_UNVERSIONED: a TRANSITIONAL switch that accepts a
shared/mesh-chat message with no `schema_version` (docs/DIVERGENCES.md 011, dated paragraph
2026-09-27). OFF by default.

THE BLOCKER IT CLEARS (found by dxxxxx on 2026-09-27). The operator decided the fleet's nodes
keep their identities and their OLD client, which is repointed at this server. The old client's
sender sets no `schema_version`, and divergence 011 refuses every such write, so after the
repoint every node's default send would be refused. The server the fleet runs today stores
those rows unchecked (an early return, "accepted until TASK-616 completes the fleet migration"),
which is the behavior this switch restores.

What is under test, each refusal beside the control one variable away:

  * THE PARSE: strict, in MESHY_CAPABILITIES' vocabulary; unset is OFF; a malformed value stops
    the settings module from loading.
  * THE WRITE, through the real memory.write handler: with the flag on, the value the old client
    builds is accepted and stored verbatim. Controls: with the flag off it is refused; with the
    flag on a value that DECLARES schema_version and is invalid is still refused, and so is a
    non-object; a valid v1 value is still accepted, and is not counted.
  * NOTHING ELSE IS SKIPPED: the key's epoch-ms rule and divergence 028's R1 to R4 refuse an
    unversioned value exactly as they refuse a v1 one.
  * COUNTED: `meshychat_mesh_unversioned_accepted_total` on GET /v1/metrics moves by one per row
    written, never for a refusal or a replay; the INFO line names the caller's label, never the
    body, at most once per label per interval.
  * READABLE, (a) by the old client's memory.read and memory.query, and (b) by the new client's
    surfaces: the projection's catch-up, GET /v1/mesh/inbox, and the addressed wait.

All message text is synthetic.

Run:  pytest tests/test_mesh_unversioned_transitional.py -q -p no:cacheprovider
"""
from __future__ import annotations

import ast
import datetime as _dt
import itertools
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.core import rls  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import db, handlers, validation  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from apps.mesh import projection  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
MESH = "shared/mesh-chat"
FLAG = "MESHY_MESH_ACCEPT_UNVERSIONED"
LOGGER = "meshychat.mesh_unversioned"
METRICS_TOKEN = "scrape-token-for-the-unversioned-tests"

ISSUER = "meshy-unversioned-test-issuer"
_jwt_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _jwt_key.private_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption())
JWT_PUBLIC_PEM = _jwt_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

@pytest.fixture()
def owner_conn():
    """Owner role, AUTOCOMMIT: seeds and observes. Bypasses RLS; never a request's role."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """The role a request runs as: meshychat_app under FORCE RLS (db.connect asserts it)."""
    conn = db.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def flag_on():
    with override_settings(MESHY_MESH_ACCEPT_UNVERSIONED=True):
        yield


@pytest.fixture()
def flag_off():
    with override_settings(MESHY_MESH_ACCEPT_UNVERSIONED=False):
        yield


@pytest.fixture()
def http():
    """The real URLconf, a real EdDSA bearer's verifier, and the metrics token."""
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM, MESHY_JWT_ISSUER=ISSUER,
                           MESHY_DSN=DSN, MESHY_METRICS_TOKEN=METRICS_TOKEN):
        yield Client()


def _peer(conn, tag: str, *, name: str | None = None) -> dict:
    """A dev-plane owner of its own, a peer whose name is a mesh label, an active key.
    `name`: another peer's name (peers.name has no UNIQUE constraint), for R4."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    name = name or f"uv-{tag}-{peer_id.hex[:8]}"
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'dev',%s)",
                 (owner_id, f"uv-{tag}-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
    conn.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                 (peer_id, owner_id, name, f"uv {tag}"))
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(pub)))
    return {"owner_id": owner_id, "peer_id": peer_id, "seed": seed, "kid": kid,
            "name": name, "fallback": f"peer-{str(peer_id)[:8]}"}


def _admit(conn, peer: dict, permission: str = "write") -> None:
    """The claim on shared/mesh-chat when nobody holds it yet, else a grant from the holder
    (tests/test_mesh_authorship.py's `_admit`: the run's database is shared)."""
    row = conn.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace "
                       "WHERE namespace = %s", (MESH,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                     "owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                     "VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                     (MESH, peer["peer_id"], peer["owner_id"]))
    elif str(row[0]) != str(peer["peer_id"]):
        conn.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                     "grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                     "VALUES (%s,%s,%s,%s,%s,%s)",
                     (MESH, peer["peer_id"], peer["owner_id"], permission, row[0], row[1]))


def _sender_and_recipient(conn, tag: str) -> tuple[dict, dict]:
    sender, recipient = _peer(conn, f"{tag}s"), _peer(conn, f"{tag}r")
    _admit(conn, sender, "write")
    _admit(conn, recipient, "read")
    return sender, recipient


_KEY_STEP = itertools.count()


def _legacy_key(label: str) -> str:
    """A legacy key with no suffix: `msg/<epoch-ms>-<sender>`. The step keeps
    two keys minted in one millisecond by this process distinct."""
    return f"msg/{int(time.time() * 1000) + next(_KEY_STEP)}-{label}"


def _legacy_value(sender: str, to: str, body: str = "synthetic legacy send") -> dict:
    """Mirrors the old client's message value field for field: `to` is ONE STRING (its --to
    argument), `ts` carries microseconds, and there is NO schema_version.
    Its `supersedes`, which v1 would refuse as an unknown field, rides along."""
    return {"to": to, "from": sender, "type": "status", "thread_id": "legacy/probe",
            "ts": "2026-09-27T09:00:00.123456Z", "body": body, "author_family": "claude",
            "supersedes": []}


def _v1_value(sender: str, to: str, body: str = "synthetic v1 send") -> dict:
    return {"schema_version": 1, "to": [to], "from": sender, "type": "status",
            "thread_id": "legacy/probe", "ts": "2026-09-27T09:00:00Z", "body": body,
            "author_family": "claude"}


def _envelope(peer: dict, skill: str, args: dict) -> dict:
    payload = {"v": 1, "kind": "skill.call", "call_id": f"call-{uuid.uuid4()}",
               "caller_peer_id": str(peer["peer_id"]), "target_peer_id": str(peer["peer_id"]),
               "skill_name": skill, "skill_version": "1", "args": args,
               "args_attachments": [],
               "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    return {"payload": payload, "signature": P.b64u(P.sign(peer["seed"], P.jcs(payload))),
            "signing_kid": peer["kid"]}


def _call(conn, peer: dict, skill: str, args: dict) -> dict:
    return handlers.dispatch(skill, _envelope(peer, skill, args), peer["peer_id"], conn)


def _legacy_args(key: str, value, author: str | None) -> dict:
    """The args the old client's `mc.write` sends: status OPEN, the mesh-chat tag, an author."""
    args = {"namespace": MESH, "key": key, "value": value, "status": "OPEN",
            "tags": ["mesh-chat"]}
    if author is not None:
        args["author"] = author
    return args


def _refused(conn, peer: dict, args: dict) -> HandlerError:
    with pytest.raises(HandlerError) as exc:
        _call(conn, peer, "memory.write", args)
    return exc.value


def _stored(conn, key: str) -> list[tuple]:
    return [(r[0], r[1], r[2], r[3]) for r in conn.execute(
        "SELECT version, is_head, author_label, value FROM shared_memory "
        "WHERE namespace = %s AND key = %s ORDER BY version", (MESH, key)).fetchall()]


def _accepted() -> int:
    from apps.core import metrics
    return metrics.MESH_UNVERSIONED_ACCEPTED.value()


def _bearer(peer_id) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    token = pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + 300,
                          "aud": "peer", "scopes": []}, JWT_PRIVATE_PEM, algorithm="EdDSA")
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# The parse
# --------------------------------------------------------------------------- #

def test_the_flag_is_off_unless_spelled_on_and_a_malformed_value_refuses():
    """MESHY_CAPABILITIES' vocabulary (apps/core/capability_flag.py), the default OFF."""
    from apps.core import capability_flag, mesh_unversioned as mu

    assert mu.ENV_VAR == FLAG
    assert (mu.FLAG_TRUE, mu.FLAG_FALSE) == (capability_flag.FLAG_TRUE,
                                             capability_flag.FLAG_FALSE)
    assert mu.accept_from_env({}) is False
    for raw in ("", "   "):
        assert mu.accept_from_env({FLAG: raw}) is False, raw
    for raw in ("1", "true", "yes", "on", " ON ", "True"):
        assert mu.accept_from_env({FLAG: raw}) is True, raw
    for raw in ("0", "false", "no", "off", "Off"):
        assert mu.accept_from_env({FLAG: raw}) is False, raw
    for raw in ("maybe", "enabled", "2", "on,off", "tru"):
        with pytest.raises(mu.UnversionedConfigError, match=FLAG):
            mu.accept_from_env({FLAG: raw})


def _child_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_") and k != FLAG}
    env.update({"DJANGO_SETTINGS_MODULE": "meshychat.settings", **extra})
    return env


def _load_settings(extra: dict) -> subprocess.CompletedProcess:
    code = f"import meshychat.settings as s; print('FLAG', repr(s.{FLAG}))"
    return subprocess.run([sys.executable, "-c", code], env=_child_env(extra), cwd=str(ROOT),
                          capture_output=True, text=True, timeout=120)


def test_settings_read_the_environment_once_and_a_malformed_value_refuses_to_load():
    """settings.py holds a bool parsed from the ENVIRONMENT by `accept_from_env`: unset it is
    False, spelled on it is True (the control that the variable is read at all), and a
    malformed value stops the settings module from loading."""
    tree = ast.parse((ROOT / "meshychat" / "settings.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and "unversioned" in n.func.id.lower()]
    assert calls, "settings.py does not call the flag's parser"

    unset = _load_settings({})
    assert unset.returncode == 0 and "FLAG False" in unset.stdout, (unset.stdout,
                                                                  unset.stderr[-1500:])
    on = _load_settings({FLAG: "true"})
    assert on.returncode == 0 and "FLAG True" in on.stdout, (on.stdout, on.stderr[-1500:])
    bad = _load_settings({FLAG: "maybe"})
    assert bad.returncode != 0 and "UnversionedConfigError" in bad.stderr, (
        bad.returncode, bad.stdout, bad.stderr[-1500:])


def test_without_django_settings_the_reader_parses_the_environment():
    """memory.write also runs in a process that never configures Django
    (tests/test_memory_skills.py drives the handlers over a bare connection); there the reader
    parses the same variable, strictly, instead of raising ImproperlyConfigured."""
    code = ("import os; from apps.core import mesh_unversioned as m; "
            "from django.conf import settings; assert not settings.configured; "
            "assert 'DJANGO_SETTINGS_MODULE' not in os.environ; "
            "print('READ', m.accept_from_settings())")

    def child(extra: dict) -> subprocess.CompletedProcess:
        env = {k: v for k, v in _child_env(extra).items() if k != "DJANGO_SETTINGS_MODULE"}
        return subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120)

    unset, on, bad = child({}), child({FLAG: "on"}), child({FLAG: "maybe"})
    assert unset.returncode == 0 and "READ False" in unset.stdout, unset.stderr[-1500:]
    assert on.returncode == 0 and "READ True" in on.stdout, on.stderr[-1500:]
    assert bad.returncode != 0 and "UnversionedConfigError" in bad.stderr, bad.stderr[-1500:]


# --------------------------------------------------------------------------- #
# The validator, alone
# --------------------------------------------------------------------------- #

def test_the_validator_returns_early_only_for_an_unversioned_object_and_only_when_told():
    """A mesh message with NO schema_version key returns early and says so (True).
    Everything else remains validated normally."""
    v = validation.validate_mesh_message_v1
    legacy = _legacy_value("alice", "bob")
    key = f"msg/{int(time.time() * 1000)}-alice"

    assert v(MESH, key, dict(legacy), accept_unversioned=True) is True
    # CONTROL: the same value without the flag is refused, with the fix named.
    for off in ({}, {"accept_unversioned": False}):
        with pytest.raises(HandlerError) as exc:
            v(MESH, key, dict(legacy), **off)
        assert exc.value.code == "E_MESH_SCHEMA" and "MESH_SCHEMA_V1=1" in exc.value.message
    # Only `is True` opens it: a truthy non-bool is not the flag.
    with pytest.raises(HandlerError):
        v(MESH, key, dict(legacy), accept_unversioned="false")

    # A value that DECLARES a version is fully validated with the flag on.
    good = _v1_value("alice", "bob")
    assert v(MESH, key, dict(good), accept_unversioned=True) is False
    for bad, why in ((dict(good, schema_version=2), "schema_version must be the integer 1"),
                     (dict(good, schema_version=True), "schema_version must be the integer 1"),
                     (dict(good, schema_version=None), "schema_version must be the integer 1"),
                     ({"schema_version": 1, "body": "x"}, "missing required fields"),
                     (dict(good, surprise="x"), "unknown fields"),
                     (dict(good, to="bob"), "to must be a non-empty array")):
        with pytest.raises(HandlerError) as exc:
            v(MESH, key, bad, accept_unversioned=True)
        assert exc.value.code == "E_MESH_SCHEMA" and why in exc.value.message, (bad, exc.value)
    # A non-object is refused with the flag on.
    for bad in ("a string", ["a", "list"], 7, True):
        with pytest.raises(HandlerError) as exc:
            v(MESH, key, bad, accept_unversioned=True)
        assert exc.value.code == "E_MESH_SCHEMA", bad
    # Out of scope stays out of scope, and says it skipped nothing.
    assert v(MESH, "roster/x", {"any": 1}, accept_unversioned=True) is False
    assert v("shared/wiki", key, {"any": 1}, accept_unversioned=True) is False


# --------------------------------------------------------------------------- #
# The write, through the real memory.write handler
# --------------------------------------------------------------------------- #

def test_with_the_flag_on_the_old_clients_value_is_accepted_and_stored_verbatim(
        flag_on, app_conn, owner_conn):
    """THE BLOCKER. The old client's default send, repointed here, with the flag on."""
    sender, recipient = _sender_and_recipient(owner_conn, "acc")
    key = _legacy_key(sender["name"])
    value = _legacy_value(sender["name"], recipient["name"])
    out = _call(app_conn, sender, "memory.write", _legacy_args(key, value, sender["name"]))
    assert out["version"] == 1 and out["replay"] is False, out
    assert _stored(owner_conn, key) == [(1, True, sender["name"], value)]


def test_with_the_flag_off_the_same_value_is_refused_and_nothing_is_written(
        flag_off, app_conn, owner_conn):
    """CONTROL, one variable apart: today's behaviour, unchanged."""
    sender, recipient = _sender_and_recipient(owner_conn, "off")
    key = _legacy_key(sender["name"])
    before = _accepted()
    err = _refused(app_conn, sender, _legacy_args(
        key, _legacy_value(sender["name"], recipient["name"]), sender["name"]))
    assert err.code == "E_MESH_SCHEMA" and "MESH_SCHEMA_V1=1" in err.message, err
    assert _stored(owner_conn, key) == []
    assert _accepted() == before


def test_the_default_setting_is_off(app_conn, owner_conn):
    """CONTROL: no override at all. This process's environment does not set the flag (the
    subprocess test proves the parse), so the loaded setting refuses."""
    from django.conf import settings
    assert os.environ.get(FLAG) is None, "this test needs the variable unset"
    assert settings.MESHY_MESH_ACCEPT_UNVERSIONED is False
    sender, recipient = _sender_and_recipient(owner_conn, "dflt")
    key = _legacy_key(sender["name"])
    err = _refused(app_conn, sender, _legacy_args(
        key, _legacy_value(sender["name"], recipient["name"]), sender["name"]))
    assert err.code == "E_MESH_SCHEMA", err
    assert _stored(owner_conn, key) == []


def test_with_the_flag_on_a_declared_version_is_still_fully_validated(
        flag_on, app_conn, owner_conn):
    sender, recipient = _sender_and_recipient(owner_conn, "decl")
    good = _v1_value(sender["name"], recipient["name"])
    before = _accepted()
    for bad, why in ((dict(good, schema_version=2), "schema_version must be the integer 1"),
                     ({"schema_version": 1, "from": sender["name"], "body": "x"},
                      "missing required fields"),
                     (dict(good, surprise="x"), "unknown fields"),
                     (dict(good, body="x" * 65_537), "body must not exceed")):
        key = _legacy_key(sender["name"])
        err = _refused(app_conn, sender, _legacy_args(key, bad, sender["name"]))
        assert err.code == "E_MESH_SCHEMA" and why in err.message, (why, err)
        assert _stored(owner_conn, key) == []
    # A non-object is refused.
    key = _legacy_key(sender["name"])
    err = _refused(app_conn, sender, _legacy_args(key, "a bare string", sender["name"]))
    assert err.code == "E_MESH_SCHEMA", err
    assert _stored(owner_conn, key) == []
    # POSITIVE CONTROL: a valid v1 value is accepted, and is NOT counted as unversioned.
    key = _legacy_key(sender["name"])
    assert _call(app_conn, sender, "memory.write",
                 _legacy_args(key, dict(good), sender["name"]))["version"] == 1
    assert _accepted() == before, "a refusal or a v1 row moved the unversioned counter"


def test_with_the_flag_on_every_other_write_rule_still_applies(flag_on, app_conn, owner_conn, monkeypatch):
    """Only the v1 FIELD validation is skipped. The key's epoch-ms and divergence 028's R1 to
    R4 refuse an unversioned value exactly as they refuse a v1 one, and nothing counts."""
    sender, recipient = _sender_and_recipient(owner_conn, "rules")
    other = _peer(owner_conn, "other")
    _admit(owner_conn, other, "write")
    to = recipient["name"]
    before = _accepted()

    # The key's ts segment (validate_mesh_key_ts).
    err = _refused(app_conn, sender, _legacy_args(
        f"msg/2026-09-27-{sender['name']}", _legacy_value(sender["name"], to), sender["name"]))
    assert err.code == "E_BAD_KEY_TS", err
    # R3: the key's embedded label is value.from.
    err = _refused(app_conn, sender, _legacy_args(
        _legacy_key(other["name"]), _legacy_value(sender["name"], to), sender["name"]))
    assert err.code == "E_KEY_LABEL_MISMATCH", err
    # R2: value.from is one of the caller's labels, the live spoof with key and from agreeing.
    key = _legacy_key(other["name"])
    err = _refused(app_conn, sender, _legacy_args(
        key, _legacy_value(other["name"], to), None))
    assert err.code == "E_FROM_MISMATCH", err
    assert _stored(owner_conn, key) == []
    # R2 with no `from` at all, and with a non-string one: v1 is not there to require a
    # label, so R3 compares str(from) with the key. Each key below embeds exactly that text,
    # so R3 passes and it is R2 that refuses.
    for shapeless, embedded in (({"to": to, "body": "no from"}, "None"),
                                ({"to": to, "from": ["a", "list"], "body": "list from"},
                                 str(["a", "list"])),
                                ({"to": to, "from": 7, "body": "number from"}, "7")):
        key = f"msg/{int(time.time() * 1000)}-{embedded}"
        err = _refused(app_conn, sender, _legacy_args(key, shapeless, None))
        assert err.code == "E_FROM_MISMATCH", (shapeless, err)
        assert _stored(owner_conn, key) == []
    # R1: a supplied author is one of the caller's labels. The old client's own default is
    # `<from>-cc`, which is not a label here: refused as it would be for a v1 value.
    key = _legacy_key(sender["name"])
    err = _refused(app_conn, sender, _legacy_args(
        key, _legacy_value(sender["name"], to), f"{sender['name']}-cc"))
    assert err.code == "E_AUTHOR_MISMATCH", err
    assert _stored(owner_conn, key) == []
    # R4: only the peer that created a message writes its later versions. The twin shares
    # the sender's LABEL, so R2 and R3 pass for it and R4 is the rule that refuses. Since divergence 066 a `peers.name` two peers hold is nobody's label, and the roster file refuses one label for two peers, so the label cache is the only place left to make the state: what a roster reload that moves a label to another peer leaves behind.
    twin = _peer(owner_conn, "twin")
    _admit(owner_conn, twin, "write")
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(sender["peer_id"]): sender["name"],
                                                     str(twin["peer_id"]): sender["name"]})
    key = _legacy_key(sender["name"])
    _call(app_conn, sender, "memory.write",
          _legacy_args(key, _legacy_value(sender["name"], to), sender["name"]))
    err = _refused(app_conn, twin, _legacy_args(
        key, _legacy_value(sender["name"], to, "overwrite"), None))
    assert err.code == "E_NOT_MESSAGE_AUTHOR", err
    assert [r[0] for r in _stored(owner_conn, key)] == [1]
    # The one accepted write above is the only one counted.
    assert _accepted() == before + 1


# --------------------------------------------------------------------------- #
# Counted and logged
# --------------------------------------------------------------------------- #

class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def log_lines():
    logger = logging.getLogger(LOGGER)
    handler, saved = _Lines(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved)


@pytest.fixture()
def clock(monkeypatch):
    """A fresh limiter on a clock the test moves, so the rate limit is deterministic."""
    from apps.core import mesh_unversioned as mu
    now = {"t": 1_000.0}
    monkeypatch.setattr(mu, "LIMITER", mu.LabelLimiter(
        mu.LOG_INTERVAL_S, mu.LOG_MAX_LABELS, clock=lambda: now["t"]))
    return now


def test_each_accepted_row_is_counted_once_and_a_replay_is_not(flag_on, app_conn, owner_conn):
    sender, recipient = _sender_and_recipient(owner_conn, "cnt")
    key = _legacy_key(sender["name"])
    envelope = _envelope(sender, "memory.write", _legacy_args(
        key, _legacy_value(sender["name"], recipient["name"]), sender["name"]))
    before = _accepted()
    first = handlers.dispatch("memory.write", envelope, sender["peer_id"], app_conn)
    assert first["replay"] is False and _accepted() == before + 1
    again = handlers.dispatch("memory.write", envelope, sender["peer_id"], app_conn)
    assert again["replay"] is True and again["id"] == first["id"], again
    assert _accepted() == before + 1, "a replay stored nothing and must count nothing"


def test_the_log_line_names_the_label_never_the_body_at_most_once_per_interval(
        flag_on, app_conn, owner_conn, log_lines, clock):
    from apps.core import mesh_unversioned as mu
    sender, recipient = _sender_and_recipient(owner_conn, "log")
    marker = f"BODY-MARKER-{secrets.token_hex(4)}"

    def send():
        _call(app_conn, sender, "memory.write", _legacy_args(
            _legacy_key(sender["name"]), _legacy_value(sender["name"], recipient["name"],
                                                       marker), sender["name"]))

    send()
    send()
    assert len(log_lines) == 1, [r.getMessage() for r in log_lines]
    line = log_lines[0]
    assert line.levelno == logging.INFO and line.name == LOGGER
    text = line.getMessage()
    assert sender["name"] in text and FLAG in text, text
    assert marker not in text and "legacy/probe" not in text and "msg/" not in text, text
    clock["t"] += mu.LOG_INTERVAL_S - 1
    send()
    assert len(log_lines) == 1, "logged again inside the interval"
    clock["t"] += 2
    send()
    assert len(log_lines) == 2, "the next interval's first acceptance was not logged"


def test_the_limiter_holds_a_bounded_number_of_labels_and_cuts_a_long_one():
    from apps.core import mesh_unversioned as mu
    now = {"t": 0.0}
    lim = mu.LabelLimiter(60.0, 2, clock=lambda: now["t"])
    assert [lim.admit(x) for x in ("a", "b", "a", "c", "d")] == [True, True, False, True, True]
    assert len(lim._last) <= 2
    assert mu.shown_label("x" * 500) == "x" * mu.LOG_LABEL_CHARS


def test_the_counter_is_on_the_metrics_route(flag_on, app_conn, owner_conn, http):
    """GET /v1/metrics carries the family, typed counter, with HELP, and moves by exactly one
    per accepted row."""
    name = "meshychat_mesh_unversioned_accepted_total"

    def scrape() -> float:
        resp = http.get("/v1/metrics", HTTP_AUTHORIZATION=f"Bearer {METRICS_TOKEN}")
        assert resp.status_code == 200, resp.content[:200]
        lines = resp.content.decode("utf-8").splitlines()
        assert f"# TYPE {name} counter" in lines, lines
        assert any(ln.startswith(f"# HELP {name} ") for ln in lines), lines
        samples = [ln for ln in lines if ln.startswith(f"{name} ")]
        assert len(samples) == 1, samples
        return float(samples[0].split(" ", 1)[1])

    before = scrape()
    sender, recipient = _sender_and_recipient(owner_conn, "met")
    _call(app_conn, sender, "memory.write", _legacy_args(
        _legacy_key(sender["name"]), _legacy_value(sender["name"], recipient["name"]),
        sender["name"]))
    assert scrape() == before + 1


# --------------------------------------------------------------------------- #
# Readable: (a) the old client's read and query, (b) the new client's surfaces
# --------------------------------------------------------------------------- #

def test_an_unversioned_row_is_readable_by_memory_read_and_memory_query(
        flag_on, app_conn, owner_conn):
    """(a) The old client reads the mesh with memory.read (one key) and memory.query (a key
    prefix). Both return the row verbatim, to the recipient."""
    sender, recipient = _sender_and_recipient(owner_conn, "rd")
    key = _legacy_key(sender["name"])
    value = _legacy_value(sender["name"], recipient["name"])
    _call(app_conn, sender, "memory.write", _legacy_args(key, value, sender["name"]))

    read = _call(app_conn, recipient, "memory.read", {"namespace": MESH, "key": key})
    memo = read["memo"]
    assert (memo["key"], memo["value"], memo["author_label"]) == (key, value, sender["name"])

    query = _call(app_conn, recipient, "memory.query",
                  {"namespace": MESH, "key_prefix": key, "author": sender["name"]})
    assert [(m["key"], m["value"]) for m in query["results"]] == [(key, value)], query


def _drain(conn) -> None:
    for _ in range(500):
        if not projection.catch_up(conn).more:
            return
    raise AssertionError("the projection did not drain")


def test_an_unversioned_row_is_projected_and_reaches_the_inbox_and_the_addressed_wait(
        flag_on, app_conn, owner_conn, http):
    """(b) The new client's surfaces: the projection's catch-up writes the row's edges (its
    string `to` as `to:string`), GET /v1/mesh/inbox answers it to the recipient, and the
    addressed wait (listen --monitor) frames it for the recipient as `to`."""
    sender, recipient = _sender_and_recipient(owner_conn, "inb")
    first = http.get("/v1/mesh/wait?addressed=1&timeout=0", **_bearer(recipient["peer_id"]))
    assert first.status_code in (200, 204), (first.status_code, first.content[:300])
    since = (first["X-Meshy-Cursor"] if first.status_code == 204
             else json.loads(first.content)["cursor"])

    key = _legacy_key(sender["name"])
    value = _legacy_value(sender["name"], recipient["name"])
    _call(app_conn, sender, "memory.write", _legacy_args(key, value, sender["name"]))

    _drain(owner_conn)
    with rls.server_internal(owner_conn):
        edges = {tuple(r) for r in owner_conn.execute(
            "SELECT edge_type, dst, src_field FROM mesh_edges WHERE src_key = %s", (key,))}
    assert edges == {("authored_by", sender["name"], "author_label"),
                     ("addressed_to", recipient["name"], "to:string"),
                     ("in_thread", "legacy/probe", "thread_id")}, edges

    inbox = http.get("/v1/mesh/inbox", **_bearer(recipient["peer_id"]))
    assert inbox.status_code == 200, inbox.content[:300]
    hits = {h["key"]: h for h in json.loads(inbox.content)["hits"]}
    assert key in hits, sorted(hits)
    assert hits[key]["src_field"] == "to:string", hits[key]

    waited = http.get(f"/v1/mesh/wait?addressed=1&timeout=0&since={since}",
                      **_bearer(recipient["peer_id"]))
    assert waited.status_code == 200, (waited.status_code, waited.content[:300])
    frames = {m["key"]: m for m in json.loads(waited.content)["messages"]}
    assert key in frames, sorted(frames)
    assert frames[key]["role"] == "to" and frames[key]["to"] == [recipient["name"]], frames[key]
