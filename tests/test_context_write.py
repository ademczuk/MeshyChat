"""context.v1: what memory.write refuses in the transcript-context namespace (apps/context, docs/design/context-v1.md).

Every refusal has its control beside it: the same value with the one fault removed is written. A test that only showed
refusals would pass on a namespace that refused everything. The secrets and client names here are synthetic.

Run:  .venv/Scripts/python.exe -m pytest tests/test_context_write.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_memory_skills import (  # noqa: F401 - fixtures are used by name; sets up Django
    DSN,
    _call,
    _claim,
    _peer,
    app_conn,
    owner_conn,
)
from apps.context import screens as S  # noqa: E402
from apps.context import validation as V  # noqa: E402
from apps.memoryskills import db, handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402

SRC = "0123456789abcdef0123456789abcdef"
#: Synthetic: no real client is named anywhere in this file.
MARKERS = ["Zorblax Corp", "quintessa-labs"]
SECRET = "AKIA" + "QWERTYUIOPASDFGH"           # the loader's vendor_key rule
CLEAN = "The deploy went out at ten and the health check is green. "
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"


def opaque(native_id: str) -> str:
    """How a writer stores an id: the first 32 hex of its sha256, never the id itself."""
    return hashlib.sha256(native_id.encode("utf-8")).hexdigest()[:32]


def _label(peer) -> str:
    return f"peer-{str(peer['peer_id'])[:8]}"


@pytest.fixture()
def ctx(app_conn, owner_conn, monkeypatch, tmp_path):
    """A claimed context namespace, a seat that may write it, its label, and a configured denylist."""
    ns = f"shared/context-{uuid.uuid4().hex[:12]}"
    group = uuid.uuid4()
    seat = {**_peer(owner_conn, "seat", trust_group=group), "group": group}
    _claim(app_conn, seat, ns)
    deny = tmp_path / "denylist.json"
    deny.write_text(json.dumps(MARKERS), encoding="utf-8")
    monkeypatch.setenv("MESHY_CONTEXT_NAMESPACE", ns)
    monkeypatch.setenv("MESHY_CONTEXT_DENYLIST", str(deny))
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    assert V.namespace() == ns
    return ns, seat, _label(seat)


def item(seat, ordinal, *, batch=1, gen=1, kind="assistant_text", text=CLEAN, **more):
    v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": SRC, "gen": gen, "batch": batch,
         "ordinal": ordinal, "item": kind, "dialect": "claude", "adapter_version": "1.23", "time": NOW}
    if kind in ("user_text", "assistant_text"):
        v["role"] = "user" if kind == "user_text" else "assistant"
    if kind != "omitted":
        v["text"] = text
        v["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    v.update(more)
    return f"ctx/{seat}/{SRC}/{gen:04d}/{batch:06d}/{ordinal:010d}", v


def task(seat, ordinal, *, step="ship the validator", event="added", **more):
    return item(seat, ordinal, kind="task", text=f"[{event}] {step}", step=step, event=event, **more)


def omitted(seat, ordinal, reason, **more):
    extra = {"omitted_bytes": 70_000} if reason == "too_large" else {}
    extra.update(more)
    return item(seat, ordinal, kind="omitted", omitted_reason=reason, **extra)


def manifest(seat, batch, first, entries, *, gen=1, counts=None):
    """entries: the stored item values in ordinal order (None digest for an omitted one)."""
    items = [[first + i, e.get("text_sha256")] for i, e in enumerate(entries)]
    if counts is None:
        counts = {"written": 0, "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 0}
        for e in entries:
            counts["written" if e["item"] != "omitted" else "omitted_" + e["omitted_reason"]] += 1
    v = {"schema": "context.v1", "kind": "manifest", "seat": seat, "src": SRC, "gen": gen, "batch": batch,
         "first": first, "last": first + len(entries) - 1, "items": items, "counts": counts}
    return f"ctxm/{seat}/{SRC}/{gen:04d}/{batch:06d}", v


def _write(conn, peer, ns, key, value, **args):
    return _call(conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value, **args})


def refused(conn, peer, ns, key, value, **args) -> str:
    with pytest.raises(HandlerError) as exc:
        _write(conn, peer, ns, key, value, **args)
    if exc.value.code == "E_INTERNAL":
        raise AssertionError("E_INTERNAL: " + str(exc.value.message)) from exc.value
    return exc.value.code


def accepted(conn, peer, ns, key, value) -> int:
    try:
        return _write(conn, peer, ns, key, value)["version"]
    except HandlerError as exc:
        assert False, f"expected the write to be accepted; refused with {exc.code}: {exc.message}"


def rows(conn, ns, key) -> int:
    return conn.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND key = %s", (ns, key)).fetchone()[0]


def write_batch(conn, peer, ns, label, batch, first, values):
    for i, v in enumerate(values):
        key = f"ctx/{label}/{SRC}/0001/{batch:06d}/{first + i:010d}"
        assert v["ordinal"] == first + i and v["batch"] == batch
        accepted(conn, peer, ns, key, v)
    return manifest(label, batch, first, values)


# --------------------------------------------------------------------------- items

def test_each_item_kind_is_written_once_and_reads_back(app_conn, owner_conn, ctx):
    ns, seat, label = ctx
    cases = [item(label, 0, kind="user_text"), item(label, 1), task(label, 2),
             task(label, 3, event="ambiguous", counts={"pending": 1, "in_progress": 0, "completed": 1}),
             item(label, 4, kind="artifact", agent_id=opaque("sub-1")), item(label, 5, kind="artifact"),
             omitted(label, 6, "secret"), omitted(label, 7, "client_name"), omitted(label, 8, "too_large")]
    for key, value in cases:
        assert accepted(app_conn, seat, ns, key, value) == 1
        head = owner_conn.execute("SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head",
                                  (ns, key)).fetchone()[0]
        assert head == value


def test_the_rules_bind_the_context_namespace_only(app_conn, owner_conn, ctx):
    """Control for every refusal: a malformed value is an ordinary row in another namespace."""
    ns, seat, label = ctx
    other = f"shared/plain-{uuid.uuid4().hex[:8]}"
    _claim(app_conn, seat, other)
    key, value = item(label, 0, text_sha256="0" * 64)
    assert accepted(app_conn, seat, other, key, value) == 1
    assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_DIGEST"


@pytest.mark.parametrize("change, code", [
    ({"text_sha256": "0" * 64}, "E_CONTEXT_DIGEST"),
    ({"surprise": 1}, "E_CONTEXT_SCHEMA"),
    ({"redacted": [0]}, "E_CONTEXT_SCHEMA"),
    ({"schema": "context.v2"}, "E_CONTEXT_SCHEMA"),
    ({"kind": "manifest"}, "E_CONTEXT_SCHEMA"),
    ({"ordinal": 1}, "E_CONTEXT_SCHEMA"),
    ({"gen": True}, "E_CONTEXT_SCHEMA"),
    ({"role": "user"}, "E_CONTEXT_SCHEMA"),
    ({"agent_id": opaque("sub-1")}, "E_CONTEXT_SCHEMA"),   # a well-formed agent_id, on a non-artifact item
    ({"dialect": "gpt"}, "E_CONTEXT_SCHEMA"),
    ({"adapter_version": ""}, "E_CONTEXT_SCHEMA"),
    ({"native_ref": "r" * 129}, "E_CONTEXT_SCHEMA"),
    ({"time": "2026-10-19T08:00:00Z"}, "E_CONTEXT_TIME"),
    ({"omitted_reason": "secret"}, "E_CONTEXT_SCHEMA"),
    ({"step": "x"}, "E_CONTEXT_SCHEMA"),
])
def test_one_fault_in_an_item_is_refused_and_writes_nothing(app_conn, owner_conn, ctx, change, code):
    ns, seat, label = ctx
    key, value = item(label, 0)
    assert refused(app_conn, seat, ns, key, {**value, **change}) == code
    assert rows(owner_conn, ns, key) == 0
    assert accepted(app_conn, seat, ns, key, value) == 1          # control: the same key without the fault


def test_a_time_more_than_five_minutes_ahead_is_refused(app_conn, ctx):
    ns, seat, label = ctx
    ahead = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
    key, value = item(label, 0, time=ahead)
    assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_TIME"
    soon = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
    assert accepted(app_conn, seat, ns, key, {**value, "time": soon}) == 1


def test_omitted_and_task_fields_stay_where_they_belong(app_conn, ctx):
    ns, seat, label = ctx
    bad = [omitted(label, 0, "secret", omitted_bytes=10),                    # a screened item keeps no size
           omitted(label, 0, "too_large", omitted_bytes=0),
           omitted(label, 0, "secret", text="leak", text_sha256=hashlib.sha256(b"leak").hexdigest()),
           task(label, 0, event="done"),
           task(label, 0, event="added", counts={"pending": 1, "in_progress": 0, "completed": 0}),
           task(label, 0, event="ambiguous"),
           task(label, 0, event="ambiguous", counts={"pending": 1, "in_progress": 0}),
           task(label, 0, step="s" * 1001)]
    for key, value in bad:
        assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_SCHEMA", value
    key, value = task(label, 0, event="ambiguous", counts={"pending": 0, "in_progress": 0, "completed": 2})
    assert accepted(app_conn, seat, ns, key, value) == 1


def _sized(label, target: int, unit: str):
    """An item whose serialized value is exactly `target` bytes, its text made of `unit` repeated (no long word runs:
    they keep these size tests fast, though since #347 the detectors are linear on them)."""
    key, value = item(label, 0, text="x")
    base = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) - len(b"x")
    per = len(json.dumps(unit, ensure_ascii=False).encode()) - 2
    n, rem = divmod(target - base, per)
    text = unit * n + " " * rem
    return item(label, 0, text=text)


