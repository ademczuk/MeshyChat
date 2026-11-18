-- MeshyChat divergence 110: a narrow record of each decision card's head, kept on write, so GET /v1/cards
-- never reads a card's value to decide what to list.
--
-- WHAT WAS SLOW. GET /v1/cards (apps/cards/store.list_cards) cost 1.2 to 2.5 s a page on production whatever the
-- page size (measured read-only by the server's owner: 21,073 card heads in shared/decisions, values averaging
-- 1,421 bytes, shared_memory 4.5 GB). EXPLAIN (ANALYZE, BUFFERS) at limit 100, warm, 1,257 ms and 161,117 buffers:
-- the edge pass `e` read every head's value and unnested its `supersedes` (847 ms, 68%), the page CTE read every
-- head's value a second time for the seat filter (199 to 218 ms), and the page was joined to `s` as a nested loop
-- because every estimate made from a jsonb expression was off by three to four orders of magnitude (15 heads
-- estimated against 21,073; 1 edge against 5,601; 1 page row against 2,389), removing 565,609 rows (120 to 140 ms).
--
-- WHAT THIS ADDS.
--   1. card_heads: one row per HEAD row of shared_memory whose key is under card/, in every namespace (the card
--      namespace is a setting, MESHY_CARDS_NAMESPACE, and a test claims its own, so the database cannot know which
--      namespace is the cards'). It holds exactly the fields the list's filters, its order and its edge pass read,
--      as typed columns with real statistics, and the head row's id; never the text.
--   2. meshy_card_head_put(...): the ONE derivation of a record from a row, used by the trigger, by the backfill
--      below and by meshy_card_head_sync for a repair. Each column is the expression store.py used on the value:
--        has_text        jsonb_typeof(value->'text') = 'string'          (false when absent)
--        redacted        value->'redacted' = 'true'::jsonb               (false when absent)
--        card_time       value->>'time' when it is a CARD TIME, else NULL  (text, compared as text)
--        time_is_string  value->'time' is a string in the card time's one spelling (apps/cards/validation.TIME_RE)
--      A card time is 24 bytes, so the record of ANY card/ key fits card_heads_page_idx. Before this bound a peer's
--      write in another namespace (card validation checks only the card namespace) with key card/x and a 16 KB
--      "time" string was aborted by this trigger, because a btree entry is capped at about 2.7 KB, and one such
--      existing head would have aborted the backfill at deploy (pxx, round 1, C2). Keys over 1,024 bytes get no
--      record at all, for the same reason (the card namespace's keys are card/<seat>/<32 hex>). The one reader that
--      differs from the code before 110 is a card in the card namespace whose time is a string in another spelling:
--      it was listed, ordered by that string, and is now not listed. Validation has refused such a time since the
--      card namespace existed (parse_time), so only a row written past validation can hold one.
--        card_type, seat, owner, thread_id    value->>'<field>'
--        supersedes      the elements of value->'supersedes' as text when it is an array, else {}
--   3. a trigger, AFTER INSERT OR UPDATE on shared_memory for a card/ key, in the writer's own statement and
--      transaction: a head row puts its record; a row that stops being the head removes the record that names it.
--      A DELETE needs no trigger: the foreign key's ON DELETE CASCADE removes the record with its row.
--
-- WHY A TRIGGER, NOT EDITS TO THE WRITE PATHS. The rows a card list reads are written by memory.write (an append:
-- retire the head, insert the next version; an ephemeral namespace: delete, insert), memory.edit, memory.redact (an
-- UPDATE of the value in place, sql_meshychat/027), prune (a delete, with the parent unlink), and by every fixture
-- and tool that inserts a row directly (tools/related_calibration.py). A record kept by each of those would be
-- right only while each one remembered; the trigger fires for all of them, and for a writer added later, in the
-- same transaction as the row, so the record and the row commit or roll back together. It does not fire when a
-- superuser disables user triggers; meshy_card_head_sync repairs a key after such a write.
--
-- WRITERS AND READERS, as sql_meshychat/023 and 049 have it. The trigger function is SECURITY DEFINER with the one
-- path every definer function has (divergence 096) and names its relations public.-qualified. card_heads has RLS
-- ENABLED and not FORCED: readable under app.server_internal only, exactly as shared_memory is, so a context that
-- could not read the rows reads no records either; the definer (the migrating role, the table's owner) writes it
-- whatever its RLS standing; meshychat_app holds no write grant on it at all. The record is derived from nothing
-- but shared_memory, and this file's backfill is its rebuild.
--
-- ESTIMATES. ANALYZE at the end, so the first plan after the deploy already has the column statistics.
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 023 and 049 have: CREATE TRIGGER takes SHARE ROW EXCLUSIVE on shared_memory.
-- Holding it to the end of this file means every card row either committed before (and is backfilled) or waits for
-- this commit (and is put by the trigger).
--
-- OPERATOR NOTE: RUN THIS IN A QUIET WINDOW. The SHARE ROW EXCLUSIVE lock on shared_memory (taken by the CREATE TABLE's
-- REFERENCES and by CREATE TRIGGER) is held to COMMIT, across the full-scan backfill, the two counts and the ANALYZE,
-- and it blocks every write to shared_memory. Measured on scratch Postgres: about 1.8 s at 22,000 card heads in a
-- 110 MB heap. The hold grows with the shared_memory heap, not with the card count alone: production's heap is about
-- 326 MB (41,719 pages, 8 Nov; the 4.5 GB is mostly TOAST and indexes). lock_timeout bounds only the WAIT to acquire
-- the lock; it does not bound how long this file then holds it. Production's migrating route (redeploy_prod_migrate.ps1)
-- stops the server, the job runner and the call worker under MAINTENANCE and verifies the stop before it applies a
-- migration, so no application writer waits on this lock there: the hold adds to the outage, and the restart after it
-- (cold mail indexes, about 3 minutes degraded on 8 Nov) is the larger cost (kxxx, 8 Nov).

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS card_heads (
    namespace       text        NOT NULL,
    key             text        NOT NULL,
    memory_id       uuid        NOT NULL UNIQUE REFERENCES shared_memory(id) ON DELETE CASCADE,
    version         integer     NOT NULL,
    created_at      timestamptz NOT NULL,
    has_text        boolean     NOT NULL,
    redacted        boolean     NOT NULL,
    card_time       text,
    time_is_string  boolean     NOT NULL,
    card_type       text,
    seat            text,
    owner           text,
    thread_id       text,
    supersedes      text[]      NOT NULL DEFAULT '{}'::text[],
    PRIMARY KEY (namespace, key)
);

-- The page's order: newest first by the card's own time, then key, over the cards a page may list.
CREATE INDEX IF NOT EXISTS card_heads_page_idx
    ON card_heads (namespace, card_time DESC, key DESC) WHERE has_text AND time_is_string;
-- The edge pass reads only the records that name something.
CREATE INDEX IF NOT EXISTS card_heads_namers_idx
    ON card_heads (namespace) WHERE supersedes <> '{}'::text[];

ALTER TABLE card_heads ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS card_heads_read ON card_heads;
CREATE POLICY card_heads_read ON card_heads
    FOR SELECT USING (current_setting('app.server_internal', true) = '1');
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON card_heads FROM meshychat_app;

CREATE OR REPLACE FUNCTION public.meshy_card_head_put(p_id uuid, p_namespace text, p_key text, p_version integer,
                                                      p_created_at timestamptz, p_value jsonb)
RETURNS void
LANGUAGE sql
SET search_path = pg_catalog, public, pg_temp
AS $$
    INSERT INTO public.card_heads AS h (namespace, key, memory_id, version, created_at, has_text, redacted, card_time,
                                        time_is_string, card_type, seat, owner, thread_id, supersedes)
    VALUES (p_namespace, p_key, p_id, p_version, p_created_at,
            coalesce(jsonb_typeof(p_value -> 'text') = 'string', false),
            coalesce(p_value -> 'redacted' = 'true'::jsonb, false),
            CASE WHEN jsonb_typeof(p_value -> 'time') = 'string'
                      AND (p_value ->> 'time') ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$'
                 THEN p_value ->> 'time' END,
            coalesce(jsonb_typeof(p_value -> 'time') = 'string'
                     AND (p_value ->> 'time') ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$', false),
            p_value ->> 'type', p_value ->> 'seat', p_value ->> 'owner', p_value ->> 'thread_id',
            CASE WHEN jsonb_typeof(p_value -> 'supersedes') = 'array'
                 THEN ARRAY(SELECT jsonb_array_elements_text(p_value -> 'supersedes'))
                 ELSE '{}'::text[] END)
    ON CONFLICT (namespace, key) DO UPDATE
        SET memory_id = EXCLUDED.memory_id, version = EXCLUDED.version, created_at = EXCLUDED.created_at,
            has_text = EXCLUDED.has_text, redacted = EXCLUDED.redacted, card_time = EXCLUDED.card_time,
            time_is_string = EXCLUDED.time_is_string, card_type = EXCLUDED.card_type, seat = EXCLUDED.seat,
            owner = EXCLUDED.owner, thread_id = EXCLUDED.thread_id, supersedes = EXCLUDED.supersedes;
$$;
REVOKE EXECUTE ON FUNCTION public.meshy_card_head_put(uuid, text, text, integer, timestamptz, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.meshy_card_head_put(uuid, text, text, integer, timestamptz, jsonb) FROM meshychat_app;

CREATE OR REPLACE FUNCTION public.meshy_card_head_track() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    -- The definer writes card_heads, so it runs for shared_memory alone: a trigger on any other table (a temporary
    -- one with shared_memory's columns, say) would let that table's rows write records (pxx, round 1, C1).
    IF TG_RELID <> 'public.shared_memory'::regclass THEN
        RAISE EXCEPTION 'meshy_card_head_track runs on public.shared_memory only, not %', TG_RELID::regclass;
    END IF;
    IF NEW.is_head THEN
        PERFORM public.meshy_card_head_put(NEW.id, NEW.namespace, NEW.key, NEW.version, NEW.created_at, NEW.value);
    ELSE
        DELETE FROM public.card_heads WHERE memory_id = NEW.id;
    END IF;
    RETURN NULL;
END;
$$;
-- FROM meshychat_app as well as FROM PUBLIC: migration 010's ALTER DEFAULT PRIVILEGES grants the app role EXECUTE on
-- every new function directly (046 carries it to meshychat_app), and a REVOKE FROM PUBLIC leaves a direct grant. Without
-- EXECUTE the app role cannot CREATE TRIGGER with this function; the trigger on shared_memory still fires for its
-- writes, since a trigger's function is not privilege-checked when it fires (pxx, round 1, C1).
REVOKE EXECUTE ON FUNCTION public.meshy_card_head_track() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.meshy_card_head_track() FROM meshychat_app;

-- A repair for one key, for the owner and an operator: the record made again from the key's head row, or removed
-- when there is none. Reads shared_memory as its caller, so it needs a context that can read it.
CREATE OR REPLACE FUNCTION public.meshy_card_head_sync(p_namespace text, p_key text)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    DELETE FROM public.card_heads WHERE namespace = p_namespace AND key = p_key;
    PERFORM public.meshy_card_head_put(sm.id, sm.namespace, sm.key, sm.version, sm.created_at, sm.value)
       FROM public.shared_memory sm
      WHERE sm.namespace = p_namespace AND sm.key = p_key AND sm.is_head AND sm.key LIKE 'card/%'
        AND octet_length(sm.key) <= 1024;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.meshy_card_head_sync(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.meshy_card_head_sync(text, text) FROM meshychat_app;

DROP TRIGGER IF EXISTS shared_memory_card_head ON shared_memory;
CREATE TRIGGER shared_memory_card_head
    AFTER INSERT OR UPDATE ON shared_memory
    FOR EACH ROW
    WHEN (NEW.key LIKE 'card/%' AND octet_length(NEW.key) <= 1024)
    EXECUTE FUNCTION public.meshy_card_head_track();

-- The backfill: every head card already written. shared_memory is readable under app.server_internal only, and its
-- RLS is FORCED, so a migrating role that does not bypass RLS would read nothing here and backfill nothing: the
-- context is set for this transaction alone.
SELECT set_config('app.server_internal', '1', true);
SELECT public.meshy_card_head_put(sm.id, sm.namespace, sm.key, sm.version, sm.created_at, sm.value)
  FROM shared_memory sm
 WHERE sm.is_head AND sm.key LIKE 'card/%' AND octet_length(sm.key) <= 1024;

DO $$
DECLARE
    heads bigint;
    recs bigint;
BEGIN
    SELECT count(*) INTO heads FROM shared_memory WHERE is_head AND key LIKE 'card/%' AND octet_length(key) <= 1024;
    SELECT count(*) INTO recs FROM card_heads;
    IF heads <> recs THEN
        RAISE EXCEPTION 'card_heads backfill: % head cards, % records', heads, recs;
    END IF;
END;
$$;

ANALYZE card_heads;
