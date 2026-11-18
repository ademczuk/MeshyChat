"""STATUS F09: a `running` call whose target is gone looks like a slow one until its deadline.

`apps/calls/heartbeat.py` gives every `running` call a heartbeat: the job records, per call,
the last time its TARGET held a live presence row (divergence 041 S2: a socket whose worker
beats every 10 s), and marks the call LOST once the target has been absent for longer than
`LOST_AFTER_SECS`. Since divergence 069 (the operator's ruling D-2026-10-03-1) a call still
lost `LOST_GRACE_SECS` later FAILS with E_TARGET_UNREACHABLE; the tests for that are at the end
of this file. What the first tests hold is the evidence that failure acts on:

  * a call whose target is online is beaten, never lost (the positive control);
  * a call whose target has been absent past the window is lost, and one absent for less is
    not (the window is a bound, not a guess);
  * only `running` calls: a `queued` call to the same absent target is not a heartbeat's
    business (its target was never working on it);
  * the memory authority's in-process calls are not judged by a socket they never had;
  * a lost call whose target comes back is beaten again and no longer lost;
  * the tick that first finds a call lost changes no status, and `lost_calls` lists exactly
    the lost ones.

Presence rows are written straight into the table by the owner, as a worker's beat would
leave them, with `seen_at` set to the age a test needs.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_call_heartbeat.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn  # noqa: E402
from apps.jobs import registry, runner  # noqa: E402
from tests.fixtures.call_rows import seed_call, seed_peer, seed_skill  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


#: The presence rows `_present` wrote, so the fixture can take them out again: a test that leaves
#: `hub_connections` rows behind makes tests/test_hub_presence.py refuse to run after it (it asserts
#: the table starts empty, design section 6). The whole gate hid that, because other files run in
#: between; two files run together did not.
_PRESENCE_ROWS: list = []


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        try:
            yield c
        finally:
            if _PRESENCE_ROWS:
                c.execute("DELETE FROM hub_connections WHERE connection_id = ANY(%s)",
                          (list(_PRESENCE_ROWS),))
                _PRESENCE_ROWS.clear()


def _run(max_batch: int | None = None) -> runner.Outcome:
    base = registry.by_name("running_call_heartbeat")
    job = registry.Job(name=f"t_heartbeat_{uuid.uuid4().hex[:8]}",
                       interval_secs=base.interval_secs, timeout_secs=base.timeout_secs,
                       jitter_frac=base.jitter_frac,
                       max_batch=base.max_batch if max_batch is None else max_batch,
                       run=base.run)
    try:
        (out,) = runner.run_due(lambda **kw: dbconn.connect(DSN, **kw), jobs=(job,),
                                holder="heartbeat-test")
    finally:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job.name,))
    assert out.claimed is True and out.result == "ok", out
    return out


def _drain() -> None:
    for _ in range(50):
        if not _run().more_work_pending:
            return
    raise AssertionError("the heartbeat never finished a round")


def _present(owner, peer, *, seen: str = "now()") -> None:
    """A socket for `peer`, last beaten at `seen`."""
    connection_id = uuid.uuid4()
    owner.execute(f"INSERT INTO hub_connections (connection_id, peer_id, worker_token, "
                  f"seen_at) VALUES (%s, %s, %s, {seen})", (connection_id, peer["peer_id"],
                                                           uuid.uuid4()))
    _PRESENCE_ROWS.append(connection_id)


def _running(owner, caller, target, skill, *, started: str, status: str = "running") -> str:
    call_id = seed_call(owner, caller, target, skill, call_id=f"f09-{uuid.uuid4().hex}")
    owner.execute(f"UPDATE skill_calls SET status = %s, started_at = {started}, "
                  f"created_at = {started} WHERE call_id = %s", (status, call_id))
    return call_id


def _beat(owner, call_id) -> tuple | None:
    return owner.execute("SELECT beat_at IS NOT NULL, lost_at IS NOT NULL FROM "
                         "skill_call_heartbeats WHERE call_id = %s", (call_id,)).fetchone()


def _status(owner, call_ids) -> dict:
    return dict(owner.execute("SELECT call_id, status FROM skill_calls WHERE call_id = ANY(%s)",
                              (list(call_ids),)).fetchall())


def test_f09_a_running_call_whose_target_is_gone_past_the_window_is_lost(owner):
    from apps.calls import heartbeat
    caller = seed_peer(owner, "f09c")
    online, gone, blip = (seed_peer(owner, t) for t in ("f09on", "f09gone", "f09blip"))
    skills = {id(p): seed_skill(owner, p) for p in (online, gone, blip)}
    long_ago = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    just_now = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS // 4})"
    _present(owner, online)
    _present(owner, blip, seen=long_ago)                   # an old, stale socket row
    calls = {
        "online": _running(owner, caller, online, skills[id(online)], started=long_ago),
        "gone": _running(owner, caller, gone, skills[id(gone)], started=long_ago),
        "fresh": _running(owner, caller, gone, skills[id(gone)], started=just_now),
        "queued": _running(owner, caller, gone, skills[id(gone)], started=long_ago,
                           status="queued"),
        "blip": _running(owner, caller, blip, skills[id(blip)], started=long_ago),
    }

    _drain()

    assert _beat(owner, calls["online"]) == (True, False), "an online target's call is beaten"
    assert _beat(owner, calls["gone"])[1] is True, "absent past the window: lost"
    assert _beat(owner, calls["blip"])[1] is True, "a stale socket row is not presence"
    fresh = _beat(owner, calls["fresh"])
    assert fresh is None or fresh[1] is False, "absent for less than the window: not lost"
    assert _beat(owner, calls["queued"]) is None, "a queued call has no heartbeat"
    expected = {calls[k]: "running" for k in ("online", "gone", "fresh", "blip")}
    expected[calls["queued"]] = "queued"
    assert _status(owner, calls.values()) == expected, (
        "the tick that first finds a call lost changes no status; it fails only after the "
        "grace (divergence 069, the tests at the end of this file)")
    with dbconn.connect(DSN) as app:
        lost = {r["call_id"] for r in heartbeat.lost_calls(app)}
    assert lost >= {calls["gone"], calls["blip"]}
    assert not lost & {calls["online"], calls["fresh"], calls["queued"]}


def test_f09_a_lost_call_whose_target_returns_is_beaten_again(owner):
    from apps.calls import heartbeat
    caller, target = seed_peer(owner, "f09rc"), seed_peer(owner, "f09rt")
    skill = seed_skill(owner, target)
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    call_id = _running(owner, caller, target, skill, started=started)
    _drain()
    assert _beat(owner, call_id)[1] is True, "control: lost while the target is away"
    _present(owner, target)
    _drain()
    assert _beat(owner, call_id) == (True, False), "the target is back: beaten, not lost"


def test_f09_the_window_runs_from_the_last_beat_not_from_the_start_of_the_call(owner):
    """Review finding 3 (MEDIUM): the 120 s absence is measured from the call's LAST BEAT.
    A call started long ago (3 windows) is beaten while its target is online; the target
    then drops off (its socket row stale). Next round: NOT lost, because the last beat is
    recent; a window measured from `started_at` would mark it lost on the first tick after a
    30 s blip. Then the beat is aged past the window, as 120 s of absence would age it: LOST.
    CONTROL: a second call to another absent target, started as long ago and never beaten,
    is lost in the first of those rounds."""
    from apps.calls import heartbeat
    caller, target, other = (seed_peer(owner, t) for t in ("f09wc", "f09wt", "f09wo"))
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    call_id = _running(owner, caller, target, seed_skill(owner, target), started=started)
    never = _running(owner, caller, other, seed_skill(owner, other), started=started)
    _present(owner, target)
    _drain()
    assert _beat(owner, call_id) == (True, False), "control: beaten while its target is online"

    owner.execute("UPDATE hub_connections SET seen_at = now() - interval '1 hour' "
                  "WHERE peer_id = %s", (target["peer_id"],))
    _drain()
    assert _beat(owner, call_id) == (True, False), (
        "absent for less than the window since the last beat: not lost")
    assert _beat(owner, never)[1] is True, "control: never beaten, absent: lost"

    with owner.transaction():
        owner.execute("SELECT set_config('app.server_internal', '1', true)")
        owner.execute("UPDATE skill_call_heartbeats SET beat_at = now() - make_interval("
                      "secs => %s) WHERE call_id = %s", (heartbeat.LOST_AFTER_SECS + 30, call_id))
    _drain()
    assert _beat(owner, call_id) == (True, True), "absent past the window since the last beat"


def test_f09_the_memory_authority_is_not_judged_by_a_socket_it_never_had(owner):
    """A call to the memory authority runs in-process (apps/calls/route.py); the authority
    holds no socket, so presence says nothing about it. CONTROL: a remote call of the same
    age with no socket is lost in the same tick."""
    from django.conf import settings
    from apps.calls import heartbeat
    caller = seed_peer(owner, "f09mc")
    mem_id = uuid.UUID(str(settings.MESHY_MEMORY_PEER_ID))
    exists = owner.execute("SELECT owner_id FROM peers WHERE id = %s", (mem_id,)).fetchone()
    if exists is None:
        owner_id = uuid.uuid4()
        owner.execute("INSERT INTO dashboard_users (id, email, plane) VALUES (%s,%s,'product')",
                      (owner_id, f"f09-mem-{owner_id.hex[:8]}@example.test"))
        owner.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                      (mem_id, owner_id, f"f09-mem-{mem_id.hex[:8]}"))
        exists = (owner_id,)
    memory = {"peer_id": mem_id, "owner_id": exists[0]}
    remote = seed_peer(owner, "f09rem")
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    mem_call = _running(owner, caller, memory, seed_skill(owner, memory), started=started)
    rem_call = _running(owner, caller, remote, seed_skill(owner, remote), started=started)
    _drain()
    assert _beat(owner, mem_call) is None
    assert _beat(owner, rem_call)[1] is True


def test_f09_the_job_is_registered_bounded_and_rotates_through_every_running_call(owner):
    """B5: one tick reads at most max_batch running calls, by (deadline_at, call_id) past a
    cursor, and says more is pending; the following ticks reach the rest."""
    from apps.calls import heartbeat
    base = registry.by_name("running_call_heartbeat")
    assert base.max_batch == heartbeat.PER_TICK and base.interval_secs <= 30
    assert heartbeat.LOST_AFTER_SECS > 3 * base.interval_secs
    caller, target = seed_peer(owner, "f09bc"), seed_peer(owner, "f09bt")
    skill = seed_skill(owner, target)
    _present(owner, target)
    ids = [_running(owner, caller, target, skill, started="now()") for _ in range(5)]
    for _ in range(50):
        out = _run(max_batch=2)
        assert out.rows <= 2
        if all(_beat(owner, c) == (True, False) for c in ids):
            break
    else:
        raise AssertionError("a bounded rotation never reached every running call")


# ---- divergence 069 (ASDLC D-2026-10-03-1, OI-66): a lost call fails after the grace period ----


def _age_loss(owner, call_id, secs: int) -> None:
    """Move the call's `lost_at` `secs` into the past, as that long of continued absence would."""
    with owner.transaction():
        owner.execute("SELECT set_config('app.server_internal', '1', true)")
        owner.execute("UPDATE skill_call_heartbeats SET lost_at = now() - make_interval(secs => %s)"
                      " WHERE call_id = %s AND lost_at IS NOT NULL", (secs, call_id))


