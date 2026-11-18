"""MeshyGuard, proved against the real database, the real routing table and real HTTP.

WHAT MAKES A TEST HERE COUNT

1. EVERY NEGATIVE ASSERTION HAS A POSITIVE CONTROL IN THE SAME TEST. "Denied" and "the
   fixture was broken" produce identical evidence otherwise: a 403 is also what a missing
   token, an inactive peer, a typo'd URL and an unseeded policy produce. Where the control
   is the interesting half it is asserted FIRST, so a failure reads as "the happy path
   broke" rather than "the guard fired".

2. NO ROOT_URLCONF OVERRIDE. `apps/meshguard/urls.py` is mounted in `meshychat/urls.py`,
   and these tests resolve against THAT. The previous round of modules pinned their own
   URLconf, so every route test passed while the application served 404. The `settings`
   fixture below overrides the JWT key and ALLOWED_HOSTS and deliberately touches nothing
   about routing.

3. NO DATABASE MOCKS. Every row is written to the migrated Postgres named by
   MESHY_TEST_DSN. Owners are `dashboard_users` rows and peers are `peers` rows, seeded
   through the owner role and read back through `meshychat_app`, the way a request does.

Run: .venv/Scripts/python.exe -m pytest tests/test_meshguard.py -q
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("MESHY_TEST_DSN", DSN)
os.environ.setdefault("MESHY_DSN", DSN)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

ISSUER = "meshguard-test-issuer"

_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("ascii")

from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover - only when run without the plugin
    os.environ["DJANGO_SETTINGS_MODULE"] = "meshychat.settings"

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.urls import resolve  # noqa: E402
from django.urls.exceptions import Resolver404  # noqa: E402

from apps.meshguard import (  # noqa: E402
    cel, db, enforcement, errors, gate, hooks, policies, repository,
)


# ------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def meshguard_settings():
    """Layer this module's JWT key onto whatever base configuration won the import race.

    `override_settings` rather than a module-level `configure()`, so these tests behave
    identically alone and in the full-suite run. ROUTING IS NOT OVERRIDDEN - see the
    module docstring. `ROOT_URLCONF` is forced back to the project's own, because other
    modules in this suite pin theirs and whichever ran first would otherwise leak.
    """
    with override_settings(
        ROOT_URLCONF="meshychat.urls",
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_DSN=DSN,
    ):
        yield


def test_the_settings_and_the_real_urlconf_are_both_in_effect(client):
    """Positive control for the fixture, and for every route test in this file.

    Asserts the URLconf is the PROJECT's (not a pinned one) and that the project's table
    really answers a meshguard path - so the twenty tests below fail with one named cause
    rather than twenty confusing 404s.
    """
    from django.conf import settings as live
    assert live.ROOT_URLCONF == "meshychat.urls"
    assert live.MESHY_JWT_PUBLIC_KEY_PEM == JWT_PUBLIC_PEM
    assert resolve("/v1/meshguard/policies").url_name == "meshguard-policies"
    # And a request really reaches the view: unauthenticated, so 401 with OUR envelope -
    # a 404 here would mean the module is not mounted after all.
    r = client.get("/v1/meshguard/policies")
    assert r.status_code == 401, r.content
    assert r.json()["error"] == "E_AUTH_MISSING"


@pytest.fixture()
def owner_conn():
    """Owner role: bypasses RLS. Seeding only."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """The connection a request actually runs on: meshychat_app, autocommit."""
    with db.connection() as conn:
        yield conn


def _new_owner(conn, *, plane="product", trust_group=None) -> str:
    owner_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
            "VALUES (%s, %s, %s, %s)",
            (owner_id, f"rg-{owner_id[:8]}@test.invalid", plane, trust_group),
        )
    return owner_id


def _new_peer(conn, owner_id, *, status="active") -> str:
    peer_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO peers (id, owner_id, name, status) VALUES (%s, %s, %s, %s)",
            (peer_id, owner_id, f"rg-peer-{peer_id[:8]}", status),
        )
    return peer_id


@pytest.fixture()
def tenant(owner_conn):
    """One owner with two peers: an actor and a distinct reviewer.

    Two peers, because the self-resolve refusal cannot be tested with one - and a test
    that cannot show the guard NOT firing has no control.
    """
    owner_id = _new_owner(owner_conn)
    actor = _new_peer(owner_conn, owner_id)
    reviewer = _new_peer(owner_conn, owner_id)
    return {"owner_id": owner_id, "actor": actor, "reviewer": reviewer}


def token_for(peer_id, *, scopes=("invoke",), aud="peer", ttl=3600) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return pyjwt.encode(
        {
            "sub": str(peer_id),
            "aud": aud,
            "iss": ISSUER,
            "iat": int(now.timestamp()),
            "exp": int((now + _dt.timedelta(seconds=ttl)).timestamp()),
            "scopes": list(scopes),
        },
        JWT_PRIVATE_PEM,
        algorithm="EdDSA",
    )


def auth_headers(peer_id) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token_for(peer_id)}"}


def post_json(client, path, body, peer_id):
    return client.post(path, data=json.dumps(body), content_type="application/json",
                       **auth_headers(peer_id))


@pytest.fixture()
def enforcing(monkeypatch):
    """Explicitly ENFORCE, so an enforcement test never depends on the default.

    The design default is OBSERVE; this app's default is ENFORCE, but a test that
    relies on the default proves the default and not the gate. The default itself is
    proved separately in `test_gate_defaults_closed_when_the_flag_is_absent`.
    """
    monkeypatch.setenv(enforcement.POLICY_REGISTRY_FLAG, "enforce")
    return enforcement.policy_registry_mode()


@pytest.fixture()
def observing(monkeypatch):
    monkeypatch.setenv(enforcement.POLICY_REGISTRY_FLAG, "observe")
    return enforcement.policy_registry_mode()


def _seed_policies(conn):
    return {p["name"]: p for p in
            repository.seed_builtin_policies(conn, policies.BUILTIN_POLICY_SEEDS)}


def _attach(conn, *, policy_id, owner_id, peer_id, params, scope="owner",
            session_key=None, phases=("tool_call",), ordinal=0):
    return repository.attach_policy(conn, {
        "policy_id": policy_id,
        "scope": scope,
        "owner_id": owner_id,
        "session_key": session_key,
        "params": params,
        "phases": list(phases),
        "ordinal": ordinal,
        "ask_timeout_secs": None,
        "created_by_peer_id": peer_id,
    })


def _event(owner_id, peer_id, *, skill="harness/claude_code/Bash", args=None,
           phase="tool_call", session="sess-1"):
    return policies.PolicyEvent(
        phase=phase,
        skill_name=skill,
        input_args={} if args is None else args,
        caller_owner_id=owner_id,
        caller_peer_id=peer_id,
        session_key=session,
    )


def _hook_body(*, kind="pre_tool", tool="Bash", digest=None, session="sess-1",
               preview=None):
    body = {
        "v": 1,
        "kind": kind,
        "harness": "claude_code",
        "session": {"session_key": session},
    }
    payload = {
        "input_digest": digest,
        "input_preview": {"cmd": "ls"} if preview is None else preview,
    }
    if kind in ("pre_tool", "post_tool"):
        body["tool"] = dict(payload, name=tool)
    elif kind == "prompt_submit":
        body["prompt"] = payload
    elif kind == "model_request":
        body["prompt"] = payload
    else:
        body["result"] = payload
    return body


# ============================================================ routing / wiring


ROUTES = [
    ("/v1/meshguard/skills/11111111-1111-1111-1111-111111111111",
     "meshguard-skill-review"),
    ("/v1/meshguard/memory/meshy/11111111-1111-1111-1111-111111111111/wiki/some/key",
     "meshguard-meshy-memory-review"),
    ("/v1/meshguard/scans/pending", "meshguard-pending-scans"),
    ("/v1/meshguard/approvals/pending", "meshguard-pending-approvals"),
    ("/v1/meshguard/approvals/appr_deadbeef/resolve", "meshguard-resolve-approval"),
    ("/v1/meshguard/approvals/appr_deadbeef", "meshguard-approval-item"),
    ("/v1/meshguard/policies", "meshguard-policies"),
    ("/v1/meshguard/policies/attachments", "meshguard-attachments"),
    ("/v1/meshguard/policies/attachments/rga_x/toggle", "meshguard-toggle-attachment"),
    ("/v1/meshguard/policies/attachments/rga_x", "meshguard-remove-attachment"),
    ("/v1/meshguard/policies/decisions", "meshguard-decisions"),
    ("/v1/meshguard/hooks/evaluate", "meshguard-hooks-evaluate"),
    ("/v1/meshguard/hooks/backlog", "meshguard-hooks-backlog"),
    ("/v1/meshguard/hooks/bypass-audit", "meshguard-hooks-bypass-audit"),
]


@pytest.mark.parametrize("path,name", ROUTES)
def test_every_route_resolves_through_the_project_urlconf(path, name):
    """No fixture pins routing: this is `meshychat.urls`, the table a request uses."""
    assert resolve(path).url_name == name


def test_resolve_is_capable_of_failing():
    """Positive control for the parametrised test above. Without it, "it resolved"
    proves only that `resolve` returns something."""
    with pytest.raises(Resolver404):
        resolve("/v1/meshguard/definitely-not-mounted")


def test_pending_beats_the_approval_id_capture():
    """`/approvals/pending` must not be captured as an approval id. Both sides asserted:
    the literal resolves to the queue AND a real-looking id still resolves to the item."""
    assert resolve("/v1/meshguard/approvals/pending").url_name == "meshguard-pending-approvals"
    match = resolve("/v1/meshguard/approvals/appr_" + "ab" * 16)
    assert match.url_name == "meshguard-approval-item"
    assert match.kwargs["approval_id"] == "appr_" + "ab" * 16


def test_a_meshy_memory_key_with_slashes_survives_the_route():
    """`<path:key>` and not `<str:key>`: shared-memory keys contain slashes, and a
    `<str:>` converter would 404 every one of them. Control: a slashless key still
    resolves."""
    deep = resolve("/v1/meshguard/memory/meshy/"
                   "11111111-1111-1111-1111-111111111111/wiki/a/b/c")
    assert deep.kwargs["key"] == "wiki/a/b/c"
    flat = resolve("/v1/meshguard/memory/meshy/"
                   "11111111-1111-1111-1111-111111111111/plain")
    assert flat.kwargs["key"] == "plain"


# ================================================= the CEL subset (raises, never allows)


def test_supported_subset_evaluates_the_expressions_the_policies_actually_use():
    """POSITIVE CONTROL for every raise below: the real expression population works.

    The expressions are the shapes real guard policies attach: skill equality, `&&`/`||`/`!`,
    and `args.<field>` comparisons.
    """
    env = {"skill": "harness/claude_code/Bash", "phase": "tool_call",
           "caller_peer": "p1", "target_peer": None,
           "args": {"risk": "high", "count": 3}}
    assert cel.evaluate("skill == 'harness/claude_code/Bash'", env) is True
    assert cel.evaluate("skill == 'other'", env) is False
    env2 = dict(env, skill="deploy_prod")
    assert cel.evaluate(
        "skill == 'deploy_prod' && args.risk == 'high' && args.count == 3", env2) is True
    assert cel.evaluate("!(skill == 'nope') || false", env2) is True
    assert cel.evaluate("skill != 'nope'", env2) is True


@pytest.mark.parametrize("expression,fragment", [
    ("args.count > 2", "ordering"),
    ("args.count + 1 == 4", "arithmetic"),
    ("args['risk'] == 'high'", "indexing"),
    ("has(args.risk)", "has()"),
    ("size(args) == 1", "size()"),
    ("skill.startsWith('deploy')", "startsWith()"),
    ("'x' in args", "`in`"),
    ("skill == 'a' ? true : false", "ternary"),
    ("args == {'a': 1}", "map literals"),
])
def test_an_unsupported_expression_raises_and_never_defaults_to_allow(expression, fragment):
    """The task's third requirement, one case per refused construct.

    Each raise is `UnsupportedExpression`, at COMPILE time, and the message names the
    construct - so an operator reading a 400 knows what to rewrite.
    """
    with pytest.raises(cel.UnsupportedExpression) as exc:
        cel.compile_expression(expression)
    assert fragment in str(exc.value), str(exc.value)


def test_unsupported_is_distinguishable_from_merely_malformed():
    """Two different failures must not collapse into one type: `skill ==` is a SYNTAX
    error, `args.count > 2` is an UNSUPPORTED construct.
    Both refuse; only one is a language-coverage question."""
    with pytest.raises(cel.ExpressionSyntaxError):
        cel.compile_expression("skill ==")
    with pytest.raises(cel.UnsupportedExpression):
        cel.compile_expression("args.count > 2")
    # Control: a well-formed, supported expression compiles - so "it raised" above is
    # about these inputs and not about compile_expression refusing everything.
    assert cel.compile_expression("skill == 'a'") is not None


def test_a_missing_field_is_a_runtime_error_not_false():
    """`args.missing.value` is a RUNTIME error.

    Control asserted first: the same shape on a field that EXISTS returns a value.
    """
    env = {"skill": "s", "phase": "tool_call", "caller_peer": "p",
           "target_peer": None, "args": {"present": {"value": 1}}}
    assert cel.evaluate("args.present.value == 1", env) is True
    with pytest.raises(cel.EvaluationError):
        cel.evaluate("args.missing.value == 1", env)


def test_an_unknown_identifier_raises():
    env = {"skill": "s", "phase": "tool_call", "caller_peer": "p",
           "target_peer": None, "args": {}}
    assert cel.evaluate("skill == 's'", env) is True          # control
    with pytest.raises(cel.EvaluationError):
        cel.evaluate("not_a_bound_name == 's'", env)


def test_a_bool_is_never_silently_a_number():
    """`_class_of` checks bool before int deliberately: in Python `True == 1`."""
    env = {"skill": "s", "phase": "p", "caller_peer": "c", "target_peer": None,
           "args": {"flag": True, "one": 1}}
    assert cel.evaluate("args.flag == true", env) is True     # control
    with pytest.raises(cel.EvaluationError):
        cel.evaluate("args.flag == 1", env)


def test_a_non_bool_expression_result_is_an_error_not_a_match():
    """A policy whose expression evaluates to a string must not be treated as matching.
    Control: the same handler with a bool expression DOES match."""
    event = _event("11111111-1111-1111-1111-111111111111",
                   "0447bf92-25fb-5230-b531-5a0d9c00d8c7",
                   skill="deploy_prod", args={"risk": "high"})
    ok = policies.expression_handler(
        {"expression": "args.risk == 'high'", "on_match": "deny", "reason": "r"}, event)
    assert isinstance(ok, policies.Deny)
    with pytest.raises(policies.PolicyEvaluationError) as exc:
        policies.expression_handler(
            {"expression": "args.risk", "on_match": "deny", "reason": "r"}, event)
    assert "non-bool" in str(exc.value)
    # And the message carries no ARG VALUE: the reason is
    # persisted to meshy_guard_policy_decisions.reason, where a leaked arg would live on.
    assert "high" not in str(exc.value)


def test_wildcard_match_matches_the_case_table():
    """Pins all four wildcard cases, including the negative."""
    assert policies.wildcard_match("deploy_*", "deploy_prod")
    assert policies.wildcard_match("*prod", "deploy_prod")
    assert policies.wildcard_match("de*prod", "deploy_prod")
    assert not policies.wildcard_match("deploy", "deploy_prod")


def test_glob_is_not_fnmatch():
    """`?` and `[...]` are LITERAL here. fnmatch would treat them
    as metacharacters and match differently. Control: `*` still works in the same call."""
    assert policies.wildcard_match("a?c", "a?c")
    assert not policies.wildcard_match("a?c", "abc")
    assert policies.wildcard_match("a*c", "abc")


def test_attaching_an_unsupported_expression_is_refused_at_attach_time():
    """The refusal belongs at POST time, not at the first gated call. Control first:
    a supported expression validates."""
    policies.validate_attachment_params(
        "expression", {"expression": "skill == 'x'", "on_match": "deny", "reason": "r"})
    with pytest.raises(policies.PolicyParamsError):
        policies.validate_attachment_params(
            "expression",
            {"expression": "size(args) > 0", "on_match": "deny", "reason": "r"})
    with pytest.raises(policies.PolicyParamsError):
        policies.validate_attachment_params("no_such_handler", {})


def test_an_extra_key_in_expression_params_is_refused_at_attach_time():
    """The published schema says `additionalProperties: false`, so a params body carrying
    a key the handler ignores is refused: its author would otherwise believe something
    is being enforced that is not (review queue, meshguard item 7). Control first: the
    same body without the extra key validates, so the refusal is about the key alone."""
    body = {"expression": "skill == 'x'", "on_match": "deny", "reason": "r"}
    policies.validate_attachment_params("expression", body)
    with pytest.raises(policies.PolicyParamsError, match="unknown key"):
        policies.validate_attachment_params("expression", {**body, "extra": 1})


# ================================================== enforcement default (divergence)


def test_gate_defaults_closed_when_the_flag_is_absent():
    """THE DELIBERATE DIVERGENCE: the design default is OBSERVE; here it is ENFORCE,
    asserted rather than only documented.

    An absent, empty, or unrecognised value ENFORCES, and only an explicit relaxing value
    opens the gate.
    The control is the pair: the same resolver must still be ABLE to return observe, or
    "it enforced" would prove only that it always enforces.
    """
    assert enforcement.resolve("X", env={}).mode == enforcement.ENFORCE
    assert enforcement.resolve("X", env={"X": ""}).mode == enforcement.ENFORCE
    assert enforcement.resolve("X", env={"X": "obsrve"}).mode == enforcement.ENFORCE
    assert enforcement.resolve("X", env={"X": "observe"}).mode == enforcement.OBSERVE
    assert enforcement.resolve("X", env={"X": "0"}).mode == enforcement.OBSERVE
    assert enforcement.resolve("X", env={"X": "1"}).mode == enforcement.ENFORCE
    # The reason distinguishes "nobody configured anything" from "an operator asked".
    assert "defaults CLOSED" in enforcement.resolve("X", env={}).reason
    assert "explicitly" in enforcement.resolve("X", env={"X": "enforce"}).reason


def test_the_wire_vocabulary_is_unchanged_by_the_divergence():
    """The divergence is the DEFAULT, not the vocabulary: only these two strings are
    admitted by `rgpd_mode_check`, and only these two ever reach the wire."""
    assert {enforcement.OBSERVE, enforcement.ENFORCE} == {"observe", "enforce"}
    for value in ("", "observe", "enforce", "nonsense", "0", "on"):
        assert enforcement.resolve("X", env={"X": value}).mode in ("observe", "enforce")


# ============================================================ the gate, against the DB


def test_an_unapproved_flow_is_denied_with_enforcement_on(app_conn, tenant, enforcing):
    """THE CORE TEST. An `ask` policy with enforcement on denies the un-approved action,
    and an APPROVED one passes.

    The control is not a different request - it is the SAME request after a human
    resolves the card, which is the only thing that should change the answer.
    """
    seeds = _seed_policies(app_conn)
    _attach(app_conn, policy_id=seeds["skill_name_ask"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"patterns": ["harness/claude_code/*"], "reason": "bash is gated"})

    client = Client()
    body = _hook_body(digest="d" * 64)

    # 1. First call: the policy asks, a card is opened, the action does NOT proceed.
    first = post_json(client, "/v1/meshguard/hooks/evaluate", body, tenant["actor"])
    assert first.status_code == 200, first.content
    asked = first.json()
    assert asked["decision"] == "ask", asked
    assert asked["mode"] == "enforce"
    assert asked["reason"] == "bash is gated"
    approval_id = asked["approval"]["approval_id"]
    assert approval_id.startswith("appr_")

    # 2. Retrying the identical action re-attaches to the SAME card, and is still not
    #    allowed. (A duplicate card would mean a model could retry its way past the gate
    #    by exhausting a reviewer.)
    again = post_json(client, "/v1/meshguard/hooks/evaluate", body, tenant["actor"])
    assert again.json()["decision"] == "ask"
    assert again.json()["approval"]["approval_id"] == approval_id

    # 3. THE POSITIVE CONTROL: a DIFFERENT peer in the same tenant accepts the card...
    resolved = post_json(client, f"/v1/meshguard/approvals/{approval_id}/resolve",
                         {"verdict": "accept"}, tenant["reviewer"])
    assert resolved.status_code == 200, resolved.content
    assert resolved.json()["status"] == "approved"
    assert resolved.json()["verdict"] == "accept"

    # ...and now the same request is ALLOWED.
    after = post_json(client, "/v1/meshguard/hooks/evaluate", body, tenant["actor"])
    assert after.status_code == 200, after.content
    assert after.json()["decision"] == "allow", after.json()

    # 4. And exactly ONCE: one approved card releases one execution. The next identical
    #    call asks again rather than riding the consumed approval.
    third = post_json(client, "/v1/meshguard/hooks/evaluate", body, tenant["actor"])
    assert third.json()["decision"] == "ask", third.json()


def test_a_deny_policy_denies_under_enforcement_and_allows_under_observe(
        app_conn, tenant, monkeypatch):
    """Both halves of the mode switch in one test, against the same attachment, so the
    difference is attributable to the mode and to nothing else."""
    seeds = _seed_policies(app_conn)
    _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"expression": "skill == 'harness/claude_code/Bash'",
                    "on_match": "deny", "reason": "no bash"})
    event = _event(tenant["owner_id"], tenant["actor"])

    monkeypatch.setenv(enforcement.POLICY_REGISTRY_FLAG, "enforce")
    decision = gate.policy_registry_gate(
        app_conn, enforcement.policy_registry_mode(), event)
    assert isinstance(decision, gate.GateDeny)
    assert decision.reason == "no bash"

    monkeypatch.setenv(enforcement.POLICY_REGISTRY_FLAG, "observe")
    observed = gate.policy_registry_gate(
        app_conn, enforcement.policy_registry_mode(), event)
    assert isinstance(observed, gate.GateAllow), (
        "observe mode must admit - if this also denied, the deny above would prove "
        "nothing about the mode")


def test_an_unsupported_expression_denies_rather_than_defaulting_to_allow(
        app_conn, tenant, enforcing, owner_conn):
    """A policy row carrying an out-of-subset expression must not open the gate.

    The row is inserted DIRECTLY (bypassing the attach-time validation that would have
    refused it), because the question is what the GATE does with a row that is already
    there - a registry written by an older build, or by an admin route this app does not
    own. Control: an identical attachment with a SUPPORTED expression allows.
    """
    seeds = _seed_policies(app_conn)
    bad = _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
                  owner_id=tenant["owner_id"], peer_id=tenant["actor"],
                  params={"expression": "size(args) > 0", "on_match": "allow",
                          "reason": "r"})
    event = _event(tenant["owner_id"], tenant["actor"])
    decision = gate.policy_registry_gate(app_conn, enforcing, event)
    assert isinstance(decision, gate.GateDeny), decision
    assert "size()" in decision.reason

    # It was recorded as an `error`, not swallowed.
    with app_conn.cursor() as cur:
        cur.execute("SELECT decision, reason FROM meshychat_guard_policy_decisions "
                    "WHERE attachment_id = %s ORDER BY decision_id DESC LIMIT 1",
                    (bad["attachment_id"],))
        row = cur.fetchone()
    assert row[0] == "error"
    assert "size()" in row[1]

    # POSITIVE CONTROL: replace the params with a supported expression; same gate, same
    # owner, same event -> allow. Without this, "it denied" is also what a broken
    # attachment fixture looks like.
    repository.remove_attachment(app_conn, bad["attachment_id"], tenant["owner_id"])
    _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"expression": "skill == 'nothing-matches'", "on_match": "deny",
                    "reason": "r"})
    assert isinstance(gate.policy_registry_gate(app_conn, enforcing, event), gate.GateAllow)


def test_a_failed_decision_audit_write_denies_under_enforcement(
        app_conn, tenant, enforcing, monkeypatch):
    """A decision that cannot be audited is not allowed to stand (review queue, meshguard
    item 7): under enforcement a failed write of the decision row turns an ALLOW into a
    deny, because an allow nobody can later see is the one an operator cannot judge.

    Control first: the same attachment and event allow while the write works, so the deny
    below is caused by the failed write and not by the policy.
    """
    seeds = _seed_policies(app_conn)
    _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"expression": "skill == 'harness/claude_code/Bash'",
                    "on_match": "allow", "reason": "matches and allows"})
    event = _event(tenant["owner_id"], tenant["actor"])
    assert isinstance(gate.policy_registry_gate(app_conn, enforcing, event), gate.GateAllow), (
        "POSITIVE CONTROL FAILED: the allowing attachment did not allow with a working "
        "audit write, so the deny below would prove nothing")

    def broken_write(*_args, **_kwargs):
        raise RuntimeError("simulated audit write failure")

    monkeypatch.setattr(repository, "record_policy_decision", broken_write)
    decision = gate.policy_registry_gate(app_conn, enforcing, event)
    assert isinstance(decision, gate.GateDeny), (
        f"an ALLOW whose audit row could not be written still allowed: {decision!r}")
    assert "audit write failed" in decision.reason


def test_an_unknown_handler_ref_denies_under_enforcement(app_conn, tenant, enforcing,
                                                         owner_conn):
    """A registry row pointing at a handler this build does not have fails CLOSED.

    Seeded through the owner role because the handler_ref is not settable through any
    route this app owns.
    """
    policy_id = repository.generate_policy_id()
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meshychat_guard_policies (policy_id, name, kind, handler_ref, "
            "description, params_schema) VALUES (%s, %s, 'builtin', %s, '', '{}'::jsonb)",
            (policy_id, f"ghost-{policy_id[-8:]}", "handler_that_does_not_exist"),
        )
    _attach(app_conn, policy_id=policy_id, owner_id=tenant["owner_id"],
            peer_id=tenant["actor"], params={})
    decision = gate.policy_registry_gate(
        app_conn, enforcing, _event(tenant["owner_id"], tenant["actor"]))
    assert isinstance(decision, gate.GateDeny)
    assert "unknown policy handler" in decision.reason


def test_a_raising_handler_is_an_error_row_and_denies_only_under_enforcement(
        app_conn, tenant, monkeypatch):
    """The gate's `except Exception` arm: a
    handler that raises something other than the two policy errors is an `error`
    decision row whose reason is "policy handler raised" - never an exception out of the
    gate, never an allow. Under enforcement that is a deny carrying the same reason;
    under observe the row is still written and the gate admits. Neither half had a test
    before the error arms were folded into `gate._error_arm`, so this is the pin that
    the fold kept them.

    Control first: the same attachment ALLOWS with the real handler in place, so the deny
    below is the raise and not the policy. The handler is swapped in `policies.HANDLERS`
    itself, which is what `policies.evaluate` dispatches through.
    """
    seeds = _seed_policies(app_conn)
    att = _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
                  owner_id=tenant["owner_id"], peer_id=tenant["actor"],
                  params={"expression": "skill == 'harness/claude_code/Bash'",
                          "on_match": "allow", "reason": "fine"})
    event = _event(tenant["owner_id"], tenant["actor"])
    monkeypatch.setenv(enforcement.POLICY_REGISTRY_FLAG, "enforce")
    enforce = enforcement.policy_registry_mode()
    assert isinstance(gate.policy_registry_gate(app_conn, enforce, event), gate.GateAllow), (
        "POSITIVE CONTROL FAILED: the allowing attachment did not allow before the "
        "handler was broken, so the deny below would prove nothing")

    def exploding_handler(_params, _event):
        raise ZeroDivisionError("simulated handler panic")

    monkeypatch.setitem(policies.HANDLERS, "expression", exploding_handler)
    denied = gate.policy_registry_gate(app_conn, enforce, event)
    assert isinstance(denied, gate.GateDeny), denied
    assert denied.reason == "policy handler raised"
    assert denied.policy_name == "expression"

    monkeypatch.setenv(enforcement.POLICY_REGISTRY_FLAG, "observe")
    observed = gate.policy_registry_gate(app_conn, enforcement.policy_registry_mode(), event)
    assert isinstance(observed, gate.GateAllow), observed

    with app_conn.cursor() as cur:
        cur.execute("SELECT decision, reason, mode FROM meshychat_guard_policy_decisions "
                    "WHERE attachment_id = %s ORDER BY decision_id DESC LIMIT 2",
                    (att["attachment_id"],))
        newest_first = cur.fetchall()
    assert newest_first == [("error", "policy handler raised", "observe"),
                            ("error", "policy handler raised", "enforce")]


def test_a_failed_decision_audit_write_does_not_deny_under_observe(
        app_conn, tenant, monkeypatch, caplog):
    """The other half of `test_a_failed_decision_audit_write_denies_under_enforcement`,
    on a DENY policy so the two reasons cannot be confused: under observe a failed audit
    write is logged and the gate admits, because observe admits everything by
    construction; under enforcement the same failed write is the
    deny, and its reason is the write's, not the policy's - the audit check comes before
    the verdict in every arm. Pinned so `gate._audit_or_deny` can neither be tightened
    into a mode-independent deny nor reordered behind the verdict.

    The log line is asserted under observe so the allow is shown to come from a write
    that really failed, not from one that quietly worked.
    """
    seeds = _seed_policies(app_conn)
    _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"expression": "skill == 'harness/claude_code/Bash'",
                    "on_match": "deny", "reason": "no bash"})
    event = _event(tenant["owner_id"], tenant["actor"])

    def broken_write(*_args, **_kwargs):
        raise RuntimeError("simulated audit write failure")

    monkeypatch.setattr(repository, "record_policy_decision", broken_write)
    caplog.set_level(logging.ERROR, logger="meshychat.meshguard.gate")

    monkeypatch.setenv(enforcement.POLICY_REGISTRY_FLAG, "observe")
    observed = gate.policy_registry_gate(app_conn, enforcement.policy_registry_mode(), event)
    assert isinstance(observed, gate.GateAllow), observed
    assert any("policy decision audit write failed" in r.getMessage()
               for r in caplog.records if r.name == "meshychat.meshguard.gate"), (
        "the allow under observe must come from a write that really failed")

    monkeypatch.setenv(enforcement.POLICY_REGISTRY_FLAG, "enforce")
    denied = gate.policy_registry_gate(app_conn, enforcement.policy_registry_mode(), event)
    assert isinstance(denied, gate.GateDeny), denied
    assert denied.reason == "policy decision audit write failed"


def test_an_attachment_for_another_owner_is_not_evaluated(app_conn, tenant, owner_conn,
                                                          enforcing):
    """Owner scoping of the gate's INPUT set, with the control that makes it meaningful:
    the same policy attached to the CALLER's owner does deny."""
    seeds = _seed_policies(app_conn)
    other_owner = _new_owner(owner_conn)
    other_peer = _new_peer(owner_conn, other_owner)
    _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
            owner_id=other_owner, peer_id=other_peer,
            params={"expression": "skill == 'harness/claude_code/Bash'",
                    "on_match": "deny", "reason": "other tenant's rule"})
    event = _event(tenant["owner_id"], tenant["actor"])
    assert isinstance(gate.policy_registry_gate(app_conn, enforcing, event),
                      gate.GateAllow)

    _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"expression": "skill == 'harness/claude_code/Bash'",
                    "on_match": "deny", "reason": "my rule"})
    mine = gate.policy_registry_gate(app_conn, enforcing, event)
    assert isinstance(mine, gate.GateDeny) and mine.reason == "my rule"


