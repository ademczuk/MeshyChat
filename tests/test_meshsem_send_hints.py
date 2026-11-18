"""Tests for POST /v1/mesh/hints (apps/meshsem/send_hints.py) and tools/meshy_node/hints_eval.py."""
from __future__ import annotations
import datetime as _dt, json, os, sys, uuid
from pathlib import Path
import psycopg, pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DSN = os.environ.get("MESHY_TEST_DSN", "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

ISSUER, _key = "meshy-sendhints-test-issuer", ed25519.Ed25519PrivateKey.generate()
PRIV = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
PUB = _key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402
if not dj_settings.configured: django.setup()  # pragma: no cover

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from apps.core import peer_limit  # noqa: E402
from apps.mesh import projection as mesh_projection  # noqa: E402
from apps.meshsem import embedjob, graphwalk, search, send_hints, spaces, views  # noqa: E402
from tests.fixtures import history_rows, meshsem_db as db, signed_rows as sr  # noqa: E402
from tests.fixtures.meshsem_fake import FakeModelHost  # noqa: E402
from tools.meshy_node import hints_eval  # noqa: E402

_MEMORY: dict = {}

def _bearer(peer_id) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return {"HTTP_AUTHORIZATION": f"Bearer {pyjwt.encode({'sub': str(peer_id), 'iss': ISSUER, 'iat': now, 'exp': now + 300, 'aud': 'peer', 'scopes': []}, PRIV, algorithm='EdDSA')}"}

def _memory_authority() -> dict:
    if not _MEMORY:
        with psycopg.connect(DSN, autocommit=True) as c: _MEMORY.update(db.memory_authority(c))
    return _MEMORY

@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        db.ensure_namespace(c); yield c

class HintsWorld:
    def __init__(self, space, embed_host, rerank_host, floor: int, card_ns: str):
        self.space, self.embed_host, self.rerank_host, self.floor, self.card_ns, self._ctx = space, embed_host, rerank_host, floor, card_ns, None
        self.memory_peer_id = _memory_authority()["peer_id"]
        self.declare()
    def declare(self, *, hints_enabled: bool = True, rerank: bool = True, card_min: float = 2.0, mesh_min: float = 1.3,
                include_context: bool = False, ctx_ns: str = "shared/transcript-context", ctx_index: bool = False,
                forbidden_rows: bool = True, walk_cards: bool = True):
        if self._ctx is not None: self._ctx.disable()
        provs = [{"id": "embed-hqb", "kind": "embed", "base_url": self.embed_host.base_url, "space": self.space.name}]
        if rerank: provs.append({"id": "rerank-hqb", "kind": "rerank", "base_url": self.rerank_host.base_url})
        graph_ns = f"shared/mesh-chat,{self.card_ns}" if walk_cards else "shared/mesh-chat"
        self._ctx = override_settings(
            ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"], MESHY_DSN=DSN, MESHY_JWT_PUBLIC_KEY_PEM=PUB,
            MESHY_JWT_ISSUER=ISSUER, MESHY_SEMANTIC_SPACE=self.space.name, MESHY_MODEL_PROVIDERS=provs,
            MESHY_EMBED_FORBIDDEN_ROWS=forbidden_rows, MESHY_MEMORY_PEER_ID=str(self.memory_peer_id),
            MESHY_SEND_HINTS_ENABLED=hints_enabled, MESHY_SEND_HINTS_INCLUDE_CONTEXT=include_context,
            MESHY_CONTEXT_NAMESPACE=ctx_ns, MESHY_CONTEXT_INDEX=ctx_index,
            MESHY_SEND_HINTS_CARD_MIN_SCORE=card_min, MESHY_SEND_HINTS_MESH_MIN_SCORE=mesh_min,
            MESHY_CARDS_NAMESPACE=self.card_ns, MESHY_GRAPH_NAMESPACES=graph_ns)
        self._ctx.enable(); search.forget()
    def close(self):
        if self._ctx is not None: self._ctx.disable()

@pytest.fixture()
def world(monkeypatch):
    space, card_ns = db.make_space("hints"), f"shared/decisions-{uuid.uuid4().hex[:8]}"
    monkeypatch.setitem(spaces.SPACES, space.name, space)
    with psycopg.connect(DSN, autocommit=True) as c:
        floor, ns_owner = db.commit_floor(c), db.peer(c, "cardnsowner")
        with c.cursor() as cur:
            cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id, claimant_plane,"
                        " ephemeral, retention_policy) VALUES (%s,%s,%s,'dev',FALSE,'keep_all')", (card_ns, ns_owner["peer_id"], ns_owner["owner_id"]))
    eh, rh = FakeModelHost(dims=db.DIMS), FakeModelHost(dims=db.DIMS)
    rh.rerank_default = 5.0
    send_hints.forget(); views._HINTS_BOUND.forget()
    w = HintsWorld(space, eh, rh, floor, card_ns)
    yield w
    for obj in (w, eh, rh): obj.close()
    for fn in (search.forget, send_hints.forget, views._HINTS_BOUND.forget): fn()

def _drain(conn, w: HintsWorld):
    for _ in range(200):
        r = embedjob.catch_up(conn)
        if not r.more or r.rows_processed == 0: break
    db.keep_only_rows_after(conn, w.space, w.floor)
    while mesh_projection.catch_up(conn).more: pass
    graphwalk.catch_up(conn)

