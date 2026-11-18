"""Does the board actually answer the question it was built for?

`sampleco/ASDLC` `intent.md` names the failure, measured on 2026-09-20:

    "'uxxx is holding' and 'the fix is unopened' read the same from the outside and they
     are not the same."

`intent.md` also names the falsifier: "If board-derived state disagrees with ground truth,
the read model is wrong and the schema needs changing before anything is built on it."

These tests are that falsifier, run against constructed ground truth. WHAT THEY CANNOT DO,
stated first because it is the honest limit: the real acts for pr/924, 929 and 930 live in
the production mesh, and measured adoption there was 1 row in 300. So this establishes that
the READ MODEL is correct on cases where the truth is known by construction. It does not
establish that the board is populated enough to be useful, which is a different claim and
is the one `adoption()` exists to keep visible.

ISOLATION. Everything is seeded on an owner connection inside a transaction that is rolled
back, so the shared dev database keeps its rows and a parallel run in another session is
unaffected. The owner role bypasses RLS, which is what makes the rollback possible; the CLI
enters `server_internal` for the same reads, matching how the app role reaches this table.

Run:  pytest tests/test_board_projection.py -v
Needs MESHY_TEST_DSN (defaults to the dev stack database built by sqlmigrate.py).
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.board import projection as B  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
NS = B.MESH_CHAT_NAMESPACE


@pytest.fixture()
def conn():
    """Owner connection, NOT autocommit. Every test's writes are rolled back.

    The mesh namespace row is created here rather than assumed. `shared_memory` has a
    foreign key to `shared_memory_namespace`, and this dev database may or may not carry
    the mesh namespace depending on what else has run against it. ON CONFLICT DO NOTHING
    makes the fixture work in both cases without deciding which one it is in.
    """
    with psycopg.connect(DSN) as c:
        c.autocommit = False
        owner = _peer(c, "nsowner")
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO shared_memory_namespace"
                " (namespace, owner_peer_id, owner_owner_id, claimant_plane,"
                "  ephemeral, retention_policy)"
                " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                (NS, owner["peer_id"], owner["owner_id"]))
        yield c
        c.rollback()


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
            " VALUES (%s,%s,'dev',%s)",
            (owner_id, f"board-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name)"
                    " VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"board-{tag}-{peer_id.hex[:8]}", f"board {tag}"))
    return {"peer_id": peer_id, "owner_id": owner_id, "label": tag}


def _row(conn, peer, value: dict, *, created_at: _dt.datetime | None = None) -> None:
    """One mesh row. `value` is the envelope; omit `subject` for a plain message.

    `provenance_call_id` is a GENERATED column defined as
    `provenance_jsonb -> 'payload' ->> 'call_id'`, and it is NOT NULL, so provenance
    must actually carry a call id rather than an empty object.
    """
    rid = uuid.uuid4()
    when = created_at or _dt.datetime.now(_dt.timezone.utc)
    # With the bytes a writer would have signed: since A2 (divergence 051) a row without
    # them is refused from every session. provenance_call_id stays GENERATED from them.
    sr.insert(conn, sr.row(peer["peer_id"], peer["owner_id"], NS,
                           f"msg/{int(when.timestamp()*1000)}-{rid.hex}", value, rid=rid,
                           call_id=f"call-{rid}", author_label=peer["label"], created_at=when))


def _act(conn, peer, subject, verb, *, ref="abc123", ts=None, claims_from=None,
         created_at=None, schema_version=1):
    value = {
        "schema_version": schema_version, "to": ["all"], "from": claims_from or peer["label"],
        "type": verb, "thread_id": "t1", "ts": ts or "2026-09-21T12:00:00Z",
        "body": f"{peer['label']} {verb} {subject}", "author_family": "claude",
        "subject": subject, "ref": ref,
    }
    _row(conn, peer, value, created_at=created_at)


def _message(conn, peer, body: str, created_at=None):
    """A row with NO subject. Prose, not an act."""
    _row(conn, peer, {
        "schema_version": 1, "to": ["all"], "from": peer["label"], "type": "note",
        "thread_id": "t1", "ts": "2026-09-21T12:00:00Z", "body": body,
        "author_family": "claude",
    }, created_at=created_at)


def _subj(tag: str) -> str:
    """A subject unique to this test run, so a rolled-back sibling cannot collide."""
    return f"pr/{tag}-{uuid.uuid4().hex[:8]}"


# ======================================================================= THE EVAL

def test_a_held_subject_and_an_unopened_subject_are_distinguishable(conn):
    """THE FAILURE THE BOARD EXISTS FOR, reproduced and then resolved.

    Under prose reconstruction these two look identical from outside: neither has a merge.
    The board must say that one was reviewed and is being held, and the other has had
    nothing done to it at all.
    """
    uxxx = _peer(conn, "uxxx")
    held = _subj("held")          # reviewed, deliberately not merged: "uxxx is holding"
    unopened = _subj("unopened")  # nothing ever declared: "the fix is unopened"

    _act(conn, uxxx, held, "reviewed")

    held_state = B.state_for(conn, held)
    unopened_state = B.state_for(conn, unopened)

    # They are different TYPES, so a caller cannot collapse them by accident.
    assert isinstance(held_state, B.SubjectState)
    assert isinstance(unopened_state, B.NoDeclaredActs)

    # The held one says what happened and what did not.
    assert held_state.has("reviewed")
    assert not held_state.has("merged"), "nothing declared a merge; it must not appear"
    assert held_state.latest_by_verb["reviewed"].actor_label == "uxxx"

    # And the prose rendering says it in words, because a structured field alone is
    # absent to a reader whose reading is the prose.
    held_text = "\n".join(B.render(held_state))
    unopened_text = "\n".join(B.render(unopened_state, adoption_pair=B.adoption(conn)))
    assert "reviewed" in held_text
    assert "no act has ever been declared" in unopened_text
    assert held_text != unopened_text, (
        "the two states rendered identically, which is the original failure"
    )


def test_absence_is_typed_rather_than_an_empty_list(conn):
    """B3. An empty list invites `if not acts:` and collapses the distinction above."""
    missing = B.state_for(conn, _subj("never"))
    assert isinstance(missing, B.NoDeclaredActs)
    assert not isinstance(missing, B.SubjectState)
    assert not hasattr(missing, "acts"), (
        "NoDeclaredActs must not carry an empty `acts`, or a caller reads it as a state"
    )
    # Positive control: the same call SHAPE returns a state when acts exist.
    p = _peer(conn, "ctrl")
    s = _subj("ctrl")
    _act(conn, p, s, "opened")
    assert isinstance(B.state_for(conn, s), B.SubjectState)


def test_the_actor_is_the_authenticated_peer_not_the_body_claim(conn):
    """The measured reason `subject` and `ref` exist rather than a prose extractor.

    A projector over prose attributed all nine `reviewed` edges on pr/882 to the peer
    REPORTING a review rather than the one who ran it. Here the body lies on purpose.
    """
    real = _peer(conn, "realactor")
    s = _subj("attrib")
    _act(conn, real, s, "reviewed", claims_from="someone-else-entirely")

    state = B.state_for(conn, s)
    act = state.acts[0]
    assert act.actor_peer_id == str(real["peer_id"]), "the authenticated peer must win"
    assert act.actor_label == "realactor"
    assert "someone-else-entirely" not in "\n".join(B.render(state)), (
        "a name from the body reached the projection"
    )


def test_ordering_uses_the_server_clock_not_the_actors(conn):
    """Defect 2 in the earlier board script: it sorted on the actor's own `ts`.

    Sender clocks on this fleet have been measured future-dated by tens of minutes, so an
    actor who sets `ts` must not be able to reorder history.
    """
    p = _peer(conn, "clock")
    s = _subj("order")
    now = _dt.datetime.now(_dt.timezone.utc)

    # Recorded FIRST, but claims a far-future client time.
    _act(conn, p, s, "opened", ts="2099-01-01T00:00:00Z",
         created_at=now - _dt.timedelta(minutes=10))
    # Recorded SECOND, claims an ordinary time.
    _act(conn, p, s, "reviewed", ts="2026-09-21T12:00:00Z", created_at=now)

    verbs = [a.verb for a in B.state_for(conn, s).acts]
    assert verbs == ["opened", "reviewed"], (
        f"acts came back {verbs}; ordering followed the actor's claimed clock rather than "
        "the server's, so an actor can reorder the board by setting a field"
    )
    # And the disagreement is REPORTED rather than silently deciding anything.
    opened = B.state_for(conn, s).acts[0]
    assert opened.clock_is_suspect
    assert "CLOCK SKEW" in "\n".join(B.render(B.state_for(conn, s)))


def test_a_prose_message_is_not_an_act(conn):
    """The board replaces state RECONSTRUCTION, not the message. Prose stays prose."""
    p = _peer(conn, "prose")
    s = _subj("prose")
    _message(conn, p, f"I reviewed {s} and I am holding it")   # says it, declares nothing
    assert isinstance(B.state_for(conn, s), B.NoDeclaredActs), (
        "a message whose BODY mentions the subject was counted as a declared act"
    )
    # Positive control: an actual act on the same subject is found.
    _act(conn, p, s, "reviewed")
    assert isinstance(B.state_for(conn, s), B.SubjectState)


def test_an_old_act_is_still_found_behind_newer_traffic(conn):
    """Defect 1 in the earlier board script: it scanned the most recent 500 rows.

    With enough newer messages an older act fell outside the window and the tool printed
    nothing, which is kxxx's exact ambiguity recurring inside the tool built to remove it.
    """
    p = _peer(conn, "window")
    s = _subj("old")
    old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)
    _act(conn, p, s, "opened", created_at=old)
    for i in range(50):                       # newer, unrelated traffic on top
        _message(conn, p, f"chatter {i}")

    state = B.state_for(conn, s)
    assert isinstance(state, B.SubjectState), "an act older than surrounding traffic was lost"
    assert state.acts[0].verb == "opened"


def test_adoption_reports_a_denominator(conn):
    """A count of declared acts without its denominator is a fact about adoption being
    read as a fact about whether work happened."""
    p = _peer(conn, "adopt")
    before_declared, before_total = B.adoption(conn)
    _act(conn, p, _subj("a"), "opened")
    _message(conn, p, "just talking")
    declared, total = B.adoption(conn)
    assert declared == before_declared + 1
    assert total == before_total + 2, "the denominator must count messages too"


def test_the_subject_lookup_does_not_scan_the_namespace(conn):
    """B5. A per-request projection over a whole namespace is the shape that rule forbids.

    `sql_meshychat/003` adds a partial index so this is an index scan over one subject.
    """
    p = _peer(conn, "cost")
    s = _subj("cost")
    _act(conn, p, s, "opened")
    for i in range(200):
        _message(conn, p, f"filler {i}")

    with conn.cursor() as cur:
        cur.execute("ANALYZE shared_memory")
        cur.execute(
            "EXPLAIN (FORMAT JSON) SELECT key FROM shared_memory "
            "WHERE namespace = %s AND value ? 'subject' AND value ->> 'subject' = %s "
            "AND is_head = TRUE", (NS, s))
        plan = json.dumps(cur.fetchone()[0])

    assert "shared_memory_declared_act_idx" in plan, (
        f"the subject lookup is not using the declared-act index, so it reads the whole "
        f"namespace on every request. Plan: {plan}"
    )


def test_the_cli_runs_as_a_module_entry_point():
    """The ENTRY POINT, not the function.

    On 2026-09-21 `apps/core/sqlmigrate.py` broke the moment it grew an `apps.` import,
    because running a file BY PATH puts its own directory on sys.path rather than the
    repository root. Its tests could not catch it: they called main() in process, where
    pytest had already fixed sys.path. `apps.board` is invoked with `-m` for that reason,
    and this asserts it, as a subprocess, with PYTHONPATH stripped so a developer who
    happens to export one does not mask a failure for everyone who does not.
    """
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([sys.executable, "-m", "apps.board", "--help"],
                         cwd=str(Path(__file__).resolve().parent.parent),
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, f"rc={out.returncode} stderr={out.stderr[:400]}"
    assert "--subject" in out.stdout
