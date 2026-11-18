-- DOWN for sql_meshychat/054. `sqlmigrate.py --rollback-to meshychat:53` runs it.
ALTER TABLE mesh_presence
    DROP COLUMN turn_reported_at,
    DROP COLUMN turn_item_at,
    DROP COLUMN turn_state;
