"""#252, the board door (memory-bxxxxx's #275 review): `POST /v1/board/acts` dispatches the
memory write in process, so the call pipeline's step 1a never sees it.

Measured on main and on #275's head before this change:
- a validly signed act whose free-text body holds U+0000 answered 500 E_INTERNAL;
- a signing_kid holding U+0000 or a lone surrogate answered 500 (it is a Postgres parameter).
Both are now the door's own 422 E_BAD_REQUEST, nothing lands, and a clean act is still 201.
"""
from __future__ import annotations

import json

import pytest

from tests.test_board_declare import (_envelope, _peer, _post, _subj,  # noqa: F401
                                      board_settings, client, owner)

BS = chr(92)


def _post_raw(client, peer: dict, body: str):
    from tests.test_board_declare import _auth
    return client.post("/v1/board/acts", data=body, content_type="application/json",
                       **_auth(peer["peer_id"]))


def test_a_signed_nul_in_the_body_is_422_and_nothing_lands(client, owner):
    """U+0000 CAN be signed: rfc8785 canonicalises it. This is an honest client's request."""
    p = _peer(owner, "nulbody")
    s = _subj("nulbody")
    env, key = _envelope(p, s, value_edit=lambda v: v.__setitem__("body", "a" + chr(0) + "b"))
    r = _post(client, p, env)
    assert r.status_code == 422, r.content[:300]
    body = r.json()
    assert body["error"] == "E_BAD_REQUEST", body
    assert "$.payload.args.value.body" in body["message"], body["message"]
    clean, _ = _envelope(p, _subj("nulbody-ok"))
    assert _post(client, p, clean).status_code == 201, "control: a clean act still lands"


@pytest.mark.parametrize("bad", ["u0000", "ud800", "udfff"])
def test_an_untextable_kid_is_422(client, owner, bad):
    p = _peer(owner, "badkid")
    env, _ = _envelope(p, _subj("badkid"))
    env["signing_kid"] = env["signing_kid"] + "~"
    body = json.dumps({"envelope": env})
    assert body.count("~") == 1, "~ is in no base64url, hex or uuid"
    r = _post_raw(client, p, body.replace("~", BS + bad))
    assert r.status_code == 422, (bad, r.status_code, r.content[:300])
    assert r.json()["error"] == "E_BAD_REQUEST"
    assert "envelope.signing_kid" in r.json()["message"]


def test_a_lone_surrogate_in_the_body_is_422(client, owner):
    """A lone surrogate cannot be signed, so the request is edited on the wire after signing."""
    p = _peer(owner, "surbody")
    env, _ = _envelope(p, _subj("surbody"), value_edit=lambda v: v.__setitem__("body", "a~b"))
    body = json.dumps({"envelope": env})
    assert body.count("~") == 1
    r = _post_raw(client, p, body.replace("~", BS + "ud800"))
    assert r.status_code == 422, r.content[:300]
    assert r.json()["error"] == "E_BAD_REQUEST"


@pytest.mark.parametrize("kid", [123, [1], {"a": 1}, True], ids=["int", "list", "object", "bool"])
def test_a_non_string_kid_is_422(client, owner, kid):
    """#277 (memory-bxxxxx's #276 review, N1): a kid that is not a string answered 500."""
    p = _peer(owner, "kidtype")
    env, _ = _envelope(p, _subj("kidtype"))
    env["signing_kid"] = kid
    r = _post_raw(client, p, json.dumps({"envelope": env}))
    assert r.status_code == 422, (kid, r.status_code, r.content[:300])
    assert r.json()["error"] == "E_BAD_REQUEST"
    assert "envelope.signing_kid must be a string" in r.json()["message"]
    clean, _ = _envelope(p, _subj("kidtype-ok"))
    assert _post(client, p, clean).status_code == 201, "control: a clean act still lands"
