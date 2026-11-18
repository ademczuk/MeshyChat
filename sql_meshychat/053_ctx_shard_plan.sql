-- MeshyChat, v1.1 step 6b: a STORED shard plan for mesh_ctx_chunks, kept equal to apps/meshsem/ctxindex's aggregate.
--
-- WHY: ctxindex._plan_rows numbered every current chunk of a namespace with row_number() OVER (seat, month ORDER BY id)
-- and grouped the result; the plan cache rebuilt it whenever ctx_chunk_changes (049) moved, so after every write. At 15M
-- current rows in one namespace that one statement passed the 30 s statement_timeout on the first search
-- (memory-bxxxxx), and it grows with the namespace. With this table the plan read is O(shards).
--
-- THE INVARIANT: at every commit, ctx_shard_plan holds exactly the rows the aggregate would return for each (space,
-- namespace): shard k of a (seat, month) is its current chunks number k*shard_max .. (k+1)*shard_max-1 in id order,
-- with lo/hi the first and last id and n the count. tests/test_ctx_shard_plan.py checks it row for row after a mixed
-- sequence of writes, the bench harness's own load included (memory-bxxxxx point 3).
--
-- HOW (design v2, kxxx, after memory-bxxxxx and pxx): statement-level triggers with transition
-- tables, one place for every writer: ctxjob, retention, the bench harness and any migration (pxx point 1).
--   INSERT  chunk ids only grow, so a statement's new current rows of a (seat, month) come after every row already
--           planned there: they fill that group's last shard up to shard_max, then open new shards. O(inserted rows).
--           When they do not (a concurrent writer committed higher ids first), the group is recomputed instead.
--   UPDATE  a change of `current`, seat, namespace, space or time_last recomputes the groups of the old and new rows.
--   DELETE  recomputes the groups of the deleted current rows (memory-bxxxxx point 1).
--           A recompute is the aggregate restricted to one (space, namespace, seat, month), through 052's partial index
--           (space, namespace, seat, time_last) WHERE current. Shards are renumbered over current rows as before, so
--           none is left thin by flips (memory-bxxxxx point 2).
--   TRUNCATE empties the plan (pxx point 2).
-- WHO WRITES IT: only SECURITY DEFINER functions, as 049's counter: a chunk delete can come from any role through
-- shared_memory's ON DELETE CASCADE, and it must not fail on the plan's policies. The trigger functions themselves are
-- NOT definer: they only read the statement's transition tables and pass ids or (space, namespace, seat, month) groups
-- to the definer helpers. The helpers are correct whatever they are passed: the insert helper re-reads the rows by id and
-- appends only rows newer than the group's plan (anything else recomputes the group), and a group recompute reads the
-- chunks. So a caller can make them do work, never make the plan wrong. meshychat_app holds no write on the tables.
-- LOCKS: every plan write first takes ctxjob's per-space advisory lock (1835233134, hashtext(space)), which ctxjob
-- already holds for its writes, spaces in sorted order. Plan writes of one space are therefore serialized, and a
-- recompute that waited sees the rows the other writer committed (each statement of a plpgsql function takes a fresh
-- snapshot under READ COMMITTED). EVERY WRITER TAKES THE SPACE LOCK BEFORE IT TOUCHES A CHUNK ROW (pxx P2-2):
-- ctxjob does; ctxindex's retention deletes one space at a time under it. A writer that locked rows first and met the
-- lock only in the trigger could deadlock with ctxjob, which holds the lock and then wants those rows.
-- CASCADES (pxx P2-1): a shared_memory delete cascades into mesh_ctx_chunks and locks those rows before the
-- delete trigger takes the space lock, the inversion retention had. It cannot meet ctxjob on the same rows, because
-- no application path deletes a context manifest: memory.prune refuses the context namespace
-- (apps/memoryskills/handlers.py, "memory.prune never compacts the context namespace"), and the only other delete,
-- the ephemeral rewrite in memory.write, deletes an existing key, which the context namespace refuses first
-- (apps/context/validation.refuse_existing_head: every context key is written once). An operator's manual delete of a
-- manifest during ingestion could still deadlock; Postgres aborts one side, and the drift check repairs the plan.
-- EXECUTE: _groups and _inserted stay callable by every role, because the trigger functions run as the role that wrote
-- the chunk (a cascade from shared_memory included), and the set of those roles is not enumerated here; a revoke that
-- missed one would make that role's chunk deletes fail. A caller can make them do work, and can hold a space's lock
-- until its own transaction ends; it cannot make the plan wrong.
-- SHARD SIZE: ctx_shard_setting holds the shard_max the triggers cut with (ctxindex.SHARD_MAX, 50000). ctxindex reads the
-- stored plan only while its SHARD_MAX equals it, and otherwise computes the aggregate as before.
-- DEPLOY (pxx point 3): the backfill and the triggers are created in this ONE transaction, after LOCK TABLE ... IN SHARE
-- ROW EXCLUSIVE MODE, so no chunk write commits between the backfill's snapshot and the triggers. The backfill is the
-- aggregate over every current chunk: it gets its own statement_timeout. The migrating route
-- (redeploy_prod_migrate.ps1 -ExpectPending '53'), after Bxxxxx's go. Down: down/053.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30min';

