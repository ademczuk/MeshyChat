-- MeshyChat divergence 023: the embed job counts the rows it skips for a missing
-- schema_version, instead of leaving them in `pending` forever.
--
-- WHAT WAS WRONG. The job (apps/meshsem/embedjob.py) walks every head msg/ row of
-- shared/mesh-chat and skips one with no string body as `skipped_empty`. Coverage, which
-- every /v1/mesh/search and /v1/mesh/context response carries, counted only the
-- embed_policy skips, so a skipped row was reported as PENDING: work to come that never
-- came, with no field saying why. Under the v1 validator (divergence 011) a message with
-- schema_version always has a non-blank body, so the rows skipped this way are the
-- LEGACY rows the validator never saw: 215 of the 300 newest production rows carried no
-- schema_version (measured 2026-09-21), and 1,655 rows in the namespace carry no body at
-- all (cursors and state rows, apps/mesh/normalize.py).
--
-- WHAT THIS ADDS. The per-walk tally beside rows_skipped_forbidden and
-- rows_skipped_empty. Coverage itself is computed from shared_memory at read time with
-- the SAME predicate the job decides by (apps/meshsem/store.py HAS_TEXT_SQL), so the two
-- cannot disagree; this column is the job's own running count, reset with the walk.
--
-- WHAT IT DOES NOT DO: skip legacy rows that DO carry a body. Those are embedded exactly
-- as before; missing schema_version is the reason recorded for a row with nothing to
-- embed, not a reason to refuse one that has text.

ALTER TABLE mesh_embeddings_projection
    ADD COLUMN IF NOT EXISTS rows_skipped_no_schema_version BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN mesh_embeddings_projection.rows_skipped_no_schema_version IS
    'Rows this walk skipped because they carry no schema_version and no string body (legacy rows the v1 validator never saw). Counted in coverage as skipped_no_schema_version, never as pending (divergence 023).';
