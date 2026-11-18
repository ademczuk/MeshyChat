"""Issue #297: Context card type, bounded `refs` validation, one-head-per-seat supersession, and default search exclusion.

Every refusal has its control beside it: the same card with the one fault removed passes.

Run (pure, no DB):
  .venv/Scripts/python.exe -m pytest tests/test_cards_context.py -k pure --noconftest -q -p no:cacheprovider
Run (full with DB):
  .venv/Scripts/python.exe -m pytest tests/test_cards_context.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from tests.test_memory_skills import (  # noqa: F401 - fixtures used by name
    _call,
    _claim,
    _peer,
    app_conn,
    owner_conn,
)
from apps.cards import store as S  # noqa: E402
from apps.cards import validation as V  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from tools.meshy_node import cards as C  # noqa: E402

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
NS = "shared/decisions"


def _ctx_card(seat: str = "antigravity", *, text: str | None = None, time: str = "2026-10-14T11:00:00.000Z",
              card_type: str = "Context", owner: str | None = None, first_hand: bool = False,
              card_id: str | None = None, **more) -> tuple[str, dict]:
    card_id = card_id or uuid.uuid4().hex
    text = text if text is not None else f"{card_type} | owner: {seat} | seat: {seat} | working on #297"
    value = {
        "schema": "card.v1",
        "id": card_id,
        "type": card_type,
        "owner": seat if owner is None else owner,
        "seat": seat,
        "time": time,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source": {
            "record_uuid": str(uuid.uuid4()),
            "block_index": 0,
            "kind": "context_snapshot",
            "first_hand": first_hand,
        },
    }
    value.update(more)
    return f"card/{seat}/{card_id}", value


# --------------------------------------------------------------------------- pure validation (no DB needed)

def test_pure_context_type_is_in_server_and_client_types():
    assert "Context" in V.TYPES
    assert "Context" in C.TYPES
    assert set(C.TYPES) == set(V.TYPES)


def test_pure_context_card_requires_seat_owner_and_non_first_hand(monkeypatch):
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", NS)
    key, good = _ctx_card("antigravity")
    assert V.validate_card(NS, key, good, now=NOW) is True

    # A Context card is the seat's working-context snapshot, never the operator's decision.
    key_op, bad_op = _ctx_card("antigravity", owner="operator")
    with pytest.raises(HandlerError) as exc:
        V.validate_card(NS, key_op, bad_op, now=NOW)
    assert exc.value.code == "E_CARD_SCHEMA" and "owner is its seat" in exc.value.message

    # source.first_hand must be False on a Context card.
    key_fh, bad_fh = _ctx_card("antigravity", first_hand=True)
    with pytest.raises(HandlerError) as exc:
        V.validate_card(NS, key_fh, bad_fh, now=NOW)
    assert exc.value.code == "E_CARD_SCHEMA" and "source.first_hand must be false" in exc.value.message

    # Attribution on a Context card must have speaker == "agent".
    att_agent = {"speaker": "agent", "quoted_author": None, "extraction": "antigravity/context", "confirmation": "typed"}
    key_att, good_att = _ctx_card("antigravity", attribution=att_agent)
    assert V.validate_card(NS, key_att, good_att, now=NOW) is True

    att_op = {"speaker": "operator", "quoted_author": None, "extraction": "antigravity/context", "confirmation": "typed"}
    key_att_bad, bad_att = _ctx_card("antigravity", attribution=att_op)
    with pytest.raises(HandlerError) as exc:
        V.validate_card(NS, key_att_bad, bad_att, now=NOW)
    assert exc.value.code == "E_CARD_SCHEMA" and 'attribution.speaker "agent"' in exc.value.message


def test_pure_context_card_supersedes_at_most_one_card(monkeypatch):
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", NS)
    prev1 = f"card/antigravity/{'a' * 32}"
    prev2 = f"card/antigravity/{'b' * 32}"

    key1, one = _ctx_card("antigravity", supersedes=[prev1])
    assert V.validate_card(NS, key1, one, now=NOW) is True

    key2, two = _ctx_card("antigravity", supersedes=[prev1, prev2])
    with pytest.raises(HandlerError) as exc:
        V.validate_card(NS, key2, two, now=NOW)
    assert exc.value.code == "E_CARD_SUPERSEDES" and "at most 1 card" in exc.value.message

    # Control: a Ruling may still supersede two cards.
    r_key, r_val = _ctx_card("antigravity", card_type="Ruling", owner="operator", first_hand=True,
                             supersedes=[prev1, prev2])
    assert V.validate_card(NS, r_key, r_val, now=NOW) is True


@pytest.mark.parametrize("good_refs", [
    {},
    {"branch": None},
    {"branch": "antigravity/context-card-schema-297"},
    {"prs": [297, "#300"]},
    {"threads": ["meshychat-transcript-context-20261010", "node-awareness-20261007"]},
    {"files": ["apps/cards/validation.py", "apps/cards/store.py", "tools/meshy_node/cards.py"]},
    {
        "branch": "antigravity/context-card-schema-297",
        "prs": [295, "#297", 298, "#299", 300],
        "threads": ["meshychat-transcript-context-20261010"],
        "files": ["apps/cards/validation.py", "tests/test_cards_context.py"],
    },
])
def test_pure_refs_valid_shapes_are_accepted(monkeypatch, good_refs):
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", NS)
    key, value = _ctx_card("antigravity", refs=good_refs)
    assert V.validate_card(NS, key, value, now=NOW) is True


@pytest.mark.parametrize("bad_refs, snippet", [
    ("not-a-dict", "refs must be an object"),
    ([1, 2], "refs must be an object"),
    ({"unknown_key": 1}, "refs has unknown fields"),
    ({"branch": ""}, "refs.branch"),
    ({"branch": "has space"}, "refs.branch"),
    ({"branch": "has\nnewline"}, "refs.branch"),
    ({"branch": "x" * 121}, "refs.branch"),
    ({"branch": 123}, "refs.branch"),
    ({"prs": "297"}, "refs.prs"),
    ({"prs": [0]}, "refs.prs"),
    ({"prs": [-1]}, "refs.prs"),
    ({"prs": [True]}, "refs.prs"),
    ({"prs": [1_000_000]}, "refs.prs"),
    ({"prs": ["297"]}, "refs.prs"),
    ({"prs": ["#0"]}, "refs.prs"),
    ({"prs": [297, "#297"]}, "must not repeat a PR number"),
    ({"prs": list(range(1, 10))}, "refs.prs"),
    ({"threads": "not-a-list"}, "refs.threads"),
    ({"threads": ["has space"]}, "refs.threads"),
    ({"threads": ["t1", "t1"]}, "must not repeat a thread id"),
    ({"threads": [f"thread-{i}" for i in range(9)]}, "refs.threads"),
    ({"files": "apps/cards/validation.py"}, "refs.files"),
    ({"files": ["/etc/passwd"]}, "refs.files"),
    ({"files": ["C:\\Windows\\System32\\cmd.exe"]}, "refs.files"),
    ({"files": ["C:/Users/user/secret.txt"]}, "refs.files"),
    ({"files": ["\\\\server\\share\\file.py"]}, "refs.files"),
    ({"files": ["../secret.py"]}, "refs.files"),
    ({"files": ["apps/../secret.py"]}, "refs.files"),
    ({"files": ["./apps/cards/validation.py"]}, "refs.files"),
    ({"files": ["apps//cards/validation.py"]}, "refs.files"),
    ({"files": ["apps/cards/validation.py/"]}, "refs.files"),
    ({"files": ["apps/cards/file with space.py"]}, "refs.files"),
    ({"files": ["x" * 201]}, "refs.files"),
    ({"files": ["apps/a.py", "apps/a.py"]}, "must not repeat a path"),
    ({"files": [f"apps/f{i}.py" for i in range(17)]}, "refs.files"),
])
def test_pure_refs_invalid_shapes_are_refused_with_control(monkeypatch, bad_refs, snippet):
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", NS)
    key, bad = _ctx_card("antigravity", refs=bad_refs)
    with pytest.raises(HandlerError) as exc:
        V.validate_card(NS, key, bad, now=NOW)
    assert exc.value.code == "E_CARD_SCHEMA" and snippet in exc.value.message, (bad_refs, exc.value.message)
    # Control: replacing the bad refs with a valid one passes.
    good_key, good = _ctx_card("antigravity", refs={"branch": "main", "files": ["apps/cards/validation.py"]})
    assert V.validate_card(NS, good_key, good, now=NOW) is True


def test_pure_matches_excludes_context_cards_unless_explicitly_requested():
    ctx_key, ctx_val = _ctx_card("antigravity")
    rul_key, rul_val = _ctx_card("antigravity", card_type="Ruling", owner="operator", first_hand=True)

    # Default search pool (types=()) excludes Context cards but keeps Ruling cards.
    assert S.matches(ctx_val, ctx_key, types=()) is False
    assert S.matches(rul_val, rul_key, types=()) is True

    # Explicit type=("Context",) includes Context cards and excludes Ruling cards.
    assert S.matches(ctx_val, ctx_key, types=("Context",)) is True
    assert S.matches(rul_val, rul_key, types=("Context",)) is False


# --------------------------------------------------------------------------- DB-backed enforcement (enforce_card & list_cards)

def test_db_context_card_one_active_head_per_seat_and_unchanged_guard(
    app_conn, owner_conn, monkeypatch
):
    from tests.test_cards_write import _accepted, _label, _refused
    from apps.memoryskills import handlers

    ns = f"shared/cards-{uuid.uuid4().hex[:12]}"
    group = uuid.uuid4()
    seat_a = {**_peer(owner_conn, "seat-a", trust_group=group), "group": group}
    seat_b = {**_peer(owner_conn, "seat-b", trust_group=group), "group": group}
    _claim(app_conn, seat_a, ns)
    _call(app_conn, seat_a, "memory.grant", {"namespace": ns, "grantee_peer_id": str(seat_b["peer_id"]),
                                             "permission": "write"})
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", ns)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    label_a = _label(seat_a)
    label_b = _label(seat_b)

    # 1. Seat A writes its first Context card.
    k1, c1 = _ctx_card(label_a, time="2026-10-14T09:00:00.000Z",
                       text=f"Context | owner: {label_a} | seat: {label_a} | working on PR 1",
                       refs={"branch": "antigravity/pr1", "prs": [297], "files": ["apps/cards/validation.py"]})
    assert _accepted(app_conn, seat_a, ns, k1, c1) == 1

    # 2. A second Context card for Seat A that forgets to supersede k1 is refused (one active head per seat).
    k2_no_sup, c2_no_sup = _ctx_card(label_a, time="2026-10-14T10:00:00.000Z",
                                     text=f"Context | owner: {label_a} | seat: {label_a} | working on PR 2")
    assert _refused(app_conn, seat_a, ns, k2_no_sup, c2_no_sup) == "E_CARD_SUPERSEDES"

    # 3. A second Context card for Seat A that supersedes k1 but has identical text_sha256 and refs is refused (E_CARD_UNCHANGED).
    k2_same, c2_same = _ctx_card(label_a, time="2026-10-14T10:00:00.000Z",
                                 text=c1["text"], refs=dict(c1["refs"]), supersedes=[k1])
    assert _refused(app_conn, seat_a, ns, k2_same, c2_same) == "E_CARD_UNCHANGED"

    # 4. Changing refs alone (or text) while superseding k1 succeeds, and leaves only k2 active.
    k2, c2 = _ctx_card(label_a, time="2026-10-14T10:00:00.000Z",
                       text=c1["text"],
                       refs={"branch": "antigravity/pr1", "prs": [297, 300], "files": ["apps/cards/validation.py"]},
                       supersedes=[k1])
    assert _accepted(app_conn, seat_a, ns, k2, c2) == 1

    # 5. Cross-seat and cross-type supersession guards:
    # Seat B cannot supersede Seat A's Context card (neither with a Context card nor with a recorded decision Ruling).
    kb_bad, cb_bad = _ctx_card(label_b, time="2026-10-14T10:30:00.000Z", supersedes=[k2])
    assert _refused(app_conn, seat_b, ns, kb_bad, cb_bad) == "E_CARD_SUPERSEDES"

    rb_key, rb_val = _ctx_card(label_b, card_type="Ruling", owner="operator", first_hand=True,
                               time="2026-10-14T10:30:00.000Z", supersedes=[k2])
    assert _refused(app_conn, seat_b, ns, rb_key, rb_val) == "E_CARD_SUPERSEDES"

    # Seat A cannot supersede a non-Context card with a Context card, nor supersede its Context card with a Decision.
    da_key, da_val = _ctx_card(label_a, card_type="Decision", owner=label_a, first_hand=True,
                               time="2026-10-14T08:00:00.000Z", text=f"Decision | owner: {label_a} | earlier")
    assert _accepted(app_conn, seat_a, ns, da_key, da_val) == 1

    k3_bad_target, c3_bad_target = _ctx_card(label_a, time="2026-10-14T11:00:00.000Z",
                                             text=f"Context | owner: {label_a} | step 3", supersedes=[da_key])
    assert _refused(app_conn, seat_a, ns, k3_bad_target, c3_bad_target) == "E_CARD_SUPERSEDES"

    da2_key, da2_val = _ctx_card(label_a, card_type="Decision", owner=label_a, first_hand=True,
                                 time="2026-10-14T11:00:00.000Z", text=f"Decision | owner: {label_a} | later",
                                 supersedes=[k2])
    assert _refused(app_conn, seat_a, ns, da2_key, da2_val) == "E_CARD_SUPERSEDES"

    # 6. Re-versioning k2 with an explicit `supersedes: []` is refused (E_CARD_SUPERSEDES), because dropping
    #    the edge to k1 would leave both k1 and k2 active (GitHub Codex P1 on #302, validation.py:492).
    #    Control: re-versioning k2 while keeping `supersedes: [k1]` succeeds and leaves only k2 active.
    c2_drop_sup = {**c2, "supersedes": []}
    assert _refused(app_conn, seat_a, ns, k2, c2_drop_sup) == "E_CARD_SUPERSEDES"
    c2_v2_text = f"Context | owner: {label_a} | seat: {label_a} | working on PR 1 and PR 2"
    c2_v2 = {
        **c2,
        "text": c2_v2_text,
        "text_sha256": hashlib.sha256(c2_v2_text.encode("utf-8")).hexdigest(),
        "supersedes": [k1],
    }
    assert _accepted(app_conn, seat_a, ns, k2, c2_v2) == 2

    # 7. Seat B writes its own Context card: GET /v1/cards?type=Context&status=active returns one active head per seat.
    kb, cb = _ctx_card(label_b, time="2026-10-14T10:30:00.000Z",
                       text=f"Context | owner: {label_b} | seat: {label_b} | reviewing PRs")
    assert _accepted(app_conn, seat_b, ns, kb, cb) == 1

    active_ctx = S.list_cards(owner_conn, ns, types=("Context",), status="active")["cards"]
    assert [c["key"] for c in active_ctx] == [kb, k2]
    superseded_ctx = S.list_cards(owner_conn, ns, types=("Context",), status="superseded")["cards"]
    assert [c["key"] for c in superseded_ctx] == [k1]
    owner_conn.commit()


def test_db_context_card_concurrent_writes_serialize_to_one_active_head(
    app_conn, owner_conn, monkeypatch
):
    """Two concurrent writers for the same (namespace, seat) serialize on the per-(namespace, seat)
    advisory lock (`pg_advisory_xact_lock`): both when creating the first Context card and when two
    writers concurrently supersede the same existing head, exactly one succeeds and the second sees
    the committed head and is refused with E_CARD_SUPERSEDES (GitHub Codex P1 on #302, validation.py:506)."""
    import threading
    from tests.test_cards_write import _label
    from tests.test_memory_skills import DSN
    from apps.memoryskills import db, handlers

    ns = f"shared/cards-{uuid.uuid4().hex[:12]}"
    group = uuid.uuid4()
    seat_a = {**_peer(owner_conn, "seat-race", trust_group=group), "group": group}
    _claim(app_conn, seat_a, ns)
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", ns)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    label_a = _label(seat_a)

    def _run_concurrent_pair(card_1: tuple[str, dict], card_2: tuple[str, dict]) -> list[str]:
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(item: tuple[str, dict]) -> None:
            k, v = item
            conn = db.connect(DSN)
            try:
                barrier.wait(timeout=5)
                try:
                    res = _call(conn, seat_a, "memory.write", {"namespace": ns, "key": k, "value": v})
                    outcome = f"ok:{res['version']}"
                except HandlerError as exc:
                    outcome = exc.code
                with lock:
                    outcomes.append(outcome)
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=(card_1,))
        t2 = threading.Thread(target=worker, args=(card_2,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        return sorted(outcomes)

    # Race 1: two concurrent first-Context writes for the same seat (no existing row for FOR UPDATE to lock).
    c1a = _ctx_card(label_a, time="2026-10-14T09:00:00.000Z",
                    text=f"Context | owner: {label_a} | seat: {label_a} | first A")
    c1b = _ctx_card(label_a, time="2026-10-14T09:01:00.000Z",
                    text=f"Context | owner: {label_a} | seat: {label_a} | first B")
    assert _run_concurrent_pair(c1a, c1b) == ["E_CARD_SUPERSEDES", "ok:1"]

    active_after_1 = S.list_cards(owner_conn, ns, types=("Context",), status="active")["cards"]
    assert len(active_after_1) == 1
    winner_key = active_after_1[0]["key"]

    # Race 2: two concurrent writes that both supersede `winner_key` (distinct new keys).
    c2a = _ctx_card(label_a, time="2026-10-14T10:00:00.000Z",
                    text=f"Context | owner: {label_a} | seat: {label_a} | second A",
                    supersedes=[winner_key])
    c2b = _ctx_card(label_a, time="2026-10-14T10:01:00.000Z",
                    text=f"Context | owner: {label_a} | seat: {label_a} | second B",
                    supersedes=[winner_key])
    assert _run_concurrent_pair(c2a, c2b) == ["E_CARD_SUPERSEDES", "ok:1"]

    active_after_2 = S.list_cards(owner_conn, ns, types=("Context",), status="active")["cards"]
    assert len(active_after_2) == 1
    second_winner = active_after_2[0]["key"]

    # Race 3 (pxx P1 on #302): Context chain A -> B -> C (where B == second_winner supersedes A == winner_key,
    # and C supersedes B). T1 re-versions B (keeping `supersedes: [winner_key]`) and holds its transaction open
    # just after `enforce_card`; T2 concurrently writes a `Decision` at B's key with no `supersedes`.
    # Without the top-of-`enforce_card` advisory lock (`hashtext('cards:' || ns)`), T2 blocks on B v1's
    # `FOR UPDATE`, re-evaluates `WHERE is_head` after T1 commits B v2, gets `head_row = None`, skips frozen-type
    # and supersedes checks, and overwrites B as a Decision with no `supersedes`, uncovering A so both A and C
    # become active Context heads. With the lock at the top of `enforce_card`, T2 sees B v2 and is refused
    # with `E_CARD_FROZEN`, leaving C as the sole active Context head.
    b_row = owner_conn.execute(
        "SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head",
        (ns, second_winner),
    ).fetchone()[0]
    c3_key, c3_val = _ctx_card(
        label_a,
        time="2026-10-14T11:00:00.000Z",
        text=f"Context | owner: {label_a} | seat: {label_a} | third C",
        supersedes=[second_winner],
    )
    assert _call(app_conn, seat_a, "memory.write", {"namespace": ns, "key": c3_key, "value": c3_val})["version"] == 1

    b_v2_text = f"Context | owner: {label_a} | seat: {label_a} | second B revised"
    b_v2 = {
        **b_row,
        "text": b_v2_text,
        "text_sha256": hashlib.sha256(b_v2_text.encode("utf-8")).hexdigest(),
        "supersedes": [winner_key],
    }
    b_bad_decision = {
        **b_row,
        "type": "Decision",
        "text": f"Decision | owner: {label_a} | seat: {label_a} | overwrite B as Decision",
        "text_sha256": hashlib.sha256(
            f"Decision | owner: {label_a} | seat: {label_a} | overwrite B as Decision".encode("utf-8")
        ).hexdigest(),
    }
    b_bad_decision.pop("supersedes", None)

    import time as _time
    t1_entered = threading.Event()
    real_ret_policy = handlers._namespace_retention_policy

    def _slow_ret_policy_for_t1(conn, namespace):
        if threading.current_thread().name == "t1-reversion-b":
            t1_entered.set()
            _time.sleep(0.08)
        return real_ret_policy(conn, namespace)

    monkeypatch.setattr(handlers, "_namespace_retention_policy", _slow_ret_policy_for_t1)
    race3_outcomes: dict[str, str] = {}

    def run_t1() -> None:
        conn = db.connect(DSN)
        try:
            res = _call(conn, seat_a, "memory.write", {"namespace": ns, "key": second_winner, "value": b_v2})
            race3_outcomes["t1"] = f"ok:{res['version']}"
        except HandlerError as exc:
            race3_outcomes["t1"] = exc.code
        finally:
            conn.close()

    def run_t2() -> None:
        conn = db.connect(DSN)
        try:
            t1_entered.wait(timeout=5)
            res = _call(conn, seat_a, "memory.write", {"namespace": ns, "key": second_winner, "value": b_bad_decision})
            race3_outcomes["t2"] = f"ok:{res['version']}"
        except HandlerError as exc:
            race3_outcomes["t2"] = exc.code
        finally:
            conn.close()

    th1 = threading.Thread(target=run_t1, name="t1-reversion-b")
    th2 = threading.Thread(target=run_t2, name="t2-decision-b")
    th1.start()
    th2.start()
    th1.join(timeout=10)
    th2.join(timeout=10)

    assert race3_outcomes == {"t1": "ok:2", "t2": "E_CARD_FROZEN"}
    active_after_3 = S.list_cards(owner_conn, ns, types=("Context",), status="active")["cards"]
    assert [c["key"] for c in active_after_3] == [c3_key]
    owner_conn.commit()