def _grant_ns(conn, ns: str, grantee: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace WHERE namespace = %s", (ns,))
        owner_peer, owner_owner = cur.fetchone()
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, permission,"
                    " granted_by_peer_id, granted_by_owner_id) VALUES (%s,%s,%s,'read',%s,%s) ON CONFLICT DO NOTHING",
                    (ns, grantee["peer_id"], grantee["owner_id"], owner_peer, owner_owner))

def _plant_card(conn, ns: str, writer: dict, *, seat: str, ctype: str, text_body: str,
                time_str: str = "2026-11-03T12:00:00.000Z", supersedes: list[str] | None = None) -> str:
    cid, rid = uuid.uuid4().hex, uuid.uuid4()
    key = f"card/{seat}/{cid}"
    val = {"schema": "card.v1", "id": cid, "type": ctype, "owner": "operator", "seat": seat, "time": time_str,
           "text": f"{ctype} | owner: operator\n{text_body}", "text_sha256": "0" * 64,
           "source": {"record_uuid": "r1", "block_index": 0, "kind": "transcript", "first_hand": True},
           **({"supersedes": supersedes} if supersedes else {})}
    history_rows.insert_seed(conn, sr.row(writer["peer_id"], writer["owner_id"], ns, key, val, rid=rid,
                                          call_id=f"call-{rid}", version=1, is_head=True, author_label=seat, embed_policy="allowed"))
    return key

def test_seat_stem_receipt_filter_and_anchor_normalization():
    for raw, exp in [("oxxxxxx", "oxxxxxx"), ("codex-oxxxxxx-cli", "oxxxxxx"), ("codex-oxxxxxx-admin-cli", "oxxxxxx"),
                     ("codex-kxxx-cli-cc", "kxxx"), ("memory-bxxxxx", "bxxxxx"), ("bxxxxx-prod", "bxxxxx"),
                     ("codex-bxxxxx-cli", "bxxxxx"), ("exxxx-session", "exxxx"), ("all", "")]:
        assert send_hints.seat_stem(raw) == exp
    assert send_hints.is_non_actionable_receipt("DONE\nYour signed ACK reached this Codex session as a queued notice without an inbox poll.")
    assert send_hints.is_non_actionable_receipt("Arrived in this Codex thread. Reply sent using python -m tools.meshy_node send with state.json.")
    assert not send_hints.is_non_actionable_receipt("DONE: patched ShardCache eviction leak in ctxindex.py (#412); do not reopen.")
    assert send_hints.normalize_rerank_text(f"Ruling | owner: operator\nSee https://github.com/org/repo-name/pull/823 sha256 {'a' * 64}") == "See repo-name #823"
    anchors = send_hints.extract_technical_anchors(
        "Check https://github.com/org/repo-name/pull/823 and ctxindex.py on localhost:7876 with codex-alpha-cli", exclude_stems={"alpha"})
    assert {"#823", "id:repo-name", "file:ctxindex.py", "port:7876"} <= anchors and "id:codex-alpha-cli" not in anchors


def test_daily_hint_ledger_preserves_current_day_claims_at_capacity_and_evicts_expired_days():
    ledger = send_hints.DailyHintLedger(max_entries=2)
    assert ledger.claim("oxxxxxx", "msg/a", "2026-11-07") is True
    assert ledger.claim("oxxxxxx", "msg/b", "2026-11-07") is True
    assert ledger.can_claim("oxxxxxx", "msg/c", "2026-11-07") is False
    assert ledger.claim("oxxxxxx", "msg/c", "2026-11-07") is False
    assert ledger.record("oxxxxxx", "msg/d", "2026-11-07") is False
    assert ledger.seen("oxxxxxx", "msg/a", "2026-11-07") is True
    assert ledger.seen("oxxxxxx", "msg/b", "2026-11-07") is True
    assert ledger.seen("oxxxxxx", "msg/c", "2026-11-07") is False
    assert ledger.claim("oxxxxxx", "msg/a", "2026-11-07") is False

    # Next UTC day evicts expired prior-day entries, and a late prior-day straggler cannot evict current-day claims.
    assert ledger.can_claim("oxxxxxx", "msg/c", "2026-11-08") is True
    assert ledger.claim("oxxxxxx", "msg/c", "2026-11-08") is True
    assert ledger.claim("oxxxxxx", "msg/d", "2026-11-08") is True
    assert ledger.seen("oxxxxxx", "msg/a", "2026-11-07") is False
    assert ledger.claim("oxxxxxx", "msg/straggler", "2026-11-07") is False
    assert ledger.seen("oxxxxxx", "msg/c", "2026-11-08") is True
    assert ledger.seen("oxxxxxx", "msg/d", "2026-11-08") is True
    assert send_hints.DailyHintLedger(max_entries=0).claim("oxxxxxx", "msg/a", "2026-11-07") is False

