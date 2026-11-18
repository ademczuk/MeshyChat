-- File transfer and memory expansion tables.
-- file_artifacts: blob metadata (R2 or FS) with call-scoped retention.
-- local_memory: per-peer-private KV store; CASCADE-deleted with the peer.
-- meshy_memory: owner-attributed, network-readable KV store.

-- file_artifacts: storage of file blob metadata (R2 or FS), call-scoped retention.
CREATE TABLE file_artifacts (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id             UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    uploaded_by_peer_id  UUID REFERENCES peers(id) ON DELETE SET NULL,
    r2_key               TEXT NOT NULL UNIQUE,
    sha256               TEXT,
    size_bytes           BIGINT,
    content_type         TEXT NOT NULL,
    filename             TEXT,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','ready','expired','failed')),
    grace_until          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finalized_at         TIMESTAMPTZ
);

CREATE INDEX file_artifacts_owner_status_idx
    ON file_artifacts(owner_id, status);

CREATE INDEX file_artifacts_grace_sweep_idx
    ON file_artifacts(status, grace_until)
    WHERE status IN ('pending','ready') AND grace_until IS NOT NULL;

-- local_memory: per-peer-private KV store; CASCADE-deleted with the peer.
CREATE TABLE local_memory (
    peer_id     UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       JSONB NOT NULL,
    source      TEXT NOT NULL DEFAULT 'peer' CHECK (source IN ('peer','server')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (peer_id, key)
);

CREATE INDEX local_memory_peer_source_idx
    ON local_memory(peer_id, source);

ALTER TABLE local_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE local_memory FORCE ROW LEVEL SECURITY;

CREATE POLICY local_memory_peer_select ON local_memory FOR SELECT
    USING (peer_id::text = current_setting('app.current_peer_id', true));

CREATE POLICY local_memory_internal_write ON local_memory
    FOR ALL USING (true) WITH CHECK (true);

CREATE TRIGGER local_memory_set_updated_at BEFORE UPDATE ON local_memory
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- meshy_memory: owner-attributed, network-readable KV store.
CREATE TABLE meshy_memory (
    created_by_peer_id   UUID REFERENCES peers(id) ON DELETE SET NULL,
    created_by_owner_id  UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    key                  TEXT NOT NULL,
    value                JSONB NOT NULL,
    source               TEXT NOT NULL DEFAULT 'peer'
                         CHECK (source IN ('peer','server')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (created_by_owner_id, key)
);

CREATE INDEX meshy_memory_owner_idx ON meshy_memory(created_by_owner_id);

ALTER TABLE meshy_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE meshy_memory FORCE ROW LEVEL SECURITY;

CREATE POLICY meshy_memory_public_select ON meshy_memory FOR SELECT USING (true);

CREATE POLICY meshy_memory_internal_write ON meshy_memory
    FOR ALL USING (true) WITH CHECK (true);

CREATE TRIGGER meshy_memory_set_updated_at BEFORE UPDATE ON meshy_memory
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
