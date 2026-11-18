-- Seat usage beside the turn report (seat-state design v2.1, moved into the turn report: memory-bxxxxx).
-- usage holds what the seat's own status line or app server last showed: model, context use and plan windows.
-- usage_at is when those numbers were OBSERVED by the seat, not when the server received them, so a seat that keeps
-- reporting one old snapshot goes stale on time. A report without usage leaves both as they are.
SET lock_timeout = '2s';
ALTER TABLE mesh_presence
    ADD COLUMN usage JSONB,
    ADD COLUMN usage_at TIMESTAMPTZ,
    ADD CONSTRAINT mesh_presence_usage_pair CHECK ((usage IS NULL) = (usage_at IS NULL)),
    ADD CONSTRAINT mesh_presence_usage_object CHECK (usage IS NULL OR jsonb_typeof(usage) = 'object');
