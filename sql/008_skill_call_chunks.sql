-- spec §5.2: stream-mode replay buffer + retention helper function.

CREATE TABLE skill_call_chunks (
    id                  BIGSERIAL PRIMARY KEY,
    call_id             TEXT NOT NULL REFERENCES skill_calls(call_id) ON DELETE CASCADE,
    sequence            INTEGER NOT NULL,
    chunk_type          TEXT NOT NULL CHECK (chunk_type IN ('text','json','file_ref')),
    content             BYTEA,
    file_artifact_id    UUID,
    chunk_signature     TEXT NOT NULL,
    chunk_signing_kid   TEXT NOT NULL,
    emitted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (call_id, sequence)
);

-- Retention helper: keep the last N chunks per call, drop older ones.
-- N comes from tier_capabilities (e.g. 200 for free, 2000 for pro);
-- the janitor calls this once a call reaches a terminal state.
CREATE OR REPLACE FUNCTION trim_chunks_for_call(
    p_call_id    TEXT,
    p_keep_last  INTEGER
) RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    WITH ranked AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY sequence DESC) AS rn
        FROM skill_call_chunks
        WHERE call_id = p_call_id
    )
    DELETE FROM skill_call_chunks
    WHERE id IN (SELECT id FROM ranked WHERE rn > p_keep_last);

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;
