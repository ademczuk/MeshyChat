"""`python -m tools.meshy_node owed --reconcile` (tools/meshy_node/reconcile.py): every ask a seat's owed lists, in
exactly one class with its evidence, and counts that balance (SD 265 item 3; design note f3a6ce; pxx's read d910ea).

No database and no server: the three owed lists and the signed reads are stand-ins, and node.read's own record=False
test fakes `invoke` and the two checks, as tests/test_meshy_node_read_ledger.py does. nxxxxx's cases (fe7c57 item 3)
are fixtures MODELED on its description (seven open or active asks with a median age of 23 h, four closed by signed
messages; three duplicate-body pairs sent to different recipients). They are not its rows, which this file never read.

Run:  pytest tests/test_meshy_node_owed_reconcile.py -q     (needs no database)
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node, reconcile, wire  # noqa: E402

NOW = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
ME = "a2b5be8f-aa4f-5634-8b28-97de57204a14"          # kxxx, on the seat roster
OXXXXXx = "610170d5-09fb-5385-9ea4-fc3df696b431"
TXXXX = "2d6d1628-bdc0-5034-a975-e0ffe6e2e9c4"
PXX = "717734fb-b4a3-5125-95a0-e21bbb0f7e8c"
SIGNERS = {OXXXXXx: "oxxxxxx", ME: "kxxx"}
#: Planted in every body and done_when: it must never reach the output.
MARKER = "PLANTED-BODY-MARKER-4c1e"
H = 3600


@pytest.fixture(autouse=True)
def _outside_any_session(monkeypatch):
    """As tests/test_meshy_node_read_ledger.py: no Claude Code session, whatever shell runs pytest."""
    monkeypatch.delenv(node.SESSION_ENV, raising=False)


def _ask(key, author, seconds_ago, *, state="open", src="to:list", thread="t1", done_when=None, now=None):
    """An owed row recorded `seconds_ago` before `now` (the fixed NOW by default). A test that goes through cli.main
    must pass the REAL clock: the command judges expiry against it (memory-bxxxxx 3a49a1: an ask built from the fixed
    NOW passed its 72 h on Tue 10 Nov 23:00 UTC and every full gate failed from then)."""
    at = (NOW if now is None else now) - timedelta(seconds=seconds_ago)
    return {"hit": {"key": key, "version": 1, "edge_type": "owes_ack", "dst": "kxxx", "src_field": src,
                    "recorded_at": at.isoformat(), "author": author},
            "thread_id": thread, "answered_in_thread": state != "open", "answered_directly": state == "answered",
            "state": state, "done_when": done_when, "edited": False, "msg_type": "request"}


def _lists(*asks, cut=()):
    return {listed: {"owed": [a for a in asks if a["state"] == listed], "truncated": listed in cut,
                     "filters": {"state": [listed]}} for listed in reconcile.STATES}


def _msg(signer, *, body=MARKER + " please review", to=("kxxx",), cc=(), closes=None, verified=True, directive=None):
    value = {"body": body, "to": list(to), "cc": list(cc)}
    if closes is not None:
        value["closes"] = closes
    if directive is not None:
        value["directive"] = directive
    out = {"body": body, "to": list(to), "cc": list(cc), "signature_verified": verified,
           "signed_by": signer if verified else None, "_value": value}
    if not verified:
        out["signature_error"] = "E_SIG: fixture"
    return out


#: The recipients route as a stand-in: each label is its own peer, except the aliases of one peer.
ALIASES = {"kxxx": ME, "peer-a2b5be8f": ME, "pxx": PXX, "peer-717734fb": PXX, "txxxx": TXXXX, "peer-2d6d1628": TXXXX}


def _recipients(labels):
    return {"recipients": [{"label": lb, "peers": [{"peer_id": ALIASES.get(lb, f"p-{lb}")}]} for lb in labels
                           if not lb.startswith("unknown")]}


def _unread(*keys, cut=False, since="default"):
    """A GET /v1/mesh/unread answer; `filters.since` is the window the server says it covered (the gate by default)."""
    answer = {"unread": {"items": [{"key": k, "version": 1} for k in keys], "total": len(keys), "truncated": cut}}
    if since is not None:
        answer["filters"] = {"since": (GATE.isoformat() if since == "default" else since), "capped": False}
    return answer


#: The unread gate as the command sets it for the default window: 14 days back from NOW.
GATE = NOW - timedelta(seconds=reconcile.UNREAD_GATE_S)


#: The versions each read asked for, by key, from the last _run (every read is of an exact version).
VERSIONS: dict = {}


def _anchor(version=1, hours_ago=1):
    """A GET /v1/mesh/neighbours answer: the receipt's head version and the server's time of it."""
    return {"anchor": {"key": "x", "version": version, "recorded_at": (NOW - timedelta(hours=hours_ago)).isoformat()}}


def _run(lists, messages=None, *, closures=(), expire_s=72 * H, max_reads=60, unread=None, recipients=_recipients,
         gate_since=GATE, anchors=None):
    reads = []
    VERSIONS.clear()

    def read(key, version):
        reads.append(key)
        VERSIONS[key] = version
        got = (messages or {})[key]
        if isinstance(got, Exception):
            raise got
        return got

    def anchor(key):
        got = (anchors or {}).get(key, _anchor())
        if isinstance(got, Exception):
            raise got
        return got
    result = reconcile.reconcile(lists, read, unread=unread if unread is not None else _unread(), gate_since=gate_since,
                                 anchor=anchor, recipients=recipients, me_label="kxxx", signers=SIGNERS,
                                 peer_labels=node.SEAT_DIRECTIVE_PEERS, closures=list(closures), expire_s=expire_s,
                                 max_reads=max_reads, now=NOW)
    return result, reads


def _cls(result):
    return {row["key"]: row["class"] for row in result["rows"]}


def _row(result, key):
    return next(row for row in result["rows"] if row["key"] == key)


def test_nxxxxxs_shape_four_closed_by_receipts_and_three_fanout_pairs_kept_as_rows():
    """Seven asks (median 23 h): four closed by two oxxxxxx receipts, one live, one active and old (still live), one
    open past the expiry. Three pairs from three authors, same body each, to different recipient sets: three groups,
    six rows kept, each with the class it would otherwise have. The counts balance."""
    asks = [_ask("msg/1-a1", "uxxx", 5 * H), _ask("msg/1-a2", "txxxx", 12 * H, state="active"),
            _ask("msg/1-a3", "nxxxxx", 20 * H), _ask("msg/1-a4", "dxxxxx", 23 * H, thread="t2"),
            _ask("msg/1-a5", "txxxx", 30 * H), _ask("msg/1-a6", "uxxx", 40 * H, state="active"),
            _ask("msg/1-a7", "nxxxxx", 80 * H)]
    pairs = []
    for n, (author, peer) in enumerate((("txxxx", TXXXX), ("pxx", PXX), ("oxxxxxx", OXXXXXx))):
        base = (2 + n) * H
        pairs += [(_ask(f"msg/2-f{n}a", author, base), _msg(peer, body=f"{MARKER} ask {n}", to=("kxxx", "pxx"))),
                  (_ask(f"msg/2-f{n}b", author, base - 180), _msg(peer, body=f"{MARKER} ask {n}",
                                                                  to=("kxxx", "nxxxxx")))]
    receipts = {"msg/1794090000000-r1": _msg(OXXXXXx, closes=["msg/1-a1", "msg/1-a2"]),
                "msg/1794090000000-r2": _msg(OXXXXXx, closes=["msg/1-a3", "msg/1-a4"])}
    result, reads = _run(_lists(*asks, *(a for a, _ in pairs)), {**receipts, **{a["hit"]["key"]: m for a, m in pairs}},
                         closures=["msg/1794090000000-r1", "msg/1794090000000-r2"])
    got = _cls(result)
    assert [got[f"msg/1-a{i}"] for i in range(1, 8)] == ["closed"] * 4 + ["live_work", "live_work", "expired_unreviewed"]
    assert _row(result, "msg/1-a3")["evidence"] == {"receipt": "msg/1794090000000-r2"}
    fan = [k for k, c in got.items() if c == "intentional_fanout"]
    assert len(fan) == 6 and len(result["fanout_groups"]) == 3
    assert all(g["kind"] == "fanout" and len(g["keys"]) == 2 and g["recipient_sets"] == 2
               for g in result["fanout_groups"])
    assert {_row(result, k)["base_class"] for k in fan} == {"live_work"}
    assert result["balanced"] and result["states"] == {"open": 11, "active": 2, "answered": 0}
    assert result["classes"]["closed"] == 4 and result["classes"]["intentional_fanout"] == 6
    assert result["equation"] == ("open 11 + active 2 + answered 0 = live_work 2 + edge_only 0 + expired_unreviewed 1 "
                                  "+ intentional_fanout 6 + retransmission 0 + answered 0 + closed 4 + unknown 0 "
                                  "+ duplicate 0")
    assert reads[:2] == ["msg/1794090000000-r1", "msg/1794090000000-r2"], "the receipts are read first"


def test_a_receipt_closes_only_verified_by_oxxxxxx_or_this_seat_and_naming_the_key_exactly():
    """pxx d910ea B. MUTANTS: any verified signer closes; the signature is not required; the key is matched without
    case or as a prefix; a `closes` string is taken as a list; a receipt that cannot close leaves the ask as it was."""
    asks = [_ask(f"msg/1-k{i}", "uxxx", (i + 1) * H) for i in range(1, 7)]
    receipts = {
        "msg/1794090000000-rm": _msg(OXXXXXx, closes=["msg/1-k1"]),
        "msg/1794090000000-rj": _msg(ME, closes=["msg/1-k2"]),
        "msg/1794090000000-rs": _msg(TXXXX, closes=["msg/1-k3"]),
        "msg/1794090000000-ru": _msg(OXXXXXx, closes=["msg/1-k4"], verified=False),
        "msg/1794090000000-rx": _msg(OXXXXXx, closes=["msg/1-K5", "msg/1-k5-other", "msg/1-k", "MSG/1-k5"], body="closes msg/1-k5"),
        "msg/1794090000000-rt": _msg(OXXXXXx, closes="msg/1-k6"),
        "msg/1794090000000-rf": OSError("E_NOT_FOUND: no such key"),
    }
    result, _ = _run(_lists(*asks), receipts, closures=list(receipts))
    got = _cls(result)
    assert got["msg/1-k1"] == "closed" and got["msg/1-k2"] == "closed"
    assert got["msg/1-k3"] == "unknown" and "msg/1794090000000-rs (signer_not_allowed)" in _row(result, "msg/1-k3")["evidence"]["why"]
    assert got["msg/1-k4"] == "unknown" and "msg/1794090000000-ru (unverified)" in _row(result, "msg/1-k4")["evidence"]["why"]
    assert got["msg/1-k5"] == "live_work", "a key named in another case, as a prefix or in a body closes nothing"
    assert got["msg/1-k6"] == "live_work", "a `closes` string is not a list, as the server's projection reads it"
    status = {r["key"]: r["status"] for r in result["receipts"]}
    assert status == {"msg/1794090000000-rm": "ok", "msg/1794090000000-rj": "ok", "msg/1794090000000-rs": "signer_not_allowed", "msg/1794090000000-ru": "unverified",
                      "msg/1794090000000-rx": "ok", "msg/1794090000000-rt": "names_no_ask", "msg/1794090000000-rf": "unreadable"}
    rx = next(r for r in result["receipts"] if r["key"] == "msg/1794090000000-rx")
    assert rx["closes_listed"] == [] and rx["names_unlisted"] == ["msg/1-K5", "msg/1-k", "msg/1-k5-other"], \
        "MSG/1-k5 is not a msg/ key to the projector, so it is not named at all"
    rs = next(r for r in result["receipts"] if r["key"] == "msg/1794090000000-rs")
    assert rs["signer"] == "txxxx" and "only kxxx or oxxxxxx" in rs["detail"]
    assert result["balanced"]


def test_a_verified_receipt_wins_over_one_that_cannot_close():
    asks = [_ask("msg/1-k1", "uxxx", H)]
    receipts = {"msg/1794090000000-rs": _msg(TXXXX, closes=["msg/1-k1"]), "msg/1794090000000-rm": _msg(OXXXXXx, closes=["msg/1-k1"])}
    result, _ = _run(_lists(*asks), receipts, closures=["msg/1794090000000-rs", "msg/1794090000000-rm"])
    assert _cls(result) == {"msg/1-k1": "closed"} and _row(result, "msg/1-k1")["evidence"] == {"receipt": "msg/1794090000000-rm"}


def test_answered_stays_apart_from_closed_and_a_receipt_closes_an_answered_ask():
    """pxx A: a direct reply is not a settlement. MUTANT: answered decided before the receipts."""
    asks = [_ask("msg/1-k1", "uxxx", H, state="answered"), _ask("msg/1-k2", "uxxx", 2 * H, state="answered")]
    result, _ = _run(_lists(*asks), {"msg/1794090000000-rm": _msg(OXXXXXx, closes=["msg/1-k2"])}, closures=["msg/1794090000000-rm"])
    assert _cls(result) == {"msg/1-k1": "answered", "msg/1-k2": "closed"}
    assert _row(result, "msg/1-k1")["evidence"] == {"server_state": "answered"}
    assert result["states"]["answered"] == 2 and result["balanced"]


def test_the_same_body_to_the_same_recipients_is_a_retransmission_and_the_first_keeps_its_class():
    """Recipients compare without order or case. MUTANT: the first copy is marked too; the order of `to` matters."""
    asks = [_ask("msg/1-r1", "txxxx", 2 * H), _ask("msg/1-r2", "txxxx", 2 * H - 300)]
    msgs = {"msg/1-r1": _msg(TXXXX, to=("kxxx", "pxx")), "msg/1-r2": _msg(TXXXX, to=("Pxx", "kxxx"))}
    result, _ = _run(_lists(*asks), msgs)
    assert _cls(result) == {"msg/1-r1": "live_work", "msg/1-r2": "retransmission"}
    assert _row(result, "msg/1-r2")["evidence"] == {"copy_of": "msg/1-r1"}
    assert _row(result, "msg/1-r2")["base_class"] == "live_work"
    assert _row(result, "msg/1-r1")["fanout_group"] == "msg/1-r1"
    # The content hash pinned in its canonical form: the signed value without NOT_CONTENT (here to and cc), sorted
    # keys, no whitespace (pxx d95791 P2-1). The fixture's value has no other field.
    canon = '{"body":"' + MARKER + ' please review"}'
    sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    assert result["fanout_groups"] == [{"id": "msg/1-r1", "kind": "retransmission", "signer": "txxxx",
                                        "content_sha256": sha, "recipient_sets": 1, "keys": ["msg/1-r1", "msg/1-r2"]}]


def test_a_subject_or_a_ref_makes_another_ask_of_the_same_body():
    """GitHub Codex on #547 at d3a42158: "please review" for two PRs, same body and recipients, hashed alike and the
    second read as a copy. subject and ref name the object, so they are part of the content. MUTANT: the hash leaves
    them out."""
    asks = [_ask("msg/1-s1", "pxx", 2 * H), _ask("msg/1-s2", "pxx", 2 * H - 60), _ask("msg/1-s3", "pxx", 2 * H - 120),
            _ask("msg/1-s4", "pxx", 2 * H - 180)]
    msgs = {"msg/1-s1": _msg(PXX, body="please review"), "msg/1-s2": _msg(PXX, body="please review"),
            "msg/1-s3": _msg(PXX, body="please review"), "msg/1-s4": _msg(PXX, body="please review")}
    msgs["msg/1-s1"]["_value"]["subject"], msgs["msg/1-s2"]["_value"]["subject"] = "#547", "#548"
    msgs["msg/1-s3"]["_value"]["ref"], msgs["msg/1-s4"]["_value"]["ref"] = "pr/547@a35a8f48", "pr/547@d3a42158"
    result, _ = _run(_lists(*asks), msgs)
    assert set(_cls(result).values()) == {"live_work"} and result["fanout_groups"] == []


def test_the_same_ask_in_another_thread_is_another_obligation():
    """GitHub Codex on #547 at 677b04c7: the same ask to the same recipients in two threads hashed alike, so the
    second read as a copy, though owed_acks judges activity per thread. thread_id is part of the content.
    MUTANT: the thread left out of the hash."""
    asks = [_ask("msg/1-t1", "pxx", 2 * H, thread="review-a"), _ask("msg/1-t2", "pxx", 2 * H - 60, thread="review-b")]
    msgs = {"msg/1-t1": _msg(PXX, body="Please review this thread"), "msg/1-t2": _msg(PXX, body="Please review this thread")}
    msgs["msg/1-t1"]["_value"]["thread_id"], msgs["msg/1-t2"]["_value"]["thread_id"] = "review-a", "review-b"
    result, _ = _run(_lists(*asks), msgs)
    assert _cls(result) == {"msg/1-t1": "live_work", "msg/1-t2": "live_work"} and result["fanout_groups"] == []


def test_the_same_words_in_reply_to_another_message_are_another_obligation():
    """GitHub Codex on #547 at 20ce7ac6: two follow-ups with the same words about two messages hashed alike, though
    normalize.project makes each its own obligation. in_reply_to is content. MUTANT: the reply target not hashed."""
    asks = [_ask("msg/1-p1", "pxx", 2 * H), _ask("msg/1-p2", "pxx", 2 * H - 60)]
    msgs = {"msg/1-p1": _msg(PXX, body="Please explain this decision"),
            "msg/1-p2": _msg(PXX, body="Please explain this decision")}
    msgs["msg/1-p1"]["_value"]["in_reply_to"], msgs["msg/1-p2"]["_value"]["in_reply_to"] = "msg/1-d1", "msg/1-d2"
    result, _ = _run(_lists(*asks), msgs)
    assert _cls(result) == {"msg/1-p1": "live_work", "msg/1-p2": "live_work"} and result["fanout_groups"] == []


def test_a_real_resend_is_one_ask_whatever_its_stamp_label_or_session_and_every_other_field_is_content():
    """Values as wire.mesh_message signs them. The same ask sent 300 s later, under the peer's other label and from
    another session, is a retransmission. One that differs only in requires_ack, a field no list of content fields
    named, is another ask. MUTANT: ts hashed; session_label hashed; from hashed; a list of fields to include."""
    def signed(sender, ts, session, **extra):
        value = wire.mesh_message(to=["kxxx"], sender=sender, msg_type="request", thread_id="t1", body="Please review",
                                  author_family="claude", ts=ts, session_label=session, **extra)
        return {"body": value["body"], "to": value["to"], "cc": [], "signature_verified": True, "signed_by": PXX,
                "_value": value}
    asks = [_ask("msg/1-w1", "pxx", 3 * H), _ask("msg/1-w2", "peer-717734fb", 3 * H - 300),
            _ask("msg/1-w3", "pxx", 3 * H - 600)]
    msgs = {"msg/1-w1": signed("pxx", "2026-11-07T21:00:00Z", "pxx-a", requires_ack=True),
            "msg/1-w2": signed("peer-717734fb", "2026-11-07T21:05:00Z", "pxx-b", requires_ack=True),
            "msg/1-w3": signed("pxx", "2026-11-07T21:10:00Z", "pxx-a")}
    result, _ = _run(_lists(*asks), msgs)
    assert _cls(result) == {"msg/1-w1": "live_work", "msg/1-w2": "retransmission", "msg/1-w3": "live_work"}
    assert _row(result, "msg/1-w2")["evidence"] == {"copy_of": "msg/1-w1"}


def test_what_is_not_content_is_five_v1_fields_and_nothing_else():
    """A misspelt name would hash the field it meant to leave out; a sixth would make it alike in two asks.
    MUTANT: a name that is not a v1 field."""
    assert reconcile.NOT_CONTENT == ("to", "cc", "from", "ts", "session_label")
    assert set(reconcile.NOT_CONTENT) <= wire.V1_REQUIRED | wire.V1_OPTIONAL


def test_the_gate_starts_where_the_servers_unread_window_actually_starts():
    """GitHub Codex on #547 at d3a42158: with a call retention under 15 days the server moves `since` up and says so
    in filters.since; a candidate between the asked gate and that cutoff is absent from the list without having been
    fetched. The gate now starts at the later of the two, and an answer without its window blocks every read.
    MUTANTS: filters.since ignored; a missing window not blocking."""
    asks = [_ask("msg/1-w1", "uxxx", 8 * 86400), _ask("msg/1-w2", "uxxx", 8 * 86400 - 60),
            _ask("msg/1-n1", "txxxx", 2 * H), _ask("msg/1-n2", "txxxx", 2 * H - 60)]
    msgs = {"msg/1-n1": _msg(TXXXX, to=("kxxx",)), "msg/1-n2": _msg(TXXXX, to=("kxxx", "pxx"))}
    capped = (NOW - timedelta(days=6)).isoformat()
    result, reads = _run(_lists(*asks), msgs, unread=_unread(since=capped))
    assert reads == ["msg/1-n1", "msg/1-n2"]
    assert _row(result, "msg/1-w1")["fanout_unchecked"] == f"unread: {reconcile.UNREAD_OLDER}"
    assert result["unread_gate"]["since"] == capped and result["unread_gate"]["requested"] == GATE.isoformat()
    blind, reads = _run(_lists(*asks), msgs, unread=_unread(since=None))
    assert reads == [] and blind["unread_gate"]["blocked"] == reconcile.UNREAD_NO_WINDOW
    assert "unread gate: " + reconcile.UNREAD_NO_WINDOW in reconcile.render(blind, since=None, thread=None,
                                                                              answered_list="x")


def test_two_directives_with_one_boilerplate_body_are_two_rows_and_a_resent_one_is_a_retransmission():
    """pxx d95791 P2-1: a directive's body is boilerplate, so the content hash covers the directive field. MUTANT: the
    body alone hashed."""
    body = "The words are in the directive field."
    asks = [_ask("msg/1-d1", "pxx", 2 * H), _ask("msg/1-d2", "pxx", 2 * H - 60), _ask("msg/1-d3", "pxx", 2 * H - 120)]
    msgs = {"msg/1-d1": _msg(PXX, body=body, directive={"words": "build item 1"}),
            "msg/1-d2": _msg(PXX, body=body, directive={"words": "build item 3"}),
            "msg/1-d3": _msg(PXX, body=body, directive={"words": "build item 1"})}
    result, _ = _run(_lists(*asks), msgs)
    assert _cls(result) == {"msg/1-d1": "live_work", "msg/1-d2": "live_work", "msg/1-d3": "retransmission"}
    assert _row(result, "msg/1-d3")["evidence"] == {"copy_of": "msg/1-d1"} and "fanout_group" not in _row(result,
                                                                                                         "msg/1-d2")


def test_recipients_compare_as_peers_so_an_alias_is_one_recipient_and_a_failed_route_groups_nothing():
    """pxx d95791 P2-2: `kxxx` and `peer-a2b5be8f` are one peer, so the same ask to both is a resend, not fan-out; a
    label the route does not know compares as itself. MUTANTS: labels compared, not peers; a failed route compares
    labels anyway."""
    asks = [_ask("msg/1-a1", "txxxx", 2 * H), _ask("msg/1-a2", "txxxx", 2 * H - 60),
            _ask("msg/1-u1", "uxxx", 5 * H), _ask("msg/1-u2", "uxxx", 5 * H - 60)]
    msgs = {"msg/1-a1": _msg(TXXXX, to=("kxxx", "pxx")), "msg/1-a2": _msg(TXXXX, to=("peer-a2b5be8f", "peer-717734fb")),
            "msg/1-u1": _msg(TXXXX, to=("kxxx", "unknown-x")), "msg/1-u2": _msg(TXXXX, to=("kxxx", "unknown-y"))}
    result, _ = _run(_lists(*asks), msgs)
    got = _cls(result)
    assert got["msg/1-a2"] == "retransmission" and _row(result, "msg/1-a2")["evidence"] == {"copy_of": "msg/1-a1"}
    assert got["msg/1-u1"] == got["msg/1-u2"] == "intentional_fanout", "two unknown labels are two recipients"

    def broken(labels):
        raise OSError("route down")
    result, reads = _run(_lists(*asks), msgs, recipients=broken)
    assert set(_cls(result).values()) == {"live_work"} and result["fanout_groups"] == [] and reads == []
    assert _row(result, "msg/1-a2")["fanout_unchecked"] == ("recipients not resolved: the recipients route failed "
                                                            "(OSError: route down)")
    assert result["fanout_unchecked"]["recipients"] == 4

    def authors_only(labels):                               # the authors resolve; the recipients' call fails
        if set(labels) - {"txxxx", "uxxx"}:
            raise OSError("route down")
        return _recipients(labels)
    result, reads = _run(_lists(*asks), msgs, recipients=authors_only)
    assert len(reads) == 4 and result["fanout_groups"] == [] and set(_cls(result).values()) == {"live_work"}
    assert _row(result, "msg/1-a2")["fanout_unchecked"] == ("recipients not resolved: the recipients route failed "
                                                            "(OSError: route down)")


def test_one_peer_posting_under_two_labels_is_one_author():
    """GitHub Codex on #547 (a35a8f48): candidates were chosen by author LABEL, so the same ask from `txxxx` and from
    `peer-2d6d1628` was never read. Authors now resolve to peers first. MUTANT: candidates by label."""
    asks = [_ask("msg/1-s1", "txxxx", 3 * H), _ask("msg/1-s2", "peer-2d6d1628", 3 * H - 60),
            _ask("msg/1-t1", "uxxx", 9 * H)]
    msgs = {"msg/1-s1": _msg(TXXXX, to=("kxxx",)), "msg/1-s2": _msg(TXXXX, to=("kxxx",))}
    result, reads = _run(_lists(*asks), msgs)
    assert reads == ["msg/1-s1", "msg/1-s2"], "uxxx's ask has nothing near it, so it is not read"
    assert _cls(result) == {"msg/1-s1": "live_work", "msg/1-s2": "retransmission", "msg/1-t1": "live_work"}
    assert _row(result, "msg/1-s2")["evidence"] == {"copy_of": "msg/1-s1"}


def test_the_labels_resolved_are_bounded_and_past_the_bound_nothing_is_read(monkeypatch):
    """At most RESOLVE_MAX_LABELS a run (the recipients route takes 30 calls a minute). MUTANT: no bound."""
    monkeypatch.setattr(reconcile, "RESOLVE_MAX_LABELS", 1)
    asks = [_ask("msg/1-a1", "txxxx", 2 * H), _ask("msg/1-a2", "uxxx", 2 * H - 60)]
    result, reads = _run(_lists(*asks), {})
    assert reads == [] and result["fanout_unchecked"]["recipients"] == 2
    assert _row(result, "msg/1-a1")["fanout_unchecked"] == "recipients not resolved: more than 1 labels to resolve"


def test_reconcile_never_makes_the_first_read_of_a_message_still_unread():
    """pxx d95791 P1: on the server every completed read is a fetch, so a candidate or receipt still unread is not
    read. The candidate keeps its class, flagged; the receipt closes nothing. A cut unread list stops every read.
    MUTANTS: unread candidates read; an unread receipt read; a cut list ignored."""
    asks = [_ask(f"msg/1-c{i}", "uxxx", 10 * H - i * 60) for i in range(1, 4)] + [_ask("msg/1-k1", "txxxx", 20 * H)]
    msgs = {f"msg/1-c{i}": _msg(TXXXX, to=("kxxx", f"x{i}")) for i in range(1, 4)}
    msgs["msg/1794090000000-rm"] = _msg(OXXXXXx, closes=["msg/1-k1"])
    result, reads = _run(_lists(*asks), msgs, closures=["msg/1794090000000-rm"], unread=_unread("msg/1-c2", "msg/1794090000000-rm"))
    assert reads == ["msg/1-c1", "msg/1-c3"]
    assert _row(result, "msg/1-c2")["fanout_unchecked"] == f"unread: {reconcile.UNREAD_STILL}"
    assert _cls(result)["msg/1-c2"] == "live_work" and _cls(result)["msg/1-k1"] == "live_work"
    assert result["receipts"] == [{"key": "msg/1794090000000-rm", "status": "unread", "detail": reconcile.UNREAD_STILL}]
    assert _cls(result)["msg/1-c1"] == _cls(result)["msg/1-c3"] == "intentional_fanout"
    assert result["fanout_unchecked"]["unread"] == 1
    assert result["unread_gate"] == {"checked": True, "cut": False, "since": GATE.isoformat(),
                                     "requested": GATE.isoformat()}
    cut, reads = _run(_lists(*asks), msgs, closures=["msg/1794090000000-rm"], unread=_unread(cut=True))
    assert reads == [] and cut["unread_gate"] == {"checked": False, "cut": True, "since": GATE.isoformat(),
                                                  "requested": GATE.isoformat(), "blocked": reconcile.UNREAD_CUT}
    assert cut["receipts"] == [{"key": "msg/1794090000000-rm", "status": "not_read", "detail": reconcile.UNREAD_CUT}]
    assert cut["fanout_unchecked"]["unread"] == 3 and cut["balanced"]
    assert "unread gate: " + reconcile.UNREAD_CUT in reconcile.render(cut, since=None, thread=None, answered_list="x")
    with pytest.raises(reconcile.Malformed, match="unread.items"):
        _run(_lists(*asks), msgs, unread={"unread": {"truncated": False}})


def test_nothing_older_than_the_unread_gate_is_read_and_a_receipt_is_placed_by_the_servers_time():
    """The gate covers at most UNREAD_GATE_S (an 89-day unread window timed out on production), so a candidate or a
    receipt older than it is not read. A receipt's time is the server's (the neighbours anchor), never the writer's
    clock in its key: a key that claims a recent time cannot pull an old receipt inside (pxx fd239c P2). Without a head
    version and time the receipt is not read. MUTANTS: old candidates read; old receipts read; the key's clock used."""
    gate = NOW - timedelta(days=5)
    asks = [_ask("msg/1-o1", "uxxx", 6 * 86400), _ask("msg/1-o2", "uxxx", 6 * 86400 - 60),
            _ask("msg/1-n1", "txxxx", 2 * H), _ask("msg/1-n2", "txxxx", 2 * H - 60)]
    fresh_key_old_server = f"msg/{int(NOW.timestamp() * 1000)}-oxxxxxx-old"     # the key says now; the server says 6 d
    msgs = {"msg/1-n1": _msg(TXXXX, to=("kxxx",)), "msg/1-n2": _msg(TXXXX, to=("kxxx", "pxx"))}
    anchors = {fresh_key_old_server: _anchor(hours_ago=6 * 24), "msg/1794090000000-gone": OSError("404"),
               "msg/1794090000000-bare": {"anchor": {"version": 1}}}
    result, reads = _run(_lists(*asks), msgs, closures=[fresh_key_old_server, "msg/1794090000000-gone",
                                                        "msg/1794090000000-bare"], gate_since=gate, anchors=anchors)
    assert reads == ["msg/1-n1", "msg/1-n2"]
    assert _row(result, "msg/1-o1")["fanout_unchecked"] == f"unread: {reconcile.UNREAD_OLDER}"
    assert _cls(result)["msg/1-o1"] == "expired_unreviewed" and _cls(result)["msg/1-n1"] == "intentional_fanout"
    assert [(r["key"], r["status"]) for r in result["receipts"]] == [
        (fresh_key_old_server, "not_read"), ("msg/1794090000000-gone", "version_not_known"),
        ("msg/1794090000000-bare", "version_not_known")]
    assert result["receipts"][0]["detail"] == reconcile.UNREAD_OLDER


