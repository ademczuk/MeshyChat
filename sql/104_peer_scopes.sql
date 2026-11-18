-- Per-peer JWT scope store.
--
-- Before this migration, peer-JWT scopes existed only as a value hardcoded at
-- each mint site. Four of the five peer mints (auth_signin, auth_refresh,
-- onboard_peers, invites/redeem) minted `scopes: Vec::new()`, so any peer that
-- renewed its token lost every scope it was provisioned with and could never
-- regain one. The single scope actually enforced on peer JWTs,
-- `skills:declare`, gates the five peer_skills write routes, which publish into
-- the shared discovery surface and the paid marketplace. The practical effect
-- was that skill publication was deny-all for every self-renewing peer.
--
-- DENY BY DEFAULT. The column is NULLABLE with NO DEFAULT and there is
-- deliberately NO BACKFILL. A NULL (or absent) row mints exactly the empty
-- vector the mint sites produce today, so this migration changes the authority
-- of precisely zero existing peers on the day it applies. The store starts
-- empty and fills one operator provisioning decision at a time.
--
-- Deliberately NOT backfilled from provision_peer.py's DEFAULT_SCOPES. That
-- constant is a CLIENT-side default applied by a provisioning script; copying
-- it into every existing peers row would grant marketplace publication to the
-- entire fleet in one migration, which is the widening this change exists to
-- avoid.
--
-- TEXT[] matches the existing scope-column convention in
-- 018_owner_api_keys.sql (`scopes TEXT[] NOT NULL DEFAULT '{}'`). The nullable
-- shape here is intentional and differs from that one: NULL means "no
-- provisioning decision has been recorded", which the mint path treats as the
-- empty set. skill_grants.scopes (JSONB, 004) is a separate authorization
-- vocabulary for skill-CALL delegation and is unrelated to this column.
--
-- There is no API write path for this column in this change. It is populated by
-- the operator-authorized provisioning path only, matching how peer identities
-- and grants are already registered.

ALTER TABLE peers ADD COLUMN scopes TEXT[];

COMMENT ON COLUMN peers.scopes IS
  'Peer-JWT scopes minted into this peer''s tokens. NULL means no provisioning '
  'decision recorded, which mints an empty scope set (deny by default). '
  'Operator-populated only; there is no self-service write path.';