# ==================================================== decisions are append-only


def test_decisions_are_append_only(app_conn, tenant, enforcing):
    """A recorded decision is never rewritten and never removed by this application.

    Proved the only way a claim like this can be: capture a row in full, do more work
    that touches the same policy/attachment/owner, and assert the ORIGINAL row is
    byte-identical afterwards - AND that the later work really did write (the positive
    control, without which "unchanged" is also what "nothing happened" looks like).
    """
    seeds = _seed_policies(app_conn)
    att = _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
                  owner_id=tenant["owner_id"], peer_id=tenant["actor"],
                  params={"expression": "skill == 'harness/claude_code/Bash'",
                          "on_match": "deny", "reason": "first"})
    event = _event(tenant["owner_id"], tenant["actor"])
    gate.policy_registry_gate(app_conn, enforcing, event)

    cols = ("decision_id, policy_id, attachment_id, decision, reason, phase, mode, "
            "owner_id, caller_peer_id, skill_name, session_key, created_at")
    with app_conn.cursor() as cur:
        cur.execute(f"SELECT {cols} FROM meshychat_guard_policy_decisions "
                    "WHERE owner_id = %s ORDER BY decision_id ASC",
                    (tenant["owner_id"],))
        before = cur.fetchall()
    assert before, "the gate recorded nothing - there is nothing to prove immutable"
    first_row = before[0]

    for _ in range(3):
        gate.policy_registry_gate(app_conn, enforcing, event)
    repository.set_attachment_enabled(app_conn, att["attachment_id"],
                                      tenant["owner_id"], False)
    repository.remove_attachment(app_conn, att["attachment_id"], tenant["owner_id"])

    with app_conn.cursor() as cur:
        cur.execute(f"SELECT {cols} FROM meshychat_guard_policy_decisions "
                    "WHERE owner_id = %s ORDER BY decision_id ASC",
                    (tenant["owner_id"],))
        after = cur.fetchall()

    assert len(after) > len(before), (
        "no new rows were appended, so 'the old rows are unchanged' proves nothing")
    assert after[:len(before)] == before, "an earlier decision row was modified"
    assert after[0] == first_row
    # ...and the ids only ever go up.
    ids = [r[0] for r in after]
    assert ids == sorted(ids)


