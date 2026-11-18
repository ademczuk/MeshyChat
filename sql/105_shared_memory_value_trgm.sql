-- 105: pg_trgm plus the value-only text extraction the trigram index (106)
-- reads. Split out of the index build itself because CONCURRENTLY refuses to
-- run in ANY transaction, explicit or implicit, and this file needs to run in
-- one (see migration 106's header for why the two cannot share a file).
--
-- WHY A TRIGRAM INDEX OVER THE DOCUMENT AND NOT A TSVECTOR. Both were measured
-- on a scratch copy of the live 31,777-row / 144 MB table (2026-08-22), and the
-- decision is the recall column, not the speed column:
--
--   shape                                   attest  sxxxxxxxx  supersedes  recall
--   pre-#514 enumerated ILIKE (today)        891ms     741ms       719ms   baseline
--   jsonb_to_tsvector OR'd with the ILIKEs  ~6000ms       -           -    mixed
--   trgm over value::text (first attempt)    587ms     676ms       244ms   STRICT SUPERSET, but see below
--   tsvector + trgm(title), word matching     18ms      21ms         3ms   loses substring
--
-- The tsvector shape is 30x faster and was the first attempt, but it changes
-- the contract from substring to word matching: searching "attes" or a partial
-- key stops matching, and measured recall DROPPED on one of four probe terms
-- (1664 against 1966). A dashboard search box is typed in fragments, so the
-- shape that preserves substring semantics wins even at higher latency.
--
-- FIX (#526 review): trgm over `value::text` matches JSON KEY NAMES, not just
-- values. `value::text` is the raw serialized document, so a query like
-- `q=title` or `q=body_md` matches every row that HAS a `title` or `body_md`
-- key, whether or not any stored value contains that word. Measured on the
-- live 3,005-head-row local dev table (2026-08-23, a much smaller corpus than
-- the 31,777-row scratch copy above, so treat these as directional, not a
-- restatement of the production numbers): searching "title" against raw text
-- returned 126 rows versus 35 once key names are excluded; "sources" returned
-- 173 versus 69; "body_md" returned 129 versus 17. With the search endpoint's
-- default 50-row limit ordered by recency, the false positives from the key
-- name alone can fill the page and bury the real matches.
--
-- The fix indexes `meshy_jsonb_values_text(value)` instead of `value::text`:
-- a recursive walk (via `jsonb_path_query` with a scalar-type filter) that
-- concatenates only string/number/boolean VALUES at any depth, never a key.
-- This keeps the "no enumerated field list" property of the original fix
-- (any field, any depth, no name to maintain) while dropping the key-name
-- false positives. Measured on the same local table: for the exact terms the
-- finding named, the new predicate is comparable or FASTER than the old one
-- (title: ~50ms vs ~100ms; body_md: ~75ms vs ~223ms; sources: ~135ms vs
-- ~135ms) because narrowing to true value hits at the index level shrinks the
-- candidate set more than the per-row extraction cost adds back. For a plain
-- content word that is not also a key name (e.g. "mesh"), the extraction cost
-- is not offset by a smaller candidate set, and the new predicate measured
-- roughly 1.5-1.8x slower (about 50-100ms absolute on this 13,504-row local
-- table). The index remains usable in both cases (Bitmap Index Scan, not a
-- sequential scan); this is a real but bounded per-row cost, not a fallback to
-- an unindexed scan, and it has not been re-measured against the production
-- scratch copy.
--
-- The tsvector option is measured and available if the fleet later decides
-- word semantics are acceptable; the numbers above are the whole basis for
-- choosing.
--
-- WHY THIS FILE IS SPLIT FROM THE INDEX BUILD. Discovered while RED-proving
-- the #526 fix: `#[sqlx::test]` (and production boot, `sqlx::migrate!(...)
-- .run(pool)`, same code path) applies each migration's SQL text as ONE
-- `conn.execute()` call. With no bind parameters, sqlx sends it over the
-- Postgres SIMPLE QUERY protocol, and Postgres wraps EVERY multi-statement
-- simple-query message in an implicit transaction, regardless of the
-- migration's own `no_tx` flag on the sqlx side; `no_tx` only controls
-- whether sqlx additionally wraps the call in an explicit `BEGIN`/`COMMIT`,
-- it cannot undo Postgres's own protocol-level wrapping of a multi-statement
-- batch. `CREATE INDEX CONCURRENTLY` refuses to run inside ANY transaction,
-- implicit included, so the original single-file version of this migration
-- (`SET lock_timeout`, `CREATE EXTENSION`, `CREATE INDEX CONCURRENTLY` all in
-- one `-- no-transaction` file) failed every time with "CREATE INDEX
-- CONCURRENTLY cannot run inside a transaction block" - not a corner case,
-- reproduced on every migration run, including the first one after this
-- comment was written. Migrations 100 and 101 already establish the working
-- split: 100 is an ordinary transactional migration with `SET lock_timeout`
-- alongside its `ALTER TABLE`/`CREATE INDEX` statements, and 101's
-- `-- no-transaction` file contains NOTHING but the bare `CREATE INDEX
-- CONCURRENTLY` statement. This migration follows 100's half of that pattern
-- (transactional, sets up the extension and the function), and migration 106
-- follows 101's half (the CONCURRENTLY build, alone). `SET lock_timeout` set
-- here as a plain (non-LOCAL) session setting survives past this migration's
-- commit for the rest of the session, so it is still in effect when migration
-- 106 runs on the same connection, matching this migration's own file header
-- intent below.
SET lock_timeout = '5s';

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Recursive value-only extraction: concatenates every scalar (string, number,
-- boolean) reachable from `doc` at any depth, and nothing else. Object and
-- array keys are never emitted, so a field name like `title` or `sources`
-- cannot appear in the output unless it is also somebody's stored value.
-- IMMUTABLE is required for use in an expression index; it holds here because
-- the function is a pure, deterministic walk of its jsonb input.
CREATE OR REPLACE FUNCTION meshy_jsonb_values_text(doc jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT string_agg(v::text, ' ')
    FROM jsonb_path_query(
        COALESCE(doc, 'null'::jsonb),
        '$.**?(@.type() == "string" || @.type() == "number" || @.type() == "boolean")'
    ) AS v
$$;
