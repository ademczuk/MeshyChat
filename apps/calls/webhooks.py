"""The invocation webhook doorbell (divergence 041 S9).

Design docs/design/skill-frames.md (fin/design-skill-frames, 2e229a6) 4.4, 4.11, 5.4, 5.5;
the approved Q2, Q3, Q4 (the webhook half), Q9, Q10 and Q16; critique F3, F10, F11. The tables
are `sql_meshychat/021`; the key is `apps/calls/webhook_key.py`; the send is S8's
`apps/core/egress.post`.

A call to a target with no socket open waits `queued` (041 S5) until the target connects, when
redelivery routes it, or its deadline passes. A target that registered a URL and proved it
controls it is told, by a POINTER-ONLY POST there, that a call is waiting; it connects and
takes the call over its socket. The doorbell carries no args and moves no status.

REGISTRATION (Q9). `PUT /v1/peers/me/invocation-
webhook {url}` and `DELETE` on the path (apps/peers/views.py), through `authenticate_peer` with
DPoP, the write boundary, and no cookie path. `check_registration_url` is S8's `check_url`:
https, 443, no userinfo, no fragment, at most 2048 printable ASCII characters, no blocked IP
literal; under the loopback test hatch only a loopback literal may be http or another port.
`register` stores the URL UNVERIFIED with a fresh 32-byte challenge, and its commit NOTIFYs the
worker. The request POSTs nothing (rule E7). The network check runs again at every send, so
the registration check is a convenience, not the guard. A PUT always starts a new
registration: a new challenge, unverified.

PROOF OF CONTROL. The worker claims an unverified registration by writing `disabled_at` and a
`peer_webhook_verifications` record with the same stamp BEFORE it POSTs (rule B7), POSTs
`{type: "invocation_webhook_verify", challenge}` signed like a doorbell, and sets
`verified_at` (and clears `disabled_at`) only when a 2xx body is the challenge, byte for byte,
and only if the row still holds that URL, that challenge and that claim: a PUT in between is
not verified by the old echo. The outcome is written to the record whatever it is. Anything
but `verified` leaves the row disabled and unverified: one verification per PUT, never
retried, and never rung. The peer PUTs again to try again. The challenge is never returned to
the peer.

THE VERIFICATION FLOOR (the S9 review of a6bb87af, F1). A peer is verified at most once per
RATE_FLOOR_SECS, whatever it PUTs or DELETEs: a registration is claimable only when no record
of the peer's was claimed in that window, so a PUT inside it is stored and its verification
waits for a sweep. Before this, each PUT bought one signed POST to any https:443 URL the peer
named, and a peer re-PUTting kept a pass busy verifying while every doorbell waited. The
record is keyed to the peer, so DELETE then PUT does not reset the floor. A claim whose
record never finished (a crash, a restart past the worker's join; review F3) is taken again
once the floor has passed, its record closed `abandoned`.

STEP 9 (4.4) calls `insert_due` in the transaction that makes a call `queued`: a `due` attempt
when the target has a verified, enabled registration and was not rung (an attempt `sent` or
`failed`, claimed) in the last RATE_FLOOR_SECS, ON CONFLICT DO NOTHING on the partial unique
index that holds one due or claimed attempt per peer. Its insert trigger NOTIFYs the worker at
commit.

THE SENDER runs in `run_call_worker` (Q4; apps/calls/worker.run_process), never in a request
and never in `run_jobs`. `run_sender` LISTENs on `meshy_call_webhook`, runs a pass at once on
every (re)LISTEN (what was committed while nothing listened), on every notification, and every
SWEEP_SECS whatever it hears (a lost notification, a crashed worker's claim). A pass alternates (review F1): one doorbell, then one verification, until neither is left or
`stop` is set, so a doorbell waits behind at most ONE verification (15 s at worst), never a
backlog of them. It finishes the POST in flight when `stop` is set and starts no other (review
F3). The doorbell:
  1. (the verification is the section above);
  2. claims a due attempt, or one claimed more than STALE_CLAIM_SECS ago, with
     `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`, the claim
     committed BEFORE the POST (rule B7), so two workers never ring one attempt;
  3. re-reads the registration (verified, enabled, the peer active) and the peer's `queued`
     calls with a future deadline and an `active` caller (OFFERABLE's caller clause, operator
     decision 2026-09-25), preferring the call that rang it; with none, the attempt is
     `skipped` and nothing is POSTed;
  4. POSTs the pointer `{type: "skill_invoke_pending", call_id, target_peer_id, skill_name,
     deadline_at}` (Q2: no args reach a public host, which keeps MUST-8 intact) as its RFC 8785
     bytes, through egress, with `Authorization: Bearer <JWT>` under the webhook key;
  5. records the outcome on the attempt, guarded on its own claim: `sent` with the status and,
     in the same transaction, a `webhook_fired` event on the call rung for, and NOTHING else,
     so the call stays `queued` and `routed` keeps one meaning;
     `failed` with the egress reason and status; `skipped`. One POST per claim: a failure is
     final; the call stays queued and reconnect redelivery still
     works.

THE JWT (Q3): header {alg: EdDSA, kid: <webhook kid>, typ: meshy-invoke+jwt}; claims {iss: the
server's issuer, sub: the target peer, aud: the exact URL, iat, exp: iat + 120, jti: uuid4,
body_sha256: hex SHA-256 of the exact body bytes}. A receiver resolves the kid in
/.well-known/meshy-invoke-jwks.json (`webhook_key.JWKS_PATH`; never /.well-known/jwks.json,
which carries the token key alone: operator decision 2026-09-25), checks aud is its own URL
and exp, dedupes jti, and hashes the body.

NEVER CONTACTED: `skill_calls.webhook_url` (caller-supplied, stored and unread, Q10) and the
legacy `peers.invocation_delivery_webhook` column (sql/009). Nothing here reads either.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import threading
import time
import uuid

from apps.calls import events, wire
from apps.calls.webhook_key import TTL_SECS, WebhookKey, WebhookKeyError, load_for_signing
from apps.core import egress, rls
from apps.crypto import primitives as P

log = logging.getLogger("meshychat.calls.webhooks")

#: The channel both of sql_meshychat/021's triggers NOTIFY and the sender LISTENs on.
CHANNEL = "meshy_call_webhook"
#: How often the sender sweeps whatever it heard (4.11).
SWEEP_SECS = 5
#: A claim older than this is a crashed worker's, and a sweep takes it again (4.11). Longer
#: than egress's 15 s budget, so a live send is never taken from under its worker.
STALE_CLAIM_SECS = 30
#: No new doorbell for a peer rung within this many seconds (4.4, 4.11).
RATE_FLOOR_SECS = 60
CHALLENGE_BYTES = 32
#: At most this many verifications and this many attempts per pass; a pass then ends, and the
#: next notification or sweep continues.
MAX_PER_PASS = 100
#: How long the sender waits on its LISTEN connection before it looks at `stop` again.
POLL_SECS = 0.5

TYPE_DOORBELL = "skill_invoke_pending"
TYPE_VERIFY = "invocation_webhook_verify"
EVENT_FIRED = "webhook_fired"


class BadWebhookUrl(ValueError):
    """A registration URL the egress rules refuse."""


class DoorbellConfigError(RuntimeError):
    """Doorbells cannot be signed: no issuer configured."""


# ------------------------------------------------------------------------ registration

def check_registration_url(url) -> str:
    """S8's `check_url`, under the hatch as the send will be. Under the hatch `check_url` lets
    http and any port through for a NAME too (the send refuses those later); a registration
    admits them for a loopback literal only."""
    if not isinstance(url, str):
        raise BadWebhookUrl("url must be a string")
    try:
        target = egress.check_url(url, allow_loopback=egress.loopback_allowed())
    except egress.EgressRefused as exc:
        raise BadWebhookUrl(f"{exc.reason}: {exc.detail}") from None
    if not target.strict and not (target.literal is not None
                                  and egress.is_plain_loopback(target.literal)):
        raise BadWebhookUrl("https on port 443 only")
    return url


def register(conn, peer_id, url: str) -> dict:
    """Store `url` for `peer_id`, UNVERIFIED, with a fresh challenge. What the PUT answers."""
    challenge = P.b64u(os.urandom(CHALLENGE_BYTES))
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO peer_invocation_webhooks (peer_id, url, challenge, requested_at) "
                "VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (peer_id) DO UPDATE SET url = EXCLUDED.url, "
                "  challenge = EXCLUDED.challenge, requested_at = EXCLUDED.requested_at, "
                "  verified_at = NULL, disabled_at = NULL",
                (str(peer_id), url, challenge))
    return {"url": url, "verified": False}


def deregister(conn, peer_id) -> bool:
    """Delete `peer_id`'s registration. True when there was one. An attempt already due is
    skipped by the sender, which rings only a verified registration."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM peer_invocation_webhooks WHERE peer_id = %s "
                        "RETURNING peer_id", (str(peer_id),))
            found = cur.fetchone() is not None
    return found


