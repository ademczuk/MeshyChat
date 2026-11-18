-- Lesson subscriptions: per-mentee binding to a mentor peer, created at
-- invitation redeem time. Used by the lesson fanout to route published
-- lessons from a mentor to all subscribed mentees.

CREATE TABLE lesson_subscriptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mentor_peer_id      UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    mentee_peer_id      UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at          TIMESTAMPTZ,
    UNIQUE (mentor_peer_id, mentee_peer_id)
);

CREATE INDEX lesson_subscriptions_mentor_idx ON lesson_subscriptions (mentor_peer_id) WHERE revoked_at IS NULL;
CREATE INDEX lesson_subscriptions_mentee_idx ON lesson_subscriptions (mentee_peer_id) WHERE revoked_at IS NULL;
