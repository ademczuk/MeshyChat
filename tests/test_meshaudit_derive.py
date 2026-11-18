"""The auditor against a real database and a fake LAN chat model over real HTTP.

What is proved here, each with its positive control in the same test:

  * valid proposals land ONLY with write=True; a dry run asks the model, reports what it
    would record, and leaves both tables untouched, because its transactions are READ ONLY
    (the database refuses a write, which is tested directly);
  * invented recipients, threads and keys are refused and counted by reason, and so are
    real values the prompt never offered;
  * inferred edges never appear in mesh_edges, before or after a rebuild of it;
  * a relation the row declares is never inferred;
  * MUST-8: a chat provider named by hostname gets nothing and the run fails loudly;
  * the ledger stops a second run re-asking, and the CLI is a real module entry point.

ISOLATION. The namespace and the mesh_edges projection are shared by the whole run. Every
test places its rows on its OWN day far in the past (`_base`), so the six-hour context
window before its subject holds only its own rows, and a mesh_edges rebuild never moves
the watermark past anything a sibling test writes afterwards. Derive is always scoped to
the test's own subject keys.

Run:  pytest tests/test_meshaudit_derive.py -v
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

from django.test import override_settings  # noqa: E402

from apps.core import rls  # noqa: E402
from apps.mesh import projection as MP  # noqa: E402
from apps.meshaudit import derive as D  # noqa: E402
from apps.meshaudit import prompt as PR  # noqa: E402
from apps.meshaudit import store  # noqa: E402
from apps.meshaudit import validate as V  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures.meshaudit_fake import FakeChat  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        db.ensure_namespace(c)
        yield c


@pytest.fixture()
def app():
    """The role derive runs as in production (dbconn.serving_connection): meshychat_app."""
    with psycopg.connect(DSN, autocommit=True) as c:
        rls.set_role(c)
        yield c


@pytest.fixture()
def chat():
    fake = FakeChat()
    with override_settings(MESHY_MODEL_PROVIDERS=[fake.provider()]):
        yield fake
    fake.close()


_DAY = iter(range(10_000))


def _base() -> _dt.datetime:
    """This test's own day, a year or more in the past: see ISOLATION above."""
    return (_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
            - _dt.timedelta(days=400 + 3 * next(_DAY)))


def _tag(t: str) -> str:
    return f"{t}-{uuid.uuid4().hex[:6]}"


class Scene:
    """A small conversation: a known recipient `bob`, an earlier message by bob in a
    declared thread, and the legacy subject by `alice` that declares nothing."""

    def __init__(self, owner, *, subject_body: str | None = None, subject_value=None,
                 subject_extra: dict | None = None):
        self.base = _base()
        self.alice, self.bob = db.peer(owner, _tag("alice")), db.peer(owner, _tag("bob"))
        self.zed = db.peer(owner, _tag("zed"))          # a real peer nobody mentions
        self.thread = _tag("t-lease")
        self.other_thread = _tag("t-other")             # real, never offered
        # zed speaks eight hours earlier, in another thread: outside the context window.
        self.far = db.row(owner, self.zed, {"to": self.bob["label"], "body": "old news",
                                            "thread_id": self.other_thread},
                          created_at=self.base - _dt.timedelta(hours=8))
        self.parent = db.row(owner, self.bob, {"to": self.alice["label"],
                                               "body": "can you take the lease renewal?",
                                               "thread_id": self.thread},
                             created_at=self.base - _dt.timedelta(minutes=10))
        self.alice_before = db.row(owner, self.alice, {"to": "fleet", "body": "on it",
                                                       "thread_id": self.thread},
                                   created_at=self.base - _dt.timedelta(minutes=5))
        value = subject_value if subject_value is not None else {
            "body": subject_body if subject_body is not None else
            f"@{self.bob['label']} done, the lease is renewed."}
        value.update(subject_extra or {})
        self.subject = db.row(owner, self.alice, value, created_at=self.base)
        MP.rebuild(owner)

    @property
    def key(self) -> str:
        return self.subject["key"]


