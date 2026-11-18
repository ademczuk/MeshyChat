"""Retention for the three auth tables that gain a row a sign-in: pairing codes, sign-in
challenges and owner sessions (STATUS D11, D15).

Each is one bounded DELETE keyed on the row's own `expires_at` (rule i: never a live
object's identity, never a row still in force), oldest first, a sub-select on the primary
key under a LIMIT so one call cannot hold locks over a whole backlog (B5). The registered
jobs are in apps/jobs/registry.py; the bodies that call these are in apps/jobs/sweeps.py.

None of the three tables carries row-level security (measured with relrowsecurity), so the
job's work connection needs no context, as the request paths that write them bind none.

  owner_sessions   deleted once `expires_at` has passed; every lookup reads
                   `expires_at > NOW()`, so an expired row authenticates nothing.
  auth_challenges  the predicate of the opportunistic prune the challenge route already
                   runs (apps/authapi/views.py); the
                   consume matches `expires_at > NOW()`, so expired is dead, consumed or not.
  pairing_codes    A day past expiry is kept, because deleting
                   a code changes what a late presentation is told, from 410 "expired" or
                   "already used" to 404 "unknown pairing code" (docs/DIVERGENCES.md 016).
"""
from __future__ import annotations

#: How long a pairing code is kept past its expiry. The code lives 60 s; a day is ample
#: for a person retrying an old deep link to be told it expired rather than unknown.
PAIRING_CODE_KEEP_SECS = 24 * 3600


def _bounded(limit: int) -> int:
    if limit <= 0:
        raise ValueError("limit must be > 0; a zero bound would be a silent no-op")
    return int(limit)


def sweep_expired_pairing_codes(conn, *, limit: int) -> int:
    """Delete up to `limit` pairing codes more than PAIRING_CODE_KEEP_SECS past expiry,
    used or not. Returns rows deleted."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pairing_codes WHERE code IN ("
            "  SELECT code FROM pairing_codes "
            "   WHERE expires_at < now() - make_interval(secs => %s) "
            "   ORDER BY expires_at LIMIT %s)",
            (PAIRING_CODE_KEEP_SECS, _bounded(limit)))
        return cur.rowcount


def sweep_expired_auth_challenges(conn, *, limit: int) -> int:
    """Delete up to `limit` challenges past `expires_at`, consumed or not."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM auth_challenges WHERE (peer_id, nonce) IN ("
            "  SELECT peer_id, nonce FROM auth_challenges "
            "   WHERE expires_at < now() ORDER BY expires_at LIMIT %s)",
            (_bounded(limit),))
        return cur.rowcount


def sweep_expired_owner_sessions(conn, *, limit: int) -> int:
    """Delete up to `limit` owner sessions past `expires_at`."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM owner_sessions WHERE id IN ("
            "  SELECT id FROM owner_sessions "
            "   WHERE expires_at < now() ORDER BY expires_at LIMIT %s)",
            (_bounded(limit),))
        return cur.rowcount
