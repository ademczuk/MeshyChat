-- spec §5.2: owner→owner permission grants per skill name.

CREATE TABLE skill_grants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grantor_owner_id    UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    grantee_owner_id    UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    granter_peer_id     UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    skill_name          TEXT NOT NULL,
    version_pin         TEXT,
    source              TEXT NOT NULL
                        CHECK (source IN ('marketplace_subscription','direct','approved_request','public_auto')),
    source_ref          TEXT,
    scopes              JSONB,
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    revoked_by          UUID REFERENCES dashboard_users(id),
    revoke_reason       TEXT
);

-- One active grant per (grantor, grantee, peer, skill_name) at a time.
CREATE UNIQUE INDEX skill_grants_active_unique
    ON skill_grants (grantor_owner_id, grantee_owner_id, granter_peer_id, skill_name)
    WHERE revoked_at IS NULL;

-- Hot path for authorize(): "does grantee X hold a non-revoked grant for
-- (granter peer, skill name)?" (spec §7.2 step 4).
CREATE INDEX skill_grants_hot_idx
    ON skill_grants (grantee_owner_id, granter_peer_id, skill_name)
    WHERE revoked_at IS NULL;
