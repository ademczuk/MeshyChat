-- spec §5.2: request→approve/deny flow for owner-to-owner access asks.

CREATE TABLE skill_access_requests (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_owner_id    UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    granter_owner_id      UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    granter_peer_id       UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    skill_name            TEXT NOT NULL,
    message               TEXT,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','approved','denied','expired','withdrawn')),
    decided_at            TIMESTAMPTZ,
    decided_by            UUID REFERENCES dashboard_users(id),
    decision_note         TEXT,
    resulting_grant_id    UUID REFERENCES skill_grants(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at            TIMESTAMPTZ NOT NULL
);

-- One pending request per (requester, granter_peer, skill_name) at a time.
CREATE UNIQUE INDEX skill_access_requests_pending_unique
    ON skill_access_requests (requester_owner_id, granter_peer_id, skill_name)
    WHERE status = 'pending';
