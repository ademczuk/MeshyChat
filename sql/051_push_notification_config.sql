-- 051_push_notification_config.sql
-- TASK-27: registered A2A push-notification configs with CRUD, generalizing the
-- single inline skill_calls.webhook_url (migration 006) into per-(peer, call)
-- callback registrations. Mirrors the A2A PushNotificationConfig shape
-- (url + optional token + optional authentication) for spec 3.1.7-3.1.10.
--
-- `peer_id` is the AUTHENTICATED peer that registered the config (the A2A client
-- asking to be notified about a task == meshy call_id). A peer CRUDs only its own
-- rows. Outbound delivery signs an Ed25519 JWT the caller verifies against the
-- meshy-server JWKS: peer signing keys are SELF-CUSTODY (peer_keys stores public
-- keys only, migration 002), so the server cannot sign AS the peer; it signs as
-- itself, which is also the correct A2A mapping (the SENDER of the push signs).

CREATE TABLE IF NOT EXISTS push_notification_configs (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    peer_id     UUID        NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    call_id     TEXT        NOT NULL,
    url         TEXT        NOT NULL,
    -- A2A PushNotificationConfig.token: opaque value the caller hands us to echo
    -- back in the callback so it can correlate/validate the notification.
    token       TEXT,
    -- A2A PushNotificationConfig.authentication: {schemes[], credentials?}. Stored
    -- as JSONB so the shape can evolve without a migration.
    authentication JSONB,
    active      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- CRUD list/get is by (peer, call); delete is by id scoped to the owning peer.
CREATE INDEX IF NOT EXISTS push_notification_configs_peer_call_idx
    ON push_notification_configs (peer_id, call_id);
