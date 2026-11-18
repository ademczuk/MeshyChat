-- Migration 036: shared mesh-chat WS-push trigger (candidate F).
--
-- A trigger on shared_memory mesh-chat message inserts emits a NOTIFY on the
-- 'mesh_msg' channel. The meshy-server mesh_notify bridge LISTENs on that
-- channel and pushes a ServerFrame::MeshMessage nudge to each active peer's
-- meshy:notify channel, so peers get near-instant delivery instead of waiting
-- on their client poll interval.
--
-- The trigger fires ONLY for actual messages (key LIKE 'msg/%'). Cursor/ack
-- tracking rows now live in shared/mesh-cursors (TASK-348), but the key prefix
-- guard remains so transient rows can never spam the nudge stream.
--
-- The payload is intentionally tiny (no message body): key, author peer id, the
-- opaque addressing field (length-bounded), and a Z-suffixed UTC timestamp at
-- second precision (consistent with the key's ts segment). That keeps it far
-- under PostgreSQL's 8000-byte NOTIFY limit and preserves the signed-read
-- discipline - the client reads the full row over its authenticated path; the
-- nudge only says "a new message exists, read it now". pg_notify is
-- transactional: the NOTIFY is delivered on COMMIT, so a rolled-back insert
-- never produces a phantom nudge. The PERFORM is exception-wrapped (see the
-- function body) so a nudge failure can never abort the message write.

CREATE OR REPLACE FUNCTION mesh_chat_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- The nudge is a pure delivery optimization and must NEVER be able to abort
    -- the message write. pg_notify can raise (e.g. 'payload string too long' if a
    -- writer ever puts a large value in the addressing field), and an unhandled
    -- trigger exception would roll back the triggering INSERT - turning an
    -- optimization into a write-availability risk. Wrap it so any failure
    -- degrades to a logged warning and the message still commits; the client
    -- poll stays authoritative. The addressing field is also bounded
    -- (left(.., 256)) so it can never bloat the payload - the client re-reads the
    -- full value from the row, so the bound is lossless in practice.
    BEGIN
        PERFORM pg_notify(
            'mesh_msg',
            json_build_object(
                'key', NEW.key,
                'from_peer_id', NEW.created_by_peer_id,
                'to', left(NEW.value ->> 'to', 256),
                'created_at', to_char(NEW.created_at AT TIME ZONE 'UTC',
                                      'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            )::text
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'mesh_chat_notify: nudge skipped for %, insert commits: %',
                      NEW.key, SQLERRM;
    END;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_mesh_notify ON shared_memory;
CREATE TRIGGER shared_memory_mesh_notify
    AFTER INSERT ON shared_memory
    FOR EACH ROW
    WHEN (NEW.namespace = 'shared/mesh-chat' AND NEW.key LIKE 'msg/%')
    EXECUTE FUNCTION mesh_chat_notify();