def test_hints_eval_graded_pairs_accounts_precut_drops_and_bidirectional_mirage():
    pairs = [
        {"send_key": "msg/1000-oxxxxxx-aaaaaa", "hint_key": "msg/0900-codex-oxxxxxx-cli-bbbbbb", "grade": 0},
        {"send_key": "msg/1100-pxx-cccccc", "hint_key": "msg/0950-uxxx-dddddd", "grade": 0},
        {"send_key": "msg/1200-kxxx-eeeeee", "hint_key": "msg/0800-dxxxxx-ffffff", "grade": 2},
        {"send_key": "msg/1300-codex-kxxx-cli-111111", "hint_key": "msg/0800-dxxxxx-ffffff", "grade": 0},
        {"send_key": "msg/1400-txxxx-222222", "hint_key": "msg/0850-uxxx-333333", "grade": 0},
    ]
    la, lb, mir = "A" * 90 + " identity minting", "B" * 90 + " identity minting", "M" * 90 + " unidirectional mirage"
    msgs = {
        "msg/1000-oxxxxxx-aaaaaa": {"author": "oxxxxxx", "to": ["vxxxxxxxxxx"], "thread_id": "t1", "body": la},
        "msg/0900-codex-oxxxxxx-cli-bbbbbb": {"author": "codex-oxxxxxx-cli", "to": ["vxxxxxxxxxx"], "thread_id": "t2", "body": lb},
        "msg/1100-pxx-cccccc": {"author": "pxx", "to": ["dxxxxx"], "thread_id": "same-th", "body": la},
        "msg/0950-uxxx-dddddd": {"author": "uxxx", "to": ["kxxx"], "thread_id": "same-th", "body": lb},
        "msg/1200-kxxx-eeeeee": {"author": "kxxx", "to": ["memory-bxxxxx"], "thread_id": "t3", "body": la},
        "msg/0800-dxxxxx-ffffff": {"author": "dxxxxx", "to": ["uxxx"], "thread_id": "t4", "body": lb},
        "msg/1300-codex-kxxx-cli-111111": {"author": "codex-kxxx-cli", "to": ["memory-bxxxxx"], "thread_id": "t5", "body": la},
        "msg/1400-txxxx-222222": {"author": "txxxx", "to": ["kxxx"], "thread_id": "t6", "body": la},
        "msg/0850-uxxx-333333": {"author": "uxxx", "to": ["dxxxxx"], "thread_id": "t7", "body": mir},
    }
    res = hints_eval.evaluate_graded_pairs(pairs, msgs, lambda q, docs: [-0.2 if q.startswith("M") else 4.0 for _ in docs], thresholds=(1.3,))
    assert res["precut_drops"] == {"ack_or_short": 0, "same_thread_or_reply": 1, "same_author_or_addressed": 1, "daily_cap": 1}
    assert res["forward_sweep"][0]["shown"] == 2 and res["calibrated_sweep"][0]["shown"] == 1 and res["calibrated_sweep"][0]["precision"] == 1.0

def test_hints_route_method_auth_permission_validation_and_default_off(owner, world):
    client = Client()
    world.declare(hints_enabled=False)
    r_get = client.get("/v1/mesh/hints")
    assert r_get.status_code == 405 and r_get.headers.get("Allow") == "POST"
    assert client.post("/v1/mesh/hints", data="{}", content_type="application/json").status_code == 401
    no_grant = db.peer(owner, "nogrant", group=_memory_authority()["group"], key=True)
    assert client.post("/v1/mesh/hints", data=json.dumps({"body": "valid text over forty characters for hint testing now"}),
                       content_type="application/json", **_bearer(no_grant["peer_id"])).status_code == 403
    reader = db.peer(owner, "antigravity", group=_memory_authority()["group"], key=True)
    db.grant(owner, reader, "read")
    assert client.post("/v1/mesh/hints", data="{bad", content_type="application/json", **_bearer(reader["peer_id"])).status_code == 422
    assert client.post("/v1/mesh/hints", data=json.dumps({"body": "valid text", "to": "not-a-list"}),
                       content_type="application/json", **_bearer(reader["peer_id"])).status_code == 422
    r_off = client.post("/v1/mesh/hints", data=json.dumps({"body": "checking default feature flag state when disabled"}),
                        content_type="application/json", **_bearer(reader["peer_id"]))
    assert r_off.status_code == 200 and r_off.json() == {"enabled": False, "hints": [], "dropped": {}, "reranked": False, "abstained_reason": "disabled"}

def test_hints_minute_bound_limits_without_spending_peer_calls_bucket(owner, world, monkeypatch):
    client, reader = Client(), db.peer(owner, "boundpeer", group=_memory_authority()["group"], key=True)
    db.grant(owner, reader, "read")
    admit_calls, orig_admit = [], peer_limit.admit
    monkeypatch.setattr(peer_limit, "admit", lambda *a, **kw: admit_calls.append(1) or orig_admit(*a, **kw))
    monkeypatch.setattr(views._HINTS_BOUND, "per_minute", 2)
    views._HINTS_BOUND.forget()
    payload = json.dumps({"body": "short", "msg_type": "ack"})
    for code in (200, 200, 429):
        assert client.post("/v1/mesh/hints", data=payload, content_type="application/json", **_bearer(reader["peer_id"])).status_code == code
    assert admit_calls == [], "POST /v1/mesh/hints must check peer_limit without spending a CALLS token"

