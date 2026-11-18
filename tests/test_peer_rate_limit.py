"""B12: a per-peer token bucket that every transport's entry calls (docs/DIVERGENCES.md 003).

Divergence 003 dropped quota with billing and named the trigger for this: "the callers are
agents, and agents loop by construction". A 1,000,000-call poller demonstrated the need for a
per-peer token bucket, no tiers, no plans, in ONE service function every transport calls. That
function is `apps.core.peer_limit.admit`. Where it is called:

  * `apps.calls.pipeline.start_call`, first, before the call_id lock or any database work:
    every call on every transport (HTTP invoke, A2A `message:send`, a WS `skill_call`, the
    dashboard's test call), which is also every memory skill (they execute at step 9).
  * `apps.wsgw.consumer` for the socket's own frames (ping, subscribe, unsubscribe,
    skill_cancel, an unknown type), in a SEPARATE bucket of the same peer, so a peer that
    spent its calls still gets its pong and its presence refresh. A skill_call frame is not
    charged there (the pipeline charges it; caller_frames only READS the bucket first, so a
    refused one never reaches a pipeline thread). A target frame that lands is not charged
    (it answers a call charged to its caller, and a stream must not be cut); one that is
    refused is, and one that meets an empty FRAMES bucket is refused before its statement
    (review a65ab61f MEDIUM 1, frame_handlers._target_frame).
  * the capability routes' wrapper (mint, revoke), after authentication, and the board's
    write door (a memory write that does not go through the pipeline).
  * the mesh transport after authentication (review MEDIUM 2): the graph routes and the wait
    (apps/mesh/views), search and context (apps/meshsem/views), the summaries
    (apps/meshsum/views).

The review round (a65ab61f) replaced the test that asserted the target-frame bypass and added
thirteen. RED on 26f1525, before `check`'s own test was added: 7 failed, 16 passed. The three
control-frame tests pass there (they close the LOW 4 gap; their RED is mutant Y1, ping only).

RED is the unchanged tree (11 failed, 1 passed): `apps.core.peer_limit` does not exist, and
every wire test meets the call past its burst ANSWERED (202 where 429 is expected, a
skill_acked where the refusal is expected, a pong where RATE_LIMITED is expected, 201 from the
capability and board wrappers), because the settings they set are read by nothing. The module is imported per use, so each test fails where its behaviour is
missing, not at collection.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_peer_rate_limit.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import uuid

import pytest

# The S5 harness first: it configures Django (tests/test_call_delivery.py), and its autouse
# fixtures (server_config, bridges_stopped) apply here too.
from tests.test_call_delivery import (  # noqa: F401  (fixtures are used by name)
    NEVER,
    _connect,
    _invoke,
    _invoke_frame,
    _next,
    _pong,
    _status,
    _until,
    bridges_stopped,
    invoke,
    owner,
    server_config,
    signed_call,
    signed_result,
    tuned,
    world,
)
from tests.fixtures.hub_presence_db import hub_connections_clean  # noqa: E402,F401 (autouse)
from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.test import RequestFactory, override_settings  # noqa: E402

from apps.calls import errors as rpc_errors  # noqa: E402
from apps.calls import pipeline, wire  # noqa: E402
from apps.calls.envelope import SignedEnvelope  # noqa: E402


def _pl():
    return importlib.import_module("apps.core.peer_limit")


@pytest.fixture(autouse=True)
def fresh_buckets():
    """The limiter is process-wide, like the sign-in limiter (apps/authapi/ratelimit.py)."""
    try:
        _pl().reset()
    except ImportError:
        pass
    yield
    try:
        _pl().reset()
    except ImportError:
        pass


def tiny(burst: int = 2, per_sec: float = 0.001):
    """A bucket a test can empty in `burst` requests and that does not refill inside it."""
    return override_settings(MESHY_PEER_RATE_BURST=burst, MESHY_PEER_RATE_PER_SEC=per_sec)


# ------------------------------------------------------------------ the bucket itself

def test_a_peer_gets_its_burst_then_a_refusal_that_says_when_to_retry():
    pl = _pl()
    bucket = pl.PeerBuckets(burst=3, refill_per_sec=0.5)
    peer = uuid.uuid4()
    assert [bucket.take(f"calls:{peer}", now=100.0) for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = bucket.take(f"calls:{peer}", now=100.0)
    assert wait == pytest.approx(2.0), "one token at 0.5/s is two seconds away"
    # POSITIVE CONTROL: the refusal is the empty bucket, not a broken one. Two seconds on,
    # exactly one token is back, and only one.
    assert bucket.take(f"calls:{peer}", now=102.0) == 0.0
    assert bucket.take(f"calls:{peer}", now=102.0) > 0.0


def test_admit_raises_with_a_whole_retry_after_and_the_transport_does_not_matter():
    """ONE bucket per peer across transports: a past incident left the WebSocket transport
    unmetered while HTTP reserved, which bypassed the limit. Spending it over HTTP must refuse
    the same peer over WS."""
    pl = _pl()
    peer, other = uuid.uuid4(), uuid.uuid4()
    with tiny(burst=2, per_sec=0.25):
        pl.admit(peer, transport="http")
        pl.admit(str(peer).upper(), transport="a2a")     # the same peer, however spelled
        with pytest.raises(pl.PeerRateLimited) as refused:
            pl.admit(peer, transport="ws")
        assert refused.value.retry_after == 4 and refused.value.transport == "ws"
        assert refused.value.code == "E_RATE_LIMITED"
        assert "4" in refused.value.message
        # POSITIVE CONTROL: another peer's bucket is its own.
        pl.admit(other, transport="ws")


def test_a_peers_frames_are_a_separate_bucket_from_its_calls():
    pl = _pl()
    peer = uuid.uuid4()
    with tiny(burst=1):
        pl.admit(peer, transport="http")
        with pytest.raises(pl.PeerRateLimited):
            pl.admit(peer, transport="http")
        pl.admit(peer, transport="ws", bucket=pl.FRAMES)   # calls spent, frames are not
        with pytest.raises(pl.PeerRateLimited):             # CONTROL: frames empty too
            pl.admit(peer, transport="ws", bucket=pl.FRAMES)
        with pytest.raises(ValueError):
            pl.admit(peer, transport="ws", bucket="everything")


def test_check_reads_the_bucket_and_takes_nothing_and_charge_never_raises():
    """`check` (a WS skill_call, a target frame) must not spend what start_call or the refusal
    charge will take, or every checked request would cost two tokens."""
    pl = _pl()
    peer = uuid.uuid4()
    with tiny(burst=2):
        pl.admit(peer, transport="ws")
        for _ in range(5):
            pl.check(peer, transport="ws")          # one token left, however often it is read
        pl.admit(peer, transport="ws")              # ...and it is still there to take
        with pytest.raises(pl.PeerRateLimited) as refused:
            pl.check(peer, transport="ws")           # CONTROL: an empty bucket is refused
        assert refused.value.retry_after >= 1
        pl.charge(peer, transport="ws")              # empty: no raise, still empty
        with pytest.raises(pl.PeerRateLimited):
            pl.check(peer, transport="ws")
        pl.check(uuid.uuid4(), transport="ws")       # CONTROL: an untracked peer is full
        with pytest.raises(ValueError):
            pl.check(peer, transport="ws", bucket="everything")


def test_the_limit_is_read_from_settings_and_a_bad_value_refuses_to_run():
    pl = _pl()
    assert (pl.config()) == (pl.DEFAULT_BURST, pl.DEFAULT_REFILL_PER_SEC)
    assert pl.DEFAULT_BURST >= 1 and pl.DEFAULT_REFILL_PER_SEC > 0
    with tiny(burst=5, per_sec=2.5):
        assert pl.config() == (5, 2.5)
    for bad in ({"MESHY_PEER_RATE_BURST": 0}, {"MESHY_PEER_RATE_BURST": "many"},
                {"MESHY_PEER_RATE_PER_SEC": 0}, {"MESHY_PEER_RATE_PER_SEC": -1},
                {"MESHY_PEER_RATE_PER_SEC": "nan"}):
        with override_settings(**bad), pytest.raises(ImproperlyConfigured):
            pl.admit(uuid.uuid4(), transport="http")


def test_an_idle_bucket_is_evicted_losslessly_and_a_full_table_fails_closed():
    """B8: bounded. A bucket idle for burst/refill seconds is full again, so dropping it
    changes nothing; past the ceiling with none idle, a NEW key is refused, never admitted
    unmetered."""
    pl = _pl()
    b = pl.PeerBuckets(burst=2, refill_per_sec=1.0, max_tracked=2)
    assert b.take("calls:a", now=0.0) == 0.0
    assert b.take("calls:b", now=0.0) == 0.0
    assert b.take("calls:c", now=0.5) > 0.0, "a new key past the ceiling was admitted"
    assert b.take("calls:a", now=0.5) == 0.0, "CONTROL: a tracked key is still served"
    assert b.take("calls:c", now=10.0) == 0.0, "idle buckets were not evicted"
    assert len(b) <= 2


def test_tokens_stay_off_by_default():
    """B12 does not switch capability tokens on: MESHY_CAPABILITIES stays unset/False."""
    from apps.core import capability_header
    assert capability_header.tokens_on() is False


# ------------------------------------------------------------------ the pipeline call site

class _Untouchable:
    """A connection that fails any use: the limit must refuse before the database."""
    def __getattr__(self, name):
        raise AssertionError(f"the pipeline touched the connection ({name}) before the limit")


def _inp(peer, call_id="ok-1"):
    env = SignedEnvelope(payload={}, signature="", signing_kid="")
    return pipeline.StartCallInput(caller_peer_id=peer, target_peer_id=uuid.uuid4(),
                                   skill_name="s", envelope=env, call_id=call_id,
                                   transport=pipeline.TRANSPORT_WS)


def test_start_call_refuses_an_empty_bucket_first_and_the_wire_says_429_with_retry_after():
    pl = _pl()
    ctx = pipeline.CallsContext(conn=_Untouchable(), envelope_nonce_enforce=True,
                                memory_peer_id=uuid.uuid4())
    peer = uuid.uuid4()
    with tiny(burst=1, per_sec=0.1):
        # POSITIVE CONTROL: with a token, start_call goes on to step 1, which refuses a call_id
        # holding a control character before anything touches the connection.
        with pytest.raises(rpc_errors.ValidationFailed):
            pipeline.start_call(ctx, _inp(peer, call_id="bad\x00id"))
        with pytest.raises(rpc_errors.RpcError) as refused:
            pipeline.start_call(ctx, _inp(peer))
    exc = refused.value
    assert (exc.code, exc.http_status, exc.retry_after) == ("E_RATE_LIMITED", 429, 10)
    api = wire.api_error_from_rpc(exc)
    response = wire.error_response(api)
    assert response.status_code == 429
    assert response["Retry-After"] == "10"
    assert json.loads(response.content)["error"] == "E_RATE_LIMITED"
    # CONTROL: an error with no retry_after carries no header.
    plain = wire.error_response(wire.api_error_from_rpc(rpc_errors.ValidationFailed("x")))
    assert not plain.has_header("Retry-After")


# ------------------------------------------------------------------ over the wire

@pytest.mark.asyncio
async def test_http_invoke_is_refused_429_once_the_callers_bucket_is_empty(owner, world, tuned):
    caller, target, other = world["caller"], world["target"], world["other"]
    with tiny(burst=2):
        for _ in range(2):                                       # POSITIVE CONTROL
            env = signed_call(caller, target, "s5.echo")
            r = await asyncio.to_thread(_invoke, caller, target, "s5.echo", env)
            assert r.status_code == 202, r.content[:300]
        env = signed_call(caller, target, "s5.echo")
        r = await asyncio.to_thread(_invoke, caller, target, "s5.echo", env)
        assert r.status_code == 429, r.content[:300]
        assert json.loads(r.content)["error"] == "E_RATE_LIMITED"
        assert int(r["Retry-After"]) >= 1
        with owner.cursor() as cur:
            cur.execute("SELECT count(*) FROM skill_calls WHERE call_id = %s",
                        (env["payload"]["call_id"],))
            assert cur.fetchone()[0] == 0, "a refused call left a row"
        # CONTROL: the limit is the caller's, not the route's.
        env = signed_call(other, target, "s5.echo")
        r = await asyncio.to_thread(_invoke, other, target, "s5.echo", env)
        assert r.status_code == 202, r.content[:300]


@pytest.mark.asyncio
async def test_a_ws_skill_call_shares_the_bucket_http_spent_and_the_pong_still_comes(
        owner, world, tuned):
    caller, target = world["caller"], world["target"]
    with tiny(burst=2):
        for _ in range(2):
            env = signed_call(caller, target, "s5.echo")
            r = await asyncio.to_thread(_invoke, caller, target, "s5.echo", env)
            assert r.status_code == 202, r.content[:300]
        c = await _connect(caller)
        try:
            env = signed_call(caller, target, "s5.echo")
            await c.send_json_to({"type": "skill_call", "envelope": env})
            reply = await _next(c)
            assert reply["type"] == "error" and reply["code"] == "SKILL_CALL_FAILED", reply
            assert reply["message"].startswith("E_RATE_LIMITED: "), reply
            await _pong(c)          # CONTROL: the frames bucket is not the calls bucket
        finally:
            await c.disconnect()


@pytest.mark.asyncio
async def test_a_refused_target_frame_is_charged_and_an_empty_bucket_refuses_before_the_database(
        owner, world, tuned, monkeypatch):
    """Review MEDIUM 1 (a65ab61f): a target frame for a made-up call_id cost a guarded UPDATE,
    a re-read, the `_why_refused` SELECT and a WARNING line, and was never charged, so a peer
    with both buckets empty could send them without limit. A target frame that is REFUSED now
    takes a FRAMES token, and a target frame that arrives with the FRAMES bucket empty is
    answered RATE_LIMITED before its statement runs."""
    from apps.wsgw import target_frames
    caller = world["caller"]
    ran = []
    real_ack = target_frames.ack

    def counted_ack(conn, **kwargs):
        ran.append(kwargs["call_id"])
        return real_ack(conn, **kwargs)

    monkeypatch.setattr(target_frames, "ack", counted_ack)
    with tiny(burst=2):
        c = await _connect(caller)
        try:
            for _ in range(2):                                    # POSITIVE CONTROL
                await c.send_json_to({"type": "skill_ack", "call_id": f"rl-{uuid.uuid4()}"})
                reply = await _next(c)
                assert reply["type"] == "error" and reply["code"] == "SKILL_ACK_FAILED", reply
            assert len(ran) == 2
            # The two refusals were charged: the socket's own ping now finds FRAMES empty.
            await c.send_json_to({"type": "ping"})
            reply = await _next(c)
            assert reply["type"] == "error" and reply["code"] == "RATE_LIMITED", reply
            for _ in range(40):
                await c.send_json_to({"type": "skill_ack", "call_id": f"flood-{uuid.uuid4()}"})
                reply = await _next(c)
                assert reply["type"] == "error" and reply["code"] == "RATE_LIMITED", reply
            assert len(ran) == 2, f"the statement ran {len(ran)} times past an empty bucket"
        finally:
            await c.disconnect()


@pytest.mark.asyncio
async def test_a_real_calls_target_frames_are_not_charged(owner, world, tuned):
    """The other half of MEDIUM 1's fix: a target frame that LANDS answers a call already
    charged to its caller and takes nothing, so a real stream is never cut by the limit."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    with tiny(burst=2):
        t = await _connect(target)
        try:
            env = signed_call(caller, target, "s5.echo", {"text": "hi"})
            call_id = env["payload"]["call_id"]
            body = await invoke(caller, target, "s5.echo", env, mode="async")
            assert body["status"] == "routed", body
            await _invoke_frame(t)
            for frame in ({"type": "skill_ack", "call_id": call_id},
                          {"type": "skill_running", "call_id": call_id},
                          signed_result(target, call_id)):
                await t.send_json_to(frame)
            await _until(lambda: _status(owner, call_id) == "completed", "the call completed")
            await _pong(t)                                        # FRAMES still full: 1
            await _pong(t)                                        # 2
            await t.send_json_to({"type": "ping"})                # CONTROL: the bucket is 2
            reply = await _next(t)
            assert reply["type"] == "error" and reply["code"] == "RATE_LIMITED", reply
        finally:
            await t.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"type": "subscribe", "channels": ["presence:rl-watched"]},
    {"type": "unsubscribe", "channels": ["presence:rl-watched"]},
    {"type": "skill_cancel", "call_id": "rl-not-a-call", "reason": "r"},
], ids=["subscribe", "unsubscribe", "skill_cancel"])
async def test_each_socket_control_frame_takes_a_frames_token(owner, world, tuned, frame):
    """Review LOW 4: mutant Y1 (only ping charged) survived the suite. Each control frame
    other than ping must spend the FRAMES bucket: with a burst of 1, the ping after it is
    refused."""
    caller = world["caller"]
    with tiny(burst=1):
        c = await _connect(caller)
        try:
            await c.send_json_to(frame)
            reply = await _next(c)
            assert reply.get("code") != "RATE_LIMITED", reply    # POSITIVE CONTROL
            await c.send_json_to({"type": "ping"})
            reply = await _next(c)
            assert reply["type"] == "error" and reply["code"] == "RATE_LIMITED", (frame, reply)
        finally:
            await c.disconnect()


