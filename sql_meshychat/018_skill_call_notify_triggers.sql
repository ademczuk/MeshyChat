-- MeshyChat divergence 041, slice S3: two NOTIFY triggers, so a committed call status edge
-- and a committed chunk reach every serving worker's CallBridge as a POINTER.
--
-- NUMBERING, LOUDLY. This is the skill-frames track's SECOND migration, beyond its 016
-- reservation. Checked at build time (2026-09-24) and again before this slice closed:
-- origin/main (228a6ab) holds 001 to 013 and 017; 014 is reserved to B02 A1 (fin/b02-a1),
-- 015 to Biscuit, 016 is this track's hub_connections, and 017 is TAKEN by the thread
-- summaries' 017_mesh_summary_requests.sql, which integ/r7 landed on origin/main. So this
-- is 018, the next letter-free free number. `sqlmigrate.discover` refuses a duplicate
-- version, not a gap.
--
-- WHY TRIGGERS AND NOT pg_notify IN APPLICATION CODE (design docs/design/skill-frames-port.md
-- 4.5). A status is written by at least five code paths in three processes: step 9
-- (apps/calls/route.py), the cancel view, the frame handlers (S4), the deadline janitor
-- (apps/calls/janitor.py, run by run_jobs and later run_call_worker) and the doorbell (S9).
-- One of them would forget, most likely the janitor, and a caller's socket would then never
-- hear of a `timeout`. A trigger fires on every writer, including one this server does not
-- run: with a second server on the same database its writes notify this one's bridges too
-- (design 5.5, Q16).
--
-- POINTERS ONLY. NOTIFY is unauthenticated: any session on the database can send one on any
-- channel. So a payload names a row, and the CallBridge (apps/wsgw/call_notify.py) re-reads
-- the row before it acts on it (S5 onward). `status` and `target` are HINTS that let a
-- worker skip a notification for a target it holds no socket for without a read; a forged
-- hint costs at most one wasted read. Nothing caller- or target-controlled rides beyond the
-- call_id: not the args, the signature, the result, the chunk content.
--
-- THE BOUND. The call_id is the only caller-chosen text in a payload, and the application
-- stores one only within 200 UTF-8 bytes (apps/calls/repository.validate_call_id, meshy-types
-- CallId::try_new). A row with a longer one can only come from another writer; its edge and
-- its chunk commit and notify NOTHING (the WHEN clauses below), so no payload ever carries
-- more than 200 bytes of anyone's text, and pg_notify's own 8000-byte refusal, which would
-- abort the triggering write, cannot be reached. sql/036 wraps its PERFORM in an EXCEPTION
-- block for that refusal instead; that costs a subtransaction per row, and a chunk stream
-- writes many rows, so the payload is bounded here instead. The NOTIFY queue filling up is
-- raised at COMMIT, where no trigger could catch it either way.
--
-- AT COMMIT. pg_notify is transactional: the pointer is delivered when the transaction that
-- wrote the edge commits, and never for one rolled back. So a target can never read a
-- `routed` pointer for a row that is not yet `routed` (design 4.4).
--
-- An INSERT is not an edge: a call is created `pending`, and nothing waits on that.
-- `UPDATE OF status` fires for any UPDATE whose SET list names status; the WHEN clause keeps
-- only a real change, so `SET status = status` notifies nothing.

-- A BOUNDED WAIT FOR ITS LOCKS (added by the S9 review of a6bb87af, finding F4, which named
-- this file's shape). CREATE TRIGGER takes SHARE ROW EXCLUSIVE on `skill_calls` and
-- `skill_call_chunks`; queued behind an open writer of either, it would hold every later
-- writer behind it. Give up after 5 s instead, as 014 does. LOCAL: it ends with the file's
-- own transaction.
SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE FUNCTION meshy_call_status_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_catalog.pg_notify(
        'meshy_call',
        pg_catalog.json_build_object(
            'call_id', NEW.call_id,
            'status', NEW.status,
            'target', NEW.target_peer_id
        )::text
    );
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION meshy_call_chunk_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_catalog.pg_notify(
        'meshy_call_chunk',
        pg_catalog.json_build_object(
            'call_id', NEW.call_id,
            'sequence', NEW.sequence
        )::text
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS skill_calls_status_notify ON skill_calls;
CREATE TRIGGER skill_calls_status_notify
    AFTER UPDATE OF status ON skill_calls
    FOR EACH ROW
    WHEN (NEW.status IS DISTINCT FROM OLD.status AND octet_length(NEW.call_id) <= 200)
    EXECUTE FUNCTION meshy_call_status_notify();

DROP TRIGGER IF EXISTS skill_call_chunks_notify ON skill_call_chunks;
CREATE TRIGGER skill_call_chunks_notify
    AFTER INSERT ON skill_call_chunks
    FOR EACH ROW
    WHEN (octet_length(NEW.call_id) <= 200)
    EXECUTE FUNCTION meshy_call_chunk_notify();

COMMENT ON FUNCTION meshy_call_status_notify() IS
    'Divergence 041 S3: NOTIFY meshy_call {call_id, status, target} on a committed status edge. A pointer: the CallBridge re-reads the row.';
COMMENT ON FUNCTION meshy_call_chunk_notify() IS
    'Divergence 041 S3: NOTIFY meshy_call_chunk {call_id, sequence} on a committed chunk. A pointer: the CallBridge re-reads the row.';
