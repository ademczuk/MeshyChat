"""The server's context validator for the F3 kinds (ingest plan D1, step 3 item 8; the parser side is #429).

DB-free: validate_context and the screening input only. tests/test_context_write.py writes F3 items and their manifest
through memory.write against Postgres. tools/mutants/lists/context_validation_f3.py breaks each rule here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from apps.context import validation as V
from apps.memoryskills.errors import HandlerError

NS = V.namespace()
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
SEAT, SRC = "kxxx", "0" * 31 + "1"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def item(ordinal=0, kind="tool_exchange", **over):
    whole = json.dumps({"tool": "Bash", "call": '{"command":"ls"}', "result": "ok"}, separators=(",", ":"))
    value = {"schema": "context.v1", "kind": "item", "seat": SEAT, "src": SRC, "gen": 1, "batch": 1,
             "ordinal": ordinal, "item": kind, "dialect": "claude", "adapter_version": "1.0",
             "time": "2026-10-27T11:59:00.000Z"}
    if kind == "tool_exchange":
        value.update(tool_ref="a" * 32, tool="Bash", call_status="ok", result_status="ok", exchange_sha256=sha(whole),
                     part=1, parts=1, text=whole, text_sha256=sha(whole))
    elif kind == "thinking":
        value.update(role="assistant", text="weigh it", text_sha256=sha("weigh it"))
    value.update(over)
    if "text" in over and "text_sha256" not in over:
        value["text_sha256"] = sha(value["text"])
    return value


def key(v):
    return "ctx/%s/%s/%04d/%06d/%010d" % (v["seat"], v["src"], v["gen"], v["batch"], v["ordinal"])


def check(v):
    return V.validate_context(NS, key(v), v, now=NOW)


def refused(v, code="E_CONTEXT_SCHEMA"):
    with pytest.raises(HandlerError) as err:
        check(v)
    assert code in str(err.value) or getattr(err.value, "code", None) == code, err.value


def test_a_one_part_exchange_and_a_thinking_item_are_accepted():
    assert check(item()) == "item"
    assert check(item(kind="thinking")) == "item"


@pytest.mark.parametrize("over", [
    {"call_status": "maybe"},
    {"result_status": "pending"},
    {"call_status": "missing", "result_status": "missing"},
    {"part": 3, "parts": 2},
    {"part": 1, "parts": V.MAX_EXCHANGE_PARTS + 1},
    {"parts": 0},
    {"tool_ref": "not-hex"},
    {"tool": "x" * (V.MAX_TOOL_NAME_CHARS + 1)},
    {"exchange_sha256": "0" * 64},
    {"text": "not json at all"},
    {"text": json.dumps({"tool": "Bash", "call": "x"})},
])
def test_a_malformed_exchange_is_refused(over):
    v = item(**over)
    if "exchange_sha256" not in over and "text" in over:
        v["exchange_sha256"] = v["text_sha256"]
    refused(v, "E_CONTEXT_DIGEST" if "exchange_sha256" in over else "E_CONTEXT_SCHEMA")


def test_a_part_of_a_split_exchange_need_not_decode_but_its_shape_is_checked():
    part = item(text='{"tool":"Bash","call":"half', part=1, parts=2, exchange_sha256="b" * 64)
    assert check(part) == "item"


def test_exchange_fields_belong_to_a_tool_exchange_only():
    refused(item(kind="thinking", tool_ref="a" * 32))
    omitted = {k: v for k, v in item(kind="omitted").items()}
    omitted.update(omitted_reason="secret", call_status="ok")
    refused(omitted)


def test_thinking_has_the_assistant_role():
    refused(item(kind="thinking", role="user"))


def test_the_screens_read_a_quoted_secret_raw_in_a_whole_exchange_and_unescaped_in_a_part():
    call = json.dumps({"password": "hunter2"}, separators=(",", ":"))
    whole = json.dumps({"tool": "Bash", "call": call, "result": "ok"}, separators=(",", ":"))
    one = item(text=whole, exchange_sha256=sha(whole))
    assert call in V._exchange_screen_text(one), "a one-part exchange is screened on its decoded call"
    part = item(text=whole[:60], part=1, parts=2, exchange_sha256="b" * 64)
    assert '"password":"hunter2' not in part["text"], "stored escaped, so the raw form is not in the text"
    texts = V._exchange_screen_text(part)
    assert any('"password":"hunter2' in t for t in texts), "a part is screened with its JSON escapes undone"


def manifest(counts, n=1):
    return {"schema": "context.v1", "kind": "manifest", "seat": SEAT, "src": SRC, "gen": 1, "batch": 1, "first": 0,
            "last": n - 1, "items": [[o, "c" * 64] for o in range(n)], "counts": counts}


def check_manifest(m):
    return V.validate_context(NS, "ctxm/%s/%s/0001/000001" % (SEAT, SRC), m, now=NOW)


BASE = {"written": 2, "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 0}


def test_manifest_counts_are_the_f1_set_or_the_f1_set_plus_the_f3_classes():
    assert check_manifest(manifest(dict(BASE), n=2)) == "manifest"
    f3 = dict(BASE, tool_exchange=1, thinking=1, call_missing=0, result_missing=1, parts_extra=0)
    assert check_manifest(manifest(f3, n=2)) == "manifest"
    with pytest.raises(HandlerError):
        check_manifest(manifest(dict(BASE, tool_exchange=1), n=2))      # some F3 keys but not all


@pytest.mark.parametrize("over", [
    {"tool_exchange": 2, "thinking": 1},             # more F3 items than written
    {"tool_exchange": 1, "call_missing": 2},          # more missing calls than exchanges
    {"tool_exchange": 1, "parts_extra": 2},
])
def test_f3_counts_that_cannot_be_true_are_refused(over):
    f3 = dict(BASE, tool_exchange=0, thinking=0, call_missing=0, result_missing=0, parts_extra=0)
    f3.update(over)
    with pytest.raises(HandlerError):
        check_manifest(manifest(f3, n=2))


# ---- txxxx's FIX FIRST on #433 e7110deb ---------------------------------------------------------------------------

def test_the_tool_name_is_screened_and_must_match_the_text_of_a_one_part_exchange():
    # GitHub Codex on #433 (validation.py:261): the top-level tool was neither screened nor tied to the text.
    sneaky = item(tool="password=hunter2")
    refused(sneaky)
    part = item(text='{"tool":"Bash","call":"half', part=1, parts=2, exchange_sha256="b" * 64, tool="password=hunter2")
    assert "password=hunter2" in V._exchange_screen_text(part)
    assert "Bash" in V._exchange_screen_text(item())


def test_unicode_escapes_are_undone_for_the_screens():
    assert V._unescape("a\\u0007b\\\"c") == "a\x07b\"c"
    assert V._unescape("cut \\u00") == "cut \\u00", "a slice ending inside an escape keeps it"


def _split(whole, n, **over):
    size = -(-len(whole) // n)
    texts = [whole[i * size:(i + 1) * size] for i in range(n)]
    out = []
    for i, t in enumerate(texts, 1):
        v = item(ordinal=i - 1, text=t, part=i, parts=n, exchange_sha256=sha(whole))
        v.update(over)
        out.append((i - 1, v))
    return out


@pytest.fixture(autouse=True)
def _denylist(tmp_path, monkeypatch):
    """The client-name screen fails closed without a denylist; every screened check here has one."""
    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps(["Zorblax Corp"]), encoding="utf-8")
    monkeypatch.setenv("MESHY_CONTEXT_DENYLIST", str(deny))


WHOLE = json.dumps({"tool": "Bash", "call": '{"command":"ls"}', "result": "r" * 300}, separators=(",", ":"))
SCREENED_NS = "shared/context-validation-test"


def test_a_whole_split_exchange_passes_the_manifest_check():
    V._check_split_exchanges(SCREENED_NS, _split(WHOLE, 3))


@pytest.mark.parametrize("damage", ["truncated", "digest", "tool", "order", "status"])
def test_a_split_exchange_that_is_not_whole_is_refused_at_the_manifest(damage):
    parts = _split(WHOLE, 3)
    if damage == "truncated":
        parts = parts[:2]
    elif damage == "digest":
        parts = _split(WHOLE, 3, exchange_sha256="0" * 64)
    elif damage == "tool":
        parts[1][1]["tool"] = "Other"
    elif damage == "order":
        parts = [parts[0], parts[2], parts[1]]
    elif damage == "status":
        parts[2][1]["result_status"] = "error"
    with pytest.raises(HandlerError) as err:
        V._check_split_exchanges(SCREENED_NS, parts)
    assert err.value.code == "E_CONTEXT_MANIFEST"


def test_a_secret_spanning_two_parts_is_caught_when_the_parts_are_joined():
    secret = "AKIA" + "QWERTYUIOPASDFGH"
    whole = json.dumps({"tool": "Bash", "call": "x", "result": "y" * 20 + " " + secret + " " + "y" * 20},
                       separators=(",", ":"))
    cut = whole.index(secret) + 8                       # the key straddles the boundary
    parts = [(0, item(ordinal=0, text=whole[:cut], part=1, parts=2, exchange_sha256=sha(whole))),
             (1, item(ordinal=1, text=whole[cut:], part=2, parts=2, exchange_sha256=sha(whole)))]
    assert secret not in parts[0][1]["text"] and secret not in parts[1][1]["text"]
    with pytest.raises(HandlerError) as err:
        V._check_split_exchanges(SCREENED_NS, parts)
    assert err.value.code == "E_CONTEXT_SECRET"
    V._check_split_exchanges(V.namespace(), parts)      # the unscreened namespace keeps it, by Bxxxxx's rule


def _nested_secret_exchange():
    """GitHub Codex on #433 (validation.py:289): a call that is itself JSON holds a quoted secret one escape layer down,
    where neither the decoded call nor the text with its escapes undone once shows it raw."""
    call = json.dumps({"note": json.dumps({"password": "hunter2-nested"}, separators=(",", ":"))},
                      separators=(",", ":"))
    whole = json.dumps({"tool": "Bash", "call": call, "result": "ok" * 40}, separators=(",", ":"))
    assert '"password":"hunter2' not in call and '"password":"hunter2' not in V._unescape(whole)
    return whole


def test_a_secret_one_json_layer_down_in_a_one_part_call_is_screened():
    whole = _nested_secret_exchange()
    texts = V._exchange_screen_text(item(text=whole, exchange_sha256=sha(whole)))
    assert any('"password":"hunter2-nested"' in t for t in texts), "the call's own JSON strings are screened"
    with pytest.raises(HandlerError) as err:
        V.screens.screen(*texts)
    assert err.value.code == "E_CONTEXT_SECRET"


def test_a_secret_one_json_layer_down_in_a_split_exchange_is_caught_at_the_manifest():
    whole = _nested_secret_exchange()
    with pytest.raises(HandlerError) as err:
        V._check_split_exchanges(SCREENED_NS, _split(whole, 2))
    assert err.value.code == "E_CONTEXT_SECRET"


def test_the_validator_walks_json_strings_exactly_one_parse_deep_like_the_parser():
    """A server screen stricter than the client's would refuse a batch the client already froze."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "transcript_cards"))
    import context_parser as cp
    for text in ('{"a":["b",{"c":"d"}]}', '{"k":"\\ud800"}', '[1,"x",{"y":["z"]}]', "plain", "[not json"):
        assert sorted(V._json_strings(text)) == sorted(cp._json_strings(text)), text
    assert sorted(V._json_strings('{"a":["b",{"c":"d"}]}')) == ["a", "b", "c", "d"]
    assert V._json_strings(None) == [] and V._json_strings('{"k":"\\ud800"}') == ["k", "�"]