# ------------------------------------------------------------------------------ step 9

def insert_due(cur, *, call_id: str, peer_id) -> int | None:
    """Step 9's half (4.4): a `due` attempt for a call just made `queued`, on the step-9
    transaction's cursor (under server_internal). Nothing when the target has no verified,
    enabled registration, already has one attempt due or claimed (the partial unique index),
    or was rung in the last RATE_FLOOR_SECS. Returns the attempt's id, or None."""
    cur.execute(
        "INSERT INTO peer_webhook_attempts (peer_id, call_id, state) "
        "SELECT w.peer_id, %(call)s, 'due' FROM peer_invocation_webhooks w "
        " WHERE w.peer_id = %(peer)s AND w.verified_at IS NOT NULL AND w.disabled_at IS NULL "
        "   AND NOT EXISTS (SELECT 1 FROM peer_webhook_attempts a "
        "                    WHERE a.peer_id = %(peer)s AND a.state IN ('sent', 'failed') "
        "                      AND a.claimed_at > now() - make_interval(secs => %(floor)s)) "
        "ON CONFLICT (peer_id) WHERE state IN ('due', 'claimed') DO NOTHING "
        "RETURNING id",
        {"call": call_id, "peer": str(peer_id), "floor": RATE_FLOOR_SECS})
    row = cur.fetchone()
    return None if row is None else row[0]


