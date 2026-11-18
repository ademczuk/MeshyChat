-- Bind refresh tokens to the key id that issued them. Nullable preserves
-- legacy rows that cannot be backfilled; new rows always set the column.

ALTER TABLE refresh_tokens
    ADD COLUMN issued_under_kid TEXT;

UPDATE refresh_tokens
SET issued_under_kid = (
    SELECT pk.kid
    FROM peer_keys pk
    WHERE pk.peer_id = refresh_tokens.peer_id
      AND pk.active = TRUE
      AND pk.revoked_at IS NULL
    ORDER BY pk.created_at DESC
    LIMIT 1
)
WHERE issued_under_kid IS NULL;