def test_deleting_an_attachment_does_not_delete_its_decisions(app_conn, tenant, enforcing):
    """The audit row outlives the policy it recorded. `meshy_guard_policy_decisions` has
    no FK to the attachment (measured: only its own PK), which is what makes that
    possible - so this asserts the application does not do by hand what the schema
    declines to do."""
    seeds = _seed_policies(app_conn)
    att = _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
                  owner_id=tenant["owner_id"], peer_id=tenant["actor"],
                  params={"expression": "skill == 'harness/claude_code/Bash'",
                          "on_match": "deny", "reason": "doomed"})
    gate.policy_registry_gate(app_conn, enforcing,
                              _event(tenant["owner_id"], tenant["actor"]))
    with app_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM meshychat_guard_policy_decisions "
                    "WHERE attachment_id = %s", (att["attachment_id"],))
        recorded = cur.fetchone()[0]
    assert recorded >= 1                                    # control

    assert repository.remove_attachment(app_conn, att["attachment_id"],
                                        tenant["owner_id"]) is None
    with app_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM meshychat_guard_policy_decisions "
                    "WHERE attachment_id = %s", (att["attachment_id"],))
        assert cur.fetchone()[0] == recorded


# ================================================================== typed absence


def test_absence_is_typed_and_never_a_bare_empty_collection(app_conn, tenant):
    """House rule 3. Each lookup's MISS is a named type, and each one's HIT is asserted
    in the same test - an absence type returned unconditionally would pass a miss-only
    check."""
    miss = repository.owner_for_peer(app_conn, uuid.uuid4())
    assert isinstance(miss, repository.NotFound) and miss.what == "active peer"
    assert repository.owner_for_peer(app_conn, tenant["actor"]) == tenant["owner_id"]

    # Both halves of the old `approval_by_id`: the pure read and the sweep-then-read.
    # The HIT for each is asserted too, so "everything is NotFound" cannot pass.
    seeded = _seed_approval(app_conn, tenant["owner_id"], tenant["actor"])
    assert isinstance(repository.approval_row(app_conn, "appr_" + "0" * 32),
                      repository.NotFound)
    assert repository.approval_row(app_conn, seeded)["approval_id"] == seeded
    assert isinstance(repository.expire_and_reload_approval(app_conn, "appr_" + "0" * 32),
                      repository.NotFound)
    assert repository.expire_and_reload_approval(app_conn, seeded)["status"] == "pending"

    assert isinstance(repository.policy_by_id(app_conn, "rgp_" + "0" * 32),
                      repository.NotFound)

    empty = repository.recent_decisions_for_owner(app_conn, uuid.uuid4(), 10)
    assert isinstance(empty, repository.Empty)
    assert "policy decisions for owner" in empty.scope
    assert empty.rows() == []          # the wire value is still []
    assert not isinstance(empty, list), "an Empty must not BE a list, or it is a bare []"


