"""File artifacts, proved against the real database, the real filesystem and the REAL
routing table.

WHAT MAKES A TEST HERE COUNT
----------------------------
1. NO PINNED URLCONF. These tests resolve against `meshychat.urls` exactly as a request
   does. The previous round of route tests all passed while the application served 404,
   because each module pinned `ROOT_URLCONF` to its own urls module - a route test that
   supplies its own URLconf proves the view and says nothing about the wiring.
   `test_the_real_routing_table_serves_this_app` is the control for that, and it is
   first in the file so its failure reads as one named cause rather than twenty 404s.

2. EVERY REJECTION IS PAIRED WITH A POSITIVE CONTROL IN THE SAME TEST. A 404 is also
   what a typo in the URL, an unseeded fixture, a wrong JWT key or a closed connection
   produces. The cross-owner tests carry the control BOTH WAYS: the stranger's token is
   shown to work on the stranger's OWN artifact in the same test that shows it is
   refused on someone else's, so "refused" cannot be "the second peer was broken".

3. NO MOCKS FOR THE THINGS UNDER TEST. Rows go to the migrated Postgres named by
   MESHY_TEST_DSN; bytes go to a real temp directory through the real presigned-token
   round trip. The only doubles are a recording cursor (to MEASURE the SQL a request
   emits) and a deliberately broken blob store in the one test about backend failure.

Run: .venv/Scripts/python.exe -m pytest tests/test_files.py -q
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("MESHY_TEST_DSN", DSN)
os.environ.setdefault("MESHY_DSN", DSN)

ISSUER = "meshy-files-test-issuer"
PUBLIC_BASE_URL = "https://meshy.test"

_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("ascii")

# The PROJECT settings module, so ROOT_URLCONF is meshychat.urls - the real one.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.crypto import primitives as P  # noqa: E402
from apps.files import blob as blobmod  # noqa: E402
from apps.files import config as cfgmod  # noqa: E402
from apps.files import db as filesdb  # noqa: E402
from apps.files import errors as ferrors  # noqa: E402
from apps.files import quota, repository as repo, service  # noqa: E402

TOKEN_SECRET_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


# ------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def files_env(tmp_path, monkeypatch):
    """The blob backend and the JWT key, applied per test.

    `override_settings` LAYERS on top of whatever base configuration won the import race,
    so this behaves identically standalone and inside the full suite. ROOT_URLCONF is
    NOT among the overrides - that is the point of this file.
    """
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    monkeypatch.setenv("MESHYCHAT_BLOB_BACKEND", "fs")
    monkeypatch.setenv("MESHYCHAT_BLOB_DIR", str(blob_dir))
    monkeypatch.setenv("MESHYCHAT_BLOB_TOKEN_SECRET", TOKEN_SECRET_HEX)
    monkeypatch.setenv("MESHYCHAT_PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.delenv("MESHYCHAT_MAX_FILE_BYTES", raising=False)
    monkeypatch.delenv("MESHYCHAT_PRESIGN_TTL_SECS", raising=False)
    monkeypatch.delenv("MESHYCHAT_UPLOAD_WINDOW_SECS", raising=False)
    monkeypatch.setenv("MESHY_DSN", DSN)
    # The routes use the store built at STARTUP (B11), not the environment, so the
    # per-test environment above is installed the way apps/files/apps.py installs it,
    # and the process's own installation comes back after the test.
    monkeypatch.setattr(blobmod, "_ACTIVE", blobmod._ACTIVE)
    _reactivate()
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_DSN=DSN,
    ):
        yield blob_dir


def _reactivate() -> None:
    """Re-read the environment into THE store, as a restart would. A test that changes a
    MESHYCHAT_BLOB_* variable calls this; nothing in the application re-reads it (B11)."""
    blobmod.activate(cfgmod.build_blob_config())


@pytest.fixture()
def owner_conn():
    """Compatibility role `meshy`: bypasses RLS. SEEDING ONLY - never the connection under test."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """The connection a request actually runs on: meshychat_app, autocommit, guarded."""
    with filesdb.connection() as conn:
        yield conn


@pytest.fixture()
def client():
    return Client()


def _seed_peer(conn, tag: str) -> dict:
    """An owner + an active peer.

    Column names read off the live schema: the owner FK targets `dashboard_users`
    (NOT `owners`), and a 'product'-plane user must have a NULL trust_group_id
    (CHECK ((plane='dev') = (trust_group_id IS NOT NULL))).
    """
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'product',NULL)",
                    (owner_id, f"files-{tag}-{owner_id.hex[:10]}@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (peer_id, owner_id, f"files-{tag}-{peer_id.hex[:10]}"))
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id}


def _mint(peer_id, *, aud="peer", ttl=300) -> str:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims = {"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + ttl,
              "scopes": []}
    if aud is not None:
        claims["aud"] = aud
    return pyjwt.encode(claims, JWT_PRIVATE_PEM, algorithm="EdDSA")


def _auth(peer_id, **kw) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {_mint(peer_id, **kw)}"}


def _token_path(upload_url: str) -> str:
    """`{public_base_url}/v1/files/raw/{token}` -> the path the test client requests."""
    assert upload_url.startswith(PUBLIC_BASE_URL + "/v1/files/raw/"), upload_url
    return upload_url[len(PUBLIC_BASE_URL):]


def _create(client, peer, *, content_type="text/plain", size, filename="note.txt"):
    return client.post("/v1/files",
                       data=json.dumps({"filename": filename,
                                        "content_type": content_type,
                                        "size_bytes_declared": size}),
                       content_type="application/json", **_auth(peer))


def _upload_and_finalize(client, peer, payload: bytes, *, content_type="text/plain"):
    """The whole three-step flow, used as the SETUP for the read tests. Every assertion
    inside it is a positive control for the tests that build on it."""
    r = _create(client, peer, content_type=content_type, size=len(payload))
    assert r.status_code == 201, r.content
    created = r.json()
    put = client.put(_token_path(created["upload_url"]), data=payload,
                     content_type=content_type)
    assert put.status_code == 200, put.content
    sha = hashlib.sha256(payload).hexdigest()
    fin = client.post(f"/v1/files/{created['id']}/finalize",
                      data=json.dumps({"sha256": sha}),
                      content_type="application/json", **_auth(peer))
    assert fin.status_code == 200, fin.content
    return created["id"], fin.json(), sha


def _seed_skill(conn, peer_id) -> uuid.UUID:
    skill_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
                    "input_schema) VALUES (%s,%s,%s,'1.0.0','sync','private','{}'::jsonb)",
                    (skill_id, peer_id, f"skill-{skill_id.hex[:8]}"))
    conn.commit()
    return skill_id


def _seed_call(conn, *, caller, target, file_id, status, skill_id, where="args"):
    """A `skill_calls` row referencing `file_id` in ONE of its two attachment arrays.

    `where="args"`: the caller SENT the artifact (args_attachments). `where="result"`: the
    target RETURNED it (result_attachments), so the caller only RECEIVED it. Every guard in
    repository.py reads both arrays; before C17 this fixture only ever filled the first,
    which is why deleting the `result_attachments` half of the DELETE guard, or of the
    participant read, passed every test. The array not named is left EMPTY, so a test on
    one array cannot pass through the other.

    Every NOT NULL column is supplied from the measured schema; `mode` and `transport`
    have CHECK constraints ('sync', 'http') and `status` has its own ten-value CHECK."""
    assert where in ("args", "result"), where
    ref = [uuid.UUID(str(file_id))]
    call_uuid, call_id = uuid.uuid4(), f"call-{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_calls (id, call_id, caller_peer_id, caller_owner_id, "
            " target_peer_id, target_owner_id, skill_id, skill_name, skill_version, "
            " mode, transport, status, args, args_attachments, result_attachments, "
            " deadline_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'s','1.0.0','sync','http',%s,'{}'::jsonb,%s,%s,"
            "        NOW() + interval '1 hour')",
            (call_uuid, call_id, caller["peer_id"], caller["owner_id"],
             target["peer_id"], target["owner_id"], skill_id, status,
             ref if where == "args" else [], ref if where == "result" else []))
    conn.commit()
    return call_id


def _attachments(conn, call_id) -> tuple[list, list]:
    """The row as stored: the control that a `where=` seed filled one array only."""
    with conn.cursor() as cur:
        cur.execute("SELECT args_attachments, result_attachments FROM skill_calls "
                    "WHERE call_id = %s", (call_id,))
        args, result = cur.fetchone()
    return [str(u) for u in args], [str(u) for u in result]


# =============================================================== routing control

def test_the_real_routing_table_serves_this_app():
    """THE CONTROL FOR THE WHOLE FILE.

    Everything below would fail as an unexplained 404 if `apps.files.urls` were not
    included in `meshychat/urls.py`, or if another test module had re-pointed
    DJANGO_SETTINGS_MODULE and won the import race. Asserted directly so that failure
    names itself.
    """
    from django.conf import settings as live
    from django.urls import resolve
    from django.urls.exceptions import Resolver404

    assert live.ROOT_URLCONF == "meshychat.urls", (
        f"ROOT_URLCONF is {live.ROOT_URLCONF!r}: these tests must resolve against the "
        "PROJECT routing table, not an app's own urls module")

    assert resolve("/v1/files").url_name == "files-create"
    fid = uuid.uuid4()
    assert resolve(f"/v1/files/{fid}").url_name == "files-metadata"
    assert resolve(f"/v1/files/{fid}/finalize").url_name == "files-finalize"
    assert resolve(f"/v1/files/{fid}/url").url_name == "files-presign-url"
    assert resolve("/v1/files/raw/abc").url_name == "files-raw"

    # Positive control for the five assertions above: resolve() must be CAPABLE of
    # failing here, otherwise "it resolved" proves nothing.
    with pytest.raises(Resolver404):
        resolve("/v1/files-definitely-not-mounted")


def test_raw_is_matched_before_the_catch_all_id_route():
    """`v1/files/<str:file_id>` would swallow `v1/files/raw/<token>` if the patterns were
    ordered the other way, and the symptom would be a confusing 400 'invalid file id'."""
    from django.urls import resolve
    match = resolve("/v1/files/raw/sometoken")
    assert match.url_name == "files-raw"
    assert match.kwargs == {"token": "sometoken"}
    # Control: the id route still matches a bare id, so the ordering did not break it.
    fid = uuid.uuid4()
    assert resolve(f"/v1/files/{fid}").kwargs == {"file_id": str(fid)}


# ================================================================= the happy path

def test_create_returns_201_with_the_presigned_bundle(owner_conn, client):
    """Returns 201 Created, not 200, with the documented public body shape."""
    p = _seed_peer(owner_conn, "create")
    r = _create(client, p["peer_id"], size=11)
    assert r.status_code == 201, r.content
    body = r.json()
    assert set(body) == {"id", "upload_url", "upload_headers", "expires_at"}
    uuid.UUID(body["id"])                       # a bare uuid string, not {"0": ...}
    assert body["upload_url"].startswith(f"{PUBLIC_BASE_URL}/v1/files/raw/")
    # Upload headers are an array of pairs, not an object.
    assert body["upload_headers"] == [["Content-Type", "text/plain"]]
    assert body["expires_at"].endswith("Z")

    # The row lands as `pending` with sha/size still NULL.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT owner_id, status, sha256, size_bytes, r2_key "
                    "FROM file_artifacts WHERE id=%s", (body["id"],))
        row = cur.fetchone()
    assert row is not None, "create replied 201 but wrote no row"
    assert row[1] == "pending" and row[2] is None and row[3] is None
    assert str(row[0]) == str(p["owner_id"]), (
        "owner_id must come from the token's peer row, never from the request body")
    assert row[4] == f"{p['owner_id']}/{body['id']}"    # The storage key combines owner and file IDs.


def test_upload_then_finalize_then_metadata(owner_conn, client):
    """The full three-step flow, end to end over real HTTP and a real filesystem."""
    p = _seed_peer(owner_conn, "flow")
    payload = b"hello meshychat"
    fid, view, sha = _upload_and_finalize(client, p["peer_id"], payload)

    assert view["status"] == "ready"
    assert view["sha256"] == sha
    assert view["size_bytes"] == len(payload)
    assert view["finalized_at"] is not None
    assert view["owner_id"] == str(p["owner_id"])
    assert view["uploaded_by_peer_id"] == str(p["peer_id"])
    # Internal-only fields are excluded from the public view.
    assert "r2_key" not in view and "grace_until" not in view
    # Field declaration order is part of the wire format.
    assert list(view) == ["id", "owner_id", "uploaded_by_peer_id", "sha256", "size_bytes",
                          "content_type", "filename", "status", "created_at",
                          "finalized_at"]

    r = client.get(f"/v1/files/{fid}", **_auth(p["peer_id"]))
    assert r.status_code == 200, r.content
    assert r.json() == view


# ============================================ owner vs stranger, both directions

def test_an_artifact_is_readable_by_its_owner_and_not_by_another_peer(owner_conn, client):
    """THE CORE ACCESS TEST, with the control in BOTH directions.

    Refusing the stranger proves nothing on its own: an unseeded peer, a bad token or a
    broken fixture all produce 404 too. So the same test shows the stranger's token
    working perfectly on the stranger's OWN artifact.
    """
    owner = _seed_peer(owner_conn, "own")
    stranger = _seed_peer(owner_conn, "str")

    owner_file, _view, _sha = _upload_and_finalize(client, owner["peer_id"], b"mine")
    stranger_file, _v2, _s2 = _upload_and_finalize(client, stranger["peer_id"], b"theirs")

    # POSITIVE: the owner reads their own.
    r = client.get(f"/v1/files/{owner_file}", **_auth(owner["peer_id"]))
    assert r.status_code == 200, r.content

    # POSITIVE CONTROL FOR THE STRANGER'S CREDENTIALS: the same token, same route,
    # their own artifact -> 200. Without this line the negative below is unreadable.
    r = client.get(f"/v1/files/{stranger_file}", **_auth(stranger["peer_id"]))
    assert r.status_code == 200, r.content

    # NEGATIVE: the stranger cannot read the owner's artifact.
    r = client.get(f"/v1/files/{owner_file}", **_auth(stranger["peer_id"]))
    assert r.status_code == 404, r.content
    # The 404 with the FILE code, not 403, does not confirm existence.
    assert r.json() == {"error": "E_FILE_NOT_FOUND", "message": "file not found"}

    # And the symmetric direction, so the result is not an artefact of who was seeded
    # first.
    r = client.get(f"/v1/files/{stranger_file}", **_auth(owner["peer_id"]))
    assert r.status_code == 404, r.content

    # The download URL applies the same rule.
    assert client.get(f"/v1/files/{owner_file}/url",
                      **_auth(owner["peer_id"])).status_code == 200
    assert client.get(f"/v1/files/{owner_file}/url",
                      **_auth(stranger["peer_id"])).status_code == 404


def test_any_peer_of_the_same_owner_can_read(owner_conn, client):
    """Access path 1 is same owner, not same peer. A second peer of
    the same owner reads the artifact; a peer of a different owner does not (control)."""
    a = _seed_peer(owner_conn, "sib-a")
    sibling_peer = uuid.uuid4()
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (sibling_peer, a["owner_id"], f"files-sib-{sibling_peer.hex[:10]}"))
    owner_conn.commit()
    other = _seed_peer(owner_conn, "sib-o")

    fid, _v, _s = _upload_and_finalize(client, a["peer_id"], b"shared within a tenant")

    r = client.get(f"/v1/files/{fid}", **_auth(sibling_peer))
    assert r.status_code == 200, r.content
    # Control: the rule really is OWNER scope, not "any authenticated peer".
    assert client.get(f"/v1/files/{fid}", **_auth(other["peer_id"])).status_code == 404


def test_a_call_participant_reads_across_owners(owner_conn, client):
    """Access path 2: a participant in a call that carried the file reads it across owners.
    The strongest control is that the `skill_calls` read really runs under `app.server_internal`.

    `skill_calls` FORCEs row-level security: under `meshychat_app` with no GUC the participant
    lookup returns ZERO ROWS and raises nothing, so this test would fail with a 404 that
    looks exactly like "not a participant". A bystander peer gets 404 in the same test,
    so a pass cannot be "everyone is allowed".
    """
    owner = _seed_peer(owner_conn, "part-o")
    participant = _seed_peer(owner_conn, "part-p")
    bystander = _seed_peer(owner_conn, "part-b")
    fid, _v, _s = _upload_and_finalize(client, owner["peer_id"], b"attached bytes")

    skill_id = _seed_skill(owner_conn, owner["peer_id"])
    _seed_call(owner_conn, caller=participant, target=owner, file_id=fid,
               status="completed", skill_id=skill_id)

    r = client.get(f"/v1/files/{fid}", **_auth(participant["peer_id"]))
    assert r.status_code == 200, (
        f"{r.content!r} - a 404 here usually means app.server_internal was not bound, "
        "so the FORCE-RLS skill_calls read silently returned no rows")

    # NEGATIVE, with the participant above as the positive control: a peer named on no
    # referencing call is still refused.
    assert client.get(f"/v1/files/{fid}",
                      **_auth(bystander["peer_id"])).status_code == 404


def test_a_participant_may_read_but_may_not_delete(owner_conn, client):
    """DELETE is owner-only, deliberately stricter than read."""
    owner = _seed_peer(owner_conn, "pd-o")
    participant = _seed_peer(owner_conn, "pd-p")
    fid, _v, _s = _upload_and_finalize(client, owner["peer_id"], b"delete me not")
    skill_id = _seed_skill(owner_conn, owner["peer_id"])
    _seed_call(owner_conn, caller=participant, target=owner, file_id=fid,
               status="completed", skill_id=skill_id)

    # POSITIVE: the participant can read it.
    assert client.get(f"/v1/files/{fid}",
                      **_auth(participant["peer_id"])).status_code == 200
    # NEGATIVE: the same peer, the same artifact, DELETE -> 404 (not 403).
    r = client.delete(f"/v1/files/{fid}", **_auth(participant["peer_id"]))
    assert r.status_code == 404, r.content
    # CONTROL that the row survived the refused delete, and that the owner can delete it.
    assert client.get(f"/v1/files/{fid}", **_auth(owner["peer_id"])).status_code == 200
    assert client.delete(f"/v1/files/{fid}",
                         **_auth(owner["peer_id"])).status_code == 204


def test_a_peer_that_only_received_the_artifact_as_a_result_may_read_it(owner_conn,
                                                                         client):
    """C17, path 2 through `result_attachments`.

    The target RETURNED its owner's artifact to a caller of another owner: the file is in
    the call's result_attachments and in nothing else. That caller may read it. Negative
    beside it: a bystander named on no call is refused, the same file, the same moment.
    Control on the fixture: the args array of that row is empty, so the read cannot have
    been authorised through the half the other tests already cover."""
    target = _seed_peer(owner_conn, "rcv-t")
    receiver = _seed_peer(owner_conn, "rcv-r")
    bystander = _seed_peer(owner_conn, "rcv-b")
    fid, _v, _s = _upload_and_finalize(client, target["peer_id"], b"a returned artifact")
    skill_id = _seed_skill(owner_conn, target["peer_id"])

    # NEGATIVE FIRST, before any call exists: the receiver is a stranger to the file.
    assert client.get(f"/v1/files/{fid}",
                      **_auth(receiver["peer_id"])).status_code == 404

    call_id = _seed_call(owner_conn, caller=receiver, target=target, file_id=fid,
                         status="completed", skill_id=skill_id, where="result")
    assert _attachments(owner_conn, call_id) == ([], [fid])

    r = client.get(f"/v1/files/{fid}", **_auth(receiver["peer_id"]))
    assert r.status_code == 200, r.content
    assert r.json()["id"] == fid
    assert client.get(f"/v1/files/{fid}/url",
                      **_auth(receiver["peer_id"])).status_code == 200
    assert client.get(f"/v1/files/{fid}",
                      **_auth(bystander["peer_id"])).status_code == 404
    # Receiving is not owning: the receiver's DELETE is the owner-only 404.
    assert client.delete(f"/v1/files/{fid}",
                         **_auth(receiver["peer_id"])).status_code == 404


def test_delete_is_refused_while_a_non_terminal_call_returned_the_file(owner_conn, client):
    """C17 through `result_attachments`: 409 E_FILE_IN_USE while a
    running call carries the file as a RESULT only. The positive control changes nothing
    but that call's status, and the same DELETE is then 204."""
    owner = _seed_peer(owner_conn, "rinuse-o")
    caller = _seed_peer(owner_conn, "rinuse-c")
    fid, _v, _s = _upload_and_finalize(client, owner["peer_id"], b"returned, in use")
    skill_id = _seed_skill(owner_conn, owner["peer_id"])
    call_id = _seed_call(owner_conn, caller=caller, target=owner, file_id=fid,
                         status="running", skill_id=skill_id, where="result")
    assert _attachments(owner_conn, call_id) == ([], [fid])

    r = client.delete(f"/v1/files/{fid}", **_auth(owner["peer_id"]))
    assert r.status_code == 409, r.content
    assert r.json() == {"error": "E_FILE_IN_USE",
                        "message": "file in use by non-terminal call(s)"}

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET status='completed' WHERE call_id=%s",
                    (call_id,))
    owner_conn.commit()
    assert client.delete(f"/v1/files/{fid}",
                         **_auth(owner["peer_id"])).status_code == 204