@pytest.mark.parametrize("unit", ["ab ", '"q ', "été ", "中文 ", "\U0001f600 ", "\x01 "])
def test_the_budget_is_the_serialized_value(app_conn, owner_conn, ctx, unit):
    """At the budget the item is stored; one byte over it is refused. Quotes and control characters cost more once
    escaped, which is why a text-byte cap alone could not bound the value."""
    ns, seat, label = ctx
    key, value = _sized(label, V.MAX_VALUE_BYTES, unit)
    assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) == V.MAX_VALUE_BYTES
    assert accepted(app_conn, seat, ns, key, value) == 1
    key2, over = _sized(label, V.MAX_VALUE_BYTES + 1, unit)
    over = {**over, "ordinal": 1}
    key2 = key2[:-10] + "0000000001"
    assert refused(app_conn, seat, ns, key2, over) == "E_CONTEXT_TOO_LARGE"
    assert rows(owner_conn, ns, key2) == 0


def test_a_lone_surrogate_is_a_typed_refusal_not_a_crash(app_conn, ctx):
    """A signed call cannot carry one (RFC 8785 refuses it before the signature, so the test's own envelope fails),
    so the server check is a backstop: called directly, it is a typed refusal, never an exception."""
    ns, seat, label = ctx
    key, value = item(label, 0)
    with pytest.raises(Exception) as signing:
        _write(app_conn, seat, ns, key, {**value, "text": "half \ud800 a pair"})
    assert not isinstance(signing.value, HandlerError)            # refused by the canonicalizer, client side
    with pytest.raises(HandlerError) as exc:
        V.validate_context(ns, key, {**value, "text": "half \ud800 a pair"})
    assert exc.value.code == "E_CONTEXT_TEXT_ENCODING"
    clean = "half � a pair"
    assert accepted(app_conn, seat, ns, key, item(label, 0, text=clean)[1]) == 1