@pytest.mark.asyncio
async def test_a_skill_call_on_an_empty_bucket_is_refused_before_a_connection_is_opened(
        owner, world, tuned, monkeypatch):
    """Review LOW 3 (socket half): every refused skill_call opened a fresh guarded connection
    in the pipeline thread, and logged a WARNING, before start_call refused it. The empty
    bucket is now read on the event loop, before the pipeline executor is reached."""
    from apps.wsgw import caller_frames
    caller, target = world["caller"], world["target"]
    started = []
    real_start = caller_frames._start_in_thread

    def counted(*args, **kwargs):
        started.append(args[0])
        return real_start(*args, **kwargs)

    monkeypatch.setattr(caller_frames, "_start_in_thread", counted)
    with tiny(burst=1):
        c = await _connect(caller)
        try:
            env = signed_call(caller, target, "s5.echo")          # POSITIVE CONTROL: 1 token
            await c.send_json_to({"type": "skill_call", "envelope": env})
            reply = await _next(c)
            assert reply.get("code") != "SKILL_CALL_FAILED", reply
            assert len(started) == 1
            for _ in range(5):
                env = signed_call(caller, target, "s5.echo")
                await c.send_json_to({"type": "skill_call", "envelope": env})
                reply = await _next(c)
                assert reply["type"] == "error" and reply["code"] == "SKILL_CALL_FAILED", reply
                assert reply["message"].startswith("E_RATE_LIMITED: "), reply
            assert len(started) == 1, f"{len(started) - 1} refused calls reached the pipeline"
            await _pong(c)                    # CONTROL: the frames bucket is not the calls one
        finally:
            await c.disconnect()


