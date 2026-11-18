-- DOWN for sql_meshychat/033. `sqlmigrate.py --rollback-to meshychat:32` runs it.
DROP INDEX IF EXISTS skill_calls_args_attachments_gin;
DROP INDEX IF EXISTS skill_calls_result_attachments_gin;
DELETE FROM meshychat_migrations WHERE version = 33;