# ================================================== the nonexistent-id status code

def test_a_download_of_a_nonexistent_id_is_404_with_the_file_code(owner_conn, client):
    """404 E_FILE_NOT_FOUND, not E_NOT_FOUND.

    Controls: the SAME token on a real artifact returns 200 on both routes, so the 404
    is about the id and not about the credentials.
    """
    p = _seed_peer(owner_conn, "missing")
    real, _v, _s = _upload_and_finalize(client, p["peer_id"], b"real bytes")
    ghost = uuid.uuid4()

    assert client.get(f"/v1/files/{real}", **_auth(p["peer_id"])).status_code == 200
    assert client.get(f"/v1/files/{real}/url", **_auth(p["peer_id"])).status_code == 200

    r = client.get(f"/v1/files/{ghost}", **_auth(p["peer_id"]))
    assert r.status_code == 404, r.content
    assert r.json() == {"error": "E_FILE_NOT_FOUND", "message": "file not found"}

    r = client.get(f"/v1/files/{ghost}/url", **_auth(p["peer_id"]))
    assert r.status_code == 404, r.content
    assert r.json()["error"] == "E_FILE_NOT_FOUND"

    r = client.delete(f"/v1/files/{ghost}", **_auth(p["peer_id"]))
    assert r.status_code == 404, r.content
    assert r.json()["error"] == "E_FILE_NOT_FOUND"


def test_a_malformed_id_is_400_not_404(owner_conn, client):
    """A malformed UUID is rejected before the handler: a 400, not a 404. Saying "no such
    file" about an id that was never well-formed would be the worse answer."""
    p = _seed_peer(owner_conn, "badid")
    r = client.get("/v1/files/not-a-uuid", **_auth(p["peer_id"]))
    assert r.status_code == 400, r.content
    assert r.json()["error"] == "E_VALIDATION"
    # Control: the same route with a well-formed (but absent) id gives the OTHER answer.
    assert client.get(f"/v1/files/{uuid.uuid4()}",
                      **_auth(p["peer_id"])).status_code == 404


def test_the_files_routes_require_a_peer_token(owner_conn, client):
    """The router is mounted inside the authenticated peer group."""
    p = _seed_peer(owner_conn, "noauth")
    fid, _v, _s = _upload_and_finalize(client, p["peer_id"], b"secret")

    r = client.get(f"/v1/files/{fid}")                    # no Authorization header
    assert r.status_code == 401, r.content
    assert r.json()["error"] == "E_AUTH_MISSING"
    # Control: the identical request WITH the token succeeds, so 401 is about the header.
    assert client.get(f"/v1/files/{fid}", **_auth(p["peer_id"])).status_code == 200


# ========================================================= size and type limits

def test_the_declared_size_limit_refuses_the_presign_up_front(owner_conn, client,
                                                             monkeypatch):
    """The declared size is checked before the row and before the presign, refusing the
    presigned URL up front rather than after PUT. 413 E_FILE_SIZE_EXCEEDED."""
    monkeypatch.setenv("MESHYCHAT_MAX_FILE_BYTES", "64")
    _reactivate()
    p = _seed_peer(owner_conn, "size")

    # POSITIVE CONTROL FIRST: exactly at the cap is allowed (the comparison is `>`).
    ok = _create(client, p["peer_id"], size=64)
    assert ok.status_code == 201, ok.content

    r = _create(client, p["peer_id"], size=65)
    assert r.status_code == 413, r.content
    body = r.json()
    assert body["error"] == "E_FILE_SIZE_EXCEEDED"
    assert body["message"] == "size exceeded: declared=65, max=64"

    # And NO row was written for the refused request: the check precedes the INSERT.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM file_artifacts WHERE owner_id=%s",
                    (p["owner_id"],))
        assert cur.fetchone()[0] == 1, "the refused create still inserted a pending row"


def test_the_upload_token_refuses_more_bytes_than_it_committed_to(owner_conn, client):
    """The presign signs `max_size_bytes`, so the FS route enforces the declared limit."""
    p = _seed_peer(owner_conn, "rawsize")
    payload = b"0123456789"
    r = _create(client, p["peer_id"], size=len(payload))
    assert r.status_code == 201, r.content
    path = _token_path(r.json()["upload_url"])

    over = client.put(path, data=payload + b"X", content_type="text/plain")
    assert over.status_code == 413, over.content
    assert over.json()["error"] == "E_FILE_SIZE_EXCEEDED"

    # POSITIVE CONTROL: the same token, the committed number of bytes -> 200. Without it
    # the 413 could be "the token was never valid".
    exact = client.put(path, data=payload, content_type="text/plain")
    assert exact.status_code == 200, exact.content


