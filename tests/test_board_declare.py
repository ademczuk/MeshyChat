"""The write door of the board: declaring an act is as cheap as sending a message, and
the door refuses everything that is not an act.

RULES THIS FILE HOLDS ITSELF TO (docs/V1-DESIGN-RULES.md C1): every negative is paired
with a positive control in the SAME test, because "refused" and "the whole route is
broken" produce the same status from outside. Every refusal is also followed by a read
that shows NOTHING LANDED, because a 4xx after a write that happened is worse than a 201.

Run:  .venv/Scripts/python.exe -m pytest tests/test_board_declare.py -q
Needs MESHY_TEST_DSN, published by tests/conftest.py as the per-run clone.
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

ISSUER = "meshy-board-declare-test-issuer"
_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption())
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover - whichever module imports first wins
    django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.board import declare as D  # noqa: E402
from apps.board import projection as B  # noqa: E402
from apps.board.__main__ import main as board_main  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402

NS = B.MESH_CHAT_NAMESPACE


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def board_settings():
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM, MESHY_JWT_ISSUER=ISSUER,
                           MESHY_DSN=DSN):
        yield


@pytest.fixture()
def client():
    return Client()


@pytest.fixture()
def owner():
    """Owner connection, AUTOCOMMIT: seeds must be visible to the route's own connection."""
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _mint(peer_id, ttl=300) -> str:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + ttl,
                         "aud": "peer", "scopes": []}, JWT_PRIVATE_PEM, algorithm="EdDSA")


def _auth(peer_id) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {_mint(peer_id)}"}


def _namespace_owner(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace "
                    "WHERE namespace = %s", (NS,))
        return cur.fetchone()


def _peer(conn, tag: str, *, grant: bool = True) -> dict:
    """A dev-plane owner, a peer with an ACTIVE signing key, and a write grant on the mesh
    namespace (or the claim itself when nobody holds it yet).

    The key is real: `handle_write` resolves the kid in `peer_keys` and verifies the
    envelope against it, so a fake kid fails the write rather than writing unverifiable
    provenance.
    """
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    # The actor label IS the peer's name: divergence 028 binds `from`, the author and the
    # key's sender label to the labels the caller is answered for, and a free-form
    # `dcl-<tag>` beside a peers.name of `dcl-<tag>-<hex>` is exactly what it refuses.
    label = f"dcl-{tag}-{peer_id.hex[:8]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"dcl-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, label, f"dcl {tag}"))
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active)"
                    " VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(pub)))
    holder = _namespace_owner(conn)
    if holder is None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                        " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                        " VALUES (%s,%s,%s,'dev',FALSE,'keep_all')", (NS, peer_id, owner_id))
    elif grant and str(holder[0]) != str(peer_id):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                        " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id)"
                        " VALUES (%s,%s,%s,'write',%s,%s)",
                        (NS, peer_id, owner_id, holder[0], holder[1]))
    return {"peer_id": peer_id, "owner_id": owner_id, "label": label, "seed": seed, "kid": kid,
            "identity": D.Identity(peer_id=str(peer_id), kid=kid, seed=seed, label=label)}


def _grant(conn, peer: dict) -> None:
    holder = _namespace_owner(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id)"
                    " VALUES (%s,%s,%s,'write',%s,%s)",
                    (NS, peer["peer_id"], peer["owner_id"], holder[0], holder[1]))


def _subj(tag: str) -> str:
    return f"pr/{tag}-{uuid.uuid4().hex[:8]}"


def _envelope(peer: dict, subject: str, verb: str = "reviewed", ref: str = "d4abcb706",
              *, value_edit=None, seed: bytes | None = None,
              kid: str | None = None) -> tuple[dict, str]:
    """A signed envelope for one act. `value_edit` mutates the value BEFORE signing so a
    test can build the exact defective row it is about; `seed` and `kid` let a test sign
    with the wrong key or name the wrong kid while the payload still names `peer`."""
    key = D.mint_key(peer["label"])
    value = D.act_value(subject=subject, verb=verb, ref=ref, actor_label=peer["label"])
    if value_edit:
        value_edit(value)
    args = {"namespace": NS, "key": key, "value": value, "author": peer["label"],
            "expected_head_version": 0}
    env = D.signed_envelope(D.Identity(peer_id=str(peer["peer_id"]), kid=kid or peer["kid"],
                                       seed=seed or peer["seed"], label=peer["label"]), args)
    return env, key