# --------------------------------------------------------------------------- write once, own seat

def test_every_key_is_written_once(app_conn, owner_conn, ctx):
    ns, seat, label = ctx
    key, value = item(label, 0)
    assert accepted(app_conn, seat, ns, key, value) == 1
    assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_WRITE_ONCE"          # even the same bytes
    assert refused(app_conn, seat, ns, key, item(label, 0, text="changed")[1]) == "E_CONTEXT_WRITE_ONCE"
    assert rows(owner_conn, ns, key) == 1
    assert accepted(app_conn, seat, ns, *item(label, 1)) == 1                        # control: a new key


def test_a_seat_writes_only_its_own_context(app_conn, owner_conn, ctx):
    ns, seat, label = ctx
    key, value = item("someone-else", 0)
    assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_SEAT_MISMATCH"
    assert refused(app_conn, seat, ns, *item(label, 0), author="someone-else") == "E_CONTEXT_SEAT_MISMATCH"
    assert refused(app_conn, seat, ns, *item(label, 0), status="OPEN") == "E_CONTEXT_SCHEMA"
    assert accepted(app_conn, seat, ns, *item(label, 0)) == 1


def test_two_racing_first_writes_leave_exactly_one_row(app_conn, owner_conn, ctx):
    """Different call_ids, one key: one is written, the other refused, and the uploader settles it by reading back."""
    ns, seat, label = ctx
    key, value = item(label, 0)
    results = []

    def go():
        conn = db.connect(DSN)
        try:
            results.append(_write(conn, seat, ns, key, value)["version"])
        except HandlerError as exc:
            results.append(exc.code)
        finally:
            conn.close()
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 2 and results.count(1) == 1, results
    assert [r for r in results if r != 1][0] in ("E_CONTEXT_WRITE_ONCE", "E_WRITE_CONFLICT"), results
    assert rows(owner_conn, ns, key) == 1


