-- Migration 021: no schema change. Quota stays at first peer_keys insert.
-- Redeem creates a peer without charging quota; desktop mandatory key publish
-- settles it. Janitor sweep handles orphans after 30 days.
SELECT 1 WHERE FALSE;