def _counts(owner, src_id) -> tuple[int, int, int]:
    """(inferred edges, ledger rows, declared edges) for one source row."""
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM mesh_inferred_edges WHERE src_id=%s),"
                        " (SELECT count(*) FROM mesh_inferred_audits WHERE src_id=%s),"
                        " (SELECT count(*) FROM mesh_edges WHERE src_id=%s)",
                        (src_id, src_id, src_id))
            return tuple(int(x) for x in cur.fetchone())


def _inferred(owner, src_id) -> set[tuple]:
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("SELECT edge_type, dst, model, provider_id, prompt_version "
                        "FROM mesh_inferred_edges WHERE src_id = %s", (src_id,))
            return {tuple(r) for r in cur.fetchall()}


def _declared(owner, src_id) -> set[tuple]:
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("SELECT edge_type, dst FROM mesh_edges WHERE src_id = %s", (src_id,))
            return {tuple(r) for r in cur.fetchall()}


def _full_answer(s: Scene) -> dict:
    return {"addressed_to": [s.bob["label"]], "in_thread": s.thread,
            "replies_to": s.parent["key"]}


# ======================================================================== the write gate

def test_valid_proposals_land_only_with_write(owner, app, chat):
    s = Scene(owner)
    chat.answer(s.key, _full_answer(s))

    dry = D.derive(app, keys=[s.key])
    assert dry.write is False and dry.rows_asked == 1, dry
    assert dry.accepted == {"addressed_to": 1, "in_thread": 1, "replies_to": 1}, dry.refused
    assert _counts(owner, s.subject["id"])[:2] == (0, 0), "the dry run wrote"
    assert len(chat.prompts_about(s.key)) == 1, "the dry run still asks the model"

    wet = D.derive(app, keys=[s.key], write=True)
    assert wet.inserted == 3 and wet.refused == {}, wet
    got = _inferred(owner, s.subject["id"])
    assert {(t, d) for t, d, *_ in got} == {("addressed_to", s.bob["label"]),
                                             ("in_thread", s.thread),
                                             ("replies_to", s.parent["key"])}
    assert {(m, p, v) for *_, m, p, v in got} == {("fake-gemma", "chat-fake",
                                                    PR.PROMPT_VERSION)}, "provenance"
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("SELECT outcome, accepted, refused, asked, model, provider_id "
                        "FROM mesh_inferred_audits WHERE src_id = %s", (s.subject["id"],))
            ledger = cur.fetchone()
    assert ledger == ("asked", 3, {}, list(V.EDGE_TYPES), "fake-gemma", "chat-fake")

    again = D.derive(app, keys=[s.key], write=True)
    assert again.rows_selected == 0, "a ledgered row is not asked again"
    assert len(chat.prompts_about(s.key)) == 2, "positive control: exactly two asks"


def test_the_dry_run_is_read_only_in_the_database(owner, app):
    """The write gate is the SESSION, not an `if`: store.record inside a dry-run
    transaction is refused by Postgres. Positive control: the writing transaction takes it."""
    s = Scene(owner)
    row = {"id": s.subject["id"], "key": s.key, "version": 1, "created_at": s.base}
    accepted = (V.Proposal("in_thread", s.thread),)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with D._txn(app, write=False):
            store.record(app, row, outcome="asked", prompt_version=PR.PROMPT_VERSION,
                         accepted=accepted, model="m", provider_id="p")
    assert _counts(owner, s.subject["id"])[:2] == (0, 0)
    with D._txn(app, write=True):
        w = store.record(app, row, outcome="asked", prompt_version=PR.PROMPT_VERSION,
                         accepted=accepted, model="m", provider_id="p")
    assert w.inserted == 1 and _counts(owner, s.subject["id"])[:2] == (1, 1)


# ============================================================== invented values refused