def test_content_type_is_required_and_the_declared_size_must_be_a_number(owner_conn,
                                                                        client):
    """The request requires `content_type` and a nonnegative integer `size_bytes_declared`.
    Django has no declarative body validation, so the handler checks the shape itself; each
    refusal is asserted here."""
    p = _seed_peer(owner_conn, "shape")
    auth = _auth(p["peer_id"])

    def post(body):
        return client.post("/v1/files", data=json.dumps(body),
                           content_type="application/json", **auth)

    # POSITIVE CONTROL FIRST: the well-formed body is accepted.
    assert post({"filename": None, "content_type": "application/pdf",
                 "size_bytes_declared": 3}).status_code == 201

    assert post({"size_bytes_declared": 3}).status_code == 400            # no type
    assert post({"content_type": "text/plain"}).status_code == 400        # no size
    assert post({"content_type": "text/plain",
                 "size_bytes_declared": -1}).status_code == 400           # negative
    assert post({"content_type": "text/plain",
                 "size_bytes_declared": "3"}).status_code == 400          # not a number
    assert post({"content_type": "", "size_bytes_declared": 3}).status_code == 400

    # The declared content_type is stored verbatim and echoed back - it is NOT sniffed
    # or normalised anywhere: the DB row is authoritative.
    r = post({"content_type": "application/pdf", "size_bytes_declared": 3})
    fid = r.json()["id"]
    meta = client.get(f"/v1/files/{fid}", **auth).json()
    assert meta["content_type"] == "application/pdf"


# ============================================================ finalize behaviour

def test_finalize_before_the_bytes_land_is_409_not_ready(owner_conn, client):
    """A missing object at finalize time is a normal client race,
    mapped to 409 E_FILE_NOT_READY, not a 404 and not a 500."""
    p = _seed_peer(owner_conn, "early")
    r = _create(client, p["peer_id"], size=4)
    fid = r.json()["id"]
    sha = hashlib.sha256(b"abcd").hexdigest()

    early = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                        content_type="application/json", **_auth(p["peer_id"]))
    assert early.status_code == 409, early.content
    assert early.json() == {"error": "E_FILE_NOT_READY",
                            "message": "file bytes not uploaded yet"}

    # POSITIVE CONTROL: PUT the bytes, and the identical finalize now succeeds.
    assert client.put(_token_path(r.json()["upload_url"]), data=b"abcd",
                      content_type="text/plain").status_code == 200
    late = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                       content_type="application/json", **_auth(p["peer_id"]))
    assert late.status_code == 200, late.content


def test_a_wrong_hash_is_422_and_marks_the_row_failed(owner_conn, client, files_env):
    """On mismatch the row flips to `failed` and
    the object is deleted - the artifact is not left recoverable by a second guess."""
    p = _seed_peer(owner_conn, "hash")
    payload = b"the real bytes"

    # POSITIVE CONTROL FIRST: the correct digest over the same bytes succeeds.
    good_id, _v, _s = _upload_and_finalize(client, p["peer_id"], payload)
    assert _v["status"] == "ready"

    r = _create(client, p["peer_id"], size=len(payload))
    fid = r.json()["id"]
    assert client.put(_token_path(r.json()["upload_url"]), data=payload,
                      content_type="text/plain").status_code == 200
    wrong = hashlib.sha256(b"different bytes").hexdigest()
    bad = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": wrong}),
                      content_type="application/json", **_auth(p["peer_id"]))
    assert bad.status_code == 422, bad.content
    assert bad.json() == {"error": "E_FILE_HASH_MISMATCH", "message": "hash mismatch"}

    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM file_artifacts WHERE id=%s", (fid,))
        assert cur.fetchone()[0] == "failed"
    assert not (files_env / str(p["owner_id"]) / fid).exists(), \
        "the object survived a hash mismatch"
    # And the control artifact's object is still there, so the delete was targeted.
    assert (files_env / str(p["owner_id"]) / good_id).exists()


def test_finalize_is_refused_across_owners(owner_conn, client):
    """The route checks the owner before any blob work. The service's
    own finalize does not, so losing this check would make finalize cross-tenant."""
    owner = _seed_peer(owner_conn, "fin-o")
    stranger = _seed_peer(owner_conn, "fin-s")
    payload = b"not yours"
    r = _create(client, owner["peer_id"], size=len(payload))
    fid = r.json()["id"]
    assert client.put(_token_path(r.json()["upload_url"]), data=payload,
                      content_type="text/plain").status_code == 200
    sha = hashlib.sha256(payload).hexdigest()

    bad = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                      content_type="application/json", **_auth(stranger["peer_id"]))
    assert bad.status_code == 404, bad.content
    # Use the generic not-found code because what is
    # missing is an artifact of the CALLER's, not "a file".
    assert bad.json()["error"] == "E_NOT_FOUND"

    # POSITIVE CONTROL: the owner's identical finalize succeeds, so the 404 was about
    # the caller and not about the artifact or the digest.
    ok = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                     content_type="application/json", **_auth(owner["peer_id"]))
    assert ok.status_code == 200, ok.content


def test_a_second_sequential_finalize_is_409_but_a_lost_race_is_idempotent(
        owner_conn, app_conn, client, monkeypatch):
    """Two DIFFERENT paths that are easy to conflate, and getting either wrong is a
    behaviour change.

    SEQUENTIAL second call: the pre-fetch sees `ready` and returns
    `NotReady("ready")` -> 409. It is NOT 200. (This is the assertion an earlier draft of
    this test got backwards, which is why the distinction is spelled out here.)

    LOST RACE: two finalizes overlap, both pre-fetch `pending`, and the loser's
    `mark_ready` matches zero rows. It then re-reads and returns the
    winner's row as an idempotent 200 - "the winner verified the same SHA over the same
    bytes" - rather than the misleading 404 the raw miss would produce. Simulated by
    making `mark_ready` do the winner's update and then report the loss, which is
    precisely what the losing transaction observes.
    """
    p = _seed_peer(owner_conn, "idem")
    payload = b"once and again"
    fid, first, sha = _upload_and_finalize(client, p["peer_id"], payload)

    second = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                         content_type="application/json", **_auth(p["peer_id"]))
    assert second.status_code == 409, second.content
    assert second.json() == {"error": "E_FILE_NOT_READY",
                             "message": "file not ready: status=ready"}

    # The race half, driven at the service layer where the branch lives.
    payload2 = b"raced bytes"
    r = _create(client, p["peer_id"], size=len(payload2))
    raced = r.json()["id"]
    assert client.put(_token_path(r.json()["upload_url"]), data=payload2,
                      content_type="text/plain").status_code == 200

    real_mark_ready = repo.mark_ready

    def lose_the_race(conn, file_id, sha256, size_bytes):
        real_mark_ready(conn, file_id, sha256, size_bytes)   # the winner commits
        return False                                        # ...and we lost

    cfg = cfgmod.build_blob_config()
    svc = service.FileService(app_conn, blobmod.from_config(cfg), cfg)
    monkeypatch.setattr(repo, "mark_ready", lose_the_race)
    row = svc.finalize(uuid.UUID(raced), hashlib.sha256(payload2).hexdigest())
    assert row.status == "ready" and row.sha256 == hashlib.sha256(payload2).hexdigest()

    # CONTROL that the simulation is the LOSS and not a no-op: with mark_ready restored
    # the same call on the same (now ready) row takes the 409 path instead.
    monkeypatch.undo()
    with pytest.raises(ferrors.ApiError) as exc:
        svc.finalize(uuid.UUID(raced), hashlib.sha256(payload2).hexdigest())
    assert exc.value.status == 409 and exc.value.code == "E_FILE_NOT_READY"


def test_a_pending_artifact_has_no_download_url(owner_conn, client):
    """Only `ready` presigns; `pending` is 409 with the observed
    status in the message so a client can wait rather than retry blindly."""
    p = _seed_peer(owner_conn, "pending")
    r = _create(client, p["peer_id"], size=4)
    fid = r.json()["id"]

    early = client.get(f"/v1/files/{fid}/url", **_auth(p["peer_id"]))
    assert early.status_code == 409, early.content
    assert early.json() == {"error": "E_FILE_NOT_READY",
                            "message": "file not ready: status=pending"}

    # ...but metadata is readable while pending, which is the control
    # that the 409 is a STATUS guard and not an access failure.
    meta = client.get(f"/v1/files/{fid}", **_auth(p["peer_id"]))
    assert meta.status_code == 200 and meta.json()["status"] == "pending"

    # POSITIVE CONTROL: finish the upload and the same URL route answers 200.
    assert client.put(_token_path(r.json()["upload_url"]), data=b"abcd",
                      content_type="text/plain").status_code == 200
    client.post(f"/v1/files/{fid}/finalize",
                data=json.dumps({"sha256": hashlib.sha256(b"abcd").hexdigest()}),
                content_type="application/json", **_auth(p["peer_id"]))
    assert client.get(f"/v1/files/{fid}/url", **_auth(p["peer_id"])).status_code == 200


# ========================================================== the download round trip

def test_the_presigned_download_returns_the_exact_bytes(owner_conn, client):
    """The GET half of the FS storage route."""
    p = _seed_peer(owner_conn, "dl")
    payload = b"\x00\x01binary\xff bytes"
    fid, _v, _s = _upload_and_finalize(client, p["peer_id"], payload,
                                       content_type="application/octet-stream")
    presign = client.get(f"/v1/files/{fid}/url", **_auth(p["peer_id"]))
    assert presign.status_code == 200, presign.content
    assert set(presign.json()) == {"url", "expires_at"}

    got = client.get(_token_path(presign.json()["url"]))
    assert got.status_code == 200, got.content
    assert got.content == payload
    # A Content-Type is always declared; nosniff only protects a
    # type that exists. The real type lives in the DB and is not reachable from a token.
    assert got["Content-Type"] == "application/octet-stream"
    assert got["Content-Length"] == str(len(payload))


def test_a_tampered_or_expired_token_is_401(owner_conn, client, monkeypatch):
    """Any token failure is 401 and leaks nothing about which one."""
    p = _seed_peer(owner_conn, "tok")
    fid, _v, _s = _upload_and_finalize(client, p["peer_id"], b"token bytes")
    presign = client.get(f"/v1/files/{fid}/url", **_auth(p["peer_id"])).json()
    path = _token_path(presign["url"])

    # POSITIVE CONTROL FIRST: the untouched token works.
    assert client.get(path).status_code == 200

    # Tamper INSIDE the token, not at its last character: base64url with no padding
    # leaves unused bits in the final character, so flipping it can decode to the very
    # same bytes and the HMAC still verifies. An earlier draft did exactly that and the
    # "tampered" token returned 200. The mutated character here is in the payload.
    prefix = "/v1/files/raw/"
    token = path[len(prefix):]
    tampered = prefix + token[:4] + ("A" if token[4] != "A" else "B") + token[5:]
    assert tampered != path
    assert client.get(tampered).status_code == 401, (
        "a token whose payload bytes were altered still verified")

    # A GET token presented to the PUT verb: the operation is signed in.
    assert client.put(path, data=b"x", content_type="text/plain").status_code == 401

    # Expiry: the same key, signed with the same secret, ten seconds in the past. (A
    # negative MESHYCHAT_PRESIGN_TTL_SECS did this before B11; the startup check now refuses
    # one.) Control: the same token one minute in the future.
    key = f"{p['owner_id']}/{fid}"
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())

    def signed(exp_unix):
        return prefix + blobmod.sign_token(bytes.fromhex(TOKEN_SECRET_HEX),
                                           blobmod.TokenPayload(key=key,
                                                                op=blobmod.TOKEN_OP_GET,
                                                                exp_unix=exp_unix))
    assert client.get(signed(now + 60)).status_code == 200
    r = client.get(signed(now - 10))
    assert r.status_code == 401, r.content
    assert r.json()["error"] == "E_AUTH_INVALID"


def test_the_token_payload_matches_the_canonical_wire_format():
    """The wire format is canonical: do not change it without bumping a version field.

    `max_size_bytes` is skipped, not null, when absent. A null would change the signed bytes,
    so tokens would be rejected by a verifier of this format even though this service's signer
    and verifier would still agree with each other. Hence the literal byte pins below.
    """
    secret = bytes.fromhex(TOKEN_SECRET_HEX)
    put = blobmod.sign_token(secret, blobmod.TokenPayload(
        key="o/f", op=blobmod.TOKEN_OP_PUT, exp_unix=1, max_size_bytes=99))
    get = blobmod.sign_token(secret, blobmod.TokenPayload(
        key="o/f", op=blobmod.TOKEN_OP_GET, exp_unix=1, max_size_bytes=None))

    put_bytes = P.b64u_decode(put)[:-blobmod.TAG_LEN]
    get_bytes = P.b64u_decode(get)[:-blobmod.TAG_LEN]
    # RFC 8785: keys lexicographic, no whitespace.
    assert put_bytes == b'{"exp_unix":1,"key":"o/f","max_size_bytes":99,"op":"PUT"}'
    assert get_bytes == b'{"exp_unix":1,"key":"o/f","op":"GET"}'
    assert b"max_size_bytes" not in get_bytes

    assert blobmod.verify_token(secret, put).max_size_bytes == 99
    assert blobmod.verify_token(secret, get).max_size_bytes is None
    # NEGATIVE, with the two round trips above as the control: a different key does not
    # verify, so `verify_token` is checking the HMAC and not merely decoding.
    with pytest.raises(blobmod.BlobError):
        blobmod.verify_token(b"\x00" * 32, put)