def _race(monkeypatch, ns, seat, key, value, *, lock=True, head_check=True):
    """Two first writes of one key, the second run while the first holds its transaction open just after enforce_context
    (memory-bxxxxx 8d4c0f's interleaving). Either guard can be turned off to test the other alone: two guards behind one
    code would otherwise hide each other from a mutant run."""
    if not lock:
        monkeypatch.setattr(V, "serialize", lambda *a, **k: None)
    if not head_check:
        monkeypatch.setattr(V, "refuse_existing_head", lambda *a, **k: None)
    real = V.enforce_context
    first_past = threading.Event()

    def enforce(conn, *a, **k):
        real(conn, *a, **k)
        if threading.current_thread().name == "first":
            first_past.set()
            time.sleep(1.5)                      # the second write runs inside this window
    monkeypatch.setattr(V, "enforce_context", enforce)
    results = {}

    def go(name):
        conn = db.connect(DSN)
        try:
            results[name] = _write(conn, seat, ns, key, value)["version"]
        except HandlerError as exc:
            results[name] = exc.code
        finally:
            conn.close()
    first = threading.Thread(target=go, args=("first",), name="first")
    first.start()
    assert first_past.wait(30), "the first write never reached enforce_context"
    second = threading.Thread(target=go, args=("second",), name="second")
    second.start()
    first.join(60)
    second.join(60)
    return results


@pytest.mark.parametrize("guard", ["lock", "head_check"])
@pytest.mark.parametrize("what", ["item", "batch-1 manifest"])
def test_a_racing_first_write_is_never_a_second_version(app_conn, owner_conn, ctx, monkeypatch, guard, what):
    """Each guard alone keeps one row: with only the lock the second waits and then sees the first; with only the head
    check the loser is refused at memory.write's own head read. Without both, the 50c92d90-to-fc1e9e7f gap wrote v2."""
    ns, seat, label = ctx
    if what == "item":
        key, value = item(label, 0)
    else:
        values = [item(label, 0)[1]]
        key, value = write_batch(app_conn, seat, ns, label, 1, 0, values)
    results = _race(monkeypatch, ns, seat, key, value, lock=(guard == "lock"), head_check=(guard == "head_check"))
    versions = [r[0] for r in owner_conn.execute(
        "SELECT version FROM shared_memory WHERE namespace = %s AND key = %s", (ns, key)).fetchall()]
    assert versions == [1], (results, versions)
    assert sorted(map(str, results.values())) in (["1", "E_CONTEXT_WRITE_ONCE"], ["1", "E_WRITE_CONFLICT"]), results


# --------------------------------------------------------------------------- the screens

@pytest.mark.parametrize("where", ["start", "middle", "end"])
@pytest.mark.parametrize("bad, code", [(SECRET, S.SECRET), ("Zorblax Corp", S.CLIENT_NAME),
                                       ("QUINTESSA labs", S.CLIENT_NAME), ("zor-blax corp", S.CLIENT_NAME)])