# ------------------------------------------------------------------ the HTTP wrappers

class _Auth:
    token = "a-verified-token"

    def __init__(self, peer_id):
        self.peer_id = peer_id


def _stub_route(monkeypatch, module, peer_id):
    monkeypatch.setattr(module, "authenticate_peer", lambda conn, request: _Auth(peer_id))
    monkeypatch.setattr(module.dbconn, "serving_connection",
                        lambda *a, **k: contextlib.nullcontext(object()))


def test_the_capability_routes_are_limited_after_authentication(monkeypatch):
    from apps.memoryskills import views
    peer = uuid.uuid4()
    request = RequestFactory().post("/v1/memory/capabilities", data="{}",
                                    content_type="application/json")
    ran = []

    def handler(request, conn, caller, body, store, issue):
        ran.append(caller)
        return views.JsonResponse({"ok": True}, status=201)

    route = views._route(handler)
    with tiny(burst=1):
        # Tokens OFF (the default): 403 UNAVAILABLE before anything, the limit included.
        for _ in range(3):
            r = route(request)
            assert r.status_code == 403
            assert json.loads(r.content)["error"] == views.UNAVAILABLE
        assert ran == []
        monkeypatch.setattr(views, "_tokens_available", lambda: True)
        monkeypatch.setattr(views.capability_keys, "current", lambda: object())
        monkeypatch.setattr(views, "_json_body", lambda request: {})
        _stub_route(monkeypatch, views, peer)
        assert route(request).status_code == 201                   # POSITIVE CONTROL
        r = route(request)
        assert r.status_code == 429, r.content
        assert json.loads(r.content)["error"] == "E_RATE_LIMITED"
        assert int(r["Retry-After"]) >= 1
        assert ran == [str(peer)], "the handler ran past an empty bucket"


