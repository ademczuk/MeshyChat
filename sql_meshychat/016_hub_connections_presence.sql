-- MeshyChat divergence 041, slice S2: `hub_connections`, which sockets each worker process
-- holds, so a process that does not hold a peer's socket can tell whether some process does.
--
-- WHY A TABLE. Upstream decides "online" from a Redis key (`pipeline.rs:409-414`, as the
-- design cites it at f6bb721). This rebuild's presence is the per-process ConnectionManager
-- (apps/wsgw/presence.py, divergence 020). Django serves each HTTP request in a thread that
-- does not own the event loop the sockets live on, and with more than one worker the HTTP
-- invoke and the target's socket are usually in DIFFERENT processes, so the in-process
-- registry cannot answer "is the target connected". One daphne process runs today, where
-- the registry is exact; the table is kept so the port is not wrong the day a second worker
-- starts (design docs/design/skill-frames-port.md 4.3, Q8, and Q14 after the `queued`
-- safety net: without it every remote call would pass through `queued`).
--
-- WHAT A ROW MEANS. One row per open socket, written by the worker holding it, keyed by
-- that socket's connection_id. `worker_token` is random per process start. The worker
-- UPSERTS every row it holds every 10 s (a real upsert, so an insert that failed once is
-- repaired by the next beat), and deletes a row when its socket closes. A peer is ONLINE
-- when it has a row with `seen_at > now() - interval '30 seconds'`: three beats, on the
-- DATABASE clock, deliberately shorter than upstream's 120 s Redis TTL (a crashed worker's
-- rows would otherwise say "online" for four times the 30 s default deadline). A crashed
-- worker's rows age out; they are not deleted by anything in this slice.
--
-- NOTHING WRITES OR READS IT YET. apps/wsgw/hub_presence.py holds the store functions. The
-- consumer does not call them: 4.3 requires a row to be written only after the worker's
-- CallBridge reports LISTENING, and the beat to run on the CallBridge thread, and the
-- CallBridge first exists in S3. A row from a worker that cannot deliver would route calls
-- into a black hole. Step 9 reads `is_online` from S5. The dashboard's `ws_online` is
-- unchanged.
--
-- RLS: every command under app.server_internal (critique F13). A presence row is a routing
-- input: a request that could write one could make an offline peer look online and strand
-- its calls in `routed` with no socket and no doorbell; one that could delete one could
-- send an online peer's calls to `queued`. So SELECT, INSERT, UPDATE and DELETE each require
-- `app.server_internal = '1'`, in the shape 012 gives skill_calls' UPDATE and DELETE and 013
-- gives its tables (one policy per command), without 008's projection_writer flag: this is
-- not a projection, and its writer is the server's CallBridge thread, not a job. ENABLE and
-- FORCE. The ON DELETE CASCADE from peers is a referential action, which RLS does not bind.
-- meshy_app's grants come from sql/010's default privileges, so no GRANT appears here.

-- A BOUNDED WAIT FOR ITS LOCKS (added by the S9 review of a6bb87af, finding F4, which found
-- 021's unbounded wait and this file's shape in it). The foreign key below takes SHARE ROW
-- EXCLUSIVE on `peers`; queued behind an open writer of `peers`, it would hold every later
-- writer behind it. Give up after 5 s instead, as 014 does. LOCAL: it ends with the file's
-- own transaction.
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS hub_connections (
    connection_id  uuid        PRIMARY KEY,
    peer_id        uuid        NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    worker_token   uuid        NOT NULL,
    connected_at   timestamptz NOT NULL DEFAULT now(),
    seen_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hub_connections_peer_seen_idx ON hub_connections (peer_id, seen_at);

ALTER TABLE hub_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE hub_connections FORCE ROW LEVEL SECURITY;
CREATE POLICY hub_connections_read ON hub_connections
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY hub_connections_insert ON hub_connections
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY hub_connections_update ON hub_connections
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY hub_connections_delete ON hub_connections
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1');

COMMENT ON TABLE hub_connections IS
    'Cross-worker WebSocket presence (divergence 041 S2): one row per open socket, upserted every 10 s by the worker holding it; a peer is online when a row has seen_at within 30 s. Every command requires app.server_internal.';
