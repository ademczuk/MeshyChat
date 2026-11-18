"""An owner's tier and per-capability overrides, set by command: `manage.py set_owner_tier`
and `manage.py bootstrap_owner --tier`. Divergence 028 (T).

THE GAP, MEASURED 2026-09-23. `bootstrap_owner` creates the operator's owner on the default
`free` tier (the dashboard_users.tier column default; sql/013 seeds free peers_per_owner = 5),
and minting a node reserves one `peers_per_owner` unit (apps/authapi/onboard.py), so a
self-hosted mesh stops at five nodes per owner. Nothing but SQL could change an owner's tier
or add an `owner_tier_overrides` row.

Every refusal sits beside the control one variable away (docs/V1-DESIGN-RULES.md C1), and
every change is read back from the database rather than from what the command said it did.

Run:  .venv/Scripts/python.exe -m pytest tests/test_owner_tier.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import uuid

import pytest

from tests.test_onboard import (  # noqa: F401 - fixtures are used by name; sets up Django
    DSN,
    _fresh_limiters,
    _pub_pem,
    mint_code,
    outcome,
    owner_conn,
    owner_sign_in,
    peer_count,
    redeem,
    server,
)
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.test import Client  # noqa: E402


def _bootstrap(tmp_path, *extra) -> dict:
    """`bootstrap_owner` as the runbook runs it (an additional owner: the run's database
    already has owners), returning the owner and a signer that can sign in as it."""
    seed = os.urandom(32)
    pub = tmp_path / f"op-{uuid.uuid4().hex[:6]}.pub"
    pub.write_bytes(_pub_pem(seed))
    out = io.StringIO()
    call_command("bootstrap_owner", "--email", f"tier-{uuid.uuid4().hex[:8]}@example.test",
                 "--public-key-file", str(pub), "--dsn", DSN, "--additional-owner", *extra,
                 "--peer-name", f"op-{uuid.uuid4().hex[:8]}",   # divergence 065: a name is an address
                 stdout=out, stderr=io.StringIO())
    made = json.loads(out.getvalue())
    return {**made, "seed": seed}


def _signed_in(owner: dict) -> Client:
    client = Client()
    r, _ = owner_sign_in(client, {"peer_id": owner["peer_id"], "seed": owner["seed"]})
    assert r.status_code == 200, r.content
    return client


def _set_tier(owner_id, *args) -> dict:
    out = io.StringIO()
    call_command("set_owner_tier", "--owner", str(owner_id), "--dsn", DSN, *args, stdout=out)
    return json.loads(out.getvalue())


def _tier(conn, owner_id) -> str:
    return conn.execute("SELECT tier FROM dashboard_users WHERE id = %s",
                        (owner_id,)).fetchone()[0]


def _overrides(conn, owner_id) -> dict:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT capability, value, note FROM owner_tier_overrides WHERE owner_id = %s",
        (owner_id,)).fetchall()}


def _used(conn, owner_id, n: int) -> None:
    """Record `n` peers_per_owner units already reserved this cycle, as
    tests/test_onboard.py's cap test does, where the path under test is not the minting."""
    first_of_month = dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    conn.execute(
        "INSERT INTO owner_usage (owner_id, capability, cycle_start, counter) "
        "VALUES (%s, 'peers_per_owner', %s, %s) ON CONFLICT (owner_id, capability, "
        "cycle_start) DO UPDATE SET counter = EXCLUDED.counter",
        (owner_id, first_of_month, n))


# --------------------------------------------------------------------------- #
# The measured gap, closed by the command
# --------------------------------------------------------------------------- #