def test_the_fs_presigns_sign_exactly_the_wire_payload(files_env, monkeypatch):
    """Both presigns are pinned at once on a frozen clock: the PUT signs its
    size and asks for the Content-Type header, the GET signs no size and asks for no
    header at all. Same URL shape for both. A key the store
    refuses is refused BEFORE anything is signed or coerced."""
    store = blobmod.FsBlobStore(cfgmod.FsConfig(str(files_env), TOKEN_SECRET_HEX,
                                                PUBLIC_BASE_URL + "/"))
    monkeypatch.setattr(blobmod, "_now_unix", lambda: 1_000)
    secret = bytes.fromhex(TOKEN_SECRET_HEX)
    expires = _dt.datetime.fromtimestamp(1_060, _dt.timezone.utc)

    put = store.presign_put("o/f", "text/plain", "99", "60")
    put_token = blobmod.sign_token(secret, blobmod.TokenPayload(
        key="o/f", op=blobmod.TOKEN_OP_PUT, exp_unix=1_060, max_size_bytes=99))
    assert put == blobmod.PresignedUrl(url=f"{PUBLIC_BASE_URL}/v1/files/raw/{put_token}",
                                       headers=[("Content-Type", "text/plain")],
                                       expires_at=expires)
    assert P.b64u_decode(put_token)[:-blobmod.TAG_LEN] == (
        b'{"exp_unix":1060,"key":"o/f","max_size_bytes":99,"op":"PUT"}')

    get = store.presign_get("o/f", 60)
    get_token = blobmod.sign_token(secret, blobmod.TokenPayload(
        key="o/f", op=blobmod.TOKEN_OP_GET, exp_unix=1_060))
    assert get == blobmod.PresignedUrl(url=f"{PUBLIC_BASE_URL}/v1/files/raw/{get_token}",
                                       headers=[], expires_at=expires)
    assert P.b64u_decode(get_token)[:-blobmod.TAG_LEN] == (
        b'{"exp_unix":1060,"key":"o/f","op":"GET"}')

    monkeypatch.setattr(blobmod, "sign_token",
                        lambda *a: pytest.fail("signed a URL for a refused key"))
    # The key is checked before the size is coerced: a bad key wins over a bad size.
    for refuse in (lambda: store.presign_put("../x", "text/plain", "not-a-size", 60),
                   lambda: store.presign_get("../x", 60)):
        with pytest.raises(blobmod.BlobKeyRejected):
            refuse()


def test_the_raw_route_checks_expiry_on_the_clock_the_presign_stamps(owner_conn, client,
                                                                     monkeypatch):
    """`_checked_token` reads `blob._now_unix`, the function `FsBlobStore` stamps
    `exp_unix` with, so moving that one clock past the expiry expires the token at the
    route. Control first: the same token on the real clock is served."""
    p = _seed_peer(owner_conn, "clock")
    fid, _v, _s = _upload_and_finalize(client, p["peer_id"], b"clock bytes")
    path = _token_path(client.get(f"/v1/files/{fid}/url", **_auth(p["peer_id"])).json()["url"])
    assert client.get(path).status_code == 200

    monkeypatch.setattr(blobmod, "_now_unix", lambda: 2**40)
    r = client.get(path)
    assert r.status_code == 401, r.content
    assert r.json()["error"] == "E_AUTH_INVALID"


# ============================================================ delete + lifecycle

def test_delete_removes_the_row_and_the_object(owner_conn, client, files_env):
    """Returns 204 No Content, with no body at all."""
    p = _seed_peer(owner_conn, "del")
    fid, _v, _s = _upload_and_finalize(client, p["peer_id"], b"delete me")
    obj = files_env / str(p["owner_id"]) / fid
    assert obj.exists(), "setup failed: the object was never written"

    r = client.delete(f"/v1/files/{fid}", **_auth(p["peer_id"]))
    assert r.status_code == 204, r.content
    assert r.content == b""
    assert not obj.exists()
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM file_artifacts WHERE id=%s", (fid,))
        assert cur.fetchone()[0] == 0
    # Control: the route now behaves like any other absent id rather than 500-ing.
    assert client.get(f"/v1/files/{fid}", **_auth(p["peer_id"])).status_code == 404


def test_delete_is_refused_while_a_non_terminal_call_references_the_file(owner_conn,
                                                                        client):
    """Returns 409 E_FILE_IN_USE. The guard is an `EXISTS` under
    server-internal context; without the GUC it would see zero rows and FAIL OPEN,
    deleting a blob out from under an in-flight call."""
    owner = _seed_peer(owner_conn, "inuse-o")
    caller = _seed_peer(owner_conn, "inuse-c")
    fid, _v, _s = _upload_and_finalize(client, owner["peer_id"], b"in use")
    skill_id = _seed_skill(owner_conn, owner["peer_id"])
    call_id = _seed_call(owner_conn, caller=caller, target=owner, file_id=fid,
                         status="running", skill_id=skill_id)

    r = client.delete(f"/v1/files/{fid}", **_auth(owner["peer_id"]))
    assert r.status_code == 409, r.content
    assert r.json() == {"error": "E_FILE_IN_USE",
                        "message": "file in use by non-terminal call(s)"}

    # POSITIVE CONTROL: the ONLY thing that changes is the call's status. If the delete
    # still failed, the 409 above would have been about something else entirely; if it
    # had succeeded while the call was running, the guard was never firing.
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET status='completed' WHERE call_id=%s",
                    (call_id,))
    owner_conn.commit()
    assert client.delete(f"/v1/files/{fid}",
                         **_auth(owner["peer_id"])).status_code == 204


def test_the_grace_window_is_respected_rather_than_reinvented(owner_conn, client,
                                                              app_conn):
    """Spec 11.3.

    Three properties, each with its own control:
      * grace starts when the referencing call is terminal;
      * it does NOT start while another non-terminal call holds a reference;
      * it is ONE-SHOT - a second call cannot reset or extend a running window.
    """
    owner = _seed_peer(owner_conn, "grace-o")
    caller = _seed_peer(owner_conn, "grace-c")
    fid, _v, _s = _upload_and_finalize(client, owner["peer_id"], b"grace bytes")
    skill_id = _seed_skill(owner_conn, owner["peer_id"])

    running = _seed_call(owner_conn, caller=caller, target=owner, file_id=fid,
                         status="running", skill_id=skill_id)
    done = _seed_call(owner_conn, caller=caller, target=owner, file_id=fid,
                      status="completed", skill_id=skill_id)

    # NEGATIVE: the terminal call cannot start grace while `running` still references it.
    assert service.set_grace_until_for_terminal_call(app_conn, done, 3600) == 0
    with owner_conn.cursor() as cur:
        cur.execute("SELECT grace_until FROM file_artifacts WHERE id=%s", (fid,))
        assert cur.fetchone()[0] is None

    # POSITIVE CONTROL: the ONLY change is the other call reaching terminal.
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET status='timeout' WHERE call_id=%s", (running,))
    owner_conn.commit()
    assert service.set_grace_until_for_terminal_call(app_conn, done, 3600) == 1
    with owner_conn.cursor() as cur:
        cur.execute("SELECT grace_until FROM file_artifacts WHERE id=%s", (fid,))
        first = cur.fetchone()[0]
    assert first is not None

    # ONE-SHOT: a second run neither counts nor moves the deadline.
    assert service.set_grace_until_for_terminal_call(app_conn, done, 999999) == 0
    with owner_conn.cursor() as cur:
        cur.execute("SELECT grace_until FROM file_artifacts WHERE id=%s", (fid,))
        assert cur.fetchone()[0] == first

    # A call ID that does not exist is 0 started, not an exception.
    assert service.set_grace_until_for_terminal_call(app_conn, "no-such-call", 60) == 0


def test_the_pending_sweep_honours_the_upload_window(owner_conn, client, app_conn,
                                                     files_env):
    """Spec 11.1. A `pending` row inside its window is untouched;
    one past it becomes `failed` and its object is deleted."""
    p = _seed_peer(owner_conn, "sweep")
    fresh = _create(client, p["peer_id"], size=3).json()
    stale = _create(client, p["peer_id"], size=3).json()
    for created in (fresh, stale):
        assert client.put(_token_path(created["upload_url"]), data=b"abc",
                          content_type="text/plain").status_code == 200
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE file_artifacts SET created_at = NOW() - interval '2 hours' "
                    "WHERE id=%s", (stale["id"],))
    owner_conn.commit()

    cfg = cfgmod.build_blob_config()
    svc = service.FileService(app_conn, blobmod.from_config(cfg), cfg)
    report = svc.sweep_pending_expired(cfg.upload_window_secs)   # default 3600

    swept = {str(i) for i in report["swept_ids"]}
    assert stale["id"] in swept
    # CONTROL: the sweep is bounded by the window, not "everything pending".
    assert fresh["id"] not in swept
    with owner_conn.cursor() as cur:
        cur.execute("SELECT id, status FROM file_artifacts WHERE id IN (%s,%s)",
                    (fresh["id"], stale["id"]))
        by_id = {str(i): s for i, s in cur.fetchall()}
    assert by_id[stale["id"]] == "failed" and by_id[fresh["id"]] == "pending"
    assert not (files_env / str(p["owner_id"]) / stale["id"]).exists()
    assert (files_env / str(p["owner_id"]) / fresh["id"]).exists()


def test_the_grace_sweep_deletes_only_what_is_past_its_window(owner_conn, client,
                                                              app_conn):
    """Spec 11.2, including the `NOT EXISTS` re-check that spares a file
    a NEW non-terminal call re-referenced after grace started."""
    owner = _seed_peer(owner_conn, "gsweep-o")
    caller = _seed_peer(owner_conn, "gsweep-c")
    expired, _v1, _s1 = _upload_and_finalize(client, owner["peer_id"], b"expired")
    future, _v2, _s2 = _upload_and_finalize(client, owner["peer_id"], b"future")
    rereferenced, _v3, _s3 = _upload_and_finalize(client, owner["peer_id"], b"held")
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE file_artifacts SET grace_until = NOW() - interval '1 hour' "
                    "WHERE id IN (%s,%s)", (expired, rereferenced))
        cur.execute("UPDATE file_artifacts SET grace_until = NOW() + interval '1 hour' "
                    "WHERE id=%s", (future,))
    owner_conn.commit()
    skill_id = _seed_skill(owner_conn, owner["peer_id"])
    _seed_call(owner_conn, caller=caller, target=owner, file_id=rereferenced,
               status="queued", skill_id=skill_id)

    cfg = cfgmod.build_blob_config()
    svc = service.FileService(app_conn, blobmod.from_config(cfg), cfg)
    swept = {str(i) for i in svc.sweep_ready_grace_expired()["swept_ids"]}

    assert expired in swept
    # Two independent controls, so "it swept" is not "it swept everything":
    assert future not in swept, "a live grace window was swept"
    assert rereferenced not in swept, (
        "a file an in-flight call re-referenced was swept - the NOT EXISTS re-check is "
        "missing, or it ran without app.server_internal and saw no rows")


# ==================================================================== quota (402)

def test_finalize_is_refused_when_the_owner_is_over_the_file_bytes_cap(owner_conn,
                                                                      client):
    """Returns 402 PAYMENT_REQUIRED, E_QUOTA_EXCEEDED.

    The cap is set with an `owner_tier_overrides` row, which is how `resolve_cap` is
    meant to be steered - no monkeypatching of the code under test.
    """
    p = _seed_peer(owner_conn, "quota")
    payload = b"0123456789"          # 10 bytes
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO owner_tier_overrides (owner_id, capability, value) "
                    "VALUES (%s,'file_bytes',9)", (p["owner_id"],))
    owner_conn.commit()

    r = _create(client, p["peer_id"], size=len(payload))
    fid = r.json()["id"]
    assert client.put(_token_path(r.json()["upload_url"]), data=payload,
                      content_type="text/plain").status_code == 200
    sha = hashlib.sha256(payload).hexdigest()
    over = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                       content_type="application/json", **_auth(p["peer_id"]))
    assert over.status_code == 402, over.content
    assert over.json() == {
        "error": "E_QUOTA_EXCEEDED",
        "message": "quota exceeded for file_bytes: 10 would exceed cap 9"}

    # The refused reservation was refunded, not left charged.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT counter FROM owner_usage WHERE owner_id=%s AND "
                    "capability='file_bytes'", (p["owner_id"],))
        rec = cur.fetchone()
    assert rec is None or rec[0] == 0, f"usage was charged for a refused finalize: {rec}"

    # POSITIVE CONTROL: raise ONLY the cap and the identical finalize succeeds, so the
    # 402 was the quota and not the upload, the digest or the token.
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE owner_tier_overrides SET value=100 WHERE owner_id=%s "
                    "AND capability='file_bytes'", (p["owner_id"],))
    owner_conn.commit()
    ok = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                     content_type="application/json", **_auth(p["peer_id"]))
    assert ok.status_code == 200, ok.content
    with owner_conn.cursor() as cur:
        cur.execute("SELECT counter FROM owner_usage WHERE owner_id=%s AND "
                    "capability='file_bytes'", (p["owner_id"],))
        assert cur.fetchone()[0] == len(payload)


