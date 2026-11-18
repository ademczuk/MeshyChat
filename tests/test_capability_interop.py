"""T12: grant-token conformance in both directions (B07 slice 6; docs/DIVERGENCES.md 040;
design docs/design/biscuit-capabilities.md 6.1 T12 and section 8, inference 1).

The captured fixture corpus contains skill-grant tokens under a fresh root with the request each
token is evaluated against. It checks parsing, signatures, term encoding, and contract decisions,
not object-capability policy:

  1. biscuit-python parses every token under the corpus's root, reads the grant tuple as four
     strings and expiry as an INTEGER of unix seconds, and an authorizer reaches the documented
     decision at the corpus's `now`; a tampered token and a foreign root are refused. A DATE
     `time` fact denies every token with an expiry because a date and an integer do not compare.
     This server refuses the corpus tokens as object capabilities because they have no root key id.
  2. A standalone conformance verifier built only on biscuit-auth, independently of this server's
     code, accepts a corpus MINTED HERE by biscuit-python: grant tokens in the grant-token shape
     the external mint function produces (allow, future expiry, expired, wrong peer, attenuated)
     all PASS, exit 0; a fixture whose token contradicts its row is a FAIL, exit 1. An object
     token delegated through a third-party block verifies signatures but reaches a Datalog TYPE
     error because the verifier states `time` as an integer and the token checks a date. The
     verifier has no authorizer limits; the 1 ms default trips under load in its debug build in
     3 of 9 measured runs, so a run whose only failures are that is retried, never counted as a
     verdict.
     This half runs the binary named by MESHY_BISCUIT_CONFORMANCE_BIN; without it the two tests
     skip. Building that binary is not part of the suite.

Run:  pytest tests/test_capability_interop.py -q
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

CORPUS = ROOT / "tests" / "fixtures" / "grant_biscuit_fixtures.json"
#: The corpus file as the external generator printed it (a fresh root per run, so the bytes are
#: this run's; the digest pins that the file is the one the divergence describes). Taken
#: over the bytes with CRLF read as LF, since a checkout with core.autocrlf rewrites them.
CORPUS_SHA256 = "3753f96c322f9d40b5ada81d2592aabaf337ed1d474bb42a7f9bfaff53b891db"
CONFORMANCE_ENV = "MESHY_BISCUIT_CONFORMANCE_BIN"


def _b():
    import biscuit_auth
    return biscuit_auth


def _corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _root(corpus):
    b = _b()
    return b.PublicKey.from_bytes(bytes.fromhex(corpus["root_public_hex"]), b.Algorithm.Ed25519)


def contract_allows(grant: dict, req: dict, now: int) -> bool:
    """Evaluate the documented grant contract."""
    return (all(grant[k] == req[k] for k in ("grantor", "grantee", "peer", "skill"))
            and (grant["expires_unix"] is None or grant["expires_unix"] > now))


def _authorizer(source: str, params: dict, biscuit):
    """With this server's explicit limits (the 1 ms default trips under load, design 1.3 row
    20), which change no decision here."""
    from apps.memoryskills.capability import _limited
    return _limited(_b().AuthorizerBuilder(source, params)).build(biscuit)


def python_decides(token: str, root, req: dict, time_fact) -> bool:
    """An authorizer for the documented contract: the grant tuple from the REQUEST and the
    token's own time check against `time_fact`."""
    b = _b()
    biscuit = b.Biscuit.from_base64(token, root)
    authorizer = _authorizer(
        "time({now}); allow if grant({grantor}, {grantee}, {peer}, {skill}); deny if true;",
        {"now": time_fact, **req}, biscuit)
    try:
        authorizer.authorize()
        return True
    except b.AuthorizationError:
        return False


def test_t12_the_corpus_is_fixture_output_verbatim():
    raw = CORPUS.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == CORPUS_SHA256
    corpus = json.loads(raw)
    assert set(corpus) == {"root_public_hex", "algorithm", "now_unix", "fixtures"}
    assert corpus["algorithm"] == "ed25519" and len(corpus["fixtures"]) >= 10


