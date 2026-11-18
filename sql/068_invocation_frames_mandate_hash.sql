-- 067_invocation_frames_mandate_hash.sql
--
-- TASK-29 AC#3 (record-only audit, kxxx's seam half): record the human-intent
-- mandate reference on each invocation. When a grant was issued under a signed
-- IntentMandate (migration 052 stored its mandate_hash on skill_grants), stamp
-- that hash (lowercase hex) onto the invocation_frame the authorize gate writes,
-- so the tamper-evident audit chain is execution -> grant -> mandate. This feeds
-- TASK-29 AC#4 + the TASK-8 AC#6 receipt provenance chain.
--
-- Enforcement of the mandate's scope + time bounds is TRANSITIVE, not re-checked
-- here: AC#2 proved the grant is a SUBSET of the mandate at issuance, and
-- update_grant now keeps a mandate-bound grant's expiry monotonically
-- NON-INCREASING (dxxxxx, bb5c3e02 -- reject extend / clear-to-infinite, allow
-- shrink), so grant.expiry <= issuance expiry <= mandate.expiry holds forever and
-- the existing grant-liveness gate at invoke is sufficient. This column is the
-- AUDIT of that binding, not a second enforcement point (the mandate is neither
-- carried nor stored at invoke, so M cannot be re-derived there).
--
-- NULL for a grant-free decision (same-tenant allow, gate-1 / grant-miss deny) or
-- a mandate-less grant -- behavior-identical to pre-mandate, like skill_grants.
ALTER TABLE invocation_frames ADD COLUMN mandate_hash TEXT;

-- mandate_hash is part of frame_hash, so an INSERT-capable actor that hashed over
-- a malformed value would produce a self-consistent frame. Constrain the format
-- (lowercase 64-hex = a 32-byte sha256, the shape grant_mandate_hash emits) so a
-- reader can trust a present value is a canonical mandate reference (mirrors the
-- 052 octet_length CHECK on the source BYTEA + 028's hex CHECK precedent).
ALTER TABLE invocation_frames
    ADD CONSTRAINT invocation_frames_mandate_hash_fmt
    CHECK (mandate_hash IS NULL OR mandate_hash ~ '^[0-9a-f]{64}$');