def test_invented_recipient_thread_and_key_are_refused_and_counted(owner, app, chat):
    """The body NAMES all three, so all three are OFFERED: only the fact check stands
    between them and the table."""
    ghost, nothread = _tag("ghost"), _tag("nosuch-thread")
    nokey = f"msg/1-{uuid.uuid4().hex}"
    s = Scene(owner, subject_body=f"@{ghost} see {nokey}, thread: {nothread} done.")
    chat.answer(s.key, {"addressed_to": [ghost], "in_thread": nothread,
                        "replies_to": nokey})
    r = D.derive(app, keys=[s.key], write=True)
    assert r.rows_asked == 1
    assert r.refused == {V.RECIPIENT_NOT_A_KNOWN_PEER: 1, V.THREAD_NOT_IN_MESH_EDGES: 1,
                         V.REPLY_TARGET_DOES_NOT_EXIST: 1}, r.refused
    assert r.inserted == 0 and _inferred(owner, s.subject["id"]) == set()
    offered = chat.prompts_about(s.key)[0]
    assert ghost in offered and nothread in offered and nokey in offered, (
        "the refusals must come from the fact check: the values WERE offered")
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("SELECT accepted, refused FROM mesh_inferred_audits "
                        "WHERE src_id = %s", (s.subject["id"],))
            assert cur.fetchone() == (0, r.refused), "the ledger keeps the histogram"


def test_real_values_that_were_never_offered_are_refused(owner, app, chat):
    """zed, the other thread and zed's old message all exist, but nothing put them in
    front of the model: picking them is producing, not selecting."""
    s = Scene(owner)
    prompt_before = len(chat.prompts)
    chat.answer(s.key, {"addressed_to": [s.zed["label"], s.bob["label"]],
                        "in_thread": s.other_thread, "replies_to": s.far["key"]})
    r = D.derive(app, keys=[s.key], write=True)
    offered = chat.prompts[prompt_before]
    assert s.zed["label"] not in offered and s.other_thread not in offered
    assert s.far["key"] not in offered
    assert r.refused == {V.RECIPIENT_NOT_OFFERED: 1, V.THREAD_NOT_OFFERED: 1,
                         V.REPLY_TARGET_NOT_OFFERED: 1}, r.refused
    assert _inferred(owner, s.subject["id"]) and {
        (t, d) for t, d, *_ in _inferred(owner, s.subject["id"])} == {
        ("addressed_to", s.bob["label"])}, "positive control: the offered peer landed"


def test_the_author_and_a_later_message_are_refused(owner, app, chat):
    s = Scene(owner)
    # A message written AFTER the subject, which the subject's body cannot be answering.
    after = db.row(owner, s.bob, {"to": s.alice["label"], "body": "thanks",
                                  "thread_id": s.thread},
                   created_at=s.base + _dt.timedelta(minutes=3))
    s2 = db.row(owner, s.alice, {"body": f"re {after['key']} and my own earlier note"},
                created_at=s.base + _dt.timedelta(minutes=4))
    MP.rebuild(owner)
    chat.answer(s2["key"], {"addressed_to": [s.alice["label"]],
                            "replies_to": s2["key"]})
    r1 = D.derive(app, keys=[s2["key"]])
    assert r1.refused == {V.RECIPIENT_IS_AUTHOR: 1, V.REPLY_TARGET_IS_SELF: 1}, r1.refused
    # alice was OFFERED: her own messages are in the context, so only the author check
    # refused her.
    assert s.alice["label"] in chat.prompts_about(s2["key"])[0]

    s3 = db.row(owner, s.alice, {"body": f"quoting {after['key']}"},
                created_at=s.base + _dt.timedelta(minutes=2))
    MP.rebuild(owner)
    chat.answer(s3["key"], {"replies_to": after["key"]})
    r2 = D.derive(app, keys=[s3["key"]])
    assert r2.refused == {V.REPLY_TARGET_NOT_EARLIER: 1}, r2.refused
    chat.answer(s3["key"], {"replies_to": s.parent["key"]})
    r3 = D.derive(app, keys=[s3["key"]])
    assert r3.accepted["replies_to"] == 1, "positive control: the earlier parent is fine"