# ------------------------------------------------------------------------- the bodies

def doorbell_body(*, call_id: str, target_peer_id, skill_name: str, deadline_at) -> bytes:
    """The pointer (Q2), as the RFC 8785 bytes that are sent and hashed."""
    return P.jcs({"type": TYPE_DOORBELL, "call_id": call_id,
                  "target_peer_id": str(target_peer_id), "skill_name": skill_name,
                  "deadline_at": wire.optional_rfc3339(deadline_at)})


def verify_body(challenge: str) -> bytes:
    return P.jcs({"type": TYPE_VERIFY, "challenge": challenge})


def signed_headers(key: WebhookKey, *, issuer: str, peer_id, url: str, body: bytes) -> dict:
    """`Authorization: Bearer <JWT>` binding the receiver (aud), the target (sub) and the body
    (body_sha256), for 120 s."""
    now = int(time.time())
    claims = {"iss": issuer, "sub": str(peer_id), "aud": url, "iat": now,
              "exp": now + TTL_SECS, "jti": str(uuid.uuid4()),
              "body_sha256": hashlib.sha256(body).hexdigest()}
    return {"Authorization": f"Bearer {key.sign_jwt(claims)}",
            "Content-Type": "application/json"}


def require_issuer() -> str:
    """The doorbell's `iss`, the server's issuer; DoorbellConfigError when none is set."""
    from django.conf import settings
    issuer = getattr(settings, "MESHY_JWT_ISSUER", None)
    if not isinstance(issuer, str) or not issuer:
        raise DoorbellConfigError("no issuer is configured (MESHYCHAT_JWT_ISSUER): a doorbell's "
                                  "iss cannot be set, so none is sent")
    return issuer