def test_an_unlimited_cap_counts_without_refusing(owner_conn, client):
    """`cap < 0` means unlimited: usage is still recorded, and the
    request is never refused. The seeded `enterprise` file_bytes value is -1."""
    p = _seed_peer(owner_conn, "unlim")
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE dashboard_users SET tier='enterprise' WHERE id=%s",
                    (p["owner_id"],))
    owner_conn.commit()
    payload = b"unlimited"
    fid, view, _s = _upload_and_finalize(client, p["peer_id"], payload)
    assert view["status"] == "ready"
    with owner_conn.cursor() as cur:
        cur.execute("SELECT counter FROM owner_usage WHERE owner_id=%s AND "
                    "capability='file_bytes'", (p["owner_id"],))
        assert cur.fetchone()[0] == len(payload), "unlimited must still COUNT"


# ============================================== typed absence + no hidden aggregates

def test_absence_is_typed_rather_than_an_empty_collection(owner_conn, app_conn, client):
    """House rule 3. A repository that answers `None`/`[]` makes "no such row", "not
    yours" and "the RLS context was never bound" the same value."""
    p = _seed_peer(owner_conn, "typed")
    fid, _v, _s = _upload_and_finalize(client, p["peer_id"], b"typed")

    missing = repo.get(app_conn, uuid.uuid4())
    assert isinstance(missing, repo.NotFound)
    assert missing is not None and missing != [] and missing != {}
    assert not missing                      # falsy, but not equal to any empty container
    assert missing.reason                   # it says WHY

    no_refs = repo.find_referencing_calls(app_conn, fid)
    assert isinstance(no_refs, repo.Empty), type(no_refs)
    assert not isinstance(no_refs, list), (
        "an empty list here is indistinguishable from an RLS-scoped read that saw "
        "nothing, which is the failure this type exists to name")

    # POSITIVE CONTROLS: the same two calls return real values for real data, so the
    # typed absences above are not simply what these functions always return.
    present = repo.get(app_conn, fid)
    assert isinstance(present, repo.FileRow) and str(present.id) == fid

    skill_id = _seed_skill(owner_conn, p["peer_id"])
    _seed_call(owner_conn, caller=p, target=p, file_id=fid, status="completed",
               skill_id=skill_id)
    refs = repo.find_referencing_calls(app_conn, fid)
    assert isinstance(refs, list) and len(refs) == 1
    assert str(refs[0].target_peer_id) == str(p["peer_id"])


def test_the_skill_calls_reads_refuse_to_run_without_server_internal(app_conn):
    """The positive control for the control. `assert_server_internal_is_bound` is what
    turns "zero referencing calls" from a silent wrong answer into a named failure, so it
    must itself be capable of firing."""
    with pytest.raises(RuntimeError, match="server_internal"):
        repo.assert_server_internal_is_bound(app_conn)      # no GUC bound here
    # CONTROL: inside the real context it passes, so the check is not simply always
    # raising.
    from apps.core import dbconn
    with dbconn.request_server_internal(app_conn) as c:
        repo.assert_server_internal_is_bound(c)


def test_every_skill_calls_access_carries_the_positive_control(app_conn, monkeypatch):
    """All five `skill_calls` accesses go through `repo._server_internal`, which binds the
    context AND asserts it took. Proven from the outside: with the binding replaced by a
    no-op, each one raises the named failure instead of reading zero rows. Before the
    helper each site spelled the pair by hand, and nothing caught one that dropped it."""
    from apps.core import dbconn
    fid = str(uuid.uuid4())
    accesses = {
        "has_non_terminal_reference": lambda: repo.has_non_terminal_reference(app_conn, fid),
        "find_referencing_calls": lambda: repo.find_referencing_calls(app_conn, fid),
        "set_grace_until": lambda: repo.set_grace_until(app_conn, fid, 60),
        "sweep_ready_past_grace": lambda: repo.sweep_ready_past_grace(app_conn),
        "attachments_of_call": lambda: repo.attachments_of_call(app_conn, "no-such-call"),
    }
    # CONTROL: with the real binding every access runs and answers.
    assert repo.has_non_terminal_reference(app_conn, fid) is False
    assert isinstance(repo.find_referencing_calls(app_conn, fid), repo.Empty)
    assert repo.set_grace_until(app_conn, fid, 60) is False
    assert isinstance(repo.attachments_of_call(app_conn, "no-such-call"), repo.NotFound)
    with repo._server_internal(app_conn) as c:
        repo.assert_server_internal_is_bound(c)

    monkeypatch.setattr(dbconn, "request_server_internal",
                        lambda conn: contextlib.nullcontext(conn))
    for name, call in accesses.items():
        with pytest.raises(RuntimeError, match="server_internal"):
            call()
            pytest.fail(f"{name} read skill_calls with no server_internal binding")


# ============================== the referencing-calls lookup past FORCE RLS (divergence 095, sql_meshychat/041)

#: What find_referencing_calls ran before 095: the instrument's control below.
_PLAIN_REFERENCING_SQL = ("SELECT id, status, caller_peer_id, target_peer_id FROM skill_calls "
                          " WHERE args_attachments @> ARRAY[%s]::uuid[] OR result_attachments @> ARRAY[%s]::uuid[] LIMIT 100")


def _plan_nodes(app_conn, sql, params):
    """The plan's node types for `sql` exactly as a request runs it: meshychat_app, server_internal bound, seq scans OFF (on a
    small database Postgres rightly prefers a scan, so 'off' is what separates 'cannot use an index' from 'chose not to')."""
    from apps.core import dbconn
    with dbconn.request_server_internal(app_conn) as c:
        with c.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("EXPLAIN (FORMAT JSON) " + sql, params)
            plan = cur.fetchone()[0][0]["Plan"]
    nodes = []

    def walk(n):
        nodes.append((n.get("Node Type"), n.get("Relation Name")))
        for child in n.get("Plans", []) or []:
            walk(child)
    walk(plan)
    return nodes


def test_the_referencing_lookup_cannot_scan_skill_calls_under_rls(app_conn):
    """THE DEFECT, pinned (production 2026-10-10: every non-owner presign timed out). Under FORCE RLS a non-LEAKPROOF `@>`
    cannot be an index condition ahead of the policy, so the plain query scans skill_calls whatever the planner settings.
    CONTROL: that plain query, run as a request runs it, IS a seq scan of skill_calls here, so the instrument can see one.
    THE CHECK: what find_referencing_calls runs now (REFERENCING_CALLS_SQL) is no scan of skill_calls."""
    fid = uuid.uuid4()
    control = _plan_nodes(app_conn, _PLAIN_REFERENCING_SQL, (fid, fid))
    assert ("Seq Scan", "skill_calls") in control, control
    nodes = _plan_nodes(app_conn, repo.REFERENCING_CALLS_SQL, (fid,))
    assert ("Seq Scan", "skill_calls") not in nodes, nodes
    assert any(t == "Function Scan" for t, _ in nodes), nodes


def test_the_definer_function_body_uses_both_gin_indexes(owner_conn):
    """Inside the function the policy no longer applies (it runs as its owner), so the GIN indexes are usable. The body,
    EXPLAINed as the owner with seq scans off, must reach skill_calls only through an index: a body edited back into one
    OR'd predicate or a dropped index would show here."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT prosrc FROM pg_proc WHERE proname = 'file_referencing_calls'")
        body = cur.fetchone()[0].strip().rstrip(";")
        cur.execute("SET enable_seqscan = off")
        cur.execute("PREPARE frc_body(uuid) AS " + body.replace("p_file_id", "$1"))
        cur.execute(f"EXPLAIN (FORMAT JSON) EXECUTE frc_body('{uuid.uuid4()}')")   # a utility statement: no bound parameter
        plan = cur.fetchone()[0][0]["Plan"]
        cur.execute("DEALLOCATE frc_body")
        cur.execute("RESET enable_seqscan")
    owner_conn.rollback()
    found = []

    def walk(n):
        found.append((n.get("Node Type"), n.get("Relation Name"), n.get("Index Name")))
        for child in n.get("Plans", []) or []:
            walk(child)
    walk(plan)
    assert not any(t == "Seq Scan" for t, _, _ in found), found
    used = {i for _, _, i in found if i}
    assert used == {"skill_calls_args_attachments_gin", "skill_calls_result_attachments_gin"}, used
    # STRUCTURAL, and labelled so: one branch per index joined by UNION, the cap outside. As the owner with seq scans off an
    # OR'd body also reaches both indexes (a BitmapOr), so the plan above cannot tell the two apart. They differ under a
    # GENERIC plan at production's size: measured on the 2026-10-10 production copy, the OR form with LIMIT 100 took a seq
    # scan (36.7 s) where the UNION form stayed on both indexes (66 ms). A database this small cannot reproduce that, so
    # the form is pinned directly.
    flat = " ".join(body.upper().split())
    assert " UNION " in flat and " OR " not in flat and flat.rstrip().endswith("LIMIT 100"), flat


def test_the_definer_function_is_narrow(owner_conn):
    """The policy does not filter inside a SECURITY DEFINER function, so its definition IS the boundary (memory-bxxxxx's
    review, 2026-10-10): definer, STRICT, one uuid in, the four columns readable_row needs out, search_path pinned, and
    EXECUTE for meshychat_app only - never PUBLIC, which Postgres grants by default."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT prosecdef, proisstrict, pronargs, proargnames, proconfig, proacl::text[] "
                    "FROM pg_proc WHERE proname = 'file_referencing_calls'")
        rows = cur.fetchall()
    owner_conn.rollback()
    assert len(rows) == 1, rows
    secdef, strict, nargs, argnames, config, acl = rows[0]
    assert secdef and strict and nargs == 1
    assert list(argnames) == ["p_file_id", "id", "status", "caller_peer_id", "target_peer_id"], argnames
    # pg_temp named LAST, so a caller's temporary table cannot shadow a name (divergence 096, sql_meshychat/042).
    assert config == ["search_path=pg_catalog, public, pg_temp"], config
    assert acl is not None, "a NULL acl means the default, which includes EXECUTE for PUBLIC"
    assert not any(entry.startswith("=") for entry in acl), acl      # "=X/owner" is PUBLIC's entry
    assert any(entry.startswith("meshychat_app=X") for entry in acl), acl
    # Its speed rests on its owner reading past RLS: an owner without bypassrls would be back to the policy's seq scan.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT r.rolbypassrls OR r.rolsuper FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    "WHERE p.proname = 'file_referencing_calls'")
        assert cur.fetchone()[0] is True, "the function's owner must bypass RLS"
    owner_conn.rollback()


def test_the_definer_function_answers_for_one_file_only(owner_conn, app_conn, client):
    """It reads past RLS, so it is its own boundary. As meshychat_app WITHOUT the server_internal binding it answers nothing at
    all (the skipped policy's predicate is kept in the body; memory-bxxxxx's review of #214). Inside the binding it
    returns the call referencing this file (in either array), nothing for another file, and exactly four columns."""
    from apps.core import dbconn
    p = _seed_peer(owner_conn, "frc")
    fid_a, _v, _s = _upload_and_finalize(client, p["peer_id"], b"frc-a")
    fid_b, _v, _s = _upload_and_finalize(client, p["peer_id"], b"frc-b")
    skill_id = _seed_skill(owner_conn, p["peer_id"])
    call_a = _seed_call(owner_conn, caller=p, target=p, file_id=fid_a, status="completed", skill_id=skill_id, where="args")
    call_b = _seed_call(owner_conn, caller=p, target=p, file_id=fid_b, status="completed", skill_id=skill_id, where="result")
    with app_conn.cursor() as cur:                      # no binding: the guard answers nothing, even for a referenced file
        cur.execute("SELECT * FROM public.file_referencing_calls(%s)", (uuid.UUID(str(fid_a)),))
        assert cur.fetchall() == [], "outside server_internal the function must answer nothing"
    out = {}
    with dbconn.request_server_internal(app_conn) as c:
        with c.cursor() as cur:
            for name, fid in (("a", fid_a), ("b", fid_b), ("none", str(uuid.uuid4()))):
                cur.execute("SELECT * FROM public.file_referencing_calls(%s)", (uuid.UUID(str(fid)),))
                out[name] = cur.fetchall()
                cols = [d.name for d in cur.description]
                assert cols == ["id", "status", "caller_peer_id", "target_peer_id"], cols
    with owner_conn.cursor() as cur:
        cur.execute("SELECT call_id, id FROM skill_calls WHERE call_id IN (%s, %s)", (call_a, call_b))
        ids = dict(cur.fetchall())
    owner_conn.rollback()
    assert [r[0] for r in out["a"]] == [ids[call_a]]
    assert [r[0] for r in out["b"]] == [ids[call_b]]
    assert out["none"] == []
    # and the repository path over it agrees, typed absence included
    assert [str(r.call_id) for r in repo.find_referencing_calls(app_conn, fid_a)] == [str(ids[call_a])]
    assert isinstance(repo.find_referencing_calls(app_conn, str(uuid.uuid4())), repo.Empty)


