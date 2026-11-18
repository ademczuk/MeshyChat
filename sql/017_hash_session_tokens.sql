-- 017: hash BFF session tokens.
--
-- Migration 015 used owner_sessions.id (the row PK) as the cookie value and
-- compared it in plaintext on every request. A read of owner_sessions (a
-- SQL-injection, a leaked backup, a read replica) therefore exposed every live
-- session token verbatim, letting the reader impersonate any signed-in owner.
--
-- Fix: the cookie now carries a high-entropy random token; the database stores
-- ONLY its SHA-256 hash. A DB read no longer yields anything usable as a cookie.
-- The raw token exists only in the client cookie; lookups hash the presented
-- token and match on token_hash.
--
-- No backfill: pre-existing rows (none in the not-yet-deployed Rust stack) have
-- a NULL token_hash and simply stop validating, forcing a fresh sign-in. The
-- column is nullable so the ALTER is safe on a populated table; application code
-- always sets it on insert, and the partial-friendly UNIQUE index permits the
-- (at most legacy) NULLs while guaranteeing uniqueness of real hashes.
ALTER TABLE owner_sessions ADD COLUMN token_hash TEXT;

CREATE UNIQUE INDEX owner_sessions_token_hash_idx ON owner_sessions (token_hash);