def _ended(owner, call_id) -> tuple:
    return owner.execute("SELECT status, error_code, error_message IS NOT NULL, "
                         "completed_at IS NOT NULL FROM skill_calls WHERE call_id = %s",
                         (call_id,)).fetchone()


def _events(owner, call_id) -> list:
    return owner.execute("SELECT event_type, source, details->>'code' FROM skill_call_events "
                         "WHERE call_id = %s AND event_type = 'failed'", (call_id,)).fetchall()


def test_069_a_lost_call_fails_once_its_grace_is_over_and_not_before(owner):
    """Two calls to absent targets, both lost. One has been lost for longer than the grace, the
    other for less (the control): only the first fails, with E_TARGET_UNREACHABLE,
    a message, a completion time, and one `failed` event from the `meshy` compatibility source carrying the code."""
    from apps.calls import heartbeat
    caller, t1, t2 = (seed_peer(owner, t) for t in ("g69c", "g69a", "g69b"))
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    over = _running(owner, caller, t1, seed_skill(owner, t1), started=started)
    inside = _running(owner, caller, t2, seed_skill(owner, t2), started=started)
    _drain()
    assert _beat(owner, over)[1] is True and _beat(owner, inside)[1] is True, "control: both lost"
    assert _status(owner, [over, inside]) == {over: "running", inside: "running"}, (
        "the tick that first finds a call lost does not fail it")

    _age_loss(owner, over, heartbeat.LOST_GRACE_SECS + 30)
    _age_loss(owner, inside, heartbeat.LOST_GRACE_SECS - 30)
    _drain()

    assert heartbeat.E_TARGET_UNREACHABLE == "E_TARGET_UNREACHABLE", "the unreachable-target code is stable"
    assert _ended(owner, over) == ("failed", heartbeat.E_TARGET_UNREACHABLE, True, True)
    assert _events(owner, over) == [("failed", "meshy", heartbeat.E_TARGET_UNREACHABLE)]
    assert _ended(owner, inside)[0] == "running", "lost for less than the grace: still running"
    assert _events(owner, inside) == []
    with dbconn.connect(DSN) as app:
        lost = {r["call_id"] for r in heartbeat.lost_calls(app)}
    assert over not in lost and inside in lost, "a failed call is no longer listed as lost"