class _Recorder:
    """Every statement a request's `apps.files.db` connection executes, with its
    parameters, tagged with the route that was running. Parameters are kept because the
    plan check below EXPLAINs the statement exactly as it ran."""

    def __init__(self):
        self.route = None
        self.rows: list[tuple[str, str, object]] = []

    def install(self, monkeypatch):
        import contextlib
        from apps.files import db as _db
        real = _db.connection
        rec = self

        class Cursor:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, query, params=None, **kw):
                rec.rows.append((rec.route, str(query), params))
                return self._inner.execute(query, params, **kw)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        class Conn:
            def __init__(self, inner):
                self._inner = inner

            def cursor(self, *a, **kw):
                return Cursor(self._inner.cursor(*a, **kw))

            def __getattr__(self, name):
                return getattr(self._inner, name)

        @contextlib.contextmanager
        def recording():
            with real() as conn:
                yield Conn(conn)

        monkeypatch.setattr(_db, "connection", recording)

    def of(self, route) -> list[tuple[str, object]]:
        return [(q, p) for r, q, p in self.rows if r == route]


#: The two tables house rule B5 is about: the ones that grow with every call and file.
BIG_TABLES = ("file_artifacts", "skill_calls")


#: Partial indexes whose PREDICATE bounds them however the table grows: in-flight calls
#: only, which finish or time out. A full walk of one of these is a bounded read (the
#: in-use guard's plan). A full walk of any other partial index is not: the grace-sweep
#: index holds every ready file in grace, the prune index every finished memory read.
BOUNDED_PARTIAL_INDEXES = frozenset({"skill_calls_owner_inflight_idx",
                                     "skill_calls_deadline_sweep_idx"})


def _unbounded_scans(conn, query: str, params) -> list[tuple[str, str]]:
    """Every (relation, how) where a statement's PLAN reads a relation without bounding
    the read, once sequential scans are made as expensive as the planner allows.

    Why `enable_seqscan = off`: on a small database Postgres correctly PREFERS a seq scan
    (docs/V1-DESIGN-RULES.md B5), so a plain EXPLAIN reports one for a well-indexed lookup
    and a check on it would fire on everything. With it off, the planner takes an index
    wherever one can serve the predicate, and what is left unbounded is the defect.

    An index scan (plain, index-only or bitmap) is BOUNDED only when
      * its Index Cond constrains the index's LEADING column (the index is entered at a
        key, not walked), or
      * it walks, with no usable condition, a partial index named in
        BOUNDED_PARTIAL_INDEXES.
    Everything else is a walk of the whole index - or of a partial index whose predicate
    grows with the table - and is reported: a Seq Scan; no Index Cond (measured: `WHERE
    id::text = $1` plans as a bitmap scan of the primary key with no condition); an Index
    Cond on a non-leading column only (review of c007c45: `status = ANY(all ten)` against
    (target_peer_id, status, deadline_at) read every row and passed); a full walk of any
    other partial index.

    NOT MEASURED: the selectivity of a leading-column condition. `created_at > '-infinity'`
    on the created_at index is entered at a key and reported bounded. The check finds the
    shapes no index can serve, not a range someone wrote to match everything.

    Planned on the seeding connection (RLS bypassed), so the plan is the statement's own
    and not a policy's `false` filter, which would plan to no scan at all and pass
    anything."""
    found: list[tuple[str, str]] = []
    leading: dict[str, str] = {}

    def leading_column(cur, index: str) -> str:
        if index not in leading:
            cur.execute("SELECT pg_get_indexdef(%s::regclass, 1, true)", (index,))
            leading[index] = cur.fetchone()[0]
        return leading[index]

    def constrains(cond: str, column: str) -> bool:
        # EXPLAIN prints an index condition with the index key on the left:
        # `(id = '...'::uuid)`, `((owner_id = ...) AND (status = ...))`, possibly
        # table-qualified.
        return re.search(r"\(\s*(?:\w+\.)?" + re.escape(column) + r"\s*(?:=|<|>|@>|IS\b)",
                         cond) is not None

    def walk(cur, node, rel):
        rel = node.get("Relation Name", rel)
        kind = node.get("Node Type")
        if kind == "Seq Scan":
            found.append((rel, "seq scan"))
        elif kind in ("Index Scan", "Index Only Scan", "Bitmap Index Scan"):
            index = node["Index Name"]
            cond = node.get("Index Cond")
            if cond is not None and constrains(cond, leading_column(cur, index)):
                pass
            elif cond is None and index in BOUNDED_PARTIAL_INDEXES:
                pass
            elif cond is None:
                found.append((rel, f"full scan of {index}"))
            else:
                found.append((rel, f"{index} not entered at its leading column: {cond}"))
        for child in node.get("Plans", []):
            walk(cur, child, rel)

    conn.commit()          # end any implicit transaction: SET LOCAL must die with ours
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("EXPLAIN (FORMAT JSON) " + query, params)
            plan = cur.fetchone()[0]
            if isinstance(plan, str):
                plan = json.loads(plan)
            walk(cur, plan[0]["Plan"], None)
    return found


def _big(scans) -> list[tuple[str, str]]:
    return [s for s in scans if s[0] in BIG_TABLES]


def test_the_plan_check_can_see_an_unbounded_scan(owner_conn):
    """THE INSTRUMENT'S CONTROL, both ways. `filename` has no index: a seq scan. A
    predicate on `id::text` has a WHERE clause and no index can serve it: the string test
    this replaced (C18) passed it, and the plan check must not. The primary-key lookup
    must come back clean, or the instrument is one that always fires."""
    assert _unbounded_scans(owner_conn, "SELECT id FROM file_artifacts WHERE filename = %s",
                            ("x",)) == [("file_artifacts", "seq scan")]
    unbounded = "SELECT id FROM file_artifacts WHERE id::text = %s"
    assert " where " in unbounded.lower()
    assert _big(_unbounded_scans(owner_conn, unbounded, ("x",))), \
        "a predicate no index serves must be reported"
    assert _unbounded_scans(owner_conn, "SELECT id FROM file_artifacts WHERE id = %s",
                            (uuid.uuid4(),)) == []


#: The terminal statuses and the full status domain (skill_calls_status_check), spelled out
#: so a predicate the CHECK makes always-true can be written below.
_TERMINAL = ("completed", "failed", "timeout", "abandoned")
_EVERY_STATUS = ("pending", "routed", "queued", "running", "input_required",
                 "auth_required") + _TERMINAL


def test_the_plan_check_sees_scans_that_an_index_does_not_bound(owner_conn):
    """THE INSTRUMENT'S SECOND CONTROL (review of c007c45, MEDIUM). An index in the plan is
    not a bound on the read. Three shapes an earlier version passed, each reading every
    row it matches on a table that only grows:

      * an Index Cond on a NON-LEADING column - `status` inside
        (target_peer_id, status, deadline_at) - over all ten statuses: the whole index;
      * a full walk of a PARTIAL index whose predicate grows without limit (every ready
        file in grace; every finished memory read);
      * the same non-leading Index Cond over the four terminal statuses.

    The first is the one that let the participant read's strict xfail XPASS with the read
    still unbounded. Positive controls beside them: the primary key, the leading column of
    the owner index, and the in-use guard's full walk of the IN-FLIGHT partial index - the
    one partial whose predicate is bounded, named in BOUNDED_PARTIAL_INDEXES."""
    fid = uuid.uuid4()
    unbounded = {
        "every-status predicate on a non-leading column":
            ("SELECT id FROM skill_calls WHERE status = ANY(%s) "
             "AND (%s = ANY(args_attachments) OR %s = ANY(result_attachments)) LIMIT 100",
             (list(_EVERY_STATUS), fid, fid)),
        "full walk of the grace-sweep partial index":
            ("SELECT id FROM file_artifacts WHERE status IN ('pending','ready') "
             "AND grace_until IS NOT NULL AND filename = %s", ("x",)),
        "terminal-status predicate on a non-leading column":
            ("SELECT id FROM skill_calls WHERE skill_name IN ('memory.query','memory.read') "
             "AND status = ANY(%s) AND %s = ANY(args_attachments)", (list(_TERMINAL), fid)),
    }
    for name, (query, params) in unbounded.items():
        assert _big(_unbounded_scans(owner_conn, query, params)), (
            f"{name}: reported bounded, and it reads every row it matches")

    bounded = {
        "primary key": ("SELECT id FROM file_artifacts WHERE id = %s", (fid,)),
        "leading column of the owner index":
            ("SELECT id FROM file_artifacts WHERE owner_id = %s AND status = 'ready'",
             (uuid.uuid4(),)),
        "the in-use guard (full walk of the in-flight partial index)":
            ("SELECT EXISTS(SELECT 1 FROM skill_calls WHERE status <> ALL(%s) "
             "AND (%s = ANY(args_attachments) OR %s = ANY(result_attachments)))",
             (list(_TERMINAL), fid, fid)),
    }
    for name, (query, params) in bounded.items():
        assert _unbounded_scans(owner_conn, query, params) == [], (
            f"{name}: reported unbounded - the instrument would fire on everything")

def test_no_request_path_issues_a_count_or_an_unbounded_scan(owner_conn, client,
                                                            monkeypatch):
    """House rule 4 / design rule B5, MEASURED on every route of the upload-read-delete
    cycle rather than promised.

    A `COUNT(*)` over a whole chain on every grant-bound authorize
    (`reconcile_chain_head`, ~490 GB/day of production disk reads:
    docs/V1-DESIGN-RULES.md B5), and Django's ORM makes that shape easy to write and hard
    to see. So every statement each route emits is recorded, and:

      * no aggregate runs on a request path;
      * every statement that reads or writes a big table is PLANNED (C18): nothing on it is
        read unbounded once `enable_seqscan = off`. The string test this replaced asserted
        `" where " in stmt`, which a predicate no index can serve also satisfies (the
        instrument's control, above, shows one);
      * the recorder is installed BEFORE the upload (C19). It used to be installed after,
        so create and finalize - the routes with the quota and the most SQL - emitted
        nothing it could see. Each route must now show statements, and the raw PUT, which
        opens no connection, must show none: the recorder is per-route, not a global
        "something was recorded".
    """
    p = _seed_peer(owner_conn, "sqlshape")
    rec = _Recorder()
    rec.install(monkeypatch)
    payload = b"measure me"

    rec.route = "create"
    r = _create(client, p["peer_id"], size=len(payload))
    assert r.status_code == 201, r.content
    created = r.json()
    rec.route = "put"
    assert client.put(_token_path(created["upload_url"]), data=payload,
                      content_type="text/plain").status_code == 200
    rec.route = "finalize"
    fin = client.post(f"/v1/files/{created['id']}/finalize",
                      data=json.dumps({"sha256": hashlib.sha256(payload).hexdigest()}),
                      content_type="application/json", **_auth(p["peer_id"]))
    assert fin.status_code == 200, fin.content
    fid = created["id"]
    rec.route = "metadata"
    assert client.get(f"/v1/files/{fid}", **_auth(p["peer_id"])).status_code == 200
    rec.route = "url"
    assert client.get(f"/v1/files/{fid}/url", **_auth(p["peer_id"])).status_code == 200
    rec.route = "delete"
    assert client.delete(f"/v1/files/{fid}", **_auth(p["peer_id"])).status_code == 204
    rec.route = None

    for route in ("create", "finalize", "metadata", "url", "delete"):
        assert rec.of(route), f"no SQL recorded for {route}: the recorder did not see it"
    assert rec.of("put") == [], "the raw PUT opens no connection; the recorder must agree"

    offenders = [(r, q) for r, q, _ in rec.rows
                 if any(a in q.lower() for a in ("count(", "sum(", "array_agg("))]
    assert not offenders, f"an aggregate ran on a request path: {offenders}"

    planned = []
    for route, query, params in rec.rows:
        low = " ".join(query.lower().split())
        if not any(t in low for t in BIG_TABLES):
            continue
        planned.append(route)
        scans = _big(_unbounded_scans(owner_conn, query, params))
        assert not scans, f"{route}: no index bounds this statement ({scans}): {query}"
    # Positive control on the loop itself: big-table statements from every route were
    # really planned, not skipped by the filter.
    assert set(planned) == {"create", "finalize", "metadata", "url", "delete"}, planned


class UnboundedScan(AssertionError):
    """The one failure the participant-read xfail below expects. Any other failure in that
    test - the 200, the control that the read reached skill_calls - is a real failure,
    not the recorded defect."""


