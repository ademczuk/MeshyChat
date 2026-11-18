-- DOWN for sql_meshychat/050. `sqlmigrate.py --rollback-to meshychat:49` runs it. Derived state: without it the sweep
-- falls back to ORPHAN_GRACE_S alone, which the code before 050 does.
SET LOCAL lock_timeout = '5s';

ALTER TABLE ctx_index_state DROP COLUMN IF EXISTS kept_at, DROP COLUMN IF EXISTS kept_seqs;