def _post(client, peer: dict, envelope: dict):
    return client.post("/v1/board/acts", data=json.dumps({"envelope": envelope}),
                       content_type="application/json", **_auth(peer["peer_id"]))


def _state(client, peer: dict, subject: str) -> dict:
    r = client.get(f"/v1/board/subjects/{subject}", **_auth(peer["peer_id"]))
    assert r.status_code == 200, r.content[:200]
    return r.json()


# --------------------------------------------------------------------------- #
# The door
# --------------------------------------------------------------------------- #

def test_declare_lands_and_the_board_answers_with_the_bearer_as_actor(client, owner):
    p = _peer(owner, "lands")
    s = _subj("lands")
    env, key = _envelope(p, s, "reviewed", "d4abcb706")

    r = _post(client, p, env)
    assert r.status_code == 201, r.content[:300]
    body = r.json()
    assert body["key"] == key and body["version"] == 1 and body["replay"] is False
    act = body["act"]
    assert act["subject"] == s and act["verb"] == "reviewed" and act["ref"] == "d4abcb706"
    # The actor is the BEARER as the database recorded it, not a field in the envelope.
    assert act["actor_peer_id"] == str(p["peer_id"])
    assert body["adoption"]["declared"] >= 1
    assert any(s in line for line in body["prose"])

    # And the read half now answers about it, from a separate request.
    st = _state(client, p, s)
    assert st["state"] == "acts"
    assert [a["key"] for a in st["acts"]] == [key]
    assert st["latest_by_verb"]["reviewed"]["actor_peer_id"] == str(p["peer_id"])
    assert st["latest_by_verb"]["reviewed"]["ref"] == "d4abcb706"


def test_the_body_restates_the_act_in_prose_unless_the_actor_writes_one(owner):
    p = _peer(owner, "prose")
    v = D.act_value(subject="pr/1", verb="reviewed", ref="abc", actor_label=p["label"])
    assert v["body"] == f"{p['label']} reviewed pr/1 at abc"
    v2 = D.act_value(subject="pr/1", verb="reviewed", ref="abc", actor_label=p["label"],
                     body="held: the fix is unopened")
    assert v2["body"] == "held: the fix is unopened"
    assert (v2["subject"], v2["type"], v2["ref"]) == ("pr/1", "reviewed", "abc")


def test_a_bare_message_is_refused_and_the_missing_fields_are_named(client, owner):
    p = _peer(owner, "bare")
    s = _subj("bare")

    def strip(value):
        value.pop("subject"); value.pop("ref")
    env, key = _envelope(p, s, value_edit=strip)
    r = _post(client, p, env)
    assert r.status_code == 422, r.content[:300]
    body = r.json()
    assert body["error"] == "E_MESH_SCHEMA"
    assert "subject" in body["message"] and "ref" in body["message"]
    # Nothing landed: the refusal happened BEFORE the handler, not after a write.
    assert _state(client, p, s)["state"] == "no_declared_acts"
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory WHERE key = %s", (key,))
        assert cur.fetchone()[0] == 0

    # Positive control: the same act WITH the fields lands.
    env2, _ = _envelope(p, s)
    assert _post(client, p, env2).status_code == 201


def test_a_row_without_schema_version_cannot_launder_through_the_door(client, owner):
    """Outside v1 the daemon returns before validating, so subject and ref would be
    stored UNCHECKED and read later as though the schema had accepted them."""
    p = _peer(owner, "nosv")
    s = _subj("nosv")
    env, key = _envelope(p, s, value_edit=lambda v: v.pop("schema_version"))
    r = _post(client, p, env)
    assert r.status_code == 422 and "schema_version" in r.json()["message"]
    assert _state(client, p, s)["state"] == "no_declared_acts"
    env2, _ = _envelope(p, s)
    assert _post(client, p, env2).status_code == 201