CREATE TABLE IF NOT EXISTS ctx_shard_setting (
    one        boolean PRIMARY KEY DEFAULT true CHECK (one),
    shard_max  integer NOT NULL CHECK (shard_max > 0)
);
INSERT INTO ctx_shard_setting (one, shard_max) VALUES (true, 50000) ON CONFLICT (one) DO NOTHING;

CREATE TABLE IF NOT EXISTS ctx_shard_plan (
    space      text        NOT NULL,
    namespace  text        NOT NULL,
    seat       text        NOT NULL,
    month      timestamptz NOT NULL,
    shard_no   integer     NOT NULL CHECK (shard_no >= 0),
    lo         bigint      NOT NULL,
    hi         bigint      NOT NULL CHECK (hi >= lo),
    n          integer     NOT NULL CHECK (n > 0),
    PRIMARY KEY (space, namespace, seat, month, shard_no)
);

ALTER TABLE ctx_shard_setting ENABLE ROW LEVEL SECURITY;
ALTER TABLE ctx_shard_plan ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ctx_shard_setting_read ON ctx_shard_setting;
CREATE POLICY ctx_shard_setting_read ON ctx_shard_setting
    FOR SELECT USING (current_setting('app.server_internal', true) = '1');
DROP POLICY IF EXISTS ctx_shard_plan_read ON ctx_shard_plan;
CREATE POLICY ctx_shard_plan_read ON ctx_shard_plan
    FOR SELECT USING (current_setting('app.server_internal', true) = '1');
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ctx_shard_setting FROM meshychat_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ctx_shard_plan FROM meshychat_app;