def test_every_read_is_of_an_exact_version_never_the_head():
    """pxx fd239c P1: an unversioned read returns the head, so an edit landing between the unread GET and the read is
    fetched unseen and leaves the seat's unread list. Candidates are read at the version the owed snapshot listed, and
    a receipt at the head version the server's anchor gave; a candidate with no version is not read. MUTANTS: the
    head read (no version); the receipt read unpinned."""
    a1, a2, a3 = _ask("msg/1-v1", "txxxx", 2 * H), _ask("msg/1-v2", "txxxx", 2 * H - 60), _ask("msg/1-v3", "txxxx",
                                                                                              2 * H - 120)
    a1["hit"]["version"], a2["hit"]["version"], a3["hit"]["version"] = 3, 1, None
    msgs = {"msg/1-v1": _msg(TXXXX, to=("kxxx",)), "msg/1-v2": _msg(TXXXX, to=("kxxx",)),
            "msg/1794090000000-rm": _msg(OXXXXXx, closes=["msg/1-v1"])}
    result, reads = _run(_lists(a1, a2, a3), msgs, closures=["msg/1794090000000-rm"],
                         anchors={"msg/1794090000000-rm": _anchor(version=2)})
    assert VERSIONS == {"msg/1794090000000-rm": 2, "msg/1-v1": 3, "msg/1-v2": 1}
    assert "msg/1-v3" not in reads and _row(result, "msg/1-v3")["fanout_unchecked"].startswith("unread: the owed list "
                                                                                                "gave no version")