def test_the_participant_read_is_bounded(owner_conn, client, monkeypatch):
    """The cross-owner read path, planned like the rest (C18). The owner path above never
    reaches it: an owner is answered before `skill_calls` is read."""
    owner = _seed_peer(owner_conn, "plan-o")
    participant = _seed_peer(owner_conn, "plan-p")
    fid, _v, _s = _upload_and_finalize(client, owner["peer_id"], b"read across owners")
    _seed_call(owner_conn, caller=participant, target=owner, file_id=fid,
               status="completed", skill_id=_seed_skill(owner_conn, owner["peer_id"]))
    rec = _Recorder()
    rec.install(monkeypatch)
    rec.route = "participant-read"
    assert client.get(f"/v1/files/{fid}",
                      **_auth(participant["peer_id"])).status_code == 200
    # Since divergence 095 the read goes through public.file_referencing_calls. This check plans every statement on
    # owner_conn, which uses the owner role (`meshy`, now `meshychat_owner`) and bypasses RLS, so it never saw the defect 095 fixes: the old OR query used the
    # GIN indexes there, while the request ran it as `meshy_app` (now `meshychat_app`) under FORCE RLS, where `@>` cannot use them (a seq scan; 27 to 32 s
    # on production's size). The bound that matters is now pinned as `meshychat_app` by
    # test_the_referencing_lookup_cannot_scan_skill_calls_under_rls; this test keeps its control that the read happens.
    calls = [(q, p) for q, p in rec.of("participant-read") if "skill_calls" in q or "file_referencing_calls" in q]
    assert calls, "control: the participant read reached the referencing-calls lookup"
    for query, params in calls:
        scans = _big(_unbounded_scans(owner_conn, query, params))
        if scans:
            raise UnboundedScan(f"no index bounds the participant read ({scans}): {query}")


def test_the_490_gb_figure_is_cited_to_its_measurement_and_not_to_this_guard():
    """C20. `has_non_terminal_reference` must cite the project's own measurement of
    ~490 GB/day of disk reads (docs/V1-DESIGN-RULES.md B5), the cost of
    `reconcile_chain_head`'s COUNT
    over a grant chain on every authorize - not of a count of the calls referencing a
    file. The docstring may keep the figure only beside what it measured and where.

    Positive: the cited source exists and carries the figure and the query. Absent: the
    old sentence, which attributed the figure to this guard's shape."""
    root = Path(__file__).resolve().parent.parent
    rules = (root / "docs" / "V1-DESIGN-RULES.md").read_text(encoding="utf-8")
    assert "~490 GB/day of production disk reads" in rules
    assert "`reconcile_chain_head` COUNTs an entire chain" in rules

    doc = " ".join((repo.has_non_terminal_reference.__doc__ or "").split())
    assert "490 GB/day" in doc, "control: the figure is still cited, not deleted"
    assert "reconcile_chain_head" in doc
    assert "docs/V1-DESIGN-RULES.md" in doc
    assert "is the defect that caused ~490 GB/day of disk reads" not in doc
    assert "490 GB/day of disk reads before the fix" not in doc


# =========================================================== backend + seam checks

def test_the_r2_configuration_carries_its_secret_and_never_prints_it(monkeypatch):
    """The R2 secret is carried to the store and never rendered. The backend
    itself (F25, formerly a seam that raised here) is proved in
    tests/test_files_object_store.py."""
    monkeypatch.setenv("MESHYCHAT_BLOB_BACKEND", "r2")
    monkeypatch.setenv("MESHYCHAT_R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("MESHYCHAT_R2_BUCKET", "bucket")
    monkeypatch.setenv("MESHYCHAT_R2_ACCESS_KEY_ID", "akid")
    # A distinctive value: asserting on the literal "secret" would collide with the
    # FIELD NAME `secret_access_key` and the test would fail on its own redaction.
    monkeypatch.setenv("MESHYCHAT_R2_SECRET_ACCESS_KEY", "NEVER-PRINT-THIS-VALUE")
    cfg = cfgmod.build_blob_config()
    assert cfg.backend == "r2" and cfg.r2 is not None and cfg.fs is None
    assert cfg.r2.secret_access_key == "NEVER-PRINT-THIS-VALUE"   # it IS carried...
    assert "NEVER-PRINT-THIS-VALUE" not in repr(cfg.r2)           # ...and never printed
    assert "<redacted>" in repr(cfg.r2)
    store = blobmod.from_config(cfg)
    assert "NEVER-PRINT-THIS-VALUE" not in repr(store)
    assert "<redacted>" in repr(store)


def test_the_fs_backend_refuses_a_key_that_escapes_the_blob_root(files_env):
    """Defence in depth: keys are server-built as
    `{owner}/{file}`, but an untrusted key reaching the store must not escape."""
    store = blobmod.FsBlobStore(cfgmod.FsConfig(str(files_env), TOKEN_SECRET_HEX,
                                                PUBLIC_BASE_URL))
    # POSITIVE CONTROL FIRST: a legitimate key resolves under the root.
    ok = store.safe_path("11111111-1111-1111-1111-111111111111/0447bf92-25fb-5230-b531-5a0d9c00d8c7")
    assert str(ok).startswith(str(files_env))

    for bad in ("../outside", "owner/../../outside", "/outside", ""):
        with pytest.raises(blobmod.BlobError):
            store.safe_path(bad)


def test_a_missing_blob_backend_is_503_not_a_traceback(owner_conn, client, monkeypatch):
    """`MESHYCHAT_BLOB_BACKEND` has no default. This service starts without blob storage
    instead of refusing to boot (a divergence stated in apps/files/config.py; a set-but-wrong
    value does refuse to start,
    tests/test_files_startup.py) and answers 503 E_BLOB_BACKEND_UNAVAILABLE."""
    p = _seed_peer(owner_conn, "nobackend")
    # POSITIVE CONTROL FIRST: with the backend configured the route works.
    assert _create(client, p["peer_id"], size=3).status_code == 201

    # As startup leaves it when the variable is unset.
    monkeypatch.delenv("MESHYCHAT_BLOB_BACKEND")
    blobmod.activate(cfgmod.load_at_startup())
    r = _create(client, p["peer_id"], size=3)
    assert r.status_code == 503, r.content
    assert r.json() == {"error": "E_BLOB_BACKEND_UNAVAILABLE",
                        "message": "blob backend error"}
    # The detail never reaches the client - it would name filesystem paths and env vars.
    assert "MESHYCHAT_BLOB_DIR" not in r.content.decode()


def test_the_authenticated_routes_use_the_store_built_at_startup(owner_conn, client,
                                                                monkeypatch):
    """B11 on the database routes (tests/test_files_startup.py proves it on the raw route in
    a real startup). After the store is built, the environment is changed to name another
    public URL, a 1-byte size cap and no backend at all; create is still 201, presigned
    against the STARTUP base URL with the startup cap. Control: a restart (`_reactivate`)
    is what makes a change take effect, and then the 1-byte cap refuses."""
    p = _seed_peer(owner_conn, "frozen")
    monkeypatch.setenv("MESHYCHAT_PUBLIC_BASE_URL", "https://elsewhere.test")
    monkeypatch.setenv("MESHYCHAT_MAX_FILE_BYTES", "1")
    r = _create(client, p["peer_id"], size=3)
    assert r.status_code == 201, r.content
    assert r.json()["upload_url"].startswith(PUBLIC_BASE_URL + "/v1/files/raw/")
    monkeypatch.delenv("MESHYCHAT_BLOB_BACKEND")
    assert _create(client, p["peer_id"], size=3).status_code == 201

    monkeypatch.setenv("MESHYCHAT_BLOB_BACKEND", "fs")
    _reactivate()
    refused = _create(client, p["peer_id"], size=3)
    assert refused.status_code == 413, refused.content


def test_the_raw_route_is_404_when_the_backend_is_not_fs(owner_conn, client, monkeypatch):
    """The raw-files route is available only when the backend is fs. Django resolves
    from a module imported once, so the path is always mounted and the VIEW answers 404 -
    a deliberate divergence, documented in views.py: the status is the 404 an unmounted route
    would give; the body is this app's JSON error, not an empty one.
    """
    p = _seed_peer(owner_conn, "rawoff")
    r = _create(client, p["peer_id"], size=3)
    path = _token_path(r.json()["upload_url"])
    # POSITIVE CONTROL FIRST: under fs the route serves.
    assert client.put(path, data=b"abc", content_type="text/plain").status_code == 200

    monkeypatch.setenv("MESHYCHAT_BLOB_BACKEND", "r2")
    monkeypatch.setenv("MESHYCHAT_R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("MESHYCHAT_R2_BUCKET", "bucket")
    monkeypatch.setenv("MESHYCHAT_R2_ACCESS_KEY_ID", "akid")
    monkeypatch.setenv("MESHYCHAT_R2_SECRET_ACCESS_KEY", "secret")
    _reactivate()
    assert client.get(path).status_code == 404


def test_a_backend_failure_at_head_is_503_and_says_nothing_about_the_filesystem(
        owner_conn, client, app_conn, monkeypatch):
    """The message is the constant "blob backend error"; the detail is logged.
    Non-not-found backend errors use BlobError rather than BlobNotFound to avoid leaking
    missing state."""
    p = _seed_peer(owner_conn, "badhead")
    fid, _v, _s = _upload_and_finalize(client, p["peer_id"], b"ok bytes")

    cfg = cfgmod.build_blob_config()
    broken = blobmod.from_config(cfg)

    def boom(_key, _ttl_secs):
        raise blobmod.BlobError("permission denied: /srv/blobs/secret-path")

    monkeypatch.setattr(broken, "presign_get", boom)
    svc = service.FileService(app_conn, broken, cfg)
    with pytest.raises(ferrors.ApiError) as exc:
        svc.presign_download(uuid.UUID(fid), p["peer_id"], p["owner_id"])
    assert exc.value.status == 503
    assert exc.value.code == "E_BLOB_BACKEND_UNAVAILABLE"
    assert exc.value.body() == {"error": "E_BLOB_BACKEND_UNAVAILABLE",
                                "message": "blob backend error"}
    assert "/srv/blobs" not in json.dumps(exc.value.body())
    # CONTROL: the unbroken store presigns fine, so the 503 is the injected failure.
    assert service.FileService(app_conn, blobmod.from_config(cfg), cfg) \
        .presign_download(uuid.UUID(fid), p["peer_id"], p["owner_id"]).url


def test_the_quota_seam_can_be_replaced_wholesale(owner_conn, client):
    """The tier service is meant to live in `apps/billing` (not in this tree yet).
    `set_quota_service` is the one import-wide seam that lets it take this over without
    touching service.py."""
    p = _seed_peer(owner_conn, "qseam")
    calls: list[tuple] = []

    class Refusing:
        def try_reserve(self, conn, owner_id, capability, amount):
            calls.append((str(owner_id), capability, amount))
            raise quota.QuotaExceeded(capability, amount, 0)

        def refund(self, conn, reservation):
            raise AssertionError("refund must not run when the reservation was refused")

        def bump(self, conn, owner_id, capability, delta):
            raise AssertionError("bump must not run when finalize never happened")

    payload = b"seamed"
    r = _create(client, p["peer_id"], size=len(payload))
    fid = r.json()["id"]
    client.put(_token_path(r.json()["upload_url"]), data=payload,
               content_type="text/plain")
    sha = hashlib.sha256(payload).hexdigest()

    previous = quota.get_quota_service()
    try:
        quota.set_quota_service(Refusing())
        out = client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                          content_type="application/json", **_auth(p["peer_id"]))
        assert out.status_code == 402, out.content
        assert calls == [(str(p["owner_id"]), "file_bytes", len(payload))]
    finally:
        quota.set_quota_service(previous)

    # POSITIVE CONTROL: with the default service restored the same finalize succeeds, so
    # the 402 came from the installed double and the seam really was uninstalled again.
    assert client.post(f"/v1/files/{fid}/finalize", data=json.dumps({"sha256": sha}),
                       content_type="application/json",
                       **_auth(p["peer_id"])).status_code == 200


# ======================================================= quota: the two fail-open shapes
#
# Both of these were written because a mutation SURVIVED. The guards below are correct in
# the code today and nothing in the 38-test suite noticed when they were removed, so each
# test here names the mutation it exists to kill and asserts the CONSEQUENCE, not the line.


def test_an_unknown_capability_is_refused_rather_than_served_unlimited(owner_conn):
    """KILLS M25: replacing `raise UnknownCapability` (quota.py:111) with `return -1`.

    -1 is UNLIMITED (quota.py:41), so a capability with no override row and no
    (tier, capability) row would be served WITH NO CAP - "the failure direction that gives
    away the product", in the docstring's own words.

    POSITIVE CONTROL FIRST: the same owner, on the same connection, resolves a capability
    that DOES have a row. Without it, a refusal here could equally be a broken fixture, an
    unseeded owner or a closed connection.
    """
    p = _seed_peer(owner_conn, "unkcap")
    owner = p["owner_id"]

    assert quota.resolve_cap(owner_conn, owner, quota.CAPABILITY_FILE_BYTES) == 1073741824

    with pytest.raises(quota.UnknownCapability):
        quota.resolve_cap(owner_conn, owner, "no_such_capability")

    # The other arm of the same guard (quota.py:105): an owner with no dashboard_users row
    # has no tier to read, and that is also a refusal rather than a default.
    with pytest.raises(quota.UnknownCapability):
        quota.resolve_cap(owner_conn, uuid.uuid4(), quota.CAPABILITY_FILE_BYTES)

    # THE CONSEQUENCE, which is what actually matters: a reservation against an unknown
    # capability must be refused and must charge nothing. Under the mutation this returns a
    # Reservation and writes an owner_usage row, because cap < 0 takes the unlimited path.
    with pytest.raises(quota.UnknownCapability):
        quota.TierQuota().try_reserve(owner_conn, owner, "no_such_capability", 10 ** 12)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT capability, counter FROM owner_usage WHERE owner_id=%s",
                    (str(owner),))
        assert cur.fetchall() == [], "a refused reservation must not write usage"
    owner_conn.rollback()


