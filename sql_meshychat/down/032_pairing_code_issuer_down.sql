-- DOWN for sql_meshychat/032 (divergence 062). `sqlmigrate.py --rollback-to meshychat:31` runs it (the rollback gate,
-- apps/core/rollback.py), newest first. tests/test_rollback_gate.py holds it to the catalog 032 found.
--
-- WHAT IS LOST: which peer issued each outstanding code. Those codes stay redeemable, as owner-minted codes, which is
-- what the code before 032 did with every code. The new peers' metadata.onboarded_by_peer_id is kept.
--
-- ORDER: ROLL THE CODE BACK FIRST, THEN RUN THIS. The 062 code reads and writes this column
-- (`create_peer` selects it, `mint_peer_code` inserts it), so dropping it under that code makes every
-- pairing redeem and every peer mint fail, not just lose issuer enforcement. The sentence above about
-- codes staying redeemable holds only once the code before 062 is what is serving.

SET LOCAL lock_timeout = '5s';

ALTER TABLE pairing_codes DROP COLUMN IF EXISTS issued_by_peer_id;

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 32;