def test_different_recipient_sets_are_fanout_every_row_kept_and_a_copy_of_one_set_is_a_retransmission():
    """cc counts in the recipient set. Every set's first row is fanout with its base class; a later copy of a set is a
    retransmission of that set's first. MUTANTS: cc ignored; only later rows marked fanout."""
    asks = [_ask("msg/1-f1", "pxx", 100 * H), _ask("msg/1-f2", "pxx", 100 * H - 120),
            _ask("msg/1-f3", "pxx", 100 * H - 240), _ask("msg/1-f4", "pxx", 100 * H - 360)]
    msgs = {"msg/1-f1": _msg(PXX, to=("kxxx",)), "msg/1-f2": _msg(PXX, to=("kxxx",), cc=("txxxx",)),
            "msg/1-f3": _msg(PXX, to=("kxxx", "uxxx")), "msg/1-f4": _msg(PXX, to=("KXXX",))}
    result, _ = _run(_lists(*asks), msgs)
    assert _cls(result) == {"msg/1-f1": "intentional_fanout", "msg/1-f2": "intentional_fanout",
                            "msg/1-f3": "intentional_fanout", "msg/1-f4": "retransmission"}
    assert {_row(result, k)["base_class"] for k in ("msg/1-f1", "msg/1-f2", "msg/1-f3")} == {"expired_unreviewed"}
    assert _row(result, "msg/1-f4")["evidence"] == {"copy_of": "msg/1-f1"}
    (group,) = result["fanout_groups"]
    assert group["kind"] == "fanout" and group["recipient_sets"] == 3 and len(group["keys"]) == 4
    assert all(_row(result, k)["fanout_group"] == "msg/1-f1" for k in group["keys"])