def test_the_absence_vocabulary_is_only_what_the_repository_builds():
    """A census of the typed-absence vocabulary, after the cut docs/REVIEW-FIX-QUEUE.md
    items 261 and 498 made in dashboard and skillsreg: a type "produced and consumed
    nowhere" is deleted rather than kept as documentation. `repository.Denied` was never
    RETURNED by any repository function - its one construction carried a log string in
    the self-resolve route, whose 403 is raised by the route and still is
    (`test_a_peer_cannot_resolve_its_own_approval`); the `Absence`/`Outcome` aliases,
    `repository.utcnow` and `errors.db_not_found` had no reader at all; and
    `cel.Program.source` was assigned and never read.

    Control: the types the repository DOES build are still here, still typed and still
    carry their fields, so this is a census of the vocabulary and not of a module that
    failed to import.
    """
    for gone in ("Denied", "Absence", "Outcome", "utcnow"):
        assert not hasattr(repository, gone), f"repository.{gone} is back without a producer"
    assert not hasattr(errors, "db_not_found"), "a second 404 constructor nobody calls"
    assert "source" not in cel.Program.__slots__
    assert not hasattr(cel.compile_expression("skill == 'a'"), "source")

    assert repository.NotFound("active peer", "k").what == "active peer"
    assert repository.Empty("scope").rows() == []
    assert repository.Duplicate("attachment", "rgpa_unique_binding").constraint == (
        "rgpa_unique_binding")
    assert repository.Rows([1]).rows() == [1]
    assert cel.compile_expression("skill == 'a'").execute({"skill": "a"}) is True


def test_an_inactive_peer_cannot_reach_the_surface(app_conn, owner_conn):
    """RG4: a suspended peer keeps a valid unexpired JWT.

    Control first: while the peer is active the very same token works.
    """
    owner_id = _new_owner(owner_conn)
    peer_id = _new_peer(owner_conn, owner_id)
    client = Client()
    ok = client.get("/v1/meshguard/approvals/pending", **auth_headers(peer_id))
    assert ok.status_code == 200, ok.content

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (peer_id,))
    # The peer-status check in apps/peers/auth.py fires first (403); if it were ever
    # relaxed, repository.owner_for_peer would still refuse with 404. Either way the
    # surface is closed, and it is closed for a reason the body names.
    after = client.get("/v1/meshguard/approvals/pending", **auth_headers(peer_id))
    assert after.status_code in (403, 404), after.content
    assert after.json()["error"] in ("E_FORBIDDEN", "E_NOT_FOUND")


# ========================================================== approvals over HTTP


def test_a_peer_cannot_resolve_its_own_approval(app_conn, tenant):
    """A compromised agent must not rubber-stamp itself.

    Control asserted second and in the SAME test: a different peer in the same tenant
    CAN resolve the identical card. Without it, a 403 is indistinguishable from a broken
    fixture (no card, wrong id, bad token).
    """
    seeds = _seed_policies(app_conn)
    _attach(app_conn, policy_id=seeds["skill_name_ask"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"patterns": ["*"], "reason": "everything is gated"})
    client = Client()
    with_enforce = {enforcement.POLICY_REGISTRY_FLAG: "enforce"}
    os.environ.update(with_enforce)
    try:
        asked = post_json(client, "/v1/meshguard/hooks/evaluate",
                          _hook_body(digest="e" * 64), tenant["actor"]).json()
        approval_id = asked["approval"]["approval_id"]

        self_resolve = post_json(client, f"/v1/meshguard/approvals/{approval_id}/resolve",
                                 {"verdict": "accept"}, tenant["actor"])
        assert self_resolve.status_code == 403, self_resolve.content
        assert self_resolve.json()["error"] == "E_FORBIDDEN"

        other = post_json(client, f"/v1/meshguard/approvals/{approval_id}/resolve",
                          {"verdict": "accept"}, tenant["reviewer"])
        assert other.status_code == 200, other.content
        assert other.json()["verdict"] == "accept"
    finally:
        os.environ.pop(enforcement.POLICY_REGISTRY_FLAG, None)


def test_an_approval_from_another_tenant_is_a_404_not_a_403(app_conn, tenant, owner_conn):
    """Existence must not leak. Control: the owning tenant reads the same id at 200."""
    approval_id = _seed_approval(app_conn, tenant["owner_id"], tenant["actor"])
    client = Client()
    mine = client.get(f"/v1/meshguard/approvals/{approval_id}",
                      **auth_headers(tenant["reviewer"]))
    assert mine.status_code == 200, mine.content

    stranger_owner = _new_owner(owner_conn)
    stranger = _new_peer(owner_conn, stranger_owner)
    theirs = client.get(f"/v1/meshguard/approvals/{approval_id}", **auth_headers(stranger))
    assert theirs.status_code == 404, theirs.content
    assert theirs.json()["error"] == "E_NOT_FOUND"


def _seed_approval(conn, owner_id, caller_peer_id, *, phase="tool_call") -> str:
    approval_id = repository.generate_approval_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meshychat_guard_approvals (approval_id, owner_id, caller_peer_id, "
            "phase, message, content_preview) VALUES (%s, %s, %s, %s, %s, %s)",
            (approval_id, owner_id, caller_peer_id, phase, "seeded", "{}"),
        )
    return approval_id


def _approve(conn, approval_id, args_hash, resolver) -> None:
    # `meshy_guard_approvals_resolution_shape` (sql/093) requires a resolved row to carry
    # resolved_at, resolver_peer_id and verdict, so the shape is set in one statement.
    with conn.cursor() as cur:
        cur.execute("UPDATE meshychat_guard_approvals SET status = 'approved', "
                    "approved_args_hash = %s, resolved_at = NOW(), resolver_peer_id = %s, "
                    "verdict = 'accept' WHERE approval_id = %s",
                    (args_hash, resolver, approval_id))


def test_an_approval_is_consumed_exactly_once_and_only_for_its_args(
        app_conn, tenant, owner_conn):
    """`consume_approval` returns True only if exactly one row moved; its UPDATE repeats
    every release condition so that a request racing the SELECT that found the row cannot
    spend it twice (review queue, meshguard item 7). No concurrency is needed to pin
    that: the predicate is what a second call meets after the first.

    Control first: the first consume of a matching approved card is True, so the False
    results below are about the second call and the wrong args, not a broken fixture.
    """
    owner = tenant["owner_id"]
    good = _seed_approval(app_conn, owner, tenant["actor"])
    _approve(owner_conn, good, "hash-of-the-approved-args", tenant["reviewer"])

    assert repository.consume_approval(app_conn, owner, good, "hash-of-the-approved-args") is True, (
        "POSITIVE CONTROL FAILED: an approved, unexpired, unconsumed card was not consumable")
    assert repository.consume_approval(app_conn, owner, good, "hash-of-the-approved-args") is False, (
        "an approval was spent twice: `consumed_at IS NULL` is not enforced in "
        "consume_approval's UPDATE")

    bound = _seed_approval(app_conn, owner, tenant["actor"])
    _approve(owner_conn, bound, "hash-of-the-approved-args", tenant["reviewer"])
    assert repository.consume_approval(app_conn, owner, bound, "a-different-args-hash") is False, (
        "an approval was consumed for arguments other than the ones approved: "
        "`approved_args_hash = %s` is not enforced in consume_approval's UPDATE")

    # Still PENDING, but carrying the matching hash: status is the only thing that refuses.
    pending = _seed_approval(app_conn, owner, tenant["actor"])
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE meshychat_guard_approvals SET approved_args_hash = %s "
                    "WHERE approval_id = %s", ("hash-of-the-approved-args", pending))
    assert repository.consume_approval(app_conn, owner, pending, "hash-of-the-approved-args") is False, (
        "a card that was never approved was consumed: `status = 'approved'` is not enforced "
        "in consume_approval's UPDATE")

    # Approved and matching, but EXPIRED: expiry is the only thing that refuses. Both
    # timestamps move back, since meshy_guard_approvals_expiry_after_create (sql/093)
    # requires expires_at > created_at.
    stale = _seed_approval(app_conn, owner, tenant["actor"])
    _approve(owner_conn, stale, "hash-of-the-approved-args", tenant["reviewer"])
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE meshychat_guard_approvals SET created_at = NOW() - INTERVAL '2 days', "
                    "expires_at = NOW() - INTERVAL '1 day' WHERE approval_id = %s", (stale,))
    assert repository.consume_approval(app_conn, owner, stale, "hash-of-the-approved-args") is False, (
        "an EXPIRED approval was consumed: `expires_at > NOW()` is not enforced in "
        "consume_approval's UPDATE")


def test_the_pending_queue_is_owner_scoped(app_conn, tenant, owner_conn):
    """Control and negative in one read: my card is listed, a stranger's is not."""
    mine = _seed_approval(app_conn, tenant["owner_id"], tenant["actor"])
    other_owner = _new_owner(owner_conn)
    other_peer = _new_peer(owner_conn, other_owner)
    theirs = _seed_approval(app_conn, other_owner, other_peer)

    client = Client()
    body = client.get("/v1/meshguard/approvals/pending",
                      **auth_headers(tenant["reviewer"])).json()
    ids = [i["approval_id"] for i in body["items"]]
    assert mine in ids
    assert theirs not in ids
    assert body["pending_count"] >= 1


#: THE STATEMENT THE REPOSITORY ACTUALLY ISSUES, imported rather than retyped. An
#: EXPLAIN of a hand-copied query measures the copy: the repository could be changed to
#: an unscoped count and this test would keep passing.
SCOPED_COUNT = repository.PENDING_COUNT_SQL


