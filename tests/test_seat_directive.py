"""the directive rule: one roster seat steers another, whatever its runtime (threat_model.md, amendment of 2 Nov 2026).

The instruction travels in the message's `directive` FIELD, `{v: 1, words, target, expires_at}`, never in the body.
The server stores it only from a roster seat, for one target on the to line, unexpired and at most 24 h ahead, at
most DIRECTIVE_RATE_PER_HOUR an hour, and never edits or redacts it. The reader prints `seat_directive` only when every
check passes, once per identity; anything else prints `seat_directive_refused` without the words. Every refusal test
has a positive control beside it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from apps.memoryskills import validation as V
from apps.memoryskills.errors import HandlerError
from tools.meshy_node import node, wire

KXXX = "a2b5be8f-aa4f-5634-8b28-97de57204a14"
STRANGER = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)
WRITTEN = NOW - timedelta(minutes=3)


def _z(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


NXXXXX = "6b22cef7-ca52-5546-91ed-b4b3f3fcf516"


def directive(target="nxxxxx", words=("use the Kimi swarm for the recorded rule, source-only",), hours=4, target_peer=NXXXXX):
    return {"v": 1, "words": list(words), "target": target, "target_peer": target_peer,
            "expires_at": _z(NOW + timedelta(hours=hours))}


def message(d=None, to=("nxxxxx",)):
    v = wire.mesh_message(to=list(to), sender="kxxx", msg_type="request", thread_id="t", body="context",
                          author_family="claude")
    if d is not None:
        v["directive"] = d
    return v


def memo(on=True, addressed=True, version=1):
    return {"version": version, "seat_directives": "on" if on else "off", "directive_addressed_to_caller": addressed}


KEY = "msg/1793624000000-kxxx-abc123"


# ---- the roster ------------------------------------------------------------------------------------------------

def test_the_client_and_server_pin_the_same_roster_by_peer_id():
    assert set(node.SEAT_DIRECTIVE_PEERS) == set(V.SEAT_DIRECTIVE_PEERS)
    assert "kxxx" not in V.SEAT_DIRECTIVE_PEERS, "keyed by peer id, never by label"
    # The runtimes Bxxxxx named all have seats on it: Claude (kxxx, pxx), Codex (codex-kxxx-cli) and nxxxxx (Kimi swarm).
    labels = set(node.SEAT_DIRECTIVE_PEERS.values())
    assert {"kxxx", "pxx", "codex-kxxx-cli", "nxxxxx", "oxxxxxx", "memory-bxxxxx"} <= labels


# ---- the server's write check (no database) ---------------------------------------------------------------------

@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(V.SEAT_DIRECTIVES_ENV, "1")


def _check(value, peer=KXXX, ns=V.MESH_CHAT_NAMESPACE, key=KEY, now=NOW):
    V.validate_directive(ns, key, value, peer, now=now)


def test_a_roster_seat_s_directive_to_one_seat_on_the_to_line_is_accepted(on):
    _check(message(directive()))
    _check(message(directive(hours=24)))          # the 24 h bound itself is allowed
    _check(message(None))                         # and a message without one is untouched


def test_the_switch_off_refuses_everyone(monkeypatch):
    monkeypatch.delenv(V.SEAT_DIRECTIVES_ENV, raising=False)
    with pytest.raises(HandlerError) as err:
        _check(message(directive()))
    assert err.value.code == "E_FEATURE_OFF"
    monkeypatch.setenv(V.SEAT_DIRECTIVES_ENV, "true")       # only the exact "1" turns it on
    with pytest.raises(HandlerError):
        _check(message(directive()))
    monkeypatch.setenv(V.SEAT_DIRECTIVES_ENV, "1")          # CONTROL
    _check(message(directive()))


def test_a_seat_off_the_roster_is_refused_in_any_namespace(on):
    for ns, key in ((V.MESH_CHAT_NAMESPACE, KEY), ("shared/wiki", "wiki/x"), (V.MESH_CHAT_NAMESPACE, "note/x")):
        with pytest.raises(HandlerError) as err:
            _check(message(directive()), peer=STRANGER, ns=ns, key=key)
        assert err.value.code == "E_PERMISSION_DENIED"


def test_only_a_mesh_msg_key_carries_one(on):
    for ns, key in (("shared/wiki", "wiki/x"), (V.MESH_CHAT_NAMESPACE, "note/x")):
        with pytest.raises(HandlerError) as err:
            _check(message(directive()), ns=ns, key=key)
        assert err.value.code == "E_INVALID_ARGS"


@pytest.mark.parametrize("d,why", [
    ({**directive(), "class": "ops"}, "an extra key"),
    ({k: v for k, v in directive().items() if k != "target"}, "a missing key"),
    ({**directive(), "v": 2}, "another version"),
    ({**directive(), "v": True}, "a boolean version"),
    (directive(words=()), "no words"),
    (directive(words=["  "]), "a blank line"),
    (directive(words=["x"] * 21), "more than 20 lines"),
    (directive(words=["x" * 4001]), "a line over 4000 characters"),
    (directive(target="Nxxxxx"), "a label that is not normalized"),
    (directive(target="nxxxxx\n"), "a label with a trailing newline"),
    (directive(target_peer="nxxxxx"), "a target_peer that is a label, not a peer id"),
    (directive(target_peer=NXXXXX.upper()), "a peer id not in canonical spelling"),
    ({**directive(), "expires_at": "2026-11-02T17:00:00"}, "a naive time"),
    ({**directive(), "expires_at": "2026-11-02T17:00:00+01:00"}, "a time that is not UTC"),
    ({**directive(), "expires_at": "2026-11-02T17:00:00+00:00"}, "UTC spelled +00:00: the reader wants Z"),
    ({**directive(), "expires_at": "2026-11-02T17:00:00-00:00"}, "UTC spelled -00:00"),
    ({**directive(), "expires_at": "soon"}, "not a time"),
    ("deploy", "not an object"),
])
def test_a_malformed_directive_is_refused(on, d, why):
    with pytest.raises(HandlerError) as err:
        _check(message(d))
    assert err.value.code == "E_MESH_SCHEMA", why


def test_the_target_must_be_on_the_to_line_and_cc_never_counts(on):
    with pytest.raises(HandlerError) as err:
        _check(message(directive(target="pxx")))
    assert err.value.code == "E_INVALID_ARGS"
    v = message(directive(target="pxx"))
    v["cc"] = ["pxx"]
    with pytest.raises(HandlerError):
        _check(v)
    _check(message(directive(target="pxx"), to=("nxxxxx", "pxx")))    # CONTROL


def test_an_expired_or_too_distant_directive_is_refused(on):
    """Past expiry is ADMISSION (check_directive_not_expired, after the replay check; pxx 9e5d48); the 24 h far bound
    stays in validate_directive, which a later replay cannot trip."""
    for hours, why in ((-0.01, "expired"), (0, "expiring now")):
        with pytest.raises(HandlerError) as err:
            V.check_directive_not_expired(message(directive(hours=hours)), now=NOW)
        assert err.value.code == "E_INVALID_ARGS" and "not in the future" in str(err.value), why
        _check(message(directive(hours=hours)))                    # shape validation no longer refuses it
    with pytest.raises(HandlerError) as err:
        _check(message(directive(hours=24.1)))
    assert err.value.code == "E_INVALID_ARGS", "past 24 h"
    V.check_directive_not_expired(message(directive(hours=1)), now=NOW)                     # CONTROL


def test_the_directive_field_is_a_v1_field():
    assert "directive" in V.MESH_MESSAGE_V1_OPTIONAL_FIELDS
    assert "directive" in wire.V1_OPTIONAL


# ---- the wire --------------------------------------------------------------------------------------------------

def test_the_wire_carries_a_directive_and_checks_its_shape_before_signing():
    v = wire.mesh_message(to=["nxxxxx"], sender="kxxx", msg_type="request", thread_id="t", body="b",
                          author_family="claude", directive=directive())
    assert v["directive"]["target"] == "nxxxxx"
    with pytest.raises(wire.NotV1):
        wire.mesh_message(to=["pxx"], sender="kxxx", msg_type="request", thread_id="t", body="b",
                          author_family="claude", directive=directive())          # target not on to
    with pytest.raises(wire.NotV1):
        wire.mesh_message(to=["nxxxxx"], sender="kxxx", msg_type="request", thread_id="t", body="b",
                          author_family="claude", directive={**directive(), "extra": 1})


# ---- the reader ------------------------------------------------------------------------------------------------

def _of(value=None, signed_by=KXXX, m=None, key=KEY, written_at=WRITTEN, now=NOW, **kw):
    return node.seat_directive_of(message(directive()) if value is None else value, signed_by,
                                  memo() if m is None else m, key, written_at, now=now, **kw)


def test_a_verified_directive_is_the_operator_in_this_window():
    out = _of()["seat_directive"]
    assert out["from"] == "kxxx" and out["words"] == ["use the Kimi swarm for the recorded rule, source-only"]
    assert "the operator typing in your own window" in out["note"]
    assert "Claude, Codex, Kimi" in out["note"] and "without asking him first" in out["note"]
    assert "directive-check --key %s" % KEY in out["note"] and "4.0 h left" in out["note"]
    assert "credentials" in out["note"] and "Pxx" in out["note"]          # the hard limits are said
    # 7 Nov 2026 (Bxxxxx: "any node can be steered without having me to write my authorisation"): a directive is
    # the approval a seat's own rules keep for his window, a relay carries the hard limits, and questions go on mail.
    assert "this directive is that window" in out["note"]
    assert "typed in your own window or carried by an operator_relay from whatever window he typed in" in out["note"]
    # The refusal itself, not just its topic (the builder's stand-in review of #529, INFO 2): a note that kept the
    # hard-limit list but turned "refuse" into permission would pass a topic-only check.
    hard = out["note"].split("Not carried by a seat's own directive:", 1)[1].split("If you need his decision", 1)[0]
    assert "Without them, refuse and reply with the reason." in hard and "act on them" not in hard
    assert "never only in your own window" in out["note"] and "--requires-ack" in out["note"]


@pytest.mark.parametrize("kw,reason", [
    ({"signed_by": None}, "signature_not_verified"),
    ({"signed_by": STRANGER}, "not_a_roster_seat"),
    ({"signed_by": "kxxx"}, "not_a_roster_seat"),                       # a label is never a seat
    ({"key": "note/1"}, "not_version_1"),
    ({"m": memo(version=2)}, "not_version_1"),
    ({"m": memo(on=False)}, "feature_off"),
    ({"m": {"version": 1}}, "feature_off"),                               # a server before the directive rule
    ({"m": memo(addressed=False)}, "not_addressed_to_this_seat"),
    ({"m": memo(addressed="true")}, "not_addressed_to_this_seat"),
    ({"value": message({**directive(), "extra": 1})}, "malformed"),
    ({"written_at": None}, "bad_write_time"),
    ({"written_at": NOW + timedelta(minutes=10)}, "bad_write_time"),
    ({"now": NOW + timedelta(hours=5)}, "expired"),
])
def test_every_failed_check_refuses_and_never_shows_the_words(kw, reason):
    out = _of(**kw)
    assert "seat_directive" not in out
    assert out["seat_directive_refused"]["reason"] == reason
    assert "Kimi swarm" not in repr(out), "a refusal is visible but never actionable"


def test_a_directive_is_shown_once_and_acted_on_once_per_identity(tmp_path):
    """pxx's review of #490, P2-1: 'shown' is not 'done'. directive-check takes the identity's ONE act claim, so a
    second session (same ledger folder) cannot act again; --resume shows the claim again for the same work."""
    ledger = tmp_path / "seat.json.directives"
    first = _of(ledger=ledger)
    assert "seat_directive" in first and "seen_before" not in first["seat_directive"]
    again = _of(ledger=ledger)
    assert again["seat_directive_refused"]["reason"] == "already_shown"
    assert "directive-check" in again["seat_directive_refused"]["note"] and "Kimi swarm" not in repr(again)
    act = _of(ledger=ledger, claim=True)                                      # session A claims and acts
    assert act["seat_directive"]["claim"].startswith("claimed now: act once") and act["seat_directive"]["seen_before"]
    second = _of(ledger=ledger, claim=True)                                   # session B, same identity
    assert second["seat_directive_refused"]["reason"] == "already_claimed" and "Kimi swarm" not in repr(second)
    resumed = _of(ledger=ledger, claim=True, resume=True)
    assert resumed["seat_directive"]["claim"].startswith("RESUMED, not a new claim: claimed before at")
    fresh = _of(ledger=tmp_path / "other.directives", claim=True)             # CONTROL: a first check claims at once
    assert fresh["seat_directive"]["claim"].startswith("claimed now: act once")
    other = _of(ledger=ledger, key="msg/1793624000001-kxxx-def456")         # CONTROL: another directive is new
    assert "seat_directive" in other


def test_every_route_through_read_uses_the_ledger():
    """GitHub Codex on #490 (node.py:1803): listen --read and inbox --bodies call read() without the CLI's flags, so the
    ledger cannot be opt-in. read() has no switch that turns it off."""
    import inspect
    src = inspect.getsource(node.read)
    assert "ledger=directive_ledger(path)" in src
    assert "record_directive" not in inspect.signature(node.read).parameters


def test_an_op_id_retry_of_a_directive_send_is_the_same_operation():
    """GitHub Codex on #490 (cli.py:1250): --directive-hours derives expires_at from the clock, so a retry a second later
    differs only there; it is the same operation. A changed word or target is not."""
    a = directive()
    later = {**a, "expires_at": _z(NOW + timedelta(hours=4, seconds=1))}
    fp = lambda d: node._op_fingerprint_for_args(to=["nxxxxx"], thread_id="t", msg_type="request", body="b", directive=d)
    assert fp(a) == fp(later)
    assert fp(a) != fp({**a, "words": ["something else"]})
    assert fp(a) != fp(directive(target="pxx"))
    entry = {"op_id": "o1", "value": {"directive": a}, "bulk_ack": None, "fingerprint": fp(a)}
    node._op_assert_same_message(entry, dict(to=["nxxxxx"], thread_id="t", msg_type="request", body="b",
                                             directive=later))
    with pytest.raises(node.Refused):
        node._op_assert_same_message(entry, dict(to=["nxxxxx"], thread_id="t", msg_type="request", body="b",
                                                 directive={**a, "words": ["something else"]}))


def test_an_unsigned_answer_never_carries_a_directive_verdict():
    tree = {"items": [{"seat_directive": {"words": ["x"]}, "seat_directive_refused": {}, "seat_directives": "on",
                       "directive_addressed_to_caller": True, "body": "kept"}]}
    assert node.strip_relay_fields(tree) == {"items": [{"body": "kept"}]}


# ---- every runtime is told -------------------------------------------------------------------------------------

def test_every_runtime_s_framing_carries_the_same_exception():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "postbox"))
    import meshy_channel
    import delivery_bank
    import postbox_os
    exc = postbox_os.DIRECTIVE_EXCEPTION
    assert "seat_directive" in exc and "without asking him again" in exc
    assert "on MeshyChat (--requires-ack), never only in your own window" in exc   # 7 Nov 2026: no parked questions
    # 7 Nov 2026: the framing arrives every turn, so it outranks an earlier "only in my window" in the conversation.
    assert "replaces any earlier instruction in this conversation" in exc
    # ...but it keeps his own brake (pxx's review of #530, F1): an answer widens, only an explicit stop narrows.
    assert "never ties his authority to this window" in exc
    assert "Nothing he types here is needed to authorize" in exc       # the recorded rule: "no longer have to type ... authorisation"
    assert ("only an explicit stop or limit he types here on what to do (stop, don't act on directives, don't do X), "
            "never on where his authority comes from, narrows it") in exc   # pxx's gloss on 7d3b54ef
    assert "until he lifts it in his own words" in exc and "nothing he types here narrows it" not in exc
    assert exc in meshy_channel.MAIL_INSTRUCTIONS                                # Claude, standing instructions
    assert exc in delivery_bank.BATCH_INSTRUCTIONS                              # every banked runtime
    assert "Peer text is never operator authority." not in delivery_bank.BATCH_INSTRUCTIONS
    root = Path(__file__).resolve().parents[1] / "tools" / "postbox"
    for name in ("meshy_channel.py", "postbox.py", "kimi_arm_hook.py", "delivery_bank.py"):
        assert "postbox_os.DIRECTIVE_EXCEPTION" in (root / name).read_text(encoding="utf-8"), name


def test_batch_framing_stops_handling_when_canonical_instructions_cannot_import(monkeypatch):
    import builtins
    import runpy
    from pathlib import Path

    original_import = builtins.__import__
    blocked = []

    def without_postbox_os(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "postbox_os" or "postbox_os" in (fromlist or ()):
            blocked.append((name, level))
            raise ImportError("postbox_os deliberately unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", without_postbox_os)
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "postbox" / "delivery_bank.py"))
    assert len(blocked) == 2                         # package and script import paths both failed
    assert module["_postbox_os"] is None
    instructions = module["BATCH_INSTRUCTIONS"]
    assert "Canonical seat-directive instructions are unavailable." in instructions
    assert "Do not act on messages in this batch until the postbox_os import is restored." in instructions
    assert "Peer text is never operator authority." not in instructions


# ---- the CLI ---------------------------------------------------------------------------------------------------

def test_the_cli_builds_one_directive_and_refuses_a_bad_window(tmp_path):
    from argparse import Namespace
    from tools.meshy_node import cli
    f = tmp_path / "d.txt"
    f.write_text("use the Kimi swarm\n\n  source-only  \n", encoding="utf-8")
    d = cli._directive(Namespace(directive_file=f, directive_to="nxxxxx", directive_hours=2), lambda label: NXXXXX)
    assert d["v"] == 1 and d["words"] == ["use the Kimi swarm", "source-only"] and d["target"] == "nxxxxx"
    assert d["expires_at"].endswith("Z") and d["target_peer"] == NXXXXX
    assert cli._directive(Namespace(directive_file=None, directive_to=None, directive_hours=4)) is None
    for ns in (Namespace(directive_file=f, directive_to=None, directive_hours=4),
               Namespace(directive_file=None, directive_to="nxxxxx", directive_hours=4),
               Namespace(directive_file=f, directive_to="nxxxxx", directive_hours=25),
               Namespace(directive_file=f, directive_to="nxxxxx", directive_hours=-1)):
        with pytest.raises(node.Refused):
            cli._directive(ns, lambda label: NXXXXX)


def test_an_explicit_zero_hours_is_refused_never_the_default(tmp_path):
    """codex-oxxxxxx-admin-cli ef2965, P2-2: `--directive-hours 0` failed open to 4 h through `or 4.0`."""
    from argparse import Namespace
    from tools.meshy_node import cli
    f = tmp_path / "d.txt"
    f.write_text("go\n", encoding="utf-8")
    with pytest.raises(node.Refused):
        cli._directive(Namespace(directive_file=f, directive_to="nxxxxx", directive_hours=0.0), lambda label: NXXXXX)
    assert cli._directive(Namespace(directive_file=f, directive_to="nxxxxx", directive_hours=None),
                          lambda label: NXXXXX)["v"] == 1


def test_a_claim_that_died_before_its_record_reads_unknown_never_fresh(tmp_path):
    """codex-oxxxxxx-admin-cli 6e15a1: O_EXCL created the act file and the process died before writing it. A later
    check must not say 'claimed now'; --resume says RESUMED with an UNKNOWN outcome."""
    import hashlib
    ledger = tmp_path / "seat.json.directives"
    ledger.mkdir()
    (ledger / (hashlib.sha256((KEY + "#act").encode()).hexdigest() + ".json")).write_text("", encoding="utf-8")
    refused = _of(ledger=ledger, claim=True)
    assert refused["seat_directive_refused"]["reason"] == "already_claimed"
    assert "UNKNOWN" in refused["seat_directive_refused"]["note"]
    resumed = _of(ledger=ledger, claim=True, resume=True)["seat_directive"]["claim"]
    assert resumed.startswith("RESUMED, not a new claim") and "UNKNOWN" in resumed and "never repeat" in resumed


def test_the_claim_names_its_claimer_for_resume(tmp_path):
    ledger = tmp_path / "seat.json.directives"
    _of(ledger=ledger, claim=True)
    resumed = _of(ledger=ledger, claim=True, resume=True)["seat_directive"]["claim"]
    import os
    assert "by pid %d" % os.getpid() in resumed


def test_an_op_id_retry_with_other_hours_is_a_different_operation():
    """codex-oxxxxxx-admin-cli 6e15a1: the requested lifetime is journalled beside the generated expiry."""
    fp = lambda h: node._op_fingerprint_for_args(to=["nxxxxx"], thread_id="t", msg_type="request", body="b",
                                                 directive=directive(), directive_hours=h)
    assert fp(4.0) == fp(4)
    assert fp(4.0) != fp(1.0)


def test_a_recorded_outcome_ends_resume_and_is_written_once(tmp_path):
    """codex-oxxxxxx-admin-cli 20e75f: resuming is continuing an interrupted attempt, never a replay of finished work.
    directive-done records the outcome once; --resume then refuses done and refused, and names an unknown outcome."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    assert "directive-done" in _of(ledger=ledger, claim=True)["seat_directive"]["claim"]
    node.record_directive_outcome(state, KEY, "done", "merged #490")
    out = _of(ledger=ledger, claim=True, resume=True)
    assert out["seat_directive_refused"]["reason"] == "already_done" and "Kimi swarm" not in repr(out)
    with pytest.raises(node.Refused):
        node.record_directive_outcome(state, KEY, "refused")                       # once only
    with pytest.raises(node.Refused):
        node.record_directive_outcome(state, KEY + "x", "maybe")                   # a closed set
    k2 = "msg/1793624000002-kxxx-b330cf"                                          # CONTROL: unknown still resumes
    _of(ledger=ledger, key=k2, claim=True)
    node.record_directive_outcome(state, k2, "unknown")
    resumed = _of(ledger=ledger, key=k2, claim=True, resume=True)["seat_directive"]["claim"]
    assert "RESUMED" in resumed and "outcome is UNKNOWN" in resumed