def test_the_window_is_600_seconds_between_neighbours():
    """600 s apart groups, 601 s does not, and a chain of neighbours each within 600 s is one group. MUTANTS: `<`
    for `<=`; anchored to the first ask instead of the one before."""
    near = [_ask("msg/1-n1", "uxxx", 5 * H), _ask("msg/1-n2", "uxxx", 5 * H - 600)]
    far = [_ask("msg/1-x1", "txxxx", 5 * H), _ask("msg/1-x2", "txxxx", 5 * H - 601)]
    chain = [_ask("msg/1-c1", "nxxxxx", 8 * H), _ask("msg/1-c2", "nxxxxx", 8 * H - 500),
             _ask("msg/1-c3", "nxxxxx", 8 * H - 1000)]
    msgs = {"msg/1-n1": _msg(TXXXX, to=("kxxx",)), "msg/1-n2": _msg(TXXXX, to=("kxxx", "pxx")),
            "msg/1-c1": _msg(PXX, to=("kxxx",)), "msg/1-c2": _msg(PXX, to=("kxxx", "pxx")),
            "msg/1-c3": _msg(PXX, to=("kxxx", "uxxx"))}
    result, reads = _run(_lists(*near, *far, *chain), msgs)
    got = _cls(result)
    assert got["msg/1-n1"] == got["msg/1-n2"] == "intentional_fanout"
    assert got["msg/1-x1"] == got["msg/1-x2"] == "live_work" and "msg/1-x1" not in reads, "601 s: not even a candidate"
    assert [got[f"msg/1-c{i}"] for i in (1, 2, 3)] == ["intentional_fanout"] * 3


