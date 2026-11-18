-- 044_agent_cards.sql
--
-- TASK-7: storage for the per-peer A2A Agent Card. The peer signs its card with its own
-- Ed25519 key (the server holds only public keys) and PUBLISHES it here; the server serves
-- the stored card unauthenticated at GET /agents/{peer_id}/agent-card.json so an external
-- A2A client can discover + verify it (resolving the key via the JWKS at the card's jku).
-- One card per peer, upserted on publish.
--
-- No explicit GRANTs: migration 010's `ALTER DEFAULT PRIVILEGES IN SCHEMA public` covers
-- meshy_app for tables created after it.
CREATE TABLE agent_cards (
    peer_id    UUID PRIMARY KEY REFERENCES peers(id) ON DELETE CASCADE,
    card       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