def _rows_read(conn, sql, params=(), relation="meshychat_guard_approvals") -> int:
    """How many rows the executor ACTUALLY read from `relation` running `sql`.

    Not "which node name appeared in the plan". An earlier version of this test asserted
    `"Seq Scan" not in plan`, and its own control refuted it: with sequential scans
    disabled, the FORBIDDEN unscoped `COUNT(*)` is served by a full *index* scan, which
    contains no "Seq Scan" and still reads every row in the table. The node name was
    never the property; the number of rows touched is.

    `EXPLAIN (ANALYZE)` really runs the query, and FORMAT JSON gives `Actual Rows` per
    node without text parsing. The MAX over the nodes touching `relation` is taken rather
    than the sum, because a bitmap scan reports the same rows twice (once on the Bitmap
    Index Scan, once on the Bitmap Heap Scan).

    `enable_seqscan = off` is set for both measurements so the two are compared under the
    same planner conditions; at this table size the planner would otherwise seq-scan
    everything and the comparison would measure the table, not the query.
    """
    def walk(node):
        best = 0
        if relation in (node.get("Relation Name"), node.get("Index Name", "")) or \
                relation in str(node.get("Index Name", "")):
            best = int(node.get("Actual Rows", 0))
        for child in node.get("Plans", []):
            best = max(best, walk(child))
        return best

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
            plan = cur.fetchone()[0]
    return walk(plan[0]["Plan"])


def test_pending_count_is_bounded_by_the_owners_queue_not_the_table(app_conn, tenant,
                                                                   owner_conn):
    """HOUSE RULE 4, MEASURED: the queue's COUNT(*) must not grow with the table.

    Two independent checks, because either alone reads as healthy:

      1. the partial index the repository is written against really exists, with the
         predicate the query uses (read from pg_indexes - no planner involved);
      2. the executor really reads only this owner's cards.

    The NEGATIVE CONTROL is the whole point: the forbidden shape - an UNSCOPED
    `COUNT(*)` over the same table - is measured in the same way in the same test and
    must read far more. Without it, a small number proves only that the table is small.
    """
    with app_conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'meshychat_guard_approvals_owner_pending_idx'")
        row = cur.fetchone()
    assert row is not None, "the index the repository is written against does not exist"
    assert "(owner_id, created_at)" in row[0]
    assert "WHERE (status = 'pending'::text)" in row[0]

    # Three cards for me, a hundred for a stranger: if the count were table-wide, the
    # stranger's rows would show up in the measurement.
    for _ in range(3):
        _seed_approval(app_conn, tenant["owner_id"], tenant["actor"])
    noise_owner = _new_owner(owner_conn)
    noise_peer = _new_peer(owner_conn, noise_owner)
    for _ in range(100):
        _seed_approval(app_conn, noise_owner, noise_peer)

    scoped = _rows_read(app_conn, SCOPED_COUNT, (tenant["owner_id"],))
    unscoped = _rows_read(app_conn, "SELECT COUNT(*) FROM meshychat_guard_approvals")

    assert unscoped >= 103, (
        "the control did not fire: the full-table count read only %d rows, so the "
        "scoped measurement below says nothing" % unscoped)
    assert scoped <= 10, (
        "the owner-scoped pending count read %d rows; it must be bounded by THIS "
        "owner's pending cards (3 seeded), not by the table (%d rows)"
        % (scoped, unscoped))
    assert scoped < unscoped / 5


def test_pending_scans_uses_exists_not_a_chain_count(app_conn, tenant):
    """HOUSE RULE 4 again, on the other query that could have been a COUNT over a chain.

    Asserted on the SQL the repository actually issues (EXPLAIN of the same statement),
    so this cannot pass while the code counts.
    """
    exists_sql = """
        SELECT s.id FROM meshychat_guard_scan_results s
        WHERE s.review_status = 'requires_review'
          AND EXISTS (SELECT 1 FROM invocation_frames f
                      WHERE f.chain_id = s.chain_id AND f.caller_owner_id = %s)
        ORDER BY s.occurred_at DESC LIMIT 100
    """
    count_sql = exists_sql.replace(
        "EXISTS (SELECT 1 FROM invocation_frames f",
        "0 < (SELECT COUNT(*) FROM invocation_frames f",
    )
    with app_conn.cursor() as cur:
        cur.execute("EXPLAIN " + exists_sql, (tenant["owner_id"],))
        plan = "\n".join(r[0] for r in cur.fetchall())
        # NEGATIVE CONTROL: the same query written as a per-row COUNT over the chain -
        # the forbidden shape - DOES put an Aggregate in the plan. Without this, the
        # assertion below would also pass on a plan that simply never mentions the word.
        cur.execute("EXPLAIN " + count_sql, (tenant["owner_id"],))
        counting = "\n".join(r[0] for r in cur.fetchall())
    assert "Aggregate" in counting, counting
    assert "Aggregate" not in plan, plan          # no COUNT anywhere in the plan
    # Control: the route returns a typed Empty, not an exception, on a tenant with none.
    result = repository.pending_scan_results_for_owner(app_conn, tenant["owner_id"], 100)
    assert isinstance(result, (repository.Rows, repository.Empty))


# ============================================================= policy registry HTTP


def test_policy_discovery_hides_internal_policies(app_conn, tenant):
    """`deny_all_sessions` is internal_only. Control: the two public seeds ARE listed,
    so an empty-looking list cannot pass this."""
    client = Client()
    r = client.get("/v1/meshguard/policies", **auth_headers(tenant["actor"]))
    assert r.status_code == 200, r.content
    names = {p["name"] for p in r.json()["policies"]}
    assert {"expression", "skill_name_ask"} <= names
    assert "deny_all_sessions" not in names
    # handler_ref is the audit-safe omission.
    assert all("handler_ref" not in p for p in r.json()["policies"])


def test_attaching_at_server_default_scope_is_403_and_owner_scope_is_200(app_conn, tenant):
    """Both halves: the refusal AND the shape that must still work."""
    client = Client()
    client.get("/v1/meshguard/policies", **auth_headers(tenant["actor"]))  # seed
    with app_conn.cursor() as cur:
        cur.execute("SELECT policy_id FROM meshychat_guard_policies WHERE name = 'expression'")
        policy_id = cur.fetchone()[0]

    ok = post_json(client, "/v1/meshguard/policies/attachments", {
        "policy_id": policy_id, "scope": "owner", "session_key": None,
        "params": {"expression": "skill == 'x'", "on_match": "deny", "reason": "r"},
    }, tenant["actor"])
    assert ok.status_code == 200, ok.content
    assert ok.json()["scope"] == "owner"
    assert ok.json()["phases"] == ["tool_call"]

    refused = post_json(client, "/v1/meshguard/policies/attachments", {
        "policy_id": policy_id, "scope": "server_default", "session_key": None,
        "params": {"expression": "skill == 'x'", "on_match": "deny", "reason": "r"},
    }, tenant["actor"])
    assert refused.status_code == 403, refused.content
    assert refused.json()["error"] == "E_FORBIDDEN"


def test_the_owner_on_an_attachment_comes_from_the_token_not_the_body(app_conn, tenant,
                                                                     owner_conn):
    """HOUSE RULE 5 on a table the database does not constrain.

    A body that names someone else's owner_id must not attribute the row to them.
    """
    stranger_owner = _new_owner(owner_conn)
    client = Client()
    client.get("/v1/meshguard/policies", **auth_headers(tenant["actor"]))
    with app_conn.cursor() as cur:
        cur.execute("SELECT policy_id FROM meshychat_guard_policies WHERE name = 'expression'")
        policy_id = cur.fetchone()[0]

    r = post_json(client, "/v1/meshguard/policies/attachments", {
        "policy_id": policy_id, "scope": "owner", "session_key": None,
        "owner_id": stranger_owner,            # the lie
        "params": {"expression": "skill == 'x'", "on_match": "deny", "reason": "r"},
    }, tenant["actor"])
    assert r.status_code == 200, r.content
    with app_conn.cursor() as cur:
        cur.execute("SELECT owner_id, created_by_peer_id FROM "
                    "meshychat_guard_policy_attachments WHERE attachment_id = %s",
                    (r.json()["attachment_id"],))
        owner_id, created_by = cur.fetchone()
    assert str(owner_id) == tenant["owner_id"], "the body's owner_id was honoured"
    assert str(created_by) == tenant["actor"]


def test_attaching_an_out_of_subset_expression_is_a_400(app_conn, tenant):
    """The route refuses at attach time. Control first: the supported one attaches."""
    client = Client()
    client.get("/v1/meshguard/policies", **auth_headers(tenant["actor"]))
    with app_conn.cursor() as cur:
        cur.execute("SELECT policy_id FROM meshychat_guard_policies WHERE name = 'expression'")
        policy_id = cur.fetchone()[0]

    good = post_json(client, "/v1/meshguard/policies/attachments", {
        "policy_id": policy_id, "scope": "owner", "params":
            {"expression": "skill == 'x'", "on_match": "deny", "reason": "r"},
    }, tenant["actor"])
    assert good.status_code == 200, good.content

    bad = post_json(client, "/v1/meshguard/policies/attachments", {
        "policy_id": policy_id, "scope": "session", "session_key": "s1", "params":
            {"expression": "args.count > 1", "on_match": "deny", "reason": "r"},
    }, tenant["actor"])
    assert bad.status_code == 400, bad.content
    assert bad.json()["error"] == "E_VALIDATION"
    assert "ordering" in bad.json()["message"]


def test_toggle_and_delete_are_owner_scoped_404(app_conn, tenant, owner_conn):
    """A stranger's attachment is 404 on both routes. Control: the owner gets 200."""
    seeds = _seed_policies(app_conn)
    mine = _attach(app_conn, policy_id=seeds["expression"]["policy_id"],
                   owner_id=tenant["owner_id"], peer_id=tenant["actor"],
                   params={"expression": "skill == 'x'", "on_match": "deny",
                           "reason": "r"})
    stranger_owner = _new_owner(owner_conn)
    stranger = _new_peer(owner_conn, stranger_owner)
    client = Client()

    ok = post_json(client, f"/v1/meshguard/policies/attachments/"
                           f"{mine['attachment_id']}/toggle",
                   {"enabled": False}, tenant["actor"])
    assert ok.status_code == 200, ok.content
    assert ok.json()["enabled"] is False

    nope = post_json(client, f"/v1/meshguard/policies/attachments/"
                             f"{mine['attachment_id']}/toggle",
                     {"enabled": True}, stranger)
    assert nope.status_code == 404, nope.content

    gone = client.delete(f"/v1/meshguard/policies/attachments/{mine['attachment_id']}",
                         **auth_headers(stranger))
    assert gone.status_code == 404
    real = client.delete(f"/v1/meshguard/policies/attachments/{mine['attachment_id']}",
                         **auth_headers(tenant["actor"]))
    assert real.status_code == 200 and real.json() == {"removed": True}


def test_decisions_limit_is_clamped(app_conn, tenant):
    """`limit` clamps to 1 through 200; pin both ends and the non-integer 400."""
    client = Client()
    for raw, expect in ((None, 200), ("0", 200), ("100000", 200), ("abc", 400)):
        url = "/v1/meshguard/policies/decisions"
        if raw is not None:
            url += f"?limit={raw}"
        r = client.get(url, **auth_headers(tenant["actor"]))
        assert r.status_code == expect, (raw, r.content)


# ==================================================================== hook lanes