def test_another_body_or_another_signer_is_no_group():
    asks = [_ask("msg/1-b1", "uxxx", H), _ask("msg/1-b2", "uxxx", H - 60), _ask("msg/1-s1", "txxxx", 3 * H),
            _ask("msg/1-s2", "txxxx", 3 * H - 60)]
    msgs = {"msg/1-b1": _msg(TXXXX, body="one", to=("kxxx",)), "msg/1-b2": _msg(TXXXX, body="two", to=("kxxx", "x")),
            "msg/1-s1": _msg(TXXXX, to=("kxxx",)), "msg/1-s2": _msg(PXX, to=("kxxx", "x"))}
    result, reads = _run(_lists(*asks), msgs)
    assert set(_cls(result).values()) == {"live_work"} and result["fanout_groups"] == []
    assert len(reads) == 4, "every candidate was read, and none grouped"


def test_edge_only_is_an_edge_that_did_not_come_from_to():
    """MUTANT: every src_field read as `to`."""
    asks = [_ask("msg/1-e1", "uxxx", H, src="cc:list"), _ask("msg/1-e2", "uxxx", 2 * H, src="to:string"),
            _ask("msg/1-e3", "uxxx", 3 * H, src="to"), _ask("msg/1-e4", "uxxx", 4 * H, src="tonight")]
    result, _ = _run(_lists(*asks))
    assert _cls(result) == {"msg/1-e1": "edge_only", "msg/1-e2": "live_work", "msg/1-e3": "live_work",
                            "msg/1-e4": "edge_only"}
    assert _row(result, "msg/1-e1")["evidence"] == {"src_field": "cc:list"}


def test_the_expiry_takes_open_asks_only_and_starts_after_the_boundary():
    """Exactly 72 h is still live; one second more is expired; an active ask is never expired (this seat posted in
    its thread since). MUTANTS: `>=` for `>`; the expiry applied to active asks."""
    asks = [_ask("msg/1-o1", "uxxx", 72 * H), _ask("msg/1-o2", "txxxx", 72 * H + 1),     # two authors: no fan-out read
            _ask("msg/1-o3", "nxxxxx", 200 * H, state="active")]
    result, _ = _run(_lists(*asks))
    assert _cls(result) == {"msg/1-o1": "live_work", "msg/1-o2": "expired_unreviewed", "msg/1-o3": "live_work"}
    assert _row(result, "msg/1-o2")["evidence"] == {"server_state": "open", "older_than_expiry": True}
    shorter, _ = _run(_lists(*asks), expire_s=24 * H)
    assert _cls(shorter)["msg/1-o1"] == "expired_unreviewed"


