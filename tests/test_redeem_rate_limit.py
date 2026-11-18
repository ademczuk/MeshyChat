"""B16: invitation redemption is rate limited per client IP; pairing redemption is not (062).

WHY. `POST /v1/auth/redeem-invitation` takes no credential but the code in its body (divergence
016's backlog). B12's limiter (apps/core/peer_limit) is keyed on the AUTHENTICATED peer, and on
that route there is no peer yet, so nothing bounded how fast one host could guess codes.
`apps/core/redeem_limit` is one token bucket per client IP, taken first in the view, before the
body is read and before any database work, so a refused guess never reaches the code lookup.

`POST /v1/onboard/peers` took the same budget until divergence 062, which removed it on the
operator's instruction (no cap or limit on minting nodes). A pairing code is 24 random bytes, so
guessing one is not a question a budget answers; `test_pairing_redemption_is_not_throttled...`
pins the removal and that the invitation budget is untouched by it.

WHAT IS PINNED, each beside the control that differs from it in one variable:

  * a burst, then 429 `E_RATE_LIMITED` with `Retry-After`, on the invitation route;
  * refill, on the limiter's one clock (`redeem_limit._clock`, replaced by a fake here: no
    sleeps), including the Retry-After the refusal promised;
  * a second IP keeps its own budget;
  * a throttled request opens NO database connection (a spy on `dbconn.serving_connection`,
    whose positive control is that it counts every unthrottled lookup);
  * a valid invitation still redeems inside the budget, and one refused by the limiter is left
    unconsumed; pairing redemption takes no budget and spends none (062);
  * the key is the SOCKET address: forwarded headers do not move it unless the operator set
    MESHYCHAT_BEHIND_TRUSTED_PROXY (the control shows they do then);
  * the table is bounded (LRU order, TTL = time to refill), one table per address family,
    IPv6 is keyed on its /48, and the two settings are validated;
  * review of c2ffa60: a /48 cycling its /64s, or a full table of one family, does not lock
    new clients out (MEDIUM-1); Retry-After rounds up (LOW-1); an absent or unparseable
    address is a throttled shared bucket over HTTP (LOW-2).

Run:  .venv313/Scripts/python.exe -m pytest tests/test_redeem_rate_limit.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib
import ipaddress
import os
import uuid

import pytest

from tests.test_onboard import (  # noqa: F401 - fixtures are used by name; sets up Django
    _fresh_limiters,
    mint_code,
    outcome,
    owner_conn,
    post,
    seed_signer,
    server,
    signed_in_owner,
)
from tests.test_owner_routes import _invitation  # noqa: E402

from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.core import dbconn  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402

INVITE = "/v1/auth/redeem-invitation"
PAIR = "/v1/onboard/peers"
BURST = 3
RATE = 0.5                       # one token per 2 s: Retry-After on an empty bucket is 2
IP_A = "203.0.113.5"
IP_B = "198.51.100.7"


def _limit_module():
    """The module under test, or None on the code before B16 (so each test FAILS on its
    assertion there, rather than the file failing to collect)."""
    try:
        return importlib.import_module("apps.core.redeem_limit")
    except ModuleNotFoundError:
        return None


@pytest.fixture()
def clock(monkeypatch):
    """The limiter's ONE clock, replaced: tests move time, they never sleep for it."""
    t = {"now": 1000.0}
    mod = _limit_module()
    if mod is not None:
        monkeypatch.setattr(mod, "_clock", lambda: t["now"])
        mod.reset()
    return t


def tight():
    return override_settings(MESHY_REDEEM_RATE_BURST=BURST, MESHY_REDEEM_RATE_PER_SEC=RATE)


def _pubkey() -> str:
    return P.b64u(P.public_key_of(os.urandom(32)))


def guess(route: str, ip: str = IP_A, code: str | None = None, **headers):
    """One redeem attempt from socket address `ip`; an unknown code unless `code` is given."""
    code = code or f"guess-{uuid.uuid4().hex}"
    body = {"code": code} if route == INVITE else {"onetime_code": code,
                                                   "public_key": _pubkey()}
    return post(Client(), route, body, REMOTE_ADDR=ip, **headers)


UNKNOWN = {INVITE: (404, "E_INVITATION_INVALID"), PAIR: (404, "E_PAIRING_INVALID")}
# The throttled routes. Pairing redemption left this list in divergence 062.
ROUTES = pytest.mark.parametrize("route", [INVITE], ids=["invitation"])


def _is_throttled(r, retry_after: str | None = None):
    assert outcome(r) == (429, "E_RATE_LIMITED"), r.content
    if retry_after is not None:
        assert r["Retry-After"] == retry_after
    else:
        assert int(r["Retry-After"]) >= 1


