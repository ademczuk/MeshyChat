-- DOWN for sql_meshychat/041. `sqlmigrate.py --rollback-to meshychat:40` runs it. Code that calls
-- public.file_referencing_calls (apps/files/repository.find_referencing_calls after divergence 095) must be rolled back
-- first: without the function its presign path fails with UndefinedFunction.
DROP FUNCTION IF EXISTS public.file_referencing_calls(uuid);

DELETE FROM meshychat_migrations WHERE version = 41;