def test_the_envelope_must_name_and_be_signed_by_the_bearer(client, owner):
    a = _peer(owner, "sig-a")
    b = _peer(owner, "sig-b")
    s = _subj("sig")

    # A's kid, B's seed: the key resolves and the signature does not verify against it.
    forged, key1 = _envelope(a, s, seed=b["seed"])
    r = _post(client, a, forged)
    assert r.status_code == 400, r.content[:300]
    assert r.json()["error"] == "E_BAD_ENVELOPE"

    # B's kid on A's payload: no such key on the bearer, refused before any verify.
    wrong_kid, key2 = _envelope(a, s, seed=b["seed"], kid=b["kid"])
    r = _post(client, a, wrong_kid)
    assert r.status_code == 400 and r.json()["error"] == "E_PROVENANCE_KEY"

    # A valid envelope of B's, presented under A's bearer: check 4, the identity binding,
    # refuses it at the door (divergence 038; before 038 the handler's binding did, 400
    # E_BAD_ENVELOPE), with invoke's code and status.
    theirs, key3 = _envelope(b, s)
    r = _post(client, a, theirs)
    assert r.status_code == 401 and r.json()["error"] == "E_SIG_IDENTITY_MISMATCH"

    assert _state(client, a, s)["state"] == "no_declared_acts"
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory WHERE key IN (%s, %s, %s)",
                    (key1, key2, key3))
        assert cur.fetchone()[0] == 0

    # Positive control: A's own, under A's bearer.
    mine, _ = _envelope(a, s)
    assert _post(client, a, mine).status_code == 201


def test_a_payload_that_names_nobody_is_refused_and_the_bearer_is_the_actor(client, owner):
    """Until divergence 038 this test PINNED the opposite: `_bind_envelope_to_the_call`
    admitted a payload with NO caller_peer_id, verified it against the bearer's key and
    attributed the row to the bearer, so the stored bytes named nobody (design B02, G2).
    The approved Q9 reverses that: the caller is REQUIRED. So the payload that names nobody
    is refused and nothing lands; the same act naming the bearer lands, written by the
    bearer.

    The mutant this test used to kill, "act.actor_peer_id copied from the request", is
    EQUIVALENT once the caller is required: every landing envelope names exactly the bearer
    (the binding compares `str(caller_peer_id)`), so echo and read-back agree by construction.
    The status only is asserted here; the code is the door's (check 4 of verify_payload)
    and is pinned by the table of envelope checks below.
    """
    p = _peer(owner, "nobody")
    s = _subj("nobody")
    key = D.mint_key(p["label"])
    value = D.act_value(subject=s, verb="reviewed", ref="d4abcb706", actor_label=p["label"])
    args = D.write_args(key=key, value=value, actor_label=p["label"])
    env = D.signed_envelope(p["identity"], args)
    del env["payload"]["caller_peer_id"]
    env["signature"] = P.b64u(P.sign(p["seed"], P.jcs(env["payload"])))  # re-sign the edited payload

    r = _post(client, p, env)
    assert r.status_code == 400, r.content[:300]
    assert "caller_peer_id" in r.json()["message"], r.content[:300]
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory WHERE key = %s", (key,))
        assert cur.fetchone()[0] == 0

    # POSITIVE CONTROL: the same act, naming the bearer, lands as the bearer.
    env2 = D.signed_envelope(p["identity"], args)
    r = _post(client, p, env2)
    assert r.status_code == 201, r.content[:300]
    assert r.json()["act"]["actor_peer_id"] == str(p["peer_id"])
    with owner.cursor() as cur:
        cur.execute("SELECT created_by_peer_id FROM shared_memory WHERE key = %s", (key,))
        assert str(cur.fetchone()[0]) == str(p["peer_id"])


def test_without_a_grant_the_door_is_403_and_nothing_lands(client, owner):
    _peer(owner, "holder")                       # ensures the namespace is claimed
    p = _peer(owner, "nogrant", grant=False)
    s = _subj("nogrant")
    env, key = _envelope(p, s)
    r = _post(client, p, env)
    assert r.status_code == 403, r.content[:300]
    assert r.json()["error"] == "E_PERMISSION_DENIED"
    assert _state(client, p, s)["state"] == "no_declared_acts"

    _grant(owner, p)
    env2, _ = _envelope(p, s)
    assert _post(client, p, env2).status_code == 201


