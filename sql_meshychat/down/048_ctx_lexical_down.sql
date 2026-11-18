-- DOWN for sql_meshychat/048. `sqlmigrate.py --rollback-to meshychat:47` runs it. Derived data: a re-walk rebuilds it.
SET LOCAL lock_timeout = '5s';

DROP TABLE IF EXISTS mesh_ctx_lexical;
ALTER TABLE ctx_index_state DROP COLUMN IF EXISTS lexical_rewalk_until, DROP COLUMN IF EXISTS lexical_derivation;