def test_t12_python_accepts_the_external_fixture_corpus():
    b = _b()
    corpus = _corpus()
    root, now = _root(corpus), corpus["now_unix"]
    decisions = {}
    for f in corpus["fixtures"]:
        biscuit = b.Biscuit.from_base64(f["token_b64"], root)        # every signature verifies
        g = f["grant"]
        terms = _authorizer("", {}, biscuit).query(
            b.Rule("g($a, $b, $c, $d) <- grant($a, $b, $c, $d)"))
        # The authority block's grant is four strings; an attenuated token's appended
        # grant is not visible to a root authorizer, so exactly the one tuple comes back.
        assert [list(t.terms) for t in terms] == [[g["grantor"], g["grantee"], g["peer"],
                                                   g["skill"]]], f["name"]
        source = biscuit.block_source(0)
        if g["expires_unix"] is not None:
            assert f"$t < {g['expires_unix']}" in source.replace("  ", " "), (f["name"], source)
        decisions[f["name"]] = python_decides(f["token_b64"], root, f["req"], now)
        assert decisions[f["name"]] == contract_allows(g, f["req"], now), (f["name"], decisions)
    assert any(decisions.values()) and not all(decisions.values()), decisions
    for f in (x for x in corpus["fixtures"] if x.get("attenuated")):
        assert b.Biscuit.from_base64(f["token_b64"], root).block_count() >= 2

    first = corpus["fixtures"][0]
    tampered = first["token_b64"][:-1] + ("B" if first["token_b64"][-1] == "A" else "A")
    with pytest.raises(Exception):
        python_decides(tampered, root, first["req"], now)
    with pytest.raises(Exception):
        python_decides(first["token_b64"], b.KeyPair().public_key, first["req"], now)


def test_t12_a_date_time_fact_denies_every_external_token_with_an_expiry():
    """Measured: the corpus writes expiry as an integer (`unix_secs`), while this server's
    verifier states `time` as a DATE (design 4.1). Asked that way, a token with an expiry denies, whatever
    its tuple: the fail-closed direction. A token with no expiry is unaffected (control)."""
    corpus = _corpus()
    root, now = _root(corpus), corpus["now_unix"]
    as_date = _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
    seen = {"with an expiry": 0, "without": 0}
    for f in corpus["fixtures"]:
        allowed = python_decides(f["token_b64"], root, f["req"], as_date)
        if f["grant"]["expires_unix"] is not None:
            seen["with an expiry"] += 1
            assert allowed is False, f["name"]
        else:
            seen["without"] += 1
            assert allowed == contract_allows(f["grant"], f["req"], now), f["name"]
    assert seen["with an expiry"] and seen["without"], seen


def test_t12_this_servers_verifier_refuses_a_foreign_token_outright():
    """A skill-grant token is not an object capability here: it carries no root key id, and the
    kid provider refuses a token without one, even with the corpus root in the verify set under
    kid 0 (E_CAPABILITY_INVALID). Control: the same store verifies an object token minted under
    that kid."""
    from apps.memoryskills import capability, capability_keys
    from apps.memoryskills.errors import HandlerError

    b = _b()
    corpus = _corpus()
    stranger = b.KeyPair()
    store = capability_keys.RootKeyStore(bytes(stranger.private_key.to_bytes()), 1, {
        0: bytes.fromhex(corpus["root_public_hex"]), 1: bytes(stranger.public_key.to_bytes())})
    now = _dt.datetime.now(_dt.timezone.utc)
    capability.clear_cache()
    for f in corpus["fixtures"]:
        with pytest.raises(HandlerError) as exc:
            capability._verify(f["token_b64"], store, now)
        assert exc.value.code == "E_CAPABILITY_INVALID", f["name"]
        # Refused at step 1, by the kid provider: not later, by the authority block's shape
        # (a corpus token also states no subject), which would hide a provider that
        # answered a kid-less token.
        assert "could not be parsed, or a signature" in exc.value.message, (
            f["name"], exc.value.message)
    builder = b.BiscuitBuilder(
        'cap_version(1); issuer({i}); subject({s}); right("ns", "", "read"); depth_cap(0);',
        {"i": str(uuid.uuid4()), "s": str(uuid.uuid4())})
    builder.set_root_key_id(1)
    ok = builder.build(stranger.private_key).to_base64()
    assert capability._verify(ok, store, now).blocks == 1
    capability.clear_cache()