def test_hints_enforces_three_drops_multisource_cards_zero_prose_and_fail_closed(owner, world):
    client, grp = Client(), _memory_authority()["group"]
    sender, alt_self = db.peer(owner, "codex-oxxxxxx-cli", group=grp, key=True), db.peer(owner, "oxxxxxx", group=grp, key=True)
    rcpt_peer, stranger, card_writer = db.peer(owner, "vxxxxxxxxxx", group=grp, key=True), db.peer(owner, "dxxxxx", group=grp, key=True), db.peer(owner, "kxxx", group=grp, key=True)
    db.grant(owner, sender, "read"); _grant_ns(owner, world.card_ns, sender)

    shared_words = "shard prefilter budget eviction latency regression fix calibration verified"
    secret_body, secret_card = "SECRET_PEER_MESSAGE_BODY_DO_NOT_RETURN", "SECRET_DECISION_CARD_PROSE_DO_NOT_RETURN"
    db.row(owner, stranger, {"schema_version": 1, "from": "dxxxxx", "to": ["kxxx"], "thread_id": "active-thread", "type": "note", "body": f"{shared_words} same thread {secret_body}"}, embed_policy="allowed")
    irt_row = db.row(owner, stranger, {"schema_version": 1, "from": "dxxxxx", "to": ["kxxx"], "thread_id": "parent-thread", "type": "note", "body": f"{shared_words} parent reply {secret_body}"}, embed_policy="allowed")
    db.row(owner, alt_self, {"schema_version": 1, "from": "oxxxxxx", "to": ["kxxx"], "thread_id": "other-self-thread", "type": "note", "body": f"{shared_words} sibling signing peer {secret_body}"}, embed_policy="allowed")
    db.row(owner, rcpt_peer, {"schema_version": 1, "from": "vxxxxxxxxxx", "to": ["kxxx"], "thread_id": "rcpt-thread", "type": "note", "body": f"{shared_words} addressed recipient {secret_body}"}, embed_policy="allowed")
    db.row(owner, stranger, {"schema_version": 1, "from": "dxxxxx", "to": ["oxxxxxx"], "thread_id": "already-got-thread", "type": "note", "body": f"{shared_words} already sent to sender {secret_body}"}, embed_policy="allowed")
    db.row(owner, stranger, {"schema_version": 1, "from": "dxxxxx", "to": ["kxxx"], "thread_id": "receipt-thread", "type": "answer", "body": "Arrived in this Codex thread. Reply sent using python -m tools.meshy_node send with state.json."}, embed_policy="allowed")
    good_mesh = db.row(owner, stranger, {"schema_version": 1, "from": "dxxxxx", "to": ["kxxx"], "thread_id": "cross-lane-thread", "subject": "ctxindex shard eviction note", "type": "note", "body": f"{shared_words} genuine cross lane finding {secret_body}"}, embed_policy="allowed")
    old_card = _plant_card(owner, world.card_ns, card_writer, seat="kxxx", ctype="Ruling", text_body=f"{shared_words} old superseded ruling {secret_card}", time_str="2026-11-03T10:00:00.000Z")
    active_card = _plant_card(owner, world.card_ns, card_writer, seat="kxxx", ctype="Ruling", text_body=f"{shared_words} active operator ruling {secret_card}", time_str="2026-11-03T11:00:00.000Z", supersedes=[old_card])
    _drain(owner, world)

    req_payload = {"body": f"Investigating {shared_words} before opening a pull request on HQD.", "thread_id": "active-thread",
                   "in_reply_to": irt_row["key"], "to": ["vxxxxxxxxxx"], "sender_label": "codex-oxxxxxx-cli"}
    r1 = client.post("/v1/mesh/hints", data=json.dumps(req_payload), content_type="application/json", **_bearer(sender["peer_id"]))
    b1 = r1.json()
    assert r1.status_code == 200 and b1["enabled"] is True and b1["reranked"] is True
    assert {h["key"] for h in b1["hints"]} == {active_card, good_mesh["key"]} and old_card not in {h["key"] for h in b1["hints"]}
    assert b1["dropped"]["same_thread_or_reply"] >= 2 and b1["dropped"]["same_author_or_addressed"] >= 3
    raw_wire = json.dumps(b1)
    assert secret_body not in raw_wire and secret_card not in raw_wire and all("body" not in h and "text" not in h for h in b1["hints"])

    r2 = client.post("/v1/mesh/hints", data=json.dumps(req_payload), content_type="application/json", **_bearer(sender["peer_id"]))
    assert r2.status_code == 200 and r2.json()["hints"] == [] and r2.json()["dropped"]["daily_cap"] == 2

    from apps.core import dbconn
    tiny_ledger = send_hints.DailyHintLedger(max_entries=1)
    req_obj = send_hints.parse_request(json.dumps(req_payload).encode("utf-8"))
    with dbconn.connect() as app_conn:
        cap1 = send_hints.evaluate_hints(app_conn, world.space, sender["peer_id"], req_obj, ledger=tiny_ledger)
        cap2 = send_hints.evaluate_hints(app_conn, world.space, sender["peer_id"], req_obj, ledger=tiny_ledger)
    assert [h["key"] for h in cap1["hints"]] == [active_card] and cap1["dropped"]["daily_cap"] == 1
    assert cap2["hints"] == [] and cap2["dropped"]["daily_cap"] == 2

    send_hints.forget(); world.rerank_host.rerank_default = 1.0
    r_low = client.post("/v1/mesh/hints", data=json.dumps(req_payload), content_type="application/json", **_bearer(sender["peer_id"]))
    assert r_low.status_code == 200 and r_low.json()["hints"] == [] and r_low.json()["dropped"]["below_threshold"] == 2

    world.rerank_host.rerank_default = 5.0
    r_after = client.post("/v1/mesh/hints", data=json.dumps(req_payload), content_type="application/json", **_bearer(sender["peer_id"]))
    assert {h["key"] for h in r_after.json()["hints"]} == {active_card, good_mesh["key"]}

    send_hints.forget(); world.rerank_host.rerank_mode = "down"
    r_down = client.post("/v1/mesh/hints", data=json.dumps(req_payload), content_type="application/json", **_bearer(sender["peer_id"]))
    assert r_down.status_code == 200 and r_down.json()["hints"] == [] and r_down.json()["reranked"] is False and r_down.json()["abstained_reason"] == "rerank_unavailable"

