"""#252, the server half: a signed call whose payload holds U+0000 or a lone surrogate is
refused with a 400 E_VALIDATION naming the JSONPath, before anything hashes or stores it.

A JSON escape spells either one and json.loads keeps it. Before the fix, every such invoke
answered 500 E_INTERNAL: step 2's rfc8785 hash raises on the surrogate, and the call row
cannot hold U+0000. Most envelopes here are signed first and edited on the wire after: a lone
surrogate cannot be signed (rfc8785 refuses to canonicalise it), so that is the only way to
send one. U+0000 CAN be signed, so an honest client can send it; one test signs it in
(memory-bxxxxx's #275 review).
"""
from __future__ import annotations

import json
import uuid

import pytest

from apps.crypto.ijson import untextable_string_path
from tests.test_call_routing import client, owner_conn, server_config, world  # noqa: F401  (fixtures; server_config is autouse)
from tests.test_call_routing import bearer, memory_rows, signed

BS = chr(92)   # the backslash of a JSON escape, written so no source line holds the escape


def _post(client, caller, target, envelope, old=None, new=None):
    body = json.dumps({"envelope": envelope, "mode_override": "async",
                       "call_id": envelope["payload"]["call_id"]})
    if old is not None:
        assert body.count(old) == 1, "the marker must occur exactly once (~ is in no base64url, hex or uuid)"
        body = body.replace(old, new)
    return client.post(f"/v1/agents/{target}/skills/memory.write/invoke",
                       data=body, content_type="application/json", headers=bearer(caller))


def _call_rows(owner_conn, call_id: str) -> int:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_calls WHERE call_id = %s", (call_id,))
        n = cur.fetchone()[0]
    owner_conn.commit()
    return n


CASES = [
    ("lone high surrogate in a value", "a~b", "a" + BS + "ud800b", "$.args.value.text"),
    ("lone low surrogate in a value", "a~b", "a" + BS + "udfffb", "$.args.value.text"),
    ("U+0000 in a value", "a~b", "a" + BS + "u0000b", "$.args.value.text"),
    ("lone surrogate in a member name", "k~", "k" + BS + "ud800", "(a member name)"),
    ("U+0000 in a list item", "l~", "l" + BS + "u0000", "$.args.value.list[0]"),
]


@pytest.mark.parametrize("label,old,new,where", CASES, ids=[c[0] for c in CASES])
def test_untextable_text_is_a_400_naming_the_path_and_nothing_is_stored(
        client, owner_conn, world, label, old, new, where):
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    key = f"k-{uuid.uuid4().hex[:8]}"
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": key,
                       "value": {"text": "a~b", "k~": 1, "list": ["l~"]}})
    resp = _post(client, caller, memory_peer, envelope, old, new)

    assert resp.status_code == 400, (label, resp.status_code, resp.content)
    body = json.loads(resp.content)
    assert body["error"] == "E_VALIDATION", body
    assert where in body["message"], body["message"]
    assert "lone surrogate" in body["message"]
    assert _call_rows(owner_conn, envelope["payload"]["call_id"]) == 0, "no call row"
    assert memory_rows(owner_conn, ns, key) == [], "no memory row"


