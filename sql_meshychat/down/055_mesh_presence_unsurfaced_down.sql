-- DOWN for sql_meshychat/055. `sqlmigrate.py --rollback-to meshychat:54` runs it.
SET lock_timeout = '2s';
ALTER TABLE mesh_presence
    DROP CONSTRAINT mesh_presence_unsurfaced_pair,
    DROP COLUMN unsurfaced_since,
    DROP COLUMN unsurfaced_oldest_at,
    DROP COLUMN unsurfaced;