def test_an_act_naming_another_seat_is_403_and_a_key_naming_one_is_422(client, owner, monkeypatch):
    """Divergence 028 through the write door: the handler's typed refusals reach the caller
    as the statuses `_STATUS_FOR` maps, 403 for an act that names someone other than the
    bearer or rewrites their row, 422 for a key that does not embed its sender. Review
    mutant M7 removed that mapping, every one fell to the 400 fallback, and the suite
    stayed green. Each refusal writes nothing; the bearer's own act is the control."""
    a = _peer(owner, "who-a")
    b = _peer(owner, "who-b")
    c = _peer(owner, "who-c")
    # c is a second node under a's LABEL: R1 to R3 cannot tell them apart, R4 can. Since divergence 066 a `peers.name` two peers hold is nobody's label, and the roster file refuses one label for two peers, so the label cache is the only place left to make the state: what a roster reload that moves a label to another peer leaves behind.
    from apps.memoryskills import handlers
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(a["peer_id"]): a["label"], str(c["peer_id"]): a["label"]})
    s = _subj("who")

    def act(peer, label, *, key=None, author=True):
        key = key or D.mint_key(label)
        value = D.act_value(subject=s, verb="reviewed", ref="d4abcb706", actor_label=label)
        args = {"namespace": NS, "key": key, "value": value, "expected_head_version": 0}
        if author:
            args["author"] = label
        ident = D.Identity(peer_id=str(peer["peer_id"]), kid=peer["kid"], seed=peer["seed"],
                           label=label)
        return D.signed_envelope(ident, args), key

    mine, mine_key = act(a, a["label"])
    r = _post(client, a, mine)
    assert r.status_code == 201, r.content[:300]

    refused = [
        (a, act(a, b["label"]), 403, "E_AUTHOR_MISMATCH"),
        (a, act(a, b["label"], author=False), 403, "E_FROM_MISMATCH"),
        (a, act(a, a["label"], key=D.mint_key(b["label"])), 422, "E_KEY_LABEL_MISMATCH"),
        (c, act(c, a["label"], key=mine_key), 403, "E_NOT_MESSAGE_AUTHOR"),
    ]
    for bearer, (env, key), status, code in refused:
        r = _post(client, bearer, env)
        assert (r.status_code, r.json()["error"]) == (status, code), r.content[:300]
        with owner.cursor() as cur:
            cur.execute("SELECT count(*), max(version) FROM shared_memory WHERE key = %s",
                        (key,))
            assert cur.fetchone() == ((1, 1) if key == mine_key else (0, None)), code

def test_replaying_the_same_envelope_is_one_row_and_says_so(client, owner):
    p = _peer(owner, "replay")
    s = _subj("replay")
    env, key = _envelope(p, s)
    first = _post(client, p, env)
    assert first.status_code == 201 and first.json()["replay"] is False
    again = _post(client, p, env)
    assert again.status_code == 200, again.content[:300]
    assert again.json()["replay"] is True and again.json()["key"] == key
    st = _state(client, p, s)
    assert len(st["acts"]) == 1


def test_get_on_the_act_door_is_405_and_the_read_routes_still_refuse_post(client, owner):
    p = _peer(owner, "verbs")
    r = client.get("/v1/board/acts", **_auth(p["peer_id"]))
    assert r.status_code == 405 and r.headers.get("Allow") == "POST"
    r = client.post("/v1/board/subjects", **_auth(p["peer_id"]))
    assert r.status_code == 405 and r.headers.get("Allow") == "GET"


