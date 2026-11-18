"""A world for the summary routes' tests: the real urlconf, a real database, the fake HQB.

Divergence 037. `SummaryWorld` points the served app at:
  * a FakeSummaryModel (tests/fixtures/meshsum_fake.py) declared as the ONE chat provider,
  * a measurement record for exactly that fake's model pin and this summary_version,
    written to a temporary file and verified by its own digest (apps/meshsum/record.py),
  * a memory authority in its own trust group, so a reader in that group with an active
    key is a caller memory.read's invoke path admits (divergence 027's body gate),
and resets every in-process memory a summary keeps (cache, token counts, pin, record,
cooldowns) so tests do not see each other's. Admission's budgets are widened for the
world, because each route test makes a few model calls; tests/test_meshsum_admission.py
holds the real values, and a route test that needs a limit sets it.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
import uuid

import jwt as pyjwt
import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.test import override_settings

from apps.mesh import projection as mesh_projection
from apps.meshsum import admission, cache, pin, prompt, record, views, window
from apps.modelhost import registry
from tests.fixtures import meshsem_db as db
from tests.fixtures.meshsum_fake import FakeSummaryModel

ISSUER = "meshy-meshsum-test-issuer"
_key = ed25519.Ed25519PrivateKey.generate()
PRIV = _key.private_bytes(encoding=serialization.Encoding.PEM,
                          format=serialization.PrivateFormat.PKCS8,
                          encryption_algorithm=serialization.NoEncryption())
PUB = _key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                     format=serialization.PublicFormat.SubjectPublicKeyInfo)

WIDE = {"inflight": 2, "peer_calls_per_hour": 1000, "peer_calls_per_5min": 1000,
        "owner_calls_per_hour": 100000, "fleet_calls_per_hour": 1000000,
        "peer_requests_per_hour": 1000}

_LAST = [_dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)]


def when(back_s: float = 0.0) -> _dt.datetime:
    """A created_at newer than every row written before it (the edges projection walks
    from its watermark, so an older-stamped row would never be projected)."""
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=back_s)
    if back_s == 0:
        if t <= _LAST[0]:
            t = _LAST[0] + _dt.timedelta(microseconds=1)
        _LAST[0] = t
    return t


def bearer(peer_id, *, cnf: str | None = None) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims = {"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + 300,
              "aud": "peer", "scopes": []}
    if cnf is not None:
        claims["cnf"] = cnf
    tok = pyjwt.encode(claims, PRIV, algorithm="EdDSA")
    return {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


def project(conn) -> None:
    for _ in range(200):
        if not mesh_projection.catch_up(conn).more:
            return


def write_record(path, *, model_pin: str, provider_id: str = "chat-fake",
                 summary_version: str | None = None, passed: bool = True) -> None:
    body = {"schema": record.SCHEMA,
            "summary_version": summary_version or prompt.SUMMARY_VERSION,
            "model_pin": model_pin, "provider_id": provider_id, "passed": passed,
            "floors": {}, "measured_at": "test"}
    body["content_sha256"] = record.content_sha256(body)
    path.write_text(json.dumps(body), encoding="utf-8")


def reset_memories() -> None:
    cache.CACHE.clear()
    window.forget_counts()
    pin.forget()
    record.forget()
    views.forget_cooldowns()


class SummaryWorld:
    def __init__(self, dsn: str, tmp_path, owner, monkeypatch):
        self.dsn, self.owner = dsn, owner
        self.model = FakeSummaryModel(chars_per_token=4)
        self.record_path = tmp_path / "summary-record.json"
        self.memory = db.memory_authority(owner)
        self.pin = pin.read_pin(registry.Provider(id="chat-fake", kind="chat",
                                                  base_url=self.model.base_url)).digest
        write_record(self.record_path, model_pin=self.pin)
        monkeypatch.setattr(admission, "LIMITS", dict(WIDE))
        self.gate = True
        self._ctx = None
        self.declare()
        reset_memories()
        with owner.cursor() as cur:
            cur.execute("UPDATE mesh_summary_requests SET state = 'done' "
                        "WHERE state = 'calling'")

    def declare(self, *, gate=None, providers=None, memory_peer_id=None) -> None:
        if self._ctx is not None:
            self._ctx.disable()
        if gate is not None:
            self.gate = gate
        self._ctx = override_settings(
            ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"], MESHY_DSN=self.dsn,
            MESHY_JWT_PUBLIC_KEY_PEM=PUB, MESHY_JWT_ISSUER=ISSUER,
            MESHY_MODEL_PROVIDERS=providers if providers is not None
            else [self.model.provider(pid="chat-fake")],
            MESHY_EMBED_FORBIDDEN_ROWS=self.gate,
            MESHY_MEMORY_PEER_ID=str(memory_peer_id or self.memory["peer_id"]),
            MESHY_SUMMARY_RECORD_PATH=str(self.record_path))
        self._ctx.enable()
        record.forget()

    def close(self) -> None:
        if self._ctx is not None:
            self._ctx.disable()
        self.model.close()
        reset_memories()

    # ---------------------------------------------------------------- peers and rows

    def reader(self, tag: str) -> dict:
        """A peer memory.read's invoke path admits, holding a read grant."""
        p = db.peer(self.owner, tag, group=self.memory["group"], key=True)
        db.grant(self.owner, p, "read")
        return p

    def name_of(self, p: dict) -> str:
        with self.owner.cursor() as cur:
            cur.execute("SELECT name FROM peers WHERE id = %s", (p["peer_id"],))
            return cur.fetchone()[0]

    def msg(self, p: dict, thread: str | None, body, *, at=None, to=None, cc=None,
            policy: str | None = "allowed", **extra) -> dict:
        value = {"schema_version": 1, "from": p["label"], "type": "note",
                 "ts": "2026-09-24T00:00:00Z", "author_family": "test", "body": body,
                 **extra}
        if thread is not None:
            value["thread_id"] = thread
        if to is not None:
            value["to"] = to
        if cc is not None:
            value["cc"] = cc
        return db.row(self.owner, p, value, created_at=at or when(), embed_policy=policy)

    def thread(self, n: int = 3, *, authors=None, bodies=None) -> tuple[str, list[dict]]:
        """A projected thread of `n` messages alternating between two authors."""
        t = f"test/sum-{uuid.uuid4().hex[:10]}"
        authors = authors or [db.peer(self.owner, "otter"), db.peer(self.owner, "heron")]
        rows = []
        for i in range(n):
            body = bodies[i] if bodies else f"note {chr(97 + i)} about the lease renewal"
            rows.append(self.msg(authors[i % len(authors)], t, body))
        project(self.owner)
        return t, rows

    def ledger(self, peer_id) -> list[dict]:
        with self.owner.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM mesh_summary_requests WHERE peer_id = %s "
                        "ORDER BY requested_at", (peer_id,))
            return cur.fetchall()

    def fake_requests(self) -> int:
        """Every request the fake HQB has seen: GETs, /tokenize and chat."""
        return len(self.model.gets) + len(self.model.tokenized) + self.model.chats()


def wait_until(pred, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False