def test_a_done_when_naming_another_seat_is_annotated_never_reclassed():
    """Whole words only: codex-kxxx-cli is not kxxx, and this seat's own label is not noted. MUTANT: substring match."""
    asks = [_ask("msg/1-d1", "uxxx", H, done_when="after pxx's GO and a run by codex-kxxx-cli"),
            _ask("msg/1-d2", "uxxx", 2 * H, done_when="kxxx replies"),
            _ask("msg/1-d3", "uxxx", 3 * H, done_when=MARKER),
            _ask("msg/1-d4", "uxxx", 4 * H, done_when="the pxxn sign, by codex-pxx-cli")]
    result, _ = _run(_lists(*asks))
    assert set(_cls(result).values()) == {"live_work"}
    assert _row(result, "msg/1-d1")["done_when_names"] == ["codex-kxxx-cli", "pxx"]
    assert _row(result, "msg/1-d4")["done_when_names"] == ["codex-pxx-cli"], "pxxn and codex-pxx-cli are not pxx"
    assert "done_when_names" not in _row(result, "msg/1-d2") and "done_when_names" not in _row(result, "msg/1-d3")


def test_malformed_rows_are_unknown_and_never_dropped():
    bad_time = _ask("msg/1-u1", "uxxx", H)
    bad_time["hit"]["recorded_at"] = "yesterday"
    no_src = _ask("msg/1-u2", "uxxx", H)
    del no_src["hit"]["src_field"]
    wrong_state = _ask("msg/1-u3", "uxxx", H)
    wrong_state["state"] = "closed"
    lists = _lists(bad_time, no_src)
    lists["open"]["owed"].append(wrong_state)                 # the server listed it under open, with state closed
    lists["open"]["owed"].append({"hit": {"recorded_at": NOW.isoformat()}, "state": "open"})   # no key at all
    result, reads = _run(lists)
    got = _cls(result)
    assert got["msg/1-u1"] == got["msg/1-u2"] == got["msg/1-u3"] == got[None] == "unknown"
    assert "no recorded_at" in _row(result, "msg/1-u1")["evidence"]["why"]
    assert "no src_field" in _row(result, "msg/1-u2")["evidence"]["why"]
    assert "state 'closed' in the open list" in _row(result, "msg/1-u3")["evidence"]["why"]
    assert "no key" in _row(result, None)["evidence"]["why"]
    assert reads == [] and result["balanced"] and result["classes"]["unknown"] == 4


def test_a_fanout_read_that_fails_or_does_not_verify_groups_nothing_and_keeps_the_rows_class_flagged():
    """Only a verified body is hashed. A failed or unverified read leaves the row in the class the server's state
    gives it, flagged with the reason (29 of 60 such reads over kxxx's whole history were
    E_SIGNATURE_KEY_UNAVAILABLE). MUTANTS: an unverified body is hashed and grouped; the row is not flagged."""
    asks = [_ask("msg/1-u4", "txxxx", 2 * H), _ask("msg/1-u5", "txxxx", 2 * H - 60),
            _ask("msg/1-u6", "txxxx", 2 * H - 120), _ask("msg/1-u7", "pxx", 100 * H), _ask("msg/1-u8", "pxx", 100 * H - 60),
            _ask("msg/1-u9", "pxx", 100 * H - 120)]
    unverified = _msg(TXXXX, to=("kxxx", "pxx"))
    unverified["signature_verified"], unverified["signature_error"] = False, "E_SIGNATURE_KEY_UNAVAILABLE: fixture"
    msgs = {"msg/1-u4": OSError("connection reset"), "msg/1-u5": unverified, "msg/1-u6": _msg(TXXXX, to=("kxxx",)),
            "msg/1-u7": _msg(PXX, to=("kxxx",)), "msg/1-u8": _msg(PXX, verified=False, to=("kxxx", "uxxx")),
            "msg/1-u9": _msg(PXX, verified=False, to=("kxxx", "nxxxxx"))}
    result, reads = _run(_lists(*asks), msgs)
    assert _cls(result) == {"msg/1-u4": "live_work", "msg/1-u5": "live_work", "msg/1-u6": "live_work",
                            "msg/1-u7": "expired_unreviewed", "msg/1-u8": "expired_unreviewed",
                            "msg/1-u9": "expired_unreviewed"}
    assert result["fanout_groups"] == [] and len(reads) == 6, "u8 and u9 share a body but neither verified"
    assert _row(result, "msg/1-u4")["fanout_unchecked"] == "its read failed: OSError: connection reset"
    assert _row(result, "msg/1-u5")["fanout_unchecked"] == ("its read did not verify: E_SIGNATURE_KEY_UNAVAILABLE: "
                                                            "fixture")
    assert "fanout_unchecked" not in _row(result, "msg/1-u6")
    assert result["fanout_unchecked"] == {"cap": 0, "unread": 0, "failed": 1, "unverified": 3, "recipients": 0}
    assert result["balanced"]
    text = reconcile.render(result, since=None, thread=None, answered_list="x")
    assert ("fan-out checks that could not run: 4 (read failed 1, unverified 3, recipients not resolved 0)" in text)


def test_the_cap_reads_receipts_first_then_candidates_oldest_first_and_names_what_it_left():
    """MUTANTS: receipts read without counting; candidates read newest first; an unread candidate shown as unknown."""
    asks = [_ask(f"msg/1-c{i}", "uxxx", 10 * H - i * 60) for i in range(1, 5)]
    msgs = {f"msg/1-c{i}": _msg(TXXXX, to=("kxxx", f"x{i}")) for i in range(1, 5)}
    msgs["msg/1794090000000-rm"] = _msg(OXXXXXx, closes=["msg/0-elsewhere"])
    result, reads = _run(_lists(*asks), msgs, closures=["msg/1794090000000-rm"], max_reads=3)
    assert reads == ["msg/1794090000000-rm", "msg/1-c1", "msg/1-c2"] and result["reads"] == 3
    nr = result["not_read"]
    assert nr["fanout_candidates"] == 2 and nr["oldest"] == "msg/1-c3" and nr["newest"] == "msg/1-c4"
    assert _cls(result)["msg/1-c3"] == "live_work"
    assert _row(result, "msg/1-c3")["fanout_unchecked"] == "not read: the 3-read cap"
    assert _cls(result)["msg/1-c1"] == "intentional_fanout"
    assert result["fanout_unchecked"] == {"cap": 2, "unread": 0, "failed": 0, "unverified": 0, "recipients": 0}
    none, reads = _run(_lists(*asks), msgs, closures=["msg/1794090000000-rm"], max_reads=0)
    assert reads == [] and none["receipts"] == [{"key": "msg/1794090000000-rm", "status": "not_read", "detail": "the 0-read cap"}]
    assert none["not_read"]["receipts"] == ["msg/1794090000000-rm"] and none["balanced"]


def test_no_body_or_done_when_text_reaches_the_output():
    """Bodies are hashed and never printed (pxx D); done_when is reduced to the seat labels it names."""
    asks = [_ask("msg/1-p1", "txxxx", H, done_when=MARKER), _ask("msg/1-p2", "txxxx", H - 60, done_when=MARKER)]
    msgs = {"msg/1-p1": _msg(TXXXX, to=("kxxx",)), "msg/1-p2": _msg(TXXXX, to=("kxxx", "pxx"))}
    result, _ = _run(_lists(*asks), msgs)
    text = reconcile.render(result, since=None, thread=None, answered_list="owed --state answered")
    assert MARKER not in json.dumps(result) and MARKER not in text
    assert "body" not in json.dumps(result["rows"])


