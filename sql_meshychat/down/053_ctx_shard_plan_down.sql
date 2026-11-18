-- DOWN for sql_meshychat/053. `sqlmigrate.py --rollback-to meshychat:52` runs it. The plan is derived data: dropping it
-- loses nothing, and the code before 053 computes the aggregate itself.
SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS mesh_ctx_chunks_plan_insert ON mesh_ctx_chunks;
DROP TRIGGER IF EXISTS mesh_ctx_chunks_plan_update ON mesh_ctx_chunks;
DROP TRIGGER IF EXISTS mesh_ctx_chunks_plan_delete ON mesh_ctx_chunks;
DROP TRIGGER IF EXISTS mesh_ctx_chunks_plan_truncate ON mesh_ctx_chunks;
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_on_insert();
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_on_update();
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_on_delete();
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_truncate();
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_rebuild();
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_inserted(bigint[]);
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_groups(text[], text[], text[], timestamptz[]);
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_group(text, text, text, timestamptz);
DROP FUNCTION IF EXISTS public.meshy_ctx_shard_plan_lock(text[]);
DROP FUNCTION IF EXISTS public.meshy_ctx_month(timestamptz);
DROP TABLE IF EXISTS ctx_shard_plan;
DROP TABLE IF EXISTS ctx_shard_setting;
