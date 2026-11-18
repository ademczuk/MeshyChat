-- 107: preserve the CANONICAL signed payload of a skill.result.
--
-- #596. Ingress verifies the signature over the FULL wire envelope, then
-- persists only the extracted `result` field plus the detached signature and
-- kid. Egress rebuilds a SignedEnvelope whose payload is that extracted field
-- with the ORIGINAL signature attached. The signature was computed over a
-- strictly larger object (spec 8.2: result, status, error_code, error_message,
-- plus signing metadata), so the delivered payload is not the payload the
-- signature covers and any consumer that verifies it fails.
--
-- Verified at source by four seats before this landed: sxxxxxxxx's review,
-- nxxxxx's trace at ffdb5ddd, the merge seat at origin/main, and a fixture read
-- showing PR 530's adapter checked only that a signature FIELD was present.
--
-- NULLABLE and ADDITIVE on purpose. Every row written before this migration has
-- no canonical payload and never will; there is nothing to backfill from, since
-- the bytes were discarded at ingress. A NOT NULL column would therefore need a
-- fabricated default, and a fabricated canonical payload is worse than an absent
-- one: it would verify against nothing while looking authoritative. NULL here
-- means "pre-fix row, reconstruction is the best available", which egress
-- reports with an explicit marker rather than leaving the caller to infer it.
--
-- No index. The column is read only on the terminal-delivery path, which already
-- has the row by primary key.

ALTER TABLE skill_calls ADD COLUMN IF NOT EXISTS result_payload jsonb;

COMMENT ON COLUMN skill_calls.result_payload IS
  'The canonical signed payload of the skill.result envelope, stored verbatim at '
  'ingress (#596). NULL for rows written before migration 107: those are delivered '
  'with a reconstructed payload and an explicit payload_reconstructed marker, '
  'because the original bytes were discarded and cannot be recovered.';
