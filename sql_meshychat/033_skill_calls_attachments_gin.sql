-- Index skill_calls attachment arrays with GIN (STATUS F34, C18).
--
-- WHY. The participant (cross-owner) file read (apps/files/repository.py,
-- find_referencing_calls, refs.rs:45-75) matches a file against args_attachments and
-- result_attachments. Without indexes on either array, every cross-owner read walks
-- the entire skill_calls table.
--
-- PREDICATE REWRITE. PostgreSQL's built-in GIN array operator class supports array
-- containment (@>), so the query is rewritten to match:
--   args_attachments @> ARRAY[%s]::uuid[] OR result_attachments @> ARRAY[%s]::uuid[]
--
-- WRITE LOCK. Both CREATE INDEX statements take SHARE lock on skill_calls. We set
-- lock_timeout = '5s' to prevent queueing behind long transactions.
--
-- REVERSAL: sql_meshychat/down/033_skill_calls_attachments_gin_down.sql.

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS skill_calls_args_attachments_gin
    ON skill_calls USING GIN (args_attachments);

CREATE INDEX IF NOT EXISTS skill_calls_result_attachments_gin
    ON skill_calls USING GIN (result_attachments);
