"""Envelope nonce replay ledger - gate 8.

The table is `envelope_nonce_seen`, sql/049_envelope_nonce_seen.sql.

WHY THIS EXISTS AT ALL. The signature proves WHO, not HOW MANY TIMES. Verification's
only replay guard is the 120-second `issued_at` skew window, and INSIDE that window a
captured envelope is byte-for-byte replayable: the signature is still valid, the key is
still live. This ledger is the second, independent answer.

TWO ORDERING RULES, BOTH SECURITY-RELEVANT:

  * It runs AFTER verify_payload, never before. The nonce is read from the SIGNED
    payload, so recording it pre-verification would let an attacker burn a nonce they
    never signed and DENY a legitimate future request - a cache-poisoning DoS.

  * The claim is ONE atomic statement - `INSERT ... ON CONFLICT DO NOTHING RETURNING`.
    A read-then-write would let two concurrent replays both observe "unseen".
    Zero rows returned means the pair was already present. The
    step 4c check (`check_nonce`) is a read, for the early refusal and its reason; it
    never decides alone, because the claim (`claim_nonce`) repeats it atomically.

WHEN THE CLAIM COMMITS (docs/DIVERGENCES.md 036, after its review). It depends on how the
request ends:

  * the call is created: the claim is written in the SAME transaction as the call row
    (`repository.insert_call`), so the two commit together;
  * the request is REFUSED after step 4c (a typed 4xx: no grant, attachments, mode):
    `burn_nonce` records it on its own, so the same bytes are refused as a replay later,
    when the grant exists;
  * the request FAILS before the call row commits (a 5xx, a dropped connection, a killed
    worker): nothing is recorded. The fleet's client resends the SAME body on a 5xx
    (mem_client's invoke retry), and a ledger row left by a failure would refuse that
    retry as a replay of a call that never existed.

The key is (peer_id, nonce), so two peers may legitimately use the same nonce string
(sql/049:20-22).

ON BY DEFAULT HERE (docs/DIVERGENCES.md 036). This project enforces unless an operator spells
the opt-out, following the rule MESHYCHAT_DPOP_ENFORCE already uses (apps/authapi/config.py).
`MESHYCHAT_ENVELOPE_NONCE_ENFORCE` is parsed ONCE, by meshychat/settings.py through
`enforce_from_env`, into the Django setting, and `enforce_from_settings` is the one reader every
transport calls when it builds a `CallsContext`.

WHAT THE LEDGER DOES AND DOES NOT SEE ON THE WIRE. Pipeline step 2 (idempotency) runs
before step 4c, so a byte-identical resend of an envelope whose first presentation
CREATED a call is answered from that row and never reaches this module: a client retry
of a lost answer keeps working, and nothing executes twice. The ledger refuses a resend
whose first presentation passed step 4c and was REFUSED (at authorization or after), a
nonce re-signed under a new call_id, and a payload with no nonce. A resend whose first
presentation FAILED before its call row committed is not a replay and runs.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured

from apps.calls.envelope import REPLAY_WINDOW_SECS, VerifiedPayload
from apps.calls.errors import NotAuthorized
from apps.core import rls
from apps.core.env import lookup

#: Read through apps.core.env.lookup.
ENV_VAR = "MESHYCHAT_ENVELOPE_NONCE_ENFORCE"

#: The spellings a human writes into a shell for a boolean switch, the same two sets
#: MESHYCHAT_DPOP_ENFORCE accepts (apps/authapi/config.py). Anything else is a typo, and a typo
#: in the value of a DEFAULT-ON security switch must not read as "off".
_FLAG_TRUE = ("1", "true", "yes", "on")
_FLAG_FALSE = ("0", "false", "no", "off")


class NonceConfigError(ImproperlyConfigured):
    """The replay switch is set to something that is not a boolean."""


def enforce_from_env(env) -> bool:
    """`MESHYCHAT_ENVELOPE_NONCE_ENFORCE`, parsed. Unset or blank is ON; only an explicit false
    spelling turns the gate off; anything else refuses to load.

    meshychat/settings.py calls this once, at import. A test that stands up a server from a
    runbook's environment calls it on that environment, so the two cannot disagree.
    """
    raw = lookup(ENV_VAR, source=env)
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value in _FLAG_TRUE:
        return True
    if value in _FLAG_FALSE:
        return False
    raise NonceConfigError(
        f"{ENV_VAR}={raw!r} is not a boolean. The envelope replay ledger defaults ON; to "
        f"turn it off set {ENV_VAR}=false explicitly.")


def enforce_from_settings() -> bool:
    """The ONE reader of settings.MESHY_ENVELOPE_NONCE_ENFORCE, for every transport that
    builds a `CallsContext` (apps/calls/views.py, apps/a2a/views.py). Read per request, so
    `override_settings` takes effect without an import-order race.

    No getattr default: settings.py always defines the name, and a missing one must raise
    rather than fall back (the fallback this replaced was False, in three places). Only a
    real bool is accepted, because the string "false" is truthy and would read as ON while
    looking like OFF to whoever wrote it.
    """
    from django.conf import settings
    value = settings.MESHY_ENVELOPE_NONCE_ENFORCE
    if not isinstance(value, bool):
        raise NonceConfigError(
            f"settings.MESHY_ENVELOPE_NONCE_ENFORCE must be a bool, got {value!r}; set "
            f"{ENV_VAR} in the environment instead")
    return value


#: How long a ledger row is kept past its envelope's `issued_at`: TWO acceptance windows.
#: One window is exactly as long as the
#: envelope stays acceptable, with no margin, while the two ends are judged on different
#: clocks at different moments: freshness on the app's clock when the request starts, the
#: sweep on the database's clock (`expires_at < now()`), and the claim some time later. A
#: sweep that ran in that gap, or a database clock running ahead, deleted a row under a
#: replay that had already passed freshness (the review of divergence 036, finding 3). The
#: second window is the margin: the gap plus the clock offset would have to exceed 120 s.
RETENTION_SECS = 2 * REPLAY_WINDOW_SECS


class NonceReplay(Exception):
    """The nonce has already been presented."""


class NonceMissing(Exception):
    """Enforcement requires a real nonce."""


@dataclass(frozen=True)
class NonceClaim:
    """A verified envelope's nonce, checked at step 4c and not yet recorded."""
    peer_id: object
    nonce: str
    issued_at: _dt.datetime


