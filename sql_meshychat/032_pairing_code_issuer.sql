-- MeshyChat divergence 062: a peer can mint a pairing code, and the code remembers which peer did.
--
-- WHY. The operator's instruction (2026-09-30): anyone on the network should be able to mint a node, with no cap or
-- limit. Until now only an OWNER could mint a pairing code (POST /v1/auth/onetime). POST /v1/peers/me/pairing-codes
-- lets any active peer mint one for its own owner. The redeem needs to know that a peer, not the owner, issued the code:
-- a code whose issuing peer has since been suspended or deleted is refused, so suspending a peer also withdraws the
-- codes it handed out.
--
-- WHAT THIS ADDS. One nullable column, pairing_codes.issued_by_peer_id. NULL is an owner-minted code, exactly as every
-- existing row is. No foreign key, on purpose: the redeem checks the issuer itself (active, same owner), and a key
-- with ON DELETE CASCADE would silently delete the codes of a deleted peer where the redeem should refuse them with a
-- reason. The durable record of who onboarded whom is on the new peer (peers.metadata.onboarded_by_peer_id, sql/018),
-- because apps/authapi/retention.py deletes a code a day after it expires.
--
-- A BOUNDED WAIT FOR ITS LOCK, as 014 and 020-031 have. ADD COLUMN with no default is a catalog-only change.

SET LOCAL lock_timeout = '5s';

ALTER TABLE pairing_codes ADD COLUMN IF NOT EXISTS issued_by_peer_id uuid;
