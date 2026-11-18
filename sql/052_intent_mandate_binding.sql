-- 052_intent_mandate_binding.sql
-- TASK-29: bind a grant to a signed human-intent mandate.
--
-- When a grant is issued under a human-intent mandate (the owner-signed
-- IntentMandate from meshy-grants/src/intent.rs), the issuer verifies the grant
-- is a SUBSET of the mandate (mandate_authorizes rejects anything broader) and
-- records the mandate's hash + signature as a provenance reference. The full
-- mandate is NOT stored: the issue-time subset check already bounds the grant,
-- the live skill_grants row stays authoritative for the grant's own bounds
-- (gate 2), and the hash is the reference a TASK-8 receipt carries.
--
-- Both columns are nullable: a grant issued WITHOUT a mandate (the common case
-- today) leaves them NULL, behavior-identical to pre-mandate issuance. This is an
-- additive, backward-compatible migration - the 19 live grants are all
-- mandate-less and admit NULL trivially.
--
--   mandate_hash : sha256 of the RFC-8785 JCS-canonical mandate (32 bytes).
--   mandate_sig  : the owner's Ed25519 signature over the mandate (64 bytes).

ALTER TABLE skill_grants
    ADD COLUMN mandate_hash BYTEA NULL,
    ADD COLUMN mandate_sig BYTEA NULL;

-- Length guards: when present, the hash is exactly 32 bytes and the signature 64.
-- A malformed-length value can never be written, so a reader can trust the sizes.
ALTER TABLE skill_grants
    ADD CONSTRAINT skill_grants_mandate_hash_len
        CHECK (mandate_hash IS NULL OR octet_length(mandate_hash) = 32),
    ADD CONSTRAINT skill_grants_mandate_sig_len
        CHECK (mandate_sig IS NULL OR octet_length(mandate_sig) = 64);