def test_hints_rejects_unidirectional_mirage_chunks_rerank_and_fails_closed_mid_reverse(owner, world):
    client, grp = Client(), _memory_authority()["group"]
    sender, stranger = db.peer(owner, "alpha", group=grp, key=True), db.peer(owner, "bravo", group=grp, key=True)
    db.grant(owner, sender, "read")

    topic = "ctxindex shard eviction budget threshold calibration"
    mirage_body = f"{topic} asymmetric forward mirage candidate padding text for length check"
    anchor_body = f"{topic} corroborated fix in ctxindex.py (#412) verified across shards"
    mirage_row = db.row(owner, stranger, {"schema_version": 1, "from": "bravo", "to": ["charlie"], "thread_id": "m-th", "type": "note", "body": mirage_body}, embed_policy="allowed")
    anchor_row = db.row(owner, stranger, {"schema_version": 1, "from": "bravo", "to": ["charlie"], "thread_id": "a-th", "type": "note", "body": anchor_body}, embed_policy="allowed")
    for idx in range(3):
        db.row(owner, stranger, {"schema_version": 1, "from": "bravo", "to": ["charlie"], "thread_id": f"bg-{idx}",
                                 "type": "note", "body": f"{topic} background survivor {idx} with eighty characters total now"}, embed_policy="allowed")
    _drain(owner, world)

    draft = f"Checking {topic} in ctxindex.py (#412) before submitting branch."
    norm_d = send_hints.normalize_rerank_text(draft)
    world.rerank_host.rerank_default, world.rerank_host.rerank_refuse = 0.2, lambda _q, docs: len(docs) > 2
    world.rerank_host.rerank_table = {
        (norm_d, send_hints.normalize_rerank_text(mirage_body)): 4.5, (send_hints.normalize_rerank_text(mirage_body), norm_d): -0.2,
        (norm_d, send_hints.normalize_rerank_text(anchor_body)): 1.2, (send_hints.normalize_rerank_text(anchor_body), norm_d): 1.2,
    }
    r_ok = client.post("/v1/mesh/hints", data=json.dumps({"body": draft, "to": ["delta"], "sender_label": "alpha"}), content_type="application/json", **_bearer(sender["peer_id"]))
    body_ok = r_ok.json()
    assert r_ok.status_code == 200 and body_ok["reranked"] is True
    assert [h["key"] for h in body_ok["hints"]] == [anchor_row["key"]] and mirage_row["key"] not in {h["key"] for h in body_ok["hints"]}
    assert body_ok["hints"][0]["rerank_score"] == pytest.approx(1.45, abs=1e-4)

    r_rcpt = client.post("/v1/mesh/hints", data=json.dumps({
        "body": "DONE\nYour signed ACK reached this Codex session as a queued notice without an inbox poll.",
        "to": ["delta"], "sender_label": "alpha"}), content_type="application/json", **_bearer(sender["peer_id"]))
    assert r_rcpt.status_code == 200 and r_rcpt.json()["abstained_reason"] == "ack_or_short_draft"

    send_hints.forget()
    world.rerank_host.rerank_refuse, world.rerank_host.rerank_fail_after, world.rerank_host.rerank_fail_mode = None, 2, "down"
    r_mid = client.post("/v1/mesh/hints", data=json.dumps({"body": draft, "to": ["delta"], "sender_label": "alpha"}), content_type="application/json", **_bearer(sender["peer_id"]))
    assert r_mid.status_code == 200 and r_mid.json()["reranked"] is False and r_mid.json()["abstained_reason"] == "rerank_unavailable" and r_mid.json()["hints"] == []
    assert not send_hints._DAILY_LEDGER.seen("alpha", anchor_row["key"], _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))

    # Concurrent overlapping evaluate_hints calls for the same sender claim each target atomically at most once
    import threading
    from apps.core import dbconn
    world.rerank_host.rerank_fail_after, world.rerank_host.rerank_fail_mode = None, None
    ledger_race = send_hints.DailyHintLedger()
    barrier = threading.Barrier(2)
    orig_chunked = send_hints._chunked_rerank
    def synced_chunked(*a, **kw):
        res = orig_chunked(*a, **kw)
        barrier.wait(timeout=5.0)
        return res
    results: list[dict] = []
    def worker():
        with dbconn.connect() as c_thr:
            req_obj = send_hints.parse_request(json.dumps({"body": draft, "to": ["delta"], "sender_label": "alpha"}).encode("utf-8"))
            results.append(send_hints.evaluate_hints(c_thr, world.space, sender["peer_id"], req_obj, ledger=ledger_race))
    send_hints._chunked_rerank = synced_chunked
    try:
        t1, t2 = threading.Thread(target=worker), threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(10); t2.join(10)
    finally:
        send_hints._chunked_rerank = orig_chunked
    shown_lists = [[h["key"] for h in r["hints"]] for r in results]
    assert sorted(shown_lists) == [[], [anchor_row["key"]]]
    assert sum(r["dropped"]["daily_cap"] for r in results) == 1