# ============================================================================ verifier run

def _grant_token(root_kp, grant: dict, *, expires_unix=None, attenuate_skill=None) -> str:
    """`mint_grant_biscuit` shape minted by biscuit-python: the grant tuple as strings and an
    integer expiry check when there is one."""
    b = _b()
    code = "grant({grantor}, {grantee}, {peer}, {skill});"
    params = dict(grant)
    if expires_unix is not None:
        code += "\ncheck if time($t), $t < {exp};"
        params["exp"] = expires_unix
    token = b.BiscuitBuilder(code, params).build(root_kp.private_key)
    if attenuate_skill is not None:
        token = token.append(b.BlockBuilder("grant({grantor}, {grantee}, {peer}, {skill});",
                                            {**grant, "skill": attenuate_skill}))
    return token.to_base64()


def _object_token(root_kp) -> str:
    """This server's object token in design 2.3's shape (cap_version, issuer, subject, a right,
    a depth cap, a DATE expiry, a root key id), delegated once through a THIRD-PARTY block signed
    by its subject's key (2.4): every term a parameter, as tests/capability_world.py mints."""
    b = _b()
    subject, delegate = b.KeyPair(), str(uuid.uuid4())
    expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)).replace(microsecond=0)
    builder = b.BiscuitBuilder(
        "cap_version(1); issuer({i}); subject({s}); right({ns}, {p}, {a}); depth_cap(1);\n"
        "check if time($t), $t < {exp};",
        {"i": str(uuid.uuid4()), "s": str(uuid.uuid4()), "ns": "ns/tasks", "p": "task/42/",
         "a": "read", "exp": expires})
    builder.set_root_key_id(7)
    token = builder.build(root_kp.private_key)
    hop = expires - _dt.timedelta(minutes=55)
    block = b.BlockBuilder("delegate({e}); delegation_expires({x}); check if time($t), $t < {x};",
                           {"e": delegate, "x": hop})
    signed = token.third_party_request().create_block(subject.private_key, block)
    return token.append_third_party(subject.public_key, signed).to_base64()


def _python_corpus(*, lie: bool = False) -> dict:
    """Grant tokens minted here, each with the row the tool checks."""
    b = _b()
    root = b.KeyPair()
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    og, ge, peer, other_peer = (str(uuid.UUID(int=n << 120)) for n in (0x11, 0x22, 0x33, 0x44))
    grant = {"grantor": og, "grantee": ge, "peer": peer, "skill": "summarize"}
    fixtures = [
        _fixture("py_exact_match_null_expiry", _grant_token(root, grant), grant, grant),
        _fixture("py_future_expiry", _grant_token(root, grant, expires_unix=now + 3600), grant,
                 grant, now + 3600),
        _fixture("py_expired", _grant_token(root, grant, expires_unix=now - 3600), grant, grant,
                 now - 3600),
        _fixture("py_wrong_peer", _grant_token(root, grant), grant,
                 {**grant, "peer": other_peer}),
        _fixture("py_attenuated_injected_skill_inert",
                 _grant_token(root, grant, attenuate_skill="translate"), grant,
                 {**grant, "skill": "translate"}, attenuated=True),
    ]
    if lie:
        fixtures.append(_fixture("py_lying_row",
                                 _grant_token(root, {**grant, "skill": "translate"}), grant,
                                 grant))
    return {"root_public_hex": bytes(root.public_key.to_bytes()).hex(), "now_unix": now,
            "fixtures": fixtures}


def _fixture(name, token, g, req, expires_unix=None, **extra) -> dict:
    return {"name": name, "token_b64": token,
            "grant": {**g, "expires_unix": expires_unix,
                      "expires_unix_millis": None if expires_unix is None
                      else expires_unix * 1000},
            "req": req, **extra}


#: What the conformance verifier prints when an authorizer runs past biscuit-auth's DEFAULT
#: limits: it sets none, and the 1 ms default trips under load in its debug build (measured: 3
#: of 9 runs; design 1.3 row 20). Not a verdict on a token.
LIMITS = "Reached Datalog execution limits"