# ====================================================== the wall between the two tables

def test_inferred_edges_never_appear_in_mesh_edges(owner, app, chat):
    s = Scene(owner)
    declared_before = _declared(owner, s.subject["id"])
    assert declared_before == {("authored_by", s.alice["label"])}, (
        "positive control: the legacy row declares only its (stamped) author")
    chat.answer(s.key, _full_answer(s))
    assert D.derive(app, keys=[s.key], write=True).inserted == 3
    assert len(_inferred(owner, s.subject["id"])) == 3, "positive control: they exist"
    assert _declared(owner, s.subject["id"]) == declared_before, "an inference leaked"
    MP.rebuild(owner)
    assert _declared(owner, s.subject["id"]) == declared_before, (
        "a rebuild of the declared projection laundered an inference into it")
    assert len(_inferred(owner, s.subject["id"])) == 3, "and the rebuild left them alone"


def test_a_relation_the_row_declares_is_not_asked_and_not_inferred(owner, app, chat):
    s = Scene(owner, subject_extra={"thread_id": _tag("t-declared")})
    chat.answer(s.key, {"addressed_to": [s.bob["label"]], "in_thread": s.thread})
    r = D.derive(app, keys=[s.key], write=True)
    assert r.rows[0].asked == ("addressed_to", "replies_to")
    assert r.refused == {V.ALREADY_DECLARED: 1}, r.refused
    assert {(t, d) for t, d, *_ in _inferred(owner, s.subject["id"])} == {
        ("addressed_to", s.bob["label"])}, "positive control: the undeclared one landed"
    assert '"in_thread"' not in chat.prompts_about(s.key)[0], "it was not even asked"


def test_a_row_the_projection_can_place_is_not_eligible(owner, app, chat):
    """to AND thread declared: not a legacy row, never selected, never sent."""
    base = _base()
    p, q = db.peer(owner, _tag("p")), db.peer(owner, _tag("q"))
    placed = db.row(owner, p, {"to": q["label"], "thread_id": _tag("t"), "body": "hi"},
                    created_at=base)
    legacy = db.row(owner, p, {"body": "hi again"},
                    created_at=base + _dt.timedelta(minutes=1))
    MP.rebuild(owner)
    r = D.derive(app, keys=[placed["key"], legacy["key"]])
    assert [x.key for x in r.rows] == [legacy["key"]], "positive control: legacy selected"
    assert chat.prompts_about(placed["key"]) == []


def test_a_row_past_the_projection_watermark_is_not_eligible_yet(owner, app, chat):
    """Past the watermark, "mesh_edges has no edge for it" means "not projected yet", not
    "declared nothing". Positive control: the same legacy content BEFORE the watermark is
    selected. The future row is deleted afterwards and nothing here catches up, so the
    shared watermark never moves."""
    base = _base()
    p = db.peer(owner, _tag("p"))
    MP.rebuild(owner)
    before = db.row(owner, p, {"body": "legacy, before the watermark"}, created_at=base)
    future = db.row(owner, p, {"body": "legacy, not projected yet"},
                    created_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1))
    try:
        with D._txn(app, write=False):
            got = store.select_subjects(app, prompt_version=PR.PROMPT_VERSION, limit=5,
                                        keys=[before["key"], future["key"]])
        assert [r["key"] for r in got] == [before["key"]]
    finally:
        with owner.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE id = %s", (future["id"],))