def test_a_bootstrapped_owner_is_refused_at_five_peers_until_set_owner_tier_lifts_it(
        server, owner_conn, tmp_path, capsys):
    owner = _bootstrap(tmp_path)
    assert _tier(owner_conn, owner["owner_id"]) == "free"
    client = _signed_in(owner)
    for n in range(5):
        r, _ = redeem(mint_code(client))
        assert r.status_code == 201, (n, r.content)
    code = mint_code(client)
    r, seed = redeem(code)
    assert outcome(r) == (402, "E_QUOTA_EXCEEDED")
    assert r.json()["message"] == "quota exceeded for peers_per_owner: 6 would exceed cap 5"
    assert peer_count(owner_conn, owner["owner_id"]) == 6, "the signer and five nodes"

    shown = _set_tier(owner["owner_id"], "--tier", "enterprise")
    assert _tier(owner_conn, owner["owner_id"]) == "enterprise"
    assert shown["tier"] == "enterprise"
    assert shown["caps"]["peers_per_owner"] == {"value": -1, "source": "tier", "current": 5}
    assert shown["not_enforced"] == ["calls_per_day", "calls_per_cycle", "concurrent_calls",
                                       "local_memory_keys", "meshy_memory_keys"]
    assert set(shown) == {"owner_id", "tier", "caps", "not_enforced"}
    assert capsys.readouterr().err.endswith(
        "caps enforced by this server for peers_per_owner (mint), file_bytes (file finalize) "
        "and log_retention_days_max (dashboard); calls_per_day, calls_per_cycle, "
        "concurrent_calls, local_memory_keys, meshy_memory_keys are stored and settable, "
        "nothing enforces them\n"
    )

    # The SAME code, unconsumed by the refusal, now redeems.
    r, _ = redeem(code, seed)
    assert r.status_code == 201, r.content
    assert peer_count(owner_conn, owner["owner_id"]) == 7


def test_an_override_lifts_one_capability_and_clearing_it_restores_the_tier_cap(
        server, owner_conn, tmp_path):
    owner = _bootstrap(tmp_path)
    oid = owner["owner_id"]
    client = _signed_in(owner)
    _used(owner_conn, oid, 5)
    code = mint_code(client)
    r, seed = redeem(code)
    assert outcome(r) == (402, "E_QUOTA_EXCEEDED")

    shown = _set_tier(oid, "--override", "peers_per_owner=6", "--note", "self-hosted mesh")
    assert _overrides(owner_conn, oid) == {"peers_per_owner": (6, "self-hosted mesh")}
    assert _tier(owner_conn, oid) == "free", "an override changes one capability, not the tier"
    assert shown["caps"]["peers_per_owner"] == {"value": 6, "source": "override", "current": 5}
    assert shown["caps"]["file_bytes"]["source"] == "tier"
    r, _ = redeem(code, seed)
    assert r.status_code == 201, r.content
    r, _ = redeem(mint_code(client))
    assert outcome(r) == (402, "E_QUOTA_EXCEEDED"), "the override is a cap, not unlimited"
    assert r.json()["message"] == "quota exceeded for peers_per_owner: 7 would exceed cap 6"

    # Re-setting replaces the value (one row per capability), and a note is optional.
    _set_tier(oid, "--override", "peers_per_owner=7")
    assert _overrides(owner_conn, oid) == {"peers_per_owner": (7, None)}

    shown = _set_tier(oid, "--clear-override", "peers_per_owner")
    assert _overrides(owner_conn, oid) == {}
    assert shown["caps"]["peers_per_owner"] == {"value": 5, "source": "tier", "current": 6}


def test_set_owner_tier_refuses_what_it_cannot_name_and_writes_nothing(owner_conn, tmp_path):
    owner = _bootstrap(tmp_path)
    oid = owner["owner_id"]
    refusals = [
        (("--tier", "platinum"), "enterprise"),               # names the valid tiers
        (("--override", "peers_per_ownr=9"), "peers_per_owner"),  # names the capabilities
        (("--override", "peers_per_owner=nine"), "integer"),
        (("--override", "peers_per_owner=-2"), "-1"),
        (("--override", "peers_per_owner"), "capability=value"),
        (("--clear-override", "peers_per_owner"), "no override"),
        (("--tier", "pro", "--override", "file_bytes=1"), "not allowed with"),
        ((), "one of the arguments"),
        (("--note", "orphan", "--tier", "pro"), "--note"),
    ]
    for args, why in refusals:
        with pytest.raises(CommandError, match=why):
            _set_tier(oid, *args)
        assert _tier(owner_conn, oid) == "free" and _overrides(owner_conn, oid) == {}, args
    with pytest.raises(CommandError, match="no owner"):
        _set_tier(uuid.uuid4(), "--tier", "pro")
    with pytest.raises(CommandError, match="no database named"):
        _no_dsn(oid)
    # Controls, each one variable from a refusal above.
    assert _set_tier(oid, "--tier", "pro")["tier"] == "pro"
    assert _tier(owner_conn, oid) == "pro"
    _set_tier(oid, "--override", "file_bytes=-1")
    assert _overrides(owner_conn, oid) == {"file_bytes": (-1, None)}