def test_a_screen_hit_anywhere_in_a_large_item_stores_nothing(app_conn, owner_conn, ctx, where, bad, code):
    ns, seat, label = ctx
    body = CLEAN * (60_000 // len(CLEAN))
    text = {"start": bad + " " + body, "middle": body[:30_000] + " " + bad + " " + body[30_000:],
            "end": body + " " + bad}[where]
    key, value = item(label, 0, text=text)
    assert refused(app_conn, seat, ns, key, value) == code
    assert rows(owner_conn, ns, key) == 0
    listed = _call(app_conn, seat, "memory.list", {"namespace": ns})
    assert key not in json.dumps(listed)
    # control: the same large item without the hit is stored
    assert accepted(app_conn, seat, ns, key, item(label, 0, text=body)[1]) == 1


def test_a_marker_only_in_a_task_step_refuses_the_task(app_conn, owner_conn, ctx):
    ns, seat, label = ctx
    key, value = task(label, 0, step="send the report to Zorblax Corp")
    value = {**value, "text": "[added] send the report", "text_sha256": hashlib.sha256(b"[added] send the report").hexdigest()}
    assert refused(app_conn, seat, ns, key, value) == S.CLIENT_NAME
    assert rows(owner_conn, ns, key) == 0
    assert accepted(app_conn, seat, ns, *task(label, 0, step="send the report")) == 1


def test_ids_are_opaque_so_no_text_rides_in_them(app_conn, owner_conn, ctx):
    """a reviewer's #346 review: ids are strings an item stores, so they must not carry text past the screens. They are
    the sha256 prefix of the native id (as src is), and adapter_version is a version: any raw id, name, secret or prose
    is refused by its shape, on an omitted item too."""
    ns, seat, label = ctx
    for field, bad in (("native_ref", "ZorblaxCorp-1"), ("native_ref", SECRET), ("native_ref", "toolu_01ABCDEFGHJKMNPQRSTVWXYZ"),
                       ("native_ref", "A" * 32), ("agent_id", "Zorblax-Corp"), ("agent_id", opaque("x")[:31]),
                       ("adapter_version", "quintessa-labs"), ("adapter_version", "1.23 beta")):
        for key, value in (item(label, 0, kind="artifact", **{field: bad}),) + (
                (omitted(label, 0, "secret", **{field: bad}),) if field != "agent_id" else ()):
            assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_SCHEMA", (field, bad)
            assert rows(owner_conn, ns, key) == 0
    # control: opaque ids and a real version are written
    assert accepted(app_conn, seat, ns, *item(label, 0, kind="artifact", agent_id=opaque("sub-1"),
                                              native_ref=opaque(str(uuid.uuid4())), adapter_version="1.23.4")) == 1


def test_a_refused_item_converges_to_an_omission_even_with_an_id_shape_the_detectors_match(app_conn, owner_conn, ctx):
    """memory-bxxxxx 683cd8, P1-2: the loader's detectors match real id shapes such as toolu_01..., so screening raw ids
    refused the omission row too and blocked the generation. With opaque ids the omission row is written."""
    ns, seat, label = ctx
    ref = opaque("toolu_01ABCDEFGHJKMNPQRSTVWXYZ")
    key, value = item(label, 0, text="the key is " + SECRET, native_ref=ref)
    assert refused(app_conn, seat, ns, key, value) == S.SECRET
    assert accepted(app_conn, seat, ns, *omitted(label, 0, "secret", native_ref=ref)) == 1
    k, m = manifest(label, 1, 0, [omitted(label, 0, "secret", native_ref=ref)[1]])
    assert accepted(app_conn, seat, ns, k, m) == 1


def test_a_context_row_takes_no_tags(app_conn, owner_conn, ctx):
    """Tags are stored beside the value, outside the screens and the budget (a reviewer, #346 review)."""
    ns, seat, label = ctx
    key, value = item(label, 0)
    assert refused(app_conn, seat, ns, key, value, tags=["Zorblax Corp"]) == "E_CONTEXT_SCHEMA"
    assert refused(app_conn, seat, ns, key, value, tags=[]) == "E_CONTEXT_SCHEMA"
    assert rows(owner_conn, ns, key) == 0
    assert accepted(app_conn, seat, ns, key, value) == 1


@pytest.mark.parametrize("deny", [None, "missing", "not json", "[]", '["ok"]', "[1]"])
def test_a_screen_that_cannot_answer_is_retried_never_an_omission(app_conn, owner_conn, ctx, monkeypatch, tmp_path,
                                                                  deny):
    """No denylist, an unreadable one, an empty one, a marker under 4 characters: E_CONTEXT_SCREEN_UNAVAILABLE, on CLEAN
    text too. The writer retries that code; only the two hit codes become an omission."""
    ns, seat, label = ctx
    if deny is None:
        monkeypatch.delenv("MESHY_CONTEXT_DENYLIST")
    elif deny == "missing":
        monkeypatch.setenv("MESHY_CONTEXT_DENYLIST", str(tmp_path / "absent.json"))
    else:
        f = tmp_path / "bad.json"
        f.write_text(deny, encoding="utf-8")
        monkeypatch.setenv("MESHY_CONTEXT_DENYLIST", str(f))
    key, value = item(label, 0)
    assert refused(app_conn, seat, ns, key, value) == S.UNAVAILABLE
    assert rows(owner_conn, ns, key) == 0


@pytest.mark.parametrize("where", ["text", "task step"])
@pytest.mark.parametrize("bad, code", [("Zorblax Corp", S.CLIENT_NAME), (SECRET, S.SECRET)])
def test_the_production_namespace_stores_names_and_secrets_as_written(app_conn, owner_conn, ctx, monkeypatch,
                                                                      where, bad, code):
    """v1.1 design step 2 widened by the operator: no client-name check (23 Oct 2026, kxxx-8136bc) and no secret check
    (24 Oct 2026, "Store everything, secrets included") on shared/transcript-context. The fixture's random namespace
    stands in for it by being made the DEFAULT_NAMESPACE. Neither screen even loads there: a broken denylist and
    detectors that cannot import change nothing. Control: in any other context namespace the very same item is still
    refused with the screen's code."""
    ns, seat, label = ctx
    if where == "text":
        key, value = item(label, 0, text=bad + " was in the tool output. " + CLEAN)
    else:
        key, value = task(label, 0, step="rotate " + bad)
    assert refused(app_conn, seat, ns, key, value) == code                    # control: other namespaces screen
    assert rows(owner_conn, ns, key) == 0
    monkeypatch.setattr(V, "DEFAULT_NAMESPACE", ns)
    assert V.screens_apply(ns) is False and V.screens_apply("shared/scratch-transcript-context") is True
    monkeypatch.setenv("MESHY_CONTEXT_DENYLIST", "/nonexistent/denylist.json")
    monkeypatch.setattr(S, "_detectors", lambda: (_ for _ in ()).throw(AssertionError("detectors were loaded")))
    assert accepted(app_conn, seat, ns, key, value) == 1                      # stored as written
    stored = owner_conn.execute("SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head",
                                (ns, key)).fetchone()[0]
    assert bad in json.dumps(stored), "the stored item lost the text it was written with"


def test_detectors_that_cannot_load_are_unavailable_not_clean(app_conn, owner_conn, ctx, monkeypatch):
    ns, seat, label = ctx

    def broken():
        raise HandlerError(S.UNAVAILABLE, "the secret screen could not load its detectors")
    monkeypatch.setattr(S, "_detectors", broken)
    key, value = item(label, 0, text="plain words " + SECRET)
    assert refused(app_conn, seat, ns, key, value) == S.UNAVAILABLE
    assert rows(owner_conn, ns, key) == 0


def test_the_server_screens_with_the_loaders_own_detectors():
    """One detector list, not a copy that drifts: the server's is the loader's object."""
    from tools.transcript_cards import transcript_cards as tc
    assert S._detectors() is tc.DETECTORS
    assert tc.detect("x " + SECRET)                         # the synthetic secret really is one


def test_no_screen_code_reaches_a_caller_who_may_not_write(app_conn, owner_conn, ctx):
    """The screens run after check_permission and the plane ban: a caller without a grant, or a product tenant, gets
    that refusal for a denylisted text, never a screen code it could probe the denylist with."""
    ns, seat, label = ctx
    stranger = _peer(owner_conn, "stranger")
    product = _peer(owner_conn, "product", plane="product")
    for peer in (stranger, product):
        lbl = _label(peer)
        key, value = item(lbl, 0, text="Zorblax Corp " + SECRET)
        code = refused(app_conn, peer, ns, key, value)
        assert code not in (S.SECRET, S.CLIENT_NAME, S.UNAVAILABLE), code
        assert code.startswith("E_PERMISSION") or code.startswith("E_PRODUCT") or code == "E_NAMESPACE_UNCLAIMED", code
    # control: the seat that may write gets the screen code for the same text
    key, value = item(label, 0, text="Zorblax Corp " + SECRET)
    assert refused(app_conn, seat, ns, key, value) in (S.SECRET, S.CLIENT_NAME)


# --------------------------------------------------------------------------- manifests

def test_a_complete_batch_gets_its_manifest(app_conn, owner_conn, ctx):
    ns, seat, label = ctx
    values = [item(label, 0)[1], omitted(label, 1, "secret")[1], task(label, 2)[1]]
    key, value = write_batch(app_conn, seat, ns, label, 1, 0, values)
    assert value["counts"] == {"written": 2, "omitted_secret": 1, "omitted_client_name": 0, "omitted_too_large": 0}
    assert accepted(app_conn, seat, ns, key, value) == 1
    assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_WRITE_ONCE"


def test_a_drop_only_batch_has_a_full_manifest(app_conn, ctx):
    ns, seat, label = ctx
    values = [omitted(label, 0, "client_name")[1], omitted(label, 1, "too_large")[1]]
    key, value = write_batch(app_conn, seat, ns, label, 1, 0, values)
    assert value["items"] == [[0, None], [1, None]]
    assert accepted(app_conn, seat, ns, key, value) == 1


def test_a_manifest_that_does_not_match_its_items_is_refused(app_conn, ctx):
    ns, seat, label = ctx
    values = [item(label, 0)[1], item(label, 1, text="second")[1]]
    key, good = write_batch(app_conn, seat, ns, label, 1, 0, values)
    wrong_digest = {**good, "items": [[0, good["items"][0][1]], [1, "f" * 64]]}
    wrong_counts = {**good, "counts": {**good["counts"], "written": 1, "omitted_secret": 1}}
    missing = manifest(label, 1, 0, values + [item(label, 2)[1]])[1]
    for bad, code in ((wrong_digest, "E_CONTEXT_MANIFEST"), (wrong_counts, "E_CONTEXT_MANIFEST"),
                      (missing, "E_CONTEXT_MANIFEST_ITEM_MISSING"), ({**good, "redacted": [0]}, "E_CONTEXT_SCHEMA"),
                      ({**good, "first": 1, "last": 2, "items": [[1, None], [2, None]]}, "E_CONTEXT_MANIFEST_ORDER")):
        assert refused(app_conn, seat, ns, key, bad) == code, bad
    assert accepted(app_conn, seat, ns, key, good) == 1


def test_manifests_commit_in_strict_successor_order(app_conn, ctx):
    ns, seat, label = ctx
    k1, m1 = write_batch(app_conn, seat, ns, label, 1, 0, [item(label, 0)[1], item(label, 1, text="b")[1]])
    k2, m2 = write_batch(app_conn, seat, ns, label, 2, 2, [item(label, 2, batch=2)[1]])
    assert refused(app_conn, seat, ns, k2, m2) == "E_CONTEXT_MANIFEST_PREDECESSOR"  # batch 2 before batch 1: retry
    assert accepted(app_conn, seat, ns, k1, m1) == 1
    k3, m3 = write_batch(app_conn, seat, ns, label, 3, 4, [item(label, 4, batch=3)[1]])
    gap = manifest(label, 3, 4, [item(label, 4, batch=3)[1]])[1]
    assert refused(app_conn, seat, ns, k3, gap) == "E_CONTEXT_MANIFEST_PREDECESSOR"  # batch 2 still missing: retry
    assert accepted(app_conn, seat, ns, k2, m2) == 1
    # batch 3 must start at 3, not 4: ordinal 3 was never written, so its range would leave a gap. Final, not retried
    assert refused(app_conn, seat, ns, k3, m3) == "E_CONTEXT_MANIFEST_ORDER"


def test_redaction_of_the_namespace_is_refused_until_it_is_built(app_conn, owner_conn, ctx):
    ns, seat, label = ctx
    key, value = item(label, 0)
    accepted(app_conn, seat, ns, key, value)
    with pytest.raises(HandlerError) as exc:
        _call(app_conn, seat, "memory.redact", {"namespace": ns, "key": key, "version": 1, "reason_code": "credential"})
    assert exc.value.code == "E_CONTEXT_REDACT_UNAVAILABLE"
    assert owner_conn.execute("SELECT value FROM shared_memory WHERE namespace = %s AND key = %s",
                              (ns, key)).fetchone()[0] == value


# --------------------------------------------------------------------------- the detectors are linear (#347)

def _screen_seconds(text):
    """Best of three runs of every detector over text. Best-of keeps a scheduler hiccup out of the figure."""
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        for _name, rx in S._detectors():
            rx.search(text)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


@pytest.mark.parametrize("shape", ["aB3_", "A1b2", "a"])
def test_the_secret_screen_is_linear_on_long_runs(shape):
    """Was the blocker (kxxx c80f5c to txxxx): three detectors were quadratic on long runs, 74 s for 64 KB on HQD.
    Fixed by #347. Measured as GROWTH, not one absolute bar: 4x the text must cost well under 8x the time (linear is
    about 4x, the quadratic detectors were about 16x), so a slow box does not fail it and a fast one cannot hide a
    regression. The detectors are loaded and compiled first, outside the timing: the first case used to pay for that."""
    S._detectors()
    small = shape * (16_384 // len(shape))
    large = shape * (65_536 // len(shape))
    t_small, t_large = _screen_seconds(small), _screen_seconds(large)
    assert t_large < 2.0, t_large
    assert t_large < 8 * max(t_small, 0.002), (t_small, t_large)


# --------------------------------------------------------------------------- F3: tool_exchange and thinking (#429)

def exchange(seat, ordinal, *, call='{"command":"ls"}', result="ok", **more):
    whole = json.dumps({"tool": "Bash", "call": call, "result": result}, ensure_ascii=False, separators=(",", ":"))
    fields = dict(tool_ref=opaque("toolu_%d" % ordinal), tool="Bash", call_status="ok", result_status="ok",
                  exchange_sha256=hashlib.sha256(whole.encode("utf-8")).hexdigest(), part=1, parts=1)
    fields.update(more)
    return item(seat, ordinal, kind="tool_exchange", text=whole, **fields)


def thinking(seat, ordinal, text="weigh the two routes"):
    return item(seat, ordinal, kind="thinking", text=text, role="assistant")


def f3_manifest(seat, batch, first, entries):
    key, value = manifest(seat, batch, first, entries)
    counts = dict(value["counts"], tool_exchange=0, thinking=0, call_missing=0, result_missing=0, parts_extra=0)
    for e in entries:
        if e["item"] in ("tool_exchange", "thinking"):
            counts[e["item"]] += 1
        if e["item"] == "tool_exchange":
            counts["call_missing"] += e["call_status"] == "missing"
            counts["result_missing"] += e["result_status"] == "missing"
            counts["parts_extra"] += e["part"] > 1
    return key, dict(value, counts=counts)


def test_f3_items_and_a_manifest_that_counts_them_are_written(app_conn, ctx):
    ns, seat, label = ctx
    values = [item(label, 0)[1], exchange(label, 1)[1], thinking(label, 2)[1],
              exchange(label, 3, result=None, result_status="missing")[1]]
    for i, v in enumerate(values):
        accepted(app_conn, seat, ns, f"ctx/{label}/{SRC}/0001/000001/{i:010d}", v)
    key, value = f3_manifest(label, 1, 0, values)
    assert value["counts"]["tool_exchange"] == 2 and value["counts"]["result_missing"] == 1
    plain = manifest(label, 1, 0, values)[1]
    assert refused(app_conn, seat, ns, key, plain) == "E_CONTEXT_MANIFEST", "F3 items need the F3 counts"
    wrong = dict(value, counts=dict(value["counts"], result_missing=0))
    assert refused(app_conn, seat, ns, key, wrong) == "E_CONTEXT_MANIFEST"
    assert accepted(app_conn, seat, ns, key, value) == 1


def test_a_quoted_secret_in_a_tool_exchange_is_refused_by_the_server_screen(app_conn, ctx):
    # json.dumps escapes the quotes inside the call, so the stored text alone would pass the detector (#429 Codex P2).
    ns, seat, label = ctx
    key, value = exchange(label, 0, call=json.dumps({"password": "hunter2"}, separators=(",", ":")))
    assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_SECRET"
    assert rows(app_conn, ns, key) == 0
    key2, part = exchange(label, 0, call=json.dumps({"password": "hunter2"}, separators=(",", ":")))
    part = dict(part, text=part["text"][:60], part=1, parts=2, exchange_sha256="b" * 64,
                text_sha256=hashlib.sha256(part["text"][:60].encode("utf-8")).hexdigest())
    assert refused(app_conn, seat, ns, key2, part) == "E_CONTEXT_SECRET", "a part is screened unescaped"


def test_a_split_exchange_gets_its_manifest_only_when_whole(app_conn, ctx):
    # txxxx on #433: a part alone passes validate_context; the manifest is where the whole is checked.
    ns, seat, label = ctx
    whole = json.dumps({"tool": "Bash", "call": '{"command":"ls"}', "result": "r" * 400}, separators=(",", ":"))
    size = -(-len(whole) // 3)
    digest = hashlib.sha256(whole.encode("utf-8")).hexdigest()
    values = []
    for i in range(3):
        text = whole[i * size:(i + 1) * size]
        values.append(item(label, i, kind="tool_exchange", text=text, tool_ref=opaque("toolu_split"), tool="Bash",
                           call_status="ok", result_status="ok", exchange_sha256=digest, part=i + 1, parts=3)[1])
    for i, v in enumerate(values[:2]):
        accepted(app_conn, seat, ns, f"ctx/{label}/{SRC}/0001/000001/{i:010d}", v)
    key2, short = f3_manifest(label, 1, 0, values[:2])
    assert refused(app_conn, seat, ns, key2, short) == "E_CONTEXT_MANIFEST", "parts 1 and 2 of 3 are not whole"
    accepted(app_conn, seat, ns, f"ctx/{label}/{SRC}/0001/000001/{2:010d}", values[2])
    key, value = f3_manifest(label, 1, 0, values)
    assert value["counts"]["parts_extra"] == 2
    assert accepted(app_conn, seat, ns, key, value) == 1


def test_f3_counts_need_an_f3_item_and_duplicate_members_are_refused(app_conn, ctx):
    # nxxxxx on #433: zero F3 counts beside F1 items only, and {"call": <secret>, "call": "x"} decoded to "x".
    ns, seat, label = ctx
    values = [item(label, 0)[1]]
    accepted(app_conn, seat, ns, f"ctx/{label}/{SRC}/0001/000001/{0:010d}", values[0])
    key, value = f3_manifest(label, 1, 0, values)
    assert refused(app_conn, seat, ns, key, value) == "E_CONTEXT_MANIFEST"
    assert accepted(app_conn, seat, ns, key, manifest(label, 1, 0, values)[1]) == 1
    dup = '{"tool":"Bash","call":"password=hunter2","call":"x","result":"ok"}'
    k2, v2 = exchange(label, 0)
    v2 = dict(v2, text=dup, text_sha256=hashlib.sha256(dup.encode()).hexdigest(),
              exchange_sha256=hashlib.sha256(dup.encode()).hexdigest())
    k2 = k2.replace("/0001/", "/0002/")
    assert refused(app_conn, seat, ns, k2, dict(v2, gen=2)) == "E_CONTEXT_SCHEMA"
    assert rows(app_conn, ns, k2) == 0
