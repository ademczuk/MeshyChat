SET LOCAL lock_timeout = '5s';

-- A seat's published presence state can go stale (divergence 082, issue #139). A beat may carry
-- `ttl_secs`; the row then holds when that state stops being true, and GET /v1/peers/presence
-- answers `effective_state` = 'unknown' past it. NULL keeps today's meaning: a beat without a
-- ttl never expires. A nullable column with no default is a catalog-only change in Postgres 11+:
-- no table rewrite, a brief ACCESS EXCLUSIVE lock, bounded by the lock_timeout above.
ALTER TABLE mesh_presence ADD COLUMN state_expires_at TIMESTAMPTZ;