def test_a_missing_field_in_the_servers_answer_raises_and_is_never_read_as_empty():
    lists = _lists(_ask("msg/1-k1", "uxxx", H))
    del lists["active"]["owed"]
    with pytest.raises(reconcile.Malformed, match="active list has no `owed` array"):
        _run(lists)
    lists = _lists(_ask("msg/1-k1", "uxxx", H))
    del lists["answered"]["truncated"]
    with pytest.raises(reconcile.Malformed, match="answered list does not say whether it was cut"):
        _run(lists)


def test_a_cut_list_is_said_and_a_key_listed_twice_stays_in_the_equation():
    """pxx d95791 P2-3: the states count every row fetched, and the duplicate is a term of the equation, so a
    balanced line never hides a changed count. MUTANT: the states counted after the duplicate was dropped."""
    ask = _ask("msg/1-k1", "uxxx", H)
    lists = _lists(ask, cut=("open",))
    lists["open"]["owed"].append(ask)
    lists["answered"]["owed"].append({**ask, "state": "answered"})            # in two snapshots
    result, _ = _run(lists)
    assert result["truncated"] == {"open": True, "active": False, "answered": False}
    assert "cut the open list" in result["cut"] and result["duplicate_keys"] == ["msg/1-k1", "msg/1-k1"]
    assert result["states"] == {"open": 2, "active": 0, "answered": 1} and result["balanced"]
    assert result["equation"] == ("open 2 + active 0 + answered 1 = live_work 1 + edge_only 0 + expired_unreviewed 0 "
                                  "+ intentional_fanout 0 + retransmission 0 + answered 0 + closed 0 + unknown 0 "
                                  "+ duplicate 2")
    text = reconcile.render(result, since=None, thread=None, answered_list="x")
    assert "cut: the server cut the open list" in text
    assert "listed twice, counted once in the classes and once as duplicate: msg/1-k1, msg/1-k1" in text


def test_the_text_view_balances_first_then_names_every_class_receipt_group_and_gap():
    asks = [_ask("msg/1-a1", "uxxx", H, state="answered"), _ask("msg/1-l1", "uxxx", 2 * H, done_when="after pxx"),
            _ask("msg/1-f1", "txxxx", 3 * H), _ask("msg/1-f2", "txxxx", 3 * H - 60), _ask("msg/1-k1", "uxxx", 4 * H)]
    msgs = {"msg/1-f1": _msg(TXXXX, to=("kxxx",)), "msg/1-f2": _msg(TXXXX, to=("kxxx", "pxx")),
            "msg/1794090000000-rm": _msg(OXXXXXx, closes=["msg/1-k1"])}
    result, _ = _run(_lists(*asks), msgs, closures=["msg/1794090000000-rm"])
    text = reconcile.render(result, since="2026-10-25T00:00:00Z", thread=None,
                            answered_list="owed --state answered --since 2026-10-25T00:00:00Z --limit 2000")
    lines = text.splitlines()
    assert lines[0] == ("owed --reconcile: open 4 + active 0 + answered 1 = live_work 1 + edge_only 0 + "
                        "expired_unreviewed 0 + intentional_fanout 2 + retransmission 0 + answered 1 + closed 1 + "
                        "unknown 0 + duplicate 0")
    assert lines[1] == "window: since 2026-10-25T00:00:00Z; expiry 72h; 3 of 60 signed reads"
    assert "live_work 1, oldest first:" in lines and any("done_when names pxx" in ln for ln in lines)
    assert any(ln.startswith("answered 1: replied to directly, not settled: `owed --state answered") for ln in lines)
    assert any("closed by msg/1794090000000-rm" in ln for ln in lines)
    assert "receipt msg/1794090000000-rm: ok, closes 1 listed" in lines
    assert any(ln.startswith("fanout group msg/1-f1: 2 asks from txxxx, 2 recipient sets") for ln in lines)


# --- the command ---------------------------------------------------------------------------------------------------

def _argv(tmp_path, *extra):
    return ["owed", "--base-url", "http://127.0.0.1:9", "--state-file", str(tmp_path / "state.json"), *extra]


@pytest.fixture
def server(monkeypatch):
    """The owed route and the signed read as stand-ins; sending is refused outright, so the command cannot ack."""
    calls = {"owed": [], "read": [], "unread": [], "recipients": []}
    monkeypatch.setattr(cli.node, "load_state", lambda path, base_url: {"base_url": base_url, "peer_id": ME,
                                                                        "label": "kxxx"})
    # cli.main runs on the real clock, so the ask is an hour old by THAT clock, never by the fixed NOW (a date bomb).
    asks = {"open": [_ask("msg/1-k1", "uxxx", H, now=datetime.now(timezone.utc))], "active": [], "answered": []}

    def owed(path, state, *, limit, states=None, since=None, thread=None):
        calls["owed"].append({"limit": limit, "states": states, "since": since, "thread": thread})
        return {"owed": asks[states], "truncated": False, "filters": {"state": [states]}}

    def read(path, state, *, key, include_value=False, record=True, **kw):
        calls["read"].append({"key": key, "include_value": include_value, "record": record, **kw})
        return _msg(OXXXXXx, closes=["msg/1-k1"])

    def unread(path, state, *, days=None, since=None, limit=None):
        calls["unread"].append({"days": days, "since": since, "limit": limit})
        calls.setdefault("order", []).append("unread")
        return _unread()

    def recipients(path, state, labels):
        calls["recipients"].append(list(labels))
        return _recipients(labels)

    def neighbours(path, state, *, key, include_inferred, limit):
        """v1 on the first call for a key, v2 on any later one: as if the receipt's author committed v2 meanwhile."""
        seen = sum(1 for c in calls.get("anchor", []) if c["key"] == key)
        calls.setdefault("anchor", []).append({"key": key, "include_inferred": include_inferred, "limit": limit})
        calls.setdefault("order", []).append("anchor")
        at = datetime.now(timezone.utc) - timedelta(minutes=5)
        return {"anchor": {"key": key, "version": 1 + seen, "recorded_at": at.isoformat()}}

    def refuse(*a, **k):
        raise AssertionError("owed --reconcile must never send")
    monkeypatch.setattr(node, "owed", owed)
    monkeypatch.setattr(node, "read", read)
    monkeypatch.setattr(node, "unread", unread)
    monkeypatch.setattr(node, "recipients", recipients)
    monkeypatch.setattr(node, "neighbours", neighbours)
    monkeypatch.setattr(node, "send", refuse)
    return calls


def test_the_command_asks_each_list_once_in_the_same_window_and_reads_without_recording(tmp_path, server, capsys):
    """One call per state, so answered volume cannot push an open ask past the limit; every read is include_value and
    record=False, so nothing is marked read or fetched and no directive is used up. MUTANTS: one call for all three
    states; the read records."""
    receipt = f"msg/{int(time.time() * 1000) - 600_000}-oxxxxxx-rm"            # ten minutes ago: inside the gate
    assert cli.main(_argv(tmp_path, "--reconcile", "--thread", "t1", "--since", "2h",
                          "--closures", receipt, "--json")) == 0
    shown = json.loads(capsys.readouterr().out)
    since = server["owed"][0]["since"]
    assert [c["states"] for c in server["owed"]] == ["open", "active", "answered"]
    assert all(c == {"limit": reconcile.LIST_LIMIT, "states": c["states"], "since": since, "thread": "t1"}
               for c in server["owed"])
    # pxx fd239c: the receipt is placed by the server's anchor (no memory.read) and read pinned to its version.
    assert server["anchor"] == [{"key": receipt, "include_inferred": False, "limit": 1}]
    assert server["read"] == [{"key": receipt, "include_value": True, "record": False, "version": 1}]
    assert shown["classes"]["closed"] == 1 and shown["since"] == since and shown["thread"] == "t1"
    assert shown["answered_list"].startswith(f"owed --state answered --since {since}")
    # pxx d95791 P1: the unread list over the same window, before any read. MUTANT: no unread call (a stub unread).
    assert server["unread"] == [{"days": None, "since": since, "limit": reconcile.UNREAD_LIMIT}]
    # A wider window is capped at UNREAD_GATE_S (an 89-day one timed out on production). MUTANT: the gate not capped.
    for wide in ("all", "2026-10-01T00:00:00Z"):
        assert cli.main(_argv(tmp_path, "--reconcile", "--since", wide)) == 0
        capsys.readouterr()
        gate = datetime.fromisoformat(server["unread"][-1]["since"].replace("Z", "+00:00"))
        expect = datetime.now(timezone.utc) - timedelta(seconds=reconcile.UNREAD_GATE_S)
        assert abs((gate - expect).total_seconds()) < 120, wide