-- The month a chunk's time_last falls in, as ctxindex computes it (a UTC month start).
CREATE OR REPLACE FUNCTION public.meshy_ctx_month(t timestamptz) RETURNS timestamptz
LANGUAGE sql IMMUTABLE
AS $$ SELECT date_trunc('month', t AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' $$;

-- ctxjob's space lock for every space named, in sorted order.
CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_lock(spaces text[]) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    sp text;
BEGIN
    FOR sp IN SELECT DISTINCT s FROM unnest(spaces) AS u(s) WHERE s IS NOT NULL ORDER BY 1 LOOP
        PERFORM pg_advisory_xact_lock(1835233134, hashtext(sp));
    END LOOP;
END;
$$;
-- Only the definer helpers below call it (as the owner), so no other role needs it (pxx INFO).
REVOKE EXECUTE ON FUNCTION public.meshy_ctx_shard_plan_lock(text[]) FROM PUBLIC;

-- Recompute one (space, namespace, seat, month) from its current chunks: the aggregate, restricted to the group. The
-- caller holds the space lock.
CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_group(sp text, ns text, st text, mo timestamptz)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    sm integer := (SELECT s.shard_max FROM public.ctx_shard_setting s WHERE s.one);
BEGIN
    -- A month as meshy_ctx_month gives it, whatever the caller passed: _groups is callable by every role, and a
    -- mid-month value would write a group under a key no chunk maps to (GitHub Codex on 01a0f9aa, pxx P2-2).
    mo := public.meshy_ctx_month(mo);
    DELETE FROM public.ctx_shard_plan p WHERE p.space = sp AND p.namespace = ns AND p.seat = st AND p.month = mo;
    INSERT INTO public.ctx_shard_plan (space, namespace, seat, month, shard_no, lo, hi, n)
    SELECT sp, ns, st, mo, x.k, min(x.id), max(x.id), count(*)
      FROM (SELECT c.id, ((row_number() OVER (ORDER BY c.id)) - 1) / sm AS k
              FROM public.mesh_ctx_chunks c
             WHERE c.space = sp AND c.namespace = ns AND c.seat = st AND c.current
               -- The month's end in UTC, as meshy_ctx_month: `mo + interval '1 month'` is applied in the session's
               -- TimeZone and moves by the DST offset (pxx P2-1).
               AND c.time_last >= mo AND c.time_last < ((mo AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC') x
     GROUP BY x.k;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.meshy_ctx_shard_plan_group(text, text, text, timestamptz) FROM PUBLIC;

-- Groups to recompute (a delete's or an update's): locks their spaces, then recomputes each.
CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_groups(sps text[], nss text[], seats text[], months timestamptz[])
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    g record;
BEGIN
    PERFORM public.meshy_ctx_shard_plan_lock(sps);
    FOR g IN SELECT DISTINCT u.sp, u.ns, u.st, u.mo FROM unnest(sps, nss, seats, months) AS u(sp, ns, st, mo)
              ORDER BY 1, 2, 3, 4 LOOP
        PERFORM public.meshy_ctx_shard_plan_group(g.sp, g.ns, g.st, g.mo);
    END LOOP;
END;
$$;

-- Newly inserted chunks, by id: each group's rows append after its plan, or the group is recomputed.
CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_inserted(ids bigint[]) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    sm integer := (SELECT s.shard_max FROM public.ctx_shard_setting s WHERE s.one);
    g record;
    last_no integer;
    last_n integer;
    room integer;
BEGIN
    PERFORM public.meshy_ctx_shard_plan_lock(ARRAY(SELECT DISTINCT c.space FROM public.mesh_ctx_chunks c
                                                    WHERE c.id = ANY(ids) AND c.current));
    FOR g IN
        SELECT r.space, r.namespace, r.seat, r.mo, min(r.id) AS first_id,
               (SELECT max(p.hi) FROM public.ctx_shard_plan p WHERE p.space = r.space AND p.namespace = r.namespace
                   AND p.seat = r.seat AND p.month = r.mo) AS planned_hi
          FROM (SELECT c.space, c.namespace, c.seat, public.meshy_ctx_month(c.time_last) AS mo, c.id
                  FROM public.mesh_ctx_chunks c WHERE c.id = ANY(ids) AND c.current) r
         GROUP BY r.space, r.namespace, r.seat, r.mo
         ORDER BY 1, 2, 3, 4
    LOOP
        IF g.planned_hi IS NOT NULL AND g.first_id <= g.planned_hi THEN
            PERFORM public.meshy_ctx_shard_plan_group(g.space, g.namespace, g.seat, g.mo);
            CONTINUE;
        END IF;
        last_no := NULL;
        last_n := NULL;
        SELECT p.shard_no, p.n INTO last_no, last_n FROM public.ctx_shard_plan p
         WHERE p.space = g.space AND p.namespace = g.namespace AND p.seat = g.seat AND p.month = g.mo
         ORDER BY p.shard_no DESC LIMIT 1;
        room := CASE WHEN last_no IS NULL THEN 0 ELSE sm - last_n END;
        INSERT INTO public.ctx_shard_plan AS p (space, namespace, seat, month, shard_no, lo, hi, n)
        SELECT g.space, g.namespace, g.seat, g.mo, x.shard_no, min(x.id), max(x.id), count(*)
          FROM (SELECT y.id, CASE WHEN last_no IS NULL THEN y.k / sm
                                  WHEN y.k < room THEN last_no
                                  ELSE last_no + 1 + (y.k - room) / sm END AS shard_no
                  FROM (SELECT c.id, (row_number() OVER (ORDER BY c.id)) - 1 AS k
                          FROM public.mesh_ctx_chunks c
                         WHERE c.id = ANY(ids) AND c.current AND c.space = g.space AND c.namespace = g.namespace
                           AND c.seat = g.seat AND public.meshy_ctx_month(c.time_last) = g.mo) y) x
         GROUP BY x.shard_no
        ON CONFLICT (space, namespace, seat, month, shard_no) DO UPDATE
            SET lo = least(p.lo, EXCLUDED.lo), hi = greatest(p.hi, EXCLUDED.hi), n = p.n + EXCLUDED.n;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_truncate() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    DELETE FROM public.ctx_shard_plan;
    RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.meshy_ctx_shard_plan_truncate() FROM PUBLIC;

-- Rebuild every group from the chunks: the backfill below, and the repair after ctx_shard_setting changes.
CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_rebuild() RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    sm integer;
    rows_written bigint;
BEGIN
    -- Every chunk writer waits until this commits, before the setting is read: run beside ingestion, the rebuild's
    -- DELETE and INSERT would otherwise race the triggers' appends cut at the old size (GitHub Codex on 01a0f9aa, pxx
    -- d5d7e9 P2-4). The migration takes the same lock; taking it twice in one transaction is a no-op.
    LOCK TABLE public.mesh_ctx_chunks IN SHARE ROW EXCLUSIVE MODE;
    sm := (SELECT s.shard_max FROM public.ctx_shard_setting s WHERE s.one);
    DELETE FROM public.ctx_shard_plan;
    INSERT INTO public.ctx_shard_plan (space, namespace, seat, month, shard_no, lo, hi, n)
    SELECT x.space, x.namespace, x.seat, x.mo, x.k, min(x.id), max(x.id), count(*)
      FROM (SELECT c.space, c.namespace, c.seat, public.meshy_ctx_month(c.time_last) AS mo, c.id,
                   ((row_number() OVER (PARTITION BY c.space, c.namespace, c.seat, public.meshy_ctx_month(c.time_last)
                                        ORDER BY c.id)) - 1) / sm AS k
              FROM public.mesh_ctx_chunks c WHERE c.current) x
     GROUP BY x.space, x.namespace, x.seat, x.mo, x.k;
    GET DIAGNOSTICS rows_written = ROW_COUNT;
    RETURN rows_written;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.meshy_ctx_shard_plan_rebuild() FROM PUBLIC;

-- The trigger functions: NOT definer (see WHO WRITES IT); they read the transition tables and call the helpers.
CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_on_insert() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    PERFORM public.meshy_ctx_shard_plan_inserted(ARRAY(SELECT n.id FROM new_rows n WHERE n.current));
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_on_update() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    sps text[];
    nss text[];
    seats text[];
    months timestamptz[];
BEGIN
    -- Only rows whose plan-relevant columns changed, and only the groups a current row leaves or enters.
    SELECT array_agg(t.sp), array_agg(t.ns), array_agg(t.st), array_agg(t.mo) INTO sps, nss, seats, months
      FROM (SELECT o.space AS sp, o.namespace AS ns, o.seat AS st, public.meshy_ctx_month(o.time_last) AS mo
              FROM old_rows o JOIN new_rows n ON n.id = o.id
             WHERE o.current AND (NOT n.current OR (o.space, o.namespace, o.seat, o.time_last)
                                           IS DISTINCT FROM (n.space, n.namespace, n.seat, n.time_last))
            UNION
            SELECT n.space, n.namespace, n.seat, public.meshy_ctx_month(n.time_last)
              FROM old_rows o JOIN new_rows n ON n.id = o.id
             WHERE n.current AND (NOT o.current OR (o.space, o.namespace, o.seat, o.time_last)
                                           IS DISTINCT FROM (n.space, n.namespace, n.seat, n.time_last))) t;
    IF sps IS NOT NULL THEN
        PERFORM public.meshy_ctx_shard_plan_groups(sps, nss, seats, months);
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION public.meshy_ctx_shard_plan_on_delete() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    sps text[];
    nss text[];
    seats text[];
    months timestamptz[];
BEGIN
    SELECT array_agg(t.sp), array_agg(t.ns), array_agg(t.st), array_agg(t.mo) INTO sps, nss, seats, months
      FROM (SELECT DISTINCT o.space AS sp, o.namespace AS ns, o.seat AS st, public.meshy_ctx_month(o.time_last) AS mo
              FROM old_rows o WHERE o.current) t;
    IF sps IS NOT NULL THEN
        PERFORM public.meshy_ctx_shard_plan_groups(sps, nss, seats, months);
    END IF;
    RETURN NULL;
END;
$$;

LOCK TABLE mesh_ctx_chunks IN SHARE ROW EXCLUSIVE MODE;
SELECT public.meshy_ctx_shard_plan_rebuild();

DROP TRIGGER IF EXISTS mesh_ctx_chunks_plan_insert ON mesh_ctx_chunks;
CREATE TRIGGER mesh_ctx_chunks_plan_insert AFTER INSERT ON mesh_ctx_chunks
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION public.meshy_ctx_shard_plan_on_insert();
DROP TRIGGER IF EXISTS mesh_ctx_chunks_plan_update ON mesh_ctx_chunks;
CREATE TRIGGER mesh_ctx_chunks_plan_update AFTER UPDATE ON mesh_ctx_chunks
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT
    EXECUTE FUNCTION public.meshy_ctx_shard_plan_on_update();
DROP TRIGGER IF EXISTS mesh_ctx_chunks_plan_delete ON mesh_ctx_chunks;
CREATE TRIGGER mesh_ctx_chunks_plan_delete AFTER DELETE ON mesh_ctx_chunks
    REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION public.meshy_ctx_shard_plan_on_delete();
DROP TRIGGER IF EXISTS mesh_ctx_chunks_plan_truncate ON mesh_ctx_chunks;
CREATE TRIGGER mesh_ctx_chunks_plan_truncate AFTER TRUNCATE ON mesh_ctx_chunks
    FOR EACH STATEMENT EXECUTE FUNCTION public.meshy_ctx_shard_plan_truncate();