def record_nonce(conn, peer_id, nonce: str, issued_at: _dt.datetime) -> bool:
    """True = first presentation (recorded now), False = replay. The one INSERT every
    claim and burn goes through; on an autocommit connection it commits alone, inside a
    transaction it commits (or rolls back) with it.

    Retention is `RETENTION_SECS` past `issued_at`, not past the server's clock: the
    envelope's acceptability is anchored on `issued_at`, so its row must be too.
    """
    expires = issued_at + _dt.timedelta(seconds=RETENTION_SECS)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO envelope_nonce_seen (peer_id, nonce, expires_at) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (peer_id, nonce) DO NOTHING RETURNING nonce",
            (str(peer_id), nonce, expires),
        )
        return cur.fetchone() is not None


def check_nonce(conn, verified: VerifiedPayload) -> NonceClaim:
    """Step 4c. Raises NonceMissing / NonceReplay; otherwise returns the
    claim for step 8 to write (`claim_nonce`) or for a refusal to burn (`burn_nonce`).

    Call this only when enforcement is ON, which is the default here (divergence 036).
    When it is OFF, a missing nonce is ACCEPTED - the rollout path that keeps pre-nonce
    signers working. "Enforcement
    off" therefore means "skip the call", not "call it and ignore the answer".
    """
    nonce = verified.payload.get("nonce")
    if not isinstance(nonce, str) or nonce == "":
        # An EMPTY nonce is treated as missing, not as a valid nonce value
        # Empty strings do not identify a nonce.
        raise NonceMissing()
    # A ledger/DB error is NOT swallowed: it propagates and the request fails CLOSED
    # The pipeline rejects a ledger error.
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM envelope_nonce_seen WHERE peer_id = %s AND nonce = %s",
                    (str(verified.peer_id), nonce))
        if cur.fetchone() is not None:
            raise NonceReplay()
    return NonceClaim(peer_id=verified.peer_id, nonce=nonce, issued_at=verified.issued_at)


def claim_nonce(conn, claim: NonceClaim) -> None:
    """Record the claim or raise NonceReplay: the atomic statement, for step 8 to run
    INSIDE the call row's transaction, so that a request which never creates its call
    leaves no row (see "WHEN THE CLAIM COMMITS" above). A concurrent presentation of the
    same (peer, nonce) that got past its own step 4c check loses here."""
    if not record_nonce(conn, claim.peer_id, claim.nonce, claim.issued_at):
        raise NonceReplay()


def burn_nonce(conn, claim: NonceClaim) -> None:
    """Record the claim of a request REFUSED after step 4c, on its own statement, so the
    same bytes are a replay from now on. Already present is fine (a presentation that
    raced this one burned or claimed it first); a ledger error propagates."""
    record_nonce(conn, claim.peer_id, claim.nonce, claim.issued_at)


