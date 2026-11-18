-- DOWN for sql_meshychat/040. `sqlmigrate.py --rollback-to meshychat:39` runs it. Back to 034's list. A tombstone written while 040 was applied may hold `schema`, `id`, `seat`,
-- `time` or `supersedes`; the trigger checks a row only when it is written, so those rows stay as
-- they are and are simply no longer read as superseding anything by code older than divergence 092.
CREATE OR REPLACE FUNCTION shared_memory_tombstone_keys() RETURNS text[]
    LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['redacted', 'reason_code', 'redacted_at',
                 'schema_version', 'from', 'to', 'cc', 'type', 'thread_id', 'in_reply_to', 'closes', 'requires_ack', 'ts', 'author_family']
$$;

DELETE FROM meshychat_migrations WHERE version = 40;
