-- DOWN for sql_meshychat/056. `sqlmigrate.py --rollback-to meshychat:55` runs it.
SET lock_timeout = '2s';
ALTER TABLE mesh_presence
    DROP CONSTRAINT mesh_presence_usage_object,
    DROP CONSTRAINT mesh_presence_usage_pair,
    DROP COLUMN usage_at,
    DROP COLUMN usage;
