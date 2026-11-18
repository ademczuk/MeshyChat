-- spec §5.2: lifecycle audit trail. CASCADE-deletes with parent skill_call.

CREATE TABLE skill_call_events (
    id                BIGSERIAL PRIMARY KEY,
    call_id           TEXT NOT NULL REFERENCES skill_calls(call_id) ON DELETE CASCADE,
    event_type        TEXT NOT NULL,
    details           JSONB,
    source            TEXT NOT NULL CHECK (source IN ('caller','target','meshy')),
    source_peer_id    UUID,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hot path: read a single call's full audit trail in time order.
CREATE INDEX skill_call_events_call_time_idx
    ON skill_call_events (call_id, occurred_at);