def test_an_override_value_is_plain_decimal_digits_within_the_bigint_column(owner_conn,
                                                                           tmp_path):
    """Review finding C1-b. The value was parsed with `int()`, which also takes a sign,
    `_` separators and any Unicode decimal digit (fullwidth, Arabic-Indic), and a value
    past the BIGINT column escaped as an untyped database error, a traceback rather than
    the command's "refused" exit. One spelling now: optional `-`, ASCII digits, within
    BIGINT. Each refusal writes nothing; the two controls are the widest values it keeps."""
    owner = _bootstrap(tmp_path)
    oid = owner["owner_id"]
    bigint_max = 2 ** 63 - 1
    for raw in ("+5", "1_000", "\uff15", "\u0663", "5\u0663", str(bigint_max + 1)):
        with pytest.raises(CommandError, match="refused"):
            _set_tier(oid, "--override", f"peers_per_owner={raw}")
        assert _overrides(owner_conn, oid) == {}, ascii(raw)
    _set_tier(oid, "--override", f"peers_per_owner={bigint_max}")
    _set_tier(oid, "--override", "file_bytes= 7 ")
    assert _overrides(owner_conn, oid) == {"peers_per_owner": (bigint_max, None),
                                           "file_bytes": (7, None)}

def _no_dsn(oid):
    """No --dsn and no MESHY_DSN/DATABASE_URL: refused, like bootstrap_owner. Raised by the
    caller's pytest.raises."""
    saved = {k: os.environ.pop(k) for k in ("MESHY_DSN", "DATABASE_URL") if k in os.environ}
    try:
        call_command("set_owner_tier", "--owner", str(oid), "--tier", "pro",
                     stdout=io.StringIO())
    finally:
        os.environ.update(saved)


# --------------------------------------------------------------------------- #
# bootstrap_owner --tier: the fresh install sets it with zero SQL
# --------------------------------------------------------------------------- #

def test_bootstrap_owner_tier_sets_the_tier_and_the_default_is_unchanged(server, owner_conn,
                                                                         tmp_path):
    default = _bootstrap(tmp_path)
    assert _tier(owner_conn, default["owner_id"]) == "free" and default["tier"] == "free"

    ent = _bootstrap(tmp_path, "--tier", "enterprise")
    assert _tier(owner_conn, ent["owner_id"]) == "enterprise" and ent["tier"] == "enterprise"
    _used(owner_conn, ent["owner_id"], 5)
    r, _ = redeem(mint_code(_signed_in(ent)))
    assert r.status_code == 201, r.content
    # Control: the same sixth mint for the default owner is the free-tier refusal.
    _used(owner_conn, default["owner_id"], 5)
    r, _ = redeem(mint_code(_signed_in(default)))
    assert outcome(r) == (402, "E_QUOTA_EXCEEDED")

    email = f"tier-bad-{uuid.uuid4().hex[:8]}@example.test"
    pub = tmp_path / "bad.pub"
    pub.write_bytes(_pub_pem(os.urandom(32)))
    with pytest.raises(CommandError, match="enterprise"):
        call_command("bootstrap_owner", "--email", email, "--public-key-file", str(pub),
                     "--dsn", DSN, "--additional-owner", "--tier", "platinum",
                     stdout=io.StringIO(), stderr=io.StringIO())
    assert owner_conn.execute("SELECT count(*) FROM dashboard_users WHERE email = %s",
                              (email,)).fetchone()[0] == 0
