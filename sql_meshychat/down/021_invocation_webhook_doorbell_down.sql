-- DOWN SCRIPT for sql_meshychat/021_invocation_webhook_doorbell.sql (divergence 041 S9; the S9
-- review of a6bb87af, finding F5).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- applies *.sql in that directory only). Since the rollback gate (apps/core/rollback.py,
-- 2026-09-25) `sqlmigrate.py --rollback-to meshychat:VERSION` runs it, in one transaction
-- with the deletion of ledger row 21; tests/test_invoke_webhook_down.py and the round trip in
-- tests/test_rollback_gate.py run it too.
--
-- BY HAND, RUN IT IN ONE TRANSACTION, as the owner role:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/021_invocation_webhook_doorbell_down.sql
-- and AFTER rolling the code back past S9. S9 code on the pre-021 schema fails step 9 of every
-- invoke to an offline target (`webhooks.insert_due` names a table this drops), whether or not
-- doorbells are on, and the call stays `pending`. Pre-S9 code on the 021 schema never touches
-- these tables. So the order is: deploy the pre-S9 code, then apply this.
--
-- WHAT IS LOST. Every registration (a peer PUTs again after 021 is re-applied, and is verified
-- again), every doorbell attempt and every verification record. Nothing else reads them: no
-- call, event or peer row changes. Upstream's `peers.invocation_delivery_webhook` was never
-- written, so there is nothing to restore there.
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 021 has (review finding F4): dropping the attempts table
-- drops its foreign keys, which locks `skill_calls` and `peers`; queued behind an open writer
-- of either, that request would hold every later writer behind it. It gives up after 5 s
-- instead; retry when the table is quiet.

SET LOCAL lock_timeout = '5s';

-- Each table's triggers, policies and indexes go with it.
DROP TABLE IF EXISTS peer_webhook_attempts;
DROP TABLE IF EXISTS peer_webhook_verifications;
DROP TABLE IF EXISTS peer_invocation_webhooks;

DROP FUNCTION IF EXISTS meshy_webhook_attempt_notify();
DROP FUNCTION IF EXISTS meshy_webhook_verify_notify();

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 21;
