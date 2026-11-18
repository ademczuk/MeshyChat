"""Only proved Claude/Codex/Antigravity/Kimi shapes; boundaries, sidechains and truncated tails have controls."""
import datetime as dt
import json

import pytest

from tools.postbox.turnstate import classify

AT = "2026-11-04T11:00:00Z"


def row(typ, **kw):
    return json.dumps(dict(type=typ, timestamp=AT, **kw)).encode() + b"\n"


@pytest.mark.parametrize("event,state", [("task_started", "open"), ("task_complete", "closed"),
                                         ("turn_aborted", "closed")])
def test_codex_markers(event, state):
    assert classify(row("event_msg", payload={"type": event}))[0] == state


@pytest.mark.parametrize("reason,state", [("tool_use", "open"), ("pause_turn", "open"),
    ("end_turn", "closed"), ("stop_sequence", "closed"), ("max_tokens", "closed"), ("refusal", "closed")])
def test_claude_stops(reason, state):
    assert classify(row("assistant", message={"stop_reason": reason}))[0] == state


def test_claude_user_duration_and_interruption_shapes():
    assert classify(row("user", message={"content": "new turn"}))[0] == "open"
    assert classify(row("system", subtype="turn_duration"))[0] == "closed"
    for content in ("[Request interrupted by user]", [{"type": "text", "text": "[Request interrupted"}]):
        assert classify(row("user", message={"content": content}))[0] == "closed"


def test_partial_lines_nonobjects_sidechains_and_unsupported_do_not_change_the_turn():
    opened = row("event_msg", payload={"type": "task_started"})
    closed_side = row("assistant", message={"stop_reason": "end_turn"}, isSidechain=True)
    assert classify(b'broken"}\n[]\nnull\n' + opened + closed_side + b'{"type":')[0] == "open"
    assert classify(row("kimi", payload={"type": "task_complete"})) == (None, None)


def test_latest_record_time_is_separate_from_last_marker_and_missing_marker_is_unknown():
    marker = row("event_msg", payload={"type": "task_started"})
    later = json.dumps({"type": "response_item", "timestamp": "2026-11-04T11:02:00+00:00"}).encode()
    assert classify(marker + later) == ("open", dt.datetime(2026, 11, 4, 11, 2, tzinfo=dt.timezone.utc))
    assert classify(later)[0] is None
    assert classify(b'{"type":"event_msg","payload":{"type":"task_started"},"timestamp":"bad"}') == (None, None)


@pytest.mark.parametrize("content", ["<command-name>/status</command-name>",
    "<command-message>status</command-message>",
    "<local-command-stdout>local output</local-command-stdout>",
    "<local-command-stderr>local error</local-command-stderr>",
    "<local-command-caveat>local only</local-command-caveat>",
    [{"type": "text", "text": "<command-name>/help</command-name>"},
     {"type": "text", "text": "<local-command-stdout>local output"}]])
def test_local_commands_neither_open_a_turn_nor_supply_model_activity_time(content):
    command = row("user", message={"content": content})
    assert classify(command) == (None, None)
    closed = row("assistant", message={"stop_reason": "end_turn"})
    assert classify(closed + command) == ("closed", dt.datetime(2026, 11, 4, 11, tzinfo=dt.timezone.utc))


@pytest.mark.parametrize("content", ["channel event", [{"type": "text", "text": "hook feedback"}],
                                     "scheduled prompt", "skill body"])
def test_meta_prompts_open_a_turn_and_advance_model_activity_time(content):
    closed = row("assistant", message={"stop_reason": "end_turn"})
    prompt = json.dumps({"type": "user", "timestamp": "2026-11-04T11:02:00Z", "isMeta": True,
                         "message": {"content": content}}).encode() + b"\n"
    expected = ("open", dt.datetime(2026, 11, 4, 11, 2, tzinfo=dt.timezone.utc))
    assert classify(prompt) == expected
    assert classify(closed + prompt) == expected


