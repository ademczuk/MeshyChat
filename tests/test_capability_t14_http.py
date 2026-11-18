"""T14, complete: over HTTP, no persisted row, response or log line holds a presented token (B07
slice 6; docs/DIVERGENCES.md 040; design docs/design/biscuit-capabilities.md 6.1 T14, 3.1, 3.2
"Where each refusal surfaces").

Slice 4's T14 (tests/test_capability_liveness.py) scanned the verifier's refusals in process, and
slice 5's the mint and revoke routes. Since slice 6 a refusal on the envelope path is a signed
failed result stored in skill_calls (error_message, result, result_payload), the signed payload is
stored in skill_calls.args and, for a write, in shared_memory.provenance_jsonb, and a token rides
every call on the two envelope routes. So this drives EVERY refusal path through the real routes:

  invalid        a token that does not parse                         view, 403
  holder         a token presented by a peer that does not hold it   view, 403
  revoked        a token revoked through the revoke route             handler, signed failure
  denied         a key outside the token's range                      handler, signed failure
  not accepted   memory.grant with a token                            handler, signed failure
                 a call to another peer with a token                  view, 403
                 a route not on the accept list (a mesh GET)          middleware, 403
  unbound        the header with no capability_sha256 in the payload  handler, signed failure
  mismatched     the header with another hash in the payload          handler, signed failure
  plane          a shared/* token delegated to a product-plane peer   handler, signed failure,
                                                                      and its tenancy_denials row
  unavailable    tokens off                                           middleware, 403

and one call the token ALLOWS (a write: its provenance and its result). Then it scans, for each
token presented, the skill_calls rows of every caller here (args, result, error_message,
result_payload), those callers' shared_memory rows (value and provenance), the register and
revocation rows of each token, the tenancy_denials rows of every caller, every response body and
header, and the captured log records and formatted lines. The needles are the whole token, its
first 16 characters (the design's), and two 24-character windows of its tail
(tests/capability_routes.py `needles`: a biscuit's head is shared by every token under one key,
so the tail is what identifies THIS one).

POSITIVE CONTROL: a token planted on purpose in a write's value by another peer is found by the
same scan in skill_calls.args and in shared_memory, and a line logged holding a token is found in
the log; each scanned place is non-empty.

Run:  pytest tests/test_capability_t14_http.py -q
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from tests.capability_invoke import OMIT, invoke_context, outcome  # noqa: E402
from tests.capability_routes import Seen, needles, peer_jwt  # noqa: E402
from tests.test_capability_header_refused import captured_logs, captured_streams  # noqa: E402


def _scan(token: str, places: dict[str, str]) -> dict[str, list[str]]:
    found = {}
    for where, text in places.items():
        hits = [name for name, needle in {**needles(token), "first 16": token[:16]}.items()
                if needle in text]
        if hits:
            found[where] = hits
    return found


def _text(rows) -> str:
    return "\n".join(str(r) for r in rows)


def _skill_grant(inv, grantee) -> None:
    """A memory.read skill grant from the memory authority's owner: what the pipeline's step 5
    asks of a caller outside its trust group (a product-plane peer is in none)."""
    inv.w.sql("INSERT INTO skill_grants (grantor_owner_id, grantee_owner_id, granter_peer_id, "
              "skill_name, source) SELECT owner_id, %s, id, 'memory.read', 'direct' "
              "FROM peers WHERE id = %s", (grantee.owner_id, inv.memory_peer))


def test_t14_over_http_no_row_response_or_log_line_holds_a_presented_token(caplog):
    with invoke_context() as inv, captured_logs(caplog), captured_streams() as formatted:
        o, a, t, other = (inv.peer(x) for x in ("O", "A", "T", "other"))
        p = inv.peer("P", plane="product")
        _skill_grant(inv, p)
        ns = inv.w.claim(o)
        shared = inv.w.claim(o, f"shared/t14h-{o.id[:8]}")
        w = inv.w
        rw = (("", "read"), ("", "write"))
        good = w.mint(issuer=o, subject=a, ns=ns, rights=rw).to_base64()
        tokens = {
            "invalid": good[:-8] + ("A" * 8 if not good.endswith("A" * 8) else "B" * 8),
            "holder": w.mint(issuer=o, subject=a, ns=ns, rights=rw).to_base64(),
            "revoked": w.mint(issuer=o, subject=a, ns=ns, rights=rw).to_base64(),
            "denied": w.mint(issuer=o, subject=a, ns=ns, rights=(("x/", "read"),)).to_base64(),
            "not accepted": w.mint(issuer=o, subject=a, ns=ns, rights=rw).to_base64(),
            "unbound": w.mint(issuer=o, subject=a, ns=ns, rights=rw).to_base64(),
            "mismatched": w.mint(issuer=o, subject=a, ns=ns, rights=rw).to_base64(),
            "plane": w.delegate(w.mint(issuer=o, subject=a, ns=shared, rights=rw), a,
                                p).to_base64(),
            "unavailable": w.mint(issuer=o, subject=a, ns=ns, rights=rw).to_base64(),
            "allowed": good,
        }
        revoked = inv.client.post("/v1/memory/capabilities/revoke",
                                  data='{"block": 0, "reason": "t14 over http"}',
                                  content_type="application/json",
                                  headers={"authorization": f"Bearer {peer_jwt(o.id)}",
                                           "Meshychat-Capability": tokens["revoked"]})
        inv.seen.append(Seen("/v1/memory/capabilities/revoke", revoked))
        assert revoked.status_code == 200, revoked.content

        read = {"namespace": ns, "key": "k"}
        got = {
            "invalid": inv.call(a, "memory.read", read, token=tokens["invalid"])[0],
            "holder": inv.call(t, "memory.read", read, token=tokens["holder"])[0],
            "revoked": inv.call(a, "memory.read", read, token=tokens["revoked"])[0],
            "denied": inv.call(a, "memory.read", read, token=tokens["denied"])[0],
            "not accepted: grant": inv.call(
                a, "memory.grant", {"namespace": ns, "grantee_peer_id": t.id,
                                    "permission": "read"}, token=tokens["not accepted"])[0],
            "not accepted: another peer": inv.call(
                a, "memory.read", read, token=tokens["not accepted"], target=other.id)[0],
            "unbound": inv.call(a, "memory.write", {"namespace": ns, "key": "u",
                                                    "value": {"n": 1}},
                                token=tokens["unbound"], capability_sha256=OMIT)[0],
            "mismatched": inv.call(a, "memory.write", {"namespace": ns, "key": "m",
                                                       "value": {"n": 1}},
                                   token=tokens["mismatched"],
                                   capability_sha256="0" * 64)[0],
            "plane": inv.call(p, "memory.read", {"namespace": shared, "key": "k"},
                              token=tokens["plane"])[0],
            "allowed": inv.call(a, "memory.write", {"namespace": ns, "key": "ok",
                                                    "value": {"n": 2}},
                                token=tokens["allowed"])[0],
        }
        mesh_get = inv.client.get("/v1/mesh/inbox", headers={
            "authorization": f"Bearer {peer_jwt(a.id)}",
            "Meshychat-Capability": tokens["not accepted"]})
        inv.seen.append(Seen("/v1/mesh/inbox", mesh_get))
        got["not accepted: a mesh GET"] = inv.seen[-1]
        with override_settings(MESHY_CAPABILITIES=False):
            got["unavailable"] = inv.call(a, "memory.read", read,
                                          token=tokens["unavailable"])[0]

        # POSITIVE CONTROL, planted on purpose by a peer of its own.
        planter = inv.peer("planter")
        planted_ns = w.claim(planter)
        planted = w.mint(issuer=planter, subject=planter, ns=planted_ns).to_base64()
        plant, _ = inv.call(planter, "memory.write", {"namespace": planted_ns, "key": "p",
                                                      "value": {"pasted": planted}})
        logging.getLogger("meshychat.t14").info("planted %s", planted)

        callers = [o.id, a.id, t.id, p.id]
        calls = w.owner.execute(
            "SELECT call_id, args, result, error_code, error_message, result_payload "
            "FROM skill_calls WHERE caller_peer_id = ANY(%s::uuid[])", (callers,)).fetchall()
        memory = w.owner.execute(
            "SELECT namespace, key, value, provenance_jsonb FROM shared_memory "
            "WHERE namespace = ANY(%s)", ([ns, shared],)).fetchall()
        ids = [bytes.fromhex(rid) for tok in tokens.values() if tok != tokens["invalid"]
               for rid in w.b.Biscuit.from_base64(tok, w.store.provider).revocation_ids]
        events = w.owner.execute(
            "SELECT row_to_json(e)::text FROM capability_event e "
            "WHERE authority_revocation_id = ANY(%s) OR revocation_id = ANY(%s)",
            (ids, ids)).fetchall()
        revocations = w.owner.execute(
            "SELECT row_to_json(r)::text FROM capability_revocation r "
            "WHERE revocation_id = ANY(%s)", (ids,)).fetchall()
        audits = w.owner.execute(
            "SELECT row_to_json(d)::text FROM tenancy_denials d "
            "WHERE caller_peer_id = ANY(%s::uuid[])", (callers,)).fetchall()
        planted_rows = (
            w.owner.execute("SELECT args FROM skill_calls WHERE caller_peer_id = %s",
                            (planter.id,)).fetchall(),
            w.owner.execute("SELECT value, provenance_jsonb FROM shared_memory "
                            "WHERE namespace = %s", (planted_ns,)).fetchall())

    assert {k: outcome(v) for k, v in got.items()} == {
        "invalid": (403, "E_CAPABILITY_INVALID", None),
        "holder": (403, "E_CAPABILITY_HOLDER", None),
        "revoked": (200, "failed", "E_CAPABILITY_REVOKED"),
        "denied": (200, "failed", "E_CAPABILITY_DENIED"),
        "not accepted: grant": (200, "failed", "E_CAPABILITY_NOT_ACCEPTED"),
        "not accepted: another peer": (403, "E_CAPABILITY_NOT_ACCEPTED", None),
        "unbound": (200, "failed", "E_BAD_ENVELOPE"),
        "mismatched": (200, "failed", "E_BAD_ENVELOPE"),
        "plane": (200, "failed", "E_PRODUCT_NO_SHARED_GRANT"),
        "allowed": (200, "completed", None),
        "not accepted: a mesh GET": (403, "E_CAPABILITY_NOT_ACCEPTED", None),
        "unavailable": (403, "E_CAPABILITY_UNAVAILABLE", None),
    }
    assert outcome(plant) == (200, "completed", None), plant
    assert any("product_no_shared_grant" in row[0] for row in audits), audits
    assert any(r[0] == "ok" and "capability" in r[1] for r in
               [(m[1], m[3]) for m in memory]), "the allowed write's provenance records how"

    places = {
        "skill_calls": _text(calls), "shared_memory": _text(memory),
        "register and revoke events": _text(events), "revocation rows": _text(revocations),
        "tenancy_denials": _text(audits),
        "responses": "\n".join(s.text() for s in inv.seen),
        "log records": "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}"
                                 for r in caplog.records
                                 if r.name != "meshychat.t14"),
        "formatted log": "\n".join(line for line in formatted.getvalue().splitlines()
                                   if "meshychat.t14" not in line),
    }
    for where, text in places.items():
        assert text.strip(), f"control: nothing was read from {where}"
    leaks = {name: _scan(token, places) for name, token in tokens.items()}
    assert all(not found for found in leaks.values()), leaks

    # The positive control: the same scan finds the planted token where it was put.
    control = {"skill_calls.args": _text(planted_rows[0]),
               "shared_memory": _text(planted_rows[1]),
               "log records": "\n".join(r.getMessage() for r in caplog.records),
               "formatted log": formatted.getvalue()}
    found = _scan(planted, control)
    assert set(found) == set(control), found
    assert all({"token", "first 16", "last 24"} <= set(v) for v in found.values()), found


def test_django_error_reports_mask_the_capability_header():
    """Slice 5's review I4, carried here: Django's error reporter (a DEBUG page, an
    AdminEmailHandler) masks `HTTP_AUTHORIZATION` and returned `HTTP_MESHYCHAT_CAPABILITY` as
    sent. Nothing renders it today (settings: ADMINS empty, DEBUG never on in a deploy), and
    since slice 6 the header reaches every envelope-path view, so the project's filter hides it
    too. Controls: the bearer is still masked, and an ordinary header is still shown."""
    from django.test import RequestFactory
    from django.views.debug import get_default_exception_reporter_filter

    token = "CAcS6QIK_gEKC2Nh" + "q" * 200
    request = RequestFactory().get("/v1/health", HTTP_MESHYCHAT_CAPABILITY=token,
                                   HTTP_AUTHORIZATION="Bearer header.payload.sig",
                                   HTTP_X_T14_ORDINARY="shown")
    meta = get_default_exception_reporter_filter().get_safe_request_meta(request)
    assert token[:16] not in str(meta) and meta["HTTP_MESHYCHAT_CAPABILITY"] != token, meta
    assert "header.payload.sig" not in str(meta["HTTP_AUTHORIZATION"])
    assert meta["HTTP_X_T14_ORDINARY"] == "shown"