def test_the_board_write_door_is_limited_after_authentication(monkeypatch):
    from apps.board import views
    peer = uuid.uuid4()
    request = RequestFactory().post("/v1/board/acts", data="{}", content_type="application/json")
    ran = []

    def handler(request, conn, auth):
        ran.append(auth.peer_id)
        return views.JsonResponse({"ok": True}, status=201)

    route = views._write_route(handler)
    _stub_route(monkeypatch, views, peer)
    with tiny(burst=1):
        assert route(request).status_code == 201                   # POSITIVE CONTROL
        r = route(request)
        assert r.status_code == 429, r.content
        assert json.loads(r.content)["error"] == "E_RATE_LIMITED"
        assert int(r["Retry-After"]) >= 1
    assert ran == [peer]


# ------------------------------------------------------------------ the mesh transport

def _drained(pl, peer):
    """The peer's CALLS bucket spent by one call, so a burst of 1 is empty when asked."""
    pl.admit(peer, transport="http")


def _stub_mesh(monkeypatch, module, peer_id, ran):
    _stub_route(monkeypatch, module, peer_id)
    monkeypatch.setattr(module.dbconn, "request_server_internal",
                        lambda conn: contextlib.nullcontext(conn))
    if hasattr(module, "usage"):
        monkeypatch.setattr(module.usage, "record",
                            lambda conn, route, peer, outcome: ran.append(("usage", outcome)))