# ---------------------------------------------------------------------- verification

@dataclasses.dataclass(frozen=True)
class VerifyClaim:
    peer_id: uuid.UUID
    url: str
    challenge: str
    #: The `disabled_at` this claim wrote, which its record is guarded on; also the
    #: verification record's `claimed_at`.
    claimed_at: object
    #: The verification record's id and `claimed_by`, unique to this claim.
    id: int = 0
    token: str = ""


def _claim_token(holder: str | None) -> str:
    from apps.jobs import runner
    return f"{holder or runner.default_holder()} {uuid.uuid4().hex[:12]}"


def claim_verification(conn, holder: str | None = None) -> VerifyClaim | None:
    """The oldest registration owed a verification, claimed and COMMITTED before its POST
    (rule B7): unverified, and either untried since its PUT (`disabled_at` NULL) or claimed by
    a verification whose record never finished; and only when no verification of this peer
    was claimed in the last RATE_FLOOR_SECS (review F1). The claim writes `disabled_at` and
    inserts the record with the same stamp; a record left unfinished is closed `abandoned`."""
    token = _claim_token(holder)
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE peer_invocation_webhooks SET disabled_at = clock_timestamp() "
                " WHERE peer_id = (SELECT w.peer_id FROM peer_invocation_webhooks w "
                "   WHERE w.verified_at IS NULL "
                "     AND (w.disabled_at IS NULL OR EXISTS ("
                "          SELECT 1 FROM peer_webhook_verifications v "
                "           WHERE v.peer_id = w.peer_id AND v.claimed_at = w.disabled_at "
                "             AND v.finished_at IS NULL)) "
                "     AND NOT EXISTS ("
                "          SELECT 1 FROM peer_webhook_verifications v "
                "           WHERE v.peer_id = w.peer_id AND v.claimed_at > "
                "                 clock_timestamp() - make_interval(secs => %(floor)s)) "
                "   ORDER BY w.requested_at, w.peer_id LIMIT 1 "
                "   FOR UPDATE SKIP LOCKED) "
                "RETURNING peer_id, url, challenge, disabled_at",
                {"floor": RATE_FLOOR_SECS})
            row = cur.fetchone()
            if row is None:
                return None
            peer_id, url, challenge, stamp = row
            cur.execute(
                "UPDATE peer_webhook_verifications SET finished_at = clock_timestamp(), "
                "       outcome = 'abandoned' WHERE peer_id = %s AND finished_at IS NULL",
                (str(peer_id),))
            cur.execute(
                "INSERT INTO peer_webhook_verifications (peer_id, url, claimed_by, claimed_at) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (str(peer_id), url, token, stamp))
            record_id = cur.fetchone()[0]
    return VerifyClaim(peer_id, url, challenge, stamp, record_id, token)


def verify(conn, claim: VerifyClaim, key: WebhookKey, issuer: str) -> str:
    """POST the challenge; set verified_at only on an exact echo of a challenge that still
    stands. Returns the outcome: verified, not_echoed, superseded, or the egress reason."""
    body = verify_body(claim.challenge)
    headers = signed_headers(key, issuer=issuer, peer_id=claim.peer_id, url=claim.url,
                             body=body)
    try:
        response = egress.post(claim.url, body, headers)
    except egress.EgressError as exc:
        log.info("webhook verification for peer %s: %s", claim.peer_id, exc)
        return _finish_verification(conn, claim, exc.reason, exc.status)
    except Exception:  # noqa: BLE001 - the row stays disabled; logged in full
        log.exception("webhook verification for peer %s raised", claim.peer_id)
        return _finish_verification(conn, claim, "internal", None)
    if response.truncated or response.body != claim.challenge.encode("ascii"):
        log.info("webhook verification for peer %s: the answer is not the challenge",
                 claim.peer_id)
        return _finish_verification(conn, claim, "not_echoed", response.status)
    return _finish_verification(conn, claim, "verified", response.status)


def _finish_verification(conn, claim: VerifyClaim, outcome: str,
                         http_status: int | None) -> str:
    """Write the outcome: on an exact echo `verified_at`, guarded on the URL, the challenge
    and this claim's stamp (else `superseded`); and in the same transaction the record,
    guarded on this claim. Returns the outcome written."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            if outcome == "verified":
                cur.execute(
                    "UPDATE peer_invocation_webhooks SET verified_at = clock_timestamp(), "
                    "       disabled_at = NULL "
                    " WHERE peer_id = %s AND url = %s AND challenge = %s AND disabled_at = %s "
                    "   AND verified_at IS NULL RETURNING peer_id",
                    (str(claim.peer_id), claim.url, claim.challenge, claim.claimed_at))
                if cur.fetchone() is None:
                    outcome = "superseded"
            cur.execute(
                "UPDATE peer_webhook_verifications SET finished_at = clock_timestamp(), "
                "       outcome = %s, http_status = %s "
                " WHERE id = %s AND claimed_by = %s AND finished_at IS NULL",
                (outcome, http_status, claim.id, claim.token))
    if outcome == "verified":
        log.info("webhook for peer %s verified", claim.peer_id)
    return outcome


# ----------------------------------------------------------------------- the doorbell

@dataclasses.dataclass(frozen=True)
class Claim:
    id: int
    peer_id: uuid.UUID
    #: The call whose step 9 inserted the attempt.
    call_id: str
    #: `claimed_by`, unique to this claim: the record is guarded on it.
    token: str


@dataclasses.dataclass(frozen=True)
class Ring:
    id: int
    peer_id: uuid.UUID
    #: The call the doorbell named, None when nothing was POSTed.
    rung_for: str | None
    state: str
    http_status: int | None
    error_class: str | None
    #: False when another worker had taken the claim, and this outcome was not written.
    recorded: bool


def claim_attempt(conn, holder: str) -> Claim | None:
    """The oldest due attempt, or one claimed more than STALE_CLAIM_SECS ago, claimed and
    COMMITTED before anything is sent (rule B7)."""
    token = f"{holder} {uuid.uuid4().hex[:12]}"
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE peer_webhook_attempts SET state = 'claimed', claimed_by = %(token)s, "
                "       claimed_at = clock_timestamp() "
                " WHERE id = (SELECT id FROM peer_webhook_attempts "
                "              WHERE state = 'due' OR (state = 'claimed' AND claimed_at < "
                "                    clock_timestamp() - make_interval(secs => %(stale)s)) "
                "              ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING id, peer_id, call_id, claimed_by",
                {"token": token, "stale": STALE_CLAIM_SECS})
            row = cur.fetchone()
    return None if row is None else Claim(*row)


def record_attempt(conn, claim: Claim, state: str, *, http_status: int | None = None,
                   error_class: str | None = None, fired_call: str | None = None) -> bool:
    """The outcome, written only while this claim still holds the row; on `sent`, the
    `webhook_fired` event on `fired_call` in the same transaction. False when another worker
    took the claim (this one outlived STALE_CLAIM_SECS)."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE peer_webhook_attempts SET state = %s, finished_at = clock_timestamp(), "
                "       http_status = %s, error_class = %s "
                " WHERE id = %s AND state = 'claimed' AND claimed_by = %s RETURNING id",
                (state, http_status, error_class, claim.id, claim.token))
            held = cur.fetchone() is not None
            if held and fired_call is not None:
                events.insert_event(cur, fired_call, EVENT_FIRED,
                                    details={"attempt_id": claim.id, "http_status": http_status})
    return held