def test_the_door_needs_a_bearer_and_a_json_object_with_an_envelope(client, owner):
    p = _peer(owner, "shape")
    assert client.post("/v1/board/acts", data="{}", content_type="application/json").status_code == 401
    assert client.post("/v1/board/acts", data="x=1", content_type="text/plain",
                       **_auth(p["peer_id"])).status_code == 415
    assert client.post("/v1/board/acts", data="not json", content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 400
    assert client.post("/v1/board/acts", data="[]", content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 422
    r = client.post("/v1/board/acts", data=json.dumps({"envelope": "nope"}),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 422 and "envelope" in r.json()["message"]


# --------------------------------------------------------------------------- #
# Divergence 038: the door runs verify_payload's checks 1-4 before dispatch
# --------------------------------------------------------------------------- #
#
# Design B02 Q9 (docs/design/signed-bytes-status.md G2, fin/design-signed-bytes 9aab90b,
# approved): until 038 nothing on this door checked the envelope's version, its kind, its
# freshness or that it names the bearer; a payload with none of them landed. The door now
# runs checks 1-4 of `apps.calls.envelope.verify_payload`, the same function and the same
# order as HTTP invoke, so a refusal carries invoke's code and status. Every payload here is
# RE-SIGNED after its edit, so the signature is valid and only the checks can refuse it.

_ABSENT = object()


def _iso_ago(seconds: float) -> str:
    """An RFC 3339 UTC instant `seconds` before now (after, for a negative)."""
    at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)
    return at.isoformat().replace("+00:00", "Z")


def _resigned(peer: dict, env: dict, **fields) -> dict:
    """`env` with `fields` set in its payload (removed for `_ABSENT`; a callable is called),
    signed again with the peer's own key."""
    payload = dict(env["payload"])
    for name, value in fields.items():
        if value is _ABSENT:
            payload.pop(name, None)
        else:
            payload[name] = value() if callable(value) else value
    return {**env, "payload": payload,
            "signature": P.b64u(P.sign(peer["seed"], P.jcs(payload)))}


def _count(owner, key: str) -> int:
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory WHERE key = %s", (key,))
        return cur.fetchone()[0]


_OTHER_PEER = str(uuid.uuid4())

#: (id, payload edits, status, code, a fragment the message must carry). The statuses and
#: codes are invoke's (apps/calls/errors.SignatureFailed): a broken REQUEST is 400, a caller
#: that is not who it claims (a replay, another identity) is 401.
_ENVELOPE_CHECKS = [
    ("no-v", {"v": _ABSENT}, 400, "E_SIG_BAD", "payload.v"),
    ("v-2", {"v": 2}, 400, "E_SIG_VERSION_UNSUPPORTED", "version 2"),
    ("no-kind", {"kind": _ABSENT}, 400, "E_SIG_BAD", "payload.kind"),
    ("kind-result", {"kind": "skill.result"}, 400, "E_SIG_KIND_MISMATCH", "skill.result"),
    ("no-issued-at", {"issued_at": _ABSENT}, 400, "E_SIG_BAD", "issued_at"),
    ("issued-at-no-offset", {"issued_at": lambda: _iso_ago(0).rstrip("Z")}, 400, "E_SIG_BAD",
     "issued_at"),
    ("a-day-old", {"issued_at": lambda: _iso_ago(86400)}, 401, "E_SIG_REPLAY", "120"),
    # Review of 038, F1: UTC instants outside Python's datetime range raised OverflowError,
    # an unhandled 500 at this door; the route answers E_SIG_REPLAY.
    ("year-1-east-of-utc", {"issued_at": "0001-01-01T00:00:00+01:00"}, 401, "E_SIG_REPLAY",
     "120"),
    ("year-9999-west-of-utc", {"issued_at": "9999-12-31T23:59:59-01:00"}, 401, "E_SIG_REPLAY",
     "120"),
    # Review of 038, F8: ISO 8601's basic form is not RFC 3339 and is refused.
    ("issued-at-basic-form",
     {"issued_at": lambda: _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")},
     400, "E_SIG_BAD", "issued_at"),
    ("no-caller", {"caller_peer_id": _ABSENT}, 400, "E_SIG_BAD", "caller_peer_id"),
    ("caller-not-a-uuid", {"caller_peer_id": "nobody"}, 400, "E_SIG_BAD", "caller_peer_id"),
    ("caller-another-peer", {"caller_peer_id": _OTHER_PEER}, 401, "E_SIG_IDENTITY_MISMATCH",
     "identity"),
    # Divergence 038's skill_name bullet. The binding compares skill_name only when present,
    # so an act naming none reached dispatch; since A1 (039) the agree-trigger refused it
    # there as an untyped 500 E_INTERNAL. The door now refuses it as malformed, before
    # dispatch, as it refuses a missing caller.
    ("no-skill-name", {"skill_name": _ABSENT}, 400, "E_SIG_BAD", "payload.skill_name"),
    ("null-skill-name", {"skill_name": None}, 400, "E_SIG_BAD", "payload.skill_name"),
]


@pytest.mark.parametrize("edits,status,code,fragment",
                         [c[1:] for c in _ENVELOPE_CHECKS], ids=[c[0] for c in _ENVELOPE_CHECKS])
def test_the_door_runs_the_envelope_checks_before_dispatch(client, owner, edits, status, code,
                                                           fragment):
    p = _peer(owner, "chk")
    s = _subj("chk")
    env, key = _envelope(p, s)
    r = _post(client, p, _resigned(p, env, **edits))
    assert (r.status_code, r.json().get("error")) == (status, code), r.content[:300]
    assert fragment in r.json()["message"], r.content[:300]
    assert _count(owner, key) == 0, "refused, and a row landed anyway"
    assert _state(client, p, s)["state"] == "no_declared_acts"

    # POSITIVE CONTROL: the same act, complete and fresh, lands.
    env2, key2 = _envelope(p, s)
    r = _post(client, p, env2)
    assert r.status_code == 201, r.content[:300]
    assert _count(owner, key2) == 1


def test_the_freshness_window_is_120_seconds_either_side(client, owner):
    """Check 3: `issued_at` within 120 s of the server clock, past or future
    (envelope.REPLAY_WINDOW_SECS). 150 s either side is refused E_SIG_REPLAY; 100 s either
    side lands, so the bound is the window and not something tighter."""
    p = _peer(owner, "win")
    s = _subj("win")
    for seconds in (150, -150):
        env, key = _envelope(p, s)
        r = _post(client, p, _resigned(p, env, issued_at=_iso_ago(seconds)))
        assert (r.status_code, r.json().get("error")) == (401, "E_SIG_REPLAY"), (seconds, r.content)
        assert _count(owner, key) == 0, seconds
    for seconds in (100, -100):
        env, key = _envelope(p, s)
        r = _post(client, p, _resigned(p, env, issued_at=_iso_ago(seconds)))
        assert r.status_code == 201, (seconds, r.content[:300])
        assert _count(owner, key) == 1, seconds


def test_a_resend_inside_the_window_is_the_replay_and_after_it_is_refused(client, owner):
    """What the door does about replay WITHOUT the nonce ledger (divergence 038 says why it
    has none). A byte-identical resend inside the 120 s window reaches the handler's dedup
    (payload hash or call_id, `handlers._existing_by_dedup`) and is answered `replay: true`,
    200, one row. The same bytes once the window has closed are refused E_SIG_REPLAY before
    dispatch: still one row.

    That second answer DIFFERS FROM INVOKE, and on purpose: invoke's idempotency check is
    step 2, before verification, so a late resend of a call that was created is answered
    from its row; the board's dedup is inside `handle_write`, after the checks the design
    puts in front of dispatch. No client in this repository posts to the door.

    The envelope is signed 115 s in the past so the window closes during the test: the
    sleep is the clock, not a race.
    """
    import time

    p = _peer(owner, "late")
    s = _subj("late")
    env, key = _envelope(p, s)
    signed_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=115)
    env = _resigned(p, env, issued_at=signed_at.isoformat().replace("+00:00", "Z"))

    first = _post(client, p, env)
    assert first.status_code == 201, first.content[:300]
    again = _post(client, p, env)
    assert (again.status_code, again.json().get("replay")) == (200, True), again.content[:300]
    assert _count(owner, key) == 1

    age = (_dt.datetime.now(_dt.timezone.utc) - signed_at).total_seconds()
    time.sleep(max(0.0, 121.5 - age))
    late = _post(client, p, env)
    assert (late.status_code, late.json().get("error")) == (401, "E_SIG_REPLAY"), \
        late.content[:300]
    assert _count(owner, key) == 1


def test_the_envelope_checks_answer_before_the_act_gate(client, owner):
    """An envelope that fails both the checks and the act gate is answered by the checks:
    whether the envelope is the bearer's and fresh is decided before what it carries, as
    invoke verifies (step 4) before the handler reads an argument. Control: the same bare
    message, fresh, gets the act gate's 422 naming the missing fields. Nothing lands."""
    p = _peer(owner, "order")
    s = _subj("order")

    def strip(value):
        value.pop("subject"); value.pop("ref")
    env, key = _envelope(p, s, value_edit=strip)
    r = _post(client, p, _resigned(p, env, issued_at=_iso_ago(600)))
    assert (r.status_code, r.json().get("error")) == (401, "E_SIG_REPLAY"), r.content[:300]
    r = _post(client, p, env)
    assert (r.status_code, r.json().get("error")) == (422, "E_MESH_SCHEMA"), r.content[:300]
    assert _count(owner, key) == 0


def test_the_door_answers_with_invokes_codes_for_the_same_envelope(client, owner):
    """One definition of the checks, not two: the door's refusal of an envelope is exactly
    what `verify_payload` raises for it, code, status and message."""
    from apps.calls.envelope import KIND_SKILL_CALL, SignedEnvelope, verify_payload
    from apps.calls.errors import SignatureFailed

    p = _peer(owner, "same")
    s = _subj("same")
    env, _ = _envelope(p, s)
    stale = _resigned(p, env, issued_at=_iso_ago(600))
    r = _post(client, p, stale)
    with pytest.raises(SignatureFailed) as exc:
        verify_payload(owner, SignedEnvelope(payload=stale["payload"],
                                             signature=stale["signature"],
                                             signing_kid=stale["signing_kid"]),
                       p["peer_id"], KIND_SKILL_CALL)
    assert (r.status_code, r.json()) == (
        exc.value.http_status, {"error": exc.value.code, "message": exc.value.message})


# --------------------------------------------------------------------------- #
# The builder: what makes it cheap, and what it refuses to guess
# --------------------------------------------------------------------------- #

def test_mint_key_is_in_band_and_a_date_shaped_key_is_refused():
    key = D.mint_key("dcl-mint")
    # The sender is in the key (divergence 028 R3): `msg/<epoch-ms>-<sender>-<nonce>`.
    assert key.startswith("msg/") and key.split("-", 1)[1].startswith("dcl-mint-"), key
    D.mint_key("dcl-mint", now_ms=int(D.now_utc().timestamp() * 1000))  # explicit, in band
    with pytest.raises(HandlerError) as exc:
        D.mint_key("dcl-mint", now_ms=20260921)  # a date, not an epoch-ms: poisons the cursor
    assert exc.value.code == "E_BAD_KEY_TS"


def test_ref_auto_is_the_head_of_the_checkout_and_is_refused_outside_one(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test"]
    subprocess.run(g + ["init", "-q"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(g + ["add", "f.txt"], cwd=repo, check=True)
    subprocess.run(g + ["commit", "-q", "-m", "one"], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()

    assert D.resolve_ref("auto", checkout=str(repo)) == head
    assert D.resolve_ref("explicit-ref", checkout=str(repo)) == "explicit-ref"

    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    with pytest.raises(D.NoCheckout) as exc:
        D.resolve_ref("auto", checkout=str(bare))
    assert "--ref" in str(exc.value)


def test_write_args_validate_with_the_daemons_own_validator_before_signing():
    label = "dcl-validate"
    v = D.act_value(subject="pr/1", verb="reviewed", ref="abc", actor_label=label)
    args = D.write_args(key=D.mint_key(label), value=v, actor_label=label)
    assert args["expected_head_version"] == 0 and args["namespace"] == NS
    bad = D.act_value(subject="pr/1", verb="Reviewed", ref="abc", actor_label=label)  # verb: uppercase
    with pytest.raises(HandlerError) as exc:
        D.write_args(key=D.mint_key(label), value=bad, actor_label=label)
    assert exc.value.code == "E_MESH_SCHEMA"
    # Divergence 028 R3, also before signing: a key minted for another sender.
    with pytest.raises(HandlerError) as exc:
        D.write_args(key=D.mint_key("someone-else"), value=v, actor_label=label)
    assert exc.value.code == "E_KEY_LABEL_MISMATCH"


def test_identity_reads_the_fleets_state_file_shape(tmp_path):
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    f = tmp_path / ".agentmeshy-x-state.json"
    f.write_text(json.dumps({"peer_id": str(uuid.uuid4()), "kid": P.kid_for_peer_key(pub),
                             "signing_key_b64url": P.b64u(seed), "public_key_b64url": P.b64u(pub),
                             "owner_id": str(uuid.uuid4()), "jwt": "x"}), encoding="utf-8")
    ident = D.Identity.from_state_file(str(f), label="x")
    assert ident.seed == seed and ident.label == "x"
    f.write_text(json.dumps({"peer_id": "p"}), encoding="utf-8")
    with pytest.raises(ValueError):
        D.Identity.from_state_file(str(f))


# --------------------------------------------------------------------------- #
# The CLI, in-process and as the entry point people actually run
# --------------------------------------------------------------------------- #

def _state_file(tmp_path, peer: dict) -> str:
    f = tmp_path / f".agentmeshy-{peer['label']}-state.json"
    f.write_text(json.dumps({"peer_id": str(peer["peer_id"]), "kid": peer["kid"],
                             "signing_key_b64url": P.b64u(peer["seed"]),
                             "public_key_b64url": P.b64u(P.public_key_of(peer["seed"])),
                             "owner_id": str(peer["owner_id"])}), encoding="utf-8")
    return str(f)


def test_cli_declare_in_process_then_the_read_shows_it(owner, tmp_path, capsys):
    p = _peer(owner, "cli")
    s = _subj("cli")
    rc = board_main(["declare", "--subject", s, "--type", "merged", "--ref", "0123abc",
                     "--state-file", _state_file(tmp_path, p), "--label", p["label"],
                     "--dsn", DSN])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "DECLARED msg/" in out and s in out
    rc = board_main(["--subject", s, "--dsn", DSN])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"{p['label']} merged it at ref 0123abc" in out


def test_cli_entry_point_distinguishes_declared_refused_and_could_not_try(owner, tmp_path):
    """Run as `python -m apps.board`, the way a seat runs it, not via main(). A script
    entry point broke this repository once while its function passed in-process."""
    p = _peer(owner, "sub")
    s = _subj("sub")
    sf = _state_file(tmp_path, p)
    env = dict(os.environ)
    env.pop("MESHY_DSN", None)

    def run(*extra):
        return subprocess.run([sys.executable, "-m", "apps.board", "declare", "--subject", s,
                               "--type", "tested", "--state-file", sf, "--label", p["label"],
                               "--dsn", DSN, *extra],
                              cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)

    ok = run("--ref", "9abcdef")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "DECLARED msg/" in ok.stdout

    no_repo = tmp_path / "plain"
    no_repo.mkdir()
    cannot = run("--ref", "auto", "--checkout", str(no_repo))
    assert cannot.returncode == 2, cannot.stdout + cannot.stderr
    assert "NOT DECLARED" in cannot.stderr

    stranger = _peer(owner, "sub-nogrant", grant=False)
    refused = subprocess.run([sys.executable, "-m", "apps.board", "declare", "--subject", s,
                              "--type", "tested", "--ref", "9abcdef", "--state-file",
                              _state_file(tmp_path, stranger), "--label", stranger["label"],
                              "--dsn", DSN], cwd=ROOT, capture_output=True, text=True, env=env,
                             timeout=120)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "REFUSED" in refused.stderr and "E_PERMISSION_DENIED" in refused.stderr


# --------------------------------------------------------------------------- #
# Divergence 038: the in-process path shares the door's checks
# --------------------------------------------------------------------------- #

def test_the_in_process_builder_signs_a_payload_the_checks_accept(owner):
    """`python -m apps.board declare` never crosses HTTP. It builds the whole payload
    itself (`declare.signed_envelope`), so what the design asks of it (Q9) is that the
    payload it builds is one checks 1-4 accept, for the identity that signs it."""
    from apps.calls.envelope import KIND_SKILL_CALL, check_claims

    p = _peer(owner, "w4b")
    env, _ = _envelope(p, _subj("w4b"))
    claims = check_claims(env["payload"], p["peer_id"], KIND_SKILL_CALL)
    assert (claims.kind, claims.peer_id) == (KIND_SKILL_CALL, p["peer_id"])
    assert {"v", "kind", "caller_peer_id", "issued_at"} <= set(env["payload"])


def test_the_cli_runs_the_same_checks_and_reports_a_refusal_as_refused(owner, tmp_path, capsys,
                                                                       monkeypatch):
    """The CLI goes through `declare.declare`, the function the door calls, so it runs the
    same checks. Its clock is set ten minutes back, so the envelope it signs is stale when
    `declare` checks it against the real one: refused E_SIG_REPLAY, exit 1 (the handler-
    refusal exit, not a traceback), nothing lands. Control: the real clock, it declares."""
    p = _peer(owner, "w4")
    s = _subj("w4")
    argv = ["declare", "--subject", s, "--type", "tested", "--ref", "0123abc",
            "--state-file", _state_file(tmp_path, p), "--label", p["label"], "--dsn", DSN]
    real = D.now_utc
    monkeypatch.setattr(D, "now_utc", lambda: real() - _dt.timedelta(seconds=600))
    rc = board_main(argv)
    cap = capsys.readouterr()
    assert rc == 1, cap.out + cap.err
    # The whole line: the code AND invoke's message (review of 038, F6: a `_refusal` that
    # dropped the message survived the suite while this asserted the code alone).
    assert ("REFUSED: E_SIG_REPLAY: envelope replay window exceeded (issued_at outside +/- 120"
            " seconds)") in cap.err, cap.err
    assert isinstance(B.state_for(owner, s), B.NoDeclaredActs)

    monkeypatch.setattr(D, "now_utc", real)
    rc = board_main(argv)
    cap = capsys.readouterr()
    assert rc == 0, cap.out + cap.err
    assert "DECLARED msg/" in cap.out