def _assert_429(r):
    assert r.status_code == 429, (r.status_code, r.content[:300])
    assert json.loads(r.content)["error"] == "E_RATE_LIMITED"
    assert int(r["Retry-After"]) >= 1


def test_the_mesh_graph_routes_are_limited_after_authentication(monkeypatch):
    """Review MEDIUM 2: every /v1/mesh/* route was unmetered. The graph routes' wrapper takes
    the peer's CALLS bucket after authentication, before the read gate and the usage row."""
    from apps.mesh import views
    peer, ran = uuid.uuid4(), []
    _stub_mesh(monkeypatch, views, peer, ran)
    monkeypatch.setattr(views, "require_mesh_read", lambda c, p: ran.append(("gate", p)))

    def handler(request, c, auth):
        ran.append(("handler", auth.peer_id))
        return views.JsonResponse({})

    route = views._route("inbox")(handler)
    request = RequestFactory().get("/v1/mesh/inbox")
    with tiny(burst=1):
        assert route(request).status_code == 200                   # POSITIVE CONTROL
        assert ("handler", peer) in ran
        ran.clear()
        _assert_429(route(request))
    assert ran == [], f"work ran past an empty bucket: {ran}"


def test_the_mesh_wait_is_limited_after_authentication(monkeypatch):
    from apps.mesh import views
    peer, ran = uuid.uuid4(), []
    _stub_route(monkeypatch, views, peer)
    monkeypatch.setattr(views, "verify_token", lambda token: {"exp": 2**40})
    monkeypatch.setattr(views.mesh_wait, "count_visible",
                        lambda conn, p: ran.append(("count", p)) or 0)
    monkeypatch.setattr(views.usage, "record_on_own_connection",
                        lambda route, p, outcome: ran.append(("usage", outcome)))
    request = RequestFactory().get("/v1/mesh/wait", {"timeout": "0"})
    with tiny(burst=1):
        assert views.wait(request).status_code == 204              # POSITIVE CONTROL
        assert ("count", peer) in ran
        ran.clear()
        _assert_429(views.wait(request))
    assert ran == [], f"work ran past an empty bucket: {ran}"