def test_a_run_shorter_than_its_declared_parts_is_refused_even_when_the_join_matches():
    # Two parts whose texts join to the exchange exactly, but which say they are 2 of 3: only the count catches it.
    half = -(-len(WHOLE) // 2)
    parts = [(i, item(ordinal=i, text=t, part=i + 1, parts=3, exchange_sha256=sha(WHOLE)))
             for i, t in enumerate((WHOLE[:half], WHOLE[half:]))]
    with pytest.raises(HandlerError) as err:
        V._check_split_exchanges(SCREENED_NS, parts)
    assert err.value.code == "E_CONTEXT_MANIFEST"


def test_parts_split_by_another_item_are_refused_even_when_the_join_matches():
    parts = _split(WHOLE, 3)
    between = (2, item(ordinal=2, kind="thinking"))
    stored = [parts[0], parts[1], between, (3, dict(parts[2][1], ordinal=3))]
    with pytest.raises(HandlerError) as err:
        V._check_split_exchanges(SCREENED_NS, stored)
    assert err.value.code == "E_CONTEXT_MANIFEST"


# ---- nxxxxx's FIX FIRST on #433 ed88569c ---------------------------------------------------------------------------

DUP = '{"tool":"Bash","call":"password=hunter2","call":"x","result":"ok"}'   # json.loads keeps the last "call"


def test_a_duplicate_member_in_a_one_part_exchange_is_refused():
    assert json.loads(DUP)["call"] == "x", "control: the plain decode drops the first member"
    refused(item(text=DUP, exchange_sha256=sha(DUP)))
    assert V._decode_exchange(DUP) is None
    assert DUP in V._exchange_screen_text(item(text=DUP, exchange_sha256=sha(DUP))), "screened as stored, not decoded"


def test_a_duplicate_member_in_a_split_exchange_is_refused_before_any_screen():
    half = len(DUP) // 2
    parts = [(i, item(ordinal=i, text=t, part=i + 1, parts=2, exchange_sha256=sha(DUP)))
             for i, t in enumerate((DUP[:half], DUP[half:]))]
    with pytest.raises(HandlerError) as err:
        V._check_split_exchanges(V.namespace(), parts)          # unscreened: only the decode can refuse it
    assert err.value.code == "E_CONTEXT_MANIFEST"


class _Cursor:
    def __init__(self, rows):
        self.rows, self.result = rows, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.result = [(k, 1, True, self.rows[k]) for k in params[1] if k in self.rows] if "ANY" in sql else []

    def fetchall(self):
        return self.result

    def fetchone(self):
        return None


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


def _enforce(values, counts):
    rows = {key(v): v for v in values}
    m = dict(manifest(counts, n=len(values)), items=[[v["ordinal"], v.get("text_sha256")] for v in values])
    V._enforce_manifest(_Conn(rows), V.namespace(), m)


def test_f3_counts_in_a_batch_with_no_f3_item_are_refused_even_at_zero():
    plain = [item(ordinal=o, kind="user_text", role="user", text=t) for o, t in enumerate(("hello", "again"))]
    f1 = dict(BASE)
    _enforce(plain, f1)                                          # control: the F1 shape passes
    with pytest.raises(HandlerError) as err:
        _enforce(plain, dict(f1, tool_exchange=0, thinking=0, call_missing=0, result_missing=0, parts_extra=0))
    assert err.value.code == "E_CONTEXT_MANIFEST"
    mixed = [plain[0], item(ordinal=1)]
    _enforce(mixed, dict(BASE, tool_exchange=1, thinking=0, call_missing=0, result_missing=0, parts_extra=0))
    only_thinking = [plain[0], item(ordinal=1, kind="thinking")]
    _enforce(only_thinking, dict(BASE, tool_exchange=0, thinking=1, call_missing=0, result_missing=0, parts_extra=0))


def test_the_manifest_step_checks_split_exchanges_whole():
    # DB-free twin of test_context_write's split test, so the mutant runner on HQD reaches this path (V17).
    parts = [v for _o, v in _split(WHOLE, 3)]
    f3 = dict(BASE, written=3, tool_exchange=3, thinking=0, call_missing=0, result_missing=0, parts_extra=2)
    _enforce(parts, f3)
    with pytest.raises(HandlerError) as err:
        _enforce(parts[:2], dict(f3, written=2, tool_exchange=2, parts_extra=1))
    assert err.value.code == "E_CONTEXT_MANIFEST"


# ---- txxxx e6a4ef / GitHub Codex on #433 validation.py:279 ----------------------------------------------------------

ESCAPED_NAME = '{"tool":"Bash","call":{"note":"' + chr(92) + 'u005aorblax Corp"},"result":"ok"}'


def test_a_call_or_result_that_is_not_a_string_is_refused_so_no_escaped_name_hides_in_an_object():
    assert chr(92) + "u005a" in ESCAPED_NAME and "Zorblax" not in ESCAPED_NAME, "control: the escape is stored"
    refused(item(text=ESCAPED_NAME, exchange_sha256=sha(ESCAPED_NAME)))
    for bad in ('{"tool":"Bash","call":"x","result":["y"]}', '{"tool":"Bash","call":7,"result":"y"}'):
        refused(item(text=bad, exchange_sha256=sha(bad)))
    ok = '{"tool":"Bash","call":null,"result":"y"}'
    assert check(item(text=ok, exchange_sha256=sha(ok), call_status="missing")) == "item"


def test_a_one_part_exchange_is_screened_unescaped_too():
    text = '{"tool":"Bash","call":"' + chr(92) + 'u005aorblax Corp","result":"ok"}'
    assert any("Zorblax Corp" in t for t in V._exchange_screen_text(item(text=text, exchange_sha256=sha(text))))
    one = item(text=ESCAPED_NAME, exchange_sha256=sha(ESCAPED_NAME))
    assert any("Zorblax Corp" in t for t in V._exchange_screen_text(one)), "even an object field is seen unescaped"


def test_a_split_exchange_whose_join_holds_an_object_field_is_refused():
    half = len(ESCAPED_NAME) // 2
    parts = [(i, item(ordinal=i, text=t, part=i + 1, parts=2, exchange_sha256=sha(ESCAPED_NAME)))
             for i, t in enumerate((ESCAPED_NAME[:half], ESCAPED_NAME[half:]))]
    with pytest.raises(HandlerError) as err:
        V._check_split_exchanges(V.namespace(), parts)
    assert err.value.code == "E_CONTEXT_MANIFEST"
