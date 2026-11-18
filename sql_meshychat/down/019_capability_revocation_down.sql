-- DOWN SCRIPT for sql_meshychat/019_capability_revocation.sql (divergence 040, slice 4;
-- written at its integration, integ/r11).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- globs *.sql in that directory only), and nothing runs it but an operator, `sqlmigrate.py
-- --rollback-to meshychat:18` (apps/core/rollback.py, which first runs the down script of
-- every applied version above 19, newest first: 022's, 021's and 020's since fin/post-r10) or
-- tests/test_rollback_gate.py's round trip. 019 is on
-- rollback.NO_DOWN_SCRIPT_ALLOWED, so it needs none; it has one because the roll-forward gate
-- refuses, on a database in service, a pending migration without one, and the test server
-- applies 019 after 020 and 021.
--
-- RUN IT IN ONE TRANSACTION, as the owner role:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/019_capability_revocation_down.sql
-- with capability tokens OFF (MESHY_CAPABILITIES unset, the default) or after rolling the code
-- back past divergence 040 slice 4. Only a token's verification (its liveness statement), the
-- mint and the revoke read or write these tables, and each runs only while tokens are on; with
-- them off the routes answer E_CAPABILITY_UNAVAILABLE and nothing of the capability code is
-- imported.
--
-- WHAT IS LOST: the issuance register and every revocation. That fails closed: the verifier
-- refuses a token whose authority id has no `issue` row, so after 019 is applied again every
-- token minted before this ran is refused, a revoked one included, and a new one must be
-- minted. The log is audit history (V1 rule A1); take a backup first if it must be kept.
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 021's down script has: dropping capability_revocation
-- drops its foreign key to peers, which locks `peers`; queued behind an open writer of it,
-- that request would hold every later writer behind it. It gives up after 5 s instead; retry.

SET LOCAL lock_timeout = '5s';

-- Each table's policies, indexes, sequence, grants and comment go with it.
DROP TABLE IF EXISTS capability_event;
DROP TABLE IF EXISTS capability_revocation;

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 19;