def ring(conn, claim: Claim, key: WebhookKey, issuer: str) -> Ring:
    """Re-check, POST the pointer, record. One POST per claim."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT w.url, p.status = 'active' FROM peer_invocation_webhooks w "
                "  JOIN peers p ON p.id = w.peer_id "
                " WHERE w.peer_id = %s AND w.verified_at IS NOT NULL AND w.disabled_at IS NULL",
                (str(claim.peer_id),))
            registration = cur.fetchone()
            call = None
            if registration is not None and registration[1]:
                cur.execute(
                    "SELECT call_id, skill_name, deadline_at FROM skill_calls c "
                    " WHERE target_peer_id = %s AND status = 'queued' "
                    "   AND deadline_at > clock_timestamp() "
                    # OFFERABLE's caller clause (operator decision 2026-09-25, 041 F23 F8):
                    # a suspended caller's queued call rings nobody.
                    "   AND EXISTS (SELECT 1 FROM peers q WHERE q.id = c.caller_peer_id "
                    "                 AND q.status = 'active') "
                    " ORDER BY (call_id = %s) DESC, deadline_at, call_id LIMIT 1",
                    (str(claim.peer_id), claim.call_id))
                call = cur.fetchone()

    def done(state, *, rung_for=None, http_status=None, error_class=None):
        held = record_attempt(conn, claim, state, http_status=http_status,
                              error_class=error_class,
                              fired_call=rung_for if state == "sent" else None)
        return Ring(claim.id, claim.peer_id, rung_for, state, http_status, error_class, held)

    if registration is None:
        return done("skipped", error_class="not_verified")
    if not registration[1]:
        # Suspended or deleted since step 9 (design 4.4: delivery re-checks `active`).
        return done("skipped", error_class="not_active")
    if call is None:
        return done("skipped", error_class="nothing_queued")
    url = registration[0]
    call_id, skill_name, deadline_at = call
    body = doorbell_body(call_id=call_id, target_peer_id=claim.peer_id, skill_name=skill_name,
                         deadline_at=deadline_at)
    headers = signed_headers(key, issuer=issuer, peer_id=claim.peer_id, url=url, body=body)
    try:
        response = egress.post(url, body, headers)
    except egress.EgressError as exc:
        log.info("doorbell %s for peer %s: %s", claim.id, claim.peer_id, exc)
        return done("failed", rung_for=call_id, http_status=exc.status,
                    error_class=exc.reason)
    except Exception:  # noqa: BLE001 - recorded as failed, logged in full, never retried
        log.exception("doorbell %s for peer %s raised", claim.id, claim.peer_id)
        return done("failed", rung_for=call_id, error_class="internal")
    log.info("doorbell %s for peer %s: %d for call %s", claim.id, claim.peer_id,
             response.status, call_id)
    return done("sent", rung_for=call_id, http_status=response.status)


# ------------------------------------------------------------------------------ a pass

@dataclasses.dataclass
class PassReport:
    #: (peer_id, outcome) per registration verified or refused.
    verifications: list = dataclasses.field(default_factory=list)
    #: One Ring per attempt claimed.
    attempts: list = dataclasses.field(default_factory=list)


def run_pass(connect, *, holder: str | None = None, limit: int = MAX_PER_PASS,
             stop: threading.Event | None = None) -> PassReport:
    """Ring what is due and verify what is owed, ONE OF EACH IN TURN (review F1: a doorbell
    waits behind at most one verification), on one connection from `connect` (a FACTORY,
    rule E7), until neither is left, `limit` turns, or `stop` is set: the POST in flight is
    finished and recorded, and no other is started (review F3). Raises WebhookKeyError or
    DoorbellConfigError before touching a row when nothing can be signed."""
    from apps.jobs import runner

    key = load_for_signing()
    issuer = require_issuer()
    holder = holder or runner.default_holder()
    report = PassReport()

    def stopping() -> bool:
        return stop is not None and stop.is_set()

    conn = connect()
    try:
        for _ in range(limit):
            if stopping():
                break
            claim = claim_attempt(conn, holder)
            if claim is not None:
                report.attempts.append(ring(conn, claim, key, issuer))
                if stopping():
                    break
            vclaim = claim_verification(conn, holder)
            if vclaim is not None:
                report.verifications.append((vclaim.peer_id,
                                             verify(conn, vclaim, key, issuer)))
            if claim is None and vclaim is None:
                break
    finally:
        conn.close()
    return report


def _pass_safely(connect, holder: str, stop: threading.Event | None = None) -> None:
    try:
        run_pass(connect, holder=holder, stop=stop)
    except (WebhookKeyError, DoorbellConfigError) as exc:
        log.error("doorbells: %s", exc)
    except Exception:  # noqa: BLE001 - the next notification or sweep tries again
        log.exception("doorbells: the pass failed")


# -------------------------------------------------------------------------- the loop

def run_sender(connect, *, stop: threading.Event, sweep_secs: float = SWEEP_SECS,
               holder: str | None = None) -> None:
    """The sender's thread in `run_call_worker`: LISTEN, pass on every (re)LISTEN, on every
    notification and every `sweep_secs`, until `stop`. A LISTEN connection that fails is
    reopened with capped backoff; the sweep's cadence is never postponed by the notifications'
    passes."""
    from apps.jobs import runner

    holder = holder or runner.default_holder()
    delay = 1.0
    while not stop.is_set():
        try:
            _listen(connect, stop, sweep_secs, holder)
            delay = 1.0
        except Exception as exc:  # noqa: BLE001 - reconnect, as the bridges do
            log.warning("doorbell sender: %s: %s; retrying in %.1fs", type(exc).__name__,
                        exc, delay)
            stop.wait(delay)
            delay = min(delay * 2, 30.0)


def _listen_connection(connect):
    """The LISTEN connection is this thread's own session, never a pooled one (dbconn.connect's pool=False). A stand-in
    connect, as the tests pass, takes no argument."""
    from apps.core import dbconn

    return connect(pool=False) if connect is dbconn.connect else connect()


def _listen(connect, stop: threading.Event, sweep_secs: float, holder: str) -> None:
    listen = _listen_connection(connect)
    try:
        with listen.cursor() as cur:
            cur.execute(f"LISTEN {CHANNEL}")
        # Whatever was committed while nothing listened was never heard: pass first.
        _pass_safely(connect, holder, stop)
        next_sweep = time.monotonic() + sweep_secs
        while not stop.is_set():
            wait = max(0.0, min(POLL_SECS, next_sweep - time.monotonic()))
            heard = False
            for _note in listen.notifies(timeout=wait, stop_after=1):
                heard = True
            if stop.is_set():
                return
            if heard:
                # The pointer is only a wake-up: drain what else is queued, then one pass.
                for _note in listen.notifies(timeout=0):
                    pass
                _pass_safely(connect, holder, stop)
            if time.monotonic() >= next_sweep:
                _pass_safely(connect, holder, stop)
                next_sweep = time.monotonic() + sweep_secs
    finally:
        listen.close()


__all__ = ["BadWebhookUrl", "CHANNEL", "Claim", "DoorbellConfigError", "EVENT_FIRED",
           "MAX_PER_PASS", "PassReport", "RATE_FLOOR_SECS", "Ring", "STALE_CLAIM_SECS",
           "SWEEP_SECS", "TYPE_DOORBELL", "TYPE_VERIFY", "VerifyClaim",
           "check_registration_url", "claim_attempt", "claim_verification", "deregister",
           "doorbell_body", "insert_due", "record_attempt", "register", "require_issuer",
           "ring", "run_pass",
           "run_sender", "signed_headers", "verify", "verify_body"]
