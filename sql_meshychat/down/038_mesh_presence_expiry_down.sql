-- DOWN for sql_meshychat/038. `sqlmigrate.py --rollback-to meshychat:37` runs it. The expiry a beat
-- carried is lost; every state then reads as it did before 038 (it never expires).
ALTER TABLE mesh_presence DROP COLUMN IF EXISTS state_expires_at;

DELETE FROM meshychat_migrations WHERE version = 38;
