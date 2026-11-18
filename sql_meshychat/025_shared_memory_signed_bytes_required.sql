-- MeshyChat divergence 051: B02 phase A2. A shared_memory row without the bytes its writer
-- signed is refused from EVERY session. Design: docs/design/signed-bytes-status.md on
-- fin/design-signed-bytes (9aab90b), section 4.1's A2 row, 4.3's A2 down, Q10; approved by
-- the operator 2026-09-24 (decision 6), its rollback-window condition removed 2026-09-27
-- (decision 43: there is no rollback to the upstream stack).
--
-- WHAT A1 LEFT OPEN. sql_meshychat/014's agree-trigger refuses a NULL signed_bytes only from
-- a meshy_app session carrying this build's marker (meshy.signed_bytes_writer), and admits it
-- from every other session: the upstream daemon (`meshy`), upstream's meshy-server (`meshy_app`
-- without the marker), a data-only restore, a test seeding history. A row admitted that way
-- is held to NOTHING at the database (014 returns before any check) and is only classified
-- afterwards, advisorily (shared_memory_legacy_verdict). Design 4.1 kept the admission while
-- the upstream stack could still write this database.
--
-- WHY NOW. Since the promotion (2026-09-27 11:24:01Z) production is this build alone; the
-- upstream daemon and meshy-server are stopped and are not a way back. Divergence 051 records
-- the read-only measurement that every row inserted since carries its bytes.
--
-- WHAT THIS DOES. A second BEFORE INSERT row trigger with `WHEN (NEW.signed_bytes IS NULL)`,
-- whose function refuses: check_violation, `E_SIGNED_BYTES_REQUIRED: ...`, the code A1
-- uses and the convention sql_meshychat/011 set. A BEFORE trigger fires for the owner role
-- and a superuser too (RLS bypass does not skip it), so it binds every inserter.
--
-- WHY A SECOND TRIGGER, NOT 014'S FUNCTION REDEFINED. The design says "the trigger's NULL
-- admission removed for every role". Redefining shared_memory_signed_bytes_agree() would
-- restate its ~120 lines here and again in the down script, three copies to drift. A row
-- reaches the table only if every BEFORE trigger admits it, and the only thing the
-- agree-trigger admits that this one refuses is a NULL signed_bytes, so the effect is the
-- design's. Rows WITH bytes are untouched: the WHEN is false for them, and 014 holds them
-- exactly as before. The function checks the NULL itself as well, so it is correct even if
-- someone re-creates the trigger without the WHEN.
--
-- THE WHEN CLAUSE ALSO PINS THE COLUMN. Postgres records a trigger's WHEN columns in
-- pg_depend, so `ALTER TABLE shared_memory DROP COLUMN signed_bytes` (014's down script)
-- refuses while this trigger exists and nothing is dropped. That is design 4.3's order,
-- enforced: A2's down script first, then A1's. Without the pin, 014's down would drop the
-- column and leave this function naming it, and every later insert would fail on a missing
-- field instead of the rollback refusing up front.
--
-- WHAT IT DOES NOT DO.
--   * Rows stored without bytes before it (every row written before 014, and the upstream
--     stack's since) are not touched: INSERT only. 014's immutability function keeps
--     signed_bytes unchangeable, so they stay NULL and the verdict job keeps classifying
--     them. Retiring such a head (the is_head UPDATE a new version makes) is an UPDATE and
--     still works.
--   * It is an ordinary trigger, like 014's: a session with session_replication_role =
--     replica, `ALTER TABLE ... DISABLE TRIGGER` and `pg_restore --disable-triggers` skip it.
--     No code here sets the first; the other two need the table's owner, who can drop the
--     trigger anyway.
--
-- RESTORES (design 2.4 and 4.2). A full pg_dump/pg_restore creates triggers after the data,
-- so it loads the historical NULL rows. A --data-only restore into a schema with this
-- trigger refuses every one of them. Recovery on HQD is a FULL restore, never data-only
-- into A2.
--
-- DEPLOY. No code change comes with it: this build's only INSERT into shared_memory,
-- handle_write (apps/memoryskills/handlers.py; tests/test_signed_bytes.py test 12 is the
-- census), always stores the bytes verify_strict accepted. Back: the down script,
-- sql_meshychat/down/025_shared_memory_signed_bytes_required_down.sql, BEFORE any code
-- rollback past A1: code without the bytes fails every write while this is live.
--
-- A BOUNDED WAIT FOR ITS LOCK, as 014 and 020-024 have. CREATE TRIGGER takes SHARE ROW
-- EXCLUSIVE on shared_memory, which conflicts with every write; queued behind a long
-- transaction it would hold every later write behind itself. Give up after 5 s; retry.

SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE FUNCTION shared_memory_signed_bytes_required() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.signed_bytes IS NULL THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_REQUIRED: every insert must store the signed bytes of %/% (divergence 051, B02 A2)',
            NEW.namespace, NEW.key USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_signed_bytes_required_trg ON shared_memory;
CREATE TRIGGER shared_memory_signed_bytes_required_trg
    BEFORE INSERT ON shared_memory
    FOR EACH ROW
    WHEN (NEW.signed_bytes IS NULL)
    EXECUTE FUNCTION shared_memory_signed_bytes_required();

COMMENT ON TRIGGER shared_memory_signed_bytes_required_trg ON shared_memory IS
  'B02 phase A2 (divergence 051): refuses a row without signed_bytes from every session. '
  'Its WHEN pins the column, so sql_meshychat/down/014 refuses until '
  'sql_meshychat/down/025 has run.';