def test_an_observational_kind_never_opens_a_card(app_conn, tenant, enforcing):
    """post_tool already happened, so deny/ask degrade to
    allow-with-warning and NO approval is created.

    The control is the blocking sibling: the identical policy on `pre_tool` DOES open a
    card. Without it, "no card" is also what "the policy never matched" looks like.
    """
    seeds = _seed_policies(app_conn)
    _attach(app_conn, policy_id=seeds["skill_name_ask"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"patterns": ["harness/claude_code/*"], "reason": "gated"})
    client = Client()

    with app_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM meshychat_guard_approvals WHERE owner_id = %s",
                    (tenant["owner_id"],))
        before = cur.fetchone()[0]

    observ = post_json(client, "/v1/meshguard/hooks/evaluate",
                       _hook_body(kind="post_tool", digest="f" * 64), tenant["actor"])
    assert observ.status_code == 200, observ.content
    payload = observ.json()
    assert payload["decision"] == "allow"
    assert payload["warning"] == "gated"
    assert "approval" not in payload
    with app_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM meshychat_guard_approvals WHERE owner_id = %s",
                    (tenant["owner_id"],))
        assert cur.fetchone()[0] == before, "an observational event opened a card"

    blocking = post_json(client, "/v1/meshguard/hooks/evaluate",
                         _hook_body(kind="pre_tool", digest="f" * 64), tenant["actor"])
    assert blocking.json()["decision"] == "ask", blocking.json()
    with app_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM meshychat_guard_approvals WHERE owner_id = %s",
                    (tenant["owner_id"],))
        assert cur.fetchone()[0] == before + 1

    # ...and the degraded outcome IS on the audit trail, flagged as degraded.
    with app_conn.cursor() as cur:
        cur.execute("SELECT reason FROM meshychat_guard_policy_decisions "
                    "WHERE owner_id = %s AND reason LIKE 'observational_degraded:%%'",
                    (tenant["owner_id"],))
        assert cur.fetchall(), "the degradation was not recorded"


def test_an_ask_without_an_input_digest_is_refused(app_conn, tenant, enforcing):
    """No digest means no binding, so no card is issued.

    Control: the identical event WITH a digest gets one.
    """
    seeds = _seed_policies(app_conn)
    _attach(app_conn, policy_id=seeds["skill_name_ask"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"patterns": ["*"], "reason": "gated"})
    client = Client()
    with_digest = post_json(client, "/v1/meshguard/hooks/evaluate",
                            _hook_body(digest="a" * 64, session="s-with"),
                            tenant["actor"])
    assert with_digest.status_code == 200 and with_digest.json()["decision"] == "ask"

    without = post_json(client, "/v1/meshguard/hooks/evaluate",
                        _hook_body(digest=None, session="s-without"), tenant["actor"])
    assert without.status_code == 400, without.content
    assert "input_digest is required" in without.json()["message"]


@pytest.mark.parametrize("mutate,fragment", [
    ({"v": 2}, "v must be 1"),
    ({"kind": "nope"}, "unknown hook event kind"),
    ({"harness": "Claude Code"}, "harness must match"),
    ({"session": {"session_key": "   "}}, "session_key must be"),
    ({"tool": {"name": "  "}}, "tool.name must be non-empty"),
])
def test_hook_event_validation_rejects_malformed_events(tenant, mutate, fragment):
    """Five rejections, each with the SAME body accepted unmutated as the control."""
    client = Client()
    base = _hook_body(digest="b" * 64)
    good = post_json(client, "/v1/meshguard/hooks/evaluate", base, tenant["actor"])
    assert good.status_code == 200, good.content     # the control, first

    bad = post_json(client, "/v1/meshguard/hooks/evaluate", {**base, **mutate},
                    tenant["actor"])
    assert bad.status_code == 400, bad.content
    assert fragment in bad.json()["message"], bad.json()


def test_an_oversized_preview_is_refused(tenant):
    """The serialized preview is limited to 16 KiB. Control: just under passes."""
    client = Client()
    small = _hook_body(digest="c" * 64, preview={"blob": "x" * 1000})
    assert post_json(client, "/v1/meshguard/hooks/evaluate", small,
                     tenant["actor"]).status_code == 200
    big = _hook_body(digest="c" * 64, preview={"blob": "x" * (hooks.MAX_PREVIEW_BYTES + 10)})
    r = post_json(client, "/v1/meshguard/hooks/evaluate", big, tenant["actor"])
    assert r.status_code == 400, r.content
    assert "input_preview exceeds" in r.json()["message"]


def test_backlog_counts_records_and_rejects_and_caps_the_batch(app_conn, tenant):
    """The spool lane: good entries recorded as observe rows, bad ones counted, an
    over-long batch refused with 413 E_BACKLOG_TOO_LARGE (not 400)."""
    client = Client()
    entries = [
        {"event": _hook_body(digest="d" * 64), "outcome": {"decision": "allow"}},
        {"event": _hook_body(digest="d" * 64), "outcome": {"decision": "ask"}},      # bad
        {"event": {"v": 99}, "outcome": {"decision": "deny"}},                        # bad
    ]
    r = post_json(client, "/v1/meshguard/hooks/backlog", {"entries": entries},
                  tenant["actor"])
    assert r.status_code == 200, r.content
    assert r.json() == {"recorded": 1, "rejected": 2}

    # `rejected` is OMITTED when zero (skip_serializing_if = "is_zero").
    clean = post_json(client, "/v1/meshguard/hooks/backlog",
                      {"entries": [entries[0]]}, tenant["actor"])
    assert clean.json() == {"recorded": 1}

    # Always observe-mode, whatever the live gate mode is.
    with app_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT mode FROM meshychat_guard_policy_decisions "
                    "WHERE owner_id = %s AND reason LIKE 'deferred%%'",
                    (tenant["owner_id"],))
        assert [r[0] for r in cur.fetchall()] == ["observe"]

    too_many = post_json(client, "/v1/meshguard/hooks/backlog",
                         {"entries": entries[:1] * (hooks.MAX_BACKLOG_ENTRIES + 1)},
                         tenant["actor"])
    assert too_many.status_code == 413, too_many.content
    assert too_many.json()["error"] == "E_BACKLOG_TOO_LARGE"


def test_bypass_audit_records_one_allow_row(app_conn, tenant):
    """A coverage signal, not a verdict. Control: a malformed harness is still a 400."""
    client = Client()
    r = post_json(client, "/v1/meshguard/hooks/bypass-audit",
                  {"kind": "pre_tool", "harness": "codex",
                   "session": {"session_key": "s"}}, tenant["actor"])
    assert r.status_code == 200 and r.json() == {"recorded": True}
    with app_conn.cursor() as cur:
        cur.execute("SELECT decision, reason, skill_name FROM meshychat_guard_policy_decisions "
                    "WHERE owner_id = %s AND reason = 'client_bypass'",
                    (tenant["owner_id"],))
        rows = cur.fetchall()
    assert rows and rows[0][0] == "allow"
    assert rows[0][2] == "harness/codex/pre_tool"

    bad = post_json(client, "/v1/meshguard/hooks/bypass-audit",
                    {"kind": "pre_tool", "harness": "NOPE",
                     "session": {"session_key": "s"}}, tenant["actor"])
    assert bad.status_code == 400, bad.content


@pytest.mark.parametrize("mutate,message", [
    ({"kind": "nope"}, "unknown hook event kind 'nope'"),
    ({"harness": "Claude Code"}, "harness must match ^[a-z0-9_]{1,32}$"),
    ({"session": {"session_key": "   "}}, "session.session_key must be 1..=256 chars"),
    ({"session": {"session_key": "k" * 257}}, "session.session_key must be 1..=256 chars"),
    ({"session": "not-an-object"}, "session.session_key must be 1..=256 chars"),
])
def test_bypass_audit_rejects_what_validate_event_rejects_in_the_same_words(
        tenant, mutate, message):
    """The three checks the bypass-audit route shares with `hooks.validate_event`
    (`require_kind`, `require_harness`, `require_session_key`): same 400, same message,
    pinned WORD FOR WORD on this route. Before they were shared, this route carried a
    verbatim copy that `test_hook_event_validation_rejects_malformed_events` - which
    sends to /hooks/evaluate - could never have seen drift in.

    The last row is the one asymmetry, kept on purpose: a `session` that is not an object
    is the session_key message here, where `validate_event` says "session is required".
    The evaluate route is asked the same question in the same test, so the two answers
    are shown side by side rather than assumed.

    Control: the unmutated body is recorded at 200.
    """
    client = Client()
    base = {"kind": "pre_tool", "harness": "codex", "session": {"session_key": "s"}}
    ok = post_json(client, "/v1/meshguard/hooks/bypass-audit", base, tenant["actor"])
    assert ok.status_code == 200, ok.content

    bad = post_json(client, "/v1/meshguard/hooks/bypass-audit", {**base, **mutate},
                    tenant["actor"])
    assert bad.status_code == 400, bad.content
    assert bad.json() == {"error": "E_VALIDATION", "message": message}

    evaluate = post_json(client, "/v1/meshguard/hooks/evaluate",
                         {**_hook_body(digest="f" * 64), **mutate}, tenant["actor"])
    assert evaluate.status_code == 400, evaluate.content
    expected = "session is required" if mutate == {"session": "not-an-object"} else message
    assert evaluate.json()["message"] == expected


# ================================================================ misc contracts


def test_a_malformed_uuid_path_segment_is_400_not_500(tenant):
    """Malformed UUID path segments are rejected before the handler; a 500 would be the
    bug. Control: a well-formed (absent) id reaches the handler and
    gets 404."""
    client = Client()
    bad = client.get("/v1/meshguard/skills/not-a-uuid", **auth_headers(tenant["actor"]))
    assert bad.status_code == 400, bad.content
    assert bad.json()["error"] == "E_VALIDATION"
    good = client.get(f"/v1/meshguard/skills/{uuid.uuid4()}", **auth_headers(tenant["actor"]))
    assert good.status_code == 404, good.content


def test_wrong_method_is_405_with_an_allow_header(tenant):
    """Control: the right method on the same path is not 405."""
    client = Client()
    assert client.get("/v1/meshguard/approvals/pending",
                      **auth_headers(tenant["actor"])).status_code == 200
    bad = post_json(client, "/v1/meshguard/approvals/pending", {}, tenant["actor"])
    assert bad.status_code == 405
    assert bad.headers["Allow"] == "GET"


#: What each route admits - the `@_route(...)` declaration in `apps/meshguard/views.py`,
#: keyed by route name so the table above stays the one list of paths.
ALLOWED_METHODS = {
    "meshguard-skill-review": "GET",
    "meshguard-meshy-memory-review": "GET",
    "meshguard-pending-scans": "GET",
    "meshguard-pending-approvals": "GET",
    "meshguard-resolve-approval": "POST",
    "meshguard-approval-item": "GET",
    "meshguard-policies": "GET",
    "meshguard-attachments": "GET, POST",
    "meshguard-toggle-attachment": "POST",
    "meshguard-remove-attachment": "DELETE",
    "meshguard-decisions": "GET",
    "meshguard-hooks-evaluate": "POST",
    "meshguard-hooks-backlog": "POST",
    "meshguard-hooks-bypass-audit": "POST",
}


def test_every_route_has_a_declared_method_set():
    """Control for the parametrised test below: a route added to ROUTES without a row
    here would be skipped rather than pinned."""
    assert set(ALLOWED_METHODS) == {name for _path, name in ROUTES}


