-- 022_call_worker_code_hash.sql
-- Where `run_call_worker` records the hash of the code it runs (rule B9; divergence 041, the
-- operator's decision of 2026-09-25 on the S5 review's F5 other half).
--
-- WHY A COLUMN ON background_job_leases. B9 asks health to report "a content hash of the
-- running code", and its incident is a daemon running stale code that no hash could
-- identify. A one-shot `run_call_worker --health` runs in a process of its own and would
-- hash the checker, never the loop that runs, and the loop had nowhere to put its own hash
-- (041 S5 review, F5). The deadline sweep's lease row is already the worker's shared state
-- (`last_finished_at` is health's other half), so every tick of the worker writes its hash
-- there, on the `call_deadline_sweep` row, whether or not that tick claimed the sweep:
--   worker_code_sha256  the full hex SHA-256 of the served tree as the worker process first
--                       read it (apps/core/code_fingerprint.running());
--   worker_code_at      when a tick last wrote it, on the database clock.
-- `run_call_worker --health` and `/v1/health` compare it with the code THEY serve and report
-- a stale worker. The shared `run_jobs` loop never writes these columns, and no other job's
-- row carries them.
--
-- The pair is written together or not at all, and the hash is 64 lowercase hex characters:
-- a CHECK each, so a malformed write fails loudly instead of reading as "stale".
--
-- A BOUNDED WAIT FOR ITS LOCK, as 014, 020 and 021 have: ALTER TABLE takes ACCESS EXCLUSIVE
-- on background_job_leases, which every job runner claims and releases rows of; queued
-- behind a long transaction it would hold every tick behind itself. It gives up after 5 s
-- instead; retry. Adding a nullable column with no default rewrites nothing.
--
-- Reversal: sql_meshychat/down/022_call_worker_code_hash_down.sql. DEPLOY ORDER: migrate,
-- then roll the code (the worker's tick writes these columns); back: roll the code back,
-- then run the down script.

SET LOCAL lock_timeout = '5s';

ALTER TABLE background_job_leases
    ADD COLUMN worker_code_sha256 TEXT,
    ADD COLUMN worker_code_at     TIMESTAMPTZ,
    ADD CONSTRAINT background_job_leases_worker_code_sha256_hex
        CHECK (worker_code_sha256 IS NULL OR worker_code_sha256 ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT background_job_leases_worker_code_pairs
        CHECK ((worker_code_sha256 IS NULL) = (worker_code_at IS NULL));

COMMENT ON COLUMN background_job_leases.worker_code_sha256 IS
    'run_call_worker only, on the call_deadline_sweep row: hex SHA-256 of the served tree as the worker process first read it, written every tick (rule B9, sql_meshychat/022).';
