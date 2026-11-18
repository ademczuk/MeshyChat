-- Web-first onboarding: relax lesson_subscriptions so a mentee can be
-- recorded against a mentor BEFORE her desktop registers a peer.
--
-- Original 020 shape (mentor_peer_id, mentee_peer_id) assumed mentee_peer_id
-- was always known at subscription time (desktop-first redeem mints peer +
-- subscription atomically). Web-first redeems against a passkey-owned
-- dashboard_user that has no peer yet; the desktop later registers via
-- POST /v1/peers and the subscription must attach to the real peer at that
-- point.
--
-- Strategy:
-- 1. Add nullable mentee_owner_id; backfill from peers.owner_id for legacy
--    desktop-first rows; make it NOT NULL going forward.
-- 2. Relax mentee_peer_id to NULL so the pending state ('subscription
--    exists at owner level, peer not yet registered') is representable.
-- 3. Replace the original UNIQUE (mentor_peer_id, mentee_peer_id) with two
--    partial unique indexes:
--    - active subs uniqueness on (mentor, mentee_peer) when peer_id is set
--    - pending subs uniqueness on (mentor, mentee_owner) when peer_id is null
-- 4. New partial index to speed up the /v1/peers attachment query.

ALTER TABLE lesson_subscriptions
    ADD COLUMN mentee_owner_id UUID REFERENCES dashboard_users(id) ON DELETE CASCADE;

-- Backfill legacy rows by following peers.owner_id. Every existing row has
-- mentee_peer_id set (the old NOT NULL invariant), so this is total.
UPDATE lesson_subscriptions ls
   SET mentee_owner_id = p.owner_id
  FROM peers p
 WHERE p.id = ls.mentee_peer_id
   AND ls.mentee_owner_id IS NULL;

ALTER TABLE lesson_subscriptions
    ALTER COLUMN mentee_owner_id SET NOT NULL;

ALTER TABLE lesson_subscriptions
    ALTER COLUMN mentee_peer_id DROP NOT NULL;

-- The original UNIQUE was generated with the default Postgres name. Drop
-- by inferred name; fall back to a name probe if the default differs.
ALTER TABLE lesson_subscriptions
    DROP CONSTRAINT IF EXISTS lesson_subscriptions_mentor_peer_id_mentee_peer_id_key;

CREATE UNIQUE INDEX lesson_subscriptions_unique_active
    ON lesson_subscriptions (mentor_peer_id, mentee_peer_id)
    WHERE mentee_peer_id IS NOT NULL AND revoked_at IS NULL;

CREATE UNIQUE INDEX lesson_subscriptions_unique_pending
    ON lesson_subscriptions (mentor_peer_id, mentee_owner_id)
    WHERE mentee_peer_id IS NULL AND revoked_at IS NULL;

CREATE INDEX lesson_subscriptions_pending_owner_idx
    ON lesson_subscriptions (mentee_owner_id)
    WHERE mentee_peer_id IS NULL AND revoked_at IS NULL;
