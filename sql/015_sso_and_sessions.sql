-- SSO bindings + sessions + onboarding dismissal.

-- Bind Authentik users to existing dashboard_users rows. NULL on legacy
-- rows; populated on first SSO login via email-match-then-bind.
ALTER TABLE dashboard_users
    ADD COLUMN authentik_sub TEXT UNIQUE;

-- Server-side replacement for the dashboard onboarding flag
-- "meshy_onboarding_dismissed". NULL = wizard shows on next login if the
-- user has zero linked agents.
ALTER TABLE dashboard_users
    ADD COLUMN onboarding_dismissed_at TIMESTAMPTZ;

-- BFF session table. Cookie value is the row id (UUID). expires_at is
-- server-controlled; cookie Max-Age matches. last_seen_at is bumped on
-- every middleware-touched request and triggers cookie re-issue when
-- within MESHY_SESSION_REFRESH_THRESHOLD_SECS of expiry.
CREATE TABLE owner_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_agent      TEXT,
    ip_inet         INET
);

CREATE INDEX owner_sessions_expires_at_idx ON owner_sessions (expires_at);
CREATE INDEX owner_sessions_owner_id_idx  ON owner_sessions (owner_id);