def _plant_ctx_batch(conn, ns: str, writer: dict, *, seat: str, src: str, texts: list[tuple[str, str]], time_str: str) -> str:
    import hashlib
    entries = []
    for idx, (kind, text) in enumerate(texts):
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        val = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": 1, "batch": 1,
               "ordinal": idx, "item": kind, "dialect": "claude", "adapter_version": "1.23",
               "time": time_str, "text": text, "text_sha256": sha}
        sr.insert(conn, sr.row(writer["peer_id"], writer["owner_id"], ns, f"ctx/{seat}/{src}/0001/000001/{idx:010d}", val, author_label=seat))
        entries.append([idx, sha])
    manifest = {"schema": "context.v1", "kind": "manifest", "seat": seat, "src": src, "gen": 1, "batch": 1,
                "first": 0, "last": len(texts) - 1, "items": entries,
                "counts": {"written": len(texts), "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 0}}
    sr.insert(conn, sr.row(writer["peer_id"], writer["owner_id"], ns, f"ctxm/{seat}/{src}/0001/000001", manifest, author_label=seat))
    return f"ctx/{seat}/{src}/0001/000001/0000000000"


def test_hints_context_and_card_read_grants_precede_state_flags_and_surface_pointer_only(owner, world, monkeypatch):
    from apps.core import dbconn
    from apps.meshsem import ctxjob
    client, grp, ctx_ns = Client(), _memory_authority()["group"], f"shared/ctxhints-{uuid.uuid4().hex[:8]}"
    ungranted = db.peer(owner, "mailonly", group=grp, key=True)
    ctx_reader = db.peer(owner, "ctxreader", group=grp, key=True)
    ctx_writer = db.peer(owner, "dxxxxx", group=grp, key=True)
    db.grant(owner, ungranted, "read"); db.grant(owner, ctx_reader, "read")

    with owner.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id, claimant_plane,"
                    " ephemeral, retention_policy) VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                    (ctx_ns, ctx_writer["peer_id"], ctx_writer["owner_id"]))
    _grant_ns(owner, ctx_ns, ctx_reader)

    config_lock = (1835233133, 990)
    owner.execute("SELECT pg_advisory_lock(%s, %s)", config_lock)
    prior_ns = owner.execute("SELECT namespace FROM ctx_index_config WHERE id = 1").fetchone()[0]
    try:
        owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (ctx_ns,))
        monkeypatch.setattr(ctxjob, "_mail_behind", lambda _cur: False)
        monkeypatch.setattr(ctxjob, "_yield_since", {})
        monkeypatch.setattr(ctxjob, "_deadline_stuck", {})

        topic = "ctxindex shard prefilter budget regression calibration verified"
        secret_ctx = "SECRET_TRANSCRIPT_TURN_DO_NOT_LEAK"
        recent_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _plant_card(owner, world.card_ns, ctx_writer, seat="kxxx", ctype="Ruling", text_body=f"{topic} active ruling card", time_str=recent_iso)
        dxxxxx_ctx_key = _plant_ctx_batch(owner, ctx_ns, ctx_writer, seat="dxxxxx", src=uuid.uuid4().hex, time_str=recent_iso,
                                          texts=[("user_text", f"Investigate {topic}"), ("assistant_text", f"Patched {topic} {secret_ctx}")])
        _plant_ctx_batch(owner, ctx_ns, ctx_reader, seat="ctxreader", src=uuid.uuid4().hex, time_str=recent_iso,
                         texts=[("user_text", f"My own turn on {topic}"), ("assistant_text", f"Self session {topic} {secret_ctx}")])

        world.declare(include_context=True, ctx_ns=ctx_ns, ctx_index=True, forbidden_rows=True, walk_cards=True)
        _drain(owner, world)
        with dbconn.connect() as app_conn:
            assert ctxjob.catch_up(app_conn, space=world.space).manifests_indexed == 2

        payload = json.dumps({"body": f"Checking {topic} before pushing a new branch.", "to": ["pxx"], "sender_label": "mailonly"})
        for inc, idx_on, gate_on, walk_c in [(False, False, False, False), (True, False, True, True),
                                             (True, True, False, True), (True, True, True, True)]:
            send_hints.forget()
            world.declare(include_context=inc, ctx_ns=ctx_ns, ctx_index=idx_on, forbidden_rows=gate_on, walk_cards=walk_c)
            resp = client.post("/v1/mesh/hints", data=payload, content_type="application/json", **_bearer(ungranted["peer_id"])).json()
            assert resp["hints"] == []
            assert resp["sources"]["decisions"] == {"ran": False, "reason": "permission_denied"}
            assert resp["sources"]["transcript_context"] == {"ran": False, "reason": "permission_denied"}

        ctx_payload = json.dumps({"body": f"Checking {topic} before pushing a new branch.", "to": ["pxx"], "sender_label": "ctxreader"})
        for inc, idx_on, gate_on, exp_reason in [(False, True, True, "disabled"), (True, False, True, "index_off"), (True, True, False, "gate_closed")]:
            send_hints.forget()
            world.declare(include_context=inc, ctx_ns=ctx_ns, ctx_index=idx_on, forbidden_rows=gate_on, walk_cards=True)
            r_flag = client.post("/v1/mesh/hints", data=ctx_payload, content_type="application/json", **_bearer(ctx_reader["peer_id"])).json()
            assert r_flag["sources"]["transcript_context"] == {"ran": False, "reason": exp_reason}
            assert r_flag["sources"]["decisions"] == {"ran": False, "reason": "permission_denied"}

        send_hints.forget()
        world.declare(include_context=True, ctx_ns=ctx_ns, ctx_index=True, forbidden_rows=True, walk_cards=True)
        r_hit = client.post("/v1/mesh/hints", data=ctx_payload, content_type="application/json", **_bearer(ctx_reader["peer_id"])).json()
        assert [h["key"] for h in r_hit["hints"]] == [dxxxxx_ctx_key]
        assert r_hit["hints"][0]["source"] == "transcript_context" and r_hit["hints"][0]["kind"] == "transcript_session"
        assert r_hit["dropped"]["same_author_or_addressed"] >= 1
        assert secret_ctx not in json.dumps(r_hit) and "text" not in r_hit["hints"][0] and "body" not in r_hit["hints"][0]
    finally:
        owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (prior_ns,))
        owner.execute("SELECT pg_advisory_unlock(%s, %s)", config_lock)


