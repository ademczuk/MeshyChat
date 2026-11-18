SET LOCAL lock_timeout = '5s';

-- A peer a mesh message is addressed to may download the files the message attaches (issue #118, operator ruling
-- 2026-10-12: keep the owner-wide rule, ADD the recipient rule and a pickup receipt). Divergence 098.
--
-- file_message_grants: one row per (file, addressed peer, message), written in the memory.write transaction that stores
-- the message (apps/files/grants.record_message_grants), and only when the SENDER could already read the file by rule 1
-- or 2, so a message cannot mint access to a file id it guessed. Recipients are resolved from `to`/`cc` when the message
-- is written; a later roster change neither adds nor removes readers. A redaction of the message deletes its rows.
--
-- file_downloads: one row per presigned download URL handed out (apps/files/service.presign_download), with the rule
-- that admitted the reader. It records that a URL was issued, not that the bytes arrived: the server never sees the GET.
CREATE TABLE IF NOT EXISTS file_message_grants (
    file_id         uuid        NOT NULL REFERENCES public.file_artifacts(id) ON DELETE CASCADE,
    peer_id         uuid        NOT NULL REFERENCES public.peers(id) ON DELETE CASCADE,
    message_key     text        NOT NULL,
    sender_peer_id  uuid        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (file_id, peer_id, message_key)
);
-- The redaction delete reads by message.
CREATE INDEX IF NOT EXISTS file_message_grants_message_idx ON public.file_message_grants (message_key);

CREATE TABLE IF NOT EXISTS file_downloads (
    id            bigserial   PRIMARY KEY,
    file_id       uuid        NOT NULL REFERENCES public.file_artifacts(id) ON DELETE CASCADE,
    peer_id       uuid        NOT NULL,
    rule          smallint    NOT NULL CHECK (rule IN (1, 2, 3)),
    sha256        text,
    -- The message whose grant admitted a rule-3 reader; NULL for rules 1 and 2.
    message_key   text,
    presigned_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((rule = 3) = (message_key IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS file_downloads_file_idx ON public.file_downloads (file_id, presigned_at DESC);

ALTER TABLE public.file_message_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.file_message_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS file_message_grants_server_internal ON public.file_message_grants;
CREATE POLICY file_message_grants_server_internal ON public.file_message_grants
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER TABLE public.file_downloads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.file_downloads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS file_downloads_server_internal ON public.file_downloads;
CREATE POLICY file_downloads_server_internal ON public.file_downloads
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

GRANT SELECT, INSERT, DELETE ON public.file_message_grants TO meshy_app;
GRANT SELECT, INSERT ON public.file_downloads TO meshy_app;
GRANT USAGE ON SEQUENCE public.file_downloads_id_seq TO meshy_app;