def test_unknown_is_replaced_once_by_a_final_outcome_and_resume_then_stops(tmp_path):
    """pxx 555fad / GitHub Codex on 54b29cb9 (node.py:1969): claim, unknown, resume, done; then resume is refused.
    done and refused are final; unknown is never replaced by unknown."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    assert "outcome is UNKNOWN" in _of(ledger=ledger, claim=True, resume=True)["seat_directive"]["claim"]
    with pytest.raises(node.Refused):
        node.record_directive_outcome(state, KEY, "unknown")
    assert node.record_directive_outcome(state, KEY, "done")["replaces"] == "unknown"
    assert _of(ledger=ledger, claim=True, resume=True)["seat_directive_refused"]["reason"] == "already_done"
    for later in ("unknown", "refused", "done"):
        with pytest.raises(node.Refused):
            node.record_directive_outcome(state, KEY, later)


def test_an_op_id_retry_after_a_label_move_is_the_same_operation():
    """GitHub Codex on 54b29cb9 (cli.py:2002): the retry re-resolves target_peer; the journalled envelope is resent."""
    a = directive()
    moved = {**a, "target_peer": "11111111-2222-4333-8444-555555555555"}
    fp = lambda d: node._op_fingerprint_for_args(to=["nxxxxx"], thread_id="t", msg_type="request", body="b", directive=d)
    assert fp(a) == fp(moved)
    entry = {"op_id": "o1", "value": {"directive": a}, "bulk_ack": None, "fingerprint": fp(a)}
    node._op_assert_same_message(entry, dict(to=["nxxxxx"], thread_id="t", msg_type="request", body="b", directive=moved))
    assert fp(a) != fp(directive(target="pxx"))                                    # CONTROL: another label is not


def test_an_outcome_needs_this_identity_s_act_claim(tmp_path):
    """codex-oxxxxxx-admin-cli bc9a07, P2-1: an outcome without a claim is refused, so done/refused can never sit beside
    a later 'claimed now'."""
    state = tmp_path / "seat.json"
    with pytest.raises(node.Refused) as err:
        node.record_directive_outcome(state, KEY, "done")
    assert "no act claim" in str(err.value)
    _of(ledger=node.directive_ledger(state), claim=True)                                          # CONTROL
    assert node.record_directive_outcome(state, KEY, "done")["outcome"] == "done"


def test_two_sessions_racing_to_replace_unknown_one_wins_the_other_names_it(tmp_path):
    """pxx db2500 / GitHub Codex on b3df13e0 (node.py:1987): two processes finishing the same unknown directive. The
    replace lock lets exactly one through; the other is refused naming the outcome on record. Threads stand in for the
    two processes (each call opens the lock file itself)."""
    import threading
    state = tmp_path / "seat.json"
    _of(ledger=node.directive_ledger(state), claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    gate, results = threading.Barrier(2), []

    def finish(outcome):
        gate.wait(timeout=10)
        try:
            results.append(("ok", node.record_directive_outcome(state, KEY, outcome)["outcome"]))
        except node.Refused as exc:
            results.append(("refused", str(exc)))
    ts = [threading.Thread(target=finish, args=(o,)) for o in ("done", "refused")]
    for th in ts:
        th.start()
    for th in ts:
        th.join(timeout=20)
    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1, results
    winner = oks[0][1]
    loser = [r for r in results if r[0] == "refused"][0][1]
    assert repr(winner) in loser
    assert node._recorded_outcome(node.directive_ledger(state), KEY)["outcome"] == winner


def test_a_replace_lock_left_by_a_crashed_winner_still_refuses_a_second_replace(tmp_path):
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    lock = node.directive_outcome_path(ledger, KEY).with_name(node.directive_outcome_path(ledger, KEY).name + ".replace.lock")
    lock.write_text("{}", encoding="utf-8")                      # the winner died after taking the lock
    with pytest.raises(node.Refused) as err:
        node.record_directive_outcome(state, KEY, "done")
    assert "'unknown_corrupt'" in str(err.value)                         # a hand-edited lock reads as corrupt


def test_a_journal_from_the_54b29cb9_fingerprint_format_still_matches_its_retry():
    """codex-oxxxxxx-admin-cli 7f56e2: an entry fingerprinted when target_peer was part of the identity. An unchanged
    retry, and one after the label moved, match it; changed words, label or hours still refuse."""
    a = directive()
    kw = dict(to=["nxxxxx"], thread_id="t", msg_type="request", body="b")
    old_fp = node._op_fingerprint_for_args(**kw, directive=a, directive_hours=4.0, _directive_keeps_peer=True)
    assert old_fp != node._op_fingerprint_for_args(**kw, directive=a, directive_hours=4.0)     # really the old format
    entry = {"op_id": "o1", "value": {"directive": a}, "bulk_ack": None, "fingerprint": old_fp}
    node._op_assert_same_message(entry, dict(kw, directive=a, directive_hours=4.0))
    moved = {**a, "target_peer": "11111111-2222-4333-8444-555555555555"}
    node._op_assert_same_message(entry, dict(kw, directive=moved, directive_hours=4.0))
    for changed in (dict(kw, directive={**a, "words": ["other"]}, directive_hours=4.0),
                    dict(kw, directive=directive(target="pxx"), directive_hours=4.0),
                    dict(kw, directive=a, directive_hours=2.0),
                    dict(kw, body="other", directive=a, directive_hours=4.0)):
        with pytest.raises(node.Refused):
            node._op_assert_same_message(entry, changed)


def test_a_first_outcome_is_never_seen_empty_by_a_racing_finisher(tmp_path):
    """codex-oxxxxxx-admin-cli 7f56e2: initial create versus a finisher. Two threads record the FIRST outcome at once
    (unknown and done); exactly one first record is published whole, and the stored outcome is one a caller got back."""
    import threading
    state = tmp_path / "seat.json"
    _of(ledger=node.directive_ledger(state), claim=True)
    gate, got = threading.Barrier(2), []

    def rec(o):
        gate.wait(timeout=10)
        try:
            got.append(node.record_directive_outcome(state, KEY, o)["outcome"])
        except node.Refused as exc:
            got.append("refused: " + str(exc))
    ts = [threading.Thread(target=rec, args=(o,)) for o in ("unknown", "done")]
    for th in ts:
        th.start()
    for th in ts:
        th.join(timeout=20)
    stored = node._recorded_outcome(node.directive_ledger(state), KEY)
    assert stored is not None and stored["outcome"] in ("unknown", "done") and "key" in stored     # never empty
    assert stored["outcome"] in got


def test_a_finisher_that_dies_after_its_lock_leaves_its_outcome_on_record(tmp_path):
    """codex-oxxxxxx-admin-cli ba83d3: the crash between the replace lock and the rename. The lock is published whole
    with the decided outcome, so the outcome IS on record: a later finalize names it, and resume is refused."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    node._replace_lock_path(ledger, KEY).write_text(json.dumps({"pid": 1, "at": "t", "outcome": "done"}), encoding="utf-8")
    assert node._recorded_outcome(ledger, KEY)["outcome"] == "done"
    assert node._recorded_outcome(ledger, KEY, include_lock=False)["outcome"] == "unknown"       # the file is unrenamed
    with pytest.raises(node.Refused) as err:
        node.record_directive_outcome(state, KEY, "refused")
    assert "'done'" in str(err.value)
    assert _of(ledger=ledger, claim=True, resume=True)["seat_directive_refused"]["reason"] == "already_done"


