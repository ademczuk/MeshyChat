-- DOWN for sql_meshychat/057. `sqlmigrate.py --rollback-to meshychat:56` runs it (apps/core/rollback.py), in one
-- transaction with the deletion of ledger row 57, and tests/test_rollback_gate.py holds it to the catalog 057 found.
--
-- ORDER: roll the code back first. apps/cards/store.py from 057 on reads card_heads and fails without it
-- (UndefinedTable); the code before 057 reads shared_memory alone and never reads these objects.
--
-- WHAT IS LOST: nothing that is not in shared_memory. The records are derived from the head rows, and 057's
-- backfill makes them again if it is applied again.

SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS shared_memory_card_head ON shared_memory;
DROP FUNCTION IF EXISTS public.meshy_card_head_track();
DROP FUNCTION IF EXISTS public.meshy_card_head_sync(text, text);
DROP FUNCTION IF EXISTS public.meshy_card_head_put(uuid, text, text, integer, timestamptz, jsonb);
DROP TABLE IF EXISTS card_heads;