def test_an_unprojected_row_is_judged_by_what_it_declares(owner, app, chat):
    """A row committed after the catch-up passed its timestamp has NO mesh_edges edges
    (the keyset blind spot of divergence 014), so SQL selects it; the row's own `to` and
    `thread_id` say it is placed, so it is not asked. Positive control: a truly legacy
    row in the same state is asked."""
    base = _base()
    p, q = db.peer(owner, _tag("p")), db.peer(owner, _tag("q"))
    MP.rebuild(owner)                                  # the watermark is past `base` now
    placed = db.row(owner, p, {"to": q["label"], "thread_id": _tag("t"), "body": "placed"},
                    created_at=base)
    legacy = db.row(owner, p, {"body": "legacy"}, created_at=base + _dt.timedelta(minutes=1))
    r = D.derive(app, keys=[placed["key"], legacy["key"]])
    outcomes = {x.key: x.outcome for x in r.rows}
    assert outcomes == {placed["key"]: "nothing_to_ask", legacy["key"]: "asked"}, outcomes
    assert chat.prompts_about(placed["key"]) == []


def test_an_inference_never_vouches_for_the_next(owner, app, chat):
    """A thread that exists ONLY as an inferred edge is not a fact. Offered through the
    body's own `thread:` anchor, it is refused by the fact check."""
    s = Scene(owner)
    only_inferred = _tag("t-only-inferred")
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("INSERT INTO mesh_inferred_edges (src_id, src_key, src_version,"
                        " edge_type, dst, model, provider_id, prompt_version, recorded_at)"
                        " VALUES (%s, %s, 1, 'in_thread', %s, 'm', 'p', 'v', %s)",
                        (s.alice_before["id"], s.alice_before["key"], only_inferred,
                         s.base))
    subject = db.row(owner, s.alice, {"body": f"thread: {only_inferred} is done"},
                     created_at=s.base + _dt.timedelta(minutes=1))
    MP.rebuild(owner)
    chat.answer(subject["key"], {"in_thread": only_inferred})
    r = D.derive(app, keys=[subject["key"]])
    assert r.refused == {V.THREAD_NOT_IN_MESH_EDGES: 1}, r.refused
    chat.answer(subject["key"], {"in_thread": s.thread})
    assert D.derive(app, keys=[subject["key"]]).accepted["in_thread"] == 1, (
        "positive control: the declared thread in the context is accepted")


def test_a_label_that_was_only_ever_addressed_is_not_a_known_peer(owner, app, chat):
    """`to` is what a SENDER typed; only authenticated authors and peers.name are peers.
    A label that appears only as a declared recipient, offered through `@`, is refused."""
    to_only = _tag("nobody")
    s = Scene(owner)
    db.row(owner, s.bob, {"to": to_only, "thread_id": s.thread, "body": "fyi"},
           created_at=s.base - _dt.timedelta(minutes=1))
    subject = db.row(owner, s.alice, {"body": f"@{to_only} @{s.bob['label']} ok"},
                     created_at=s.base + _dt.timedelta(minutes=1))
    MP.rebuild(owner)
    chat.answer(subject["key"], {"addressed_to": [to_only, s.bob["label"]]})
    r = D.derive(app, keys=[subject["key"]])
    assert r.refused == {V.RECIPIENT_NOT_A_KNOWN_PEER: 1}, r.refused
    assert r.accepted["addressed_to"] == 1, "positive control: bob"


def test_a_row_with_no_body_is_ledgered_without_asking(owner, app, chat):
    s = Scene(owner, subject_value={"state": "cursor"})
    r = D.derive(app, keys=[s.key], write=True)
    assert r.rows_no_body == 1 and r.rows_asked == 0 and chat.prompts_about(s.key) == []
    assert _counts(owner, s.subject["id"])[:2] == (0, 1)


def test_a_truncated_answer_writes_nothing(owner, app, chat):
    s = Scene(owner)
    chat.answer(s.key, _full_answer(s), finish="length")
    r = D.derive(app, keys=[s.key], write=True)
    assert r.refused == {V.ANSWER_TRUNCATED: 1} and r.inserted == 0
    assert _counts(owner, s.subject["id"])[:2] == (0, 1), "asked, so ledgered; nothing kept"