def test_069_a_target_that_returns_after_the_grace_is_beaten_not_failed(owner):
    """The grace is over but the target is online at the tick: the call is beaten and runs on.
    CONTROL: a second call, its target still absent, lost as long, fails in the same tick."""
    from apps.calls import heartbeat
    caller, back, away = (seed_peer(owner, t) for t in ("g69rc", "g69back", "g69away"))
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    returns = _running(owner, caller, back, seed_skill(owner, back), started=started)
    stays = _running(owner, caller, away, seed_skill(owner, away), started=started)
    _drain()
    for c in (returns, stays):
        _age_loss(owner, c, heartbeat.LOST_GRACE_SECS + 30)
    _present(owner, back)
    _drain()
    assert _ended(owner, returns)[0] == "running" and _beat(owner, returns) == (True, False)
    assert _ended(owner, stays)[0] == "failed", "control: absent past the grace fails"


def test_069_the_failure_rereads_its_guard_under_the_lock(owner):
    """`_fail_expired` is handed ids from the tick's read; between that read and the write a
    call can complete, or its target can return and clear `lost_at`. Neither may be failed.
    CONTROL: a third id, still running and still lost past the grace, is failed by the same
    statement."""
    from psycopg.rows import dict_row
    from apps.calls import heartbeat
    from apps.core import rls
    caller, a, b, c = (seed_peer(owner, t) for t in ("g69gc", "g69ga", "g69gb", "g69gd"))
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    done = _running(owner, caller, a, seed_skill(owner, a), started=started)
    cleared = _running(owner, caller, b, seed_skill(owner, b), started=started)
    due = _running(owner, caller, c, seed_skill(owner, c), started=started)
    _drain()
    for x in (done, cleared, due):
        _age_loss(owner, x, heartbeat.LOST_GRACE_SECS + 30)
    owner.execute("UPDATE skill_calls SET status = 'completed', completed_at = now() "
                  "WHERE call_id = %s", (done,))
    with owner.transaction():
        owner.execute("SELECT set_config('app.server_internal', '1', true)")
        owner.execute("UPDATE skill_call_heartbeats SET lost_at = NULL WHERE call_id = %s",
                      (cleared,))
    with dbconn.connect(DSN) as app, rls.server_internal(app):
        with app.cursor(row_factory=dict_row) as cur:
            failed = heartbeat._fail_expired(cur, [done, cleared, due], heartbeat.LOST_GRACE_SECS)
    assert failed == [due]
    assert _ended(owner, done)[0] == "completed" and _events(owner, done) == []
    assert _ended(owner, cleared)[0] == "running" and _events(owner, cleared) == []


