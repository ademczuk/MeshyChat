"""Proposing acts from prose, and the wall between a proposal and a declaration.

THE TWO PROPERTIES THIS FILE EXISTS TO HOLD, and everything else here is detail:

  1. A PROPOSAL MAY ONLY SELECT AMONG ANCHORS THE TEXT CONTAINS. A model that returns a
     version string not present in the source is refused, not trusted. A fabricated sha is
     invisible once published because nobody re-derives forty characters from a message.
  2. AN INFERRED ACT IS NEVER A DECLARED ONE. It carries its own prefixed verb, renders as
     "INFERRED, not declared", and does not satisfy `has()`. If that wall ever falls, a
     model's guess becomes indistinguishable from a seat's statement, which is the failure
     the whole board is a response to.

Every model in here is a STUB. The GPU pass is measured separately (divergence 008) because
a test that needs a 26B model resident is a test that is skipped on every other box.

Run:  .venv/Scripts/python.exe -m pytest tests/test_board_derive.py -q
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

from apps.board import declare as D  # noqa: E402
from apps.board import derive as R  # noqa: E402
from apps.board import projection as B  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

NS = B.MESH_CHAT_NAMESPACE
SHA = "51fbdcbf569f203bedddc86076b91bdfcb647e74"
OTHER_SHA = "d4abcb706c9e93dcfd8388c99e3e679893feabce"


# --------------------------------------------------------------------------- #
# The deterministic pass
# --------------------------------------------------------------------------- #

def test_a_plain_report_resolves_and_a_question_about_it_does_not():
    """Paired on purpose: the same anchors, opposite framings. Without the positive half,
    a refusal here could equally mean the extractor is broken."""
    ok = R.propose(f"uxxx reviewed pr/851 at {SHA}", source_key="msg/1-a")
    assert isinstance(ok, R.Proposal)
    assert (ok.subject, ok.verb, ok.ref) == ("pr/851", "reviewed", SHA)
    assert ok.by == "rules" and ok.source_key == "msg/1-a"

    for framing in (f"should I review pr/851 at {SHA}?",
                    f"pr/851 was not reviewed at {SHA}",
                    f'ACTION is "check whether pr/851 is reviewed at {SHA} and merge it"'):
        r = R.propose(framing, source_key="msg/1-b")
        assert isinstance(r, R.Refused), framing
        assert r.reason == R.NEGATED, (framing, r.reason)


def test_the_triple_must_co_occur_in_ONE_sentence():
    """The correction that made this module work, and the one that keeps it honest.

    Requiring the first LINE proposed nothing on 300 production rows. Widening to the
    whole BODY is the wrong repair: a version in one sentence and a pull request in
    another are not thereby one act, and binding them is the reconstruction the board
    exists to stop.
    """
    split = f"We looked at pr/851 today. Separately, main is now at {SHA} and it merged."
    r = R.propose(split, source_key="msg/2")
    assert isinstance(r, R.Refused), r
    # The control: the same facts in ONE sentence DO resolve, so the refusal above is
    # about sentence scope and not about the words being unrecognisable.
    joined = f"We merged pr/851 at {SHA} today."
    assert isinstance(R.propose(joined, source_key="msg/2"), R.Proposal)


def test_the_negation_filter_is_scoped_to_the_bearing_sentence():
    """A whole-line filter refused `#931 BLOCKED at <sha>` for a word in a LATER clause.
    Measured on the real corpus: 7 of the 11 candidate rows were lost that way."""
    body = (f"#931 blocked at {SHA}, node kxxx. I could not reproduce the other thing "
            f"and will not be looking further today.")
    r = R.propose(body, source_key="msg/3")
    assert isinstance(r, R.Proposal), r
    assert (r.subject, r.verb, r.ref) == ("ref/931", "blocked", SHA)
    # Control: the negation INSIDE the bearing sentence still refuses.
    assert isinstance(R.propose(f"#931 was not blocked at {SHA}", source_key="msg/3"), R.Refused)


@pytest.mark.parametrize("body,why", [
    (f"pr/851 and pr/852 both merged at {SHA}", "two subjects"),
    (f"pr/851 merged at {SHA} then at {OTHER_SHA}", "two versions"),
    (f"pr/851 fixed and pushed at {SHA}", "two verbs"),
])
def test_ambiguity_refuses_rather_than_picking(body, why):
    """Taking the first of two would bind an act to whichever the author happened to
    mention earlier, silently and unfalsifiably. AMBIGUOUS is what the model pass is for."""
    r = R.propose(body, source_key="msg/4")
    assert isinstance(r, R.Refused), (why, r)
    assert r.reason == R.AMBIGUOUS, (why, r.reason)


def test_an_epoch_ms_mesh_key_is_never_read_as_a_version():
    """Without the letter requirement in SHA_RE, every message proposes its own key as the
    version it was performed at. Measured: it moved the sha count on 300 rows by 32.

    The epoch is 13 digits and all-numeric, so it fails the letter requirement. The nonce
    after it is hex and CAN match once it is 7 characters, which is correct: that is a
    real hex token in the text, and the proposal still has to survive the other gates.
    """
    assert R.refs_in("msg/1758470000123-abcd") == ()          # nonce too short to be a sha
    assert R.refs_in("see msg/1758470000123-abcdef0") == ("abcdef0",)   # the epoch is absent
    assert "1758470000123" not in R.refs_in("see msg/1758470000123-abcdef0")
    assert R.refs_in(f"at {SHA}") == (SHA,)
    # A bare 13-digit number anywhere is never a version.
    assert R.refs_in("we saw 1758470000123 rows") == ()


def test_named_refusals_distinguish_nothing_here_from_nearly_there():
    """`no_anchors` and `ambiguous` are different facts and only one of them is a case the
    model pass can help with, so they must not collapse into None."""
    assert R.propose("morning all", source_key="k").reason == R.NO_ANCHORS
    assert R.propose(f"main is at {SHA}", source_key="k").reason == R.NO_SUBJECT
    assert R.propose("pr/851 was reviewed", source_key="k").reason == R.NO_REF
    assert R.propose(f"pr/851 at {SHA}", source_key="k").reason == R.NO_VERB


# --------------------------------------------------------------------------- #
# The model pass, and the constraint that makes it safe
# --------------------------------------------------------------------------- #

AMBIG = f"pr/851 fixed and pushed at {SHA}"


def _stub(payload):
    """A model that returns exactly this text."""
    return lambda prompt: payload


def test_the_model_resolves_an_ambiguity_the_rules_refused():
    r = R.propose_with_model(AMBIG, source_key="msg/5", generate=_stub(
        json.dumps({"subject": "pr/851", "verb": "pushed", "ref": SHA})), model_id="stub")
    assert isinstance(r, R.Proposal), r
    assert (r.subject, r.verb, r.ref, r.by) == ("pr/851", "pushed", SHA, "stub")


def test_a_model_that_INVENTS_a_version_is_refused():
    """THE LOAD-BEARING TEST. The model is asked to select, never to produce, because a
    fabricated sha is invisible once published: nobody re-derives forty characters from a
    message, they read it."""
    invented = "0" * 39 + "f"
    assert invented not in AMBIG
    r = R.propose_with_model(AMBIG, source_key="msg/6", generate=_stub(
        json.dumps({"subject": "pr/851", "verb": "pushed", "ref": invented})))
    assert isinstance(r, R.Refused), r
    assert r.reason == "model_declined_or_invented"
    # Control: the SAME call with a version that IS in the text is accepted, so the
    # refusal is about the invention and not about the shape of the response.
    ok = R.propose_with_model(AMBIG, source_key="msg/6", generate=_stub(
        json.dumps({"subject": "pr/851", "verb": "pushed", "ref": SHA})))
    assert isinstance(ok, R.Proposal)


@pytest.mark.parametrize("subject,verb", [("pr/999", "pushed"), ("pr/851", "audited")])
def test_a_model_that_invents_a_subject_or_a_verb_is_refused(subject, verb):
    """Same rule, the other two fields. An invented verb also leaves the closed
    vocabulary, which is how two rows that mean one thing start looking like two states."""
    r = R.propose_with_model(AMBIG, source_key="msg/7", generate=_stub(
        json.dumps({"subject": subject, "verb": verb, "ref": SHA})))
    assert isinstance(r, R.Refused) and r.reason == "model_declined_or_invented"


@pytest.mark.parametrize("payload,reason", [
    (json.dumps({"subject": None, "verb": None, "ref": None}), "model_declined_or_invented"),
    ("I am not sure about this one, sorry.", "model_returned_no_json"),
    ("", "model_returned_no_json"),
])
def test_a_declining_or_unparseable_model_refuses_rather_than_guessing(payload, reason):
    r = R.propose_with_model(AMBIG, source_key="msg/8", generate=_stub(payload))
    assert isinstance(r, R.Refused) and r.reason == reason


def test_a_model_that_raises_is_a_refusal_not_a_crash():
    def boom(prompt):
        raise TimeoutError("the model host went away")
    r = R.propose_with_model(AMBIG, source_key="msg/9", generate=boom)
    assert isinstance(r, R.Refused) and r.reason.startswith("model_error: TimeoutError")


def test_json_is_recovered_from_prose_and_code_fences():
    """Models wrap JSON in fences and in reasoning however they are asked not to. Measured
    on the live 26B model: it emitted ```json around the object every time."""
    fenced = '```json\n{"subject": "pr/851", "verb": "pushed", "ref": "%s"}\n```' % SHA
    assert isinstance(R.propose_with_model(AMBIG, source_key="k", generate=_stub(fenced)),
                      R.Proposal)
    assert R._first_json_object("not json at all") is None
    assert R._first_json_object('prefix {"broken": ') is None


def test_the_model_sees_one_sentence_not_the_message():
    """A message is a place to find several acts and the prompt asks for one, so handing
    the model the whole body invites it to blend two."""
    seen = []

    def spy(prompt):
        seen.append(prompt)
        return json.dumps({"subject": None, "verb": None, "ref": None})
    body = f"pr/851 fixed and pushed at {SHA}\nUnrelated: the weather in Vienna is fine."
    R.propose_with_model(body, source_key="k", generate=spy)
    assert len(seen) == 1, "one prompt per bearing sentence, and only one sentence bears"
    assert "weather in Vienna" not in seen[0]


def test_a_thought_in_the_reasoning_channel_is_never_read_as_an_answer():
    """The glue a peer's challenge exposed as untested, and the measurement that settled it.

    An earlier version of `_http_generate` concatenated `reasoning_content` onto `content`,
    because this node's model routes output to the reasoning channel until it stops
    thinking. Measured on 17 real prompts against the live model: all 14 stop-terminated
    answers carried their JSON in `content`, none of the 3 truncated ones did, and the
    cell that would have justified the fallback (stop, empty content, pick only in
    reasoning) was ZERO. So the fallback could only fire on a truncated generation, where a
    JSON object in the thinking is a thought mid-flight, and the select-only rule cannot
    tell a considered pick from an abandoned one.
    """
    from apps.board.__main__ import answer_text

    picked = json.dumps({"subject": "pr/851", "verb": "pushed", "ref": SHA})
    # The truncated shape, 2 of 17 in the measurement: a pick sits in the thinking only.
    assert answer_text({"content": "", "reasoning_content": picked}) == ""
    assert answer_text({"reasoning_content": picked}) == ""
    # Positive control: a real answer is returned unchanged, so the rule above is about
    # the channel and not about a reader that returns nothing.
    assert answer_text({"content": picked, "reasoning_content": "thinking..."}) == picked
    assert answer_text({"content": picked}) == picked

    # And end to end: a model whose pick is only in the thinking must not produce a
    # proposal. This is the behaviour the concatenation would have allowed.
    def thinking_only(prompt):
        return answer_text({"content": "", "reasoning_content": picked})
    r = R.propose_with_model(AMBIG, source_key="msg/rc", generate=thinking_only)
    assert isinstance(r, R.Refused) and r.reason == "model_returned_no_json"

    def answered(prompt):
        return answer_text({"content": picked, "reasoning_content": picked})
    assert isinstance(R.propose_with_model(AMBIG, source_key="msg/rc", generate=answered),
                      R.Proposal)


def test_available_generate_provider_reads_a_probed_snapshot():
    """Capability comes from `apps.modelhost`, which probes for FUNCTION. A provider that
    answered 200 but does not work must not be picked up here."""
    assert R.available_generate_provider({"providers": []}) is None
    assert R.available_generate_provider(
        {"providers": [{"kind": "generate", "available": False}]}) is None
    assert R.available_generate_provider(
        {"providers": [{"kind": "embed", "available": True}]}) is None
    picked = R.available_generate_provider(
        {"providers": [{"kind": "embed", "available": True},
                       {"kind": "generate", "available": True, "id": "gemma"}]})
    assert picked and picked["id"] == "gemma"


# --------------------------------------------------------------------------- #
# The wall: an inferred act is never a declared one
# --------------------------------------------------------------------------- #

@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _peer(conn, tag: str) -> dict:
    """A dev peer that can WRITE the mesh namespace whether or not it claimed it.

    THE ORDER DEPENDENCY THIS CLOSES, because `ON CONFLICT DO NOTHING` hides it. Run
    alone, this peer is the first to claim `shared/mesh-chat` and holds implicit admin as
    its owner. Run after any sibling suite that already claimed it, the insert is a no-op
    and the peer has NO grant, so every write is E_PERMISSION_DENIED. The test then passes
    in isolation and fails in the suite, which is the shape that cost this repository a
    44-failure wall once already.
    """
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"drv-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"drv-{tag}-{peer_id.hex[:8]}", f"drv {tag}"))
        # The label IS the peer's name (divergence 028: an act is written under one of the
        # labels the caller is answered for, never a free-form one).
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                    " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                    " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                    (NS, peer_id, owner_id))
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace"
                    " WHERE namespace = %s", (NS,))
        holder = cur.fetchone()
        if holder and str(holder[0]) != str(peer_id):
            cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                        " grantee_owner_id, permission, granted_by_peer_id,"
                        " granted_by_owner_id) VALUES (%s,%s,%s,'write',%s,%s)",
                        (NS, peer_id, owner_id, holder[0], holder[1]))
    return {"peer_id": peer_id, "owner_id": owner_id, "label": f"drv-{tag}-{peer_id.hex[:8]}"}


def _row(conn, peer, value):
    rid = uuid.uuid4()
    # With the bytes a writer would have signed: since A2 (divergence 051) a row without
    # them is refused from every session.
    sr.insert(conn, sr.row(
        peer["peer_id"], peer["owner_id"], NS,
        f"msg/{int(_dt.datetime.now().timestamp()*1000)}-{rid.hex}", value, rid=rid,
        call_id=f"call-{rid}", author_label=peer["label"]))


def test_an_inferred_act_does_not_satisfy_has_and_says_so_in_prose(owner):
    """The wall, end to end against the database.

    A subject whose ONLY review is inferred must read as unreviewed to anything that asks,
    and the prose a human reads must say the word "INFERRED". A structured flag nobody
    restates is, to a reader whose reading is the prose, absent.
    """
    p = _peer(owner, "wall")
    subject = f"pr/wall-{uuid.uuid4().hex[:8]}"
    prop = R.Proposal(subject=subject, verb="reviewed", ref=SHA, source_key="msg/src-1",
                      evidence=f"somebody reviewed {subject} at {SHA}", by="stub")
    _row(owner, p, D.act_value(subject=subject, verb=prop.inferred_verb, ref=SHA,
                               actor_label=p["label"], in_reply_to=prop.source_key,
                               body=f"proposed from {prop.source_key}"))

    state = B.state_for(owner, subject)
    assert isinstance(state, B.SubjectState)
    assert len(state.acts) == 1
    act = state.acts[0]
    assert act.inferred is True and act.verb == "inferred.reviewed"
    assert act.declared_verb == "reviewed"
    assert act.source_key == "msg/src-1", "a proposal that cannot be traced back is worse than none"

    # The wall itself.
    assert state.has("reviewed") is False, "an inferred review must not read as a review"
    assert state.has("inferred.reviewed") is True
    assert state.declared == () and len(state.inferred) == 1

    prose = "\n".join(B.render(state))
    assert "INFERRED, not declared" in prose
    assert "msg/src-1" in prose, "the prose must name the row a reader can go and check"
    assert "Unconfirmed" in prose

    # POSITIVE CONTROL: the same act DECLARED does satisfy has(), so the assertion above
    # is about the prefix and not about a projection that never returns True.
    declared_subject = f"pr/wall-{uuid.uuid4().hex[:8]}"
    _row(owner, p, D.act_value(subject=declared_subject, verb="reviewed", ref=SHA,
                               actor_label=p["label"]))
    declared_state = B.state_for(owner, declared_subject)
    assert declared_state.has("reviewed") is True
    assert declared_state.inferred == () and len(declared_state.declared) == 1
    assert "INFERRED" not in "\n".join(B.render(declared_state))


def test_a_sweep_reads_rows_skips_already_declared_ones_and_reports_why(owner):
    """The batch path, end to end against the database.

    Two properties in one test because they are the same decision. A row whose author
    DECLARED the act is never a candidate: the declaration is the better record and a
    proposal beside it is noise a reader has to reconcile. And the refusal histogram is
    returned, because a sweep that proposes nothing and a sweep with nothing to propose
    about are different states.
    """
    p = _peer(owner, "sweep")
    # A NUMERIC pull-request reference, because that is what the extractor recognises: the
    # subject vocabulary is deliberately narrow (`pr/<n>`, `issue/<n>`), and a unique hex
    # suffix here would be a fixture the code was never built to read. Six digits from the
    # uuid keeps it unique across concurrent runs without leaving that vocabulary.
    subject = f"pr/{int(uuid.uuid4().int % 900000) + 100000}"
    # 1: prose carrying a clean act, nobody declared it.
    _row(owner, p, {"body": f"{subject} merged at {SHA}, main is green.", "from": p["label"]})
    # 2: the same act, DECLARED by its author. Must be skipped, not re-proposed.
    _row(owner, p, D.act_value(subject=subject, verb="merged", ref=SHA, actor_label=p["label"]))
    # 3: prose with nothing to point at.
    _row(owner, p, {"body": "morning all, coffee first", "from": p["label"]})

    rows = R.recent_rows(owner, limit=50)
    assert len(rows) >= 3
    candidates = R.undeclared(rows)
    declared_keys = {r["key"] for r in rows} - {r["key"] for r in candidates}
    assert declared_keys, "the declared row must be excluded from the candidates"

    proposals, reasons = R.sweep(candidates)
    mine = [x for x in proposals if x.subject == subject]
    assert len(mine) == 1, f"expected one proposal for {subject}, got {mine}"
    assert (mine[0].verb, mine[0].ref) == ("merged", SHA)
    assert reasons.get("PROPOSED", 0) >= 1 and reasons.get(R.NO_ANCHORS, 0) >= 1, reasons


def test_one_act_reported_in_three_messages_becomes_one_proposal(owner):
    """Deduplication on the triple, and the corroboration kept rather than discarded.

    Measured on the real corpus: one merge was reported in three separate messages and
    another in two, so a sweep that proposed per MESSAGE would have put five rows on the
    board for two events. Three seats independently reporting one merge is also stronger
    evidence than one, which is why the extra source keys are carried instead of dropped.
    """
    p = _peer(owner, "dedup")
    n = int(uuid.uuid4().int % 900000) + 100000
    subject = f"pr/{n}"
    # Three DIFFERENT wordings of one act. None of them may carry a second vocabulary verb:
    # an earlier draft opened one with "Confirmed:", which is itself a verb, so that row
    # was refused as ambiguous and the test measured two rows while claiming three. The
    # rule was working; the fixture was wrong.
    for phrasing in (f"{subject} merged at {SHA}.",
                     f"For the record, {subject} merged at {SHA} just now.",
                     f"Also worth noting, {subject} merged at {SHA}."):
        _row(owner, p, {"body": phrasing, "from": p["label"]})

    proposals, reasons = R.sweep(R.undeclared(R.recent_rows(owner, limit=50)))
    mine = [x for x in proposals if x.subject == subject]
    assert len(mine) == 1, f"three messages, one act, got {len(mine)} proposals"
    assert len(mine[0].corroborations) == 2, mine[0].corroborations
    assert mine[0].source_key not in mine[0].corroborations
    assert reasons.get("corroborated", 0) >= 2

    # POSITIVE CONTROL: a DIFFERENT version of the same subject is a different act and is
    # NOT collapsed, so the dedup keys on the triple rather than on the subject.
    _row(owner, p, {"body": f"{subject} merged at {OTHER_SHA}.", "from": p["label"]})
    proposals2, _ = R.sweep(R.undeclared(R.recent_rows(owner, limit=50)))
    assert len({x.ref for x in proposals2 if x.subject == subject}) == 2


def test_the_cli_writes_inferred_acts_and_the_board_shows_them_as_proposals(owner, tmp_path):
    """The whole chain as the entry point people actually run, not as a function call.

    `apps/core/sqlmigrate.py` broke in this repository the moment it grew an `apps.` import,
    and its in-process tests could not see it, so a CLI is proved by running it.

    Also proves the DRY RUN is really dry: the first invocation must write nothing, because
    `--write` publishes model-read claims to a shared substrate and a default that quietly
    published would make that decision for the operator.
    """
    import subprocess
    p = _peer(owner, "cliwrite")
    n = int(uuid.uuid4().int % 900000) + 100000
    subject = f"pr/{n}"
    _row(owner, p, {"body": f"{subject} merged at {SHA}, main is green.", "from": p["label"]})

    seed = os.urandom(32)
    from apps.crypto import primitives as P
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    with owner.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active)"
                    " VALUES (%s,%s,%s,TRUE)", (p["peer_id"], kid, P.b64u(pub)))
    sf = tmp_path / ".agentmeshy-deriver-state.json"
    sf.write_text(json.dumps({"peer_id": str(p["peer_id"]), "kid": kid,
                              "signing_key_b64url": P.b64u(seed),
                              "owner_id": str(p["owner_id"])}), encoding="utf-8")

    env = dict(os.environ)
    env.pop("MESHY_DSN", None)
    base = [sys.executable, "-m", "apps.board", "derive", "--limit", "40", "--dsn", DSN]

    dry = subprocess.run(base, cwd=ROOT, capture_output=True, text=True, env=env, timeout=180)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "DRY RUN: nothing was written" in dry.stdout
    assert subject in dry.stdout
    assert isinstance(B.state_for(owner, subject), B.NoDeclaredActs), "a dry run wrote a row"

    wet = subprocess.run(base + ["--write", "--state-file", str(sf), "--label", p["label"]],
                         cwd=ROOT, capture_output=True, text=True, env=env, timeout=300)
    # NOT `returncode == 0`. A sweep reads the WHOLE namespace, so in a full-suite run it
    # also proposes about rows a sibling test seeded, and one of those being refused would
    # fail this test for a reason that has nothing to do with it. The assertion that
    # belongs here is about THIS subject; the exit code is only checked for a crash.
    assert wet.returncode in (0, 1), wet.stdout + wet.stderr
    assert "wrote" in wet.stdout, wet.stdout + wet.stderr

    state = B.state_for(owner, subject)
    assert isinstance(state, B.SubjectState), wet.stdout
    assert state.has("merged") is False, "the CLI wrote a DECLARED act; it must write a proposal"
    assert state.has("inferred.merged") is True
    act = state.inferred[0]
    assert act.ref == SHA and act.source_key, "the written act must name the row it came from"
    assert "INFERRED, not declared" in "\n".join(B.render(state))


def test_derive_write_runs_the_door_checks_and_reports_each_refusal(owner, tmp_path, capsys,
                                                                   monkeypatch):
    """Divergence 038. `derive --write` declares through `declare_in_process`, so it runs
    the checks the door runs. With the clock ten minutes back every envelope it signs is
    stale: each is reported `REFUSED <subject>: E_SIG_REPLAY ...`, the run exits 1 and
    writes nothing, rather than dying on the first. Control: the real clock writes the
    proposal."""
    from apps.board.__main__ import main as board_main
    from apps.crypto import primitives as P

    p = _peer(owner, "drvstale")
    n = int(uuid.uuid4().int % 900000) + 100000
    subject = f"pr/{n}"
    _row(owner, p, {"body": f"{subject} merged at {SHA}, main is green.", "from": p["label"]})
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    with owner.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active)"
                    " VALUES (%s,%s,%s,TRUE)", (p["peer_id"], kid, P.b64u(pub)))
    sf = tmp_path / ".agentmeshy-deriver-state.json"
    sf.write_text(json.dumps({"peer_id": str(p["peer_id"]), "kid": kid,
                              "signing_key_b64url": P.b64u(seed),
                              "owner_id": str(p["owner_id"])}), encoding="utf-8")
    argv = ["derive", "--limit", "40", "--dsn", DSN, "--write", "--state-file", str(sf),
            "--label", p["label"]]

    real = D.now_utc
    monkeypatch.setattr(D, "now_utc", lambda: real() - _dt.timedelta(seconds=600))
    rc = board_main(argv)
    cap = capsys.readouterr()
    assert rc == 1, cap.out + cap.err
    assert (f"REFUSED {subject}: E_SIG_REPLAY: envelope replay window exceeded (issued_at"
            " outside +/- 120 seconds)") in cap.err, cap.err
    assert isinstance(B.state_for(owner, subject), B.NoDeclaredActs)

    monkeypatch.setattr(D, "now_utc", real)
    rc = board_main(argv)
    cap = capsys.readouterr()
    assert rc in (0, 1), cap.out + cap.err    # sibling tests' rows may be refused; see above
    assert B.state_for(owner, subject).has("inferred.merged") is True, cap.out + cap.err


def test_the_same_number_under_two_spellings_is_surfaced_and_not_merged(owner):
    """The defect this closes is one I shipped.

    `derive.subjects_in` emits `ref/936` when the prose says only `#936`, which is right:
    it declines to guess whether the number names a pull request or an issue. The cost is
    that the board can hold `pr/936` and `ref/936` for one object, and a reader asking
    about the first silently misses the second. Measured on the real corpus, both spellings
    of #936 and of #100 were proposed in the same sweep.

    Both failure modes are asserted, because they pull in opposite directions: hiding the
    other spelling is a silent filter, and merging it asserts an identity nobody
    established.
    """
    p = _peer(owner, "alias")
    n = int(uuid.uuid4().int % 900000) + 100000
    _row(owner, p, D.act_value(subject=f"pr/{n}", verb="reviewed", ref=SHA,
                               actor_label=p["label"]))
    _row(owner, p, D.act_value(subject=f"ref/{n}", verb="merged", ref=OTHER_SHA,
                               actor_label=p["label"]))

    # NOT merged: the merge was recorded against a spelling that never said "pr".
    state = B.state_for(owner, f"pr/{n}")
    assert [a.verb for a in state.acts] == ["reviewed"]
    assert state.has("merged") is False, "a ref/N act must not answer for pr/N"

    # NOT hidden either.
    same = B.same_number_acts(owner, f"pr/{n}")
    assert list(same) == [f"ref/{n}"]
    assert [a.verb for a in same[f"ref/{n}"]] == ["merged"]
    prose = "\n".join(B.render_same_number(f"pr/{n}", same))
    assert f"ref/{n}" in prose and "NOT counted as acts on" in prose

    # The bridge runs both ways, and only through the unspecified spelling.
    assert set(B.same_number_subjects(f"ref/{n}")) == {f"pr/{n}", f"issue/{n}"}
    assert B.same_number_subjects(f"pr/{n}") == (f"ref/{n}",)
    assert f"issue/{n}" not in B.same_number_subjects(f"pr/{n}"), (
        "pr/N and issue/N are different objects that share a number; merging them would be "
        "the invention this module refuses")
    assert B.same_number_subjects("something-else") == ()


def test_a_subject_carrying_both_keeps_them_apart(owner):
    """The case that matters most: a real review AND a proposal on one subject. The
    declared one must win `has()`, and the inferred one must still be visible rather than
    hidden, because hiding it would be a silent filter."""
    p = _peer(owner, "both")
    subject = f"pr/both-{uuid.uuid4().hex[:8]}"
    _row(owner, p, D.act_value(subject=subject, verb="inferred.merged", ref=OTHER_SHA,
                               actor_label=p["label"], in_reply_to="msg/src-2"))
    _row(owner, p, D.act_value(subject=subject, verb="reviewed", ref=SHA,
                               actor_label=p["label"]))
    state = B.state_for(owner, subject)
    assert state.has("reviewed") is True
    assert state.has("merged") is False, "the merge was proposed, not declared"
    assert {a.verb for a in state.acts} == {"reviewed", "inferred.merged"}
    assert len(state.declared) == 1 and len(state.inferred) == 1
    prose = "\n".join(B.render(state))
    assert "1 declared act(s), plus 1 inferred (unconfirmed)" in prose