def test_a_receipts_anchor_is_taken_before_the_unread_list_so_a_later_version_is_never_read(tmp_path, server, capsys):
    """pxx a85e6d: the anchor was asked inside reconcile, AFTER the unread GET, so a receipt version committed between
    the two was pinned and read while unread. Anchors now come first. Here the first anchor says v1 and any later one
    v2 (committed meanwhile): the read is at v1, and the anchor is asked once, before the unread list. MUTANT: the
    anchor asked lazily, after the unread GET; the read is at v2."""
    receipt = f"msg/{int(time.time() * 1000) - 600_000}-oxxxxxx-rm"
    assert cli.main(_argv(tmp_path, "--reconcile", "--closures", receipt, "--json")) == 0
    capsys.readouterr()
    assert server["order"] == ["anchor", "unread"]
    assert [r["version"] for r in server["read"]] == [1]


def test_the_signers_are_oxxxxxxs_pinned_peer_and_this_seat(tmp_path, server, monkeypatch, capsys):
    seen = {}
    real = reconcile.reconcile

    def spy(lists, read, **kw):
        seen.update(kw)
        return real(lists, read, **kw)
    monkeypatch.setattr(reconcile, "reconcile", spy)
    assert cli.main(_argv(tmp_path, "--reconcile")) == 0
    assert seen["signers"] == {OXXXXXx: "oxxxxxx", ME: "kxxx"} and seen["me_label"] == "kxxx"
    assert node.SEAT_DIRECTIVE_PEERS[OXXXXXx] == "oxxxxxx" == node.OPERATOR_VOICES[OXXXXXx]
    assert seen["expire_s"] == 72 * H and seen["max_reads"] == 60
    assert capsys.readouterr().out.startswith("owed --reconcile: open 1 + active 0 + answered 0 = live_work 1")


def test_options_that_go_with_reconcile_are_refused_without_it_and_its_own_conflicts_refused(tmp_path, server):
    for extra in (["--closures", "msg/1794090000000-rm"], ["--expire", "72h"], ["--max-reads", "5"], ["--json"],
                  ["--reconcile", "--state", "open"], ["--reconcile", "--counts-only"],
                  ["--reconcile", "--expire", "0h"], ["--reconcile", "--expire", "3x"],
                  ["--reconcile", "--max-reads", "-1"]):
        assert cli.main(_argv(tmp_path, *extra)) == 2, extra
    assert server["owed"] == [] and server["read"] == []


def test_the_expiry_reads_hours_and_days_and_the_defaults_match_the_module():
    assert cli._owed_expire_s("72h") == cli._owed_expire_s("3d") == 72 * H
    assert cli.OWED_RECONCILE_EXPIRE == reconcile.DEFAULT_EXPIRE
    assert cli.OWED_RECONCILE_MAX_READS == reconcile.DEFAULT_MAX_READS


def test_counts_that_do_not_balance_exit_1_and_say_so(tmp_path, server, monkeypatch, capsys):
    """The check blocks rather than warns. MUTANT: `balanced` always true."""
    monkeypatch.setattr(reconcile, "_base", lambda row, expire_s: "bogus")
    assert cli.main(_argv(tmp_path, "--reconcile")) == 1
    assert "NOT BALANCED" in capsys.readouterr().out.splitlines()[0]


def test_a_malformed_answer_or_an_unechoed_filter_exits_1(tmp_path, server, monkeypatch, capsys):
    monkeypatch.setattr(node, "owed", lambda path, state, **kw: {"truncated": False, "filters": {}})
    assert cli.main(_argv(tmp_path, "--reconcile")) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "owed_answer_malformed"

    def unechoed(path, state, **kw):
        raise node.FiltersNotApplied({"error": node.E_FILTERS_NOT_APPLIED})
    monkeypatch.setattr(node, "owed", unechoed)
    assert cli.main(_argv(tmp_path, "--reconcile")) == 1
    assert json.loads(capsys.readouterr().out)["error"] == node.E_FILTERS_NOT_APPLIED


# --- node.read(record=False) ---------------------------------------------------------------------------------------

@pytest.fixture
def signed_read(monkeypatch):
    """node.read against a fake server whose message carries a directive; the two checks pass."""
    calls = {"directive": 0}
    monkeypatch.setattr(node, "require_peer", lambda *a, **k: None)
    memo = {"namespace": wire.MESH_NAMESPACE, "key": "msg/1-d", "version": 1, "is_head": True,
            "created_at": "2026-11-07 22:00:00+00:00", "author_label": "oxxxxxx", "created_by_peer_id": OXXXXXx,
            "value": {"body": MARKER, "from": "oxxxxxx", "to": ["kxxx"], "thread_id": "t1",
                      "directive": {"act": "x"}}}
    monkeypatch.setattr(node, "invoke", lambda path, state, *, skill, args, **kw: {
        "status": "completed", "result_signature_verified": True, "call_id": "c1", "result": {"memo": memo}})
    monkeypatch.setattr(wire, "author_signature_ok", lambda m, keys: (True, None))
    monkeypatch.setattr(wire, "status_check", lambda m, verified, why: {"status_verified": True, "author_signed": True})

    def directive(*a, **k):
        calls["directive"] += 1
        return {"seat_directive_refused": {"reason": "fixture"}}
    monkeypatch.setattr(node, "seat_directive_of", directive)
    return calls


def test_a_read_that_only_counts_records_nothing_and_evaluates_no_directive(tmp_path, signed_read):
    """CONTROL first: the default read records the read, notes the fetch (the sidecar exists, so it is opted in) and
    evaluates the directive. Then record=False does none of the three, and cannot claim. MUTANTS: record=False still
    notes the read; still evaluates the directive."""
    state = {"base_url": "http://fixture", "peer_id": ME, "status": "bound"}
    counted = tmp_path / "counted-state.json"
    Path(f"{counted}{node.FETCHED_SUFFIX}").write_text("", encoding="utf-8")
    message = node.read(counted, state, key="msg/1-d", record=False)
    assert message["signature_verified"] is True and message["signed_by"] == OXXXXXx
    assert signed_read["directive"] == 0
    assert not Path(f"{counted}{node.READ_LEDGER_SUFFIX}").exists()
    assert Path(f"{counted}{node.FETCHED_SUFFIX}").read_text(encoding="utf-8") == ""
    recorded = tmp_path / "recorded-state.json"
    Path(f"{recorded}{node.FETCHED_SUFFIX}").write_text("", encoding="utf-8")
    node.read(recorded, state, key="msg/1-d")
    assert signed_read["directive"] == 1
    assert Path(f"{recorded}{node.READ_LEDGER_SUFFIX}").exists()
    assert "msg/1-d" in Path(f"{recorded}{node.FETCHED_SUFFIX}").read_text(encoding="utf-8")
    with pytest.raises(node.Refused, match="cannot claim or resume a directive"):
        node.read(counted, state, key="msg/1-d", record=False, directive_claim=True)
