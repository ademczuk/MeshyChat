-- 079_heartbeat_beacon_cache.sql
-- TASK-23 increment 1: deploy-dark signed heartbeat beacon cache foundation.
--
-- `mesh_presence` is advisory and unsigned; this table stores the newest
-- trust-grade parent-signed HBHC beacon per parent peer for offline verifiers.

CREATE TABLE IF NOT EXISTS heartbeat_beacons (
    parent_peer_id UUID PRIMARY KEY,
    seq BIGINT NOT NULL,
    beat_unix_secs BIGINT NOT NULL,
    signature BYTEA NOT NULL,
    cached_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- NULL means no HBHC freshness binding, preserving current deploy-dark behavior
-- until the verify-path reject wiring lands in the next increment.
ALTER TABLE skill_grants
    ADD COLUMN IF NOT EXISTS freshness_ceiling_secs BIGINT;