def test_an_over_refund_floors_at_zero_and_does_not_mint_free_quota(owner_conn):
    """KILLS M26: removing the `GREATEST(0, ...)` floor from _bump (quota.py:137).

    A stray or duplicate refund - the refund path is best-effort by design
    - would drive owner_usage.counter NEGATIVE. The counter is the
    left-hand side of the cap predicate in _bump_checked, so a negative counter is not an
    untidy number: it is free quota past the cap, silently.

    The assertion is therefore about ADMISSION, not about the stored value alone.
    """
    p = _seed_peer(owner_conn, "floor")
    owner = p["owner_id"]
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO owner_tier_overrides (owner_id, capability, value) "
                    "VALUES (%s,'file_bytes',100)", (owner,))
    svc = quota.TierQuota()
    cap_name = quota.CAPABILITY_FILE_BYTES

    svc.try_reserve(owner_conn, owner, cap_name, 50)
    cycle = quota._cycle_start(owner_conn, owner)
    assert quota.current_usage(owner_conn, owner, cap_name, cycle) == 50

    # Refund FOUR TIMES what was reserved: 50 - 200 would be -150 unfloored.
    svc.refund(owner_conn, quota.Reservation(str(owner), cap_name, 200))
    assert quota.current_usage(owner_conn, owner, cap_name, cycle) == 0,         "the counter went negative: the zero floor is gone"

    # The consequence. From a floored 0 the cap still binds exactly: 100 is admitted and
    # the next byte is not. From an unfloored -150 both would be admitted, and the second
    # assertion is the one that fails.
    svc.try_reserve(owner_conn, owner, cap_name, 100)
    with pytest.raises(quota.QuotaExceeded):
        svc.try_reserve(owner_conn, owner, cap_name, 1)
    owner_conn.rollback()


def test_every_rejected_key_is_a_refusal_and_not_an_internal_error(owner_conn, client,
                                                                  files_env):
    """KILLS the message-substring routing that was at views.py:390-392.

    `safe_path` has FOUR rejection classes and only one of their messages contains the
    word "escapes", so `if "escapes" in str(exc)` routed the other three to 500. A signed
    token for `C:/Windows/win.ini` is rejected as "blob key must be relative" - measured
    at 500 before this fix.

    Why it matters beyond tidiness: a 5xx says "the server is broken, retry", a 401 says
    "this token will never work". The client that most wants to know the difference is the
    one probing with traversal keys.

    Both verbs, because PUT and GET reach the guard through different code paths.
    """
    p = _seed_peer(owner_conn, "traversal")
    secret = bytes.fromhex(TOKEN_SECRET_HEX)
    exp = int(_dt.datetime.now(_dt.timezone.utc).timestamp()) + 300

    def token_for(key, op):
        return blobmod.sign_token(secret, blobmod.TokenPayload(
            key=key, op=op, exp_unix=exp,
            max_size_bytes=64 if op == blobmod.TOKEN_OP_PUT else None))

    # POSITIVE CONTROL FIRST: an identically-minted token for a LEGAL key works on both
    # verbs. Without it, four 401s could equally mean the secret, the clock or the route
    # is wrong, and the test would pass for the wrong reason.
    good = f"{p['owner_id']}/{uuid.uuid4()}"
    assert client.put(f"/v1/files/raw/{token_for(good, blobmod.TOKEN_OP_PUT)}",
                      data=b"legal", content_type="text/plain").status_code == 200
    ok = client.get(f"/v1/files/raw/{token_for(good, blobmod.TOKEN_OP_GET)}")
    assert ok.status_code == 200 and ok.content == b"legal"

    # One key per rejection class in safe_path (blob.py:206-213), named by the class.
    rejected = {
        "": "empty",
        "C:/Windows/win.ini": "windows drive letter - absolute, NOT matched by 'escapes'",
        "/etc/passwd": "posix absolute - also NOT matched by 'escapes'",
        "owner/../../etc/passwd": "dot-dot - the ONLY one the old substring caught",
    }
    if sys.platform != "win32":
        # Windows only (kxxx, 2026-10-10): safe_path judges a key with this OS's Path, and a POSIX path has no drive,
        # so on macOS or Linux `C:/Windows/win.ini` is an ordinary relative key and is stored (pxx's local run, 200).
        # HQD, the gate and the production host, runs Windows, where it is refused; the case runs there.
        rejected.pop("C:/Windows/win.ini")
    for key, why in rejected.items():
        for op, call in ((blobmod.TOKEN_OP_PUT,
                          lambda t: client.put(f"/v1/files/raw/{t}", data=b"x",
                                               content_type="text/plain")),
                         (blobmod.TOKEN_OP_GET,
                          lambda t: client.get(f"/v1/files/raw/{t}"))):
            r = call(token_for(key, op))
            assert r.status_code == 401, f"{op} {key!r} ({why}) -> {r.status_code}"
            assert r.json()["error"] == "E_AUTH_INVALID", r.content
            # The refusal must not echo the path back to whoever probed with it.
            assert key == "" or key not in r.content.decode()


# ===================================== the entitlements gate, no longer a no-op

def _set_entitlement(conn, owner_id, *, active, fresh=True):
    """Upsert the Accounts verdict. `fresh=False` makes `recheck_at` already past, which is
    the STALE case - a different denial from `inactive`, and the one that quietly keeps
    granting if it is folded into the other."""
    import datetime as _d
    when = _d.datetime.now(_d.timezone.utc) + (_d.timedelta(hours=1) if fresh
                                               else _d.timedelta(hours=-1))
    with conn.cursor() as cur:
        # `issuer` and `subject` are NOT NULL - read off information_schema rather than
        # assumed. The first draft of this helper omitted both and died on the constraint.
        cur.execute(
            "INSERT INTO accounts_entitlements "
            "  (owner_id, issuer, subject, active, recheck_at) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (owner_id) DO UPDATE "
            "SET active = EXCLUDED.active, recheck_at = EXCLUDED.recheck_at",
            (str(owner_id), "accounts-test", f"sub-{owner_id}", active, when))
    conn.commit()


def test_a_revoked_owner_cannot_reserve_storage(owner_conn, client):
    """`check_product_access` WAS A DOCUMENTED NO-OP, and that is why this test matters.

    The seam was correctly placed and correctly called from inside `resolve_cap` - the
    chokepoint - and it returned None. A gate that returns None is indistinguishable from a
    gate that passed, so no test could see it and a revoked owner kept reserving storage.

    The same hole was live one layer up on the dashboard, where the check had been hoisted
    to a per-route helper and one route was missed. Two apps, one gate, so the
    implementation now lives in `apps/core/entitlements` and both call it.

    ORDER MATTERS HERE: the POSITIVE CONTROL runs first and uploads successfully, so the
    refusal below cannot be a broken fixture, a bad token or a full disk.
    """
    p = _seed_peer(owner_conn, "entitled")

    # CONTROL: an ACTIVE entitlement uploads and finalizes normally.
    _set_entitlement(owner_conn, p["owner_id"], active=True)
    fid, view, _s = _upload_and_finalize(client, p["peer_id"], b"allowed")
    assert view["status"] == "ready"

    # REVOKED: the identical call is refused, and refused at 402 with a message that says
    # what actually happened rather than inventing a quota that was never exceeded.
    _set_entitlement(owner_conn, p["owner_id"], active=False)
    payload = b"denied"
    created = _create(client, p["peer_id"], size=len(payload))
    assert created.status_code == 201, created.content
    fid2 = created.json()["id"]
    assert client.put(_token_path(created.json()["upload_url"]), data=payload,
                      content_type="text/plain").status_code == 200
    sha = hashlib.sha256(payload).hexdigest()
    denied = client.post(f"/v1/files/{fid2}/finalize", data=json.dumps({"sha256": sha}),
                         content_type="application/json", **_auth(p["peer_id"]))
    assert denied.status_code == 402, denied.content
    body = denied.json()
    assert body["error"] == "E_QUOTA_EXCEEDED"
    assert "product access denied" in body["message"], body
    assert "would exceed cap" not in body["message"], (
        "a revoked entitlement is being reported as a full quota - an operator reading this "
        "goes looking at usage counters for a problem that lives in the entitlement row")


def test_a_stale_entitlement_verdict_denies_rather_than_falling_back(owner_conn, client):
    """THE STALE CASE IS NOT THE INACTIVE CASE, and collapsing them is the failure that
    looks like nothing is wrong: an EXPIRED cached verdict would keep granting access.

    Here the row still says active=True; only `recheck_at` has passed. It must deny.
    """
    p = _seed_peer(owner_conn, "stale")

    _set_entitlement(owner_conn, p["owner_id"], active=True, fresh=True)
    fid, view, _s = _upload_and_finalize(client, p["peer_id"], b"fresh-ok")
    assert view["status"] == "ready", "control: a fresh active verdict must pass"

    # SAME row, still active=True, only the freshness changed.
    _set_entitlement(owner_conn, p["owner_id"], active=True, fresh=False)
    payload = b"stale"
    created = _create(client, p["peer_id"], size=len(payload))
    fid2 = created.json()["id"]
    client.put(_token_path(created.json()["upload_url"]), data=payload,
               content_type="text/plain")
    sha = hashlib.sha256(payload).hexdigest()
    denied = client.post(f"/v1/files/{fid2}/finalize", data=json.dumps({"sha256": sha}),
                         content_type="application/json", **_auth(p["peer_id"]))
    assert denied.status_code == 402, (
        f"a STALE verdict was honoured as if still valid ({denied.status_code})")


def test_an_owner_with_no_entitlement_row_is_not_denied(owner_conn, client):
    """NO ROW PASSES - an owner predating the Accounts cutover must not be locked out.

    This is the arm that makes the gate safe to turn on, and the one a fail-closed instinct
    gets wrong. Every other test in this file relies on it implicitly, which is exactly why
    it is asserted somewhere explicitly.
    """
    p = _seed_peer(owner_conn, "norow")
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM accounts_entitlements WHERE owner_id = %s",
                    (str(p["owner_id"]),))
    owner_conn.commit()
    fid, view, _s = _upload_and_finalize(client, p["peer_id"], b"grandfathered")
    assert view["status"] == "ready"


# ------------------------------------------- the JSON depth middleware leaves raw uploads alone
# #280's JsonDepthMiddleware read request.body for every application/json request. The raw
# route receives the file's own Content-Type, so a .json attachment arrives as
# application/json: measured on main 2a337bfa, a 3 MB one answered RequestDataTooBig, and the
# same bytes as application/octet-stream uploaded fine.

def _big_json() -> bytes:
    payload = json.dumps({"rows": ["x" * 100] * 30_000}).encode()
    assert len(payload) > 2_621_440, "must exceed DATA_UPLOAD_MAX_MEMORY_SIZE's default"
    return payload


def test_a_json_attachment_over_2_5_mb_uploads_and_finalizes(owner_conn, client):
    p = _seed_peer(owner_conn, "bigjson")
    payload = _big_json()
    fid, view, sha = _upload_and_finalize(client, p["peer_id"], payload,
                                          content_type="application/json")
    assert view["status"] == "ready" and sha == hashlib.sha256(payload).hexdigest()


def test_the_same_bytes_as_octet_stream_upload_too(owner_conn, client):
    """Control: the size alone was never the problem."""
    p = _seed_peer(owner_conn, "bigbin")
    _upload_and_finalize(client, p["peer_id"], _big_json(),
                         content_type="application/octet-stream")


def test_a_deeply_nested_json_file_is_stored_not_refused(owner_conn, client):
    """A file's contents are nobody's request: 300 levels is past the 256 request cap."""
    from apps.core.jsondepth import MAX_DEPTH
    p = _seed_peer(owner_conn, "deepjson")
    payload = ("[" * (MAX_DEPTH + 44) + "]" * (MAX_DEPTH + 44)).encode()
    fid, view, sha = _upload_and_finalize(client, p["peer_id"], payload,
                                          content_type="application/json")
    assert view["status"] == "ready"


def test_the_exempt_prefix_is_the_raw_route():
    """The middleware exempts by path; if the raw route ever moves, this fails first."""
    from django.urls import resolve
    from apps.core.jsondepth import RAW_UPLOAD_PREFIX
    assert resolve(RAW_UPLOAD_PREFIX + "sometoken").url_name == "files-raw"
    assert resolve("/v1/files").url_name != "files-raw"


def test_the_exemption_is_only_the_raw_route(owner_conn, client):
    """A deep JSON request to the files CREATE route is still refused by the cap: the
    exemption must not widen to every /v1/files path."""
    from apps.core.jsondepth import MAX_DEPTH
    p = _seed_peer(owner_conn, "deepcreate")
    deep = json.loads("[" * (MAX_DEPTH + 1) + "]" * (MAX_DEPTH + 1))
    r = client.post("/v1/files", data=json.dumps({"filename": "a.txt", "content_type": "text/plain",
                                                  "size_bytes_declared": 1, "x": deep}),
                    content_type="application/json", **_auth(p["peer_id"]))
    assert r.status_code == 400 and r.json()["error"] == "E_VALIDATION", r.content[:200]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
