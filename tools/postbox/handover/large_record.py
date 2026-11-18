"""Bounded validation of large rollout records, with header-only telemetry.

No payload strings are retained. Only qualified root headers before payload
are interpreted; unsupported layouts fail closed. State is JSON serializable.
"""
from __future__ import annotations

import codecs
import json
import re


RELEVANT_TYPES = frozenset({"session_meta", "turn_context", "task_started", "task_complete",
                           "turn_aborted", "compacted", "context_compacted", "token_count"})


def header(raw):
    text = raw[:4096].decode("utf-8", errors="ignore")
    decoder = json.JSONDecoder()
    pos, values = 0, {}
    def space():
        nonlocal pos
        while pos < len(text) and text[pos] in " \r\n\t":
            pos += 1
    space()
    if text[pos] != "{":
        raise ValueError()
    pos += 1
    while True:
        space()
        key, pos = decoder.raw_decode(text, pos)
        if not isinstance(key, str) or key in values:
            raise ValueError()
        space()
        if text[pos] != ":":
            raise ValueError()
        pos += 1
        space()
        if key == "payload":
            if text[pos] != "{" or not isinstance(values.get("timestamp"), str):
                raise ValueError()
            kind = values.get("type")
            if kind in {"compacted", "context_compacted"}:
                return {"type": kind, "timestamp": values["timestamp"], "payload": {}}
            # Only known telemetry-irrelevant root types may be skipped.
            if kind in {"response_item", "world_state", "token_usage_record"}:
                return {"type": kind, "timestamp": values["timestamp"], "payload": {}}
            if kind == "event_msg":
                pos += 1
                space()
                payload_key, pos = decoder.raw_decode(text, pos)
                space()
                if payload_key != "type" or text[pos] != ":":
                    raise ValueError()
                pos += 1
                space()
                payload_type, pos = decoder.raw_decode(text, pos)
                if not isinstance(payload_type, str) or payload_type in RELEVANT_TYPES:
                    raise ValueError()
                return {"type": kind, "timestamp": values["timestamp"],
                        "payload": {"type": payload_type}}
            raise ValueError()
        if key not in {"type", "timestamp", "ordinal"}:
            raise ValueError()
        values[key], pos = decoder.raw_decode(text, pos)
        space()
        if text[pos] != ",":
            raise ValueError()
        pos += 1


def feed(state, raw, *, final=False):
    """Validate JSON incrementally. Bound depth, primitive size and UTF-8 carry.

    Container expectations: key/end, key, colon, value/end, value, comma/end.
    Root header order/meaning is independently checked by header().
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    decoder.setstate((bytes(state.get("utf8", [])), 0))
    text = decoder.decode(raw, final=final)
    state["utf8"] = list(decoder.getstate()[0])
    stack = state.setdefault("stack", [])
    mode = state.get("mode", "")
    token = state.get("token", "")
    escape = state.get("escape", 0)
    string_key = state.get("string_key", False)
    done = state.get("done", False)
    key_token = state.get("key_token", "")
    payload_type_seen = state.get("payload_type_seen", False)

    def complete():
        nonlocal done
        if not stack:
            done = True
        else:
            stack[-1][1] = "comma"

    for char in text:
        if mode == "string":
            # An ignored event must not change its type later in the payload.
            # Retain only bounded direct-payload key text, never value strings.
            if string_key and len(stack) == 2 and state.get("guard_payload_type"):
                if char == '"' and not escape:
                    if json.loads('"' + key_token + '"') == "type":
                        if payload_type_seen:
                            raise ValueError()
                        payload_type_seen = True
                    key_token = ""
                else:
                    key_token += char
                    if len(key_token) > 128:
                        raise ValueError()
            if escape > 1:
                if char not in "0123456789abcdefABCDEF":
                    raise ValueError()
                escape -= 1
                if escape == 1:
                    escape = 0
            elif escape == 1:
                if char == "u":
                    escape = 5
                elif char in '\\"/bfnrt':
                    escape = 0
                else:
                    raise ValueError()
            elif char == "\\":
                escape = 1
            elif char == '"':
                mode = ""
                if string_key:
                    stack[-1][1] = "colon"
                else:
                    complete()
            elif ord(char) < 32:
                raise ValueError()
            continue
        if mode == "atom":
            if char not in " \t\r\n,]}":
                token += char
                if len(token) > 128:
                    raise ValueError()
                continue
            if not re.fullmatch(r"(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)", token):
                raise ValueError()
            token, mode = "", ""
            complete()
        if char in " \r\n\t":
            continue
        if done:
            raise ValueError()
        expect = stack[-1][1] if stack else "value"
        if expect in {"key_end", "key"}:
            if char == "}" and expect == "key_end":
                stack.pop()
                complete()
            elif char == '"':
                mode, string_key, key_token = "string", True, ""
            else:
                raise ValueError()
        elif expect == "colon":
            if char != ":":
                raise ValueError()
            stack[-1][1] = "value"
        elif expect == "comma":
            container = stack[-1][0]
            if char == ("}" if container == "object" else "]"):
                stack.pop()
                complete()
            elif char == ",":
                # Qualified headers end at payload: no later root overrides.
                if len(stack) == 1 and state.get("root_payload_seen"):
                    raise ValueError()
                stack[-1][1] = "key" if container == "object" else "value"
            else:
                raise ValueError()
        elif expect in {"value", "value_end"}:
            if char == "]" and expect == "value_end":
                stack.pop()
                complete()
            elif char in "{[":
                if len(stack) >= 128:
                    raise ValueError()
                if len(stack) == 1 and char == "{":
                    state["root_payload_seen"] = True
                stack.append(["object", "key_end"] if char == "{" else ["array", "value_end"])
            elif char == '"':
                mode, string_key = "string", False
            elif char in "-0123456789tfn":
                mode, token = "atom", char
            else:
                raise ValueError()
    state.update(mode=mode, token=token, escape=escape, string_key=string_key, done=done,
                 key_token=key_token, payload_type_seen=payload_type_seen)
    if final and (not done or stack or mode):
        raise ValueError()
