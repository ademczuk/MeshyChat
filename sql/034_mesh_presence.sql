-- 034: mesh_presence — advisory peer liveness + blocked-on signal.
--
-- DELIBERATELY OUTSIDE the signed shared_memory substrate. Presence is advisory
-- and spoofable-by-design; it MUST NEVER be an auth/trust input (same rule as
-- "the roster is never an auth surface"). A real operation still HMAC-gates at
-- the receiver regardless of what presence says. Keeping it in a plain table
-- (not the signed chain) means no provenance overhead and, crucially, writes go
-- through a plain API route that is NOT a skill-call — so heartbeats never
-- consume calls_per_day (the quota drain that bit the 30s pollers).
--
-- One row per peer, upserted. `blocked_on` is a change-only transition (set when
-- the peer parks waiting on another, cleared when it unblocks); its validity is
-- tied to last_beat — a blocked_on whose owner's last_beat is stale (older than
-- ~2 poll intervals) is ABANDONED and ignored by readers, so a crashed blocker
-- never leaves a peer chasing a ghost.

CREATE TABLE mesh_presence (
    peer_id      UUID PRIMARY KEY REFERENCES peers(id) ON DELETE CASCADE,
    state        TEXT NOT NULL DEFAULT 'active'
                 CHECK (state IN ('active','idle','asleep')),
    -- The peer this one is currently blocked waiting on (advisory). NULL = not
    -- blocked. FK-free on purpose: it may name a peer by id that the reader
    -- resolves via the roster; no referential coupling for an advisory hint.
    blocked_on   UUID,
    -- Free-form short note for the block/idle reason (e.g. "awaiting models sync").
    note         TEXT,
    last_beat    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX mesh_presence_blocked_on_idx ON mesh_presence (blocked_on)
    WHERE blocked_on IS NOT NULL;

CREATE TRIGGER mesh_presence_updated_at
    BEFORE UPDATE ON mesh_presence
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