# ------------------------------------------------------------------------ the HTTP surface

@ROUTES
def test_a_burst_then_429_with_retry_after(server, clock, route):
    with tight():
        for _ in range(BURST):                         # control: inside the budget
            assert outcome(guess(route)) == UNKNOWN[route]
        _is_throttled(guess(route), "2")               # (1 - 0 tokens) / 0.5 per s
        _is_throttled(guess(route), "2")               # a refusal takes nothing more


@ROUTES
def test_the_bucket_refills_on_the_limiter_clock(server, clock, route):
    with tight():
        for _ in range(BURST):
            guess(route)
        _is_throttled(guess(route), "2")
        clock["now"] += 1.5                            # 0.75 of a token back
        _is_throttled(guess(route), "1")
        clock["now"] += 0.5                            # one whole token
        assert outcome(guess(route)) == UNKNOWN[route]
        _is_throttled(guess(route))                    # and only one


@ROUTES
def test_a_second_ip_is_unaffected(server, clock, route):
    with tight():
        for _ in range(BURST):
            guess(route, IP_A)
        _is_throttled(guess(route, IP_A))
        for _ in range(BURST):
            assert outcome(guess(route, IP_B)) == UNKNOWN[route]
        _is_throttled(guess(route, IP_A))              # A is still spent


def test_pairing_redemption_is_not_throttled_and_spends_no_invitation_budget(server, clock):
    """Divergence 062. An exhausted invitation budget does not throttle pairing redemption,
    and pairing redemptions far past the burst do not spend the invitation budget."""
    with tight():
        for _ in range(BURST):
            guess(INVITE)
        _is_throttled(guess(INVITE))                              # control: the budget is spent
        for _ in range(BURST * 3):
            assert outcome(guess(PAIR)) == UNKNOWN[PAIR]
        for _ in range(BURST * 3):
            assert outcome(guess(PAIR, IP_B)) == UNKNOWN[PAIR]
        for _ in range(BURST):                                    # IP_B's invitation budget is whole
            assert outcome(guess(INVITE, IP_B)) == UNKNOWN[INVITE]
        _is_throttled(guess(INVITE, IP_B))


@ROUTES
def test_a_throttled_redeem_never_looks_the_code_up(server, clock, monkeypatch, route):
    opened = []
    real = dbconn.serving_connection

    def spy(*args, **kwargs):
        opened.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(dbconn, "serving_connection", spy)
    with tight():
        for i in range(BURST):
            assert outcome(guess(route)) == UNKNOWN[route]
            # Positive control: the instrument sees every lookup that happens.
            assert len(opened) == i + 1
        _is_throttled(guess(route))
        assert len(opened) == BURST, "a throttled guess reached the database"


def test_a_valid_invitation_redeems_within_the_budget(server, clock, owner_conn):
    mentor = seed_signer(owner_conn, "b16-inv")
    with tight():
        for _ in range(BURST - 1):
            guess(INVITE)
        good = _invitation(owner_conn, mentor["peer_id"])
        assert guess(INVITE, code=good).status_code == 201           # the last token
        held = _invitation(owner_conn, mentor["peer_id"])
        _is_throttled(guess(INVITE, code=held))
        assert owner_conn.execute("SELECT uses FROM invitations WHERE code = %s",
                                  (held,)).fetchone()[0] == 0, "a refused redeem spent a seat"
        clock["now"] += 2.0
        assert guess(INVITE, code=held).status_code == 201


def test_a_valid_pairing_code_redeems_after_any_number_of_guesses(server, clock, owner_conn):
    """Divergence 062: the redeem the budget used to refuse on the same address succeeds."""
    _, owner = signed_in_owner(owner_conn, "b16-pair")
    with tight():
        for _ in range(BURST * 3):
            guess(PAIR)
        held = mint_code(owner)
        assert guess(PAIR, code=held).status_code == 201
        assert owner_conn.execute("SELECT used_at FROM pairing_codes WHERE code = %s",
                                  (held,)).fetchone()[0] is not None


SPOOF = ("HTTP_X_FORWARDED_FOR", "HTTP_CF_CONNECTING_IP", "HTTP_X_REAL_IP")


@ROUTES
def test_forwarded_headers_do_not_move_the_key(server, clock, route):
    """MESHYCHAT_BEHIND_TRUSTED_PROXY unset: a fresh forwarded address per request is still the
    same socket, so the same bucket."""
    with tight(), authconfig.override_config(behind_trusted_proxy=False):
        for i in range(BURST):
            fake = f"192.0.2.{i + 1}"
            assert outcome(guess(route, **{h: fake for h in SPOOF})) == UNKNOWN[route]
        _is_throttled(guess(route, **{h: "192.0.2.200" for h in SPOOF}))