def test_hints_freshness_cutoff_card_anchor_exclusion_and_author_peer_drop(owner, world):
    client, grp = Client(), _memory_authority()["group"]
    sender, stranger, card_writer = db.peer(owner, "alpha2", group=grp, key=True), db.peer(owner, "dxxxxx2", group=grp, key=True), db.peer(owner, "kxxx2", group=grp, key=True)
    db.grant(owner, sender, "read"); _grant_ns(owner, world.card_ns, sender)

    now = _dt.datetime.now(_dt.timezone.utc)
    topic = "ctxindex shard eviction budget threshold calibration"
    stale_row = db.row(owner, stranger, {"schema_version": 1, "from": "dxxxxx2", "to": ["pxx"], "thread_id": "stale-th",
                                         "type": "note", "body": f"{topic} stale row in ctxindex.py (#412) older than fourteen days"},
                       embed_policy="allowed", created_at=now - _dt.timedelta(days=15))
    fresh_row = db.row(owner, stranger, {"schema_version": 1, "from": "dxxxxx2", "to": ["pxx"], "thread_id": "fresh-th",
                                         "type": "note", "body": f"{topic} fresh row in ctxindex.py (#412) within fourteen days"},
                       embed_policy="allowed", created_at=now - _dt.timedelta(days=2))
    borderline_card = _plant_card(owner, world.card_ns, card_writer, seat="kxxx2", ctype="Decision",
                                  text_body=f"{topic} borderline card in ctxindex.py (#412) below two point zero",
                                  time_str=now.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    _drain(owner, world)

    # At rerank score 1.90: fresh_row gets +0.25 anchor bonus -> 2.15 >= 1.3 (kept), while borderline_card
    # does NOT get anchor bonus -> 1.90 < 2.0 (dropped), and stale_row is pre-dropped by 14-day freshness.
    world.rerank_host.rerank_default = 1.90
    draft = f"Checking {topic} in ctxindex.py (#412) before opening pull request."
    r = client.post("/v1/mesh/hints", data=json.dumps({"body": draft, "to": ["charlie"], "sender_label": "alpha2"}),
                    content_type="application/json", **_bearer(sender["peer_id"])).json()
    assert r["sources"]["mesh_chat"]["stale_dropped"] == 1
    assert [h["key"] for h in r["hints"]] == [fresh_row["key"]]
    assert stale_row["key"] not in {h["key"] for h in r["hints"]} and borderline_card not in {h["key"] for h in r["hints"]}
    assert r["dropped"]["below_threshold"] == 1

    # Direct author_peer == sender_peer drop when labels are completely disjoint (e.g. rotated handle).
    cand = send_hints._Candidate("mesh_chat", "shared/mesh-chat", "msg/999-old", "note", "legacy-handle",
                                 "peer-uuid-same", ("legacy-handle",), ("pxx",), "th-x", None, None,
                                 "eighty character candidate body text for author peer equality check now", "norm", 1.3)
    kept, dropped = send_hints.apply_precut_drops(
        [cand], req=send_hints.HintRequest(body=draft, to=("charlie",), sender_label="new-handle"),
        sender_peer="peer-uuid-same", sender_labels={"new-handle"}, sender_stems={"new-handle"},
        addressed_labels={"charlie"}, addressed_stems={"charlie"}, primary_sender_id="new-handle", utc_day="2026-11-05")
    assert kept == [] and dropped["same_author_or_addressed"] == 1


def test_hints_eval_must8_locality_proxy_bypass_and_redirect_refusal(monkeypatch, tmp_path):
    """MUST-8 on hints_eval: IP-literal loopback/RFC1918 only, HTTP_PROXY ignored, redirects not followed."""
    import http.server, threading, urllib.error, urllib.request
    hits: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)))
            hits.append(self.path)
            if self.path == "/redir/rerank":
                self.send_response(302); self.send_header("Location", "http://8.8.8.8/rerank"); self.end_headers(); return
            out = {"results": [{"index": i, "relevance_score": 2.5 - 0.1 * i} for i in range(len(body["documents"]))]}
            raw = json.dumps(out).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    class _ProxyHandler(http.server.BaseHTTPRequestHandler):
        proxy_hits = 0
        def log_message(self, *a): pass
        def do_POST(self):
            _ProxyHandler.proxy_hits += 1
            # Read the request body before answering. On Windows, closing a socket that still holds unread bytes
            # sends RST, and the client can then see WinError 10053 in place of this 502 (seen in gate mgu9).
            self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
            self.send_response(502); self.send_header("Content-Length", "0"); self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    proxy_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
    t1 = threading.Thread(target=srv.serve_forever, daemon=True); t1.start()
    t2 = threading.Thread(target=proxy_srv.serve_forever, daemon=True); t2.start()
    try:
        port, pport = srv.server_address[1], proxy_srv.server_address[1]
        for bad in (f"http://localhost:{port}", f"https://8.8.8.8:{port}",
                    f"http://u:p@127.0.0.1:{port}", f"http://127.0.0.1:{port}?x=1"):
            with pytest.raises(hints_eval.ContentRefused):
                hints_eval.http_rerank(bad, "q", [])
        assert hints_eval.main(["--base-url", "http://127.0.0.1:6310",
                                "--state-file", str(tmp_path / "missing.json"),
                                "--rerank-url", f"http://localhost:{port}"]) == 2
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{pport}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{pport}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        # Positive control: build_opener() (never urlopen(), which reuses/pollutes process-global _opener)
        # honours HTTP_PROXY, while hints_eval._content_opener() bypasses it.
        with pytest.raises(urllib.error.HTTPError) as pexc:
            urllib.request.build_opener().open(
                urllib.request.Request(f"http://127.0.0.1:{port}/rerank", data=b"{}", method="POST"), timeout=2)
        assert pexc.value.code == 502 and _ProxyHandler.proxy_hits == 1
        monkeypatch.setattr(hints_eval, "_OPENER", hints_eval._content_opener())
        scores = hints_eval.http_rerank(f"http://127.0.0.1:{port}", "query", ["doc-a", "doc-b"], timeout=3.0)
        assert scores == [2.5, 2.4] and _ProxyHandler.proxy_hits == 1
        hits.clear()
        with pytest.raises(urllib.error.HTTPError) as rexc:
            hints_eval.http_rerank(f"http://127.0.0.1:{port}/redir", "query", ["doc-a"], timeout=3.0)
        assert rexc.value.code == 302 and hits == ["/redir/rerank"]
    finally:
        srv.shutdown(); srv.server_close()
        proxy_srv.shutdown(); proxy_srv.server_close()


