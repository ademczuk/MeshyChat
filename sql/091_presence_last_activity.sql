-- 091: split heartbeat liveness from work activity on the advisory roster.
--
-- last_beat is written only by POST /v1/peers/me/presence. last_activity is
-- written by the invoke work path, so a peer can be working without beating or
-- beating without working.

ALTER TABLE mesh_presence
    ADD COLUMN IF NOT EXISTS last_activity TIMESTAMPTZ;

UPDATE mesh_presence
SET last_activity = last_beat
WHERE last_activity IS NULL
  AND last_beat IS NOT NULL;

ALTER TABLE mesh_presence
    ALTER COLUMN last_beat DROP DEFAULT,
    ALTER COLUMN last_beat DROP NOT NULL;

CREATE INDEX IF NOT EXISTS mesh_presence_last_activity_idx ON mesh_presence (last_activity)
    WHERE last_activity IS NOT NULL;