def test_069_the_grace_setting_is_a_positive_whole_number_or_refused():
    from django.test import override_settings
    from apps.calls import heartbeat
    with override_settings(MESHY_LOST_CALL_GRACE_SECS=None):
        assert heartbeat.lost_grace_secs() == heartbeat.LOST_GRACE_SECS
    with override_settings(MESHY_LOST_CALL_GRACE_SECS=" 45 "):
        assert heartbeat.lost_grace_secs() == 45
    for bad in ("0", "-5", "soon", "1.5"):
        with override_settings(MESHY_LOST_CALL_GRACE_SECS=bad):
            with pytest.raises(ValueError):
                heartbeat.lost_grace_secs()


def test_069_the_setting_is_the_grace_the_job_uses(owner):
    """With the grace set to 5 s, a call lost 20 s ago fails; at the default it would not
    (CONTROL, the same call aged the same, before the override)."""
    from django.test import override_settings
    from apps.calls import heartbeat
    caller, t = seed_peer(owner, "g69sc"), seed_peer(owner, "g69st")
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    call_id = _running(owner, caller, t, seed_skill(owner, t), started=started)
    _drain()
    _age_loss(owner, call_id, 20)
    _drain()
    assert _ended(owner, call_id)[0] == "running", "control: 20 s is inside the default grace"
    with override_settings(MESHY_LOST_CALL_GRACE_SECS="5"):
        _drain()
    assert _ended(owner, call_id)[0] == "failed"


