-- 002_background_job_leases.sql
-- The lease table behind apps/jobs. See docs/DIVERGENCES.md 002.
--
-- WHY A TABLE AND NOT LISTEN/NOTIFY. NOTIFY delivers only to sessions listening at the
-- moment it fires; a notification sent while every worker is down is gone, with nothing
-- recording that it was ever due. The durable half of a queue is always a table. This
-- one holds one row per registered job, and that is the whole scheduler state.
--
-- WHY job_name IS THE PRIMARY KEY. The PK is the mutual exclusion. Two workers cannot
-- hold one job because there is only ever one row to hold, and the claim is a single
-- conditional UPDATE rather than a read-then-write. The database enforces it; no
-- application code can forget to.
--
-- Upstream parity note: this table has no counterpart in the Rust server, which runs its
-- 11 boot jobs as in-process tokio tasks with no shared claim at all. That design cannot
-- survive two server instances, which is why this diverges rather than ports.

CREATE TABLE IF NOT EXISTS background_job_leases (
    job_name             TEXT        PRIMARY KEY,

    -- Claim state. holder is NULL exactly when the job is not held.
    holder               TEXT,
    leased_until         TIMESTAMPTZ,

    -- Schedule state. next_run_at is the ONLY thing that makes a job due; it is written
    -- with jitter on release, never derived from process start. An hourly job anchored
    -- to process start is what produced the fixed-minute fleet-wide stall in upstream
    -- #385, and every instance that booted together shared the same minute forever.
    next_run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_started_at      TIMESTAMPTZ,
    last_finished_at     TIMESTAMPTZ,

    -- Outcome, kept so a failing job is visible without reading logs.
    last_result          TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER     NOT NULL DEFAULT 0,

    CONSTRAINT background_job_leases_holder_pairs_with_lease
        CHECK ((holder IS NULL) = (leased_until IS NULL))
);

-- The claim query filters on next_run_at and leased_until together, so the index that
-- serves it carries both. Without it a claim is a seq scan over every registered job on
-- every tick, which is small today and is exactly the shape rule B5 exists to refuse.
CREATE INDEX IF NOT EXISTS background_job_leases_due_idx
    ON background_job_leases (next_run_at, leased_until);

COMMENT ON TABLE background_job_leases IS
    'One row per registered background job. The primary key IS the mutual exclusion: a claim is a single conditional UPDATE, so two workers cannot hold the same job.';