def test_a_finisher_that_dies_before_its_lock_strands_nothing(tmp_path):
    """The crash BEFORE the hard link: only a private temp file is left, no lock, so the next finalize succeeds."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    lk = node._replace_lock_path(ledger, KEY)
    lk.with_name(lk.name + ".999.1.new").write_text(json.dumps({"outcome": "refused"}), encoding="utf-8")
    assert node.record_directive_outcome(state, KEY, "done")["outcome"] == "done"
    assert node._recorded_outcome(ledger, KEY)["outcome"] == "done"


def test_a_new_directive_key_must_carry_the_current_time():
    """pxx ce0126 / f6f4d3: the rate count reads a key range, so a new directive's msg/ key epoch must be within
    DIRECTIVE_KEY_SKEW_MS of the server clock, strictly (the count's range is half-open). Run at admission, after the
    replay check (see the DB test for the replay)."""
    now_ms = int(NOW.timestamp() * 1000)
    for key, why in (("msg/1000000000000-kxxx-b330cf", "2001"), ("msg/4000000000000-kxxx-b330cf", "2096"),
                     (f"msg/{now_ms - V.DIRECTIVE_KEY_SKEW_MS - 1000}-kxxx-b330cf", "just past the skew, old"),
                     (f"msg/{now_ms + V.DIRECTIVE_KEY_SKEW_MS}-kxxx-b330cf", "exactly at the skew: the range excludes it"),
                     ("msg/notatime-kxxx-b330cf", "no epoch"),
                     (f"msg/0{now_ms}-kxxx-b330cf", "a leading zero: sorts below the counted range"),
                     ("msg/" + str(now_ms).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")) + "-kxxx-b330cf",
                      "Arabic-Indic digits: sort above it"),
                     ("msg/" + str(now_ms).translate(str.maketrans("0123456789", "０１２３４５６７８９")) + "-kxxx-b330cf",
                      "full-width digits"),
                     (f"msg/{now_ms}0-kxxx-b330cf", "14 digits")):
        with pytest.raises(HandlerError) as err:
            V.check_directive_key_time(key, now=NOW)
        assert err.value.code == "E_INVALID_ARGS" and "current time" in str(err.value), why
    V.check_directive_key_time(f"msg/{now_ms}-kxxx-b330cf", now=NOW)                               # CONTROL
    V.check_directive_key_time(f"msg/{now_ms - V.DIRECTIVE_KEY_SKEW_MS + 1000}-kxxx-b330cf", now=NOW)


def test_resume_refuses_a_corrupt_or_hand_edited_replace_lock(tmp_path):
    """pxx 83405a INFO: a lock is published whole, so an unreadable one, or one naming no final outcome, was edited by
    hand or corrupted. Resume refuses it (unknown_corrupt) and names the manual reconciliation, never the words."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    for bad in ("", "{}", "null", "[]", '{"outcome": "maybe"}'):
        node._replace_lock_path(ledger, KEY).write_text(bad, encoding="utf-8")
        out = _of(ledger=ledger, claim=True, resume=True)
        assert out["seat_directive_refused"]["reason"] == "unknown_corrupt", bad
        assert "Reconcile by hand" in out["seat_directive_refused"]["note"] and "Kimi swarm" not in repr(out)
    node._replace_lock_path(ledger, KEY).write_bytes(b'{"outcome": "\xff\xfe"}')       # not UTF-8 (pxx F1)
    assert _of(ledger=ledger, claim=True, resume=True)["seat_directive_refused"]["reason"] == "unknown_corrupt"
    node._replace_lock_path(ledger, KEY).unlink()                                         # CONTROL: no lock, resumes
    assert "seat_directive" in _of(ledger=ledger, claim=True, resume=True)


def test_an_outcome_file_that_is_not_utf8_reads_as_unknown_not_a_crash(tmp_path):
    """pxx F1 on 2755f4d1: UnicodeDecodeError is a ValueError raised by the read itself; it is bytes, not a sharing
    moment, and keeps the old meaning of an outcome whose bytes are not a record."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    node.directive_outcome_path(ledger, KEY).write_bytes(b'{"outcome": "\xff\xfe"}')
    assert node._recorded_outcome(ledger, KEY)["outcome"] == "unknown"


def _flaky_reads(monkeypatch, name, fails, after=0):
    """Path.read_text raises PermissionError for the file `name`: its first `after` reads pass, then `fails` reads fail
    (every later read when fails is None), as Windows answers while another session deletes or replaces a link of the
    same file."""
    real = node.Path.read_text
    seen = {"n": 0, "reads": 0}

    def read_text(self, *a, **kw):
        if self.name == name:
            seen["reads"] += 1
            if seen["reads"] > after and (fails is None or seen["n"] < fails):
                seen["n"] += 1
                raise PermissionError(13, "The process cannot access the file", str(self))
        return real(self, *a, **kw)
    monkeypatch.setattr(node.Path, "read_text", read_text)
    return seen


def test_a_lock_read_that_fails_for_a_moment_is_retried_not_taken_for_corrupt(tmp_path, monkeypatch):
    """memory-bxxxxx's flake in the racing test above: on Windows the loser opened the lock while the winner deleted
    its temp link of the same file, the read failed once, and the loser named 'unknown_corrupt' instead of the
    winner's outcome. A lock carrying "done" beside an unknown outcome (a winner that died before its rename) must
    read as done when its first reads fail."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    lock = node._replace_lock_path(ledger, KEY)
    lock.write_text(json.dumps({"pid": 1, "at": "2026-11-05T12:00:00+00:00", "outcome": "done"}), encoding="utf-8")
    assert node._recorded_outcome(ledger, KEY)["outcome"] == "done"                             # CONTROL
    seen = _flaky_reads(monkeypatch, lock.name, fails=2)
    assert node._recorded_outcome(ledger, KEY)["outcome"] == "done"
    assert seen["n"] == 2


def test_an_outcome_that_cannot_be_read_is_refused_never_replayed_or_overwritten(tmp_path, monkeypatch):
    """Before this, an outcome file that could not be opened read as "unknown": --resume showed a directive whose act
    was DONE as actionable again, and directive-done could replace that final "done". Both are refused now."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "done")
    assert _of(ledger=ledger, claim=True, resume=True)["seat_directive_refused"]["reason"] == "already_done"  # CONTROL
    _flaky_reads(monkeypatch, node.directive_outcome_path(ledger, KEY).name, fails=None)
    out = _of(ledger=ledger, claim=True, resume=True)
    assert "seat_directive" not in out
    assert out["seat_directive_refused"]["reason"] == "outcome_unreadable", out
    with pytest.raises(node.Refused) as err:
        node.record_directive_outcome(state, KEY, "refused")
    assert "could not be read just now" in str(err.value)
    monkeypatch.undo()
    assert node._recorded_outcome(ledger, KEY)["outcome"] == "done"                             # still final


def test_under_the_lock_an_unreadable_reread_still_records_the_outcome(tmp_path, monkeypatch):
    """pxx F2 on 2755f4d1: past the replace lock, an outcome file that could not be re-read was refused as "nothing was
    recorded, try again", though the lock already carried the outcome, and every retry then met "already recorded".
    The lock holder is the only writer and the read before the lock saw unknown, so it goes on and replaces."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    _flaky_reads(monkeypatch, node.directive_outcome_path(ledger, KEY).name, fails=None, after=1)
    assert node.record_directive_outcome(state, KEY, "done", note="finished")["outcome"] == "done"
    monkeypatch.undo()
    got = node._recorded_outcome(ledger, KEY)
    assert got["outcome"] == "done" and got.get("note") == "finished"


def test_a_replace_that_keeps_failing_leaves_the_outcome_on_record_through_the_lock(tmp_path, monkeypatch):
    """pxx INFO 1: os.replace fails on Windows while another process has the outcome open. It is retried like the
    reads; if it keeps failing the outcome stands through the replace lock, note included, and the caller gets no bare
    PermissionError."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "The process cannot access the file", str(dst))
    monkeypatch.setattr(node.os, "replace", replace)
    rec = node.record_directive_outcome(state, KEY, "refused", note="abandoned")
    assert rec["outcome"] == "refused" and "replace lock" in rec["via"] and calls["n"] == node._RECORD_READ_TRIES
    monkeypatch.undo()
    got = node._recorded_outcome(ledger, KEY)
    assert got["outcome"] == "refused" and got.get("note") == "abandoned"


def test_a_temp_whose_delete_failed_is_never_written_through_by_a_later_call(tmp_path, monkeypatch):
    """pxx F3 on 2755f4d1: after os.link a temp and the outcome are one file. With a fixed temp name (pid + id(rec)) a
    later call in the same process wrote through the left-behind link and truncated the final record. id is pinned
    here so the old naming collides every time; temp names are now random and created exclusively."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    monkeypatch.setattr(node, "id", lambda obj: 4242, raising=False)
    real_unlink = node.Path.unlink

    def unlink(self, *a, **kw):
        if self.name.endswith(".new"):
            raise PermissionError(13, "The process cannot access the file", str(self))
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(node.Path, "unlink", unlink)
    assert node.record_directive_outcome(state, KEY, "done")["outcome"] == "done"
    with pytest.raises(node.Refused):
        node.record_directive_outcome(state, KEY, "unknown")
    monkeypatch.undo()
    on_disk = json.loads(node.directive_outcome_path(ledger, KEY).read_text(encoding="utf-8"))
    assert on_disk["outcome"] == "done"
    assert node._recorded_outcome(ledger, KEY)["outcome"] == "done"


def test_a_missing_outcome_file_still_answers_from_a_surviving_lock(tmp_path):
    """pxx F4 on 2755f4d1: an outcome file gone before the read (removed from outside) made _recorded_outcome answer
    None without reading the lock, so --resume showed a finished directive. The lock's final outcome answers."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    node._replace_lock_path(ledger, KEY).write_text(json.dumps({"pid": 1, "at": "2026-11-05T12:00:00+00:00",
                                                                "outcome": "done"}), encoding="utf-8")
    node.directive_outcome_path(ledger, KEY).unlink()
    assert node._recorded_outcome(ledger, KEY)["outcome"] == "done"
    assert _of(ledger=ledger, claim=True, resume=True)["seat_directive_refused"]["reason"] == "already_done"
    node._replace_lock_path(ledger, KEY).unlink()                                         # CONTROL: nothing on record
    assert node._recorded_outcome(ledger, KEY) is None


def test_a_lock_that_stays_unreadable_refuses_resume(tmp_path, monkeypatch):
    """codex-pxx-cli's M4 (unreadable lock read as unknown) survived every other test: an unknown outcome beside a
    lock that cannot be opened through every retry must refuse, never resume."""
    state = tmp_path / "seat.json"
    ledger = node.directive_ledger(state)
    _of(ledger=ledger, claim=True)
    node.record_directive_outcome(state, KEY, "unknown")
    lock = node._replace_lock_path(ledger, KEY)
    lock.write_text(json.dumps({"pid": 1, "at": "2026-11-05T12:00:00+00:00", "outcome": "done"}), encoding="utf-8")
    _flaky_reads(monkeypatch, lock.name, fails=None)
    out = _of(ledger=ledger, claim=True, resume=True)
    assert "seat_directive" not in out
    assert out["seat_directive_refused"]["reason"] == "outcome_unreadable", out


def test_codex_oxxxxxx_cli_is_on_both_rosters():
    assert node.SEAT_DIRECTIVE_PEERS.get("2ea9214c-7fd2-5764-8e85-4ddd1ec94d07") == "codex-oxxxxxx-cli"
    assert "2ea9214c-7fd2-5764-8e85-4ddd1ec94d07" in V.SEAT_DIRECTIVE_PEERS