def test_the_mesh_search_and_context_routes_are_limited_after_authentication(monkeypatch):
    """/v1/mesh/search embeds the query and reranks the top 40: the costliest mesh read."""
    from apps.meshsem import views
    peer, ran = uuid.uuid4(), []
    _stub_mesh(monkeypatch, views, peer, ran)
    monkeypatch.setattr(views, "check_permission", lambda *a, **k: ran.append(("gate",)))

    def handler(request, conn, auth):
        ran.append(("handler",))
        return views.JsonResponse({})

    route = views._route(handler)
    request = RequestFactory().get("/v1/mesh/search", {"q": "x"})
    with tiny(burst=1):
        assert route(request).status_code == 200                   # POSITIVE CONTROL
        ran.clear()
        _assert_429(route(request))
    assert ran == [], f"work ran past an empty bucket: {ran}"


def test_the_mesh_summary_routes_are_limited_after_authentication(monkeypatch):
    from apps.meshsum import views
    peer, ran = uuid.uuid4(), []
    _stub_mesh(monkeypatch, views, peer, ran)
    monkeypatch.setattr(views, "check_permission", lambda *a, **k: ran.append(("gate",)))

    def summarize(request, conn, auth, kind):
        ran.append(("handler", kind))
        return views.JsonResponse({})

    monkeypatch.setattr(views, "_summarize", summarize)
    route = views._route("summary_thread", "thread")
    request = RequestFactory().get("/v1/mesh/summaries/thread")
    with tiny(burst=1):
        assert route(request).status_code == 200                   # POSITIVE CONTROL
        ran.clear()
        _assert_429(route(request))
    assert ran == [], f"work ran past an empty bucket: {ran}"


def test_a_mesh_read_and_a_call_share_the_peers_bucket():
    """One bucket per peer, whatever the transport (#206): a peer that spent its calls is
    refused on the mesh too, and the other way round."""
    pl = _pl()
    peer = uuid.uuid4()
    with tiny(burst=1):
        _drained(pl, peer)
        with pytest.raises(pl.PeerRateLimited):
            pl.admit(peer, transport="mesh")


def test_a_fractional_burst_is_refused_not_truncated():
    """Review INFO 7: MESHY_PEER_RATE_BURST=1.5 as a float became int(1.5) == 1 silently,
    while the string "1.5" raised. Both refuse now; a whole float is still a burst."""
    pl = _pl()
    with override_settings(MESHY_PEER_RATE_BURST=1.5), pytest.raises(ImproperlyConfigured):
        pl.config()
    with override_settings(MESHY_PEER_RATE_BURST=3.0):                # CONTROL
        assert pl.config()[0] == 3