def test_069_a_target_online_at_the_write_is_not_failed(owner):
    """The tick reads presence per target, then writes; a target that reconnects between the
    two is online when the call is failed. The ruling fails a call whose target is UNREACHABLE,
    so the write checks presence itself. Both calls are lost past the grace; one target has a
    live socket by the time of the write (no tick runs in between, as in the race). CONTROL:
    the call whose target is still absent, in the same statement, is failed."""
    from psycopg.rows import dict_row
    from apps.calls import heartbeat
    from apps.core import rls
    caller, back, away = (seed_peer(owner, t) for t in ("g69pc", "g69pb", "g69pa"))
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    returned = _running(owner, caller, back, seed_skill(owner, back), started=started)
    absent = _running(owner, caller, away, seed_skill(owner, away), started=started)
    _drain()
    for c in (returned, absent):
        _age_loss(owner, c, heartbeat.LOST_GRACE_SECS + 30)
    _present(owner, back)                      # the target reconnects after the tick's read
    with dbconn.connect(DSN) as app, rls.server_internal(app):
        with app.cursor(row_factory=dict_row) as cur:
            failed = heartbeat._fail_expired(cur, [returned, absent], heartbeat.LOST_GRACE_SECS)
    assert failed == [absent]
    assert _ended(owner, returned)[0] == "running" and _events(owner, returned) == []
    assert _ended(owner, absent)[0] == "failed"


def test_069_the_grace_is_read_from_the_environment_when_no_setting_names_it(monkeypatch):
    """`MESHY_LOST_CALL_GRACE_SECS` is a deployment variable like the repo's others
    (apps/core/env.py `setting`): a setting wins, else the environment, else the default. A
    value the environment gives is held to the same rule as a setting (positive, whole)."""
    from django.test import override_settings
    from apps.calls import heartbeat
    monkeypatch.setenv("MESHY_LOST_CALL_GRACE_SECS", "45")
    with override_settings(MESHY_LOST_CALL_GRACE_SECS=None):
        assert heartbeat.lost_grace_secs() == 45, "the environment variable reaches the job"
    with override_settings(MESHY_LOST_CALL_GRACE_SECS="60"):
        assert heartbeat.lost_grace_secs() == 60, "a setting beats the environment"
    monkeypatch.setenv("MESHY_LOST_CALL_GRACE_SECS", "0")
    with override_settings(MESHY_LOST_CALL_GRACE_SECS=None):
        with pytest.raises(ValueError):
            heartbeat.lost_grace_secs()
    monkeypatch.delenv("MESHY_LOST_CALL_GRACE_SECS")
    with override_settings(MESHY_LOST_CALL_GRACE_SECS=None):
        assert heartbeat.lost_grace_secs() == heartbeat.LOST_GRACE_SECS, "unset: the default"


def test_069_a_call_the_heartbeat_fails_has_its_chunks_trimmed_at_terminal(owner):
    """The janitor trims a call's chunks to the newest CHUNK_KEEP_LAST when it ends the call
    (apps/calls/janitor.py); the heartbeat's failure does the same. 205 chunks in, 200 left, the
    oldest five gone. CONTROL: a call lost for less than the grace is not ended and keeps all 205."""
    from apps.calls import chunks, heartbeat
    caller, t1, t2 = (seed_peer(owner, t) for t in ("g69tc", "g69ta", "g69tb"))
    started = f"now() - make_interval(secs => {heartbeat.LOST_AFTER_SECS * 3})"
    ended = _running(owner, caller, t1, seed_skill(owner, t1), started=started)
    kept = _running(owner, caller, t2, seed_skill(owner, t2), started=started)
    for c in (ended, kept):
        owner.execute("INSERT INTO skill_call_chunks (call_id, sequence, chunk_type, content, "
                      "chunk_signature, chunk_signing_kid) "
                      "SELECT %s, g, 'text', 'x'::bytea, 's', 'k' FROM generate_series(0, 204) g",
                      (c,))
    _drain()
    _age_loss(owner, ended, heartbeat.LOST_GRACE_SECS + 30)
    _age_loss(owner, kept, heartbeat.LOST_GRACE_SECS - 30)
    _drain()
    assert _ended(owner, ended)[0] == "failed" and _ended(owner, kept)[0] == "running"
    count = "SELECT count(*), min(sequence) FROM skill_call_chunks WHERE call_id = %s"
    assert owner.execute(count, (ended,)).fetchone() == (chunks.CHUNK_KEEP_LAST, 5)
    assert owner.execute(count, (kept,)).fetchone() == (205, 0), "control: a running call is not trimmed"