@ROUTES
def test_behind_a_trusted_proxy_the_forwarded_address_is_the_key(server, clock, route):
    """Control for the test above: the variation it sends WOULD reach a header-keyed
    limiter, because with the operator's assertion it does reach this one."""
    with tight(), authconfig.override_config(behind_trusted_proxy=True):
        for i in range(BURST * 2):
            r = guess(route, HTTP_CF_CONNECTING_IP=f"192.0.2.{i + 1}")
            assert outcome(r) == UNKNOWN[route]
        for _ in range(BURST):
            guess(route, HTTP_CF_CONNECTING_IP="192.0.2.99")
        _is_throttled(guess(route, HTTP_CF_CONNECTING_IP="192.0.2.99"))


# ----------------------------------------------------------------------------- the module

def _mod():
    mod = _limit_module()
    assert mod is not None, "apps.core.redeem_limit does not exist"
    return mod


def test_the_table_is_bounded_lru_with_a_ttl():
    mod = _mod()
    b = mod.IpBuckets(2, 1.0, max_tracked=3)           # TTL = 2 / 1.0 = 2 s to full
    for k in ("a", "b", "c"):
        assert b.take(k, now=0.0) == 0.0
    assert b.take("d", now=0.0) > 0.0, "full of live buckets: a NEW key is refused"
    assert len(b) == 3
    assert b.take("a", now=1.5) == 0.0                 # a is now the most recent
    assert b.take("d", now=2.0) == 0.0                 # b is idle a whole TTL: evicted
    assert len(b) == 3 and set(b.keys()) == {"c", "a", "d"}
    assert b.take("a", now=2.0) == 0.0                 # a kept its (refilled) bucket


def test_a_refused_new_key_is_not_admitted_by_evicting_a_live_bucket():
    """Evicting a partly spent bucket would hand its IP a fresh burst."""
    mod = _mod()
    b = mod.IpBuckets(1, 1.0, max_tracked=2)
    assert b.take("x", now=0.0) == 0.0
    assert b.take("y", now=0.0) == 0.0
    assert b.take("z", now=0.5) > 0.0
    assert b.take("x", now=0.5) > 0.0, "x was evicted and got a fresh burst"


def test_the_key_groups_ipv6_by_its_48():
    """Review MEDIUM-1: a /64 key let one free /48 (65,536 /64s) be 65,536 clients. A /48 is
    one site's allocation (RFC 6177), so it is one client here, as a NAT address is."""
    mod = _mod()
    assert mod.ip_key("2001:db8:1:2::1") == mod.ip_key("2001:db8:1:ffff:ffff::9")
    assert mod.ip_key("2001:db8:1:2::1") != mod.ip_key("2001:db8:2::1")        # control
    assert mod.ip_key("::ffff:203.0.113.5") == mod.ip_key("203.0.113.5")
    assert mod.ip_key("203.0.113.5") != mod.ip_key("203.0.113.6")
    assert mod.ip_key(None) == mod.ip_key("not an address")    # one shared unknown bucket


def test_defaults_and_settings():
    mod = _mod()
    assert mod.config() == (mod.DEFAULT_BURST, mod.DEFAULT_REFILL_PER_SEC)
    assert mod.DEFAULT_BURST <= 20 and mod.DEFAULT_REFILL_PER_SEC <= 0.1
    with override_settings(MESHY_REDEEM_RATE_BURST=7, MESHY_REDEEM_RATE_PER_SEC="0.25"):
        assert mod.config() == (7, 0.25)
    for bad in ({"MESHY_REDEEM_RATE_BURST": 0}, {"MESHY_REDEEM_RATE_BURST": "1.5"},
                {"MESHY_REDEEM_RATE_PER_SEC": "nan"}, {"MESHY_REDEEM_RATE_PER_SEC": -1}):
        with override_settings(**bad), pytest.raises(ImproperlyConfigured):
            mod.config()


# ------------------------------------------------- review of c2ffa60: MEDIUM-1, LOW-1, LOW-2

def _v6(net: str, i: int, shift: int) -> str:
    """The i-th address of `net`, stepping by 2**shift (80: /48s, 64: /64s)."""
    return str(ipaddress.IPv6Address(int(ipaddress.IPv6Network(net).network_address)
                                     + (i << shift) + 1))


def _v4(i: int) -> str:
    return str(ipaddress.IPv4Address(int(ipaddress.IPv4Address("10.0.0.0")) + i + 1))


