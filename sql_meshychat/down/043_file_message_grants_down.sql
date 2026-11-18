-- DOWN for sql_meshychat/043. `sqlmigrate.py --rollback-to meshychat:42` runs it. Roll back the code that reads and
-- writes these tables first (apps/files/grants.py and its callers in memory.write, redaction and the presign path):
-- without the tables a mail write that attaches a file fails with UndefinedTable. The rows are lost with the tables;
-- recipients of other owners then get the 404 they got before 043, and the download receipts are gone.
DROP TABLE IF EXISTS public.file_downloads;
DROP TABLE IF EXISTS public.file_message_grants;

DELETE FROM meshychat_migrations WHERE version = 43;