def test_hints_eval_live_benchmark_enforces_freshness_and_hydrates_metadata_exclusions(monkeypatch, tmp_path):
    """evaluate_live_benchmark drops stale (>14d) hits before read, and hydrates type/to/cc/signed_by via node.read."""
    now = _dt.datetime(2026, 11, 7, 18, 0, tzinfo=_dt.timezone.utc)
    fresh_ts = (now - _dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    drafts = (hints_eval.DEFAULT_BENCHMARK_DRAFTS[0],)
    rerank_calls, read_keys = [], []
    monkeypatch.setattr(hints_eval.node, "load_state", lambda sf, bu: {"peer_id": "p1", "label": "antigravity"})
    monkeypatch.setattr(hints_eval.cards, "search", lambda *a, **kw: {"hits": []})
    body = "When reading seat_directive run meshy_node directive-check --key K once and directive-done after."

    monkeypatch.setattr(hints_eval.node, "read", lambda sf, st, key: (read_keys.append(key) or {}))
    monkeypatch.setattr(hints_eval.node, "search", lambda *a, **kw: {"hits": [
        {"key": "msg/100-old", "author": "kxxx", "thread_id": "other-1", "recorded_at": "2026-10-01T00:00:00Z", "body": body},
        {"key": "msg/101-nots", "author": "kxxx", "thread_id": "other-2", "body": body},
    ]})
    monkeypatch.setattr(hints_eval, "http_rerank", lambda u, q, docs, **kw: (rerank_calls.append((q, docs)) or [3.0] * len(docs)))
    stale_out = hints_eval.evaluate_live_benchmark(tmp_path / "st.json", "http://127.0.0.1:6310",
                                                   "http://127.0.0.1:7876", drafts=drafts, now=now)
    assert stale_out["hints_shown"] == 0 and stale_out["precision"] is None and rerank_calls == [] and read_keys == []

    # Candidate._as_json omits type/to/cc/signed_by; node.read hydrates ACK, already-addressed to/cc, rotated self, 404, and valid hit.
    docs_by_key = {
        "msg/102-ack": {"key": "msg/102-ack", "author": "kxxx", "to": ["dxxxxx"], "thread_id": "t-ack", "type": "ack", "recorded_at": fresh_ts, "body": body},
        "msg/103-to-me": {"key": "msg/103-to-me", "author": "kxxx", "to": ["antigravity"], "thread_id": "t-to", "type": "note", "recorded_at": fresh_ts, "body": body},
        "msg/104-cc-stem": {"key": "msg/104-cc-stem", "author": "kxxx", "to": ["dxxxxx"], "cc": ["antigravity-prod"], "thread_id": "t-cc", "type": "note", "recorded_at": fresh_ts, "body": body},
        "msg/105-self-peer": {"key": "msg/105-self-peer", "author": "old-handle", "to": ["dxxxxx"], "thread_id": "t-self", "type": "note", "signed_by": "p1", "recorded_at": fresh_ts, "body": body},
        "msg/106-fresh": {"key": "msg/106-fresh", "author": "kxxx", "to": ["dxxxxx"], "thread_id": "other-3", "type": "note", "signed_by": "p2", "recorded_at": fresh_ts, "body": body},
    }
    def _fake_read(sf, st, key):
        read_keys.append(key)
        if key == "msg/107-gone": raise hints_eval.node.ServerSaid(404, {"code": "E_NOT_FOUND"})
        return docs_by_key[key]

    monkeypatch.setattr(hints_eval.node, "read", _fake_read)
    monkeypatch.setattr(hints_eval.node, "search", lambda *a, **kw: {"hits": [
        {"key": k, "author": "kxxx" if k != "msg/105-self-peer" else "old-handle", "thread_id": "th", "recorded_at": fresh_ts, "body": body}
        for k in ("msg/102-ack", "msg/103-to-me", "msg/104-cc-stem", "msg/105-self-peer", "msg/107-gone", "msg/106-fresh")
    ]})
    fresh_out = hints_eval.evaluate_live_benchmark(tmp_path / "st.json", "http://127.0.0.1:6310",
                                                   "http://127.0.0.1:7876", drafts=drafts, now=now)
    assert fresh_out["hints_shown"] == 1 and fresh_out["true_positives"] == 1 and fresh_out["precision"] == 1.0
    assert [h["key"] for h in fresh_out["items"]] == ["msg/106-fresh"]
    assert len(rerank_calls) == 2 and len(rerank_calls[0][1]) == 1