def test_meta_local_caveat_is_excluded_but_tool_results_and_real_text_open():
    assert classify(row("user", isMeta=True,
                        message={"content": "<local-command-caveat>local only"})) == (None, None)
    assert classify(row("user", message={"content": [{"type": "tool_result", "content": "result"}]}))[0] == "open"
    assert classify(row("user", message={"content": [
        {"type": "text", "text": "<command-name>/custom</command-name>"},
        {"type": "text", "text": "a real model prompt"}]}))[0] == "open"


def test_antigravity_steps_distinguish_tool_turns_wakeups_and_completed_responses():
    def ag(step, typ, at, status="DONE", **kw):
        return json.dumps(dict(step_index=step, type=typ, status=status, created_at=at, **kw)).encode() + b"\n"

    t0 = ag(0, "USER_INPUT", "2026-11-05T08:00:00Z", source="USER_EXPLICIT")
    t1 = ag(1, "PLANNER_RESPONSE", "2026-11-05T08:00:05Z", tool_calls=[{"name": "read_file"}])
    t2 = ag(2, "GENERIC", "2026-11-05T08:00:10Z")
    t3 = ag(3, "PLANNER_RESPONSE", "2026-11-05T08:00:15Z")
    t4 = ag(4, "SYSTEM_MESSAGE", "2026-11-05T08:35:00Z", source="SYSTEM")
    assert classify(t0)[0] == "open"
    assert classify(t0 + t1 + t2) == ("open", dt.datetime(2026, 11, 5, 8, 0, 10, tzinfo=dt.timezone.utc))
    assert classify(t0 + t1 + t2 + t3) == ("closed", dt.datetime(2026, 11, 5, 8, 0, 15, tzinfo=dt.timezone.utc))
    assert classify(t0 + t1 + t2 + t3 + t4) == ("open", dt.datetime(2026, 11, 5, 8, 35, 0, tzinfo=dt.timezone.utc))
    # Boolean step_index and naive/invalid created_at fail closed.
    assert classify(b'{"step_index":true,"type":"USER_INPUT","status":"DONE","created_at":"2026-11-05T08:00:00Z"}\n') == (
        None, None)
    assert classify(ag(5, "USER_INPUT", "2026-11-05T08:00:00")) == (None, None)


def test_kimi_python_and_kimi_code_wire_markers_and_epoch_bounds():
    def py_wire(ts, mtype):
        return json.dumps({"timestamp": ts, "message": {"type": mtype, "payload": {}}}).encode() + b"\n"

    def kc_wire(ms, typ, **kw):
        return json.dumps(dict(type=typ, time=ms, **kw)).encode() + b"\n"

    started = py_wire(1793256970.5, "TurnBegin")
    step = py_wire(1793256980.0, "ContentPart")
    ended = py_wire(1793257136.25, "TurnEnd")
    assert classify(started + step) == ("open", dt.datetime.fromtimestamp(1793256980.0, tz=dt.timezone.utc))
    assert classify(started + step + ended) == ("closed", dt.datetime.fromtimestamp(1793257136.25, tz=dt.timezone.utc))

    kc_open = kc_wire(1793612765368, "agent.turn.started", kind="event")
    kc_llm = kc_wire(1793612790000, "llm.request", agentId="main")
    kc_sub_close = kc_wire(1793612800000, "turn.ended", agentId="agent-231")
    kc_close = kc_wire(1793612810760, "turn.ended", agentId="main")
    # Subagent agentId != main never closes a main session turn.
    assert classify(kc_open + kc_llm + kc_sub_close) == (
        "open", dt.datetime.fromtimestamp(1793612790.0, tz=dt.timezone.utc))
    assert classify(kc_open + kc_llm + kc_sub_close + kc_close) == (
        "closed", dt.datetime.fromtimestamp(1793612810.76, tz=dt.timezone.utc))

    # Booleans, out-of-band epochs, and seconds-vs-milliseconds confusion fail closed.
    assert classify(py_wire(True, "TurnBegin")) == (None, None)
    assert classify(py_wire(1793612810760, "TurnBegin")) == (None, None)
    assert classify(kc_wire(1793256970, "turn.ended", agentId="main")) == (None, None)
