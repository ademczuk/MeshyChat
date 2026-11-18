ALTER TABLE skill_grants
    ADD COLUMN IF NOT EXISTS approved_requires_human_auth BOOLEAN NOT NULL DEFAULT false;

-- Backfill: an upgraded database can already hold grants approved before this
-- migration whose granted skill currently requires human auth (migration 065
-- added skills.requires_human_auth earlier). Without this, those pre-existing
-- grants keep approved_requires_human_auth=false, so a later publisher drop of
-- the current flag would let the (current OR approved) gate fall open. Capture
-- the current requirement for every existing grant whose live skill needs it.
UPDATE skill_grants AS sg
SET approved_requires_human_auth = true
FROM skills AS s
WHERE s.peer_id = sg.granter_peer_id
  AND s.name = sg.skill_name
  AND s.is_current = true
  AND s.deleted_at IS NULL
  AND s.requires_human_auth = true;