def to_rpc_error(exc: Exception) -> NotAuthorized:
    """Map ledger failures to the stable authorization reason strings."""
    if isinstance(exc, NonceReplay):
        return NotAuthorized("envelope replay")
    if isinstance(exc, NonceMissing):
        return NotAuthorized("missing envelope nonce")
    return NotAuthorized("nonce ledger")


def sweep_expired_nonces(conn, limit: int) -> int:
    """A nonce is meaningful for one window only, and its row is
    kept for two (RETENTION_SECS), so an expired row can never deny a fresh request.
    `flow_continuation_nonces` has its own function below
    (`sweep_expired_flow_nonces`) and its own registered job.

    `limit` bounds ONE call to that many rows and is what the scheduler passes
    (apps/jobs/registry.py). It is required (#130, 2026-10-06): the unbounded form it
    replaced is the shape rule B5 refuses for repeated work, since a single DELETE over
    every expired row holds locks for as long as the backlog is large, and the backlog is
    largest exactly when the sweeper has been down. Bounded batches make a long backlog
    take more ticks instead of one long transaction, and no production caller used it.

    Returns rows deleted. A caller that gets back exactly `limit` should assume more
    remain rather than that it has caught up; the scheduler reads it that way.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a whole number > 0; 0 would be a silent no-op")
    with conn.cursor() as cur:
        # Sub-select on the PRIMARY KEY, ordered by the column the partial index in
        # sql/049 covers, so the bound costs an index range scan rather than a sort
        # over the whole table.
        cur.execute(
            "DELETE FROM envelope_nonce_seen "
            " WHERE (peer_id, nonce) IN ("
            "   SELECT peer_id, nonce FROM envelope_nonce_seen "
            "    WHERE expires_at < now() ORDER BY expires_at LIMIT %s)",
            (limit,),
        )
        return cur.rowcount


#: How long past `expires_at` a SPENT flow nonce whose call row is absent is still kept:
#: one flow window (120 s). The consume and the call row are two commits (pipeline step
#: 8 inserts the call after it), so in between the row names a call that does not exist YET.
#: The consume matches `expires_at >= NOW()`, so the margin puts at least one window between
#: the consume and the delete: time for that request to write its row, or to have failed.
FLOW_SPENT_MARGIN_SECS = 120


def sweep_expired_flow_nonces(conn, *, limit: int) -> int:
    """Sweep expired flow nonces in bounded batches. A spent row whose call still exists is
    kept, and an unspent row goes at expiry.

    A SPENT row
    (`consumed_by_call_id` set) is the only record of its consuming call's parent: the
    receipt reads `parent_call_id` from it (apps/calls/reads.receipt_provenance), so deleting
    it at expiry blanks the receipt's parent to ''.

    Operator decision, 2026-09-26 (docs/DIVERGENCES.md 036): a spent row is KEPT WHILE ITS
    CALL EXISTS, and deleted once the call is gone (removed by a call retention, or never
    written) and it is FLOW_SPENT_MARGIN_SECS past expiry. "The call" is the `skill_calls`
    row whose UNIQUE `call_id` equals `consumed_by_call_id`; a receipt loads its call by that
    key. An UNSPENT row is deleted past expiry, with no margin, because it can never be
    consumed: the consume requires an expiry at or after the current time, and the sweep's `<`
    leaves the boundary instant to it. The existence check only ever keeps a
    row the expiry rule would delete, so the sweep stays keyed on expiry (rule i).

    server_internal: `skill_calls` is FORCE-RLS with a peer-scoped SELECT policy, so under
    meshychat_app with no context it reads EMPTY, the NOT EXISTS is true for every row, and every
    spent row would go. `flow_continuation_nonces` itself has no row-level security.

    Oldest first, under `limit` (B5), and the LIMIT is taken over the deletable rows only:
    kept rows never use up a batch. Returns rows deleted.
    """
    if limit <= 0:
        raise ValueError("limit must be > 0; a zero bound would be a silent no-op")
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM flow_continuation_nonces WHERE nonce IN ("
                "  SELECT n.nonce FROM flow_continuation_nonces n "
                "   WHERE n.expires_at < now() "
                "     AND (n.consumed_by_call_id IS NULL "
                "          OR (n.expires_at < now() - make_interval(secs => %s) "
                "              AND NOT EXISTS (SELECT 1 FROM skill_calls c "
                "                               WHERE c.call_id = n.consumed_by_call_id))) "
                "   ORDER BY n.expires_at LIMIT %s)",
                (FLOW_SPENT_MARGIN_SECS, limit))
            return cur.rowcount