def test_control_the_same_envelope_unedited_is_accepted(client, owner_conn, world):
    """The positive control: the identical shape without the edit is written, so a 400 above
    is the text and not the envelope."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    key = f"k-{uuid.uuid4().hex[:8]}"
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": key,
                       "value": {"text": "a~b", "k~": 1, "list": ["l~"]}})
    resp = _post(client, caller, memory_peer, envelope)
    assert resp.status_code == 202, resp.content
    assert len(memory_rows(owner_conn, ns, key)) == 1


def test_untextable_string_path_finds_each_place_and_passes_clean_text():
    lone = chr(0xD800)
    assert untextable_string_path({"a": "ok", "b": [1, {"c": "x" + chr(0) + "y"}]}) == "$.b[1].c"
    assert untextable_string_path({"a": [lone]}) == "$.a[0]"
    assert untextable_string_path({lone: 1}) == "$ (a member name)"
    assert untextable_string_path(lone) == "$"
    # an ancestor name is echoed escaped and bounded (pxx's #275 review): no raw newline, and
    # at most 64 characters per segment
    newline = chr(10)
    path = untextable_string_path({"a" + newline + "b" + "x" * 200: {"c": chr(0)}})
    assert newline not in path, path
    assert path.startswith("$.a" + chr(92) + "nb") and path.endswith(".c"), path
    segment = path[len("$."):-len(".c")]
    assert len(segment) == 64 and segment.endswith("..."), path
    # clean text, including a PAIRED surrogate (an astral character) and the U+FFFD a loader
    # substitutes, passes
    assert untextable_string_path({"emoji": "\U0001F600", "rep": "�", "n": 1,
                                   "t": True, "z": None, "l": []}) is None


@pytest.mark.parametrize("field,bad,status,code", [
    ("signing_kid", "u0000", 422, "E_VALIDATION"), ("signing_kid", "ud800", 422, "E_VALIDATION"),
    ("signature", "u0000", 401, "E_SIG_BAD"), ("signature", "udfff", 401, "E_SIG_BAD")])
def test_untextable_signing_kid_or_signature_is_a_typed_4xx_not_500(
        client, owner_conn, world, field, bad, status, code):
    """pxx's #275 review: the envelope's other two strings. The kid is a Postgres parameter
    (fetch_peer_key) and answered 500 before the fix; it is now the envelope's own 422 shape
    answer. The signature already answered 401 E_SIG_BAD (its decode refuses); held here."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": f"k-{uuid.uuid4().hex[:8]}", "value": {"t": 1}})
    envelope[field] = envelope[field][:4] + "~" + envelope[field][4:]
    resp = _post(client, caller, memory_peer, envelope, "~", BS + bad)
    assert resp.status_code == status, (field, bad, resp.status_code, resp.content)
    body = json.loads(resp.content)
    assert body["error"] == code, body
    if status == 422:
        assert f"envelope.{field}" in body["message"], body
    assert _call_rows(owner_conn, envelope["payload"]["call_id"]) == 0


def test_a_signed_nul_is_a_400_not_a_500(client, owner_conn, world):
    """memory-bxxxxx's #275 review: U+0000 CAN be signed (rfc8785 canonicalises it), so an
    honest client can send one. No wire edit here: the NUL is in the signed bytes."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    key = f"k-{uuid.uuid4().hex[:8]}"
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": key, "value": {"text": "a" + chr(0) + "b"}})
    resp = _post(client, caller, memory_peer, envelope)
    assert resp.status_code == 400, resp.content
    body = json.loads(resp.content)
    assert body["error"] == "E_VALIDATION" and "$.args.value.text" in body["message"], body
    assert memory_rows(owner_conn, ns, key) == []


def test_an_unencodable_mode_override_is_422_not_500(client, world):
    """memory-bxxxxx's #275 review: the `unknown variant` message echoed the value raw, so a
    lone surrogate made the 422 response itself unencodable (a 500). It is now escaped."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": f"k-{uuid.uuid4().hex[:8]}", "value": {"t": 1}})
    body = json.dumps({"envelope": envelope, "mode_override": "x~",
                       "call_id": envelope["payload"]["call_id"]})
    assert body.count("~") == 1
    resp = client.post(f"/v1/agents/{memory_peer}/skills/memory.write/invoke",
                       data=body.replace("~", BS + "ud800"), content_type="application/json",
                       headers=bearer(caller))
    assert resp.status_code == 422, resp.content
    assert "unknown variant `x" + BS + BS + "ud800`" in resp.content.decode("utf-8"), resp.content


def test_echo_text_always_encodes_and_is_bounded():
    from apps.crypto.ijson import echo_text
    for v in ["plain", "x" + chr(0xD800), "a" + chr(10) + "b", 5, None, ["a"], "y" * 500]:
        out = echo_text(v)
        out.encode("utf-8")                     # never raises
        assert len(out) <= 64 and chr(10) not in out, (v, out)
    assert echo_text("plain") == "plain", "ordinary values are echoed unchanged"