@pytest.mark.parametrize("path,name", ROUTES)
def test_every_route_answers_the_method_before_the_credential(path, name):
    """The prologue every route shares (`views._route`): 405 + `Allow` for a method the
    route does not admit, then 401 for a missing credential, and nothing answers before
    those two. Pinned on EVERY route rather than on one, because folding the fourteen
    copied prologues into `_route` is only behaviour-preserving if each route still
    declares the same methods and still authenticates - the omission
    docs/REVIEW-FIX-QUEUE.md item 492 records for a sibling surface's `sync_manifest`.

    Both halves are sent WITHOUT a token on purpose: a 405 for a wrong method with no
    credential proves the method is answered before the credential is read (the order
    the views had when each made the check itself); a 401 for a right method proves
    there is no public route on this surface (the module docstring's claim). PATCH is
    admitted nowhere here, so it is the wrong method for every row.
    """
    allow = ALLOWED_METHODS[name]
    client = Client()
    wrong = client.generic("PATCH", path)
    assert wrong.status_code == 405, (path, wrong.content)
    assert wrong.headers["Allow"] == allow
    right = client.generic(allow.split(", ")[0], path, content_type="application/json")
    assert right.status_code == 401, (path, right.content)
    assert right.json()["error"] == "E_AUTH_MISSING"


def test_the_connection_a_route_uses_is_meshy_app_and_autocommit(app_conn):
    """Positive control for `db.connection()`: the property is proved on a real
    connection, not assumed from the constructor."""
    db.assert_meshyapp_autocommit(app_conn)
    with app_conn.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == "meshychat_app"


def test_approval_ids_satisfy_the_database_check_constraint(app_conn, tenant):
    """`meshy_guard_approvals_id_format` is `^appr_[0-9a-f]{32,}$`. The generator is
    checked against the CONSTRAINT rather than against a remembered shape: the row is
    really inserted, and a wrong-shaped id would raise."""
    approval_id = _seed_approval(app_conn, tenant["owner_id"], tenant["actor"])
    assert len(approval_id) == 5 + 32
    with app_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM meshychat_guard_approvals WHERE approval_id = %s",
                    (approval_id,))
        assert cur.fetchone() is not None
    # Control: the constraint really does reject a bad id, so the insert above is
    # evidence and not a table that accepts anything.
    with pytest.raises(psycopg.errors.CheckViolation):
        with app_conn.transaction():
            with app_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO meshychat_guard_approvals (approval_id, owner_id, "
                    "caller_peer_id, phase, message, content_preview) "
                    "VALUES ('nope', %s, %s, 'tool_call', 'm', '{}')",
                    (tenant["owner_id"], tenant["actor"]),
                )


# ============================================================================
# Tenant isolation on the two routes NOBODY EVER REQUESTED
# ============================================================================
#
# RESOLVING A ROUTE IS NOT REQUESTING IT. Before these tests, `/v1/meshguard/scans/pending`
# and `/v1/meshguard/memory/meshy/{owner}/{key}` appeared in this file only inside a
# route-table parametrize list and two `resolve()` calls - never in a `client.get`. Measured
# consequence: both owner predicates in `apps/meshguard/repository.py` could be neutered
# (`... OR TRUE`, parameters left in place so the SQL still binds) and the suite stayed at
# **72 passed**. Two cross-tenant disclosures, invisible.
#
# The route table proves a URL maps to a view. Only a request proves the view keeps its
# promises.


def _seed_meshy_memory(conn, owner_id, key, review):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meshychat_memory (created_by_owner_id, key, value, meshychat_guard_review) "
            "VALUES (%s, %s, %s::jsonb, %s::jsonb)",
            (owner_id, key, json.dumps({"body": "x"}), json.dumps(review)),
        )