def _run_tool(corpus: dict, *, attempts: int = 6):
    """The tool's run, retried while its only failures are LIMITS (none of them is a decision);
    every failure line of every attempt is returned beside the last run."""
    tool = os.environ.get(CONFORMANCE_ENV)
    if not tool:
        pytest.skip(f"{CONFORMANCE_ENV} names no built biscuit conformance binary")
    seen = []
    for _ in range(attempts):
        ran = subprocess.run([tool], input=json.dumps(corpus), capture_output=True, text=True,
                             timeout=120)
        fails = [ln for ln in ran.stdout.splitlines() if ln.startswith("FAIL")]
        seen.extend(fails)
        if not fails or any(LIMITS not in ln for ln in fails):
            return ran, seen
    return ran, seen


def test_t12_external_conformance_tool_accepts_python_minted_tokens():
    """Grant tokens minted by biscuit-python in `mint_grant_biscuit`'s shape: every one PASSES
    the configured verifier (interop, the contract, and the tamper, foreign-root and attenuation passes),
    exit 0. Positive control: a row its token contradicts is a FAIL, exit 1."""
    corpus = _python_corpus()
    ran, seen = _run_tool(corpus)
    print("T12 CONFORMANCE", ran.stdout, seen)
    assert all(LIMITS in ln for ln in seen), seen
    assert ran.returncode == 0, ran.stdout + ran.stderr
    lines = ran.stdout.splitlines()
    for f in corpus["fixtures"]:
        assert any(line.startswith(f"PASS [{f['name']}]") for line in lines), f["name"]
    assert "PASS [py_exact_match_null_expiry] decision=true" in ran.stdout
    assert "PASS [py_expired] decision=false" in ran.stdout
    assert lines[-1] == f"{len(corpus['fixtures']) + 3} passed, 0 failed", lines[-1]

    lying, _ = _run_tool(_python_corpus(lie=True))
    assert lying.returncode == 1, lying.stdout
    assert [ln for ln in lying.stdout.splitlines()
            if ln.startswith("FAIL") and LIMITS not in ln] == [
        "FAIL [py_lying_row] independent biscuit-auth says false, documented contract says true"
    ], lying.stdout


def test_t12_external_tool_verifies_this_servers_delegated_object_token():
    """This server's object token (design 2.3's authority and a third-party delegation block):
    the configured verifier deserializes it and verifies every signature, the delegation's included,
    and its authorizer then fails with a Datalog TYPE error, because it states `time` as an
    integer and the token checks a date. Controls, same corpus shape: the same token with one
    character changed, and the intact token under a foreign root, fail EARLIER, at
    "error deserializing or verifying the token". So the type error is reached only past the
    signatures. Never an allow: the tool's policy is the grant contract, which an object token
    does not state."""
    b = _b()
    root = b.KeyPair()
    token = _object_token(root)
    middle = len(token) // 2
    tampered = token[:middle] + ("A" if token[middle] != "A" else "B") + token[middle + 1:]
    row = {"grantor": "g", "grantee": "e", "peer": "p", "skill": "s"}

    def verdict(tok, root_hex):
        ran, seen = _run_tool({"root_public_hex": root_hex, "now_unix": 1, "fixtures": [
            _fixture("object", tok, row, row, expires_unix=0)]})
        fails = [ln for ln in ran.stdout.splitlines() if ln.startswith("FAIL [object]")]
        assert ran.returncode == 1 and len(fails) == 1, (ran.stdout, seen)
        return fails[0]

    own = bytes(root.public_key.to_bytes()).hex()
    foreign = bytes(b.KeyPair().public_key.to_bytes()).hex()
    assert verdict(token, own).endswith("Datalog  execution failure: Invalid type")
    unverified = "a valid fixture token failed to verify: error deserializing or verifying the token"
    assert verdict(tampered, own).endswith(unverified)
    assert verdict(token, foreign).endswith(unverified)
    assert b.Biscuit.from_base64(token, root.public_key).block_count() == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
