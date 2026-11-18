-- Index the declared-act projection so asking "what is the state of X" is bounded.
--
-- WHY THIS IS A DIVERGENCE. Upstream has no board reader, so it never needed to look a
-- mesh row up by `subject`. `docs/DIVERGENCES.md` D-003 carries the measurement.
--
-- WHY PARTIAL. A declared act is a mesh row carrying `subject`. Measured on the 300 most
-- recent production rows when the board spec was written, 1 of 300 carried the pair, so a
-- full index over shared_memory would be ~300x larger than the rows anyone queries. The
-- partial predicate matches the definition of the thing: a row without `subject` is a
-- message, not an act, and the projection never reads it.
--
-- WHY created_at IS IN THE INDEX AND value->>'ts' IS NOT. `ts` is a CLIENT-SUPPLIED
-- envelope field. It is validated for SHAPE by MESH_TIMESTAMP_RE and not for truth, and
-- sender clocks on this fleet have been measured future-dated by up to 67 minutes.
-- `created_at` is assigned by the database that is also doing the ordering. Ordering a
-- board by the actor's own clock lets an actor reorder history by setting it.
--
-- B5: with this index, `state_for(subject)` is an index scan over one subject's acts
-- rather than a scan of the namespace. tests/test_board_projection.py asserts that it does
-- not scale with the size of the namespace around it.

CREATE INDEX IF NOT EXISTS shared_memory_declared_act_idx
    ON shared_memory (namespace, (value ->> 'subject'), created_at)
    WHERE value ? 'subject';

COMMENT ON INDEX shared_memory_declared_act_idx IS
    'Declared-act projection (apps/board). Partial: only rows carrying a subject are acts.';