def test_the_walk_waits_for_the_declared_projection(owner):
    """With no mesh_edges watermark, "declares nothing" and "not projected yet" look the
    same, so nothing is eligible and the caller is told, not handed an empty list."""
    class Rollback(Exception):
        pass

    MP.rebuild(owner)
    with rls.server_internal(owner):
        assert store.watermark(owner) is not None, "positive control: a watermark exists"
    with pytest.raises(Rollback):
        with rls.server_internal(owner):
            with owner.cursor() as cur:
                cur.execute("DELETE FROM mesh_edges_projection")
            with pytest.raises(store.ProjectionNotRun):
                store.select_subjects(owner, prompt_version=PR.PROMPT_VERSION, limit=5)
            raise Rollback
    with rls.server_internal(owner):
        assert store.watermark(owner) is not None, "the rollback restored it"


def test_the_question_is_asked_without_thinking(owner, app, chat):
    """HQB's model ran out of 1,536 and 2,048 tokens still reasoning on this prompt
    (prompt.py). The fake does the same unless the request turns thinking off, so this
    is the test that fails if the flag stops being sent."""
    s = Scene(owner)
    chat.answer(s.key, _full_answer(s))
    r = D.derive(app, keys=[s.key])
    assert r.accepted["in_thread"] == 1, r.refused
    sent = [pl for pl in chat.payloads if f"MESSAGE {s.key} sent by"
            in pl["messages"][-1]["content"]]
    assert sent and all(pl.get("chat_template_kwargs") == {"enable_thinking": False}
                        and pl.get("temperature") == 0.0 for pl in sent), sent

    chat.ignores_no_think = True                  # positive control: the failure is real
    r2 = D.derive(app, keys=[s.key])
    assert r2.refused == {V.ANSWER_EMPTY: 1} and r2.accepted["in_thread"] == 0


def test_an_empty_answer_is_ledgered_and_the_walk_moves_on(owner, app, chat):
    """A provider that answered HTTP 200 with no content DID answer. The row is refused
    `answer_empty` and ledgered, and the sweep carries on, so one row the model cannot
    answer does not pin every future sweep at the same place."""
    s = Scene(owner)
    second = db.row(owner, s.alice, {"body": f"@{s.bob['label']} and the other lease too"},
                    created_at=s.base + _dt.timedelta(minutes=1))
    MP.rebuild(owner)
    chat.answer(second["key"], raw="", finish="length")      # newest first: asked first
    chat.answer(s.key, _full_answer(s))
    r = D.derive(app, keys=[s.key, second["key"]], write=True)
    assert not r.provider_failed and r.rows_asked == 2, r.stopped
    assert [x.key for x in r.rows] == [second["key"], s.key]
    assert r.refused == {V.ANSWER_EMPTY: 1} and r.inserted == 3
    assert _counts(owner, second["id"])[:2] == (0, 1), "asked, refused, ledgered"
    assert _counts(owner, s.subject["id"])[:2] == (3, 1), "positive control: the next row"


# ============================================================================== MUST-8

def test_a_chat_provider_named_by_hostname_gets_nothing_and_the_run_fails(owner, app, chat):
    s = Scene(owner)
    chat.answer(s.key, _full_answer(s))
    by_name = chat.provider(pid="chat-by-name", base_url=f"http://localhost:{chat.port}")
    with override_settings(MESHY_MODEL_PROVIDERS=[by_name]):
        r = D.derive(app, keys=[s.key], write=True)
    assert chat.prompts_about(s.key) == [], "mesh text reached a provider MUST-8 refuses"
    assert r.provider_failed and r.rows_asked == 0 and r.rows[0].outcome == "not_asked"
    assert any(a.get("verdict") == "refused_must8" for a in r.attempts), r.attempts
    assert _counts(owner, s.subject["id"])[:2] == (0, 0), "an unasked row is not ledgered"

    ok = D.derive(app, keys=[s.key], write=True)          # the fixture's 127.0.0.1
    assert len(chat.prompts_about(s.key)) == 1 and ok.inserted == 3, (
        "positive control: the same host by IP literal is asked")


