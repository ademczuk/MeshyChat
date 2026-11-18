-- DOWN for sql_meshychat/028. `sqlmigrate.py --rollback-to meshychat:27` runs it (the rollback gate,
-- apps/core/rollback.py). The prune code that names memory.list must go back FIRST: with
-- this index gone its victim query matches no index and reads the table (092's header).
-- WHAT COMES BACK: nothing else; 092's index was never dropped. WHAT IS LOST: nothing; a list
-- result already replaced by its summary stays replaced.
DROP INDEX IF EXISTS skill_calls_result_prune_v2_idx;
DELETE FROM meshychat_migrations WHERE version = 28;
