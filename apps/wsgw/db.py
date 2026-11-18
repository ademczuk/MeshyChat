"""The database work the WS upgrade does, and nothing else.

The upgrade path issues three base statements. `key_state` adds the standing of a
`cnf`-bound token and `mesh_read` checks whether the peer holds read on shared/mesh-chat:

  1. `SELECT EXISTS(SELECT 1 FROM peers WHERE id = $1 AND status = 'active')`
     - an inactive peer is refused with 403.
  2. `INSERT INTO dpop_jti_seen (jti, peer_id, expires_at) VALUES (...)
      ON CONFLICT (jti) DO NOTHING RETURNING jti` -
     zero rows returned means the jti was already spent, i.e. a REPLAY.
  3. `SELECT card FROM agent_cards WHERE peer_id = $1` for the 0RTT hash comparison.

MEASURED, not guessed:
  * `peers`, `dpop_jti_seen` and `agent_cards` all have relrowsecurity = FALSE
    in this database, so no `app.current_peer_id` context is needed to read
    them - and `server_internal()` would be a privileged context taken for no
    reason. Checked against pg_class/pg_policies before writing this.
  * `dpop_jti_seen.jti` is `bytea` and `peer_id` carries an FK to `peers(id)`:
    a jti cannot be spent for a peer that does not exist. That FK is why the
    replay test seeds a real peer instead of a random UUID.
  * Connections still `SET ROLE meshychat_app` and run AUTOCOMMIT, per apps/core/rls.py:
    autocommit is what makes `SET LOCAL` bind to a real transaction rather than
    a savepoint inside an implicit one.

Every call is sync psycopg pushed onto a worker thread with `asyncio.to_thread`,
because the consumer is async and psycopg3's sync connection would block the
event loop for every other socket in the process.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg

from apps.core import dbconn

from . import revocation


def dsn() -> str:
    """Delegates to `apps.core.dbconn.dsn()` - ONE resolution for the whole project.

    Each app used to carry its own two-line resolver, and they had QUIETLY DIVERGED. A
    resolver short enough to read at a glance is one nobody reads: `apps/skillsreg` and
    `apps/peers` ignored `DATABASE_URL` and `settings.MESHY_DSN`, and `apps/wsgw` did not
    read `MESHY_DSN` AT ALL - a deployment configured the documented way would have had the
    WebSocket gateway on a hardcoded localhost dev database while every route used the real
    one, with nothing reporting a problem.

    They also all read `MESHY_TEST_DSN`, which the application layer deliberately does not
    know about: that variable belongs to `tests/conftest.py`, and test modules promote it to
    `MESHY_DSN` at import time. An app reading it directly made these surfaces pass in a
    configuration where the others would correctly have failed.

    `tests/test_connection_role_is_verified.py` now asserts every app resolves to the same
    string, and that moving `DATABASE_URL` moves all of them together.
    """
    return dbconn.dsn()


def connect() -> psycopg.Connection:
    """An RLS-bound connection: role meshychat_app, autocommit (see apps/core/rls.py)."""
    # dbconn.connect() does SET ROLE and then verifies it (assert_not_bypassing).
    return dbconn.connect(dsn())


def _peer_is_active(peer_id: uuid.UUID) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM peers WHERE id = %s AND status = 'active')",
            (peer_id,),
        )
        return bool(cur.fetchone()[0])


async def peer_is_active(peer_id: uuid.UUID) -> bool:
    """Whether the peer is active."""
    return await asyncio.to_thread(_peer_is_active, peer_id)


def _claim_dpop_jti(jti: bytes, peer_id: uuid.UUID, expires_at_unix: int) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dpop_jti_seen (jti, peer_id, expires_at) "
            "VALUES (%s, %s, to_timestamp(%s)) "
            "ON CONFLICT (jti) DO NOTHING RETURNING jti",
            (jti, peer_id, expires_at_unix),
        )
        return cur.fetchone() is not None


async def claim_dpop_jti(jti: bytes, peer_id: uuid.UUID, expires_at_unix: int) -> bool:
    """Spend a DPoP jti. False == already spent == REPLAY.

    A ledger error and a replay both fail the upgrade closed; a raised exception represents
    the first and False the second, so the two stay distinguishable in the log.
    """
    return await asyncio.to_thread(_claim_dpop_jti, jti, peer_id, expires_at_unix)


def _agent_card(peer_id: uuid.UUID):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT card FROM agent_cards WHERE peer_id = %s", (peer_id,))
        row = cur.fetchone()
        return row[0] if row else None


def _key_state(peer_id: uuid.UUID, key: bytes) -> str:
    with connect() as conn:
        return revocation.key_states(conn, [(peer_id, key)])[(peer_id, key)]


async def key_state(peer_id: uuid.UUID, key: bytes) -> str:
    """live | dead | unknown for the key a token is bound to (apps/wsgw/revocation.py).

    Without this check, a key revocation that closed a socket would be undone by the next
    upgrade presenting the same unexpired token (divergence 030)."""
    return await asyncio.to_thread(_key_state, peer_id, key)


def _mesh_read(peer_id: uuid.UUID) -> bool:
    with connect() as conn:
        return revocation.has_mesh_read(conn, peer_id)


async def mesh_read(peer_id: uuid.UUID) -> bool:
    """Whether the peer holds read on shared/mesh-chat now, by THE predicate the mesh push
    uses (`revocation.has_mesh_read`). NOT a gate: a peer without read may hold a socket
    (divergence 020). It is recorded on the socket so a later revoke is judged as the
    transition it is, and a notification about a socket that never had read closes
    nothing (divergence 030)."""
    return await asyncio.to_thread(_mesh_read, peer_id)


async def agent_card(peer_id: uuid.UUID):
    """None when the peer has published no card, which is a normal upgrade: the 0RTT
    negotiation is opt-in and fail-open."""
    return await asyncio.to_thread(_agent_card, peer_id)
