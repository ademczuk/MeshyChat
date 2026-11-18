SET LOCAL lock_timeout = '5s';

-- A redacted decision card keeps the fields that say WHAT IT REPLACED (divergence 092).
--
-- A card is superseded when a later head card names its key in `supersedes` (divergence 091,
-- apps/cards/store.py). A tombstone kept none of `supersedes`, `time`, `seat`, `id` or `schema`,
-- so redacting the superseding card (for a credential, say) brought the ruling it had replaced
-- back as the current one, in every seat's default read, with nothing saying so.
--
-- These five are identifiers and pointers, as `in_reply_to` and `closes` are for a mail row: a
-- label, a hex id, an instant, a schema name and a list of card keys. No prose. The handler keeps
-- them for a card key of the card namespace only (apps/memoryskills/redact.CARD_FIELDS); this
-- function is the one list the trigger checks for every namespace, so it PERMITS them and
-- requires nothing. tests/test_redaction_db_rule.py pins this list against the handler's.
CREATE OR REPLACE FUNCTION shared_memory_tombstone_keys() RETURNS text[]
    LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['redacted', 'reason_code', 'redacted_at',
                 'schema_version', 'from', 'to', 'cc', 'type', 'thread_id', 'in_reply_to', 'closes', 'requires_ack', 'ts', 'author_family',
                 'schema', 'id', 'seat', 'time', 'supersedes']
$$;