def test_one_48_is_one_client_whichever_64_it_sends_from(clock):
    """MEDIUM-1: cycling /64s inside one /48 is one budget; another /48 is not (control)."""
    mod = _mod()
    with tight():
        for i in range(BURST):
            assert mod.admit(_v6("2001:db8:abcd::/48", i, 64)) is None
        refused = mod.admit(_v6("2001:db8:abcd::/48", BURST, 64))
        assert refused is not None and refused.status_code == 429, "a fresh /64 got a fresh burst"
        assert mod.admit(_v6("2001:db8:abce::/48", 0, 64)) is None


def test_a_48_cycling_10000_64s_does_not_lock_out_new_clients(clock):
    """MEDIUM-1, the review's repro at the defaults: 10,000 /64s of ONE /48, each touched every
    290 s (under the 300 s TTL), filled the table and every client the process had not seen
    got 429 indefinitely. Keyed on the /48 they are one bucket, and new clients get in."""
    mod = _mod()
    start = clock["now"]
    for lap in range(3):
        clock["now"] = start + 290.0 * lap
        for i in range(10_000):
            mod.admit(_v6("2001:db8:abcd::/48", i, 64))
        clock["now"] += 1.0
        assert mod.admit(f"198.51.100.{lap + 9}") is None, f"new IPv4 client refused, lap {lap}"
        assert mod.admit(_v6("2001:db8:1::/48", lap, 80)) is None,             f"new IPv6 client refused, lap {lap}"


def test_a_full_ipv6_table_does_not_lock_out_ipv4_and_the_reverse(clock):
    """MEDIUM-1: each address family has its own table, so address abundance in one (IPv6
    space is cheap) cannot refuse a new client of the other. The stated residual: a NEW
    client of the SAME family is refused while that family's table is full of live buckets,
    and its Retry-After is the time until the oldest bucket's TTL runs out (the earliest a
    slot can free), not one refill interval."""
    mod = _mod()
    start = clock["now"]
    for i in range(mod.MAX_TRACKED):
        assert mod.admit(_v6("2001:db8::/32", i, 80)) is None      # 10,000 distinct /48s
    clock["now"] = start + 100.0
    assert mod.admit("192.0.2.77") is None, "an IPv6 flood refused a new IPv4 client"
    refused = mod.admit(_v6("2001:db9::/32", 0, 80))
    assert refused is not None and refused.status_code == 429
    ttl = mod.DEFAULT_BURST / mod.DEFAULT_REFILL_PER_SEC                  # 300 s
    assert refused["Retry-After"] == str(int(ttl - 100.0)), refused["Retry-After"]

    mod.reset()
    clock["now"] = start
    for i in range(mod.MAX_TRACKED):
        assert mod.admit(_v4(i)) is None
    clock["now"] = start + 100.0
    assert mod.admit("2001:db8:77::1") is None, "an IPv4 flood refused a new IPv6 client"
    refused = mod.admit("192.0.2.78")
    assert refused is not None and refused["Retry-After"] == str(int(ttl - 100.0))


def test_a_new_key_refused_at_the_cap_is_told_when_a_slot_frees():
    """MEDIUM-1: the refusal of a NEW key at the cap waits for the least recently used
    bucket's TTL (the earliest any slot frees), not one refill interval, which promised a
    retry that would be refused again."""
    mod = _mod()
    b = mod.IpBuckets(2, 1.0, max_tracked=3)           # TTL 2 s
    for k in ("a", "b", "c"):
        assert b.take(k, now=0.0) == 0.0
    assert b.take("d", now=0.5) == pytest.approx(1.5)
    assert b.take("d", now=2.0) == 0.0                 # and at that moment it is admitted


def test_retry_after_rounds_up(server, clock):
    """LOW-1: a 1.5 s wait is `Retry-After: 2`. Rounding down would send the client back
    while its bucket is still empty (mutant M8, `int(wait)`, survived the waits 2.0 and 0.5)."""
    with tight():
        for _ in range(BURST):
            guess(INVITE)
        clock["now"] += 0.5                            # 0.25 of a token: 1.5 s to go
        _is_throttled(guess(INVITE), "2")


@ROUTES
def test_an_absent_or_unparseable_address_is_one_throttled_bucket(server, clock, route):
    """LOW-2: no usable socket address is ONE shared bucket, never a skipped check (mutant M7,
    `admit` returning None for the unknown key, survived a key-level assertion)."""
    with tight():
        for i in range(BURST):
            ip = "" if i % 2 else "not-an-address"
            assert outcome(guess(route, ip)) == UNKNOWN[route]
        _is_throttled(guess(route, ""))
        _is_throttled(guess(route, "999.1.1.1"))
        assert outcome(guess(route, IP_B)) == UNKNOWN[route]      # control: a real address