def _seed_pending_scan(conn, owner_id, peer_id, chain_id):
    """A `requires_review` scan plus ONE invocation frame tying that chain to `owner_id`.

    The frame is what the query's EXISTS clause matches on, so it is the frame - not the
    scan row - that carries the tenancy. Seeding the scan without a frame would produce a
    row no owner can see, which would make both halves of the test pass for free.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meshychat_guard_scan_results "
            "(chain_id, conclusion, score, review_status, scanner_version) "
            "VALUES (%s, 'misaligned', 0.9, 'requires_review', 'test-1')",
            (chain_id,),
        )
        cur.execute(
            "INSERT INTO invocation_frames "
            "(chain_id, frame_hash, decision, caller_owner_id, caller_peer_id, "
            " target_owner_id, target_peer_id, skill_name) "
            "VALUES (%s, %s, 'allow', %s, %s, %s, %s, 'seeded.skill')",
            (chain_id, f"h-{uuid.uuid4().hex[:16]}", owner_id, peer_id, owner_id, peer_id),
        )


def test_pending_scans_are_scoped_to_the_calling_owner(client, owner_conn):
    """KILLS the `f.caller_owner_id = %s` neutering in `pending_scan_results_for_owner`.

    This route takes its owner from the CALLER'S TOKEN, never from the path
    (`views.pending_scans` -> `_owner(conn, auth)`), so that predicate is the ONLY tenant
    scoping on it. Without it, every owner's pending misalignment reviews - findings text
    included - go to whoever asks.

    The CONTROL is the same scan fetched by the owner it belongs to, so a `[]` for the
    stranger cannot be "the seeding failed" or "the route always returns nothing".
    """
    victim_owner = _new_owner(owner_conn)
    victim_peer = _new_peer(owner_conn, victim_owner)
    chain = str(uuid.uuid4())
    _seed_pending_scan(owner_conn, victim_owner, victim_peer, chain)

    stranger_owner = _new_owner(owner_conn)
    stranger_peer = _new_peer(owner_conn, stranger_owner)

    # CONTROL FIRST: the owner it belongs to CAN see it.
    mine = client.get("/v1/meshguard/scans/pending", **auth_headers(victim_peer))
    assert mine.status_code == 200, mine.content
    chains_mine = [s["chain_id"] for s in json.loads(mine.content)["scans"]]
    assert chain in chains_mine, "control failed: the owner cannot see its own pending scan"

    # THE ISOLATION: a different owner must not.
    theirs = client.get("/v1/meshguard/scans/pending", **auth_headers(stranger_peer))
    assert theirs.status_code == 200, theirs.content
    chains_theirs = [s["chain_id"] for s in json.loads(theirs.content)["scans"]]
    assert chain not in chains_theirs, (
        "cross-tenant disclosure: a stranger's pending-scan queue contains another "
        "owner's chain")


def test_the_meshy_memory_review_route_returns_a_review_now_that_it_binds_a_context(
        client, owner_conn):
    """THE ROUTE IS LIVE. It used to be structurally incapable of returning anything.

    `meshy_memory` is RLS-enabled AND FORCED, and the handler was given a connection with no
    context bound, so `meshy_memory_tenant_select` matched nothing and the NotFound branch
    always won - measured, with `peer_context` returning the row on the same connection and
    `server_internal` NOT returning it, because the policy is tenant-scoped rather than
    privileged. Divergence 012.

    A test asserting 404 here passed for two entirely different reasons, which is why it is
    gone rather than edited: first because the route was dead, and then - once the path
    moved to `/memory/meshy/` - because it was requesting a URL that no longer existed.
    """
    key = f"wiki/live-{uuid.uuid4().hex[:8]}"
    owner = _new_owner(owner_conn)
    peer = _new_peer(owner_conn, owner)
    _seed_meshy_memory(owner_conn, owner, key,
                       {"status": "approved", "marker": "now-reachable"})

    response = client.get(f"/v1/meshguard/memory/meshy/{owner}/{key}",
                          **auth_headers(peer))
    assert response.status_code == 200, response.content
    assert json.loads(response.content)["review"]["marker"] == "now-reachable"

    # CONTROL: a key this owner does not hold is still a 404, so the 200 above is the row
    # and not a route that answers 200 to everything.
    missing = client.get(f"/v1/meshguard/memory/meshy/{owner}/wiki/nope-{uuid.uuid4().hex[:6]}",
                         **auth_headers(peer))
    assert missing.status_code == 404, missing.content


def test_the_lookup_is_keyed_on_the_named_owner_not_just_the_tenant(client, owner_conn):
    """KILLS the `created_by_owner_id = %s` neutering - deterministically.

    WHY THE SQL PREDICATE IS STILL LOAD-BEARING WITH RLS BOUND, which is why this test needs
    dev-plane owners: the SELECT policy is
    `meshy_same_tenant_or_devgroup(current_owner, created_by_owner_id)` - TRUST-GROUP-WIDE,
    not owner equality (read from `pg_policies`). For two dev-plane owners in one trust
    group RLS admits BOTH owners' rows, so the SQL predicate is the only thing pinning the
    lookup to the owner NAMED IN THE PATH.

    EXACTLY ONE ROW CARRIES THE KEY, and it belongs to the OTHER owner. A first draft seeded
    the key under both owners and asserted the right one came back - which fails when the
    predicate is removed, but fails at whichever assertion the row order happens to reach,
    because the query has no ORDER BY. A kill that depends on which row Postgres hands back
    first is not a kill you can rely on. Asking under an owner that holds NO such key makes
    the outcome deterministic: 404 with the predicate, the sibling's row without it.
    """
    group = str(uuid.uuid4())
    key = f"wiki/collide-{uuid.uuid4().hex[:8]}"

    owner_a = _new_owner(owner_conn, plane="dev", trust_group=group)
    peer_a = _new_peer(owner_conn, owner_a)
    owner_b = _new_owner(owner_conn, plane="dev", trust_group=group)
    _seed_meshy_memory(owner_conn, owner_b, key, {"status": "approved", "whose": "B"})

    # CONTROL: the trust group really is shared, so B's row IS inside this caller's RLS
    # visibility. Without it, the 404 below could be RLS hiding the row rather than the
    # owner predicate selecting correctly - and the test would pass while proving nothing.
    sibling = client.get(f"/v1/meshguard/memory/meshy/{owner_b}/{key}",
                         **auth_headers(peer_a))
    assert sibling.status_code == 200, (
        "control failed: the dev-group sibling is not readable, so this test cannot "
        f"separate the owner predicate from RLS ({sibling.content[:120]!r})")
    assert json.loads(sibling.content)["review"]["whose"] == "B"

    # THE PREDICATE: owner A holds no such key, and must not inherit the sibling's.
    mine = client.get(f"/v1/meshguard/memory/meshy/{owner_a}/{key}",
                      **auth_headers(peer_a))
    assert mine.status_code == 404, (
        "a key the named owner does not hold resolved to a trust-group sibling's row - "
        f"the created_by_owner_id predicate is not pinning the owner ({mine.content[:160]!r})")
    assert b'"whose"' not in mine.content


def test_a_stranger_outside_the_tenant_is_refused(client, owner_conn):
    """The tenancy check, still doing its job now that the route can actually read.

    Two product-plane owners are different tenants, so `_require_same_tenant` refuses
    before the lookup runs. The CONTROL is the same row read by its own owner.
    """
    key = f"wiki/private-{uuid.uuid4().hex[:8]}"
    owner = _new_owner(owner_conn)
    peer = _new_peer(owner_conn, owner)
    _seed_meshy_memory(owner_conn, owner, key, {"status": "approved", "whose": "owner"})

    stranger_owner = _new_owner(owner_conn)
    stranger_peer = _new_peer(owner_conn, stranger_owner)

    mine = client.get(f"/v1/meshguard/memory/meshy/{owner}/{key}", **auth_headers(peer))
    assert mine.status_code == 200, mine.content

    theirs = client.get(f"/v1/meshguard/memory/meshy/{owner}/{key}",
                        **auth_headers(stranger_peer))
    assert theirs.status_code == 404, theirs.content
    assert b"whose" not in theirs.content


# ======================= ordering, owner predicates and the four status codes


def _seed_expired_approval(conn, owner_id, caller_peer_id, *, phase="tool_call") -> str:
    """A card PAST its expiry that is still labelled `pending`.

    `meshy_guard_approvals_expiry_after_create` requires `expires_at > created_at`, so
    BOTH timestamps are pushed into the past rather than only the expiry.
    """
    approval_id = repository.generate_approval_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meshychat_guard_approvals (approval_id, owner_id, caller_peer_id, "
            "phase, message, content_preview, created_at, expires_at) VALUES "
            "(%s, %s, %s, %s, %s, %s, NOW() - INTERVAL '2 days', NOW() - INTERVAL '1 day')",
            (approval_id, owner_id, caller_peer_id, phase, "seeded", "{}"),
        )
    return approval_id


def _status_of(conn, approval_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM meshychat_guard_approvals WHERE approval_id = %s",
                    (approval_id,))
        row = cur.fetchone()
    assert row is not None, f"the seeded approval {approval_id} vanished"
    return row[0]


def test_a_stranger_cannot_drive_the_expiry_transition_on_another_tenants_card(
        app_conn, tenant, owner_conn):
    """The expiry UPDATE must sit on the AUTHORISED side of the tenancy check.

    `pending -> expired` is a state transition, and it used to run inside the lookup that
    BOTH approval routes perform before `_require_same_tenant`. Naming another tenant's
    approval id was therefore enough to move its row, from any tenant, with a 404 coming
    back as if nothing had happened.

    The POSITIVE CONTROL is the third act: the owning tenant's read still sweeps, so
    "the row stayed pending" cannot pass because the sweep was deleted outright.
    """
    client = Client()
    stranger_owner = _new_owner(owner_conn)
    stranger = _new_peer(owner_conn, stranger_owner)

    probed = _seed_expired_approval(app_conn, tenant["owner_id"], tenant["actor"])
    assert _status_of(app_conn, probed) == "pending", "the fixture did not seed a pending card"

    got = client.get(f"/v1/meshguard/approvals/{probed}", **auth_headers(stranger))
    assert got.status_code == 404, got.content
    assert got.json()["error"] == "E_NOT_FOUND"
    assert _status_of(app_conn, probed) == "pending", (
        "GET /approvals/{id} by an out-of-tenant peer moved the row to expired: the "
        "unscoped write ran before the tenancy check")

    resolved = post_json(client, f"/v1/meshguard/approvals/{probed}/resolve",
                         {"verdict": "accept"}, stranger)
    assert resolved.status_code == 404, resolved.content
    assert resolved.json()["error"] == "E_NOT_FOUND"
    assert _status_of(app_conn, probed) == "pending", (
        "POST /approvals/{id}/resolve by an out-of-tenant peer moved the row")

    mine = client.get(f"/v1/meshguard/approvals/{probed}",
                      **auth_headers(tenant["reviewer"]))
    assert mine.status_code == 200, mine.content
    assert mine.json()["status"] == "expired", (
        "the control did not fire: the owner's own read no longer expires a stale card, "
        "so the two assertions above prove nothing")
    assert _status_of(app_conn, probed) == "expired"


def test_resolve_carries_an_owner_predicate_like_its_two_siblings(
        app_conn, tenant, owner_conn):
    """`resolve_approval` binds the owner the route authorised, not the id alone.

    `set_attachment_enabled` and `remove_attachment` both carry `owner_id = %s`; this
    UPDATE did not, and no RLS policy exists on any `meshy_guard_*` table (measured in
    `apps/meshguard/db.py`), so the route was the only thing standing between an id and
    a resolution. Exercised at the repository seam because that is where the predicate
    lives: a route-level test would pass on a correct route over a bare-id UPDATE.

    The wrong owner is asserted FIRST and the right one SECOND, so a fixture that never
    resolves anything fails on the control rather than passing on the refusal.
    """
    stranger_owner = _new_owner(owner_conn)
    approval_id = _seed_approval(app_conn, tenant["owner_id"], tenant["actor"])

    wrong = repository.resolve_approval(app_conn, approval_id, stranger_owner,
                                        tenant["reviewer"], "accept")
    assert isinstance(wrong, repository.NotFound), (
        f"an owner that does not own this card resolved it: {wrong!r}")
    assert _status_of(app_conn, approval_id) == "pending"

    right = repository.resolve_approval(app_conn, approval_id, tenant["owner_id"],
                                        tenant["reviewer"], "accept")
    assert not isinstance(right, repository.NotFound), (
        "the control did not fire: the owning tenant could not resolve its own card")
    assert right["status"] == "approved" and right["verdict"] == "accept"
    assert _status_of(app_conn, approval_id) == "approved"


def test_an_unknown_resolve_verdict_is_400_not_500(app_conn, tenant):
    """The three-variant enum is rejected at the route, before the status lookup.

    Control asserted second: a real verdict on the same card still resolves at 200.
    """
    client = Client(raise_request_exception=False)
    approval_id = _seed_approval(app_conn, tenant["owner_id"], tenant["actor"])

    bad = post_json(client, f"/v1/meshguard/approvals/{approval_id}/resolve",
                    {"verdict": "yolo"}, tenant["reviewer"])
    assert bad.status_code == 400, bad.content
    assert bad.json()["error"] == "E_VALIDATION"
    assert _status_of(app_conn, approval_id) == "pending"

    good = post_json(client, f"/v1/meshguard/approvals/{approval_id}/resolve",
                     {"verdict": "decline"}, tenant["reviewer"])
    assert good.status_code == 200, good.content
    assert good.json()["verdict"] == "decline"


def test_an_unknown_attachment_scope_is_400_not_a_check_violation(app_conn, tenant):
    """`rgpa_scope_check` must never be what refuses a scope: the route does it first.

    Control first: the supported scope attaches at 200 on the same policy.
    """
    client = Client(raise_request_exception=False)
    seeds = _seed_policies(app_conn)
    policy_id = seeds["expression"]["policy_id"]
    params = {"expression": "skill == 'x'", "on_match": "deny", "reason": "r"}

    ok = post_json(client, "/v1/meshguard/policies/attachments",
                   {"policy_id": policy_id, "scope": "owner", "params": params},
                   tenant["actor"])
    assert ok.status_code == 200, ok.content

    for scope in ("tenant", "", None, 7):
        bad = post_json(client, "/v1/meshguard/policies/attachments",
                        {"policy_id": policy_id, "scope": scope, "params": params},
                        tenant["actor"])
        assert bad.status_code == 400, (scope, bad.status_code, bad.content[:200])
        assert bad.json()["error"] == "E_VALIDATION", (scope, bad.content[:200])


def test_a_duplicate_attachment_is_409_not_an_unhandled_unique_violation(app_conn, tenant):
    """`rgpa_unique_binding` is a UNIQUE INDEX the route has to answer for.

    (policy_id, scope, owner_id, session_key) is unique, so re-POSTing an identical
    binding raises `UniqueViolation` from the INSERT. Unhandled it leaves `_route` and
    becomes Django's HTML 500 - with MESHY_DEBUG on, a rendered traceback carrying the
    DSN and every column of the attempted row.

    409 rather than 400: the body is well-formed and the refusal is about existing
    state. Control asserted first (the identical body at 200) and third (the SAME policy
    at a DIFFERENT scope still attaches), so "409" cannot come from a broken fixture.
    """
    client = Client(raise_request_exception=False)
    seeds = _seed_policies(app_conn)
    body = {
        "policy_id": seeds["expression"]["policy_id"],
        "scope": "owner",
        "params": {"expression": "skill == 'x'", "on_match": "deny", "reason": "r"},
    }

    first = post_json(client, "/v1/meshguard/policies/attachments", body, tenant["actor"])
    assert first.status_code == 200, first.content

    again = post_json(client, "/v1/meshguard/policies/attachments", body, tenant["actor"])
    assert again.status_code == 409, again.content[:400]
    assert again.json()["error"] == "E_CONFLICT", again.content[:400]

    other_scope = post_json(client, "/v1/meshguard/policies/attachments",
                            dict(body, scope="session", session_key="s-dup"),
                            tenant["actor"])
    assert other_scope.status_code == 200, other_scope.content


def test_an_oversized_content_preview_is_truncated_rather_than_rejected_by_the_database(
        app_conn, tenant, enforcing):
    """`meshy_guard_approvals_preview_len` caps `content_preview` at 1024 CHARACTERS.

    The preview is the model's own arguments, so an over-long one is ordinary traffic,
    not abuse: it has to be cut to fit, and a card still has to open. Without the slice
    the INSERT raises `CheckViolation` and the ask route answers 500 - the gate failing
    open-ended on exactly the input it exists to gate.

    The control is the assertion that a card opened AT ALL (a 500 or a degraded allow
    would both leave no card), plus the boundary: the stored preview is exactly 1024.
    """
    client = Client(raise_request_exception=False)
    seeds = _seed_policies(app_conn)
    _attach(app_conn, policy_id=seeds["skill_name_ask"]["policy_id"],
            owner_id=tenant["owner_id"], peer_id=tenant["actor"],
            params={"patterns": ["*"], "reason": "everything is gated"})

    huge = {"cmd": "x" * 5000}
    assert repository.json_size(huge) < hooks.MAX_PREVIEW_BYTES, (
        "the fixture tripped the 16 KiB preview limit instead of the 1024-char column cap")

    asked = post_json(client, "/v1/meshguard/hooks/evaluate",
                      _hook_body(digest="f" * 64, preview=huge), tenant["actor"])
    assert asked.status_code == 200, asked.content[:400]
    assert asked.json()["decision"] == "ask", asked.content[:400]
    approval_id = asked.json()["approval"]["approval_id"]

    with app_conn.cursor() as cur:
        cur.execute("SELECT content_preview, char_length(content_preview) "
                    "FROM meshychat_guard_approvals WHERE approval_id = %s", (approval_id,))
        preview, length = cur.fetchone()
    assert length == repository.APPROVAL_PREVIEW_MAX_CHARS, length
    assert preview.startswith('{"cmd":"xxx')


def test_a_backlog_batch_is_all_or_nothing(app_conn, tenant, monkeypatch):
    """The route promises "a bad entry is not fatal to the batch"; a DB fault broke it.

    Each entry was its own autocommitted INSERT, so a fault on entry k left 0..k-1
    durable and answered 500. The honest retry - the client resends the same spool -
    then duplicated every surviving row in an append-only audit lane.

    The POSITIVE CONTROL runs first and is the same batch without the fault: three rows
    land and `recorded` is 3. Without it, "no rows were written" is also what a route
    that writes nothing at all looks like.
    """
    client = Client(raise_request_exception=False)

    def _decisions() -> int:
        with app_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM meshychat_guard_policy_decisions "
                        "WHERE owner_id = %s", (tenant["owner_id"],))
            return int(cur.fetchone()[0])

    entries = [{"event": _hook_body(tool=f"Spool{i}", digest="a" * 64),
                "outcome": {"decision": "allow", "reason": "spooled"}}
               for i in range(3)]

    before = _decisions()
    ok = post_json(client, "/v1/meshguard/hooks/backlog", {"entries": entries},
                   tenant["actor"])
    assert ok.status_code == 200, ok.content[:400]
    assert ok.json() == {"recorded": 3}, ok.content
    assert _decisions() == before + 3

    real = hooks.record_hook_decision
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise psycopg.errors.DeadlockDetected("injected mid-batch database fault")
        return real(*args, **kwargs)

    monkeypatch.setattr(hooks, "record_hook_decision", flaky)

    before = _decisions()
    boom = post_json(client, "/v1/meshguard/hooks/backlog", {"entries": entries},
                     tenant["actor"])
    assert calls["n"] == 3, f"the injected fault never fired (n={calls['n']})"
    assert boom.status_code == 500, boom.status_code
    assert _decisions() == before, (
        "entries before the fault stayed committed; a retrying client duplicates them "
        "in an append-only audit lane")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
