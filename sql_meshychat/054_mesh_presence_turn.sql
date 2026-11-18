-- Step B: advisory current turn, no history and no presence beat/NOTIFY side effects.
ALTER TABLE mesh_presence
    ADD COLUMN turn_state TEXT CHECK (turn_state IN ('open', 'closed')),
    ADD COLUMN turn_item_at TIMESTAMPTZ,
    ADD COLUMN turn_reported_at TIMESTAMPTZ;