def test_a_provider_that_is_down_stops_the_run_and_records_nothing(owner, app, chat):
    s = Scene(owner)
    chat.down = True
    r = D.derive(app, keys=[s.key], write=True)
    assert r.provider_failed and "no chat provider answered" in (r.stopped or "")
    assert _counts(owner, s.subject["id"])[:2] == (0, 0)


# ====================================================================== coverage and CLI

def test_coverage_counts_eligible_and_audited(owner, app, chat):
    s = Scene(owner)
    with rls.server_internal(owner):
        before = store.coverage(owner, prompt_version=PR.PROMPT_VERSION)
    chat.answer(s.key, _full_answer(s))
    D.derive(app, keys=[s.key], write=True)
    with rls.server_internal(owner):
        after = store.coverage(owner, prompt_version=PR.PROMPT_VERSION)
    assert before["eligible"] >= 1 and after["eligible"] == before["eligible"]
    assert after["audited"] == before["audited"] + 1
    assert after["edges"]["in_thread"] == before["edges"]["in_thread"] + 1
    assert after["audited"] <= after["eligible"]


def _cli(*args: str, providers: list[dict]) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "DJANGO_SETTINGS_MODULE", "MESHY_DSN")}
    env["MESHY_MODEL_PROVIDERS"] = json.dumps(providers)
    return subprocess.run([sys.executable, "-m", "apps.meshaudit", "--dsn", DSN, *args],
                          cwd=str(ROOT), capture_output=True, text=True, env=env,
                          timeout=180)


def test_the_cli_is_dry_unless_told_to_write(owner, chat):
    """`python -m apps.meshaudit derive` in a fresh process, as the app role."""
    s = Scene(owner)
    chat.answer(s.key, _full_answer(s))
    dry = _cli("derive", "--key", s.key, providers=[chat.provider()])
    assert dry.returncode == 0, dry.stderr[-800:]
    assert "DRY RUN" in dry.stdout and f"INFERRED in_thread -> {s.thread}" in dry.stdout
    assert _counts(owner, s.subject["id"])[:2] == (0, 0), dry.stdout

    wet = _cli("derive", "--write", "--key", s.key, providers=[chat.provider()])
    assert wet.returncode == 0, wet.stderr[-800:]
    assert "written: 3 edge(s)" in wet.stdout, wet.stdout
    assert _counts(owner, s.subject["id"])[:2] == (3, 1)

    status = _cli("status", providers=[])
    assert status.returncode == 0 and PR.PROMPT_VERSION in status.stdout, status.stdout


def test_the_cli_exits_non_zero_when_no_provider_answers(owner, chat):
    s = Scene(owner)
    down = _cli("derive", "--write", "--key", s.key,
                providers=[chat.provider(base_url="http://localhost:1")])
    assert down.returncode == 1, (down.returncode, down.stdout, down.stderr[-400:])
    assert "STOPPED: no chat provider answered" in down.stdout
    assert _counts(owner, s.subject["id"])[:2] == (0, 0)


def test_the_cli_exits_2_while_the_projection_has_never_run(owner):
    """Exit 2 with the reason on stderr, never an empty success: the CLI side of
    test_the_walk_waits_for_the_declared_projection. The subprocess has its own
    connection, so the watermark is cleared for real and rebuilt afterwards."""
    Scene(owner)                                       # a row for the rebuild to walk
    MP.rebuild(owner)
    with rls.server_internal(owner):
        assert store.watermark(owner) is not None, "positive control: a watermark exists"
    try:
        with rls.server_internal(owner):
            with owner.cursor() as cur:
                cur.execute("DELETE FROM mesh_edges_projection")
        r = _cli("derive", providers=[])
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr[-400:])
        assert "derive [" not in r.stdout, r.stdout
        assert "COULD NOT RUN: mesh_edges has no watermark" in r.stderr, r.stderr
    finally:
        MP.rebuild(owner)
    with rls.server_internal(owner):
        assert store.watermark(owner) is not None, "the rebuild restored it"
